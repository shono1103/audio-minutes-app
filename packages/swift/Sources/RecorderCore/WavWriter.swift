import AVFoundation
import CryptoKit
import Foundation

/// 16 kHz mono PCM16 の WAV をストリーミングで書く。停止時に header を確定し sha256 を返す。
public final class WavWriter: @unchecked Sendable {
    public static let targetSampleRate: Double = 16000
    public let url: URL
    private let handle: FileHandle
    private var dataBytes: Int = 0
    private var finalized = false
    private let lock = NSLock()
    public private(set) var framesWritten: Int = 0

    public init(url: URL) throws {
        self.url = url
        FileManager.default.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600])
        handle = try FileHandle(forWritingTo: url)
        try handle.write(contentsOf: Self.header(dataBytes: 0))
    }

    /// PCM16 little-endian mono サンプルを追記する。
    public func append(pcm16: Data) throws {
        lock.lock(); defer { lock.unlock() }
        guard !finalized else { throw WavError.alreadyFinalized }
        try handle.write(contentsOf: pcm16)
        dataBytes += pcm16.count
        framesWritten += pcm16.count / 2
    }

    public func append(buffer: AVAudioPCMBuffer) throws {
        guard let channel = buffer.int16ChannelData?[0] else { return }
        let count = Int(buffer.frameLength)
        let data = Data(bytes: channel, count: count * 2)
        try append(pcm16: data)
    }

    /// header を確定して閉じる。ファイル全体の sha256 とサイズを返す。
    public func finalize() throws -> (sha256: String, byteSize: Int, durationMs: Int) {
        lock.lock(); defer { lock.unlock() }
        guard !finalized else { throw WavError.alreadyFinalized }
        try handle.seek(toOffset: 0)
        try handle.write(contentsOf: Self.header(dataBytes: dataBytes))
        try handle.synchronize()
        // 読取hashを先に終えることで、hash失敗時に書込handleを閉じず再試行可能にする。
        let digest = try Self.sha256(of: url)
        try handle.close()
        let durationMs = Int((Double(framesWritten) * 1000 / Self.targetSampleRate).rounded())
        // IO が全て成功するまで確定済みにしない。途中失敗時は呼び出し側が再試行できる。
        finalized = true
        return (digest, 44 + dataBytes, durationMs)
    }

    public var durationMs: Int { Int((Double(framesWritten) * 1000 / Self.targetSampleRate).rounded()) }

    static func header(dataBytes: Int) -> Data {
        var data = Data()
        func append32(_ value: UInt32) { data.append(contentsOf: withUnsafeBytes(of: value.littleEndian) { Array($0) }) }
        func append16(_ value: UInt16) { data.append(contentsOf: withUnsafeBytes(of: value.littleEndian) { Array($0) }) }
        data.append(contentsOf: Array("RIFF".utf8))
        append32(UInt32(36 + dataBytes))
        data.append(contentsOf: Array("WAVE".utf8))
        data.append(contentsOf: Array("fmt ".utf8))
        append32(16)
        append16(1)                                   // PCM
        append16(1)                                   // mono
        append32(UInt32(targetSampleRate))
        append32(UInt32(targetSampleRate) * 2)        // byte rate
        append16(2)                                   // block align
        append16(16)                                  // bits
        data.append(contentsOf: Array("data".utf8))
        append32(UInt32(dataBytes))
        return data
    }

    public static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1024 * 1024), !chunk.isEmpty { hasher.update(data: chunk) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}

public enum WavError: Error, LocalizedError {
    case alreadyFinalized
    case converterUnavailable
    case conversionFailed(String)
    public var errorDescription: String? {
        switch self {
        case .alreadyFinalized: return "WAV は既に確定しています"
        case .converterUnavailable: return "音声変換器を作成できません"
        case .conversionFailed(let detail): return "音声変換に失敗しました: \(detail)"
        }
    }
}

/// 任意フォーマットの PCM を 16 kHz mono PCM16 へ変換して WavWriter へ流す。
public final class PCMDownmixer: @unchecked Sendable {
    private let converter: AVAudioConverter
    private let outputFormat: AVAudioFormat
    private let inputFormat: AVAudioFormat

    public init(inputFormat: AVAudioFormat) throws {
        guard let output = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: WavWriter.targetSampleRate, channels: 1, interleaved: true),
              let converter = AVAudioConverter(from: inputFormat, to: output) else {
            throw WavError.converterUnavailable
        }
        self.inputFormat = inputFormat
        self.outputFormat = output
        self.converter = converter
    }

    public func convert(_ input: AVAudioPCMBuffer) throws -> AVAudioPCMBuffer {
        let ratio = outputFormat.sampleRate / inputFormat.sampleRate
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * ratio)) + 16
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            throw WavError.conversionFailed("出力bufferを確保できません")
        }
        var consumed = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, outStatus in
            if consumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return input
        }
        guard status != .error, output.frameLength > 0 else {
            throw WavError.conversionFailed(error?.localizedDescription ?? "変換結果が空です")
        }
        return output
    }

    /// 入力レベル (RMS、0.0〜1.0)。GUI のレベルメーター用。全チャンネル・全フレームを
    /// `stride` (interleaved ならチャンネル数、non-interleaved なら 1) に従って読む。
    public static func rmsLevel(_ buffer: AVAudioPCMBuffer) -> Float {
        let frames = Int(buffer.frameLength)
        let channels = Int(buffer.format.channelCount)
        let stride = Int(buffer.stride)
        guard frames > 0, channels > 0, stride > 0 else { return 0 }
        var sum: Double = 0
        var sampleCount = 0
        func accumulate(_ value: Double) {
            guard value.isFinite else { return }
            sum += value * value
            sampleCount += 1
        }
        if let floats = buffer.floatChannelData {
            for channel in 0..<channels {
                let data = floats[channel]
                for frame in 0..<frames { accumulate(Double(data[frame * stride])) }
            }
        } else if let ints16 = buffer.int16ChannelData {
            for channel in 0..<channels {
                let data = ints16[channel]
                for frame in 0..<frames { accumulate(Double(data[frame * stride]) / 32768) }
            }
        } else if let ints32 = buffer.int32ChannelData {
            for channel in 0..<channels {
                let data = ints32[channel]
                for frame in 0..<frames { accumulate(Double(data[frame * stride]) / 2147483648) }
            }
        } else {
            return 0
        }
        guard sampleCount > 0 else { return 0 }
        let rms = (sum / Double(sampleCount)).squareRoot()
        guard rms.isFinite else { return 0 }
        return Float(min(1, max(0, rms)))
    }
}
