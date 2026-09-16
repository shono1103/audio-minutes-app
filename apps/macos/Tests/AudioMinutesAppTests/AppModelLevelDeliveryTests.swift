import ClientCore
import Foundation
import XCTest

@testable import AudioMinutesApp

/// `RecordingCoordinator.onLevels` → `AppModel.configureCallbacks()` → `@Published
/// appLevel/micLevel` という本番の配線を、実際に生成した `AppModel` を通して検証する。
/// `LevelApplier` 単体の判定ロジック (配送逆転・重複・世代境界の拒否) は
/// `LevelApplierTests`/`LevelAggregatorTests` で網羅済みなので、ここでは「本物の AppModel
/// が実際に登録する callback が published 値へ正しく反映されるか」だけを確認する。
/// `startBackgroundServices: false` と一時ディレクトリにより、discovery/bridge/network の
/// 起動と実環境への副作用を避ける。
@MainActor
final class AppModelLevelDeliveryTests: XCTestCase {
    private func makeModel() throws -> AppModel {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        return AppModel(paths: AppPaths(root: directory), startBackgroundServices: false)
    }

    /// callback が積む `Task { @MainActor in ... }` が実行されるまで、条件が満たされるか
    /// timeout するまで yield しながら待つ。実 sleep には依存しない。
    private func waitUntil(timeout: TimeInterval = 2, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition(), Date() < deadline { await Task.yield() }
    }

    /// 「何も変化しない」ことを確認するための待機。すでに enqueue 済みの MainActor Task を
    /// 処理させるのに十分な回数 yield する（各 Task 本体に追加の await 点はないため、
    /// 少数の yield で十分処理し切れる）。
    private func drain(_ times: Int = 50) async {
        for _ in 0..<times { await Task.yield() }
    }

    func testRealCallbackUpdatesPublishedLevels() async throws {
        let model = try makeModel()

        model.recorder.onLevels?(0.3, 0.1, 1)
        await waitUntil { model.appLevel == 0.3 && model.micLevel == 0.1 }

        XCTAssertEqual(model.appLevel, 0.3)
        XCTAssertEqual(model.micLevel, 0.1)
    }

    /// app/mic 別々の capture callback スレッドからの通知が MainActor 上で入れ替わっても、
    /// 古い sequence の通知が後から届けば無視されることを、実際の AppModel の callback
    /// 配線を通して確認する。
    func testStaleSequenceDeliveredAfterNewerIsIgnoredThroughRealWiring() async throws {
        let model = try makeModel()

        model.recorder.onLevels?(0.7, 0.4, 5)
        await waitUntil { model.micLevel == 0.4 }

        model.recorder.onLevels?(0.7, 0, 3) // 古い sequence の遅延通知
        await drain()

        XCTAssertEqual(model.appLevel, 0.7)
        XCTAssertEqual(model.micLevel, 0.4, "古い sequence の通知が mic を上書きしてはいけない")
    }

    func testDuplicateSequenceIsIgnoredThroughRealWiring() async throws {
        let model = try makeModel()

        model.recorder.onLevels?(0.1, 0.1, 1)
        await waitUntil { model.appLevel == 0.1 }

        model.recorder.onLevels?(0.9, 0.9, 1) // 同じ sequence の再通知
        await drain()

        XCTAssertEqual(model.appLevel, 0.1)
        XCTAssertEqual(model.micLevel, 0.1)
    }

    /// `RecordingCoordinator.resetLevels()` が停止時に発行するゼロ通知 (sequence が進む) を
    /// 模した検証。
    func testZeroAfterStopIsNotOverwrittenByStaleValueThroughRealWiring() async throws {
        let model = try makeModel()

        model.recorder.onLevels?(0.8, 0.6, 10)
        await waitUntil { model.appLevel == 0.8 }

        model.recorder.onLevels?(0, 0, 11) // 停止時のゼロ通知
        await waitUntil { model.appLevel == 0 && model.micLevel == 0 }

        model.recorder.onLevels?(0.8, 0.6, 9) // 停止直前に計算されていた古い通知が遅延して届く
        await drain()

        XCTAssertEqual(model.appLevel, 0)
        XCTAssertEqual(model.micLevel, 0)
    }

    /// 開始失敗時の `resetLevels()` によるゼロ通知も同様に古い値で上書きされないことを確認する。
    func testZeroAfterStartFailureIsNotOverwrittenThroughRealWiring() async throws {
        let model = try makeModel()

        model.recorder.onLevels?(0.4, 0.4, 1)
        await waitUntil { model.appLevel == 0.4 }

        model.recorder.onLevels?(0, 0, 2) // 開始失敗時のゼロ通知
        await waitUntil { model.appLevel == 0 && model.micLevel == 0 }

        model.recorder.onLevels?(0.4, 0.4, 1) // 失敗前の古い通知
        await drain()

        XCTAssertEqual(model.appLevel, 0)
        XCTAssertEqual(model.micLevel, 0)
    }

    /// 再開始をまたいで前回セッションからの遅延通知が届いても、新セッションの値を
    /// 上書きしないことを確認する。
    func testStaleNotificationFromPreviousSessionIsRejectedAcrossRestartThroughRealWiring() async throws {
        let model = try makeModel()

        model.recorder.onLevels?(0.9, 0.9, 1) // 前回セッションの値
        await waitUntil { model.appLevel == 0.9 }

        model.recorder.onLevels?(0, 0, 2) // 停止時のゼロ
        await waitUntil { model.appLevel == 0 }

        model.recorder.onLevels?(0, 0, 3) // 次回開始時のゼロ
        model.recorder.onLevels?(0.1, 0, 4) // 新セッションの最初の値
        await waitUntil { model.appLevel == 0.1 }

        model.recorder.onLevels?(0.9, 0.9, 1) // 前回セッションからの遅延通知
        await drain()

        XCTAssertEqual(model.appLevel, 0.1, "前回セッションからの遅延通知 (sequence 1) は新セッションの値を上書きしない")
        XCTAssertEqual(model.micLevel, 0)
    }
}
