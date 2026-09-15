import AppKit
import SwiftUI

struct RootView: View {
    @ObservedObject var model: AppModel
    @AppStorage("onboarding_completed") private var onboardingCompleted = false
    @AppStorage("appearance") private var appearance = "system"

    var body: some View {
        NavigationSplitView {
            List(AppModel.Pane.allCases, selection: $model.pane) { pane in
                Label(pane.rawValue, systemImage: icon(pane)).tag(pane)
            }
            .navigationTitle("audio-minutes")
            .accessibilityLabel("主要画面")
        } detail: {
            VStack(spacing: 0) {
                Group {
                    switch model.pane {
                    case .recording: RecordingView(model: model)
                    case .sessions: SessionsView(model: model)
                    case .settings: SettingsView(model: model)
                    }
                }
                Divider()
                HStack {
                    Image(systemName: model.busy ? "arrow.triangle.2.circlepath" : "info.circle")
                    Text(model.message).lineLimit(2)
                    Spacer()
                    Button("更新") { Task { await model.refresh() } }.keyboardShortcut("r", modifiers: .command)
                }
                .padding(10)
                .accessibilityElement(children: .combine)
            }
        }
        .sheet(isPresented: Binding(get: { !onboardingCompleted }, set: { if !$0 { onboardingCompleted = true } })) {
            OnboardingView(model: model) { onboardingCompleted = true }
        }
        .task { await model.requestNotifications(); await model.refresh() }
        .preferredColorScheme(appearance == "light" ? .light : appearance == "dark" ? .dark : nil)
    }

    private func icon(_ pane: AppModel.Pane) -> String {
        switch pane { case .recording: return "record.circle"; case .sessions: return "list.bullet.rectangle"; case .settings: return "gearshape" }
    }
}

private struct OnboardingView: View {
    @ObservedObject var model: AppModel
    let complete: () -> Void
    @State private var step = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("audio-minutes の準備").font(.largeTitle)
            Group {
                switch step {
                case 0:
                    Label("録音にはマイクとシステム音声録音の許可が必要です。ファイル取り込みだけなら許可は不要です。", systemImage: "mic.badge.plus")
                    Button("プライバシー設定を開く") {
                        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!)
                    }
                case 1:
                    Label(model.capabilities == nil ? "localhostサービスへ未接続です。CLIの service start を利用してください。" : "localhostサービスに接続しました。", systemImage: "server.rack")
                    Text("接続先: \(model.settings.apiBaseUrl)").textSelection(.enabled)
                case 2:
                    Label(model.me == nil ? "システムブラウザーでログインします。アプリはパスワードを受け取りません。" : "\(model.me?.email ?? "") でログイン済みです。", systemImage: "person.badge.key")
                    if model.me == nil { Button("パスキー優先でログイン") { Task { await model.login() } } }
                default:
                    Label(model.capabilities?.transcriptionAvailable == true ? "文字起こしモデルを利用できます。" : "モデル準備状態は設定画面または audio-minutes service models status で確認できます。", systemImage: "waveform.badge.magnifyingglass")
                    Text("録音の同意と所属組織の規則を、開始前に確認してください。").foregroundStyle(.secondary)
                }
            }.font(.title3)
            Spacer()
            HStack {
                Button("戻る") { step -= 1 }.disabled(step == 0)
                Spacer()
                if step < 3 { Button("次へ") { step += 1 }.keyboardShortcut(.defaultAction) }
                else { Button("始める", action: complete).keyboardShortcut(.defaultAction) }
            }
        }
        .padding(32).frame(width: 620, height: 360)
        .accessibilityElement(children: .contain)
    }
}
