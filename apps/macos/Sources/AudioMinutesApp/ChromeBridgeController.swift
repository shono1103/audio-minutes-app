import BridgeCore
import ClientCore
import Foundation
import RecorderCore

enum ChromeBridgeState: String, Sendable {
    case notInstalled = "未導入"
    case connected = "接続済み"
    case disconnected = "切断"
    case incompatible = "バージョン不一致"
}

/// GUI側のversioned protocol終端。PCMをRecorderCoreへ渡し、受信完了後だけackする。
final class ChromeBridgeController: @unchecked Sendable {
    let sink = ChromeTabTrackSink()
    private let server: BridgeServer
    private let lock = NSLock()
    private var helloAccepted = false
    private var activeCaptureID: String?
    private var stopRequestedByGUI = false
    private var stopSemaphore: DispatchSemaphore?

    var onState: (@Sendable (ChromeBridgeState) -> Void)?
    var onTabs: (@Sendable ([BridgeTab]) -> Void)?
    var onCapturePending: (@Sendable (String) -> Void)?
    var onCaptureStarted: (@Sendable (String) -> Void)?
    var onCaptureStopped: (@Sendable (String) -> Void)?
    var onError: (@Sendable (String) -> Void)?

    init(socketPath: String) { server = BridgeServer(path: socketPath) }

    func start() throws {
        try server.start(handler: { [weak self] data in try self?.handle(data) }, onDisconnect: { [weak self] in
            guard let self else { return }
            let semaphore = self.lock.withLock { () -> DispatchSemaphore? in
                self.helloAccepted = false; self.activeCaptureID = nil
                let value = self.stopSemaphore; self.stopSemaphore = nil; return value
            }
            semaphore?.signal()
            self.onState?(.disconnected)
            self.sink.captureStopped(reason: "disconnected")
        })
    }

    func stop() { server.stop() }
    func listTabs() throws { try server.send(BridgeOutgoing.listTabs()) }

    @discardableResult
    func requestCapture(tabID: Int) throws -> String {
        guard lock.withLock({ helloAccepted }) else { throw BridgeError.disconnected }
        let id = UUID().uuidString.lowercased()
        lock.withLock { activeCaptureID = id }
        do { try server.send(BridgeOutgoing.requestCapture(id: id, tabID: tabID)) }
        catch {
            lock.withLock { if activeCaptureID == id { activeCaptureID = nil } }
            throw error
        }
        return id
    }

    func stopCapture() throws {
        guard let id = lock.withLock({ activeCaptureID }) else { return }
        try server.send(BridgeOutgoing.stopCapture(id: id))
    }

    /// 拡張は最終chunkを送信し終えてからcapture_stoppedを返す。WAV確定はその後に行う。
    func stopCaptureAndWait(timeout: TimeInterval = 5) throws {
        guard let id = lock.withLock({ activeCaptureID }) else { return }
        let semaphore = DispatchSemaphore(value: 0)
        lock.withLock { stopRequestedByGUI = true; stopSemaphore = semaphore }
        do { try server.send(BridgeOutgoing.stopCapture(id: id)) }
        catch {
            lock.withLock { stopRequestedByGUI = false; stopSemaphore = nil }
            throw error
        }
        guard semaphore.wait(timeout: .now() + timeout) == .success else {
            lock.withLock { stopRequestedByGUI = false; stopSemaphore = nil }
            throw BridgeError.disconnected
        }
    }

    private func handle(_ data: Data) throws -> Data? {
        let message = try BridgeCodec.decode(data)
        if message.type == "hello" {
            let compatible = try BridgeCodec.validateHello(message)
            lock.withLock { helloAccepted = compatible }
            onState?(compatible ? .connected : .incompatible)
            return try BridgeCodec.encode(.hello(compatible: compatible))
        }
        guard lock.withLock({ helloAccepted }), message.version == BridgeProtocol.version else {
            throw BridgeProtocolError.incompatible(message.version)
        }
        switch message.type {
        case "tabs": onTabs?(message.tabs ?? [])
        case "capture_pending":
            guard let id = message.captureId else { throw BridgeProtocolError.invalidMessage("capture_id") }
            onCapturePending?(id)
        case "capture_started":
            guard let id = message.captureId, let sampleRate = message.sampleRate, let channels = message.channels,
                  id == lock.withLock({ activeCaptureID }) else { throw BridgeProtocolError.invalidMessage("capture_started") }
            try sink.captureStarted(sampleRate: sampleRate, channels: channels)
            onCaptureStarted?(id)
        case "chunk":
            guard let id = message.captureId, let seq = message.seq, let pts = message.ptsMs, let raw = message.data,
                  let pcm = Data(base64Encoded: raw), id == lock.withLock({ activeCaptureID }) else {
                throw BridgeProtocolError.invalidMessage("chunk")
            }
            try sink.receive(BridgeChunk(seq: seq, ptsMs: pts, pcm16le: pcm))
            return try BridgeCodec.encode(.ack(id: id, seq: seq))
        case "capture_stopped":
            let reason = message.reason ?? "error"
            let (requested, semaphore) = lock.withLock { () -> (Bool, DispatchSemaphore?) in
                activeCaptureID = nil
                let values = (stopRequestedByGUI, stopSemaphore)
                stopRequestedByGUI = false; stopSemaphore = nil
                return values
            }
            sink.captureStopped(reason: reason == "host_disconnected" ? "disconnected" : reason,
                                notifyTargetLoss: !requested)
            semaphore?.signal()
            onCaptureStopped?(reason)
        case "error":
            let detail = message.message ?? message.code ?? "Chrome連携でエラーが発生しました"
            if let id = message.captureId, id == lock.withLock({ activeCaptureID }) {
                lock.withLock { activeCaptureID = nil }
                sink.captureStopped(reason: "error")
            }
            onError?(detail)
        default: throw BridgeProtocolError.invalidMessage(message.type)
        }
        return nil
    }
}
