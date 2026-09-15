import Darwin
import Foundation

/// GUIが所有するUnix socket server。NativeBridgeは1接続だけ許可し、切断を録音停止へ通知する。
public final class BridgeServer: @unchecked Sendable {
    private final class ClientConnection: @unchecked Sendable {
        let handle: FileHandle
        let framed: FramedConnection

        init(handle: FileHandle) {
            self.handle = handle
            self.framed = FramedConnection(duplex: handle)
        }
    }

    public typealias Handler = @Sendable (Data) throws -> Data?
    private let path: String
    private let queue = DispatchQueue(label: "dev.audio-minutes.bridge-server", qos: .userInitiated)
    private let lock = NSLock()
    private var listener: Int32 = -1
    private var connection: ClientConnection?
    private var running = false
    private var handler: Handler?
    private var disconnectHandler: (@Sendable () -> Void)?

    public init(path: String) { self.path = path }

    public func start(handler: @escaping Handler, onDisconnect: @escaping @Sendable () -> Void) throws {
        try FileManager.default.createDirectory(
            at: URL(fileURLWithPath: path).deletingLastPathComponent(), withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let descriptor = try BridgeUnixSocket.listen(path: path)
        lock.withLock {
            listener = descriptor; running = true; self.handler = handler; disconnectHandler = onDisconnect
        }
        queue.async { [weak self] in self?.acceptLoop() }
    }

    public func send<T: Encodable>(_ value: T) throws {
        let data = try JSONEncoder().encode(value)
        guard let connection = lock.withLock({ self.connection }) else { throw BridgeTransportError.socketConnectFailed(ENOTCONN) }
        // handler 応答と任意タイミングの ACK/stop は、接続ごとに同じ writer と lock を共有する。
        try connection.framed.writeJSONFrame(data)
    }

    public func stop() {
        let values: (Int32, ClientConnection?) = lock.withLock {
            running = false
            let result = (listener, connection)
            listener = -1; connection = nil
            return result
        }
        if values.0 >= 0 { Darwin.shutdown(values.0, SHUT_RDWR); Darwin.close(values.0) }
        try? values.1?.handle.close()
        Darwin.unlink(path)
    }

    private func acceptLoop() {
        while lock.withLock({ running }) {
            let current = lock.withLock { listener }
            guard current >= 0 else { break }
            let descriptor = Darwin.accept(current, nil, nil)
            if descriptor < 0 { if errno == EINTR { continue }; break }
            let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
            let client = ClientConnection(handle: handle)
            let previous = lock.withLock { () -> ClientConnection? in
                let old = connection; connection = client; return old
            }
            try? previous?.handle.close()
            serve(client)
            lock.withLock { if connection === client { connection = nil } }
            try? handle.close()
            disconnectHandler?()
        }
    }

    private func serve(_ connection: ClientConnection) {
        do {
            while lock.withLock({ running }), let body = try connection.framed.readJSONFrame() {
                if let response = try handler?(body) { try connection.framed.writeJSONFrame(response) }
            }
        } catch { /* 切断は下で一律通知する。本文やURLはログへ出さない。 */ }
    }

    deinit { stop() }
}
