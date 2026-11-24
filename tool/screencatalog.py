#!/usr/bin/env python3
"""ScreenCatalog: a screenshot catalog of every screen variant an iOS app registers, recorded as
runs and compared as files, without XCUITest.

The app links the ScreenCatalog Swift package and registers named `<Feature>.<variant>` screens.
`record` enumerates them from the installed app (a `-ScreenCatalogList` launch, `GET /variants` on
the package's in-app server), launches each one as `-ScreenCatalogVariant <Feature>.<variant>
-ScreenCatalogPort <port>`, waits for the server and for the app's activity to go quiet, and
screenshots it into a new run folder. Every run is an immutable set of screenshots. One run per simulator type (model and iOS
version) is pinned as that type's baseline; `compare` is a pure file operation between two runs of
one type, never a capture. Docs: docs/reference.md.

Commands:
  record                         capture into <catalog>/runs/<run-id>/ (nothing else is written)
  compare RUN [--against RUN2]   judge RUN against RUN2 (default: the pinned baseline of RUN's type)
  set-baseline RUN               pin RUN as its type's baseline (--allow-partial, --allow-stopped)
  update-baseline RUN            copy a partial RUN's screens over the pinned baseline (a new revision)
  accept RUN NAME APPEARANCE     copy one screen from RUN over the pinned baseline (a new revision)
  delete-run RUN                 delete a run, its thumbnails and every comparison that involves it
                                 (the pinned run too: its type then has no baseline until one is set)
  prune [--keep N]               keep the newest N runs per source and simulator type, never the pin
  export RUN                     zip a run; `import ZIP` unzips one into runs/
  sync push [RUN…] [--all]       push the pinned runs and baselines.json to the store (or those runs; --all: every
                                 finished run); refused when the remote revision is higher (--force)
  sync pull [RUN…] [--list]      pull baselines.json and every pinned run missing or behind here (or those
                                 runs; --list: the remote runs); refused over a higher local revision (--force)
  list                           JSON: every run, the pin per type, and which runs each compares with

The app under test is described by `screencatalog.json` (found walking up from the working directory,
or named with `--config <path>` / $SCREENCATALOG_CONFIG): its bundle id, how to build it, the main
branch, where runs are shared. docs/configuration.md lists every key.

This script never builds (the web UI, screencatalog_ui.py, does). Build a Debug simulator build of
your app and install it first:
  xcodebuild -project <App>.xcodeproj -scheme <Scheme> -configuration Debug \\
    -destination 'platform=iOS Simulator,id=<UDID>' build -quiet
  xcrun simctl install <UDID> <path to the built .app>

Usage:
  python3 tool/screencatalog.py record --udid <UDID> --run-id cli-nightly
  python3 tool/screencatalog.py compare cli-nightly
  python3 tool/screencatalog.py record --udid <UDID-A> --udid <UDID-B> --feature Feed   # two simulators
  python3 tool/screencatalog.py update-baseline cli-20261008-140000

Exit codes: 0 clean; 1 any FAIL or ERROR (or MISSING without --allow-missing) in a compare, any ERROR
or an early stop in a record; 2 usage, setup or a refused operation.
Standard library only (Xcode's python3 is 3.9): PNGs are decoded through ImageIO (ctypes) on macOS, else with
zlib and the PNG filter spec; compare judges pairs in a process pool.
"""

# Xcode ships python3 3.9.6; keep annotations lazy so `X | None` hints parse there.
from __future__ import annotations

import argparse
import fnmatch
import base64
import datetime as dt
import hashlib
import html
import itertools
import json
import os
import plistlib
import re
import shutil
import signal
import uuid
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

# MARK: - Configuration (screencatalog.json)
#
# Everything about the app under test comes from a JSON file, read once at import: the file named by
# $SCREENCATALOG_CONFIG (which `--config <path>` sets, see `apply_config_flag`), else the first
# `screencatalog.json` found walking up from the working directory. Precedence for any one value:
# command-line flag, then environment variable, then the file, then the built-in default. Relative
# paths in the file resolve against the file's folder. docs/configuration.md lists every key.

CONFIG_NAME = "screencatalog.json"
CONFIG_ENV = "SCREENCATALOG_CONFIG"
CATALOG_DIR_ENV = "SCREENCATALOG_DIR"


def find_config(start: Path | None = None, environ=os.environ) -> Path | None:
    explicit = environ.get(CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser().resolve()
    folder = (start or Path.cwd()).resolve()
    for candidate in (folder, *folder.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate / CONFIG_NAME
    return None


def load_config(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        sys.exit(f"screencatalog: cannot read {path}: {error}")
    except ValueError as error:
        sys.exit(f"screencatalog: {path} is not valid JSON: {error}")
    if not isinstance(data, dict):
        sys.exit(f"screencatalog: {path}: expected a JSON object")
    return data


CONFIG_PATH = find_config()
CONFIG = load_config(CONFIG_PATH)
CONFIG_DIR = CONFIG_PATH.parent if CONFIG_PATH else Path.cwd().resolve()


def setting(*keys: str, default=None):
    """`CONFIG[keys[0]][keys[1]]…`, or `default` when any step is missing or null."""
    node = CONFIG
    for key in keys:
        if not isinstance(node, dict) or node.get(key) is None:
            return default
        node = node[key]
    return node


def config_path(value: str | None, default: str | None = None) -> Path | None:
    """A path from the config file, relative to the file's folder."""
    value = value or default
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else CONFIG_DIR / path


def _git_toplevel(folder: Path) -> Path | None:
    try:
        proc = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None


# The repository a run's commit and "dirty" state are read from: the git checkout holding the config file.
REPO_ROOT = _git_toplevel(CONFIG_DIR) or CONFIG_DIR
DEFAULT_CATALOG_DIR = (Path(os.environ[CATALOG_DIR_ENV]).expanduser() if os.environ.get(CATALOG_DIR_ENV)
                       else config_path(setting("catalogDir"), ".screencatalog"))
DEFAULT_EXCLUDE_FILE = config_path(setting("exclusionsFile"), "screencatalog-exclusions.json")
DEFAULT_BUNDLE = setting("app", "bundleId")  # no built-in default: `record` refuses without one
# How the web UI builds the app before a record (`xcodebuild`, run in the config file's folder).
BUILD = {"project": setting("build", "project"), "workspace": setting("build", "workspace"),
         "scheme": setting("build", "scheme"), "configuration": setting("build", "configuration", default="Debug"),
         "extraArgs": list(setting("build", "extraArgs", default=[])),
         "unsetEnv": list(setting("build", "unsetEnv", default=[])),
         "derivedDataPath": setting("build", "derivedDataPath")}
# Tracked files a build rewrites that are not inputs of the app binary (a generated file, say), so a
# change to them does not make the tree "dirty" for a run: the commit still describes what was built.
# `git.buildChurnPaths` in the config; none by default.
BUILD_CHURN_PATHS = tuple(setting("git", "buildChurnPaths", default=()))
GIT_REMOTE = setting("git", "remote", default="origin")
MAIN_BRANCH = setting("git", "mainBranch", default="main")
# Launch arguments and wire protocol of the ScreenCatalog Swift package (docs/protocol.md).
VARIANT_ARGUMENT, LIST_ARGUMENT, PORT_ARGUMENT = "-ScreenCatalogVariant", "-ScreenCatalogList", "-ScreenCatalogPort"
PROTOCOL = 1
DEFAULT_PORT = int(setting("record", "port", default=8766))
STATUS_BAR = {"time": "9:41", "batteryState": "charged", "batteryLevel": 100, "wifiBars": 3, "cellularBars": 4,
              **setting("record", "statusBar", default={})}
SCREENSHOT_MASKS = ("black", "ignored", "alpha")
DEFAULT_SCREENSHOT_MASK = "black"  # see docs/reference.md, Compositor artefacts
# Where screenshots come from: `simctl io screenshot` (the display, SpringBoard's overlays included,
# bistably), or the app drawing its own windows over the in-app server's GET /screenshot.
CAPTURE_MODES = ("simctl", "app")
DEFAULT_CAPTURE_MODE = setting("record", "capture", default="app")  # app capture is byte-stable run to run; simctl is bistable
APPEARANCES = ("light", "dark")
RUNS_DIR = "runs"            # <catalog>/runs/<run-id>/{run.json,manifest.json,<appearance>/<name>.png}
COMPARES_DIR = "compares"    # <catalog>/compares/<run-id>__<against-id>__r<revision>/{compare.json,report.html,diff/}
THUMBS_DIR = ".thumbs"       # <catalog>/.thumbs/runs/<run-id>/…, .thumbs/compares/<key>/… (the web UI's cache)
BASELINES_FILE = "baselines.json"  # {"<deviceDir>": "<run-id>"}: the pinned run per simulator type
RUN_FILE = "run.json"
MANIFEST_FILE = "manifest.json"
COMPARE_FILE = "compare.json"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_KEEP_RUNS = int(setting("record", "keepRuns", default=5))
DEFAULT_CHANNEL_THRESHOLD = int(setting("compare", "channelThreshold", default=8))  # compare --channel-threshold's default: per-channel difference a pixel may have
DEFAULT_MAX_DIFF_PERCENT = float(setting("compare", "maxDiffPercent", default=0.01))  # compare --max-diff-percent's default: differing pixels a screen may have
RUN_SOURCES = ("cli", "ui")  # run.json `source`; prune keeps the newest N of each, per simulator type
HISTORY_KEPT = 100  # a run's history: the newest pin, update and accept events 

PASS, FAIL, MISSING, ERROR, SKIPPED, RECORDED = "PASS", "FAIL", "MISSING", "ERROR", "SKIPPED", "RECORDED"


class CatalogError(Exception):
    """A refused operation (unknown run, type mismatch, the pinned run, …): the CLI prints it and exits 2."""


class Output:
    """Where a run's lines go. The CLI prints them; screencatalog_ui.py subclasses this to
    stream them into its page, and reads `progress` and `rows` instead of parsing lines."""

    def line(self, text: str) -> None:
        print(text, flush=True)

    def error(self, text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    def progress(self, done: int, total: int) -> None:
        """Called with (0, total) when capturing starts and after every finished capture."""

    def rows(self, mode: str, rows: list[dict]) -> None:
        """The final rows: capture rows for `record`, judged rows for `compare`."""

    def phase(self, text: str | None) -> None:
        """What a long operation is doing beyond capturing; None when that is over."""


class LinesOnly(Output):
    """Forwards another output's lines and errors, nothing else."""

    def __init__(self, outer: Output):
        self.outer = outer

    def line(self, text: str) -> None:
        self.outer.line(text)

    def error(self, text: str) -> None:
        self.outer.error(text)


class Quiet(Output):
    def line(self, text: str) -> None:
        pass

    def error(self, text: str) -> None:
        pass


CONSOLE = Output()


# MARK: - PNG (8-bit, non-interlaced; gray, gray+alpha, RGB, RGBA)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}
_COLOR_TYPE = {1: 0, 3: 2, 2: 4, 4: 6}


@dataclass
class Image:
    width: int
    height: int
    channels: int
    data: bytes  # rows of width * channels bytes, no filter bytes

    @property
    def stride(self) -> int:
        return self.width * self.channels


def decode_png(blob: bytes) -> Image:
    """Decodes an 8-bit PNG: through ImageIO on macOS (about 20x faster), else in pure Python. Both
    give the same samples: ImageIO's raw decoded bytes are taken without any colour matching."""
    if blob[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG")
    image = _imageio_decode(blob)
    return image if image is not None else decode_png_python(blob)


def decode_png_python(blob: bytes) -> Image:
    if blob[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG")
    pos, header, idat = 8, None, []
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        kind = blob[pos + 4:pos + 8]
        body = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError("PNG has no IHDR")
    width, height, depth, color_type, _, _, interlace = header
    if depth != 8 or color_type not in _CHANNELS or interlace != 0:
        raise ValueError(f"unsupported PNG: bit depth {depth}, color type {color_type}, interlace {interlace}")
    channels = _CHANNELS[color_type]
    raw = zlib.decompress(b"".join(idat))
    return Image(width, height, channels, _unfilter(raw, width, height, channels))


def encode_png(image: Image, level: int = 6) -> bytes:
    if image.channels not in _COLOR_TYPE:
        raise ValueError(f"cannot encode {image.channels} channels")
    stride = image.stride
    raw = b"".join(b"\x00" + image.data[y * stride:(y + 1) * stride] for y in range(image.height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", image.width, image.height, 8, _COLOR_TYPE[image.channels], 0, 0, 0)
    return PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b"")


_MASKS: dict[int, tuple[int, int]] = {}


def _add_rows(a: bytes, b: bytes) -> bytearray:
    """Bytewise (a + b) mod 256, as one big-int operation instead of a per-byte loop."""
    n = len(a)
    if n not in _MASKS:
        _MASKS[n] = (int.from_bytes(b"\x7f" * n, "big"), int.from_bytes(b"\x80" * n, "big"))
    low, high = _MASKS[n]
    x, y = int.from_bytes(a, "big"), int.from_bytes(b, "big")
    return bytearray((((x & low) + (y & low)) ^ ((x ^ y) & high)).to_bytes(n, "big"))


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytes:
    stride = width * bpp
    if len(raw) < height * (stride + 1):
        raise ValueError("PNG data is truncated")
    out = bytearray(stride * height)
    prev = bytearray(stride)
    mask = (255).__and__
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:  # Sub: a running sum per channel
            for c in range(bpp):
                line[c::bpp] = bytes(map(mask, itertools.accumulate(line[c::bpp])))
        elif kind == 2:  # Up
            line = _add_rows(line, prev)
        elif kind == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:  # Paeth
            for i in range(stride):
                if i >= bpp:
                    a, c = line[i - bpp], prev[i - bpp]
                else:
                    a = c = 0
                b = prev[i]
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    predictor = a
                elif pb <= pc:
                    predictor = b
                else:
                    predictor = c
                line[i] = (line[i] + predictor) & 0xFF
        elif kind != 0:
            raise ValueError(f"bad PNG filter type {kind} on row {y}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return bytes(out)


# MARK: - ImageIO (macOS): the fast decoder

class _ImageIO:
    """CoreFoundation, ImageIO and CoreGraphics through ctypes. The image's data provider hands back
    the decoded PNG samples as stored (no colour matching, no premultiplying); any layout this does
    not take as is (premultiplied or first alpha, little-endian, float, 16-bit, indexed) returns None
    and the pure-Python decoder takes over."""

    def __init__(self) -> None:
        import ctypes
        self.ctypes = ctypes
        frameworks = "/System/Library/Frameworks/{0}.framework/{0}"
        cf = ctypes.CDLL(frameworks.format("CoreFoundation"))
        io = ctypes.CDLL(frameworks.format("ImageIO"))
        cg = ctypes.CDLL(frameworks.format("CoreGraphics"))
        ptr, size = ctypes.c_void_p, ctypes.c_size_t

        def bind(lib, name, restype, *argtypes):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, list(argtypes)
            return fn

        self.data_create = bind(cf, "CFDataCreate", ptr, ptr, ctypes.c_char_p, ctypes.c_long)
        self.data_length = bind(cf, "CFDataGetLength", ctypes.c_long, ptr)
        self.data_bytes = bind(cf, "CFDataGetBytePtr", ptr, ptr)
        self.release = bind(cf, "CFRelease", None, ptr)
        self.source_create = bind(io, "CGImageSourceCreateWithData", ptr, ptr, ptr)
        self.source_status = bind(io, "CGImageSourceGetStatusAtIndex", ctypes.c_int32, ptr, size)
        self.image_create = bind(io, "CGImageSourceCreateImageAtIndex", ptr, ptr, size, ptr)
        self.width = bind(cg, "CGImageGetWidth", size, ptr)
        self.height = bind(cg, "CGImageGetHeight", size, ptr)
        self.bits_per_component = bind(cg, "CGImageGetBitsPerComponent", size, ptr)
        self.bits_per_pixel = bind(cg, "CGImageGetBitsPerPixel", size, ptr)
        self.bytes_per_row = bind(cg, "CGImageGetBytesPerRow", size, ptr)
        self.bitmap_info = bind(cg, "CGImageGetBitmapInfo", ctypes.c_uint32, ptr)
        self.color_space = bind(cg, "CGImageGetColorSpace", ptr, ptr)
        self.model = bind(cg, "CGColorSpaceGetModel", ctypes.c_int32, ptr)
        self.components = bind(cg, "CGColorSpaceGetNumberOfComponents", size, ptr)
        self.provider = bind(cg, "CGImageGetDataProvider", ptr, ptr)
        self.copy_data = bind(cg, "CGDataProviderCopyData", ptr, ptr)

    def decode(self, blob: bytes) -> Image | None:
        data = self.data_create(None, blob, len(blob))
        if not data:
            return None
        source = image = pixels = None
        try:
            source = self.source_create(data, None)
            if not source or self.source_status(source, 0) != 0:  # kCGImageStatusComplete
                return None
            image = self.image_create(source, 0, None)
            if not image:
                return None
            width, height = self.width(image), self.height(image)
            bpc, bpp, row_bytes = self.bits_per_component(image), self.bits_per_pixel(image), self.bytes_per_row(image)
            info = self.bitmap_info(image)
            alpha, byte_order, is_float = info & 0x1F, info & 0x7000, info & 0x100
            space = self.color_space(image)
            model = self.model(space) if space else -1
            colours = self.components(space) if space else 0
            # model 0 monochrome, 1 RGB; alpha 0 none, 3 last, 5 none-skip-last; order 0 default, 0x3000/0x4000 big
            if (bpc != 8 or is_float or byte_order not in (0, 0x3000, 0x4000) or model not in (0, 1)
                    or alpha not in (0, 3, 5) or bpp % 8):
                return None
            stored = bpp // 8
            channels = colours + (1 if alpha == 3 else 0)
            if stored != channels + (1 if alpha == 5 else 0) or channels not in (1, 2, 3, 4):
                return None
            pixels = self.copy_data(self.provider(image))
            if not pixels or self.data_length(pixels) < row_bytes * height:
                return None
            raw = self.ctypes.string_at(self.data_bytes(pixels), row_bytes * height)
        finally:
            for ref in (pixels, image, source, data):
                if ref:
                    self.release(ref)
        stride = width * stored
        if row_bytes != stride:
            raw = b"".join(raw[y * row_bytes:y * row_bytes + stride] for y in range(height))
        if alpha == 5:  # padded RGB(X): keep the colour bytes, as the PNG stored them
            out = bytearray(width * height * channels)
            for c in range(channels):
                out[c::channels] = raw[c::stored]
            raw = bytes(out)
        return Image(width, height, channels, raw)


_IMAGEIO: list = []  # [] not tried yet, [None] unavailable, [_ImageIO] loaded


def _imageio_decode(blob: bytes) -> Image | None:
    """The PNG through ImageIO, or None (not macOS, the frameworks did not load, a layout it does not take)."""
    if not _IMAGEIO:
        loaded = None
        if sys.platform == "darwin" and not os.environ.get("SCREENCATALOG_PURE_PYTHON"):
            try:
                loaded = _ImageIO()
            except (OSError, AttributeError):
                loaded = None
        _IMAGEIO.append(loaded)
    if _IMAGEIO[0] is None:
        return None
    try:
        return _IMAGEIO[0].decode(blob)
    except Exception:  # noqa: BLE001 - any ctypes failure falls back to the pure-Python decoder
        return None


def to_rgb(image: Image) -> Image:
    """Drops alpha and expands gray, so two screenshots compare on colour alone."""
    if image.channels == 3:
        return image
    data = image.data
    if image.channels == 4:
        rgb = bytearray(image.width * image.height * 3)
        for c in range(3):
            rgb[c::3] = data[c::4]
        return Image(image.width, image.height, 3, bytes(rgb))
    gray = data[::image.channels]
    rgb = bytearray(len(gray) * 3)
    for c in range(3):
        rgb[c::3] = gray
    return Image(image.width, image.height, 3, bytes(rgb))


# MARK: - Diff

@dataclass
class Diff:
    differing: int
    total: int
    dimensions_match: bool = True
    rows: list[int] = field(default_factory=list)  # the rows whose bytes differ, top to bottom
    mask: bytes = b""  # per pixel of those rows, joined: 1 when it differs beyond the threshold, else 0
    width: int = 0

    @property
    def percent(self) -> float:
        return 100.0 * self.differing / self.total if self.total else 0.0

    @property
    def pixels(self) -> list[int]:
        """The indices of the differing pixels, in order."""
        out = []
        for i, y in enumerate(self.rows):
            line = self.mask[i * self.width:(i + 1) * self.width]
            base, x = y * self.width, line.find(1)
            while x >= 0:
                out.append(base + x)
                x = line.find(1, x + 1)
        return out


_FLAG_BIT = bytes(1 if v & 0x40 else 0 for v in range(256))
_FULL_BYTE = bytes(0xFF if v else 0 for v in range(256))


def _channel_flags(a: bytes, b: bytes, channel_threshold: int) -> bytes:
    """Per byte of two equal-length buffers: 1 when |a - b| > channel_threshold, else 0. Each byte
    is widened into a 16-bit lane of one big integer, so the whole buffer takes a few big-int adds:
    a lane holds 0xBFFF - t + a - b, which never leaves 0x8000..0xFFFF (no carry or borrow crosses
    lanes) and has bit 14 set exactly when a - b > t; the mirror sum covers b - a > t."""
    n = len(a)
    if n == 0:
        return b""
    wide = bytearray(2 * n)
    wide[1::2] = a
    x = int.from_bytes(wide, "big")
    wide[1::2] = b
    y = int.from_bytes(wide, "big")
    k = int.from_bytes((0xBFFF - channel_threshold).to_bytes(2, "big") * n, "big")
    either = (x + k - y) | (y + k - x)
    return either.to_bytes(2 * n, "big")[0::2].translate(_FLAG_BIT)


def diff_images(baseline: Image, current: Image, channel_threshold: int) -> Diff:
    """A pixel differs when any colour channel moved by more than `channel_threshold` (alpha is
    ignored). Rows are compared as bytes first; only the rows that differ are judged per pixel."""
    if (baseline.width, baseline.height) != (current.width, current.height):
        return Diff(0, baseline.width * baseline.height, dimensions_match=False)
    if baseline.channels == current.channels and baseline.channels in (3, 4):
        a, b = baseline, current
    else:
        a, b = to_rgb(baseline), to_rgb(current)
    width, channels, total = a.width, a.channels, a.width * a.height
    if a.data == b.data:
        return Diff(0, total, width=width)
    stride, data_a, data_b = a.stride, a.data, b.data
    rows = [y for y in range(a.height)
            if data_a[y * stride:(y + 1) * stride] != data_b[y * stride:(y + 1) * stride]]
    joined_a = b"".join(data_a[y * stride:(y + 1) * stride] for y in rows)
    joined_b = b"".join(data_b[y * stride:(y + 1) * stride] for y in rows)
    flags = _channel_flags(joined_a, joined_b, channel_threshold)
    count = len(rows) * width
    mask = 0
    for c in range(3):  # a pixel differs when any of R, G, B does
        mask |= int.from_bytes(flags[c::channels], "big")
    mask_bytes = mask.to_bytes(count, "big")
    return Diff(count - mask_bytes.count(0), total, rows=rows, mask=mask_bytes, width=width)


_DIM = bytes(v // 3 for v in range(256))


def diff_image(baseline: Image, diff: Diff) -> Image:
    """The baseline dimmed to a third, with every differing pixel painted pure red."""
    rgb = to_rgb(baseline)
    out = bytearray(rgb.data.translate(_DIM))
    if diff.differing:
        stride = rgb.stride
        wide = bytearray(len(diff.mask) * 3)
        for c in range(3):
            wide[c::3] = diff.mask
        red = int.from_bytes(wide.translate(_FULL_BYTE), "big")  # 0xFF on every byte of a differing pixel
        dim = int.from_bytes(b"".join(bytes(out[y * stride:(y + 1) * stride]) for y in diff.rows), "big")
        paint = int.from_bytes(b"\xff\x00\x00" * len(diff.mask), "big")
        keep = int.from_bytes(wide.translate(bytes(0 if v else 0xFF for v in range(256))), "big")
        painted = ((dim & keep) | (paint & red)).to_bytes(len(wide), "big")
        for i, y in enumerate(diff.rows):
            out[y * stride:(y + 1) * stride] = painted[i * stride:(i + 1) * stride]
    return Image(rgb.width, rgb.height, 3, bytes(out))


def judge(diff: Diff, max_diff_percent: float) -> tuple[str, str]:
    if not diff.dimensions_match:
        return FAIL, "dimensions differ"
    if diff.percent > max_diff_percent:
        return FAIL, f"{diff.differing} px differ ({diff.percent:.4f}% > {max_diff_percent}%)"
    return PASS, f"{diff.differing} px differ ({diff.percent:.4f}%)"


def compare_files(baseline: Path, current: Path, channel_threshold: int, max_diff_percent: float
                  ) -> tuple[str, str, Diff | None, Image | None]:
    blob_a, blob_b = baseline.read_bytes(), current.read_bytes()
    if blob_a == blob_b:  # identical bytes: skip the decode
        return PASS, "identical", None, None
    image_a, image_b = decode_png(blob_a), decode_png(blob_b)
    result = diff_images(image_a, image_b, channel_threshold)
    status, reason = judge(result, max_diff_percent)
    if not result.dimensions_match:
        reason = f"dimensions differ: baseline {image_a.width}x{image_a.height}, current {image_b.width}x{image_b.height}"
    return status, reason, result, image_a


def judge_pair(baseline: str, current: str, channel_threshold: int, max_diff_percent: float,
               diff_target: str | None) -> tuple[str, str, float | None, bool]:
    """One pair, as a pool worker runs it: (status, reason, diff %, whether the diff PNG was written
    to `diff_target`). The diff PNG is written only for a FAIL of two images of one size."""
    try:
        status, reason, result, baseline_image = compare_files(Path(baseline), Path(current), channel_threshold,
                                                               max_diff_percent)
    except (ValueError, zlib.error, OSError, struct.error) as error:  # struct.error: a truncated chunk walk
        return ERROR, f"cannot decode: {error}", None, False
    wrote = False
    if (diff_target and status == FAIL and result is not None and result.dimensions_match
            and baseline_image is not None):
        target = Path(diff_target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encode_png(diff_image(baseline_image, result)))
        wrote = True
    return status, reason, round(result.percent, 5) if result is not None else None, wrote


def same_bytes(a: Path, b: Path) -> bool:
    """Byte-identical files, settled by their sizes first: the common unchanged screen never decodes."""
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


# MARK: - Compositor bands

# The simulator composites two system overlays into some screenshots and not others, whatever the
# app draws: the Dynamic Island cut-out at the top while something on screen animates (on an iPhone
# 17 at 3x: rows 41-152, x 414-791, 39,456 px), and the home indicator at the bottom until it fades
# out after launch (rows 2583-2597 of 2622, x 387-818, 6,433 px). The bands are fractions of the
# image height, so they scale with the device, and span the full width: the top 7 % holds the island
# and the status bar (184 rows of 2622, about the 62 pt top safe area), the bottom 3 % the indicator
# (79 rows of 2622).
BAND_TOP_FRACTION = 0.07
BAND_BOTTOM_FRACTION = 0.03


def band_rows(height: int) -> tuple[range, range]:
    """(top, bottom) row ranges of the compositor bands of an image `height` rows tall."""
    top = max(1, round(height * BAND_TOP_FRACTION))
    bottom = max(1, round(height * BAND_BOTTOM_FRACTION))
    return range(0, min(top, height)), range(max(0, height - bottom), height)


def _png_pixels(blob: bytes) -> bytes:
    """The decompressed IDAT stream: equal streams mean equal pixels, without unfiltering."""
    if blob[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG")
    pos, header, idat = 8, b"", []
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        kind = blob[pos + 4:pos + 8]
        if kind == b"IHDR":
            header = blob[pos + 8:pos + 8 + length]
        elif kind == b"IDAT":
            idat.append(blob[pos + 8:pos + 8 + length])
        elif kind == b"IEND":
            break
        pos += 12 + length
    return header + zlib.decompress(b"".join(idat))


def differing_bands(first: bytes, second: bytes) -> list[str]:
    """Which compositor bands differ between two screenshots (PNG bytes): [] when both match,
    else any of "top" and "bottom" ("size" when the dimensions differ). Pixels outside the bands
    are ignored, so a screen whose content animates can still be stable here. Identical pixel
    streams, the usual case, are settled without decoding (a few ms instead of ~0.5 s per image)."""
    if first == second or _png_pixels(first) == _png_pixels(second):
        return []
    a, b = to_rgb(decode_png(first)), to_rgb(decode_png(second))
    if (a.width, a.height) != (b.width, b.height):
        return ["size"]
    stride = a.stride
    found = []
    for label, rows in zip(("top", "bottom"), band_rows(a.height)):
        span = slice(rows.start * stride, rows.stop * stride)
        if a.data[span] != b.data[span]:
            found.append(label)
    return found


def stable_screenshot(take, target: Path, retakes: int, interval_s: float, sleep=time.sleep
                      ) -> tuple[str | None, int, bool]:
    """Screenshots into `target` until two in a row, `interval_s` apart, have the same compositor
    bands, and keeps the second of that pair. When they differ, waits and takes another, up to
    `retakes` more; then keeps the last with unstable bands. `take(path)` writes one screenshot and
    returns None or why it failed. Returns (error, screenshots taken, bands unstable)."""
    shots = [target.with_name(target.stem + ".a.png"), target.with_name(target.stem + ".b.png")]
    for each in shots:
        each.unlink(missing_ok=True)
    taken, previous = 0, None
    try:
        for attempt in range(retakes + 2):
            if attempt:
                sleep(interval_s)
            path = shots[attempt % 2]
            problem = take(path)
            taken += 1
            if problem is None and not path.exists():
                problem = "no file written"
            if problem is not None:
                return problem, taken, False
            blob = path.read_bytes()
            if previous is not None and not differing_bands(previous, blob):
                os.replace(path, target)
                return None, taken, False
            previous = blob
        os.replace(shots[(retakes + 1) % 2], target)
        return None, taken, True
    finally:
        for each in shots:
            each.unlink(missing_ok=True)


# MARK: - Naming, filtering

def sanitize(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "_", text).strip("_")


def device_dir_name(model: str, os_name: str) -> str:
    """`iPhone 17` + `iOS 27.0` → `iPhone_17_iOS_27.0`: the simulator type's key."""
    return f"{sanitize(model)}_{sanitize(os_name)}"


def zip_name(run_id: str) -> str:
    return f"screencatalog_{run_id}.zip"


def shot_relpath(appearance: str, name: str) -> str:
    """A screen's path inside its run folder: `<appearance>/<name>.png`."""
    return f"{appearance}/{sanitize(name)}.png"


def feature_of(name: str) -> str:
    return name.split(".", 1)[0]


def load_exclusions(path: Path) -> dict[str, str]:
    """name → reason. An exclusion without a reason is an error: it is how a flake gets buried."""
    if not path.exists():
        return {}
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected a JSON list of {{name, reason}}")
    out: dict[str, str] = {}
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path}: entry without a name: {entry!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{path}: exclusion '{name}' has no reason")
        out[name] = reason.strip()
    return out


def select_variants(entries: list[dict], only: list[str], features: list[str], exclusions: dict[str, str]
                    ) -> tuple[list[dict], list[dict]]:
    """(to capture, skipped). `--only` matches a substring (an exact name included); `--feature`
    matches the feature exactly; both lists OR within themselves and AND with each other."""
    picked = [e for e in entries
              if (not only or any(o in e["name"] for o in only))
              and (not features or e["feature"] in features)]
    keep = [e for e in picked if e["name"] not in exclusions]
    skipped = [dict(e, reason=exclusions[e["name"]]) for e in picked if e["name"] in exclusions]
    return keep, skipped


def variant_record(name: str, appearance: str, status: str, *, settle_ms: int | None = None,
                   settled: bool | None = None, duration_ms: int | None = None, error: str | None = None,
                   udid: str | None = None, shots: int | None = None, unstable_bands: bool | None = None,
                   shown_ms: int | None = None, shown_at: str | None = None, capture_mode: str | None = None) -> dict:
    """One manifest row: a captured screen (RECORDED), a failed capture (ERROR) or an excluded one (SKIPPED).
    A capture adds `shots` (screenshots taken) and `unstableBands` (its compositor bands never held
    still for two screenshots in a row; see `stable_screenshot`). `shownMs` is the time from launch to
    the variant being reported as shown and `shownAt` that moment (ISO); both are None when it never
    was within `--max-wait-ms`, or the variant was not captured. `captureMode` (see CAPTURE_MODES)
    is on every row a capture attempted."""
    extra = {} if shots is None else {"shots": shots, "unstableBands": bool(unstable_bands)}
    if capture_mode is not None:
        extra["captureMode"] = capture_mode
    return {
        "name": name,
        "feature": feature_of(name),
        "appearance": appearance,
        "status": status,
        "file": shot_relpath(appearance, name),
        "settleMs": settle_ms,
        "settled": settled,
        "durationMs": duration_ms,
        "error": error,
        "udid": udid,
        "shownMs": shown_ms,
        "shownAt": shown_at,
        **extra,
    }


def merge_manifest_variants(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """`fresh` rows replace the rows of the same (name, appearance); the rest are kept."""
    fresh_keys = {(v["name"], v["appearance"]) for v in fresh}
    kept = [v for v in existing if (v["name"], v["appearance"]) not in fresh_keys]
    return sorted(kept + fresh, key=lambda v: (v["appearance"], v["name"]))


def iso(timestamp: float | None = None) -> str:
    moment = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc) if timestamp else dt.datetime.now(dt.timezone.utc)
    return moment.replace(microsecond=0).isoformat()


def filter_label(only: list[str], features: list[str]) -> str:
    """`--only` and `--feature` as one short label; "all" when neither narrows the run."""
    parts = [", ".join(only)] if only else []
    if features:
        parts.append("feature " + ", ".join(features))
    return " · ".join(parts) or "all"


def append_history(run: dict, entry: dict) -> None:
    run["history"] = (list(run.get("history") or []) + [entry])[-HISTORY_KEPT:]


def totals(rows: list[dict]) -> dict[str, dict[str, int]]:
    """{appearance: {status: count}}."""
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = out.setdefault(row["appearance"], {})
        bucket[row["status"]] = bucket.get(row["status"], 0) + 1
    return out


def screen_counts(rows: list[dict]) -> dict[str, int]:
    """Screens with a screenshot per appearance: run.json `counts`."""
    out = {a: 0 for a in sorted({r["appearance"] for r in rows})}
    for row in rows:
        if row["status"] == RECORDED:
            out[row["appearance"]] += 1
    return out


def status_counts(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row["status"]] = out.get(row["status"], 0) + 1
    return out


# MARK: - JSON files

def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path: Path, data) -> None:
    """Atomic: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    temp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temp, path)


# MARK: - Runs (runs/<run-id>/)

def valid_run_id(run_id) -> bool:
    """Letters, digits, '.', '_' and '-', no '..' and no '__' (the compare key's separator)."""
    return (isinstance(run_id, str) and bool(RUN_ID_RE.match(run_id)) and ".." not in run_id
            and "__" not in run_id)


def run_root(catalog: Path, run_id: str) -> Path:
    if not valid_run_id(run_id):
        raise ValueError(f"bad run id {run_id!r}: letters, digits, '.', '_' and '-' only, no '..' or '__'")
    return catalog / RUNS_DIR / run_id


def new_run_id(catalog: Path, source: str = "cli") -> str:
    """`<source>-<local yyyymmdd-hhmmss>`, with `-2`, `-3`… when that folder exists already."""
    base = f"{source}-{time.strftime('%Y%m%d-%H%M%S')}"
    candidate, n = base, 2
    while (catalog / RUNS_DIR / candidate).exists():
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def read_run(path: Path) -> dict | None:
    """A run folder's run.json, or None when the folder is not a run."""
    data = read_json(path / RUN_FILE)
    return data if isinstance(data, dict) else None


def write_run(path: Path, **fields) -> dict:
    data = read_run(path) or {}
    data.update(fields)
    write_json(path / RUN_FILE, data)
    return data


def read_rows(path: Path) -> list[dict]:
    """A run folder's manifest rows."""
    data = read_json(path / MANIFEST_FILE)
    rows = data.get("variants") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def write_rows(path: Path, run_id: str, rows: list[dict]) -> None:
    write_json(path / MANIFEST_FILE, {"schema": 2, "run": run_id,
                                      "variants": sorted(rows, key=lambda v: (v["appearance"], v["name"]))})


def load_run(catalog: Path, run_id: str) -> dict:
    """run.json of `run_id`; CatalogError when there is no such run."""
    if not valid_run_id(run_id):
        raise CatalogError(f"bad run id {run_id!r}")
    data = read_run(catalog / RUNS_DIR / run_id)
    if data is None:
        raise CatalogError(f"no run {run_id} in {catalog / RUNS_DIR}")
    return data


def process_alive(pid: int | None) -> bool:
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_is_active(run: dict) -> bool:
    """A run still capturing: marked active by a process that is still alive."""
    return bool(run.get("active")) and run.get("pid") is not None and process_alive(run.get("pid"))


def run_is_complete(run: dict) -> bool:
    """Capturing ended normally (`finished` set). A stopped run is complete but `stopped`; an
    aborted one (the capture raised) never gets `finished`."""
    return bool(run.get("finished")) and not run_is_active(run)


def run_is_stopped(run: dict) -> bool:
    return bool(run.get("stopped") or run.get("aborted")) or (not run.get("finished") and not run_is_active(run))


def run_is_partial(run: dict) -> bool:
    """Filtered (`--only` / `--feature`) or one appearance only: not every screen of the type."""
    found = run.get("filter") or {}
    return bool(found.get("only") or found.get("feature")) or run.get("appearance", "both") != "both"


def run_source(run: dict) -> str:
    return run.get("source") or "cli"


def run_device_dir(run: dict) -> str | None:
    return (run.get("deviceType") or {}).get("deviceDir")


def run_type(run: dict) -> tuple[str | None, str | None]:
    kind = run.get("deviceType") or {}
    return kind.get("model"), kind.get("os")


def type_label(run: dict) -> str:
    model, os_name = run_type(run)
    return f"{model or '?'}, {os_name or '?'}"


def run_revision(run: dict) -> int:
    revision = run.get("revision")
    return revision if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1 else 1


def bump_revision(run: dict) -> int:
    """Adds 1 to `run`'s revision and sets `revisionAt` to now; returns the new revision."""
    revision = run_revision(run) + 1
    run["revision"], run["revisionAt"] = revision, iso()
    return revision


def run_order_key(item: tuple[str, dict]):
    run_id, data = item
    # `started` has whole seconds; `startedEpoch` orders two runs started in the same second.
    return (data.get("started") or "", data.get("startedEpoch") or 0, run_id)


def iter_runs(catalog: Path) -> list[tuple[str, dict]]:
    """(run id, run.json) of every run folder, newest first."""
    runs = []
    root = catalog / RUNS_DIR
    if root.is_dir():
        for path in root.iterdir():
            if path.is_dir() and not path.is_symlink() and valid_run_id(path.name):
                data = read_run(path)
                if data is not None:
                    runs.append((path.name, data))
    return sorted(runs, key=run_order_key, reverse=True)


# MARK: - Baselines (baselines.json)

def read_baselines(catalog: Path) -> dict[str, str]:
    data = read_json(catalog / BASELINES_FILE)
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and valid_run_id(v)}


def write_baselines(catalog: Path, pins: dict[str, str]) -> None:
    write_json(catalog / BASELINES_FILE, dict(sorted(pins.items())))


def pinned(catalog: Path, device_dir: str | None) -> str | None:
    """The run pinned as `device_dir`'s baseline, when it exists."""
    run_id = read_baselines(catalog).get(device_dir or "")
    return run_id if run_id and read_run(catalog / RUNS_DIR / run_id) is not None else None


def same_type(a: dict, b: dict) -> bool:
    return run_type(a) == run_type(b) and None not in run_type(a)


def capture_mode(run: dict) -> str:
    """How the run's screenshots were taken; runs from before the flag were all `simctl`."""
    return run.get("captureMode") or "simctl"


def same_kind(a: dict, b: dict) -> bool:
    """Whether two runs can be compared: one simulator type, one capture mode."""
    return same_type(a, b) and capture_mode(a) == capture_mode(b)


def comparable(catalog: Path, run_id: str) -> list[str]:
    """The other runs of `run_id`'s simulator type (model and iOS version) and capture mode, newest
    first; runs still capturing are left out."""
    run = load_run(catalog, run_id)
    return [other for other, data in iter_runs(catalog)
            if other != run_id and same_kind(run, data) and not run_is_active(data)]


def describe_run(catalog: Path, run_id: str, data: dict, pins: dict[str, str] | None = None) -> dict:
    """run.json plus what is derived: id, complete, partial, stopped, active, pinned."""
    pins = read_baselines(catalog) if pins is None else pins
    return dict(data, id=run_id, complete=run_is_complete(data), partial=run_is_partial(data),
                stopped=run_is_stopped(data), active=run_is_active(data),
                pinned=pins.get(run_device_dir(data) or "") == run_id, revision=run_revision(data))


def list_runs(catalog: Path) -> list[dict]:
    """Every run, newest first, described (see describe_run)."""
    pins = read_baselines(catalog)
    return [describe_run(catalog, run_id, data, pins) for run_id, data in iter_runs(catalog)]


# MARK: - Compare (compares/<run-id>__<against-id>__r<revision>/)

def compare_key(run_id: str, against_id: str, revision: int) -> str:
    return f"{run_id}__{against_id}__r{revision}"


def run_stamp(catalog: Path, run_id: str, run: dict) -> dict:
    """What a comparison depends on in one run: its revision and its manifest's modification time.
    Accepting, updating or re-capturing changes one of them, and the comparison is recomputed."""
    try:
        mtime = (catalog / RUNS_DIR / run_id / MANIFEST_FILE).stat().st_mtime_ns
    except OSError:
        mtime = None
    return {"revision": run_revision(run), "manifestMtimeNs": mtime}


def exclusions_digest(exclusions: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(exclusions.items())).encode()).hexdigest()[:12]


def iter_compares(catalog: Path) -> list[tuple[str, dict | None]]:
    """(key, compare.json or None for a folder without one) for every compare folder."""
    root = catalog / COMPARES_DIR
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and not path.name.startswith("."):
            data = read_json(path / COMPARE_FILE)
            out.append((path.name, data if isinstance(data, dict) else None))
    return out


def remove_compare(catalog: Path, key: str) -> None:
    shutil.rmtree(catalog / COMPARES_DIR / key, ignore_errors=True)
    shutil.rmtree(catalog / THUMBS_DIR / COMPARES_DIR / key, ignore_errors=True)


def key_pair(key: str, data: dict | None) -> tuple[str | None, str | None, int | None]:
    """(run, against, revision) of a compare folder: from its compare.json, else parsed from the key."""
    if data and data.get("run") and data.get("against"):
        return data["run"], data["against"], data.get("againstRevision")
    parts = key.split("__")
    if len(parts) == 3 and re.fullmatch(r"r\d+", parts[2]):
        return parts[0], parts[1], int(parts[2][1:])
    return None, None, None


def check_comparable(run_id: str, run: dict, against_id: str, against: dict) -> list[str]:
    """Notes for a comparison of two runs of one type; CatalogError when the types differ."""
    if not same_type(run, against):
        raise CatalogError(f"cannot compare {run_id} ({type_label(run)}) with {against_id} ({type_label(against)}): "
                           "runs compare only within one simulator type, the same model and iOS version")
    if capture_mode(run) != capture_mode(against):
        raise CatalogError(f"cannot compare {run_id} (captureMode {capture_mode(run)}) with {against_id} "
                           f"(captureMode {capture_mode(against)}): simctl screenshots carry the status bar and "
                           "SpringBoard's overlays, app screenshots only the app's windows, so every screen would "
                           "differ; record both runs with the same --capture")
    build_a = (run.get("deviceType") or {}).get("osBuild")
    build_b = (against.get("deviceType") or {}).get("osBuild")
    if build_a and build_b and build_a != build_b:
        return [f"iOS build differs: {run_id} {build_a}, {against_id} {build_b}"]
    return []


def missing_reason(name: str, run_id: str) -> str:
    """A MISSING row's reason: captured in the run, but the run it is compared with has no screenshot."""
    return f"no screenshot of {name} in {run_id}"


def judge_rows(catalog: Path, run_id: str, run_rows: list[dict], against_id: str, against_rows: list[dict],
               key: str, stage: Path, channel_threshold: int, max_diff_percent: float,
               exclusions: dict[str, str], output: Output | None = None, pool=None,
               workers: int | None = None, stop: threading.Event | None = None) -> list[dict]:
    """Each of the run's rows as PASS / FAIL / MISSING / ERROR / SKIPPED against the other run. Only
    the screens the run has are judged, so a partial run against a full one judges its subset. Diff
    images go into `stage/diff/`; row paths are relative to the catalog, as they will be once the
    stage is renamed to compares/<key>/. Pairs that are not byte-identical are judged in `pool` (an
    executor), else in a process pool of `workers` (default min(4, cpus)) made for this call when there
    are POOL_MIN_PAIRS or more, else in-process; rows keep the run's order either way. `output.progress`
    hears (pairs done, pairs total); a set `stop` cancels: CompareCancelled once the running pairs end."""
    run_path, against_path = catalog / RUNS_DIR / run_id, catalog / RUNS_DIR / against_id
    references = {(r["name"], r["appearance"]): r for r in against_rows if r.get("status") == RECORDED}
    rows, jobs, identical = [], [], 0
    for record in run_rows:
        name, appearance = record["name"], record["appearance"]
        row = {k: record.get(k) for k in ("name", "feature", "appearance", "status", "error", "udid")}
        if record.get("unstableBands"):
            row["unstableBands"] = True
        row.update(feature=row["feature"] or feature_of(name), current=None, baseline=None, diff=None,
                   diffPercent=None, note=None)
        reference = references.get((name, appearance))
        reference_file = against_path / reference["file"] if reference else None
        if reference_file is not None and reference_file.is_file():
            row["baseline"] = f"{RUNS_DIR}/{against_id}/{reference['file']}"
        else:
            reference_file = None
        if name in exclusions or record.get("status") == SKIPPED:
            row.update(status=SKIPPED, error=exclusions.get(name) or record.get("error"))
            rows.append(row)
            continue
        if record.get("status") != RECORDED:
            row.update(status=ERROR, error=record.get("error") or f"not captured ({record.get('status')})")
            rows.append(row)
            continue
        current_file = run_path / record["file"]
        row["current"] = f"{RUNS_DIR}/{run_id}/{record['file']}"
        if not current_file.is_file():
            row.update(status=ERROR, error="no screenshot file")
        elif reference_file is None:
            row.update(status=MISSING, error=missing_reason(name, against_id))
        elif same_bytes(reference_file, current_file):  # identical bytes: no decode
            row.update(status=PASS, error=None, note="identical")
            identical += 1
        else:
            relative = f"diff/{shot_relpath(appearance, name)}"
            jobs.append((len(rows), relative, (str(reference_file), str(current_file), channel_threshold,
                                               max_diff_percent, str(stage / relative))))
        rows.append(row)
    _judge_jobs(rows, jobs, identical, key, output or Quiet(), pool, workers, stop)
    return rows


POOL_MIN_PAIRS = 4  # fewer pairs to decode than this are judged in-process: a pool costs ~0.3 s to start


class CompareCancelled(CatalogError):
    """A comparison stopped by its `stop` event; its stage folder is removed."""


def _judge_jobs(rows: list[dict], jobs: list[tuple], identical: int, key: str, output: Output, pool,
                workers: int | None, stop: threading.Event | None) -> None:
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
    from concurrent.futures.process import BrokenProcessPool

    total = identical + len(jobs)
    done = identical
    output.progress(done, total)

    def apply(index: int, relative: str, outcome: tuple) -> None:
        status, reason, percent, wrote = outcome
        rows[index].update(status=status, error=reason if status != PASS else None, note=reason, diffPercent=percent)
        if wrote:
            rows[index]["diff"] = f"{COMPARES_DIR}/{key}/{relative}"

    def cancelled() -> bool:
        return stop is not None and stop.is_set()

    workers = min(4, os.cpu_count() or 1) if workers is None else workers
    own = None
    if pool is None and workers > 1 and len(jobs) >= POOL_MIN_PAIRS:
        own = pool = ProcessPoolExecutor(min(workers, len(jobs)))
    try:
        if pool is None:
            for index, relative, args in jobs:
                if cancelled():
                    raise CompareCancelled(f"compare {key}: cancelled")
                apply(index, relative, judge_pair(*args))
                done += 1
                output.progress(done, total)
            return
        pending = {}
        for job in jobs:
            try:
                pending[pool.submit(judge_pair, *job[2])] = job
            except (BrokenProcessPool, RuntimeError):  # a broken or shut pool: judge it here
                apply(job[0], job[1], judge_pair(*job[2]))
                done += 1
        while pending:
            finished, _ = wait(list(pending), timeout=0.25, return_when=FIRST_COMPLETED)
            if cancelled():
                for future in pending:
                    future.cancel()
                wait(list(pending), timeout=20)  # the running pairs end before the stage folder is removed
                raise CompareCancelled(f"compare {key}: cancelled")
            for future in finished:
                index, relative, args = pending.pop(future)
                try:
                    outcome = future.result()
                except BrokenProcessPool:
                    outcome = judge_pair(*args)
                apply(index, relative, outcome)
                done += 1
            if finished:
                output.progress(done, total)
    finally:
        if own is not None:
            own.shutdown(wait=True, cancel_futures=True)


def verdict(rows: list[dict], allow_missing: bool = False, reviews: dict | None = None) -> int:
    """1 on any ERROR, FAIL or MISSING (`allow_missing` lets MISSING pass); with `reviews` (`--allow-confirmed`),
    a FAIL or MISSING a reviewer has confirmed passes too."""
    for row in rows:
        status = row["status"]
        if status == ERROR:
            return 1
        if status in REVIEWABLE and reviews is not None:
            review = reviews.get((row["name"], row["appearance"]))
            if review and review["state"] == "confirmed":
                continue
        if status == FAIL or (status == MISSING and not allow_missing):
            return 1
    return 0


def current_compare(catalog: Path, run_id: str, against_id: str | None = None, *, channel_threshold: int | None = None,
                    max_diff_percent: float | None = None) -> dict | None:
    """The stored comparison of `run_id` with `against_id` (default: the pinned baseline of its type)
    when it is still current: keyed by the other run's revision and with both runs unchanged since.
    With a tolerance or threshold given, it must also have been judged with them. None otherwise."""
    try:
        run = load_run(catalog, run_id)
        against_id = against_id or pinned(catalog, run_device_dir(run))
        if not against_id or against_id == run_id:
            return None
        against = load_run(catalog, against_id)
    except CatalogError:
        return None
    key = compare_key(run_id, against_id, run_revision(against))
    data = read_json(catalog / COMPARES_DIR / key / COMPARE_FILE)
    if not isinstance(data, dict):
        return None
    if data.get("stamps") != {"run": run_stamp(catalog, run_id, run), "against": run_stamp(catalog, against_id, against)}:
        return None
    settings = data.get("settings") or {}
    if channel_threshold is not None and settings.get("channelThreshold") != channel_threshold:
        return None
    if max_diff_percent is not None and settings.get("maxDiffPercent") != max_diff_percent:
        return None
    return data


def compare_runs(catalog: Path, run_id: str, against_id: str | None = None, output: Output = CONSOLE, *,
                 channel_threshold: int = DEFAULT_CHANNEL_THRESHOLD, max_diff_percent: float = DEFAULT_MAX_DIFF_PERCENT,
                 exclusions: dict[str, str] | None = None, force: bool = False, report: bool = False,
                 pool=None, workers: int | None = None, stop: threading.Event | None = None) -> dict:
    """Compares run `run_id` with `against_id` (default: the pinned baseline of its type) and returns
    the comparison (compare.json, plus `reused`). Never captures. `report` also writes report.html
    (self-contained, every FAIL's three PNGs embedded: about 1 MB per FAIL). Reuses compares/<key>/ when both runs
    are unchanged since it was written and the settings match; otherwise judges every screen again and
    replaces it. Older keys of the same pair (an earlier revision of the other run) are removed.
    Pairs judge in parallel (`pool`, `workers`: see judge_rows), `output.progress` hears pairs done of
    total, and a set `stop` raises CompareCancelled with nothing left behind (the stage is removed).
    CatalogError for an unknown run, a run still capturing, no pinned baseline, or two types."""
    exclusions = exclusions or {}
    run = load_run(catalog, run_id)
    if against_id is None:
        against_id = pinned(catalog, run_device_dir(run))
        if against_id is None:
            raise CatalogError(f"no baseline is pinned for {type_label(run)}: pin a run with set-baseline, "
                               f"or pass --against <run>")
        if against_id == run_id:
            raise CatalogError(f"{run_id} is the pinned baseline of {type_label(run)}: pass --against <run>")
    elif against_id == run_id:
        raise CatalogError(f"{run_id} cannot be compared with itself")
    against = load_run(catalog, against_id)
    for each_id, each in ((run_id, run), (against_id, against)):
        if run_is_active(each):
            raise CatalogError(f"run {each_id} is still capturing")
    notes = check_comparable(run_id, run, against_id, against)
    revision = run_revision(against)
    key = compare_key(run_id, against_id, revision)
    settings = {"channelThreshold": channel_threshold, "maxDiffPercent": max_diff_percent,
                "exclusions": exclusions_digest(exclusions)}
    stamps = {"run": run_stamp(catalog, run_id, run), "against": run_stamp(catalog, against_id, against)}
    final = catalog / COMPARES_DIR / key
    existing = read_json(final / COMPARE_FILE)
    if (not force and isinstance(existing, dict) and existing.get("stamps") == stamps
            and existing.get("settings") == settings):
        result = dict(existing, reused=True)
        output.line(f"compare {key}: unchanged since {existing.get('comparedAt')}, reused")
    else:
        started = time.monotonic()
        stage = catalog / COMPARES_DIR / f".{key}.{os.getpid()}.tmp"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        try:
            rows = judge_rows(catalog, run_id, read_rows(catalog / RUNS_DIR / run_id), against_id,
                              read_rows(catalog / RUNS_DIR / against_id), key, stage, channel_threshold,
                              max_diff_percent, exclusions, output=output, pool=pool, workers=workers, stop=stop)
            result = {"schema": 2, "key": key, "run": run_id, "against": against_id, "againstRevision": revision,
                      "againstPinned": pinned(catalog, run_device_dir(against)) == against_id,
                      "deviceType": run.get("deviceType"), "comparedAt": iso(),
                      "seconds": round(time.monotonic() - started, 2), "settings": settings, "stamps": stamps,
                      "notes": notes, "runStopped": run_is_stopped(run), "runPartial": run_is_partial(run),
                      "counts": totals(rows), "rows": rows}
            write_json(stage / COMPARE_FILE, result)
            meta = {"run": run_id, "against": f"{against_id} r{revision}", "device": type_label(run),
                    "compared": result["comparedAt"], "channel threshold": channel_threshold,
                    "max diff %": max_diff_percent, **({"notes": "; ".join(notes)} if notes else {})}
            if report:
                (stage / "report.html").write_text(render_report(rows, meta, root=catalog))
            if final.exists():
                shutil.rmtree(final)
            shutil.rmtree(catalog / THUMBS_DIR / COMPARES_DIR / key, ignore_errors=True)
            os.replace(stage, final)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        result = dict(result, reused=False)
        output.line(f"compare {key}: {len(rows)} rows in {result['seconds']:.1f}s")
    for other, data in iter_compares(catalog):
        pair = key_pair(other, data)
        if other != key and pair[0] == run_id and pair[1] == against_id:
            remove_compare(catalog, other)
            output.line(f"removed the superseded comparison {other}")
    for note in notes:
        output.line(f"note: {note}")
    if result.get("runStopped"):
        output.line(f"note: {run_id} stopped early; only the screens it captured are judged")
    for appearance, counts in sorted(result["counts"].items()):
        output.line(f"{appearance}: " + ", ".join(f"{s} {counts.get(s, 0)}" for s in (PASS, FAIL, MISSING, ERROR, SKIPPED)))
    reviews = review_rows(catalog, result["rows"])
    for row in result["rows"]:
        if row["status"] in (FAIL, ERROR, MISSING):
            review = reviews.get((row["name"], row["appearance"]))
            tail = f" · {review_line(review)}" if row["status"] in REVIEWABLE else ""
            output.line(f"  {row['status']:7} {row['appearance']:5} {row['name']}: {row.get('error') or ''}{tail}")
    summary = review_summary(reviews)
    if summary["changes"]:
        output.line(f"review: {summary['changes']} change{'s' if summary['changes'] != 1 else ''}: {summary['open']} open, "
                    f"{summary['expected']} expected, {summary['confirmed']} confirmed, {summary['rejected']} rejected")
    result["reviews"] = reviews
    if report and not (final / "report.html").is_file():
        meta = {"run": run_id, "against": f"{against_id} r{revision}", "device": type_label(run),
                "compared": result.get("comparedAt"), "channel threshold": channel_threshold,
                "max diff %": max_diff_percent, **({"notes": "; ".join(notes)} if notes else {})}
        (final / "report.html").write_text(render_report(result["rows"], meta, root=catalog))
    if report:
        output.line(f"report: {final / 'report.html'}")
    output.line(f"comparison: {final / COMPARE_FILE}")
    output.rows("compare", result["rows"])
    return result


def data_uri(path: str | None, root: Path | None = None) -> str | None:
    if not path:
        return None
    file = Path(path) if root is None or Path(path).is_absolute() else root / path
    if not file.is_file():
        return None
    return "data:image/png;base64," + base64.b64encode(file.read_bytes()).decode()


def render_report(rows: list[dict], meta: dict, root: Path | None = None) -> str:
    """One self-contained HTML page; row image paths are resolved against `root` when relative."""
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        counts.setdefault(row["appearance"], {s: 0 for s in (PASS, FAIL, MISSING, ERROR, SKIPPED)})
        counts[row["appearance"]][row["status"]] = counts[row["appearance"]].get(row["status"], 0) + 1
    summary = "".join(
        f"<tr><td>{html.escape(a)}</td>" + "".join(f"<td class='{s.lower()}'>{c.get(s, 0)}</td>" for s in (PASS, FAIL, MISSING, ERROR, SKIPPED)) + "</tr>"
        for a, c in sorted(counts.items()))

    def cell(path: str | None, label: str) -> str:
        uri = data_uri(path, root)
        if not uri:
            return f"<figure><div class='none'>no {label}</div><figcaption>{label}</figcaption></figure>"
        return f"<figure><img src='{uri}' alt='{label}'><figcaption>{label}</figcaption></figure>"

    order = {FAIL: 0, ERROR: 1, MISSING: 2}
    problems = sorted((r for r in rows if r["status"] in order), key=lambda r: (order[r["status"]], r["appearance"], r["name"]))
    body = "".join(
        f"<tr><td class='{r['status'].lower()}'>{r['status']}</td><td><code>{html.escape(r['name'])}</code></td>"
        f"<td>{html.escape(r['appearance'])}</td><td>{html.escape(r.get('error') or '')}</td>"
        f"<td class='shots'>{cell(r.get('baseline'), 'baseline')}{cell(r.get('current'), 'current')}{cell(r.get('diff'), 'diff')}</td></tr>"
        for r in problems) or "<tr><td colspan='5'>No FAIL, ERROR or MISSING rows.</td></tr>"
    skipped = "".join(
        f"<li><code>{html.escape(r['name'])}</code> ({html.escape(r['appearance'])}): {html.escape(r.get('error') or '')}</li>"
        for r in rows if r["status"] == SKIPPED)
    meta_rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in meta.items())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Screen catalog report</title>
<style>
:root {{ color-scheme: light dark; --fg:#1d1d1f; --bg:#fff; --line:#d2d2d7; --pass:#1a7f37; --fail:#cf222e; --warn:#9a6700; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg:#f5f5f7; --bg:#161618; --line:#3a3a3c; --pass:#3fb950; --fail:#ff7b72; --warn:#d29922; }} }}
body {{ font: 14px/1.4 -apple-system, system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 16px; }}
table {{ border-collapse: collapse; margin: 12px 0; }}
th, td {{ border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }}
.pass {{ color: var(--pass); font-weight: 600; }} .fail, .error {{ color: var(--fail); font-weight: 600; }}
.missing, .skipped {{ color: var(--warn); font-weight: 600; }}
.shots {{ display: flex; gap: 8px; border: none; }}
figure {{ margin: 0; }} figure img {{ width: 220px; border: 1px solid var(--line); }}
figcaption {{ text-align: center; font-size: 12px; }}
.none {{ width: 220px; height: 120px; display: grid; place-items: center; border: 1px dashed var(--line); }}
</style></head><body>
<h1>Screen catalog report</h1>
<table>{meta_rows}</table>
<h2>Totals</h2>
<table><tr><th>appearance</th><th>PASS</th><th>FAIL</th><th>MISSING</th><th>ERROR</th><th>SKIPPED</th></tr>{summary}</table>
<h2>FAIL, ERROR and MISSING</h2>
<table><tr><th>status</th><th>variant</th><th>appearance</th><th>reason</th><th>baseline / current / diff (red = differs)</th></tr>{body}</table>
<h2>Skipped (exclusions file)</h2><ul>{skipped or '<li>none</li>'}</ul>
</body></html>
"""


# MARK: - Where a commit stands against main

MAIN_REFS = (f"{GIT_REMOTE}/{MAIN_BRANCH}", MAIN_BRANCH)
FETCH_TIMEOUT = 60


def git_output(repo: Path, *args: str, timeout: float = 10) -> str | None:
    """stdout of a git command in `repo`, stripped; None when git is missing, fails or times out."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def main_head(repo: Path) -> dict | None:
    """main as this clone last saw it: `origin/main` (as of the last fetch), else a local `main`; None when
    `repo` is not a git clone or has neither."""
    for ref in MAIN_REFS:
        sha = git_output(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if sha:
            return {"ref": ref, "sha": sha, "committedAt": git_output(repo, "log", "-1", "--format=%cI", sha)}
    return None


def fetch_main(repo: Path) -> bool:
    """Brings `origin/main` up to date; False when the clone has no origin or the fetch fails (offline)."""
    return git_output(repo, "fetch", "--quiet", GIT_REMOTE, MAIN_BRANCH, timeout=FETCH_TIMEOUT) is not None


def standing(repo: Path, sha: str | None, main: dict | None) -> dict | None:
    """Where a run's commit stands against `main`: `at` (it is main's head), `behind` (on main, `behind`
    commits before its head), `off` (`ahead` commits of it are not on main: a branch) or `unknown` (the
    commit is not in this clone: fetch). None when the run has no sha or there is no main."""
    if not sha or not main:
        return None
    if git_output(repo, "cat-file", "-e", f"{sha}^{{commit}}") is None:
        return {"ref": main["ref"], "state": "unknown", "behind": None, "ahead": None}
    behind = git_output(repo, "rev-list", "--count", f"{sha}..{main['sha']}")
    ahead = git_output(repo, "rev-list", "--count", f"{main['sha']}..{sha}")
    if behind is None or ahead is None:
        # A failed or timed-out count is not "0 ahead, 0 behind": that would read as `at` and pass the gate.
        return {"ref": main["ref"], "state": "unknown", "behind": None, "ahead": None, "gitFailed": True}
    state = "off" if int(ahead) else "at" if int(behind) == 0 else "behind"
    return {"ref": main["ref"], "state": state, "behind": int(behind), "ahead": int(ahead)}


def main_blocker(run: dict, state: dict | None, main: dict | None) -> str | None:
    """Why a run cannot become, or be copied into, the baseline: the baseline means "main at a commit",
    so the run must have been recorded from a clean checkout of a commit that is on main (`at` or
    `behind`). None when it was."""
    git = run.get("git") or {}
    if not git.get("sha"):
        return "it has no commit recorded"
    if git.get("dirty"):
        return f"it was recorded with uncommitted changes on {git['sha'][:9]}"
    if main is None:
        return f"this clone has neither {MAIN_REFS[0]} nor {MAIN_REFS[1]} to check its commit against"
    if state is not None and state.get("gitFailed"):
        return f"git could not place its commit {git['sha'][:9]} against {main['ref']} (rev-list failed)"
    if state is None or state["state"] == "unknown":
        return f"its commit {git['sha'][:9]} is not in this clone"
    if state["state"] == "off":
        n = state["ahead"]
        return (f"it was recorded off {main['ref']}: {n} commit{'s' if n != 1 else ''} of {git['sha'][:9]} "
                f"{'are' if n != 1 else 'is'} not on it")
    return None


def check_on_main(repo: Path, run_id: str, run: dict, output: "Output", fetch: bool = True) -> dict | None:
    """The gate before a run replaces, or is copied into, the pinned baseline: its commit must be on main.
    Fetches `origin/main` first so a stale clone does not refuse a fresh commit of main (and judges
    against main as the clone last saw it when the fetch fails). Returns the standing; raises CatalogError
    with the reason. There is no override: a run of a branch, a squash or a dirty tree is not a
    reference, so merge first, then record off main."""
    if fetch and not fetch_main(repo):
        output.line(f"could not fetch {GIT_REMOTE} {MAIN_BRANCH}: judging against main as this clone last saw it")
    main = main_head(repo)
    state = standing(repo, (run.get("git") or {}).get("sha"), main)
    why = main_blocker(run, state, main)
    if why:
        raise CatalogError(f"{run_id} cannot be the baseline: {why}. A baseline means main at a commit: merge first, "
                           "then record off main and pin that run")
    return state


# MARK: - Baseline operations

def set_baseline(catalog: Path, run_id: str, *, allow_partial: bool = False, allow_stopped: bool = False,
                 skip_review: bool = False, output: Output = CONSOLE, check_main: bool = True,
                 repo: Path = REPO_ROOT, channel_threshold: int = DEFAULT_CHANNEL_THRESHOLD,
                 max_diff_percent: float = DEFAULT_MAX_DIFF_PERCENT) -> dict:
    """Pins `run_id` as its type's baseline. Refuses a run still capturing, a stopped (or aborted) run
    without `allow_stopped`, a partial run without `allow_partial`, a run whose commit is not on main
    (`check_on_main`; no override, `check_main=False` is for tests of the rest), and, while the type has
    a pin, a run whose changes against it are not all confirmed by a reviewer (`check_reviewed`;
    `skip_review` pins anyway). Nothing is recomputed: comparisons are keyed by their pair, so those
    against the previous pin simply stop being current. {"deviceDir", "pinned", "previous", "review",
    "main"}; the pin is also an event in the run's history."""
    run = load_run(catalog, run_id)
    device_dir = run_device_dir(run)
    if not device_dir:
        raise CatalogError(f"run {run_id} has no simulator type in its run.json")
    if run_is_active(run):
        raise CatalogError(f"run {run_id} is still capturing")
    if run_is_stopped(run) and not allow_stopped:
        raise CatalogError(f"run {run_id} stopped before capturing everything it selected; pass --allow-stopped "
                           "to pin it anyway")
    if run_is_partial(run) and not allow_partial:
        raise CatalogError(f"run {run_id} is partial (filter {(run.get('filter') or {}).get('label') or 'all'}, "
                           f"appearance {run.get('appearance', 'both')}): use update-baseline {run_id} to copy its "
                           "screens into the pinned baseline, or pass --allow-partial to pin it")
    pins = read_baselines(catalog)
    previous = pins.get(device_dir)
    if previous == run_id:
        output.line(f"{run_id} is already the baseline of {type_label(run)}")
        return {"deviceDir": device_dir, "pinned": run_id, "previous": previous, "review": None, "main": None}
    main = check_on_main(repo, run_id, run, output) if check_main else None
    review = None
    if previous and read_run(catalog / RUNS_DIR / previous) is not None and not skip_review:
        review = check_reviewed(catalog, run_id, previous, channel_threshold=channel_threshold,
                                max_diff_percent=max_diff_percent)["summary"]
    pins[device_dir] = run_id
    write_baselines(catalog, pins)
    path = catalog / RUNS_DIR / run_id
    data = read_run(path) or run
    append_history(data, {"kind": "pin", "at": iso(), "revision": run_revision(data), "replaced": previous,
                          **({"main": main} if main else {}), **({"review": review} if review else {}),
                          **({"skippedReview": True} if skip_review and previous else {})})
    write_run(path, **data)
    output.line(f"pinned {run_id} as the baseline of {type_label(run)}" + (f" (was {previous})" if previous else ""))
    return {"deviceDir": device_dir, "pinned": run_id, "previous": previous, "review": review, "main": main}


def _copy_into_baseline(catalog: Path, run_id: str, rows: list[dict], kind: str, output: Output,
                        skip_review: bool = False, check_main: bool = True, repo: Path = REPO_ROOT,
                        channel_threshold: int = DEFAULT_CHANNEL_THRESHOLD,
                        max_diff_percent: float = DEFAULT_MAX_DIFF_PERCENT) -> dict:
    """Copies `rows`' screens of run `run_id` over the pinned baseline of its type, replaces their
    manifest rows, bumps the baseline's revision and logs a history event. The event. The run's commit
    must be on main (`check_on_main`), and each screen's change must be confirmed by a reviewer first
    (`check_reviewed`), unless `skip_review`."""
    run = load_run(catalog, run_id)
    pin = pinned(catalog, run_device_dir(run))
    if pin is None:
        raise CatalogError(f"no baseline is pinned for {type_label(run)}: pin a run with set-baseline first")
    if pin == run_id:
        raise CatalogError(f"{run_id} is the pinned baseline itself")
    baseline = load_run(catalog, pin)
    check_comparable(run_id, run, pin, baseline)
    for each_id, each in ((run_id, run), (pin, baseline)):
        if run_is_active(each):
            raise CatalogError(f"run {each_id} is still capturing")
    main = check_on_main(repo, run_id, run, output) if check_main else None
    if not skip_review:
        check_reviewed(catalog, run_id, pin, {(r["name"], r["appearance"]) for r in rows},
                       channel_threshold=channel_threshold, max_diff_percent=max_diff_percent)
    source, target = catalog / RUNS_DIR / run_id, catalog / RUNS_DIR / pin
    now, fresh = iso(), []
    for row in rows:
        relative = shot_relpath(row["appearance"], row["name"])
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.parent / f".{destination.name}.{os.getpid()}.tmp"
        shutil.copyfile(source / row["file"], temp)
        os.replace(temp, destination)
        fresh.append(dict(row, file=relative, status=RECORDED, **{"from": run_id, "copiedAt": now}))
    merged = merge_manifest_variants(read_rows(target), fresh)
    write_rows(target, pin, merged)
    data = read_run(target) or baseline
    revision = bump_revision(data)
    event = {"kind": kind, "at": now, "revision": revision, "from": run_id,
             "variants": [{"name": r["name"], "appearance": r["appearance"]} for r in fresh],
             "app": (run.get("app") or {}).get("sha256"), "git": (run.get("git") or {}).get("sha"),
             **({"main": main} if main else {})}
    append_history(data, event)
    data["counts"], data["statuses"] = screen_counts(merged), status_counts(merged)
    write_run(target, **data)
    output.line(f"{kind}: copied {len(fresh)} screen{'s' if len(fresh) != 1 else ''} from {run_id} into {pin}, "
                f"now r{revision}")
    return dict(event, baseline=pin)


def update_baseline(catalog: Path, run_id: str, output: Output = CONSOLE, skip_review: bool = False,
                    check_main: bool = True, repo: Path = REPO_ROOT, channel_threshold: int = DEFAULT_CHANNEL_THRESHOLD,
                    max_diff_percent: float = DEFAULT_MAX_DIFF_PERCENT) -> dict:
    """Copies a partial run's captured screens over the pinned baseline of its type (ERROR and SKIPPED
    rows are left alone): one new revision, one `update` history event. A full run is refused: pin it
    with set_baseline instead."""
    run = load_run(catalog, run_id)
    if not run_is_partial(run):
        raise CatalogError(f"{run_id} is a full run: pin it with `set-baseline {run_id}` instead of copying "
                           "every screen over the baseline")
    path = catalog / RUNS_DIR / run_id
    rows = [r for r in read_rows(path) if r.get("status") == RECORDED and (path / r["file"]).is_file()]
    left = [r for r in read_rows(path) if r.get("status") != RECORDED]
    if not rows:
        raise CatalogError(f"run {run_id} has no captured screens to copy")
    event = _copy_into_baseline(catalog, run_id, rows, "update", output, skip_review, check_main, repo,
                                channel_threshold, max_diff_percent)
    if left:
        output.line(f"left {len(left)} ERROR/SKIPPED row{'s' if len(left) != 1 else ''} of {run_id} out")
    return event


def accept_screen(catalog: Path, run_id: str, name: str, appearance: str, output: Output = CONSOLE,
                  skip_review: bool = False, check_main: bool = True, repo: Path = REPO_ROOT,
                  channel_threshold: int = DEFAULT_CHANNEL_THRESHOLD, max_diff_percent: float = DEFAULT_MAX_DIFF_PERCENT) -> dict:
    """Copies one captured screen of `run_id` over the pinned baseline of its type: a new revision and
    an `accept` history event."""
    path = catalog / RUNS_DIR / run_id
    load_run(catalog, run_id)
    row = next((r for r in read_rows(path) if r["name"] == name and r["appearance"] == appearance), None)
    if row is None or row.get("status") != RECORDED or not (path / row["file"]).is_file():
        raise CatalogError(f"run {run_id} has no screenshot of {name} ({appearance})")
    return _copy_into_baseline(catalog, run_id, [row], "accept", output, skip_review, check_main, repo,
                               channel_threshold, max_diff_percent)


# MARK: - Deleting, pruning

def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _remove_run(catalog: Path, run_id: str) -> list[str]:
    """Deletes a run folder, its thumbnails and every comparison it is part of; the removed keys."""
    keys = [key for key, data in iter_compares(catalog) if run_id in key_pair(key, data)[:2]]
    for key in keys:
        remove_compare(catalog, key)
    shutil.rmtree(catalog / RUNS_DIR / run_id, ignore_errors=True)
    shutil.rmtree(catalog / THUMBS_DIR / RUNS_DIR / run_id, ignore_errors=True)
    return keys


def no_baseline_notice(run: dict) -> str:
    """What deleting a type's pinned run leaves: no baseline until one is set."""
    return f"no baseline for {type_label(run)}: set one from a run (set-baseline <run>)"


def delete_run(catalog: Path, run_id: str, output: Output = CONSOLE) -> dict:
    """Deletes a run, its thumbnails and the comparisons that involve it. Refuses a run still capturing.
    Deleting the pinned run also removes its type's pin: that type has no baseline until one is set
    (`unpinned` names the type, `notice` says so). {"deleted", "bytes", "compares", "unpinned", "notice"}."""
    run = load_run(catalog, run_id)
    path = catalog / RUNS_DIR / run_id
    if path.is_symlink():
        raise CatalogError(f"{path} is a symlink")
    if run_is_active(run):
        raise CatalogError(f"run {run_id} is still capturing")
    device_dir = run_device_dir(run)
    pins = read_baselines(catalog)
    unpinned = device_dir if device_dir and pins.get(device_dir) == run_id else None
    size = dir_bytes(path)
    if unpinned:  # the pin goes first: a pin naming a missing run is never left behind
        del pins[unpinned]
        write_baselines(catalog, pins)
    keys = _remove_run(catalog, run_id)
    output.line(f"deleted {run_id} ({size / 1e6:.1f} MB)" + (f" and {len(keys)} comparison(s)" if keys else ""))
    notice = None
    if unpinned:
        notice = f"{run_id} was the baseline: " + no_baseline_notice(run)
        output.line(f"note: {notice}")
    return {"deleted": run_id, "bytes": size, "compares": keys, "unpinned": unpinned, "notice": notice}


def prune(catalog: Path, keep: int, protect=frozenset(), output: Output | None = None) -> dict:
    """Keeps the newest `keep` runs of each source (cli, ui) per simulator type and deletes the rest,
    with their thumbnails and comparisons; never a pinned run (which does not count toward `keep`), a
    run still capturing, or one in `protect`. Then drops every comparison that is no longer current:
    one whose run is gone, or keyed by an older revision of the run it was compared with.
    {"runs": [...], "compares": [...]}."""
    if keep < 1:
        raise CatalogError("--keep must be at least 1")
    pins = set(read_baselines(catalog).values())
    seen: dict[tuple[str, str], int] = {}
    removed_runs, removed_compares = [], []
    for run_id, data in iter_runs(catalog):
        if run_id in pins:
            continue
        pool = (run_source(data), run_device_dir(data) or "")
        seen[pool] = seen.get(pool, 0) + 1
        if seen[pool] <= keep or run_id in protect or run_is_active(data):
            continue
        removed_compares += _remove_run(catalog, run_id)
        removed_runs.append(run_id)
    revisions = {run_id: run_revision(data) for run_id, data in iter_runs(catalog)}
    for key, data in iter_compares(catalog):
        run_id, against_id, revision = key_pair(key, data)
        if run_id not in revisions or against_id not in revisions or revision != revisions[against_id]:
            remove_compare(catalog, key)
            removed_compares.append(key)
    for stage in (catalog / COMPARES_DIR).glob(".*.tmp") if (catalog / COMPARES_DIR).is_dir() else []:
        match = re.search(r"\.(\d+)\.tmp$", stage.name)
        if match and not process_alive(int(match.group(1))):
            shutil.rmtree(stage, ignore_errors=True)
    if output is not None:
        if removed_runs:
            output.line(f"pruned {len(removed_runs)} run(s) (keep {keep} per source and type): {', '.join(removed_runs)}")
        if removed_compares:
            output.line(f"dropped {len(removed_compares)} comparison(s) that are no longer current")
    return {"runs": removed_runs, "compares": removed_compares}


def prune_runs(catalog: Path, keep: int, protect=frozenset()) -> list[str]:
    """The run ids `prune` removed (the older entry point's shape)."""
    return prune(catalog, keep, protect)["runs"]


# MARK: - Export, import

def export_run(catalog: Path, run_id: str, destination: Path | None = None) -> Path:
    """The run folder stored (PNGs are not recompressed) as `<catalog>/screencatalog_<run-id>.zip`,
    paths starting `<run-id>/`; its launch logs are left out."""
    run = load_run(catalog, run_id)
    if run_is_active(run):
        raise CatalogError(f"run {run_id} is still capturing")
    root = catalog / RUNS_DIR
    archive_path = destination or catalog / zip_name(run_id)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted((root / run_id).rglob("*")):
            if path.is_file() and not path.is_symlink() and path.relative_to(root / run_id).parts[0] != "logs":
                archive.write(path, path.relative_to(root).as_posix())
    return archive_path


def import_run(catalog: Path, archive_path: Path, output: Output = CONSOLE) -> str:
    """Unzips an exported run into runs/<run-id>/; refuses an existing id or any path outside it."""
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1:
            raise CatalogError(f"{archive_path}: expected one run folder, found {sorted(tops)}")
        run_id = tops.pop()
        if not valid_run_id(run_id) or f"{run_id}/{RUN_FILE}" not in names:
            raise CatalogError(f"{archive_path}: not an exported run ({run_id!r}/{RUN_FILE} missing)")
        if any(n.startswith("/") or ".." in Path(n).parts or "\\" in n for n in names):
            raise CatalogError(f"{archive_path}: a path leaves the run folder")
        target = catalog / RUNS_DIR / run_id
        if target.exists():
            raise CatalogError(f"run {run_id} exists already")
        stage = catalog / RUNS_DIR / f".{run_id}.import.tmp"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        try:
            archive.extractall(stage)
            data = read_run(stage / run_id)
            if data is None:
                raise CatalogError(f"{archive_path}: its run.json does not parse")
            write_run(stage / run_id, active=False, pid=None, importedFrom=str(archive_path), importedAt=iso())
            os.replace(stage / run_id, target)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    output.line(f"imported {run_id} ({type_label(data)})")
    return run_id


# MARK: - Sync (S3 through the aws CLI): runs/ and baselines.json are canonical; nothing else leaves the Mac

SYNC_FILE = "sync.json"  # <catalog>/sync.json: the (masked) bucket and the last push / pull per run, for the web UI
SYNC_PREFIX = setting("storage", "prefix", default="screencatalog")  # s3://<bucket>/<prefix>/{baselines.json,runs/<run-id>/}
STORAGE_TYPE = setting("storage", "type", default="s3")  # "s3" (the aws CLI) or "directory" (a shared folder)
STORE_NOUNS = {"s3": "the bucket", "directory": "the shared folder"}  # how messages name the store
BUCKET_ENV = "SCREENCATALOG_S3_BUCKET"
SYNC_EXCLUDES = ("logs/*",)  # a run's launch logs never leave the Mac
AWS_TIMEOUT = 3600  # one `aws s3 sync` of a run folder (a large run is hundreds of MB)
AWS_QUICK_TIMEOUT = 120  # `aws s3 ls`, and `aws s3 cp` of one JSON file
TRANSFER_VERBS = ("upload", "download", "copy", "delete")  # `aws s3 sync`'s per-file lines, counted rather than shown
TRANSFER_PROGRESS_EVERY = 100
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def mask_bucket(bucket: str | None) -> str | None:
    """`*******-****-assets`: every '-' / '.' segment but the last starred; one segment keeps its last 4."""
    if not bucket:
        return None
    parts = re.split(r"([.-])", bucket)
    if len(parts) == 1:
        return "*" * max(0, len(bucket) - 4) + bucket[-4:]
    return "".join(p if p in ".-" or i == len(parts) - 1 else "*" * len(p) for i, p in enumerate(parts))


def push_blocker(run: dict, is_pinned: bool = False, include_partial: bool = False) -> str | None:
    """Why `run` is not pushed, or None. Never a run still capturing; a pinned run always otherwise (it was
    pinned on purpose); else never one that did not finish, and a stopped or partial one only with
    `include_partial` (`sync push --all`)."""
    if run_is_active(run):
        return "still capturing"
    if is_pinned:
        return None
    if not run.get("finished"):
        return "did not finish capturing"
    if include_partial:
        return None
    if run_is_stopped(run):
        return "stopped early: push --all includes it"
    if run_is_partial(run):
        label = (run.get("filter") or {}).get("label") or f"appearance {run.get('appearance', 'both')}"
        return f"partial ({label}): push --all includes it"
    return None


def diverged(local: dict, remote: dict) -> bool:
    """The same revision reached twice: both sides accepted or updated from one revision independently."""
    return (run_revision(local) == run_revision(remote) > 1
            and local.get("revisionAt") != remote.get("revisionAt"))


def read_sync(catalog: Path) -> dict:
    data = read_json(catalog / SYNC_FILE)
    data = data if isinstance(data, dict) else {}
    runs = data.get("runs") if isinstance(data.get("runs"), dict) else {}
    return {"bucket": data.get("bucket"), "prefix": data.get("prefix"), "lastPush": data.get("lastPush"),
            "lastPull": data.get("lastPull"), "baselines": data.get("baselines") if isinstance(data.get("baselines"), dict) else {},
            "runs": {k: v for k, v in runs.items() if valid_run_id(k) and isinstance(v, dict)}}


def record_sync(catalog: Path, remote: Remote, kind: str, run_id: str | None = None, revision: int | None = None) -> None:
    """Notes a push or pull of one run (or of baselines.json when `run_id` is None) in sync.json."""
    data, now = read_sync(catalog), iso()
    data.update(bucket=remote.masked, prefix=remote.prefix)
    data["lastPush" if kind == "push" else "lastPull"] = now
    entry = data["runs"].setdefault(run_id, {}) if run_id else data["baselines"]
    entry[f"{kind}edAt"] = now
    if revision is not None:
        entry[f"{kind}edRevision"] = revision
    write_json(catalog / SYNC_FILE, data)


class Remote:
    """`s3://<bucket>/<prefix>/` through the `aws` CLI on PATH. Credentials come from the environment and are
    never read here; the bucket is masked in every line this prints."""

    def __init__(self, bucket: str | None, prefix: str = SYNC_PREFIX, output: Output = CONSOLE,
                 stop_event: threading.Event | None = None):
        if not bucket:
            raise CatalogError(f"no bucket: pass --bucket or set {BUCKET_ENV}")  # or storage.bucket in the config
        if not BUCKET_RE.match(bucket):
            raise CatalogError("the bucket is not an S3 bucket name")
        prefix = (prefix or "").strip("/")
        if not PREFIX_RE.match(prefix) or ".." in prefix.split("/"):
            raise CatalogError(f"bad prefix {prefix!r}")
        aws = shutil.which("aws")
        if aws is None:
            raise CatalogError("the aws CLI is not on PATH: brew install awscli")
        self.bucket, self.prefix, self.aws = bucket, prefix, aws
        self.masked = mask_bucket(bucket)
        self.noun = STORE_NOUNS["s3"]
        self.output, self.stop_event = output, stop_event
        # Global aws options from the config: an S3-compatible endpoint (R2, MinIO), a profile, a region.
        self.extra = [x for flag, key in (("--endpoint-url", "endpointUrl"), ("--profile", "profile"), ("--region", "region"))
                      if setting("storage", key) for x in (flag, str(setting("storage", key)))]

    def url(self, *parts: str) -> str:
        return "s3://" + "/".join((self.bucket, self.prefix) + parts)

    def show(self, text: str) -> str:
        return text.replace(self.bucket, self.masked)

    def call(self, args: list[str], stream: bool, timeout: float) -> tuple[int, list[str]]:
        """Runs `aws <args>`; streams its lines (bucket masked) when `stream`. A timeout or a stop kills it."""
        if self.stop_event is not None and self.stop_event.is_set():
            raise CatalogError("stopped")
        if stream:
            self.output.line("$ aws " + self.show(" ".join(args)))
        proc = subprocess.Popen([self.aws, *self.extra, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                bufsize=1, stdin=subprocess.DEVNULL)
        why: list[str] = []

        def watch() -> None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    proc.wait(timeout=0.1)
                    return
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() > deadline or (self.stop_event is not None and self.stop_event.is_set()):
                    why.append("timed out" if time.monotonic() > deadline else "stopped")
                    proc.kill()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        lines, transfers = [], {}
        assert proc.stdout is not None
        with proc.stdout:
            for raw in proc.stdout:
                for line in raw.replace("\r", "\n").splitlines():
                    if not line.strip():
                        continue
                    lines.append(line)
                    if not stream:
                        continue
                    verb = line.split(":", 1)[0] if ":" in line else ""
                    if verb in TRANSFER_VERBS:
                        # `s3 sync` prints one line per file: a run is hundreds of them, which drowned the log.
                        transfers[verb] = transfers.get(verb, 0) + 1
                        if transfers[verb] % TRANSFER_PROGRESS_EVERY == 0:
                            self.output.line(f"… {transfers[verb]} files {verb}ed so far")
                    else:
                        self.output.line(self.show(line))
        code = proc.wait()
        watcher.join(1)
        if stream and transfers:
            self.output.line(", ".join(f"{n} file{'s' if n != 1 else ''} {verb}ed" for verb, n in transfers.items()))
        if why:
            raise CatalogError("stopped" if why[0] == "stopped" else f"aws {' '.join(args[:2])} timed out after {timeout:.0f} s")
        return code, lines

    def _fail(self, what: str, code: int, lines: list[str]) -> CatalogError:
        return CatalogError(f"aws {what} failed ({code}): {self.show(' '.join(lines[-3:])) or 'no output'}")

    def run_ids(self) -> list[str]:
        """The run folders under <prefix>/runs/."""
        code, lines = self.call(["s3", "ls", self.url(RUNS_DIR) + "/"], False, AWS_QUICK_TIMEOUT)
        if code != 0:
            if not lines:  # `aws s3 ls` exits 1 with no output when nothing is under the prefix
                return []
            raise self._fail("s3 ls", code, lines)
        found = (re.match(r"^\s*PRE (.+)/$", line) for line in lines)
        return sorted(m.group(1) for m in found if m and valid_run_id(m.group(1)))

    def get_json(self, *parts: str):
        """A remote JSON file parsed, or None when there is none."""
        with tempfile.TemporaryDirectory(prefix="screencatalog-sync-") as tmp:
            target = Path(tmp, "file.json")
            code, lines = self.call(["s3", "cp", "--only-show-errors", self.url(*parts), str(target)], False,
                                    AWS_QUICK_TIMEOUT)
            if code != 0:
                text = " ".join(lines)
                if "404" in text or "Not Found" in text or "does not exist" in text:
                    return None
                raise self._fail("s3 cp " + "/".join(parts), code, lines)
            data = read_json(target)
            if data is None:
                raise CatalogError(f"{self.show(self.url(*parts))} does not parse")
            return data

    def put(self, source: Path, *parts: str) -> None:
        code, lines = self.call(["s3", "cp", "--only-show-errors", str(source), self.url(*parts)], True, AWS_QUICK_TIMEOUT)
        if code != 0:
            raise self._fail("s3 cp", code, lines)

    def get(self, target: Path, *parts: str) -> None:
        code, lines = self.call(["s3", "cp", "--only-show-errors", self.url(*parts), str(target)], True, AWS_QUICK_TIMEOUT)
        if code != 0:
            raise self._fail("s3 cp", code, lines)

    def remove(self, *parts: str) -> None:
        """`aws s3 rm --recursive` of a remote folder."""
        code, lines = self.call(["s3", "rm", "--recursive", "--only-show-errors", self.url(*parts) + "/"], True, AWS_TIMEOUT)
        if code != 0:
            raise self._fail("s3 rm", code, lines)

    def sync(self, source: str, target: str, excludes: tuple[str, ...]) -> None:
        args = ["s3", "sync", source, target, "--exact-timestamps", "--no-progress"]
        for pattern in excludes:
            args += ["--exclude", pattern]
        code, lines = self.call(args, True, AWS_TIMEOUT)
        if code != 0:
            raise self._fail("s3 sync", code, lines)

    def push_dir(self, local: Path, *parts: str, excludes: tuple[str, ...] = ()) -> None:
        """Uploads what differs under `local` to <prefix>/<parts>/; never deletes."""
        self.sync(str(local) + "/", self.url(*parts) + "/", excludes)

    def pull_dir(self, local: Path, *parts: str, excludes: tuple[str, ...] = ()) -> None:
        """Downloads what differs under <prefix>/<parts>/ into `local`; never deletes."""
        self.sync(self.url(*parts) + "/", str(local) + "/", excludes)


class DirectoryRemote(Remote):
    """The same store as a plain folder (a network share, a synced drive, a checkout of an artefacts repo):
    `storage: {"type": "directory", "path": …}`. Same layout as the bucket prefix; copies what differs by
    size or modification time and never deletes."""

    def __init__(self, path: Path | None, output: Output = CONSOLE, stop_event: threading.Event | None = None):
        if path is None:
            raise CatalogError("no storage folder: set storage.path in screencatalog.json")
        self.root = Path(path)
        self.bucket = self.masked = str(self.root)
        self.noun = STORE_NOUNS["directory"]
        self.prefix, self.aws, self.extra = "", None, []
        self.output, self.stop_event = output, stop_event

    def url(self, *parts: str) -> str:
        return str(self.root.joinpath(*parts))

    def show(self, text: str) -> str:
        return text

    def _check_stop(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise CatalogError("stopped")

    def run_ids(self) -> list[str]:
        folder = self.root / RUNS_DIR
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir() if p.is_dir() and valid_run_id(p.name))

    def get_json(self, *parts: str):
        path = self.root.joinpath(*parts)
        if not path.is_file():
            return None
        data = read_json(path)
        if data is None:
            raise CatalogError(f"{path} does not parse")
        return data

    def put(self, source: Path, *parts: str) -> None:
        self._check_stop()
        target = self.root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        shutil.copy2(source, staged)
        os.replace(staged, target)

    def get(self, target: Path, *parts: str) -> None:
        self._check_stop()
        source = self.root.joinpath(*parts)
        if not source.is_file():
            raise CatalogError(f"{source} does not exist")
        shutil.copy2(source, target)

    def remove(self, *parts: str) -> None:
        self._check_stop()
        shutil.rmtree(self.root.joinpath(*parts), ignore_errors=True)

    def _copy_tree(self, source: Path, target: Path, excludes: tuple[str, ...]) -> None:
        copied = 0
        for path in sorted(source.rglob("*")) if source.is_dir() else []:
            relative = path.relative_to(source).as_posix()
            if not path.is_file() or path.name.endswith(".tmp") or any(fnmatch.fnmatch(relative, pattern) for pattern in excludes):
                continue
            self._check_stop()
            destination = target / relative
            if destination.is_file():
                theirs, ours = destination.stat(), path.stat()
                if theirs.st_size == ours.st_size and int(theirs.st_mtime) == int(ours.st_mtime):
                    continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            copied += 1
        self.output.line(f"{copied} file{'s' if copied != 1 else ''} copied")

    def push_dir(self, local: Path, *parts: str, excludes: tuple[str, ...] = ()) -> None:
        self._copy_tree(Path(local), self.root.joinpath(*parts), excludes)

    def pull_dir(self, local: Path, *parts: str, excludes: tuple[str, ...] = ()) -> None:
        self._copy_tree(self.root.joinpath(*parts), Path(local), excludes)


def store_noun() -> str:
    """What messages call the configured store: "the bucket" for S3, "the shared folder" for a folder."""
    return STORE_NOUNS["directory" if STORAGE_TYPE == "directory" else "s3"]


def configured_bucket() -> str | None:
    return os.environ.get(BUCKET_ENV) or setting("storage", "bucket")


def sync_configured() -> bool:
    """Whether a shared store is set up: a folder, or a bucket (from the environment or the config)."""
    return bool(setting("storage", "path")) if STORAGE_TYPE == "directory" else bool(configured_bucket())


def sync_available() -> bool:
    """`sync_configured()` and, for S3, the aws CLI on PATH."""
    return sync_configured() and (STORAGE_TYPE == "directory" or shutil.which("aws") is not None)


def make_remote(bucket: str | None = None, prefix: str | None = None, output: Output = CONSOLE,
                stop_event: threading.Event | None = None) -> Remote:
    """The shared store: `--bucket` wins, else the configured storage (a folder or a bucket)."""
    if not bucket and STORAGE_TYPE == "directory":
        return DirectoryRemote(config_path(setting("storage", "path")), output, stop_event)
    return Remote(bucket or configured_bucket(), prefix or SYNC_PREFIX, output, stop_event)


def run_build_key(run: dict) -> tuple | None:
    """What makes two runs the same build of one type: (deviceDir, commit, app binary hash, capture mode).
    None when the run cannot stand in for another: a dirty tree (the sha does not describe what was
    built), no sha or app hash, or a run that is not complete and full (active, stopped or partial)."""
    git, app = run.get("git") or {}, run.get("app") or {}
    if not git.get("sha") or git.get("dirty") or not app.get("sha256"):
        return None
    if not run.get("finished") or run_is_active(run) or run_is_stopped(run) or run_is_partial(run):
        return None
    return (run_device_dir(run), git["sha"], app["sha256"], capture_mode(run))


def find_duplicate(runs, device_dir: str | None, git: dict | None, app_sha: str | None, mode: str,
                   exclude=()) -> str | None:
    """Among (run id, run.json) pairs, the first complete full run of this type, commit, app and capture
    mode: the run a new record (or a push) would only repeat. None for a dirty tree, which can never
    match, or when nothing matches."""
    git = git or {}
    if not git.get("sha") or git.get("dirty") or not app_sha:
        return None
    key = (device_dir, git["sha"], app_sha, mode)
    for run_id, data in runs:
        if run_id not in exclude and isinstance(data, dict) and run_build_key(data) == key:
            return run_id
    return None


def remote_runs_json(remote: Remote) -> list[tuple[str, dict]]:
    """(run id, run.json) of every run on the remote, one small read each."""
    out = []
    for run_id in remote.run_ids():
        data = remote.get_json(RUNS_DIR, run_id, RUN_FILE)
        if isinstance(data, dict):
            out.append((run_id, data))
    return out


def duplicate_message(duplicate: str, where: str, run: dict) -> str:
    git, app = run.get("git") or {}, run.get("app") or {}
    return (f"a complete run of this build exists already: {duplicate} ({where}): {type_label(run)}, commit "
            f"{(git.get('sha') or '')[:9]}, app {app.get('sha256')}, {capture_mode(run)} capture. Open or pull it instead; "
            "to record it again, delete that run first")


def remote_listing(catalog: Path, remote: Remote) -> dict:
    """`sync pull --list` and GET /api/sync/remote: every remote run's facts and how the local copy stands
    (`local`: missing, behind, same, ahead or diverged)."""
    pins = remote.get_json(BASELINES_FILE)
    pins = {k: v for k, v in pins.items() if isinstance(k, str) and valid_run_id(v)} if isinstance(pins, dict) else {}
    out = []
    for run_id, data in remote_runs_json(remote):
        local = read_run(catalog / RUNS_DIR / run_id)
        revision = run_revision(data)
        if local is None:
            state = "missing"
        elif diverged(local, data):
            state = "diverged"
        else:
            state = {-1: "behind", 0: "same", 1: "ahead"}[(run_revision(local) > revision) - (run_revision(local) < revision)]
        model, os_name = run_type(data)
        out.append({"id": run_id, "revision": revision, "revisionAt": data.get("revisionAt"),
                    "deviceDir": run_device_dir(data), "model": model, "os": os_name, "started": data.get("started"),
                    "partial": run_is_partial(data), "stopped": run_is_stopped(data),
                    "pinned": pins.get(run_device_dir(data) or "") == run_id, "counts": data.get("counts") or {},
                    "git": (data.get("git") or {}).get("sha"), "dirty": bool((data.get("git") or {}).get("dirty")),
                    "appSha256": (data.get("app") or {}).get("sha256"), "captureMode": capture_mode(data),
                    "complete": run_build_key(data) is not None,
                    "local": state, "localRevision": run_revision(local) if local is not None else None})
    out.sort(key=lambda r: (r.get("started") or "", r["id"]), reverse=True)
    return {"bucket": remote.masked, "prefix": remote.prefix, "baselines": pins, "runs": out}


def _push_run(catalog: Path, remote: Remote, run_id: str) -> None:
    """Files first, run.json last: a reader that sees the new run.json sees every file it names."""
    remote.push_dir(catalog / RUNS_DIR / run_id, RUNS_DIR, run_id, excludes=SYNC_EXCLUDES + (RUN_FILE,))
    remote.put(catalog / RUNS_DIR / run_id / RUN_FILE, RUNS_DIR, run_id, RUN_FILE)


def sync_push(catalog: Path, remote: Remote, run_ids=(), include_all: bool = False, force: bool = False,
              replace_pins: bool = False, output: Output = CONSOLE) -> dict:
    """Pushes runs to the shared store (the bucket, or the shared folder). No ids: every pinned run, then baselines.json (`include_all`: every finished
    run, partial and stopped ones too, never one still capturing). Ids: those runs (`include_all` lets them be
    partial or stopped), and baselines.json when one of them is a pinned baseline. Refused, before anything is sent: a run whose remote revision is higher (`force`
    pushes anyway), baselines.json when the remote pins a type to a run you do not have (`force`), and a run
    the remote lacks when it already holds the same build of that type under another id, which no flag
    overrides: delete that run there first. `replace_pins` lifts only the baselines.json rule: this Mac's pin
    for a type replaces a remote pin it never had (the web UI asks first; the CLI's `--force` covers it too)."""
    runs = dict(iter_runs(catalog))
    pins = {t: r for t, r in read_baselines(catalog).items() if r in runs}
    pinned_ids = set(pins.values())
    problems = []
    if run_ids:
        chosen = list(dict.fromkeys(run_ids))
        for run_id in chosen:
            load_run(catalog, run_id)
            why = push_blocker(runs[run_id], run_id in pinned_ids, include_all)
            if why:
                problems.append(f"cannot push {run_id}: {why}")
        # Pushing a pinned run shares the baseline, so baselines.json goes with it; a run that is not a pin goes alone.
        push_pins = any(run_id in pinned_ids for run_id in chosen)
        if push_pins:
            output.line(f"{BASELINES_FILE} goes too: " + ", ".join(r for r in chosen if r in pinned_ids) + " is the pinned baseline of its type")
    else:
        chosen = []
        for run_id, data in sorted(runs.items(), key=lambda item: run_order_key(item)):
            why = push_blocker(data, run_id in pinned_ids, include_all)
            if (include_all or run_id in pinned_ids) and why is None:
                chosen.append(run_id)
            elif include_all and why:
                output.line(f"skipping {run_id}: {why}")
        push_pins = True
    if problems:
        raise CatalogError("; ".join(problems))
    remote_runs = {}
    remote_runs_listing = None
    for run_id in chosen:
        theirs = remote.get_json(RUNS_DIR, run_id, RUN_FILE)
        if isinstance(theirs, dict):
            remote_runs[run_id] = theirs
        else:
            # A run the remote lacks: refused, `--force` or not, when the remote already has the same build of
            # the same type under another id; a pull would bring that one here for nothing more than a second copy.
            if remote_runs_listing is None:
                remote_runs_listing = remote_runs_json(remote)
            twin = find_duplicate(remote_runs_listing, run_device_dir(runs[run_id]), runs[run_id].get("git"),
                                  (runs[run_id].get("app") or {}).get("sha256"), capture_mode(runs[run_id]), exclude=(run_id,))
            if twin is not None:
                problems.append(f"{run_id}: the remote already has a run of this build on {type_label(runs[run_id])}: "
                                f"{twin}; pull it instead, or delete it there first")
            continue
        if force:
            continue
        mine, yours = run_revision(theirs), run_revision(runs[run_id])
        if mine > yours:
            problems.append(f"{run_id}: remote baseline is r{mine}, yours is r{yours}: pull first")
        elif diverged(runs[run_id], theirs):
            problems.append(f"{run_id}: remote and local are both r{yours} but were changed separately: pull --force "
                            "to take the remote one, or push --force to replace it")
    merged, theirs_pins = None, None
    if push_pins:
        theirs = remote.get_json(BASELINES_FILE)
        theirs_pins = {k: v for k, v in theirs.items() if isinstance(k, str) and valid_run_id(v)} if isinstance(theirs, dict) else {}
        unknown = [f"{t} → {r}" for t, r in sorted(theirs_pins.items()) if r not in runs and pins.get(t) not in (None, r)]
        if unknown and not force and not replace_pins:
            problems.append(f"remote {BASELINES_FILE} pins {', '.join(unknown)}, which you do not have: pull first, "
                            "or pass --force to replace it")
        merged = dict(sorted(dict(theirs_pins, **pins).items()))
    if problems:
        raise CatalogError("; ".join(problems))
    total = len(chosen) + (1 if push_pins else 0)
    output.progress(0, total)
    pushed, already = [], []
    for index, run_id in enumerate(chosen):
        revision = run_revision(runs[run_id])
        theirs = remote_runs.get(run_id)
        # run.json goes up last, so a remote run.json of the same revision, made at the same moment, means every
        # file it names is there: nothing to send, and `aws s3 sync` is not even asked.
        if theirs is not None and not force and revision == run_revision(theirs) \
                and theirs.get("revisionAt") == runs[run_id].get("revisionAt"):
            output.line(f"{run_id} r{revision} is already on the remote: nothing to push")
            already.append(run_id)
            output.progress(index + 1, total)
            continue
        output.line(f"pushing {run_id} r{revision} ({type_label(runs[run_id])})")
        _push_run(catalog, remote, run_id)
        record_sync(catalog, remote, "push", run_id, revision)
        pushed.append(run_id)
        output.progress(index + 1, total)
    _sync_reviews(catalog, remote, "push", output)
    if push_pins:
        if merged == theirs_pins and not force:
            output.line(f"{BASELINES_FILE} is up to date on the remote")
        else:
            with tempfile.TemporaryDirectory(prefix="screencatalog-sync-") as tmp:
                source = Path(tmp, BASELINES_FILE)
                source.write_text(json.dumps(merged, indent=2) + "\n")
                output.line(f"pushing {BASELINES_FILE} ({len(merged)} pin{'s' if len(merged) != 1 else ''})")
                remote.put(source, BASELINES_FILE)
            record_sync(catalog, remote, "push")
        output.progress(total, total)
    output.line(f"pushed {len(pushed)} run{'s' if len(pushed) != 1 else ''}"
                + (f" ({len(already)} already there)" if already else "")
                + (f" and {BASELINES_FILE}" if push_pins and merged != theirs_pins else "")
                + f" to {remote.show(remote.url())}")
    return {"pushed": pushed, "alreadyPushed": already, "baselines": merged}


def _pull_run(catalog: Path, remote: Remote, run_id: str) -> None:
    """Files first, run.json last (a new run appears whole, from a staging folder): an interrupted pull
    leaves the old revision's run.json, so the next pull fetches it again."""
    target = catalog / RUNS_DIR / run_id
    fresh = not target.exists()
    folder = catalog / RUNS_DIR / f".{run_id}.pull.{os.getpid()}.tmp" if fresh else target
    if fresh:
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
    try:
        remote.pull_dir(folder, RUNS_DIR, run_id, excludes=SYNC_EXCLUDES + (RUN_FILE,))
        temp = folder / f".{RUN_FILE}.{os.getpid()}.tmp"
        remote.get(temp, RUNS_DIR, run_id, RUN_FILE)
        if not isinstance(read_json(temp), dict):
            if temp.exists():
                temp.unlink()
            raise CatalogError(f"the remote {run_id}/{RUN_FILE} does not parse")
        os.replace(temp, folder / RUN_FILE)
        if fresh:
            os.replace(folder, target)
    finally:
        if fresh:
            shutil.rmtree(folder, ignore_errors=True)


def sync_pull(catalog: Path, remote: Remote, run_ids=(), force: bool = False, output: Output = CONSOLE) -> dict:
    """Pulls from the shared store. No ids: baselines.json and every run it pins that is missing here or behind in
    revision. Ids: those runs. Refused, before anything is fetched: a local run with a higher revision than
    the remote one (`force` overwrites it), and one still capturing."""
    remote_pins = None
    if run_ids:
        wanted = list(dict.fromkeys(run_ids))
        for run_id in wanted:
            if not valid_run_id(run_id):
                raise CatalogError(f"bad run id {run_id!r}")
    else:
        found = remote.get_json(BASELINES_FILE)
        remote_pins = {k: v for k, v in found.items() if isinstance(k, str) and valid_run_id(v)} if isinstance(found, dict) else {}
        wanted = sorted(set(remote_pins.values()))
        if not remote_pins:
            output.line(f"no {BASELINES_FILE} on the remote: nothing is pinned there")
    plan, problems = [], []
    for run_id in wanted:
        theirs = remote.get_json(RUNS_DIR, run_id, RUN_FILE)
        if not isinstance(theirs, dict):
            problems.append(f"no run {run_id} on the remote")
            continue
        mine = read_run(catalog / RUNS_DIR / run_id)
        remote_revision = run_revision(theirs)
        if mine is None:
            plan.append((run_id, remote_revision))
            continue
        local_revision = run_revision(mine)
        if run_is_active(mine):
            problems.append(f"{run_id} is still capturing here")
        elif local_revision > remote_revision and not force:
            problems.append(f"{run_id}: yours is r{local_revision}, remote baseline is r{remote_revision}: push it, "
                            "or pull --force to overwrite yours")
        elif diverged(mine, theirs) and not force:
            problems.append(f"{run_id}: remote and local are both r{local_revision} but were changed separately: "
                            "pull --force to take the remote one, or push --force to replace it")
        elif local_revision < remote_revision or force:
            plan.append((run_id, remote_revision))
        else:
            output.line(f"{run_id} r{local_revision} is up to date")
    if problems:
        raise CatalogError("; ".join(problems))
    output.progress(0, len(plan))
    _sync_reviews(catalog, remote, "pull", output)
    for index, (run_id, revision) in enumerate(plan):
        output.line(f"pulling {run_id} r{revision}")
        _pull_run(catalog, remote, run_id)
        record_sync(catalog, remote, "pull", run_id, revision)
        output.progress(index + 1, len(plan))
    changed = {}
    if remote_pins:
        pins = read_baselines(catalog)
        for device_dir, run_id in remote_pins.items():
            if pins.get(device_dir) != run_id and read_run(catalog / RUNS_DIR / run_id) is not None:
                changed[device_dir] = {"was": pins.get(device_dir), "now": run_id}
                output.line(f"baseline of {device_dir}: {run_id}" + (f" (was {pins[device_dir]}, which stays as a run)"
                                                                    if pins.get(device_dir) else ""))
                pins[device_dir] = run_id
        if changed:
            write_baselines(catalog, pins)
        record_sync(catalog, remote, "pull")
    output.line(f"pulled {len(plan)} run{'s' if len(plan) != 1 else ''}" + (f" and {BASELINES_FILE}" if remote_pins is not None else ""))
    return {"pulled": [run_id for run_id, _ in plan], "pins": changed}


# MARK: - Review: who marked a change expected, who confirmed it

REVIEWS_DIR = "reviews"  # <catalog>/reviews/<pair>/<event>.json: one file per event, written once, never rewritten
REVIEW_KINDS = ("expect", "confirm", "reject", "withdraw")
REVIEW_STATES = ("open", "expected", "confirmed", "rejected")
REVIEWABLE = (FAIL, MISSING)  # a changed screen, or a screen the other run does not have
REVIEWER_ENV = "SCREENCATALOG_REVIEWER"  # "Name <email>", for a job with no git identity
NOTE_MAX = 2000
VARIANT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_digests: dict[tuple[str, int, int], str] = {}


def file_digest(path: Path) -> str | None:
    """sha256 of a file, hex; None when there is no file. Cached by (path, size, mtime): a run's screens
    never change, so a pair is hashed once per process."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _digests:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        _digests[key] = digest.hexdigest()
    return _digests[key]


def review_pair(catalog: Path, row: dict) -> dict | None:
    """What a verdict is about: {name, appearance, current, baseline}, the sha256 of the run's screen and of
    the other run's (`none` for a MISSING row: the other run has no screen of it). A verdict is keyed by
    the images, not the runs, so a re-record that captures the same bytes, against the same baseline
    screen, carries the verdict over, and a baseline whose screen changed does not. None for a row that
    is not FAIL or MISSING, or has no screen."""
    if row.get("status") not in REVIEWABLE or not row.get("current"):
        return None
    current = file_digest(catalog / row["current"])
    if current is None:
        return None
    baseline = file_digest(catalog / row["baseline"]) if row.get("baseline") else None
    return {"name": row["name"], "appearance": row["appearance"], "current": current, "baseline": baseline or "none"}


def pair_dir(pair: dict) -> str:
    return f"{pair['name']}__{pair['appearance']}__{pair['current'][:16]}__{pair['baseline'][:16]}"


def same_person(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    if a.get("email") and b.get("email"):
        return a["email"].strip().lower() == b["email"].strip().lower()
    return (a.get("name") or "").strip().lower() == (b.get("name") or "").strip().lower() != ""


def person_label(who: dict | None) -> str:
    return (who or {}).get("name") or (who or {}).get("email") or "someone"


def parse_person(text: str | None) -> dict | None:
    """`Name <email>`, `Name` or `email` as {name, email}; None for nothing."""
    text = (text or "").strip()
    if not text:
        return None
    match = re.match(r"^(.*?)\s*<([^<>]+)>$", text)
    if match:
        return {"name": match.group(1).strip() or None, "email": match.group(2).strip()}
    if "@" in text:
        return {"name": None, "email": text}
    return {"name": text, "email": None}


def git_user(repo: Path = REPO_ROOT) -> dict | None:
    """Who is reviewing: $SCREENCATALOG_REVIEWER, else git's user.name / user.email in `repo`. None without either."""
    found = parse_person(os.environ.get(REVIEWER_ENV))
    if found:
        return found
    name = run(["git", "-C", str(repo), "config", "user.name"], check=False).stdout.strip() or None
    email = run(["git", "-C", str(repo), "config", "user.email"], check=False).stdout.strip() or None
    return {"name": name, "email": email} if name or email else None


def read_review_events(catalog: Path, pair: dict) -> list[dict]:
    """Every event of a pair, oldest first (by `at`, then file name, so every Mac folds them the same way)."""
    folder = catalog / REVIEWS_DIR / pair_dir(pair)
    events = []
    if folder.is_dir():
        for file in folder.glob("*.json"):
            data = read_json(file)
            if isinstance(data, dict) and data.get("kind") in REVIEW_KINDS and isinstance(data.get("by"), dict):
                events.append(dict(data, file=file.name))
    events.sort(key=lambda e: (e.get("at") or "", e["file"]))
    return events


def fold_review(events: list[dict]) -> dict:
    """The state a pair's events reach, in order. `open` → `expected` by an `expect` (author, note) →
    `confirmed` by a `confirm` from someone else, or `rejected` by a `reject` (reviewer, reason). A `withdraw`
    by the author reopens; after a reject, a new `expect` is needed. An event out of turn (a confirm by the
    author, a confirm of nothing, a second expect of a confirmed change) changes nothing and is kept in
    `events` with why (`ignored`)."""
    state = {"state": "open", "author": None, "note": None, "reviewer": None, "reason": None, "at": None, "events": []}
    for event in events:
        by, kind, ignored = event.get("by") or {}, event["kind"], None
        if kind == "expect":
            if state["state"] == "confirmed":
                ignored = "already confirmed"
            else:
                state.update(state="expected", author=by, note=event.get("note"), reviewer=None, reason=None, at=event.get("at"))
        elif state["state"] == "open":
            ignored = "nothing is marked expected"
        elif kind == "withdraw":
            if same_person(by, state["author"]):
                state.update(state="open", author=None, note=None, reviewer=None, reason=None, at=event.get("at"))
            else:
                ignored = "only the author withdraws"
        elif kind == "confirm":
            if same_person(by, state["author"]):
                ignored = "the author cannot confirm their own change"
            elif state["state"] == "confirmed":
                ignored = "already confirmed"
            elif state["state"] == "rejected":
                ignored = "rejected: it must be marked expected again first"
            else:
                state.update(state="confirmed", reviewer=by, reason=None, at=event.get("at"))
        elif kind == "reject":
            if state["state"] == "rejected":
                ignored = "already rejected"
            else:
                state.update(state="rejected", reviewer=by, reason=event.get("note"), at=event.get("at"))
        kept = {k: event.get(k) for k in ("kind", "by", "at", "note", "run", "against")}
        if ignored:
            kept["ignored"] = ignored
        state["events"].append(kept)
    return state


def add_review_event(catalog: Path, pair: dict, kind: str, by: dict | None, note: str | None = None, *,
                     run_id: str | None = None, against_id: str | None = None,
                     against_revision: int | None = None) -> dict:
    """Writes one event for a pair (a new file, never a rewrite) and returns the pair's folded state.
    `reject` needs a reason; `expect` takes a note. Refuses an event that would change nothing."""
    if kind not in REVIEW_KINDS:
        raise CatalogError(f"kind must be one of {', '.join(REVIEW_KINDS)}")
    if not by or not (by.get("name") or by.get("email")):
        raise CatalogError(f"no identity: set git user.name and user.email, or {REVIEWER_ENV}=\"Name <email>\"")
    if not VARIANT_NAME_RE.match(pair.get("name") or "") or pair.get("appearance") not in APPEARANCES:
        raise CatalogError("bad pair")
    note = (note or "").strip() or None
    if note and len(note) > NOTE_MAX:
        raise CatalogError(f"the note is longer than {NOTE_MAX} characters")
    if kind == "reject" and not note:
        raise CatalogError("a rejection needs a reason")
    now = dt.datetime.now(dt.timezone.utc)  # microseconds: two events in one second must still fold in order
    event = {"schema": 1, "pair": pair, "kind": kind, "by": {"name": by.get("name"), "email": by.get("email")},
             "at": now.isoformat(), "note": note, "run": run_id, "against": against_id, "againstRevision": against_revision}
    after = fold_review(read_review_events(catalog, pair) + [event])
    last = after["events"][-1]
    if last.get("ignored"):
        raise CatalogError(f"{kind} changes nothing: {last['ignored']}")
    folder = catalog / REVIEWS_DIR / pair_dir(pair)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%S%f")
    who = re.sub(r"[^a-z0-9]+", "-", person_label(by).lower()).strip("-")[:24] or "someone"
    write_json(folder / f"{stamp}-{kind}-{who}-{uuid.uuid4().hex[:6]}.json", event)
    return dict(after, pair=pair)


def review_rows(catalog: Path, rows: list[dict]) -> dict[tuple[str, str], dict]:
    """The review of every reviewable row of a comparison, by (name, appearance): the pair and its folded state."""
    out = {}
    for row in rows:
        pair = review_pair(catalog, row)
        if pair is not None:
            out[(row["name"], row["appearance"])] = dict(fold_review(read_review_events(catalog, pair)), pair=pair)
    return out


def review_summary(reviews: dict) -> dict:
    """{changes, open, expected, confirmed, rejected}: how many reviewable screens are in each state."""
    summary = {"changes": len(reviews), **{state: 0 for state in REVIEW_STATES}}
    for review in reviews.values():
        summary[review["state"]] += 1
    return summary


def review_blockers(rows: list[dict], reviews: dict) -> list[str]:
    """Why a comparison's run cannot become, or be copied into, the baseline: every ERROR row, and every
    FAIL or MISSING row a reviewer has not confirmed."""
    out = []
    for row in rows:
        key = (row["name"], row["appearance"])
        if row["status"] == ERROR:
            out.append(f"{row['name']} ({row['appearance']}): ERROR, {row.get('error') or 'not captured'}")
        elif row["status"] in REVIEWABLE:
            review = reviews.get(key)
            state = review["state"] if review else "open"
            if state != "confirmed":
                who = f" by {person_label(review['author'])}" if review and review.get("author") else ""
                out.append(f"{row['name']} ({row['appearance']}): {row['status']}, {state}{who if state != 'open' else ''}")
    return out


def check_reviewed(catalog: Path, run_id: str, against_id: str | None = None, names: set | None = None, *,
                   channel_threshold: int = DEFAULT_CHANNEL_THRESHOLD,
                   max_diff_percent: float = DEFAULT_MAX_DIFF_PERCENT) -> dict:
    """The gate before a run replaces, or is copied into, the pinned baseline: the run's current comparison
    with it must exist, judged at the tolerance in force (a looser compare would simply have no changes to
    review), and every change in it (`names` narrows to those (name, appearance) pairs) must be confirmed
    by a reviewer. Returns {summary, blockers} and raises CatalogError with the blockers."""
    stored = current_compare(catalog, run_id, against_id, channel_threshold=channel_threshold,
                             max_diff_percent=max_diff_percent)
    if stored is None:
        judged = (current_compare(catalog, run_id, against_id) or {}).get("settings") or {}
        if judged:
            raise CatalogError(f"the current comparison of {run_id} with the baseline was judged at channel threshold "
                               f"{judged.get('channelThreshold')}, max {judged.get('maxDiffPercent')}% differing pixels, "
                               f"not the tolerance in force (channel threshold {channel_threshold}, max "
                               f"{max_diff_percent}%): compare it again at that tolerance")
        raise CatalogError(f"no current comparison of {run_id} with the baseline: compare it first, then have every "
                           "change confirmed by a reviewer (or pass --skip-review)")
    rows = [r for r in stored.get("rows") or [] if names is None or (r["name"], r["appearance"]) in names]
    reviews = review_rows(catalog, rows)
    blockers = review_blockers(rows, reviews)
    summary = review_summary(reviews)
    if blockers:
        shown = "; ".join(blockers[:5]) + (f"; and {len(blockers) - 5} more" if len(blockers) > 5 else "")
        raise CatalogError(f"{len(blockers)} change{'s' if len(blockers) != 1 else ''} not confirmed by a reviewer "
                           f"({summary['open']} open, {summary['expected']} expected, {summary['rejected']} rejected, "
                           f"{summary['confirmed']} confirmed): {shown}")
    return {"summary": summary, "blockers": blockers}


def review_line(review: dict | None) -> str:
    """One review state for a compare's FAIL line: `expected by alex: "new header"`, `confirmed by sam`."""
    if not review or review["state"] == "open":
        return "open"
    who = person_label(review["reviewer"] if review["state"] in ("confirmed", "rejected") else review["author"])
    text = f"{review['state']} by {who}"
    note = review.get("reason") if review["state"] == "rejected" else review.get("note")
    return text + (f': "{note}"' if note else "")


def _sync_reviews(catalog: Path, remote: Remote, kind: str, output: Output) -> None:
    """Reviews travel with every push and pull: `aws s3 sync` of reviews/ in that direction, never deleting.
    Every event is its own file, so the two sides only ever gain files: a union, no conflicts."""
    local = catalog / REVIEWS_DIR
    if kind == "push":
        if not local.is_dir():
            return
        remote.push_dir(local, REVIEWS_DIR)
    else:
        local.mkdir(parents=True, exist_ok=True)
        remote.pull_dir(local, REVIEWS_DIR)
    output.line(f"{kind}ed {REVIEWS_DIR}/")



def sync_delete(catalog: Path, remote: Remote, run_ids, output: Output = CONSOLE) -> dict:
    """Deletes runs from the shared store (`runs/<id>/`, every object). Refused, before anything goes: an id the
    remote does not have, and a run the remote's baselines.json pins (push another baseline for that type
    first). A Mac that pulled the run keeps its copy; this Mac's sync.json forgets the push."""
    wanted = list(dict.fromkeys(run_ids))
    if not wanted:
        raise CatalogError(f"name the run(s) to delete from {remote.noun}")
    for run_id in wanted:
        if not valid_run_id(run_id):
            raise CatalogError(f"bad run id {run_id!r}")
    pins = remote.get_json(BASELINES_FILE)
    pins = {k: v for k, v in pins.items() if isinstance(k, str) and valid_run_id(v)} if isinstance(pins, dict) else {}
    problems = []
    for run_id in wanted:
        if not isinstance(remote.get_json(RUNS_DIR, run_id, RUN_FILE), dict):
            problems.append(f"no run {run_id} on the remote")
        pinned_types = [t for t, r in pins.items() if r == run_id]
        if pinned_types:
            problems.append(f"{run_id} is {remote.noun}'s baseline of {', '.join(pinned_types)}: push another baseline "
                            "for that type first")
    if problems:
        raise CatalogError("; ".join(problems))
    for run_id in wanted:
        output.line(f"deleting {run_id} from {remote.show(remote.url(RUNS_DIR, run_id))}/")
        remote.remove(RUNS_DIR, run_id)
        data = read_sync(catalog)
        if data["runs"].pop(run_id, None) is not None:
            write_json(catalog / SYNC_FILE, data)
    output.line(f"deleted {len(wanted)} run{'s' if len(wanted) != 1 else ''} from {remote.noun}; a Mac that pulled one keeps its copy")
    return {"deleted": wanted}


# MARK: - Listing

def catalog_listing(catalog: Path) -> dict:
    """`list`: every run (newest first) with `comparable` (the other runs of its type) and
    `baselineCompare` (its current comparison with the pinned baseline, counts only, or None), the
    pin per type."""
    runs = list_runs(catalog)
    out = []
    for run in runs:
        others = [r["id"] for r in runs if r["id"] != run["id"] and same_kind(run, r) and not r["active"]]
        stored = current_compare(catalog, run["id"]) if not run["pinned"] else None
        out.append(dict(run, comparable=others,
                        baselineCompare={"key": stored["key"], "counts": stored["counts"],
                                         "comparedAt": stored["comparedAt"]} if stored else None))
    return {"catalog": str(catalog), "runs": out, "baselines": read_baselines(catalog)}


# MARK: - Simulator and bridge

def run(cmd: list[str], check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def simctl(*args: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
    return run(["xcrun", "simctl", *args], check=check, timeout=timeout)


def resolve_udid(udid: str | None) -> str:
    if udid:
        return udid
    booted = [d["udid"] for devices in json.loads(simctl("list", "devices", "booted", "-j").stdout)["devices"].values()
              for d in devices if d.get("state") == "Booted"]
    if len(booted) != 1:
        raise RuntimeError(f"{len(booted)} simulators are booted; pass --udid")
    return booted[0]


def resolve_udids(udids: list[str]) -> list[str]:
    if not udids:
        return [resolve_udid(None)]
    if len(set(udids)) != len(udids):
        raise RuntimeError("the same --udid is given twice")
    return list(udids)


def shard_variants(entries: list, count: int) -> list[list]:
    """Round-robin, order kept within each shard: shard i gets entries i, i+count, i+2*count, …"""
    return [entries[i::count] for i in range(count)]


def device_info(udid: str) -> dict:
    devices = json.loads(simctl("list", "devices", "-j").stdout)["devices"]
    for runtime_id, entries in devices.items():
        for d in entries:
            if d["udid"] == udid:
                if d.get("state") != "Booted":
                    raise RuntimeError(f"simulator {udid} is {d.get('state')}; boot it first")
                types = {t["identifier"]: t["name"] for t in json.loads(simctl("list", "devicetypes", "-j").stdout)["devicetypes"]}
                runtimes = {r["identifier"]: r for r in json.loads(simctl("list", "runtimes", "-j").stdout)["runtimes"]}
                runtime = runtimes.get(runtime_id, {})
                return {
                    "udid": udid,
                    "simulatorName": d["name"],
                    "model": types.get(d.get("deviceTypeIdentifier", ""), d["name"]),
                    "os": runtime.get("name") or runtime_id.rsplit(".", 1)[-1].replace("-", " ", 1).replace("-", "."),
                    "osBuild": runtime.get("buildversion"),
                }
    raise RuntimeError(f"no simulator with UDID {udid}")


def app_binary(udid: str, bundle: str) -> dict:
    app = Path(simctl("get_app_container", udid, bundle, "app").stdout.strip())
    return {"bundleId": bundle, **app_identity(app)}


def app_identity(app: Path) -> dict:
    """What identifies an installed app bundle: the code binary's path and mtime, the marketing
    version and build number from Info.plist, and a short SHA-256 of the binary so two builds with
    the same version string can still be told apart."""
    plist = plistlib.loads((app / "Info.plist").read_bytes())
    executable = plist.get("CFBundleExecutable") or app.stem
    # A Debug build keeps the app's code in `<exe>.debug.dylib`; the executable is a stub.
    debug_dylib = app / f"{executable}.debug.dylib"
    binary = debug_dylib if debug_dylib.exists() else app / executable
    digest = hashlib.sha256()
    with open(binary, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return {"path": str(binary), "modified": iso(binary.stat().st_mtime),
            "version": plist.get("CFBundleShortVersionString"), "build": plist.get("CFBundleVersion"),
            "sha256": digest.hexdigest()[:12]}


def git_state(repo: Path = REPO_ROOT) -> dict:
    """The commit a run is built from and whether the tree differs from it: `{sha, dirty}`, plus `ignored`
    (the `BUILD_CHURN_PATHS` that were modified) when a build's own rewrites were left out of `dirty`."""
    sha = run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=False).stdout.strip()
    excludes = [f":(exclude){path}" for path in BUILD_CHURN_PATHS]
    dirty = bool(run(["git", "-C", str(repo), "status", "--porcelain", "--", ".", *excludes], check=False).stdout.strip())
    churned = (run(["git", "-C", str(repo), "status", "--porcelain", "--", *BUILD_CHURN_PATHS], check=False).stdout
               if BUILD_CHURN_PATHS else "")
    ignored = [line[3:] for line in churned.splitlines() if line.strip()]
    state = {"sha": sha or None, "dirty": dirty}
    if ignored:
        state["ignored"] = ignored
    return state


class Bridge:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path: str, timeout: float = 1.5):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as response:
            return json.loads(response.read())

    def try_get(self, path: str):
        try:
            return self.get(path)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError, ValueError):
            return None

    def screenshot(self, target: Path, timeout: float = 15) -> str | None:
        """GET /screenshot (the app's own windows, as PNG) into `target`; None, or why it failed."""
        try:
            with urllib.request.urlopen(self.base + "/screenshot", timeout=timeout) as response:
                kind = response.headers.get("Content-Type", "")
                blob = response.read()
        except urllib.error.HTTPError as error:
            return f"GET /screenshot: HTTP {error.code}"
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as error:
            return f"GET /screenshot: {error}"
        if kind != "image/png" or blob[:8] != PNG_SIGNATURE:
            return f"GET /screenshot: not a PNG ({kind or 'no Content-Type'}, {len(blob)} bytes)"
        target.write_bytes(blob)
        return None


@dataclass
class Launch:
    pid: int | None
    stderr_path: Path

    def stderr_tail(self, lines: int = 6) -> str:
        try:
            text = self.stderr_path.read_text(errors="replace").strip().splitlines()
        except OSError:
            return ""
        return " | ".join(text[-lines:])


def launch(udid: str, bundle: str, arguments: list[str], log_dir: Path, tag: str) -> Launch:
    simctl("terminate", udid, bundle, check=False)
    log_dir = log_dir.resolve()  # a relative --stderr path leaves the file empty; simctl needs it absolute
    log_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = log_dir / f"{sanitize(tag)}.stderr.log"
    stderr_path.write_text("")
    proc = simctl("launch", f"--stderr={stderr_path}", f"--stdout={log_dir / (sanitize(tag) + '.stdout.log')}",
                  udid, bundle, *arguments, check=False)
    if proc.returncode != 0:
        # A launch-time crash (an unregistered variant name `fatalError`s) can be fast enough
        # that simctl reports "No such process"; the app's own stderr says why.
        time.sleep(0.3)
        tail = Launch(None, stderr_path).stderr_tail()
        simctl_error = proc.stderr.strip().splitlines()[0] if proc.stderr.strip() else f"exit {proc.returncode}"
        raise RuntimeError(f"simctl launch failed ({simctl_error})" + (f": {tail}" if tail else ""))
    match = re.search(r":\s*(\d+)\s*$", proc.stdout.strip())
    if not match:
        # Without the process id nothing ties the bridge's answers, or a crash, to this launch.
        raise RuntimeError(f"simctl launch reported no process id (it printed {proc.stdout.strip()!r})")
    return Launch(int(match.group(1)), stderr_path)


def wait_for_health(bridge: Bridge, run_launch: Launch, timeout_s: float) -> str | None:
    """None when the bridge answers for this process; otherwise why it never did."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = bridge.try_get("/status")
        if isinstance(status, dict) and status.get("pid") == run_launch.pid:
            if status.get("protocol", PROTOCOL) != PROTOCOL:
                return (f"the app speaks ScreenCatalog protocol {status.get('protocol')}, this tool speaks {PROTOCOL}: "
                        "update the tool or the package so they match")
            return None
        if not process_alive(run_launch.pid):
            time.sleep(0.3)  # let stderr flush
            tail = run_launch.stderr_tail()
            return "process exited before the bridge came up" + (f": {tail}" if tail else "")
        time.sleep(0.15)
    tail = run_launch.stderr_tail()
    return f"bridge did not answer within {timeout_s:.0f}s" + (f": {tail}" if tail else "")


def wait_for_shown(bridge: Bridge, max_wait_ms: int, pid: int | None) -> float | None:
    """`time.monotonic()` when `/status` first reports the variant as shown, polling for at most
    `max_wait_ms`; None when it never does (or the process exits first)."""
    deadline = time.monotonic() + max_wait_ms / 1000
    while True:
        status = bridge.try_get("/status")
        if isinstance(status, dict) and status.get("shown") is True:
            return time.monotonic()
        if time.monotonic() >= deadline or not process_alive(pid):
            return None
        time.sleep(0.1)


def activity_seq(activity) -> int:
    return int(activity.get("seq") or 0) if isinstance(activity, dict) else 0


def wait_for_settle(bridge: Bridge, settle_ms: int, max_wait_ms: int, pid: int | None) -> tuple[bool, int, str | None]:
    """Polls /activity until its `seq` stops growing and `busy` is 0 for `settle_ms`. (settled, ms to settle, error)."""
    start = time.monotonic()
    last_change = start
    first = bridge.try_get("/activity?since=0")
    seq = activity_seq(first)
    # Tracked apart from the quiet period: with `settle_ms` 0 nothing else would wait for work in flight.
    busy = isinstance(first, dict) and int(first.get("busy") or 0) > 0
    while True:
        now = time.monotonic()
        if not busy and (now - last_change) * 1000 >= settle_ms:
            return True, int((last_change - start) * 1000), None
        if (now - start) * 1000 >= max_wait_ms:
            return False, int((now - start) * 1000), None
        if not process_alive(pid):
            return False, int((now - start) * 1000), "process exited while settling"
        time.sleep(0.1)
        fresh = bridge.try_get(f"/activity?since={seq}")
        if not isinstance(fresh, dict):
            continue
        busy = int(fresh.get("busy") or 0) > 0
        if activity_seq(fresh) > seq or busy:
            seq = max(seq, activity_seq(fresh))
            last_change = time.monotonic()


def enumerate_variants(udid: str, bundle: str, bridge: Bridge, log_dir: Path, health_timeout: float) -> list[dict]:
    """The app's registered variants: a `-ScreenCatalogList` launch, then GET /variants."""
    list_launch = launch(udid, bundle, [LIST_ARGUMENT, PORT_ARGUMENT, str(bridge.base.rsplit(":", 1)[1])], log_dir, "_list")
    problem = wait_for_health(bridge, list_launch, health_timeout)
    if problem:
        raise RuntimeError(f"variant list launch: {problem}")
    listing = bridge.get("/variants", timeout=5)
    simctl("terminate", udid, bundle, check=False)
    return [{"name": e["name"], "feature": e.get("feature") or feature_of(e["name"])} for e in listing["variants"]]



# MARK: - Capture

def capture_variant(args, bridge: Bridge, udid: str, entry: dict, appearance: str, out_root: Path,
                    log_dir: Path, port: int | None = None) -> dict:
    """Launches one variant, waits for it to settle, and screenshots it to `out_root/<appearance>/<name>.png`."""
    name = entry["name"]
    started = time.monotonic()
    target = out_root / shot_relpath(appearance, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    def finish(status: str, **extra) -> dict:
        return variant_record(name, appearance, status, udid=udid, capture_mode=args.capture,
                              duration_ms=int((time.monotonic() - started) * 1000), **extra)

    try:
        run_launch = launch(udid, args.bundle, [VARIANT_ARGUMENT, name, PORT_ARGUMENT, str(port or args.port)],
                            log_dir, f"{appearance}.{name}")
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        return finish(ERROR, error=str(error))
    launched = time.monotonic()
    problem = wait_for_health(bridge, run_launch, args.health_timeout)
    if problem:
        return finish(ERROR, error=problem)
    shown = wait_for_shown(bridge, args.max_wait_ms, run_launch.pid)
    shown_ms = None if shown is None else int((shown - launched) * 1000)
    shown_at = None if shown is None else iso(time.time() - (time.monotonic() - shown))
    shown_fields = {"shown_ms": shown_ms, "shown_at": shown_at}
    settled, settle_ms, problem = wait_for_settle(bridge, args.settle_ms, args.max_wait_ms, run_launch.pid)
    if problem:
        tail = run_launch.stderr_tail()
        return finish(ERROR, settle_ms=settle_ms, settled=settled, error=problem + (f": {tail}" if tail else ""),
                      **shown_fields)
    # Floors for apps whose first frames are not the variant's (a launch screen fading out, a system
    # overlay such as the home indicator settling): never sooner than max(launch + --min-launch-ms,
    # shown + --min-shown-ms). The floor counts from the variant appearing as well as from the launch
    # because under load a launch takes longer, and a floor from launch alone can land inside a fade.
    # A variant that never reported as shown leaves only the launch floor. With the band check, the first
    # screenshot is only the probe, so it is taken one interval early: the one kept is the second
    # or a later one, never sooner than the floor.
    checking = args.band_retakes >= 0
    floor = launched + args.min_launch_ms / 1000
    if shown is not None:
        floor = max(floor, shown + args.min_shown_ms / 1000)
    remaining = floor - (args.band_interval_ms / 1000 if checking else 0) - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)

    def take(path: Path) -> str | None:
        if args.capture == "app":
            return bridge.screenshot(path)
        shot = simctl("io", udid, "screenshot", "--type=png", f"--mask={args.screenshot_mask}", str(path), check=False)
        return None if shot.returncode == 0 else (shot.stderr.strip() or f"exit {shot.returncode}")

    if not checking:  # --band-retakes -1: one screenshot, no band check
        problem, shots, unstable = take(target), 1, False
    else:
        problem, shots, unstable = stable_screenshot(take, target, args.band_retakes, args.band_interval_ms / 1000)
    if problem is not None or not target.exists():
        return finish(ERROR, settle_ms=settle_ms, settled=settled, shots=shots, **shown_fields,
                      error=f"screenshot failed: {problem or 'no file written'}")
    if not process_alive(run_launch.pid):
        return finish(ERROR, settle_ms=settle_ms, settled=settled, shots=shots, **shown_fields,
                      error="process exited during the screenshot: " + run_launch.stderr_tail())
    return finish(RECORDED, settle_ms=settle_ms, settled=settled, shots=shots, unstable_bands=unstable,
                  **shown_fields)


def capture(args, udids: list[str], out_root: Path, keep: list[dict], skipped: list[dict],
            appearances: list[str], log_dir: Path, output: Output = CONSOLE,
            stop_event: threading.Event | None = None) -> tuple[list[dict], int]:
    """(rows, captures not started). With several simulators the variants are sharded round-robin and
    each shard runs on its own simulator, in its own thread, on bridge port `--port` + its index.
    Stops starting captures once `--budget-minutes` is spent, on SIGINT/SIGTERM (when called from the
    main thread) or when `stop_event` is set, and returns what it has, so a bounded run still writes
    its manifest."""
    parallel = len(udids) > 1
    shards = shard_variants(keep, len(udids))
    order = {entry["name"]: index for index, entry in enumerate(keep)}
    total = len(keep) * len(appearances)
    lock = threading.Lock()
    counts = {"started": 0, "done": 0}
    captured: list[dict] = []
    entered: set[str] = set()
    failures: list[BaseException] = []
    run_started = time.monotonic()
    deadline = run_started + args.budget_minutes * 60 if args.budget_minutes else None
    stop = {"why": None}

    def should_stop() -> bool:
        with lock:
            if not stop["why"] and stop_event is not None and stop_event.is_set():
                stop["why"] = "stop requested"
            if not stop["why"] and deadline and time.monotonic() >= deadline:
                stop["why"] = f"--budget-minutes {args.budget_minutes} spent"
            return bool(stop["why"])

    def run_shard(index: int) -> None:
        udid, port = udids[index], args.port + index
        bridge = Bridge(port)
        tag = f"sim{index + 1} " if parallel else ""
        for appearance in appearances:
            if stop["why"] or (stop_event is not None and stop_event.is_set()):
                break
            with lock:
                entered.add(appearance)
            simctl("ui", udid, "appearance", appearance)
            for entry in shards[index]:
                if should_stop():
                    break
                with lock:
                    counts["started"] += 1
                record = capture_variant(args, bridge, udid, entry, appearance, out_root, log_dir, port)
                settle = f"settle {record['settleMs']}ms" if record["settleMs"] is not None else ""
                if record["settled"] is False and record["status"] != ERROR:
                    settle += " (hit --max-wait-ms)"
                if record.get("unstableBands"):
                    settle += f" unstable bands after {record['shots']} screenshots"
                with lock:
                    counts["done"] += 1
                    captured.append(record)
                    line = (f"[{counts['done']}/{total}] {tag}{appearance:5} {entry['name']}: {record['status']} "
                            f"{record['durationMs']}ms {settle}")
                    if record["error"]:
                        line += f" — {record['error']}"
                    output.line(line)
                    output.progress(counts["done"], total)

    def run_shard_guarded(index: int) -> None:
        try:
            run_shard(index)
        except BaseException as error:  # re-raised on the calling thread once every shard has stopped
            with lock:
                failures.append(error)
                stop["why"] = stop["why"] or f"sim{index + 1} failed: {error}"

    def request_stop(signum, _frame):
        stop["why"] = signal.Signals(signum).name

    on_main_thread = threading.current_thread() is threading.main_thread()
    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)} if on_main_thread else {}
    output.progress(0, total)
    try:
        if parallel:
            threads = [threading.Thread(target=run_shard_guarded, args=(i,), name=f"screencatalog-sim{i + 1}", daemon=True)
                       for i in range(len(udids))]
            for thread in threads:
                thread.start()
            for thread in threads:
                while thread.is_alive():  # a timed join keeps the main thread responsive to SIGINT
                    thread.join(0.2)
            if failures:
                raise failures[0]
        else:
            run_shard(0)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        for udid in udids:
            simctl("terminate", udid, args.bundle, check=False)
    # Rows in the order a single simulator produces them: per appearance, the captures in catalog
    # order, then that appearance's skipped variants.
    results: list[dict] = []
    for appearance in appearances:
        if appearance not in entered:
            continue
        results += sorted((r for r in captured if r["appearance"] == appearance), key=lambda r: order[r["name"]])
        results += [variant_record(entry["name"], appearance, SKIPPED, error=entry["reason"]) for entry in skipped]
    elapsed = time.monotonic() - run_started
    timed = [r for r in results if r["durationMs"] is not None]
    if timed:
        output.line(f"captured {len(timed)} in {elapsed:.0f}s ({elapsed / len(timed):.2f}s per variant)")
    not_started = total - counts["started"]
    if stop["why"]:
        output.line(f"stopped early ({stop['why']}): {not_started} of {total} captures not started")
    return results, not_started


# MARK: - Record

def record(args, catalog: Path, exclusions: dict[str, str], output: Output = CONSOLE,
           stop_event: threading.Event | None = None, check_duplicate: bool = True) -> int:
    """Captures the selected variants into a new run folder, runs/<run-id>/, and writes nothing else
    (besides `--zip`'s export and the `--keep-runs` prune). The run is complete once its run.json has
    `finished`; a stopped one has `stopped: true` and keeps its partial captures. Refused when a complete
    run of the same build exists (`find_duplicate`), here or in the bucket; `check_duplicate=False` is for
    a caller that has made that check itself (the web UI), not a way around it."""
    appearances = list(APPEARANCES) if args.appearance == "both" else [args.appearance]
    if args.run_id is not None and not valid_run_id(args.run_id):
        raise CatalogError(f"bad --run-id {args.run_id!r}: letters, digits, '.', '_' and '-' only, no '..' or '__'")
    if args.run_id and (catalog / RUNS_DIR / args.run_id).exists():
        raise CatalogError(f"run {args.run_id} exists already")
    if args.keep_runs < 0:
        raise CatalogError("--keep-runs must be 0 (no pruning) or more")
    try:
        udids = resolve_udids(args.udid)
        devices = [device_info(udid) for udid in udids]
        apps = [app_binary(udid, args.bundle) for udid in udids]
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
        raise CatalogError(str(error)) from error
    kinds = {(d["model"], d["os"]) for d in devices}
    if len(kinds) > 1:
        # One run is one simulator type, so every simulator must render the same pixels.
        found = ", ".join(f"{d['udid']} is {d['model']} {d['os']}" for d in devices)
        raise CatalogError(f"parallel simulators must be the same model and iOS version: {found}")
    udid, device, app = udids[0], devices[0], apps[0]
    device_dir = device_dir_name(device["model"], device["os"])
    simulators = [{"udid": d["udid"], "name": d["simulatorName"], "port": args.port + i, "appModified": a["modified"],
                   "appSha256": a["sha256"]} for i, (d, a) in enumerate(zip(devices, apps))]
    output.line(f"device {device['model']} {device['os']} ({device.get('osBuild')}) on {len(udids)} simulator(s); "
                f"app {app.get('version')} ({app.get('build')}) {app['sha256']}, modified {app['modified']}")
    for i, sim in enumerate(simulators):
        output.line(f"  sim{i + 1} {sim['name']} ({sim['udid']}) port {sim['port']}")
    if len({a["sha256"] for a in apps}) > 1:
        output.line("warning: the simulators have different app builds: "
                    + ", ".join(f"{s['name']} {s['appSha256']}" for s in simulators))
    git = git_state()
    if check_duplicate:
        facts = {"deviceType": {"model": device["model"], "os": device["os"], "deviceDir": device_dir}, "git": git,
                 "app": app, "captureMode": args.capture}
        duplicate = find_duplicate(iter_runs(catalog), device_dir, git, app["sha256"], args.capture)
        if duplicate is not None:
            raise CatalogError(duplicate_message(duplicate, "in this catalog", facts))
        if sync_available():
            try:
                duplicate = find_duplicate(remote_runs_json(make_remote(None, args.sync_prefix, Quiet(), stop_event)),
                                           device_dir, git, app["sha256"], args.capture)
            except CatalogError as error:
                output.line(f"warning: could not check {store_noun()} for a run of this build: {error}")
                duplicate = None
            if duplicate is not None:
                raise CatalogError(duplicate_message(duplicate, f"in {store_noun()}", facts))

    for each in udids:
        # Re-applying an override over an existing one can leave the old status bar drawn (no
        # cellular bars, battery not charging: thousands of pixels), so a stale simulator
        # would differ from the baseline by the status bar alone. Clearing first redraws it.
        simctl("status_bar", each, "clear")
        simctl("status_bar", each, "override", *[x for key, value in STATUS_BAR.items() for x in (f"--{key}", str(value))])
    run_id = args.run_id or new_run_id(catalog, args.source)
    path = run_root(catalog, run_id)
    log_dir = path / "logs"  # each launch's stdout and stderr, kept with the run

    def discard(message: str) -> CatalogError:
        if read_run(path) is None:
            shutil.rmtree(path, ignore_errors=True)
        return CatalogError(message)

    try:
        entries = enumerate_variants(udid, args.bundle, Bridge(args.port), log_dir, args.health_timeout)
    except (RuntimeError, urllib.error.URLError, OSError, KeyError) as error:
        raise discard(f"cannot enumerate variants: {error}") from error
    keep, skipped = select_variants(entries, args.only, args.feature, exclusions)
    if not keep and not skipped:
        raise discard("no variant matched the filters")
    output.line(f"{len(entries)} variants in the app; capturing {len(keep)} x {len(appearances)} appearance(s), "
                f"skipping {len(skipped)}")
    write_run(path, id=run_id, source=args.source, started=iso(), startedEpoch=time.time(), active=True, pid=os.getpid(),
              deviceType={"model": device["model"], "os": device["os"], "osBuild": device.get("osBuild"),
                          "deviceDir": device_dir},
              simulators=simulators, app=app, git=git,
              filter={"only": list(args.only), "feature": list(args.feature),
                      "label": filter_label(args.only, args.feature)},
              appearance=args.appearance, appearances=appearances, captureMode=args.capture, settleMs=args.settle_ms, maxWaitMs=args.max_wait_ms,
              minLaunchMs=args.min_launch_ms, minShownMs=args.min_shown_ms, bandRetakes=args.band_retakes, bandIntervalMs=args.band_interval_ms,
              revision=1, revisionAt=None, history=[])
    output.line(f"run {run_id} → {path}")
    started = time.monotonic()
    try:
        results, not_started = capture(args, udids, path, keep, skipped, appearances, log_dir, output, stop_event)
        write_rows(path, run_id, results)
        finished = iso()
        errors = [r for r in results if r["status"] == ERROR]
        code = 1 if errors or not_started else 0
        write_run(path, active=False, finished=finished, revisionAt=finished,
                  elapsedSeconds=round(time.monotonic() - started, 1), stopped=bool(not_started),
                  notStarted=not_started, counts=screen_counts(results), statuses=status_counts(results),
                  exitCode=code)
    finally:
        if (read_run(path) or {}).get("active"):
            write_run(path, active=False, aborted=True, stopped=True)  # raised before the run was closed
    for appearance, counts in sorted(totals(results).items()):
        output.line(f"{appearance}: " + ", ".join(f"{s} {n}" for s, n in sorted(counts.items())))
    for row in errors:
        output.line(f"  ERROR {row['appearance']:5} {row['name']}: {row['error']}")
    output.rows("record", results)
    if args.zip:
        archive_path = export_run(catalog, run_id)
        output.line(f"zip: {archive_path} ({archive_path.stat().st_size // 1024} KiB)")
    if args.keep_runs:
        prune(catalog, args.keep_runs, protect={run_id}, output=output)
    pin = pinned(catalog, device_dir)
    if pin is None:
        output.line(f"no baseline is pinned for {device['model']} {device['os']}: "
                    f"`set-baseline {run_id}` pins this run")
    else:
        output.line(f"compare it with the baseline ({pin}): `compare {run_id}`")
    if not_started:
        output.line(f"incomplete: {not_started} captures not started")
    return code


# MARK: - Main

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR)
    judging = argparse.ArgumentParser(add_help=False)
    judging.add_argument("--exclude-file", type=Path, default=DEFAULT_EXCLUDE_FILE)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    rec = commands.add_parser("record", parents=[common, judging], help="capture into a new run, runs/<run-id>/")
    rec.add_argument("--udid", action="append", default=[], metavar="UDID",
                     help="simulator UDID (default: the only booted simulator); repeat it to capture on "
                          "several simulators in parallel, which must be the same model and iOS")
    rec.add_argument("--bundle", default=DEFAULT_BUNDLE)
    rec.add_argument("--port", type=int, default=DEFAULT_PORT,
                     help=f"in-app server port (default {DEFAULT_PORT}); with several --udid, simulator i uses port + i")
    rec.add_argument("--only", action="append", default=[], metavar="NAME", help="variant name or substring (repeatable)")
    rec.add_argument("--feature", action="append", default=[], metavar="FEATURE", help="feature, exact (repeatable)")
    configured = [a for a in setting("record", "appearances", default=list(APPEARANCES)) if a in APPEARANCES]
    rec.add_argument("--appearance", choices=("light", "dark", "both"),
                     default=configured[0] if len(configured) == 1 else "both")
    rec.add_argument("--settle-ms", type=int, default=int(setting("record", "settleMs", default=1500)),
                     help="quiet period on /activity: no new activity and nothing in flight (default 1500)")
    rec.add_argument("--max-wait-ms", type=int, default=int(setting("record", "maxWaitMs", default=10000)),
                     help="cap on the settle wait (default 10000)")
    rec.add_argument("--min-launch-ms", type=int, default=int(setting("record", "minLaunchMs", default=0)),
                     help="never screenshot sooner than this after launch: raise it for an app whose launch screen "
                          "outlives the first frames (default 0)")
    rec.add_argument("--min-shown-ms", type=int, default=int(setting("record", "minShownMs", default=300)),
                     help="never screenshot sooner than this after /status first reports the variant as shown: raise "
                          "it when a system overlay (the home indicator, with --capture simctl) is still fading (default 300)")
    rec.add_argument("--screenshot-mask", choices=SCREENSHOT_MASKS, default=DEFAULT_SCREENSHOT_MASK,
                     help="simctl's --mask policy for the display's cut-out and rounded corners on every screenshot "
                          f"(default {DEFAULT_SCREENSHOT_MASK}: the Dynamic Island area is always black; `ignored` "
                          "saves the framebuffer, island or not)")
    rec.add_argument("--capture", choices=CAPTURE_MODES, default=DEFAULT_CAPTURE_MODE,
                     help="where screenshots come from: `simctl` (simctl io screenshot: the display, with the status "
                          "bar and, bistably, the home indicator and Dynamic Island) or `app` (the in-app server's "
                          "GET /screenshot: the app's own windows only, no system overlays). Runs compare only "
                          f"within one mode (default {DEFAULT_CAPTURE_MODE})")
    rec.add_argument("--band-retakes", type=int, default=3,
                     help="screenshots taken after the first pair when the Dynamic Island or home indicator bands "
                          "still differ; then the last is kept with unstableBands (default 3; -1 takes one "
                          "screenshot and checks nothing)")
    rec.add_argument("--band-interval-ms", type=int, default=400,
                     help="wait between the screenshots of the band check (default 400)")
    rec.add_argument("--health-timeout", type=float, default=float(setting("record", "healthTimeout", default=20)), help="seconds to wait for /status (default 20)")
    rec.add_argument("--budget-minutes", type=float, help="stop starting captures after this long, then finish normally")
    rec.add_argument("--zip", action="store_true", help="export the run as a zip once it is recorded")
    rec.add_argument("--run-id", help="the run folder's name (default <source>-<yyyymmdd-hhmmss>)")
    rec.add_argument("--sync-prefix", default=SYNC_PREFIX,
                     help=f"the store's key prefix (S3) for the check that this build has no run there yet (default {SYNC_PREFIX})")
    rec.add_argument("--source", choices=RUN_SOURCES, default="cli",
                     help="who started the run, written to its run.json and used by pruning (default cli; the web UI passes ui)")
    rec.add_argument("--keep-runs", type=int, default=DEFAULT_KEEP_RUNS,
                     help=f"after recording, prune to the newest N runs per source and simulator type, never the "
                          f"pinned run (default {DEFAULT_KEEP_RUNS}; 0 keeps everything)")

    cmp_ = commands.add_parser("compare", parents=[common, judging], help="compare two runs of one simulator type")
    cmp_.add_argument("run")
    cmp_.add_argument("--against", metavar="RUN", help="the run to compare with (default: the pinned baseline of RUN's type)")
    cmp_.add_argument("--channel-threshold", type=int, default=DEFAULT_CHANNEL_THRESHOLD,
                      help=f"a channel delta above this differs (default {DEFAULT_CHANNEL_THRESHOLD})")
    cmp_.add_argument("--max-diff-percent", type=float, default=DEFAULT_MAX_DIFF_PERCENT,
                      help=f"FAIL above this %% of differing pixels (default {DEFAULT_MAX_DIFF_PERCENT})")
    cmp_.add_argument("--allow-missing", action="store_true", help="MISSING screens do not fail the compare")
    cmp_.add_argument("--allow-confirmed", action="store_true",
                      help="a FAIL or MISSING screen whose change a reviewer has confirmed (`review`) does not fail the compare")
    cmp_.add_argument("--force", action="store_true", help="judge again even when the stored comparison is current")
    cmp_.add_argument("--report", action="store_true",
                      help="also write report.html, self-contained with every FAIL's images embedded (~1 MB per FAIL)")

    pin = commands.add_parser("set-baseline", parents=[common], help="pin a run as its simulator type's baseline")
    pin.add_argument("run")
    pin.add_argument("--allow-partial", action="store_true", help="pin a filtered or one-appearance run")
    pin.add_argument("--allow-stopped", action="store_true", help="pin a run that stopped before capturing everything")
    pin.add_argument("--skip-review", action="store_true",
                     help="pin although changes against the current baseline are not all confirmed by a reviewer")

    update = commands.add_parser("update-baseline", parents=[common],
                                 help="copy a partial run's screens over the pinned baseline")
    update.add_argument("run")
    update.add_argument("--skip-review", action="store_true", help="copy screens whose change no reviewer confirmed")

    accept = commands.add_parser("accept", parents=[common], help="copy one screen of a run over the pinned baseline")
    accept.add_argument("run")
    accept.add_argument("name")
    accept.add_argument("appearance", choices=APPEARANCES)
    accept.add_argument("--skip-review", action="store_true", help="copy the screen although no reviewer confirmed its change")

    review = commands.add_parser("review", parents=[common],
                                 help="list the changes of a run's comparison with their review state, or mark one")
    review.add_argument("run")
    review.add_argument("name", nargs="?", help="with APPEARANCE and --expect/--confirm/--reject/--withdraw: the screen to mark")
    review.add_argument("appearance", nargs="?", choices=APPEARANCES)
    review.add_argument("--against", metavar="RUN", help="the run compared with (default: the pinned baseline of RUN's type)")
    marks = review.add_mutually_exclusive_group()
    marks.add_argument("--expect", action="store_true", help="the author: this change is intended (--note says why)")
    marks.add_argument("--confirm", action="store_true", help="a reviewer other than the author: agreed")
    marks.add_argument("--reject", action="store_true", help="a reviewer: not agreed (--note is the reason, required)")
    marks.add_argument("--withdraw", action="store_true", help="the author: take the expectation back")
    review.add_argument("--note", default=None, help="why (expect) or the reason (reject)")
    review.add_argument("--as", dest="reviewer", default=None, metavar="\"Name <email>\"",
                        help=f"who is marking (default: ${REVIEWER_ENV}, else git user.name / user.email)")
    review.add_argument("--json", action="store_true", help="print the changes and their reviews as JSON")

    delete = commands.add_parser("delete-run", parents=[common], help="delete a run and its comparisons")
    delete.add_argument("run")

    prune_ = commands.add_parser("prune", parents=[common], help="delete older runs and comparisons that are no longer current")
    prune_.add_argument("--keep", type=int, default=DEFAULT_KEEP_RUNS,
                        help=f"runs kept per source and simulator type, besides the pinned one (default {DEFAULT_KEEP_RUNS})")

    export = commands.add_parser("export", parents=[common], help="zip a run")
    export.add_argument("run")
    export.add_argument("--zip", action="store_true", help="the format (the only one; accepted for clarity)")
    export.add_argument("--out", type=Path, help="the zip's path (default <catalog>/screencatalog_<run-id>.zip)")

    imp = commands.add_parser("import", parents=[common], help="unzip an exported run into runs/")
    imp.add_argument("archive", type=Path)

    sync = commands.add_parser("sync", help="share runs and baselines.json through S3 (the aws CLI)")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True, metavar="push|pull|delete")
    remote = argparse.ArgumentParser(add_help=False)
    remote.add_argument("--bucket", default=None, help=f"the S3 bucket (default ${BUCKET_ENV})")
    remote.add_argument("--prefix", default=SYNC_PREFIX, help=f"the key prefix (default {SYNC_PREFIX})")
    remote.add_argument("--force", action="store_true",
                        help="push over a higher remote revision or a remote pin you do not have; pull over a higher "
                             "local revision")
    push = sync_commands.add_parser("push", parents=[common, remote],
                                    help="push the pinned runs and baselines.json, or the runs named")
    push.add_argument("runs", nargs="*", metavar="RUN")
    push.add_argument("--all", action="store_true",
                      help="every finished run, partial and stopped ones too (never one still capturing); with "
                           "RUNs, lets those be partial or stopped")
    pull = sync_commands.add_parser("pull", parents=[common, remote],
                                    help="pull baselines.json and the pinned runs missing or behind here, or the runs named")
    pull.add_argument("runs", nargs="*", metavar="RUN")
    pull.add_argument("--list", action="store_true", help="print the remote runs with their facts (JSON) and pull nothing")
    delete_remote = sync_commands.add_parser("delete", parents=[common, remote],
                                             help="delete runs from the shared store (never its baseline of a type)")
    delete_remote.add_argument("runs", nargs="+", metavar="RUN")

    commands.add_parser("list", parents=[common], help="JSON: runs, pins, and which runs each compares with")
    return parser.parse_args(argv)


def review_command(catalog: Path, args: argparse.Namespace, output: Output) -> int:
    """`review <run> [--against RUN]` lists the changes of the run's current comparison with their review
    state (exit 1 while any is not confirmed); with NAME APPEARANCE and a mark, writes that event first."""
    run = load_run(catalog, args.run)
    against_id = args.against or pinned(catalog, run_device_dir(run))
    if not against_id:
        raise CatalogError(f"no baseline is pinned for {type_label(run)}: pass --against <run>")
    stored = current_compare(catalog, args.run, against_id)
    if stored is None:
        raise CatalogError(f"no current comparison of {args.run} with {against_id}: run `compare {args.run}"
                           + (f" --against {against_id}" if args.against else "") + "` first")
    rows = stored.get("rows") or []
    kind = next((k for k in REVIEW_KINDS if getattr(args, k)), None)
    if kind or args.name or args.appearance:
        if not (kind and args.name and args.appearance):
            raise CatalogError("marking takes NAME, APPEARANCE and one of --expect, --confirm, --reject, --withdraw")
        row = next((r for r in rows if r["name"] == args.name and r["appearance"] == args.appearance), None)
        pair = review_pair(catalog, row) if row else None
        if pair is None:
            raise CatalogError(f"{args.name} ({args.appearance}) is not a FAIL or MISSING screen of that comparison")
        by = parse_person(args.reviewer) if args.reviewer else git_user()
        state = add_review_event(catalog, pair, kind, by, args.note, run_id=args.run, against_id=against_id,
                                 against_revision=stored.get("againstRevision"))
        output.line(f"{args.name} ({args.appearance}): {review_line(state)}")
    reviews = review_rows(catalog, rows)
    if args.json:
        output.line(json.dumps({"run": args.run, "against": against_id, "summary": review_summary(reviews),
                                "changes": [dict(v, name=k[0], appearance=k[1]) for k, v in reviews.items()]}, indent=2))
    else:
        for (name, appearance), review in reviews.items():
            output.line(f"  {appearance:5} {name}: {review_line(review)}")
        summary = review_summary(reviews)
        output.line(f"{summary['changes']} change{'s' if summary['changes'] != 1 else ''}: {summary['open']} open, "
                    f"{summary['expected']} expected, {summary['confirmed']} confirmed, {summary['rejected']} rejected")
    return 0 if all(r["state"] == "confirmed" for r in reviews.values()) else 1


def main(argv: list[str]) -> int:
    return run_catalog(parse_args(argv))


def apply_config_flag(argv: list[str]) -> list[str]:
    """`--config <path>`: the config is read at import, so this sets $SCREENCATALOG_CONFIG and starts the
    script again without the flag. Returns `argv` unchanged when the flag is absent or names the file
    already loaded."""
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            value, rest = argv[index + 1], argv[:index] + argv[index + 2:]
        elif arg.startswith("--config="):
            value, rest = arg.split("=", 1)[1], argv[:index] + argv[index + 1:]
        else:
            continue
        chosen = Path(value).expanduser().resolve()
        if not chosen.is_file():
            sys.exit(f"screencatalog: no config file at {chosen}")
        if chosen == CONFIG_PATH:
            return rest
        os.environ[CONFIG_ENV] = str(chosen)
        os.execv(sys.executable, [sys.executable, str(Path(sys.argv[0]).resolve()), *rest])
    return argv


def cli() -> int:
    """Entry point of the `screencatalog` command."""
    return main(apply_config_flag(sys.argv[1:]))


def run_catalog(args: argparse.Namespace, output: Output = CONSOLE, stop_event: threading.Event | None = None,
                check_duplicate: bool = True) -> int:
    """One command for parsed arguments. screencatalog_ui.py calls this in-process with its
    own `output` and a `stop_event`; the CLI calls it through `main`. A refused operation prints its
    reason and returns 2."""
    catalog = args.catalog_dir.resolve()
    command = args.command
    try:
        if command == "list":
            output.line(json.dumps(catalog_listing(catalog), indent=2))
            return 0
        if command in ("record", "compare"):
            try:
                exclusions = load_exclusions(args.exclude_file)
            except (ValueError, json.JSONDecodeError) as error:
                raise CatalogError(str(error)) from error
            if command == "record":
                if not args.bundle:
                    raise CatalogError(f"no bundle id: set app.bundleId in {CONFIG_NAME} or pass --bundle")
                return record(args, catalog, exclusions, output, stop_event, check_duplicate=check_duplicate)
            result = compare_runs(catalog, args.run, args.against, output, channel_threshold=args.channel_threshold,
                                  max_diff_percent=args.max_diff_percent, exclusions=exclusions, force=args.force,
                                  report=args.report)
            return verdict(result["rows"], args.allow_missing, result["reviews"] if args.allow_confirmed else None)
        if command == "set-baseline":
            set_baseline(catalog, args.run, allow_partial=args.allow_partial, allow_stopped=args.allow_stopped,
                         skip_review=args.skip_review, output=output)
        elif command == "update-baseline":
            update_baseline(catalog, args.run, output, skip_review=args.skip_review)
        elif command == "accept":
            accept_screen(catalog, args.run, args.name, args.appearance, output, skip_review=args.skip_review)
        elif command == "review":
            return review_command(catalog, args, output)
        elif command == "delete-run":
            delete_run(catalog, args.run, output)
        elif command == "prune":
            result = prune(catalog, args.keep, output=output)
            if not result["runs"] and not result["compares"]:
                output.line("nothing to prune")
        elif command == "export":
            archive_path = export_run(catalog, args.run, args.out)
            output.line(f"zip: {archive_path} ({archive_path.stat().st_size // 1024} KiB)")
        elif command == "import":
            import_run(catalog, args.archive, output)
        elif command == "sync":
            remote = make_remote(args.bucket, args.prefix, output, stop_event)
            if args.sync_command == "push":
                sync_push(catalog, remote, args.runs, include_all=args.all, force=args.force, output=output)
            elif args.sync_command == "delete":
                sync_delete(catalog, remote, args.runs, output=output)
            elif args.list:
                output.line(json.dumps(remote_listing(catalog, remote), indent=2))
            else:
                sync_pull(catalog, remote, args.runs, force=args.force, output=output)
        return 0
    except CatalogError as error:
        output.error(f"error: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(cli())
