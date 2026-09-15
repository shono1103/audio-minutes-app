import Foundation
import XCTest

@testable import ClientCore

final class LocalSessionOwnershipTests: XCTestCase {
    private let owner = UUID(uuidString: "11111111-1111-4111-8111-111111111111")!

    override func tearDown() {
        RequestStub.reset()
        super.tearDown()
    }

    func testCanonicalOriginDropsPathCaseAndDefaultPort() throws {
        XCTAssertEqual(try APIClient.canonicalOrigin(for: URL(string: "HTTPS://Minutes.Example.test:443/api/")!),
                       "https://minutes.example.test")
        XCTAssertEqual(try APIClient.canonicalOrigin(for: URL(string: "http://localhost:8787/v1")!),
                       "http://localhost:8787")
    }

    func testLegacyAndOfflineUnassignedStatesFailClosedBeforeNetwork() async throws {
        let root = temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = LocalSessionStore(paths: AppPaths(root: root))
        let service = try makeService()

        var legacyObject = try XCTUnwrap(JSONSerialization.jsonObject(with: ContractCoding.encoder().encode(makeState())) as? [String: Any])
        legacyObject.removeValue(forKey: "destination_origin")
        legacyObject.removeValue(forKey: "owner_user_id")
        let legacy = try ContractCoding.decoder().decode(LocalSessionState.self,
                                                         from: JSONSerialization.data(withJSONObject: legacyObject))
        await XCTAssertThrowsErrorAsync(try await store.uploadPending(legacy, service: service)) { error in
            XCTAssertEqual(error as? LocalSessionError, .destinationUnknown)
        }

        let unassigned = makeState(origin: "http://localhost:8787", owner: nil)
        await XCTAssertThrowsErrorAsync(try await store.uploadPending(unassigned, service: service)) { error in
            XCTAssertEqual(error as? LocalSessionError, .ownerUnknown)
        }
        XCTAssertTrue(RequestStub.requests.isEmpty)
    }

    func testDifferentDestinationAndCurrentOwnerAreRejectedBeforeSessionCreation() async throws {
        let root = temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = LocalSessionStore(paths: AppPaths(root: root))
        let service = try makeService()

        let wrongDestination = makeState(origin: "http://localhost:9999", owner: owner)
        await XCTAssertThrowsErrorAsync(try await store.uploadPending(wrongDestination, service: service)) { error in
            guard case LocalSessionError.destinationMismatch = error else { return XCTFail("unexpected: \(error)") }
        }
        XCTAssertTrue(RequestStub.requests.isEmpty)

        RequestStub.handler = { request in
            XCTAssertEqual(request.url?.path, "/v1/me")
            return (200, Data(#"{"user_id":"22222222-2222-4222-8222-222222222222","email":"other@example.test","role":"member"}"#.utf8))
        }
        await XCTAssertThrowsErrorAsync(try await store.uploadPending(makeState(origin: "http://localhost:8787", owner: owner),
                                                                       service: service)) { error in
            guard case LocalSessionError.ownerMismatch = error else { return XCTFail("unexpected: \(error)") }
        }
        XCTAssertEqual(RequestStub.requests.map(\.httpMethod), ["GET"])
    }

    func testCancelUsesSessionDeletionForDualTrackWithOneCompletedUpload() async throws {
        let root = temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = LocalSessionStore(paths: AppPaths(root: root))
        var state = makeState(origin: "http://localhost:8787", owner: owner, inputKind: .recordedDualTrack)
        state.uploadURLs = [
            "app_audio": "http://localhost:8787/v1/uploads/upload-completed",
            "microphone": "http://localhost:8787/v1/uploads/upload-pending",
        ]
        state.completedTracks = ["app_audio"]
        try store.save(state)

        RequestStub.handler = { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/me"):
                return (200, Self.ownerResponse)
            case ("DELETE", "/v1/sessions/\(state.package.sessionId.uuidString.lowercased())"):
                return (204, Data())
            default:
                XCTFail("unexpected request: \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return (500, Data())
            }
        }

        try await store.cancelPending(state, service: try makeService())

        XCTAssertEqual(RequestStub.requests.map { "\($0.httpMethod ?? "") \($0.url?.path ?? "")" }, [
            "GET /v1/me",
            "DELETE /v1/sessions/\(state.package.sessionId.uuidString.lowercased())",
        ])
        XCTAssertFalse(FileManager.default.fileExists(atPath: store.directory(for: state.package.sessionId).path))
    }

    func testCancelUsesSessionDeletionWhenAllUploadsCompletedButFinalizeFailed() async throws {
        let root = temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = LocalSessionStore(paths: AppPaths(root: root))
        var state = makeState(origin: "http://localhost:8787", owner: owner, inputKind: .recordedDualTrack)
        state.uploadURLs = [
            "app_audio": "http://localhost:8787/v1/uploads/upload-app-completed",
            "microphone": "http://localhost:8787/v1/uploads/upload-mic-completed",
        ]
        state.completedTracks = ["app_audio", "microphone"]
        state.lastError = "finalize failed"
        XCTAssertTrue(state.pendingUpload, "全upload完了後もfinalize失敗なら取消対象")
        try store.save(state)

        RequestStub.handler = { request in
            if request.url?.path == "/v1/me" { return (200, Self.ownerResponse) }
            if request.url?.path == "/v1/sessions/\(state.package.sessionId.uuidString.lowercased())" {
                return (404, Data()) // 既にserver削除済みでも冪等成功
            }
            XCTFail("completed uploadへTerminationを送ってはいけません: \(request.url?.path ?? "")")
            return (500, Data())
        }

        try await store.cancelPending(state, service: try makeService())

        XCTAssertEqual(RequestStub.requests.map(\.url?.path), [
            "/v1/me", "/v1/sessions/\(state.package.sessionId.uuidString.lowercased())",
        ])
        XCTAssertFalse(FileManager.default.fileExists(atPath: store.directory(for: state.package.sessionId).path))
    }

    func testCancelKeepsLocalStateWhenSessionDeletionIsNotConfirmed() async throws {
        enum Failure { case status(Int, Data), transport }
        let failures: [Failure] = [
            .status(401, Data()),
            .status(403, Data()),
            .status(409, Data(#"{"error":{"code":"conflict","message":"別の競合","request_id":"req-1"}}"#.utf8)),
            .transport,
        ]

        for failure in failures {
            RequestStub.reset()
            let root = temporaryRoot()
            defer { try? FileManager.default.removeItem(at: root) }
            let store = LocalSessionStore(paths: AppPaths(root: root))
            let state = makeState(origin: "http://localhost:8787", owner: owner)
            try store.save(state)
            RequestStub.handler = { request in
                if request.url?.path == "/v1/me" { return (200, Self.ownerResponse) }
                switch failure {
                case .status(let status, let body): return (status, body)
                case .transport: throw URLError(.notConnectedToInternet)
                }
            }

            await XCTAssertThrowsErrorAsync(try await store.cancelPending(state, service: try makeService())) { _ in }

            XCTAssertTrue(FileManager.default.fileExists(atPath: store.metadataURL(for: state.package.sessionId).path),
                          "server削除を確認できない場合はlocalを保持する")
        }
    }

    private static let ownerResponse = Data(
        #"{"user_id":"11111111-1111-4111-8111-111111111111","email":"owner@example.test","role":"owner"}"#.utf8
    )

    private func makeState(origin: String? = nil, owner: UUID? = nil,
                           inputKind: InputKind = .importedMixed) -> LocalSessionState {
        let package = RecordingPackage(
            inputKind: inputKind, startedAt: Date(), title: "test", titleEditedByUser: true,
            languageMode: .auto, allowExternalSend: false, formatProfileId: nil, tracks: [],
            source: RecordingSource(kind: "file")
        )
        return LocalSessionState(package: package, trackFiles: [:], destinationOrigin: origin, ownerUserId: owner)
    }

    private func temporaryRoot() -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try! FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return root
    }

    private func makeService() throws -> SessionService {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RequestStub.self]
        return SessionService(client: try APIClient(baseURL: URL(string: "http://localhost:8787")!,
                                                    session: URLSession(configuration: configuration)))
    }
}

private final class RequestStub: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    private static var recorded: [URLRequest] = []
    static var handler: ((URLRequest) throws -> (Int, Data))?

    static var requests: [URLRequest] { lock.withLock { recorded } }
    static func reset() { lock.withLock { recorded = [] }; handler = nil }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.withLock { Self.recorded.append(request) }
        do {
            let (status, body) = try XCTUnwrap(Self.handler)(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: body)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}

private func XCTAssertThrowsErrorAsync<T>(_ expression: @autoclosure () async throws -> T,
                                          _ verify: (Error) -> Void,
                                          file: StaticString = #filePath, line: UInt = #line) async {
    do { _ = try await expression(); XCTFail("error was not thrown", file: file, line: line) }
    catch { verify(error) }
}
