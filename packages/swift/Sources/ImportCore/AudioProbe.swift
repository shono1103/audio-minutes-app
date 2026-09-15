import AudioToolbox
import AVFoundation
import Foundation

/// 取り込み音声の実形式検査 (FR-138)。拡張子・申告 MIME を信用せず、内容から判定する。
public struct AudioProbeResult: Sendable, Equatable {
    public var container: String        // wav | m4a | mp3 | flac
    public var codec: String            // pcm_s16le | pcm_s24le | pcm_f32le | aac | mp3 | flac
    public var sampleRate: Int
    public var channels: Int
    public var durationMs: Int
    public var byteSize: Int
}

public enum ImportError: Error, LocalizedError, Sendable, Equatable {
    case fileNotFound(String)
    case unsupportedFormat(String)
    case corrupted(String)
    case tooLong(durationMs: Int, limitMs: Int)
    case tooLarge(bytes: Int, limitBytes: Int)
    case copyFailed(String)

    public var errorDescription: String? {
        switch self {
        case .fileNotFound(let name): return "ファイルが見つかりません: \(name)"
        case .unsupportedFormat(let detail): return "非対応の音声形式です (\(detail))。WAV / M4A (AAC) / MP3 / FLAC を指定してください"
        case .corrupted(let detail): return "音声ファイルを読めません (\(detail))。破損している可能性があります"
        case .tooLong(let duration, let limit): return "音声が長すぎます (\(duration / 60000) 分)。上限は \(limit / 60000) 分です"
        case .tooLarge(let bytes, let limit): return "ファイルが大きすぎます (\(bytes / 1_048_576) MiB)。上限は \(limit / 1_048_576) MiB です"
        case .copyFailed(let detail): return "管理領域へのコピーに失敗しました: \(detail)"
        }
    }
}

public enum ImportLimits {
    public static let maxDurationMs = 14_400_000        // 4 時間
    public static let maxBytes = 2_147_483_648          // 2 GiB
}

public enum AudioProbe {
    /// 先頭バイトから候補コンテナを判定する。AudioFile の判定と両方が一致した場合だけ受理する。
    public static func sniffContainer(_ header: Data) -> String? {
        guard header.count >= 12 else { return nil }
        let bytes = [UInt8](header.prefix(12))
        if bytes[0..<4] == [0x52, 0x49, 0x46, 0x46], bytes[8..<12] == [0x57, 0x41, 0x56, 0x45] { return "wav" }
        if bytes[0..<4] == [0x66, 0x4C, 0x61, 0x43] { return "flac" }
        if bytes[4..<8] == [0x66, 0x74, 0x79, 0x70] { return "m4a" }
        if bytes[0..<3] == [0x49, 0x44, 0x33] { return "mp3" }
        if bytes[0] == 0xFF, (bytes[1] & 0xE0) == 0xE0 { return "mp3" }
        return nil
    }

    static func containerName(_ type: AudioFileTypeID) -> String? {
        switch type {
        case kAudioFileWAVEType, kAudioFileRF64Type: return "wav"
        case kAudioFileM4AType, kAudioFileMPEG4Type: return "m4a"
        case kAudioFileMP3Type: return "mp3"
        case kAudioFileFLACType: return "flac"
        default: return nil
        }
    }

    static func codecName(_ format: AudioStreamBasicDescription) -> String? {
        switch format.mFormatID {
        case kAudioFormatLinearPCM:
            if format.mFormatFlags & kAudioFormatFlagIsFloat != 0 { return format.mBitsPerChannel == 32 ? "pcm_f32le" : nil }
            switch format.mBitsPerChannel {
            case 16: return "pcm_s16le"
            case 24: return "pcm_s24le"
            default: return nil
            }
        case kAudioFormatMPEG4AAC, kAudioFormatMPEG4AAC_HE, kAudioFormatMPEG4AAC_LD, kAudioFormatMPEG4AAC_ELD, kAudioFormatMPEG4AAC_HE_V2:
            return "aac"
        case kAudioFormatMPEGLayer3: return "mp3"
        case kAudioFormatFLAC: return "flac"
        default: return nil
        }
    }

    public static func probe(_ url: URL) throws -> AudioProbeResult {
        guard FileManager.default.fileExists(atPath: url.path) else { throw ImportError.fileNotFound(url.lastPathComponent) }
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        let byteSize = (attributes[.size] as? NSNumber)?.intValue ?? 0
        if byteSize > ImportLimits.maxBytes { throw ImportError.tooLarge(bytes: byteSize, limitBytes: ImportLimits.maxBytes) }

        let handle = try FileHandle(forReadingFrom: url)
        let header = try handle.read(upToCount: 16) ?? Data()
        try handle.close()
        guard let sniffed = sniffContainer(header) else { throw ImportError.unsupportedFormat("先頭バイトが対応形式ではありません") }

        var fileID: AudioFileID?
        let openStatus = AudioFileOpenURL(url as CFURL, .readPermission, 0, &fileID)
        guard openStatus == noErr, let file = fileID else { throw ImportError.corrupted("AudioFileOpenURL \(openStatus)") }
        defer { AudioFileClose(file) }

        var fileType: AudioFileTypeID = 0
        var size = UInt32(MemoryLayout<AudioFileTypeID>.size)
        guard AudioFileGetProperty(file, kAudioFilePropertyFileFormat, &size, &fileType) == noErr,
              let container = containerName(fileType) else {
            throw ImportError.unsupportedFormat("コンテナを判定できません")
        }
        guard container == sniffed else { throw ImportError.unsupportedFormat("先頭バイト (\(sniffed)) とコンテナ (\(container)) が一致しません") }

        var format = AudioStreamBasicDescription()
        size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        guard AudioFileGetProperty(file, kAudioFilePropertyDataFormat, &size, &format) == noErr else {
            throw ImportError.corrupted("データ形式を取得できません")
        }
        guard let codec = codecName(format) else { throw ImportError.unsupportedFormat("codec 0x\(String(format.mFormatID, radix: 16))") }
        let allowed: [String: Set<String>] = ["wav": ["pcm_s16le", "pcm_s24le", "pcm_f32le"], "m4a": ["aac"], "mp3": ["mp3"], "flac": ["flac"]]
        guard allowed[container]?.contains(codec) == true else { throw ImportError.unsupportedFormat("\(container) 内の \(codec)") }

        var duration: Double = 0
        size = UInt32(MemoryLayout<Double>.size)
        guard AudioFileGetProperty(file, kAudioFilePropertyEstimatedDuration, &size, &duration) == noErr, duration.isFinite, duration > 0 else {
            throw ImportError.corrupted("再生時間を取得できません")
        }
        let durationMs = Int((duration * 1000).rounded())
        if durationMs > ImportLimits.maxDurationMs { throw ImportError.tooLong(durationMs: durationMs, limitMs: ImportLimits.maxDurationMs) }
        guard format.mSampleRate >= 8000, format.mChannelsPerFrame >= 1, format.mChannelsPerFrame <= 2 else {
            throw ImportError.unsupportedFormat("sample rate \(Int(format.mSampleRate)) / channels \(format.mChannelsPerFrame)")
        }
        return AudioProbeResult(container: container, codec: codec, sampleRate: Int(format.mSampleRate),
                                channels: Int(format.mChannelsPerFrame), durationMs: durationMs, byteSize: byteSize)
    }
}
