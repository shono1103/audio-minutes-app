import Foundation

// contracts/schemas/*.v1.schema.json の Swift 表現。JSON Schema が正本で、ここは写し。

public enum InputKind: String, Codable, Sendable, CaseIterable {
    case recordedDualTrack = "recorded_dual_track"
    case importedMixed = "imported_mixed"
}

public enum LanguageMode: String, Codable, Sendable, CaseIterable, Identifiable {
    case auto, ja, en, mixed
    public var id: String { rawValue }
    public var label: String {
        switch self {
        case .auto: return "自動"
        case .ja: return "日本語"
        case .en: return "英語"
        case .mixed: return "日英混在"
        }
    }
}

public enum TrackRole: String, Codable, Sendable {
    case app, microphone, mixed
}

public enum TrackID: String, Codable, Sendable {
    case appAudio = "app-audio"
    case microphone
    case importedAudio = "imported-audio"
}

public struct TimeBase: Codable, Sendable, Equatable {
    public var unit: String = "ms"
    public var origin: String = "recording_start"
    public init() {}
}

public struct RecordingTrack: Codable, Sendable, Equatable {
    public var trackId: TrackID
    public var role: TrackRole
    public var startOffsetMs: Int
    public var container: String
    public var codec: String
    public var sampleRate: Int
    public var channels: Int
    public var durationMs: Int
    public var byteSize: Int
    public var sha256: String
    public var originalContainer: String?

    public init(trackId: TrackID, role: TrackRole, startOffsetMs: Int, container: String, codec: String,
                sampleRate: Int, channels: Int, durationMs: Int, byteSize: Int, sha256: String,
                originalContainer: String? = nil) {
        self.trackId = trackId
        self.role = role
        self.startOffsetMs = startOffsetMs
        self.container = container
        self.codec = codec
        self.sampleRate = sampleRate
        self.channels = channels
        self.durationMs = durationMs
        self.byteSize = byteSize
        self.sha256 = sha256
        self.originalContainer = originalContainer
    }
}

public struct RecordingSource: Codable, Sendable, Equatable {
    public var kind: String
    public var appName: String?
    public var bundleId: String?
    public var tabHost: String?
    public var client: String?

    public init(kind: String, appName: String? = nil, bundleId: String? = nil, tabHost: String? = nil,
                client: String? = ClientInfo.userAgent) {
        self.kind = kind
        self.appName = appName
        self.bundleId = bundleId
        self.tabHost = tabHost
        self.client = client
    }
}

public struct RecordingPackage: Codable, Sendable, Equatable {
    public var schemaVersion: String = "recording-package/2"
    public var sessionId: UUID
    public var inputKind: InputKind
    public var timeBase: TimeBase = TimeBase()
    public var startedAt: Date
    /// 表示用タイトル。未入力でもクライアントが録音元・ファイル名と日時から仮生成する (FR-107 / FR-141)。
    public var title: String?
    /// `title` が利用者の入力・編集によるものなら true。仮タイトルなら false のままにする。
    /// false のときだけ議事録生成で Claude の提案タイトルを採用してよいので、
    /// 「タイトルが空でない」ことを利用者入力の代わりにしない。
    public var titleEditedByUser: Bool
    public var languageMode: LanguageMode
    public var allowExternalSend: Bool
    public var formatProfileId: UUID?
    public var tracks: [RecordingTrack]
    public var source: RecordingSource

    public init(sessionId: UUID = UUID(), inputKind: InputKind, startedAt: Date, title: String?,
                titleEditedByUser: Bool = false, languageMode: LanguageMode, allowExternalSend: Bool,
                formatProfileId: UUID?, tracks: [RecordingTrack], source: RecordingSource) {
        self.sessionId = sessionId
        self.inputKind = inputKind
        self.startedAt = startedAt
        self.title = title
        self.titleEditedByUser = titleEditedByUser
        self.languageMode = languageMode
        self.allowExternalSend = allowExternalSend
        self.formatProfileId = formatProfileId
        self.tracks = tracks
        self.source = source
    }

    public var requiredTrackIDs: [TrackID] {
        inputKind == .recordedDualTrack ? [.appAudio, .microphone] : [.importedAudio]
    }
}

public enum ClientInfo {
    public static let version = "0.1.0"
    public static var userAgent: String { "audio-minutes-client/\(version)" }
}

// --- error -------------------------------------------------------------------

/// contracts/schemas/error.v1.schema.json の error_code。未知のコードも decode できるよう
/// enum ではなく RawRepresentable にしている (契約の追加でクライアントが壊れないようにする)。
public struct ErrorCode: RawRepresentable, Codable, Sendable, Equatable, Hashable {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }

    public static let invalidRequest = ErrorCode(rawValue: "invalid_request")
    public static let unauthorized = ErrorCode(rawValue: "unauthorized")
    public static let forbidden = ErrorCode(rawValue: "forbidden")
    public static let notFound = ErrorCode(rawValue: "not_found")
    public static let conflict = ErrorCode(rawValue: "conflict")
    public static let rateLimited = ErrorCode(rawValue: "rate_limited")
    public static let invalidInput = ErrorCode(rawValue: "invalid_input")
    public static let unsupportedFormat = ErrorCode(rawValue: "unsupported_format")
    public static let limitExceeded = ErrorCode(rawValue: "limit_exceeded")
    public static let checksumMismatch = ErrorCode(rawValue: "checksum_mismatch")
    public static let uploadIncomplete = ErrorCode(rawValue: "upload_incomplete")
    public static let uploadExpired = ErrorCode(rawValue: "upload_expired")
    public static let decodeFailed = ErrorCode(rawValue: "decode_failed")
    public static let engineCrashed = ErrorCode(rawValue: "engine_crashed")
    public static let timeout = ErrorCode(rawValue: "timeout")
    public static let cancelled = ErrorCode(rawValue: "cancelled")
    public static let gpuUnavailable = ErrorCode(rawValue: "gpu_unavailable")
    public static let unsupportedModel = ErrorCode(rawValue: "unsupported_model")
    public static let backendError = ErrorCode(rawValue: "backend_error")
    public static let claudeNotAuthenticated = ErrorCode(rawValue: "claude_not_authenticated")
    public static let claudeCredentialConflict = ErrorCode(rawValue: "claude_credential_conflict")
    public static let claudeRateLimited = ErrorCode(rawValue: "claude_rate_limited")
    public static let claudeCliIncompatible = ErrorCode(rawValue: "claude_cli_incompatible")
    public static let claudeOutputInvalid = ErrorCode(rawValue: "claude_output_invalid")
    public static let claudeUnknownOutcome = ErrorCode(rawValue: "claude_unknown_outcome")
    public static let externalSendForbidden = ErrorCode(rawValue: "external_send_forbidden")
    public static let notConnectedOwner = ErrorCode(rawValue: "not_connected_owner")
    public static let audioDeleted = ErrorCode(rawValue: "audio_deleted")
    public static let internalError = ErrorCode(rawValue: "internal")

    /// 契約が定義する既知のコード。未知のコードとの区別に使う。
    public static let known: Set<ErrorCode> = [
        .invalidRequest,
        .unauthorized,
        .forbidden,
        .notFound,
        .conflict,
        .rateLimited,
        .invalidInput,
        .unsupportedFormat,
        .limitExceeded,
        .checksumMismatch,
        .uploadIncomplete,
        .uploadExpired,
        .decodeFailed,
        .engineCrashed,
        .timeout,
        .cancelled,
        .gpuUnavailable,
        .unsupportedModel,
        .backendError,
        .claudeNotAuthenticated,
        .claudeCredentialConflict,
        .claudeRateLimited,
        .claudeCliIncompatible,
        .claudeOutputInvalid,
        .claudeUnknownOutcome,
        .externalSendForbidden,
        .notConnectedOwner,
        .audioDeleted,
        .internalError,
    ]
}

public struct ApiErrorBody: Codable, Sendable, Equatable {
    public var code: ErrorCode
    public var message: String
    public var stage: String?
    public var retryable: Bool?
    public var requestId: String
    public var retainedArtifacts: [String]?
    public var details: JSONValue?
}

public struct ApiError: Codable, Sendable, Equatable {
    public var error: ApiErrorBody
}

public struct JobFailure: Codable, Sendable, Equatable {
    public var code: ErrorCode
    public var stage: String
    public var retryable: Bool
    public var message: String
    public var exitCode: Int?
    public var retainedArtifacts: [String]?
    public var diagnostics: JSONValue?
}

// --- session -----------------------------------------------------------------

public enum SessionStatus: String, Codable, Sendable, CaseIterable {
    case uploading, validating, queued, transcribing, transcribed
    case queuedMinutes = "queued_minutes"
    case generatingMinutes = "generating_minutes"
    case completed, failed, deleting

    public var label: String {
        switch self {
        case .uploading: return "アップロード中"
        case .validating: return "検証中"
        case .queued: return "待機中"
        case .transcribing: return "文字起こし中"
        case .transcribed: return "文字起こし完了 (送信なし)"
        case .queuedMinutes: return "議事録待機中"
        case .generatingMinutes: return "議事録生成中"
        case .completed: return "完了"
        case .failed: return "失敗"
        case .deleting: return "削除中"
        }
    }

    public var isTerminal: Bool { self == .completed || self == .failed || self == .transcribed }
}

public struct SessionTrack: Codable, Sendable, Equatable {
    public var trackId: String
    public var role: TrackRole
    public var uploadState: String
    public var uploadId: String?
    public var uploadUrl: String?
    public var uploadExpiresAt: Date?
    public var artifactId: String?
}

public struct Session: Codable, Sendable, Equatable, Identifiable {
    public var schemaVersion: String
    public var sessionId: UUID
    public var ownerId: UUID
    public var title: String
    public var titleEditedByUser: Bool
    public var titleRevision: Int?
    public var inputKind: InputKind
    public var languageMode: LanguageMode
    public var allowExternalSend: Bool
    public var formatSnapshot: FormatProfile?
    public var status: SessionStatus
    public var failure: JobFailure?
    public var startedAt: Date?
    public var durationMs: Int?
    public var createdAt: Date
    public var updatedAt: Date
    public var tracks: [SessionTrack]
    public var currentMinutesVersionId: UUID?
    public var transcriptRevision: Int?
    public var reviewCount: Int?
    public var audioRetained: Bool
    public var audioExpiresAt: Date?
    public var sharedWith: [UUID]
    public var isSharedView: Bool?

    public var id: UUID { sessionId }
}

/// API の一覧応答は `{"items": [...]}` の envelope。配列を直接 decode すると
/// 空一覧でも必ず typeMismatch になるため、envelope を型として持つ (R06)。
public struct ItemList<Element: Codable & Sendable>: Codable, Sendable {
    public var items: [Element]
    public init(items: [Element] = []) { self.items = items }
}

/// 議事録の版を返す応答 (`{"version": {...}}`)。
public struct MinutesVersionEnvelope: Codable, Sendable {
    public var version: MinutesVersion
}

/// 版履歴 (`{"items": [...], "current_minutes_version_id": ...}`)。
public struct MinutesVersionList: Codable, Sendable {
    public var items: [MinutesVersion]
    public var currentMinutesVersionId: UUID?
}

public struct SessionList: Codable, Sendable {
    public var items: [Session]
    public var nextCursor: String?
}

public struct SessionPatch: Codable, Sendable {
    public var title: String?
    public var languageMode: LanguageMode?
    public var allowExternalSend: Bool?
    public init(title: String? = nil, languageMode: LanguageMode? = nil, allowExternalSend: Bool? = nil) {
        self.title = title
        self.languageMode = languageMode
        self.allowExternalSend = allowExternalSend
    }
}

public struct RetryRequest: Codable, Sendable {
    public var stage: String
    public var languageMode: LanguageMode?
    public init(stage: String, languageMode: LanguageMode? = nil) {
        self.stage = stage
        self.languageMode = languageMode
    }
}

public struct JobSummary: Codable, Sendable, Equatable, Identifiable {
    public var jobId: UUID
    public var kind: String
    public var status: String
    public var attempt: Int
    public var maxAttempts: Int
    public var cancelRequested: Bool?
    public var createdAt: Date
    public var startedAt: Date?
    public var finishedAt: Date?
    public var failure: JobFailure?
    public var id: UUID { jobId }
}

public struct ShareEntry: Codable, Sendable, Equatable, Identifiable {
    public var userId: UUID
    public var email: String?
    /// 一覧だけが返す。共有追加の応答には含まれない。
    public var createdAt: Date?
    public var id: UUID { userId }
}

// --- transcript --------------------------------------------------------------

public struct Word: Codable, Sendable, Equatable {
    public var startMs: Int
    public var endMs: Int
    public var word: String
    public var probability: Double?
}

public struct Segment: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var trackId: String
    public var source: TrackRole
    public var startMs: Int
    public var endMs: Int
    public var text: String
    public var language: String
    public var languageProbability: Double?
    public var modelId: String
    public var modelRevision: String
    public var engine: String
    public var engineVersion: String
    public var strategy: String
    public var avgLogprob: Double?
    public var noSpeechProb: Double?
    public var words: [Word]?
    public var replacedByReview: String?
}

public struct ReviewAttempt: Codable, Sendable, Equatable {
    public var attempt: Int
    public var modelId: String
    public var modelRevision: String
    public var language: String?
    public var languageProbability: Double?
    public var candidateText: String?
    public var accepted: Bool
    public var reason: String
}

public struct ReviewItem: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var trackId: String
    public var startMs: Int
    public var endMs: Int
    public var reason: String
    public var attempts: [ReviewAttempt]
    public var resolved: Bool
}

public struct TranscriptTrack: Codable, Sendable, Equatable {
    public var trackId: String
    public var role: TrackRole
    public var sourceArtifactId: String
    public var startOffsetMs: Int
    public var durationMs: Int
    public var normalization: JSONValue?
}

public struct Backend: Codable, Sendable, Equatable {
    public var requestedBackend: String
    public var effectiveBackend: String
    public var gpuVerified: Bool
    public var gpuName: String?
    public var driver: String?
    public var vm: String?
    public var fallbackReason: String?
    public var softwareRendererDetected: Bool?
}

public struct ProcessingModel: Codable, Sendable, Equatable {
    public var profile: String
    public var modelId: String
    public var modelRevision: String
    public var computeType: String?
    public var quantization: String?
    public var fileSha256: String?
}

public struct Processing: Codable, Sendable, Equatable {
    public var engine: String
    public var engineVersion: String
    public var strategy: String
    public var models: [ProcessingModel]
    public var decode: JSONValue?
    public var backend: Backend
    public var resources: JSONValue
    public var timings: JSONValue
}

public struct Transcript: Codable, Sendable, Equatable {
    public var schemaVersion: String
    public var sessionId: UUID
    public var revision: Int
    public var inputKind: InputKind
    public var languageMode: LanguageMode
    public var tracks: [TranscriptTrack]
    public var segments: [Segment]
    public var review: [ReviewItem]
    public var processing: Processing

    public var unresolvedReview: [ReviewItem] { review.filter { !$0.resolved } }
}

// --- minutes / format --------------------------------------------------------

public struct FormatSection: Codable, Sendable, Equatable, Identifiable, Hashable {
    public var key: String
    public var title: String
    public var enabled: Bool
    public var instructions: String?
    public var id: String { key }
    public init(key: String, title: String, enabled: Bool, instructions: String? = nil) {
        self.key = key
        self.title = title
        self.enabled = enabled
        self.instructions = instructions
    }
}

public struct FormatProfile: Codable, Sendable, Equatable, Identifiable, Hashable {
    public var schemaVersion: String = "format-profile/1"
    public var profileId: UUID
    public var ownerId: UUID?
    public var version: Int
    public var name: String
    public var outputLanguage: String
    public var builtin: Bool?
    public var isDefault: Bool?
    public var sections: [FormatSection]
    public var additionalInstructions: String
    public var templateMarkdown: String
    public var updatedAt: Date?

    public var id: UUID { profileId }

    public init(profileId: UUID = UUID(), ownerId: UUID? = nil, version: Int = 1, name: String,
                outputLanguage: String = "ja", builtin: Bool? = false, isDefault: Bool? = false,
                sections: [FormatSection], additionalInstructions: String = "", templateMarkdown: String,
                updatedAt: Date? = nil) {
        self.profileId = profileId
        self.ownerId = ownerId
        self.version = version
        self.name = name
        self.outputLanguage = outputLanguage
        self.builtin = builtin
        self.isDefault = isDefault
        self.sections = sections
        self.additionalInstructions = additionalInstructions
        self.templateMarkdown = templateMarkdown
        self.updatedAt = updatedAt
    }
}

public struct FormatProfileInput: Codable, Sendable {
    public var name: String
    public var outputLanguage: String
    public var sections: [FormatSection]
    public var additionalInstructions: String
    public var templateMarkdown: String
    public init(name: String, outputLanguage: String, sections: [FormatSection], additionalInstructions: String,
                templateMarkdown: String) {
        self.name = name
        self.outputLanguage = outputLanguage
        self.sections = sections
        self.additionalInstructions = additionalInstructions
        self.templateMarkdown = templateMarkdown
    }
}

public struct FormatPreview: Codable, Sendable {
    public var markdown: String
    public var warnings: [String]?
}

public struct MinutesVersion: Codable, Sendable, Equatable, Identifiable {
    public var schemaVersion: String
    public var versionId: UUID
    public var sessionId: UUID
    public var versionNumber: Int
    public var kind: String
    public var createdBy: String
    public var createdAt: Date
    public var parentVersionId: UUID?
    public var restoredFromVersionId: UUID?
    public var transcriptRevision: Int?
    public var formatSnapshot: FormatProfile?
    public var instructions: String?
    public var insufficientInformation: [String]?
    public var titleProposal: String?
    public var artifactId: String
    public var isCandidate: Bool?
    public var id: UUID { versionId }

    public var kindLabel: String {
        switch kind {
        case "claude_generated": return "Claude 生成"
        case "manual_edit": return "手動編集"
        case "claude_regenerated": return "Claude 再生成"
        case "restored": return "復元"
        default: return kind
        }
    }
}

public struct MinutesDocument: Codable, Sendable, Equatable {
    public var version: MinutesVersion
    public var bodyMarkdown: String
}

public struct ManualEditRequest: Codable, Sendable {
    public var parentVersionId: UUID
    public var bodyMarkdown: String
    public var expectedCurrentVersionId: UUID?
    public init(parentVersionId: UUID, bodyMarkdown: String, expectedCurrentVersionId: UUID?) {
        self.parentVersionId = parentVersionId
        self.bodyMarkdown = bodyMarkdown
        self.expectedCurrentVersionId = expectedCurrentVersionId
    }
}

public struct RegenerateRequest: Codable, Sendable {
    public var baseVersionId: UUID?
    public var instructions: String
    public var formatProfileId: UUID?
    public var useSnapshot: Bool
    public init(baseVersionId: UUID?, instructions: String, formatProfileId: UUID?, useSnapshot: Bool) {
        self.baseVersionId = baseVersionId
        self.instructions = instructions
        self.formatProfileId = formatProfileId
        self.useSnapshot = useSnapshot
    }
}

public struct MinutesCompare: Codable, Sendable {
    public var from: UUID
    public var to: UUID
    public var diff: String
}

/// 非同期処理を受け付けた応答 (`POST /retry`、`POST /minutes/regenerate`)。
/// API は投入した job と、更新後のセッションをまとめて返す。
public struct AcceptedJob: Codable, Sendable {
    public var jobId: UUID
    public var session: Session
}

// --- account / admin ---------------------------------------------------------

public struct Me: Codable, Sendable, Equatable {
    public var userId: UUID
    public var email: String
    public var role: String
    public var reauthValidUntil: Date?
    public var passkeyCount: Int?
    public var defaultFormatProfileId: UUID?
    public var isOwner: Bool { role == "owner" }
}

public struct BrowserReauthStart: Codable, Sendable, Equatable {
    public var requestId: String
    public var reauthUrl: String
    public var expiresAt: Date
}

public struct BrowserReauthStatus: Codable, Sendable, Equatable {
    public var requestId: String?
    public var status: String
    public var expiresAt: Date?
    public var reauthValidUntil: Date?
}

public struct Health: Codable, Sendable {
    public var status: String
    public var version: String?
}

public struct Capabilities: Codable, Sendable {
    public var version: String?
    public var contracts: JSONValue?
    public var database: String?
    public var workers: [WorkerStatus]?
    public var transcriptionAvailable: Bool?
    public var minutesAvailable: Bool?
    public var claude: ClaudeAvailability?
    public var limits: JSONValue?
    public var pollIntervalMs: Int?

    public struct WorkerStatus: Codable, Sendable {
        public var workerId: String
        public var kind: String
        public var version: String?
        public var alive: Bool
        public var heartbeatAt: Date?
        public var backend: JSONValue?
        public var models: JSONValue?
        public var claudeState: String?
    }

    public struct ClaudeAvailability: Codable, Sendable {
        public var state: String
        public var connectedOwnerIsMe: Bool?
    }
}

public struct Invitation: Codable, Sendable, Identifiable {
    public var id: UUID
    public var email: String
    public var role: String
    public var expiresAt: Date
    /// 発行直後の応答だけに含まれる。一覧では返らない。
    public var url: String?
    public var usedAt: Date?
    public var revokedAt: Date?
}

public struct AdminUser: Codable, Sendable, Identifiable {
    public var id: UUID
    public var email: String
    public var role: String
    public var disabled: Bool
    /// 一覧だけが返す。PATCH の応答には含まれない。
    public var createdAt: Date?
}

public struct Retention: Codable, Sendable, Equatable {
    public var uploadHours: Int
    public var audioDays: Int
    public var logDays: Int
    public init(uploadHours: Int, audioDays: Int, logDays: Int) {
        self.uploadHours = uploadHours
        self.audioDays = audioDays
        self.logDays = logDays
    }
}

/// `GET /v1/admin/claude` の全項目。`POST /v1/admin/claude/logout` は state だけを返すため、
/// state 以外はすべて optional にしてある。
public struct ClaudeStatus: Codable, Sendable, Equatable {
    public var state: String
    public var cliVersion: String?
    public var cliSupported: Bool?
    public var checkedAt: Date?
    public var conflictEnvVars: [String]?
    public var connectedOwnerId: UUID?
    public var connectedOwnerIsMe: Bool?
    public var detail: String?

    public var label: String {
        switch state {
        case "cli_missing": return "CLI 未導入"
        case "checking": return "確認中"
        case "logged_out": return "未ログイン"
        case "login_pending": return "ログイン待ち"
        case "logged_in": return "subscription 接続済み"
        case "expired": return "認証失効・再ログインが必要"
        case "credential_conflict": return "API 課金用資格情報との競合"
        case "rate_limited": return "利用上限到達"
        case "cli_incompatible": return "CLI 互換性エラー"
        default: return state
        }
    }
}

public struct ClaudeLoginSession: Codable, Sendable, Equatable {
    public var authSessionId: String
    public var state: String
    public var url: String?
    public var expiresAt: Date?
    public var failureCode: String?
}

public struct RecoveryCodes: Codable, Sendable {
    public var recoveryCodes: [String]
}

public struct Passkey: Codable, Sendable, Identifiable {
    public var id: UUID
    public var label: String?
    public var createdAt: Date?
    public var lastUsedAt: Date?
}

/// 既存accountへWeb画面経由でパスキーを追加する非同期request。
/// WebAuthn option/credential はnative clientへ渡さず、server origin内で完結させる。
public struct PasskeyRegistration: Codable, Sendable, Equatable {
    public var requestId: String
    public var status: String
    public var passkeyUrl: String?
    public var expiresAt: Date?
}
