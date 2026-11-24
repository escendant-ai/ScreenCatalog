# Using ScreenCatalog with The Composable Architecture

The package has no dependency on TCA; these are patterns for an app that uses it. They are a recipe, not shipped code, and have not been compiled against a particular TCA version: adjust the spelling to the version you use.

## A variant builds a store

```swift
registry.add("Profile.default") {
    ProfileView(store: Store(initialState: Profile.State(user: .fixture)) { Profile() })
}
registry.add("Profile.editing") {
    var state = Profile.State(user: .fixture)
    state.destination = .edit(.init(user: .fixture))
    return ProfileView(store: Store(initialState: state) { Profile() })
}
```

Presented state (a sheet, a cover, an alert) is set in the initial state, so the screenshot shows the presentation.

## Fakes for every dependency, before any store exists

Do it in `prepare`, which runs before the variant's builder:

```swift
ScreenCatalog.bootstrap(options: ScreenCatalogOptions(prepare: { _ in
    prepareDependencies {
        $0.context = .preview            // every dependency resolves to its previewValue
        $0.date = .constant(Date(timeIntervalSince1970: 1_767_260_460))
    }
})) { MyCatalog.registry }
```

Give each client a `previewValue` that returns fixtures. A client whose fixture depends on the variant can read `ScreenCatalog.activeVariantName`.

## The settle signal from the reducer

Every action is activity. A higher-order reducer reports it:

```swift
extension Reducer {
    /// Tells the screen catalog recorder that the feature is still doing something.
    func reportingCatalogActivity() -> some Reducer<State, Action> {
        Reduce { state, action in
            ScreenCatalog.noteActivity(String(describing: type(of: action)))
            return self.reduce(into: &state, action: action)
        }
    }
}
```

Apply it to the root reducer of each variant's store (or to the app's root reducer). The recorder then waits until no action has been sent for the quiet period. Long-running effects that send nothing for a while (a request with no intermediate actions) are better wrapped in `ScreenCatalog.withActivity` inside the client, so the screen is not captured while the request is in flight.

## Effects that never finish

A timer, a websocket subscription or a `for await` over a stream sends actions forever, or changes the screen after the screenshot. In a catalog run, give the feature a clock that does not advance (`TestClock`) and a stream client whose preview value finishes immediately.
