import BridgeCore
import Darwin
import Foundation
import XCTest

final class UnixSocketTests: XCTestCase {
    func testConnectsAndRelaysAFrame() throws {
        let path = "/tmp/am-bridge-\(UUID().uuidString).sock"
        let listener = try makeListener(path: path)
        defer {
            Darwin.close(listener)
            path.withCString { _ = Darwin.unlink($0) }
        }

        let served = expectation(description: "server accepted a frame")
        DispatchQueue.global().async {
            let descriptor = Darwin.accept(listener, nil, nil)
            guard descriptor >= 0 else { return }
            let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
            let connection = FramedConnection(duplex: handle)
            if let frame = try? connection.readJSONFrame() {
                try? connection.writeJSONFrame(frame)
                served.fulfill()
            }
            try? handle.close()
        }

        let clientHandle = try BridgeUnixSocket.connect(path: path)
        defer { try? clientHandle.close() }
        let client = FramedConnection(duplex: clientHandle)
        let body = Data(#"{"type":"hello","version":1}"#.utf8)
        try client.writeJSONFrame(body)

        XCTAssertEqual(try client.readJSONFrame(), body)
        wait(for: [served], timeout: 2)
    }

    func testBridgeServerSerializesHandlerResponsesAndConcurrentSends() throws {
        let path = "/tmp/am-bridge-server-\(UUID().uuidString).sock"
        let server = BridgeServer(path: path)
        let writesFinished = expectation(description: "並行送信完了")
        let writeQueue = DispatchQueue(label: "test.bridge-writes", attributes: .concurrent)
        let count = 100
        let group = DispatchGroup()
        try server.start(handler: { body in
            for index in 0..<count {
                group.enter()
                writeQueue.async {
                    defer { group.leave() }
                    try? server.send(BridgeOutgoing.stopCapture(id: "stop-\(index)"))
                }
            }
            writeQueue.async {
                group.wait()
                writesFinished.fulfill()
            }
            return body // serve 側の ACK 相当応答も同じ writer を使う
        }, onDisconnect: {})
        defer { server.stop() }

        let clientHandle = try BridgeUnixSocket.connect(path: path)
        defer { try? clientHandle.close() }
        let client = FramedConnection(duplex: clientHandle)
        let ack = Data(#"{"type":"ack","version":1,"capture_id":"capture","seq":1}"#.utf8)
        try client.writeJSONFrame(ack)

        var received: [Data] = []
        for _ in 0...count {
            if let frame = try client.readJSONFrame() { received.append(frame) }
        }
        wait(for: [writesFinished], timeout: 3)
        XCTAssertEqual(received.count, count + 1)
        XCTAssertTrue(received.contains(ack))
        XCTAssertNoThrow(try received.forEach { _ = try JSONSerialization.jsonObject(with: $0) })
    }

    private func makeListener(path: String) throws -> Int32 {
        let descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw posixError() }
        var address = sockaddr_un()
        let pathBytes = Array(path.utf8) + [0]
        guard pathBytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
            Darwin.close(descriptor)
            throw BridgeTransportError.socketPathTooLong
        }
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.initializeMemory(as: UInt8.self, repeating: 0)
            buffer.copyBytes(from: pathBytes)
        }
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0, Darwin.listen(descriptor, 1) == 0 else {
            let error = posixError()
            Darwin.close(descriptor)
            throw error
        }
        return descriptor
    }

    private func posixError() -> NSError {
        NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
    }
}
