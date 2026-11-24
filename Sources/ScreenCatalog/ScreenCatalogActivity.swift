import Foundation
import os

/// In-flight work begun with `ScreenCatalog.beginActivity`. Ending is idempotent, and a token that
/// is released without being ended ends itself, so dropped work cannot keep a screen from settling.
public final class ActivityToken: Sendable {

    private let log: ActivityLog?
    private let label: String
    private let ended = OSAllocatedUnfairLock(initialState: false)

    init(log: ActivityLog?, label: String) {
        self.log = log
        self.label = label
    }

    public func end() {
        let first = ended.withLock { ended -> Bool in
            defer { ended = true }
            return !ended
        }
        if first { log?.end(label) }
    }

    deinit { end() }
}

/// The settle signal: a sequence number that grows with every noted event, a count of work in
/// flight, and the newest entries for diagnosis. `/activity` serves it; the recorder waits for
/// `seq` to stop growing while `busy` is 0.
final class ActivityLog: Sendable {

    struct Entry: Codable, Equatable, Sendable {
        let seq: Int
        let date: Date
        let label: String
    }

    struct Snapshot: Codable, Equatable, Sendable {
        /// The newest sequence number: 0 when nothing has happened.
        let seq: Int
        /// Work begun and not yet ended.
        let busy: Int
        /// Entries newer than the `since` asked for, oldest first; at most `capacity` are kept.
        let entries: [Entry]
    }

    private struct State {
        var seq = 0
        var busy = 0
        var entries: [Entry] = []
    }

    private let state = OSAllocatedUnfairLock(initialState: State())
    private let capacity: Int

    init(capacity: Int = 2000) {
        self.capacity = capacity
    }

    func note(_ label: String) {
        append(label) { _ in }
    }

    func begin(_ label: String) {
        append(label.isEmpty ? "begin" : "begin \(label)") { $0.busy += 1 }
    }

    func end(_ label: String) {
        append(label.isEmpty ? "end" : "end \(label)") { $0.busy = max(0, $0.busy - 1) }
    }

    func snapshot(after seq: Int = 0) -> Snapshot {
        state.withLock { state in
            Snapshot(seq: state.seq, busy: state.busy, entries: state.entries.filter { $0.seq > seq })
        }
    }

    private func append(_ label: String, _ change: @Sendable (inout State) -> Void) {
        let capacity = capacity
        let date = Date()
        state.withLock { state in
            change(&state)
            state.seq += 1
            state.entries.append(Entry(seq: state.seq, date: date, label: label))
            if state.entries.count > capacity {
                state.entries.removeFirst(state.entries.count - capacity)
            }
        }
    }
}
