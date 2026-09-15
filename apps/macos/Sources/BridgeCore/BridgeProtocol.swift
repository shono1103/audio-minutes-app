import Foundation

public enum BridgeProtocol {
    public static let version = 1
    public static let hostVersion = "0.1.0"
    public static let extensionID = "cglcpocpendfgbhepidgbpilokapdlnm"
}

public struct BridgeTab: Codable, Sendable, Equatable, Identifiable {
    public var tabId: Int
    public var title: String
    public var faviconUrl: String?
    public var audible: Bool
    public var windowId: Int
    public var id: Int { tabId }

    public init(tabId: Int, title: String, faviconUrl: String?, audible: Bool, windowId: Int) {
        self.tabId = tabId
        self.title = title
        self.faviconUrl = faviconUrl
        self.audible = audible
        self.windowId = windowId
    }
}

/// 拡張から届くunion。typeごとの必須値はvalidateで検査する。
public struct BridgeIncoming: Decodable, Sendable, Equatable {
    public var type: String
    public var version: Int
    public var extensionVersion: String?
    public var extensionId: String?
    public var tabs: [BridgeTab]?
    public var captureId: String?
    public var tabId: Int?
    public var sampleRate: Double?
    public var channels: Int?
    public var seq: Int?
    public var ptsMs: Int?
    public var data: String?
    public var sampleCount: Int?
    public var reason: String?
    public var lastSeq: Int?
    public var code: String?
    public var message: String?
}

public struct BridgeOutgoing: Encodable, Sendable, Equatable {
    public var type: String
    public var version: Int = BridgeProtocol.version
    public var hostVersion: String?
    public var compatible: Bool?
    public var captureId: String?
    public var tabId: Int?
    public var seq: Int?

    public static func hello(compatible: Bool) -> Self { .init(type: "hello_ack", hostVersion: BridgeProtocol.hostVersion, compatible: compatible) }
    public static func listTabs() -> Self { .init(type: "list_tabs") }
    public static func requestCapture(id: String, tabID: Int) -> Self { .init(type: "request_capture", captureId: id, tabId: tabID) }
    public static func stopCapture(id: String) -> Self { .init(type: "stop_capture", captureId: id) }
    public static func ack(id: String, seq: Int) -> Self { .init(type: "ack", captureId: id, seq: seq) }
}

public enum BridgeProtocolError: Error, LocalizedError, Equatable {
    case invalidMessage(String)
    case incompatible(Int)
    case unauthorizedExtension

    public var errorDescription: String? {
        switch self {
        case .invalidMessage(let field): return "bridge messageが不正です: \(field)"
        case .incompatible(let version): return "Chrome連携protocolの版が非互換です: \(version)"
        case .unauthorizedExtension: return "固定IDと異なるChrome拡張を拒否しました"
        }
    }
}

public enum BridgeCodec {
    private static let decoder: JSONDecoder = { let value = JSONDecoder(); value.keyDecodingStrategy = .convertFromSnakeCase; return value }()
    private static let encoder: JSONEncoder = { let value = JSONEncoder(); value.keyEncodingStrategy = .convertToSnakeCase; return value }()

    public static func decode(_ data: Data) throws -> BridgeIncoming {
        do { return try decoder.decode(BridgeIncoming.self, from: data) }
        catch { throw BridgeProtocolError.invalidMessage(error.localizedDescription) }
    }

    public static func encode(_ value: BridgeOutgoing) throws -> Data { try encoder.encode(value) }

    public static func validateHello(_ message: BridgeIncoming) throws -> Bool {
        guard message.type == "hello", let extensionID = message.extensionId else { throw BridgeProtocolError.invalidMessage("hello") }
        guard extensionID == BridgeProtocol.extensionID else { throw BridgeProtocolError.unauthorizedExtension }
        return message.version == BridgeProtocol.version
    }
}
