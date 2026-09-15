import BridgeCore
import Darwin
import Foundation

let allowedOrigin = "chrome-extension://cglcpocpendfgbhepidgbpilokapdlnm/"
let origin = CommandLine.arguments.dropFirst().first
guard origin == allowedOrigin else {
    FileHandle.standardError.write(Data("許可されていない拡張originです\n".utf8))
    exit(2)
}

let socketPath: String
if let override = ProcessInfo.processInfo.environment["AM_BRIDGE_SOCKET"], !override.isEmpty {
    socketPath = override
} else {
    let applicationSupport = try FileManager.default.url(
        for: .applicationSupportDirectory,
        in: .userDomainMask,
        appropriateFor: nil,
        create: false
    )
    socketPath = applicationSupport
        .appendingPathComponent("AudioMinutes", isDirectory: true)
        .appendingPathComponent("bridge.sock")
        .path
}

signal(SIGPIPE, SIG_IGN)

do {
    let socketHandle = try BridgeUnixSocket.connect(path: socketPath)
    let chrome = FramedConnection(input: .standardInput, output: .standardOutput)
    let gui = FramedConnection(duplex: socketHandle)

    DispatchQueue.global(qos: .userInitiated).async {
        do {
            while let response = try gui.readJSONFrame() {
                try chrome.writeJSONFrame(response)
            }
        } catch {
            FileHandle.standardError.write(Data("GUIからのbridge中継が終了しました: \(error.localizedDescription)\n".utf8))
        }
        exit(3)
    }

    while let message = try chrome.readJSONFrame() {
        try gui.writeJSONFrame(message)
    }
    try? socketHandle.close()
} catch {
    FileHandle.standardError.write(Data("NativeBridgeを開始できません: \(error.localizedDescription)\n".utf8))
    exit(3)
}
