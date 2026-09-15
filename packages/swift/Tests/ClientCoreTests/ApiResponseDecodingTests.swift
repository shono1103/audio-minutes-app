import XCTest
@testable import ClientCore

/// `contracts/fixtures/api/` に置いた API の実応答を、SessionService が使う型で decode する (R06)。
///
/// 一覧応答は `{"items": [...]}` の envelope なので、配列を直接 decode すると空一覧でも
/// 必ず typeMismatch になる。空/非空の両方を通し、単一結果の戻り値型もここで固定する。
final class ApiResponseDecodingTests: XCTestCase {
    private static let fixtures: URL = {
        // Tests/ClientCoreTests → Tests → packages/swift → packages → リポジトリルート
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("contracts/fixtures/api", isDirectory: true)
    }()

    private func decode<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
        let data = try Data(contentsOf: Self.fixtures.appendingPathComponent(name))
        return try ContractCoding.decoder().decode(T.self, from: data)
    }

    // MARK: - 一覧 (envelope)

    func testJobListDecodesEmptyAndNonEmpty() throws {
        XCTAssertEqual(try decode(ItemList<JobSummary>.self, "jobs-list.empty.json").items.count, 0)
        let list = try decode(ItemList<JobSummary>.self, "jobs-list.json")
        XCTAssertEqual(list.items.count, 2)
        XCTAssertEqual(list.items[0].kind, "transcription")
        XCTAssertEqual(list.items[0].cancelRequested, false)
        XCTAssertEqual(list.items[1].failure?.code, .claudeRateLimited)
    }

    func testShareListAndSingleShareDecode() throws {
        XCTAssertEqual(try decode(ItemList<ShareEntry>.self, "shares-list.empty.json").items.count, 0)
        let list = try decode(ItemList<ShareEntry>.self, "shares-list.json")
        XCTAssertEqual(list.items.first?.email, "member@example.test")
        // 共有追加は配列ではなく 1 件を返す
        let added = try decode(ShareEntry.self, "share-added.json")
        XCTAssertEqual(added.email, "member@example.test")
        XCTAssertNil(added.createdAt)
    }

    func testPasskeyListDecodes() throws {
        XCTAssertEqual(try decode(ItemList<Passkey>.self, "passkeys-list.empty.json").items.count, 0)
        let list = try decode(ItemList<Passkey>.self, "passkeys-list.json")
        XCTAssertEqual(list.items.count, 2)
        XCTAssertEqual(list.items[0].label, "MacBook")
        XCTAssertNil(list.items[1].lastUsedAt)
    }

    func testFormatListDecodes() throws {
        XCTAssertEqual(try decode(ItemList<FormatProfile>.self, "formats-list.empty.json").items.count, 0)
        let list = try decode(ItemList<FormatProfile>.self, "formats-list.json")
        XCTAssertEqual(list.items.first?.name, "標準")
    }

    func testAdminListsDecode() throws {
        XCTAssertEqual(try decode(ItemList<Invitation>.self, "invitations-list.empty.json").items.count, 0)
        let invitations = try decode(ItemList<Invitation>.self, "invitations-list.json")
        XCTAssertEqual(invitations.items.first?.role, "member")
        XCTAssertNil(invitations.items.first?.url, "一覧では招待 URL を返さない")

        let created = try decode(Invitation.self, "invitation-created.json")
        XCTAssertEqual(created.url, "http://localhost:8787/auth/invite/xxxxx")

        let users = try decode(ItemList<AdminUser>.self, "users-list.json")
        XCTAssertEqual(users.items.first?.role, "owner")
        XCTAssertEqual(users.items.first?.disabled, false)

        let patched = try decode(AdminUser.self, "user-patched.json")
        XCTAssertTrue(patched.disabled)
        XCTAssertNil(patched.createdAt, "PATCH の応答に created_at は含まれない")
    }

    func testSessionListDecodes() throws {
        XCTAssertEqual(try decode(SessionList.self, "sessions-list.empty.json").items.count, 0)
        let list = try decode(SessionList.self, "sessions-list.json")
        XCTAssertEqual(list.items.count, 1)
        XCTAssertNotNil(list.nextCursor)
    }

    // MARK: - 議事録の版

    func testMinutesVersionListAndEnvelopeDecode() throws {
        let empty = try decode(MinutesVersionList.self, "minutes-versions-list.empty.json")
        XCTAssertEqual(empty.items.count, 0)
        XCTAssertNil(empty.currentMinutesVersionId)

        let list = try decode(MinutesVersionList.self, "minutes-versions-list.json")
        XCTAssertEqual(list.items.count, 1)
        XCTAssertEqual(list.currentMinutesVersionId, list.items[0].versionId)

        // 手動編集保存・現在版選択・復元はいずれも {"version": ...} を返す
        let envelope = try decode(MinutesVersionEnvelope.self, "minutes-version-envelope.json")
        XCTAssertEqual(envelope.version.versionId, list.items[0].versionId)
    }

    // MARK: - 単一結果

    func testAccountAndCapabilityResponsesDecode() throws {
        let me = try decode(Me.self, "me.json")
        XCTAssertTrue(me.isOwner)
        XCTAssertEqual(me.passkeyCount, 2)

        let codes = try decode(RecoveryCodes.self, "recovery-codes.json")
        XCTAssertEqual(codes.recoveryCodes.count, 2)

        let capabilities = try decode(Capabilities.self, "capabilities.json")
        XCTAssertEqual(capabilities.workers?.count, 2)
        XCTAssertEqual(capabilities.minutesAvailable, true)
        XCTAssertEqual(capabilities.claude?.state, "logged_in")
    }

    func testClaudeStatusAndLogoutDecode() throws {
        let status = try decode(ClaudeStatus.self, "claude-status.json")
        XCTAssertEqual(status.cliVersion, "2.1.268")
        XCTAssertEqual(status.connectedOwnerIsMe, true)
        // logout は state だけを返すので、他の項目が無くても decode できる必要がある
        let logout = try decode(ClaudeStatus.self, "claude-logout.json")
        XCTAssertEqual(logout.state, "logged_out")
        XCTAssertNil(logout.cliVersion)
    }

    func testAcceptedJobCarriesSession() throws {
        let accepted = try decode(AcceptedJob.self, "accepted-job.json")
        XCTAssertEqual(accepted.session.sessionId, accepted.session.id)
        XCTAssertEqual(accepted.jobId.uuidString.lowercased(), "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e")
    }
}
