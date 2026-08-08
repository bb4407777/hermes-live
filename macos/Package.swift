// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "HermesLiveMac",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "HermesLiveMac", path: "Sources/HermesLiveMac")
    ]
)
