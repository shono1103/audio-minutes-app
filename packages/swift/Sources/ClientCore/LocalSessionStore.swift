import Foundation

public struct LocalSessionBinding: Sendable, Equatable {
    public var destinationOrigin: String?
    public var ownerUserId: UUID?

    public init(destinationOrigin: String?, ownerUserId: UUID?) {
        self.destinationOrigin = destinationOrigin
        self.ownerUserId = ownerUserId
    }

    public static func capture(apiURL: URL?, currentUser: Me?) -> Self {
        .init(destinationOrigin: apiURL.flatMap { try? APIClient.canonicalOrigin(for: $0) },
              ownerUserId: currentUser?.userId)
    }
}

/// ローカル録音パッケージと未完了 upload の再開情報 (FR-104、FR-105)。
/// `sessions/<session_id>/recording/metadata.json` と各トラックファイルを置く。
public struct LocalSessionState: Codable, Sendable, Equatable {
    public var package: RecordingPackage
    public var trackFiles: [String: String]          // track_id -> recording/ 内の相対ファイル名
    /// 作成時に固定した送信先originと所有者。nilはoffline作成またはlegacyであり、自動再割当しない。
    public var destinationOrigin: String?
    public var ownerUserId: UUID?
    // `JSONDecoder.convertFromSnakeCase` は `upload_urls` を `uploadUrls` にするため、保存名もこの綴りにする。
    private var uploadUrls: [String: String]         // track_id -> tus upload URL
    public var uploadURLs: [String: String] {
        get { uploadUrls }
        set { uploadUrls = newValue }
    }
    public var completedTracks: [String]
    public var finalized: Bool
    public var serverSessionKnown: Bool
    public var lastError: String?
    public var updatedAt: Date

    public init(package: RecordingPackage, trackFiles: [String: String],
                destinationOrigin: String? = nil, ownerUserId: UUID? = nil) {
        self.package = package
        self.trackFiles = trackFiles
        self.destinationOrigin = destinationOrigin
        self.ownerUserId = ownerUserId
        self.uploadUrls = [:]
        self.completedTracks = []
        self.finalized = false
        self.serverSessionKnown = false
        self.lastError = nil
        self.updatedAt = Date()
    }

    public var pendingUpload: Bool { !finalized }
}

public enum LocalSessionError: Error, LocalizedError, Sendable, Equatable {
    case destinationUnknown
    case destinationMismatch(expected: String, actual: String)
    case ownerUnknown
    case ownerMismatch(expected: UUID, actual: UUID)
    case notPending

    public var errorDescription: String? {
        switch self {
        case .destinationUnknown:
            return "この録音の送信先が記録されていないため、自動送信できません"
        case .destinationMismatch(let expected, let actual):
            return "この録音の送信先（\(expected)）と現在の送信先（\(actual)）が一致しません"
        case .ownerUnknown:
            return "この録音の所有者が未割当のため、自動送信できません"
        case .ownerMismatch(let expected, let actual):
            return "この録音の所有者（\(expected.uuidString.lowercased())）と現在の利用者（\(actual.uuidString.lowercased())）が一致しません"
        case .notPending:
            return "この録音には未完了アップロードがありません"
        }
    }
}

/// 録音確定に失敗した場合に、利用者が残った WAV を判別して回収するための情報。
/// server へ送れる完全な package とは分離し、部分トラックを成功扱いしない。
public struct LocalRecoveryTrack: Codable, Sendable, Equatable, Identifiable {
    public var trackId: TrackID
    public var role: TrackRole
    public var fileName: String
    public var headerFinalized: Bool
    public var durationMs: Int?
    public var byteSize: Int?
    public var sha256: String?
    public var error: String?

    public var id: TrackID { trackId }

    public init(trackId: TrackID, role: TrackRole, fileName: String, headerFinalized: Bool,
                durationMs: Int? = nil, byteSize: Int? = nil, sha256: String? = nil, error: String? = nil) {
        self.trackId = trackId
        self.role = role
        self.fileName = fileName
        self.headerFinalized = headerFinalized
        self.durationMs = durationMs
        self.byteSize = byteSize
        self.sha256 = sha256
        self.error = error
    }
}

public struct LocalRecoveryState: Codable, Sendable, Equatable, Identifiable {
    public var schemaVersion: String = "local-recovery/1"
    public var sessionId: UUID
    public var startedAt: Date
    public var title: String
    public var titleEditedByUser: Bool
    public var languageMode: LanguageMode
    public var allowExternalSend: Bool
    public var formatProfileId: UUID?
    public var source: RecordingSource
    public var tracks: [LocalRecoveryTrack]
    public var error: String
    public var savedAt: Date

    public var id: UUID { sessionId }

    public init(sessionId: UUID, startedAt: Date, title: String, titleEditedByUser: Bool,
                languageMode: LanguageMode, allowExternalSend: Bool, formatProfileId: UUID?,
                source: RecordingSource, tracks: [LocalRecoveryTrack], error: String, savedAt: Date = Date()) {
        self.sessionId = sessionId
        self.startedAt = startedAt
        self.title = title
        self.titleEditedByUser = titleEditedByUser
        self.languageMode = languageMode
        self.allowExternalSend = allowExternalSend
        self.formatProfileId = formatProfileId
        self.source = source
        self.tracks = tracks
        self.error = error
        self.savedAt = savedAt
    }
}

public final class LocalSessionStore: @unchecked Sendable {
    public let paths: AppPaths
    private let lock = NSLock()

    public init(paths: AppPaths = AppPaths()) { self.paths = paths }

    public func directory(for sessionID: UUID) -> URL {
        paths.sessionsDir.appendingPathComponent(sessionID.uuidString.lowercased(), isDirectory: true)
    }

    public func recordingDirectory(for sessionID: UUID) -> URL {
        directory(for: sessionID).appendingPathComponent("recording", isDirectory: true)
    }

    public func metadataURL(for sessionID: UUID) -> URL {
        recordingDirectory(for: sessionID).appendingPathComponent("metadata.json")
    }

    public func recoveryURL(for sessionID: UUID) -> URL {
        recordingDirectory(for: sessionID).appendingPathComponent("recovery.json")
    }

    public func prepare(sessionID: UUID) throws -> URL {
        let directory = recordingDirectory(for: sessionID)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        return directory
    }

    public func save(_ state: LocalSessionState) throws {
        lock.lock(); defer { lock.unlock() }
        var copy = state
        copy.updatedAt = Date()
        let directory = try prepare(sessionID: state.package.sessionId)
        let data = try ContractCoding.encoder(pretty: true).encode(copy)
        try data.write(to: directory.appendingPathComponent("metadata.json"), options: [.atomic])
    }

    /// 録音確定前の障害でも WAV を削除せず、再起動後に利用者が回収できる情報を残す。
    public func saveRecovery(_ recovery: LocalRecoveryState) throws {
        lock.lock(); defer { lock.unlock() }
        let directory = try prepare(sessionID: recovery.sessionId)
        let data = try ContractCoding.encoder(pretty: true).encode(recovery)
        try data.write(to: directory.appendingPathComponent("recovery.json"), options: [.atomic])
    }

    /// 開始直後など詳細情報を組み立てられない障害向けの互換入口。
    public func saveRecovery(sessionID: UUID, error: String) throws {
        try saveRecovery(LocalRecoveryState(
            sessionId: sessionID, startedAt: Date(), title: "回収可能な録音", titleEditedByUser: false,
            languageMode: .auto, allowExternalSend: false, formatProfileId: nil,
            source: RecordingSource(kind: "unknown"), tracks: [], error: error
        ))
    }

    public func load(sessionID: UUID) throws -> LocalSessionState? {
        let url = metadataURL(for: sessionID)
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        return try ContractCoding.decoder().decode(LocalSessionState.self, from: try Data(contentsOf: url))
    }

    public func loadRecovery(sessionID: UUID) throws -> LocalRecoveryState? {
        let url = recoveryURL(for: sessionID)
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        return try ContractCoding.decoder().decode(LocalRecoveryState.self, from: try Data(contentsOf: url))
    }

    public func trackFileURL(_ state: LocalSessionState, trackID: TrackID) -> URL? {
        guard let name = state.trackFiles[trackID.rawValue] else { return nil }
        return recordingDirectory(for: state.package.sessionId).appendingPathComponent(name)
    }

    public func trackFileURLs(_ state: LocalSessionState) -> [TrackID: URL] {
        var result: [TrackID: URL] = [:]
        for (key, name) in state.trackFiles {
            if let track = TrackID(rawValue: key) {
                result[track] = recordingDirectory(for: state.package.sessionId).appendingPathComponent(name)
            }
        }
        return result
    }

    public func listAll() throws -> [LocalSessionState] {
        guard FileManager.default.fileExists(atPath: paths.sessionsDir.path) else { return [] }
        let entries = try FileManager.default.contentsOfDirectory(at: paths.sessionsDir, includingPropertiesForKeys: nil)
        return entries.compactMap { entry in
            guard let id = UUID(uuidString: entry.lastPathComponent) else { return nil }
            return try? load(sessionID: id)
        }.sorted { $0.package.startedAt > $1.package.startedAt }
    }

    /// 次回起動時に再開すべき未完了 upload (FR-104)。
    public func pendingUploads() throws -> [LocalSessionState] {
        try listAll().filter(\.pendingUpload)
    }

    public func recoveries() throws -> [LocalRecoveryState] {
        guard FileManager.default.fileExists(atPath: paths.sessionsDir.path) else { return [] }
        let entries = try FileManager.default.contentsOfDirectory(at: paths.sessionsDir, includingPropertiesForKeys: nil)
        return entries.compactMap { entry in
            guard let id = UUID(uuidString: entry.lastPathComponent) else { return nil }
            return try? loadRecovery(sessionID: id)
        }.sorted { $0.startedAt > $1.startedAt }
    }

    public func remove(sessionID: UUID) throws {
        lock.lock(); defer { lock.unlock() }
        let directory = directory(for: sessionID)
        if FileManager.default.fileExists(atPath: directory.path) {
            try FileManager.default.removeItem(at: directory)
        }
    }

    /// ローカルに固定した送信先・所有者と、現在の接続先・認証主体を照合する。
    /// legacy / offline未割当を現在の利用者へ暗黙に割り当てることはしない。
    @discardableResult
    public func validateDestinationAndOwner(_ state: LocalSessionState, service: SessionService) async throws -> Me {
        guard let expectedOrigin = state.destinationOrigin else { throw LocalSessionError.destinationUnknown }
        let actualOrigin = service.client.canonicalOrigin
        guard expectedOrigin == actualOrigin else {
            throw LocalSessionError.destinationMismatch(expected: expectedOrigin, actual: actualOrigin)
        }
        guard let expectedOwner = state.ownerUserId else { throw LocalSessionError.ownerUnknown }
        let current = try await service.me()
        guard expectedOwner == current.userId else {
            throw LocalSessionError.ownerMismatch(expected: expectedOwner, actual: current.userId)
        }
        return current
    }

    /// サーバーへ送って finalize まで行い、ローカル状態を更新する。
    public func uploadPending(_ state: LocalSessionState, service: SessionService,
                              progress: (@Sendable (UploadProgress) -> Void)? = nil) async throws -> Session {
        guard state.pendingUpload else { throw LocalSessionError.notPending }
        try await validateDestinationAndOwner(state, service: service)
        var working = state
        let session: Session
        do {
            session = try await service.createSession(working.package)   // session_id で冪等
        } catch {
            working.lastError = String(describing: error)
            try save(working)
            throw error
        }
        working.serverSessionKnown = true
        try save(working)
        let existing = working.uploadURLs.compactMapValues { URL(string: $0) }
        let store = self
        let sessionID = working.package.sessionId
        let result = try await service.uploadAndFinalize(
            session: session, package: working.package, trackFiles: trackFileURLs(working), progress: progress,
            uploadURLStore: { trackID, url in
                if var latest = try? store.load(sessionID: sessionID) {
                    latest.uploadURLs[trackID] = url.absoluteString
                    try? store.save(latest)
                }
            },
            existingUploadURLs: existing)
        if var latest = try load(sessionID: sessionID) {
            latest.finalized = true
            latest.completedTracks = latest.package.requiredTrackIDs.map(\.rawValue)
            latest.lastError = nil
            try save(latest)
        }
        return result
    }


    /// server session削除を主操作にし、配下のpending/completed tusを一括取消してからローカル録音を削除する。
    /// 完了済みtus単体のTerminationは409になるため呼ばない。serverが既に削除済みでも成功とし、
    /// 通信・認証・権限・競合などでsession削除を確認できない場合はローカルを保持する。
    public func cancelPending(_ state: LocalSessionState, service: SessionService) async throws {
        guard state.pendingUpload else { throw LocalSessionError.notPending }
        try await validateDestinationAndOwner(state, service: service)

        do {
            try await service.deleteIfExists(state.package.sessionId)
        } catch {
            var retained = state
            retained.lastError = "アップロード中止に失敗しました: \(error.localizedDescription)"
            try? save(retained)
            throw error
        }
        try remove(sessionID: state.package.sessionId)
    }
}
