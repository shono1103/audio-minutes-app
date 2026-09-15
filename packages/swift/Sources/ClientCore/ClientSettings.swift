import Foundation

/// `~/Library/Application Support/AudioMinutes/settings.json`。GUI と CLI が共有する (docs/integration-contract.md)。
public struct ClientSettings: Codable, Sendable, Equatable {
    public var schemaVersion: String = "client-settings/1"
    public var apiBaseUrl: String
    public var deployDir: String?
    public var profile: String
    public var dockerContext: String?
    public var colimaProfile: String?
    public var defaultLanguageMode: LanguageMode
    public var defaultFormatProfileId: UUID?
    public var lastMicrophoneUID: String?
    public var lastTarget: LastTarget?
    public var notifications: Notifications
    public var claudeSendDefault: Bool

    public struct LastTarget: Codable, Sendable, Equatable {
        public var kind: String        // app | chrome_tab
        public var bundleId: String
        public var tabTitle: String?
        public init(kind: String, bundleId: String, tabTitle: String? = nil) {
            self.kind = kind
            self.bundleId = bundleId
            self.tabTitle = tabTitle
        }
    }

    public struct Notifications: Codable, Sendable, Equatable {
        public var minutesCompleted: Bool
        public var processingFailed: Bool

        public init(minutesCompleted: Bool = true, processingFailed: Bool = true) {
            self.minutesCompleted = minutesCompleted
            self.processingFailed = processingFailed
        }
    }

    /// 既定はサーバー側 AM_PUBLIC_BASE_URL の canonical origin と揃える (WebAuthn の RP ID と
    /// upload URL の same-origin 検査が同じ host を前提にするため)。
    public init(apiBaseUrl: String = "http://localhost:8787", deployDir: String? = nil,
                profile: String = "macos-colima-cpu", dockerContext: String? = "colima", colimaProfile: String? = "default",
                defaultLanguageMode: LanguageMode = .auto, defaultFormatProfileId: UUID? = nil,
                lastMicrophoneUID: String? = nil, lastTarget: LastTarget? = nil,
                notifications: Notifications = Notifications(), claudeSendDefault: Bool = true) {
        self.apiBaseUrl = apiBaseUrl
        self.deployDir = deployDir
        self.profile = profile
        self.dockerContext = dockerContext
        self.colimaProfile = colimaProfile
        self.defaultLanguageMode = defaultLanguageMode
        self.defaultFormatProfileId = defaultFormatProfileId
        self.lastMicrophoneUID = lastMicrophoneUID
        self.lastTarget = lastTarget
        self.notifications = notifications
        self.claudeSendDefault = claudeSendDefault
    }

    public static let knownProfiles = ["macos-colima-cpu", "macos-colima-krunkit-vulkan", "linux-cpu"]

    /// deploy/settings.schema.json と同じ規則で検証する。
    public func validate() throws {
        guard let url = URL(string: apiBaseUrl) else { throw SettingsError.invalid("api_base_url が URL ではありません") }
        try APIClient.validate(baseURL: url)
        guard Self.knownProfiles.contains(profile) else { throw SettingsError.invalid("profile が不明です: \(profile)") }
        guard schemaVersion == "client-settings/1" else { throw SettingsError.invalid("schema_version が不明です") }
        if let deployDir, deployDir.isEmpty { throw SettingsError.invalid("deploy_dir が空です") }
    }

    public var apiURL: URL? { URL(string: apiBaseUrl) }

    /// `deploy_dir/../scripts/service.sh` を解決する。
    public var serviceScriptURL: URL? {
        guard let deployDir else { return nil }
        return URL(fileURLWithPath: deployDir).deletingLastPathComponent().appendingPathComponent("scripts/service.sh")
    }

    public var doctorScriptURL: URL? {
        guard let deployDir else { return nil }
        return URL(fileURLWithPath: deployDir).deletingLastPathComponent().appendingPathComponent("scripts/doctor.sh")
    }
}

public enum SettingsError: Error, LocalizedError, Sendable {
    case invalid(String)
    public var errorDescription: String? {
        if case .invalid(let message) = self { return "設定が不正です: \(message)" }
        return nil
    }
}

/// アプリ管理領域のパス。
public struct AppPaths: Sendable {
    public let root: URL

    public init(root: URL? = nil) {
        if let root {
            self.root = root
        } else {
            let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            self.root = support.appendingPathComponent("AudioMinutes", isDirectory: true)
        }
    }

    public var settingsFile: URL { root.appendingPathComponent("settings.json") }
    public var sessionsDir: URL { root.appendingPathComponent("sessions", isDirectory: true) }
    public var importsDir: URL { root.appendingPathComponent("imports", isDirectory: true) }
    public var bridgeSocket: URL { root.appendingPathComponent("bridge.sock") }

    public func ensureDirectories() throws {
        for directory in [root, sessionsDir, importsDir] {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
        }
    }
}

public final class SettingsStore: @unchecked Sendable {
    public let paths: AppPaths
    private let lock = NSLock()

    public init(paths: AppPaths = AppPaths()) { self.paths = paths }

    public func load() throws -> ClientSettings {
        lock.lock(); defer { lock.unlock() }
        guard FileManager.default.fileExists(atPath: paths.settingsFile.path) else { return ClientSettings() }
        let data = try Data(contentsOf: paths.settingsFile)
        let settings = try ContractCoding.decoder().decode(ClientSettings.self, from: data)
        try settings.validate()
        return settings
    }

    public func save(_ settings: ClientSettings) throws {
        try settings.validate()
        lock.lock(); defer { lock.unlock() }
        try paths.ensureDirectories()
        let data = try ContractCoding.encoder(pretty: true).encode(settings)
        try data.write(to: paths.settingsFile, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: paths.settingsFile.path)
    }
}
