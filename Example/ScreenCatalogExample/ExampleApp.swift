import ScreenCatalog
import SwiftUI

@main
struct ExampleApp: App {

    init() {
        // A no-op on a normal launch and in Release builds; the registry is only built on a
        // `-ScreenCatalogVariant` or `-ScreenCatalogList` launch.
        ScreenCatalog.bootstrap { ExampleCatalog.registry }
    }

    var body: some Scene {
        WindowGroup {
            ScreenCatalogHost {
                ContentView()
            }
        }
    }
}

struct ContentView: View {
    var body: some View {
        TabView {
            HomeView(userName: "Alex", items: HomeView.sampleItems)
                .tabItem { Label("Home", systemImage: "house") }
            ProfileView(load: ProfileView.sampleLoad)
                .tabItem { Label("Profile", systemImage: "person") }
            ClockView()
                .tabItem { Label("Clock", systemImage: "clock") }
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gear") }
        }
    }
}
