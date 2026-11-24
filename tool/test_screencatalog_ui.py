#!/usr/bin/env python3
"""
Tests for the web UI in screencatalog_ui.py over the run model, and for the parallel-simulator
sharding in screencatalog.py: the run listing grouped by simulator type with the pin per type
(/api/runs, /api/run), which runs a run can be compared with (never another type) and the picker's
empty states, /api/compare computing in the background and reusing a current comparison, accept
copying into the pinned baseline and the comparison recomputing, set-baseline / update-baseline /
delete / export and their refusals, the path rules of
/img/, /thumb/ and /mask/ for runs/ and compares/, the thumbnail cache and the overlay mask, clone
fleets, the Sync endpoints and panel (the fake `aws` of test_screencatalog), and the page's structure (two landing cards, no Compare workflow page, Failures before the
counts). Only the simulator is faked; records go through the real run_catalog and comparisons
through the real compare_runs.

Run from tool/: python3 -m unittest test_screencatalog_ui
"""

# The scripts run under whatever `python3` is on PATH; Xcode ships 3.9.6.
from __future__ import annotations

import argparse
import contextlib
import http.client
import io
import json
import os
import plistlib
import shutil
import tempfile
import threading
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import test_screencatalog as tsc  # first: it pins the config the modules load
import screencatalog as sc
import screencatalog_ui as ui

UDID_A = "11111111-2222-3333-4444-555555555555"
UDID_B = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
DEVICE = "iPhone_17_iOS_27.0"
T17 = "com.apple.CoreSimulator.SimDeviceType.iPhone-17"
T18 = "com.apple.CoreSimulator.SimDeviceType.iPhone-18-Pro"
IOS27 = "com.apple.CoreSimulator.SimRuntime.iOS-27-0"
WATCH = "com.apple.CoreSimulator.SimRuntime.watchOS-12-0"
TYPE17 = "iPhone-17@iOS-27-0"
TYPE18 = "iPhone-18-Pro@iOS-27-0"
BUNDLE = sc.DEFAULT_BUNDLE
KIND17 = ("iPhone 17", "iOS 27.0")
KIND18 = ("iPhone 18 Pro", "iOS 27.0")


class Collect(sc.Output):
    def __init__(self):
        self.lines, self.errors, self.progressed, self.final = [], [], [], None

    def line(self, text):
        self.lines.append(text)

    def error(self, text):
        self.errors.append(text)

    def progress(self, done, total):
        self.progressed.append((done, total))

    def rows(self, mode, rows):
        self.final = (mode, rows)


def png(value: int, block: bool = False) -> bytes:
    data = bytearray([value]) * (40 * 40 * 3)
    if block:
        for y in range(10):
            for x in range(10):
                data[(y * 40 + x) * 3:(y * 40 + x) * 3 + 3] = b"\xff\x00\x00"
    return sc.encode_png(sc.Image(40, 40, 3, bytes(data)))


class ShardingTests(unittest.TestCase):

    def test_round_robin(self) -> None:
        self.assertEqual(sc.shard_variants(list("abcdefg"), 3), [["a", "d", "g"], ["b", "e"], ["c", "f"]])
        self.assertEqual(sc.shard_variants(list("ab"), 3), [["a"], ["b"], []])

    def test_stable_and_complete(self) -> None:
        entries = [f"V.{i}" for i in range(23)]
        first, second = sc.shard_variants(entries, 4), sc.shard_variants(list(entries), 4)
        self.assertEqual(first, second)
        flat = [e for shard in first for e in shard]
        self.assertEqual(sorted(flat), sorted(entries))
        for shard in first:  # catalog order kept inside each shard
            self.assertEqual(shard, sorted(shard, key=entries.index))

    def test_single_simulator_is_the_whole_list(self) -> None:
        entries = [f"V.{i}" for i in range(5)]
        self.assertEqual(sc.shard_variants(entries, 1), [entries])


class CaptureTests(unittest.TestCase):
    """`capture` with the per-variant work and simctl faked."""

    def setUp(self) -> None:
        self.calls, self.simctl_calls = [], []
        self.lock = threading.Lock()
        originals = (sc.capture_variant, sc.simctl)
        self.addCleanup(lambda: (setattr(sc, "capture_variant", originals[0]), setattr(sc, "simctl", originals[1])))
        self.on_capture = None

        def fake_capture(args, bridge, udid, entry, appearance, out_root, log_dir, port=None):
            with self.lock:
                self.calls.append((udid, port, bridge.base, entry["name"], appearance))
            if self.on_capture:
                self.on_capture()
            time.sleep(0.01)
            target = out_root / sc.shot_relpath(appearance, entry["name"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png(10))
            return sc.variant_record(entry["name"], appearance, sc.RECORDED, udid=udid, settle_ms=1, settled=True,
                                     duration_ms=5)

        def fake_simctl(*args, check=True, timeout=60):
            with self.lock:
                self.simctl_calls.append(args)

        sc.capture_variant, sc.simctl = fake_capture, fake_simctl
        self.keep = [{"name": f"F.v{i}", "feature": "F"} for i in range(5)]
        self.skipped = [{"name": "F.skip", "feature": "F", "reason": "flaky"}]
        self.args = argparse.Namespace(port=9100, budget_minutes=None, bundle="b")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out_root = Path(tmp.name)

    def run_capture(self, udids, stop_event=None):
        out = Collect()
        rows, not_started = sc.capture(self.args, udids, self.out_root, self.keep, self.skipped, ["light", "dark"],
                                       self.out_root / "logs", out, stop_event)
        return rows, not_started, out

    def test_single_simulator_rows_and_lines(self) -> None:
        rows, not_started, out = self.run_capture([UDID_A])
        self.assertEqual(not_started, 0)
        self.assertEqual([(r["appearance"], r["name"], r["status"]) for r in rows],
                         [("light", f"F.v{i}", sc.RECORDED) for i in range(5)] + [("light", "F.skip", sc.SKIPPED)]
                         + [("dark", f"F.v{i}", sc.RECORDED) for i in range(5)] + [("dark", "F.skip", sc.SKIPPED)])
        self.assertEqual({r["udid"] for r in rows if r["status"] == sc.RECORDED}, {UDID_A})
        self.assertEqual(out.lines[0], "[1/10] light F.v0: RECORDED 5ms settle 1ms")
        self.assertEqual({c[1] for c in self.calls}, {9100})
        self.assertEqual(out.progressed[0], (0, 10))
        self.assertEqual(out.progressed[-1], (10, 10))

    def test_two_simulators_split_round_robin(self) -> None:
        rows, not_started, out = self.run_capture([UDID_A, UDID_B])
        self.assertEqual(not_started, 0)
        # Same row order as one simulator produces.
        self.assertEqual([(r["appearance"], r["name"]) for r in rows if r["status"] == sc.RECORDED],
                         [(a, f"F.v{i}") for a in ("light", "dark") for i in range(5)])
        for r in rows:
            if r["status"] == sc.RECORDED:
                index = int(r["name"][-1])
                self.assertEqual(r["udid"], UDID_A if index % 2 == 0 else UDID_B)
        ports = {(c[0], c[1], c[2]) for c in self.calls}
        self.assertEqual(ports, {(UDID_A, 9100, "http://127.0.0.1:9100"), (UDID_B, 9101, "http://127.0.0.1:9101")})
        appearance_calls = [c for c in self.simctl_calls if c[0] == "ui"]
        self.assertEqual(sorted(appearance_calls), sorted([("ui", u, "appearance", a) for u in (UDID_A, UDID_B)
                                                           for a in ("light", "dark")]))
        terminated = {c[1] for c in self.simctl_calls if c[0] == "terminate"}
        self.assertEqual(terminated, {UDID_A, UDID_B})
        progress_lines = [line for line in out.lines if line.startswith("[")]
        self.assertEqual(len(progress_lines), 10)
        self.assertEqual(sorted(int(line[1:line.index("/")]) for line in progress_lines), list(range(1, 11)))
        self.assertTrue(all(" sim1 " in line or " sim2 " in line for line in progress_lines))

    def test_stop_event_stops_every_shard(self) -> None:
        stop = threading.Event()
        count = {"n": 0}

        def after_two():
            count["n"] += 1
            if count["n"] >= 2:
                stop.set()

        self.on_capture = after_two
        rows, not_started, out = self.run_capture([UDID_A, UDID_B], stop)
        self.assertGreater(not_started, 0)
        self.assertLessEqual(len(self.calls), 3)  # at most one more in flight on the other simulator
        self.assertTrue(any(line.startswith("stopped early (stop requested)") for line in out.lines))

    def run_catalog_faked(self, udids):
        names = ("device_info", "app_binary", "enumerate_variants", "git_state")
        originals = {n: getattr(sc, n) for n in names}
        self.addCleanup(lambda: [setattr(sc, n, f) for n, f in originals.items()])
        sc.device_info = lambda udid: {"udid": udid, "simulatorName": "s", "osBuild": "b", "model": "iPhone 17",
                                       "os": "iOS 27.0"}
        sc.app_binary = lambda udid, bundle: {"bundleId": bundle, "path": "/x", "modified": "t", "sha256": "abc"}
        sc.enumerate_variants = lambda *a: self.keep
        sc.git_state = lambda: {"sha": None, "dirty": False}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        argv = ["record", "--catalog-dir", tmp.name, "--exclude-file", str(Path(tmp.name, "none.json")),
                "--appearance", "light", "--run-id", "par"]
        for udid in udids:
            argv += ["--udid", udid]
        out = Collect()
        self.assertEqual(sc.run_catalog(sc.parse_args(argv), out), 0, out.errors)
        path = Path(tmp.name, sc.RUNS_DIR, "par")
        return sc.read_rows(path), sc.read_run(path), [c for c in self.simctl_calls if c[0] == "status_bar"]

    def test_parallel_run_clears_each_status_bar_and_records_the_simulator(self) -> None:
        rows, run, status_bar = self.run_catalog_faked([UDID_A, UDID_B])
        self.assertEqual([c[1:3] for c in status_bar],
                         [(UDID_A, "clear"), (UDID_A, "override"), (UDID_B, "clear"), (UDID_B, "override")])
        self.assertEqual([r["udid"] for r in rows], [UDID_A, UDID_B, UDID_A, UDID_B, UDID_A])
        self.assertEqual([s["port"] for s in run["simulators"]], [8766, 8767])

    def test_single_run_clears_then_pins_status_bar(self) -> None:
        rows, run, status_bar = self.run_catalog_faked([UDID_A])
        self.assertEqual([c[1:3] for c in status_bar], [(UDID_A, "clear"), (UDID_A, "override")])
        self.assertEqual([s["udid"] for s in run["simulators"]], [UDID_A])

    def test_mixed_devices_are_refused(self) -> None:
        originals = (sc.device_info, sc.app_binary)
        self.addCleanup(lambda: (setattr(sc, "device_info", originals[0]), setattr(sc, "app_binary", originals[1])))
        sc.device_info = lambda udid: {"udid": udid, "simulatorName": "x", "osBuild": None,
                                       "model": "iPhone 17" if udid == UDID_A else "iPhone 17 Pro", "os": "iOS 27.0"}
        sc.app_binary = lambda udid, bundle: {"bundleId": bundle, "path": "/x", "modified": "t", "sha256": "abc"}
        with tempfile.TemporaryDirectory() as tmp:
            args = sc.parse_args(["record", "--udid", UDID_A, "--udid", UDID_B, "--catalog-dir", tmp,
                                  "--exclude-file", str(Path(tmp, "none.json"))])
            out = Collect()
            self.assertEqual(sc.run_catalog(args, out), 2)
        self.assertIn("same model and iOS version", out.errors[0])
        self.assertEqual(self.calls, [])


class FakeSimulator:
    """Patches the simulator half of screencatalog so `run_catalog` records for real without one:
    each capture writes `pngs[name]` where the real one would write the screenshot."""

    VARIANTS = [{"name": "A.same", "feature": "A"}, {"name": "B.changed", "feature": "B"}]

    def __init__(self, test: unittest.TestCase):
        names = ("device_info", "app_binary", "enumerate_variants", "git_state", "simctl", "capture_variant")
        originals = {n: getattr(sc, n) for n in names}
        test.addCleanup(lambda: [setattr(sc, n, f) for n, f in originals.items()])
        self.pngs = {"A.same": png(10), "B.changed": png(10, block=True)}
        sc.device_info = lambda udid: {"udid": udid, "simulatorName": "sim " + udid[:4], "osBuild": "24A434",
                                       "model": "iPhone 17", "os": "iOS 27.0"}
        sc.app_binary = lambda udid, bundle: {"bundleId": bundle, "path": "/x", "modified": "2026-10-07T09:59:37+00:00",
                                              "version": "2.0", "build": "7", "sha256": "06b3f4885b4e"}
        sc.enumerate_variants = lambda *a: list(self.VARIANTS)
        sc.git_state = lambda: {"sha": "0123456789abcdef", "dirty": False}
        sc.simctl = lambda *a, **k: None

        def capture(args, bridge, udid, entry, appearance, out_root, log_dir, port=None):
            target = out_root / sc.shot_relpath(appearance, entry["name"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.pngs[entry["name"]])
            return sc.variant_record(entry["name"], appearance, sc.RECORDED, udid=udid, settle_ms=1, settled=True,
                                     duration_ms=3)

        sc.capture_variant = capture


def make_run(catalog: Path, run_id: str, screens: dict, *, kind=KIND17, os_build="24A434", started=1, only=(),
             feature=(), appearance="both", stopped=False, pinned=False, git: dict | None = None) -> Path:
    """A run folder written by hand; `screens` is {(name, appearance): png bytes}. `started` orders runs."""
    path = catalog / sc.RUNS_DIR / run_id
    rows = []
    for (name, look), blob in screens.items():
        (path / sc.shot_relpath(look, name)).parent.mkdir(parents=True, exist_ok=True)
        (path / sc.shot_relpath(look, name)).write_bytes(blob)
        rows.append(sc.variant_record(name, look, sc.RECORDED, udid=UDID_A))
    sc.write_rows(path, run_id, rows)
    model, os_name = kind
    sc.write_run(path, id=run_id, source="cli", started=f"2026-10-08T10:{started:02d}:00+00:00", startedEpoch=started,
                 finished=f"2026-10-08T10:{started:02d}:30+00:00", active=False,
                 deviceType={"model": model, "os": os_name, "osBuild": os_build,
                             "deviceDir": sc.device_dir_name(model, os_name)},
                 simulators=[{"udid": UDID_A, "name": "sim", "port": 8766}],
                 app={"version": "2.0", "build": str(started), "sha256": f"sha{run_id}"[:12]},
                 git=git or {"sha": f"{started:040d}", "dirty": False},
                 filter={"only": list(only), "feature": list(feature), "label": sc.filter_label(list(only), list(feature))},
                 appearance=appearance, counts=sc.screen_counts(rows), statuses=sc.status_counts(rows), stopped=stopped,
                 captureMode=sc.DEFAULT_CAPTURE_MODE,
                 revision=1, revisionAt=f"2026-10-08T10:{started:02d}:30+00:00", history=[])
    if pinned:
        pins = sc.read_baselines(catalog)
        pins[sc.device_dir_name(model, os_name)] = run_id
        sc.write_baselines(catalog, pins)
    return path


def full(value: int = 10, changed: bool = False) -> dict:
    out = {(n, a): png(value) for n in ("A.same", "B.changed") for a in sc.APPEARANCES}
    if changed:
        out[("B.changed", "light")] = png(value, block=True)
    return out


class ServerCase(unittest.TestCase):
    """The HTTP API against a temp catalog: `base` (iPhone 17, pinned), `run` (iPhone 17, newer, one
    screen changed), `pro` (iPhone 18 Pro, no pin) and `part` (iPhone 17, B.changed light only).
    Records go through the real `run_catalog` with only the simulator faked."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = Path(self.tmp.name, "catalog").resolve()
        make_run(self.catalog, "base", full(), started=1, pinned=True)
        make_run(self.catalog, "run", full(changed=True), started=2)
        make_run(self.catalog, "pro", full(), kind=KIND18, started=3)
        make_run(self.catalog, "part", {("B.changed", "light"): png(10, block=True)}, only=["B."], appearance="light",
                 started=4)
        Path(self.tmp.name, "secret.png").write_bytes(png(1))
        self.sim = FakeSimulator(self)
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()

        def gated_runner(args, output, stop_event):
            self.started.set()
            self.gate.wait(10)
            return sc.run_catalog(args, output, stop_event)

        # `repo` is not a git clone, so no run has a standing against main (MainStandingTests cover that).
        # A record builds first: the fake builder succeeds without xcodebuild and "builds" self.built, an
        # .app whose binary differs from what FakeSimctl installs (b"code"), so a template gets an install.
        self.built = Path(self.tmp.name, "DerivedData", "Sample.app")
        self.built.mkdir(parents=True)
        (self.built / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": BUNDLE, "CFBundleExecutable": "Sample",
                                                                 "CFBundleShortVersionString": "2.0", "CFBundleVersion": "7"}))
        (self.built / "Sample.debug.dylib").write_bytes(b"built-code")
        self.builds: list[str] = []
        self.build_exit = 0

        def fake_builder(job, udid):
            self.builds.append(udid)
            job.add("** BUILD SUCCEEDED **" if self.build_exit == 0 else "error: boom")
            return self.build_exit

        self.app = ui.CatalogUI(self.catalog, exclude_file=Path(self.tmp.name, "none.json"), runner=gated_runner,
                                warm_thumbnails=False, thumb_processes=0, repo=Path(self.tmp.name), check_main=False,
                                builder=fake_builder, built_app_finder=lambda: self.built if self.built.is_dir() else None)
        self.server = ui.make_server(self.app, 0)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.app.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.gate.set)

    def request(self, method: str, path: str, body=None) -> tuple[int, object]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json"} if method == "POST" else {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        kind = response.getheader("Content-Type") or ""
        return response.status, (json.loads(data) if kind.startswith("application/json") else data)

    def ok(self, method: str, path: str, body=None, status: int = 200):
        code, data = self.request(method, path, body)
        self.assertEqual(code, status, data)
        return data

    def start(self, body: dict) -> dict:
        self.started.clear()
        started = self.ok("POST", "/api/run", body, 202)
        self.assertTrue(self.started.wait(5))
        return started

    def wait_idle(self) -> dict:
        for _ in range(400):
            status = self.ok("GET", "/api/status")
            if not status["active"]:
                return status
            time.sleep(0.05)
        self.fail("run never finished")

    def results(self, run: str, against: str | None = None) -> dict:
        """The comparison once it is ready (computing it first if needed)."""
        body = {"run": run, **({"against": against} if against else {})}
        self.assertIn(self.request("POST", "/api/compare", body)[0], (200, 202))
        query = f"/api/results?run={run}" + (f"&against={against}" if against else "")
        for _ in range(400):
            data = self.ok("GET", query)
            if data["status"] != "computing":
                self.assertEqual(data["status"], "ready", data)
                return data
            time.sleep(0.05)
        self.fail("comparison never finished")

    def runs(self) -> dict:
        return {r["id"]: r for r in self.ok("GET", "/api/runs")["runs"]}

    ALEX = {"name": "Alex", "email": "alex@example.com"}
    SAM = {"name": "Sam", "email": "sam@example.com"}

    def review(self, who: dict, kind: str, run: str, name: str, appearance: str = "light", against: str | None = None,
               note: str | None = None, status: int = 200):
        self.app.user_override = who
        body = {"run": run, "name": name, "appearance": appearance, "kind": kind}
        if against:
            body["against"] = against
        if note is not None:
            body["note"] = note
        return self.ok("POST", "/api/review", body, status)

    def approve(self, run: str, name: str, appearance: str = "light", against: str | None = None) -> None:
        """The whole lifecycle for one change: compared, expected by Alex, confirmed by Sam."""
        self.results(run, against)
        self.review(self.ALEX, "expect", run, name, appearance, against, note="intended")
        self.review(self.SAM, "confirm", run, name, appearance, against)


class ListingTests(ServerCase):

    def test_runs_are_grouped_by_type_with_the_pin_and_newest_first(self) -> None:
        make_run(self.catalog, "old-ios", full(), kind=("iPhone 17", "iOS 26.2"), started=5)
        body = self.ok("GET", "/api/runs")
        self.assertEqual([r["id"] for r in body["runs"]], ["old-ios", "part", "pro", "run", "base"])
        self.assertEqual([(g["typeLabel"], g["pinned"], g["runs"]) for g in body["groups"]],
                         [("iPhone 17 · iOS 27.0", "base", ["part", "run", "base"]),
                          ("iPhone 18 Pro · iOS 27.0", None, ["pro"]),
                          ("iPhone 17 · iOS 26.2", None, ["old-ios"])])
        self.assertEqual(body["baselines"], {DEVICE: "base"})
        runs = {r["id"]: r for r in body["runs"]}
        base, part = runs["base"], runs["part"]
        self.assertEqual((base["title"], base["pinned"], base["revision"], base["counts"]), ("Baseline", True, 1,
                                                                                               {"light": 2, "dark": 2}))
        self.assertEqual((runs["run"]["title"], runs["run"]["pinned"]), ("Run", False))
        self.assertEqual((part["partial"], part["filter"]["label"], part["appearance"]), (True, "B.", "light"))
        self.assertEqual((base["app"], base["appSha"], base["typeLabel"]), ("2.0 (1)", "shabase", "iPhone 17 · iOS 27.0"))
        self.assertGreater(base["bytes"], 0)
        for key in ("id", "title", "pinned", "revision", "revisionAt", "started", "finished", "source", "deviceDir",
                    "model", "os", "osBuild", "typeLabel", "filter", "appearance", "partial", "stopped", "active",
                    "complete", "counts", "app", "appSha", "git", "simulators", "comparable", "baselineCompare", "bytes"):
            self.assertIn(key, base)

    def test_comparable_is_one_type_only(self) -> None:
        runs = self.runs()
        self.assertEqual(runs["run"]["comparable"], ["part", "base"])  # never pro, an iPhone 18 Pro run
        self.assertEqual(runs["base"]["comparable"], ["part", "run"])
        self.assertEqual(runs["pro"]["comparable"], [])
        # An iPhone 18 Pro run is not offered against the iPhone 17 pin, and the server refuses the pair too.
        code, body = self.request("POST", "/api/compare", {"run": "pro", "against": "base"})
        self.assertEqual(code, 400, body)
        self.assertIn("cannot compare pro (iPhone 18 Pro, iOS 27.0) with base (iPhone 17, iOS 27.0)", body["error"])
        code, body = self.request("POST", "/api/compare", {"run": "pro"})
        self.assertEqual(code, 409, body)
        self.assertIn("no baseline is pinned for iPhone 18 Pro · iOS 27.0", body["error"])

    def test_run_detail_and_the_gallery_diff_layers(self) -> None:
        detail = self.ok("GET", "/api/run?id=run")
        self.assertEqual((detail["run"]["id"], detail["pin"]["id"], detail["against"]), ("run", "base", None))
        self.assertEqual(detail["imageSize"], [40, 40])
        b = next(v for v in detail["variants"] if v["name"] == "B.changed")
        self.assertTrue(b["light"]["url"].startswith("/img/runs/run/light/B.changed.png?v="))
        self.assertTrue(b["light"]["thumbUrl"].startswith("/thumb/runs/run/light/B.changed.png?v="))
        self.assertIsNone(b["light"]["compare"])
        self.results("run")
        detail = self.ok("GET", "/api/run?id=run")
        self.assertEqual((detail["against"]["id"], detail["against"]["key"], detail["against"]["revision"]),
                         ("base", "run__base__r1", 1))
        self.assertEqual(detail["against"]["counts"]["all"], {"PASS": 3, "FAIL": 1})
        layers = b_light = next(v for v in detail["variants"] if v["name"] == "B.changed")["light"]["compare"]
        self.assertEqual(b_light["status"], sc.FAIL)
        self.assertTrue(layers["baselineUrl"].startswith("/img/runs/base/light/B.changed.png"))
        self.assertTrue(layers["diffUrl"].startswith("/img/compares/run__base__r1/diff/light/B.changed.png"))
        self.assertTrue(layers["maskUrl"].startswith("/mask/run__base__r1/light/B.changed.png?v="))
        pinned = self.ok("GET", "/api/run?id=base")
        self.assertEqual((pinned["run"]["pinned"], pinned["pin"], pinned["against"]), (True, None, None))
        self.assertEqual(self.request("GET", "/api/run?id=nope")[0], 404)
        self.assertEqual(self.request("GET", "/api/run?id=../x")[0], 400)


class MainStandingTests(unittest.TestCase):
    """Where each run's commit stands against main, from a throwaway clone: main has c1 → c2, a branch
    has c3 from c1, and one run names a commit the clone has never seen."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name, "repo")
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.c1 = self.commit("c1")
        self.c2 = self.commit("c2")
        self.git("checkout", "-q", "-b", "feature", self.c1)
        self.c3 = self.commit("c3")
        self.git("checkout", "-q", "main")
        self.catalog = Path(self.tmp.name, "catalog")
        make_run(self.catalog, "at", full(), started=1, pinned=True, git={"sha": self.c2, "dirty": False})
        make_run(self.catalog, "behind", full(), started=2, git={"sha": self.c1, "dirty": False})
        make_run(self.catalog, "off", full(), started=3, git={"sha": self.c3, "dirty": True})
        make_run(self.catalog, "unknown", full(), started=4, git={"sha": "f" * 40, "dirty": False})
        make_run(self.catalog, "nogit", full(), started=5, git={"sha": None, "dirty": False})
        self.app = ui.CatalogUI(self.catalog, exclude_file=Path(self.tmp.name, "none.json"), warm_thumbnails=False,
                                thumb_processes=0, repo=self.repo)
        self.addCleanup(self.app.close)

    def git(self, *args: str) -> str:
        return sc.git_output(self.repo, *args) or ""

    def commit(self, name: str) -> str:
        Path(self.repo, name).write_text(name)
        self.git("add", name)
        self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "HEAD")

    def test_main_is_the_local_branch_without_a_remote(self) -> None:
        main = sc.main_head(self.repo)
        self.assertEqual((main["ref"], main["sha"]), ("main", self.c2))
        self.assertTrue(main["committedAt"].startswith("20"), main)

    def test_origin_main_wins_when_the_clone_has_one(self) -> None:
        self.git("update-ref", "refs/remotes/origin/main", self.c1)
        main = sc.main_head(self.repo)
        self.assertEqual((main["ref"], main["sha"]), ("origin/main", self.c1))
        self.assertEqual(sc.standing(self.repo, self.c2, main)["state"], "off")

    def test_each_run_carries_its_standing_and_the_listing_names_main(self) -> None:
        body = self.app.runs()
        self.assertEqual((body["main"]["ref"], body["main"]["sha"]), ("main", self.c2))
        runs = {r["id"]: r["main"] for r in body["runs"]}
        self.assertEqual(runs["at"], {"ref": "main", "state": "at", "behind": 0, "ahead": 0})
        self.assertEqual(runs["behind"], {"ref": "main", "state": "behind", "behind": 1, "ahead": 0})
        self.assertEqual(runs["off"], {"ref": "main", "state": "off", "behind": 1, "ahead": 1})
        self.assertEqual(runs["unknown"], {"ref": "main", "state": "unknown", "behind": None, "ahead": None})
        self.assertIsNone(runs["nogit"])
        self.assertEqual(self.app.run_detail("behind")["run"]["main"]["state"], "behind")

    def test_the_standing_follows_main_as_it_moves(self) -> None:
        self.assertEqual(self.app.runs()["baselines"], {DEVICE: "at"})
        self.assertEqual({r["id"]: r["main"]["state"] for r in self.app.runs()["runs"] if r["main"]}["at"], "at")
        self.commit("c4")
        after = {r["id"]: r["main"] for r in self.app.runs()["runs"]}
        self.assertEqual((after["at"]["state"], after["at"]["behind"]), ("behind", 1))
        self.assertEqual((after["behind"]["state"], after["behind"]["behind"]), ("behind", 2))

    def test_the_routes_refuse_a_run_whose_commit_is_not_on_main(self) -> None:
        for run_id, why in (("unknown", "is not in this clone"), ("nogit", "no commit recorded"),
                            ("off", "recorded with uncommitted changes")):
            with self.assertRaises(ui.RequestError) as caught:
                self.app.set_baseline({"run": run_id})
            self.assertEqual(caught.exception.status, 409)
            self.assertIn(f"{run_id} cannot be the baseline: it", str(caught.exception))
            self.assertIn(why, str(caught.exception))
        self.assertEqual(self.app.runs()["baselines"], {DEVICE: "at"})
        self.app.delete_run({"run": "at"})
        pinned = self.app.set_baseline({"run": "behind"})
        self.assertEqual((pinned["pinned"], pinned["main"]["state"], pinned["main"]["behind"]), ("behind", "behind", 1))
        self.assertEqual(self.app.runs()["baselines"], {DEVICE: "behind"})

    def test_no_clone_means_no_standing(self) -> None:
        plain = Path(self.tmp.name, "plain")
        plain.mkdir()
        self.assertIsNone(sc.main_head(plain))
        app = ui.CatalogUI(self.catalog, exclude_file=Path(self.tmp.name, "none.json"), warm_thumbnails=False,
                           thumb_processes=0, repo=plain)
        self.addCleanup(app.close)
        body = app.runs()
        self.assertIsNone(body["main"])
        self.assertTrue(all(r["main"] is None for r in body["runs"]))


class RequestGuardTests(ServerCase):
    """Only the page may talk to the server: a wrong Host, a cross-origin POST or a POST without a JSON
    content type is refused before any route runs."""

    def raw(self, method: str, path: str, headers: dict, body: bytes = b"{}") -> tuple[int, object]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest(method, path, skip_host=True)
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.putheader("Content-Length", str(len(body)) if method == "POST" else "0")
        conn.endheaders(body if method == "POST" else None)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data) if (response.getheader("Content-Type") or "").startswith("application/json") else data

    def test_the_hosts_the_server_answers(self) -> None:
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"LOCALHOST:{self.port}"):
            self.assertEqual(self.raw("GET", "/api/runs", {"Host": host})[0], 200, host)
        for host in ("evil.example", f"evil.example:{self.port}", f"127.0.0.1:{self.port + 1}", ""):
            code, data = self.raw("GET", "/api/runs", {"Host": host})
            self.assertEqual(code, 403, host)
            self.assertIn("is not this server", data["error"])

    def test_a_post_must_be_json_from_this_origin(self) -> None:
        host = f"127.0.0.1:{self.port}"
        before = self.ok("GET", "/api/runs")["baselines"]
        code, data = self.raw("POST", "/api/set-baseline", {"Host": host, "Content-Type": "text/plain"}, b'{"run": "run"}')
        self.assertEqual((code, data["error"]), (403, "a POST must carry Content-Type: application/json"))
        code, data = self.raw("POST", "/api/set-baseline",
                              {"Host": host, "Content-Type": "application/json", "Origin": "http://evil.example"}, b'{"run": "run"}')
        self.assertEqual((code, data["error"]), (403, "cross-origin request from http://evil.example refused"))
        self.assertEqual(self.ok("GET", "/api/runs")["baselines"], before)
        same = {"Host": host, "Content-Type": "application/json", "Origin": f"http://{host}"}
        self.assertIn(self.raw("POST", "/api/compare", same, b'{"run": "run"}')[0], (200, 202))  # 200 when done within the wait


class ReviewTests(ServerCase):
    """`POST /api/review` and what the results, the listing and the baseline operations say about reviews."""

    def test_results_carry_each_changes_review_and_the_summary(self) -> None:
        self.app.user_override = self.ALEX
        data = self.results("run")
        rows = {(r["name"], r["appearance"]): r for r in data["rows"]}
        self.assertIsNone(rows[("A.same", "light")]["review"])
        fail = rows[("B.changed", "light")]["review"]
        self.assertEqual((fail["state"], fail["author"], fail["events"]), ("open", None, []))
        self.assertEqual((len(fail["pair"]["current"]), len(fail["pair"]["baseline"])), (16, 16))
        self.assertEqual(data["user"], self.ALEX)
        self.assertEqual(data["review"], {"changes": 1, "open": 1, "expected": 0, "confirmed": 0, "rejected": 0})
        self.assertEqual(data["blockers"], ["B.changed (light): FAIL, open"])

    def test_the_lifecycle_over_the_api(self) -> None:
        self.results("run")
        marked = self.review(self.ALEX, "expect", "run", "B.changed", note="new header")
        self.assertEqual((marked["review"]["state"], marked["review"]["author"], marked["review"]["note"]),
                         ("expected", self.ALEX, "new header"))
        self.assertEqual(marked["summary"], {"changes": 1, "open": 0, "expected": 1, "confirmed": 0, "rejected": 0})
        self.assertEqual(marked["blockers"], ["B.changed (light): FAIL, expected by Alex"])
        code, body = self.request("POST", "/api/review", {"run": "run", "name": "B.changed", "appearance": "light", "kind": "confirm"})
        self.assertEqual((code, body["error"]), (409, "confirm changes nothing: the author cannot confirm their own change"))
        self.assertEqual(self.review(self.SAM, "reject", "run", "B.changed", status=409)["error"],
                         "a rejection needs a reason")
        rejected = self.review(self.SAM, "reject", "run", "B.changed", note="padding is off")["review"]
        self.assertEqual((rejected["state"], rejected["reviewer"], rejected["reason"]), ("rejected", self.SAM, "padding is off"))
        self.review(self.ALEX, "expect", "run", "B.changed", note="fixed")
        confirmed = self.review(self.SAM, "confirm", "run", "B.changed")
        self.assertEqual(confirmed["review"]["state"], "confirmed")
        self.assertEqual(confirmed["blockers"], [])
        self.assertEqual([e["kind"] for e in confirmed["review"]["events"]], ["expect", "reject", "expect", "confirm"])
        row = next(r for r in self.results("run")["rows"] if r["name"] == "B.changed" and r["appearance"] == "light")
        self.assertEqual((row["review"]["state"], row["review"]["reviewer"]), ("confirmed", self.SAM))
        # the listing's baseline comparison carries the summary, and no blockers now
        listed = self.runs()["run"]["baselineCompare"]
        self.assertEqual((listed["review"]["confirmed"], listed["blockers"]), (1, 0))

    def test_refusals(self) -> None:
        self.app.user_override = self.ALEX
        for body, code, text in (({"run": "run", "appearance": "light", "kind": "expect"}, 400, "name and appearance"),
                                 ({"run": "run", "name": "B.changed", "appearance": "light", "kind": "bless"}, 400, "kind must be"),
                                 ({"run": "run", "name": "B.changed", "appearance": "light", "kind": "expect", "note": 3}, 400, "note must be"),
                                 ({"run": "run", "name": "B.changed", "appearance": "light", "kind": "expect"}, 409,
                                  "no current comparison of run with base: compare first"),
                                 ({"run": "nope", "name": "B.changed", "appearance": "light", "kind": "expect"}, 404, "no run")):
            with self.subTest(body=body):
                got, data = self.request("POST", "/api/review", body)
                self.assertEqual(got, code, data)
                self.assertIn(text, data["error"])
        self.results("run")
        self.assertIn("A.same (light) is not a changed or new screen",
                      self.review(self.ALEX, "expect", "run", "A.same", status=409)["error"])
        self.assertIn("no identity for the review", self.review({}, "expect", "run", "B.changed", status=409)["error"])
        self.assertIsNone(self.results("run")["user"] or None)

    def test_a_new_screen_is_a_change_too(self) -> None:
        make_run(self.catalog, "newer", {**full(), ("C.new", "light"): png(5)}, started=9)
        data = self.results("newer")
        new = next(r for r in data["rows"] if r["name"] == "C.new")
        self.assertEqual((new["status"], new["review"]["state"], new["review"]["pair"]["baseline"]), ("MISSING", "open", "none"))
        self.assertEqual(data["review"]["changes"], 1)
        self.assertEqual(self.request("POST", "/api/set-baseline", {"run": "newer"})[0], 409)
        self.approve("newer", "C.new")
        self.assertEqual(self.ok("POST", "/api/set-baseline", {"run": "newer"})["review"]["confirmed"], 1)


class CompareTests(ServerCase):

    def test_compare_computes_then_reuses_and_results_contract(self) -> None:
        first = self.ok("POST", "/api/compare", {"run": "run"})
        self.assertEqual((first["status"], first["against"], first["key"], first["reused"]), ("ready", "base", "run__base__r1", False))
        again = self.ok("POST", "/api/compare", {"run": "run", "against": "base"})
        self.assertEqual((again["status"], again["reused"]), ("ready", True))
        forced = self.ok("POST", "/api/compare", {"run": "run", "force": True})
        self.assertFalse(forced["reused"])
        data = self.ok("GET", "/api/results?run=run")  # against omitted: the pin
        self.assertEqual((data["status"], data["key"], data["againstPinned"], data["againstRevision"]),
                         ("ready", "run__base__r1", True, 1))
        self.assertEqual((data["run"]["id"], data["against"]["id"]), ("run", "base"))
        self.assertEqual(data["counts"], {"all": {"PASS": 3, "FAIL": 1}, "light": {"PASS": 1, "FAIL": 1}, "dark": {"PASS": 2}})
        self.assertEqual(data["settings"]["maxDiffPercent"], 0.01)
        self.assertEqual(data["imageSize"], [40, 40])
        fail = next(r for r in data["rows"] if r["status"] == sc.FAIL)
        self.assertEqual((fail["name"], fail["appearance"]), ("B.changed", "light"))
        for key, prefix in (("currentUrl", "/img/runs/run/light/"), ("baselineUrl", "/img/runs/base/light/"),
                            ("diffUrl", "/img/compares/run__base__r1/diff/light/"),
                            ("currentThumbUrl", "/thumb/runs/run/light/"), ("diffThumbUrl", "/thumb/compares/run__base__r1/diff/light/"),
                            ("maskUrl", "/mask/run__base__r1/light/"), ("maskThumbUrl", "/mask/run__base__r1/light/")):
            self.assertTrue(fail[key].startswith(prefix + "B.changed.png?v="), (key, fail[key]))
        self.assertTrue(fail["maskThumbUrl"].endswith("&size=thumb"))
        passed = next(r for r in data["rows"] if r["status"] == sc.PASS)
        self.assertIsNone(passed["diffUrl"])
        self.assertIsNone(passed["maskUrl"])
        # Any pair of one type compares, the pin on either side.
        other = self.results("base", "run")
        self.assertEqual((other["key"], other["againstPinned"]), ("base__run__r1", False))
        partial = self.results("part")
        self.assertEqual((partial["runPartial"], [r["name"] for r in partial["rows"]]), (True, ["B.changed"]))

    def test_refusals(self) -> None:
        for body, code, text in (({"run": "run", "against": "run"}, 400, "with itself"),
                                 ({"run": "nope"}, 404, "no run nope"),
                                 ({"run": "run", "against": "nope"}, 404, "no run nope"),
                                 ({"run": "../base"}, 400, "run id"),
                                 ({"run": "base"}, 400, "with itself")):
            with self.subTest(body=body):
                status, answer = self.request("POST", "/api/compare", body)
                self.assertEqual(status, code, answer)
                self.assertIn(text, answer["error"])
        self.assertEqual(self.request("GET", "/api/results?run=pro")[0], 409)

    def test_a_slow_comparison_answers_202_and_shows_in_status(self) -> None:
        self.app.compare_wait = 0
        gate = threading.Event()
        original = sc.compare_runs

        def gated(*args, **kwargs):
            gate.wait(10)
            return original(*args, **kwargs)

        sc.compare_runs = gated
        self.addCleanup(setattr, sc, "compare_runs", original)
        self.addCleanup(gate.set)
        status, body = self.request("POST", "/api/compare", {"run": "run"})
        self.assertEqual((status, body["status"], body["against"]), (202, "computing", "base"))
        self.assertEqual(self.ok("GET", "/api/results?run=run&against=base")["status"], "computing")
        compares = self.ok("GET", "/api/status")["compares"]
        self.assertEqual([(c["run"], c["against"], c["active"]) for c in compares], [("run", "base", True)])
        self.assertEqual(self.request("POST", "/api/compare", {"run": "run"})[0], 202)  # joins the same task
        self.assertEqual(self.request("POST", "/api/runs/delete", {"run": "run"})[0], 409)
        gate.set()
        self.assertEqual(self.results("run")["key"], "run__base__r1")
        self.assertEqual(self.ok("GET", "/api/status")["compares"], [])

    def gated_compare(self, report=(3, 10)):
        """compare_runs replaced by one that reports progress and waits for its stop event (or a release)."""
        self.app.compare_wait = 0
        release = threading.Event()
        seen = {}
        original = sc.compare_runs

        def gated(*args, **kwargs):
            seen.update(kwargs)
            args[3].progress(*report)
            for _ in range(200):
                if kwargs["stop"].is_set():
                    raise sc.CompareCancelled("compare run__base__r1: cancelled")
                if release.is_set():
                    return original(*args, **kwargs)
                time.sleep(0.05)
            raise AssertionError("never stopped")

        sc.compare_runs = gated
        self.addCleanup(setattr, sc, "compare_runs", original)
        self.addCleanup(release.set)
        return release, seen

    def test_computing_status_carries_progress_and_eta(self) -> None:
        release, seen = self.gated_compare()
        self.assertEqual(self.request("POST", "/api/compare", {"run": "run"})[0], 202)
        task = self.ok("GET", "/api/results?run=run&against=base")["task"]
        self.assertEqual((task["done"], task["total"], task["perPairMs"], task["eta"]), (3, 10, None, None))
        self.assertEqual((seen["workers"], seen["pool"]), (0, None))  # thumb_processes=0: in-process
        compare_task = self.app.tasks[("run", "base")]
        compare_task.first = (time.time() - 2.0, 1)  # 2 pairs in 2 s: 1000 ms a pair, 7 pairs left
        task = self.ok("GET", "/api/results?run=run&against=base")["task"]
        self.assertEqual((task["done"], task["total"], task["eta"]), (3, 10, 7))
        self.assertAlmostEqual(task["perPairMs"], 1000, delta=150)
        release.set()
        self.assertEqual(self.results("run")["key"], "run__base__r1")

    def test_cancel_stops_the_task_and_stores_nothing(self) -> None:
        self.assertEqual(self.ok("POST", "/api/compare/cancel", {"run": "run", "against": "base"})["cancelled"], False)
        self.gated_compare()
        self.assertEqual(self.request("POST", "/api/compare", {"run": "run"})[0], 202)
        stopped = self.ok("POST", "/api/compare/cancel", {"run": "run", "against": "base"})
        self.assertEqual((stopped["cancelled"], stopped["error"]), (True, None))
        data = self.ok("GET", "/api/results?run=run&against=base")
        self.assertEqual((data["status"], data["task"]["cancelled"], data["task"]["active"]), ("cancelled", True, False))
        self.assertFalse((self.catalog / sc.COMPARES_DIR).exists() and any((self.catalog / sc.COMPARES_DIR).iterdir()))
        self.assertEqual(self.request("POST", "/api/compare/cancel", {"run": "../x", "against": "base"})[0], 400)

    def test_a_cancelled_real_compare_removes_its_partial_folder(self) -> None:
        stop = threading.Event()

        class StopAtStart(sc.Quiet):
            def progress(self, done, total):
                stop.set()

        with self.assertRaises(sc.CompareCancelled):
            sc.compare_runs(self.catalog, "run", output=StopAtStart(), workers=0, stop=stop)
        self.assertEqual([p.name for p in (self.catalog / sc.COMPARES_DIR).iterdir()], [])

    def test_a_changed_run_is_recomputed_not_flagged(self) -> None:
        first = self.results("run")
        manifest = self.catalog / sc.RUNS_DIR / "run" / sc.MANIFEST_FILE
        stat = manifest.stat()
        os.utime(manifest, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        self.assertEqual(self.ok("GET", "/api/results?run=run&against=base")["status"], "needed")
        again = self.ok("POST", "/api/compare", {"run": "run", "against": "base"})
        self.assertFalse(again["reused"])
        self.assertEqual(self.results("run")["key"], first["key"])

    def test_accept_copies_into_the_pin_and_the_comparison_recomputes(self) -> None:
        self.results("run")
        for body, code in (({"run": "run", "name": "B.changed", "appearance": "light"}, 400),
                           ({"run": "base", "against": "run", "name": "B.changed", "appearance": "light"}, 409),
                           ({"run": "run", "against": "base", "name": "B.changed", "appearance": "sepia"}, 400),
                           ({"run": "run", "against": "base", "name": "Nope.x", "appearance": "light"}, 409)):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/accept", body)[0], code)
        code, body = self.request("POST", "/api/accept", {"run": "run", "against": "base", "name": "B.changed", "appearance": "light"})
        self.assertEqual(code, 409)
        self.assertIn("1 change not confirmed by a reviewer (1 open, 0 expected, 0 rejected, 0 confirmed): B.changed (light): FAIL, open",
                      body["error"])
        self.approve("run", "B.changed")
        accepted = self.ok("POST", "/api/accept", {"run": "run", "against": "base", "name": "B.changed", "appearance": "light"})
        self.assertEqual((accepted["revision"], accepted["baseline"], accepted["event"]["kind"]), (2, "base", "accept"))
        data = self.results("run", "base")
        self.assertEqual((data["key"], data["againstRevision"]), ("run__base__r2", 2))
        self.assertEqual(data["counts"]["all"], {"PASS": 4})
        self.assertEqual(sorted(p.name for p in (self.catalog / sc.COMPARES_DIR).iterdir()), ["run__base__r2"])
        self.assertEqual((self.catalog / sc.RUNS_DIR / "base" / "light" / "B.changed.png").read_bytes(), png(10, block=True))
        history = self.ok("GET", "/api/run?id=base")["run"]["history"]
        self.assertEqual([(e["kind"], e["revision"], e["from"]) for e in history], [("accept", 2, "run")])
        self.assertEqual(self.runs()["base"]["revision"], 2)


class BaselineOperationTests(ServerCase):

    def test_set_baseline_rules(self) -> None:
        code, body = self.request("POST", "/api/set-baseline", {"run": "part"})
        self.assertEqual(code, 409)
        self.assertIn("run part is partial", body["error"])
        self.assertIn("use update-baseline part", body["error"])
        make_run(self.catalog, "halted", full(), started=6, stopped=True)
        code, body = self.request("POST", "/api/set-baseline", {"run": "halted"})
        self.assertEqual(code, 409)
        self.assertIn("stopped before capturing everything", body["error"])
        self.assertEqual(self.request("POST", "/api/set-baseline", {"run": "nope"})[0], 404)
        self.assertEqual(self.request("POST", "/api/set-baseline", {"run": "a/b"})[0], 400)
        code, body = self.request("POST", "/api/set-baseline", {"run": "run"})
        self.assertEqual(code, 409)
        self.assertIn("no current comparison of run with the baseline", body["error"])
        self.results("run")
        code, body = self.request("POST", "/api/set-baseline", {"run": "run"})
        self.assertEqual((code, body["error"]), (409, "1 change not confirmed by a reviewer (1 open, 0 expected, 0 rejected, "
                                                      "0 confirmed): B.changed (light): FAIL, open"))
        self.approve("run", "B.changed")
        moved = self.ok("POST", "/api/set-baseline", {"run": "run"})
        self.assertEqual(moved, {"deviceDir": DEVICE, "pinned": "run", "previous": "base", "main": None,
                                 "review": {"changes": 1, "open": 0, "expected": 0, "confirmed": 1, "rejected": 0}})
        runs = self.runs()
        self.assertEqual((runs["run"]["pinned"], runs["run"]["title"], runs["base"]["pinned"]), (True, "Baseline", False))
        self.assertEqual(self.ok("GET", "/api/runs")["groups"][0]["pinned"], "run")
        pin_event = self.ok("GET", "/api/run?id=run")["run"]["history"][0]
        self.assertEqual((pin_event["kind"], pin_event["replaced"]), ("pin", "base"))
        # The new pin is compared against when `against` is omitted; the old one is just a run.
        self.assertEqual(self.results("base")["key"], "base__run__r1")
        self.assertEqual(self.ok("POST", "/api/set-baseline", {"run": "pro"})["previous"], None)  # its own type

    def test_update_baseline_rules(self) -> None:
        code, body = self.request("POST", "/api/update-baseline", {"run": "run"})
        self.assertEqual(code, 409)
        self.assertIn("is a full run", body["error"])
        make_run(self.catalog, "pro-part", {("A.same", "light"): png(3)}, kind=KIND18, only=["A."], started=7)
        code, body = self.request("POST", "/api/update-baseline", {"run": "pro-part"})
        self.assertEqual(code, 409)
        self.assertIn("no baseline is pinned for iPhone 18 Pro, iOS 27.0", body["error"])
        self.assertEqual(self.request("POST", "/api/update-baseline", {"run": "part"})[0], 409)  # not compared, not confirmed
        self.approve("part", "B.changed")
        event = self.ok("POST", "/api/update-baseline", {"run": "part"})
        self.assertEqual((event["kind"], event["revision"], event["baseline"], event["variants"]),
                         ("update", 2, "base", [{"name": "B.changed", "appearance": "light"}]))
        self.assertEqual(self.runs()["base"]["revision"], 2)
        self.assertEqual(self.results("run")["key"], "run__base__r2")

    def test_delete_rules(self) -> None:
        self.results("run")
        self.ok("GET", "/thumb/runs/run/light/A.same.png")
        self.assertEqual(self.request("POST", "/api/runs/delete", {"run": "nope"})[0], 404)
        self.assertEqual(self.request("POST", "/api/runs/delete", {"run": "../base"})[0], 400)
        busy = make_run(self.catalog, "busy", full(), started=8)
        sc.write_run(busy, active=True, pid=os.getpid())
        code, body = self.request("POST", "/api/runs/delete", {"run": "busy"})
        self.assertEqual(code, 409)
        self.assertIn("still capturing", body["error"])
        deleted = self.ok("POST", "/api/runs/delete", {"run": "run"})
        self.assertEqual((deleted["deleted"], deleted["compares"]), ("run", ["run__base__r1"]))
        self.assertFalse((self.catalog / sc.RUNS_DIR / "run").exists())
        self.assertFalse((self.catalog / sc.COMPARES_DIR / "run__base__r1").exists())
        self.assertFalse((self.catalog / ui.THUMB_DIR / sc.RUNS_DIR / "run").exists())
        self.assertNotIn("run", self.runs())

    def test_deleting_the_baseline_unpins_its_type(self) -> None:
        self.results("run")
        deleted = self.ok("POST", "/api/runs/delete", {"run": "base"})
        self.assertEqual((deleted["deleted"], deleted["compares"], deleted["unpinned"]),
                         ("base", ["run__base__r1"], "iPhone_17_iOS_27.0"))
        self.assertIn("no baseline for iPhone 17, iOS 27.0", deleted["notice"])
        listing = self.ok("GET", "/api/runs")
        self.assertNotIn("iPhone_17_iOS_27.0", listing["baselines"])
        group = next(g for g in listing["groups"] if g["deviceDir"] == "iPhone_17_iOS_27.0")
        self.assertIsNone(group["pinned"])
        self.assertFalse(any(r["pinned"] for r in listing["runs"]))
        code, body = self.request("POST", "/api/compare", {"run": "run"})  # Compare… shows "No baseline…"
        self.assertEqual(code, 409)
        self.assertIn("no baseline is pinned for iPhone 17 · iOS 27.0", body["error"])
        self.assertEqual(self.runs()["run"]["comparable"], ["part"])

    def test_export(self) -> None:
        body = self.ok("POST", "/api/export", {"run": "run"})
        path = Path(body["path"])
        self.assertEqual(path, self.catalog / "screencatalog_run.zip")
        with zipfile.ZipFile(path) as archive:
            self.assertIn("run/light/B.changed.png", archive.namelist())
            self.assertIn("run/run.json", archive.namelist())
        self.assertEqual(self.request("POST", "/api/export", {"run": "nope"})[0], 404)


class RecordTests(ServerCase):

    def test_status_contract_and_one_run_at_a_time(self) -> None:
        self.gate.clear()
        idle = self.ok("GET", "/api/status")
        keys = {"active", "runId", "mode", "started", "finished", "done", "total", "perVariantMs", "eta", "lines",
                "lastSeq", "exitCode", "summary", "udids", "phase", "compares"}
        self.assertEqual(set(idle), keys)
        self.assertFalse(idle["active"])
        started = self.start({"mode": "record", "udids": [UDID_A, UDID_B], "appearance": "light"})
        self.assertRegex(started["runId"], r"^ui-\d{8}-\d{6}-record$")  # the job; the run id comes when the capture starts
        self.assertIsNone(started["run"])
        running = self.ok("GET", "/api/status")
        self.assertEqual((running["active"], running["mode"], running["udids"]), (True, "record", [UDID_A, UDID_B]))
        status, conflict = self.request("POST", "/api/run", {"mode": "record", "udids": [UDID_A]})
        self.assertEqual(status, 409)
        self.assertIn("still active", conflict["error"])
        self.assertEqual(self.ok("POST", "/api/compare", {"run": "run"})["status"], "ready")  # a file operation: allowed
        self.gate.set()
        done = self.wait_idle()
        self.assertEqual((done["done"], done["total"], done["exitCode"]), (2, 2, 0))
        summary = done["summary"]
        self.assertRegex(summary["run"], r"^ui-\d{8}-\d{6}$")
        self.assertEqual((summary["pinned"], summary["comparable"], summary["typeLabel"]), ("base", True, "iPhone 17 · iOS 27.0"))
        self.assertEqual(summary["counts"]["all"], {"RECORDED": 2})
        texts = [line["text"] for line in done["lines"]]
        self.assertIn("run " + summary["run"], texts)
        argv = next(t for t in texts if t.startswith("$ screencatalog.py record "))
        for part in ("--udid " + UDID_A + " --udid " + UDID_B, "--run-id " + summary["run"], "--source ui", "--keep-runs 5"):
            self.assertIn(part, argv)

    def test_record_writes_a_new_run_and_never_the_baseline(self) -> None:
        before = {p: p.read_bytes() for p in (self.catalog / sc.RUNS_DIR / "base").rglob("*") if p.is_file()}
        started = self.start({"mode": "record", "udids": [UDID_A], "appearance": "light",
                              "filters": {"only": ["B."], "feature": []}})
        done = self.wait_idle()
        self.assertEqual(done["exitCode"], 0, done["lines"])
        run_id = done["summary"]["run"]
        run = sc.read_run(self.catalog / sc.RUNS_DIR / run_id)
        self.assertEqual((run["source"], run["filter"]["label"], run["appearance"], run["counts"]), ("ui", "B.", "light", {"light": 1}))
        self.assertEqual(run["params"]["udids"], [UDID_A])
        after = {p: p.read_bytes() for p in (self.catalog / sc.RUNS_DIR / "base").rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(sc.read_baselines(self.catalog), {DEVICE: "base"})
        self.assertEqual(self.results(run_id)["counts"]["all"], {"FAIL": 1})  # its B.changed has the red block

    def test_compare_mode_and_bad_bodies(self) -> None:
        code, body = self.request("POST", "/api/run", {"mode": "compare", "udids": [UDID_A]})
        self.assertEqual(code, 400)
        self.assertIn("POST /api/compare", body["error"])
        for bad in ({"mode": "compare-only"}, {"mode": "record"}, {"mode": "record", "udids": ["nope"]},
                    {"mode": "record", "udids": [UDID_A], "appearance": "sepia"},
                    {"mode": "record", "udids": [UDID_A], "filters": {"only": "A"}}):
            with self.subTest(body=bad):
                self.assertEqual(self.request("POST", "/api/run", bad)[0], 400)
        for gone in ("/api/recordings", "/api/recording?id=x", "/api/zip"):
            self.assertEqual(self.request("GET", gone)[0], 404)


class ImageRouteTests(ServerCase):

    def traversal(self, route: str, good: tuple[str, ...]) -> None:
        link = self.catalog / sc.RUNS_DIR / "base" / "light" / "link.png"
        link.symlink_to(Path(self.tmp.name, "secret.png"))
        outside = Path(self.tmp.name, "outside", "light")
        outside.mkdir(parents=True)
        (outside / "x.png").write_bytes(png(1))
        (self.catalog / sc.RUNS_DIR / "evil").symlink_to(Path(self.tmp.name, "outside"))
        (self.catalog / sc.RUNS_DIR / "base" / "logs").mkdir()
        (self.catalog / sc.RUNS_DIR / "base" / "logs" / "x.png").write_bytes(png(1))
        for path in (f"/{route}/runs/base/../../secret.png", f"/{route}/runs/base/%2e%2e/%2e%2e/secret.png",
                     f"/{route}/runs/base/..%2f..%2fsecret.png", f"/{route}/../secret.png", f"/{route}/secret.png",
                     f"/{route}/runs/base/logs/x.png", f"/{route}/runs/base/run.json", f"/{route}/runs/base",
                     f"/{route}/runs/base/light", f"/{route}/runs/base//light/A.same.png",
                     f"/{route}/runs/..%2fbase/light/A.same.png", f"/{route}/runs/a__b/light/A.same.png",
                     f"/{route}/runs/evil/light/x.png", f"/{route}/runs/base/light/link.png",
                     f"/{route}/baseline/{DEVICE}/light/A.same.png", f"/{route}/ui-runs/r/current/{DEVICE}/light/A.same.png",
                     f"/{route}/compares/run__base__r1/compare.json", f"/{route}/compares/nokey/diff/light/B.changed.png",
                     f"/{route}/compares/run__base__r1/light/B.changed.png",
                     f"/{route}/.thumbs/runs/base/light/A.same.png"):
            with self.subTest(path=path):
                code, body = self.request("GET", path)
                self.assertIn(code, (403, 404))
                self.assertNotEqual(body[:8] if isinstance(body, bytes) else b"", sc.PNG_SIGNATURE)
        self.assertEqual(self.request("GET", f"/{route}/runs/base/light/link.png")[0], 403)
        self.assertEqual(self.request("GET", f"/{route}/runs/evil/light/x.png")[0], 403)
        self.assertEqual(self.request("GET", f"/{route}/runs/base/light/nope.png")[0], 404)
        for path in good:
            self.assertEqual(self.request("GET", path)[0], 200, path)

    def test_img_resolves_runs_and_compares_and_rejects_the_rest(self) -> None:
        self.results("run")
        self.traversal("img", ("/img/runs/base/light/A.same.png", "/img/runs/run/dark/A.same.png",
                               "/img/compares/run__base__r1/diff/light/B.changed.png"))

    def test_thumb_paths_and_cache(self) -> None:
        self.results("run")
        self.traversal("thumb", ("/thumb/runs/base/light/A.same.png", "/thumb/compares/run__base__r1/diff/light/B.changed.png"))
        self.assertFalse(Path(self.tmp.name, ui.THUMB_DIR).exists())  # nothing written outside the catalog
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/thumb/runs/run/light/A.same.png?v=1")
        response = conn.getresponse()
        self.assertEqual((response.status, response.read()[:8]), (200, sc.PNG_SIGNATURE))
        self.assertIn("immutable", response.getheader("Cache-Control"))
        conn.close()
        cached = self.catalog / ui.THUMB_DIR / sc.RUNS_DIR / "run" / "light" / "A.same.png"
        source = self.catalog / sc.RUNS_DIR / "run" / "light" / "A.same.png"
        self.assertEqual(cached.stat().st_mtime_ns, source.stat().st_mtime_ns)
        self.assertTrue((self.catalog / ui.THUMB_DIR / sc.COMPARES_DIR / "run__base__r1" / "diff" / "light" / "B.changed.png").is_file())

    def get(self, path: str) -> tuple[int, bytes, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body, response.getheader("Cache-Control") or ""

    def test_mask_route_paths_pixels_and_cache(self) -> None:
        fail = next(r for r in self.results("run")["rows"] if r["status"] == sc.FAIL)
        for path, code in (("/mask/run__base__r1/light/../../x.png", 403), ("/mask/runs/run/light/B.changed.png", 403),
                           ("/mask/run__base__r1/sepia/B.changed.png", 403), ("/mask/run__base__r1/light/x/B.changed.png", 403),
                           ("/mask/nope__base__r1/light/B.changed.png", 404), ("/mask/run__base__r9/light/B.changed.png", 404),
                           ("/mask/run__base__r1/light/Nope.png", 404)):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], code)
        code, blob, cache = self.get(fail["maskUrl"])
        self.assertEqual(code, 200)
        self.assertIn("immutable", cache)
        mask = sc.decode_png(blob)
        self.assertEqual((mask.width, mask.height, mask.channels), (40, 40, 4))
        block = {y * 40 + x for y in range(10) for x in range(10)}  # png(block=True): the 10 x 10 red square
        for i in range(40 * 40):
            self.assertEqual(mask.data[i * 4:i * 4 + 4], b"\xff\x00\x00\xff" if i in block else b"\x00\x00\x00\x00", i)
        code, blob, _ = self.get(fail["maskThumbUrl"])
        self.assertEqual((code, sc.decode_png(blob).width), (200, 40))
        _, blob, _ = self.get("/mask/run__base__r1/light/A.same.png")
        self.assertEqual(set(sc.decode_png(blob).data[3::4]), {0})  # identical: fully transparent
        cached = self.catalog / ui.THUMB_DIR / sc.COMPARES_DIR / "run__base__r1" / ui.MASK_DIR / "light"
        self.assertEqual(sorted(p.name for p in cached.iterdir() if p.name.startswith("B.")),
                         ["B.changed.key", "B.changed.png", "B.changed.thumb.png"])
        # An accept makes a new key; the old comparison goes, its masks with it.
        self.approve("run", "B.changed")
        self.ok("POST", "/api/accept", {"run": "run", "against": "base", "name": "B.changed", "appearance": "light"})
        self.results("run", "base")
        self.assertFalse((self.catalog / ui.THUMB_DIR / sc.COMPARES_DIR / "run__base__r1").exists())


class PageTests(ServerCase):

    def page(self) -> str:
        code, body = self.request("GET", "/")
        self.assertEqual(code, 200)
        return body.decode()

    def view(self, page: str, name: str) -> str:
        return page.split(f'id="view-{name}"', 1)[1].split('<div class="view"', 1)[0]

    def function(self, page: str, name: str) -> str:
        return page.split("<script>", 1)[1].split(f"function {name}(", 1)[1].split("\n}\n", 1)[0]

    def test_the_pin_buttons_carry_the_main_blocker(self) -> None:
        page = self.page()
        self.assertIn("const pinWhy = mainBlocker(run) || reviewWhy;", page)
        self.assertIn('"data-action": "set-baseline", disabled: busy || !!pinWhy, "data-blocked": pinWhy || null,', page)
        self.assertIn('"data-action": "update-baseline", disabled: busy || !!pinWhy, "data-blocked": pinWhy || null,', page)
        self.assertIn('return "Cannot be the baseline: recorded with uncommitted changes";', page)
        self.assertIn("main is fetched first, and the pin is refused unless the commit is on it", page)

    def test_landing_has_two_cards_and_no_compare_workflow(self) -> None:
        page = self.page()
        self.assertIn("prefers-color-scheme: dark", page)
        self.assertIn(json.dumps(str(self.catalog)), page)
        self.assertNotIn("__CATALOG_DIR__", page)
        home = self.view(page, "home")
        self.assertEqual(home.count('class="card"'), 2)
        for marker in ('id="cardRecord"', 'id="cardRecordings"', ">Record<", ">Recordings<"):
            self.assertIn(marker, home)
        for gone in ('id="view-compare"', 'id="cardCompare"', 'id="btnCompare"', 'id="btnRejudge"', "Delete results",
                     'id="staleBanner"', "/api/recordings", "/api/zip", '"compare-only"', "This replaces the baseline"):
            self.assertNotIn(gone, page)
        self.assertNotIn("http://", page.replace("http://127.0.0.1", ""))  # no external assets
        home_js = self.function(page, "renderHome")
        self.assertIn('": baseline r" + p.revision + " · " + screensText(p.counts)', home_js)
        self.assertIn('" pinned"', home_js)

    def test_record_view_and_after_a_record(self) -> None:
        page = self.page()
        record = self.view(page, "record")
        for marker in ('id="devices"', 'id="deviceFamilySeg"', 'id="deviceOsSeg"', 'id="clones"', 'id="btnDeleteClones"', 'id="appearance"', 'id="filter"',
                       'id="feature"', 'id="settle"', 'id="budget"', 'id="skipBuild"', 'id="btnRecord"', 'id="btnStop"',
                       'id="progress"', 'id="log"', 'id="afterRun"'):
            self.assertIn(marker, record)
        self.assertNotIn('id="allowDuplicate"', record)
        self.assertNotIn("confirm(", self.function(page, "startRun"))  # a record never touches the baseline
        after = self.function(page, "renderAfterRun")
        self.assertIn('"View run"', after)
        self.assertIn('if (sum.comparable) box.append(el("a", {class: "button", href: compareHash(sum.run, sum.pinned)}, "Compare with baseline"))', after)

    def test_recordings_rows_actions_and_confirms(self) -> None:
        page = self.page()
        row = self.function(page, "runRow")
        for marker in ('"Baseline"', '"Open"', '"Compare…"', '"Set as baseline"', '"Update baseline"', '"Export"', '"Delete"',
                       'run.pinned ? "" : el("button"', "run.partial && pin && !run.pinned",
                       '"data-action": "delete", disabled: busy,', '"stopped early"', "filterText(run)"):
            self.assertIn(marker, row)
        self.assertNotIn("pin another run first", row)  # the baseline may be deleted
        self.assertIn('if (run.pinned) return "This is the baseline for " + run.typeLabel + ". After deleting it there is no '
                      'baseline for that type until you set one. Delete?";', self.function(page, "deleteQuestion"))
        self.assertIn('el("span", {class: "no-pin", "data-no-baseline": ""}, "no baseline: set one from a run below")',
                      self.function(page, "renderRecordings"))
        notes = self.function(page, "updateDeviceNotes")
        for marker in ('id="hostNote"', 'id="hostWarn"'):
            self.assertIn(marker, self.view(page, "record"))
        for marker in ('"Recommended: " + host.recommendedClones', '"free memory " + gb(host.freeGB) + " GB"',
                       '"swap " + gb(host.swapUsedGB) + " of " + gb(host.swapTotalGB) + " GB"', '" already booted"',
                       '"More than the recommended " + host.recommendedClones + ": this Mac is likely to swap and captures slow '
                       'from ~7 s to a minute or fail. Shut down other simulators first."',
                       "hostWarn.hidden = n <= host.recommendedClones;"):
            self.assertIn(marker, notes)
        self.assertIn('store.get("clones", null) === null) $("clones").value = host.recommendedClones;', self.function(page, "loadSims"))
        self.assertIn('"No baseline for " + t.model + " · " + t.os + ": set one from a run in Recordings.";\n    warn = true;', notes)

        title = row.split("const title = ", 1)[1].split(";", 1)[0]
        self.assertNotIn("run.id", title)  # the run id shows in the drill-down, not in the list
        self.assertIn('"It replaces baseline r" + pin.revision', self.function(page, "setBaselineQuestion"))
        update = self.function(page, "updateBaselineQuestion")
        self.assertIn('"Copy this run\'s " + n + " screenshot"', update)
        self.assertIn('The baseline becomes r" + (pin.revision + 1)', update)
        self.assertIn("!run.partial && !run.stopped && !confirm(setBaselineQuestion(run, pin))", self.function(page, "setBaseline"))

    def test_results_page_shows_progress_and_cancel(self) -> None:
        page = self.page()
        self.assertIn('<button id="btnCancelCompare" type="button" class="danger" hidden', self.view(page, "results"))
        self.assertIn('"Comparing " + task.done + " / " + task.total + (task.eta != null ? " · ETA " + task.eta + " s" : "")',
                      self.function(page, "progressText"))
        self.assertIn('api("/api/compare/cancel", {run: route.run, against: route.against})', self.function(page, "cancelCompare"))
        fetch = self.function(page, "fetchResults")
        self.assertIn('$("btnCancelCompare").hidden = data.status !== "computing";', fetch)
        self.assertIn('if (data.status === "cancelled")', fetch)

    def test_picker_lists_the_pin_first_and_says_when_there_is_nothing(self) -> None:
        picker = self.function(self.page(), "openPicker")
        self.assertIn('"No baseline for " + run.typeLabel + ". Set one from a run in the list."', picker)
        self.assertIn('"No other " + run.typeLabel + " run to compare against. Record one."', picker)
        self.assertIn('"#record/" + encodeURIComponent(run.deviceDir)', picker)
        self.assertIn("(pin ? [pickOption(pin, true)] : []).concat(rest.map", picker)
        self.assertIn("run.comparable.map", picker)  # this type's runs only, as the server lists them
        # What the strings become for the fixture: an iPhone 18 Pro run with no pin and no other run.
        pro = self.runs()["pro"]
        self.assertEqual((pro["typeLabel"], pro["comparable"]), ("iPhone 18 Pro · iOS 27.0", []))
        self.assertNotIn(pro["deviceDir"], self.ok("GET", "/api/runs")["baselines"])

    def test_results_page_shows_failures_before_the_counts(self) -> None:
        page = self.page()
        results = self.view(page, "results")
        order = [results.index(m) for m in ('id="resultsTitle"', 'id="facts"', 'id="resultsActions"', 'id="failSection"',
                                            'id="failViewSeg"', 'id="failures"', 'id="counts"', 'id="problems"',
                                            'id="passDetails"')]
        self.assertEqual(order, sorted(order))
        seg = results.split('id="failViewSeg"', 1)[1].split("</div>", 1)[0]
        self.assertEqual([b.split('"', 1)[0] for b in seg.split('data-v="')[1:]], ["side", "overlay", "swipe"])
        self.assertIn(">Recompute<", results)
        for marker in ('id="devices"', 'id="btnRecord"', 'id="progress'):
            self.assertNotIn(marker, results)
        self.assertIn("const accept = r.currentUrl && results && results.againstPinned && confirmed;", self.function(page, "failCard"))
        self.assertIn("reviewStrip(r)", self.function(page, "failCard"))
        strip = self.function(page, "reviewStrip")
        for label in ('"Mark expected…"', '"Withdraw"', '"Confirm"', '"Reject…"', "waiting for a reviewer"):
            self.assertIn(label, strip)
        self.assertIn('"/api/review"', self.function(page, "markReview"))
        self.assertIn("replacePins = true", self.function(page, "syncPush"))  # only after the confirm
        self.assertIn('"/api/sync/remote?refresh=1"', self.function(page, "pinsThisPushReplaces"))
        self.assertIn('id="reviewSummary"', results)
        self.assertIn('r.status === "MISSING" && r.currentUrl', self.function(page, "failureOrder"))
        self.assertIn('"Run " + run.id + " against " + againstText(', self.function(page, "renderResultsHead"))
        self.assertIn('"Baseline r"', self.function(page, "againstText"))
        self.assertIn('"/api/compare"', self.function(page, "computeResults"))
        body = self.function(page, "failBody")
        self.assertIn("r.maskThumbUrl", body)
        self.assertNotIn("r.maskUrl", body)
        lightbox = page.split('id="lightbox"', 1)[1].split("</div>\n</div>", 1)[0]
        self.assertIn('data-layer="overlay"', lightbox)

    def test_gallery_offers_the_diff_only_with_a_current_comparison(self) -> None:
        page = self.page()
        actions = self.function(page, "renderGalleryActions")
        self.assertIn("if (gallery.against)", actions)
        self.assertIn('"show diff against baseline "', actions)
        self.assertIn("withDiff && s.compare", self.function(page, "buildGalleryVariants"))
        self.assertIn('id="galleryHistory"', self.view(page, "gallery"))
        self.assertIn('" → "', self.function(page, "historyRev"))


# MARK: - Sync (S3) endpoints and the Sync panel, against the fake aws of test_screencatalog

class SyncDeleteServerTests(ServerCase):
    """`POST /api/sync/delete`: a remote run goes, the bucket's baseline never does, and the listing refreshes."""

    def setUp(self) -> None:
        super().setUp()
        self.s3 = tsc.install_fake_aws(self, Path(self.tmp.name))
        self.bucket = self.s3 / tsc.FAKE_BUCKET / sc.SYNC_PREFIX

    def test_push_replace_pins_field(self) -> None:
        self.ok("POST", "/api/sync/push", {}, 202)
        self.wait_idle()
        self.ok("POST", "/api/runs/delete", {"run": "base"})
        make_run(self.catalog, "fresh", full(), started=9, pinned=True, git={"sha": "f" * 40, "dirty": False})
        self.ok("POST", "/api/sync/push", {}, 202)
        refused = self.wait_idle()
        self.assertEqual(refused["exitCode"], 2)
        self.assertTrue(any("which you do not have" in line["text"] for line in refused["lines"]))
        self.assertEqual(self.request("POST", "/api/sync/push", {"replacePins": "yes"})[0], 400)
        self.ok("POST", "/api/sync/push", {"replacePins": True}, 202)
        done = self.wait_idle()
        self.assertEqual((done["exitCode"], done["summary"]["baselines"]), (0, {DEVICE: "fresh"}))

    def test_delete_route(self) -> None:
        self.ok("POST", "/api/sync/push", {"all": True}, 202)
        self.wait_idle()
        self.assertEqual(self.request("POST", "/api/sync/delete", {"runs": []})[0], 400)
        self.assertEqual(self.request("POST", "/api/sync/delete", {"runs": ["a/b"]})[0], 400)
        code, body = self.request("POST", "/api/sync/delete", {"runs": ["base"]})
        self.assertEqual(code, 409)
        self.assertIn("is the bucket's baseline", body["error"])
        self.assertEqual(self.ok("POST", "/api/sync/delete", {"runs": ["run"]}), {"deleted": ["run"]})
        self.assertFalse((self.bucket / sc.RUNS_DIR / "run").exists())
        self.assertTrue((self.catalog / sc.RUNS_DIR / "run").is_dir())
        self.assertNotIn("run", [r["id"] for r in self.ok("GET", "/api/sync/remote")["runs"]])  # listing not stale
        self.assertEqual(self.request("POST", "/api/sync/delete", {"runs": ["run"]})[0], 404)  # gone already


class SyncServerTests(ServerCase):

    def setUp(self) -> None:
        super().setUp()
        self.s3 = tsc.install_fake_aws(self, Path(self.tmp.name))
        self.bucket = self.s3 / tsc.FAKE_BUCKET / sc.SYNC_PREFIX

    def sync(self, verb: str, body: dict) -> dict:
        started = self.ok("POST", f"/api/sync/{verb}", body, 202)
        self.assertEqual(started["mode"], verb)
        status = self.wait_idle()
        text = "\n".join(line["text"] for line in status["lines"])
        self.assertNotIn(tsc.FAKE_BUCKET, text)
        self.assertNotIn(tsc.FAKE_SECRET, text)
        return status

    def test_messages_name_a_folder_store_as_the_shared_folder(self) -> None:
        self.assertEqual(self.ok("GET", "/api/sync/status")["store"], "the bucket")
        self.assertIn('let storeName = "the bucket";', ui.render_page(self.app))
        with mock.patch.object(sc, "STORAGE_TYPE", "directory"), \
                mock.patch.object(sc, "CONFIG", {"storage": {"type": "directory", "path": self.tmp.name}}):
            self.assertEqual(self.ok("GET", "/api/sync/status")["store"], "the shared folder")
            page = ui.render_page(self.app)
        self.assertIn('let storeName = "the shared folder";', page)
        self.assertNotIn("__STORE_NAME__", page)
        for hard_coded in ('"in the bucket', "the bucket's baseline", "from the bucket", "to the bucket"):
            self.assertNotIn(hard_coded, page)

    def test_a_folder_store_is_named_by_its_path_and_needs_no_aws(self) -> None:
        store = Path(self.tmp.name, "shared")
        with mock.patch.object(sc, "STORAGE_TYPE", "directory"), \
                mock.patch.object(sc, "CONFIG", {"storage": {"type": "directory", "path": str(store)}}), \
                mock.patch.object(ui.shutil, "which", lambda _name: None):
            status = self.ok("GET", "/api/sync/status")
        self.assertEqual({k: status[k] for k in ("configured", "kind", "label", "bucket", "prefix", "aws")},
                         {"configured": True, "kind": "directory", "label": str(store), "bucket": str(store),
                          "prefix": "", "aws": True})
        with mock.patch.object(sc, "STORAGE_TYPE", "directory"), mock.patch.object(sc, "CONFIG", {"storage": {}}):
            status = self.ok("GET", "/api/sync/status")
        self.assertEqual((status["configured"], status["label"]), (False, None))

    def test_status_and_the_runs_listing_carry_the_sync_state(self) -> None:
        status = self.ok("GET", "/api/sync/status")
        self.assertEqual({k: status[k] for k in ("configured", "kind", "label", "bucket", "prefix", "env", "aws", "lastPush", "lastPull")},
                         {"configured": True, "kind": "s3", "label": "s3://*******-****-assets/screencatalog/",
                          "bucket": "*******-****-assets", "prefix": "screencatalog",
                          "env": "SCREENCATALOG_S3_BUCKET", "aws": True, "lastPush": None, "lastPull": None})
        listing = self.ok("GET", "/api/runs")
        self.assertEqual(listing["sync"]["bucket"], "*******-****-assets")
        runs = {r["id"]: r for r in listing["runs"]}
        self.assertIsNone(runs["base"]["pushBlocked"])
        self.assertIsNone(runs["run"]["pushBlocked"])
        self.assertEqual(runs["part"]["pushBlocked"], "partial (B.): push --all includes it")
        self.assertIsNone(runs["base"]["sync"])

    def test_push_then_pull_as_jobs_with_the_conflict_rule(self) -> None:
        status = self.sync("push", {})
        self.assertEqual(status["exitCode"], 0, status["lines"])
        self.assertEqual((status["mode"], status["summary"]["pushed"]), ("push", ["base"]))
        lines = [line["text"] for line in status["lines"]]
        self.assertTrue(any(t.startswith("$ aws s3 sync ") and "s3://*******-****-assets/screencatalog/runs/base/" in t
                            and "--exclude logs/*" in t for t in lines), lines)
        self.assertEqual(json.loads((self.bucket / sc.BASELINES_FILE).read_text()), {DEVICE: "base"})
        self.assertEqual(self.runs()["base"]["sync"]["pushedRevision"], 1)
        self.assertTrue(self.ok("GET", "/api/sync/status")["lastPush"])
        remote = self.ok("GET", "/api/sync/remote")
        self.assertEqual(([r["id"] for r in remote["runs"]], remote["cached"]), (["base"], False))
        self.assertEqual(remote["runs"][0]["local"], "same")
        self.assertTrue(self.ok("GET", "/api/sync/remote")["cached"])
        self.assertEqual(self.sync("push", {"runs": ["run"]})["exitCode"], 0)
        remote = self.ok("GET", "/api/sync/remote")  # a push drops the cached listing
        self.assertEqual((sorted(r["id"] for r in remote["runs"]), remote["cached"]), (["base", "run"], False))
        # someone else's newer revision: push is refused with the reason, pull brings it
        theirs = json.loads((self.bucket / sc.RUNS_DIR / "base" / sc.RUN_FILE).read_text())
        theirs.update(revision=3, revisionAt="2026-10-08T12:00:00+00:00")
        (self.bucket / sc.RUNS_DIR / "base" / sc.RUN_FILE).write_text(json.dumps(theirs))
        status = self.sync("push", {})
        self.assertEqual(status["exitCode"], 2)
        self.assertIn("error: base: remote baseline is r3, yours is r1: pull first", [l["text"] for l in status["lines"]])
        status = self.sync("pull", {})
        self.assertEqual(status["exitCode"], 0, status["lines"])
        self.assertEqual(status["summary"]["pulled"], ["base"])
        self.assertEqual(self.runs()["base"]["revision"], 3)
        self.assertEqual(self.runs()["base"]["sync"]["pulledRevision"], 3)
        self.assertEqual(self.sync("push", {"runs": ["part"], "all": True})["exitCode"], 0)
        self.assertEqual(self.sync("pull", {"runs": ["part"], "force": True})["exitCode"], 0)

    def test_bad_bodies_and_an_unconfigured_server(self) -> None:
        for verb, body in (("push", {"runs": "base"}), ("push", {"runs": ["../x"]}), ("push", {"all": "yes"}),
                           ("push", {"bogus": 1}), ("pull", {"all": True})):
            code, data = self.request("POST", f"/api/sync/{verb}", body)
            self.assertEqual(code, 400, (verb, body, data))
        with mock.patch.dict(os.environ, {sc.BUCKET_ENV: ""}):
            status = self.ok("GET", "/api/sync/status")
            self.assertEqual((status["configured"], status["bucket"]), (False, None))
            for method, path, body in (("POST", "/api/sync/push", {}), ("POST", "/api/sync/pull", {}),
                                       ("GET", "/api/sync/remote", None)):
                code, data = self.request(method, path, body)
                self.assertEqual(code, 409, data)
                self.assertIn("sync is not configured: set SCREENCATALOG_S3_BUCKET", data["error"])
        with mock.patch.dict(os.environ, {"PATH": str(Path(self.tmp.name, "nowhere"))}):
            self.assertFalse(self.ok("GET", "/api/sync/status")["aws"])
            code, data = self.request("POST", "/api/sync/push", {})
            self.assertEqual((code, data["error"]), (409, "the aws CLI is not on PATH: brew install awscli"))

    def test_the_sync_panel(self) -> None:
        page = PageTests.page(self)
        recordings = PageTests.view(self, page, "recordings")
        self.assertLess(recordings.index('id="syncPanel"'), recordings.index('id="recGroups"'))
        for marker in ('id="syncBucket"', 'id="btnPushBaseline"', ">Push baseline<", 'id="btnPull"', ">Pull<",
                       'id="syncLast"', 'id="syncRemote"', 'id="syncLog"'):
            self.assertIn(marker, recordings)
        function = lambda name: PageTests.function(self, page, name)  # noqa: E731
        sync = function("renderSync")
        self.assertIn('"not configured: set storage in screencatalog.json, or " + sync.env + " in the server\'s environment"', sync)
        self.assertIn(": sync.label;", sync)
        self.assertIn('"last push " + fmtTime(sync.lastPush) + " · last pull " + fmtTime(sync.lastPull)', sync)
        row = function("runRow")
        self.assertIn('"data-action": "push", "data-blocked": pushWhy || null,', row)
        self.assertIn("disabled: !!pushWhy || !syncReady || jobActive, onclick: () => syncPush([run.id])}, \"Push\")", row)
        self.assertIn('"Not pushed: " + pushWhy', row)
        self.assertIn("syncLine(run.sync)", row)
        self.assertIn('"pushed " + fmtTime(state.pushedAt)', function("syncLine"))
        self.assertIn('api("/api/sync/push", body)', function("syncPush"))
        self.assertIn('api("/api/sync/pull", runs ? {runs: runs} : {})', function("syncPull"))
        self.assertIn('"/api/sync/remote"', function("loadRemote"))
        self.assertIn('push: "Pushing", pull: "Pulling"', function("updateStrip"))
        self.assertIn('mode === "push" || mode === "pull" ? "syncLog" : "log"', function("log"))
        self.assertIn("renderSync(data.sync);", function("renderRecordings"))


# MARK: - Device types and clone fleets

class FakeSimctl:
    """`simctl` over an in-memory device set whose data folders are real temp folders, so the app check
    (`installed_app` reads Containers/Bundle/Application) and `clone` (copies the data folder) behave
    as the real ones do. Like the real one, `clone` refuses a booted source."""

    def __init__(self, test: unittest.TestCase, root: Path):
        self.root, self.devices, self.calls, self.serial = root, [], [], 0
        original = sc.simctl
        test.addCleanup(lambda: setattr(sc, "simctl", original))
        sc.simctl = self

    def add(self, name: str, state: str = "Shutdown", type_id: str = T17, runtime: str = IOS27,
            app_modified: float | None = None) -> str:
        self.serial += 1
        udid = f"{self.serial:08X}-0000-4000-8000-000000000000"
        data = self.root / udid / "data"
        data.mkdir(parents=True)
        if app_modified is not None:
            self.install_app(data, app_modified)
        self.devices.append({"udid": udid, "name": name, "state": state, "deviceTypeIdentifier": type_id,
                             "runtime": runtime, "dataPath": str(data), "isAvailable": True})
        return udid

    @staticmethod
    def install_app(data: Path, modified: float) -> None:
        app = data / "Containers" / "Bundle" / "Application" / "C0FFEE" / "Sample.app"
        shutil.rmtree(app.parent, ignore_errors=True)
        app.mkdir(parents=True)
        (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": BUNDLE, "CFBundleExecutable": "Sample"}))
        (app / "Sample.debug.dylib").write_bytes(b"code")
        os.utime(app / "Sample.debug.dylib", (modified, modified))

    def device(self, udid: str) -> dict:
        return next(d for d in self.devices if d["udid"] == udid)

    def state(self, name: str) -> str:
        return next(d["state"] for d in self.devices if d["name"] == name)

    def verbs(self) -> list[tuple]:
        return [c for c in self.calls if c[0] != "list"]

    def __call__(self, *args, check=True, timeout=60):
        self.calls.append(args)
        out = ""
        if args[0] == "list":
            by_runtime: dict[str, list] = {}
            for d in self.devices:
                by_runtime.setdefault(d["runtime"], []).append({k: v for k, v in d.items() if k != "runtime"})
            out = json.dumps({
                "devicetypes": [{"identifier": T17, "name": "iPhone 17"}, {"identifier": T18, "name": "iPhone 18 Pro"}],
                "runtimes": [{"identifier": IOS27, "name": "iOS 27.0", "buildversion": "24A434", "platform": "iOS"},
                             {"identifier": WATCH, "name": "watchOS 12.0", "buildversion": "23R1", "platform": "watchOS"}],
                "devices": by_runtime})
        elif args[0] == "shutdown":
            self.device(args[1])["state"] = "Shutdown"
        elif args[0] == "bootstatus":
            self.device(args[1])["state"] = "Booted"
        elif args[0] == "clone":
            source = self.device(args[1])
            if source["state"] == "Booted":
                raise RuntimeError("xcrun simctl clone failed (149): Unable to clone device in current state: Booted")
            udid = self.add(args[2], type_id=source["deviceTypeIdentifier"], runtime=source["runtime"])
            shutil.rmtree(self.root / udid / "data")
            shutil.copytree(source["dataPath"], self.root / udid / "data")
            out = udid + "\n"
        elif args[0] == "install":
            self.install_app(Path(self.device(args[1])["dataPath"]), time.time())
        elif args[0] == "delete":
            self.devices.remove(self.device(args[1]))
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


class FleetTests(unittest.TestCase):
    """`resolve_fleet`: which simulators a device type + clone count becomes, and what it runs to get there."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sim = FakeSimctl(self, Path(tmp.name))
        self.lines: list[str] = []

    def resolve(self, clones: int, device_type: str = TYPE17) -> dict:
        return ui.resolve_fleet(device_type, clones, BUNDLE, self.lines.append)

    def test_booted_template_is_shut_down_to_clone_then_everything_boots(self) -> None:
        template = self.sim.add("iPhone 17", "Booted", app_modified=1_700_000_000)
        self.sim.add("iPhone 17 spare")  # shut down, no app: not the template
        fleet = self.resolve(3)
        c1, c2 = (next(d["udid"] for d in self.sim.devices if d["name"] == f"screencatalog-clone-{k}") for k in (1, 2))
        self.assertEqual(fleet, {"template": template, "udids": [template, c1, c2], "clones": [c1, c2], "created": [c1, c2]})
        verbs = self.sim.verbs()
        self.assertEqual(verbs[:3], [("shutdown", template), ("clone", template, "screencatalog-clone-1"),
                                     ("clone", template, "screencatalog-clone-2")])
        self.assertEqual(sorted(v[1] for v in verbs[3:]), sorted([template, c1, c2]))
        self.assertTrue(all(v[0] == "bootstatus" for v in verbs[3:]))
        self.assertEqual({self.sim.state(n) for n in ("iPhone 17", "screencatalog-clone-1", "screencatalog-clone-2")}, {"Booted"})
        self.assertIsNotNone(ui.installed_app(dict(self.sim.device(c2)), BUNDLE))  # the clone carries the app
        self.assertTrue(any("needs its source shut down" in line for line in self.lines))

    def test_existing_clones_are_reused_and_only_the_missing_one_is_made(self) -> None:
        template = self.sim.add("iPhone 17", "Booted", app_modified=1_700_000_000)
        c1 = self.sim.add("screencatalog-clone-1", app_modified=1_700_000_000)
        fleet = self.resolve(3)
        c2 = fleet["udids"][2]
        self.assertEqual((fleet["udids"][:2], fleet["created"]), ([template, c1], [c2]))
        self.assertEqual([v for v in self.sim.verbs() if v[0] == "clone"], [("clone", template, "screencatalog-clone-2")])
        self.sim.calls.clear()
        self.sim.device(c1)["state"] = self.sim.device(c2)["state"] = "Shutdown"  # as a finished run leaves them
        again = self.resolve(3)
        self.assertEqual((again["udids"], again["created"]), ([template, c1, c2], []))
        self.assertEqual(sorted(self.sim.verbs()), sorted([("bootstatus", c1, "-b"), ("bootstatus", c2, "-b")]))
        self.assertFalse([v for v in self.sim.verbs() if v[0] in ("clone", "shutdown")])

    def test_fewer_clones_use_a_prefix_and_leave_the_rest_alone(self) -> None:
        template = self.sim.add("iPhone 17", "Booted", app_modified=1_700_000_000)
        c1 = self.sim.add("screencatalog-clone-1", "Booted", app_modified=1_700_000_000)
        self.sim.add("screencatalog-clone-2", app_modified=1_700_000_000)
        self.assertEqual(self.resolve(2)["udids"], [template, c1])
        self.assertEqual(self.resolve(1)["udids"], [template])
        self.assertEqual(self.sim.verbs(), [])

    def test_a_stale_clone_gets_the_templates_app(self) -> None:
        template = self.sim.add("iPhone 17", "Booted", app_modified=1_700_000_000)
        c1 = self.sim.add("screencatalog-clone-1", app_modified=1_600_000_000)
        self.resolve(2)
        app = ui.installed_app(dict(self.sim.device(template)), BUNDLE)["path"]
        self.assertEqual(self.sim.verbs(), [("bootstatus", c1, "-b"), ("install", c1, app)])

    def test_shut_down_template_is_cloned_without_a_shutdown_and_booted(self) -> None:
        other = self.sim.add("iPhone 18 Pro", "Booted", type_id=T18, app_modified=1_700_000_000)
        template = self.sim.add("iPhone 17", app_modified=1_700_000_000)
        fleet = self.resolve(2)
        verbs = self.sim.verbs()
        self.assertEqual(verbs[0], ("clone", template, "screencatalog-clone-1"))
        self.assertNotIn(("shutdown", template), verbs)
        self.assertEqual(sorted(v[1] for v in verbs[1:]), sorted(fleet["udids"]))
        self.assertNotIn(other, fleet["udids"])

    def test_template_is_a_booted_one_else_a_shut_down_one(self) -> None:
        first = self.sim.add("iPhone 17 a", app_modified=1_700_000_000)
        booted = self.sim.add("iPhone 17 b", "Booted", app_modified=1_700_000_000)
        self.sim.add("screencatalog-clone-1", "Booted", app_modified=1_700_000_000)  # a clone is never the template
        self.assertEqual(self.resolve(1)["template"], booted)
        self.sim.device(booted)["state"] = "Shutdown"
        self.sim.add("iPhone 17 0 bare")  # shut down, no app: a shut-down one with the app goes first
        self.assertEqual(self.resolve(1)["template"], first)
        self.assertEqual(self.sim.state("iPhone 17 a"), "Booted")  # a shut-down template is booted
        self.sim.device(first)["state"] = "Shutdown"
        bare_booted = self.sim.add("iPhone 17 c", "Booted")  # booted wins, so it is the template, and it has no app
        with self.assertRaises(ui.RequestError) as caught:
            self.resolve(1)
        self.assertIn(bare_booted, str(caught.exception))

    def test_template_without_the_app_is_409_and_nothing_runs(self) -> None:
        self.sim.add("iPhone 17", "Booted")
        with self.assertRaises(ui.RequestError) as caught:
            self.resolve(3)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("build & install first", str(caught.exception))
        self.assertEqual(self.sim.verbs(), [])

    def test_unknown_type_and_bad_counts(self) -> None:
        self.sim.add("iPhone 17", "Booted", app_modified=1_700_000_000)
        with self.assertRaises(ui.RequestError) as caught:
            self.resolve(1, "iPhone-99@iOS-27-0")
        self.assertEqual(caught.exception.status, 404)
        for bad in (0, 9, True):
            with self.subTest(clones=bad), self.assertRaises(ui.RequestError):
                self.resolve(bad)

    def test_device_types_group_and_report_the_pin(self) -> None:
        catalog = Path(self.sim.root, "catalog")
        make_run(catalog, "base", full(), started=1, pinned=True)
        sc.write_run(catalog / sc.RUNS_DIR / "base", revision=3, revisionAt="2026-10-08T14:46:00+00:00")
        make_run(catalog, "pro", full(), kind=KIND18, started=2)  # a run, not a pin: no baseline for that type
        self.sim.add("iPhone 18 Pro", "Booted", type_id=T18)
        self.sim.add("iPhone 17", "Booted")
        self.sim.add("iPhone 17 spare")
        self.sim.add("screencatalog-clone-1")
        self.sim.add("Watch", runtime=WATCH)  # not iOS: not offered
        body = ui.device_types(ui.list_simulators(), catalog)
        self.assertEqual(set(body), {"deviceTypes"})
        first, second = body["deviceTypes"]
        self.assertEqual(first, {"id": TYPE17, "model": "iPhone 17", "os": "iOS 27.0", "osBuild": "24A434",
                                 "deviceDir": DEVICE, "available": 2, "booted": 1, "clones": 1, "isBaseline": True,
                                 "baselineOsBuild": "24A434", "family": "iPhone",
                                 "pinned": {"run": "base", "model": "iPhone 17", "os": "iOS 27.0", "osBuild": "24A434",
                                            "revision": 3, "revisionAt": "2026-10-08T14:46:00+00:00",
                                            "counts": {"light": 2, "dark": 2}}})
        self.assertEqual((second["id"], second["isBaseline"], second["pinned"], second["available"], second["clones"]),
                         (TYPE18, False, None, 1, 0))

    def test_device_types_are_iphones_then_ipads_then_newest_os_then_natural_model_order(self) -> None:
        catalog = Path(self.sim.root, "empty")
        sims = [{"model": m, "os": o, "osBuild": "x", "state": "Shutdown", "udid": f"u{i}", "name": m,
                 "deviceType": f"{m}@{o}", "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0", "isClone": False}
                for i, (m, o) in enumerate([("iPhone 18 Pro", "iOS 27.0"), ("iPad mini (A17 Pro)", "iOS 26.2"), ("iPhone 17", "iOS 26.2"),
                                            ("iPhone 17 Pro", "iOS 27.0"), ("Apple Vision Pro", "iOS 27.0"),
                                            ("iPhone 17", "iOS 27.0"), ("iPad (A16)", "iOS 27.0")])]
        body = ui.device_types(sims, catalog)
        self.assertEqual([(t["family"], t["model"], t["os"]) for t in body["deviceTypes"]],
                         [("iPhone", "iPhone 17", "iOS 27.0"), ("iPhone", "iPhone 17 Pro", "iOS 27.0"),
                          ("iPhone", "iPhone 18 Pro", "iOS 27.0"), ("iPhone", "iPhone 17", "iOS 26.2"),
                          ("iPad", "iPad (A16)", "iOS 27.0"), ("iPad", "iPad mini (A17 Pro)", "iOS 26.2"),
                          ("other", "Apple Vision Pro", "iOS 27.0")])
        self.assertEqual([ui.device_family(m) for m in ("iPhone 17", "iPad Air 13-inch (M4)", "ipad", "", None)],
                         ["iPhone", "iPad", "iPad", "other", "other"])
        self.assertEqual(ui.device_types([], Path(self.sim.root, "empty")), {"deviceTypes": []})



VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               {free}.
Pages active:                            900000.
Pages inactive:                           {inactive}.
Pages speculative:                        {speculative}.
Pages wired down:                         300000.
"""


def fake_host(free_gb: float, swap_used_m: float = 0.0, swap_total_m: float = 4096.0, ram: int = 36 * 1024 ** 3,
              fail: str | None = None):
    """A `run(*argv)` for `host_capacity`: `free_gb` split over free, inactive and speculative pages."""
    pages = int(free_gb * 1024 ** 3 / 16384)
    stat = VM_STAT.format(free=pages // 2, inactive=pages // 4, speculative=pages - pages // 2 - pages // 4)
    swap = f"total = {swap_total_m:.2f}M  used = {swap_used_m:.2f}M  free = {swap_total_m - swap_used_m:.2f}M  (encrypted)"

    def run(*argv: str) -> str:
        if argv[0] == fail:
            raise OSError("boom")
        return {"sysctl -n hw.memsize": f"{ram}\n", "sysctl -n vm.swapusage": swap + "\n", "vm_stat": stat}[" ".join(argv)]
    return run


class HostCapacityTests(unittest.TestCase):
    SIMS = [{"state": "Booted"}, {"state": "Shutdown"}, {"state": "Booted"}]

    def test_normal_host_recommends_free_memory_over_the_per_simulator_estimate(self) -> None:
        self.assertEqual(ui.GB_PER_SIMULATOR, 2.5)
        with mock.patch.object(ui.sys, "platform", "darwin"):
            self.assertEqual(ui.host_capacity(self.SIMS, fake_host(10.0, swap_used_m=100)),
                             {"ramGB": 36.0, "freeGB": 10.0, "swapUsedGB": 0.1, "swapTotalGB": 4.0,
                              "bootedSimulators": 2, "recommendedClones": 4})
            self.assertEqual(ui.host_capacity(self.SIMS, fake_host(2.4))["recommendedClones"], 1)  # clamped up
            self.assertEqual(ui.host_capacity([], fake_host(100.0))["recommendedClones"], ui.MAX_CLONES)  # and down

    def test_heavy_swap_takes_one_off_but_never_below_one(self) -> None:
        with mock.patch.object(ui.sys, "platform", "darwin"):
            heavy = ui.host_capacity(self.SIMS, fake_host(10.0, swap_used_m=18_000, swap_total_m=20_000))
            self.assertEqual((heavy["recommendedClones"], heavy["swapUsedGB"], heavy["swapTotalGB"]), (3, 17.6, 19.5))
            half = ui.host_capacity(self.SIMS, fake_host(10.0, swap_used_m=2048, swap_total_m=4096))
            self.assertEqual(half["recommendedClones"], 4)  # exactly half is not over half
            self.assertEqual(ui.host_capacity(self.SIMS, fake_host(1.0, swap_used_m=4000))["recommendedClones"], 1)

    def test_unreadable_host_has_null_memory_and_the_fallback(self) -> None:
        expected = {"ramGB": None, "freeGB": None, "swapUsedGB": None, "swapTotalGB": None, "bootedSimulators": 2,
                    "recommendedClones": 2}
        with mock.patch.object(ui.sys, "platform", "darwin"):
            for command in ("sysctl", "vm_stat"):
                with self.subTest(failing=command):
                    self.assertEqual(ui.host_capacity(self.SIMS, fake_host(10.0, fail=command)), expected)
            self.assertEqual(ui.host_capacity(self.SIMS, lambda *argv: "unexpected output"), expected)
        with mock.patch.object(ui.sys, "platform", "linux"):
            self.assertEqual(ui.host_capacity(self.SIMS, fake_host(10.0)), expected)


class FleetServerTests(ServerCase):
    """`/api/device-types`, `/api/run` with `deviceType` + `clones` (the build and install a record does
    first), and Delete clones, over the fake simulator set; records go through the real `run_catalog`."""

    def setUp(self) -> None:
        super().setUp()
        self.sims = FakeSimctl(self, Path(self.tmp.name, "devices"))
        self.template = self.sims.add("iPhone 17", "Booted", app_modified=1_700_000_000)
        self.sims.add("iPhone 18 Pro", "Booted", type_id=T18, app_modified=1_700_000_000)

    def test_device_types_endpoint(self) -> None:
        body = self.ok("GET", "/api/device-types")
        self.assertEqual([(t["id"], t["isBaseline"], (t["pinned"] or {}).get("run")) for t in body["deviceTypes"]],
                         [(TYPE17, True, "base"), (TYPE18, False, None)])
        self.assertEqual(self.request("GET", "/api/simulators")[0], 404)  # replaced

    def test_device_types_carries_the_host_read_per_request(self) -> None:
        reads = iter([{"recommendedClones": 3, "bootedSimulators": 2}, {"recommendedClones": 1, "bootedSimulators": 2}])
        with mock.patch.object(ui, "host_capacity", side_effect=lambda sims: next(reads)) as host:
            first = self.ok("GET", "/api/device-types")
            second = self.ok("GET", "/api/device-types")
        self.assertEqual((first["host"]["recommendedClones"], second["host"]["recommendedClones"]), (3, 1))
        self.assertEqual(host.call_args.args[0], ui.list_simulators())  # the booted count comes from the same listing
        self.assertEqual(set(first), {"deviceTypes", "host"})

    def test_device_types_host_has_every_field_on_this_machine(self) -> None:
        host = self.ok("GET", "/api/device-types")["host"]
        self.assertEqual(set(host), {"ramGB", "freeGB", "swapUsedGB", "swapTotalGB", "bootedSimulators", "recommendedClones"})
        self.assertEqual(host["bootedSimulators"], 2)  # both fake simulators are booted
        self.assertIn(host["recommendedClones"], range(1, ui.MAX_CLONES + 1))

    def test_record_with_device_type_and_clones_resolves_udids_and_shuts_clones_down(self) -> None:
        started = self.start({"mode": "record", "deviceType": TYPE17, "clones": 3, "appearance": "light"})
        done = self.wait_idle()
        clones = [d["udid"] for d in self.sims.devices if d["name"].startswith("screencatalog-clone-")]
        self.assertEqual(len(clones), 2)
        self.assertEqual(done["udids"], [self.template] + clones)
        texts = [line["text"] for line in done["lines"]]
        argv = next(t for t in texts if t.startswith("$ screencatalog.py record"))
        self.assertIn(" ".join("--udid " + u for u in [self.template] + clones), argv)
        self.assertEqual(sum(t.startswith("$ xcrun simctl clone") for t in texts), 2)
        # The record built first (on the template), then installed the build on the template whose app differed,
        # and the clones, cloned from the template before the install, got it too.
        self.assertEqual(self.builds, [self.template])
        self.assertTrue(any(t.startswith("building Sample for iPhone 17") for t in texts), texts)
        self.assertTrue(any(t.startswith("built in ") for t in texts), texts)
        self.assertNotEqual(done["phase"], "building")
        built_sha = sc.app_identity(self.built)["sha256"]
        self.assertIn(f"iPhone 17 has build {sc.app_identity(Path(ui.installed_app(self.sims.device(self.template), BUNDLE)['path']))['sha256']}"
                      if False else "installing " + built_sha, " ".join(texts))
        self.assertEqual([v[1] for v in self.sims.verbs() if v[0] == "install"], [self.template] + clones)
        self.assertLess(texts.index(next(t for t in texts if t.startswith("built in "))),
                        texts.index(next(t for t in texts if t.startswith("$ xcrun simctl clone"))))
        self.assertEqual(self.sims.state("iPhone 17"), "Booted")
        self.assertEqual({self.sims.state(f"screencatalog-clone-{k}") for k in (1, 2)}, {"Shutdown"})
        run = sc.read_run(self.catalog / sc.RUNS_DIR / done["summary"]["run"])
        self.assertEqual([s["udid"] for s in run["simulators"]], [self.template] + clones)
        self.assertEqual((run["params"]["deviceType"], run["params"]["clones"], run["params"]["udids"]),
                         (TYPE17, 3, [self.template] + clones))
        time.sleep(1.1)  # the next run id is the next second
        again = self.start({"mode": "record", "deviceType": TYPE17, "clones": 3, "appearance": "light"})
        texts = [line["text"] for line in self.wait_idle()["lines"]]
        self.assertFalse(any(t.startswith("$ xcrun simctl clone") or "simctl create" in t for t in texts), texts)
        self.assertEqual(sum(t.startswith("reusing screencatalog-clone-") for t in texts), 2)
        self.assertNotEqual(again["runId"], started["runId"])
        self.assertNotEqual(self.wait_idle()["summary"]["run"], done["summary"]["run"])

    def test_a_type_without_a_pin_records_too(self) -> None:
        sc.device_info = lambda udid: {"udid": udid, "simulatorName": "iPhone 18 Pro", "osBuild": "24A434",
                                       "model": "iPhone 18 Pro", "os": "iOS 27.0"}
        self.start({"mode": "record", "deviceType": TYPE18, "clones": 1, "appearance": "light"})
        done = self.wait_idle()
        self.assertEqual(done["exitCode"], 0, done["lines"])
        self.assertEqual((done["summary"]["pinned"], done["summary"]["comparable"]), (None, False))

    def test_back_compat_udids(self) -> None:
        self.start({"mode": "record", "udids": [UDID_A], "appearance": "light"})
        self.assertEqual(self.wait_idle()["udids"], [UDID_A])
        self.assertEqual([v for v in self.sims.verbs() if v[0] in ("clone", "bootstatus", "shutdown")], [])

    def test_refusals(self) -> None:
        code, body = self.request("POST", "/api/run", {"mode": "record", "deviceType": "iPhone-99@iOS-27-0"})
        self.assertEqual(code, 404, body)
        for bad in ({"mode": "record", "deviceType": TYPE17, "clones": 0},
                    {"mode": "record", "deviceType": TYPE17, "clones": 9},
                    {"mode": "record", "deviceType": TYPE17, "clones": "2"},
                    {"mode": "record", "deviceType": TYPE17, "udids": [UDID_A]},
                    {"mode": "record", "deviceType": "; rm -rf /"}):
            with self.subTest(body=bad):
                self.assertEqual(self.request("POST", "/api/run", bad)[0], 400)
        self.assertEqual([v for v in self.sims.verbs() if v[0] != "list"], [])

    def test_delete_clones_removes_only_that_types_clones(self) -> None:
        self.sims.add("screencatalog-clone-1", "Booted")
        self.sims.add("screencatalog-clone-2")
        other = self.sims.add("screencatalog-clone-1", type_id=T18)
        code, body = self.request("POST", "/api/clones/delete", {"deviceType": TYPE17})
        self.assertEqual(code, 200, body)
        self.assertEqual([d["name"] for d in body["deleted"]], ["screencatalog-clone-1", "screencatalog-clone-2"])
        self.assertEqual({d["name"] for d in self.sims.devices}, {"iPhone 17", "iPhone 18 Pro", "screencatalog-clone-1"})
        self.assertIn(other, {d["udid"] for d in self.sims.devices})
        self.assertEqual(self.request("POST", "/api/clones/delete", {"deviceType": "x"})[0], 400)

    def test_a_record_of_a_build_that_already_has_a_run_is_refused(self) -> None:
        # The fake simulator reports app 06b3f4885b4e and commit 0123456789abcdef: a complete run of that build exists.
        make_run(self.catalog, "same-build", full(), started=9, git={"sha": "0123456789abcdef", "dirty": False})
        sc.write_run(self.catalog / sc.RUNS_DIR / "same-build", app={"version": "2.0", "build": "7", "sha256": "06b3f4885b4e"})
        self.ok("POST", "/api/run", {"mode": "record", "deviceType": TYPE17, "clones": 1, "appearance": "light"}, 202)
        done = self.wait_idle()
        texts = [line["text"] for line in done["lines"]]
        self.assertEqual(done["exitCode"], 1, texts)
        self.assertEqual(done["summary"]["duplicate"], {"run": "same-build", "where": "local", "git": "0123456789abcdef",
                                                        "app": "06b3f4885b4e"})
        self.assertTrue(any(t.startswith("a complete run of this build exists already: same-build (in this catalog)") for t in texts), texts)
        self.assertFalse(any(t.startswith("$ screencatalog.py record") for t in texts))
        self.assertNotIn("run", done["summary"])  # no run folder was ever made
        # The only way past it is deleting that run.
        self.ok("POST", "/api/runs/delete", {"run": "same-build"})
        time.sleep(1.1)
        self.start({"mode": "record", "deviceType": TYPE17, "clones": 1, "appearance": "light"})
        again = self.wait_idle()
        self.assertEqual(again["exitCode"], 0, [line["text"] for line in again["lines"]])
        self.assertNotIn("duplicate", again["summary"])

    def test_a_template_with_the_build_already_is_not_reinstalled(self) -> None:
        (self.built / "Sample.debug.dylib").write_bytes(b"code")  # what FakeSimctl's apps hold
        self.start({"mode": "record", "deviceType": TYPE17, "clones": 1, "appearance": "light"})
        done = self.wait_idle()
        texts = [line["text"] for line in done["lines"]]
        self.assertEqual(done["exitCode"], 0, texts)
        self.assertIn(f"iPhone 17 already has this build ({sc.app_identity(self.built)['sha256']})", texts)
        self.assertEqual([v for v in self.sims.verbs() if v[0] == "install"], [])

    def test_a_bare_simulator_gets_the_build(self) -> None:
        bare = self.sims.add("iPhone 17 bare", "Booted")
        self.sims.device(self.template)["state"] = "Shutdown"
        shutil.rmtree(Path(self.sims.device(self.template)["dataPath"], "Containers"))
        self.start({"mode": "record", "deviceType": TYPE17, "clones": 1, "appearance": "light"})
        done = self.wait_idle()
        texts = [line["text"] for line in done["lines"]]
        self.assertEqual(done["exitCode"], 0, texts)
        self.assertEqual([v[1] for v in self.sims.verbs() if v[0] == "install"], [bare])
        self.assertTrue(any(t.startswith("iPhone 17 bare has no app: installing") for t in texts), texts)

    def test_a_failed_build_ends_the_record_before_any_simulator_is_touched(self) -> None:
        self.build_exit = 65
        self.ok("POST", "/api/run", {"mode": "record", "deviceType": TYPE17, "clones": 2, "appearance": "light"}, 202)
        done = self.wait_idle()  # the capture runner is never reached, so not self.start()
        texts = [line["text"] for line in done["lines"]]
        self.assertEqual(done["exitCode"], 1, texts)
        self.assertIn("xcodebuild exited 65: nothing captured", texts)
        self.assertEqual([v for v in self.sims.verbs() if v[0] != "list"], [])
        self.assertNotIn("run", done["summary"])  # no run folder was ever made

    def test_skip_build_still_checks_the_install(self) -> None:
        self.start({"mode": "record", "deviceType": TYPE17, "clones": 1, "appearance": "light", "skipBuild": True})
        done = self.wait_idle()
        texts = [line["text"] for line in done["lines"]]
        self.assertEqual(done["exitCode"], 0, texts)
        self.assertEqual(self.builds, [])
        self.assertTrue(any(t.startswith("build skipped") for t in texts), texts)
        self.assertEqual([v[1] for v in self.sims.verbs() if v[0] == "install"], [self.template])
        # No build in DerivedData: the simulators keep their app, and only a bare template is refused.
        shutil.rmtree(self.built)
        self.start({"mode": "record", "deviceType": TYPE17, "clones": 1, "appearance": "light", "skipBuild": True})
        texts = [line["text"] for line in self.wait_idle()["lines"]]
        self.assertTrue(any("the simulators keep the app they have" in t for t in texts), texts)
        shutil.rmtree(Path(self.sims.device(self.template)["dataPath"], "Containers"))
        code, body = self.request("POST", "/api/run", {"mode": "record", "deviceType": TYPE17, "clones": 1, "skipBuild": True})
        self.assertEqual(code, 409, body)
        self.assertIn("has no com.example.sample and DerivedData has no Debug build", body["error"])
        self.assertEqual(self.request("POST", "/api/run", {"mode": "record", "deviceType": TYPE17, "skipBuild": "yes"})[0], 400)
        self.assertEqual(self.request("POST", "/api/build", {"deviceType": TYPE17})[0], 404)  # the button is gone

class BuildLogSummaryTests(unittest.TestCase):
    """xcodebuild's output is summarised into the job log: errors once each, the verdict, a warning count."""

    def test_warnings_stay_out_errors_once_and_the_verdict_in(self) -> None:
        lines: list[str] = []
        summary = ui.BuildLogSummary(lines.append)
        for text in ["/a/B.swift:36:34: warning: 'sendable' is deprecated", "   36 |     @Reducer(state: .equatable)",
                     "      |          `- warning: 'sendable' is deprecated", "/a/C.swift:1:1: error: cannot find 'x' in scope",
                     "/a/D.swift:9:9: error: cannot find 'x' in scope", "error: the following command failed with exit code 0",
                     "error: the following command failed with exit code 0", "** BUILD FAILED **"]:
            summary.line(text)
        summary.finish(65, Path("/tmp/build-logs/ui-1.log"))
        self.assertEqual(lines, ["/a/C.swift:1:1: error: cannot find 'x' in scope",
                                 "error: the following command failed with exit code 0", "** BUILD FAILED **",
                                 "repeated: 2 × error: cannot find 'x' in scope; 2 × error: the following command failed with exit code 0",
                                 "xcodebuild exit 65: 2 warning lines, 2 distinct error lines; full output: /tmp/build-logs/ui-1.log"])


class ThumbnailTests(unittest.TestCase):
    """The thumbnail generator: size, box filter, and the mtime-keyed disk cache."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.source = self.dir / "src.png"
        self.target = self.dir / ".thumbs" / "baseline" / DEVICE / "light" / "src.png"
        width, height = 1000, 500  # left half red, right half blue, opaque RGBA like a simulator screenshot
        row = (b"\xff\x00\x00\xff" * (width // 2)) + (b"\x00\x00\xff\xff" * (width // 2))
        self.source.write_bytes(sc.encode_png(sc.Image(width, height, 4, row * height)))

    def test_size_and_box_filter(self) -> None:
        self.assertTrue(ui.thumbnail(self.source, self.target))
        thumb = sc.decode_png(self.target.read_bytes())
        self.assertEqual((thumb.width, thumb.height, thumb.channels), (250, 125, 3))  # factor round(1000 / 240) = 4
        self.assertEqual(thumb.data[:3], b"\xff\x00\x00")
        self.assertEqual(thumb.data[-3:], b"\x00\x00\xff")
        small = self.dir / "small.png"
        small.write_bytes(sc.encode_png(sc.Image(100, 50, 3, b"\x10" * 100 * 50 * 3)))
        self.assertTrue(ui.thumbnail(small, self.dir / "small-thumb.png"))
        self.assertEqual(sc.decode_png((self.dir / "small-thumb.png").read_bytes()).width, 100)  # never upscaled

    def test_cache_hit_on_same_mtime_and_regenerated_on_newer(self) -> None:
        self.assertTrue(ui.thumbnail(self.source, self.target))
        self.assertEqual(self.target.stat().st_mtime_ns, self.source.stat().st_mtime_ns)
        original = sc.decode_png

        def must_not_decode(blob):
            raise AssertionError("decoded a source whose thumbnail is cached")

        sc.decode_png = must_not_decode
        try:
            self.assertFalse(ui.thumbnail(self.source, self.target))
        finally:
            sc.decode_png = original
        # A re-recorded screenshot: new pixels, newer mtime.
        self.source.write_bytes(sc.encode_png(sc.Image(1000, 500, 3, b"\x00\xff\x00" * 1000 * 500)))
        stat = self.source.stat()
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        self.assertTrue(ui.thumbnail(self.source, self.target))
        self.assertEqual(sc.decode_png(self.target.read_bytes()).data[:3], b"\x00\xff\x00")
        self.assertEqual(self.target.stat().st_mtime_ns, self.source.stat().st_mtime_ns)
        self.assertFalse(ui.thumbnail(self.source, self.target))
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["src.png"])  # no temp files left


class MaskTests(unittest.TestCase):
    """The overlay mask: red and opaque exactly where a pixel differs by more than the threshold,
    transparent elsewhere; the diff writer's PNG reused when it is newer than both inputs; the
    thumbnail-sized mask a max over each block; the key-file cache."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.width, self.height = 30, 20
        base = bytearray(b"\x64" * self.width * self.height * 3)  # (100, 100, 100)
        self.base_image = sc.Image(self.width, self.height, 3, bytes(base))
        cur = bytearray(base)
        self.at_threshold, self.over, self.far = 5, 47, 599  # pixel indices
        cur[self.at_threshold * 3] = 100 + 8  # moved by exactly the threshold: not a difference
        cur[self.over * 3 + 1] = 100 + 9  # one past it, on green
        cur[self.far * 3 + 2] = 0  # blue moved by 100
        self.cur_image = sc.Image(self.width, self.height, 3, bytes(cur))
        self.baseline, self.current = self.dir / "baseline.png", self.dir / "current.png"
        self.baseline.write_bytes(sc.encode_png(self.base_image))
        self.current.write_bytes(sc.encode_png(self.cur_image))
        self.diff = self.dir / "diff.png"  # absent unless a test writes it
        self.target = self.dir / "mask" / "x.png"

    def opaque(self, path: Path) -> set[int]:
        image = sc.decode_png(path.read_bytes())
        self.assertEqual(image.channels, 4)
        alpha = image.data[3::4]
        for i, a in enumerate(alpha):  # red where opaque, fully transparent elsewhere
            self.assertEqual(image.data[i * 4:i * 4 + 4], b"\xff\x00\x00\xff" if a else b"\x00\x00\x00\x00")
        return {i for i, a in enumerate(alpha) if a}

    def test_transparent_except_differing_pixels_and_threshold_respected(self) -> None:
        self.assertTrue(ui.make_mask(self.baseline, self.current, self.diff, 8, self.target, "k8"))
        image = sc.decode_png(self.target.read_bytes())
        self.assertEqual((image.width, image.height), (self.width, self.height))
        self.assertEqual(self.opaque(self.target), {self.over, self.far})
        self.assertTrue(ui.make_mask(self.baseline, self.current, self.diff, 9, self.target, "k9"))
        self.assertEqual(self.opaque(self.target), {self.far})
        self.assertTrue(ui.make_mask(self.baseline, self.current, self.diff, 7, self.target, "k7"))
        self.assertEqual(self.opaque(self.target), {self.at_threshold, self.over, self.far})

    def test_reuses_the_diff_writers_png_only_when_newer_than_both_inputs(self) -> None:
        result = sc.diff_images(self.base_image, self.cur_image, 8)
        self.diff.write_bytes(sc.encode_png(sc.diff_image(self.base_image, result)))
        decoded = []
        original = sc.decode_png
        sc.decode_png = lambda blob: decoded.append(1) or original(blob)
        self.addCleanup(setattr, sc, "decode_png", original)
        ui.make_mask(self.baseline, self.current, self.diff, 8, self.target, "fresh")
        self.assertEqual(len(decoded), 1)  # the diff alone
        self.assertEqual(self.opaque(self.target), {self.over, self.far})
        stat = self.current.stat()  # the capture changed after the diff was written: recompute
        os.utime(self.current, ns=(stat.st_atime_ns, self.diff.stat().st_mtime_ns + 5_000_000_000))
        decoded.clear()
        ui.make_mask(self.baseline, self.current, self.diff, 8, self.target, "stale")
        self.assertEqual(len(decoded), 2)  # baseline and current
        self.assertEqual(self.opaque(self.target), {self.over, self.far})

    def test_thumbnail_mask_keeps_a_single_pixel(self) -> None:
        width, height = 1000, 600  # factor round(1000 / 240) = 4, as the screenshot thumbnail
        alpha = bytearray(width * height)
        alpha[301 * width + 997] = 255
        out_w, out_h, small = ui.shrink_alpha(width, height, bytes(alpha))
        self.assertEqual((out_w, out_h), (250, 150))
        self.assertEqual({i for i, a in enumerate(small) if a}, {75 * 250 + 249})
        self.assertEqual(set(small), {0, 255})
        self.assertEqual(ui.shrink_alpha(100, 50, b"\x00" * 5000), (100, 50, b"\x00" * 5000))  # never upscaled
        self.assertTrue(ui.make_mask(self.baseline, self.current, self.diff, 8, self.target, "k"))
        thumb = ui.mask_thumb_path(self.target)
        self.assertEqual(sc.decode_png(thumb.read_bytes()).width, self.width)

    def test_cache_by_key(self) -> None:
        self.assertTrue(ui.make_mask(self.baseline, self.current, self.diff, 8, self.target, "k1"))
        self.assertTrue(ui.mask_fresh(self.target, "k1"))
        self.assertFalse(ui.make_mask(self.baseline, self.current, self.diff, 8, self.target, "k1"))
        self.assertFalse(ui.mask_fresh(self.target, "k2"))
        self.assertTrue(ui.make_mask(self.baseline, self.current, self.diff, 8, self.target, "k2"))
        ui.mask_thumb_path(self.target).unlink()
        self.assertFalse(ui.mask_fresh(self.target, "k2"))  # the key vouches for both images
        self.assertEqual(sorted(p.name for p in self.target.parent.iterdir()), ["x.key", "x.png"])  # no temp files
        key = ui.mask_key(self.baseline, self.current, 8)
        stat = self.baseline.stat()
        os.utime(self.baseline, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
        self.assertNotEqual(ui.mask_key(self.baseline, self.current, 8), key)
        self.assertNotEqual(ui.mask_key(self.baseline, self.current, 9), ui.mask_key(self.baseline, self.current, 8))


if __name__ == "__main__":
    unittest.main()
