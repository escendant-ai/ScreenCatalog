#if DEBUG
import Foundation

/// What an HTTP request line asks the catalog server for. The server only reads: every route is a GET.
enum CatalogRoute: Equatable {

    /// `GET <path>`: one of the routes, or an unknown one.
    case read(path: String, query: [String: String])
    /// Any other method, or a line that does not parse.
    case notAllowed

    static let notAllowedMessage = "read-only server: GET only"
    static let routes = ["/status", "/variants", "/activity?since=", "/screenshot"]

    init(requestLine: String) {
        let parts = requestLine.split(separator: " ")
        guard parts.count >= 2, parts[0] == "GET", let components = URLComponents(string: String(parts[1])) else {
            self = .notAllowed
            return
        }

        let query = Dictionary(
            (components.queryItems ?? []).map { ($0.name, $0.value ?? "") },
            uniquingKeysWith: { _, last in last }
        )
        self = .read(path: components.path, query: query)
    }
}
#endif
