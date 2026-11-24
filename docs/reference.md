# Reference

How the tool models runs, baselines and comparisons, every command and flag, what one capture does, the web UI and its API, the catalog's layout on disk, sharing, the main-branch and review gates, the tolerance, and the two capture modes. For a first run, start with the [README](../README.md); for wiring an app, [adopting.md](adopting.md); for every config key, [configuration.md](configuration.md); for the app-to-tool wire format, [protocol.md](protocol.md).

Commands are written as `python3 tool/screencatalog.py …`, run from a folder at or below the one holding `screencatalog.json`. With the tool installed (`pipx install ./tool`) the same commands are `screencatalog …` and `screencatalog-ui`.

## Runs, baselines and comparisons

- **Record is the only capture.** `record` captures into a new run folder, `runs/<run-id>/`, and writes nothing else. A run is an immutable set of screenshots: nothing later rewrites it, except a pinned run's screens through `update-baseline` and `accept`.
- **Baseline is a role.** One run per simulator type (model and iOS version, e.g. `iPhone_17_iOS_27.0`) is pinned as that type's baseline in `baselines.json`. Pinning is deliberate: `set-baseline`; a record never pins anything.
- **Compare is a file operation** between two runs of the same type, never a capture. By default a run is compared with the pinned baseline of its type; `--against <run>` picks any other run of that type. Runs of different types are refused (a different model or iOS version); a different iOS build is only a note in the result.
- **Comparisons are derived data**, stored in `compares/<run-id>__<other-run-id>__r<revision>/`, keyed by the pair and the other run's revision. When the baseline changes (an update or an accept bumps its revision), the next compare is a new key and is computed afresh; the superseded key of that pair is removed. A key whose two runs are unchanged since it was written is reused, so a repeated compare costs a JSON read. A verdict is never stale: either it is current for the pair or it is recomputed.

```bash
# Record: capture every variant, light and dark, into a new run
python3 tool/screencatalog.py record --udid <UDID>

# Compare that run with the pinned baseline of its type (no simulator)
python3 tool/screencatalog.py compare cli-20261008-140000

# The first baseline of a type, or a deliberate replacement: pin a complete, full run
python3 tool/screencatalog.py set-baseline cli-20261008-140000

# Refresh part of the baseline: record a filtered run, then copy its screens over the pinned run
python3 tool/screencatalog.py record --udid <UDID> --feature Settings --run-id cli-settings
python3 tool/screencatalog.py update-baseline cli-onboarding
```

### Commands

| command | does |
|---|---|
| `record [flags]` | captures the selected variants into `runs/<run-id>/` ([flags below](#narrowing-a-record)). The run id is `cli-<yyyymmdd-hhmmss>` (local time, the moment the capture starts, which in the web UI is after the build and install, so the id is the recorded time; the web UI's runs start `ui-`). An id is never changed after recording: it is the folder, the S3 prefix and the key every comparison and sync record uses, and a run a teammate has pulled must keep one name unless `--run-id` names it. Exclusions are SKIPPED rows in its manifest, with their reason. Exits 1 on any ERROR row or when it stopped early. Afterwards it prunes to the newest `--keep-runs` (default 5; 0 keeps all) runs per source and type, never the pinned run |
| `compare <run> [--against <run>]` | judges `<run>`'s screens against the other run's and writes `compares/<key>/compare.json` and, for each FAIL, `diff/<appearance>/<name>.png`. Only the screens `<run>` has are judged, so a partial run against a full baseline judges its subset. `--max-diff-percent`, `--channel-threshold`, `--allow-missing`, `--exclude-file` as below; `--force` judges again even when the stored comparison is current; `--report` also writes `report.html` |
| `set-baseline <run>` | pins `<run>` for its type. Refuses a partial run (filtered, or one appearance) unless `--allow-partial`, a stopped or aborted run unless `--allow-stopped`, a run still capturing, a run whose commit is not on main ([Only main is pinned](#only-main-is-pinned); it fetches the main branch first, and there is no override), and, while the type has a pin, a run that has no current comparison with it or whose changes a reviewer has not all confirmed ([Review](#review); `--skip-review` pins anyway and the history says so). The pin is a `pin` event in the run's history, naming the run it replaced, where its commit stood against main and the review counts. Nothing is recomputed |
| `update-baseline <run>` | copies a partial run's captured screens over the pinned run's files and manifest rows (ERROR and SKIPPED rows are left out), bumps the pinned run's revision and logs an `update` event with the variant list and the source run. Refuses a full run ("pin it with `set-baseline`"), a run of a type with no pin, a run whose commit is not on main ([Only main is pinned](#only-main-is-pinned)), and a run whose copied screens' changes are not all confirmed by a reviewer (`--skip-review`) |
| `accept <run> <name> <appearance>` | copies one captured screen of `<run>` over the pinned run: a new revision and an `accept` event. The screen's change must be confirmed by a reviewer (`--skip-review`) |
| `review <run> [--against <run>] [--json]` | the changes (FAIL and MISSING screens) of the run's current comparison with their review state; exit 1 while any is not confirmed. `review <run> <name> <appearance> --expect\|--confirm\|--reject\|--withdraw [--note …] [--as "Name <email>"]` marks one first ([Review](#review)) |
| `delete-run <run>` | deletes the run folder, its thumbnails and every comparison it is part of. Refuses a run still capturing. The pinned run may be deleted: its type's pin goes with it, and the command prints `note: <run> was the baseline: no baseline for iPhone 17, iOS 27.0: set one from a run (set-baseline <run>)`; until a run is pinned, `compare` without `--against` refuses that type |
| `prune [--keep N]` | keeps the newest N runs (default 5) of each source (`cli`, `ui`) per type; never the pinned run, which does not count toward N, or a run still capturing. Then drops every comparison that is no longer current: one whose run is gone, or keyed by an older revision of the run it was compared with |
| `export <run> [--zip] [--out <path>]` | zips the run folder, stored not recompressed, as `screencatalog_<run-id>.zip` next to `runs/` (paths start `<run-id>/`; its launch logs are left out) |
| `import <zip>` | unzips an exported run into `runs/<run-id>/`; refuses an id that exists and any path outside the run folder. Pin it with `set-baseline` to compare against it |
| `sync push [<run>…] [--all] [--force]` | pushes the pinned runs and `baselines.json` to S3, or the runs named (and `baselines.json` when one of them is a pinned baseline), and always `reviews/`; `--all` every finished run. Refused when a remote revision is higher (`--force` pushes anyway), and for a run the remote lacks when it already holds the same build of that type under another id ("pull it instead, or delete it there first"), which no flag overrides ([Sharing a baseline](#sharing-a-baseline)) |
| `sync delete <run>…` | deletes those runs from the bucket (`runs/<id>/`, every object). Refused for a run the bucket pins as a type's baseline ("push another baseline for that type first") and for an id the bucket lacks. A Mac that pulled the run keeps its copy; `sync.json` here forgets the push |
| `sync pull [<run>…] [--list] [--force]` | pulls `reviews/`, then `baselines.json` and every pinned run missing here or behind, or the runs named; `--list` prints the remote runs. Refused over a higher local revision |
| `list` | JSON for scripts and the web UI: every run (newest first) with its `run.json` facts plus `complete`, `partial`, `stopped`, `active`, `pinned`, `comparable` (the other runs of its type and capture mode) and `baselineCompare` (its current comparison with the pin, counts only, or null); `baselines` |

Every command takes `--catalog-dir <dir>` (after the command name). A refused operation prints `error: …` and exits 2.

**Revisions.** Every run has a `revision`, starting at 1 when it is recorded. It changes only when the run is modified as a baseline: each `update-baseline` and `accept` adds 1, sets `revisionAt` and appends an event to the run's `history` (`{kind: update|accept, at, revision, from, variants: [{name, appearance}], app, git, main}`, `main` being where the source run's commit stood against main; `set-baseline` adds a `pin` event without a new revision).

**A run's state.** A run folder is complete once its `run.json` has `finished`. A record stopped by `--budget-minutes`, SIGINT, SIGTERM or the web UI's Stop is complete with `stopped: true` and keeps the screens it captured; one whose capture raised is `aborted: true`, `stopped: true` and never `finished`. A run is **partial** when it was filtered (`--only` / `--feature`) or covers one appearance; `update-baseline` takes a partial run, `set-baseline` a full one.

### Narrowing a record

| flag | effect |
|---|---|
| `--only <text>` | variants whose name contains `<text>` (an exact name counts); repeatable |
| `--feature <Feature>` | variants of that feature, matched exactly; repeatable. Combined with `--only`, both must match |
| `--appearance light\|dark\|both` | default `both` |
| `--exclude-file <path>` | default `screencatalog-exclusions.json` ([Exclusions](#exclusions)); `compare` applies it too, so a variant excluded after a run was recorded is SKIPPED in its comparisons |
| `--udid <UDID>` | defaults to the only booted simulator; with several booted, pass it. Repeat it to capture on several simulators at once ([below](#parallel-simulators)) |
| `--budget-minutes <n>` | stop starting captures after `n` minutes, then write the run as normal (`stopped: true`) |
| `--run-id <id>` | the run folder's name: letters, digits, `.`, `_`, `-`, no `..` or `__`; refused when it exists |
| `--source cli\|ui` | who started the run, written to its `run.json` and used by pruning; default `cli` (the web UI passes `ui`) |
| `--keep-runs <n>` | runs kept per source and type after the record, default 5; 0 prunes nothing |
| `--zip` | export the run once it is recorded |
| `--capture simctl\|app` | where screenshots come from, default `app`: `xcrun simctl io screenshot` (the display, with the status bar and, bistably, SpringBoard's overlays), or `app`, the in-app server's `GET /screenshot` (the app's own windows only). Written to `run.json` and every captured row as `captureMode`; runs of two modes are never compared ([Compositor artefacts](#compositor-artefacts)) |
| `--band-retakes <n>` | screenshots taken after the first pair when the [compositor bands](#compositor-artefacts) still differ, default 3; then the last is kept and its row says `unstableBands: true`. `-1` takes one screenshot and checks nothing |
| `--min-launch-ms <n>` | never screenshot sooner than this after the launch, default 0. Raise it for an app whose launch screen outlives its first frames |
| `--min-shown-ms <n>` | never screenshot sooner than this after `/status` first reports the variant as shown, default 300 ([What one capture does](#what-one-capture-does), step 4). Raise it when a system overlay is still fading, which matters in `simctl` capture mode |
| `--band-interval-ms <n>` | the wait between the band check's screenshots, default 400 |
| `--screenshot-mask black\|ignored\|alpha` | `simctl` mode only: `simctl io screenshot --mask=` on every screenshot, default `black`: the Dynamic Island cut-out and rounded corners are painted black whatever the framebuffer holds ([Compositor artefacts](#compositor-artefacts)). `ignored` saves the raw framebuffer; `alpha` is transparent corners |
| `--port <n>` | in-app server port, default `8766`. One port per simulator, so pick another when a second run is on the same Mac. With several `--udid`, simulator *i* uses `<n>` + *i* |

The variant list always comes from the installed app: one `-ScreenCatalogList -ScreenCatalogPort <port>` launch and `GET /variants`, which lists the app's `ScreenRegistry` in order. A variant added to the registry is in the next run with no other step.

## What one capture does

For each appearance (`xcrun simctl ui <UDID> appearance light|dark`), for each variant, in order:

1. `xcrun simctl terminate`, then `xcrun simctl launch <UDID> <bundle id> -ScreenCatalogVariant <name> -ScreenCatalogPort <port>`, with the app's stderr written to `runs/<run-id>/logs/<appearance>.<name>.stderr.log`.
2. Poll `/status` until it answers with the launched pid (`--health-timeout`, default 20 s). If the process dies first, the row is ERROR with the end of its stderr: an unregistered name `fatalError`s, and the message is the reason. A `/status` that reports another protocol version is an ERROR naming both versions.
3. Settle: poll `/activity?since=` until its `seq` has not grown and its `busy` is 0 for `--settle-ms` (default 1500), capped at `--max-wait-ms` (default 10000). A capture that hits the cap still takes its screenshot, with `settled: false` in the manifest. What counts as activity is whatever the app reports through `ScreenCatalog.noteActivity` and `beginActivity` ([adopting.md](adopting.md#telling-the-recorder-when-a-screen-is-busy)).
4. Before the settle, poll `/status` until `shown` is true, for at most `--max-wait-ms`; that moment is `shownMs` after the launch (`shownAt` in the row). Then never earlier than `max(launch + --min-launch-ms, shown + --min-shown-ms)`: defaults 0 and 300. The floors exist for apps whose first frames are not the variant's (a launch screen fading out) and for system overlays that settle after launch. The floor counts from the variant appearing as well as from the launch because under load a launch takes longer, and a floor from launch alone can land inside a fade. If `/status` never reports `shown` within `--max-wait-ms`, only the launch floor applies and the row has `shownAt: null` and `shownMs: null`.
5. `xcrun simctl io <UDID> screenshot --mask=black` (`--capture app`: `GET /screenshot` on the in-app server instead), twice, `--band-interval-ms` (400 ms) apart, the first one interval before the floor so the one kept is never earlier than it. The second is kept when the [compositor bands](#compositor-artefacts) of the two match; otherwise another is taken after the same wait, up to `--band-retakes` (3) times, each compared with the one before, and the last is kept with `unstableBands: true` in its row. Identical pixel data, the usual case, is settled from the decompressed PNG stream without decoding.

Once per run the status bar is cleared and pinned: `xcrun simctl status_bar <UDID> clear`, then `xcrun simctl status_bar <UDID> override --time 9:41 --batteryState charged --batteryLevel 100 --wifiBars 3 --cellularBars 4`. The override stays on the simulator afterwards (`xcrun simctl status_bar <UDID> clear` removes it).

A capture costs a terminate and launch, the settle wait and the screenshot (one `GET /screenshot`, about 0.1 s in `app` mode against about 0.5 s for `simctl io`), so a few seconds per variant and appearance; [parallel simulators](#parallel-simulators) divide the total. `--budget-minutes <n>` stops starting new captures after `n` minutes and then finishes normally (manifest, `run.json` with `stopped: true`, zip), so a bounded run leaves usable output; SIGINT and SIGTERM do the same. A run stopped early exits 1 and says how many captures it did not start. Appearances run one after the other, so a budget that runs out covers all of `light` before any of the tail of `dark`.

### Parallel simulators

Repeat `--udid` to split one run across several simulators: `python3 tool/screencatalog.py record --udid <UDID-A> --udid <UDID-B>`. The variant list is dealt round-robin in catalog order (simulator 1 gets variants 1, 3, 5…, simulator 2 gets 2, 4, 6…), and each simulator runs its share in its own thread on its own port (`--port` + its index). Every simulator gets the status bar override and its own appearance switch. Progress lines stay one per variant, tagged `sim1` / `sim2`; `--budget-minutes`, SIGINT and SIGTERM stop every simulator after the capture it is on. The output is one run folder, as for a single simulator, so the simulators must be the **same model and iOS version**: the script refuses to start otherwise, and warns when their installed apps differ. `run.json` lists the simulators under `simulators` and the manifest records which one took each variant (`udid` per row).

Each capture takes as long as on one simulator, so the speed-up is close to the number of simulators while the Mac has cores and memory for them.

Before every run each simulator's status bar override is cleared and set again. Re-applying an override over an existing one can leave the old status bar drawn (no cellular bars, a battery that is not charging), and a stale simulator then differs from the baseline by the status bar alone, on every variant in `simctl` mode. The override's values come from `record.statusBar` in the config.

## Web UI

`tool/screencatalog_ui.py` is a local page over the same runs, pins and comparisons ([Runs, baselines and comparisons](#runs-baselines-and-comparisons) is the model; the page adds no state of its own). It listens on 127.0.0.1 only, and only the page may talk to it: a request whose `Host` is not the server's own, a POST without `Content-Type: application/json`, or a POST with another `Origin` is answered 403 before any route runs, so a web page open in the same browser cannot delete, push or record through it:

```bash
python3 tool/screencatalog_ui.py            # prints http://127.0.0.1:8899/
python3 tool/screencatalog_ui.py --port 8899 --catalog-dir <dir> --bridge-port 8766 --keep-runs 5
```

It binds **127.0.0.1 only** and needs nothing but the standard library. Records run the capture script in-process, one at a time (a second answers `409`); stopping the server (^C or SIGTERM) stops an active record and terminates the app on its simulators. `--keep-runs <n>` (default 5, 0 keeps all) is passed to every record with `--source ui`. `--max-diff-percent` and `--channel-threshold` set the comparisons' tolerance (defaults 0.01 and 8).

The landing page has two cards:

| card | route | does |
|---|---|---|
| **Record** | `#record` (`#record/<deviceDir>` preselects a type) | the record panel: simulator type, picked through two segmented controls, the family (iPhones, then iPads) and the iOS version within it (newest first; each control hidden when it has one choice), then the models of that family and version (its pin tagged **baseline**), in a list that is never clipped or scrolled on its own, simulators in parallel ([clones](#clones); under it the recommended count from this Mac's free memory and swap, `floor(free GB / 2.5)` from 1 to 8 and one fewer when swap used is over half of swap total, 2.5 GB per simulator being an estimate, which is also the default until a count is chosen, and an amber note when the chosen count is above it), appearance, filter, feature, settle, budget, **Skip the build**, **Record**, **Stop**, the progress strip and log. **Record** builds first ([Clones](#clones)): the strip says Building until the capture starts. Its card lists the pin per type ("iPhone 17 iOS 27.0: baseline r2 · 42 light · 42 dark"). A record writes one new run and never touches a baseline, so nothing asks first. When it ends: **View run**, and **Compare with baseline** when the type has a pin |
| **Recordings** | `#recordings` | every run, newest first, under one heading per simulator type (newest iOS first). Its card shows the run and pin counts. The heading says where the pin's commit stands against main ([below](#where-a-run-stands-against-main)) |

Recordings opens with the **Sync** panel ([Sharing a baseline](#sharing-a-baseline)); its Remote runs list has **Pull** and **Delete** per run, Delete disabled on the bucket's baseline of a type; **Push baseline** asks before replacing a remote pin whose run this Mac does not have (after a local delete and re-record, say), naming the bucket's run and yours: the bucket from the server's environment, masked to its last segment (`s3://*******-****-assets/screencatalog/`), or "not configured: set SCREENCATALOG_S3_BUCKET in the server's environment", or a note that the `aws` CLI is not on the server's PATH; **Push baseline** (`sync push`), **Pull** (`sync pull`), the last push and pull times, and **Remote runs**, folded, listing what the bucket holds with how each stands here and a **Pull** for a run missing or behind here. Push and pull run as jobs, one at a time with records and builds, in the header strip ("Pushing 1/2"); the `aws` output streams into the panel's log, a refusal ends the job with exit 2 and its reason, and the list refreshes when one ends. The page has no force: use the CLI's `--force`. `--sync-prefix` changes the prefix.

A row of Recordings is a run: **Baseline** with its revision for the pin, else **Run** (the only time shown for any run is when its capture started; a pin's own history, with the times of each pin, accept and update, is on the run's page); a partial run's filter and a stopped run's "stopped early" under it; screenshots per appearance, size, app `version (build)` and binary hash, git, recorded time. Its actions:

| action | does |
|---|---|
| **Open** | the run's gallery, `#recordings/<run>` |
| **Compare…** | a picker, "Compare <run> against": the type's pinned baseline first, tagged, then the type's other runs newest first; runs of other types are never listed. With no pin: "No baseline for iPhone 17 · iOS 27.0. Set one from a run in the list." With no other run of the type: "No other iPhone 17 · iOS 27.0 run to compare against. Record one." and a **Record** button for that type. A choice opens `#compare/<run>/<against>` |
| **Set as baseline** | `set-baseline`, after a confirm that names the pin it replaces; not on the pin. A partial or stopped run is refused with the module's message, without a confirm. Disabled, with the reason as its tooltip, for a run with no commit or with uncommitted changes, and while the run's comparison with the pin has changes no reviewer confirmed ([Review](#review)); a run off main, or whose commit this clone lacks, says so in the tooltip and is refused by the server after it fetches main ([Only main is pinned](#only-main-is-pinned)); without a comparison the server's refusal ("compare it first") is shown |
| **Update baseline** | only on a partial run of a type with a pin: `update-baseline`, after a confirm with its screenshot count and the revision it makes; gated by the review like Set as baseline |
| **Export** | `export`: `screencatalog_<run-id>.zip` next to `runs/` |
| **Push** | `sync push <run>`, as a job in the status strip (for the pinned run this also pushes `baselines.json`, asking first when it would replace a remote pin this Mac does not have); disabled, with the reason as its tooltip, for a run capturing, stopped or partial (the pin is always pushable) and while Sync is not configured. Under the run, a muted "pushed 8 Oct 14:50 (r2) · pulled …" line from `sync.json` |
| **Delete** | `delete-run`, after a confirm; disabled only while the run captures. On the pin the confirm says "This is the baseline for iPhone 17 · iOS 27.0. After deleting it there is no baseline for that type until you set one. Delete?"; afterwards the type's heading reads "no baseline: set one from a run below" (amber, as for any type without a pin), the Record card's status line for that type says "No baseline for iPhone 17 · iOS 27.0: set one from a run in Recordings.", and **Compare…** on its runs shows the "No baseline…" empty state |

**A comparison** (`#compare/<run>/<against>`) is computed when the page opens, or reused when it is current; while it computes the page says "Comparing 120 / 300 · ETA 40 s" (pairs judged of all; the ETA is the rate of the decoded pairs so far) with a **Cancel** button, the header strip says so too, and a record can run meanwhile. **Cancel** stops it once the pairs in flight finish (under a second) and stores nothing: no partial compare folder is left, a stored comparison of the pair stays as it was, and the page says "Cancelled" until **Recompute**. Since a changed run or a new baseline revision is a different key, the page just recomputes: there is no stale state. It shows "Run <id> against Baseline r2 (8 Oct 14:46)" (or "against run <id>"), both runs' recorded time, device, filter, app, git and id, the tolerance, an iOS build note when the builds differ, then **Failures** as cards (Side by side / Overlay / Swipe, each opening the viewer with baseline, current, diff and overlay layers), the counts, the MISSING, ERROR and SKIPPED rows, and PASS folded away. **Accept as baseline…** on a card (only when `<against>` is the pin) copies that screen into the pin, a new revision, and the comparison recomputes against it. **Recompute** judges again with `force`.

**A gallery** (`#recordings/<run>`) shows the run's screenshots by feature, with the appearance switch, feature chips, name filter and size; a thumbnail opens the lightbox (zoom, pan, ← →, `T` light / dark). The pin's gallery carries its history (`r1 → r2` per update or accept, the pins). When a comparison of the run with its type's pin is current, **show diff against baseline** adds the baseline, diff and overlay layers to its tiles and the viewer; otherwise **Compare with baseline** computes one.

Thumbnails (~240 px) and overlay masks are made on first request in a process pool and cached under `.thumbs/runs/<run>/…` and `.thumbs/compares/<key>/…` (masks in `mask/`), keyed by the source's mtime (a mask by both inputs' and the threshold), so they go with their run or comparison; deleting `.thumbs/` is always safe.

### Clones

A record on *N* simulators uses one simulator of the type (the **template**) and *N* − 1 clones named `screencatalog-clone-1`, `screencatalog-clone-2`, …. The template is a booted simulator of the type, else a shut-down one, which is booted; a clone is never the template while another exists. A missing clone is made with `xcrun simctl clone <template>` (which refuses a booted source, so the template is shut down for it and booted again); an existing one is reused, and gets the template's app installed when its own differs. Clones are booted for the record and shut down, not deleted, when it ends. **Delete clones** deletes them.

**A record of a build that already has a run is refused.** <a id="a-record-of-a-build-that-already-has-a-run"></a>Two complete full runs of one simulator type from the same commit, the same app binary hash and the same capture mode are the same pixels, so before anything is captured a record looks for one in this catalog and, when `SCREENCATALOG_S3_BUCKET` is set and the `aws` CLI is on PATH, in the bucket, and refuses with its id ("a complete run of this build exists already: ui-20261009-120441 (in the bucket) …"; the web UI's record ends with that line and a link, or "Pull it from Recordings"). A run from a dirty tree never counts, since its commit does not describe what was built; nor does a partial or stopped one. There is no override: to record a build again (a determinism check), delete the run that exists, with Delete on Recordings or `delete-run` for a local one, and **Delete** on the Remote runs panel (`sync delete`) for one that is only in the bucket. A bucket that cannot be read is a warning, not a refusal.

**A record builds and installs first.** Before any simulator is touched it runs an incremental `xcodebuild` of the scheme in the config's `build` section (`build.project` or `build.workspace`, `build.scheme`, `build.configuration`, plus `build.extraArgs`, and with the variables in `build.unsetEnv` removed from its environment), run in the config file's folder (a failed build ends the record with nothing captured; the record's log shows each distinct compiler error once and the warning count, and the full `xcodebuild` output is in `build-logs/<run-id>.log`), then compares the built binary's hash with the template's installed app and installs the build only when they differ or the template has none ("iPhone 17 already has this build (…)" or "… has build …: installing …" in the log); clones get the template's app as above. So changing the model between records needs nothing: the build is a no-op and the new simulator gets an install. **Skip the build** skips only the `xcodebuild`: the install check still uses the last Debug build in DerivedData, and with none there the simulators keep the app they have. A record with the build skipped on a template that has no app and no build to install is refused (`409`).

### API

JSON on the same port. Errors are `{"error": "<message>"}` with 400 (bad input, two types, a run against itself), 404 (unknown run or comparison) or 409 (refused by the run model, with its message; a record already active).

| endpoint | contract |
|---|---|
| `GET /api/runs` | `{"catalog", "runs": [...], "groups": [{"deviceDir", "model", "os", "typeLabel", "pinned", "runs": [ids]}], "baselines": {deviceDir: run}, "sync", "main"}`. Each run: `id`, `title` (`Baseline` / `Run`), `pinned`, `revision`, `revisionAt`, `started`, `finished`, `source`, `deviceDir`, `model`, `os`, `osBuild`, `typeLabel`, `filter {only, feature, label}`, `appearance`, `partial`, `stopped`, `active`, `complete`, `counts`, `statuses`, `app`, `appSha`, `git`, `simulators`, `comparable` (the type's other runs, newest first), `baselineCompare` (its current comparison with the pin, or null), `bytes` |
| `GET /api/run?id=<run>` | `{"run": <the run, with history>, "variants": [{name, feature, light, dark}], "imageSize", "pin": {id, revision, revisionAt} \| null, "against": {id, key, revision, counts} \| null}`; a shot has `status`, `reason`, `url`, `thumbUrl` and `compare` (the current comparison with the pin's `status`, `reason` and layer URLs, or null) |
| `POST /api/run` | `{"mode": "record", "deviceType": "<id>", "clones": 1–8, "skipBuild"?: false, "appearance", "filters": {only, feature}, "settleMs", "budgetMinutes"}` (or `"udids": [...]` instead of `deviceType`) → 202 `{runId, mode, run: null}`; `runId` is the job's id (`ui-…-record`); the run's own id is minted when the capture starts, after the build, and arrives as `summary.run` on `/api/status`. `mode: "compare"` is a 400 pointing at `/api/compare`. A finished record's `/api/status` `summary` has `run`, `pinned`, `comparable` |
| `POST /api/compare` | `{"run", "against"?, "force"?}` (`against` omitted: the pin) → 200 `{"status": "ready", run, against, key, againstRevision, reused}` when current or done within 1.5 s, else 202 `{"status": "computing", run, against, task}` |
| `POST /api/compare/cancel` | `{"run", "against"}` → `{"cancelled": true, run, against, error}` once the pair's computing comparison has stopped and its stage folder is removed; `{"cancelled": false}` when none was computing; 409 if it has not stopped within 30 s |
| `GET /api/results?run=&against=` | `status` `ready` (`key`, `againstRevision`, `comparedAt`, `seconds`, `settings`, `notes`, `runPartial`, `runStopped`, `counts`, `rows` with `currentUrl`, `baselineUrl`, `diffUrl`, thumbnails and `maskUrl`), `computing` (`task`: `done`, `total` pairs, `perPairMs`, `eta` seconds, both null until a decoded pair finishes), `error`, `cancelled` or `needed`; always `run`, `against` and `againstPinned` |
| `POST /api/accept` | `{"run", "against", "name", "appearance"}`; `against` must be the pin and the screen's change confirmed (409 otherwise) → `{"event", "baseline", "revision", "status": "computing"}` |
| `POST /api/set-baseline` | `{"run", "allowPartial"?, "allowStopped"?}` → `{"deviceDir", "pinned", "previous", "review", "main"}` (the review counts, or null for a type's first pin; where the run's commit stands against main, as fetched); 409 with the reason while the run's commit is not on main, or with the unconfirmed changes while the review gate holds |
| `POST /api/review` | `{"run", "against"?, "name", "appearance", "kind": "expect"\|"confirm"\|"reject"\|"withdraw", "note"?}`: one review event on that screen's change in the run's current comparison, by the server's identity → `{"name", "appearance", "review", "summary", "blockers"}`. 400 for bad input, 409 for no current comparison, a screen that is not a change, no identity, or an event that changes nothing (the module's reason: "the author cannot confirm their own change") |
| `POST /api/update-baseline` | `{"run"}` → the `update` history event with `baseline` |
| `POST /api/export` | `{"run"}` → `{"run", "path", "sizeKiB"}` |
| `POST /api/runs/delete` | `{"run"}` → `{"deleted", "bytes", "compares", "unpinned", "notice"}` (`unpinned`: the deviceDir whose pin went with the run, else null); 409 for a run capturing or one whose comparison is computing |
| `GET /api/sync/status` | `{"configured", "kind" (`s3` or `directory`), "label" (how the page names the store: `s3://<masked bucket>/<prefix>/` or the folder's path), "store" (what messages call it: `the bucket` or `the shared folder`), "bucket" (masked, or the folder; null when not configured), "prefix", "env": "SCREENCATALOG_S3_BUCKET", "aws" (the CLI is on PATH), "lastPush", "lastPull", "baselines": {pushedAt, pulledAt}, "runs": {run: {pushedAt, pushedRevision, pulledAt, pulledRevision}}}`. `/api/runs` carries it as `sync`, and each run its own entry as `sync` and `pushBlocked` (why **Push** is disabled, or null) |
| `GET /api/sync/remote` | `{"bucket", "prefix", "baselines", "runs": [{id, revision, revisionAt, deviceDir, model, os, started, partial, stopped, pinned, counts, local: missing\|behind\|same\|ahead\|diverged, localRevision}], "fetchedAt", "cached"}`, cached 30 s (`?refresh=1` skips it; a push or pull drops it); 409 when not configured |
| `POST /api/sync/push` | `{"runs"?: [ids], "all"?: bool, "force"?: bool, "replacePins"?: bool}` (`replacePins` lifts only the rule that refuses `baselines.json` when the bucket pins a type to a run this Mac does not have; the page's **Push baseline** asks before setting it, naming both runs) → 202 `{runId, mode: "push"}`, a job like a record's; its `/api/status` `summary` has `pushed` and `baselines`. 400 for another field or a bad id, 409 when not configured, no `aws`, or a job is active |
| `POST /api/sync/delete` | `{"runs": [ids]}` → `{"deleted": [ids]}`, synchronously (a few seconds per run); 400 for no runs or a bad id, 404 for an id the bucket lacks, 409 for the bucket's baseline of a type or Sync not configured. The cached remote listing is dropped |
| `POST /api/sync/pull` | `{"runs"?: [ids], "force"?: bool}` → 202 `{runId, mode: "pull"}`; `summary` has `pulled` and `pins` (the types whose pin changed). It holds the lock accept, update and delete take |
| `GET /api/device-types` | `{"deviceTypes": [{"id", "model", "os", "osBuild", "deviceDir", "available", "booted", "clones", "isBaseline", "baselineOsBuild", "pinned": {run, revision, revisionAt, counts, …} \| null}], "host": {ramGB, freeGB, swapUsedGB, swapTotalGB, bootedSimulators, recommendedClones}}`, newest iOS first; `host` is read per request, its memory fields `null` and `recommendedClones` 2 when they cannot be read |
| `GET /api/status?since=<seq>` | the record, build, push or pull job (`active`, `mode`, `done`, `total`, `eta`, `lines`, `summary`, …) and `compares`, the comparisons computing |
| also | `GET /api/menu?deviceType=`, `/api/exclusions`; `POST /api/clones/delete`, `/api/stop` |

Images: `/img/runs/<run>/<appearance>/<name>.png` and `/img/compares/<key>/diff/<appearance>/<name>.png`, their thumbnails under `/thumb/…`, and `/mask/<key>/<appearance>/<name>.png` (`&size=thumb` for the card-sized one). Every other path, `..`, a symlink out of its folder and a run or comparison folder that resolves outside `runs/` or `compares/` is refused.

## Where it lives

The catalog is the folder named by `catalogDir` in the config (default `.screencatalog/` next to the config file; `$SCREENCATALOG_DIR` and `--catalog-dir` override it). Add it to `.gitignore`: screenshots are shared through the store ([Sharing a baseline](#sharing-a-baseline)), not through git.

```
.screencatalog/
  runs/<run-id>/                       one recorded run; immutable unless it is the pinned baseline
    run.json                           id, source (cli|ui), started, finished, deviceType {model, os, osBuild, deviceDir},
                                       simulators [{udid, name, port, appModified, appSha256}],
                                       app {version, build, sha256, modified, path}, git {sha, dirty, ignored?},
                                       filter {only, feature, label}, appearance, counts {light: n, dark: n} (screens with a
                                       screenshot), statuses, captureMode (simctl|app; absent on older runs = simctl), settleMs, maxWaitMs, minLaunchMs, minShownMs, bandRetakes, bandIntervalMs, elapsedSeconds, stopped,
                                       revision, revisionAt, history
    manifest.json                      one row per variant and appearance: name, feature, appearance, status
                                       (RECORDED, ERROR, SKIPPED), file, settleMs, settled, durationMs, error, udid,
                                       shownMs and shownAt (launch to the variant being reported as shown, and when; null if never),
                                       shots and unstableBands (a capture's screenshots, and whether its
                                       compositor bands never held still), captureMode;
                                       a row copied in by update-baseline or accept adds from and copiedAt
    light/<Feature>.<variant>.png
    dark/<Feature>.<variant>.png
    logs/                              each launch's stdout and stderr
  baselines.json                       {"<deviceDir>": "<run-id>"}: the pinned run per simulator type
  compares/<run-id>__<other-run-id>__r<revision>/
    compare.json                       key, run, against, againstRevision, comparedAt, settings (channelThreshold,
                                       maxDiffPercent, exclusions digest), stamps (both runs' revision and manifest
                                       mtime: what reuse checks), notes, counts, rows (each with current, baseline and
                                       diff paths relative to the catalog)
    diff/<appearance>/<Feature>.<variant>.png                      FAILs only
    report.html                        with compare --report only
  screencatalog_<run-id>.zip        from export / record --zip
  sync.json                            the masked bucket, the prefix, and the last push / pull per run and of baselines.json
  build-logs/<run-id>.log              the full xcodebuild output of a record's build; the record's log keeps a summary
                                       (each distinct error once, the verdict lines, the warning count). Never synced
  reviews/<name>__<appearance>__<current16>__<baseline16>/
    <utc-stamp>-<kind>-<who>-<id>.json  one review event (schema 1): pair, kind, by {name, email}, at, note, run, against,
                                       againstRevision. Written once, never rewritten; synced both ways
  .thumbs/runs/<run-id>/…, .thumbs/compares/<key>/…                the web UI's cache; deleting it is always safe
```

## Sharing a baseline

The team shares runs through S3: `sync push` and `sync pull` (and the **Sync** panel on Recordings, [Web UI](#web-ui)) mirror three things, and only three, to `s3://$SCREENCATALOG_S3_BUCKET/screencatalog/`:

```
s3://<bucket>/screencatalog/
  baselines.json                 the pinned run per simulator type
  runs/<run-id>/                 run.json, manifest.json, light/, dark/ (never logs/)
  reviews/<pair>/<event>.json    every review event ([Review](#review)); pushed and pulled on every sync, never deleted
```

Comparisons, thumbnails, launch logs, `sync.json` and zips are never synced: each Mac computes its own comparisons from the runs. Runs are immutable except the pinned one, whose `run.json` `revision` rises with every accept and update, so the revision is what both directions check.

One-time setup for S3: install the CLI (`brew install awscli`), give it credentials the usual way (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_DEFAULT_REGION`, or a profile), and name the bucket in `$SCREENCATALOG_S3_BUCKET` or `storage.bucket` in the config. The tool reads only the bucket name (`--bucket` overrides it, `--prefix` or `storage.prefix` the `screencatalog` prefix; `storage.endpointUrl`, `storage.profile` and `storage.region` are passed to the CLI, which is how an S3-compatible store such as R2 or MinIO is used); the `aws` CLI reads the credentials itself, and nothing prints them. The bucket is masked to its last segment (`*******-****-assets`) in every line the tool prints.

Without S3, set `"storage": {"type": "directory", "path": "<folder>"}`: the same three things are mirrored into that folder (a network share, a synced drive), with the same commands and the same conflict rule. Files are copied when their size or modification time differs and nothing is ever deleted by a push or a pull. This page says "the bucket" for the store throughout; with a folder store the tool's and the web UI's messages say "the shared folder" instead.

```bash
python3 tool/screencatalog.py sync push                      # every pinned run, then baselines.json
python3 tool/screencatalog.py sync push cli-20261008-140000  # those runs (not baselines.json)
python3 tool/screencatalog.py sync push --all                # every finished run, partial and stopped too, then baselines.json
python3 tool/screencatalog.py sync pull                      # baselines.json, and every pinned run missing here or behind in revision
python3 tool/screencatalog.py sync pull cli-20261008-140000  # those runs
python3 tool/screencatalog.py sync pull --list               # JSON: the remote runs, their facts, and how each stands here
```

Each run folder goes up or down with `aws s3 sync --exact-timestamps --exclude 'logs/*'` (only changed files move, so a repeat is cheap and idempotent, and a run whose remote `run.json` is the same revision made at the same moment is skipped outright with "is already on the remote: nothing to push", as is an unchanged `baselines.json`; the per-file `upload:` / `download:` lines are counted rather than shown, a progress line every 100 and a total at the end), its `run.json` last as an `aws s3 cp`: a reader that sees the new `run.json` sees every file it names, and an interrupted pull leaves the old revision's `run.json`, so the next pull fetches it again. A run new to this Mac is pulled into a staging folder and appears whole. `baselines.json` goes with `aws s3 cp`, after the runs it pins.

**What is pushed.** By default the pinned runs (whatever their filter: they were pinned on purpose). Never a run still capturing; never one that did not finish; a stopped or partial run only with `--all` (which, with run ids, lets those be partial or stopped). A push of `baselines.json` replaces the pins of the types pinned here and keeps the remote's pins of other types.

**The conflict rule.** Everything is checked before anything moves; a refusal exits 2 and transfers nothing.

- `push` downloads each run's remote `run.json` first. If its revision is higher than yours: `error: <run>: remote baseline is r<n>, yours is r<m>: pull first`.
- `push` refuses `baselines.json` when the remote pins one of your pinned types to a run you do not have (someone pinned a newer run): pull first.
- `pull` refuses to overwrite a local run whose revision is higher than the remote one (`… yours is r<m>, remote baseline is r<n>: push it, or pull --force to overwrite yours`), and a run still capturing here.
- Both refuse a run that is at the same revision on both sides but was changed separately (its `revisionAt` differs): two people accepted from the same revision. Decide whose wins, then `--force` that direction.

`--force` overrides the refusals for that command. A pull that pins a different run for a type says so (`baseline of <type>: <run> (was <run>, which stays as a run)`); the run that was pinned here stays as an ordinary run.

`sync.json` in the catalog records the masked bucket, the prefix, and the last push and pull per run (with the revision) and of `baselines.json`; the web UI shows it. It is never synced.

### Where a run stands against main

Every run records the commit it was built from (`git.sha` and `git.dirty` in `run.json`; a file listed in `git.buildChurnPaths` in the config, one a build rewrites that is not an input of the binary, does not count as dirty and is listed as `git.ignored` instead), and the server reads, from the repository it runs in, where that commit stands against **main as this clone last saw it**: `origin/main` (as of the last fetch; the listing never fetches, the baseline gate below does), else a local `main`. `GET /api/runs` carries it as `main` on the listing (`{ref, sha, committedAt}`) and on every run, and `/api/run` and `/api/results` on their run facts: `{"ref", "state", "behind", "ahead"}` with `state` one of `at` (the run's commit is main's head), `behind` (on main, `behind` commits before its head), `off` (`ahead` of its commits are not on main: recorded on a branch) or `unknown` (the commit is not in this clone: fetch; or, with `gitFailed`, git could not count, which the gate refuses rather than reading as "at"). `null` when the run has no commit or the server's directory is not a clone.

The page shows it beside the pin on the Recordings heading ("baseline r2 · 9 Oct 14:50 · at origin/main"; amber for anything but `at`: "12 commits behind origin/main", "recorded off origin/main: 3 commits not on it, 12 behind", "commit not in this clone: git fetch", and "uncommitted changes" for a dirty run), on a run's page under its title, and in the facts above a comparison. It is what tells the team the baseline is stale, and it is what the baseline gate judges.

### Only main is pinned

A baseline means "main at a commit": a reference every teammate can rebuild. A run of a branch is not one, because a squash or a rebase rewrites the commit it names into one that exists nowhere, and a run of a dirty tree is not one, because its commit does not describe the build. So `set-baseline`, `update-baseline` and `accept` (and their API routes and buttons) refuse a run unless it was recorded from a clean checkout of a commit that is on main (`at` or `behind`). The gate runs `git fetch origin main` first, so a clone that has not fetched since the merge does not refuse a fresh commit of main (offline, it prints `could not fetch origin main: judging against main as this clone last saw it` and judges against `origin/main` as it stands; a commit the clone lacks is then refused with "fetch"). The message names the reason: "ui-20261009-151036 cannot be the baseline: it was recorded off origin/main: 3 commits of 0c7dfd760 are not on it. A baseline means main at a commit: merge first, then record off main and pin that run". The pin, update and accept events record the standing as `main` (`{ref, state, behind, ahead}`). There is no override, on the CLI or in the web UI: being behind is allowed and shown, being off is not. The first baseline of a type, and the one after a merge, is therefore always a record off a checkout of `main` ([README § Workflow](../README.md#workflow)). A review verdict is keyed by the images, not the run, so the branch run that was reviewed and the main run that is pinned share their verdicts when their pixels match.

## Review

A change is a screen the compare judged FAIL, or MISSING with a screen in the run (a new variant the baseline has no screen of). Before a run can become the baseline, or a screen be copied into it, every change needs two people: the **author** marks it expected, with a note on why, and a **reviewer**, anyone else, confirms it or rejects it with a reason. The web UI's compare page carries it: every change is a card (needing action first: open, rejected, expected, confirmed), with its state ("expected by Alex · “new header”"), the buttons the current identity may press (**Mark expected…** on an open or rejected change; **Withdraw** for its author, **Confirm** and **Reject…** for anyone else while it is expected; **Reject…** on a confirmed one), the event history as the state's tooltip, and above the cards the counts ("3 changes · 1 open · 1 expected · 1 confirmed · the run cannot become the baseline until every change is confirmed by a reviewer"). **Accept as baseline…** appears only on a confirmed change. On Recordings, **Set as baseline** and **Update baseline** are disabled while a run's comparison with the pin has unconfirmed changes, with the counts as the tooltip.

**A verdict is about the two images, not the run.** The key is the sha256 of the run's screen and of the baseline's (`none` for a new screen), with the variant name and appearance. Captures are byte-identical, so an author who re-records after a rebase, or a reviewer who pulls the run on their own Mac, sees the same pair and the same verdict; and when the baseline moves and its screen of that variant changes, the pair is new and the change is reviewed again, which is right because the diff is different.

**Rules** (`fold_review`): `open` → `expected` by an `expect` → `confirmed` by a `confirm` from someone other than the author, or `rejected` by a `reject` (a reason is required). A `withdraw` by the author reopens; after a reject a new `expect` is needed before a confirm; a confirmed change can still be rejected (it reopens as rejected). An event out of turn, a confirm by the author, a confirm of nothing, a second expect of a confirmed change, is refused and writes nothing. Identity is the git `user.name` / `user.email` of the repository the server or CLI runs in, or `SCREENCATALOG_REVIEWER="Name <email>"` (`review --as` on the CLI); two people are the same when their emails match, else their names. There is no authentication: it is a local tool over a shared bucket, and the event log says who and when, the same trust the git history carries.

**Storage and sync.** Each event is its own file under `reviews/<pair>/`, written once and never rewritten, named by its UTC time to the microsecond, so a folder is append-only and the two sides of a sync only ever gain files: `sync push` and `sync pull` always mirror `reviews/` in their direction, with no conflict rule needed. Events fold in time order (then file name), so every Mac reaches the same state.

**The gate.** After the main gate ([Only main is pinned](#only-main-is-pinned)), `set-baseline`, `update-baseline` and `accept` (and their API routes and buttons) refuse while the run's current comparison with the pin is missing, was judged at a tolerance other than the one in force (the defaults on the CLI, the server's own settings in the web UI: a looser compare would simply have no changes to review, so the message names both tolerances and asks for the compare again), has an ERROR screen (`set-baseline` only: `update-baseline` copies only captured screens), or has a change not confirmed; the message lists the first five. `--skip-review` on the CLI overrides, and the pin event records `skippedReview`; the web UI has no override. `compare --allow-confirmed` exits 0 when every FAIL or MISSING is confirmed, which is what a PR job checks; `review <run>` prints the changes with their states and exits 1 while any is not confirmed.

## Tolerance

A pixel **differs** when any of its red, green or blue channels moved by more than `--channel-threshold` (default 8, out of 255); alpha is ignored. A variant **FAILs** when the share of differing pixels is above `--max-diff-percent` (default 0.01), or when the two images are different sizes. Byte-identical files pass without being decoded.

### Compare performance

A compare reads both files of a pair and passes byte-identical ones (equal sizes first, then equal bytes) without decoding. The rest are decoded through **ImageIO** on macOS (CoreFoundation, ImageIO and CoreGraphics through `ctypes`: `CGImageSourceCreateWithData` → `CGImageSourceCreateImageAtIndex` → `CGDataProviderCopyData`, the stored samples with no colour matching), about 60 ms for a 1206 × 2622 screenshot instead of 0.4–0.65 s in pure Python. The pure-Python decoder stays as the fallback (not macOS, the frameworks do not load, or a layout ImageIO hands back differently: premultiplied or first alpha, little-endian, float, 16-bit, indexed); `SCREENCATALOG_PURE_PYTHON=1` forces it. Both give identical samples (a test checks synthetic PNGs of every colour type and filter, and a recorded screenshot). Rows are compared as bytes first; only differing rows are judged per pixel, all of them at once as 16-bit lanes of one big integer. Pairs are judged in a process pool of min(4, CPUs) workers (the web UI shares its thumbnail pool), which also write the diff PNGs; rows keep the run's order. `compare.json` records `seconds`.

Measured on a 12-core Mac with 4 workers: a compare of about 500 pairs takes about 4 s when three quarters of them are byte-identical, about 1 s when nearly all are, and about 20 s when every pair differs, on the CLI and in the web UI alike.

Know what the default lets through. On a 1206 × 2622 screenshot, 0.01 % is about 316 pixels: an 18 × 18 block, so a changed word or a moved icon fails while anti-aliasing noise passes. Raise `--max-diff-percent` (0.1 is about 3,160 pixels, a 56 × 56 block) only for a run that is meant to ignore small copy changes, and use `compare <run> --max-diff-percent <n>` to judge an existing run at a different tolerance without relaunching anything (a comparison stored with other settings is judged again).

Statuses:

| status | meaning | fails the run |
|---|---|---|
| PASS | within tolerance | no |
| FAIL | over tolerance, or a size change | yes |
| MISSING | captured in the run, but the run it is compared with has no screenshot of it in that appearance; the row's reason names it (`no screenshot of Home.default in baseline-iPhone_17_iOS_27.0-20261008`) | yes, unless `--allow-missing` |
| ERROR | the run has no screenshot of it: the app died, the in-app server never answered, or the screenshot failed | yes |
| SKIPPED | listed in the exclusions file; the row's reason is the exclusion's reason | no |

Exit code: `compare` exits 0 clean, 1 on any FAIL or ERROR (or MISSING without `--allow-missing`); `record` exits 1 when any capture was an ERROR or it stopped before capturing everything it selected; every command exits 2 for a usage or setup problem or a refused operation (no simulator, app not installed, bad exclusions file, nothing matched, an unknown run, runs of two types, a type with no pin).

## Reading the report

`compare --report` also writes `compares/<key>/report.html`, one self-contained file (images are embedded, nothing is fetched), so it can be attached or opened anywhere. It has the run's metadata, a PASS / FAIL / MISSING / ERROR / SKIPPED table per appearance, then one row per FAIL, ERROR and MISSING variant with the reason and three images side by side: the baseline, the current capture, and the diff. In the diff the baseline is dimmed to a third of its brightness and every differing pixel is pure red, so the change is wherever the red is. The SKIPPED variants are listed last with their reasons. With many failing rows the file is large, since each row embeds three full-size PNGs (about 1 MB per failing row), which is why it is opt-in; `compare.json` has the same rows.

The terminal prints the same totals and one line per FAIL, ERROR and MISSING.

## Exclusions

`screencatalog-exclusions.json` lists the variants that cannot render the same way twice:

```json
[
  { "name": "<Feature>.<variant>", "reason": "<what differs between two captures, and why>" }
]
```

They are SKIPPED, never captured, and listed in the report. **Every entry needs a reason**: the script refuses to start when one is missing or blank, because an exclusion without one is how a real flake, or a real regression, gets buried. A reason names the cause (a relative date, a random id, a countdown, an animation that does not stop), so whoever fixes the fixture knows when to delete the line.

Keep the file empty when you can: a cause is better handled at the source than skipped. [adopting.md](adopting.md#making-screens-deterministic) lists the usual causes (the clock, the network, remote images, animations, random values, timers) and how an app pins each under `ScreenCatalog.isCatalogRun`. Add an exclusion only for a cause none of those reach.

### Compositor artefacts

`simctl io screenshot` saves the display, and the system composites overlays onto it that are not the app's and can appear in some screenshots of a screen and not others: the **Dynamic Island** at the top and the **home indicator** at the bottom. The compare ignores no rows: the catalog must show what the app paints under the indicator, because a background that does not fill the safe area is a real bug. So there are two capture modes (`--capture`, recorded as `captureMode` in `run.json` and on each captured row):

- **`app` (the default): the app draws its own windows.** The in-app server's `GET /screenshot` ([protocol.md](protocol.md)) renders every visible window of the app's scene, sheets and covers included, in window-level order with `drawHierarchy(afterScreenUpdates: true)`, over black, at the screen's scale: the same size and 8-bit RGB as `simctl`. It never sees the status bar, the home indicator, the Dynamic Island or the keyboard, which are system windows, and its corners are square. Two records of the same build are the same bytes.
- **`simctl`: the display, with each overlay kept out as far as possible.**
  - *The Dynamic Island: `--mask=black` on every screenshot.* The island is sometimes in the framebuffer itself (it flashes in while something animates), so `simctl io screenshot` saves it in some captures and not others. `--mask=black` paints the whole cut-out black over whatever the framebuffer holds (`--screenshot-mask`, default `black`). The rounded corners are black too.
  - *The home indicator: a floor counted from the variant appearing.* The capture never screenshots before `max(launch + --min-launch-ms, shown + --min-shown-ms)` (step 4 of [What one capture does](#what-one-capture-does)). A floor reduces how often the bar is caught mid-fade; it cannot remove an overlay the compositor draws steadily, which is why `app` is the default.
- **Runs of two modes are not compared.** Every screen would differ by the status bar and corners, so `compare` and `update-baseline` refuse a pair whose `captureMode` differs, and the web UI does not offer one as a compare target. A run whose `run.json` has no `captureMode` counts as `simctl`. Switching the baseline's mode means recording and pinning a new full run.
- **The capture checks the bands, as a backstop, in both modes.** Two screenshots in a row must have the same top band (7 % of the height) and bottom band (3 %). The bands are fractions of the height, so they scale with the device, and span the full width. A pair that differs is retaken ([What one capture does](#what-one-capture-does), step 5). The check only sees something that changes between two screenshots 400 ms apart; in `app` mode there is no overlay to catch, but it still catches app content changing in those bands, and costs one 0.1 s render.

#### What `app` mode gives up

- **No system chrome.** The status bar, the Dynamic Island cut-out, the rounded corners and the home indicator are never in the picture, so a screen that collides with any of them (content under the status bar text, a button under the home indicator) is not caught here; `simctl` mode would show it. The area under the safe area is still captured, so a background that stops short of the edge still fails.
- **No keyboard.** The keyboard is a system window. A variant whose screenshot should show the keyboard shows the field without it.
- **A different renderer.** `drawHierarchy(afterScreenUpdates:)` renders the app's layer tree in-process; the display is composited by the render server. Materials and blur render. Video does not: an `AVPlayerLayer` is not expected to render through `drawHierarchy`, so a variant showing a playing video shows the layer's background. Compare the first such variant by eye with a `simctl` capture.
- **Only the app's own windows.** A system alert, the system share sheet or any other out-of-process UI is not rendered.

To triage a FAIL, capture the variant three times per appearance and diff the captures against the baseline and against each other: one that differs from the baseline in every capture, by a varying number of pixels, and whose captures differ from each other, animates (fix the view so it holds one frame in a catalog run); one whose three captures are identical to each other but not to the baseline drifted from it (re-record it); one that passes in all three was a one-off.
