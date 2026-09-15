import Foundation
import XCTest

@testable import ImportCore

final class AudioProbeTests: XCTestCase {
    func testSniffsSupportedContainerHeaders() {
        XCTAssertEqual(AudioProbe.sniffContainer(Data("RIFF\0\0\0\0WAVE".utf8)), "wav")
        XCTAssertEqual(AudioProbe.sniffContainer(Data("fLaC\0\0\0\0\0\0\0\0".utf8)), "flac")
        XCTAssertEqual(AudioProbe.sniffContainer(Data([0, 0, 0, 24] + Array("ftypM4A ".utf8))), "m4a")
        XCTAssertEqual(AudioProbe.sniffContainer(Data("ID3\0\0\0\0\0\0\0\0\0".utf8)), "mp3")
    }

    func testRejectsUnknownOrShortHeaders() {
        XCTAssertNil(AudioProbe.sniffContainer(Data("not audio data".utf8)))
        XCTAssertNil(AudioProbe.sniffContainer(Data("RIFF".utf8)))
    }
}
