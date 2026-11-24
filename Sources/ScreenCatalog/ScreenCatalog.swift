import Foundation
import SwiftUI

/// The entry points an app uses to take part in a screen catalog run.
///
/// A catalog run is a launch with `-ScreenCatalogVariant <Feature>.<variant>` (show one registered
/// screen) or `-ScreenCatalogList` (list them), usually with `-ScreenCatalogPort <port>` so the
/// recording tool can talk to the app. Without those arguments, and in every Release build,
/// everything here is inert: `bootstrap` returns at once and `ScreenCatalogHost` shows the app.
public enum ScreenCatalog {

    /// `-ScreenCatalogVariant <Feature>.<variant>`: launch straight into one registered screen.
    public static let variantArgument = "-ScreenCatalogVariant"
    /// `-ScreenCatalogList`: launch into the list of registered screens.
    public static let listArgument = "-ScreenCatalogList"
    /// `-ScreenCatalogPort <port>`: serve the catalog protocol on `127.0.0.1:<port>` (simulator only).
    public static let portArgument = "-ScreenCatalogPort"
    /// The wire protocol version `/status` reports. The recording tool refuses any other.
    public static let protocolVersion = 1

    /// Whether this process was launched for the catalog. Key determinism hooks off this: a network
    /// client that throws instead of reaching a server, a fixed clock, paused animations.
    public static var isCatalogRun: Bool { launch.mode != .none }

    /// The `<Feature>.<variant>` this process was launched to show, if any. A fixture can branch on
    /// it when several variants share one view.
    public static var activeVariantName: String? {
        if case .variant(let name) = launch.mode { return name }
        return nil
    }

    // MARK: - Activity (the settle signal)

    /// Records that something happened. The recorder waits until no activity has been noted, and
    /// nothing begun is still in flight, for a quiet period before it takes the screenshot.
    /// Call it from anywhere: a store's action hook, a network client, an image loader.
    /// A no-op outside a catalog run.
    public static func noteActivity(_ label: @autoclosure () -> String = "") {
        guard isCatalogRun else { return }
        activity.note(label())
    }

    /// Marks work as in flight until the returned token ends (or is released). The recorder does
    /// not consider the screen settled while any token is open.
    @discardableResult
    public static func beginActivity(_ label: String = "") -> ActivityToken {
        guard isCatalogRun else { return ActivityToken(log: nil, label: label) }
        activity.begin(label)
        return ActivityToken(log: activity, label: label)
    }

    /// Runs `operation` as in-flight work: `beginActivity` before it, `end()` after, on every path.
    public static func withActivity<T>(
        _ label: String = "",
        isolation: isolated (any Actor)? = #isolation,
        _ operation: () async throws -> T
    ) async rethrows -> T {
        let token = beginActivity(label)
        defer { token.end() }
        return try await operation()
    }

    // MARK: - Bootstrap

    /// Call first thing in `App.init()` (or `application(_:didFinishLaunchingWithOptions:)`).
    ///
    /// On a catalog launch this builds the registry, resolves the requested variant (an unknown
    /// name is a `fatalError` naming it, which the recorder reports), applies `options` and starts
    /// the local server. On any other launch, and in Release builds, it does nothing and never
    /// calls `registry`.
    @MainActor
    public static func bootstrap(
        options: ScreenCatalogOptions = ScreenCatalogOptions(),
        registry: @escaping @MainActor () -> ScreenRegistry
    ) {
        #if DEBUG
        ScreenCatalogRuntime.shared.bootstrap(launch: launch, options: options, registry: registry)
        #endif
    }

    /// The view a catalog launch should show instead of the app: the requested variant, or the
    /// variant list. Nil on a normal launch. `ScreenCatalogHost` uses this; a UIKit app can put it
    /// in a `UIHostingController` itself.
    @MainActor
    public static var catalogRootView: AnyView? {
        #if DEBUG
        return ScreenCatalogRuntime.shared.rootView
        #else
        return nil
        #endif
    }

    // MARK: - Internal

    static let launch = ScreenCatalogLaunch(arguments: ProcessInfo.processInfo.arguments)
    static let activity = ActivityLog()
}

/// What `ScreenCatalog.bootstrap` does before a variant is built.
public struct ScreenCatalogOptions: Sendable {

    /// Turns UIKit and Core Animation animations off for the process, so a screenshot never lands
    /// mid-transition.
    public var disablesAnimations: Bool
    /// Clears the app's `UserDefaults` domain before the variant is built, so state persisted by an
    /// earlier variant (or an earlier run of the app) cannot leak into this one. Seed any defaults
    /// a variant needs in `prepare` or in its builder, which both run after the reset.
    public var resetsUserDefaults: Bool
    /// Runs after the reset and before the variant's builder: install fixtures, swap dependencies
    /// for fakes, register fonts.
    public var prepare: @MainActor @Sendable (ScreenVariant) -> Void

    public init(
        disablesAnimations: Bool = true,
        resetsUserDefaults: Bool = true,
        prepare: @escaping @MainActor @Sendable (ScreenVariant) -> Void = { _ in }
    ) {
        self.disablesAnimations = disablesAnimations
        self.resetsUserDefaults = resetsUserDefaults
        self.prepare = prepare
    }
}

/// Shows the catalog's view on a catalog launch and `content` otherwise. Make it the root of the
/// app's `WindowGroup`.
public struct ScreenCatalogHost<Content: View>: View {

    private let content: () -> Content

    public init(@ViewBuilder content: @escaping () -> Content) {
        self.content = content
    }

    public var body: some View {
        if let catalog = ScreenCatalog.catalogRootView {
            catalog
        } else {
            content()
        }
    }
}
