import Foundation
import XCTest

@testable import RecorderCore

/// 公開 API の開始可否 (`canStart`) と実際の遷移表を一致させる (R15)。
///
/// stop() 後の saved から次の start() が `invalidTransition` になると、
/// 2 回目以降の録音ができない。canStart が true を返す状態からは必ず録音へ移れること、
/// 逆に録音中・送信中からは始められないことを固定する。
final class RecordingStateMachineTests: XCTestCase {
    func testRestoredChromeTabNeverFallsBackToWholeApplication() throws {
        XCTAssertThrowsError(try ChromeAudioSelection.requiresExplicitSelection.validated(availableTabIDs: [10]))
        XCTAssertThrowsError(try ChromeAudioSelection.tab(7).validated(availableTabIDs: [10]))
        XCTAssertNil(try ChromeAudioSelection.wholeApplication.validated(availableTabIDs: []))
        XCTAssertEqual(try ChromeAudioSelection.tab(10).validated(availableTabIDs: [10]), 10)
    }

    func testRecordingStopsAtFourHourBoundary() {
        XCTAssertFalse(RecordingCoordinator.hasReachedMaximumDuration(14_399.999))
        XCTAssertTrue(RecordingCoordinator.hasReachedMaximumDuration(14_400))
    }

    private func machine(advancingTo states: [RecordingState]) throws -> RecordingStateMachine {
        var machine = RecordingStateMachine()
        for state in states { try machine.transition(to: state) }
        return machine
    }

    func testCanStartStatesCanActuallyEnterRecording() throws {
        let paths: [(String, [RecordingState])] = [
            ("初回 (idle)", []),
            ("stop 後 (saved)", [.recording, .finalizing, .saved]),
            ("送信済み (uploaded)", [.recording, .finalizing, .saved, .uploading, .uploaded]),
            ("失敗後 (failed)", [.recording, .failed]),
            ("送信失敗後 (saved へ戻る)", [.recording, .finalizing, .saved, .uploading, .saved]),
        ]
        for (label, path) in paths {
            var subject = try machine(advancingTo: path)
            XCTAssertTrue(subject.canStart, "\(label): canStart が false")
            XCTAssertNoThrow(try subject.transition(to: .recording), "\(label): 開始できない")
            XCTAssertTrue(subject.isRecording, "\(label): recording になっていない")
        }
    }

    func testStatesThatCannotStartAreRejected() throws {
        var recording = try machine(advancingTo: [.recording])
        XCTAssertFalse(recording.canStart)
        XCTAssertThrowsError(try recording.transition(to: .recording)) { error in
            XCTAssertEqual(error as? RecordingError, .alreadyRecording)
        }

        var uploading = try machine(advancingTo: [.recording, .finalizing, .saved, .uploading])
        XCTAssertFalse(uploading.canStart, "送信中に次の録音を始めさせない")
        XCTAssertThrowsError(try uploading.transition(to: .recording))

        var finalizing = try machine(advancingTo: [.recording, .finalizing])
        XCTAssertFalse(finalizing.canStart, "確定処理中に次の録音を始めさせない")
        XCTAssertThrowsError(try finalizing.transition(to: .recording))
    }

    func testCanStartAndTransitionTableAgreeForEveryState() throws {
        // 全状態で「canStart == 実際に recording へ移れる」ことを確認する
        let reachable: [RecordingState: [RecordingState]] = [
            .idle: [],
            .recording: [.recording],
            .finalizing: [.recording, .finalizing],
            .saved: [.recording, .finalizing, .saved],
            .uploading: [.recording, .finalizing, .saved, .uploading],
            .uploaded: [.recording, .finalizing, .saved, .uploading, .uploaded],
            .failed: [.recording, .failed],
        ]
        for (state, path) in reachable {
            var subject = try machine(advancingTo: path)
            XCTAssertEqual(subject.state, state, "前提の経路が間違っている")
            let declared = subject.canStart
            let actual = (try? subject.transition(to: .recording)) != nil
            XCTAssertEqual(declared, actual, "\(state.rawValue): canStart=\(declared) だが実際は \(actual)")
        }
    }

    func testSavedPackageIsKeptWhenStartingTheNextRecording() throws {
        // saved → recording が許されていること自体が「前回の package を消さずに次へ進める」条件。
        // (RecordingCoordinator.start は新しい session_id で別ディレクトリを作る)
        var subject = try machine(advancingTo: [.recording, .finalizing, .saved])
        XCTAssertNoThrow(try subject.transition(to: .recording))
    }
}
