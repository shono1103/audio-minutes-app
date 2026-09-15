import Foundation

/// contracts/api/README.md の操作を型付きで提供する。GUI と CLI が共有する。
public final class SessionService: @unchecked Sendable {
    public let client: APIClient
    public let uploader: TusUploader

    public init(client: APIClient) {
        self.client = client
        self.uploader = TusUploader(client: client)
    }

    private func path(_ sessionID: UUID, _ suffix: String = "") -> String {
        "/v1/sessions/\(sessionID.uuidString.lowercased())\(suffix)"
    }

    // MARK: - account

    public func me() async throws -> Me { try await client.get("/v1/me") }
    public func health() async throws -> Health { try await client.get("/v1/health") }
    public func capabilities() async throws -> Capabilities { try await client.get("/v1/capabilities") }

    public func startBrowserReauth() async throws -> BrowserReauthStart {
        try await client.post("/v1/auth/reauth/browser")
    }

    public func browserReauthStatus(_ requestID: String) async throws -> BrowserReauthStatus {
        try await client.get("/v1/auth/reauth/browser/\(requestID)")
    }

    public func regenerateRecoveryCodes() async throws -> RecoveryCodes { try await client.post("/v1/account/recovery-codes") }

    public func passkeys() async throws -> [Passkey] {
        let list: ItemList<Passkey> = try await client.get("/v1/account/passkeys")
        return list.items
    }

    public func deletePasskey(_ id: UUID) async throws {
        try await client.delete("/v1/account/passkeys/\(id.uuidString.lowercased())")
    }

    public func startPasskeyRegistration() async throws -> PasskeyRegistration {
        try await client.post("/v1/account/passkeys/browser")
    }

    public func passkeyRegistrationStatus(_ requestID: String) async throws -> PasskeyRegistration {
        try await client.get("/v1/account/passkeys/browser/\(requestID)")
    }

    public func isAllowedServerBrowserURL(_ raw: String) -> Bool {
        guard let url = URL(string: raw) else { return false }
        return AuthManager.isAllowedReauthURL(url, baseURL: client.baseURL)
    }

    // MARK: - sessions

    public func createSession(_ package: RecordingPackage) async throws -> Session {
        try await client.post("/v1/sessions", body: package)
    }

    public func listSessions(cursor: String? = nil, limit: Int = 50) async throws -> SessionList {
        try await client.get("/v1/sessions", query: ["cursor": cursor, "limit": String(limit)])
    }

    public func session(_ id: UUID) async throws -> Session { try await client.get(path(id)) }

    public func update(_ id: UUID, _ patch: SessionPatch) async throws -> Session { try await client.patch(path(id), body: patch) }

    public func delete(_ id: UUID) async throws { try await client.delete(path(id)) }

    /// 未完了uploadの中止では、既に削除済みのsessionを成功として扱う。
    public func deleteIfExists(_ id: UUID) async throws {
        do { try await delete(id) }
        catch APIClientError.api(_, let status) where status == 404 { return }
        catch APIClientError.unexpectedStatus(let status) where status == 404 { return }
    }

    public func finalize(_ id: UUID) async throws -> Session { try await client.post(path(id, "/finalize")) }

    public func retry(_ id: UUID, stage: String, languageMode: LanguageMode? = nil) async throws -> AcceptedJob {
        try await client.post(path(id, "/retry"), body: RetryRequest(stage: stage, languageMode: languageMode))
    }

    public func jobs(_ id: UUID) async throws -> [JobSummary] {
        let list: ItemList<JobSummary> = try await client.get(path(id, "/jobs"))
        return list.items
    }

    /// 待機中は即時取消、実行中は取消要求を立てる。terminal job への再送は冪等。
    public func cancelJob(_ id: UUID, jobID: UUID) async throws -> JobSummary {
        try await client.post(path(id, "/jobs/\(jobID.uuidString.lowercased())/cancel"))
    }

    public func shares(_ id: UUID) async throws -> [ShareEntry] {
        let list: ItemList<ShareEntry> = try await client.get(path(id, "/shares"))
        return list.items
    }

    public struct ShareRequest: Codable, Sendable {
        public var email: String?
        public var userId: UUID?
        public init(email: String? = nil, userId: UUID? = nil) {
            self.email = email
            self.userId = userId
        }
    }

    /// 追加した 1 件を返す (API は配列ではなく単一の共有先を返す)。
    @discardableResult
    public func share(_ id: UUID, email: String) async throws -> ShareEntry {
        try await client.post(path(id, "/shares"), body: ShareRequest(email: email))
    }

    public func unshare(_ id: UUID, userID: UUID) async throws {
        try await client.delete(path(id, "/shares/\(userID.uuidString.lowercased())"))
    }

    // MARK: - upload

    /// 録音パッケージの全トラックを送信し、完了後に finalize する。片側でも未完なら finalize しない。
    public func uploadAndFinalize(session: Session, package: RecordingPackage, trackFiles: [TrackID: URL],
                                  progress: (@Sendable (UploadProgress) -> Void)? = nil,
                                  uploadURLStore: (@Sendable (String, URL) -> Void)? = nil,
                                  existingUploadURLs: [String: URL] = [:]) async throws -> Session {
        var completed = 0
        for required in package.requiredTrackIDs {
            guard let file = trackFiles[required] else {
                throw APIClientError.invalidURL("トラックのファイルがありません: \(required.rawValue)")
            }
            let sessionTrack = session.tracks.first { $0.trackId == required.rawValue }
            let uploadURL: URL
            if let existing = existingUploadURLs[required.rawValue] {
                uploadURL = existing
            } else if let raw = sessionTrack?.uploadUrl, let url = URL(string: raw) {
                uploadURL = url.host == nil ? try client.url(raw) : url
            } else {
                let length = Int((try? FileManager.default.attributesOfItem(atPath: file.path)[.size] as? NSNumber)?.intValue ?? 0)
                uploadURL = try await uploader.createUpload(sessionID: package.sessionId, trackID: required.rawValue, length: length)
            }
            uploadURLStore?(required.rawValue, uploadURL)
            try await uploader.upload(file: file, to: uploadURL, trackID: required.rawValue, progress: progress)
            completed += 1
        }
        guard completed == package.requiredTrackIDs.count else {
            throw APIClientError.api(ApiErrorBody(code: .uploadIncomplete, message: "必須トラックが揃っていません",
                                                  stage: "upload", retryable: true, requestId: "local",
                                                  retainedArtifacts: nil, details: nil), status: 409)
        }
        return try await finalize(package.sessionId)
    }

    // MARK: - artifacts

    public func transcript(_ id: UUID) async throws -> Transcript { try await client.get(path(id, "/transcript")) }
    public func transcriptMarkdown(_ id: UUID) async throws -> String { try await client.getText(path(id, "/transcript.md")) }
    public func audio(_ id: UUID, trackID: String, range: String? = nil) async throws -> RawResponse {
        try await client.getData(path(id, "/audio/\(trackID)"), range: range)
    }

    public func audioURL(_ id: UUID, trackID: String) throws -> URL { try client.url(path(id, "/audio/\(trackID)")) }

    public static func audioFileExtension(_ response: RawResponse) -> String {
        if let disposition = response.header("Content-Disposition"),
           let name = disposition.split(separator: ";").map({ $0.trimmingCharacters(in: .whitespaces) })
            .first(where: { $0.lowercased().hasPrefix("filename=") })?
            .split(separator: "=", maxSplits: 1).last?.trimmingCharacters(in: CharacterSet(charactersIn: "\"")),
           let ext = name.split(separator: ".").last.map(String.init)?.lowercased(),
           ["wav", "m4a", "mp3", "flac"].contains(ext) { return ext }
        let contentType = response.header("Content-Type")?.split(separator: ";").first?.lowercased()
        return ["audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/flac": "flac",
                "audio/mp4": "m4a", "audio/x-m4a": "m4a"][contentType ?? ""] ?? "bin"
    }

    // MARK: - minutes

    public func currentMinutes(_ id: UUID) async throws -> MinutesDocument { try await client.get(path(id, "/minutes")) }
    public func minutesVersions(_ id: UUID) async throws -> MinutesVersionList {
        try await client.get(path(id, "/minutes/versions"))
    }
    public func minutesVersion(_ id: UUID, versionID: UUID) async throws -> MinutesDocument {
        try await client.get(path(id, "/minutes/versions/\(versionID.uuidString.lowercased())"))
    }
    public func saveManualEdit(_ id: UUID, _ request: ManualEditRequest) async throws -> MinutesVersion {
        let envelope: MinutesVersionEnvelope = try await client.post(path(id, "/minutes/versions"), body: request)
        return envelope.version
    }
    public func regenerate(_ id: UUID, _ request: RegenerateRequest) async throws -> AcceptedJob {
        try await client.post(path(id, "/minutes/regenerate"), body: request)
    }
    public struct SelectVersion: Codable, Sendable {
        public var versionId: UUID
        public init(versionId: UUID) { self.versionId = versionId }
    }
    /// 現在版に選んだ版を返す (API はセッションではなく版を返す)。
    @discardableResult
    public func selectCurrent(_ id: UUID, versionID: UUID) async throws -> MinutesVersion {
        let envelope: MinutesVersionEnvelope = try await client.post(path(id, "/minutes/current"),
                                                                    body: SelectVersion(versionId: versionID))
        return envelope.version
    }
    public func restore(_ id: UUID, versionID: UUID) async throws -> MinutesVersion {
        let envelope: MinutesVersionEnvelope =
            try await client.post(path(id, "/minutes/versions/\(versionID.uuidString.lowercased())/restore"))
        return envelope.version
    }
    public func compare(_ id: UUID, from: UUID, to: UUID) async throws -> MinutesCompare {
        try await client.get(path(id, "/minutes/compare"),
                             query: ["from": from.uuidString.lowercased(), "to": to.uuidString.lowercased()])
    }

    // MARK: - formats

    public func formats() async throws -> [FormatProfile] {
        let list: ItemList<FormatProfile> = try await client.get("/v1/formats")
        return list.items
    }
    public func format(_ id: UUID) async throws -> FormatProfile { try await client.get("/v1/formats/\(id.uuidString.lowercased())") }
    public func createFormat(_ input: FormatProfileInput) async throws -> FormatProfile { try await client.post("/v1/formats", body: input) }
    public func updateFormat(_ id: UUID, _ input: FormatProfileInput) async throws -> FormatProfile {
        try await client.put("/v1/formats/\(id.uuidString.lowercased())", body: input)
    }
    public func deleteFormat(_ id: UUID) async throws { try await client.delete("/v1/formats/\(id.uuidString.lowercased())") }
    public func duplicateFormat(_ id: UUID) async throws -> FormatProfile {
        try await client.post("/v1/formats/\(id.uuidString.lowercased())/duplicate")
    }
    public func setDefaultFormat(_ id: UUID) async throws -> FormatProfile {
        try await client.post("/v1/formats/\(id.uuidString.lowercased())/default")
    }
    public func previewFormat(_ input: FormatProfileInput) async throws -> FormatPreview {
        try await client.post("/v1/formats/preview", body: input)
    }

    // MARK: - admin

    public struct InvitationRequest: Codable, Sendable {
        public var email: String
        public var role: String
        public init(email: String, role: String) {
            self.email = email
            self.role = role
        }
    }

    public func invitations() async throws -> [Invitation] {
        let list: ItemList<Invitation> = try await client.get("/v1/admin/invitations")
        return list.items
    }
    public func invite(email: String, role: String) async throws -> Invitation {
        try await client.post("/v1/admin/invitations", body: InvitationRequest(email: email, role: role))
    }
    public func revokeInvitation(_ id: UUID) async throws { try await client.delete("/v1/admin/invitations/\(id.uuidString.lowercased())") }
    public func users() async throws -> [AdminUser] {
        let list: ItemList<AdminUser> = try await client.get("/v1/admin/users")
        return list.items
    }

    public struct UserPatch: Codable, Sendable {
        public var role: String?
        public var disabled: Bool?
        public init(role: String? = nil, disabled: Bool? = nil) {
            self.role = role
            self.disabled = disabled
        }
    }

    public func updateUser(_ id: UUID, _ patch: UserPatch) async throws -> AdminUser {
        try await client.patch("/v1/admin/users/\(id.uuidString.lowercased())", body: patch)
    }
    public func retention() async throws -> Retention { try await client.get("/v1/admin/retention") }
    public func updateRetention(_ retention: Retention) async throws -> Retention { try await client.put("/v1/admin/retention", body: retention) }

    // MARK: - Claude 接続 (owner 専用)

    public func claudeStatus() async throws -> ClaudeStatus { try await client.get("/v1/admin/claude") }
    public func claudeLogin() async throws -> ClaudeLoginSession { try await client.post("/v1/admin/claude/login") }
    public func claudeLoginStatus(_ id: String) async throws -> ClaudeLoginSession { try await client.get("/v1/admin/claude/login/\(id)") }
    public func claudeCancelLogin(_ id: String) async throws -> ClaudeLoginSession { try await client.post("/v1/admin/claude/login/\(id)/cancel") }
    public struct ClaudeLoginCode: Codable, Sendable { public var code: String; public init(code: String) { self.code = code } }
    public func claudeSubmitLoginCode(_ id: String, code: String) async throws -> ClaudeLoginSession {
        try await client.post("/v1/admin/claude/login/\(id)/code", body: ClaudeLoginCode(code: code))
    }
    public func claudeLogout() async throws -> ClaudeStatus { try await client.post("/v1/admin/claude/logout") }

    /// Anthropic 公式 origin の allowlist。一致しない URL は表示しない (FR-124)。
    public static let claudeAuthOrigins: Set<String> = ["https://claude.ai", "https://console.anthropic.com", "https://platform.claude.com"]

    public static func isAllowedClaudeAuthURL(_ raw: String) -> Bool {
        guard let url = URL(string: raw), url.scheme?.lowercased() == "https", let host = url.host?.lowercased() else { return false }
        return claudeAuthOrigins.contains("https://\(host)")
    }
}
