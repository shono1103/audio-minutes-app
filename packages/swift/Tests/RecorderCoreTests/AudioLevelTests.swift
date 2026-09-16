import AVFoundation
import Foundation
import XCTest

@testable import RecorderCore

final class AudioLevelTests: XCTestCase {
    private func floatBuffer(interleaved: Bool, channels: AVAudioChannelCount, frames: Int, fill: (UnsafeMutablePointer<Float>) -> Void) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: channels, interleaved: interleaved)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = AVAudioFrameCount(frames)
        fill(buffer.floatChannelData![0])
        return buffer
    }

    private func int16Buffer(interleaved: Bool, channels: AVAudioChannelCount, frames: Int, fill: (UnsafeMutablePointer<Int16>) -> Void) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: channels, interleaved: interleaved)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = AVAudioFrameCount(frames)
        fill(buffer.int16ChannelData![0])
        return buffer
    }

    private func int32Buffer(interleaved: Bool, channels: AVAudioChannelCount, frames: Int, fill: (UnsafeMutablePointer<Int32>) -> Void) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(commonFormat: .pcmFormatInt32, sampleRate: 48_000, channels: channels, interleaved: interleaved)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = AVAudioFrameCount(frames)
        fill(buffer.int32ChannelData![0])
        return buffer
    }

    // MARK: - 基本ケース

    func testSilentBufferIsZero() {
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 8) { data in
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
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 4) { data in
            for i in 0..<4 { data[i] = 0.5 }
        }
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), 0.5, accuracy: 0.0001)
    }

    /// 片側 0・片側 0.5 の期待 RMS は 0.5/sqrt(2)。
    func testFloat32MonoHalfSilentHalfLoud() {
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 8) { data in
            for i in 0..<4 { data[i] = 0 }
            for i in 4..<8 { data[i] = 0.5 }
        }
        let expected = Float(0.5 / 2.0.squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.0001)
    }

    // MARK: - stride を無視すると誤った結果になるケース

    /// interleaved stereo で前半フレームを無音・後半フレームを有音にする。全フレーム同一値だと
    /// stride を無視した誤った読み取りでも一致してしまうため、フレームごとに値を変える。
    func testFloat32InterleavedStereoRespectsStride() {
        let channels: AVAudioChannelCount = 2
        let frames = 8
        let buffer = floatBuffer(interleaved: true, channels: channels, frames: frames) { flat in
            for frame in 0..<frames {
                let value: Float = frame < 4 ? 0 : 0.6
                flat[frame * 2 + 0] = value
                flat[frame * 2 + 1] = value
            }
        }
        XCTAssertEqual(Int(buffer.stride), 2)
        // 全 16 サンプルのうち 8 個が 0、8 個が 0.6
        let expected = Float((8.0 * 0.36 / 16.0).squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.0001)
    }

    /// 左右で異なる信号を持つ interleaved buffer。両チャンネルの寄与が正しく合算されることを確認する。
    func testInt16InterleavedStereoDifferentChannels() {
        let channels: AVAudioChannelCount = 2
        let frames = 4
        let buffer = int16Buffer(interleaved: true, channels: channels, frames: frames) { flat in
            for frame in 0..<frames {
                flat[frame * 2 + 0] = 16_384    // 左: 0.5
                flat[frame * 2 + 1] = 0          // 右: 0
            }
        }
        // 8サンプル中 4つが 0.5、4つが 0
        let expected = Float((4.0 * 0.25 / 8.0).squareRoot())
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), expected, accuracy: 0.001)
    }

    func testInt16NegativeMaxAmplitude() {
        let buffer = int16Buffer(interleaved: false, channels: 1, frames: 4) { data in
            for i in 0..<4 { data[i] = Int16.min }
        }
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), 1.0, accuracy: 0.001)
    }

    func testInt32NonZeroIsNoLongerAlwaysZero() {
        let buffer = int32Buffer(interleaved: false, channels: 1, frames: 4) { data in
            for i in 0..<4 { data[i] = Int32.max / 2 }
        }
        XCTAssertEqual(PCMDownmixer.rmsLevel(buffer), 0.5, accuracy: 0.001)
    }

    func testFloat32NonFiniteSamplesAreExcludedAndResultStaysFinite() {
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 4) { data in
            data[0] = .nan
            data[1] = .infinity
            data[2] = 0.5
            data[3] = 0.5
        }
        let level = PCMDownmixer.rmsLevel(buffer)
        XCTAssertTrue(level.isFinite)
        XCTAssertEqual(level, 0.5, accuracy: 0.0001)
    }

    func testResultIsClampedToUnitRange() {
        // clamp の境界を確認するため、変換前の異常値 (実際には発生しないが防御的に) を模擬する。
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 2) { data in
            data[0] = 1.5
            data[1] = 1.5
        }
        XCTAssertLessThanOrEqual(PCMDownmixer.rmsLevel(buffer), 1.0)
    }

    func testInputBufferIsNotMutated() {
        let buffer = floatBuffer(interleaved: false, channels: 1, frames: 4) { data in
            for i in 0..<4 { data[i] = 0.25 }
        }
        _ = PCMDownmixer.rmsLevel(buffer)
        let data = buffer.floatChannelData![0]
        for i in 0..<4 { XCTAssertEqual(data[i], 0.25) }
    }
}
