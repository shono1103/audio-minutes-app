// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "AudioMinutesCLI",
    platforms: [.macOS("14.2")],
    products: [.executable(name: "audio-minutes", targets: ["AudioMinutesCLI"])],
    dependencies: [.package(path: "../../packages/swift")],
    targets: [
        .executableTarget(
            name: "AudioMinutesCLI",
            dependencies: [
                .product(name: "ClientCore", package: "swift"),
                .product(name: "ImportCore", package: "swift"),
            ],
            linkerSettings: [.linkedFramework("AppKit")]
        ),
        .testTarget(name: "AudioMinutesCLITests", dependencies: [
            "AudioMinutesCLI",
            .product(name: "ClientCore", package: "swift"),
        ]),
    ]
)
