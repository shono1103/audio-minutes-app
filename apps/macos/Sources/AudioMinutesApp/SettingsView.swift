import ClientCore
import SwiftUI

struct SettingsView: View {
    @ObservedObject var model: AppModel
    @AppStorage("appearance") private var appearance = "system"
    @State private var selectedFormat: UUID?
    @State private var formatName = ""
    @State private var outputLanguage = "ja"
    @State private var template = "# {{ title }}\n\n{{ sections }}"
    @State private var additional = ""
    @State private var formatSections: [FormatSection] = Self.defaultSections
    @State private var preview = ""
    @State private var inviteEmail = ""
    @State private var inviteRole = "member"
    @State private var claudeCode = ""

    var body: some View {
        TabView {
            client.tabItem { Label("一般", systemImage: "gearshape") }
            account.tabItem { Label("アカウント", systemImage: "person.crop.circle") }
            formatEditor.tabItem { Label("フォーマット", systemImage: "doc.badge.gearshape") }
            if model.me?.isOwner == true { owner.tabItem { Label("管理", systemImage: "person.2.badge.gearshape") } }
        }
        .padding().navigationTitle("設定")
    }

    private var client: some View {
        Form {
            Section("接続") {
                TextField("API URL", text: $model.settings.apiBaseUrl)
                    .accessibilityHint("localhost以外のHTTPは拒否され、リモートはHTTPSだけ許可されます")
                LabeledContent("サービス", value: model.capabilities == nil ? "到達不可" : "接続済み")
                Text("起動・停止・更新は audio-minutes service start|stop|update を実行してください。GUIはDocker socketを保持しません。")
                    .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            Section("既定値") {
                Picker("言語", selection: $model.languageMode) { ForEach(LanguageMode.allCases) { Text($0.label).tag($0) } }
                Toggle("Claude送信を既定で有効", isOn: $model.allowExternalSend)
                Picker("外観", selection: $appearance) { Text("システム").tag("system"); Text("ライト").tag("light"); Text("ダーク").tag("dark") }
                Text("Reduce Motionなどのアクセシビリティ設定にはmacOSのシステム設定を使用します。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("通知") {
                Toggle("議事録生成完了", isOn: $model.settings.notifications.minutesCompleted)
                Toggle("処理失敗", isOn: $model.settings.notifications.processingFailed)
                Text("通知にはタイトルと状態だけを表示します。拒否されてもアプリ内の監視は継続します。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button("設定を保存") { model.saveSettings() }.keyboardShortcut(.defaultAction)
        }.formStyle(.grouped)
    }

    private var account: some View {
        Form {
            if let me = model.me {
                LabeledContent("メール", value: me.email)
                LabeledContent("ロール", value: me.role)
                LabeledContent("パスキー", value: String(me.passkeyCount ?? 0))
                ForEach(model.passkeys) { passkey in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(passkey.label ?? "名称なしのパスキー")
                            if let created = passkey.createdAt { Text(created.formatted()).font(.caption).foregroundStyle(.secondary) }
                        }
                        Spacer()
                        Button("削除", role: .destructive) { Task { await model.deletePasskey(passkey) } }
                    }
                }
                Button("ブラウザーでパスキーを追加") { Task { await model.addPasskey() } }
                if let registration = model.passkeyRegistration {
                    Text("追加状態: \(registration.status) / \(registration.requestId)")
                        .font(.caption).textSelection(.enabled)
                    if let url = registration.passkeyUrl {
                        Text(url).font(.caption.monospaced()).textSelection(.enabled).lineLimit(3)
                        HStack {
                            Button("ブラウザーで開く") { model.openPasskeyRegistrationURL() }
                            Button("URLをコピー") { model.copyPasskeyRegistrationURL() }
                        }
                    }
                }
                Button("ログアウト") { Task { await model.logout() } }
                Text("追加時の本人確認とWebAuthnはサーバーのWeb画面で完結します。リカバリーコード再発行はCLIでも行えます。")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("ログインしていません")
                Button("パスキー優先でブラウザーログイン") { Task { await model.login() } }
                Text("登録済みならパスキーが最初に提示されます。初回登録・招待の手順もブラウザー内で完結します。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }.formStyle(.grouped)
    }

    private var formatEditor: some View {
        HSplitView {
            List(model.formats, selection: $selectedFormat) { profile in
                VStack(alignment: .leading) { Text(profile.name); Text("v\(profile.version)\(profile.isDefault == true ? "・既定" : "")").font(.caption) }.tag(profile.profileId)
            }.frame(minWidth: 220)
            Form {
                TextField("名前", text: $formatName)
                Picker("出力言語", selection: $outputLanguage) { Text("日本語").tag("ja"); Text("English").tag("en") }
                TextField("追加指示", text: $additional, axis: .vertical)
                Section("章（ドラッグで並べ替え）") {
                    ForEach(Array(formatSections.enumerated()), id: \.element.key) { index, section in
                        HStack {
                            Toggle(isOn: $formatSections[index].enabled) { TextField("章タイトル", text: $formatSections[index].title) }
                            Text(section.key).font(.caption.monospaced()).foregroundStyle(.secondary)
                            Button { moveSection(from: index, by: -1) } label: { Image(systemName: "arrow.up") }
                                .disabled(index == 0).accessibilityLabel("上へ移動")
                            Button { moveSection(from: index, by: 1) } label: { Image(systemName: "arrow.down") }
                                .disabled(index == formatSections.count - 1).accessibilityLabel("下へ移動")
                        }
                    }
                }
                TextEditor(text: $template).frame(minHeight: 120).font(.body.monospaced()).accessibilityLabel("Markdownテンプレート")
                HStack {
                    Button("新規作成") { Task { await model.createFormat(input()) } }.disabled(formatName.isEmpty)
                    Button("更新") { if let selectedFormat { Task { await model.updateFormat(selectedFormat, input()) } } }.disabled(selectedFormat == nil || formatName.isEmpty)
                    Button("複製") { if let selectedFormat { Task { await model.duplicateFormat(selectedFormat) } } }.disabled(selectedFormat == nil)
                    Button("既定に設定") { if let selectedFormat { Task { await model.setDefaultFormat(selectedFormat) } } }.disabled(selectedFormat == nil)
                    Button("削除", role: .destructive) { if let selectedFormat { Task { await model.deleteFormat(selectedFormat) } } }.disabled(selectedFormat == nil)
                }
                Button("検証してプレビュー") { Task { preview = await model.previewFormat(input()) } }
                if !preview.isEmpty { ScrollView { Text(preview).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }.frame(maxHeight: 160) }
            }.formStyle(.grouped)
        }
        .onChange(of: selectedFormat) { _, id in
            guard let profile = model.formats.first(where: { $0.profileId == id }) else { return }
            formatName = profile.name; outputLanguage = profile.outputLanguage; additional = profile.additionalInstructions
            template = profile.templateMarkdown; formatSections = profile.sections
        }
    }

    private var owner: some View {
        ScrollView {
            Form {
                Section("Claude subscription") {
                    LabeledContent("状態", value: model.claude?.label ?? "未確認")
                    if let detail = model.claude?.detail { Text(detail).foregroundStyle(.secondary) }
                    HStack { Button("ログイン") { Task { await model.claudeLogin() } }; Button("ログアウト") { Task { await model.claudeLogout() } } }
                    if let login = model.claudeLoginSession, ["pending", "url_ready"].contains(login.state) {
                        Text("login: \(login.state) / \(login.authSessionId)").font(.caption).textSelection(.enabled)
                        if let url = model.claudeAuthURL {
                            Text(url).font(.caption.monospaced()).textSelection(.enabled).lineLimit(3)
                            HStack {
                                Button("認証画面を開く") { model.openClaudeAuthURL() }
                                Button("URLをコピー") { model.copyClaudeAuthURL() }
                            }
                        } else {
                            Text("認証URLを待っています。状態確認は継続中です。")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        HStack {
                            TextField("認証コード（必要な場合）", text: $claudeCode)
                            Button("コード送信") { Task { await model.submitClaudeLoginCode(claudeCode); claudeCode = "" } }.disabled(claudeCode.isEmpty)
                            Button("取消") { Task { await model.cancelClaudeLogin() } }
                        }
                    }
                    Text("認証URLは公式originを検証して一時表示し、明示操作時だけブラウザーまたはクリップボードへ渡します。API keyとの競合時は処理しません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("管理操作の再認証") {
                    Button("システムブラウザーで再認証") { Task { await model.reauthenticate() } }
                    Text("パスワードまたはパスキーはWeb画面だけで入力し、このアプリには渡しません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("保持期間") {
                    if let value = model.retention {
                        Stepper("未完了upload: \(value.uploadHours)時間", value: retentionBinding(\.uploadHours), in: 1...720)
                        Stepper("音声: \(value.audioDays)日", value: retentionBinding(\.audioDays), in: 1...3650)
                        Stepper("ログ: \(value.logDays)日", value: retentionBinding(\.logDays), in: 1...365)
                        Button("保持期間を保存") { if let r = model.retention { Task { await model.updateRetention(upload: r.uploadHours, audio: r.audioDays, log: r.logDays) } } }
                    }
                }
                Section("招待") {
                    HStack {
                        TextField("メールアドレス", text: $inviteEmail)
                        Picker("ロール", selection: $inviteRole) { Text("member").tag("member"); Text("owner").tag("owner") }.frame(width: 130)
                        Button("招待発行") { Task { await model.invite(email: inviteEmail, role: inviteRole); inviteEmail = "" } }.disabled(inviteEmail.isEmpty)
                    }
                    ForEach(model.invitations) { invitation in
                        HStack {
                            Text("\(invitation.email) — \(invitation.role) — \(invitation.expiresAt.formatted())")
                            Spacer()
                            Button("失効", role: .destructive) { Task { await model.revokeInvitation(invitation) } }
                        }
                    }
                }
                Section("ユーザー") {
                    ForEach(model.adminUsers) { user in
                        HStack {
                            Text(user.email).frame(maxWidth: .infinity, alignment: .leading)
                            Picker("ロール", selection: Binding(
                                get: { user.role },
                                set: { role in Task { await model.updateUser(user, role: role) } }
                            )) { Text("member").tag("member"); Text("owner").tag("owner") }
                            .labelsHidden().frame(width: 110)
                            Button(user.disabled ? "有効化" : "無効化", role: user.disabled ? nil : .destructive) {
                                Task { await model.updateUser(user, disabled: !user.disabled) }
                            }
                        }
                    }
                    Text("ロール変更・無効化には再認証が必要です。最後のownerはサーバーが保護します。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }.formStyle(.grouped)
        }
    }

    private func input() -> FormatProfileInput {
        return .init(name: formatName, outputLanguage: outputLanguage, sections: formatSections,
                     additionalInstructions: additional, templateMarkdown: template)
    }

    private static let defaultSections = [
            FormatSection(key: "summary", title: "要約", enabled: true),
            FormatSection(key: "decisions", title: "決定事項", enabled: true),
            FormatSection(key: "actions", title: "アクションアイテム", enabled: true),
            FormatSection(key: "open_questions", title: "未決事項・確認事項", enabled: true),
        ]

    private func moveSection(from index: Int, by offset: Int) {
        let destination = index + offset
        guard formatSections.indices.contains(index), formatSections.indices.contains(destination) else { return }
        formatSections.swapAt(index, destination)
    }

    private func retentionBinding(_ keyPath: WritableKeyPath<Retention, Int>) -> Binding<Int> {
        Binding(get: { model.retention?[keyPath: keyPath] ?? 1 }, set: { model.retention?[keyPath: keyPath] = $0 })
    }
}
