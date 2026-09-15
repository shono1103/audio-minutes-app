import XCTest
import ClientCore
@testable import AudioMinutesCLI

final class ArgumentsTests: XCTestCase {
    func testParsesValueFlagAndPositional() throws {
        let value = try Arguments(
            ["input.wav", "--title", "会議", "--language=ja", "--no-external-send"],
            valueOptions: ["--title", "--language"], flagOptions: ["--no-external-send"]
        )
        XCTAssertEqual(value.positionals, ["input.wav"])
        XCTAssertEqual(value.value("--title"), "会議")
        XCTAssertEqual(value.value("--language"), "ja")
        XCTAssertTrue(value.has("--no-external-send"))
    }

    func testRejectsUnknownAndDuplicateOptions() throws {
        XCTAssertThrowsError(try Arguments(["--record"], flagOptions: []))
        XCTAssertThrowsError(try Arguments(["--title", "a", "--title", "b"], valueOptions: ["--title"]))
        XCTAssertThrowsError(try Arguments(["--language"], valueOptions: ["--language"]))
    }

    func testDoubleDashAllowsOptionLikeFileName() throws {
        let value = try Arguments(["--", "--meeting.wav"])
        XCTAssertEqual(value.positionals, ["--meeting.wav"])
    }

    func testExitCodesAreStable() {
        XCTAssertEqual(CLI.exitCode(for: CLIError.usage("bad")), 2)
        XCTAssertEqual(CLI.exitCode(for: APIClientError.unauthenticated), 3)
        XCTAssertEqual(CLI.exitCode(for: APIClientError.transport("offline")), 5)
    }

    func testImportUsesClaudeSendDefaultUnlessExplicitlyOverridden() throws {
        var disabledByDefault = ClientSettings()
        disabledByDefault.claudeSendDefault = false
        let none = try Arguments(["meeting.wav"], flagOptions: ["--external-send", "--no-external-send", "--no-claude"])
        XCTAssertFalse(try CLI.importExternalSend(args: none, settings: disabledByDefault))

        let enable = try Arguments(["meeting.wav", "--external-send"], flagOptions: ["--external-send", "--no-external-send", "--no-claude"])
        XCTAssertTrue(try CLI.importExternalSend(args: enable, settings: disabledByDefault))

        var enabledByDefault = ClientSettings()
        enabledByDefault.claudeSendDefault = true
        let disable = try Arguments(["meeting.wav", "--no-external-send"], flagOptions: ["--external-send", "--no-external-send", "--no-claude"])
        XCTAssertFalse(try CLI.importExternalSend(args: disable, settings: enabledByDefault))
    }

    func testImportRejectsConflictingExternalSendOverrides() throws {
        let args = try Arguments(
            ["meeting.wav", "--external-send", "--no-claude"],
            flagOptions: ["--external-send", "--no-external-send", "--no-claude"]
        )
        XCTAssertThrowsError(try CLI.importExternalSend(args: args, settings: ClientSettings()))
    }

    func testCLIImportBindingUsesCanonicalServerAndCurrentUser() throws {
        let me = try ContractCoding.decoder().decode(
            Me.self,
            from: Data(#"{"user_id":"11111111-1111-4111-8111-111111111111","email":"owner@example.test","role":"owner"}"#.utf8)
        )
        let binding = CLI.localSessionBinding(baseURL: URL(string: "HTTPS://Minutes.Example.test:443/api")!, currentUser: me)
        XCTAssertEqual(binding.destinationOrigin, "https://minutes.example.test")
        XCTAssertEqual(binding.ownerUserId, me.userId)

        let offline = CLI.localSessionBinding(baseURL: URL(string: "http://localhost:8787")!, currentUser: nil)
        XCTAssertNil(offline.ownerUserId, "認証主体が不明なoffline取込を現在利用者へ暗黙割当しない")
    }

    func testResumeCancellationRequiresExplicitConfirmation() throws {
        let unconfirmed = try Arguments(["--cancel"], flagOptions: ["--cancel", "--yes"])
        XCTAssertThrowsError(try CLI.confirmedPendingCancellationID(unconfirmed))
        let confirmed = try Arguments(["11111111-1111-4111-8111-111111111111", "--cancel", "--yes"],
                                      flagOptions: ["--cancel", "--yes"])
        XCTAssertTrue(confirmed.has("--cancel"))
        XCTAssertTrue(confirmed.has("--yes"))
        XCTAssertEqual(confirmed.positionals.count, 1)
        XCTAssertEqual(try CLI.confirmedPendingCancellationID(confirmed), "11111111-1111-4111-8111-111111111111")
    }
}
