import AppKit
import BridgeCore
import ClientCore
import Foundation
import ImportCore
import RecorderCore
import UserNotifications

struct RecordingStopOutcome {
    var state: LocalSessionState?
    var bridgeError: Error?
    var recorderError: Error?
    var succeeded: Bool { state != nil && recorderError == nil }
}

enum RecordingStopSequence {
    /// Chrome 側の停止確認が timeout / error でも、local recorder の停止と WAV 確定は必ず試す。
    static func run(stopBridge: (() throws -> Void)?, stopRecorder: () throws -> LocalSessionState) -> RecordingStopOutcome {
        var bridgeError: Error?
        do { try stopBridge?() } catch { bridgeError = error }
        do { return RecordingStopOutcome(state: try stopRecorder(), bridgeError: bridgeError, recorderError: nil) }
        catch { return RecordingStopOutcome(state: nil, bridgeError: bridgeError, recorderError: error) }
    }
}

/// `RecordingCoordinator.onLevels` の通知を1箇所で適用し、古い sequence の通知による
/// 上書きを防ぐ。app/mic は別々の capture callback スレッドから MainActor 上へ非同期
/// (Task) で届くため、Task の実行順序は発火順序と一致するとは限らない。sequence は
/// 発火のたびに単調増加するため、既に適用した値以下の sequence を無視するだけで、
/// 順序の入れ替わりや録音停止後の遅延通知を安全に破棄できる。
struct LevelApplier {
    private(set) var lastSequence = 0
    private(set) var app: Float = 0
    private(set) var mic: Float = 0

    /// sequence が既に適用済みの値以下なら無視する。適用した場合のみ true を返す。
    @discardableResult
    mutating func apply(app: Float, mic: Float, sequence: Int) -> Bool {
        guard sequence > lastSequence else { return false }
        lastSequence = sequence
        self.app = app
        self.mic = mic
        return true
    }
}

@MainActor
final class AppModel: ObservableObject {
    enum Pane: String, CaseIterable, Identifiable { case recording = "録音・取り込み", sessions = "セッション", settings = "設定"; var id: String { rawValue } }

    @Published var pane: Pane = .recording
    @Published var settings: ClientSettings
    @Published var apps: [RecordableApp] = []
    @Published var microphones: [InputDevice] = []
    @Published var selectedAppID: String?
    @Published var selectedTabID: Int?
    @Published var chromeWholeApplicationExplicitlySelected: Bool
    @Published var selectedMicrophoneUID: String?
    @Published var chromeTabs: [BridgeTab] = []
    @Published var chromeState: ChromeBridgeState = .notInstalled
    @Published var sessions: [Session] = []
    @Published var selectedSession: Session?
    @Published var transcript: Transcript?
    @Published var minutes: MinutesDocument?
    @Published var versions: MinutesVersionList?
    @Published var shares: [ShareEntry] = []
    @Published var jobs: [JobSummary] = []
    @Published var formats: [FormatProfile] = []
    @Published var me: Me?
    @Published var capabilities: Capabilities?
    @Published var claude: ClaudeStatus?
    @Published var claudeLoginSession: ClaudeLoginSession?
    @Published var retention: Retention?
    @Published var adminUsers: [AdminUser] = []
    @Published var invitations: [Invitation] = []
    @Published var passkeys: [Passkey] = []
    @Published var passkeyRegistration: PasskeyRegistration?
    @Published var localPendingUploads: [LocalSessionState] = []
    @Published var localRecoveries: [LocalRecoveryState] = []
    @Published var comparison = ""
    @Published var appLevel: Float = 0
    @Published var micLevel: Float = 0
    @Published var elapsed: TimeInterval = 0
    @Published var message = "準備中"
    @Published var busy = false
    @Published var title = ""
    @Published var languageMode: LanguageMode
    @Published var allowExternalSend: Bool
    @Published var selectedFormatID: UUID?
    @Published var pendingImportURL: URL?
    @Published var audioPlaybackURL: URL?
    @Published var liveUploadedChunks = 0
    @Published var liveFailedChunks = 0

    let settingsStore: SettingsStore
    let localStore: LocalSessionStore
    let recorder: RecordingCoordinator
    let bridge: ChromeBridgeController
    private var auth: AuthManager?
    private var service: SessionService?
    private var discovery: AppDiscoveryObserver?
    private var pollingTask: Task<Void, Never>?
    private var elapsedTimer: Timer?
    private var lastStatuses: [UUID: SessionStatus] = [:]
    private var liveUploaders: [UUID: LiveChunkUploadCoordinator] = [:]
    private var levelApplier = LevelApplier()

    var recordingState: RecordingState { recorder.state }
    var isRecording: Bool { recorder.state == .recording || recorder.state == .finalizing }
    var freeSpaceBytes: Int64 {
        let values = try? settingsStore.paths.root.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values?.volumeAvailableCapacityForImportantUsage ?? 0
    }

    convenience init() {
        self.init(paths: AppPaths(), startBackgroundServices: true)
    }

    /// テスト用の生成経路。一時ディレクトリ (`paths`) を使い、discovery/bridge/network の
    /// バックグラウンド起動 (`startBackgroundServices: false`) を抑止して生成できる。
    /// `init()` の通常起動はこの経路を `startBackgroundServices: true` で呼ぶだけで挙動を
    /// 変えない。`configureCallbacks()` は常に実行するため、`recorder.onLevels` など本番と
    /// 同じ callback 配線をテストから直接検証できる。
    init(paths: AppPaths, startBackgroundServices: Bool) {
        let store = SettingsStore(paths: paths)
        let local = LocalSessionStore(paths: paths)
        settingsStore = store
        localStore = local
        let loaded = (try? store.load()) ?? ClientSettings()
        settings = loaded
        languageMode = loaded.defaultLanguageMode
        allowExternalSend = loaded.claudeSendDefault
        selectedFormatID = loaded.defaultFormatProfileId
        chromeWholeApplicationExplicitlySelected = Self.restoredChromeWholeApplicationSelection(loaded.lastTarget)
        recorder = RecordingCoordinator(store: local)
        bridge = ChromeBridgeController(socketPath: store.paths.bridgeSocket.path)
        try? store.paths.ensureDirectories()
        reloadLocalSessions()
        configureCallbacks()
        guard startBackgroundServices else { return }
        configureClient()
        startDiscovery()
        startBridge()
        resumeAndMonitor()
    }

    private func configureCallbacks() {
        recorder.onLevels = { [weak self] app, mic, sequence in Task { @MainActor in
            guard let self, self.levelApplier.apply(app: app, mic: mic, sequence: sequence) else { return }
            self.appLevel = self.levelApplier.app
            self.micLevel = self.levelApplier.mic
        } }
        recorder.onStoppedByTargetLoss = { [weak self] result in Task { @MainActor in
            guard let self else { return }
            self.elapsedTimer?.invalidate()
            self.reloadLocalSessions()
            switch result {
            case .saved(let state):
                self.message = "入力元が失われたため停止し、取得済み音声を保存しました"
                await self.upload(state)
            case .failed(let detail):
                self.message = "入力元が失われ、録音の確定にも失敗しました。Finderから回収できます: \(detail)"
            }
        } }
        recorder.onStoppedByTrackFailure = { [weak self] detail in Task { @MainActor in
            self?.elapsedTimer?.invalidate()
            self?.reloadLocalSessions()
            self?.message = "録音トラックの保存に失敗したため停止しました。取得済みファイルは保持しています: \(detail)"
        } }
        bridge.onState = { [weak self] state in Task { @MainActor in self?.chromeState = state } }
        bridge.onTabs = { [weak self] tabs in Task { @MainActor in self?.chromeTabs = tabs } }
        bridge.onCapturePending = { [weak self] _ in Task { @MainActor in self?.message = "対象MeetタブでChrome拡張を押して録音を許可してください" } }
        bridge.onCaptureStarted = { [weak self] _ in Task { @MainActor in self?.message = "Google Meetタブを録音中です" } }
        // capture_stopped は local recorder.stop の前に届く。最終表示はlocal確定結果のcallback/停止処理が決める。
        bridge.onCaptureStopped = { _ in }
        bridge.onError = { [weak self] error in Task { @MainActor in self?.message = error } }
    }

    private func configureClient() {
        do {
            guard let url = settings.apiURL else { throw SettingsError.invalid("api_base_url") }
            let auth = try AuthManager(baseURL: url)
            self.auth = auth
            service = SessionService(client: try APIClient(baseURL: url, tokenProvider: auth))
        } catch { message = error.localizedDescription }
    }

    private func startDiscovery() {
        discovery = AppDiscoveryObserver { [weak self] apps in Task { @MainActor in self?.applyApps(apps) } }
        discovery?.refresh()
        microphones = InputDevices.list()
        let resolved = InputDevices.resolve(preferredUID: settings.lastMicrophoneUID)
        selectedMicrophoneUID = resolved.device?.uid
        if resolved.switchedToDefault { message = "前回のマイクが見つかりません。システム既定へ仮切替しました" }
    }

    private func applyApps(_ values: [RecordableApp]) {
        apps = values
        if selectedAppID == nil {
            selectedAppID = values.first(where: { $0.bundleID == settings.lastTarget?.bundleId })?.bundleID ?? values.first?.bundleID
        }
        if let selectedAppID, !values.contains(where: { $0.bundleID == selectedAppID }), !isRecording {
            self.selectedAppID = nil; selectedTabID = nil
        }
    }

    private func startBridge() {
        do { try bridge.start(); chromeState = .disconnected }
        catch { chromeState = .notInstalled; message = error.localizedDescription }
    }

    func refresh() async {
        reloadLocalSessions()
        guard let service else { return }
        busy = true; defer { busy = false }
        do {
            async let sessionResult = service.listSessions()
            async let formatsResult = service.formats()
            async let meResult = service.me()
            async let capsResult = service.capabilities()
            sessions = try await sessionResult.items
            formats = try await formatsResult
            me = try await meResult
            capabilities = try await capsResult
            passkeys = (try? await service.passkeys()) ?? []
            if me?.isOwner == true {
                async let claudeResult = service.claudeStatus()
                async let retentionResult = service.retention()
                claude = try? await claudeResult
                retention = try? await retentionResult
                adminUsers = (try? await service.users()) ?? []
                invitations = (try? await service.invitations()) ?? []
            }
            notifyStatusChanges(sessions)
            if pane == .sessions, let selectedID = selectedSession?.sessionId,
               let updated = sessions.first(where: { $0.sessionId == selectedID }) {
                await showSession(updated)
            }
            message = "最新状態を取得しました"
        } catch { message = error.localizedDescription }
    }

    func showSession(_ session: Session) async {
        selectedSession = session; pane = .sessions
        guard let service else { return }
        do {
            async let jobsResult = service.jobs(session.sessionId)
            jobs = try await jobsResult
            transcript = try? await service.transcript(session.sessionId)
            minutes = try? await service.currentMinutes(session.sessionId)
            versions = session.isSharedView == true ? nil : try? await service.minutesVersions(session.sessionId)
            shares = session.isSharedView == true ? [] : (try? await service.shares(session.sessionId)) ?? []
        } catch { message = error.localizedDescription }
    }

    func login() async {
        guard let auth else { return }
        do { _ = try await auth.login { url in await MainActor.run { _ = NSWorkspace.shared.open(url) } }; await refresh() }
        catch { message = error.localizedDescription }
    }

    func logout() async {
        guard let auth else { return }
        do { try await auth.logout(); me = nil; sessions = []; selectedSession = nil; passkeys = []; message = "ログアウトしました" }
        catch { message = error.localizedDescription }
    }

    func addPasskey() async {
        guard let service else { return }
        do {
            var registration = try await service.startPasskeyRegistration()
            passkeyRegistration = registration
            guard let raw = registration.passkeyUrl, service.isAllowedServerBrowserURL(raw), let url = URL(string: raw) else {
                throw AuthError.reauthFailed("パスキー登録URLのoriginがAPIと一致しません")
            }
            _ = NSWorkspace.shared.open(url)
            message = "ブラウザーで本人確認後、パスキーを追加してください"
            for _ in 0..<300 {
                if ["completed", "expired", "failed", "cancelled"].contains(registration.status) { break }
                try await Task.sleep(for: .seconds(1))
                var next = try await service.passkeyRegistrationStatus(registration.requestId)
                if next.passkeyUrl == nil { next.passkeyUrl = registration.passkeyUrl }
                registration = next
                passkeyRegistration = registration
            }
            switch registration.status {
            case "completed":
                passkeys = try await service.passkeys()
                me = try await service.me()
                message = "パスキーを追加しました"
            case "expired": message = "パスキー追加画面の有効期限が切れました"
            case "failed": message = "パスキーの追加に失敗しました"
            default: message = "パスキー追加の完了を確認できませんでした"
            }
        } catch is CancellationError {
            message = "パスキー追加の監視を終了しました"
        } catch { message = error.localizedDescription }
    }

    func deletePasskey(_ passkey: Passkey) async {
        guard let service else { return }
        do {
            try await service.deletePasskey(passkey.id)
            passkeys = try await service.passkeys()
            me = try await service.me()
            message = "パスキーを削除しました"
        } catch { message = error.localizedDescription }
    }

    func openPasskeyRegistrationURL() {
        guard let raw = passkeyRegistration?.passkeyUrl, let service,
              service.isAllowedServerBrowserURL(raw), let url = URL(string: raw) else {
            message = "有効なパスキー登録URLがありません"; return
        }
        _ = NSWorkspace.shared.open(url)
    }

    func copyPasskeyRegistrationURL() {
        guard let raw = passkeyRegistration?.passkeyUrl else { return }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(raw, forType: .string)
        message = "パスキー登録URLをコピーしました"
    }

    func saveSettings() {
        do {
            settings.defaultLanguageMode = languageMode
            settings.defaultFormatProfileId = selectedFormatID
            settings.lastMicrophoneUID = selectedMicrophoneUID
            settings.claudeSendDefault = allowExternalSend
            try settingsStore.save(settings)
            configureClient(); message = "設定を保存しました"
        } catch { message = error.localizedDescription }
    }

    func startRecording() async {
        guard let microphone = microphones.first(where: { $0.uid == selectedMicrophoneUID }),
              let selectedAppID, let app = apps.first(where: { $0.bundleID == selectedAppID }) else {
            message = "録音対象とマイクを選択してください"; return
        }
        let binding = await bindingForNewLocalSession()
        let micPermission = await MicrophoneCapture.requestPermission()
        let target: RecordingTarget
        if app.app == .chrome {
            let selection: ChromeAudioSelection = selectedTabID.map(ChromeAudioSelection.tab)
                ?? (chromeWholeApplicationExplicitlySelected ? .wholeApplication : .requiresExplicitSelection)
            let tabID: Int
            do {
                guard let validated = try selection.validated(availableTabIDs: Set(chromeTabs.map(\.tabId))) else {
                    target = .app(app)
                    return await startValidatedRecording(target: target, app: app, microphone: microphone,
                                                         micPermission: micPermission, binding: binding)
                }
                tabID = validated
            } catch { message = error.localizedDescription; return }
            guard chromeState == .connected, let tab = chromeTabs.first(where: { $0.tabId == tabID }) else {
                message = "指定したGoogle Meetタブとの接続を確認できません。録音元を再選択してください"; return
            }
            target = .chromeTab(bundleID: app.bundleID, tabID: tab.tabId, title: tab.title)
        } else { target = .app(app) }
        await startValidatedRecording(target: target, app: app, microphone: microphone,
                                      micPermission: micPermission, binding: binding)
    }

    private func startValidatedRecording(target: RecordingTarget, app: RecordableApp, microphone: InputDevice,
                                         micPermission: Bool, binding: LocalSessionBinding) async {
        let chromeTabID: Int? = if case .chromeTab(_, let tabID, _) = target { tabID } else { nil }
        let preconditions = RecordingPreconditions(
            microphonePermission: micPermission, systemAudioPermission: true, targetAvailable: true,
            microphoneAvailable: true, freeSpaceBytes: freeSpaceBytes,
            chromeBridgeConnectedIfNeeded: chromeTabID == nil || chromeState == .connected
        )
        guard preconditions.ok else { message = preconditions.failures.joined(separator: " / "); return }
        do {
            settings.lastMicrophoneUID = microphone.uid
            settings.lastTarget = .init(kind: chromeTabID == nil ? "app" : "chrome_tab", bundleId: app.bundleID,
                                        tabTitle: chromeTabID.flatMap { id in chromeTabs.first(where: { $0.tabId == id })?.title })
            try settingsStore.save(settings)
            liveUploadedChunks = 0
            liveFailedChunks = 0
            try recorder.start(target: target, microphone: microphone,
                               options: .init(title: title, languageMode: languageMode, allowExternalSend: allowExternalSend,
                                              formatProfileId: selectedFormatID,
                                              destinationOrigin: binding.destinationOrigin,
                                              ownerUserId: binding.ownerUserId),
                               chromeSink: chromeTabID == nil ? nil : bridge.sink)
            if let service, let sessionID = recorder.currentSessionID {
                let startedAt = recorder.currentStartedAt ?? Date()
                let entered = title.trimmingCharacters(in: .whitespacesAndNewlines)
                let liveTitle = entered.isEmpty ? RecordingCoordinator.provisionalTitle(target: target, date: startedAt) : entered
                let coordinator = LiveChunkUploadCoordinator(
                    service: service,
                    startRequest: LiveSessionStart(
                        sessionId: sessionID,
                        startedAt: startedAt,
                        title: liveTitle,
                        titleEditedByUser: !entered.isEmpty,
                        languageMode: languageMode,
                        allowExternalSend: allowExternalSend,
                        formatProfileId: selectedFormatID,
                        source: target.source
                    )
                ) { [weak self] progress in
                    Task { @MainActor in
                        self?.liveUploadedChunks = progress.uploaded
                        self?.liveFailedChunks = progress.failed
                    }
                }
                liveUploaders[sessionID] = coordinator
                recorder.onLiveChunkReady = { [weak coordinator] chunk in coordinator?.enqueue(chunk) }
                recorder.onLiveChunkFailure = { [weak self] _ in
                    Task { @MainActor in self?.liveFailedChunks += 1 }
                }
                coordinator.begin()
            }
            if case .chromeTab(_, let tabID, _) = target {
                do { _ = try bridge.requestCapture(tabID: tabID) }
                catch {
                    let state = try recorder.stop()
                    elapsedTimer?.invalidate()
                    message = "Chromeタブ取得を開始できなかったため停止し、取得済み音声を保存しました: \(error.localizedDescription)"
                    Task { await upload(state) }
                    return
                }
            }
            startElapsedTimer(); message = "録音中: \(target.displayName)"
        } catch { message = error.localizedDescription }
    }

    func selectChromeTab(_ tabID: Int?) {
        let selection = Self.chromeSelection(tabID: tabID)
        selectedTabID = selection.tabID
        chromeWholeApplicationExplicitlySelected = selection.wholeApplicationExplicitlySelected
    }

    func selectChromeWholeApplication() {
        selectChromeTab(nil)
    }

    static func chromeSelection(tabID: Int?) -> (tabID: Int?, wholeApplicationExplicitlySelected: Bool) {
        (tabID, tabID == nil)
    }

    static func restoredChromeWholeApplicationSelection(_ target: ClientSettings.LastTarget?) -> Bool {
        target?.kind == "app" && target?.bundleId == "com.google.Chrome"
    }

    static func localSessionBinding(settings: ClientSettings, currentUser: Me?) -> LocalSessionBinding {
        LocalSessionBinding.capture(apiURL: settings.apiURL, currentUser: currentUser)
    }

    private func bindingForNewLocalSession() async -> LocalSessionBinding {
        let apiURL = service?.client.baseURL ?? settings.apiURL
        if let me { return LocalSessionBinding.capture(apiURL: apiURL, currentUser: me) }
        guard let service else { return LocalSessionBinding.capture(apiURL: apiURL, currentUser: nil) }
        do {
            let current = try await service.me()
            me = current
            return LocalSessionBinding.capture(apiURL: apiURL, currentUser: current)
        } catch {
            return LocalSessionBinding.capture(apiURL: apiURL, currentUser: nil)
        }
    }

    static func filteredChromeTabs(_ tabs: [BridgeTab], query: String) -> [BridgeTab] {
        let term = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !term.isEmpty else { return tabs }
        return tabs.filter { $0.title.localizedCaseInsensitiveContains(term) }
    }

    @discardableResult
    func stopRecording(automatic: Bool = false) -> Bool {
        let stopBridge: (() throws -> Void)? = if case .chromeTab = recorder.currentTarget {
            { [bridge] in try bridge.stopCaptureAndWait() }
        } else { nil }
        let outcome = RecordingStopSequence.run(stopBridge: stopBridge) { try recorder.stop() }
        elapsedTimer?.invalidate()
        reloadLocalSessions()
        guard let state = outcome.state else {
            let bridgeDetail = outcome.bridgeError.map { "Chrome停止確認: \($0.localizedDescription) / " } ?? ""
            message = "\(automatic ? "4時間の上限で" : "")停止を試みましたが、録音の確定に失敗しました。Finderから回収できます: \(bridgeDetail)\(outcome.recorderError?.localizedDescription ?? "詳細なし")"
            return false
        }
        let prefix = automatic ? "4時間の上限に達したため自動停止し、録音を保存しました" : "録音を保存しました"
        if let warning = outcome.bridgeError {
            message = "\(prefix)。Chrome側の停止確認には失敗しましたが、ローカル音声を確定してアップロードします: \(warning.localizedDescription)"
        } else {
            message = "\(prefix)。アップロードを開始します"
        }
        Task { await upload(state) }
        return true
    }

    func importAudio(_ url: URL) async {
        guard let service else { message = "サービス設定を確認してください"; return }
        busy = true; defer { busy = false }
        do {
            let binding = await bindingForNewLocalSession()
            let request = ImportRequest(fileURL: url, title: title, languageMode: languageMode,
                                        allowExternalSend: allowExternalSend, formatProfileId: selectedFormatID,
                                        destinationOrigin: binding.destinationOrigin, ownerUserId: binding.ownerUserId)
            let result = try ImportService(store: localStore).run(request)
            let session = try await localStore.uploadPending(result.state, service: service)
            message = "取り込みを受け付けました: \(session.title)"; await refresh()
        } catch { message = error.localizedDescription }
    }

    private func upload(_ state: LocalSessionState) async {
        guard let service else { return }
        do {
            recorder.onLiveChunkReady = nil
            recorder.onLiveChunkFailure = nil
            if let live = liveUploaders.removeValue(forKey: state.package.sessionId) {
                let progress = await live.finish()
                liveUploadedChunks = progress.uploaded
                liveFailedChunks = progress.failed
            }
            try recorder.markUploading(); _ = try await localStore.uploadPending(state, service: service)
            try recorder.markUploaded(); reloadLocalSessions(); await refresh()
        } catch {
            recorder.markUploadFailed(); reloadLocalSessions()
            message = "アップロードを再開できます: \(error.localizedDescription)"
        }
    }

    func resumePending() async {
        guard let service else { return }
        for state in (try? localStore.pendingUploads()) ?? [] {
            do { _ = try await localStore.uploadPending(state, service: service) }
            catch { message = "未完了アップロードを保持しています: \(error.localizedDescription)" }
        }
        await refresh()
    }

    func resumeLocal(_ state: LocalSessionState) async {
        guard let service else { message = "サービス設定を確認してください"; return }
        do {
            _ = try await localStore.uploadPending(state, service: service)
            message = "アップロードを再開しました: \(state.package.title ?? state.package.sessionId.uuidString)"
            reloadLocalSessions(); await refresh()
        } catch {
            message = "未完了アップロードを保持しています: \(error.localizedDescription)"
            reloadLocalSessions()
        }
    }

    func cancelLocal(_ state: LocalSessionState) async {
        guard let service else { message = "サービス設定を確認してください"; return }
        do {
            try await localStore.cancelPending(state, service: service)
            message = "未完了アップロードとローカル録音を削除しました"
            reloadLocalSessions(); await refresh()
        } catch {
            message = "中止を完了できなかったためローカル録音を保持しています: \(error.localizedDescription)"
            reloadLocalSessions()
        }
    }

    func revealLocalSession(_ id: UUID) {
        NSWorkspace.shared.activateFileViewerSelecting([localStore.recordingDirectory(for: id)])
    }

    private func reloadLocalSessions() {
        localPendingUploads = (try? localStore.pendingUploads()) ?? []
        localRecoveries = (try? localStore.recoveries()) ?? []
    }

    func updateTitle(_ value: String) async {
        guard let service, let selectedSession else { return }
        do { let updated = try await service.update(selectedSession.sessionId, .init(title: value)); await showSession(updated); await refresh() }
        catch { message = error.localizedDescription }
    }

    func retry(stage: String) async {
        guard let service, let selectedSession else { return }
        do { _ = try await service.retry(selectedSession.sessionId, stage: stage, languageMode: stage == "transcription" ? languageMode : nil); await refresh() }
        catch { message = error.localizedDescription }
    }

    func cancel(_ job: JobSummary) async {
        guard let service, let selectedSession else { return }
        do { _ = try await service.cancelJob(selectedSession.sessionId, jobID: job.jobId); await showSession(selectedSession) }
        catch { message = error.localizedDescription }
    }

    func share(email: String) async {
        guard let service, let selectedSession else { return }
        do { _ = try await service.share(selectedSession.sessionId, email: email); await showSession(selectedSession) }
        catch { message = error.localizedDescription }
    }

    func unshare(_ entry: ShareEntry) async {
        guard let service, let selectedSession else { return }
        do { try await service.unshare(selectedSession.sessionId, userID: entry.userId); await showSession(selectedSession) }
        catch { message = error.localizedDescription }
    }

    func saveMinutes(body: String) async {
        guard let service, let selectedSession, let parent = versions?.currentMinutesVersionId else { return }
        do {
            _ = try await service.saveManualEdit(selectedSession.sessionId, .init(parentVersionId: parent, bodyMarkdown: body, expectedCurrentVersionId: parent))
            await showSession(selectedSession); message = "手動編集版を保存しました"
        } catch { message = error.localizedDescription }
    }

    func regenerate(instructions: String, base: UUID?, format: UUID?) async {
        guard let service, let selectedSession else { return }
        do {
            _ = try await service.regenerate(selectedSession.sessionId, .init(baseVersionId: base, instructions: instructions,
                                                                              formatProfileId: format, useSnapshot: format == nil))
            await refresh(); message = "Claude再生成を受け付けました。現在版は成功まで維持されます"
        } catch { message = error.localizedDescription }
    }

    func selectVersion(_ id: UUID) async {
        guard let service, let selectedSession else { return }
        do { _ = try await service.selectCurrent(selectedSession.sessionId, versionID: id); await showSession(selectedSession) }
        catch { message = error.localizedDescription }
    }

    func restoreVersion(_ id: UUID) async {
        guard let service, let selectedSession else { return }
        do { _ = try await service.restore(selectedSession.sessionId, versionID: id); await showSession(selectedSession) }
        catch { message = error.localizedDescription }
    }

    func compareVersions(_ from: UUID, _ to: UUID) async {
        guard let service, let selectedSession else { return }
        do { comparison = try await service.compare(selectedSession.sessionId, from: from, to: to).diff }
        catch { message = error.localizedDescription }
    }

    func exportSelected(to directory: URL, includeAudio: Bool) async {
        guard let service, let session = selectedSession else { return }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try ContractCoding.encoder(pretty: true).encode(session).write(to: directory.appendingPathComponent("session.json"), options: .atomic)
            if let transcript = try await optionalArtifact({ try await service.transcript(session.sessionId) }) {
                try ContractCoding.encoder(pretty: true).encode(transcript).write(to: directory.appendingPathComponent("transcript.json"), options: .atomic)
            }
            if let markdown = try await optionalArtifact({ try await service.transcriptMarkdown(session.sessionId) }) { try Data(markdown.utf8).write(to: directory.appendingPathComponent("transcript.md"), options: .atomic) }
            if let minutes = try await optionalArtifact({ try await service.currentMinutes(session.sessionId) }) { try Data(minutes.bodyMarkdown.utf8).write(to: directory.appendingPathComponent("minutes.md"), options: .atomic) }
            if includeAudio {
                for track in session.tracks where track.artifactId != nil {
                    let response = try await service.audio(session.sessionId, trackID: track.trackId)
                    let ext = SessionService.audioFileExtension(response)
                    try response.body.write(to: directory.appendingPathComponent("\(track.trackId).\(ext)"), options: .atomic)
                }
            }
            message = "書き出しました: \(directory.path)"
        } catch { message = "書き出しに失敗しました（未生成の成果物は省略しますが、通信・保存エラーは中断します）: \(error.localizedDescription)" }
    }

    private func optionalArtifact<T>(_ operation: () async throws -> T) async throws -> T? {
        do { return try await operation() }
        catch APIClientError.api(_, let status) where status == 404 { return nil }
    }

    func loadAudio(trackID: String) async {
        guard let service, let session = selectedSession,
              session.tracks.contains(where: { $0.trackId == trackID }) else { return }
        do {
            let response = try await service.audio(session.sessionId, trackID: trackID)
            let ext = SessionService.audioFileExtension(response)
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("audio-minutes-\(session.sessionId)-\(trackID).\(ext)")
            try response.body.write(to: url, options: .atomic)
            audioPlaybackURL = url
        } catch { message = "音声を読み込めません: \(error.localizedDescription)" }
    }

    func createFormat(_ input: FormatProfileInput) async {
        guard let service else { return }
        do { _ = try await service.createFormat(input); formats = try await service.formats() }
        catch { message = error.localizedDescription }
    }

    func updateFormat(_ id: UUID, _ input: FormatProfileInput) async {
        guard let service else { return }
        do { _ = try await service.updateFormat(id, input); formats = try await service.formats() }
        catch { message = error.localizedDescription }
    }

    func duplicateFormat(_ id: UUID) async {
        guard let service else { return }
        do { _ = try await service.duplicateFormat(id); formats = try await service.formats() }
        catch { message = error.localizedDescription }
    }

    func deleteFormat(_ id: UUID) async {
        guard let service else { return }
        do { try await service.deleteFormat(id); formats = try await service.formats() }
        catch { message = error.localizedDescription }
    }

    func setDefaultFormat(_ id: UUID) async {
        guard let service else { return }
        do { _ = try await service.setDefaultFormat(id); formats = try await service.formats(); selectedFormatID = id }
        catch { message = error.localizedDescription }
    }

    func previewFormat(_ input: FormatProfileInput) async -> String {
        guard let service else { return "" }
        do { return try await service.previewFormat(input).markdown }
        catch { message = error.localizedDescription; return "" }
    }

    func claudeLogin() async {
        guard let service else { return }
        do {
            var login = try await service.claudeLogin()
            claudeLoginSession = login
            message = "Claudeログインを開始しました。URLが準備できるまで状態を更新します"
            var announcedURL = false
            for _ in 0..<360 {
                var next = try await service.claudeLoginStatus(login.authSessionId)
                if next.url == nil { next.url = login.url }
                login = next
                claudeLoginSession = login
                if login.state == "url_ready", let raw = login.url, !announcedURL {
                    guard SessionService.isAllowedClaudeAuthURL(raw) else {
                        _ = try? await service.claudeCancelLogin(login.authSessionId)
                        throw AuthError.reauthFailed("Claude認証URLのoriginが許可されていません")
                    }
                    announcedURL = true
                    message = "Claude認証URLを表示しました。本人が「認証画面を開く」または「URLをコピー」を選んでください"
                }
                if ["completed", "failed", "cancelled", "expired"].contains(login.state) { break }
                try await Task.sleep(for: .seconds(1))
            }
            if !["completed", "failed", "cancelled", "expired"].contains(login.state) {
                login = try await service.claudeCancelLogin(login.authSessionId)
                claudeLoginSession = login
            }
            switch login.state {
            case "completed": message = "Claudeにログインしました"
            case "cancelled": message = "Claudeログインを取り消しました"
            case "expired": message = "Claudeログインの有効期限が切れました"
            case "failed": message = "Claudeログインに失敗しました: \(login.failureCode ?? "詳細なし")"
            default: message = "Claudeログインの完了を確認できませんでした"
            }
            claude = try await service.claudeStatus()
        } catch is CancellationError {
            if let login = claudeLoginSession { _ = try? await service.claudeCancelLogin(login.authSessionId) }
            message = "Claudeログインを取り消しました"
        } catch { message = error.localizedDescription }
    }

    var claudeAuthURL: String? {
        guard let raw = claudeLoginSession?.url, SessionService.isAllowedClaudeAuthURL(raw) else { return nil }
        return raw
    }

    func openClaudeAuthURL() {
        guard let raw = claudeAuthURL, let url = URL(string: raw) else {
            message = "有効なClaude認証URLがありません"; return
        }
        _ = NSWorkspace.shared.open(url)
    }

    func copyClaudeAuthURL() {
        guard let raw = claudeAuthURL else { return }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(raw, forType: .string)
        message = "Claude認証URLをコピーしました"
    }

    func submitClaudeLoginCode(_ code: String) async {
        guard let service, let login = claudeLoginSession else { return }
        do { claudeLoginSession = try await service.claudeSubmitLoginCode(login.authSessionId, code: code); message = "認証コードをterminalへ渡しました" }
        catch { message = error.localizedDescription }
    }

    func cancelClaudeLogin() async {
        guard let service, let login = claudeLoginSession else { return }
        do { claudeLoginSession = try await service.claudeCancelLogin(login.authSessionId); message = "Claudeログインを取り消しました" }
        catch { message = error.localizedDescription }
    }

    func claudeLogout() async {
        guard let service else { return }
        do { claude = try await service.claudeLogout() }
        catch { message = error.localizedDescription }
    }

    func updateRetention(upload: Int, audio: Int, log: Int) async {
        guard let service else { return }
        do { retention = try await service.updateRetention(.init(uploadHours: upload, audioDays: audio, logDays: log)) }
        catch { message = error.localizedDescription }
    }

    func invite(email: String, role: String) async {
        guard let service else { return }
        do { let invitation = try await service.invite(email: email, role: role); invitations = try await service.invitations(); message = invitation.url ?? "招待を発行しました" }
        catch { message = error.localizedDescription }
    }

    func revokeInvitation(_ invitation: Invitation) async {
        guard let service else { return }
        do { try await service.revokeInvitation(invitation.id); invitations = try await service.invitations(); message = "招待を失効しました" }
        catch { message = error.localizedDescription }
    }

    func updateUser(_ user: AdminUser, role: String? = nil, disabled: Bool? = nil) async {
        guard let service else { return }
        do { _ = try await service.updateUser(user.id, .init(role: role, disabled: disabled)); adminUsers = try await service.users() }
        catch { message = error.localizedDescription }
    }

    func reauthenticate() async {
        guard let auth, let service else { return }
        do {
            _ = try await auth.reauthenticateInBrowser { url in await MainActor.run { _ = NSWorkspace.shared.open(url) } }
            me = try await service.me(); message = "再認証しました"
        }
        catch { message = error.localizedDescription }
    }

    func openNotificationSession(identifier: String) async {
        guard let id = UUID(uuidString: identifier) else { return }
        if let session = sessions.first(where: { $0.sessionId == id }) { await showSession(session) }
        else { await refresh(); if let session = sessions.first(where: { $0.sessionId == id }) { await showSession(session) } }
        NSApp.activate(ignoringOtherApps: true)
    }

    func deleteSelected() async {
        guard let service, let selectedSession else { return }
        do { try await service.delete(selectedSession.sessionId); self.selectedSession = nil; await refresh() }
        catch { message = error.localizedDescription }
    }

    private func startElapsedTimer() {
        elapsedTimer?.invalidate()
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.elapsed = self.recorder.elapsed
                if RecordingCoordinator.hasReachedMaximumDuration(self.elapsed), self.recorder.state == .recording {
                    _ = self.stopRecording(automatic: true)
                }
            }
        }
    }

    private func resumeAndMonitor() {
        Task { await resumePending() }
        pollingTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3)); await self?.refresh()
            }
        }
    }

    private func notifyStatusChanges(_ values: [Session]) {
        for session in values {
            defer { lastStatuses[session.sessionId] = session.status }
            guard lastStatuses[session.sessionId] != nil, lastStatuses[session.sessionId] != session.status else { continue }
            let enabled = session.status == .completed ? settings.notifications.minutesCompleted : session.status == .failed ? settings.notifications.processingFailed : false
            guard enabled else { continue }
            let content = UNMutableNotificationContent(); content.title = session.title; content.body = session.status.label
            UNUserNotificationCenter.current().add(.init(identifier: session.sessionId.uuidString, content: content, trigger: nil))
        }
    }

    func requestNotifications() async { _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) }

    func quit() { NSApp.terminate(nil) }
    deinit { pollingTask?.cancel(); elapsedTimer?.invalidate(); bridge.stop() }
}
