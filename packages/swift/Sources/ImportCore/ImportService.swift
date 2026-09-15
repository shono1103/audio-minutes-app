import ClientCore
import CryptoKit
import Foundation

/// 既存音声ファイルの取り込み (FR-137〜146)。元ファイルを変更せず管理領域へコピーし、
/// `imported_mixed` の録音パッケージを作る。録音権限は要求しない。
public struct ImportRequest: Sendable {
    public var fileURL: URL
    public var title: String?
    public var languageMode: LanguageMode
    public var allowExternalSend: Bool
    public var formatProfileId: UUID?
    public var destinationOrigin: String?
    public var ownerUserId: UUID?

    public init(fileURL: URL, title: String? = nil, languageMode: LanguageMode = .auto,
                allowExternalSend: Bool = true, formatProfileId: UUID? = nil,
                destinationOrigin: String? = nil, ownerUserId: UUID? = nil) {
        self.fileURL = fileURL
        self.title = title
        self.languageMode = languageMode
        self.allowExternalSend = allowExternalSend
        self.formatProfileId = formatProfileId
        self.destinationOrigin = destinationOrigin
        self.ownerUserId = ownerUserId
    }
}

public struct ImportResult: Sendable {
    public let state: LocalSessionState
    public let probe: AudioProbeResult
    public let copiedFile: URL
}

public final class ImportService: @unchecked Sendable {
    private let store: LocalSessionStore
    private let client: String

    public init(store: LocalSessionStore, client: String = "audio-minutes-client/\(ClientInfo.version)") {
        self.store = store
        self.client = client
    }

    /// ファイル名と取込日時から仮タイトルを作る (FR-141)。basename はタイトル以外へ送らない。
    public static func provisionalTitle(for fileURL: URL, at date: Date = Date()) -> String {
        let base = fileURL.deletingPathExtension().lastPathComponent
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ja_JP")
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        let name = base.isEmpty ? "取り込み音声" : base
        return "\(name) (\(formatter.string(from: date)) 取り込み)"
    }

    public func run(_ request: ImportRequest, now: Date = Date()) throws -> ImportResult {
        let probe = try AudioProbe.probe(request.fileURL)
        let sessionID = UUID()
        let directory = try store.prepare(sessionID: sessionID)
        let fileName = "imported-audio.\(probe.container)"
        let destination = directory.appendingPathComponent(fileName)
        do {
            try FileManager.default.copyItem(at: request.fileURL, to: destination)
        } catch {
            try? FileManager.default.removeItem(at: store.directory(for: sessionID))
            throw ImportError.copyFailed(error.localizedDescription)
        }
        let digest: String
        do {
            digest = try Self.sha256(of: destination)
            let copiedSize = (try FileManager.default.attributesOfItem(atPath: destination.path)[.size] as? NSNumber)?.intValue ?? -1
            guard copiedSize == probe.byteSize else { throw ImportError.copyFailed("サイズが一致しません") }
        } catch {
            try? FileManager.default.removeItem(at: store.directory(for: sessionID))
            throw error
        }
        // 利用者入力とファイル名由来の仮タイトルを区別する (FR-141)。仮タイトルでも
        // 表示用に送るが、titleEditedByUser は false のままにして Claude の提案タイトルを
        // 採用できるようにする。
        let entered = request.title?.trimmingCharacters(in: .whitespacesAndNewlines)
        let titleEditedByUser = !(entered ?? "").isEmpty
        let title = titleEditedByUser ? entered! : Self.provisionalTitle(for: request.fileURL, at: now)
        let track = RecordingTrack(trackId: .importedAudio, role: .mixed, startOffsetMs: 0, container: probe.container,
                                   codec: probe.codec, sampleRate: probe.sampleRate, channels: probe.channels,
                                   durationMs: probe.durationMs, byteSize: probe.byteSize, sha256: digest,
                                   originalContainer: probe.container)
        let package = RecordingPackage(sessionId: sessionID, inputKind: .importedMixed, startedAt: now, title: title,
                                       titleEditedByUser: titleEditedByUser,
                                       languageMode: request.languageMode, allowExternalSend: request.allowExternalSend,
                                       formatProfileId: request.formatProfileId, tracks: [track],
                                       source: RecordingSource(kind: "file", client: client))
        let state = LocalSessionState(package: package, trackFiles: [TrackID.importedAudio.rawValue: fileName],
                                      destinationOrigin: request.destinationOrigin, ownerUserId: request.ownerUserId)
        try store.save(state)
        return ImportResult(state: state, probe: probe, copiedFile: destination)
    }

    public static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1024 * 1024), !chunk.isEmpty {
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
