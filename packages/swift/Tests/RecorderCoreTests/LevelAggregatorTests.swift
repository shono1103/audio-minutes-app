import Foundation
import XCTest

@testable import RecorderCore

final class LevelAggregatorTests: XCTestCase {
    func testAppUpdateDoesNotClearMic() {
        let aggregator = LevelAggregator()
        let generation = aggregator.reset()
        XCTAssertEqual(aggregator.publish(app: nil, mic: 0.4, generation: generation)?.mic, 0.4)
        let result = aggregator.publish(app: 0.7, mic: nil, generation: generation)
        XCTAssertEqual(result?.app, 0.7)
        XCTAssertEqual(result?.mic, 0.4, "app 更新で mic の最新値を消してはいけない")
    }

    func testMicUpdateDoesNotClearApp() {
        let aggregator = LevelAggregator()
        let generation = aggregator.reset()
        XCTAssertEqual(aggregator.publish(app: 0.3, mic: nil, generation: generation)?.app, 0.3)
        let result = aggregator.publish(app: nil, mic: 0.9, generation: generation)
        XCTAssertEqual(result?.app, 0.3, "mic 更新で app の最新値を消してはいけない")
        XCTAssertEqual(result?.mic, 0.9)
    }

    func testResetStartsNewGenerationAtZero() {
        let aggregator = LevelAggregator()
        let generation = aggregator.reset()
        _ = aggregator.publish(app: 0.8, mic: 0.6, generation: generation)
        let nextGeneration = aggregator.reset()
        XCTAssertNotEqual(generation, nextGeneration)
        // 新世代は 0 から始まる。古い世代からの通知は無視する。
        XCTAssertNil(aggregator.publish(app: 0.1, mic: nil, generation: generation), "旧世代からの通知は破棄する")
        let fresh = aggregator.publish(app: nil, mic: nil, generation: nextGeneration)
        XCTAssertEqual(fresh?.app, 0)
        XCTAssertEqual(fresh?.mic, 0)
    }

    func testStaleGenerationNotificationIsRejectedAfterStopAndRestart() {
        let aggregator = LevelAggregator()
        let firstGeneration = aggregator.reset()
        _ = aggregator.publish(app: 0.9, mic: 0.9, generation: firstGeneration)

        // 停止 → 再開始で世代が進む。
        let secondGeneration = aggregator.reset()
        XCTAssertNotEqual(firstGeneration, secondGeneration)

        // 前世代の capture callback から遅延して届いた通知は無視されるべき。
        XCTAssertNil(aggregator.publish(app: 0.5, mic: nil, generation: firstGeneration))
        // 現世代の通知は反映される。
        let latest = aggregator.publish(app: 0.2, mic: nil, generation: secondGeneration)
        XCTAssertEqual(latest?.app, 0.2)
        XCTAssertEqual(latest?.mic, 0, "再開始直後は mic がリセットされている")
    }

    /// app/mic 2系統の並行 producer から同時に更新しても最終値が両方とも失われないことを確認する。
    /// sleep には依存せず、DispatchQueue.concurrentPerform で完了を待つ。
    func testConcurrentProducersDoNotLoseEitherChannel() {
        let aggregator = LevelAggregator()
        let generation = aggregator.reset()
        let iterations = 2_000

        DispatchQueue.concurrentPerform(iterations: iterations * 2) { index in
            if index % 2 == 0 {
                _ = aggregator.publish(app: Float(index) / Float(iterations * 2), mic: nil, generation: generation)
            } else {
                _ = aggregator.publish(app: nil, mic: Float(index) / Float(iterations * 2), generation: generation)
            }
        }

        let result = aggregator.publish(app: nil, mic: nil, generation: generation)
        XCTAssertNotNil(result)
        XCTAssertGreaterThanOrEqual(result!.app, 0)
        XCTAssertLessThanOrEqual(result!.app, 1)
        XCTAssertGreaterThanOrEqual(result!.mic, 0)
        XCTAssertLessThanOrEqual(result!.mic, 1)
    }
}
