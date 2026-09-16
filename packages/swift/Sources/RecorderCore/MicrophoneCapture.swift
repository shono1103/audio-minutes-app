import AVFoundation
import CoreAudio
import Foundation

/// 入力デバイス (FR-002、FR-150)。
public struct InputDevice: Sendable, Equatable, Hashable, Identifiable {
    public let id: AudioObjectID
    public let uid: String
    public let name: String
    public let isDefault: Bool
}

public enum InputDevices {
    public static func list() -> [InputDevice] {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                                 mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else { return [] }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &ids) == noErr else { return [] }
        let defaultID = defaultInputDeviceID()
        return ids.compactMap { id in
            guard hasInputStreams(id), let uid = stringProperty(id, kAudioDevicePropertyDeviceUID),
                  let name = stringProperty(id, kAudioObjectPropertyName) else { return nil }
            return InputDevice(id: id, uid: uid, name: name, isDefault: id == defaultID)
        }
    }

    public static func defaultInputDeviceID() -> AudioObjectID {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var id: AudioObjectID = 0
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &id)
        return id
    }

    private static func hasInputStreams(_ id: AudioObjectID) -> Bool {
        var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                                 mScope: kAudioDevicePropertyScopeInput,
                                                 mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        return AudioObjectGetPropertyDataSize(id, &address, 0, nil, &size) == noErr && size > 0
    }

    private static func stringProperty(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = withUnsafeMutablePointer(to: &value) { pointer in
            AudioObjectGetPropertyData(id, &address, 0, nil, &size, pointer)
        }
        guard status == noErr, let value else { return nil }
        return value.takeRetainedValue() as String
    }

    /// 前回選択したマイクを解決する。利用不能ならシステム既定へ仮切替し、その旨を返す (FR-150)。
    public static func resolve(preferredUID: String?) -> (device: InputDevice?, switchedToDefault: Bool) {
        let devices = list()
        if let preferredUID, let match = devices.first(where: { $0.uid == preferredUID }) { return (match, false) }
        let fallback = devices.first(where: \.isDefault) ?? devices.first
        return (fallback, preferredUID != nil && fallback != nil)
    }
}

/// AVAudioEngine の入力ノードからマイク音声を取得する (FR-004)。
public final class MicrophoneCapture: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private weak var sink: AudioChunkSink?
    public let device: InputDevice
    public private(set) var isRunning = false
    private var observer: NSObjectProtocol?

    public init(device: InputDevice) {
        self.device = device
    }

    public static func requestPermission() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
    }

    public func start(sink: AudioChunkSink) throws {
        self.sink = sink
        let input = engine.inputNode
        guard let unit = input.audioUnit else { throw CaptureError.microphoneUnavailable(device.name) }
        var deviceID = device.id
        let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                                          &deviceID, UInt32(MemoryLayout<AudioObjectID>.size))
        guard status == noErr else { throw CaptureError.microphoneUnavailable("\(device.name) (\(status))") }
        let format = input.inputFormat(forBus: 0)
        guard format.sampleRate > 0 else { throw CaptureError.microphoneUnavailable(device.name) }
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, time in
            self?.sink?.receive(buffer: buffer, hostTime: time.hostTime)
        }
        observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self] _ in
            guard let self,
                  Self.shouldStopAfterConfigurationChange(selectedUID: self.device.uid,
                                                          availableDevices: InputDevices.list()) else { return }
            // AVAudioEngine は開始直後にも通常の構成変更を通知する。選択した入力デバイスが
            // 実際に利用不能になった場合だけ停止し、取得済みを保持する。
            self.sink?.targetLost()
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw CaptureError.microphoneUnavailable("\(device.name): \(error.localizedDescription)")
        }
        isRunning = true
    }

    static func shouldStopAfterConfigurationChange(selectedUID: String, availableDevices: [InputDevice]) -> Bool {
        !availableDevices.contains { $0.uid == selectedUID }
    }

    public func stop() {
        guard isRunning else { return }
        isRunning = false
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
    }

    deinit { stop() }
}
