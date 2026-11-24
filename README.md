# ScreenCatalog

A screenshot of every screen state your iOS app registers, in light and dark, taken from the real app on a simulator. It does two jobs:

1. **A catalog.** One folder holds what every registered screen looks like, so a reviewer, a designer or a coding agent can see a screen without building or launching anything.
2. **A visual regression check.** One recorded run per simulator type is pinned as the baseline. A later run is compared with it pixel by pixel, within a tolerance, and every difference comes with a diff image and goes through a two-person review before it can become the new baseline.

No XCUITest and no test target are involved. Your app links a small Swift package and registers named screens. A Python tool launches the app once per screen, waits until the app says it is idle, asks the app for a screenshot of its own windows, and does everything else (comparing, reviewing, sharing) as file operations.

```
┌─────────────── your app (Debug, simulator) ───────────────┐      ┌──────────── tool/ (Python 3.9+, stdlib only) ────────────┐
│ ScreenCatalog.bootstrap { registry }                      │      │ record   launch each variant, wait, GET /screenshot      │
│ -ScreenCatalogVariant Profile.empty -ScreenCatalogPort N  │◀────▶│ compare  two runs, as files; diff images; HTML report    │
│ 127.0.0.1:N  /status /variants /activity /screenshot      │ HTTP │ review   expect / confirm / reject each change           │
└───────────────────────────────────────────────────────────┘      │ sync     share runs and the baseline (S3 or a folder)    │
                                                                   │ web UI   record, browse, compare, review in a browser    │
                                                                   └──────────────────────────────────────────────────────────┘
```

## Status

The Python tool's test suite and the Swift package's tests pass; see [CONTRIBUTING.md](CONTRIBUTING.md) for how to run them. Requirements: macOS with Xcode (Swift 6 toolchain), iOS 16+ for the package, Python 3.9+ for the tool. The in-app server only runs in Debug builds on a simulator.

## Quick start with the example app

```bash
# 1. Build the example for a booted simulator and install it
cd Example
xcodebuild -project ScreenCatalogExample.xcodeproj -scheme ScreenCatalogExample -configuration Debug \
  -destination 'platform=iOS Simulator,id=<UDID>' -derivedDataPath .build build -quiet
xcrun simctl install <UDID> .build/Build/Products/Debug-iphonesimulator/ScreenCatalogExample.app

# 2. Record every variant, light and dark (the config is Example/screencatalog.json)
python3 ../tool/screencatalog.py record --udid <UDID>

# 3. Or do all of it from a browser: build, record, browse, compare, review
python3 ../tool/screencatalog_ui.py        # prints http://127.0.0.1:8899/
```

A run lands in `Example/.screencatalog/runs/<run-id>/{light,dark}/<Feature>.<variant>.png`. Use a simulator of your own for records: the tool terminates and relaunches the app once per screenshot, overrides the status bar and flips the system appearance.

To see a comparison, commit, pin the run (`set-baseline <run-id>`), change a view, commit, rebuild and reinstall, record again, then `compare <new-run-id> --report`.

## Adopting it in your app

1. **Add the package** (this repository's URL, or a local path) and link `ScreenCatalog`.
2. **Register screens and bootstrap**, first thing in your app's entry point:

   ```swift
   import ScreenCatalog
   import SwiftUI

   @main
   struct MyApp: App {
       init() {
           ScreenCatalog.bootstrap { MyCatalog.registry }   // a no-op on a normal launch and in Release
       }
       var body: some Scene {
           WindowGroup { ScreenCatalogHost { RootView() } }
       }
   }

   @MainActor
   enum MyCatalog {
       static var registry: ScreenRegistry {
           var registry = ScreenRegistry()
           registry.add("Profile.default") { ProfileView(model: .fixture) }
           registry.add("Profile.empty") { ProfileView(model: .empty) }
           return registry
       }
   }
   ```

3. **Add `screencatalog.json`** next to your project:

   ```json
   {
     "app": { "bundleId": "com.example.MyApp.dev" },
     "build": { "project": "MyApp.xcodeproj", "scheme": "MyApp" },
     "storage": { "type": "s3", "bucket": "my-team-artifacts" }
   }
   ```

4. **Make the screens deterministic** (a fixed clock, no network, bundled images, no endless animations) and tell the recorder when a screen is busy. [docs/adopting.md](docs/adopting.md) is the checklist, including UIKit apps and a recipe for apps built on The Composable Architecture ([docs/tca-recipe.md](docs/tca-recipe.md)).
5. Add `.screencatalog/` to `.gitignore`.

## Workflow

The baseline means "the main branch at a commit". That one rule shapes the team flow:

- **Author of a UI change.** Pull the baseline (`sync pull`), record on your branch (`--feature <Feature>` while iterating, a full record before review), `compare <run>`, and mark each intended change expected with a note (`review <run> <name> <appearance> --expect --note "…"`). A regression is fixed, not marked expected. Push the run (`sync push <run>`) and name it in the pull request.
- **Reviewer.** `sync pull <run>`, open the comparison in the web UI, and confirm or reject each change. Nobody can confirm their own change. A verdict is keyed by the two images, not the run, so it carries over when the same pixels are recorded again.
- **After the merge.** Check out the main branch at the merge commit, build, record a full run, `set-baseline <run>`, `sync push`. Only a run recorded from a clean checkout of a commit on the main branch can be pinned, and only once every change has been confirmed.

A record of a build that already has a complete run (same simulator type, commit, app binary and capture mode) is refused: the pixels would be the same.

## How it differs from other tools

- Libraries such as swift-snapshot-testing render a view inside a test process and keep reference images next to the tests. ScreenCatalog launches the whole app, so a screen is captured with its real navigation, sheets, covers and app-level setup, and the images live outside git with an explicit baseline and review trail. The cost is speed: a launch per screenshot.
- Hosted and self-hosted review services take a directory of PNGs and add pull-request checks. ScreenCatalog's run folders are plain PNGs plus JSON, so they can be fed to one; what it does not have is a CI job or a pull-request status check of its own.

## Limitations

- The default capture is the app drawing its own windows. System chrome (status bar, Dynamic Island, home indicator, rounded corners), the keyboard, system alerts and playing video are not in the picture. `--capture simctl` captures the display instead, at the price of system overlays that differ between captures. See [docs/reference.md](docs/reference.md#compositor-artefacts).
- Recording needs a Mac with simulators. Comparing, reviewing and browsing need only Python.
- Review identity is the git user name and email; there is no authentication.
- No CI integration is included.

## Documentation

- [docs/adopting.md](docs/adopting.md): wiring an app, determinism, the settle signal
- [docs/configuration.md](docs/configuration.md): every `screencatalog.json` key
- [docs/reference.md](docs/reference.md): commands, flags, the web UI and its API, the catalog on disk, sharing, the gates, tolerance
- [docs/protocol.md](docs/protocol.md): the HTTP contract between the app and the tool
- [docs/tca-recipe.md](docs/tca-recipe.md): using it with The Composable Architecture

## License

MIT. See [LICENSE](LICENSE).
