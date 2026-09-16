import ClientCore
import SwiftUI
import UniformTypeIdentifiers

struct RecordingView: View {
    @ObservedObject var model: AppModel
    @State private var showingImporter = false
    @State private var chromeTabQuery = ""

    var body: some View {
        ScrollView {
            Form {
                Section("録音対象") {
                    Picker("対応アプリ", selection: $model.selectedAppID) {
                        Text("選択してください").tag(String?.none)
                        ForEach(model.apps) { app in Text(app.displayName).tag(String?.some(app.bundleID)) }
                    }
                    .disabled(model.isRecording)
                    if model.selectedAppID == "com.google.Chrome" {
                        Button {
                            model.selectChromeWholeApplication()
                        } label: {
                            HStack {
                                Image(systemName: model.chromeWholeApplicationExplicitlySelected ? "largecircle.fill.circle" : "circle")
                                Text("Chromeアプリ全体")
                                Spacer()
                                Text("拡張接続は不要").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .buttonStyle(.plain)
                        .disabled(model.isRecording)

                        Text("Chrome全体は、前回のMeetタブ選択とは別に毎回明示して切り替えられます。")
                            .font(.caption).foregroundStyle(.secondary)

                        DisclosureGroup("Google Meet タブ — \(model.chromeState.rawValue)") {
                            TextField("タブを検索", text: $chromeTabQuery)
                            let tabs = AppModel.filteredChromeTabs(model.chromeTabs, query: chromeTabQuery)
                            if tabs.isEmpty {
                                Text("一致するGoogle Meetタブはありません").foregroundStyle(.secondary)
                            } else {
                                ForEach(tabs) { tab in
                                    Button {
                                        model.selectChromeTab(tab.tabId)
                                    } label: {
                                        HStack {
                                            ChromeFavicon(urlString: tab.faviconUrl)
                                            Text(tab.title).lineLimit(1)
                                            Spacer()
                                            Image(systemName: tab.audible ? "speaker.wave.2" : "speaker.slash")
                                                .foregroundStyle(.secondary)
                                            if model.selectedTabID == tab.tabId {
                                                Image(systemName: "checkmark")
                                            }
                                        }
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                        }
                        .disabled(model.chromeState != .connected || model.isRecording)
                        .accessibilityHint("接続済みの場合だけmeet.google.comのタブを選択できます")
                        if model.chromeState != .connected {
                            Text("Meetタブ選択には拡張とNativeBridgeの接続が必要です。Chromeアプリ全体は上で選択できます。")
                                .foregroundStyle(.secondary)
                        }
                    }
                    Picker("マイク", selection: $model.selectedMicrophoneUID) {
                        ForEach(model.microphones) { mic in Text("\(mic.name)\(mic.isDefault ? "（既定）" : "")").tag(String?.some(mic.uid)) }
                    }.disabled(model.isRecording)
                }

                Section("処理設定") {
                    TextField("タイトル（空欄なら仮タイトル）", text: $model.title).disabled(model.isRecording)
                    Picker("言語", selection: $model.languageMode) { ForEach(LanguageMode.allCases) { Text($0.label).tag($0) } }.disabled(model.isRecording)
                    Picker("議事録フォーマット", selection: $model.selectedFormatID) {
                        Text("既定").tag(UUID?.none)
                        ForEach(model.formats) { Text("\($0.name) v\($0.version)").tag(UUID?.some($0.profileId)) }
                    }.disabled(model.isRecording)
                    Toggle("文字起こしをClaudeへ送信して議事録を生成する", isOn: $model.allowExternalSend).disabled(model.isRecording)
                    Text(model.allowExternalSend ? "文字起こし本文をAnthropicへ送信します" : "外部送信禁止: 文字起こし完了で正常停止します")
                        .font(.caption).foregroundStyle(.secondary)
                    LabeledContent("処理先", value: model.settings.apiBaseUrl)
                    LabeledContent("空き容量", value: ByteCountFormatter.string(fromByteCount: model.freeSpaceBytes, countStyle: .file))
                }

                Section("入力レベル") {
                    LabeledContent("アプリ音声") { ProgressView(value: Double(model.appLevel), total: 1).progressViewStyle(.linear).frame(width: 240) }
                    LabeledContent("マイク") { ProgressView(value: Double(model.micLevel), total: 1).progressViewStyle(.linear).frame(width: 240) }
                    if model.isRecording {
                        Label("録音中 \(duration(model.elapsed)) — 入力元と処理先は固定されています", systemImage: "record.circle.fill")
                            .foregroundStyle(.red).accessibilityLabel("録音中、経過時間 \(duration(model.elapsed))")
                        if model.liveFailedChunks > 0 {
                            Label("先行文字起こしを利用できない区間があります。停止後に完全音声で処理します", systemImage: "exclamationmark.arrow.triangle.2.circlepath")
                                .font(.caption).foregroundStyle(.orange)
                        } else if model.liveUploadedChunks > 0 {
                            Label("文字起こしを先行処理中 — 送信済み \(model.liveUploadedChunks)チャンク", systemImage: "waveform.badge.magnifyingglass")
                                .font(.caption).foregroundStyle(.secondary)
                        } else {
                            Label("文字起こし準備中", systemImage: "clock")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }

                Section {
                    HStack {
                        if model.isRecording {
                            Button("停止して保存") { _ = model.stopRecording() }.keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent).tint(.red)
                        } else {
                            Button("録音開始") { Task { await model.startRecording() } }.keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent)
                            Button("音声ファイルを取り込む") { showingImporter = true }
                        }
                    }
                    Text("WAV / M4A(AAC) / MP3 / FLAC、最大2 GiB・4時間。元ファイルは変更しません。ここへドラッグ&ドロップもできます。")
                        .font(.caption).foregroundStyle(.secondary)
                }

                if !model.localPendingUploads.isEmpty || !model.localRecoveries.isEmpty {
                    Section("このMacに残っている録音") {
                        ForEach(model.localPendingUploads, id: \.package.sessionId) { state in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(state.package.title ?? state.package.sessionId.uuidString).font(.headline)
                                Text("未完了アップロード — \(state.package.startedAt.formatted())")
                                    .font(.caption).foregroundStyle(.secondary)
                                if let error = state.lastError { Text(error).font(.caption).foregroundStyle(.red) }
                                HStack {
                                    Button("アップロード再開") { Task { await model.resumeLocal(state) } }
                                    Button("中止して削除", role: .destructive) { Task { await model.cancelLocal(state) } }
                                    Button("Finderで回収") { model.revealLocalSession(state.package.sessionId) }
                                }
                            }
                        }
                        ForEach(model.localRecoveries) { recovery in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(recovery.title).font(.headline)
                                Text("録音確定エラー — \(recovery.startedAt.formatted())")
                                    .font(.caption).foregroundStyle(.red)
                                ForEach(recovery.tracks) { track in
                                    Text("\(track.role.rawValue): \(track.headerFinalized ? "WAV確定済み" : "WAV未確定")\(track.error.map { " — \($0)" } ?? "")")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Text("不完全な録音は自動送信しません。Finderから音声を回収してください。")
                                    .font(.caption).foregroundStyle(.secondary)
                                Button("Finderで回収") { model.revealLocalSession(recovery.sessionId) }
                            }
                        }
                    }
                }
            }
            .formStyle(.grouped)
        }
        .navigationTitle("録音・取り込み")
        .fileImporter(isPresented: $showingImporter, allowedContentTypes: [.audio], allowsMultipleSelection: false) { result in
            if case .success(let urls) = result, let url = urls.first { Task { await model.importAudio(url) } }
        }
        .dropDestination(for: URL.self) { urls, _ in
            guard let url = urls.first else { return false }
            Task { await model.importAudio(url) }; return true
        }
    }

    private func duration(_ value: TimeInterval) -> String {
        let seconds = Int(value); return String(format: "%02d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
    }
}

private struct ChromeFavicon: View {
    let urlString: String?

    var body: some View {
        if let urlString, let url = URL(string: urlString), ["https", "http", "data"].contains(url.scheme?.lowercased() ?? "") {
            AsyncImage(url: url) { image in
                image.resizable().scaledToFit()
            } placeholder: {
                Image(systemName: "globe")
            }
            .frame(width: 16, height: 16)
        } else {
            Image(systemName: "globe").frame(width: 16, height: 16)
        }
    }
}
