import ClientCore
import Darwin
import Foundation

@main
struct AudioMinutesCommand {
    static func main() async {
        do {
            var cli = CLI()
            try await cli.run(Array(CommandLine.arguments.dropFirst()))
        } catch {
            let code = CLI.exitCode(for: error)
            if CommandLine.arguments.contains("--json") {
                let payload = CLIErrorOutput(error: error, exitCode: Int(code))
                if let data = try? ContractCoding.encoder(pretty: true).encode(payload) {
                    FileHandle.standardError.write(data)
                    FileHandle.standardError.write(Data("\n".utf8))
                }
            } else {
                FileHandle.standardError.write(Data("error: \(error.localizedDescription)\n".utf8))
            }
            Darwin.exit(code)
        }
    }
}

struct CLIErrorOutput: Encodable {
    struct Body: Encodable {
        var code: String
        var message: String
        var status: Int?
        var requestId: String?
    }
    var error: Body
    var exitCode: Int

    init(error source: Error, exitCode: Int) {
        if case APIClientError.api(let body, let status) = source {
            error = Body(code: body.code.rawValue, message: body.message, status: status, requestId: body.requestId)
        } else if case APIClientError.unauthenticated = source {
            error = Body(code: "unauthenticated", message: source.localizedDescription, status: 401, requestId: nil)
        } else if source is CLIError {
            error = Body(code: "usage", message: source.localizedDescription, status: nil, requestId: nil)
        } else {
            error = Body(code: "client_error", message: source.localizedDescription, status: nil, requestId: nil)
        }
        self.exitCode = exitCode
    }
}
