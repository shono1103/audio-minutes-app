import Darwin
import Foundation

public enum BridgeUnixSocket {
    private static func address(path: String) throws -> (sockaddr_un, socklen_t) {
        let pathBytes = Array(path.utf8) + [0]
        var address = sockaddr_un()
        let pathCapacity = MemoryLayout.size(ofValue: address.sun_path)
        guard pathBytes.count <= pathCapacity else { throw BridgeTransportError.socketPathTooLong }
        address.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.initializeMemory(as: UInt8.self, repeating: 0)
            buffer.copyBytes(from: pathBytes)
        }
        let length = socklen_t(MemoryLayout<sockaddr_un>.size)
        address.sun_len = UInt8(length)
        return (address, length)
    }

    public static func connect(path: String) throws -> FileHandle {
        var (address, addressLength) = try address(path: path)

        let descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            throw BridgeTransportError.socketConnectFailed(errno)
        }

        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { socketAddress in
                Darwin.connect(descriptor, socketAddress, addressLength)
            }
        }
        guard result == 0 else {
            let code = errno
            Darwin.close(descriptor)
            throw BridgeTransportError.socketConnectFailed(code)
        }
        return FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
    }

    /// NativeBridgeからの接続を受けるlocal-only listener。既存socket fileだけを置換する。
    public static func listen(path: String) throws -> Int32 {
        var (address, addressLength) = try address(path: path)
        let descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw BridgeTransportError.socketBindFailed(errno) }
        Darwin.unlink(path)
        let bindResult = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(descriptor, $0, addressLength) }
        }
        guard bindResult == 0 else {
            let code = errno; Darwin.close(descriptor); throw BridgeTransportError.socketBindFailed(code)
        }
        guard Darwin.chmod(path, S_IRUSR | S_IWUSR) == 0, Darwin.listen(descriptor, 1) == 0 else {
            let code = errno; Darwin.close(descriptor); Darwin.unlink(path); throw BridgeTransportError.socketListenFailed(code)
        }
        return descriptor
    }
}
