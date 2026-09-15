// swift-tools-version: 5.10
// audio-minutes の macOS クライアント共通コア。View と CLI に依存しない。
import PackageDescription

let package = Package(
    name: "AudioMinutesCore",
    platforms: [.macOS("14.2")],
    products: [
        .library(name: "ClientCore", targets: ["ClientCore"]),
        .library(name: "ImportCore", targets: ["ImportCore"]),
        .library(name: "RecorderCore", targets: ["RecorderCore"]),
    ],
    targets: [
        .target(
            name: "ClientCore",
            swiftSettings: [.enableUpcomingFeature("StrictConcurrency")]
        ),
        .target(
            name: "ImportCore",
            dependencies: ["ClientCore"],
            linkerSettings: [.linkedFramework("AVFoundation"), .linkedFramework("AudioToolbox")]
        ),
        .target(
            name: "RecorderCore",
            dependencies: ["ClientCore"],
            linkerSettings: [
                .linkedFramework("CoreAudio"),
                .linkedFramework("AudioToolbox"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("AppKit"),
            ]
        ),
        .testTarget(name: "ClientCoreTests", dependencies: ["ClientCore"]),
        .testTarget(name: "ImportCoreTests", dependencies: ["ImportCore"]),
        .testTarget(name: "RecorderCoreTests", dependencies: ["RecorderCore"]),
    ]
)
