#if DEBUG
import Foundation
import Network
import os

/// The catalog protocol over HTTP on `127.0.0.1:<port>`: `/status`, `/variants`, `/activity` and
/// `/screenshot`, all GET. It listens on the loopback interface only and answers only requests
/// whose `Host` is that address, so a web page open in a browser on the same Mac cannot reach it by
/// rebinding a name to 127.0.0.1. docs/protocol.md is the reference.
@MainActor
final class CatalogServer {

    /// What the routes report, read fresh for every request.
    struct Model {
        /// `variant` or `list`.
        let mode: String
        let variant: String?
        let shown: Bool
        let variants: [ScreenVariant]
    }

    struct Status: Encodable {
        let version: Int
        let bundle: String
        let pid: Int32
        let mode: String
        let variant: String?
        let shown: Bool
        let routes: [String]

        enum CodingKeys: String, CodingKey {
            case version = "protocol"
            case bundle, pid, mode, variant, shown, routes
        }
    }

    struct Variants: Encodable {
        struct Row: Encodable {
            let name: String
            let feature: String
        }

        let version: Int
        let variants: [Row]

        enum CodingKeys: String, CodingKey {
            case version = "protocol"
            case variants
        }
    }

    private let port: UInt16
    private let activity: ActivityLog
    private let model: @MainActor () -> Model
    private let queue = DispatchQueue(label: "screencatalog.server")
    private var listener: NWListener?
    private let logger = Logger(subsystem: "ScreenCatalog", category: "server")

    init(port: UInt16, activity: ActivityLog, model: @escaping @MainActor () -> Model) {
        self.port = port
        self.activity = activity
        self.model = model
    }

    func start() {
        guard let endpointPort = NWEndpoint.Port(rawValue: port) else { return }
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: endpointPort)
        parameters.allowLocalEndpointReuse = true

        do {
            let listener = try NWListener(using: parameters)
            listener.newConnectionHandler = { [weak self, queue] connection in
                connection.start(queue: queue)
                guard let self else {
                    connection.cancel()
                    return
                }
                Self.receive(on: Peer(connection: connection), buffer: Data(), server: self, queue: queue)
            }
            listener.stateUpdateHandler = { [logger, port] state in
                logger.notice("ScreenCatalog server on 127.0.0.1:\(port, privacy: .public): \(String(describing: state), privacy: .public)")
            }
            listener.start(queue: queue)
            self.listener = listener
        } catch {
            logger.error("ScreenCatalog server failed to start: \(error.localizedDescription, privacy: .public)")
        }
    }

    func stop() {
        listener?.cancel()
        listener = nil
    }

    /// The response to a request, given everything before its blank line.
    func respond(to head: String) -> Data {
        let lines = head.split(separator: "\r\n").map(String.init)
        let hostHeader = lines.dropFirst().compactMap { line -> String? in
            guard line.lowercased().hasPrefix("host:") else { return nil }
            return String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)
        }.first
        let allowedHosts: Set<String> = ["127.0.0.1:\(port)", "localhost:\(port)"]
        guard let hostHeader, allowedHosts.contains(hostHeader.lowercased()) else {
            return HTTPResponse.json(403, ["error": "forbidden host"])
        }

        switch CatalogRoute(requestLine: lines.first ?? "") {
        case .read(let path, let query):
            return read(path, query: query)
        case .notAllowed:
            return HTTPResponse.json(405, ["error": CatalogRoute.notAllowedMessage])
        }
    }

    // MARK: - Private

    /// `NWConnection` is only ever touched on `queue`; the box lets it cross into the task that
    /// asks the main actor for the response.
    private struct Peer: @unchecked Sendable {
        let connection: NWConnection
    }

    /// Network.framework wants every `NWConnection` call on the queue passed to `start(queue:)`.
    /// Only `respond` hops to the main actor; send and cancel stay on `queue`.
    private nonisolated static func receive(on peer: Peer, buffer: Data, server: CatalogServer, queue: DispatchQueue) {
        let limit = 64 * 1024
        peer.connection.receive(minimumIncompleteLength: 1, maximumLength: limit) { data, _, isComplete, error in
            var buffer = buffer
            if let data { buffer.append(data) }

            if let headerEnd = buffer.range(of: Data("\r\n\r\n".utf8)) {
                let head = String(bytes: buffer[..<headerEnd.lowerBound], encoding: .utf8) ?? ""
                Task {
                    let response = await server.respond(to: head)
                    queue.async {
                        peer.connection.send(content: response, completion: .contentProcessed { _ in peer.connection.cancel() })
                    }
                }
            } else if isComplete || error != nil || buffer.count > limit {
                peer.connection.cancel()
            } else {
                receive(on: peer, buffer: buffer, server: server, queue: queue)
            }
        }
    }

    private func read(_ path: String, query: [String: String]) -> Data {
        let model = model()
        switch path {
        case "/", "/status":
            return HTTPResponse.json(200, Status(
                version: ScreenCatalog.protocolVersion,
                bundle: Bundle.main.bundleIdentifier ?? "",
                pid: ProcessInfo.processInfo.processIdentifier,
                mode: model.mode,
                variant: model.variant,
                shown: model.shown,
                routes: CatalogRoute.routes
            ))
        case "/variants":
            return HTTPResponse.json(200, Variants(
                version: ScreenCatalog.protocolVersion,
                variants: model.variants.map { Variants.Row(name: $0.name, feature: $0.feature) }
            ))
        case "/activity":
            return HTTPResponse.json(200, activity.snapshot(after: query["since"].flatMap(Int.init) ?? 0))
        case "/screenshot":
            return Screenshot.response()
        default:
            return HTTPResponse.json(404, ["error": "unknown route", "routes": CatalogRoute.routes.joined(separator: " ")])
        }
    }
}
#endif
