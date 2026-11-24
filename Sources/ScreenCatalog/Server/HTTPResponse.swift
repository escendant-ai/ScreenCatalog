#if DEBUG
import Foundation

/// A whole HTTP/1.1 response as bytes. Every response closes the connection.
enum HTTPResponse {

    static func json(_ status: Int, _ value: some Encodable) -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        encoder.dateEncodingStrategy = .iso8601
        let body = (try? encoder.encode(value)) ?? Data("{}".utf8)
        return make(status, contentType: "application/json", body: body)
    }

    static func make(_ status: Int, contentType: String, body: Data, headers: [String: String] = [:]) -> Data {
        let reason = [
            200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            503: "Service Unavailable"
        ][status] ?? "Error"
        let extra = headers.sorted { $0.key < $1.key }.map { "\($0.key): \($0.value)" }
        let head = ([
            "HTTP/1.1 \(status) \(reason)",
            "Content-Type: \(contentType)",
            "Content-Length: \(body.count)",
            "Connection: close"
        ] + extra).joined(separator: "\r\n") + "\r\n\r\n"
        return Data(head.utf8) + body
    }
}
#endif
