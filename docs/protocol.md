# Protocol

What the recording tool and the app say to each other. The Swift package implements the app side; this page is the contract, for anyone writing another client or another app-side implementation (a UIKit-only app, another platform).

Protocol version: **1**.

## Launch arguments

| argument | effect |
|---|---|
| `-ScreenCatalogVariant <Feature>.<variant>` | show that registered screen instead of the app. An unknown name ends the process with a message on stderr that names it |
| `-ScreenCatalogList` | show the list of registered screens (and serve it on `/variants`) |
| `-ScreenCatalogPort <port>` | with either of the above: serve the routes below on `127.0.0.1:<port>`. Ignored on a normal launch, on a device and in a Release build |

A variant name is `<Feature>.<variant>`: the feature in ASCII letters, digits, `_` and `-`; the variant the same plus `.`. The part before the first `.` is the feature.

## Transport

HTTP/1.1 over TCP on the loopback interface only. Every route is a `GET`; every response carries `Connection: close`. A request whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>` is answered `403`, so a page in a browser on the same machine cannot reach the server through a name that resolves to 127.0.0.1. Any other method is `405`; an unknown path is `404` with the list of routes. JSON bodies are UTF-8; dates are ISO 8601.

## Routes

### `GET /status`

```json
{
  "protocol": 1,
  "bundle": "com.example.MyApp.dev",
  "pid": 4242,
  "mode": "variant",
  "variant": "Profile.empty",
  "shown": true,
  "routes": ["/status", "/variants", "/activity?since=", "/screenshot"]
}
```

- `protocol`: the tool refuses any value other than the one it speaks, with a message naming both.
- `pid`: the tool matches it against the process it launched, so an answer from a previous launch still shutting down is not mistaken for this one.
- `mode`: `variant` or `list`. `variant` is absent in `list` mode.
- `shown`: true once the catalog's view has appeared (one run-loop turn after its first `onAppear`). The tool records how long that took and counts `--min-shown-ms` from it.

`GET /` answers the same.

### `GET /variants`

```json
{ "protocol": 1, "variants": [{ "name": "Profile.default", "feature": "Profile" }, { "name": "Profile.empty", "feature": "Profile" }] }
```

In registration order, which is the order the tool records in.

### `GET /activity?since=<seq>`

```json
{ "seq": 17, "busy": 0, "entries": [{ "seq": 17, "date": "2026-01-01T09:41:00Z", "label": "end load profile" }] }
```

- `seq`: grows by one with every noted event; 0 when nothing has happened.
- `busy`: work begun and not ended.
- `entries`: the events newer than `since`, oldest first, for diagnosis. Only the newest 2,000 are kept.

**Settled** means: `seq` has not grown and `busy` is 0 for the quiet period (`--settle-ms`). The tool polls with the last `seq` it saw. What counts as an event is the app's choice ([adopting.md](adopting.md#telling-the-recorder-when-a-screen-is-busy)); an app that reports nothing is settled after one quiet period.

### `GET /screenshot`

`200` with `Content-Type: image/png`: every visible window of the app's foreground scene, drawn bottom-up by window level, flattened over opaque black, 8-bit sRGB, at the screen's native scale. Diagnostic headers: `X-ScreenCatalog-Scale`, `X-ScreenCatalog-Size`, `X-ScreenCatalog-Windows`, `X-ScreenCatalog-Render-Ms`, and `X-ScreenCatalog-Undrawn` when a window reported it drew nothing.

`503` with a JSON `error` when there is no window scene.

The tool rejects a body that is not a PNG. Two captures of an unchanged screen are expected to be byte-identical; that is what makes a comparison trustworthy.

## What the tool does with it, per variant

1. `simctl terminate`, then `simctl launch <udid> <bundle> -ScreenCatalogVariant <name> -ScreenCatalogPort <port>`.
2. Poll `/status` until `pid` matches (or the process exits, which is reported with the end of its stderr).
3. Poll `/status` until `shown`.
4. Poll `/activity` until settled, or a cap.
5. `GET /screenshot` twice and keep the second when the top and bottom bands match; retake otherwise.

The variant list for a run comes from one `-ScreenCatalogList` launch and `GET /variants`.
