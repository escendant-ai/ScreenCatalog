import Foundation

/// What the launch arguments ask for. Always `.none` in a Release build, whatever was passed.
struct ScreenCatalogLaunch: Equatable, Sendable {

    enum Mode: Equatable, Sendable {
        /// A normal launch.
        case none
        /// `-ScreenCatalogList`
        case list
        /// `-ScreenCatalogVariant <name>`
        case variant(String)
    }

    let mode: Mode
    /// `-ScreenCatalogPort <port>`; nil when absent, not a port, or not a catalog launch.
    let port: UInt16?

    init(arguments: [String]) {
        #if DEBUG
        if let name = Self.value(after: ScreenCatalog.variantArgument, in: arguments) {
            mode = .variant(name)
        } else if arguments.contains(ScreenCatalog.listArgument) {
            mode = .list
        } else {
            mode = .none
        }
        if mode == .none {
            port = nil
        } else {
            port = Self.value(after: ScreenCatalog.portArgument, in: arguments).flatMap(UInt16.init).flatMap { $0 == 0 ? nil : $0 }
        }
        #else
        mode = .none
        port = nil
        #endif
    }

    /// The argument following `flag`, unless it is missing or is itself a flag.
    private static func value(after flag: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1) else { return nil }
        let value = arguments[index + 1]
        return value.isEmpty || value.hasPrefix("-") ? nil : value
    }
}
