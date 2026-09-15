import AppKit
import ClientCore
import Foundation

struct StateOutput: Encodable { var state: String; var server: String? = nil }
struct ImportOutput: Encodable { var sessionId: UUID; var inputKind: InputKind; var status: SessionStatus }
struct ResumeItem: Encodable { var sessionId: UUID; var status: SessionStatus?; var error: String? }
struct JobOutput: Encodable { var jobId: UUID; var status: SessionStatus }
struct DeleteOutput: Encodable { var deleted: Bool; var sessionId: UUID }
struct ExportOutput: Encodable { var sessionId: UUID; var outputDirectory: String; var files: [String] }
struct ScriptOutput: Encodable { var command: String; var exitCode: Int32; var output: String }
struct ReauthOutput: Encodable { var state: String; var reauthValidUntil: Date? }

struct CLI {
    var json = false
    var serverOverride: URL?

    mutating func run(_ rawArguments: [String]) async throws {
        var raw = rawArguments
        json = removeFlag("--json", from: &raw)
        if let value = try removeValue("--server", from: &raw) {
            guard let url = URL(string: value) else { throw CLIError.usage("--server に有効な URL を指定してください") }
            try APIClient.validate(baseURL: url)
            serverOverride = url
        }
        guard let command = raw.first else { print(Self.help); return }
        let rest = Array(raw.dropFirst())
        if ["help", "--help", "-h"].contains(command) { print(Self.help); return }

        let settings = try SettingsStore().load()
        if command == "doctor" { try runDoctor(rest, settings: settings); return }
        if command == "service" { try runService(rest, settings: settings); return }

        let baseURL = try resolvedBaseURL(settings)
        let auth = try AuthManager(baseURL: baseURL)
        let client = try APIClient(baseURL: baseURL, tokenProvider: auth)
        let service = SessionService(client: client)
        switch command {
        case "health":
            try Arguments(rest).requirePositionals(0...0, usage: "health [--server URL] [--json]")
            try output(try await service.health()) { "\($0.status)\t\($0.version ?? "unknown")" }
        case "auth": try await runAuth(rest, auth: auth, server: baseURL)
        case "import": try await runImport(rest, settings: settings, service: service)
        case "resume": try await runResume(rest, service: service)
        case "status": try await runStatus(rest, service: service)
        case "sessions": try await runSessions(rest, service: service)
        case "export": try await runExport(rest, service: service)
        case "minutes": try await runMinutes(rest, service: service)
        case "formats", "format": try await runFormats(rest, service: service)
        case "claude": try await runClaude(rest, service: service)
        case "account": try await runAccount(rest, service: service)
        case "admin": try await runAdmin(rest, service: service)
        default: throw CLIError.usage("不明なコマンドです: \(command)\n\n\(Self.help)")
        }
    }

    static func exitCode(for error: Error) -> Int32 {
        if error is CLIError || error is SettingsError { return 2 }
        if case APIClientError.unauthenticated = error { return 3 }
        if case APIClientError.api(let body, let status) = error {
            if status == 401 { return 3 }
            if status == 403 || status == 404 || body.code == .conflict { return 4 }
            return body.retryable == true ? 5 : 4
        }
        if let api = error as? APIClientError, api.isUnreachable { return 5 }
        return 1
    }

    mutating func removeFlag(_ name: String, from raw: inout [String]) -> Bool {
        let found = raw.contains(name)
        raw.removeAll { $0 == name }
        return found
    }

    mutating func removeValue(_ name: String, from raw: inout [String]) throws -> String? {
        var found: String?
        var output: [String] = []
        var index = 0
        while index < raw.count {
            if raw[index] == name {
                guard found == nil, index + 1 < raw.count else { throw CLIError.usage("\(name) は1回だけ値付きで指定してください") }
                found = raw[index + 1]; index += 2
            } else if raw[index].hasPrefix("\(name)=") {
                guard found == nil else { throw CLIError.usage("\(name) は1回だけ指定してください") }
                found = String(raw[index].dropFirst(name.count + 1)); index += 1
            } else { output.append(raw[index]); index += 1 }
        }
        raw = output
        return found
    }

    func resolvedBaseURL(_ settings: ClientSettings) throws -> URL {
        guard let result = serverOverride ?? settings.apiURL else { throw SettingsError.invalid("api_base_url") }
        try APIClient.validate(baseURL: result)
        return result
    }

    func uuid(_ raw: String) throws -> UUID {
        guard let id = UUID(uuidString: raw) else { throw CLIError.invalidUUID(raw) }
        return id
    }

    func language(_ raw: String?) throws -> LanguageMode? {
        guard let raw else { return nil }
        guard let value = LanguageMode(rawValue: raw) else { throw CLIError.usage("--language は auto / ja / en / mixed のいずれかです") }
        return value
    }

    func output<T: Encodable>(_ value: T, text: (T) -> String) throws {
        if json {
            let data = try ContractCoding.encoder(pretty: true).encode(value)
            FileHandle.standardOutput.write(data); print()
        } else { print(text(value)) }
    }

    func optionalArtifact<T>(_ operation: () async throws -> T) async throws -> T? {
        do { return try await operation() }
        catch APIClientError.api(_, let status) where status == 404 { return nil }
    }

    func readJSON<T: Decodable>(_ path: String, as type: T.Type) throws -> T {
        do { return try ContractCoding.decoder().decode(T.self, from: Data(contentsOf: URL(fileURLWithPath: path))) }
        catch { throw CLIError.invalidInput("\(path): \(error.localizedDescription)") }
    }

    func resolveFormat(_ raw: String?, service: SessionService) async throws -> UUID? {
        guard let raw, raw != "default" else { return nil }
        if let id = UUID(uuidString: raw) { return id }
        let matches = try await service.formats().filter { $0.name == raw }
        guard matches.count == 1 else { throw CLIError.invalidInput("format は UUID または一意な名前で指定してください: \(raw)") }
        return matches[0].profileId
    }

    func runAuth(_ raw: [String], auth: AuthManager, server: URL) async throws {
        let args = try Arguments(raw)
        try args.requirePositionals(0...1, usage: "auth {status|login|logout}")
        switch args.positionals.first ?? "status" {
        case "status": try output(StateOutput(state: await auth.isLoggedIn() ? "logged_in" : "logged_out", server: server.absoluteString)) { $0.state }
        case "login":
            _ = try await auth.login { url in await MainActor.run { _ = NSWorkspace.shared.open(url) } }
            try output(StateOutput(state: "logged_in", server: server.absoluteString)) { $0.state }
        case "logout": try await auth.logout(); try output(StateOutput(state: "logged_out", server: server.absoluteString)) { $0.state }
        default: throw CLIError.usage("auth {status|login|logout}")
        }
    }

    func runDoctor(_ raw: [String], settings: ClientSettings) throws {
        try Arguments(raw).requirePositionals(0...0, usage: "doctor [--json]")
        guard let script = settings.doctorScriptURL else { throw SettingsError.invalid("deploy_dir がないため doctor.sh を解決できません") }
        try runScript(script, arguments: ["--profile", settings.profile], commandName: "doctor")
    }

    func runService(_ raw: [String], settings: ClientSettings) throws {
        guard let command = raw.first else { throw CLIError.usage("service {start|stop|restart|status|logs|bootstrap|config|readiness|models|update}") }
        let allowed = ["start", "stop", "restart", "status", "logs", "bootstrap", "config", "readiness", "models", "update"]
        guard allowed.contains(command) else {
            throw CLIError.usage("service {start|stop|restart|status|logs|bootstrap|config|readiness|models|update}")
        }
        let rest = Array(raw.dropFirst())
        switch command {
        case "start", "stop", "restart", "status", "bootstrap", "config":
            try Arguments(rest).requirePositionals(0...0, usage: "service \(command)")
        case "logs":
            let args = try Arguments(rest); try args.requirePositionals(0...1, usage: "service logs [LINES]")
            if let lines = args.positionals.first, Int(lines) == nil { throw CLIError.usage("LINES は整数です") }
        case "models":
            let args = try Arguments(rest); try args.requirePositionals(0...1, usage: "service models {status|verify|smoke}")
            if let action = args.positionals.first, !["status", "verify", "smoke"].contains(action) { throw CLIError.usage("service models {status|verify|smoke}") }
        case "readiness":
            _ = try Arguments(rest, valueOptions: ["--timeout"])
        case "update":
            let args = try Arguments(rest, valueOptions: ["--backup-dir"], flagOptions: ["--yes"])
            try args.requirePositionals(0...0, usage: "service update --backup-dir DIR [--yes]")
            guard args.value("--backup-dir") != nil else { throw CLIError.usage("service update --backup-dir DIR [--yes]") }
        default: break
        }
        guard let script = settings.serviceScriptURL else { throw SettingsError.invalid("deploy_dir がないため service.sh を解決できません") }
        try runScript(script, arguments: ["--profile", settings.profile] + raw, commandName: "service \(command)")
    }

    func runScript(_ script: URL, arguments: [String], commandName: String) throws {
        let process = Process(), pipe = Pipe()
        process.executableURL = script; process.arguments = arguments
        if json { process.standardOutput = pipe; process.standardError = pipe }
        try process.run()
        // JSON時は先にpipeを読み続け、大きなconfig/log出力でもpipe満杯でdeadlockさせない。
        let captured = json ? String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self) : ""
        process.waitUntilExit()
        if json { try output(ScriptOutput(command: commandName, exitCode: process.terminationStatus, output: captured)) { $0.output } }
        guard process.terminationStatus == 0 else { throw CLIError.commandFailed(commandName, process.terminationStatus) }
    }

    static let help = """
    audio-minutes doctor [--json]
    audio-minutes service {start|stop|restart|status|logs|bootstrap|config|readiness|models|update}
    audio-minutes auth {status|login|logout} [--server URL] [--json]
    audio-minutes import AUDIO_FILE [--title TITLE] [--language auto|ja|en|mixed] [--format UUID|NAME] [--external-send|--no-external-send] [--server URL] [--json]
    audio-minutes resume [SESSION_ID] [--server URL] [--json]
    audio-minutes resume SESSION_ID --cancel --yes [--server URL] [--json]
    audio-minutes status SESSION_ID [--watch] [--server URL] [--json]
    audio-minutes sessions {list|show|update|retry|jobs|cancel|delete|shares|share|unshare} ...
    audio-minutes export SESSION_ID --out DIRECTORY [--audio]
    audio-minutes minutes {show|versions|edit|regenerate|select|restore|compare} ...
    audio-minutes formats {list|show|create|update|delete|duplicate|default|preview} ...
    audio-minutes claude {status|login|login-status|submit-code|cancel|logout} ...
    audio-minutes account {me|reauth|passkeys|add-passkey|delete-passkey|recovery-codes} ...
    audio-minutes admin {users|invitations|invite|revoke-invitation|update-user|retention} ...

    共通option: --server URL / --json。CLIは録音・入力列挙・Chromeタブ操作を提供しません。
    """
}
