import Foundation

/// API 呼び出しの失敗。契約の error.v1 を保持する。
public enum APIClientError: Error, LocalizedError, Sendable {
    case insecureBaseURL(URL)
    case invalidURL(String)
    case unauthenticated
    case api(ApiErrorBody, status: Int)
    case transport(String)
    case decoding(String)
    case unexpectedStatus(Int)

    public var errorDescription: String? {
        switch self {
        case .insecureBaseURL(let url):
            return "平文 HTTP は loopback (127.0.0.1 / localhost) 以外に使えません: \(url.absoluteString)"
        case .invalidURL(let value): return "URL が不正です: \(value)"
        case .unauthenticated: return "ログインしていません。`audio-minutes auth login` を実行してください"
        case .api(let body, let status): return "[\(status)] \(body.code.rawValue): \(body.message) (request_id=\(body.requestId))"
        case .transport(let message): return "サービスに接続できません: \(message)。`audio-minutes doctor` で状態を確認してください"
        case .decoding(let message): return "応答を解釈できません: \(message)"
        case .unexpectedStatus(let status): return "予期しない応答 (HTTP \(status))"
        }
    }

    public var isUnreachable: Bool {
        if case .transport = self { return true }
        return false
    }
}

/// アクセストークンの供給元。AuthManager が実装する。
public protocol TokenProvider: Sendable {
    func accessToken(forceRefresh: Bool) async throws -> String?
}

public struct NoTokenProvider: TokenProvider {
    public init() {}
    public func accessToken(forceRefresh: Bool) async throws -> String? { nil }
}

public struct StaticTokenProvider: TokenProvider {
    let token: String
    public init(token: String) { self.token = token }
    public func accessToken(forceRefresh: Bool) async throws -> String? { token }
}

/// 生の HTTP 応答。tus の header を読むために使う。
public struct RawResponse: Sendable {
    public let status: Int
    public let headers: [String: String]
    public let body: Data

    public func header(_ name: String) -> String? {
        headers.first { $0.key.caseInsensitiveCompare(name) == .orderedSame }?.value
    }
}

/// minutes-api への HTTP クライアント。Bearer 付与、error.v1 の解釈、平文 URL の拒否を担当する。
public final class APIClient: @unchecked Sendable {
    public let baseURL: URL
    public let tokenProvider: TokenProvider
    private let session: URLSession
    private let decoder = ContractCoding.decoder()
    private let encoder = ContractCoding.encoder()

    /// ポーリング間隔 (サーバー指示があれば更新する)。
    public private(set) var pollIntervalMs: Int = 3000

    public init(baseURL: URL, tokenProvider: TokenProvider = NoTokenProvider(), session: URLSession? = nil) throws {
        try Self.validate(baseURL: baseURL)
        self.baseURL = baseURL
        self.tokenProvider = tokenProvider
        if let session {
            self.session = session
        } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = 60
            configuration.timeoutIntervalForResource = 3600
            configuration.httpAdditionalHeaders = ["User-Agent": ClientInfo.userAgent]
            self.session = URLSession(configuration: configuration)
        }
    }

    /// scheme / host / 有効portだけからなる送信先origin。pathや末尾slashの違いでは別送信先にしない。
    public static func canonicalOrigin(for url: URL) throws -> String {
        try validate(baseURL: url)
        guard let scheme = url.scheme?.lowercased(), let host = url.host?.lowercased() else {
            throw APIClientError.invalidURL(url.absoluteString)
        }
        var components = URLComponents()
        components.scheme = scheme
        components.host = host
        if let port = url.port, !((scheme == "https" && port == 443) || (scheme == "http" && port == 80)) {
            components.port = port
        }
        guard let origin = components.string else { throw APIClientError.invalidURL(url.absoluteString) }
        return origin
    }

    public var canonicalOrigin: String { try! Self.canonicalOrigin(for: baseURL) }

    /// 平文 HTTP は loopback だけ許可する (ADR-0008)。
    public static func validate(baseURL: URL) throws {
        guard let scheme = baseURL.scheme?.lowercased(), let host = baseURL.host?.lowercased() else {
            throw APIClientError.invalidURL(baseURL.absoluteString)
        }
        if scheme == "https" { return }
        if scheme == "http", ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host) { return }
        throw APIClientError.insecureBaseURL(baseURL)
    }

    public func url(_ path: String, query: [String: String?] = [:]) throws -> URL {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw APIClientError.invalidURL(path)
        }
        let basePath = components.path.hasSuffix("/") ? String(components.path.dropLast()) : components.path
        components.path = basePath + path
        let items = query.compactMap { key, value in value.map { URLQueryItem(name: key, value: $0) } }
        components.queryItems = items.isEmpty ? nil : items
        guard let url = components.url else { throw APIClientError.invalidURL(path) }
        return url
    }

    // MARK: - 型付き呼び出し

    public func get<T: Decodable>(_ path: String, query: [String: String?] = [:]) async throws -> T {
        try await send(method: "GET", path: path, query: query, body: nil as Data?)
    }

    public func post<T: Decodable, B: Encodable>(_ path: String, body: B) async throws -> T {
        try await send(method: "POST", path: path, body: try encoder.encode(body))
    }

    public func post<T: Decodable>(_ path: String) async throws -> T {
        try await send(method: "POST", path: path, body: nil as Data?)
    }

    public func patch<T: Decodable, B: Encodable>(_ path: String, body: B) async throws -> T {
        try await send(method: "PATCH", path: path, body: try encoder.encode(body))
    }

    public func put<T: Decodable, B: Encodable>(_ path: String, body: B) async throws -> T {
        try await send(method: "PUT", path: path, body: try encoder.encode(body))
    }

    public func delete(_ path: String) async throws {
        _ = try await raw(method: "DELETE", path: path, expect: 200...299)
    }

    public func getText(_ path: String) async throws -> String {
        let response = try await raw(method: "GET", path: path, headers: ["Accept": "text/markdown, text/plain, */*"], expect: 200...299)
        return String(decoding: response.body, as: UTF8.self)
    }

    public func getData(_ path: String, range: String? = nil) async throws -> RawResponse {
        var headers: [String: String] = [:]
        if let range { headers["Range"] = range }
        return try await raw(method: "GET", path: path, headers: headers, expect: 200...299)
    }

    private func send<T: Decodable>(method: String, path: String, query: [String: String?] = [:], body: Data?,
                                    contentType: String = "application/json") async throws -> T {
        var headers: [String: String] = ["Accept": "application/json"]
        if body != nil { headers["Content-Type"] = contentType }
        let response = try await raw(method: method, path: path, query: query, headers: headers, body: body, expect: 200...299)
        if T.self == EmptyResponse.self { return EmptyResponse() as! T }
        do {
            return try decoder.decode(T.self, from: response.body.isEmpty ? Data("{}".utf8) : response.body)
        } catch {
            throw APIClientError.decoding(String(describing: error))
        }
    }

    // MARK: - 生の呼び出し (tus など)

    public func raw(method: String, path: String, query: [String: String?] = [:], headers: [String: String] = [:],
                    body: Data? = nil, absoluteURL: URL? = nil, expect: ClosedRange<Int>? = nil,
                    retryOnUnauthorized: Bool = true) async throws -> RawResponse {
        let target = try absoluteURL ?? url(path, query: query)
        if let absoluteURL {
            // upload URL 等の絶対 URL は同じ origin だけ許可する
            guard absoluteURL.host?.lowercased() == baseURL.host?.lowercased(), absoluteURL.port == baseURL.port,
                  absoluteURL.scheme?.lowercased() == baseURL.scheme?.lowercased() else {
                throw APIClientError.invalidURL(absoluteURL.absoluteString)
            }
        }
        var request = URLRequest(url: target)
        request.httpMethod = method
        request.httpBody = body
        for (key, value) in headers { request.setValue(value, forHTTPHeaderField: key) }
        if let token = try await tokenProvider.accessToken(forceRefresh: false) {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let (data, urlResponse): (Data, URLResponse)
        do {
            (data, urlResponse) = try await session.data(for: request)
        } catch {
            throw APIClientError.transport(error.localizedDescription)
        }
        guard let http = urlResponse as? HTTPURLResponse else { throw APIClientError.transport("HTTP 応答ではありません") }
        var headerMap: [String: String] = [:]
        for (key, value) in http.allHeaderFields {
            if let key = key as? String, let value = value as? String { headerMap[key] = value }
        }
        if let retryAfter = headerMap.first(where: { $0.key.caseInsensitiveCompare("Retry-After") == .orderedSame })?.value,
           let seconds = Int(retryAfter) {
            pollIntervalMs = max(1000, seconds * 1000)
        }
        let response = RawResponse(status: http.statusCode, headers: headerMap, body: data)

        if http.statusCode == 401, retryOnUnauthorized,
           let refreshed = try await tokenProvider.accessToken(forceRefresh: true) {
            var retried = request
            retried.setValue("Bearer \(refreshed)", forHTTPHeaderField: "Authorization")
            return try await raw(method: method, path: path, query: query, headers: headers, body: body,
                                 absoluteURL: absoluteURL, expect: expect, retryOnUnauthorized: false)
        }
        if let expect, !expect.contains(http.statusCode) {
            if http.statusCode == 401 { throw APIClientError.unauthenticated }
            if let apiError = try? decoder.decode(ApiError.self, from: data) {
                throw APIClientError.api(apiError.error, status: http.statusCode)
            }
            throw APIClientError.unexpectedStatus(http.statusCode)
        }
        // サーバーがポーリング間隔を指示していれば取り込む (capabilities など)。
        // 応答型を決め打ちせず、この項目だけを見る。
        if let interval = (try? decoder.decode(PollIntervalHint.self, from: data))?.pollIntervalMs {
            pollIntervalMs = max(1000, interval)
        }
        return response
    }
}

public struct EmptyResponse: Decodable, Sendable {
    public init() {}
}

/// 応答に含まれていればポーリング間隔として使う項目だけを取り出す。
private struct PollIntervalHint: Decodable {
    var pollIntervalMs: Int?
}
