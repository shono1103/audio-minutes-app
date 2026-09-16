import AVFoundation
import ClientCore
import Foundation

/// クライアント側の録音状態 (全体計画 5 節)。GUI が唯一の所有者。
public enum RecordingState: String, Sendable, Equatable {
    case idle, recording, finalizing, saved, uploading, uploaded, failed
}

public enum RecordingTarget: Sendable, Equatable {
    case app(RecordableApp)
    case chromeTab(bundleID: String, tabID: Int, title: String)

    public var displayName: String {
        switch self {
        case .app(let app): return app.displayName
        case .chromeTab(_, _, let title): return "Google Meet「\(title)」"
        }
    }

    public var source: RecordingSource {
        switch self {
        case .app(let app): return RecordingSource(kind: "app", appName: app.displayName, bundleId: app.bundleID)
        case .chromeTab(let bundleID, _, _): return RecordingSource(kind: "chrome_tab", appName: "Google Chrome", bundleId: bundleID, tabHost: "meet.google.com")
        }
    }
}

public enum ChromeAudioSelection: Sendable, Equatable {
    case requiresExplicitSelection
    case wholeApplication
    case tab(Int)

    public func validated(availableTabIDs: Set<Int>) throws -> Int? {
        switch self {
        case .requiresExplicitSelection:
            throw RecordingError.preconditionFailed(["復元されたChrome録音元を再選択してください"])
        case .wholeApplication:
            return nil
        case .tab(let id):
            guard availableTabIDs.contains(id) else {
                throw RecordingError.preconditionFailed(["指定したGoogle Meetタブが見つかりません。録音元を再選択してください"])
            }
            return id
        }
    }
}

public struct RecordingOptions: Sendable, Equatable {
    public var title: String?
    public var languageMode: LanguageMode
    public var allowExternalSend: Bool
    public var formatProfileId: UUID?
    public var destinationOrigin: String?
    public var ownerUserId: UUID?
    public init(title: String? = nil, languageMode: LanguageMode = .auto, allowExternalSend: Bool = true,
                formatProfileId: UUID? = nil, destinationOrigin: String? = nil, ownerUserId: UUID? = nil) {
        self.title = title
        self.languageMode = languageMode
        self.allowExternalSend = allowExternalSend
        self.formatProfileId = formatProfileId
        self.destinationOrigin = destinationOrigin
        self.ownerUserId = ownerUserId
    }
}

public enum RecordingError: Error, LocalizedError, Sendable, Equatable {
    case alreadyRecording
    case notRecording
    case invalidTransition(from: RecordingState, to: RecordingState)
    case preconditionFailed([String])
    case trackFailed(String)

    public var errorDescription: String? {
        switch self {
        case .alreadyRecording: return "すでに録音中です。同時に開始できる録音は 1 件です"
        case .notRecording: return "録音していません"
        case .invalidTransition(let from, let to): return "状態遷移が不正です: \(from.rawValue) → \(to.rawValue)"
        case .preconditionFailed(let reasons): return "録音を開始できません: " + reasons.joined(separator: " / ")
        case .trackFailed(let detail): return "トラックの保存に失敗しました: \(detail)"
        }
    }
}

public enum AutomaticRecordingStop: Sendable {
    case saved(LocalSessionState)
    case failed(String)
}

/// 録音状態機械。遷移規則だけを持ち、音声取得は `RecordingCoordinator` が行う (テスト対象)。
public struct RecordingStateMachine: Sendable, Equatable {
    public private(set) var state: RecordingState = .idle

    public init() {}

    /// 公開 API の `canStart` と一致させる (R15)。stop() 後の saved、送信済みの uploaded、
    /// 失敗後の failed からは次の録音を直接始められる。前回の未送信 package は
    /// 消さず、`start()` が新しい session_id で別ディレクトリを作る。
    static let allowed: [RecordingState: Set<RecordingState>] = [
        .idle: [.recording],
        .recording: [.finalizing, .failed],
        .finalizing: [.saved, .failed],
        .saved: [.recording, .uploading, .idle],
        .uploading: [.uploaded, .saved, .failed],
        .uploaded: [.recording, .idle],
        .failed: [.recording, .idle, .saved, .finalizing],
    ]

    public mutating func transition(to next: RecordingState) throws {
        if state == .recording, next == .recording { throw RecordingError.alreadyRecording }
        guard Self.allowed[state]?.contains(next) == true else { throw RecordingError.invalidTransition(from: state, to: next) }
        state = next
    }

    public var canStart: Bool { state == .idle || state == .uploaded || state == .saved || state == .failed }
    public var isRecording: Bool { state == .recording }
}

/// 開始前の前提条件。不足理由を列挙して表示する (FR-080、FR-086)。
public struct RecordingPreconditions: Sendable, Equatable {
    public var microphonePermission: Bool
    public var systemAudioPermission: Bool
    public var targetAvailable: Bool
    public var microphoneAvailable: Bool
    public var freeSpaceBytes: Int64
    public var chromeBridgeConnectedIfNeeded: Bool

    public init(microphonePermission: Bool, systemAudioPermission: Bool, targetAvailable: Bool,
                microphoneAvailable: Bool, freeSpaceBytes: Int64, chromeBridgeConnectedIfNeeded: Bool = true) {
        self.microphonePermission = microphonePermission
        self.systemAudioPermission = systemAudioPermission
        self.targetAvailable = targetAvailable
        self.microphoneAvailable = microphoneAvailable
        self.freeSpaceBytes = freeSpaceBytes
        self.chromeBridgeConnectedIfNeeded = chromeBridgeConnectedIfNeeded
    }

    /// 4 時間 × 2 トラック × 16 kHz PCM16 ≒ 922 MB を最低確保する。
    public static let minimumFreeBytes: Int64 = 1_000_000_000

    public var failures: [String] {
        var reasons: [String] = []
        if !microphonePermission { reasons.append("マイクの権限が必要です") }
        if !systemAudioPermission { reasons.append("システム音声録音の権限が必要です") }
        if !targetAvailable { reasons.append("録音対象アプリが実行されていません") }
        if !microphoneAvailable { reasons.append("利用できるマイクがありません") }
        if freeSpaceBytes < Self.minimumFreeBytes { reasons.append("保存先の空き容量が不足しています (1 GB 以上必要)") }
        if !chromeBridgeConnectedIfNeeded { reasons.append("Chrome 連携を確認できません。再選択するか Chrome 全体を選んでください") }
        return reasons
    }

    public var ok: Bool { failures.isEmpty }
}

/// トラック 1 本分の書き込み。IO スレッドからバッファを受け取り、変換して WAV へ流す。
struct TrackFinalization: Sendable, Equatable {
    var sha256: String
    var byteSize: Int
    var durationMs: Int
}

struct TrackFinalizationAttempt: Sendable {
    var trackID: TrackID
    var role: TrackRole
    var finalization: TrackFinalization?
    var errorDescription: String?

    var succeeded: Bool { finalization != nil && errorDescription == nil }
}

protocol TrackAudioWriting: AnyObject, Sendable {
    func append(buffer: AVAudioPCMBuffer) throws
    func finalize() throws -> (sha256: String, byteSize: Int, durationMs: Int)
}

extension WavWriter: TrackAudioWriting {}

final class TrackWriter: AudioChunkSink, @unchecked Sendable {
    let trackID: TrackID
    let role: TrackRole
    let writer: any TrackAudioWriting
    private var downmixer: PCMDownmixer?
    private let makeDownmixer: @Sendable (AVAudioFormat) throws -> PCMDownmixer
    private(set) var firstHostTime: UInt64?
    private(set) var lastHostTime: UInt64?
    private let lock = NSLock()
    var onLevel: (@Sendable (Float) -> Void)?
    var onTargetLost: (@Sendable () -> Void)?
    var onFailure: (@Sendable (Error) -> Void)?
    private(set) var lastError: Error?
    private var finalization: TrackFinalization?
    private var liveChunkWriter: LiveChunkWriter?
    var onLiveChunkFailure: (@Sendable (Error) -> Void)?

    init(trackID: TrackID, role: TrackRole, url: URL, sessionID: UUID? = nil,
         liveChunkDirectory: URL? = nil,
         onChunkReady: (@Sendable (LiveAudioChunk) -> Void)? = nil) throws {
        self.trackID = trackID
        self.role = role
        self.writer = try WavWriter(url: url)
        self.makeDownmixer = { try PCMDownmixer(inputFormat: $0) }
        if let sessionID, let liveChunkDirectory, let onChunkReady {
            self.liveChunkWriter = try LiveChunkWriter(
                sessionID: sessionID, trackID: trackID, role: role,
                directory: liveChunkDirectory, onReady: onChunkReady
            )
        }
    }

    init(trackID: TrackID, role: TrackRole, writer: any TrackAudioWriting,
         makeDownmixer: @escaping @Sendable (AVAudioFormat) throws -> PCMDownmixer = { try PCMDownmixer(inputFormat: $0) }) {
        self.trackID = trackID
        self.role = role
        self.writer = writer
        self.makeDownmixer = makeDownmixer
    }

    func receive(buffer: AVAudioPCMBuffer, hostTime: UInt64) {
        lock.lock()
        guard lastError == nil else { lock.unlock(); return }
        if firstHostTime == nil { firstHostTime = hostTime }
        lastHostTime = hostTime
        let level = PCMDownmixer.rmsLevel(buffer)
        do {
            if downmixer == nil { downmixer = try makeDownmixer(buffer.format) }
            guard let downmixer else { throw WavError.converterUnavailable }
            let converted = try downmixer.convert(buffer)
            try writer.append(buffer: converted)
            if let liveChunkWriter {
                do { try liveChunkWriter.append(buffer: converted) }
                catch {
                    self.liveChunkWriter = nil
                    onLiveChunkFailure?(error)
                }
            }
            let onLevel = onLevel
            lock.unlock()
            onLevel?(level)
        } catch {
            lastError = error
            let callback = onFailure
            lock.unlock()
            callback?(error)
        }
    }

    func finalize() throws -> TrackFinalization {
        try lock.withLock {
            let result = try finalizeWriter()
            if let lastError { throw lastError }
            return result
        }
    }

    /// append 障害があっても WAV writer の finalize 自体は試み、その成否を回収情報へ残す。
    /// 呼び出し側は各 track を個別に呼ぶため、一方の失敗で他方の header 確定を妨げない。
    func finalizeForRecovery() -> TrackFinalizationAttempt {
        lock.withLock {
            do {
                let result = try finalizeWriter()
                return TrackFinalizationAttempt(
                    trackID: trackID, role: role, finalization: result,
                    errorDescription: lastError?.localizedDescription
                )
            } catch {
                return TrackFinalizationAttempt(
                    trackID: trackID, role: role, finalization: finalization,
                    errorDescription: error.localizedDescription
                )
            }
        }
    }

    private func finalizeWriter() throws -> TrackFinalization {
        if let finalization { return finalization }
        let value = try writer.finalize()
        if let liveChunkWriter {
            do { try liveChunkWriter.finalize() }
            catch {
                self.liveChunkWriter = nil
                onLiveChunkFailure?(error)
            }
        }
        let result = TrackFinalization(sha256: value.sha256, byteSize: value.byteSize, durationMs: value.durationMs)
        finalization = result
        return result
    }

    func targetLost() { onTargetLost?() }
}

/// app/mic 2 系統の入力レベルを世代付きで集約する。両系統は別々の capture callback スレッドから
/// 並行に通知するため、読み書きはこの型の内部 lock で直列化する。世代は開始・停止・開始失敗の
/// たびに進め、前世代の capture callback から遅延して届く通知を破棄できるようにする。
final class LevelAggregator: @unchecked Sendable {
    private var lastApp: Float = 0
    private var lastMic: Float = 0
    private var generation: Int = 0
    private let lock = NSLock()

    /// 新しい世代へ進め、値を 0 にリセットして新世代の識別子を返す。
    @discardableResult
    func reset() -> Int {
        lock.withLock {
            generation += 1
            lastApp = 0
            lastMic = 0
            return generation
        }
    }

    /// 通知元の世代が現世代と一致する場合のみ値を更新し、通知すべき最新の (app, mic) を返す。
    /// 一致しない場合 (停止後や前世代からの遅延通知) は nil を返し、呼び出し側は外部通知を行わない。
    func publish(app: Float?, mic: Float?, generation: Int) -> (app: Float, mic: Float)? {
        lock.withLock {
            guard generation == self.generation else { return nil }
            if let app { lastApp = app }
            if let mic { lastMic = mic }
            return (lastApp, lastMic)
        }
    }
}

/// GUI が所有する録音の実行体。2 トラックを同期して保存し、録音パッケージを作る。
public final class RecordingCoordinator: @unchecked Sendable {
    public static let maximumDuration: TimeInterval = 4 * 60 * 60
    public static func hasReachedMaximumDuration(_ elapsed: TimeInterval) -> Bool { elapsed >= maximumDuration }

    public private(set) var machine = RecordingStateMachine()
    private let store: LocalSessionStore
    private let clock = ClockSync()
    private var appWriter: TrackWriter?
    private var micWriter: TrackWriter?
    private var processCapture: ProcessTapCapture?
    private var microphone: MicrophoneCapture?
    private var chromeSink: ChromeTabTrackSink?
    private var sessionID = UUID()
    private var startedAt = Date()
    private var startHostTime: UInt64 = 0
    private var target: RecordingTarget?
    private var options = RecordingOptions()
    private let lock = NSLock()
    private let failureQueue = DispatchQueue(label: "dev.audio-minutes.recording-failure")
    private var failureStopScheduled = false

    public var onLevels: (@Sendable (_ app: Float, _ mic: Float) -> Void)?
    public var onStoppedByTargetLoss: (@Sendable (AutomaticRecordingStop) -> Void)?
    public var onStoppedByTrackFailure: (@Sendable (String) -> Void)?
    public var onLiveChunkReady: (@Sendable (LiveAudioChunk) -> Void)?
    public var onLiveChunkFailure: (@Sendable (String) -> Void)?

    public var state: RecordingState { lock.withLock { machine.state } }
    public var currentSessionID: UUID? { state == .idle ? nil : sessionID }
    public var currentStartedAt: Date? { state == .idle ? nil : startedAt }
    public var elapsed: TimeInterval { state == .recording ? Date().timeIntervalSince(startedAt) : 0 }
    public var currentTarget: RecordingTarget? { target }

    public init(store: LocalSessionStore) {
        self.store = store
    }

    /// 録音開始。対象の同一性と PID 集合を再確認し、その集合で固定する。
    public func start(target: RecordingTarget, microphone device: InputDevice, options: RecordingOptions,
                      chromeSink: ChromeTabTrackSink? = nil) throws {
        try lock.withLock {
            guard machine.canStart else { throw RecordingError.alreadyRecording }
            try machine.transition(to: .recording)
        }
        do {
            sessionID = UUID()
            lock.withLock { failureStopScheduled = false }
            startedAt = Date()
            startHostTime = mach_absolute_time()
            self.target = target
            self.options = options
            let generation = resetLevels()
            let directory = try store.prepare(sessionID: sessionID)
            let chunkDirectory = directory.appendingPathComponent("chunks", isDirectory: true)
            let chunkReady: @Sendable (LiveAudioChunk) -> Void = { [weak self] chunk in self?.onLiveChunkReady?(chunk) }
            let app = try TrackWriter(
                trackID: .appAudio, role: .app, url: directory.appendingPathComponent("app-audio.wav"),
                sessionID: sessionID, liveChunkDirectory: chunkDirectory, onChunkReady: chunkReady
            )
            let mic = try TrackWriter(
                trackID: .microphone, role: .microphone, url: directory.appendingPathComponent("microphone.wav"),
                sessionID: sessionID, liveChunkDirectory: chunkDirectory, onChunkReady: chunkReady
            )
            appWriter = app
            micWriter = mic
            app.onLevel = { [weak self] level in self?.publishLevels(app: level, mic: nil, generation: generation) }
            mic.onLevel = { [weak self] level in self?.publishLevels(app: nil, mic: level, generation: generation) }
            let lost: @Sendable () -> Void = { [weak self] in self?.handleTargetLost() }
            app.onTargetLost = lost
            mic.onTargetLost = lost
            let failed: @Sendable (Error) -> Void = { [weak self] error in self?.handleTrackFailure(error) }
            app.onFailure = failed
            mic.onFailure = failed
            let liveFailed: @Sendable (Error) -> Void = { [weak self] error in self?.onLiveChunkFailure?(error.localizedDescription) }
            app.onLiveChunkFailure = liveFailed
            mic.onLiveChunkFailure = liveFailed

            switch target {
            case .app(let selected):
                guard let confirmed = AppDiscovery.reconfirm(selected) else { throw CaptureError.targetLost }
                let capture = ProcessTapCapture(pids: confirmed.pids)
                try capture.start(sink: app)
                processCapture = capture
            case .chromeTab:
                guard let chromeSink else { throw RecordingError.preconditionFailed(["Chrome 連携が接続されていません"]) }
                chromeSink.attach(sink: app)
                self.chromeSink = chromeSink
            }
            let micCapture = MicrophoneCapture(device: device)
            try micCapture.start(sink: mic)
            microphone = micCapture
        } catch {
            teardownCaptures()
            resetLevels()
            _ = lock.withLock { try? machine.transition(to: .failed) }
            try? store.remove(sessionID: sessionID)
            _ = lock.withLock { try? machine.transition(to: .idle) }
            throw error
        }
    }

    private let levels = LevelAggregator()

    /// レベル集約を新しい世代へリセットし、UI へ 0 を通知する。開始・停止・開始失敗のたびに呼ぶ。
    @discardableResult
    private func resetLevels() -> Int {
        let generation = levels.reset()
        onLevels?(0, 0)
        return generation
    }

    private func publishLevels(app: Float?, mic: Float?, generation: Int) {
        guard let snapshot = levels.publish(app: app, mic: mic, generation: generation) else { return }
        onLevels?(snapshot.app, snapshot.mic)
    }

    private func handleTargetLost() {
        guard state == .recording else { return }
        do { onStoppedByTargetLoss?(.saved(try stop())) }
        catch { onStoppedByTargetLoss?(.failed(error.localizedDescription)) }
    }

    private func handleTrackFailure(_ error: Error) {
        let shouldStop = lock.withLock { () -> Bool in
            guard machine.state == .recording, !failureStopScheduled else { return false }
            failureStopScheduled = true
            return true
        }
        guard shouldStop else { return }
        failureQueue.async { [weak self] in
            guard let self else { return }
            _ = try? self.stop()
            self.onStoppedByTrackFailure?(error.localizedDescription)
        }
    }

    /// 停止して 2 トラックを確定し、ローカル状態を保存する。
    @discardableResult
    public func stop() throws -> LocalSessionState {
        try lock.withLock { try machine.transition(to: .finalizing) }
        teardownCaptures()
        resetLevels()
        guard let appWriter, let micWriter, let target else {
            _ = lock.withLock { try? machine.transition(to: .failed) }
            throw RecordingError.notRecording
        }
        let offsets = clock.offsets(firstHostTimes: [
            TrackID.appAudio.rawValue: appWriter.firstHostTime ?? startHostTime,
            TrackID.microphone.rawValue: micWriter.firstHostTime ?? startHostTime,
        ])
        // 片方が失敗してももう片方を必ず finalize し、可能な限り双方の WAV header を確定する。
        let attempts = Self.finalizeIndependently([appWriter, micWriter])
        let failures = attempts.compactMap(\.errorDescription)
        guard failures.isEmpty,
              let appResult = attempts.first(where: { $0.trackID == .appAudio })?.finalization,
              let micResult = attempts.first(where: { $0.trackID == .microphone })?.finalization else {
            let detail = failures.isEmpty ? "トラック確定結果が不足しています" : failures.joined(separator: " / ")
            _ = lock.withLock { try? machine.transition(to: .failed) }
            let recovery = makeRecovery(target: target, attempts: attempts, error: detail)
            do { try store.saveRecovery(recovery) }
            catch { throw RecordingError.trackFailed("\(detail) / 回収情報の保存にも失敗しました: \(error.localizedDescription)") }
            throw RecordingError.trackFailed(detail)
        }
        do {
            let tracks = [
                RecordingTrack(trackId: .appAudio, role: .app, startOffsetMs: offsets.offsetsMs[TrackID.appAudio.rawValue] ?? 0,
                               container: "wav", codec: "pcm_s16le", sampleRate: 16000, channels: 1,
                               durationMs: appResult.durationMs, byteSize: appResult.byteSize, sha256: appResult.sha256),
                RecordingTrack(trackId: .microphone, role: .microphone, startOffsetMs: offsets.offsetsMs[TrackID.microphone.rawValue] ?? 0,
                               container: "wav", codec: "pcm_s16le", sampleRate: 16000, channels: 1,
                               durationMs: micResult.durationMs, byteSize: micResult.byteSize, sha256: micResult.sha256),
            ]
            // 利用者入力と録音元由来の仮タイトルを区別する (FR-107)。未入力のときは
            // 仮タイトルを送りつつ titleEditedByUser を false にして、議事録生成で
            // Claude の提案タイトルへ更新できるようにする。
            let entered = options.title?.trimmingCharacters(in: .whitespaces)
            let titleEditedByUser = !(entered ?? "").isEmpty
            let title = titleEditedByUser ? entered! : Self.provisionalTitle(target: target, date: startedAt)
            let package = RecordingPackage(sessionId: sessionID, inputKind: .recordedDualTrack, startedAt: startedAt,
                                           title: title, titleEditedByUser: titleEditedByUser,
                                           languageMode: options.languageMode,
                                           allowExternalSend: options.allowExternalSend,
                                           formatProfileId: options.formatProfileId, tracks: tracks, source: target.source)
            var state = LocalSessionState(package: package, trackFiles: [
                TrackID.appAudio.rawValue: "app-audio.wav", TrackID.microphone.rawValue: "microphone.wav",
            ], destinationOrigin: options.destinationOrigin, ownerUserId: options.ownerUserId)
            state.lastError = nil
            try store.save(state)
            try lock.withLock { try machine.transition(to: .saved) }
            return state
        } catch {
            _ = lock.withLock { try? machine.transition(to: .failed) }
            try? store.saveRecovery(makeRecovery(target: target, attempts: attempts, error: error.localizedDescription))
            throw RecordingError.trackFailed(error.localizedDescription)
        }
    }

    static func finalizeIndependently(_ writers: [TrackWriter]) -> [TrackFinalizationAttempt] {
        writers.map { $0.finalizeForRecovery() }
    }

    private func makeRecovery(target: RecordingTarget, attempts: [TrackFinalizationAttempt], error: String) -> LocalRecoveryState {
        let entered = options.title?.trimmingCharacters(in: .whitespaces)
        let titleEditedByUser = !(entered ?? "").isEmpty
        let recoveryTracks = attempts.map { attempt in
            let result = attempt.finalization
            let fileName = attempt.trackID == .appAudio ? "app-audio.wav" : "microphone.wav"
            return LocalRecoveryTrack(
                trackId: attempt.trackID, role: attempt.role, fileName: fileName,
                headerFinalized: result != nil, durationMs: result?.durationMs,
                byteSize: result?.byteSize, sha256: result?.sha256, error: attempt.errorDescription
            )
        }
        return LocalRecoveryState(
            sessionId: sessionID, startedAt: startedAt,
            title: titleEditedByUser ? entered! : Self.provisionalTitle(target: target, date: startedAt),
            titleEditedByUser: titleEditedByUser, languageMode: options.languageMode,
            allowExternalSend: options.allowExternalSend, formatProfileId: options.formatProfileId,
            source: target.source, tracks: recoveryTracks, error: error
        )
    }

    public static func provisionalTitle(target: RecordingTarget, date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ja_JP")
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        return "\(target.displayName) \(formatter.string(from: date))"
    }

    /// upload の状態遷移だけを扱う (実送信は LocalSessionStore.uploadPending)。
    public func markUploading() throws { try lock.withLock { try machine.transition(to: .uploading) } }
    public func markUploaded() throws { try lock.withLock { try machine.transition(to: .uploaded); try machine.transition(to: .idle) } }
    public func markUploadFailed() { _ = lock.withLock { try? machine.transition(to: .saved) } }
    public func reset() { _ = lock.withLock { try? machine.transition(to: .idle) } }

    private func teardownCaptures() {
        processCapture?.stop()
        processCapture = nil
        microphone?.stop()
        microphone = nil
        chromeSink?.detach()
        chromeSink = nil
    }
}
