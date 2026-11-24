import SwiftUI
import XCTest
@testable import ScreenCatalog

final class ActivityTests: XCTestCase {

    func testNotesGrowTheSequenceAndAreServedAfterACursor() {
        let log = ActivityLog()
        XCTAssertEqual(log.snapshot(), ActivityLog.Snapshot(seq: 0, busy: 0, entries: []))

        log.note("first")
        log.note("second")
        log.note("third")
        let all = log.snapshot()
        XCTAssertEqual(all.seq, 3)
        XCTAssertEqual(all.busy, 0)
        XCTAssertEqual(all.entries.map(\.label), ["first", "second", "third"])
        XCTAssertEqual(log.snapshot(after: 2).entries.map(\.seq), [3])
        XCTAssertEqual(log.snapshot(after: 3).entries, [])
    }

    func testBeginAndEndCountWorkInFlightAndNeverGoNegative() {
        let log = ActivityLog()
        log.begin("load")
        log.begin("")
        XCTAssertEqual(log.snapshot().busy, 2)
        log.end("load")
        log.end("")
        log.end("stray")
        let snapshot = log.snapshot()
        XCTAssertEqual(snapshot.busy, 0)
        XCTAssertEqual(snapshot.seq, 5, "begin and end both count as activity")
        XCTAssertEqual(snapshot.entries.map(\.label), ["begin load", "begin", "end load", "end", "end stray"])
    }

    func testOnlyTheNewestEntriesAreKept() {
        let log = ActivityLog(capacity: 3)
        for index in 1...10 { log.note("n\(index)") }
        let snapshot = log.snapshot()
        XCTAssertEqual(snapshot.seq, 10)
        XCTAssertEqual(snapshot.entries.map(\.seq), [8, 9, 10])
    }

    func testATokenEndsOnceAndWhenReleased() {
        let log = ActivityLog()
        log.begin("a")
        let token = ActivityToken(log: log, label: "a")
        token.end()
        token.end()
        XCTAssertEqual(log.snapshot().busy, 0)
        XCTAssertEqual(log.snapshot().seq, 2, "the second end did nothing")

        log.begin("b")
        var dropped: ActivityToken? = ActivityToken(log: log, label: "b")
        XCTAssertNotNil(dropped)
        dropped = nil
        XCTAssertEqual(log.snapshot().busy, 0, "a released token ends itself")
    }

    func testConcurrentNotesAreAllCounted() async {
        let log = ActivityLog(capacity: 10_000)
        await withTaskGroup(of: Void.self) { group in
            for _ in 0..<50 {
                group.addTask {
                    for _ in 0..<100 { log.note("x") }
                }
            }
        }
        XCTAssertEqual(log.snapshot().seq, 5000)
        XCTAssertEqual(log.snapshot().entries.map(\.seq), Array(1...5000))
    }
}

final class RouteTests: XCTestCase {

    func testGetWithAndWithoutAQuery() {
        XCTAssertEqual(CatalogRoute(requestLine: "GET /status HTTP/1.1"), .read(path: "/status", query: [:]))
        XCTAssertEqual(
            CatalogRoute(requestLine: "GET /activity?since=12&since=40&x= HTTP/1.1"),
            .read(path: "/activity", query: ["since": "40", "x": ""])
        )
    }

    func testAnythingButGetIsRefused() {
        for line in ["POST /status HTTP/1.1", "DELETE /variants HTTP/1.1", "", "GET"] {
            XCTAssertEqual(CatalogRoute(requestLine: line), .notAllowed, line)
        }
    }
}

@MainActor
final class ServerTests: XCTestCase {

    private func makeServer(port: UInt16 = 8766, shown: Bool = true, activity: ActivityLog = ActivityLog()) -> CatalogServer {
        var registry = ScreenRegistry()
        registry.add("Home.default", "Home.empty") { EmptyView() }
        registry.add("Profile.loaded") { EmptyView() }
        let variants = registry.variants
        return CatalogServer(port: port, activity: activity) {
            CatalogServer.Model(mode: "variant", variant: "Home.empty", shown: shown, variants: variants)
        }
    }

    private func request(_ line: String, host: String = "127.0.0.1:8766") -> String {
        "\(line)\r\nHost: \(host)\r\nAccept: */*"
    }

    /// (status code, parsed JSON body) of a raw response.
    private func parse(_ response: Data) throws -> (Int, [String: Any]) {
        let separator = try XCTUnwrap(response.range(of: Data("\r\n\r\n".utf8)))
        let head = String(decoding: response[..<separator.lowerBound], as: UTF8.self)
        let status = try XCTUnwrap(Int(head.split(separator: " ")[1]))
        let body = response[separator.upperBound...]
        XCTAssertTrue(head.contains("Content-Length: \(body.count)"), head)
        XCTAssertTrue(head.contains("Connection: close"), head)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        return (status, json)
    }

    func testStatusReportsTheProtocolTheProcessAndWhetherTheVariantIsShown() throws {
        let (status, json) = try parse(makeServer().respond(to: request("GET /status HTTP/1.1")))
        XCTAssertEqual(status, 200)
        XCTAssertEqual(json["protocol"] as? Int, 1)
        XCTAssertEqual(json["pid"] as? Int, Int(ProcessInfo.processInfo.processIdentifier))
        XCTAssertEqual(json["mode"] as? String, "variant")
        XCTAssertEqual(json["variant"] as? String, "Home.empty")
        XCTAssertEqual(json["shown"] as? Bool, true)

        let (_, notYet) = try parse(makeServer(shown: false).respond(to: request("GET / HTTP/1.1")))
        XCTAssertEqual(notYet["shown"] as? Bool, false)
    }

    func testVariantsListsNamesWithTheirFeatureInRegistryOrder() throws {
        let (status, json) = try parse(makeServer().respond(to: request("GET /variants HTTP/1.1")))
        XCTAssertEqual(status, 200)
        let rows = try XCTUnwrap(json["variants"] as? [[String: String]])
        XCTAssertEqual(rows, [
            ["name": "Home.default", "feature": "Home"],
            ["name": "Home.empty", "feature": "Home"],
            ["name": "Profile.loaded", "feature": "Profile"]
        ])
    }

    func testActivityHonoursSince() throws {
        let activity = ActivityLog()
        activity.note("a")
        activity.begin("load")
        let server = makeServer(activity: activity)

        let (_, all) = try parse(server.respond(to: request("GET /activity HTTP/1.1")))
        XCTAssertEqual(all["seq"] as? Int, 2)
        XCTAssertEqual(all["busy"] as? Int, 1)
        XCTAssertEqual((all["entries"] as? [[String: Any]])?.count, 2)

        let (_, later) = try parse(server.respond(to: request("GET /activity?since=1 HTTP/1.1")))
        XCTAssertEqual((later["entries"] as? [[String: Any]])?.compactMap { $0["label"] as? String }, ["begin load"])
    }

    func testOnlyTheLoopbackHostIsAnswered() throws {
        let server = makeServer()
        for host in ["evil.example:8766", "127.0.0.1:9999", "127.0.0.1"] {
            let (status, json) = try parse(server.respond(to: request("GET /status HTTP/1.1", host: host)))
            XCTAssertEqual(status, 403, host)
            XCTAssertEqual(json["error"] as? String, "forbidden host")
        }
        XCTAssertEqual(try parse(server.respond(to: "GET /status HTTP/1.1")).0, 403, "no Host header at all")
        XCTAssertEqual(try parse(server.respond(to: request("GET /status HTTP/1.1", host: "LOCALHOST:8766"))).0, 200)
    }

    func testOtherMethodsAndUnknownRoutes() throws {
        let server = makeServer()
        XCTAssertEqual(try parse(server.respond(to: request("POST /status HTTP/1.1"))).0, 405)
        let (status, json) = try parse(server.respond(to: request("GET /nope HTTP/1.1")))
        XCTAssertEqual(status, 404)
        XCTAssertEqual(json["routes"] as? String, "/status /variants /activity?since= /screenshot")
    }

    #if !canImport(UIKit)
    func testScreenshotNeedsUIKit() throws {
        XCTAssertEqual(try parse(makeServer().respond(to: request("GET /screenshot HTTP/1.1"))).0, 503)
    }
    #endif

    func testTheListenerAnswersOverTheLoopbackInterface() async throws {
        let port = UInt16.random(in: 20_000...40_000)
        let activity = ActivityLog()
        activity.note("over the wire")
        let server = makeServer(port: port, activity: activity)
        server.start()
        defer { server.stop() }

        let url = try XCTUnwrap(URL(string: "http://127.0.0.1:\(port)/activity?since=0"))
        var lastError: Error?
        for _ in 0..<50 {
            do {
                let (data, response) = try await URLSession.shared.data(from: url)
                XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
                let json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
                XCTAssertEqual(json["seq"] as? Int, 1)
                return
            } catch {
                lastError = error  // the listener is not ready yet
                try await Task.sleep(nanoseconds: 100_000_000)
            }
        }
        XCTFail("the server never answered: \(String(describing: lastError))")
    }
}
