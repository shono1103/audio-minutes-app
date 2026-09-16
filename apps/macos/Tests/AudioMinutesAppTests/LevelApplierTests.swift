import Foundation
import XCTest

@testable import AudioMinutesApp

final class LevelApplierTests: XCTestCase {
    func testAppliesInOrderSequences() {
        var applier = LevelApplier()
        XCTAssertTrue(applier.apply(app: 0.3, mic: 0.1, sequence: 1))
        XCTAssertEqual(applier.app, 0.3)
        XCTAssertEqual(applier.mic, 0.1)
        XCTAssertTrue(applier.apply(app: 0.5, mic: 0.2, sequence: 2))
        XCTAssertEqual(applier.app, 0.5)
        XCTAssertEqual(applier.mic, 0.2)
    }

    /// RecordingCoordinator の2つの capture callback スレッドが MainActor 上へ独立した Task
    /// で通知を届けると、Task の実行順序が発火順序と入れ替わることがある。sequence の古い
    /// (小さい・同じ) 通知は無視し、より新しい値を古い値で上書きしないことを確認する。
    func testRejectsOutOfOrderDeliveryAfterReversal() {
        var applier = LevelApplier()
        XCTAssertTrue(applier.apply(app: 0.7, mic: 0.4, sequence: 5), "sequence 5 (mic 更新) を先に適用")
        let applied = applier.apply(app: 0.7, mic: 0, sequence: 3)
        XCTAssertFalse(applied, "sequence 3 (app 更新、mic 未更新の古い snapshot) は後から届いても無視する")
        XCTAssertEqual(applier.mic, 0.4, "古い通知で mic を 0 に巻き戻してはいけない")
        XCTAssertEqual(applier.app, 0.7)
    }

    func testDuplicateSequenceIsRejected() {
        var applier = LevelApplier()
        XCTAssertTrue(applier.apply(app: 0.1, mic: 0.1, sequence: 1))
        XCTAssertFalse(applier.apply(app: 0.9, mic: 0.9, sequence: 1), "同じ sequence の再通知は適用しない")
        XCTAssertEqual(applier.app, 0.1)
        XCTAssertEqual(applier.mic, 0.1)
    }

    /// 停止 (resetLevels) が発行するゼロ snapshot は sequence が増加しているため、
    /// 停止直前に発火した古い publish が後から届いても上書きされない。
    func testZeroSnapshotAfterStopIsNotOverwrittenByStaleValue() {
        var applier = LevelApplier()
        XCTAssertTrue(applier.apply(app: 0.8, mic: 0.6, sequence: 10))
        // 停止時の resetLevels によるゼロ通知 (sequence が進む)。
        XCTAssertTrue(applier.apply(app: 0, mic: 0, sequence: 11))
        // 停止直前に capture callback が計算していた古い snapshot が遅れて届く。
        XCTAssertFalse(applier.apply(app: 0.8, mic: 0.6, sequence: 9))
        XCTAssertEqual(applier.app, 0)
        XCTAssertEqual(applier.mic, 0)
    }

    /// 開始失敗時の resetLevels によるゼロ通知も同様に、それ以前の古い値で上書きされない。
    func testZeroSnapshotAfterStartFailureIsNotOverwritten() {
        var applier = LevelApplier()
        XCTAssertTrue(applier.apply(app: 0.4, mic: 0.4, sequence: 1))
        XCTAssertTrue(applier.apply(app: 0, mic: 0, sequence: 2), "開始失敗時のゼロ通知")
        XCTAssertFalse(applier.apply(app: 0.4, mic: 0.4, sequence: 1), "失敗前の古い通知は無視する")
        XCTAssertEqual(applier.app, 0)
        XCTAssertEqual(applier.mic, 0)
    }

    /// 再開始をまたいで前回セッションからの遅延通知が届いても、新セッションの値を上書きしない。
    func testStaleNotificationFromPreviousSessionIsRejectedAcrossRestart() {
        var applier = LevelApplier()
        XCTAssertTrue(applier.apply(app: 0.9, mic: 0.9, sequence: 1), "前回セッションの値")
        XCTAssertTrue(applier.apply(app: 0, mic: 0, sequence: 2), "停止時のゼロ")
        XCTAssertTrue(applier.apply(app: 0, mic: 0, sequence: 3), "次回開始時のゼロ")
        XCTAssertTrue(applier.apply(app: 0.1, mic: 0, sequence: 4), "新セッションの最初の値")
        let stale = applier.apply(app: 0.9, mic: 0.9, sequence: 1)
        XCTAssertFalse(stale, "前回セッションからの遅延通知 (sequence 1) は新セッションの値を上書きしない")
        XCTAssertEqual(applier.app, 0.1)
        XCTAssertEqual(applier.mic, 0)
    }
}
