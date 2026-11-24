import ScreenCatalog
import SwiftUI

struct HomeView: View {

    static let sampleItems = ["Groceries", "Call the plumber", "Book flights"]

    let userName: String
    let items: [String]

    var body: some View {
        NavigationStack {
            Group {
                if items.isEmpty {
                    ContentUnavailableView("Nothing to do", systemImage: "checkmark.circle", description: Text("New items appear here."))
                } else {
                    List(items, id: \.self) { item in
                        Label(item, systemImage: "circle")
                    }
                }
            }
            .navigationTitle("Hello, \(userName)")
        }
    }
}

struct SettingsView: View {

    static let notificationsKey = "notificationsEnabled"

    @AppStorage(SettingsView.notificationsKey) private var notificationsEnabled = true

    var body: some View {
        NavigationStack {
            Form {
                Toggle("Notifications", isOn: $notificationsEnabled)
                LabeledContent("Status", value: notificationsEnabled ? "On" : "Off")
            }
            .navigationTitle("Settings")
        }
    }
}

struct ProfileView: View {

    struct Profile: Equatable {
        let name: String
        let bio: String
    }

    static let sampleLoad: @Sendable () async -> Profile = {
        try? await Task.sleep(for: .milliseconds(800))
        return Profile(name: "Alex Example", bio: "Writes lists. Occasionally finishes one.")
    }

    /// Nil leaves the screen in its loading state.
    let load: (@Sendable () async -> Profile)?
    @State private var profile: Profile?

    var body: some View {
        NavigationStack {
            Group {
                if let profile {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(profile.name).font(.title2.bold())
                        Text(profile.bio)
                        Spacer()
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
                } else if ScreenCatalog.isCatalogRun {
                    // A spinner is a different frame in every screenshot; the catalog gets a still.
                    Text("Loading…").foregroundStyle(.secondary)
                } else {
                    ProgressView()
                }
            }
            .navigationTitle("Profile")
        }
        .task {
            guard let load, profile == nil else { return }
            // Tells the recorder work is in flight: it will not screenshot until this returns.
            profile = await ScreenCatalog.withActivity("load profile") { await load() }
        }
    }
}

struct ClockView: View {

    /// A catalog run pins "now", so the screenshot is the same today and next year.
    private var now: Date {
        ScreenCatalog.isCatalogRun ? Date(timeIntervalSince1970: 1_767_260_460) : Date()
    }

    private var formatted: String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "EEEE d MMMM yyyy, HH:mm 'UTC'"
        return formatter.string(from: now)
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 8) {
                Image(systemName: "clock").font(.largeTitle)
                Text(formatted).multilineTextAlignment(.center)
            }
            .padding()
            .navigationTitle("Clock")
        }
    }
}
