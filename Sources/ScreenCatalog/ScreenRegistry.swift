import SwiftUI

/// One screen of the catalog: a `<Feature>.<variant>` name and how to build its view.
public struct ScreenVariant: Identifiable, Sendable {

    /// `<Feature>.<variant>`, e.g. `Profile.empty`. The screenshot's file name.
    public let name: String
    /// Builds the screen. Called once, on a launch that names this variant.
    public let make: @MainActor @Sendable () -> AnyView

    public var id: String { name }

    /// The part before the first `.`: what the tool groups and filters by (`--feature`).
    public var feature: String { name.split(separator: ".", maxSplits: 1).first.map(String.init) ?? name }
    /// The part after the first `.`.
    public var variant: String {
        let parts = name.split(separator: ".", maxSplits: 1, omittingEmptySubsequences: false)
        return parts.count == 2 ? String(parts[1]) : ""
    }

    public init(name: String, make: @escaping @MainActor @Sendable () -> AnyView) {
        self.name = name
        self.make = make
    }
}

/// The app's list of catalog screens, in the order they were added (the order the tool records in).
///
/// ```swift
/// var registry = ScreenRegistry()
/// registry.add("Profile.default") { ProfileView(model: .fixture) }
/// registry.add("Profile.empty", "Profile.error") { ProfileView(model: .fixture) }   // a fake tells them apart
/// registry.addPassingName("Tour.step1", "Tour.step2") { name in TourView(step: name) }
/// ```
public struct ScreenRegistry: Sendable {

    public private(set) var variants: [ScreenVariant] = []

    public init() {}

    /// Registers one builder under each of `names`. Several names for one builder suit variants a
    /// fixture or fake tells apart by `ScreenCatalog.activeVariantName`.
    public mutating func add<V: View>(_ names: String..., @ViewBuilder make: @escaping @MainActor @Sendable () -> V) {
        for name in names {
            variants.append(ScreenVariant(name: name) { AnyView(make()) })
        }
    }

    /// As `add`, with the variant's own name passed to the builder.
    public mutating func addPassingName<V: View>(
        _ names: String...,
        @ViewBuilder make: @escaping @MainActor @Sendable (_ name: String) -> V
    ) {
        for name in names {
            variants.append(ScreenVariant(name: name) { AnyView(make(name)) })
        }
    }

    /// Appends another registry's variants: lets each feature build its own list.
    public mutating func add(contentsOf other: ScreenRegistry) {
        variants.append(contentsOf: other.variants)
    }

    public func variant(named name: String) -> ScreenVariant? {
        variants.first { $0.name == name }
    }

    // MARK: - Validation

    public enum Issue: Equatable, Sendable, CustomStringConvertible {
        /// The name is registered more than once; only the first could ever be launched.
        case duplicate(String)
        /// The name is not `<Feature>.<variant>` in letters, digits, `_`, `-` and `.`.
        case malformed(String)

        public var description: String {
            switch self {
            case .duplicate(let name): "'\(name)' is registered more than once"
            case .malformed(let name): "'\(name)' is not <Feature>.<variant> (letters, digits, '_', '-', '.')"
            }
        }
    }

    /// What is wrong with the registered names; empty when nothing is. `bootstrap` stops a catalog
    /// launch on any issue, and a unit test asserting this is empty catches it sooner.
    public func issues() -> [Issue] {
        var seen = Set<String>()
        var reported = Set<String>()
        var issues: [Issue] = []
        for variant in variants {
            if !Self.isWellFormed(variant.name), reported.insert("m:" + variant.name).inserted {
                issues.append(.malformed(variant.name))
            }
            if !seen.insert(variant.name).inserted, reported.insert("d:" + variant.name).inserted {
                issues.append(.duplicate(variant.name))
            }
        }
        return issues
    }

    static func isWellFormed(_ name: String) -> Bool {
        let parts = name.split(separator: ".", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else { return false }
        let allowed = { (character: Character) in
            character.isASCII && (character.isLetter || character.isNumber || character == "_" || character == "-")
        }
        return parts[0].allSatisfy(allowed) && parts[1].allSatisfy { allowed($0) || $0 == "." }
            && !name.hasSuffix(".") && !name.contains("..")
    }
}
