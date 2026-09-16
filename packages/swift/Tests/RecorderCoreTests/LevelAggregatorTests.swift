import Foundation
import XCTest

@testable import RecorderCore

final class LevelAggregatorTests: XCTestCase {
    func testAppUpdateDoesNotClearMic() {
        let aggregator = LevelAggregator()
        let generation = aggregator.reset().generation
        XCTAssertEqual(aggregator.publish(app: nil, mic: 0.4, generation: generation)?.mic, 0.4)
        let result = aggregator.publish(app: 0.7, mic: nil, generation: generation)
        XCTAssertEqual(result?.app, 0.7)
        XCTAssertEqual(result?.mic, 0.4, "app 更新で mic の最新値を消してはいけない")
    }

    func testMicUpdateDoesNotClearApp() {
        let aggregator = LevelAggregator()
        let generation = aggregator.reset().generation
        XCTAssertEqual(aggregator.publish(app: 0.3, mic: nil, generation: generation)?.app, 0.3)
        let result = aggregator.publish(app: nil, mic: 0.9, generation: generation)
        XCTAssertEqual(result?.app, 0.3, "mic 更新で app の最新値を消してはいけない")
        XCTAssertEqual(result?.mic, 0.9)
    }

    func testResetStartsNewGenerationAtZero() {
        let aggregator = LevelAggregator()
        let generation = aggregator.reset().generation
        _ = aggregator.publish(app: 0.8, mic: 0.6, generation: generation)
        let nextGeneration = aggregator.reset().generation
        XCTAssertNotEqual(generation, nextGeneration)
        // 新世代は 0 から始まる。古い世代からの通知は無視する。
        XCTAssertNil(aggregator.publish(app: 0.1, mic: nil, generation: generation), "旧世代からの通知は破棄する")
        let fresh = aggregator.publish(app: nil, mic: nil, generation: nextGeneration)
        XCTAssertEqual(fresh?.app, 0)
        XCTAssertEqual(fresh?.mic, 0)
    }

    func testStaleGenerationNotificationIsRejectedAfterStopAndRestart() {
        let aggregator = LevelAggregator()
        let firstGeneration = aggregator.reset().generation
        _ = aggregator.publish(app: 0.9, mic: 0.9, generation: firstGeneration)

        // 停止 → 再開始で世代が進む。
        let secondGeneration = aggregator.reset().generation
        XCTAssertNotEqual(firstGeneration, secondGeneration)

        // 前世代の capture callback から遅延して届いた通知は無視されるべき。
        XCTAssertNil(aggregator.publish(app: 0.5, mic: nil, generation: firstGeneration))
        // 現世代の通知は反映される。
        let latest = aggregator.publish(app: 0.2, mic: nil, generation: secondGeneration)
        XCTAssertEqual(latest?.app, 0.2)
        XCTAssertEqual(latest?.mic, 0, "再開始直後は mic がリセットされている")
    }

    /// sequence は reset/publish のたびに必ず増加し、世代をまたいでも巻き戻らない。受信側が
    /// 「自分より新しい sequence を上書きしない」判定だけで配送順序の入れ替わりに対応できる
    /// 前提となる性質。
    func testSequenceIsMonotonicAcrossPublishAndResetAndGenerations() {
        let aggregator = LevelAggregator()
        var sequences: [Int] = []
        let firstGeneration = aggregator.reset()
        sequences.append(firstGeneration.sequence)
        sequences.append(aggregator.publish(app: 0.1, mic: nil, generation: firstGeneration.generation)!.sequence)
        sequences.append(aggregator.publish(app: nil, mic: 0.2, generation: firstGeneration.generation)!.sequence)
        let secondGeneration = aggregator.reset()
        sequences.append(secondGeneration.sequence)
        sequences.append(aggregator.publish(app: 0.3, mic: nil, generation: secondGeneration.generation)!.sequence)

        XCTAssertEqual(sequences, sequences.sorted())
        XCTAssertEqual(Set(sequences).count, sequences.count, "sequence は重複してはいけない")
    }

    /// app/mic 2系統の並行 producer から同時に更新しても最終値が両方とも失われず、
    /// 発行された sequence がすべて相異なり単調増加であることを確認する。
    /// sleep には依存せず、DispatchQueue.concurrentPerform で完了を待つ。
    func testConcurrentProducersDoNotLoseEitherChannelAndSequenceStaysUnique() {
        let aggregator = LevelAggregator()
        let generation = aggregator.reset().generation
        let iterations = 2_000
        let box = NSLock()
        var sequences: [Int] = []

        DispatchQueue.concurrentPerform(iterations: iterations * 2) { index in
            let snapshot: LevelAggregator.Snapshot?
            if index % 2 == 0 {
                snapshot = aggregator.publish(app: Float(index) / Float(iterations * 2), mic: nil, generation: generation)
            } else {
                snapshot = aggregator.publish(app: nil, mic: Float(index) / Float(iterations * 2), generation: generation)
            }
            if let snapshot { box.withLock { sequences.append(snapshot.sequence) } }
        }

        let result = aggregator.publish(app: nil, mic: nil, generation: generation)
        XCTAssertNotNil(result)
        XCTAssertGreaterThanOrEqual(result!.app, 0)
        XCTAssertLessThanOrEqual(result!.app, 1)
        XCTAssertGreaterThanOrEqual(result!.mic, 0)
        XCTAssertLessThanOrEqual(result!.mic, 1)
        XCTAssertEqual(sequences.count, iterations * 2)
        XCTAssertEqual(Set(sequences).count, sequences.count, "並行呼び出しでも sequence が重複してはいけない")
    }
}
