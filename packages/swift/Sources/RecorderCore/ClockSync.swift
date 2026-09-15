import Foundation

/// 2 トラックの共通時刻基準 (FR-005)。両トラックの最初のバッファの host time から
/// `recording_start` を決め、各トラックの `start_offset_ms` を算出する。
public struct ClockSync: Sendable, Equatable {
    /// host time (mach_absolute_time) 1 tick あたりのナノ秒。
    public let nanosecondsPerTick: Double

    public init(nanosecondsPerTick: Double = ClockSync.systemNanosecondsPerTick()) {
        self.nanosecondsPerTick = nanosecondsPerTick
    }

    public static func systemNanosecondsPerTick() -> Double {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        guard info.denom != 0 else { return 1 }
        return Double(info.numer) / Double(info.denom)
    }

    public func milliseconds(from start: UInt64, to end: UInt64) -> Int {
        guard end >= start else { return 0 }
        return Int((Double(end - start) * nanosecondsPerTick / 1_000_000).rounded())
    }

    /// 各トラックの最初のサンプルの host time から共通原点とオフセットを求める。
    public func offsets(firstHostTimes: [String: UInt64]) -> (origin: UInt64, offsetsMs: [String: Int]) {
        guard let origin = firstHostTimes.values.min() else { return (0, [:]) }
        var offsets: [String: Int] = [:]
        for (track, time) in firstHostTimes {
            offsets[track] = milliseconds(from: origin, to: time)
        }
        return (origin, offsets)
    }

    /// 期待サンプル数と実サンプル数からドリフト (ms) を求める。metadata へ記録するだけで補正はしない。
    public func driftMs(elapsedHostTicks: UInt64, framesWritten: Int, sampleRate: Int) -> Int {
        let elapsedMs = Double(elapsedHostTicks) * nanosecondsPerTick / 1_000_000
        let writtenMs = Double(framesWritten) * 1000 / Double(sampleRate)
        return Int((writtenMs - elapsedMs).rounded())
    }
}
