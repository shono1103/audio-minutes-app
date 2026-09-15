import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

public enum CaptureError: Error, LocalizedError, Sendable {
    case pidTranslationFailed([pid_t])
    case tapCreationFailed(OSStatus)
    case aggregateDeviceFailed(OSStatus)
    case tapAssignmentFailed(OSStatus)
    case ioProcFailed(OSStatus)
    case deviceStartFailed(OSStatus)
    case formatUnavailable
    case targetLost
    case microphoneUnavailable(String)
    case permissionDenied(String)

    public var errorDescription: String? {
        switch self {
        case .pidTranslationFailed(let pids): return "対象プロセスを Core Audio で解決できません (PID \(pids.map(String.init).joined(separator: ",")))"
        case .tapCreationFailed(let status): return "プロセスタップを作成できません (\(status))。システム音声録音の権限を確認してください"
        case .aggregateDeviceFailed(let status): return "集約デバイスを作成できません (\(status))"
        case .tapAssignmentFailed(let status): return "タップをデバイスへ割り当てられません (\(status))"
        case .ioProcFailed(let status): return "IO proc を作成できません (\(status))"
        case .deviceStartFailed(let status): return "デバイスを開始できません (\(status))"
        case .formatUnavailable: return "音声フォーマットを取得できません"
        case .targetLost: return "録音対象アプリが終了したため録音を停止しました。取得済みの音声は保存しています"
        case .microphoneUnavailable(let name): return "マイクを利用できません: \(name)"
        case .permissionDenied(let what): return "\(what) の権限が拒否されています。システム設定 > プライバシーとセキュリティで許可してください"
        }
    }
}

/// 音声バッファの受け手。IO スレッドから呼ばれるので軽量にする。
public protocol AudioChunkSink: AnyObject, Sendable {
    func receive(buffer: AVAudioPCMBuffer, hostTime: UInt64)
    func targetLost()
}

private func propertyAddress(_ selector: AudioObjectPropertySelector,
                             scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

/// macOS 14.2 以降の Core Audio process tap による対象アプリ音声の取得 (FR-003)。
/// 開始時の PID 集合を固定し、以後追加探索や tap 再構築を行わない (FR-135、FR-136)。
public final class ProcessTapCapture: @unchecked Sendable {
    public let pids: [pid_t]
    private var tapID: AudioObjectID = kAudioObjectUnknown
    private var deviceID: AudioObjectID = kAudioObjectUnknown
    private var ioProcID: AudioDeviceIOProcID?
    private var format: AVAudioFormat?
    private weak var sink: AudioChunkSink?
    private var processListener: AudioObjectPropertyListenerBlock?
    private let queue = DispatchQueue(label: "dev.audio-minutes.process-tap")
    public private(set) var isRunning = false

    public init(pids: [pid_t]) {
        self.pids = pids
    }

    static func translate(pids: [pid_t]) throws -> [AudioObjectID] {
        var objects: [AudioObjectID] = []
        var failed: [pid_t] = []
        for pid in pids {
            var address = propertyAddress(kAudioHardwarePropertyTranslatePIDToProcessObject)
            var object: AudioObjectID = 0
            var size = UInt32(MemoryLayout<AudioObjectID>.size)
            var mutablePID = pid
            let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                                    UInt32(MemoryLayout<pid_t>.size), &mutablePID, &size, &object)
            if status == noErr, object != kAudioObjectUnknown { objects.append(object) } else { failed.append(pid) }
        }
        // 一部の helper が未解決でも、1 つ以上解決できれば録音は開始できる。全滅なら失敗。
        guard !objects.isEmpty else { throw CaptureError.pidTranslationFailed(failed) }
        return objects
    }

    public func start(sink: AudioChunkSink) throws {
        self.sink = sink
        let processes = try Self.translate(pids: pids)
        let description = CATapDescription(stereoMixdownOfProcesses: processes)
        description.name = "audio-minutes-tap"
        description.isPrivate = true
        description.muteBehavior = .unmuted
        description.isExclusive = false

        var tap: AudioObjectID = kAudioObjectUnknown
        let tapStatus = AudioHardwareCreateProcessTap(description, &tap)
        guard tapStatus == noErr else { throw CaptureError.tapCreationFailed(tapStatus) }
        tapID = tap

        var formatAddress = propertyAddress(kAudioTapPropertyFormat)
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        guard AudioObjectGetPropertyData(tapID, &formatAddress, 0, nil, &size, &asbd) == noErr,
              let tapFormat = AVAudioFormat(streamDescription: &asbd) else {
            cleanup()
            throw CaptureError.formatUnavailable
        }
        format = tapFormat

        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "audio-minutes-aggregate",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceSubDeviceListKey: [] as CFArray,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: description.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]] as CFArray,
        ]
        var device: AudioObjectID = 0
        let aggregateStatus = AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &device)
        guard aggregateStatus == noErr else {
            cleanup()
            throw CaptureError.aggregateDeviceFailed(aggregateStatus)
        }
        deviceID = device

        var procID: AudioDeviceIOProcID?
        let ioStatus = AudioDeviceCreateIOProcIDWithBlock(&procID, deviceID, queue) { [weak self] _, inputData, inputTime, _, _ in
            self?.handle(inputData, time: inputTime)
        }
        guard ioStatus == noErr, let procID else {
            cleanup()
            throw CaptureError.ioProcFailed(ioStatus)
        }
        ioProcID = procID
        let startStatus = AudioDeviceStart(deviceID, procID)
        guard startStatus == noErr else {
            cleanup()
            throw CaptureError.deviceStartFailed(startStatus)
        }
        installProcessListener()
        isRunning = true
    }

    /// 対象 process object が失われたら停止する (FR-136)。別 PID へは追従しない。
    private func installProcessListener() {
        var address = propertyAddress(kAudioHardwarePropertyProcessObjectList)
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self, self.isRunning else { return }
            if (try? Self.translate(pids: self.pids)) == nil {
                self.sink?.targetLost()
            }
        }
        processListener = block
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &address, queue, block)
    }

    private func handle(_ inputData: UnsafePointer<AudioBufferList>, time: UnsafePointer<AudioTimeStamp>) {
        guard let format, let sink else { return }
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inputData))
        guard let first = list.first, let data = first.mData, first.mDataByteSize > 0 else { return }
        let frames = AVAudioFrameCount(first.mDataByteSize / max(1, format.streamDescription.pointee.mBytesPerFrame))
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return }
        buffer.frameLength = frames
        let destination = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
        for (index, source) in list.enumerated() where index < destination.count {
            if let sourceData = source.mData, let target = destination[index].mData {
                memcpy(target, sourceData, Int(min(source.mDataByteSize, destination[index].mDataByteSize)))
            }
        }
        sink.receive(buffer: buffer, hostTime: time.pointee.mHostTime)
    }

    public func stop() {
        isRunning = false
        cleanup()
    }

    private func cleanup() {
        if let processListener {
            var address = propertyAddress(kAudioHardwarePropertyProcessObjectList)
            AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &address, queue, processListener)
            self.processListener = nil
        }
        if let ioProcID, deviceID != kAudioObjectUnknown {
            AudioDeviceStop(deviceID, ioProcID)
            AudioDeviceDestroyIOProcID(deviceID, ioProcID)
            self.ioProcID = nil
        }
        if deviceID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(deviceID)
            deviceID = kAudioObjectUnknown
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = kAudioObjectUnknown
        }
    }

    deinit { cleanup() }
}
