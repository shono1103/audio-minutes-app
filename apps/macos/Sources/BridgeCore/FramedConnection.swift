import Foundation

public enum BridgeTransportError: Error, LocalizedError, Equatable {
    case invalidFrame
    case frameTooLarge(Int)
    case invalidJSON
    case socketPathTooLong
    case socketConnectFailed(Int32)
    case socketBindFailed(Int32)
    case socketListenFailed(Int32)

    public var errorDescription: String? {
        switch self {
        case .invalidFrame:
            return "bridge frameが途中で終了しました"
        case .frameTooLarge(let size):
            return "bridge frameが上限を超えています (\(size) bytes)"
        case .invalidJSON:
            return "bridge frameがJSONではありません"
        case .socketPathTooLong:
            return "bridge socketのパスが長すぎます"
        case .socketConnectFailed(let code):
            return "GUIのbridge socketへ接続できません (errno=\(code))"
        case .socketBindFailed(let code):
            return "GUIのbridge socketを作成できません (errno=\(code))"
        case .socketListenFailed(let code):
            return "GUIのbridge socketで待受できません (errno=\(code))"
        }
    }
}

/// Native MessagingとGUI socketで共用する、4 byte little-endian length + JSONのframe。
public final class FramedConnection: @unchecked Sendable {
    public static let maximumFrameBytes = 1_048_576

    private let input: FileHandle
    private let output: FileHandle
    private let writeLock = NSLock()

    public init(input: FileHandle, output: FileHandle) {
        self.input = input
        self.output = output
    }

    public convenience init(duplex handle: FileHandle) {
        self.init(input: handle, output: handle)
    }

    public func readJSONFrame() throws -> Data? {
        guard let header = try readExactly(4) else { return nil }
        let length = header.withUnsafeBytes { raw in
            Int(UInt32(littleEndian: raw.loadUnaligned(as: UInt32.self)))
        }
        guard length <= Self.maximumFrameBytes else {
            throw BridgeTransportError.frameTooLarge(length)
        }
        guard let body = try readExactly(length) else {
            throw BridgeTransportError.invalidFrame
        }
        guard (try? JSONSerialization.jsonObject(with: body)) != nil else {
            throw BridgeTransportError.invalidJSON
        }
        return body
    }

    public func writeJSONFrame(_ body: Data) throws {
        guard body.count <= Self.maximumFrameBytes else {
            throw BridgeTransportError.frameTooLarge(body.count)
        }
        guard (try? JSONSerialization.jsonObject(with: body)) != nil else {
            throw BridgeTransportError.invalidJSON
        }
        var length = UInt32(body.count).littleEndian
        let header = Data(bytes: &length, count: 4)
        try writeLock.withLock {
            try output.write(contentsOf: header)
            try output.write(contentsOf: body)
        }
    }

    private func readExactly(_ count: Int) throws -> Data? {
        if count == 0 { return Data() }
        var result = Data()
        result.reserveCapacity(count)
        while result.count < count {
            guard let chunk = try input.read(upToCount: count - result.count), !chunk.isEmpty else {
                if result.isEmpty { return nil }
                throw BridgeTransportError.invalidFrame
            }
            result.append(chunk)
        }
        return result
    }
}
