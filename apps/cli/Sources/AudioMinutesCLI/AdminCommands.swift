import AppKit
import ClientCore
import Darwin
import Foundation

extension CLI {
    func runClaude(_ raw: [String], service: SessionService) async throws {
        let command = raw.first ?? "status", rest = raw.isEmpty ? [] : Array(raw.dropFirst())
        let count = ["login-status", "cancel", "submit-code"].contains(command) ? 1...1 : 0...0
        let args = try Arguments(rest, valueOptions: command == "submit-code" ? ["--code"] : [])
        try args.requirePositionals(count, usage: "claude {status|login|login-status ID|submit-code ID --code CODE|cancel ID|logout}")
        switch command {
        case "status": try output(try await service.claudeStatus()) { "\($0.state)\t\($0.cliVersion ?? "unknown")\t\($0.label)" }
        case "login":
            let isTerminal = Self.standardOutputIsTerminal()
            try Self.validateClaudeLoginInvocation(json: json, isTerminal: isTerminal)
            var value = try await service.claudeLogin()
            var displayedURL = false
            do {
                for _ in 0..<360 {
                    if value.state == "url_ready", let raw = value.url, !displayedURL {
                        do {
                            if let url = try Self.claudeLoginURLForTerminal(raw, json: json, isTerminal: isTerminal) {
                                print("認証URL\t\(url)")
                                print("本人が認証画面を開いてください。必要なら別terminalで `audio-minutes claude submit-code \(value.authSessionId) --code CODE` を実行してください")
                            }
                        } catch {
                            _ = try? await service.claudeCancelLogin(value.authSessionId)
                            throw error
                        }
                        displayedURL = true
                    }
                    if ["completed", "failed", "cancelled", "expired"].contains(value.state) { break }
                    try await Task.sleep(for: .seconds(1))
                    value = try await service.claudeLoginStatus(value.authSessionId)
                }
            } catch is CancellationError {
                _ = try? await service.claudeCancelLogin(value.authSessionId)
                throw AuthError.cancelled
            }
            if !["completed", "failed", "cancelled", "expired"].contains(value.state) {
                _ = try? await service.claudeCancelLogin(value.authSessionId)
                throw AuthError.timeout
            }
            guard value.state == "completed" else { throw CLIError.invalidInput("Claude login \(value.state): \(value.failureCode ?? "詳細なし")") }
            // 認証 URL は対話中の非 JSON 表示に限定し、機械可読出力には含めない。
            value.url = nil
            try output(value) { $0.state }
        case "login-status":
            var value = try await service.claudeLoginStatus(args.positionals[0])
            let displayURL = try Self.claudeLoginURLForTerminal(
                value.url,
                json: json,
                isTerminal: Self.standardOutputIsTerminal()
            )
            if json { value.url = nil }
            try output(value) { "\($0.state)\(displayURL.map { "\n\($0)" } ?? "")" }
        case "submit-code":
            guard let code = args.value("--code"), !code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw CLIError.usage("--code が必要です") }
            try output(try await service.claudeSubmitLoginCode(args.positionals[0], code: code)) { $0.state }
        case "cancel": try output(try await service.claudeCancelLogin(args.positionals[0])) { $0.state }
        case "logout": try output(try await service.claudeLogout()) { $0.state }
        default: throw CLIError.usage("claude {status|login|login-status ID|submit-code ID --code CODE|cancel ID|logout}")
        }
    }

    static func standardOutputIsTerminal() -> Bool {
        isatty(STDOUT_FILENO) == 1
    }

    static func validateClaudeLoginInvocation(json: Bool, isTerminal: Bool) throws {
        guard !json else {
            throw CLIError.usage("claude login は対話専用のため --json では実行できません")
        }
        guard isTerminal else {
            throw CLIError.invalidInput("claude login は認証URLを安全に表示できる対話terminalで実行してください")
        }
    }

    static func claudeLoginURLForTerminal(_ raw: String?, json: Bool, isTerminal: Bool) throws -> String? {
        guard let raw else { return nil }
        guard SessionService.isAllowedClaudeAuthURL(raw) else {
            throw CLIError.invalidInput("Claude 認証 URL の origin が許可されていません")
        }
        if json { return nil }
        guard isTerminal else {
            throw CLIError.invalidInput("Claude 認証 URL は対話terminalにだけ表示できます")
        }
        return raw
    }

    func runAccount(_ raw: [String], service: SessionService) async throws {
        let command = raw.first ?? "me", rest = raw.isEmpty ? [] : Array(raw.dropFirst())
        switch command {
        case "me":
            try Arguments(rest).requirePositionals(0...0, usage: "account me")
            try output(try await service.me()) { "\($0.userId.uuidString.lowercased())\t\($0.email)\t\($0.role)" }
        case "passkeys":
            try Arguments(rest).requirePositionals(0...0, usage: "account passkeys")
            try output(try await service.passkeys()) { $0.map { "\($0.id.uuidString.lowercased())\t\($0.label ?? "")" }.joined(separator: "\n") }
        case "add-passkey":
            try Arguments(rest).requirePositionals(0...0, usage: "account add-passkey")
            var registration = try await service.startPasskeyRegistration()
            guard let raw = registration.passkeyUrl, service.isAllowedServerBrowserURL(raw), let url = URL(string: raw) else {
                throw CLIError.invalidInput("パスキー登録 URL の origin が API と一致しません")
            }
            _ = await MainActor.run { NSWorkspace.shared.open(url) }
            if !json {
                print("ブラウザーで本人確認後、パスキーを追加してください")
                print("url\t\(raw)")
            }
            for _ in 0..<300 {
                guard !["completed", "expired", "failed", "cancelled"].contains(registration.status) else { break }
                try await Task.sleep(for: .seconds(1))
                var next = try await service.passkeyRegistrationStatus(registration.requestId)
                if next.passkeyUrl == nil { next.passkeyUrl = registration.passkeyUrl }
                registration = next
            }
            guard registration.status == "completed" else {
                throw CLIError.invalidInput("パスキー追加 \(registration.status)")
            }
            try output(registration) { $0.status }
        case "delete-passkey":
            let args = try Arguments(rest); try args.requirePositionals(1...1, usage: "account delete-passkey PASSKEY_ID")
            try await service.deletePasskey(uuid(args.positionals[0])); try output(StateOutput(state: "deleted")) { $0.state }
        case "recovery-codes":
            try Arguments(rest).requirePositionals(0...0, usage: "account recovery-codes")
            try output(try await service.regenerateRecoveryCodes()) { $0.recoveryCodes.joined(separator: "\n") }
        case "reauth":
            try Arguments(rest).requirePositionals(0...0, usage: "account reauth")
            let baseURL = service.client.baseURL
            let auth = try AuthManager(baseURL: baseURL)
            let validUntil = try await auth.reauthenticateInBrowser { url in await MainActor.run { _ = NSWorkspace.shared.open(url) } }
            try output(ReauthOutput(state: "completed", reauthValidUntil: validUntil)) { value in
                "completed\t\(value.reauthValidUntil?.formatted(.iso8601) ?? "")"
            }
        default: throw CLIError.usage("account {me|reauth|passkeys|add-passkey|delete-passkey|recovery-codes}")
        }
    }

    func runAdmin(_ raw: [String], service: SessionService) async throws {
        guard let command = raw.first else { throw CLIError.usage("admin {users|invitations|invite|revoke-invitation|update-user|retention}") }
        let rest = Array(raw.dropFirst())
        switch command {
        case "users":
            try Arguments(rest).requirePositionals(0...0, usage: "admin users")
            try output(try await service.users()) { $0.map { "\($0.id.uuidString.lowercased())\t\($0.email)\t\($0.role)\t\($0.disabled)" }.joined(separator: "\n") }
        case "invitations":
            try Arguments(rest).requirePositionals(0...0, usage: "admin invitations")
            try output(try await service.invitations()) { $0.map { "\($0.id.uuidString.lowercased())\t\($0.email)\t\($0.role)" }.joined(separator: "\n") }
        case "invite":
            let args = try Arguments(rest, valueOptions: ["--email", "--role"]); try args.requirePositionals(0...0, usage: "admin invite --email EMAIL [--role member|owner]")
            guard let email = args.value("--email") else { throw CLIError.usage("--email が必要です") }; let role = args.value("--role") ?? "member"
            guard ["member", "owner"].contains(role) else { throw CLIError.usage("--role は member / owner です") }
            try output(try await service.invite(email: email, role: role)) { "\($0.id.uuidString.lowercased())\t\($0.email)\t\($0.url ?? "")" }
        case "revoke-invitation":
            let args = try Arguments(rest); try args.requirePositionals(1...1, usage: "admin revoke-invitation INVITATION_ID")
            try await service.revokeInvitation(uuid(args.positionals[0])); try output(StateOutput(state: "revoked")) { $0.state }
        case "update-user":
            let args = try Arguments(rest, valueOptions: ["--role"], flagOptions: ["--disable", "--enable"]); try args.requirePositionals(1...1, usage: "admin update-user USER_ID [--role member|owner] [--disable|--enable]")
            guard !(args.has("--disable") && args.has("--enable")) else { throw CLIError.usage("--disable と --enable は同時指定できません") }
            let disabled = args.has("--disable") ? true : args.has("--enable") ? false : nil
            let role = args.value("--role"); if let role, !["member", "owner"].contains(role) { throw CLIError.usage("--role は member / owner です") }
            let value = try await service.updateUser(uuid(args.positionals[0]), .init(role: role, disabled: disabled))
            try output(value) { "\($0.id.uuidString.lowercased())\t\($0.email)\t\($0.role)\t\($0.disabled)" }
        case "retention":
            let args = try Arguments(rest, valueOptions: ["--upload-hours", "--audio-days", "--log-days"]); try args.requirePositionals(0...0, usage: "admin retention [--upload-hours N --audio-days N --log-days N]")
            if args.values.isEmpty {
                try output(try await service.retention()) { "upload=\($0.uploadHours)h audio=\($0.audioDays)d log=\($0.logDays)d" }
            } else {
                let current = try await service.retention()
                guard let upload = Int(args.value("--upload-hours") ?? String(current.uploadHours)),
                      let audio = Int(args.value("--audio-days") ?? String(current.audioDays)),
                      let log = Int(args.value("--log-days") ?? String(current.logDays)), upload > 0, audio > 0, log > 0 else {
                    throw CLIError.usage("保持期間は正の整数です")
                }
                let value = try await service.updateRetention(.init(uploadHours: upload, audioDays: audio, logDays: log))
                try output(value) { "upload=\($0.uploadHours)h audio=\($0.audioDays)d log=\($0.logDays)d" }
            }
        default: throw CLIError.usage("admin {users|invitations|invite|revoke-invitation|update-user|retention}")
        }
    }
}
