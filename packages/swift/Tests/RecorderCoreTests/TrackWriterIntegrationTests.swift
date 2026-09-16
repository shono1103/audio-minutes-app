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

    private func containsNonZeroByte(_ url: URL) throws -> Bool {
        let data = try Data(contentsOf: url)
        return data.contains { $0 != 0 }
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
        XCTAssertTrue(try containsNonZeroByte(url), "変換後の保存 PCM が無音のままではいけない")

        let data = try Data(contentsOf: url)
        XCTAssertEqual(String(decoding: data.prefix(4), as: UTF8.self), "RIFF")
        XCTAssertEqual(String(decoding: data[8..<12], as: UTF8.self), "WAVE")
        XCTAssertEqual(try WavWriter.sha256(of: url), result.sha256)
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
        XCTAssertTrue(try containsNonZeroByte(url))
        XCTAssertEqual(try WavWriter.sha256(of: url), result.sha256)
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
