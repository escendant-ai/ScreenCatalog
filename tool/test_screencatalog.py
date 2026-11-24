#!/usr/bin/env python3
"""
Tests for screencatalog.py without a simulator: the PNG codec, the diff and its tolerance,
naming and exclusion filtering, and the run model: `record` writing one run folder and nothing
else (the simulator faked), comparisons keyed by the pair and the other run's revision (reused,
recomputed after an accept or an update, refused across simulator types), set-baseline,
update-baseline, accept, delete-run, prune, export / import, `list` / `comparable`, and `sync push` / `sync pull`
against a fake `aws` on PATH that mirrors S3 into a temp directory.

The decoder is the part most likely to be quietly wrong: screenshots use several PNG filter
types per image, and a mis-implemented filter decodes to plausible-looking garbage that then
"differs" everywhere. So every filter type is round-tripped through an independent encoder here.

Run from tool/: python3 -m unittest test_screencatalog
"""

# The scripts run under whatever `python3` is on PATH; Xcode ships 3.9.6.
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import plistlib
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

# The tests run against a fixed config, whatever screencatalog.json the working directory has.
os.environ["SCREENCATALOG_CONFIG"] = str(Path(__file__).resolve().parent / "testdata" / "screencatalog.json")
for _name in ("SCREENCATALOG_DIR", "SCREENCATALOG_S3_BUCKET", "SCREENCATALOG_REVIEWER"):
    os.environ.pop(_name, None)

import screencatalog as sc

T17 = ("iPhone 17", "iOS 27.0")
D17 = "iPhone_17_iOS_27.0"


def synthetic(width: int, height: int, channels: int) -> sc.Image:
    data = bytes((x * 37 + y * 11 + c * 101) & 0xFF for y in range(height) for x in range(width) for c in range(channels))
    return sc.Image(width, height, channels, data)


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def encode_with_filter(image: sc.Image, kinds: list[int]) -> bytes:
    """An encoder independent of the module's: row y uses filter kinds[y % len(kinds)]."""
    bpp, stride = image.channels, image.stride
    raw, prev = bytearray(), bytes(stride)
    for y in range(image.height):
        line = image.data[y * stride:(y + 1) * stride]
        kind = kinds[y % len(kinds)]
        out = bytearray()
        for i, v in enumerate(line):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            predictor = [0, a, b, (a + b) >> 1, paeth(a, b, c)][kind]
            out.append((v - predictor) & 0xFF)
        raw += bytes([kind]) + out
        prev = line

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    color = {1: 0, 2: 4, 3: 2, 4: 6}[image.channels]
    ihdr = struct.pack(">IIBBBBB", image.width, image.height, 8, color, 0, 0, 0)
    return sc.PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")



class AppIdentityTests(unittest.TestCase):
    """`app_identity`: version, build and a short binary hash read from an installed app bundle."""

    def test_reads_version_build_and_hashes_the_debug_dylib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp, "Sample.app")
            app.mkdir()
            (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "Sample",
                                                              "CFBundleShortVersionString": "1.4.2", "CFBundleVersion": "318"}))
            (app / "Sample").write_bytes(b"stub")
            (app / "Sample.debug.dylib").write_bytes(b"code" * 1000)
            identity = sc.app_identity(app)
            self.assertEqual((identity["version"], identity["build"]), ("1.4.2", "318"))
            self.assertTrue(identity["path"].endswith("Sample.debug.dylib"))
            self.assertEqual(identity["sha256"], hashlib.sha256(b"code" * 1000).hexdigest()[:12])
            (app / "Sample.debug.dylib").unlink()
            self.assertTrue(sc.app_identity(app)["path"].endswith("/Sample"))

class PNGCodecTests(unittest.TestCase):

    def test_encode_decode_round_trip(self) -> None:
        for channels in (3, 4):
            with self.subTest(channels=channels):
                image = synthetic(17, 9, channels)
                decoded = sc.decode_png(sc.encode_png(image))
                self.assertEqual((decoded.width, decoded.height, decoded.channels), (17, 9, channels))
                self.assertEqual(decoded.data, image.data)

    def test_every_filter_type_decodes(self) -> None:
        for channels in (3, 4):
            for kinds in ([0], [1], [2], [3], [4], [0, 1, 2, 3, 4]):
                with self.subTest(channels=channels, filters=kinds):
                    image = synthetic(13, 10, channels)
                    self.assertEqual(sc.decode_png(encode_with_filter(image, kinds)).data, image.data)

    def test_unsupported_png_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sc.decode_png(b"GIF89a")
        sixteen_bit = bytearray(sc.encode_png(synthetic(2, 2, 3)))
        sixteen_bit[24] = 16  # IHDR bit depth
        with self.assertRaises(ValueError):
            sc.decode_png(bytes(sixteen_bit))

    def test_to_rgb_drops_alpha(self) -> None:
        rgba = sc.Image(2, 1, 4, bytes([1, 2, 3, 255, 4, 5, 6, 0]))
        self.assertEqual(sc.to_rgb(rgba).data, bytes([1, 2, 3, 4, 5, 6]))


def real_screenshot() -> Path | None:
    """One recorded screenshot: $SCREENCATALOG_SAMPLE_PNG, else the first in the repo's catalog."""
    named = os.environ.get("SCREENCATALOG_SAMPLE_PNG")
    if named:
        return Path(named)
    return next(iter(sorted((sc.DEFAULT_CATALOG_DIR / sc.RUNS_DIR).glob("*/light/*.png"))), None)


class ImageIODecodeTests(unittest.TestCase):
    """The ImageIO decoder (macOS) and the pure-Python one give the same samples; the second is the fallback."""

    def setUp(self) -> None:
        if sc._imageio_decode(sc.encode_png(synthetic(2, 2, 3))) is None:
            self.skipTest("ImageIO is not available here")

    def test_synthetic_pngs_decode_identically(self) -> None:
        for channels in (1, 2, 3, 4):
            for kinds in ([0], [1], [2], [3], [4], [0, 1, 2, 3, 4]):
                with self.subTest(channels=channels, filters=kinds):
                    blob = encode_with_filter(synthetic(13, 10, channels), kinds)
                    fast, slow = sc._imageio_decode(blob), sc.decode_png_python(blob)
                    self.assertIsNotNone(fast)
                    self.assertEqual((fast.width, fast.height, fast.channels, fast.data),
                                     (slow.width, slow.height, slow.channels, slow.data))

    def test_a_real_screenshot_decodes_identically(self) -> None:
        path = real_screenshot()
        if path is None or not path.is_file():
            self.skipTest("no recorded screenshot to decode")
        blob = path.read_bytes()
        fast, slow = sc._imageio_decode(blob), sc.decode_png_python(blob)
        self.assertIsNotNone(fast)
        self.assertEqual((fast.width, fast.height, fast.channels), (slow.width, slow.height, slow.channels))
        self.assertTrue(fast.data == slow.data, "ImageIO and pure-Python samples differ")

    def test_the_pure_python_decoder_takes_over_when_imageio_fails(self) -> None:
        image = synthetic(9, 4, 4)
        blob = sc.encode_png(image)
        loaded = sc._IMAGEIO[0]

        class Broken:
            def decode(self, _blob):
                raise OSError("ctypes failure")

        sc._IMAGEIO[0] = Broken()
        self.addCleanup(sc._IMAGEIO.__setitem__, 0, loaded)
        self.assertEqual(sc.decode_png(blob).data, image.data)
        sc._IMAGEIO[0] = None  # the frameworks did not load
        self.assertEqual(sc.decode_png(blob).data, image.data)
        with self.assertRaises(ValueError):
            sc.decode_png(b"GIF89a")


def reference_diff(a: sc.Image, b: sc.Image, threshold: int) -> list[int]:
    """The per-pixel rule spelled out: any of R, G, B moved by more than `threshold`."""
    a, b = sc.to_rgb(a), sc.to_rgb(b)
    return [p for p in range(a.width * a.height)
            if any(abs(a.data[p * 3 + c] - b.data[p * 3 + c]) > threshold for c in range(3))]


class DiffTests(unittest.TestCase):

    def test_row_shortcut_and_lanes_match_the_per_pixel_rule(self) -> None:
        import random
        rnd = random.Random(7)
        for trial in range(200):
            width, height, channels = rnd.randint(1, 9), rnd.randint(1, 6), rnd.choice((1, 2, 3, 4))
            a = sc.Image(width, height, channels, bytes(rnd.randrange(256) for _ in range(width * height * channels)))
            data = bytearray(a.data)
            for _ in range(rnd.randint(0, 8)):
                i = rnd.randrange(len(data))
                data[i] = (data[i] + rnd.randint(-30, 30)) % 256
            b = sc.Image(width, height, channels, bytes(data))
            threshold = rnd.choice((0, 1, 8, 29, 255))
            with self.subTest(trial=trial):
                diff = sc.diff_images(a, b, threshold)
                expected = reference_diff(a, b, threshold)
                self.assertEqual((diff.pixels, diff.differing), (expected, len(expected)))
                painted = bytearray(sc.to_rgb(a).data.translate(bytes(v // 3 for v in range(256))))
                for p in expected:
                    painted[p * 3:p * 3 + 3] = b"\xff\x00\x00"
                self.assertEqual(sc.diff_image(a, diff).data, bytes(painted))

    def solid(self, value: int, width: int = 100, height: int = 10) -> sc.Image:
        return sc.Image(width, height, 3, bytes([value]) * (width * height * 3))

    def with_pixel(self, image: sc.Image, index: int, rgb: tuple[int, int, int]) -> sc.Image:
        data = bytearray(image.data)
        data[index * 3:index * 3 + 3] = bytes(rgb)
        return sc.Image(image.width, image.height, image.channels, bytes(data))

    def test_identical_images_have_no_difference(self) -> None:
        diff = sc.diff_images(self.solid(100), self.solid(100), 8)
        self.assertEqual((diff.differing, diff.total, diff.percent), (0, 1000, 0.0))

    def test_channel_threshold_is_exclusive(self) -> None:
        base = self.solid(100)
        at_threshold = self.with_pixel(base, 5, (108, 100, 100))
        over = self.with_pixel(base, 5, (100, 100, 91))
        self.assertEqual(sc.diff_images(base, at_threshold, 8).differing, 0)
        self.assertEqual(sc.diff_images(base, over, 8).pixels, [5])

    def test_percent_and_verdict(self) -> None:
        base = self.solid(0)
        current = base
        for index in range(2):  # 2 of 1000 pixels = 0.2%
            current = self.with_pixel(current, index * 37, (255, 255, 255))
        diff = sc.diff_images(base, current, 8)
        self.assertAlmostEqual(diff.percent, 0.2)
        self.assertEqual(sc.judge(diff, 0.1)[0], sc.FAIL)
        self.assertEqual(sc.judge(diff, 0.2)[0], sc.PASS)  # at the limit passes; only above fails

    def test_alpha_is_ignored(self) -> None:
        opaque = sc.Image(1, 1, 4, bytes([10, 20, 30, 255]))
        clear = sc.Image(1, 1, 4, bytes([10, 20, 30, 0]))
        self.assertEqual(sc.diff_images(opaque, clear, 8).differing, 0)

    def test_dimension_mismatch_fails(self) -> None:
        diff = sc.diff_images(self.solid(0, 10, 10), self.solid(0, 10, 11), 8)
        self.assertFalse(diff.dimensions_match)
        self.assertEqual(sc.judge(diff, 100.0), (sc.FAIL, "dimensions differ"))

    def test_diff_image_dims_baseline_and_paints_differences_red(self) -> None:
        base = self.solid(90, 3, 1)
        diff = sc.diff_images(base, self.with_pixel(base, 1, (0, 0, 0)), 8)
        out = sc.diff_image(base, diff)
        self.assertEqual(out.data, bytes([30, 30, 30, 255, 0, 0, 30, 30, 30]))

    def test_compare_files_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = self.solid(50)
            a, b, c = Path(tmp, "a.png"), Path(tmp, "b.png"), Path(tmp, "c.png")
            a.write_bytes(sc.encode_png(base))
            b.write_bytes(sc.encode_png(base))
            changed = base
            for index in range(10):
                changed = self.with_pixel(changed, index, (255, 0, 0))
            c.write_bytes(sc.encode_png(changed))
            self.assertEqual(sc.compare_files(a, b, 8, 0.1)[:2], (sc.PASS, "identical"))
            status, reason, diff, _ = sc.compare_files(a, c, 8, 0.1)
            self.assertEqual(status, sc.FAIL)
            self.assertEqual(diff.differing, 10)
            self.assertIn("1.0000% > 0.1%", reason)


class ExclusionTests(unittest.TestCase):

    ENTRIES = [{"name": n, "feature": n.split(".")[0]}
               for n in ("Feed.default", "Feed.empty", "Home.default", "Home.longName", "Settings.about")]

    def write(self, payload) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(payload, handle)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def test_missing_file_means_no_exclusions(self) -> None:
        self.assertEqual(sc.load_exclusions(Path("/nonexistent/exclusions.json")), {})

    def test_every_exclusion_needs_a_reason(self) -> None:
        for bad in ([{"name": "Home.default"}], [{"name": "Home.default", "reason": "  "}], [{"reason": "x"}], {"a": 1}):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                sc.load_exclusions(self.write(bad))
        self.assertEqual(sc.load_exclusions(self.write([{"name": "Home.default", "reason": "relative date"}])),
                         {"Home.default": "relative date"})

    def test_filters_and_exclusions(self) -> None:
        names = lambda rows: [r["name"] for r in rows]  # noqa: E731
        keep, skipped = sc.select_variants(self.ENTRIES, [], [], {})
        self.assertEqual((len(keep), skipped), (5, []))
        keep, _ = sc.select_variants(self.ENTRIES, ["default"], [], {})
        self.assertEqual(names(keep), ["Feed.default", "Home.default"])
        keep, _ = sc.select_variants(self.ENTRIES, [], ["Home", "Settings"], {})
        self.assertEqual(names(keep), ["Home.default", "Home.longName", "Settings.about"])
        keep, _ = sc.select_variants(self.ENTRIES, ["default"], ["Home"], {})
        self.assertEqual(names(keep), ["Home.default"])
        keep, skipped = sc.select_variants(self.ENTRIES, [], ["Home"], {"Home.longName": "async avatar", "Feed.empty": "x"})
        self.assertEqual(names(keep), ["Home.default"])
        self.assertEqual(skipped, [{"name": "Home.longName", "feature": "Home", "reason": "async avatar"}])

    def test_feature_filter_is_exact(self) -> None:
        keep, _ = sc.select_variants(self.ENTRIES, [], ["Hom"], {})
        self.assertEqual(keep, [])



class NamingTests(unittest.TestCase):

    def test_device_folder_and_zip_names(self) -> None:
        self.assertEqual(sc.device_dir_name("iPhone 17", "iOS 27.0"), D17)
        self.assertEqual(sc.device_dir_name("iPhone 17 Pro Max", "iOS 26.2"), "iPhone_17_Pro_Max_iOS_26.2")
        self.assertEqual(sc.zip_name("cli-1"), "screencatalog_cli-1.zip")

    def test_paths(self) -> None:
        self.assertEqual(sc.shot_relpath("dark", "Feed.default"), "dark/Feed.default.png")

    def test_variant_record_shape(self) -> None:
        record = sc.variant_record("Feed.empty", "light", sc.RECORDED, settle_ms=120, settled=True, duration_ms=6000,
                                   udid="U")
        self.assertEqual(record, {
            "name": "Feed.empty", "feature": "Feed", "appearance": "light", "status": sc.RECORDED,
            "file": "light/Feed.empty.png", "settleMs": 120, "settled": True, "durationMs": 6000, "error": None,
            "udid": "U", "shownMs": None, "shownAt": None,
        })
        json.dumps(record)

    def test_merge_rows(self) -> None:
        old = [sc.variant_record(n, a, sc.RECORDED) for n in ("A.x", "B.y") for a in ("light", "dark")]
        merged = sc.merge_manifest_variants(old, [sc.variant_record("A.x", "dark", sc.ERROR, error="boom")])
        self.assertEqual([(v["appearance"], v["name"], v["status"]) for v in merged],
                         [("dark", "A.x", sc.ERROR), ("dark", "B.y", sc.RECORDED),
                          ("light", "A.x", sc.RECORDED), ("light", "B.y", sc.RECORDED)])

    def test_filter_label_and_history_cap(self) -> None:
        self.assertEqual(sc.filter_label([], []), "all")
        self.assertEqual(sc.filter_label(["default"], ["Home", "Feed"]), "default · feature Home, Feed")
        run = {"history": [{"n": i} for i in range(sc.HISTORY_KEPT)]}
        sc.append_history(run, {"n": "new"})
        self.assertEqual((len(run["history"]), run["history"][0], run["history"][-1]),
                         (sc.HISTORY_KEPT, {"n": 1}, {"n": "new"}))

    def test_run_ids(self) -> None:
        for good in ("cli-20261007-121500", "ui-20261007-121500-2", "baseline-iPhone_17_iOS_27.0-20261008", "a"):
            self.assertTrue(sc.valid_run_id(good), good)
        for bad in ("", None, "../x", "a/b", ".hidden", "a..b", "a b", "a__b", "x" * 129):
            self.assertFalse(sc.valid_run_id(bad), bad)
        with self.assertRaises(ValueError):
            sc.run_root(Path("/c"), "../baseline")
        with tempfile.TemporaryDirectory() as tmp:
            first = sc.new_run_id(Path(tmp), "ui")
            self.assertRegex(first, r"^ui-\d{8}-\d{6}$")
            (Path(tmp) / sc.RUNS_DIR / first).mkdir(parents=True)
            self.assertEqual(sc.new_run_id(Path(tmp), "ui"), first + "-2")

def solid_png(value: int, block: bool = False, width: int = 20) -> bytes:
    data = bytearray([value]) * (width * 20 * 3)
    if block:
        for i in range(25):  # 25 of 400 pixels
            data[i * 3:i * 3 + 3] = b"\xff\x00\x00"
    return sc.encode_png(sc.Image(width, 20, 3, bytes(data)))


class Lines(sc.Output):
    def __init__(self):
        self.lines, self.errors, self.final = [], [], None

    def line(self, text):
        self.lines.append(text)

    def error(self, text):
        self.errors.append(text)

    def rows(self, mode, rows):
        self.final = (mode, rows)


QUIET = sc.Quiet()


class CatalogCase(unittest.TestCase):
    """A temporary catalog and a way to write run folders by hand."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        self.clock = 0

    def make_run(self, run_id: str, screens: dict, *, kind=T17, os_build="24A434", source="cli", only=(),
                 feature=(), appearance="both", finished=True, stopped=False, extra_rows=(), **fields) -> Path:
        """`screens`: {(name, appearance): png bytes}. Runs made later are newer."""
        self.clock += 1
        path = self.catalog / sc.RUNS_DIR / run_id
        rows = []
        for (name, look), blob in screens.items():
            (path / sc.shot_relpath(look, name)).parent.mkdir(parents=True, exist_ok=True)
            (path / sc.shot_relpath(look, name)).write_bytes(blob)
            rows.append(sc.variant_record(name, look, sc.RECORDED, udid="U1"))
        rows += list(extra_rows)
        sc.write_rows(path, run_id, rows)
        model, os_name = kind
        data = {"id": run_id, "source": source, "started": f"2026-10-08T10:00:{self.clock:02d}+00:00",
                "startedEpoch": 1_000 + self.clock, "active": False,
                "deviceType": {"model": model, "os": os_name, "osBuild": os_build,
                               "deviceDir": sc.device_dir_name(model, os_name)},
                "simulators": [{"udid": "U1", "name": "sim", "port": 8800}],
                "app": {"sha256": f"app{run_id}"}, "git": {"sha": f"git{run_id}", "dirty": False},
                "filter": {"only": list(only), "feature": list(feature), "label": sc.filter_label(list(only), list(feature))},
                "appearance": appearance, "counts": sc.screen_counts(rows), "stopped": stopped,
                "revision": 1, "history": [], **fields}
        if finished:
            data["finished"] = f"2026-10-08T10:30:{self.clock:02d}+00:00"
        sc.write_run(path, **data)
        return path

    def full(self, value: int = 9, names=("A.same", "B.changed")) -> dict:
        return {(n, a): solid_png(value) for n in names for a in sc.APPEARANCES}

    def pin(self, run_id: str) -> None:
        pins = sc.read_baselines(self.catalog)
        pins[sc.run_device_dir(sc.read_run(self.catalog / sc.RUNS_DIR / run_id))] = run_id
        sc.write_baselines(self.catalog, pins)

    def statuses(self, result: dict) -> dict:
        return {(r["name"], r["appearance"]): r["status"] for r in result["rows"]}


class CompareTests(CatalogCase):

    def setUp(self) -> None:
        super().setUp()
        self.make_run("base", self.full())
        self.pin("base")
        screens = self.full()
        screens[("B.changed", "light")] = solid_png(9, block=True)
        self.make_run("run", screens)

    def test_a_truncated_png_is_an_error_row_not_a_crash(self) -> None:
        whole, cut = self.root / "whole.png", self.root / "cut.png"
        whole.write_bytes(solid_png(9))
        cut.write_bytes(solid_png(9)[:40])  # ends inside a chunk header: the chunk walk raises struct.error
        status, reason, percent, wrote = sc.judge_pair(str(whole), str(cut), 8, 0.01, None)
        self.assertEqual((status, percent, wrote), (sc.ERROR, None, False))
        self.assertTrue(reason.startswith("cannot decode:"), reason)

    def test_runs_of_two_capture_modes_are_refused(self) -> None:
        self.make_run("appshot", self.full(), captureMode="app")
        with self.assertRaisesRegex(sc.CatalogError, "captureMode app.*captureMode simctl"):
            sc.compare_runs(self.catalog, "appshot", "base", QUIET)
        with self.assertRaisesRegex(sc.CatalogError, "captureMode"):
            sc.compare_runs(self.catalog, "appshot", output=QUIET)  # against the pin
        self.assertNotIn("appshot", sc.comparable(self.catalog, "run"))
        self.make_run("appshot2", self.full(), captureMode="app")
        self.assertEqual(sc.comparable(self.catalog, "appshot2"), ["appshot"])
        self.assertEqual(sc.compare_runs(self.catalog, "appshot2", "appshot", QUIET)["counts"],
                         {"light": {sc.PASS: 2}, "dark": {sc.PASS: 2}})
        # A run from before the flag was simctl.
        self.make_run("explicit", self.full(), captureMode="simctl")
        sc.compare_runs(self.catalog, "explicit", "base", QUIET)

    def test_compare_against_the_pin_is_keyed_by_pair_and_revision_and_reused(self) -> None:
        out = Lines()
        result = sc.compare_runs(self.catalog, "run", output=out)
        self.assertEqual((result["key"], result["against"], result["againstRevision"], result["reused"]),
                         ("run__base__r1", "base", 1, False))
        self.assertEqual(self.statuses(result)[("B.changed", "light")], sc.FAIL)
        self.assertEqual(sum(1 for s in self.statuses(result).values() if s == sc.PASS), 3)
        folder = self.catalog / sc.COMPARES_DIR / "run__base__r1"
        stored = json.loads((folder / sc.COMPARE_FILE).read_text())
        self.assertEqual(stored["counts"], {"light": {sc.PASS: 1, sc.FAIL: 1}, "dark": {sc.PASS: 2}})
        fail = next(r for r in stored["rows"] if r["status"] == sc.FAIL)
        self.assertEqual((fail["current"], fail["baseline"], fail["diff"]),
                         ("runs/run/light/B.changed.png", "runs/base/light/B.changed.png",
                          "compares/run__base__r1/diff/light/B.changed.png"))
        self.assertTrue((self.catalog / fail["diff"]).is_file())
        self.assertFalse((folder / "report.html").exists())  # opt-in
        self.assertEqual(out.final[0], "compare")
        self.assertEqual(sc.verdict(result["rows"]), 1)
        # Nothing changed: the stored comparison is reused, not rewritten.
        mtime = (folder / sc.COMPARE_FILE).stat().st_mtime_ns
        again = sc.compare_runs(self.catalog, "run", output=QUIET)
        self.assertTrue(again["reused"])
        self.assertEqual((folder / sc.COMPARE_FILE).stat().st_mtime_ns, mtime)
        self.assertEqual(sc.current_compare(self.catalog, "run")["key"], "run__base__r1")
        # Other settings or --force judge again in place.
        self.assertFalse(sc.compare_runs(self.catalog, "run", output=QUIET, max_diff_percent=50.0)["reused"])
        self.assertFalse(sc.compare_runs(self.catalog, "run", output=QUIET, max_diff_percent=50.0, force=True)["reused"])
        self.assertTrue(sc.compare_runs(self.catalog, "run", output=QUIET, max_diff_percent=50.0, report=True)["reused"])
        self.assertTrue((folder / "report.html").is_file())

    def test_an_accept_makes_a_new_key_and_the_old_one_goes(self) -> None:
        sc.compare_runs(self.catalog, "run", output=QUIET)
        event = sc.accept_screen(self.catalog, "run", "B.changed", "light", QUIET, skip_review=True, check_main=False)
        self.assertEqual((event["kind"], event["revision"], event["baseline"]), ("accept", 2, "base"))
        self.assertIsNone(sc.current_compare(self.catalog, "run"))  # stale until compared again
        result = sc.compare_runs(self.catalog, "run", output=QUIET)
        self.assertEqual((result["key"], result["reused"]), ("run__base__r2", False))
        self.assertEqual(set(self.statuses(result).values()), {sc.PASS})
        self.assertEqual(sorted(p.name for p in (self.catalog / sc.COMPARES_DIR).iterdir()), ["run__base__r2"])

    def test_a_change_to_the_compared_run_recomputes_under_the_same_key(self) -> None:
        sc.compare_runs(self.catalog, "base", "run", output=QUIET)  # base judged against run: key run's revision
        self.make_run("part", {("B.changed", "light"): solid_png(9, block=True)}, only=["B."])
        # base changes; run (the key's revision) does not
        sc.update_baseline(self.catalog, "part", QUIET, skip_review=True, check_main=False)
        result = sc.compare_runs(self.catalog, "base", "run", output=QUIET)
        self.assertEqual((result["key"], result["reused"]), ("base__run__r1", False))
        self.assertEqual(set(self.statuses(result).values()), {sc.PASS})

    def test_types_must_match_and_a_build_difference_is_a_note(self) -> None:
        self.make_run("pro", self.full(), kind=("iPhone 18 Pro", "iOS 27.0"))
        with self.assertRaisesRegex(sc.CatalogError, r"cannot compare pro \(iPhone 18 Pro, iOS 27.0\) with base "
                                                     r"\(iPhone 17, iOS 27.0\)"):
            sc.compare_runs(self.catalog, "pro", "base", output=QUIET)
        with self.assertRaisesRegex(sc.CatalogError, "no baseline is pinned for iPhone 18 Pro, iOS 27.0"):
            sc.compare_runs(self.catalog, "pro", output=QUIET)
        self.make_run("old-ios", self.full(), kind=("iPhone 17", "iOS 26.2"))
        with self.assertRaises(sc.CatalogError):
            sc.compare_runs(self.catalog, "old-ios", "base", output=QUIET)
        self.make_run("other-build", self.full(), os_build="24A999")
        result = sc.compare_runs(self.catalog, "other-build", output=QUIET)
        self.assertEqual(result["notes"], ["iOS build differs: other-build 24A999, base 24A434"])
        self.assertEqual(set(self.statuses(result).values()), {sc.PASS})

    def test_refusals(self) -> None:
        with self.assertRaisesRegex(sc.CatalogError, "is the pinned baseline"):
            sc.compare_runs(self.catalog, "base", output=QUIET)
        with self.assertRaisesRegex(sc.CatalogError, "with itself"):
            sc.compare_runs(self.catalog, "run", "run", output=QUIET)
        with self.assertRaisesRegex(sc.CatalogError, "no run nope"):
            sc.compare_runs(self.catalog, "nope", output=QUIET)
        sc.write_run(self.catalog / sc.RUNS_DIR / "run", active=True, pid=os.getpid())
        with self.assertRaisesRegex(sc.CatalogError, "still capturing"):
            sc.compare_runs(self.catalog, "run", output=QUIET)

    def test_a_partial_run_judges_only_its_screens_and_names_what_is_missing(self) -> None:
        self.make_run("part", {("A.same", "dark"): solid_png(9), ("C.new", "dark"): solid_png(1)},
                      only=["A.", "C."], appearance="dark", stopped=True,
                      extra_rows=[sc.variant_record("D.err", "dark", sc.ERROR, error="bridge never answered"),
                                  sc.variant_record("E.skip", "dark", sc.SKIPPED, error="animates")])
        result = sc.compare_runs(self.catalog, "part", output=QUIET, exclusions={"A.same": "now excluded"})
        rows = {r["name"]: (r["status"], r["error"]) for r in result["rows"]}
        self.assertEqual(rows, {"A.same": (sc.SKIPPED, "now excluded"),
                                "C.new": (sc.MISSING, "no screenshot of C.new in base"),
                                "D.err": (sc.ERROR, "bridge never answered"),
                                "E.skip": (sc.SKIPPED, "animates")})
        self.assertTrue(result["runStopped"] and result["runPartial"])
        self.assertEqual(sc.verdict([r for r in result["rows"] if r["status"] != sc.ERROR], allow_missing=True), 0)

    def test_cli_exit_codes(self) -> None:
        argv = ["compare", "run", "--catalog-dir", str(self.catalog), "--exclude-file", str(self.root / "none.json")]
        self.assertEqual(sc.run_catalog(sc.parse_args(argv), QUIET), 1)
        self.assertEqual(sc.run_catalog(sc.parse_args(argv + ["--max-diff-percent", "50"]), QUIET), 0)
        out = Lines()
        self.assertEqual(sc.run_catalog(sc.parse_args(["compare", "nope", "--catalog-dir", str(self.catalog)]), out), 2)
        self.assertIn("no run nope", out.errors[0])


class BaselineOperationTests(CatalogCase):

    def test_set_baseline_rules(self) -> None:
        self.make_run("full", self.full())
        self.make_run("part", {("A.same", "light"): solid_png(9)}, only=["A."])
        self.make_run("light-only", {("A.same", "light"): solid_png(9)}, appearance="light")
        self.make_run("stopped", self.full(), stopped=True)
        self.make_run("aborted", self.full(), finished=False, aborted=True)
        for run_id, message in (("part", "is partial"), ("light-only", "is partial"), ("stopped", "stopped before"),
                                ("aborted", "stopped before")):
            with self.subTest(run=run_id), self.assertRaisesRegex(sc.CatalogError, message):
                sc.set_baseline(self.catalog, run_id, output=QUIET, check_main=False)
        self.assertEqual(sc.read_baselines(self.catalog), {})
        self.assertEqual(sc.set_baseline(self.catalog, "full", output=QUIET, check_main=False),
                         {"deviceDir": D17, "pinned": "full", "previous": None, "review": None, "main": None})
        with self.assertRaisesRegex(sc.CatalogError, "no current comparison of part with the baseline"):
            sc.set_baseline(self.catalog, "part", allow_partial=True, output=QUIET, check_main=False)
        self.assertEqual(sc.set_baseline(self.catalog, "part", allow_partial=True, skip_review=True, output=QUIET,
                                         check_main=False)["previous"], "full")
        self.assertEqual(sc.read_baselines(self.catalog), {D17: "part"})
        self.assertEqual(sc.set_baseline(self.catalog, "stopped", allow_stopped=True, skip_review=True, output=QUIET, check_main=False)["previous"], "part")
        history = sc.read_run(self.catalog / sc.RUNS_DIR / "part")["history"]
        self.assertEqual([(e["kind"], e["replaced"], e["revision"]) for e in history], [("pin", "full", 1)])
        sc.write_run(self.catalog / sc.RUNS_DIR / "full", active=True, pid=os.getpid())
        with self.assertRaisesRegex(sc.CatalogError, "still capturing"):
            sc.set_baseline(self.catalog, "full", output=QUIET, check_main=False)

    def test_update_baseline_copies_a_partial_runs_screens_and_bumps_the_revision(self) -> None:
        self.make_run("base", self.full())
        self.pin("base")
        self.make_run("full", self.full(3))
        with self.assertRaisesRegex(sc.CatalogError, "is a full run: pin it with `set-baseline full`"):
            sc.update_baseline(self.catalog, "full", QUIET, check_main=False)
        self.make_run("part", {("B.changed", "light"): solid_png(5), ("C.new", "light"): solid_png(6)}, only=["B.", "C."],
                      extra_rows=[sc.variant_record("A.same", "light", sc.ERROR, error="boom")])
        event = sc.update_baseline(self.catalog, "part", QUIET, skip_review=True, check_main=False)
        self.assertEqual((event["kind"], event["revision"], event["from"], event["app"]), ("update", 2, "part", "apppart"))
        self.assertEqual(event["variants"], [{"name": "B.changed", "appearance": "light"},
                                             {"name": "C.new", "appearance": "light"}])
        base = self.catalog / sc.RUNS_DIR / "base"
        self.assertEqual((base / "light" / "B.changed.png").read_bytes(), solid_png(5))
        self.assertEqual((base / "light" / "A.same.png").read_bytes(), solid_png(9))  # the ERROR row is left alone
        rows = {(r["name"], r["appearance"]): r for r in sc.read_rows(base)}
        self.assertEqual((rows[("C.new", "light")]["from"], rows[("C.new", "light")]["status"]), ("part", sc.RECORDED))
        self.assertEqual(rows[("A.same", "light")]["status"], sc.RECORDED)
        data = sc.read_run(base)
        self.assertEqual((data["revision"], data["counts"], data["history"][-1]["kind"]),
                         (2, {"dark": 2, "light": 3}, "update"))
        self.assertRegex(data["revisionAt"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(sc.read_run(self.catalog / sc.RUNS_DIR / "part")["revision"], 1)  # the source is unchanged

    def test_update_and_accept_need_a_pin_of_the_type(self) -> None:
        self.make_run("part", {("A.same", "light"): solid_png(9)}, only=["A."])
        with self.assertRaisesRegex(sc.CatalogError, "no baseline is pinned"):
            sc.update_baseline(self.catalog, "part", QUIET, check_main=False)
        self.make_run("pro", self.full(), kind=("iPhone 18 Pro", "iOS 27.0"))
        self.pin("pro")
        with self.assertRaisesRegex(sc.CatalogError, "no baseline is pinned for iPhone 17"):
            sc.accept_screen(self.catalog, "part", "A.same", "light", QUIET, check_main=False)

    def test_accept(self) -> None:
        self.make_run("base", self.full())
        self.pin("base")
        self.make_run("run", {("A.same", "dark"): solid_png(1)},
                      extra_rows=[sc.variant_record("B.changed", "dark", sc.ERROR, error="x")])
        with self.assertRaisesRegex(sc.CatalogError, "no screenshot of B.changed"):
            sc.accept_screen(self.catalog, "run", "B.changed", "dark", QUIET, check_main=False)
        with self.assertRaisesRegex(sc.CatalogError, "is the pinned baseline itself"):
            sc.accept_screen(self.catalog, "base", "A.same", "dark", QUIET, check_main=False)
        event = sc.accept_screen(self.catalog, "run", "A.same", "dark", QUIET, skip_review=True, check_main=False)
        self.assertEqual((event["kind"], event["revision"], event["variants"]),
                         ("accept", 2, [{"name": "A.same", "appearance": "dark"}]))
        self.assertEqual((self.catalog / sc.RUNS_DIR / "base" / "dark" / "A.same.png").read_bytes(), solid_png(1))
        self.assertEqual(sc.accept_screen(self.catalog, "run", "A.same", "dark", QUIET, skip_review=True, check_main=False)["revision"], 3)


class MainGateTests(CatalogCase):
    """Only a run recorded from a clean checkout of a commit on main can become, or be copied into, the
    baseline. A throwaway clone with a bare origin: main has c1 → c2 (pushed), a branch has c3 from c1."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.c1 = self.commit("c1")
        self.c2 = self.commit("c2")
        origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "-b", "feature", self.c1)
        self.c3 = self.commit("c3")
        self.git("checkout", "-q", "main")
        self.lines: list[str] = []
        self.out = type("Out", (), {"line": lambda _self, text: self.lines.append(text)})()

    def git(self, *args: str) -> str:
        return sc.git_output(self.repo, *args) or ""

    def commit(self, name: str) -> str:
        (self.repo / name).write_text(name)
        self.git("add", name)
        self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "HEAD")

    def test_git_state_ignores_a_build_rewriting_a_generated_file(self) -> None:
        # A generated file a build phase rewrites is not an input of the app binary, so it alone does not make
        # the tree dirty; it is named as `ignored`. Any other change, tracked or untracked, still does.
        schema = self.repo / sc.BUILD_CHURN_PATHS[0]
        schema.parent.mkdir(parents=True)
        schema.write_text('{"version": 1}')
        self.git("add", sc.BUILD_CHURN_PATHS[0])
        self.git("commit", "-q", "-m", "generated")
        sha = self.git("rev-parse", "HEAD")
        self.assertEqual(sc.git_state(self.repo), {"sha": sha, "dirty": False})
        schema.write_text('{"version": 2}')
        self.assertEqual(sc.git_state(self.repo), {"sha": sha, "dirty": False, "ignored": [sc.BUILD_CHURN_PATHS[0]]})
        (self.repo / "c1").write_text("edited")
        self.assertEqual(sc.git_state(self.repo), {"sha": sha, "dirty": True, "ignored": [sc.BUILD_CHURN_PATHS[0]]})
        self.git("checkout", "-q", "--", ".")
        (self.repo / "new").write_text("untracked")
        self.assertEqual(sc.git_state(self.repo), {"sha": sha, "dirty": True})

    def test_only_a_clean_commit_on_main_can_be_pinned(self) -> None:
        self.make_run("at", self.full(), git={"sha": self.c2, "dirty": False})
        self.make_run("behind", self.full(), git={"sha": self.c1, "dirty": False})
        self.make_run("off", self.full(), git={"sha": self.c3, "dirty": False})
        self.make_run("unknown", self.full(), git={"sha": "f" * 40, "dirty": False})
        self.make_run("dirty", self.full(), git={"sha": self.c2, "dirty": True})
        self.make_run("nogit", self.full(), git={"sha": None, "dirty": False})
        pinned = sc.set_baseline(self.catalog, "at", output=self.out, repo=self.repo)
        self.assertEqual((pinned["pinned"], pinned["main"]["state"], pinned["main"]["ref"]), ("at", "at", "origin/main"))
        self.assertEqual(sc.load_run(self.catalog, "at")["history"][-1]["main"], pinned["main"])
        for run_id, why in (("off", f"recorded off origin/main: 1 commit of {self.c3[:9]} is not on it"),
                            ("unknown", f"commit {'f' * 9} is not in this clone"),
                            ("dirty", f"recorded with uncommitted changes on {self.c2[:9]}"),
                            ("nogit", "no commit recorded")):
            with self.assertRaisesRegex(sc.CatalogError, f"{run_id} cannot be the baseline: it.*{why}.*merge first"):
                sc.set_baseline(self.catalog, run_id, skip_review=True, output=self.out, repo=self.repo)
            self.assertEqual(sc.read_baselines(self.catalog), {D17: "at"})
        behind = sc.set_baseline(self.catalog, "behind", skip_review=True, output=self.out, repo=self.repo)
        self.assertEqual((behind["previous"], behind["main"]["state"], behind["main"]["behind"]), ("at", "behind", 1))
        self.assertFalse([line for line in self.lines if line.startswith("could not fetch")], self.lines)

    def test_update_and_accept_are_gated_too(self) -> None:
        self.make_run("at", self.full(), git={"sha": self.c2, "dirty": False})
        sc.set_baseline(self.catalog, "at", output=self.out, repo=self.repo)
        shot = {("B.changed", "light"): solid_png(3)}
        self.make_run("branch", shot, only=["B."], appearance="light", git={"sha": self.c3, "dirty": False})
        self.make_run("main", shot, only=["B."], appearance="light", git={"sha": self.c1, "dirty": False})
        for call in (lambda r: sc.update_baseline(self.catalog, r, self.out, skip_review=True, repo=self.repo),
                     lambda r: sc.accept_screen(self.catalog, r, "B.changed", "light", self.out, skip_review=True, repo=self.repo)):
            with self.assertRaisesRegex(sc.CatalogError, "branch cannot be the baseline: it was recorded off origin/main"):
                call("branch")
            self.assertEqual(sc.load_run(self.catalog, "at")["revision"], 1)
        event = sc.update_baseline(self.catalog, "main", self.out, skip_review=True, repo=self.repo)
        self.assertEqual((event["revision"], event["main"]["state"], event["main"]["behind"]), (2, "behind", 1))
        self.assertEqual(sc.load_run(self.catalog, "at")["history"][-1]["main"], event["main"])

    def test_the_gate_fetches_main_first_so_a_stale_clone_does_not_refuse_a_fresh_commit(self) -> None:
        c4 = self.commit("c4")
        self.git("push", "-q", "origin", "main")
        self.git("update-ref", "refs/remotes/origin/main", self.c2)  # as if this clone had not fetched since c2
        self.make_run("fresh", self.full(), git={"sha": c4, "dirty": False})
        self.assertEqual(sc.standing(self.repo, c4, sc.main_head(self.repo))["state"], "off")
        pinned = sc.set_baseline(self.catalog, "fresh", output=self.out, repo=self.repo)
        self.assertEqual(pinned["main"], {"ref": "origin/main", "state": "at", "behind": 0, "ahead": 0})
        self.assertEqual(self.git("rev-parse", "origin/main"), c4)

    def test_a_failed_git_count_is_not_at_main(self) -> None:
        self.make_run("at", self.full(), git={"sha": self.c2, "dirty": False})
        real = sc.git_output

        def flaky(repo, *args, **kwargs):
            return None if args[:1] == ("rev-list",) else real(repo, *args, **kwargs)

        sc.git_output = flaky
        self.addCleanup(setattr, sc, "git_output", real)
        self.assertEqual(sc.standing(self.repo, self.c2, sc.main_head(self.repo)),
                         {"ref": "origin/main", "state": "unknown", "behind": None, "ahead": None, "gitFailed": True})
        with self.assertRaisesRegex(sc.CatalogError, f"git could not place its commit {self.c2[:9]} against origin/main"):
            sc.set_baseline(self.catalog, "at", output=self.out, repo=self.repo)
        self.assertEqual(sc.read_baselines(self.catalog), {})

    def test_without_a_reachable_origin_the_gate_warns_and_judges_main_as_last_seen(self) -> None:
        self.git("remote", "set-url", "origin", str(self.root / "gone.git"))
        self.make_run("at", self.full(), git={"sha": self.c2, "dirty": False})
        pinned = sc.set_baseline(self.catalog, "at", output=self.out, repo=self.repo)
        self.assertEqual(pinned["main"]["state"], "at")
        self.assertIn("could not fetch origin main: judging against main as this clone last saw it", self.lines)
        self.make_run("off", self.full(), git={"sha": self.c3, "dirty": False})
        with self.assertRaisesRegex(sc.CatalogError, "recorded off origin/main"):
            sc.set_baseline(self.catalog, "off", skip_review=True, output=self.out, repo=self.repo)
        self.assertEqual(sc.main_blocker({"git": {"sha": "abc"}}, None, None),
                         "this clone has neither origin/main nor main to check its commit against")

class ReviewTests(CatalogCase):
    """A change (a FAIL or a new screen) is reviewed by the pair of images it is: the author marks it
    expected, someone else confirms or rejects it, and nothing replaces the baseline until every change
    is confirmed. Events are files, one each, so a push and a pull only ever add."""

    ALEX = {"name": "Alex", "email": "alex@example.com"}
    SAM = {"name": "Sam", "email": "sam@example.com"}

    def setUp(self) -> None:
        super().setUp()
        self.make_run("base", self.full())
        self.pin("base")
        screens = self.full()
        screens[("B.changed", "light")] = solid_png(9, block=True)
        screens[("C.new", "light")] = solid_png(5)
        self.make_run("run", screens)
        self.result = sc.compare_runs(self.catalog, "run", output=QUIET)
        self.rows = {(r["name"], r["appearance"]): r for r in self.result["rows"]}

    def pair(self, name: str, appearance: str = "light") -> dict:
        return sc.review_pair(self.catalog, self.rows[(name, appearance)])

    def mark(self, kind: str, by: dict, name: str = "B.changed", note: str | None = None) -> dict:
        return sc.add_review_event(self.catalog, self.pair(name), kind, by, note, run_id="run", against_id="base",
                                   against_revision=1)

    def test_the_gate_wants_the_comparison_at_the_tolerance_in_force(self) -> None:
        sc.compare_runs(self.catalog, "run", output=QUIET, max_diff_percent=50.0)  # loose: nothing to review
        with self.assertRaisesRegex(sc.CatalogError, r"judged at channel threshold 8, max 50.0% differing pixels, not the "
                                    r"tolerance in force \(channel threshold 8, max 0.01%\): compare it again"):
            sc.set_baseline(self.catalog, "run", output=QUIET, check_main=False)
        with self.assertRaisesRegex(sc.CatalogError, "judged at channel threshold 8, max 50.0%"):
            sc.accept_screen(self.catalog, "run", "B.changed", "light", QUIET, check_main=False)
        self.assertEqual(sc.read_baselines(self.catalog), {D17: "base"})
        sc.compare_runs(self.catalog, "run", output=QUIET)
        with self.assertRaisesRegex(sc.CatalogError, "2 changes not confirmed by a reviewer"):
            sc.set_baseline(self.catalog, "run", output=QUIET, check_main=False)
        sc.compare_runs(self.catalog, "run", output=QUIET, max_diff_percent=50.0)
        # A gate run at the loose tolerance accepts the loose comparison: only the new screen is left to review.
        with self.assertRaisesRegex(sc.CatalogError, r"1 change not confirmed by a reviewer .*C.new \(light\): MISSING"):
            sc.set_baseline(self.catalog, "run", output=QUIET, check_main=False, max_diff_percent=50.0)

    def test_a_pair_is_the_two_images_and_only_changes_have_one(self) -> None:
        pair = self.pair("B.changed")
        self.assertEqual((pair["name"], pair["appearance"]), ("B.changed", "light"))
        self.assertEqual(pair["current"], hashlib.sha256(solid_png(9, block=True)).hexdigest())
        self.assertEqual(pair["baseline"], hashlib.sha256(solid_png(9)).hexdigest())
        self.assertEqual(self.pair("C.new")["baseline"], "none")  # MISSING: the baseline has no screen of it
        self.assertIsNone(self.pair("A.same"))  # PASS
        self.assertEqual(self.result["reviews"].keys(), {("B.changed", "light"), ("C.new", "light")})
        self.assertEqual(sc.review_summary(self.result["reviews"]),
                         {"changes": 2, "open": 2, "expected": 0, "confirmed": 0, "rejected": 0})

    def test_the_lifecycle_and_what_is_ignored(self) -> None:
        with self.assertRaisesRegex(sc.CatalogError, "nothing is marked expected"):
            self.mark("confirm", self.SAM)
        state = self.mark("expect", self.ALEX, note="new header")
        self.assertEqual((state["state"], state["author"], state["note"]), ("expected", self.ALEX, "new header"))
        with self.assertRaisesRegex(sc.CatalogError, "the author cannot confirm their own change"):
            self.mark("confirm", self.ALEX)
        with self.assertRaisesRegex(sc.CatalogError, "only the author withdraws"):
            self.mark("withdraw", self.SAM)
        with self.assertRaisesRegex(sc.CatalogError, "a rejection needs a reason"):
            self.mark("reject", self.SAM)
        state = self.mark("reject", self.SAM, note="the padding is off")
        self.assertEqual((state["state"], state["reviewer"], state["reason"]), ("rejected", self.SAM, "the padding is off"))
        with self.assertRaisesRegex(sc.CatalogError, "marked expected again first"):
            self.mark("confirm", self.SAM)
        self.mark("expect", self.ALEX, note="padding fixed in the mock")
        state = self.mark("confirm", self.SAM)
        self.assertEqual((state["state"], state["reviewer"], state["note"]), ("confirmed", self.SAM, "padding fixed in the mock"))
        with self.assertRaisesRegex(sc.CatalogError, "already confirmed"):
            self.mark("expect", self.ALEX)
        self.assertEqual([e["kind"] for e in state["events"]], ["expect", "reject", "expect", "confirm"])
        folder = self.catalog / sc.REVIEWS_DIR / sc.pair_dir(self.pair("B.changed"))
        self.assertEqual(len(list(folder.glob("*.json"))), 4)  # one file per event; refused ones wrote nothing
        # withdraw reopens
        self.mark("expect", self.ALEX, name="C.new")
        self.assertEqual(self.mark("withdraw", self.ALEX, name="C.new")["state"], "open")
        # identity: by email when both have one, else by name
        self.assertTrue(sc.same_person({"name": "S", "email": "A@x.com"}, {"name": "T", "email": "a@x.com"}))
        self.assertTrue(sc.same_person({"name": "Alex", "email": None}, {"name": "alex", "email": None}))
        self.assertFalse(sc.same_person({"name": None, "email": None}, {"name": None, "email": None}))
        with self.assertRaisesRegex(sc.CatalogError, "no identity"):
            self.mark("expect", {"name": None, "email": None})

    def test_the_verdict_follows_the_images_not_the_run(self) -> None:
        self.mark("expect", self.ALEX, note="intended")
        self.mark("confirm", self.SAM)
        # Re-recorded with the same bytes: the same pair, so the same verdict.
        screens = self.full()
        screens[("B.changed", "light")] = solid_png(9, block=True)
        self.make_run("again", screens)
        again = sc.compare_runs(self.catalog, "again", output=QUIET)
        self.assertEqual(again["reviews"][("B.changed", "light")]["state"], "confirmed")
        # Different bytes (the baseline's screen changed, or the run's): a new pair, open again.
        self.make_run("base2", {**self.full(), ("B.changed", "light"): solid_png(7)})
        moved = sc.compare_runs(self.catalog, "again", "base2", output=QUIET)
        self.assertEqual(moved["reviews"][("B.changed", "light")]["state"], "open")

    def test_the_baseline_waits_for_every_change_to_be_confirmed(self) -> None:
        self.make_run("full", {**self.full(), ("B.changed", "light"): solid_png(9, block=True), ("C.new", "light"): solid_png(5)})
        with self.assertRaisesRegex(sc.CatalogError, "no current comparison of full with the baseline"):
            sc.set_baseline(self.catalog, "full", output=QUIET, check_main=False)
        sc.compare_runs(self.catalog, "full", output=QUIET)
        with self.assertRaisesRegex(sc.CatalogError, r"2 changes not confirmed by a reviewer \(2 open, 0 expected, 0 rejected, "
                                                     r"0 confirmed\): B.changed \(light\): FAIL, open; C.new \(light\): MISSING, open"):
            sc.set_baseline(self.catalog, "full", output=QUIET, check_main=False)
        self.mark("expect", self.ALEX)  # the same pair as full's B.changed
        self.mark("expect", self.ALEX, name="C.new")
        with self.assertRaisesRegex(sc.CatalogError, "2 changes not confirmed by a reviewer \\(0 open, 2 expected"):
            sc.set_baseline(self.catalog, "full", output=QUIET, check_main=False)
        with self.assertRaisesRegex(sc.CatalogError, "B.changed \\(light\\): FAIL, expected by Alex"):
            sc.accept_screen(self.catalog, "full", "B.changed", "light", QUIET, check_main=False)
        self.mark("confirm", self.SAM)
        self.mark("confirm", self.SAM, name="C.new")
        result = sc.set_baseline(self.catalog, "full", output=QUIET, check_main=False)
        self.assertEqual((result["previous"], result["review"]),
                         ("base", {"changes": 2, "open": 0, "expected": 0, "confirmed": 2, "rejected": 0}))
        self.assertEqual(sc.read_run(self.catalog / sc.RUNS_DIR / "full")["history"][-1]["review"]["confirmed"], 2)
        # An ERROR row blocks whatever the reviews say; skip_review records that it was skipped.
        self.pin("base")
        screens = self.full()
        del screens[("B.changed", "light")]
        self.make_run("broken", screens, extra_rows=[sc.variant_record("B.changed", "light", sc.ERROR, error="crashed")])
        sc.compare_runs(self.catalog, "broken", output=QUIET)
        with self.assertRaisesRegex(sc.CatalogError, r"1 change not confirmed .*: B.changed \(light\): ERROR, crashed"):
            sc.set_baseline(self.catalog, "broken", output=QUIET, check_main=False)
        # update-baseline copies only captured screens, so an ERROR row of a partial run is left out, not a blocker
        self.make_run("partly", {("A.same", "light"): solid_png(9)}, only=["A."],
                      extra_rows=[sc.variant_record("B.changed", "light", sc.ERROR, error="crashed")])
        sc.compare_runs(self.catalog, "partly", output=QUIET)
        self.assertEqual(sc.update_baseline(self.catalog, "partly", QUIET, check_main=False)["kind"], "update")
        self.assertEqual(sc.set_baseline(self.catalog, "full", skip_review=True, output=QUIET, check_main=False)["review"], None)
        self.assertTrue(sc.read_run(self.catalog / sc.RUNS_DIR / "full")["history"][-1]["skippedReview"])

    def test_compare_prints_the_review_and_allow_confirmed_passes_confirmed_changes(self) -> None:
        self.mark("expect", self.ALEX, note="intended")
        out = Lines()
        sc.compare_runs(self.catalog, "run", output=out)
        self.assertIn('  FAIL    light B.changed: 25 px differ (6.2500% > 0.01%) · expected by Alex: "intended"', out.lines)
        self.assertIn("  MISSING light C.new: no screenshot of C.new in base · open", out.lines)
        self.assertIn("review: 2 changes: 1 open, 1 expected, 0 confirmed, 0 rejected", out.lines)
        args = sc.parse_args(["compare", "run", "--catalog-dir", str(self.catalog), "--allow-confirmed"])
        self.assertEqual(sc.run_catalog(args, QUIET), 1)
        self.mark("confirm", self.SAM)
        self.mark("expect", self.ALEX, name="C.new")
        self.mark("confirm", self.SAM, name="C.new")
        self.assertEqual(sc.run_catalog(args, QUIET), 0)
        plain = sc.parse_args(["compare", "run", "--catalog-dir", str(self.catalog)])
        self.assertEqual(sc.run_catalog(plain, QUIET), 1)  # without the flag a confirmed FAIL still fails

    def test_the_review_command(self) -> None:
        def cli(*argv: str) -> tuple[int, Lines]:
            out = Lines()
            return sc.run_catalog(sc.parse_args(["review", *argv, "--catalog-dir", str(self.catalog)]), out), out
        code, out = cli("run")
        self.assertEqual(code, 1)
        self.assertEqual(out.lines, ["  light B.changed: open", "  light C.new: open",
                                     "2 changes: 2 open, 0 expected, 0 confirmed, 0 rejected"])
        code, out = cli("run", "B.changed", "light", "--expect", "--note", "new header", "--as", "Alex <alex@example.com>")
        self.assertEqual((code, out.lines[0]), (1, 'B.changed (light): expected by Alex: "new header"'))
        code, out = cli("run", "B.changed", "light", "--confirm", "--as", "Alex <alex@example.com>")
        self.assertEqual((code, out.errors), (2, ["error: confirm changes nothing: the author cannot confirm their own change"]))
        with mock.patch.dict(os.environ, {sc.REVIEWER_ENV: "Sam <sam@example.com>"}):
            code, out = cli("run", "B.changed", "light", "--confirm")
        self.assertEqual((code, out.lines[0]), (1, 'B.changed (light): confirmed by Sam: "new header"'))
        code, out = cli("run", "--json")
        data = json.loads("\n".join(out.lines))
        self.assertEqual(data["summary"], {"changes": 2, "open": 1, "expected": 0, "confirmed": 1, "rejected": 0})
        self.assertEqual([(c["name"], c["state"]) for c in data["changes"]], [("B.changed", "confirmed"), ("C.new", "open")])
        code, out = cli("run", "A.same", "light", "--expect", "--as", "Alex")
        self.assertEqual((code, out.errors), (2, ["error: A.same (light) is not a FAIL or MISSING screen of that comparison"]))
        code, out = cli("run", "B.changed")
        self.assertEqual(code, 2)
        self.assertIn("marking takes NAME, APPEARANCE and one of", out.errors[0])
        self.make_run("other", self.full())
        code, out = cli("other")
        self.assertEqual((code, out.errors), (2, ["error: no current comparison of other with base: run `compare other` first"]))

    def test_reviews_travel_with_every_push_and_pull(self) -> None:
        s3 = install_fake_aws(self, self.root)
        self.mark("expect", self.ALEX, note="intended")
        out = Lines()
        self.assertEqual(sc.run_catalog(sc.parse_args(["sync", "push", "--catalog-dir", str(self.catalog)]), out), 0, out.lines + out.errors)
        remote = s3 / FAKE_BUCKET / sc.SYNC_PREFIX / sc.REVIEWS_DIR / sc.pair_dir(self.pair("B.changed"))
        self.assertEqual(len(list(remote.glob("*.json"))), 1)
        self.assertIn(f"pushed {sc.REVIEWS_DIR}/", out.lines)
        # A teammate pulls, confirms, pushes; the author's pull folds both files.
        mate = self.root / "mate"
        mate.mkdir()
        out = Lines()
        self.assertEqual(sc.run_catalog(sc.parse_args(["sync", "pull", "--catalog-dir", str(mate)]), out), 0, out.errors)
        self.assertEqual(sc.fold_review(sc.read_review_events(mate, self.pair("B.changed")))["state"], "expected")
        sc.add_review_event(mate, self.pair("B.changed"), "confirm", self.SAM)
        out = Lines()
        self.assertEqual(sc.run_catalog(sc.parse_args(["sync", "push", "--catalog-dir", str(mate)]), out), 0, out.errors)
        self.assertEqual(sc.run_catalog(sc.parse_args(["sync", "pull", "--catalog-dir", str(self.catalog)]), Lines()), 0)
        state = sc.fold_review(sc.read_review_events(self.catalog, self.pair("B.changed")))
        self.assertEqual((state["state"], [e["kind"] for e in state["events"]]), ("confirmed", ["expect", "confirm"]))
        # a catalog with no reviews pushes nothing for them, and a pull makes the folder
        empty = self.root / "empty"
        empty.mkdir()
        out = Lines()
        self.assertEqual(sc.run_catalog(sc.parse_args(["sync", "push", "--catalog-dir", str(empty)]), out), 0, out.errors)
        self.assertNotIn(f"pushed {sc.REVIEWS_DIR}/", out.lines)


class ParallelCompareTests(CatalogCase):
    """Pairs judged in a process pool give the rows an in-process compare gives, in the run's order;
    progress counts pairs; a set stop cancels and leaves no folder behind."""

    def setUp(self) -> None:
        super().setUp()
        names = [f"S.{i:02d}" for i in range(6)]
        self.make_run("base", self.full(names=names))
        self.pin("base")
        screens = self.full(names=names)
        for i, name in enumerate(names):
            if i % 2:
                screens[(name, "light")] = solid_png(9, block=True)
            if i % 3 == 0:
                screens[(name, "dark")] = solid_png(10)  # a delta of 1: PASS, decoded
        self.make_run("run", screens)

    def rows(self, result: dict) -> list[tuple]:
        return [(r["name"], r["appearance"], r["status"], r["note"], r["diffPercent"], r["diff"]) for r in result["rows"]]

    def test_pool_and_in_process_give_the_same_rows_and_progress(self) -> None:
        serial = sc.compare_runs(self.catalog, "run", output=QUIET, workers=0, force=True)
        diffs = {r["diff"]: (self.catalog / r["diff"]).read_bytes() for r in serial["rows"] if r["diff"]}
        self.assertEqual(len(diffs), 3)
        progress = []

        class Progress(sc.Quiet):
            def progress(self, done, total):
                progress.append((done, total))

        original = sc.POOL_MIN_PAIRS
        sc.POOL_MIN_PAIRS = 1
        self.addCleanup(setattr, sc, "POOL_MIN_PAIRS", original)
        pooled = sc.compare_runs(self.catalog, "run", output=Progress(), workers=2, force=True)
        self.assertEqual(self.rows(pooled), self.rows(serial))
        self.assertEqual({r["diff"]: (self.catalog / r["diff"]).read_bytes() for r in pooled["rows"] if r["diff"]}, diffs)
        self.assertEqual(progress[0], (7, 12))  # the byte-identical pairs are done before any decode
        self.assertEqual(progress[-1], (12, 12))
        self.assertIsInstance(pooled["seconds"], float)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(2) as pool:  # a caller's pool (the web UI passes its process pool)
            self.assertEqual(self.rows(sc.compare_runs(self.catalog, "run", output=QUIET, pool=pool, force=True)),
                             self.rows(serial))

    def test_a_set_stop_cancels_and_leaves_no_folder(self) -> None:
        sc.compare_runs(self.catalog, "run", output=QUIET, workers=0)
        stored = (self.catalog / sc.COMPARES_DIR / "run__base__r1" / sc.COMPARE_FILE).read_bytes()
        for workers in (0, 2):
            with self.subTest(workers=workers):
                stop = threading.Event()

                class StopAfterOne(sc.Quiet):
                    def progress(self, done, total):
                        if done >= 7:  # before the first decode
                            stop.set()

                original = sc.POOL_MIN_PAIRS
                sc.POOL_MIN_PAIRS = 1
                self.addCleanup(setattr, sc, "POOL_MIN_PAIRS", original)
                with self.assertRaisesRegex(sc.CompareCancelled, "cancelled"):
                    sc.compare_runs(self.catalog, "run", output=StopAfterOne(), workers=workers, force=True, stop=stop)
                self.assertEqual(sorted(p.name for p in (self.catalog / sc.COMPARES_DIR).iterdir()), ["run__base__r1"])
                self.assertEqual((self.catalog / sc.COMPARES_DIR / "run__base__r1" / sc.COMPARE_FILE).read_bytes(), stored)


class DeletePruneTests(CatalogCase):

    def test_delete_run(self) -> None:
        self.make_run("base", self.full())
        self.pin("base")
        self.make_run("run", self.full())
        self.make_run("other", self.full())
        sc.compare_runs(self.catalog, "run", output=QUIET)
        sc.compare_runs(self.catalog, "other", "run", output=QUIET)
        sc.compare_runs(self.catalog, "other", output=QUIET)
        (self.catalog / sc.THUMBS_DIR / sc.RUNS_DIR / "run" / "light").mkdir(parents=True)
        (self.catalog / sc.THUMBS_DIR / sc.COMPARES_DIR / "run__base__r1").mkdir(parents=True)
        result = sc.delete_run(self.catalog, "run", QUIET)
        self.assertEqual((result["deleted"], sorted(result["compares"])), ("run", ["other__run__r1", "run__base__r1"]))
        self.assertIsNone(result["unpinned"])
        self.assertFalse((self.catalog / sc.RUNS_DIR / "run").exists())
        self.assertFalse((self.catalog / sc.THUMBS_DIR / sc.RUNS_DIR / "run").exists())
        self.assertFalse((self.catalog / sc.THUMBS_DIR / sc.COMPARES_DIR / "run__base__r1").exists())
        self.assertEqual([p.name for p in (self.catalog / sc.COMPARES_DIR).iterdir()], ["other__base__r1"])
        sc.write_run(self.catalog / sc.RUNS_DIR / "other", active=True, pid=os.getpid())
        with self.assertRaisesRegex(sc.CatalogError, "still capturing"):
            sc.delete_run(self.catalog, "other", QUIET)

    def test_deleting_the_pinned_run_removes_the_pin_and_says_a_baseline_must_be_set(self) -> None:
        self.make_run("base", self.full())
        self.pin("base")
        self.make_run("run", self.full())
        sc.compare_runs(self.catalog, "run", output=QUIET)
        out = Lines()
        result = sc.delete_run(self.catalog, "base", out)
        self.assertEqual((result["deleted"], result["compares"], result["unpinned"]),
                         ("base", ["run__base__r1"], "iPhone_17_iOS_27.0"))
        self.assertEqual(result["notice"], "base was the baseline: no baseline for iPhone 17, iOS 27.0: set one from a run "
                                           "(set-baseline <run>)")
        self.assertIn("note: " + result["notice"], out.lines)
        self.assertEqual(sc.read_baselines(self.catalog), {})
        self.assertEqual(list((self.catalog / sc.COMPARES_DIR).iterdir()), [])
        with self.assertRaisesRegex(sc.CatalogError, "no baseline is pinned for iPhone 17, iOS 27.0"):
            sc.compare_runs(self.catalog, "run", output=QUIET)
        self.make_run("again", self.full())
        self.pin("again")  # the CLI prints the same notice
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = sc.main(["delete-run", "again", "--catalog-dir", str(self.catalog)])
        self.assertEqual(code, 0)
        self.assertIn("note: again was the baseline: no baseline for iPhone 17, iOS 27.0", stdout.getvalue())

    def test_prune_per_source_and_type_never_the_pin_and_drops_stale_comparisons(self) -> None:
        self.make_run("base", self.full())  # oldest, pinned
        self.pin("base")
        for i in range(1, 4):
            self.make_run(f"cli{i}", self.full())
        for i in range(1, 3):
            self.make_run(f"ui{i}", self.full(), source="ui")
        self.make_run("pro1", self.full(), kind=("iPhone 18 Pro", "iOS 27.0"))
        self.make_run("pro2", self.full(), kind=("iPhone 18 Pro", "iOS 27.0"))
        sc.compare_runs(self.catalog, "cli1", output=QUIET)
        sc.compare_runs(self.catalog, "cli3", output=QUIET)
        sc.compare_runs(self.catalog, "ui2", output=QUIET)
        sc.accept_screen(self.catalog, "cli3", "A.same", "light", QUIET, check_main=False)  # cli3__base__r1 and ui2__base__r1 are stale now
        sc.compare_runs(self.catalog, "cli3", output=QUIET)
        (self.catalog / sc.COMPARES_DIR / "gone__base__r1").mkdir()  # an orphan without compare.json
        sc.write_run(self.catalog / sc.RUNS_DIR / "cli2", active=True, pid=os.getpid())
        result = sc.prune(self.catalog, keep=1, output=QUIET)
        # cli: cli3 kept (newest), cli2 active, cli1 pruned. ui: ui2 kept, ui1 pruned. pro: pro2 kept, pro1 pruned.
        self.assertEqual(sorted(result["runs"]), ["cli1", "pro1", "ui1"])
        self.assertEqual(sorted(p.name for p in (self.catalog / sc.RUNS_DIR).iterdir()),
                         ["base", "cli2", "cli3", "pro2", "ui2"])
        self.assertEqual(sorted(result["compares"]), ["cli1__base__r1", "gone__base__r1", "ui2__base__r1"])
        self.assertEqual([p.name for p in (self.catalog / sc.COMPARES_DIR).iterdir()], ["cli3__base__r2"])
        self.assertEqual(sc.prune(self.catalog, keep=1), {"runs": [], "compares": []})
        self.assertEqual(sc.prune_runs(self.catalog, 5), [])
        with self.assertRaises(sc.CatalogError):
            sc.prune(self.catalog, keep=0)


class ExportListTests(CatalogCase):

    def test_export_and_import_round_trip(self) -> None:
        self.make_run("run", self.full())
        (self.catalog / sc.RUNS_DIR / "run" / "logs").mkdir()
        (self.catalog / sc.RUNS_DIR / "run" / "logs" / "light.A.same.stderr.log").write_text("noise")
        archive = sc.export_run(self.catalog, "run")
        self.assertEqual(archive, self.catalog / "screencatalog_run.zip")
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(sorted(z.namelist()), ["run/dark/A.same.png", "run/dark/B.changed.png",
                                                    "run/light/A.same.png", "run/light/B.changed.png",
                                                    "run/manifest.json", "run/run.json"])
            self.assertEqual({i.compress_type for i in z.infolist()}, {zipfile.ZIP_STORED})
        with self.assertRaisesRegex(sc.CatalogError, "exists already"):
            sc.import_run(self.catalog, archive, QUIET)
        other = self.root / "other"
        self.assertEqual(sc.import_run(other, archive, QUIET), "run")
        self.assertEqual(sc.read_rows(other / sc.RUNS_DIR / "run"), sc.read_rows(self.catalog / sc.RUNS_DIR / "run"))
        evil = self.root / "evil.zip"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("run/run.json", "{}")
            z.writestr("run/../../escape.txt", "x")
        with self.assertRaisesRegex(sc.CatalogError, "leaves the run folder"):
            sc.import_run(self.root / "third", evil, QUIET)

    def test_list_and_comparable(self) -> None:
        self.make_run("base", self.full())
        self.pin("base")
        self.assertEqual(sc.comparable(self.catalog, "base"), [])  # no other run of the type
        self.make_run("run", self.full())
        self.make_run("pro", self.full(), kind=("iPhone 18 Pro", "iOS 27.0"))
        self.make_run("busy", self.full(), finished=False, active=True, pid=os.getpid())
        self.assertEqual(sc.comparable(self.catalog, "run"), ["base"])
        self.assertEqual(sc.comparable(self.catalog, "pro"), [])
        self.assertEqual(sc.pinned(self.catalog, D17), "base")
        self.assertIsNone(sc.pinned(self.catalog, "iPhone_18_Pro_iOS_27.0"))
        sc.compare_runs(self.catalog, "run", output=QUIET)
        out = Lines()
        self.assertEqual(sc.run_catalog(sc.parse_args(["list", "--catalog-dir", str(self.catalog)]), out), 0)
        listing = json.loads(out.lines[0])
        runs = {r["id"]: r for r in listing["runs"]}
        self.assertEqual([r["id"] for r in listing["runs"]], ["busy", "pro", "run", "base"])
        self.assertEqual(listing["baselines"], {D17: "base"})
        self.assertEqual((runs["run"]["comparable"], runs["pro"]["comparable"], runs["base"]["comparable"]),
                         (["base"], [], ["run"]))
        self.assertTrue(runs["base"]["pinned"] and runs["busy"]["active"] and not runs["busy"]["complete"])
        self.assertEqual(runs["run"]["baselineCompare"]["key"], "run__base__r1")
        self.assertIsNone(runs["pro"]["baselineCompare"])


class RecordTests(CatalogCase):
    """`record` with the simulator faked: one run folder, nothing else."""

    def setUp(self) -> None:
        super().setUp()
        self.exclude = self.root / "exclusions.json"
        self.exclude.write_text(json.dumps([{"name": "C.skip", "reason": "animates"}]))
        self.stop = threading.Event()
        captured = []
        self.captured = captured
        stop = self.stop

        def fake_capture_variant(args, bridge, udid, entry, appearance, out_root, log_dir, port=None):
            target = out_root / sc.shot_relpath(appearance, entry["name"])
            target.parent.mkdir(parents=True, exist_ok=True)
            captured.append(entry["name"])
            if len(captured) == 2 and getattr(args, "stop_after_two", False):
                stop.set()
            if entry["name"] == "B.err":
                return sc.variant_record(entry["name"], appearance, sc.ERROR, udid=udid, error="bridge never answered")
            target.write_bytes(solid_png(9))
            return sc.variant_record(entry["name"], appearance, sc.RECORDED, udid=udid, settle_ms=0, settled=True,
                                     duration_ms=1)

        fakes = {
            "device_info": lambda udid: {"udid": udid, "simulatorName": f"sim-{udid}", "model": "iPhone 17",
                                         "os": "iOS 27.0", "osBuild": "24A434"},
            "app_binary": lambda udid, bundle: {"bundleId": bundle, "path": "/x", "modified": "2026-10-08T10:00:00+00:00",
                                                "version": "2.0", "build": "1", "sha256": "abc123"},
            "simctl": lambda *a, **k: None,
            "enumerate_variants": lambda *a, **k: [{"name": n, "feature": n.split(".")[0]}
                                                   for n in ("A.ok", "B.err", "C.skip", "D.ok")],
            "capture_variant": fake_capture_variant,
            "git_state": lambda: {"sha": "deadbeef", "dirty": False},
        }
        for name, fake in fakes.items():
            original = getattr(sc, name)
            setattr(sc, name, fake)
            self.addCleanup(setattr, sc, name, original)

    def record(self, *extra: str, stop_after_two: bool = False) -> tuple[int, Lines]:
        args = sc.parse_args(["record", "--catalog-dir", str(self.catalog), "--exclude-file", str(self.exclude),
                              "--udid", "U1", "--port", "8800", *extra])
        args.stop_after_two = stop_after_two
        out = Lines()
        return sc.run_catalog(args, out, self.stop), out

    def test_a_run_of_the_same_build_is_refused_until_that_run_is_deleted(self) -> None:
        same = dict(git={"sha": "deadbeef", "dirty": False}, app={"sha256": "abc123", "version": "2.0", "build": "1"},
                    captureMode="app")
        self.make_run("earlier", self.full(), **same)
        code, out = self.record()
        self.assertEqual(code, 2)
        self.assertEqual(out.errors, ["error: a complete run of this build exists already: earlier (in this catalog): "
                                      "iPhone 17, iOS 27.0, commit deadbeef, app abc123, app capture. Open or pull it instead; "
                                      "to record it again, delete that run first"])
        self.assertEqual([p.name for p in (self.catalog / sc.RUNS_DIR).iterdir()], ["earlier"])  # nothing written
        with self.assertRaises(SystemExit):  # there is no flag around it
            sc.parse_args(["record", "--allow-duplicate"])
        sc.delete_run(self.catalog, "earlier", QUIET)
        self.assertEqual(self.record()[0], 1)  # B.err: the usual ERROR exit, but it recorded
        # What does not count: another capture mode, a partial run, a stopped run, a dirty tree, another commit.
        for run_id, fields in (("mode", dict(same, captureMode="simctl")),
                               ("partial", dict(same, only=["A."])),
                               ("stopped", dict(same, stopped=True)),
                               ("dirty", dict(same, git={"sha": "deadbeef", "dirty": True})),
                               ("other", dict(same, git={"sha": "cafef00d", "dirty": False}))):
            with self.subTest(run=run_id):
                shutil.rmtree(self.catalog / sc.RUNS_DIR, ignore_errors=True)
                self.make_run(run_id, self.full(), **fields)
                self.assertEqual(self.record()[0], 1, run_id)
        shutil.rmtree(self.catalog / sc.RUNS_DIR, ignore_errors=True)
        sc.git_state = lambda: {"sha": "deadbeef", "dirty": True}
        self.make_run("earlier", self.full(), **same)
        self.assertEqual(self.record()[0], 1)  # a dirty tree records: its sha cannot be matched

    def test_record_writes_the_capture_mode(self) -> None:
        self.record("--run-id", "cli-default", "--appearance", "light")
        self.assertEqual(sc.read_run(self.catalog / sc.RUNS_DIR / "cli-default")["captureMode"], "app")
        self.record("--run-id", "cli-simctl", "--appearance", "light", "--capture", "simctl")
        self.assertEqual(sc.read_run(self.catalog / sc.RUNS_DIR / "cli-simctl")["captureMode"], "simctl")

    def test_record_writes_one_run_folder_and_nothing_else(self) -> None:
        code, out = self.record("--run-id", "cli-one", "--appearance", "light")
        self.assertEqual(code, 1)  # B.err is an ERROR
        self.assertEqual(sorted(p.name for p in self.catalog.iterdir()), ["runs"])
        path = self.catalog / sc.RUNS_DIR / "cli-one"
        self.assertEqual(sorted(p.name for p in path.iterdir()), ["light", "manifest.json", "run.json"])
        run = sc.read_run(path)
        self.assertEqual((run["source"], run["deviceType"], run["filter"], run["appearance"], run["revision"],
                          run["history"], run["counts"], run["stopped"], run["active"], run["git"]),
                         ("cli", {"model": "iPhone 17", "os": "iOS 27.0", "osBuild": "24A434", "deviceDir": D17},
                          {"only": [], "feature": [], "label": "all"}, "light", 1, [], {"light": 2}, False, False,
                          {"sha": "deadbeef", "dirty": False}))
        self.assertEqual(run["simulators"], [{"udid": "U1", "name": "sim-U1", "port": 8800,
                                              "appModified": "2026-10-08T10:00:00+00:00", "appSha256": "abc123"}])
        self.assertEqual(run["revisionAt"], run["finished"])
        self.assertTrue(sc.run_is_complete(run) and sc.run_is_partial(run))  # one appearance: partial
        rows = {r["name"]: (r["status"], r["error"], r["file"]) for r in sc.read_rows(path)}
        self.assertEqual(rows, {"A.ok": (sc.RECORDED, None, "light/A.ok.png"),
                                "B.err": (sc.ERROR, "bridge never answered", "light/B.err.png"),
                                "C.skip": (sc.SKIPPED, "animates", "light/C.skip.png"),
                                "D.ok": (sc.RECORDED, None, "light/D.ok.png")})
        self.assertNotIn("C.skip", self.captured)
        self.assertEqual(out.final[0], "record")
        self.assertIn("no baseline is pinned for iPhone 17 iOS 27.0: `set-baseline cli-one` pins this run", out.lines)

    def test_a_stopped_record_keeps_its_partial_captures(self) -> None:
        code, out = self.record("--run-id", "cli-stopped", stop_after_two=True)
        self.assertEqual(code, 1)
        run = sc.read_run(self.catalog / sc.RUNS_DIR / "cli-stopped")
        self.assertTrue(run["stopped"] and run["finished"] and run["notStarted"] > 0)
        self.assertEqual(run["counts"], {"light": 1})
        with self.assertRaisesRegex(sc.CatalogError, "stopped before"):
            sc.set_baseline(self.catalog, "cli-stopped", output=QUIET, check_main=False)

    def test_record_refusals_and_prune(self) -> None:
        self.make_run("taken", self.full())
        code, out = self.record("--run-id", "taken")
        self.assertEqual((code, "exists already" in out.errors[0]), (2, True))
        code, out = self.record("--run-id", "a__b")
        self.assertEqual(code, 2)
        code, out = self.record("--only", "Nothing.matches")
        self.assertEqual((code, out.errors), (2, ["error: no variant matched the filters"]))
        self.assertEqual(sorted(p.name for p in (self.catalog / sc.RUNS_DIR).iterdir()), ["taken"])  # nothing left behind
        self.pin("taken")
        for i in range(3):
            self.make_run(f"old{i}", self.full())
        code, out = self.record("--only", "A.", "--keep-runs", "2")
        self.assertEqual(code, 0)
        left = sorted(p.name for p in (self.catalog / sc.RUNS_DIR).iterdir())
        self.assertEqual(len(left), 3)  # the pin, the new run, the newest old one
        self.assertIn("taken", left)
        self.assertIn("old2", left)
        self.assertTrue(any(line.startswith("compare it with the baseline (taken)") for line in out.lines))


# MARK: - Sync through S3, against a fake `aws` on PATH

FAKE_BUCKET = "example-test-assets"
FAKE_SECRET = "s3cr3t-never-logged"
FAKE_AWS = r'''
"""A fake `aws` CLI: the `s3 ls`, `s3 cp` and `s3 sync` subset screencatalog.py uses, mirroring
s3://<bucket>/<key> to $FAKE_S3_ROOT/<bucket>/<key>. Every call is appended to $FAKE_S3_ROOT/../aws-calls.jsonl."""
import fnmatch
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(os.environ["FAKE_S3_ROOT"])
args = sys.argv[1:]
with open(ROOT.parent / "aws-calls.jsonl", "a") as log:
    log.write(json.dumps(args) + "\n")
FLAGS = {"--exact-timestamps", "--no-progress", "--only-show-errors", "--recursive"}
excludes, positional, i = [], [], 0
while i < len(args):
    if args[i] == "--exclude":
        excludes.append(args[i + 1]); i += 2
    elif args[i] in FLAGS:
        i += 1
    elif args[i].startswith("--"):
        sys.exit(f"fake aws: unsupported option {args[i]}")
    else:
        positional.append(args[i]); i += 1


def local(url):
    return ROOT / url[len("s3://"):] if url.startswith("s3://") else Path(url)


def missing(url):
    print(f'fatal error: An error occurred (404) when calling the HeadObject operation: Key "{url}" does not exist',
          file=sys.stderr)
    sys.exit(1)


if positional[:2] == ["s3", "ls"]:
    folder = local(positional[2])
    if not folder.is_dir():
        sys.exit(1)
    for path in sorted(folder.iterdir()):
        print(f"                           PRE {path.name}/" if path.is_dir()
              else f"2026-10-08 10:00:00 {path.stat().st_size:>10} {path.name}")
elif positional[:2] == ["s3", "cp"]:
    source, target = positional[2], positional[3]
    if not local(source).is_file():
        missing(source)
    local(target).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local(source), local(target))
elif positional[:2] == ["s3", "sync"]:
    source, target = local(positional[2]), local(positional[3])
    verb = "upload" if positional[3].startswith("s3://") else "download"
    for path in sorted(source.rglob("*")) if source.is_dir() else []:
        relative = path.relative_to(source).as_posix()
        if not path.is_file() or any(fnmatch.fnmatch(relative, p) for p in excludes):
            continue
        destination = target / relative
        if destination.is_file() and destination.stat().st_size == path.stat().st_size \
                and destination.stat().st_mtime == path.stat().st_mtime:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        print(f"{verb}: {positional[2].rstrip('/')}/{relative} to {positional[3].rstrip('/')}/{relative}")
elif positional[:2] == ["s3", "rm"]:
    folder = local(positional[2])
    if not folder.is_dir():
        sys.exit(1)
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        print(f"delete: {positional[2].rstrip('/')}/{path.relative_to(folder).as_posix()}")
    shutil.rmtree(folder)
else:
    sys.exit(f"fake aws: unsupported command {positional}")
'''


def install_fake_aws(test: unittest.TestCase, root: Path) -> Path:
    """Puts the fake `aws` first on PATH (and sets the bucket and a secret that must never be printed) for
    the test; the fake S3 root, where s3://<bucket>/<key> lands as <root>/<bucket>/<key>."""
    bin_dir, s3 = root / "fake-bin", root / "fake-s3"
    bin_dir.mkdir()
    s3.mkdir()
    script = bin_dir / "aws"
    script.write_text(f"#!{sys.executable}\n" + FAKE_AWS)
    script.chmod(0o755)
    patcher = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                                           "FAKE_S3_ROOT": str(s3), sc.BUCKET_ENV: FAKE_BUCKET,
                                           "AWS_SECRET_ACCESS_KEY": FAKE_SECRET})
    patcher.start()
    test.addCleanup(patcher.stop)
    return s3


class SyncTests(CatalogCase):
    """`sync push` / `sync pull` against a fake aws mirroring into a directory: the default sets, the
    conflict rule both ways, --force, --all, logs never leaving, sync.json, and nothing secret printed."""

    def setUp(self) -> None:
        super().setUp()
        self.s3 = install_fake_aws(self, self.root)
        self.bucket = self.s3 / FAKE_BUCKET / sc.SYNC_PREFIX
        self.make_run("base", self.full())
        self.pin("base")
        (self.catalog / sc.RUNS_DIR / "base" / "logs").mkdir()
        (self.catalog / sc.RUNS_DIR / "base" / "logs" / "launch.log").write_text("log")
        screens = self.full()
        screens[("B.changed", "light")] = solid_png(9, block=True)
        self.make_run("run", screens)
        self.make_run("part", {("A.same", "light"): solid_png(9)}, only=["A."], appearance="light")
        self.make_run("cap", self.full(), finished=False, active=True, pid=os.getpid())
        self.out = Lines()

    def remote(self, out: sc.Output | None = None) -> sc.Remote:
        return sc.Remote(FAKE_BUCKET, output=out or self.out)

    def other_catalog(self) -> Path:
        other = self.root / "teammate"
        other.mkdir(exist_ok=True)
        return other

    def cli(self, *argv: str, catalog: Path | None = None) -> tuple[int, Lines]:
        out = Lines()
        args = sc.parse_args(["sync", *argv, "--catalog-dir", str(catalog or self.catalog)])
        return sc.run_catalog(args, out), out

    def remote_run(self, run_id: str) -> dict:
        return json.loads((self.bucket / sc.RUNS_DIR / run_id / sc.RUN_FILE).read_text())

    def assert_nothing_secret(self, out: Lines) -> None:
        text = "\n".join(out.lines + out.errors)
        self.assertNotIn(FAKE_SECRET, text)
        self.assertNotIn(FAKE_BUCKET, text)

    def test_delete_removes_a_remote_run_but_never_the_remote_baseline(self) -> None:
        self.assertEqual(self.cli("push", "--all")[0], 0)  # base (pinned), run, part
        code, out = self.cli("delete", "base")
        self.assertEqual((code, out.errors), (2, ["error: base is the bucket's baseline of iPhone_17_iOS_27.0: push another "
                                                   "baseline for that type first"]))
        code, out = self.cli("delete", "nope")
        self.assertEqual((code, out.errors), (2, ["error: no run nope on the remote"]))
        code, out = self.cli("delete", "run", "part")
        self.assertEqual(code, 0, out.errors)
        self.assertEqual(out.lines[-1], "deleted 2 runs from the bucket; a Mac that pulled one keeps its copy")
        self.assertEqual(sorted(p.name for p in (self.bucket / sc.RUNS_DIR).iterdir()), ["base"])
        self.assertTrue((self.catalog / sc.RUNS_DIR / "run").is_dir())  # the local copy stays
        self.assertNotIn("run", sc.read_sync(self.catalog)["runs"])
        self.assertIn("base", sc.read_sync(self.catalog)["runs"])
        self.assert_nothing_secret(out)
        with self.assertRaises(SystemExit):
            sc.parse_args(["sync", "delete"])  # a run must be named

    def test_pushing_a_pinned_run_by_name_pushes_the_pins_too(self) -> None:
        code, out = self.cli("push", "base")
        self.assertEqual(code, 0, out.errors)
        self.assertIn(f"{sc.BASELINES_FILE} goes too: base is the pinned baseline of its type", out.lines)
        self.assertEqual(json.loads((self.bucket / sc.BASELINES_FILE).read_text()), {D17: "base"})
        self.assertFalse((self.bucket / sc.RUNS_DIR / "run").exists())
        code, out = self.cli("push", "run")  # not a pin: the pins file is left alone
        self.assertEqual(code, 0, out.errors)
        self.assertFalse(any(sc.BASELINES_FILE in line for line in out.lines))

    def test_replace_pins_lifts_only_the_unknown_pin_rule(self) -> None:
        self.assertEqual(self.cli("push")[0], 0)  # base pinned and up
        sc.delete_run(self.catalog, "base", QUIET)  # the old pin is gone here, as after a re-record
        self.make_run("fresh", self.full(5), git={"sha": "fresh", "dirty": False}, app={"sha256": "appfresh"})
        self.pin("fresh")
        out = Lines()
        self.assertEqual(sc.run_catalog(sc.parse_args(["sync", "push", "--catalog-dir", str(self.catalog)]), out), 2)
        self.assertIn("which you do not have: pull first, or pass --force to replace it", out.errors[0])
        result = sc.sync_push(self.catalog, self.remote(), replace_pins=True, output=Lines())
        self.assertEqual((result["pushed"], result["baselines"]), (["fresh"], {D17: "fresh"}))
        self.assertEqual(json.loads((self.bucket / sc.BASELINES_FILE).read_text()), {D17: "fresh"})
        self.assertTrue((self.bucket / sc.RUNS_DIR / "base").is_dir())  # the old run stays until deleted
        self.assertEqual(self.cli("delete", "base")[0], 0)  # and now it can be

    def test_push_refuses_a_run_the_remote_holds_as_another_id(self) -> None:
        self.assertEqual(self.cli("push")[0], 0)  # base is up
        base = sc.read_run(self.catalog / sc.RUNS_DIR / "base")
        self.make_run("twin", self.full(), git=base["git"], app=base["app"])
        code, out = self.cli("push", "twin")
        self.assertEqual(code, 2)
        self.assertEqual(out.errors, ["error: twin: the remote already has a run of this build on iPhone 17, iOS 27.0: base; "
                                      "pull it instead, or delete it there first"])
        self.assertFalse((self.bucket / sc.RUNS_DIR / "twin").exists())
        self.assertEqual(self.cli("push", "twin", "--force")[0], 2)  # --force is for revision conflicts, not this
        self.assertFalse((self.bucket / sc.RUNS_DIR / "twin").exists())
        shutil.rmtree(self.bucket / sc.RUNS_DIR / "base")  # the only way: the other run goes
        self.assertEqual(self.cli("push", "twin")[0], 0)
        self.assertTrue((self.bucket / sc.RUNS_DIR / "twin" / sc.RUN_FILE).is_file())
        listed = {r["id"]: r for r in sc.remote_listing(self.catalog, self.remote())["runs"]}
        self.assertEqual((listed["twin"]["git"], listed["twin"]["appSha256"], listed["twin"]["captureMode"], listed["twin"]["complete"]),
                         (base["git"]["sha"], base["app"]["sha256"], "simctl", True))

    def test_push_default_set_is_the_pins_and_baselines_json_without_logs(self) -> None:
        code, out = self.cli("push")
        self.assertEqual(code, 0, out.errors)
        # Per-file transfer lines are counted, not shown: a run is hundreds of them.
        self.assertFalse([l for l in out.lines if l.startswith("upload: ")], out.lines)
        self.assertIn("5 files uploaded", out.lines)  # base: 4 screens + manifest.json (run.json goes by cp)
        # Pushing again with nothing changed sends nothing: the remote run.json already names every file.
        code, again = self.cli("push")
        self.assertEqual(code, 0, again.errors)
        self.assertIn("base r1 is already on the remote: nothing to push", again.lines)
        self.assertIn(f"{sc.BASELINES_FILE} is up to date on the remote", again.lines)
        self.assertIn("pushed 0 runs (1 already there) to s3://*******-****-assets/screencatalog", again.lines)
        self.assertFalse([l for l in again.lines if l.startswith("$ aws s3 sync")], again.lines)
        self.assertEqual(sorted(p.name for p in (self.bucket / sc.RUNS_DIR).iterdir()), ["base"])
        files = sorted(p.relative_to(self.bucket / sc.RUNS_DIR / "base").as_posix()
                       for p in (self.bucket / sc.RUNS_DIR / "base").rglob("*") if p.is_file())
        self.assertEqual(files, ["dark/A.same.png", "dark/B.changed.png", "light/A.same.png", "light/B.changed.png",
                                 "manifest.json", "run.json"])
        self.assertEqual(json.loads((self.bucket / sc.BASELINES_FILE).read_text()), {D17: "base"})
        self.assertFalse(any(p.name in ("compares", ".thumbs", "sync.json") for p in self.bucket.rglob("*")))
        calls = [json.loads(line) for line in (self.root / "aws-calls.jsonl").read_text().splitlines()]
        syncs = [c for c in calls if c[:2] == ["s3", "sync"]]
        self.assertEqual(len(syncs), 1)
        self.assertIn("--exact-timestamps", syncs[0])
        self.assertEqual(syncs[0][syncs[0].index("--exclude") + 1], "logs/*")
        # run.json is the commit marker: it is sent after every other file of the run
        self.assertEqual(calls.index(syncs[0]) + 1, calls.index(next(c for c in calls if c[:2] == ["s3", "cp"]
                                                                     and c[-1].endswith("runs/base/run.json"))))
        state = sc.read_sync(self.catalog)
        self.assertEqual(state["bucket"], "*******-****-assets")
        self.assertEqual(state["runs"]["base"]["pushedRevision"], 1)
        self.assertTrue(state["runs"]["base"]["pushedAt"] and state["baselines"]["pushedAt"] and state["lastPush"])
        self.assertNotIn("run", state["runs"])
        self.assert_nothing_secret(out)
        self.assertTrue(any("*******-****-assets" in line for line in out.lines))
        # idempotent: a second push sends no file again
        before = (self.root / "aws-calls.jsonl").read_text().count("\n")
        code, again = self.cli("push")
        self.assertEqual(code, 0)
        self.assertFalse(any(line.startswith("upload:") for line in again.lines), again.lines)
        self.assertGreater((self.root / "aws-calls.jsonl").read_text().count("\n"), before)

    def test_push_all_and_named_runs(self) -> None:
        result = sc.sync_push(self.catalog, self.remote(), include_all=True, output=self.out)
        self.assertEqual(sorted(result["pushed"]), ["base", "part", "run"])
        self.assertIn("skipping cap: still capturing", self.out.lines)
        with self.assertRaisesRegex(sc.CatalogError, r"cannot push part: partial \(A\.\): push --all includes it"):
            sc.sync_push(self.catalog, self.remote(), ["part"], output=self.out)
        with self.assertRaisesRegex(sc.CatalogError, "cannot push cap: still capturing"):
            sc.sync_push(self.catalog, self.remote(), ["cap"], include_all=True, output=self.out)
        self.assertEqual(sc.sync_push(self.catalog, self.remote(), ["run"], output=self.out),
                         {"pushed": [], "alreadyPushed": ["run"], "baselines": None})  # --all pushed it a moment ago
        with self.assertRaisesRegex(sc.CatalogError, "no run nope"):
            sc.sync_push(self.catalog, self.remote(), ["nope"], output=self.out)

    def test_push_refuses_a_higher_remote_revision_unless_forced(self) -> None:
        self.assertEqual(self.cli("push")[0], 0)
        theirs = self.remote_run("base")
        theirs.update(revision=3, revisionAt="2026-10-08T12:00:00+00:00")
        (self.bucket / sc.RUNS_DIR / "base" / sc.RUN_FILE).write_text(json.dumps(theirs))
        code, out = self.cli("push")
        self.assertEqual(code, 2)
        self.assertEqual(out.errors, ["error: base: remote baseline is r3, yours is r1: pull first"])
        code, out = self.cli("push", "--force")
        self.assertEqual(code, 0, out.errors)
        self.assertEqual(sc.run_revision(self.remote_run("base")), 1)

    def test_push_refuses_a_remote_pin_you_do_not_have_unless_forced(self) -> None:
        self.bucket.mkdir(parents=True)
        (self.bucket / sc.BASELINES_FILE).write_text(json.dumps({D17: "theirs", "iPhone_18_iOS_27.0": "pro"}))
        code, out = self.cli("push")
        self.assertEqual(code, 2)
        self.assertIn("remote baselines.json pins iPhone_17_iOS_27.0 → theirs, which you do not have: pull first",
                      out.errors[0])
        self.assertFalse((self.bucket / sc.RUNS_DIR).exists())  # refused before anything was sent
        self.assertEqual(self.cli("push", "--force")[0], 0)
        # another type's pin is kept: only the types pinned here are replaced
        self.assertEqual(json.loads((self.bucket / sc.BASELINES_FILE).read_text()),
                         {D17: "base", "iPhone_18_iOS_27.0": "pro"})

    def test_pull_default_set_then_a_newer_revision_then_up_to_date(self) -> None:
        self.assertEqual(self.cli("push", "--all")[0], 0)
        mate = self.other_catalog()
        code, out = self.cli("pull", catalog=mate)
        self.assertEqual(code, 0, out.errors)
        self.assertEqual(sorted(p.name for p in (mate / sc.RUNS_DIR).iterdir()), ["base"])  # the pins only
        self.assertEqual(sc.read_baselines(mate), {D17: "base"})
        self.assertFalse((mate / sc.RUNS_DIR / "base" / "logs").exists())
        self.assertEqual((mate / sc.RUNS_DIR / "base" / "light" / "B.changed.png").read_bytes(), solid_png(9))
        state = sc.read_sync(mate)
        self.assertEqual(state["runs"]["base"]["pulledRevision"], 1)
        self.assertTrue(state["baselines"]["pulledAt"] and state["lastPull"])
        self.assert_nothing_secret(out)
        # the owner accepts a screen (r2) and pushes; the teammate's pull brings r2 and the new pixels
        sc.accept_screen(self.catalog, "run", "B.changed", "light", QUIET, skip_review=True, check_main=False)
        self.assertEqual(self.cli("push")[0], 0)
        code, out = self.cli("pull", catalog=mate)
        self.assertEqual(code, 0, out.errors)
        self.assertEqual(sc.run_revision(sc.read_run(mate / sc.RUNS_DIR / "base")), 2)
        self.assertEqual((mate / sc.RUNS_DIR / "base" / "light" / "B.changed.png").read_bytes(), solid_png(9, block=True))
        code, out = self.cli("pull", catalog=mate)
        self.assertIn("base r2 is up to date", out.lines)
        self.assertEqual([p.name for p in (mate / sc.RUNS_DIR).iterdir()], ["base"])  # no staging folder left
        # named runs, and the listing
        self.assertEqual(self.cli("pull", "run", catalog=mate)[0], 0)
        self.assertTrue((mate / sc.RUNS_DIR / "run" / "manifest.json").is_file())
        listing = sc.remote_listing(mate, self.remote())
        states = {r["id"]: (r["revision"], r["pinned"], r["local"]) for r in listing["runs"]}
        self.assertEqual(states, {"base": (2, True, "same"), "run": (1, False, "same"), "part": (1, False, "missing")})
        self.assertEqual(listing["bucket"], "*******-****-assets")
        code, out = self.cli("pull", "--list", catalog=mate)
        self.assertEqual(json.loads("\n".join(out.lines))["baselines"], {D17: "base"})

    def test_pull_refuses_a_higher_local_revision_unless_forced(self) -> None:
        self.assertEqual(self.cli("push")[0], 0)
        path = self.catalog / sc.RUNS_DIR / "base"
        sc.write_run(path, revision=3, revisionAt="2026-10-08T12:00:00+00:00")
        code, out = self.cli("pull")
        self.assertEqual(code, 2)
        self.assertEqual(out.errors, ["error: base: yours is r3, remote baseline is r1: push it, or pull --force to "
                                      "overwrite yours"])
        self.assertEqual(sc.run_revision(sc.read_run(path)), 3)
        code, out = self.cli("pull", "--force")
        self.assertEqual(code, 0, out.errors)
        self.assertEqual(sc.run_revision(sc.read_run(path)), 1)
        self.assertTrue((path / "logs" / "launch.log").is_file())  # local logs are left alone

    def test_the_same_revision_changed_on_both_sides_is_refused_both_ways(self) -> None:
        self.assertEqual(self.cli("push")[0], 0)
        theirs = self.remote_run("base")
        theirs.update(revision=2, revisionAt="2026-10-08T12:00:00+00:00")
        (self.bucket / sc.RUNS_DIR / "base" / sc.RUN_FILE).write_text(json.dumps(theirs))
        sc.write_run(self.catalog / sc.RUNS_DIR / "base", revision=2, revisionAt="2026-10-08T12:30:00+00:00")
        for verb in ("push", "pull"):
            code, out = self.cli(verb)
            self.assertEqual(code, 2)
            self.assertIn("base: remote and local are both r2 but were changed separately", out.errors[0])

    def test_pull_replaces_a_local_pin_and_keeps_the_old_run(self) -> None:
        self.assertEqual(self.cli("push")[0], 0)
        self.pin("run")
        code, out = self.cli("pull")
        self.assertEqual(code, 0, out.errors)
        self.assertEqual(sc.read_baselines(self.catalog), {D17: "base"})
        self.assertIn(f"baseline of {D17}: base (was run, which stays as a run)", out.lines)
        self.assertIsNotNone(sc.read_run(self.catalog / sc.RUNS_DIR / "run"))

    def test_configuration_errors(self) -> None:
        with self.assertRaisesRegex(sc.CatalogError, "no bucket: pass --bucket or set SCREENCATALOG_S3_BUCKET"):
            sc.Remote(None)
        with self.assertRaisesRegex(sc.CatalogError, "not an S3 bucket name"):
            sc.Remote("Bad_Bucket")
        with self.assertRaisesRegex(sc.CatalogError, "bad prefix"):
            sc.Remote(FAKE_BUCKET, "a/../b")
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "nowhere")}):
            with self.assertRaisesRegex(sc.CatalogError, "the aws CLI is not on PATH"):
                sc.Remote(FAKE_BUCKET)
        with mock.patch.dict(os.environ, {sc.BUCKET_ENV: ""}):
            code, out = self.cli("pull")
        self.assertEqual((code, out.errors), (2, ["error: no bucket: pass --bucket or set SCREENCATALOG_S3_BUCKET"]))
        self.assertEqual(sc.mask_bucket("example-test-assets"), "*******-****-assets")
        self.assertEqual(sc.mask_bucket("mybucket"), "****cket")
        self.assertIsNone(sc.mask_bucket(None))
        # an empty remote: nothing pinned, nothing listed
        code, out = self.cli("pull")
        self.assertEqual(code, 0, out.errors)
        self.assertIn("no baselines.json on the remote: nothing is pinned there", out.lines)
        self.assertEqual(sc.remote_listing(self.catalog, self.remote())["runs"], [])


def subprocess_result(code: int = 0):
    """What `sc.simctl` returns, for fakes: a CompletedProcess with empty output."""
    return subprocess.CompletedProcess([], code, "", "")


class AppCaptureTests(unittest.TestCase):
    """`--capture app`: screenshots from the in-app server's GET /screenshot instead of simctl."""

    def serve(self, body: bytes, content_type: str = "image/png", status: int = 200) -> int:
        """A fake bridge on a free loopback port: /screenshot answers with `body`, /status with the
        variant shown. Returns the port."""
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server's name)
                if self.path == "/screenshot":
                    payload, kind, code = body, content_type, status
                else:
                    payload, kind, code = b'{"protocol": 1, "shown": true}', "application/json", 200
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def capture(self, port: int, argv: list[str]) -> tuple[dict, Path, list]:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root, simctl_calls = Path(folder.name), []

        def simctl(*args, **_kwargs):
            simctl_calls.append(args)
            return subprocess_result()

        fakes = {"launch": lambda *a, **k: sc.Launch(42, root / "x.log"), "wait_for_health": lambda *a, **k: None,
                 "wait_for_settle": lambda *a, **k: (True, 0, None), "process_alive": lambda pid: True,
                 "simctl": simctl}
        for name, fake in fakes.items():
            original = getattr(sc, name)
            setattr(sc, name, fake)
            self.addCleanup(setattr, sc, name, original)
        args = sc.parse_args(["record", "--min-launch-ms", "0", "--min-shown-ms", "0", "--band-interval-ms", "0",
                              "--max-wait-ms", "500", *argv])
        row = sc.capture_variant(args, sc.Bridge(port), "U1", {"name": "A.x"}, "light", root, root)
        return row, root, simctl_calls

    def test_the_default_mode_is_app(self) -> None:
        self.assertEqual(sc.parse_args(["record"]).capture, "app")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            sc.parse_args(["record", "--capture", "xcode"])

    def test_app_mode_keeps_the_bridges_png_and_never_calls_simctl(self) -> None:
        blob = solid_png(7)
        row, root, simctl_calls = self.capture(self.serve(blob), ["--capture", "app"])
        self.assertEqual((row["status"], row["shots"], row["unstableBands"], row["captureMode"]),
                         (sc.RECORDED, 2, False, "app"))
        self.assertEqual((root / "light" / "A.x.png").read_bytes(), blob)
        self.assertEqual([c for c in simctl_calls if c[:1] == ("io",)], [])

    def test_app_mode_reports_a_bridge_that_does_not_answer_with_a_png(self) -> None:
        row, root, _ = self.capture(self.serve(b'{"error": "no connected window scene"}', "application/json", 503),
                                    ["--capture", "app"])
        self.assertEqual((row["status"], row["captureMode"]), (sc.ERROR, "app"))
        self.assertIn("HTTP 503", row["error"])
        row, _, _ = self.capture(self.serve(b"not a png"), ["--capture", "app"])
        self.assertIn("not a PNG", row["error"])
        self.assertFalse((root / "light" / "A.x.png").exists())

    def test_simctl_mode_rows_say_so(self) -> None:
        row, _, simctl_calls = self.capture(self.serve(b""), ["--capture", "simctl"])
        self.assertEqual([c[:3] for c in simctl_calls if c[:1] == ("io",)], [("io", "U1", "screenshot")])
        self.assertEqual(row["captureMode"], "simctl")
        self.assertNotIn("captureMode", sc.variant_record("A.x", "light", sc.SKIPPED, error="animates"))


class CompositorBandTests(unittest.TestCase):
    """The band check `record` runs before keeping a screenshot: the Dynamic Island (top) and home
    indicator (bottom) bands of two screenshots in a row, and the retake loop around it."""

    W, H = 4, 100  # bands: rows 0-6 (7 %) and 97-99 (3 %)

    def png(self, painted: tuple[int, ...] = (), value: int = 200) -> bytes:
        data = bytearray(self.W * self.H * 3)
        for y in painted:
            data[y * self.W * 3:(y + 1) * self.W * 3] = bytes([value]) * (self.W * 3)
        return sc.encode_png(sc.Image(self.W, self.H, 3, bytes(data)))

    def test_band_rows_scale_with_the_height(self) -> None:
        self.assertEqual(sc.band_rows(100), (range(0, 7), range(97, 100)))
        self.assertEqual(sc.band_rows(2622), (range(0, 184), range(2543, 2622)))  # iPhone 17 at 3x
        top, bottom = sc.band_rows(2622)
        self.assertIn(152, top)      # the Dynamic Island's last row
        self.assertIn(2583, bottom)  # the home indicator's first row
        self.assertEqual(sc.band_rows(10), (range(0, 1), range(9, 10)))

    def test_identical_screenshots_have_no_differing_band(self) -> None:
        self.assertEqual(sc.differing_bands(self.png((50,)), self.png((50,))), [])
        # Same pixels, different encoding: decided on the pixels, not the bytes.
        image = sc.decode_png(self.png((2, 50)))
        self.assertEqual(sc.differing_bands(self.png((2, 50)), encode_with_filter(image, [1, 2, 3, 4])), [])

    def test_a_different_top_band(self) -> None:
        self.assertEqual(sc.differing_bands(self.png(), self.png((0,))), ["top"])
        self.assertEqual(sc.differing_bands(self.png(), self.png((6,))), ["top"])

    def test_a_different_bottom_band(self) -> None:
        self.assertEqual(sc.differing_bands(self.png(), self.png((99,))), ["bottom"])
        self.assertEqual(sc.differing_bands(self.png(), self.png((97,))), ["bottom"])
        self.assertEqual(sc.differing_bands(self.png(), self.png((1, 98))), ["top", "bottom"])

    def test_content_outside_the_bands_is_ignored(self) -> None:
        self.assertEqual(sc.differing_bands(self.png(), self.png((7, 50, 96))), [])

    def test_a_size_change(self) -> None:
        other = sc.encode_png(sc.Image(self.W, self.H + 1, 3, bytes(self.W * (self.H + 1) * 3)))
        self.assertEqual(sc.differing_bands(self.png(), other), ["size"])

    def run_loop(self, sequence: list, retakes: int = 3) -> tuple[tuple, list, list, Path]:
        """stable_screenshot with a fake screenshot: each call writes the next PNG of `sequence`
        (or returns the error string in it)."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        target = Path(folder.name) / "light" / "A.x.png"
        target.parent.mkdir(parents=True)
        feed, sleeps, written = iter(sequence), [], []

        def take(path: Path):
            item = next(feed)
            if isinstance(item, str):
                return item
            path.write_bytes(item)
            written.append(item)
            return None

        result = sc.stable_screenshot(take, target, retakes, 0.4, sleep=sleeps.append)
        return result, sleeps, written, target

    def test_two_matching_screenshots_keep_the_second(self) -> None:
        first, second = self.png((50,)), self.png((50, 60))  # content differs, bands match
        result, sleeps, _, target = self.run_loop([first, second])
        self.assertEqual(result, (None, 2, False))
        self.assertEqual(sleeps, [0.4])
        self.assertEqual(target.read_bytes(), second)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["A.x.png"])  # no probe left behind

    def test_a_differing_band_retakes_until_two_match(self) -> None:
        island, plain = self.png((1,)), self.png()
        result, sleeps, _, target = self.run_loop([island, plain, plain])
        self.assertEqual(result, (None, 3, False))
        self.assertEqual(sleeps, [0.4, 0.4])
        self.assertEqual(target.read_bytes(), plain)

    def test_bands_that_never_hold_still_keep_the_last_and_flag_it(self) -> None:
        shots = [self.png((1,)), self.png(), self.png((98,)), self.png(), self.png((2,))]
        result, sleeps, written, target = self.run_loop(shots, retakes=3)
        self.assertEqual(result, (None, 5, True))
        self.assertEqual(len(sleeps), 4)
        self.assertEqual(target.read_bytes(), shots[-1])
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["A.x.png"])
        result, _, _, target = self.run_loop([self.png((1,)), self.png()], retakes=0)
        self.assertEqual(result, (None, 2, True))
        self.assertEqual(target.read_bytes(), self.png())

    def test_a_failed_screenshot_stops_the_loop(self) -> None:
        result, _, _, target = self.run_loop([self.png(), "device busy"])
        self.assertEqual(result, ("device busy", 2, False))
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_the_row_carries_the_flag(self) -> None:
        row = sc.variant_record("A.x", "light", sc.RECORDED, shots=5, unstable_bands=True)
        self.assertEqual((row["shots"], row["unstableBands"]), (5, True))
        row = sc.variant_record("A.x", "light", sc.RECORDED, shots=2, unstable_bands=False)
        self.assertEqual((row["shots"], row["unstableBands"]), (2, False))
        self.assertNotIn("unstableBands", sc.variant_record("A.x", "light", sc.SKIPPED, error="animates"))

    def test_capture_variant_writes_the_flag_on_its_row(self) -> None:
        """capture_variant with the simulator faked: screenshots whose top band never holds still."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        shots = iter([self.png((1,)), self.png(), self.png((1,)), self.png(), self.png((1,)), self.png(), self.png()])

        def simctl(*args, **_kwargs):
            if args[:2] == ("io", "U1"):
                Path(args[-1]).write_bytes(next(shots))
            return subprocess_result()

        fakes = {"launch": lambda *a, **k: sc.Launch(42, root / "x.log"), "wait_for_health": lambda *a, **k: None,
                 "wait_for_shown": lambda *a, **k: None, "wait_for_settle": lambda *a, **k: (True, 0, None), "process_alive": lambda pid: True,
                 "simctl": simctl}
        for name, fake in fakes.items():
            original = getattr(sc, name)
            setattr(sc, name, fake)
            self.addCleanup(setattr, sc, name, original)
        args = sc.parse_args(["record", "--capture", "simctl", "--min-launch-ms", "0", "--band-interval-ms", "0"])
        row = sc.capture_variant(args, None, "U1", {"name": "A.x"}, "light", root, root)
        self.assertEqual((row["status"], row["shots"], row["unstableBands"]), (sc.RECORDED, 5, True))
        self.assertEqual((root / "light" / "A.x.png").read_bytes(), self.png((1,)))
        args = sc.parse_args(["record", "--capture", "simctl", "--min-launch-ms", "0", "--band-interval-ms", "0", "--band-retakes", "1"])
        row = sc.capture_variant(args, None, "U1", {"name": "A.x"}, "light", root, root)
        self.assertEqual((row["status"], row["shots"], row["unstableBands"]), (sc.RECORDED, 2, False))

    def fake_simulator(self, root: Path, calls: list) -> None:
        def simctl(*args, **_kwargs):
            if args[:2] == ("io", "U1"):
                calls.append(args)
                Path(args[-1]).write_bytes(self.png())
            return subprocess_result()

        fakes = {"launch": lambda *a, **k: sc.Launch(42, root / "x.log"), "wait_for_health": lambda *a, **k: None,
                 "wait_for_shown": lambda *a, **k: None, "wait_for_settle": lambda *a, **k: (True, 0, None), "process_alive": lambda pid: True,
                 "simctl": simctl}
        for name, fake in fakes.items():
            original = getattr(sc, name)
            setattr(sc, name, fake)
            self.addCleanup(setattr, sc, name, original)

    def test_every_screenshot_passes_the_mask_flag(self) -> None:
        """The probe and the kept screenshot both carry --mask; black by default (see the docs: ignored
        leaves the island's framebuffer in, which flashes)."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root, calls = Path(folder.name), []
        self.fake_simulator(root, calls)
        args = sc.parse_args(["record", "--capture", "simctl", "--min-launch-ms", "0", "--band-interval-ms", "0"])
        self.assertEqual(args.screenshot_mask, "black")
        sc.capture_variant(args, None, "U1", {"name": "A.x"}, "light", root, root)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call[:5], ("io", "U1", "screenshot", "--type=png", "--mask=black"))
        calls.clear()
        args = sc.parse_args(["record", "--capture", "simctl", "--min-launch-ms", "0", "--band-retakes", "-1", "--screenshot-mask", "ignored"])
        sc.capture_variant(args, None, "U1", {"name": "A.x"}, "light", root, root)
        self.assertEqual([c[4] for c in calls], ["--mask=ignored"])

    def test_an_unknown_mask_is_refused(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            sc.parse_args(["record", "--capture", "simctl", "--screenshot-mask", "none"])

    def test_the_kept_screenshot_is_never_before_the_launch_floor(self) -> None:
        """The probe is taken one interval early; the screenshot kept is not taken before the floor."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root, times = Path(folder.name), []
        started = {}

        def launch(*_a, **_k):
            started["at"] = time.monotonic()
            return sc.Launch(42, root / "x.log")

        def simctl(*args, **_kwargs):
            if args[:2] == ("io", "U1"):
                times.append(time.monotonic() - started["at"])
                Path(args[-1]).write_bytes(self.png())
            return subprocess_result()

        fakes = {"launch": launch, "wait_for_health": lambda *a, **k: None,
                 "wait_for_shown": lambda *a, **k: None, "wait_for_settle": lambda *a, **k: (True, 0, None), "process_alive": lambda pid: True,
                 "simctl": simctl}
        for name, fake in fakes.items():
            original = getattr(sc, name)
            setattr(sc, name, fake)
            self.addCleanup(setattr, sc, name, original)
        args = sc.parse_args(["record", "--capture", "simctl", "--min-launch-ms", "300", "--band-interval-ms", "200"])
        row = sc.capture_variant(args, None, "U1", {"name": "A.x"}, "light", root, root)
        self.assertEqual(row["shots"], 2)
        self.assertGreaterEqual(times[0], 0.1)   # the probe: one interval before the floor
        self.assertLess(times[0], 0.3)
        self.assertGreaterEqual(times[1], 0.3)   # the kept screenshot: at or after the floor
        times.clear()
        args = sc.parse_args(["record", "--capture", "simctl", "--min-launch-ms", "300", "--band-retakes", "-1"])
        row = sc.capture_variant(args, None, "U1", {"name": "A.x"}, "light", root, root)
        self.assertEqual((row["shots"], row["unstableBands"]), (1, False))
        self.assertGreaterEqual(times[0], 0.3)

    def shown_run(self, flip_at: float | None, argv: list[str]) -> tuple[dict, list[float]]:
        """capture_variant against a fake bridge whose /status reports the variant as shown from `flip_at`
        seconds after the launch (never when None). Returns (row, screenshot times since launch)."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root, times, started = Path(folder.name), [], {}

        class FakeBridge:
            def try_get(self, path):
                if path != "/status":
                    return None
                late = flip_at is None or time.monotonic() - started["at"] < flip_at
                return {"protocol": 1, "shown": not late}

        def launch(*_a, **_k):
            started["at"] = time.monotonic()
            return sc.Launch(42, root / "x.log")

        def simctl(*args, **_kwargs):
            if args[:2] == ("io", "U1"):
                times.append(time.monotonic() - started["at"])
                Path(args[-1]).write_bytes(self.png())
            return subprocess_result()

        fakes = {"launch": launch, "wait_for_health": lambda *a, **k: None,
                 "wait_for_settle": lambda *a, **k: (True, 0, None), "process_alive": lambda pid: True,
                 "simctl": simctl}
        for name, fake in fakes.items():
            original = getattr(sc, name)
            setattr(sc, name, fake)
            self.addCleanup(setattr, sc, name, original)
        args = sc.parse_args(["record", "--capture", "simctl", "--band-interval-ms", "100", *argv])
        row = sc.capture_variant(args, FakeBridge(), "U1", {"name": "A.x"}, "light", root, root)
        return row, times

    def test_floor_counts_from_the_variant_when_shown_early(self) -> None:
        """Shown at 0.1 s: the shown floor (0.1 + 0.5) is later than the launch floor (0.2)."""
        row, times = self.shown_run(0.1, ["--min-launch-ms", "200", "--min-shown-ms", "500", "--max-wait-ms", "1000"])
        self.assertEqual((row["status"], row["shots"]), (sc.RECORDED, 2))
        self.assertGreaterEqual(row["shownMs"], 100)
        self.assertLess(row["shownMs"], 400)
        self.assertIsNotNone(row["shownAt"])
        self.assertGreaterEqual(times[0], row["shownMs"] / 1000 + 0.5 - 0.1 - 0.001)  # the probe is one interval early
        self.assertGreaterEqual(times[1], row["shownMs"] / 1000 + 0.5 - 0.001)
        self.assertGreaterEqual(times[1], 0.2)

    def test_floor_counts_from_the_launch_when_that_is_later(self) -> None:
        """Shown at 0.1 s with a short shown floor: the launch floor (0.7) binds."""
        row, times = self.shown_run(0.1, ["--min-launch-ms", "700", "--min-shown-ms", "100", "--max-wait-ms", "1000"])
        self.assertGreaterEqual(times[1], 0.7)
        self.assertGreaterEqual(times[0], 0.6 - 0.001)
        self.assertLess(times[0], 0.7)
        self.assertIsNotNone(row["shownMs"])

    def test_floor_counts_from_the_variant_when_shown_late(self) -> None:
        """Shown at 0.4 s: the shown floor (0.4 + 0.4) is later than the launch floor (0.2)."""
        row, times = self.shown_run(0.4, ["--min-launch-ms", "200", "--min-shown-ms", "400", "--max-wait-ms", "2000"])
        self.assertGreaterEqual(row["shownMs"], 400)
        self.assertGreaterEqual(times[1], row["shownMs"] / 1000 + 0.4 - 0.001)
        self.assertGreaterEqual(times[1], 0.8 - 0.001)

    def test_floor_falls_back_to_the_launch_when_never_shown(self) -> None:
        row, times = self.shown_run(None, ["--min-launch-ms", "600", "--min-shown-ms", "5000", "--max-wait-ms", "300"])
        self.assertEqual(row["status"], sc.RECORDED)
        self.assertIsNone(row["shownMs"])
        self.assertIsNone(row["shownAt"])
        self.assertGreaterEqual(times[1], 0.6)
        self.assertLess(times[1], 2.0)  # the 5 s shown floor did not apply

    def test_min_shown_defaults(self) -> None:
        args = sc.parse_args(["record"])
        self.assertEqual((args.min_launch_ms, args.min_shown_ms), (0, 300))


class ParseTests(unittest.TestCase):

    def test_subcommands(self) -> None:
        args = sc.parse_args(["record", "--udid", "A", "--udid", "B", "--only", "Settings", "--appearance", "light",
                              "--source", "ui", "--zip"])
        self.assertEqual((args.command, args.udid, args.only, args.appearance, args.source, args.zip, args.keep_runs),
                         ("record", ["A", "B"], ["Settings"], "light", "ui", True, sc.DEFAULT_KEEP_RUNS))
        args = sc.parse_args(["compare", "r1", "--against", "r0", "--max-diff-percent", "0.1"])
        self.assertEqual((args.run, args.against, args.max_diff_percent), ("r1", "r0", 0.1))
        self.assertTrue(sc.parse_args(["set-baseline", "r1", "--allow-partial"]).allow_partial)
        self.assertEqual(sc.parse_args(["accept", "r1", "Home.default", "dark"]).name, "Home.default")
        self.assertEqual(sc.parse_args(["prune", "--keep", "3"]).keep, 3)
        for bad in (["record", "--source", "disk"], ["accept", "r1", "X", "dusk"], ["--record"], []):
            with self.subTest(argv=bad), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                sc.parse_args(bad)


if __name__ == "__main__":
    unittest.main()
