import Foundation
import XCTest

@testable import ClientCore

final class ContractCodingTests: XCTestCase {
    func testBrowserReauthContractAndOriginValidation() throws {
        let startJSON = #"{"request_id":"req-1","reauth_url":"http://127.0.0.1:8787/reauth/req-1","expires_at":"2026-09-14T03:00:00Z"}"#
        let start = try ContractCoding.decoder().decode(BrowserReauthStart.self, from: Data(startJSON.utf8))
        XCTAssertEqual(start.requestId, "req-1")
        let statusJSON = #"{"request_id":"req-1","status":"completed","reauth_valid_until":"2026-09-14T03:05:00Z"}"#
        let status = try ContractCoding.decoder().decode(BrowserReauthStatus.self, from: Data(statusJSON.utf8))
        XCTAssertEqual(status.status, "completed")
        XCTAssertNotNil(status.reauthValidUntil)

        let base = URL(string: "http://127.0.0.1:8787")!
        XCTAssertTrue(AuthManager.isAllowedReauthURL(URL(string: "http://127.0.0.1:8787/reauth/req-1")!, baseURL: base))
        XCTAssertFalse(AuthManager.isAllowedReauthURL(URL(string: "https://evil.example/reauth/req-1")!, baseURL: base))
    }

    func testBrowserPasskeyRegistrationContractAndOriginValidation() throws {
        let json = #"{"request_id":"pk-1","status":"pending","passkey_url":"http://127.0.0.1:8787/auth/account/passkeys/token","expires_at":"2026-09-14T03:00:00Z"}"#
        let value = try ContractCoding.decoder().decode(PasskeyRegistration.self, from: Data(json.utf8))
        XCTAssertEqual(value.requestId, "pk-1")
        XCTAssertEqual(value.status, "pending")
        XCTAssertNotNil(value.passkeyUrl)

        let base = URL(string: "http://127.0.0.1:8787")!
        let client = try APIClient(baseURL: base)
        let service = SessionService(client: client)
        XCTAssertTrue(service.isAllowedServerBrowserURL(value.passkeyUrl!))
        XCTAssertFalse(service.isAllowedServerBrowserURL("https://evil.example/auth/account/passkeys/token"))
    }

    func testAudioExportExtensionUsesResponseMetadata() {
        XCTAssertEqual(SessionService.audioFileExtension(.init(status: 200, headers: ["Content-Type": "audio/mpeg"], body: Data())), "mp3")
        XCTAssertEqual(SessionService.audioFileExtension(.init(status: 200, headers: ["Content-Disposition": "attachment; filename=track.flac"], body: Data())), "flac")
        XCTAssertEqual(SessionService.audioFileExtension(.init(status: 200, headers: [:], body: Data())), "bin")
    }
    func testRecordingPackageUsesContractKeysAndRoundTrips() throws {
        let sessionID = UUID(uuidString: "11111111-1111-4111-8111-111111111111")!
        let startedAt = Date(timeIntervalSince1970: 1_786_000_000.125)
        let track = RecordingTrack(
            trackId: .importedAudio,
            role: .mixed,
            startOffsetMs: 0,
            container: "wav",
            codec: "pcm_s16le",
            sampleRate: 16_000,
            channels: 1,
            durationMs: 1_000,
            byteSize: 32_044,
            sha256: String(repeating: "a", count: 64)
        )
        let package = RecordingPackage(
            sessionId: sessionID,
            inputKind: .importedMixed,
            startedAt: startedAt,
            title: "テスト",
            titleEditedByUser: true,
            languageMode: .auto,
            allowExternalSend: true,
            formatProfileId: nil,
            tracks: [track],
            source: RecordingSource(kind: "file_import")
        )

        let data = try ContractCoding.encoder().encode(package)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(object["schema_version"] as? String, "recording-package/2")
        XCTAssertEqual(object["input_kind"] as? String, "imported_mixed")
        XCTAssertEqual(object["allow_external_send"] as? Bool, true)
        XCTAssertEqual(object["title_edited_by_user"] as? Bool, true)

        let decoded = try ContractCoding.decoder().decode(RecordingPackage.self, from: data)
        XCTAssertEqual(decoded.sessionId, sessionID)
        XCTAssertEqual(decoded.requiredTrackIDs, [.importedAudio])
        XCTAssertTrue(decoded.titleEditedByUser)
        XCTAssertEqual(decoded.startedAt.timeIntervalSince1970, startedAt.timeIntervalSince1970, accuracy: 0.001)
    }

    func testPlainHttpIsRestrictedToLoopback() throws {
        XCTAssertNoThrow(try APIClient.validate(baseURL: URL(string: "http://127.0.0.1:8787")!))
        XCTAssertNoThrow(try APIClient.validate(baseURL: URL(string: "https://minutes.example.test")!))
        XCTAssertThrowsError(try APIClient.validate(baseURL: URL(string: "http://minutes.example.test")!))
    }

    func testClientSettingsMatchVersionedSchemaKeys() throws {
        let data = try ContractCoding.encoder().encode(ClientSettings())
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(object["schema_version"] as? String, "client-settings/1")
        XCTAssertEqual(object["profile"] as? String, "macos-colima-cpu")
        XCTAssertNotNil(object["notifications"] as? [String: Any])
        XCTAssertEqual(object["claude_send_default"] as? Bool, true)
        XCTAssertNil(object["onboarding_completed"])
    }
}
