import ClientCore
import Foundation

extension CLI {
    func runMinutes(_ raw: [String], service: SessionService) async throws {
        guard let command = raw.first else { throw CLIError.usage("minutes {show|versions|edit|regenerate|select|restore|compare}") }
        let rest = Array(raw.dropFirst())
        switch command {
        case "show":
            let args = try Arguments(rest, valueOptions: ["--version"]); try args.requirePositionals(1...1, usage: "minutes show SESSION_ID [--version VERSION_ID]")
            let sid = try uuid(args.positionals[0])
            let document: MinutesDocument
            if let rawVersion = args.value("--version") { document = try await service.minutesVersion(sid, versionID: uuid(rawVersion)) }
            else { document = try await service.currentMinutes(sid) }
            if json { try output(document) { $0.bodyMarkdown } } else { print(document.bodyMarkdown) }
        case "versions":
            let args = try Arguments(rest); try args.requirePositionals(1...1, usage: "minutes versions SESSION_ID")
            try output(try await service.minutesVersions(uuid(args.positionals[0]))) { $0.items.map { "\($0.versionId.uuidString.lowercased())\tv\($0.versionNumber)\t\($0.kind)" }.joined(separator: "\n") }
        case "edit":
            let args = try Arguments(rest, valueOptions: ["--parent", "--expected"]); try args.requirePositionals(2...2, usage: "minutes edit SESSION_ID MARKDOWN_FILE [--parent VERSION_ID] [--expected VERSION_ID]")
            let sid = try uuid(args.positionals[0]), body = try String(contentsOfFile: args.positionals[1], encoding: .utf8)
            let versions = try await service.minutesVersions(sid)
            let parent = try args.value("--parent").map(uuid) ?? versions.currentMinutesVersionId
            guard let parent else { throw CLIError.invalidInput("編集元の議事録版がありません") }
            let expected = try args.value("--expected").map(uuid) ?? versions.currentMinutesVersionId
            let request = ManualEditRequest(parentVersionId: parent, bodyMarkdown: body, expectedCurrentVersionId: expected)
            try output(try await service.saveManualEdit(sid, request)) { "\($0.versionId.uuidString.lowercased())\tv\($0.versionNumber)\t\($0.kind)" }
        case "regenerate":
            let args = try Arguments(rest, valueOptions: ["--base", "--instructions", "--instructions-file", "--format"], flagOptions: ["--original-format"])
            try args.requirePositionals(1...1, usage: "minutes regenerate SESSION_ID [--base VERSION_ID] [--instructions TEXT|--instructions-file FILE] [--format UUID|NAME|--original-format]")
            guard !(args.value("--instructions") != nil && args.value("--instructions-file") != nil) else { throw CLIError.usage("instructions は文字列かファイルの一方だけです") }
            guard !(args.has("--original-format") && args.value("--format") != nil) else { throw CLIError.usage("format option は同時指定できません") }
            let instructions: String
            if let file = args.value("--instructions-file") { instructions = try String(contentsOfFile: file, encoding: .utf8) }
            else { instructions = args.value("--instructions") ?? "" }
            let request = RegenerateRequest(
                baseVersionId: try args.value("--base").map(uuid), instructions: instructions,
                formatProfileId: try await resolveFormat(args.value("--format"), service: service),
                useSnapshot: args.has("--original-format") || args.value("--format") == nil
            )
            let accepted = try await service.regenerate(uuid(args.positionals[0]), request)
            try output(JobOutput(jobId: accepted.jobId, status: accepted.session.status)) { "\($0.jobId.uuidString.lowercased())\t\($0.status.rawValue)" }
        case "select", "restore":
            let args = try Arguments(rest); try args.requirePositionals(2...2, usage: "minutes \(command) SESSION_ID VERSION_ID")
            let sid = try uuid(args.positionals[0]), vid = try uuid(args.positionals[1])
            let version: MinutesVersion
            if command == "select" { version = try await service.selectCurrent(sid, versionID: vid) }
            else { version = try await service.restore(sid, versionID: vid) }
            try output(version) { "\($0.versionId.uuidString.lowercased())\tv\($0.versionNumber)\t\($0.kind)" }
        case "compare":
            let args = try Arguments(rest); try args.requirePositionals(3...3, usage: "minutes compare SESSION_ID FROM_VERSION TO_VERSION")
            let value = try await service.compare(uuid(args.positionals[0]), from: uuid(args.positionals[1]), to: uuid(args.positionals[2]))
            if json { try output(value) { $0.diff } } else { print(value.diff) }
        default: throw CLIError.usage("minutes {show|versions|edit|regenerate|select|restore|compare}")
        }
    }

    func runFormats(_ raw: [String], service: SessionService) async throws {
        let command = raw.first ?? "list", rest = raw.isEmpty ? [] : Array(raw.dropFirst())
        switch command {
        case "list":
            let args = try Arguments(rest); try args.requirePositionals(0...0, usage: "formats list")
            try output(try await service.formats()) { $0.map { "\($0.profileId.uuidString.lowercased())\tv\($0.version)\t\($0.name)\($0.isDefault == true ? "\tdefault" : "")" }.joined(separator: "\n") }
        case "show":
            let args = try Arguments(rest); try args.requirePositionals(1...1, usage: "formats show PROFILE_ID")
            try output(try await service.format(uuid(args.positionals[0]))) { "\($0.profileId.uuidString.lowercased())\tv\($0.version)\t\($0.name)" }
        case "create", "preview":
            let args = try Arguments(rest, valueOptions: ["--file"]); try args.requirePositionals(0...0, usage: "formats \(command) --file INPUT.json")
            guard let file = args.value("--file") else { throw CLIError.usage("--file が必要です") }
            let input = try readJSON(file, as: FormatProfileInput.self)
            if command == "create" { try output(try await service.createFormat(input)) { "\($0.profileId.uuidString.lowercased())\tv\($0.version)\t\($0.name)" } }
            else { try output(try await service.previewFormat(input)) { $0.markdown } }
        case "update":
            let args = try Arguments(rest, valueOptions: ["--file"]); try args.requirePositionals(1...1, usage: "formats update PROFILE_ID --file INPUT.json")
            guard let file = args.value("--file") else { throw CLIError.usage("--file が必要です") }
            let input = try readJSON(file, as: FormatProfileInput.self)
            try output(try await service.updateFormat(uuid(args.positionals[0]), input)) { "\($0.profileId.uuidString.lowercased())\tv\($0.version)\t\($0.name)" }
        case "delete", "duplicate", "default":
            let args = try Arguments(rest, flagOptions: ["--yes"]); try args.requirePositionals(1...1, usage: "formats \(command) PROFILE_ID")
            let id = try uuid(args.positionals[0])
            if command == "delete" {
                guard args.has("--yes") else { throw CLIError.usage("削除には --yes が必要です") }
                try await service.deleteFormat(id); try output(StateOutput(state: "deleted")) { $0.state }
            } else {
                let value: FormatProfile
                if command == "duplicate" { value = try await service.duplicateFormat(id) }
                else { value = try await service.setDefaultFormat(id) }
                try output(value) { "\($0.profileId.uuidString.lowercased())\tv\($0.version)\t\($0.name)" }
            }
        default: throw CLIError.usage("formats {list|show|create|update|delete|duplicate|default|preview}")
        }
    }
}
