import ClientCore
import Foundation
import RecorderCore

/// 音声callbackを止めずにchunkを送信し、停止時には登録済みtaskの完了を待つ。
final class LiveChunkUploadCoordinator: @unchecked Sendable {
    private static let retryDelaysNanoseconds: [UInt64] = [1_000_000_000, 2_000_000_000, 4_000_000_000]

    struct Progress: Sendable {
        var uploaded: Int
        var failed: Int
    }

    private let service: SessionService
    private let startRequest: LiveSessionStart
    private let onProgress: @Sendable (Progress) -> Void
    private let lock = NSLock()
    private let group = DispatchGroup()
    private var beginTask: Task<Bool, Never>?
    private var uploaded = 0
    private var failed = 0

    init(service: SessionService, startRequest: LiveSessionStart,
         onProgress: @escaping @Sendable (Progress) -> Void) {
        self.service = service
        self.startRequest = startRequest
        self.onProgress = onProgress
    }

    func begin() {
        lock.withLock {
            guard beginTask == nil else { return }
            beginTask = Task { [service, startRequest] in
                for delay in [UInt64(0)] + Self.retryDelaysNanoseconds {
                    if delay > 0 { try? await Task.sleep(nanoseconds: delay) }
                    do { _ = try await service.beginLiveSession(startRequest); return true }
                    catch { continue }
                }
                return false
            }
        }
    }

    func enqueue(_ chunk: LiveAudioChunk) {
        group.enter()
        Task { [self] in
            defer { self.group.leave() }
            let beginTask = self.lock.withLock { self.beginTask }
            guard await beginTask?.value == true else {
                self.record(success: false)
                return
            }
            for delay in [UInt64(0)] + Self.retryDelaysNanoseconds {
                if delay > 0 { try? await Task.sleep(nanoseconds: delay) }
                do {
                    _ = try await self.service.uploadLiveChunk(
                        sessionID: chunk.sessionID,
                        trackID: chunk.trackID,
                        sequence: chunk.sequence,
                        startOffsetMs: chunk.startOffsetMs,
                        durationMs: chunk.durationMs,
                        sha256: chunk.sha256,
                        file: chunk.fileURL
                    )
                    try? FileManager.default.removeItem(at: chunk.fileURL)
                    self.record(success: true)
                    return
                } catch { continue }
            }
            self.record(success: false)
        }
    }

    func finish() async -> Progress {
        await withCheckedContinuation { continuation in
            group.notify(queue: .global(qos: .utility)) { continuation.resume() }
        }
        return lock.withLock { Progress(uploaded: uploaded, failed: failed) }
    }

    private func record(success: Bool) {
        let progress = lock.withLock { () -> Progress in
            if success { uploaded += 1 } else { failed += 1 }
            return Progress(uploaded: uploaded, failed: failed)
        }
        onProgress(progress)
    }
}
