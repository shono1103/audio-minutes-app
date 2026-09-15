import AVFoundation
import Foundation

/// Chrome 連携拡張 → NativeBridge → GUI へ届く PCM chunk を、GUI 所有の track として受ける境界 (FR-089、FR-099)。
/// 連番の欠落・サイズ超過・切断時は停止して取得済みを保持する。音声は外部へ出さない。
public struct BridgeChunk: Sendable, Equatable {
    public let seq: Int
    public let ptsMs: Int
    public let pcm16le: Data
    public init(seq: Int, ptsMs: Int, pcm16le: Data) {
        self.seq = seq
        self.ptsMs = ptsMs
        self.pcm16le = pcm16le
    }
}

public enum BridgeError: Error, LocalizedError, Sendable, Equatable {
    case sequenceGap(expected: Int, got: Int)
    case chunkTooLarge(Int)
    case notCapturing
    case disconnected
    case invalidFormat

    public var errorDescription: String? {
        switch self {
        case .sequenceGap(let expected, let got): return "音声 chunk の連番が飛びました (期待 \(expected)、受信 \(got))"
        case .chunkTooLarge(let size): return "音声 chunk が大きすぎます (\(size) bytes)"
        case .notCapturing: return "Chrome タブの取得が開始されていません"
        case .disconnected: return "Chrome 連携が切断されました。録音を停止し、取得済みの音声を保持します"
        case .invalidFormat: return "Chrome 連携の音声形式が不正です"
        }
    }
}

public final class ChromeTabTrackSink: @unchecked Sendable {
    public static let maxChunkBytes = 1_048_576
    public static let maxBufferedChunks = 64

    private weak var sink: AudioChunkSink?
    private var format: AVAudioFormat?
    private var expectedSeq = 0
    private var startHostTime: UInt64 = 0
    private let lock = NSLock()
    public private(set) var isCapturing = false
    public private(set) var receivedChunks = 0
    public var onFailure: (@Sendable (BridgeError) -> Void)?

    public init() {}

    func attach(sink: AudioChunkSink) { lock.withLock { self.sink = sink } }
    func detach() { lock.withLock { sink = nil; isCapturing = false } }

    /// `capture_started`。
    public func captureStarted(sampleRate: Double, channels: Int) throws {
        guard sampleRate >= 8000, (1...2).contains(channels),
              let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: AVAudioChannelCount(channels), interleaved: true) else {
            throw BridgeError.invalidFormat
        }
        lock.withLock {
            self.format = format
            expectedSeq = 0
            receivedChunks = 0
            startHostTime = mach_absolute_time()
            isCapturing = true
        }
    }

    /// `chunk`。連番とサイズを検証し、track へ流す。
    public func receive(_ chunk: BridgeChunk) throws {
        let (format, sink): (AVAudioFormat?, AudioChunkSink?) = lock.withLock { (self.format, self.sink) }
        guard isCapturing, let format else { throw BridgeError.notCapturing }
        guard chunk.pcm16le.count <= Self.maxChunkBytes else {
            fail(.chunkTooLarge(chunk.pcm16le.count))
            throw BridgeError.chunkTooLarge(chunk.pcm16le.count)
        }
        let expected = lock.withLock { expectedSeq }
        guard chunk.seq == expected else {
            fail(.sequenceGap(expected: expected, got: chunk.seq))
            throw BridgeError.sequenceGap(expected: expected, got: chunk.seq)
        }
        lock.withLock { expectedSeq += 1; receivedChunks += 1 }
        let bytesPerFrame = Int(format.streamDescription.pointee.mBytesPerFrame)
        guard bytesPerFrame > 0 else { throw BridgeError.invalidFormat }
        let frames = AVAudioFrameCount(chunk.pcm16le.count / bytesPerFrame)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames), let target = buffer.int16ChannelData?[0] else { return }
        buffer.frameLength = frames
        chunk.pcm16le.withUnsafeBytes { raw in
            if let base = raw.baseAddress { memcpy(target, base, Int(frames) * bytesPerFrame) }
        }
        let hostTime = startHostTime + UInt64(Double(chunk.ptsMs) * 1_000_000 / ClockSync.systemNanosecondsPerTick())
        sink?.receive(buffer: buffer, hostTime: hostTime)
    }

    /// `capture_stopped` または切断。録音は停止し、取得済みを保持する。
    public func captureStopped(reason: String, notifyTargetLoss: Bool = true) {
        lock.withLock { isCapturing = false }
        if reason == "disconnected" { fail(.disconnected); return }
        if notifyTargetLoss { sink?.targetLost() }
    }

    private func fail(_ error: BridgeError) {
        lock.withLock { isCapturing = false }
        onFailure?(error)
        sink?.targetLost()
    }
}
