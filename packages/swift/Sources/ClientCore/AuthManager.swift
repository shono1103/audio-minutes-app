import CryptoKit
import Foundation
import Network

/// 保存するトークン一式。URL・認可コードは保存しない。
public struct TokenSet: Codable, Sendable, Equatable {
    public var accessToken: String
    public var refreshToken: String
    public var expiresAt: Date
    public var scope: String?

    public var isExpiring: Bool { expiresAt.timeIntervalSinceNow < 60 }
}

public enum AuthError: Error, LocalizedError, Sendable {
    case stateMismatch
    case callbackError(String)
    case listenerFailed(String)
    case tokenExchangeFailed(String)
    case cancelled
    case timeout
    case reauthExpired
    case reauthFailed(String)

    public var errorDescription: String? {
        switch self {
        case .stateMismatch: return "認可応答の state が一致しません"
        case .callbackError(let message): return "認可が拒否されました: \(message)"
        case .listenerFailed(let message): return "loopback の待受に失敗しました: \(message)"
        case .tokenExchangeFailed(let message): return "トークン交換に失敗しました: \(message)"
        case .cancelled: return "ログインを取り消しました"
        case .timeout: return "ログインがタイムアウトしました"
        case .reauthExpired: return "再認証画面の有効期限が切れました"
        case .reauthFailed(let detail): return "再認証に失敗しました: \(detail)"
        }
    }
}

/// PKCE の verifier / challenge (S256)。
public struct PKCE: Sendable, Equatable {
    public let verifier: String
    public let challenge: String

    public init() {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        self.init(verifierBytes: bytes)
    }

    init(verifierBytes: [UInt8]) {
        verifier = Data(verifierBytes).base64URLEncoded()
        challenge = Data(SHA256.hash(data: Data(verifier.utf8))).base64URLEncoded()
    }

    public static func challenge(for verifier: String) -> String {
        Data(SHA256.hash(data: Data(verifier.utf8))).base64URLEncoded()
    }
}

extension Data {
    public func base64URLEncoded() -> String {
        base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}

/// Authorization Code + PKCE (S256) によるログインとトークン管理 (FR-039〜045)。
public actor AuthManager: TokenProvider {
    public static let clientID = "audio-minutes-native"

    public let baseURL: URL
    private let store: SecretStore
    private let account: String
    private var cached: TokenSet?
    private var refreshTask: Task<String?, Error>?
    private let session: URLSession

    public init(baseURL: URL, store: SecretStore = KeychainStore()) throws {
        try APIClient.validate(baseURL: baseURL)
        self.baseURL = baseURL
        self.store = store
        self.account = "\(baseURL.host ?? "localhost"):\(baseURL.port ?? (baseURL.scheme == "https" ? 443 : 80))"
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        self.session = URLSession(configuration: configuration)
    }

    // MARK: - TokenProvider

    public func accessToken(forceRefresh: Bool) async throws -> String? {
        guard let tokens = try loadTokens() else { return nil }
        if !forceRefresh, !tokens.isExpiring { return tokens.accessToken }
        if let task = refreshTask { return try await task.value }
        let task = Task<String?, Error> { [tokens] in
            defer { Task { await self.clearRefreshTask() } }
            return try await self.refresh(using: tokens).accessToken
        }
        refreshTask = task
        return try await task.value
    }

    private func clearRefreshTask() { refreshTask = nil }

    public func isLoggedIn() -> Bool { (try? loadTokens()) != nil }

    public func loadTokens() throws -> TokenSet? {
        if let cached { return cached }
        guard let data = try store.read(account: account) else { return nil }
        let tokens = try ContractCoding.decoder().decode(TokenSet.self, from: data)
        cached = tokens
        return tokens
    }

    private func save(_ tokens: TokenSet) throws {
        cached = tokens
        try store.write(account: account, data: try ContractCoding.encoder().encode(tokens))
    }

    // MARK: - ログイン

    /// システムブラウザーで認可画面を開き、loopback で認可コードを受け取ってトークンへ交換する。
    /// `openURL` は呼び出し側 (GUI / CLI) が実装する。待受はログイン中だけ。
    public func login(scope: String = "sessions minutes formats account",
                      openURL: @Sendable (URL) async -> Void,
                      timeout: TimeInterval = 300) async throws -> TokenSet {
        let pkce = PKCE()
        let state = Data((0..<16).map { _ in UInt8.random(in: 0...255) }).base64URLEncoded()
        let listener = try LoopbackCallbackListener()
        defer { listener.stop() }
        let redirectURI = "http://127.0.0.1:\(listener.port)/callback"

        var components = URLComponents(url: baseURL.appendingPathComponent("oauth/authorize"), resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "response_type", value: "code"),
            URLQueryItem(name: "client_id", value: Self.clientID),
            URLQueryItem(name: "redirect_uri", value: redirectURI),
            URLQueryItem(name: "code_challenge", value: pkce.challenge),
            URLQueryItem(name: "code_challenge_method", value: "S256"),
            URLQueryItem(name: "state", value: state),
            URLQueryItem(name: "scope", value: scope),
        ]
        guard let authorizeURL = components.url else { throw APIClientError.invalidURL("oauth/authorize") }
        await openURL(authorizeURL)

        let callback = try await listener.waitForCallback(timeout: timeout)
        guard callback.state == state else { throw AuthError.stateMismatch }
        if let error = callback.error { throw AuthError.callbackError(error) }
        let tokens = try await exchange(code: callback.code, verifier: pkce.verifier, redirectURI: redirectURI)
        try save(tokens)
        return tokens
    }

    private func exchange(code: String, verifier: String, redirectURI: String) async throws -> TokenSet {
        try await tokenRequest([
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirectURI,
            "client_id": Self.clientID,
            "code_verifier": verifier,
        ])
    }

    private func refresh(using tokens: TokenSet) async throws -> TokenSet {
        do {
            let refreshed = try await tokenRequest([
                "grant_type": "refresh_token",
                "refresh_token": tokens.refreshToken,
                "client_id": Self.clientID,
            ])
            try save(refreshed)
            return refreshed
        } catch let error as APIClientError {
            if case .api(let body, _) = error, body.code == .unauthorized || body.code == .invalidRequest {
                try? logoutLocally()
            }
            throw error
        }
    }

    private func tokenRequest(_ form: [String: String]) async throws -> TokenSet {
        var request = URLRequest(url: baseURL.appendingPathComponent("oauth/token"))
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = Data(form.map { "\($0.key)=\(Self.formEncode($0.value))" }.joined(separator: "&").utf8)
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw APIClientError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else { throw APIClientError.transport("HTTP 応答ではありません") }
        guard (200...299).contains(http.statusCode) else {
            if let apiError = try? ContractCoding.decoder().decode(ApiError.self, from: data) {
                throw APIClientError.api(apiError.error, status: http.statusCode)
            }
            throw AuthError.tokenExchangeFailed("HTTP \(http.statusCode)")
        }
        struct TokenResponse: Decodable {
            var accessToken: String
            var refreshToken: String
            var expiresIn: Int
            var scope: String?
        }
        let decoded: TokenResponse
        do {
            decoded = try ContractCoding.decoder().decode(TokenResponse.self, from: data)
        } catch {
            throw AuthError.tokenExchangeFailed("応答を解釈できません")
        }
        return TokenSet(accessToken: decoded.accessToken, refreshToken: decoded.refreshToken,
                        expiresAt: Date().addingTimeInterval(TimeInterval(decoded.expiresIn)), scope: decoded.scope)
    }

    static func formEncode(_ value: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }

    // MARK: - ログアウト

    public func logout() async throws {
        if let tokens = try loadTokens() {
            var request = URLRequest(url: baseURL.appendingPathComponent("oauth/revoke"))
            request.httpMethod = "POST"
            request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
            request.httpBody = Data("token=\(Self.formEncode(tokens.refreshToken))&client_id=\(Self.clientID)".utf8)
            _ = try? await session.data(for: request)
        }
        try logoutLocally()
    }

    public func logoutLocally() throws {
        cached = nil
        try store.delete(account: account)
    }

    /// password/passkey はWeb画面だけで扱い、native clientは開始と完了確認だけを行う。
    public func reauthenticateInBrowser(
        openURL: @Sendable (URL) async -> Void,
        timeout: TimeInterval = 300,
        pollInterval: Duration = .seconds(1)
    ) async throws -> Date? {
        let client = try APIClient(baseURL: baseURL, tokenProvider: self)
        let service = SessionService(client: client)
        let started = try await service.startBrowserReauth()
        guard let url = URL(string: started.reauthUrl), Self.isAllowedReauthURL(url, baseURL: baseURL) else {
            throw AuthError.reauthFailed("再認証URLのoriginがAPIと一致しません")
        }
        await openURL(url)
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            try Task.checkCancellation()
            let status = try await service.browserReauthStatus(started.requestId)
            switch status.status {
            case "pending": try await Task.sleep(for: pollInterval)
            case "completed": return status.reauthValidUntil
            case "expired": throw AuthError.reauthExpired
            default: throw AuthError.reauthFailed("未知の状態です: \(status.status)")
            }
        }
        throw AuthError.timeout
    }

    public static func isAllowedReauthURL(_ url: URL, baseURL: URL) -> Bool {
        func port(_ value: URL) -> Int? { value.port ?? (value.scheme == "https" ? 443 : value.scheme == "http" ? 80 : nil) }
        return url.scheme == baseURL.scheme && url.host?.lowercased() == baseURL.host?.lowercased() && port(url) == port(baseURL)
    }
}

// MARK: - loopback callback

struct AuthCallback: Sendable {
    let code: String
    let state: String?
    let error: String?
}

/// ランダムな空きポートで 1 回だけ認可応答を受け取る loopback HTTP リスナー (RFC 8252)。
final class LoopbackCallbackListener: @unchecked Sendable {
    private let listener: NWListener
    private let queue = DispatchQueue(label: "dev.audio-minutes.loopback")
    private var continuation: CheckedContinuation<AuthCallback, Error>?
    private var connections: [NWConnection] = []
    private let lock = NSLock()
    private(set) var port: UInt16 = 0

    init() throws {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = NWEndpoint.hostPort(host: .ipv4(.loopback), port: .any)
        parameters.allowLocalEndpointReuse = false
        do {
            listener = try NWListener(using: parameters)
        } catch {
            throw AuthError.listenerFailed(error.localizedDescription)
        }
        let group = DispatchGroup()
        group.enter()
        var readyPort: UInt16 = 0
        var failure: Error?
        listener.stateUpdateHandler = { state in
            switch state {
            case .ready:
                readyPort = self.listener.port?.rawValue ?? 0
                group.leave()
            case .failed(let error):
                failure = error
                group.leave()
            default: break
            }
        }
        listener.newConnectionHandler = { [weak self] connection in self?.accept(connection) }
        listener.start(queue: queue)
        if group.wait(timeout: .now() + 5) == .timedOut { throw AuthError.listenerFailed("待受開始がタイムアウトしました") }
        if let failure { throw AuthError.listenerFailed(failure.localizedDescription) }
        guard readyPort != 0 else { throw AuthError.listenerFailed("ポートを確保できません") }
        port = readyPort
    }

    func waitForCallback(timeout: TimeInterval) async throws -> AuthCallback {
        try await withThrowingTaskGroup(of: AuthCallback.self) { group in
            group.addTask {
                try await withCheckedThrowingContinuation { continuation in
                    self.lock.withLock { self.continuation = continuation }
                }
            }
            group.addTask {
                try await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                throw AuthError.timeout
            }
            let result = try await group.next()!
            group.cancelAll()
            return result
        }
    }

    private func accept(_ connection: NWConnection) {
        lock.withLock { connections.append(connection) }
        connection.start(queue: queue)
        connection.receive(minimumIncompleteLength: 1, maximumLength: 16 * 1024) { [weak self] data, _, _, _ in
            guard let self, let data, let requestLine = String(data: data, encoding: .utf8)?.split(separator: "\r\n").first else {
                connection.cancel()
                return
            }
            let parts = requestLine.split(separator: " ")
            guard parts.count >= 2, parts[0] == "GET",
                  let components = URLComponents(string: "http://127.0.0.1\(parts[1])"),
                  components.path == "/callback" else {
                self.respond(connection, status: "404 Not Found", body: "not found")
                return
            }
            let items = components.queryItems ?? []
            let code = items.first { $0.name == "code" }?.value
            let state = items.first { $0.name == "state" }?.value
            let error = items.first { $0.name == "error" }?.value
            self.respond(connection, status: "200 OK", body: Self.successHTML(error: error))
            let callback = AuthCallback(code: code ?? "", state: state, error: error ?? (code == nil ? "missing_code" : nil))
            let continuation = self.lock.withLock { () -> CheckedContinuation<AuthCallback, Error>? in
                let value = self.continuation
                self.continuation = nil
                return value
            }
            continuation?.resume(returning: callback)
        }
    }

    private func respond(_ connection: NWConnection, status: String, body: String) {
        let payload = "HTTP/1.1 \(status)\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n\(body)"
        connection.send(content: Data(payload.utf8), completion: .contentProcessed { _ in connection.cancel() })
    }

    private static func successHTML(error: String?) -> String {
        let message = error == nil ? "ログインが完了しました。このタブを閉じて audio-minutes へ戻ってください。" : "ログインが拒否されました。アプリへ戻ってください。"
        return "<!doctype html><html lang=\"ja\"><meta charset=\"utf-8\"><title>audio-minutes</title><body style=\"font-family:-apple-system;padding:2rem\"><p>\(message)</p></body></html>"
    }

    func stop() {
        listener.cancel()
        lock.withLock {
            connections.forEach { $0.cancel() }
            connections.removeAll()
            continuation?.resume(throwing: AuthError.cancelled)
            continuation = nil
        }
    }
}
