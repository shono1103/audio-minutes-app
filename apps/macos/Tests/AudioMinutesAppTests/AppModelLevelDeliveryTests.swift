import ClientCore
import Foundation
import XCTest

@testable import AudioMinutesApp

/// `RecordingCoordinator.onLevels` → `AppModel.configureCallbacks()` → `@Published
/// appLevel/micLevel` という本番の配線を、実際に生成した `AppModel` を通して検証する。
/// `LevelApplier` 単体の判定ロジック (配送逆転・重複・世代境界の拒否) は
/// `LevelApplierTests`/`LevelAggregatorTests` で網羅済みなので、ここでは「本物の AppModel
/// が実際に登録する callback が、実際の capture callback スレッドからの通知を受けても
/// published 値へ正しく反映されるか」を確認する。`startBackgroundServices: false` と
/// 一時ディレクトリにより、discovery/bridge/network の起動と実環境への副作用を避ける。
@MainActor
final class AppModelLevelDeliveryTests: XCTestCase {
    private func makeModel() throws -> AppModel {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        return AppModel(paths: AppPaths(root: directory), startBackgroundServices: false)
    }

    /// `recorder.onLevels` を呼び出し、`AppModel` が積む `Task { @MainActor in ... }` が
    /// 完了する (published 値への適用・拒否のいずれであっても) まで、`AppModel.
    /// onLevelsHandled` というテスト専用 hook と `XCTestExpectation` の非同期 API
    /// (`fulfillment(of:timeout:)`) で明示的に待つ。固定回数の `Task.yield` は特定 Task の
    /// 完了を保証しないため使わない。closure 自体は `Task.detached` から呼び出し、実際の
    /// capture callback が MainActor 外の別スレッドから通知する状況を模す。timeout した
    /// 場合は `fulfillment(of:timeout:)` がテストを明示的に失敗させる。
    private func deliver(_ model: AppModel, app: Float, mic: Float, sequence: Int, timeout: TimeInterval = 2) async {
        let expectation = expectation(description: "onLevels(\(app), \(mic), \(sequence)) handled")
        model.onLevelsHandled = { expectation.fulfill() }
        let closure = model.recorder.onLevels
        Task.detached { closure?(app, mic, sequence) }
        await fulfillment(of: [expectation], timeout: timeout)
    }

    func testRealCallbackFromBackgroundThreadUpdatesPublishedLevels() async throws {
        let model = try makeModel()

        await deliver(model, app: 0.3, mic: 0.1, sequence: 1)

        XCTAssertEqual(model.appLevel, 0.3)
        XCTAssertEqual(model.micLevel, 0.1)
    }

    /// app/mic 別々の capture callback スレッドからの通知が MainActor 上で入れ替わっても、
    /// 古い sequence の通知が後から届けば無視されることを、実際の AppModel の callback
    /// 配線を通して確認する。2回目の `deliver` の完了 (拒否されたという結果を含む) を
    /// 待ってから assert するため、Task がまだ未処理のまま偶然 assertion が通ることはない。
    func testStaleSequenceDeliveredAfterNewerIsIgnoredThroughRealWiring() async throws {
        let model = try makeModel()

        await deliver(model, app: 0.7, mic: 0.4, sequence: 5)
        await deliver(model, app: 0.7, mic: 0, sequence: 3) // 古い sequence の遅延通知

        XCTAssertEqual(model.appLevel, 0.7)
        XCTAssertEqual(model.micLevel, 0.4, "古い sequence の通知が mic を上書きしてはいけない")
    }

    func testDuplicateSequenceIsIgnoredThroughRealWiring() async throws {
        let model = try makeModel()

        await deliver(model, app: 0.1, mic: 0.1, sequence: 1)
        await deliver(model, app: 0.9, mic: 0.9, sequence: 1) // 同じ sequence の再通知

        XCTAssertEqual(model.appLevel, 0.1)
        XCTAssertEqual(model.micLevel, 0.1)
    }

    /// `RecordingCoordinator.resetLevels()` が停止時に発行するゼロ通知 (sequence が進む) を
    /// 模した検証。
    func testZeroAfterStopIsNotOverwrittenByStaleValueThroughRealWiring() async throws {
        let model = try makeModel()

        await deliver(model, app: 0.8, mic: 0.6, sequence: 10)
        await deliver(model, app: 0, mic: 0, sequence: 11) // 停止時のゼロ通知
        await deliver(model, app: 0.8, mic: 0.6, sequence: 9) // 停止直前に計算されていた古い通知が遅延して届く

        XCTAssertEqual(model.appLevel, 0)
        XCTAssertEqual(model.micLevel, 0)
    }

    /// 開始失敗時の `resetLevels()` によるゼロ通知も同様に古い値で上書きされないことを確認する。
    func testZeroAfterStartFailureIsNotOverwrittenThroughRealWiring() async throws {
        let model = try makeModel()

        await deliver(model, app: 0.4, mic: 0.4, sequence: 1)
        await deliver(model, app: 0, mic: 0, sequence: 2) // 開始失敗時のゼロ通知
        await deliver(model, app: 0.4, mic: 0.4, sequence: 1) // 失敗前の古い通知

        XCTAssertEqual(model.appLevel, 0)
        XCTAssertEqual(model.micLevel, 0)
    }

    /// 再開始をまたいで前回セッションからの遅延通知が届いても、新セッションの値を
    /// 上書きしないことを確認する。
    func testStaleNotificationFromPreviousSessionIsRejectedAcrossRestartThroughRealWiring() async throws {
        let model = try makeModel()

        await deliver(model, app: 0.9, mic: 0.9, sequence: 1) // 前回セッションの値
        await deliver(model, app: 0, mic: 0, sequence: 2) // 停止時のゼロ
        await deliver(model, app: 0, mic: 0, sequence: 3) // 次回開始時のゼロ
        await deliver(model, app: 0.1, mic: 0, sequence: 4) // 新セッションの最初の値
        await deliver(model, app: 0.9, mic: 0.9, sequence: 1) // 前回セッションからの遅延通知

        XCTAssertEqual(model.appLevel, 0.1, "前回セッションからの遅延通知 (sequence 1) は新セッションの値を上書きしない")
        XCTAssertEqual(model.micLevel, 0)
    }
}
