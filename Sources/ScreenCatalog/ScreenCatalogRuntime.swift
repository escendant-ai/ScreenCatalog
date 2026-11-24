#if DEBUG
import Foundation
import QuartzCore
import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// The state of a catalog launch: what to show, whether it is on screen yet, and the server.
@MainActor
final class ScreenCatalogRuntime {

    static let shared = ScreenCatalogRuntime()

    enum Presentation {
        /// A normal launch: show the app.
        case app
        case list([ScreenVariant])
        case variant(ScreenVariant)
    }

    private(set) var presentation: Presentation = .app
    private(set) var variants: [ScreenVariant] = []
    /// True one run-loop turn after the catalog's view first appeared.
    private(set) var isShown = false
    private var builtRootView: AnyView?
    private var server: CatalogServer?

    func bootstrap(launch: ScreenCatalogLaunch, options: ScreenCatalogOptions, registry: @MainActor () -> ScreenRegistry) {
        guard launch.mode != .none, case .app = presentation else { return }

        let registry = registry()
        let issues = registry.issues()
        guard issues.isEmpty else {
            fatalError("ScreenCatalog: " + issues.map(\.description).joined(separator: "; "))
        }
        variants = registry.variants

        switch launch.mode {
        case .none:
            return
        case .list:
            presentation = .list(variants)
        case .variant(let name):
            guard let variant = registry.variant(named: name) else {
                fatalError("ScreenCatalog: no variant named '\(name)' among the \(variants.count) registered")
            }
            if options.disablesAnimations {
                Self.disableAnimations()
            }
            if options.resetsUserDefaults, let domain = Bundle.main.bundleIdentifier {
                UserDefaults.standard.removePersistentDomain(forName: domain)
            }
            options.prepare(variant)
            presentation = .variant(variant)
        }

        #if targetEnvironment(simulator)
        if let port = launch.port {
            startServer(port: port)
        }
        #endif
    }

    /// The catalog's root view, built once: a variant's builder runs a single time per process.
    var rootView: AnyView? {
        if let builtRootView { return builtRootView }
        let view: AnyView
        switch presentation {
        case .app:
            return nil
        case .list(let variants):
            view = AnyView(VariantListView(variants: variants).onAppear { [weak self] in self?.markShown() })
        case .variant(let variant):
            view = AnyView(
                variant.make()
                    .transaction { $0.animation = nil }
                    .onAppear { [weak self] in self?.markShown() }
            )
        }
        builtRootView = view
        return view
    }

    func startServer(port: UInt16) {
        let server = CatalogServer(port: port, activity: ScreenCatalog.activity) { [weak self] in
            self?.model ?? CatalogServer.Model(mode: "list", variant: nil, shown: false, variants: [])
        }
        server.start()
        self.server = server
    }

    private var model: CatalogServer.Model {
        switch presentation {
        case .variant(let variant):
            CatalogServer.Model(mode: "variant", variant: variant.name, shown: isShown, variants: variants)
        case .list, .app:
            CatalogServer.Model(mode: "list", variant: nil, shown: isShown, variants: variants)
        }
    }

    private func markShown() {
        // One turn later, so the first frame has been committed before the recorder is told.
        DispatchQueue.main.async { [weak self] in
            self?.isShown = true
        }
    }

    private static func disableAnimations() {
        #if canImport(UIKit)
        UIView.setAnimationsEnabled(false)
        #endif
        CATransaction.setDisableActions(true)
    }
}

/// What a `-ScreenCatalogList` launch shows: the registered names, grouped by feature. Read-only;
/// a variant is chosen by launching with `-ScreenCatalogVariant`.
private struct VariantListView: View {

    let variants: [ScreenVariant]

    private var features: [(name: String, variants: [ScreenVariant])] {
        var order: [String] = []
        var groups: [String: [ScreenVariant]] = [:]
        for variant in variants {
            if groups[variant.feature] == nil { order.append(variant.feature) }
            groups[variant.feature, default: []].append(variant)
        }
        return order.map { ($0, groups[$0] ?? []) }
    }

    var body: some View {
        List {
            ForEach(features, id: \.name) { feature in
                Section(feature.name) {
                    ForEach(feature.variants) { variant in
                        Text(variant.name).font(.body.monospaced())
                    }
                }
            }
        }
    }
}
#endif
