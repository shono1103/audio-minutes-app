import Foundation

/// tus 1.0 (Core / Creation / Expiration / Termination) クライアント。
/// 再開時はサーバーの `Upload-Offset` を正とし、送信済みデータを重複適用しない (FR-046〜051)。
public struct UploadProgress: Sendable, Equatable {
    public let trackID: String
    public let sentBytes: Int
    public let totalBytes: Int
    public var fraction: Double { totalBytes == 0 ? 0 : Double(sentBytes) / Double(totalBytes) }
}

public enum TusError: Error, LocalizedError, Sendable {
    case missingLocation
    case missingOffset
    case offsetBeyondFile(Int, Int)
    case fileUnreadable(String)

    public var errorDescription: String? {
        switch self {
        case .missingLocation: return "upload 作成応答に Location がありません"
        case .missingOffset: return "サーバーが Upload-Offset を返しません"
        case .offsetBeyondFile(let offset, let size): return "サーバー offset \(offset) がファイルサイズ \(size) を超えています"
        case .fileUnreadable(let path): return "ファイルを読めません: \(path)"
        }
    }
}

public final class TusUploader: @unchecked Sendable {
    public static let tusVersion = "1.0.0"
    private let client: APIClient
    public var chunkSize: Int

    public init(client: APIClient, chunkSize: Int = 4 * 1024 * 1024) {
        self.client = client
        self.chunkSize = chunkSize
    }

    private var baseHeaders: [String: String] { ["Tus-Resumable": Self.tusVersion] }

    /// Creation extension。セッションに紐づく upload を作り、絶対 URL を返す。
    public func createUpload(sessionID: UUID, trackID: String, length: Int) async throws -> URL {
        let metadata = "track_id \(Data(trackID.utf8).base64EncodedString())"
        var headers = baseHeaders
        headers["Upload-Length"] = String(length)
        headers["Upload-Metadata"] = metadata
        let response = try await client.raw(method: "POST", path: "/v1/sessions/\(sessionID.uuidString.lowercased())/uploads",
                                            headers: headers, expect: 200...299)
        guard let location = response.header("Location") else { throw TusError.missingLocation }
        if let absolute = URL(string: location), absolute.host != nil { return absolute }
        return try client.url(location)
    }

    /// HEAD で受信済み offset を確認する。
    public func offset(of uploadURL: URL) async throws -> Int {
        let response = try await client.raw(method: "HEAD", path: "", headers: baseHeaders, absoluteURL: uploadURL, expect: 200...299)
        guard let raw = response.header("Upload-Offset"), let offset = Int(raw) else { throw TusError.missingOffset }
        return offset
    }

    /// 中断位置から再開して最後まで送る。進捗は `progress` へ通知する。
    public func upload(file: URL, to uploadURL: URL, trackID: String,
                       progress: (@Sendable (UploadProgress) -> Void)? = nil) async throws {
        guard let handle = try? FileHandle(forReadingFrom: file) else { throw TusError.fileUnreadable(file.lastPathComponent) }
        defer { try? handle.close() }
        let size = Int((try? handle.seekToEnd()) ?? 0)
        var offset = try await self.offset(of: uploadURL)
        if offset > size { throw TusError.offsetBeyondFile(offset, size) }
        progress?(UploadProgress(trackID: trackID, sentBytes: offset, totalBytes: size))
        while offset < size {
            try Task.checkCancellation()
            try handle.seek(toOffset: UInt64(offset))
            guard let chunk = try handle.read(upToCount: min(chunkSize, size - offset)), !chunk.isEmpty else { break }
            var headers = baseHeaders
            headers["Content-Type"] = "application/offset+octet-stream"
            headers["Upload-Offset"] = String(offset)
            let response = try await client.raw(method: "PATCH", path: "", headers: headers, body: chunk,
                                                absoluteURL: uploadURL, expect: 200...299)
            guard let raw = response.header("Upload-Offset"), let newOffset = Int(raw) else { throw TusError.missingOffset }
            // サーバーの offset が正。送信量と食い違えばサーバー値に合わせて続ける
            offset = newOffset
            progress?(UploadProgress(trackID: trackID, sentBytes: offset, totalBytes: size))
        }
    }

    /// Termination extension。未完了 upload を明示的に中止する。
    public func cancel(_ uploadURL: URL) async throws {
        _ = try await client.raw(method: "DELETE", path: "", headers: baseHeaders, absoluteURL: uploadURL, expect: 200...299)
    }

    /// 取消済み・session削除に伴い消滅済みのuploadは冪等成功とする。
    public func cancelIfExists(_ uploadURL: URL) async throws {
        do { try await cancel(uploadURL) }
        catch APIClientError.api(_, let status) where status == 404 || status == 410 { return }
        catch APIClientError.unexpectedStatus(let status) where status == 404 || status == 410 { return }
    }

    /// サーバーの対応 extension を確認する。
    public func capabilities() async throws -> [String] {
        let response = try await client.raw(method: "OPTIONS", path: "/v1/uploads", headers: baseHeaders, expect: 200...299)
        return response.header("Tus-Extension")?.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) } ?? []
    }
}
