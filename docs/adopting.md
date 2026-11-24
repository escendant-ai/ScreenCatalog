# Adopting ScreenCatalog in an app

## 1. Link the package and bootstrap

Add the `ScreenCatalog` package to the app target, then:

```swift
import ScreenCatalog
import SwiftUI

@main
struct MyApp: App {
    init() {
        ScreenCatalog.bootstrap { MyCatalog.registry }
    }
    var body: some Scene {
        WindowGroup { ScreenCatalogHost { RootView() } }
    }
}
```

`bootstrap` must run before anything that the catalog should not see happen: analytics start-up, a network client being created, a sign-in check. On a normal launch, and in every Release build, it returns at once and never calls the registry closure; `ScreenCatalogHost` then shows its content unchanged.

On a catalog launch it:

1. builds the registry and stops with a `fatalError` naming any duplicate or malformed name, or an unknown requested variant (the recorder reports the message);
2. turns off UIKit and Core Animation animations (`disablesAnimations`);
3. clears the app's `UserDefaults` domain (`resetsUserDefaults`), so nothing persisted by an earlier variant leaks into this one;
4. calls your `prepare` hook with the variant;
5. starts the local server when `-ScreenCatalogPort` was passed (simulator only).

```swift
ScreenCatalog.bootstrap(options: ScreenCatalogOptions(
    disablesAnimations: true,
    resetsUserDefaults: true,
    prepare: { variant in
        Fonts.register()
        Dependencies.useFakes(for: variant.name)
    }
)) { MyCatalog.registry }
```

**UIKit apps.** Call `bootstrap` in `application(_:didFinishLaunchingWithOptions:)` and, when `ScreenCatalog.catalogRootView` is not nil, make a `UIHostingController` of it the window's root view controller. A UIKit screen is registered by wrapping it in a `UIViewControllerRepresentable`.

**Release builds.** The registry, the server and the screenshot code are compiled only when the package is built with `DEBUG` defined (Swift Package Manager defines it for debug builds). Your own registrations and fixtures are your code: wrap them in `#if DEBUG` if you do not want them linked into a Release binary, and pass an empty registry otherwise.

```swift
ScreenCatalog.bootstrap {
    #if DEBUG
    MyCatalog.registry
    #else
    ScreenRegistry()
    #endif
}
```

## 2. Register screens

A name is `<Feature>.<variant>`. The feature is what the tool filters by (`--feature Profile`) and what the web UI groups by.

```swift
@MainActor
enum MyCatalog {
    static var registry: ScreenRegistry {
        var registry = ScreenRegistry()
        registry.add("Profile.default") { ProfileView(model: .fixture) }

        // one builder, several names: a fake tells them apart by ScreenCatalog.activeVariantName
        registry.add("Feed.empty", "Feed.error", "Feed.loaded") { FeedView() }

        // or have the name handed to the builder
        registry.addPassingName("Tour.step1", "Tour.step2") { name in TourView(step: name) }

        // each feature can own its list
        registry.add(contentsOf: SettingsCatalog.registry)
        return registry
    }
}
```

A builder runs once, on the launch that names its variant, after the `UserDefaults` reset and `prepare`. It may seed state before returning its view.

Wrap a screen in whatever it needs to look as it does in the app: a `NavigationStack` for its bar, a sheet presented from a host view, an environment object.

A unit test keeps the registry honest without launching anything:

```swift
@MainActor
func testCatalogNamesAreValid() {
    XCTAssertEqual(MyCatalog.registry.issues(), [])
}
```

## 3. Making screens deterministic

A comparison is only worth having when two captures of an unchanged screen are the same bytes. Record the catalog twice from one build and compare the two runs: every FAIL is a screen that does not render the same way twice. The usual causes, and what to key off `ScreenCatalog.isCatalogRun` (or do in `prepare`) for each:

| cause | fix |
|---|---|
| the clock: dates, "3 minutes ago", greetings by time of day | inject a clock; give it one fixed instant in a catalog run. Format with an explicit locale and time zone |
| the network | a client that returns fixtures, or throws loudly, in a catalog run. A request that reaches a real server is a flake waiting to happen |
| remote images | bundle the fixture images; load them from file URLs |
| spinners and looping animations | draw a still, or pause the animation at a fixed progress. `disablesAnimations` stops UIKit and Core Animation implicit animations and SwiftUI transactions, not a library with its own timing |
| random values: ids shown on screen, shuffled lists, generated waveforms | seed or fix them |
| timers that change the screen: auto-hiding chrome, countdowns | a clock that does not advance |
| persisted state: `UserDefaults`, keychain, files, a database | `UserDefaults` is cleared for you; reset or redirect the rest in `prepare` |
| fonts registered late | register them in `prepare`, before the first frame |
| locale, text size, right-to-left | the simulator's settings are part of the run; keep them fixed across runs you compare |

A cause you cannot fix at the source can be excluded, with a reason ([reference](reference.md#exclusions)).

## 4. Telling the recorder when a screen is busy

The recorder takes the screenshot once the app has been quiet for a period (1.5 s by default). "Quiet" is the app's activity signal:

```swift
// Something happened: the quiet period starts again.
ScreenCatalog.noteActivity("feed reloaded")

// Work is in flight: the screen is not settled until it ends.
let token = ScreenCatalog.beginActivity("load profile")
defer { token.end() }

// The same, around an async call.
let profile = await ScreenCatalog.withActivity("load profile") { await client.profile() }
```

All three are no-ops outside a catalog run, so they can stay in production code paths. Good places to call them: the one function every network request goes through, an image loader's completion, a store's or view model's action hook.

An app that reports nothing is captured after one quiet period from launch. That is fine for screens that render synchronously from fixtures; a screen that loads asynchronously needs the signal or it may be captured half-loaded. Work that never ends keeps `busy` above 0, and the capture then waits for the cap (10 s) and is marked `settled: false`.

## 5. Add the config and record

Add `screencatalog.json` ([configuration.md](configuration.md)), add the catalog folder to `.gitignore`, then follow the [README](../README.md#quick-start-with-the-example-app) with your own project in place of the example.

When a first full record compares cleanly with a second one of the same build, pin it from the main branch and share it ([README § Workflow](../README.md#workflow)).
