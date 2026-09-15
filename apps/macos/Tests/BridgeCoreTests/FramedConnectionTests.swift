import BridgeCore
import Darwin
import Foundation
import XCTest

final class FramedConnectionTests: XCTestCase {
    func testRoundTripsJSONAcrossSocketPair() throws {
        let (left, right) = try socketPair()
        defer { try? left.close(); try? right.close() }
        let sender = FramedConnection(duplex: left)
        let receiver = FramedConnection(duplex: right)
        let body = Data(#"{"type":"hello","version":1}"#.utf8)

        try sender.writeJSONFrame(body)

        XCTAssertEqual(try receiver.readJSONFrame(), body)
    }

    func testReadsHeaderAndBodyArrivingInPieces() throws {
        let (left, right) = try socketPair()
        defer { try? left.close(); try? right.close() }
        let receiver = FramedConnection(duplex: right)
        let body = Data(#"{"type":"tabs","version":1}"#.utf8)
        var length = UInt32(body.count).littleEndian
        let header = Data(bytes: &length, count: 4)

        try left.write(contentsOf: header.prefix(2))
        try left.write(contentsOf: header.suffix(2))
        try left.write(contentsOf: body.prefix(5))
        try left.write(contentsOf: body.dropFirst(5))

        XCTAssertEqual(try receiver.readJSONFrame(), body)
    }

    func testRejectsOversizedFrameBeforeReadingBody() throws {
        let (left, right) = try socketPair()
        defer { try? left.close(); try? right.close() }
        let receiver = FramedConnection(duplex: right)
        var length = UInt32(FramedConnection.maximumFrameBytes + 1).littleEndian
        try left.write(contentsOf: Data(bytes: &length, count: 4))

        XCTAssertThrowsError(try receiver.readJSONFrame()) { error in
            XCTAssertEqual(error as? BridgeTransportError, .frameTooLarge(FramedConnection.maximumFrameBytes + 1))
        }
    }

    private func socketPair() throws -> (FileHandle, FileHandle) {
        var descriptors: [Int32] = [-1, -1]
        guard Darwin.socketpair(AF_UNIX, SOCK_STREAM, 0, &descriptors) == 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        return (
            FileHandle(fileDescriptor: descriptors[0], closeOnDealloc: true),
            FileHandle(fileDescriptor: descriptors[1], closeOnDealloc: true)
        )
    }
}
