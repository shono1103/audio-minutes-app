import AVFoundation
import Foundation
import XCTest

@testable import RecorderCore

final class AudioLevelTests: XCTestCase {
    private func floatBuffer(interleaved: Bool, channels: AVAudioChannelCount, frames: Int, fill: (AVAudioPCMBuffer) -> Void) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: channels, interleaved: interleaved)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = AVAudioFrameCount(frames)
        fill(buffer)
        return buffer
    }

    private func int16Buffer(interleaved: Bool, channels: AVAudioChannelCount, frames: Int, fill: (AVAudioPCMBuffer) -> Void) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: channels, interleaved: interleaved)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = AVAudioFrameCount(frames)
        fill(buffer)
        return buffer
    }

    private func int32Buffer(interleaved: Bool, channels: AVAudioChannelCount, frames: Int, fill: (AVAudioPCMBuffer) -> Void) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(commonFormat: .pcmFormatInt32, sampleRate: 48_000, channels: channels, interleaved: interleaved)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = AVAudioFrameCount(frames)
        fill(buffer)
        return buffer
    }

    // MARK: - 基本ケース

    func testSilentBufferIsZero() {
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 8) { buffer in
            let data = buffer.floatChannelData![0]
            for i in 0..<8 { data[i] = 0 }
        }
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), 0)
    }

    func testEmptyFrameLengthIsZero() {
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 8)!
        buffer.frameLength = 0
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), 0)
    }

    func testFloat32MonoConstantAmplitude() {
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 4) { buffer in
            let data = buffer.floatChannelData![0]
            for i in 0..<4 { data[i] = 0.5 }
        }
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), 0.5, accuracy: 0.0001)
    }

    /// 片側 0・片側 0.5 の期待 RMS は 0.5/sqrt(2)。
    func testFloat32MonoHalfSilentHalfLoud() {
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 8) { buffer in
            let data = buffer.floatChannelData![0]
            for i in 0..<4 { data[i] = 0 }
            for i in 4..<8 { data[i] = 0.5 }
        }
        let expected = Float(0.5 / 2.0.squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.0001)
    }

    // MARK: - interleaved: stride を無視すると誤った結果になるケース
    //
    // 全フレーム同一値だと stride を無視した誤った読み取りでも一致してしまうため、前半
    // フレームを無音・後半フレームを有音にしてフレームごとに値を変える。書き込み・検証とも
    // `buffer.stride` (Apple の AVAudioPCMBuffer が提供する実メモリレイアウト) をそのまま
    // 使うため、実装側の読み取り式 (`frame * stride`) と偶然一致するテストにはならない。

    func testFloat32InterleavedStereoRespectsStride() {
        let frames = 8
        let buffer = floatBuffer(interleaved: true, channels: 2, frames: frames) { buffer in
            let stride = Int(buffer.stride)
            let data = buffer.floatChannelData!
            for frame in 0..<frames {
                let value: Float = frame < 4 ? 0 : 0.6
                data[0][frame * stride] = value
                data[1][frame * stride] = value
            }
        }
        XCTAssertEqual(Int(buffer.stride), 2)
        // 全 16 サンプルのうち 8 個が 0、8 個が 0.6
        let expected = Float((8.0 * 0.36 / 16.0).squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.0001)
    }

    func testInt16InterleavedStereoRespectsStride() {
        let frames = 8
        let buffer = int16Buffer(interleaved: true, channels: 2, frames: frames) { buffer in
            let stride = Int(buffer.stride)
            let data = buffer.int16ChannelData!
            for frame in 0..<frames {
                let value: Int16 = frame < 4 ? 0 : 16_384 // 0.5
                data[0][frame * stride] = value
                data[1][frame * stride] = value
            }
        }
        XCTAssertEqual(Int(buffer.stride), 2)
        // 全 16 サンプルのうち 8 個が 0、8 個が 0.5
        let expected = Float((8.0 * 0.25 / 16.0).squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.001)
    }

    func testInt32InterleavedStereoRespectsStride() {
        let frames = 8
        let buffer = int32Buffer(interleaved: true, channels: 2, frames: frames) { buffer in
            let stride = Int(buffer.stride)
            let data = buffer.int32ChannelData!
            for frame in 0..<frames {
                let value: Int32 = frame < 4 ? 0 : 1_073_741_824 // 0.5
                data[0][frame * stride] = value
                data[1][frame * stride] = value
            }
        }
        XCTAssertEqual(Int(buffer.stride), 2)
        let expected = Float((8.0 * 0.25 / 16.0).squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.001)
    }

    /// 左右で異なる信号を持つ interleaved buffer。両チャンネルの寄与が正しく合算されることを確認する。
    func testInt16InterleavedStereoDifferentChannels() {
        let frames = 4
        let buffer = int16Buffer(interleaved: true, channels: 2, frames: frames) { buffer in
            let stride = Int(buffer.stride)
            let data = buffer.int16ChannelData!
            for frame in 0..<frames {
                data[0][frame * stride] = 16_384 // 左: 0.5
                data[1][frame * stride] = 0       // 右: 0
            }
        }
        // 8サンプル中 4つが 0.5、4つが 0
        let expected = Float((4.0 * 0.25 / 8.0).squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.001)
    }

    // MARK: - non-interleaved (チャンネルごとに独立したメモリ、stride == 1)

    func testFloat32NonInterleavedStereoDifferentChannels() {
        let frames = 4
        let buffer = floatBuffer(interleaved: false, channels: 2, frames: frames) { buffer in
            XCTAssertEqual(Int(buffer.stride), 1)
            let data = buffer.floatChannelData!
            for frame in 0..<frames {
                data[0][frame] = 0.5 // 左
                data[1][frame] = 0   // 右
            }
        }
        let expected = Float((4.0 * 0.25 / 8.0).squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.0001)
    }

    func testInt16NonInterleavedStereoDifferentChannels() {
        let frames = 4
        let buffer = int16Buffer(interleaved: false, channels: 2, frames: frames) { buffer in
            XCTAssertEqual(Int(buffer.stride), 1)
            let data = buffer.int16ChannelData!
            for frame in 0..<frames {
                data[0][frame] = 16_384 // 左: 0.5
                data[1][frame] = 0       // 右
            }
        }
        let expected = Float((4.0 * 0.25 / 8.0).squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.001)
    }

    func testInt32NonInterleavedStereoDifferentChannels() {
        let frames = 4
        let buffer = int32Buffer(interleaved: false, channels: 2, frames: frames) { buffer in
            XCTAssertEqual(Int(buffer.stride), 1)
            let data = buffer.int32ChannelData!
            for frame in 0..<frames {
                data[0][frame] = 1_073_741_824 // 左: 0.5
                data[1][frame] = 0              // 右
            }
        }
        let expected = Float((4.0 * 0.25 / 8.0).squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.001)
    }

    // MARK: - 極値・異常値

    func testInt16NegativeMaxAmplitude() {
        let buffer = int16Buffer(interleaved: false, channels: 1, frames: 4) { buffer in
            let data = buffer.int16ChannelData![0]
            for i in 0..<4 { data[i] = Int16.min }
        }
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), 1.0, accuracy: 0.001)
    }

    func testInt32NonZeroIsNoLongerAlwaysZero() {
        let buffer = int32Buffer(interleaved: false, channels: 1, frames: 4) { buffer in
            let data = buffer.int32ChannelData![0]
            for i in 0..<4 { data[i] = Int32.max / 2 }
        }
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), 0.5, accuracy: 0.001)
    }

    /// 非有限サンプルは 0 として扱い、分母 (frames * channels) からは除外しない。
    /// [NaN, Inf, 0.5, 0.5] の期待 RMS は sqrt((0+0+0.25+0.25)/4) = sqrt(0.125)。
    func testNonFiniteSamplesContributeZeroButCountTowardDenominator() {
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 4) { buffer in
            let data = buffer.floatChannelData![0]
            data[0] = .nan
            data[1] = .infinity
            data[2] = 0.5
            data[3] = 0.5
        }
        let level = PCMDownmixer.rmsLevel(buffer)
        let expected = Float((0.5 / 4.0).squareRoot())
        XCTAssertTrue(level.isFinite)
        XCTAssertEqual(level, expected, accuracy: 0.0001)
    }

    func testAllNonFiniteSamplesResultInZero() {
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 4) { buffer in
            let data = buffer.floatChannelData![0]
            data[0] = .nan
            data[1] = .infinity
            data[2] = -.infinity
            data[3] = .nan
        }
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), 0)
    }

    func testResultIsClampedToUnitRange() {
        // clamp の境界を確認するため、変換前の異常値 (実際には発生しないが防御的に) を模擬する。
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 2) { buffer in
            let data = buffer.floatChannelData![0]
            data[0] = 1.5
            data[1] = 1.5
        }
        XCTAssertLessThanOrEqual(PCMDownmixer.rmsLevel(buffer), 1.0)
    }

    func testInputBufferIsNotMutated() {
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 4) { buffer in
            let data = buffer.floatChannelData![0]
            for i in 0..<4 { data[i] = 0.25 }
        }
        _ = PCMDownmixer.rmsLevel(buffer)
        let data = buffer.floatChannelData![0]
        for i in 0..<4 { XCTAssertEqual(data[i], 0.25) }
    }
}
