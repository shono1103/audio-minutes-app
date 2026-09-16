import Foundation
import AVFoundation
import XCTest

@testable import RecorderCore

final class RecordingPrimitivesTests: XCTestCase {
    private final class LockedBox<Value>: @unchecked Sendable {
        private let lock = NSLock()
        private var value: Value
        init(_ value: Value) { self.value = value }
        func read() -> Value { lock.withLock { value } }
        func update(_ body: (inout Value) -> Void) { lock.withLock { body(&value) } }
    }

    private final class FaultWriter: TrackAudioWriting, @unchecked Sendable {
        var appendError: Error?
        var finalizeFailures = 0
        private(set) var appendCalls = 0
        private(set) var finalizeCalls = 0

        func append(buffer: AVAudioPCMBuffer) throws {
            appendCalls += 1
            if let appendError { throw appendError }
        }

        func finalize() throws -> (sha256: String, byteSize: Int, durationMs: Int) {
            finalizeCalls += 1
            if finalizeFailures > 0 {
                finalizeFailures -= 1
                throw CocoaError(.fileWriteOutOfSpace)
            }
            return (String(repeating: "a", count: 64), 44, 0)
        }
    }

    private func buffer() -> AVAudioPCMBuffer {
        let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
        let value = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 64)!
        value.frameLength = 64
        return value
    }

    func testClockOffsetsUseEarliestTrackAsOrigin() {
        let sync = ClockSync(nanosecondsPerTick: 1_000_000)
        let result = sync.offsets(firstHostTimes: ["app": 1_000, "microphone": 1_012])
        XCTAssertEqual(result.origin, 1_000)
        XCTAssertEqual(result.offsetsMs, ["app": 0, "microphone": 12])
        XCTAssertEqual(sync.driftMs(elapsedHostTicks: 1_000, framesWritten: 16_160, sampleRate: 16_000), 10)
    }

    func testMicrophoneConfigurationChangeStopsOnlyWhenSelectedDeviceDisappears() {
        let selected = InputDevice(id: 1, uid: "selected", name: "選択中", isDefault: true)
        let other = InputDevice(id: 2, uid: "other", name: "別のマイク", isDefault: false)

        XCTAssertFalse(MicrophoneCapture.shouldStopAfterConfigurationChange(
            selectedUID: selected.uid, availableDevices: [selected, other]
        ))
        XCTAssertTrue(MicrophoneCapture.shouldStopAfterConfigurationChange(
            selectedUID: selected.uid, availableDevices: [other]
        ))
    }

    func testWavWriterFinalizesHeaderAndChecksum() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("test.wav")
        let writer = try WavWriter(url: url)
        try writer.append(pcm16: Data(repeating: 0, count: 320))

        let result = try writer.finalize()
        XCTAssertEqual(result.byteSize, 364)
        XCTAssertEqual(result.durationMs, 10)
        XCTAssertEqual(result.sha256.count, 64)
        let data = try Data(contentsOf: url)
        XCTAssertEqual(String(decoding: data.prefix(4), as: UTF8.self), "RIFF")
        XCTAssertEqual(String(decoding: data[8..<12], as: UTF8.self), "WAVE")
        XCTAssertThrowsError(try writer.finalize())
    }

    func testLiveChunkWriterCreatesThirtySecondChunkAndFlushesRemainder() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let chunks = LockedBox<[LiveAudioChunk]>([])
        let sessionID = UUID()
        let writer = try LiveChunkWriter(
            sessionID: sessionID, trackID: .appAudio, role: .app, directory: directory
        ) { chunk in chunks.update { $0.append(chunk) } }
        let format = AVAudioFormat(
            commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true
        )!
        let frames = AVAudioFrameCount(31 * 16_000)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames
        if let samples = buffer.int16ChannelData?[0] {
            samples.initialize(repeating: 0, count: Int(frames))
        }

        try writer.append(buffer: buffer)
        try writer.finalize()

        let result = chunks.read()
        XCTAssertEqual(result.map(\.sequence), [0, 1])
        XCTAssertEqual(result.map(\.startOffsetMs), [0, 29_000])
        XCTAssertEqual(result.map(\.durationMs), [30_000, 2_000])
        XCTAssertTrue(result.allSatisfy { FileManager.default.fileExists(atPath: $0.fileURL.path) })
        XCTAssertTrue(result.allSatisfy { $0.sha256.count == 64 })
    }

    func testAppendDiskFailureNotifiesOnceAndStopsAcceptingData() {
        let writer = FaultWriter()
        writer.appendError = CocoaError(.fileWriteOutOfSpace)
        let track = TrackWriter(trackID: .appAudio, role: .app, writer: writer)
        let failures = LockedBox(0)
        track.onFailure = { _ in failures.update { $0 += 1 } }

        track.receive(buffer: buffer(), hostTime: 1)
        track.receive(buffer: buffer(), hostTime: 2)

        XCTAssertEqual(failures.read(), 1)
        XCTAssertEqual(writer.appendCalls, 1)
        XCTAssertNotNil(track.lastError)
        XCTAssertThrowsError(try track.finalize())
        XCTAssertThrowsError(try track.finalize(), "append失敗したtrackを再試行で成功扱いにしない")
        XCTAssertEqual(writer.finalizeCalls, 1, "WAV確定結果は再利用しつつtrack障害を維持する")
    }

    func testDownmixSetupFailureIsNotTreatedAsSuccessfulInput() {
        let writer = FaultWriter()
        let track = TrackWriter(trackID: .microphone, role: .microphone, writer: writer) { _ in
            throw WavError.converterUnavailable
        }
        let failure = LockedBox<Error?>(nil)
        track.onFailure = { error in failure.update { $0 = error } }

        track.receive(buffer: buffer(), hostTime: 1)

        XCTAssertNotNil(failure.read())
        XCTAssertEqual(writer.appendCalls, 0)
    }

    func testOneTrackFinalizeFailureCanRetryWithoutRefinalizingSuccessfulTrack() throws {
        let healthy = FaultWriter()
        let faulty = FaultWriter()
        faulty.finalizeFailures = 1
        let app = TrackWriter(trackID: .appAudio, role: .app, writer: healthy)
        let mic = TrackWriter(trackID: .microphone, role: .microphone, writer: faulty)

        _ = try app.finalize()
        XCTAssertThrowsError(try mic.finalize())
        _ = try app.finalize()
        _ = try mic.finalize()

        XCTAssertEqual(healthy.finalizeCalls, 1)
        XCTAssertEqual(faulty.finalizeCalls, 2)
    }

    func testFinalizeBatchAttemptsMicrophoneAfterAppTrackFailure() {
        let faultyApp = FaultWriter()
        faultyApp.finalizeFailures = 1
        let healthyMic = FaultWriter()
        let app = TrackWriter(trackID: .appAudio, role: .app, writer: faultyApp)
        let mic = TrackWriter(trackID: .microphone, role: .microphone, writer: healthyMic)

        let attempts = RecordingCoordinator.finalizeIndependently([app, mic])

        XCTAssertEqual(faultyApp.finalizeCalls, 1)
        XCTAssertEqual(healthyMic.finalizeCalls, 1, "app track の失敗後も microphone WAV header を確定する")
        XCTAssertFalse(attempts[0].succeeded)
        XCTAssertTrue(attempts[1].succeeded)
        XCTAssertNotNil(attempts[1].finalization)
    }

    func testFinalizeBatchRecordsHeaderFinalizationDespitePriorAppendFailure() {
        let underlying = FaultWriter()
        underlying.appendError = CocoaError(.fileWriteOutOfSpace)
        let track = TrackWriter(trackID: .appAudio, role: .app, writer: underlying)
        track.receive(buffer: buffer(), hostTime: 1)

        let attempt = RecordingCoordinator.finalizeIndependently([track])[0]

        XCTAssertFalse(attempt.succeeded, "append 障害を成功扱いしない")
        XCTAssertNotNil(attempt.finalization, "回収用 WAV header の確定結果は保持する")
        XCTAssertNotNil(attempt.errorDescription)
        XCTAssertEqual(underlying.finalizeCalls, 1)
    }
}
