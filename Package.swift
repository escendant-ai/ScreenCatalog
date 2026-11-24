// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ScreenCatalog",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [
        .library(name: "ScreenCatalog", targets: ["ScreenCatalog"])
    ],
    targets: [
        .target(name: "ScreenCatalog"),
        .testTarget(name: "ScreenCatalogTests", dependencies: ["ScreenCatalog"])
    ],
    swiftLanguageModes: [.v6]
)
