# Configuration

Everything about the app under test lives in one JSON file, `screencatalog.json`. The tool reads it once at start.

## Which file is read

1. `--config <path>` on either command (`screencatalog.py`, `screencatalog_ui.py`), anywhere on the command line.
2. Else the file named by `$SCREENCATALOG_CONFIG`.
3. Else the first `screencatalog.json` found walking up from the working directory.
4. Else none: built-in defaults, and `record` refuses for want of a bundle id.

Relative paths in the file resolve against the file's folder. The git repository whose commit and "dirty" state a run records is the checkout containing that folder.

For any one value the precedence is: command-line flag, then environment variable (where one exists), then the file, then the built-in default.

## Keys

```json
{
  "app": { "bundleId": "com.example.MyApp.dev" },
  "catalogDir": ".screencatalog",
  "exclusionsFile": "screencatalog-exclusions.json",
  "build": {
    "project": "MyApp.xcodeproj",
    "workspace": null,
    "scheme": "MyApp",
    "configuration": "Debug",
    "extraArgs": [],
    "unsetEnv": [],
    "derivedDataPath": null
  },
  "git": { "remote": "origin", "mainBranch": "main", "buildChurnPaths": [] },
  "storage": { "type": "s3", "bucket": "my-team-artifacts", "prefix": "screencatalog" },
  "record": {
    "port": 8766,
    "appearances": ["light", "dark"],
    "capture": "app",
    "settleMs": 1500,
    "maxWaitMs": 10000,
    "minLaunchMs": 0,
    "minShownMs": 300,
    "healthTimeout": 20,
    "keepRuns": 5,
    "statusBar": { "time": "9:41", "batteryState": "charged", "batteryLevel": 100, "wifiBars": 3, "cellularBars": 4 }
  },
  "compare": { "channelThreshold": 8, "maxDiffPercent": 0.01 },
  "ui": { "port": 8899, "clonePrefix": "screencatalog-clone-", "maxClones": 8 }
}
```

Every key is optional except `app.bundleId` (or `--bundle`). The values above are the defaults, apart from the app, build and storage examples.

| key | meaning | flag / environment override |
|---|---|---|
| `app.bundleId` | the bundle id of the Debug build that links the package | `--bundle` |
| `catalogDir` | where runs, comparisons and reviews are kept. Add it to `.gitignore` | `--catalog-dir`, `$SCREENCATALOG_DIR` |
| `exclusionsFile` | the list of variants never captured, each with a reason ([reference](reference.md#exclusions)). A missing file means none | `--exclude-file` |
| `build.project` / `build.workspace` | what the web UI passes to `xcodebuild` as `-project` or `-workspace` (a workspace wins). The command runs in the config file's folder | |
| `build.scheme`, `build.configuration` | `-scheme` and `-configuration`. The built app is looked for under `<configuration>-iphonesimulator` | |
| `build.extraArgs` | extra `xcodebuild` arguments, e.g. `["-skipMacroValidation"]` | |
| `build.unsetEnv` | environment variables removed for the build, e.g. a token that breaks package resolution | |
| `build.derivedDataPath` | passed as `-derivedDataPath`, and the only place the built app is then looked for. Without it the tool searches Xcode's DerivedData for the folder built from the configured project or workspace | |
| `git.remote`, `git.mainBranch` | the branch a baseline must be on ([reference](reference.md#only-main-is-pinned)). The gate fetches `<remote> <mainBranch>` and judges against `<remote>/<mainBranch>`, else a local `<mainBranch>` | |
| `git.buildChurnPaths` | tracked files a build rewrites that are not inputs of the binary; a change to one does not make a run "dirty" | |
| `storage.type` | `s3` (through the `aws` CLI) or `directory` (a shared folder) | |
| `storage.bucket`, `storage.prefix` | the bucket and key prefix for `s3` | `--bucket` / `$SCREENCATALOG_S3_BUCKET`, `--prefix` |
| `storage.endpointUrl`, `storage.profile`, `storage.region` | passed to the `aws` CLI as `--endpoint-url`, `--profile`, `--region`: an S3-compatible store, a named profile | |
| `storage.path` | the folder for `directory` | |
| `record.port` | the in-app server's port; with several simulators, simulator *i* uses port + *i* | `--port` (`--bridge-port` on the web UI) |
| `record.appearances` | `["light", "dark"]`, or one of them | `--appearance` |
| `record.capture` | `app` or `simctl` ([reference](reference.md#compositor-artefacts)) | `--capture` |
| `record.settleMs`, `record.maxWaitMs` | the quiet period the recorder waits for, and its cap | `--settle-ms`, `--max-wait-ms` |
| `record.minLaunchMs`, `record.minShownMs` | floors before the screenshot, counted from the launch and from the variant being shown | `--min-launch-ms`, `--min-shown-ms` |
| `record.healthTimeout` | seconds to wait for the in-app server after a launch | `--health-timeout` |
| `record.keepRuns` | runs kept per source and simulator type after a record; 0 keeps all | `--keep-runs` |
| `record.statusBar` | the `simctl status_bar override` values, merged over the defaults. Only visible in `simctl` capture mode | |
| `compare.channelThreshold`, `compare.maxDiffPercent` | the tolerance ([reference](reference.md#tolerance)) | `--channel-threshold`, `--max-diff-percent` |
| `ui.port` | the web UI's port on 127.0.0.1 | `--port` |
| `ui.clonePrefix`, `ui.maxClones` | the name prefix and the cap of the simulators the web UI clones for parallel records | |

## Environment variables

| variable | meaning |
|---|---|
| `SCREENCATALOG_CONFIG` | the config file |
| `SCREENCATALOG_DIR` | the catalog folder, over `catalogDir` |
| `SCREENCATALOG_S3_BUCKET` | the bucket, over `storage.bucket` |
| `SCREENCATALOG_REVIEWER` | `"Name <email>"`: the review identity, instead of the git user ([reference](reference.md#review)) |
| `SCREENCATALOG_PURE_PYTHON` | `1` forces the pure-Python PNG decoder |

AWS credentials are never read by the tool; the `aws` CLI finds them itself.
