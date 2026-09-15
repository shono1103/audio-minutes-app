import BridgeCore
import Foundation
import XCTest

final class BridgeProtocolTests: XCTestCase {
    func testHelloRequiresVersionAndFixedExtensionID() throws {
        let valid = Data(#"{"type":"hello","version":1,"extension_version":"0.1.0","extension_id":"cglcpocpendfgbhepidgbpilokapdlnm"}"#.utf8)
        XCTAssertTrue(try BridgeCodec.validateHello(BridgeCodec.decode(valid)))

        let old = Data(#"{"type":"hello","version":0,"extension_id":"cglcpocpendfgbhepidgbpilokapdlnm"}"#.utf8)
        XCTAssertFalse(try BridgeCodec.validateHello(BridgeCodec.decode(old)))

        let foreign = Data(#"{"type":"hello","version":1,"extension_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#.utf8)
        XCTAssertThrowsError(try BridgeCodec.validateHello(BridgeCodec.decode(foreign)))
    }

    func testTabsNeverContainPageURLField() throws {
        let body = Data(#"{"type":"tabs","version":1,"tabs":[{"tab_id":42,"title":"Meet","favicon_url":null,"audible":true,"window_id":3}]}"#.utf8)
        let decoded = try BridgeCodec.decode(body)
        XCTAssertEqual(decoded.tabs?.first?.tabId, 42)
        XCTAssertFalse(String(decoding: body, as: UTF8.self).contains("\"url\""))
    }

    func testChunkAckUsesVersionedSnakeCaseContract() throws {
        let data = try BridgeCodec.encode(.ack(id: "capture-1", seq: 7))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(object["type"] as? String, "ack")
        XCTAssertEqual(object["version"] as? Int, 1)
        XCTAssertEqual(object["capture_id"] as? String, "capture-1")
        XCTAssertEqual(object["seq"] as? Int, 7)
    }

    func testSocketServerRelaysAndReportsDisconnect() throws {
        let path = "/tmp/am-gui-bridge-\(UUID().uuidString).sock"
        let server = BridgeServer(path: path)
        let disconnected = expectation(description: "disconnect")
        try server.start(handler: { body in
            let incoming = try BridgeCodec.decode(body)
            return try BridgeCodec.encode(.hello(compatible: try BridgeCodec.validateHello(incoming)))
        }, onDisconnect: { disconnected.fulfill() })
        defer { server.stop() }

        let handle = try BridgeUnixSocket.connect(path: path)
        let connection = FramedConnection(duplex: handle)
        try connection.writeJSONFrame(Data(#"{"type":"hello","version":1,"extension_id":"cglcpocpendfgbhepidgbpilokapdlnm"}"#.utf8))
        let reply = try XCTUnwrap(connection.readJSONFrame())
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: reply) as? [String: Any])
        XCTAssertEqual(object["compatible"] as? Bool, true)
        try handle.close()
        wait(for: [disconnected], timeout: 2)
    }
}
