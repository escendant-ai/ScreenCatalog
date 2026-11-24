import SwiftUI
import XCTest
@testable import ScreenCatalog

final class RegistryTests: XCTestCase {

    func testAddRegistersEveryNameInOrder() {
        var registry = ScreenRegistry()
        registry.add("Home.default") { Text("home") }
        registry.add("Profile.empty", "Profile.error") { Text("profile") }

        XCTAssertEqual(registry.variants.map(\.name), ["Home.default", "Profile.empty", "Profile.error"])
        XCTAssertEqual(registry.variant(named: "Profile.error")?.name, "Profile.error")
        XCTAssertNil(registry.variant(named: "Profile.missing"))
        XCTAssertEqual(registry.issues(), [])
    }

    @MainActor
    func testAddPassingNameHandsTheBuilderItsOwnName() {
        let seen = Box()
        var registry = ScreenRegistry()
        registry.addPassingName("Tour.step1", "Tour.step2") { name in
            seen.names.append(name)
            return Text(name)
        }

        _ = registry.variant(named: "Tour.step2")?.make()
        _ = registry.variant(named: "Tour.step1")?.make()
        XCTAssertEqual(seen.names, ["Tour.step2", "Tour.step1"])
    }

    func testFeatureAndVariantSplitOnTheFirstDot() {
        let variant = ScreenVariant(name: "Settings.account.signedOut") { AnyView(EmptyView()) }
        XCTAssertEqual(variant.feature, "Settings")
        XCTAssertEqual(variant.variant, "account.signedOut")
        XCTAssertEqual(variant.id, "Settings.account.signedOut")
    }

    func testContentsOfAnotherRegistryAreAppended() {
        var feature = ScreenRegistry()
        feature.add("Feed.default") { EmptyView() }
        var registry = ScreenRegistry()
        registry.add("Home.default") { EmptyView() }
        registry.add(contentsOf: feature)
        XCTAssertEqual(registry.variants.map(\.name), ["Home.default", "Feed.default"])
    }

    func testIssuesReportDuplicatesAndMalformedNamesOnce() {
        var registry = ScreenRegistry()
        registry.add("Home.default", "Home.default", "Home.default") { EmptyView() }
        registry.add("NoVariant", "Bad name.x", ".x", "Trailing.", "Has/slash.x", "Double..dot") { EmptyView() }

        XCTAssertEqual(registry.issues(), [
            .duplicate("Home.default"),
            .malformed("NoVariant"), .malformed("Bad name.x"), .malformed(".x"), .malformed("Trailing."),
            .malformed("Has/slash.x"), .malformed("Double..dot")
        ])
        XCTAssertTrue(ScreenRegistry.isWellFormed("Feature_1.variant-2.state"))
    }
}

/// A main-actor mutable box a `@Sendable` builder can write to.
@MainActor
private final class Box: Sendable {
    var names: [String] = []
}

final class LaunchTests: XCTestCase {

    func testANormalLaunchIsNotACatalogRun() {
        let launch = ScreenCatalogLaunch(arguments: ["App", "-SomethingElse", "YES", "-ScreenCatalogPort", "8766"])
        XCTAssertEqual(launch.mode, .none)
        XCTAssertNil(launch.port, "a port alone does not start anything")
    }

    func testVariantWithPort() {
        let launch = ScreenCatalogLaunch(arguments: ["App", "-ScreenCatalogVariant", "Home.default", "-ScreenCatalogPort", "8766"])
        XCTAssertEqual(launch.mode, .variant("Home.default"))
        XCTAssertEqual(launch.port, 8766)
    }

    func testListAndABadPort() {
        XCTAssertEqual(ScreenCatalogLaunch(arguments: ["App", "-ScreenCatalogList"]).mode, .list)
        for bad in ["0", "70000", "abc", "-1"] {
            let launch = ScreenCatalogLaunch(arguments: ["App", "-ScreenCatalogList", "-ScreenCatalogPort", bad])
            XCTAssertNil(launch.port, bad)
        }
    }

    func testAVariantWinsOverTheListAndNeedsAName() {
        let both = ScreenCatalogLaunch(arguments: ["App", "-ScreenCatalogList", "-ScreenCatalogVariant", "Home.empty"])
        XCTAssertEqual(both.mode, .variant("Home.empty"))

        XCTAssertEqual(ScreenCatalogLaunch(arguments: ["App", "-ScreenCatalogVariant"]).mode, .none)
        let flagNext = ScreenCatalogLaunch(arguments: ["App", "-ScreenCatalogVariant", "-ScreenCatalogPort", "8766"])
        XCTAssertEqual(flagNext.mode, .none, "the next flag is not a variant name")
    }

    func testTheTestProcessItselfIsNotACatalogRun() {
        XCTAssertFalse(ScreenCatalog.isCatalogRun)
        XCTAssertNil(ScreenCatalog.activeVariantName)
        ScreenCatalog.noteActivity("ignored")
        ScreenCatalog.beginActivity("ignored").end()
        XCTAssertEqual(ScreenCatalog.activity.snapshot().seq, 0, "outside a catalog run nothing is recorded")
    }

    @MainActor
    func testBootstrapOnANormalLaunchNeverBuildsTheRegistry() {
        ScreenCatalog.bootstrap { XCTFail("the registry is only built on a catalog launch"); return ScreenRegistry() }
        XCTAssertNil(ScreenCatalog.catalogRootView)
    }
}
