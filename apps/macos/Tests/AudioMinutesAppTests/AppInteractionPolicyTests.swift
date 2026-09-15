import BridgeCore
import ClientCore
import Foundation
import XCTest

@testable import AudioMinutesApp

@MainActor
final class AppInteractionPolicyTests: XCTestCase {
    func testGUIBindingPreservesDestinationAndOwnerOrLeavesUnknownOwnerUnassigned() throws {
        var settings = ClientSettings(apiBaseUrl: "HTTPS://Minutes.Example.test:443/api")
        let me = try ContractCoding.decoder().decode(
            Me.self,
            from: Data(#"{"user_id":"11111111-1111-4111-8111-111111111111","email":"owner@example.test","role":"owner"}"#.utf8)
        )
        var binding = AppModel.localSessionBinding(settings: settings, currentUser: me)
        XCTAssertEqual(binding.destinationOrigin, "https://minutes.example.test")
        XCTAssertEqual(binding.ownerUserId, me.userId)

        settings.apiBaseUrl = "http://localhost:8787"
        binding = AppModel.localSessionBinding(settings: settings, currentUser: nil)
        XCTAssertEqual(binding.destinationOrigin, "http://localhost:8787")
        XCTAssertNil(binding.ownerUserId)
    }

    func testChromeTabSearchIsCaseInsensitiveAndKeepsFaviconMetadata() {
        let tabs = [
            BridgeTab(tabId: 1, title: "Weekly MEET", faviconUrl: "https://example.test/meet.ico", audible: true, windowId: 1),
            BridgeTab(tabId: 2, title: "設計レビュー", faviconUrl: nil, audible: false, windowId: 1),
        ]
        let result = AppModel.filteredChromeTabs(tabs, query: "meet")
        XCTAssertEqual(result.map(\.tabId), [1])
        XCTAssertEqual(result.first?.faviconUrl, "https://example.test/meet.ico")
    }

    func testChromeWholeApplicationCanExplicitlyReplacePreviousTabSelection() {
        XCTAssertFalse(AppModel.restoredChromeWholeApplicationSelection(
            .init(kind: "chrome_tab", bundleId: "com.google.Chrome", tabTitle: "Meet")
        ))
        XCTAssertFalse(AppModel.restoredChromeWholeApplicationSelection(nil))
        XCTAssertTrue(AppModel.restoredChromeWholeApplicationSelection(
            .init(kind: "app", bundleId: "com.google.Chrome")
        ))

        let previous = AppModel.chromeSelection(tabID: 42)
        XCTAssertEqual(previous.tabID, 42)
        XCTAssertFalse(previous.wholeApplicationExplicitlySelected)

        let switched = AppModel.chromeSelection(tabID: nil)
        XCTAssertNil(switched.tabID)
        XCTAssertTrue(switched.wholeApplicationExplicitlySelected)
    }
}
