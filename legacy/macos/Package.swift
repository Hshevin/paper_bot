// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PaperHarbor",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "PaperHarbor", targets: ["PaperHarbor"]),
        .executable(name: "PaperHarborAgent", targets: ["PaperHarborAgent"])
    ],
    targets: [
        .target(name: "PaperCore"),
        .executableTarget(name: "PaperHarbor", dependencies: ["PaperCore"]),
        .executableTarget(name: "PaperHarborAgent", dependencies: ["PaperCore"]),
        .executableTarget(name: "PaperCoreChecks", dependencies: ["PaperCore"], path: "Tests/PaperCoreTests")
    ],
    swiftLanguageModes: [.v5]
)
