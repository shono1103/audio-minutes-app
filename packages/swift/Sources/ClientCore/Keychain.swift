import Foundation
import Security

/// 長期トークンを OS の安全な資格情報ストアへ保存する (FR-044)。service は `dev.audio-minutes.client`。
public protocol SecretStore: Sendable {
    func read(account: String) throws -> Data?
    func write(account: String, data: Data) throws
    func delete(account: String) throws
}

public struct KeychainStore: SecretStore {
    public static let service = "dev.audio-minutes.client"
    public init() {}

    public func read(account: String) throws -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw KeychainError.status(status) }
        return item as? Data
    }

    public func write(account: String, data: Data) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: account,
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        let update = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if update == errSecItemNotFound {
            var add = query
            add.merge(attributes) { _, new in new }
            let status = SecItemAdd(add as CFDictionary, nil)
            guard status == errSecSuccess else { throw KeychainError.status(status) }
        } else if update != errSecSuccess {
            throw KeychainError.status(update)
        }
    }

    public func delete(account: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw KeychainError.status(status) }
    }
}

public enum KeychainError: Error, LocalizedError {
    case status(OSStatus)
    public var errorDescription: String? {
        if case .status(let status) = self {
            return "Keychain 操作に失敗しました (\(status))"
        }
        return nil
    }
}

/// テストや一時的な利用向けのメモリ上の SecretStore。
public final class InMemorySecretStore: SecretStore, @unchecked Sendable {
    private var storage: [String: Data] = [:]
    private let lock = NSLock()
    public init() {}
    public func read(account: String) throws -> Data? { lock.withLock { storage[account] } }
    public func write(account: String, data: Data) throws { lock.withLock { storage[account] = data } }
    public func delete(account: String) throws { _ = lock.withLock { storage.removeValue(forKey: account) } }
}
