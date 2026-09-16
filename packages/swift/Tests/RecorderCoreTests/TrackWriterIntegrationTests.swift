import AVFoundation
import Foundation
import XCTest

@testable import RecorderCore

/// FaultWriter を使わず、実際の WavWriter/PCMDownmixer/LiveChunkWriter を通した TrackWriter の
/// 回帰テスト。onLevel の非ゼロ通知と保存音声 (header・PCM・duration・checksum) を同時に
/// 確認し、レベル通知だけが直り保存音声が壊れる退行を検出できるようにする。
final class TrackWriterIntegrationTests: XCTestCase {
    private final class LockedBox<Value>: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Value
        init(_ value: Value) { self.value = value }
        func read() -> Value { lock.withLock { value } }
        func update(_ body: (inout Value) -> Void) { lock.withLock { body(&value) } }
    }

    private func makeDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    /// 48kHz stereo Float32、AVAudioEngine の入力 tap 相当の non-interleaved buffer。
    private func stereoFloat32Buffer(seconds: Double, sampleRate: Double = 48_000, amplitude: Float = 0.5) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 2, interleaved: false)!
        let frames = Int(seconds * sampleRate)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = AVAudioFrameCount(frames)
        let data = buffer.floatChannelData!
        for frame in 0..<frames {
            let value = amplitude * Float(sin(Double(frame) * 0.05))
            data[0][frame] = value
            data[1][frame] = value
        }
        return buffer
    }

    /// ChromeTabTrackSink 相当の interleaved PCM16 buffer。
    private func interleavedInt16Buffer(seconds: Double, sampleRate: Double, channels: AVAudioChannelCount, amplitude: Int16 = 8_000) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: channels, interleaved: true)!
        let frames = Int(seconds * sampleRate)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = AVAudioFrameCount(frames)
        let stride = Int(buffer.stride)
        let data = buffer.int16ChannelData!
        for frame in 0..<frames {
            let value = Int16(Double(amplitude) * sin(Double(frame) * 0.05))
            for channel in 0..<Int(channels) { data[channel][frame * stride] = value }
        }
        return buffer
    }

    // MARK: - WAV 実データ検証
    //
    // WavWriter は常に固定44byteのRIFF/WAVE/fmt/dataヘッダーを書く (WavWriter.header 参照)。
    // ファイル全体に非ゼロbyteがあるかどうかの判定は、PCMが全て無音でもRIFF/WAVEの文字列
    // 部分で true になってしまい保存音声の破損を検出できない。ここではheaderをスキップして
    // 実際のPCM16サンプルをdecodeし、その値・RMSとfmtチャンクの形式を検証する。

    private enum WavInspectionError: Error { case tooShort }

    private struct WavInspection {
        var audioFormat: UInt16
        var channels: UInt16
        var sampleRate: UInt32
        var bitsPerSample: UInt16
        var dataByteCount: UInt32
        var pcm16: [Int16]
    }

    private func readUInt16LE(_ bytes: [UInt8], _ offset: Int) -> UInt16 {
        UInt16(bytes[offset]) | (UInt16(bytes[offset + 1]) << 8)
    }

    private func readUInt32LE(_ bytes: [UInt8], _ offset: Int) -> UInt32 {
        UInt32(bytes[offset]) | (UInt32(bytes[offset + 1]) << 8)
            | (UInt32(bytes[offset + 2]) << 16) | (UInt32(bytes[offset + 3]) << 24)
    }

    private func inspectWav(_ url: URL) throws -> WavInspection {
        let bytes = [UInt8](try Data(contentsOf: url))
        guard bytes.count >= 44 else { throw WavInspectionError.tooShort }
        var pcm16: [Int16] = []
        pcm16.reserveCapacity((bytes.count - 44) / 2)
        var offset = 44
        while offset + 1 < bytes.count {
            pcm16.append(Int16(bitPattern: readUInt16LE(bytes, offset)))
            offset += 2
        }
        return WavInspection(
            audioFormat: readUInt16LE(bytes, 20), channels: readUInt16LE(bytes, 22),
            sampleRate: readUInt32LE(bytes, 24), bitsPerSample: readUInt16LE(bytes, 34),
            dataByteCount: readUInt32LE(bytes, 40), pcm16: pcm16
        )
    }

    private func rms(of samples: [Int16]) -> Double {
        guard !samples.isEmpty else { return 0 }
        let sum = samples.reduce(0.0) { $0 + Double($1) * Double($1) }
        return (sum / Double(samples.count)).squareRoot()
    }

    func testStereoFloat32AppAudioProducesNonZeroLevelAndValidWav() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("app-audio.wav")
        let track = try TrackWriter(trackID: .appAudio, role: .app, url: url)
        let levels = LockedBox<[Float]>([])
        track.onLevel = { level in levels.update { $0.append(level) } }

        track.receive(buffer: stereoFloat32Buffer(seconds: 1.0), hostTime: 1)

        XCTAssertEqual(levels.read().count, 1)
        XCTAssertGreaterThan(levels.read().first ?? 0, 0, "非ゼロの入力レベルが通知される")

        let result = try track.finalize()
        // 48kHz→16kHz のresampleはconverterの初期遅延 (priming) により出力フレーム数が
        // 比率どおりの16,000にはならない。正確な一致ではなく許容差で確認する。
        XCTAssertLessThanOrEqual(abs(result.durationMs - 1_000), 150, "48kHz→16kHzのresample遅延を考慮した許容差")
        XCTAssertGreaterThan(result.byteSize, 44, "PCMデータが書き込まれている")
        let framesWritten = (result.byteSize - 44) / 2
        XCTAssertLessThanOrEqual(abs(Int((Double(framesWritten) * 1000 / 16_000).rounded()) - result.durationMs), 1, "byteSizeとdurationMsは同じframe数から一貫して算出される")

        let data = try Data(contentsOf: url)
        XCTAssertEqual(String(decoding: data.prefix(4), as: UTF8.self), "RIFF")
        XCTAssertEqual(String(decoding: data[8..<12], as: UTF8.self), "WAVE")
        XCTAssertEqual(try WavWriter.sha256(of: url), result.sha256)

        let inspection = try inspectWav(url)
        XCTAssertEqual(inspection.audioFormat, 1, "PCM (非圧縮)")
        XCTAssertEqual(inspection.channels, 1)
        XCTAssertEqual(inspection.sampleRate, 16_000)
        XCTAssertEqual(inspection.bitsPerSample, 16)
        XCTAssertEqual(inspection.dataByteCount, UInt32(result.byteSize - 44))
        XCTAssertEqual(inspection.pcm16.count, framesWritten)
        XCTAssertGreaterThan(rms(of: inspection.pcm16), 0, "変換後の保存 PCM サンプル自体が無音のままではいけない")
    }

    func testChromeLikeMonoInt16ProducesNonZeroLevelAndValidWav() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("app-audio.wav")
        let track = try TrackWriter(trackID: .appAudio, role: .app, url: url)
        let levels = LockedBox<[Float]>([])
        track.onLevel = { level in levels.update { $0.append(level) } }

        track.receive(buffer: interleavedInt16Buffer(seconds: 1.0, sampleRate: 16_000, channels: 1), hostTime: 1)

        XCTAssertEqual(levels.read().count, 1)
        XCTAssertGreaterThan(levels.read().first ?? 0, 0, "非ゼロの入力レベルが通知される")

        let result = try track.finalize()
        XCTAssertEqual(result.durationMs, 1_000)
        XCTAssertEqual(try WavWriter.sha256(of: url), result.sha256)

        let inspection = try inspectWav(url)
        XCTAssertEqual(inspection.audioFormat, 1)
        XCTAssertEqual(inspection.channels, 1)
        XCTAssertEqual(inspection.sampleRate, 16_000)
        XCTAssertEqual(inspection.bitsPerSample, 16)
        XCTAssertEqual(inspection.dataByteCount, UInt32(result.byteSize - 44))
        XCTAssertEqual(inspection.pcm16.count, 16_000)
        XCTAssertGreaterThan(rms(of: inspection.pcm16), 0, "変換後の保存 PCM サンプル自体が無音のままではいけない")
    }

    /// containsNonZeroByte 相当の粗い判定 (ファイル全体に非ゼロbyteがあるか) では、PCMが
    /// 全て無音でもRIFF/WAVEヘッダー分で true になってしまい判別できない。実際にPCM
    /// サンプルをdecodeしたRMSでは、無音入力なら0になることを確認し、有音入力での
    /// 非ゼロ判定 (上記2テスト) と対照させる。
    func testSilentInputProducesZeroRmsInSavedPcm() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("app-audio.wav")
        let track = try TrackWriter(trackID: .appAudio, role: .app, url: url)

        track.receive(buffer: interleavedInt16Buffer(seconds: 1.0, sampleRate: 16_000, channels: 1, amplitude: 0), hostTime: 1)
        _ = try track.finalize()

        let inspection = try inspectWav(url)
        XCTAssertFalse(inspection.pcm16.isEmpty)
        XCTAssertEqual(rms(of: inspection.pcm16), 0, "無音入力を保存したPCMのRMSは0でなければならない")
    }

    /// 31秒分の音声を TrackWriter に通し、完全 WAV と 30秒+残り2秒 (1秒 overlap のため
    /// 29秒刻み) の live chunks の両方が確定することを確認する。入力を変換先と同じ
    /// 16kHz mono PCM16 にすることで PCMDownmixer は恒等変換に近くなり、既存の
    /// `LiveChunkWriter` 単体テストが検証している境界値 (`startOffsetMs`/`durationMs`) を
    /// TrackWriter 経由でも再現できる。
    func testTrackWriterFinalizesFullWavAndLiveChunksForLongRecording() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let sessionID = UUID()
        let chunkDirectory = directory.appendingPathComponent("chunks", isDirectory: true)
        let chunks = LockedBox<[LiveAudioChunk]>([])
        let track = try TrackWriter(
            trackID: .appAudio, role: .app, url: directory.appendingPathComponent("app-audio.wav"),
            sessionID: sessionID, liveChunkDirectory: chunkDirectory,
            onChunkReady: { chunk in chunks.update { $0.append(chunk) } }
        )

        track.receive(buffer: interleavedInt16Buffer(seconds: 31, sampleRate: 16_000, channels: 1), hostTime: 1)
        let result = try track.finalize()

        XCTAssertEqual(result.durationMs, 31_000)
        let readChunks = chunks.read()
        XCTAssertEqual(readChunks.map(\.sequence), [0, 1])
        XCTAssertEqual(readChunks.map(\.startOffsetMs), [0, 29_000])
        XCTAssertEqual(readChunks.map(\.durationMs), [30_000, 2_000])
        XCTAssertTrue(readChunks.allSatisfy { FileManager.default.fileExists(atPath: $0.fileURL.path) })
        XCTAssertTrue(readChunks.allSatisfy { $0.sha256.count == 64 })
    }
}
