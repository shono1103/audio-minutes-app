import ClientCore
import Foundation
import ImportCore

extension CLI {
    func runImport(_ raw: [String], settings: ClientSettings, service: SessionService) async throws {
        let args = try Arguments(raw, valueOptions: ["--title", "--language", "--format"], flagOptions: ["--external-send", "--no-external-send", "--no-claude"])
        try args.requirePositionals(1...1, usage: "import AUDIO_FILE [--title TITLE] [--language MODE] [--format UUID|NAME] [--external-send|--no-external-send]")
        let binding = Self.localSessionBinding(baseURL: service.client.baseURL, currentUser: try? await service.me())
        let request = ImportRequest(
            fileURL: URL(fileURLWithPath: args.positionals[0]), title: args.value("--title"),
            languageMode: try language(args.value("--language")) ?? settings.defaultLanguageMode,
            allowExternalSend: try Self.importExternalSend(args: args, settings: settings),
            formatProfileId: try await resolveFormat(args.value("--format"), service: service) ?? settings.defaultFormatProfileId,
            destinationOrigin: binding.destinationOrigin,
            ownerUserId: binding.ownerUserId
        )
        let store = LocalSessionStore()
        let imported = try ImportService(store: store).run(request)
        if !json { print("prepared\t\(imported.state.package.sessionId.uuidString.lowercased())") }
        let session = try await store.uploadPending(imported.state, service: service) { progress in
            if !json { print("upload\t\(progress.trackID)\t\(progress.sentBytes)/\(progress.totalBytes)") }
        }
        try output(ImportOutput(sessionId: session.sessionId, inputKind: session.inputKind, status: session.status)) {
            "accepted\t\($0.sessionId.uuidString.lowercased())\t\($0.inputKind.rawValue)\t\($0.status.rawValue)"
        }
    }

    /// 明示指定を設定より優先し、指定なしなら保存済み既定値を使う。
    static func importExternalSend(args: Arguments, settings: ClientSettings) throws -> Bool {
        let disabled = args.has("--no-external-send") || args.has("--no-claude")
        guard !(args.has("--external-send") && disabled) else {
            throw CLIError.usage("外部送信optionは同時指定できません")
        }
        if args.has("--external-send") { return true }
        if disabled { return false }
        return settings.claudeSendDefault
    }

    func runResume(_ raw: [String], service: SessionService) async throws {
        let args = try Arguments(raw, flagOptions: ["--cancel", "--yes"])
        try args.requirePositionals(0...1, usage: "resume [SESSION_ID] [--cancel --yes]")
        let store = LocalSessionStore()
        if args.has("--cancel") {
            let rawID = try Self.confirmedPendingCancellationID(args)
            let id = try uuid(rawID)
            guard let state = try store.load(sessionID: id), state.pendingUpload else {
                throw CLIError.missingLocalSession(rawID)
            }
            try await store.cancelPending(state, service: service)
            try output(DeleteOutput(deleted: true, sessionId: id)) { _ in "cancelled-and-deleted\t\(id.uuidString.lowercased())" }
            return
        }
        guard !args.has("--yes") else { throw CLIError.usage("--yes は --cancel と一緒に指定してください") }
        let states: [LocalSessionState]
        if let rawID = args.positionals.first {
            let id = try uuid(rawID)
            guard let state = try store.load(sessionID: id), state.pendingUpload else { throw CLIError.missingLocalSession(rawID) }
            states = [state]
        } else { states = try store.pendingUploads() }
        var results: [ResumeItem] = []
        for state in states {
            do {
                let session = try await store.uploadPending(state, service: service) { progress in
                    if !json { print("upload\t\(state.package.sessionId.uuidString.lowercased())\t\(progress.trackID)\t\(progress.sentBytes)/\(progress.totalBytes)") }
                }
                results.append(.init(sessionId: session.sessionId, status: session.status, error: nil))
            } catch {
                results.append(.init(sessionId: state.package.sessionId, status: nil, error: error.localizedDescription))
                if states.count == 1 { throw error }
            }
        }
        try output(results) { values in
            values.isEmpty ? "再開対象はありません" : values.map { "\($0.sessionId.uuidString.lowercased())\t\($0.status?.rawValue ?? "failed")\t\($0.error ?? "")" }.joined(separator: "\n")
        }
    }

    static func localSessionBinding(baseURL: URL, currentUser: Me?) -> LocalSessionBinding {
        LocalSessionBinding.capture(apiURL: baseURL, currentUser: currentUser)
    }

    static func confirmedPendingCancellationID(_ args: Arguments) throws -> String {
        guard args.has("--yes"), let rawID = args.positionals.first else {
            throw CLIError.usage("未完了アップロードの中止・削除には SESSION_ID と --yes が必要です")
        }
        return rawID
    }

    func runStatus(_ raw: [String], service: SessionService) async throws {
        let args = try Arguments(raw, flagOptions: ["--watch"])
        try args.requirePositionals(1...1, usage: "status SESSION_ID [--watch]")
        let id = try uuid(args.positionals[0])
        while true {
            let session = try await service.session(id)
            if !args.has("--watch") || session.status.isTerminal {
                try output(session) { "\($0.sessionId.uuidString.lowercased())\t\($0.status.rawValue)\t\($0.title)" }
                return
            }
            if !json { print("\(session.status.rawValue)\t\(session.title)") }
            try await Task.sleep(for: .milliseconds(service.client.pollIntervalMs))
        }
    }

    func runSessions(_ raw: [String], service: SessionService) async throws {
        let explicit = raw.first.map { !$0.hasPrefix("--") } ?? false
        let command = explicit ? raw[0] : "list"
        let rest = explicit ? Array(raw.dropFirst()) : raw
        switch command {
        case "list":
            let args = try Arguments(rest, valueOptions: ["--cursor", "--limit"])
            try args.requirePositionals(0...0, usage: "sessions [list] [--cursor CURSOR] [--limit 1...200]")
            let limit = Int(args.value("--limit") ?? "50") ?? 0
            guard (1...200).contains(limit) else { throw CLIError.usage("--limit は 1...200 です") }
            let list = try await service.listSessions(cursor: args.value("--cursor"), limit: limit)
            try output(list) { $0.items.map { "\($0.sessionId.uuidString.lowercased())\t\($0.status.rawValue)\t\($0.title)" }.joined(separator: "\n") }
        case "show":
            let args = try Arguments(rest); try args.requirePositionals(1...1, usage: "sessions show SESSION_ID")
            try output(try await service.session(uuid(args.positionals[0]))) { "\($0.sessionId.uuidString.lowercased())\t\($0.status.rawValue)\t\($0.title)" }
        case "update":
            let args = try Arguments(rest, valueOptions: ["--title", "--language"], flagOptions: ["--external-send", "--no-external-send"])
            try args.requirePositionals(1...1, usage: "sessions update SESSION_ID [--title TITLE] [--language MODE] [--external-send|--no-external-send]")
            guard !(args.has("--external-send") && args.has("--no-external-send")) else { throw CLIError.usage("外部送信optionは同時指定できません") }
            let external = args.has("--external-send") ? true : args.has("--no-external-send") ? false : nil
            let patch = SessionPatch(title: args.value("--title"), languageMode: try language(args.value("--language")), allowExternalSend: external)
            try output(try await service.update(uuid(args.positionals[0]), patch)) { "\($0.sessionId.uuidString.lowercased())\t\($0.status.rawValue)\t\($0.title)" }
        case "retry":
            let args = try Arguments(rest, valueOptions: ["--stage", "--language"]); try args.requirePositionals(1...1, usage: "sessions retry SESSION_ID --stage transcription|minutes [--language MODE]")
            let stage = args.value("--stage") ?? "transcription"
            guard ["transcription", "minutes"].contains(stage) else { throw CLIError.usage("--stage は transcription / minutes です") }
            let accepted = try await service.retry(uuid(args.positionals[0]), stage: stage, languageMode: try language(args.value("--language")))
            try output(JobOutput(jobId: accepted.jobId, status: accepted.session.status)) { "\($0.jobId.uuidString.lowercased())\t\($0.status.rawValue)" }
        case "jobs":
            let args = try Arguments(rest); try args.requirePositionals(1...1, usage: "sessions jobs SESSION_ID")
            try output(try await service.jobs(uuid(args.positionals[0]))) { $0.map { "\($0.jobId.uuidString.lowercased())\t\($0.kind)\t\($0.status)" }.joined(separator: "\n") }
        case "cancel":
            let args = try Arguments(rest); try args.requirePositionals(2...2, usage: "sessions cancel SESSION_ID JOB_ID")
            try output(try await service.cancelJob(uuid(args.positionals[0]), jobID: uuid(args.positionals[1]))) { "\($0.jobId.uuidString.lowercased())\t\($0.status)\tcancel_requested=\($0.cancelRequested ?? false)" }
        case "delete":
            let args = try Arguments(rest, flagOptions: ["--yes"]); try args.requirePositionals(1...1, usage: "sessions delete SESSION_ID --yes")
            guard args.has("--yes") else { throw CLIError.usage("削除には --yes が必要です") }
            let id = try uuid(args.positionals[0]); try await service.delete(id)
            try output(DeleteOutput(deleted: true, sessionId: id)) { _ in "deleted\t\(id.uuidString.lowercased())" }
        case "shares":
            let args = try Arguments(rest); try args.requirePositionals(1...1, usage: "sessions shares SESSION_ID")
            try output(try await service.shares(uuid(args.positionals[0]))) { $0.map { "\($0.userId.uuidString.lowercased())\t\($0.email ?? "")" }.joined(separator: "\n") }
        case "share":
            let args = try Arguments(rest, valueOptions: ["--email"]); try args.requirePositionals(1...1, usage: "sessions share SESSION_ID --email EMAIL")
            guard let email = args.value("--email") else { throw CLIError.usage("--email が必要です") }
            try output(try await service.share(uuid(args.positionals[0]), email: email)) { "\($0.userId.uuidString.lowercased())\t\($0.email ?? "")" }
        case "unshare":
            let args = try Arguments(rest); try args.requirePositionals(2...2, usage: "sessions unshare SESSION_ID USER_ID")
            try await service.unshare(uuid(args.positionals[0]), userID: uuid(args.positionals[1]))
            try output(StateOutput(state: "unshared")) { $0.state }
        default: throw CLIError.usage("sessions {list|show|update|retry|jobs|cancel|delete|shares|share|unshare}")
        }
    }

    func runExport(_ raw: [String], service: SessionService) async throws {
        let args = try Arguments(raw, valueOptions: ["--out"], flagOptions: ["--audio"])
        try args.requirePositionals(1...1, usage: "export SESSION_ID --out DIRECTORY [--audio]")
        guard let rawOut = args.value("--out") else { throw CLIError.usage("--out が必要です") }
        let id = try uuid(args.positionals[0]), directory = URL(fileURLWithPath: rawOut, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var files: [String] = []
        let session = try await service.session(id)
        try ContractCoding.encoder(pretty: true).encode(session).write(to: directory.appendingPathComponent("session.json"), options: .atomic); files.append("session.json")
        if let transcript = try await optionalArtifact({ try await service.transcript(id) }) {
            try ContractCoding.encoder(pretty: true).encode(transcript).write(to: directory.appendingPathComponent("transcript.json"), options: .atomic); files.append("transcript.json")
        }
        if let markdown = try await optionalArtifact({ try await service.transcriptMarkdown(id) }) {
            try Data(markdown.utf8).write(to: directory.appendingPathComponent("transcript.md"), options: .atomic); files.append("transcript.md")
        }
        if let minutes = try await optionalArtifact({ try await service.currentMinutes(id) }) {
            try ContractCoding.encoder(pretty: true).encode(minutes.version).write(to: directory.appendingPathComponent("minutes-version.json"), options: .atomic)
            try Data(minutes.bodyMarkdown.utf8).write(to: directory.appendingPathComponent("minutes.md"), options: .atomic)
            files += ["minutes-version.json", "minutes.md"]
        }
        if args.has("--audio") {
            for track in session.tracks where track.artifactId != nil {
                let response = try await service.audio(id, trackID: track.trackId)
                let ext = SessionService.audioFileExtension(response)
                let name = "\(track.trackId).\(ext)"; try response.body.write(to: directory.appendingPathComponent(name), options: .atomic); files.append(name)
            }
        }
        try output(ExportOutput(sessionId: id, outputDirectory: directory.path, files: files.sorted())) { "exported\t\($0.outputDirectory)\t\($0.files.joined(separator: ","))" }
    }
}
