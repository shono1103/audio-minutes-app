import AppKit
import SwiftUI
import UserNotifications

@main
struct AudioMinutesApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup(id: "main") {
            RootView(model: model)
                .frame(minWidth: 1040, minHeight: 600)
                .onAppear { delegate.model = model }
        }
        .commands {
            CommandGroup(replacing: .appTermination) {
                Button("audio-minutes を終了") { model.quit() }.keyboardShortcut("q")
            }
            CommandMenu("録音") {
                Button(model.isRecording ? "録音を停止" : "録音を開始") {
                    if model.isRecording { _ = model.stopRecording() } else { Task { await model.startRecording() } }
                }.keyboardShortcut("r", modifiers: [.command, .shift])
            }
        }

        MenuBarExtra("audio-minutes", systemImage: model.isRecording ? "record.circle.fill" : "waveform") {
            MenuBarView(model: model)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    weak var model: AppModel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let identifier = response.notification.request.identifier
        Task { @MainActor [weak self] in await self?.model?.openNotificationSession(identifier: identifier); completionHandler() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model, model.isRecording else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "録音中です"
        alert.informativeText = "終了する前に録音を停止して保存します。録音を破棄する終了操作はありません。"
        alert.addButton(withTitle: "録音を停止して保存して終了")
        alert.addButton(withTitle: "キャンセル")
        guard alert.runModal() == .alertFirstButtonReturn else { return .terminateCancel }
        return model.stopRecording() ? .terminateNow : .terminateCancel
    }
}

private struct MenuBarView: View {
    @ObservedObject var model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Text(model.isRecording ? "録音中 \(model.elapsed.formatted(.number.precision(.fractionLength(0)))) 秒" : "録音停止中")
        if model.isRecording {
            Button("停止して保存") { _ = model.stopRecording() }
        } else {
            Button("最後の設定で録音開始") { Task { await model.startRecording() } }
                .disabled(model.selectedAppID == nil || model.selectedMicrophoneUID == nil || model.selectedTabID != nil && model.chromeState != .connected)
        }
        Divider()
        Button("メインウィンドウを開く") { openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true) }
        Button("終了") { model.quit() }.keyboardShortcut("q")
    }
}
