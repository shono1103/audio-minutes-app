import AppKit
import AVKit
import ClientCore
import SwiftUI

struct SessionsView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        HSplitView {
            List(model.sessions, selection: Binding(get: { model.selectedSession?.id }, set: { id in
                if let session = model.sessions.first(where: { $0.id == id }) { Task { await model.showSession(session) } }
            })) { session in
                VStack(alignment: .leading, spacing: 4) {
                    Text(session.title).font(.headline).lineLimit(1)
                    Label(session.status.label, systemImage: icon(session.status)).font(.caption).foregroundStyle(session.status == .failed ? .red : .secondary)
                    if session.isSharedView == true { Text("共有されたセッション").font(.caption2) }
                }
                .tag(session.id)
                .accessibilityElement(children: .combine)
            }
            .frame(minWidth: 240, idealWidth: 280, maxWidth: 340)
            if model.selectedSession != nil {
                SessionDetailView(model: model)
                    .frame(minWidth: 560, maxWidth: .infinity, maxHeight: .infinity)
                    .layoutPriority(1)
            }
            else { ContentUnavailableView("セッションを選択", systemImage: "doc.text.magnifyingglass") }
        }
        .navigationTitle("セッション")
    }

    private func icon(_ status: SessionStatus) -> String {
        switch status { case .completed, .transcribed: return "checkmark.circle"; case .failed: return "exclamationmark.triangle"; case .uploading, .validating, .queued, .queuedMinutes: return "clock"; default: return "gearshape.2" }
    }
}

private struct SessionDetailView: View {
    @ObservedObject var model: AppModel
    @State private var editedTitle = ""
    @State private var editedMinutes = ""
    @State private var instructions = ""
    @State private var shareEmail = ""
    @State private var fromVersion: UUID?
    @State private var toVersion: UUID?
    @State private var showDelete = false
    @State private var regenerationBase: UUID?
    @State private var regenerationFormat: UUID?

    var body: some View {
        TabView {
            overview.tabItem { Label("概要", systemImage: "info.circle") }
            minutes.tabItem { Label("議事録", systemImage: "doc.text") }
            transcript.tabItem { Label("文字起こし", systemImage: "captions.bubble") }
            audio.tabItem { Label("音声", systemImage: "waveform") }
            processing.tabItem { Label("処理", systemImage: "gearshape.2") }
            versions.tabItem { Label("版", systemImage: "clock.arrow.circlepath") }
            sharing.tabItem { Label("共有", systemImage: "person.2") }
        }
        .padding()
        .onChange(of: model.selectedSession?.id, initial: true) { _, _ in
            editedTitle = model.selectedSession?.title ?? ""
            editedMinutes = model.minutes?.bodyMarkdown ?? ""
            regenerationBase = model.versions?.currentMinutesVersionId
        }
        .onChange(of: model.minutes?.bodyMarkdown) { _, value in editedMinutes = value ?? "" }
    }

    private var overview: some View {
        Form {
            if let session = model.selectedSession {
                TextField("タイトル", text: $editedTitle)
                    .disabled(session.isSharedView == true)
                Button("タイトルを保存") { Task { await model.updateTitle(editedTitle) } }.disabled(session.isSharedView == true || editedTitle.isEmpty)
                LabeledContent("状態", value: session.status.label)
                LabeledContent("入力", value: session.inputKind == .importedMixed ? "取り込み音声（音源分離なし）" : "アプリ音声＋マイク")
                LabeledContent("言語", value: session.languageMode.label)
                LabeledContent("Claude送信", value: session.allowExternalSend ? "送信する" : "外部送信禁止")
                LabeledContent("音声", value: session.audioRetained ? "保持中" : "削除済み")
                if let failure = session.failure { Text("\(failure.stage): \(failure.message)").foregroundStyle(.red) }
                HStack {
                    Button("書き出し") { export(includeAudio: false) }
                    Button("音声も書き出し") { export(includeAudio: true) }.disabled(!session.audioRetained)
                    Spacer()
                    Button("削除", role: .destructive) { showDelete = true }.disabled(session.isSharedView == true)
                }
                .confirmationDialog("サーバー上の音声と成果物を削除します", isPresented: $showDelete) {
                    Button("削除", role: .destructive) { Task { await model.deleteSelected() } }
                }
            }
        }.formStyle(.grouped)
    }

    private var minutes: some View {
        VStack(alignment: .leading) {
            if model.selectedSession?.isSharedView == true {
                ScrollView { Text(model.minutes?.bodyMarkdown ?? "議事録はまだありません").textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
            } else {
                TextEditor(text: $editedMinutes).font(.body.monospaced()).accessibilityLabel("議事録本文")
                HStack {
                    Button("手動編集版として保存") { Task { await model.saveMinutes(body: editedMinutes) } }.disabled(model.versions?.currentMinutesVersionId == nil)
                    TextField("Claudeへの修正指示", text: $instructions)
                }
                HStack {
                    Picker("元版", selection: $regenerationBase) {
                        Text("現在版").tag(UUID?.none)
                        ForEach(model.versions?.items ?? []) { Text("v\($0.versionNumber)").tag(UUID?.some($0.versionId)) }
                    }
                    Picker("フォーマット", selection: $regenerationFormat) {
                        Text("録音時スナップショット").tag(UUID?.none)
                        ForEach(model.formats) { Text($0.name).tag(UUID?.some($0.profileId)) }
                    }
                    Button("再生成") { Task { await model.regenerate(instructions: instructions, base: regenerationBase ?? model.versions?.currentMinutesVersionId, format: regenerationFormat) } }
                        .disabled(model.selectedSession?.allowExternalSend != true)
                }
            }
        }
    }

    private var transcript: some View {
        List {
            if let review = model.transcript?.unresolvedReview, !review.isEmpty {
                Section("要確認区間") {
                    ForEach(review) { item in Label("\(time(item.startMs))–\(time(item.endMs)) \(item.reason)", systemImage: "exclamationmark.waveform") }
                }
            }
            Section("文字起こし（読み取り専用）") {
                ForEach(model.transcript?.segments ?? []) { segment in
                    VStack(alignment: .leading) {
                        Text("\(time(segment.startMs))  \(segment.source.rawValue) / \(segment.language)").font(.caption).foregroundStyle(.secondary)
                        Text(segment.text).textSelection(.enabled)
                    }
                }
            }
        }
    }

    private var audio: some View {
        VStack(alignment: .leading, spacing: 12) {
            if model.selectedSession?.audioRetained == true {
                ForEach(model.selectedSession?.tracks ?? [], id: \.trackId) { track in
                    Button("\(track.role.rawValue) を読み込む") { Task { await model.loadAudio(trackID: track.trackId) } }
                }
                if let url = model.audioPlaybackURL {
                    AudioPlaybackView(url: url).frame(minHeight: 100)
                    Text("再生コントロールのタイムラインで任意位置へシークできます。").font(.caption).foregroundStyle(.secondary)
                }
            } else {
                ContentUnavailableView("音声は削除済みです", systemImage: "waveform.slash")
            }
        }
    }

    private var processing: some View {
        List(model.jobs) { job in
            HStack {
                VStack(alignment: .leading) { Text(job.kind); Text("\(job.status) / attempt \(job.attempt)").font(.caption) }
                Spacer()
                if !["succeeded", "failed", "cancelled"].contains(job.status) { Button("取消") { Task { await model.cancel(job) } } }
            }
        }
        .safeAreaInset(edge: .bottom) {
            HStack { Button("文字起こしを再実行") { Task { await model.retry(stage: "transcription") } }; Button("議事録だけ再実行") { Task { await model.retry(stage: "minutes") } } }.padding()
        }
    }

    private var versions: some View {
        VStack {
            List(model.versions?.items ?? []) { version in
                HStack {
                    VStack(alignment: .leading) { Text("v\(version.versionNumber) — \(version.kindLabel)"); Text(version.createdAt.formatted()).font(.caption) }
                    Spacer()
                    Button("現在版") { Task { await model.selectVersion(version.versionId) } }
                    Button("復元版を作る") { Task { await model.restoreVersion(version.versionId) } }
                }
            }
            HStack {
                Picker("比較元", selection: $fromVersion) { Text("選択").tag(UUID?.none); ForEach(model.versions?.items ?? []) { Text("v\($0.versionNumber)").tag(UUID?.some($0.versionId)) } }
                Picker("比較先", selection: $toVersion) { Text("選択").tag(UUID?.none); ForEach(model.versions?.items ?? []) { Text("v\($0.versionNumber)").tag(UUID?.some($0.versionId)) } }
                Button("比較") { if let fromVersion, let toVersion { Task { await model.compareVersions(fromVersion, toVersion) } } }
            }
            if !model.comparison.isEmpty { ScrollView { Text(model.comparison).font(.body.monospaced()).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }.frame(maxHeight: 180) }
        }
    }

    private var sharing: some View {
        VStack {
            List(model.shares) { entry in
                HStack { Text(entry.email ?? entry.userId.uuidString); Spacer(); Button("解除") { Task { await model.unshare(entry) } } }
            }
            HStack { TextField("登録済みユーザーのメール", text: $shareEmail); Button("閲覧共有") { Task { await model.share(email: shareEmail); shareEmail = "" } }.disabled(shareEmail.isEmpty) }
        }.disabled(model.selectedSession?.isSharedView == true)
    }

    private func export(includeAudio: Bool) {
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.canCreateDirectories = true
        if panel.runModal() == .OK, let url = panel.url { Task { await model.exportSelected(to: url, includeAudio: includeAudio) } }
    }
    private func time(_ milliseconds: Int) -> String { String(format: "%02d:%02d", milliseconds / 60_000, milliseconds / 1_000 % 60) }
}

private struct AudioPlaybackView: View {
    @State private var player: AVPlayer

    init(url: URL) { _player = State(initialValue: AVPlayer(url: url)) }

    var body: some View {
        VideoPlayer(player: player)
            .onDisappear { player.pause() }
    }
}
