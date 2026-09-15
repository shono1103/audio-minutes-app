import ClientCore
import Foundation
import XCTest

@testable import AudioMinutesApp

final class RecordingStopSequenceTests: XCTestCase {
    private enum TestError: Error { case bridge, recorder }

    func testLocalRecorderStopRunsAfterBridgeTimeout() {
        var localStopCalled = false

        let outcome = RecordingStopSequence.run(stopBridge: { throw TestError.bridge }) {
            localStopCalled = true
            throw TestError.recorder
        }

        XCTAssertTrue(localStopCalled, "bridge停止確認の失敗をdefer相当で処理し、local WAV確定を必ず試す")
        XCTAssertNotNil(outcome.bridgeError)
        XCTAssertNotNil(outcome.recorderError)
        XCTAssertFalse(outcome.succeeded)
    }

    func testBridgeWarningDoesNotDiscardSuccessfullySavedLocalRecording() {
        let state = makeState()

        let outcome = RecordingStopSequence.run(stopBridge: { throw TestError.bridge }) { state }

        XCTAssertTrue(outcome.succeeded)
        XCTAssertEqual(outcome.state, state)
        XCTAssertNotNil(outcome.bridgeError, "成功通知にはbridge警告を残せる")
        XCTAssertNil(outcome.recorderError)
    }

    private func makeState() -> LocalSessionState {
        let package = RecordingPackage(
            inputKind: .recordedDualTrack, startedAt: Date(), title: "test", titleEditedByUser: true,
            languageMode: .auto, allowExternalSend: false, formatProfileId: nil,
            tracks: [], source: RecordingSource(kind: "chrome_tab")
        )
        return LocalSessionState(package: package, trackFiles: [:])
    }
}
