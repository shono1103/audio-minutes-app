import Foundation
import XCTest

@testable import ClientCore

final class LocalSessionStoreTests: XCTestCase {
    func testRecoveryMetadataCanBeListedWithoutCompletePackage() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = LocalSessionStore(paths: AppPaths(root: root))
        let id = UUID()
        let recovery = LocalRecoveryState(
            sessionId: id, startedAt: Date(timeIntervalSince1970: 100), title: "途中までの録音",
            titleEditedByUser: true, languageMode: .ja, allowExternalSend: false, formatProfileId: nil,
            source: RecordingSource(kind: "app", appName: "Zoom"),
            tracks: [
                LocalRecoveryTrack(trackId: .appAudio, role: .app, fileName: "app-audio.wav",
                                   headerFinalized: false, error: "disk full"),
                LocalRecoveryTrack(trackId: .microphone, role: .microphone, fileName: "microphone.wav",
                                   headerFinalized: true, durationMs: 1_000, byteSize: 32_044,
                                   sha256: String(repeating: "a", count: 64)),
            ],
            error: "app track: disk full"
        )

        try store.saveRecovery(recovery)

        XCTAssertNil(try store.load(sessionID: id), "不完全な録音を upload 再開可能な package と誤認しない")
        let listed = try XCTUnwrap(store.recoveries().first)
        XCTAssertEqual(listed.sessionId, id)
        XCTAssertEqual(listed.title, "途中までの録音")
        XCTAssertEqual(listed.tracks.count, 2)
        XCTAssertTrue(listed.tracks[1].headerFinalized)
        XCTAssertEqual(store.recordingDirectory(for: id).lastPathComponent, "recording")
    }
}
