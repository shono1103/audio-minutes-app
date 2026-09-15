import Foundation

enum CLIError: Error, LocalizedError {
    case usage(String)
    case invalidUUID(String)
    case missingLocalSession(String)
    case invalidInput(String)
    case commandFailed(String, Int32)

    var errorDescription: String? {
        switch self {
        case .usage(let message): return message
        case .invalidUUID(let raw): return "UUID が不正です: \(raw)"
        case .missingLocalSession(let raw): return "ローカルの再開情報がありません: \(raw)"
        case .invalidInput(let message): return "入力が不正です: \(message)"
        case .commandFailed(let command, let status): return "\(command) が終了コード \(status) で失敗しました"
        }
    }
}

/// 値付きoption、flag、位置引数を分離し、未知・重複optionを拒否する。
struct Arguments {
    private(set) var positionals: [String] = []
    private(set) var values: [String: String] = [:]
    private(set) var flags: Set<String> = []

    init(_ raw: [String], valueOptions: Set<String> = [], flagOptions: Set<String> = []) throws {
        var index = 0
        var positionalOnly = false
        while index < raw.count {
            let token = raw[index]
            if positionalOnly {
                positionals.append(token)
            } else if token == "--" {
                positionalOnly = true
            } else if token.hasPrefix("--") {
                let pair = token.split(separator: "=", maxSplits: 1).map(String.init)
                let name = pair[0]
                if valueOptions.contains(name) {
                    guard values[name] == nil else { throw CLIError.usage("option を重複指定できません: \(name)") }
                    if pair.count == 2 {
                        guard !pair[1].isEmpty else { throw CLIError.usage("\(name) に値が必要です") }
                        values[name] = pair[1]
                    } else {
                        index += 1
                        guard index < raw.count, !raw[index].hasPrefix("--") else { throw CLIError.usage("\(name) に値が必要です") }
                        values[name] = raw[index]
                    }
                } else if flagOptions.contains(name), pair.count == 1 {
                    guard !flags.contains(name) else { throw CLIError.usage("option を重複指定できません: \(name)") }
                    flags.insert(name)
                } else {
                    throw CLIError.usage("不明な option です: \(name)")
                }
            } else {
                positionals.append(token)
            }
            index += 1
        }
    }

    func value(_ name: String) -> String? { values[name] }
    func has(_ name: String) -> Bool { flags.contains(name) }
    func requirePositionals(_ range: ClosedRange<Int>, usage: String) throws {
        guard range.contains(positionals.count) else { throw CLIError.usage(usage) }
    }
}
