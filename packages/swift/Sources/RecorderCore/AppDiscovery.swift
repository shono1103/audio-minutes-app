import AppKit
import Foundation

/// 録音対象として許可するアプリ (FR-001、FR-133、FR-134)。
public struct SupportedApp: Sendable, Equatable, Hashable {
    public let bundleIDs: [String]
    public let displayName: String
    public let isBrowser: Bool

    public static let zoom = SupportedApp(bundleIDs: ["us.zoom.xos"], displayName: "Zoom", isBrowser: false)
    public static let teams = SupportedApp(bundleIDs: ["com.microsoft.teams2", "com.microsoft.teams"], displayName: "Microsoft Teams", isBrowser: false)
    public static let chrome = SupportedApp(bundleIDs: ["com.google.Chrome"], displayName: "Google Chrome", isBrowser: true)
    public static let all: [SupportedApp] = [.zoom, .teams, .chrome]

    public static func match(bundleID: String?) -> SupportedApp? {
        guard let bundleID else { return nil }
        return all.first { $0.bundleIDs.contains(bundleID) }
    }
}

/// 実行中の対応アプリ。PID 集合は録音開始直前に再確認し、録音中は固定する (FR-135)。
public struct RecordableApp: Sendable, Equatable, Hashable, Identifiable {
    public let app: SupportedApp
    public let bundleID: String
    public let pids: [pid_t]
    public var id: String { bundleID }
    public var displayName: String { app.displayName }

    public init(app: SupportedApp, bundleID: String, pids: [pid_t]) {
        self.app = app
        self.bundleID = bundleID
        self.pids = pids.sorted()
    }
}

public struct RunningAppInfo: Sendable, Equatable {
    public let bundleID: String?
    public let pid: pid_t
    public init(bundleID: String?, pid: pid_t) {
        self.bundleID = bundleID
        self.pid = pid
    }
}

public enum AppDiscovery {
    /// 実行中アプリ一覧から対応アプリだけを抽出する (純関数、テスト対象)。
    public static func filterSupported(_ apps: [RunningAppInfo]) -> [RecordableApp] {
        var grouped: [String: (SupportedApp, [pid_t])] = [:]
        for info in apps {
            guard let supported = SupportedApp.match(bundleID: info.bundleID), let bundleID = info.bundleID else { continue }
            var entry = grouped[bundleID] ?? (supported, [])
            entry.1.append(info.pid)
            grouped[bundleID] = entry
        }
        return SupportedApp.all.flatMap { supported in
            supported.bundleIDs.compactMap { bundleID in
                grouped[bundleID].map { RecordableApp(app: $0.0, bundleID: bundleID, pids: $0.1) }
            }
        }
    }

    public static func runningApps() -> [RecordableApp] {
        let infos = NSWorkspace.shared.runningApplications.map { RunningAppInfo(bundleID: $0.bundleIdentifier, pid: $0.processIdentifier) }
        return filterSupported(infos)
    }

    /// 選択したアプリの同一性を再確認し、その時点の PID 集合を返す。存在しなければ nil。
    public static func reconfirm(_ selected: RecordableApp) -> RecordableApp? {
        runningApps().first { $0.bundleID == selected.bundleID }
    }
}

/// アプリの起動・終了通知を購読して候補一覧を更新する (待機中のみ)。
public final class AppDiscoveryObserver: @unchecked Sendable {
    private var tokens: [NSObjectProtocol] = []
    private let handler: @Sendable ([RecordableApp]) -> Void

    public init(handler: @escaping @Sendable ([RecordableApp]) -> Void) {
        self.handler = handler
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            tokens.append(center.addObserver(forName: name, object: nil, queue: nil) { [weak self] _ in
                self?.handler(AppDiscovery.runningApps())
            })
        }
    }

    public func refresh() { handler(AppDiscovery.runningApps()) }

    deinit {
        tokens.forEach { NSWorkspace.shared.notificationCenter.removeObserver($0) }
    }
}
