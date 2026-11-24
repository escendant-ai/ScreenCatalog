# Contributing

## Layout

- `Sources/ScreenCatalog/`: the Swift package an app links. No dependencies.
- `Tests/ScreenCatalogTests/`: its tests.
- `tool/`: the recording, comparing and review tool, and the web UI. Python 3.9+, standard library only. Two single-file scripts, kept that way on purpose: they run from a checkout with no install step, on the Python that ships with Xcode.
- `Example/`: a sample app wired to the package, with its `screencatalog.json`.
- `docs/`: the documentation.

## Running the tests

```bash
# The tool (no simulator needed)
cd tool && python3 -m unittest test_screencatalog test_screencatalog_ui test_screencatalog_config

# The package: logic and the local server, on macOS
swift test

# The package on an iOS simulator (covers the UIKit build)
xcodebuild test -scheme ScreenCatalog -destination 'platform=iOS Simulator,name=<a simulator you have>'
```

The tool's tests pin their own config (`tool/testdata/screencatalog.json`), so they do not depend on the directory they run from. The tool must keep working on Python 3.9: `python3 --version` should be 3.9 or later.

A real screenshot only exists in a run of the example app; the package's unit tests cannot produce one. After a change to `Screenshot.swift`, the server or the recorder's capture path, record the example twice from one build and compare the runs: they should be identical.

## Changing the protocol

`docs/protocol.md` is the contract between the package and the tool. A change that an older counterpart would misread needs a new protocol version in both `ScreenCatalog.protocolVersion` and `PROTOCOL` in `tool/screencatalog.py`.

## Style

- Swift: Swift 6 language mode, no third-party dependencies in the package.
- Python: standard library only; type hints with `from __future__ import annotations`.
- Docs: say what something does and what it costs; when a number is quoted, say how it was measured.
