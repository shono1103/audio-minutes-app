// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "AudioMinutesMac",
    platforms: [.macOS("14.2")],
    products: [
        .executable(name: "AudioMinutesApp", targets: ["AudioMinutesApp"]),
        .executable(name: "NativeBridge", targets: ["NativeBridge"]),
        .library(name: "BridgeCore", targets: ["BridgeCore"]),
    ],
    dependencies: [.package(path: "../../packages/swift")],
    targets: [
        .executableTarget(
            name: "AudioMinutesApp",
            dependencies: [
                "BridgeCore",
                .product(name: "ClientCore", package: "swift"),
                .product(name: "ImportCore", package: "swift"),
                .product(name: "RecorderCore", package: "swift"),
            ]
        ),
        .target(name: "BridgeCore"),
        .executableTarget(name: "NativeBridge", dependencies: ["BridgeCore"]),
        .testTarget(name: "BridgeCoreTests", dependencies: ["BridgeCore"]),
        .testTarget(
            name: "AudioMinutesAppTests",
            dependencies: [
                "AudioMinutesApp",
                "BridgeCore",
                .product(name: "ClientCore", package: "swift"),
            ]
        ),
    ]
)
