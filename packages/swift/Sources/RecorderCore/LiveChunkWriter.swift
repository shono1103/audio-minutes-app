import AVFoundation
import ClientCore
import Foundation

/// 録音中に送信できる、headerとchecksumが確定した単一trackのWAV chunk。
public struct LiveAudioChunk: Sendable, Equatable {
    public var sessionID: UUID
    public var trackID: TrackID
    public var role: TrackRole
    public var sequence: Int
    public var startOffsetMs: Int
    public var durationMs: Int
    public var byteSize: Int
    public var sha256: String
    public var fileURL: URL
}

/// 完全WAVへの書き込みと並行し、30秒ごとの不変WAVを作る。
final class LiveChunkWriter: @unchecked Sendable {
    static let chunkDurationMs = 30_000
    static let overlapDurationMs = 1_000
    static let bytesPerSecond = Int(WavWriter.targetSampleRate) * 2
    static let targetBytes = bytesPerSecond * chunkDurationMs / 1000
    static let overlapBytes = bytesPerSecond * overlapDurationMs / 1000
    static let stepBytes = targetBytes - overlapBytes

    private let sessionID: UUID
    private let trackID: TrackID
    private let role: TrackRole
    private let directory: URL
    private let onReady: @Sendable (LiveAudioChunk) -> Void
    private var pending = Data()
    private var totalBytes = 0
    private var coveredUntilBytes = 0
    private var nextStartBytes = 0
    private var sequence = 0

    init(sessionID: UUID, trackID: TrackID, role: TrackRole, directory: URL,
         onReady: @escaping @Sendable (LiveAudioChunk) -> Void) throws {
        self.sessionID = sessionID
        self.trackID = trackID
        self.role = role
        self.directory = directory
        self.onReady = onReady
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
    }

    func append(buffer: AVAudioPCMBuffer) throws {
        guard let channel = buffer.int16ChannelData?[0] else { return }
        let data = Data(bytes: channel, count: Int(buffer.frameLength) * 2)
        pending.append(data)
        totalBytes += data.count
        while pending.count >= Self.targetBytes {
            try emit(Data(pending.prefix(Self.targetBytes)))
            pending.removeFirst(Self.stepBytes)
            nextStartBytes += Self.stepBytes
        }
    }

    func finalize() throws {
        if totalBytes > coveredUntilBytes, !pending.isEmpty { try emit(pending) }
    }

    private func emit(_ pcm: Data) throws {
        let trackDirectory = directory.appendingPathComponent(trackID.rawValue, isDirectory: true)
        try FileManager.default.createDirectory(
            at: trackDirectory, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let url = trackDirectory.appendingPathComponent(String(format: "%06d.wav", sequence))
        let writer = try WavWriter(url: url)
        try writer.append(pcm16: pcm)
        let result = try writer.finalize()
        onReady(LiveAudioChunk(
            sessionID: sessionID,
            trackID: trackID,
            role: role,
            sequence: sequence,
            startOffsetMs: nextStartBytes * 1000 / Self.bytesPerSecond,
            durationMs: result.durationMs,
            byteSize: result.byteSize,
            sha256: result.sha256,
            fileURL: writer.url
        ))
        coveredUntilBytes = max(coveredUntilBytes, nextStartBytes + pcm.count)
        sequence += 1
    }
}
