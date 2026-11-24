import ScreenCatalog
import SwiftUI

/// Every screen the catalog records, as `<Feature>.<variant>`. Adding a state to the catalog is one
/// `add` here; the recording tool reads the list from the running app.
@MainActor
enum ExampleCatalog {

    static var registry: ScreenRegistry {
        var registry = ScreenRegistry()

        registry.add("Home.default") {
            HomeView(userName: "Alex", items: HomeView.sampleItems)
        }
        registry.add("Home.empty") {
            HomeView(userName: "Alex", items: [])
        }
        // One builder under several names, told apart by the name it is handed.
        registry.addPassingName("Home.longName", "Home.manyItems") { name in
            HomeView(
                userName: name == "Home.longName" ? "Maximiliana Featherstonehaugh-Cholmondeley" : "Alex",
                items: name == "Home.manyItems" ? (1...30).map { "Item \($0)" } : HomeView.sampleItems
            )
        }

        registry.add("Settings.default") {
            SettingsView()
        }
        // UserDefaults is cleared before every variant, so a builder seeds exactly what it needs.
        registry.add("Settings.notificationsOff") {
            UserDefaults.standard.set(false, forKey: SettingsView.notificationsKey)
            return SettingsView()
        }

        registry.add("Profile.loading") {
            ProfileView(load: nil)
        }
        // Loads asynchronously; the recorder waits for the load to finish before the screenshot.
        registry.add("Profile.loaded") {
            ProfileView(load: ProfileView.sampleLoad)
        }

        registry.add("Clock.default") {
            ClockView()
        }

        return registry
    }
}
