#!/usr/bin/env python3
"""Local web UI for the screen catalog's runs: record a run on a simulator type, browse every run
in a gallery with a zoomable lightbox, compare two runs of one type (review the failures, accept a
screen into the baseline), and pin one run per type as its baseline. Docs: docs/reference.md
§ Web UI; the model it shows is § Runs, baselines and comparisons.

  python3 tool/screencatalog_ui.py [--config screencatalog.json] [--port 8899] [--catalog-dir <dir>]

Serves one page and a JSON API on 127.0.0.1 only.
Records call screencatalog.py's `run_catalog` in-process, one at a time, in a background
thread; comparisons call its `compare_runs` in their own background thread (a comparison is a file
operation, so one can run while a record captures). Recordings' Sync panel pushes and pulls runs and
baselines.json through the configured store (`sync_push` / `sync_pull`: S3 through the aws CLI, or a folder) as
jobs like a record. Standard library only, like the capture script (Xcode's python3 is 3.9).
"""

# Xcode ships python3 3.9.6; keep annotations lazy so `X | None` hints parse there.
from __future__ import annotations

import argparse
import collections
import json
import operator
import os
import plistlib
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screencatalog as sc  # noqa: E402

UDID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
DEVICE_TYPE_RE = re.compile(r"^[A-Za-z0-9._()-]{1,120}@[A-Za-z0-9._-]{1,60}$")
LINES_KEPT = 2000
MAX_BODY = 64 * 1024
DERIVED_DATA = Path.home() / "Library" / "Developer" / "Xcode" / "DerivedData"
THUMB_DIR = sc.THUMBS_DIR  # <catalog>/.thumbs/runs/<run>/<appearance>/<name>.png, .thumbs/compares/<key>/diff/…; mtime = the source's
THUMB_WIDTH = 240
MASK_DIR = "mask"  # .thumbs/compares/<key>/mask/<appearance>/<name>.png (+ .thumb.png, .key): removed with the comparison
MASK_VERSION = 1  # part of the mask cache key: bump when the mask's pixels change meaning
DEFAULT_CHANNEL_THRESHOLD = sc.DEFAULT_CHANNEL_THRESHOLD
DEFAULT_MAX_DIFF_PERCENT = sc.DEFAULT_MAX_DIFF_PERCENT
COMPARE_WAIT_SECONDS = 1.5  # POST /api/compare answers with the result when it is ready within this, else 202
SYNC_REMOTE_TTL = 30  # seconds GET /api/sync/remote reuses its listing (each listing is one aws call per remote run)
CLONE_PREFIX = sc.setting("ui", "clonePrefix", default="screencatalog-clone-")  # simulators this page clones from a template: screencatalog-clone-1, -2, …
MAX_CLONES = int(sc.setting("ui", "maxClones", default=8))  # simulators per run, the template included; each simulator costs a core and ~1 GB
GB_PER_SIMULATOR = 2.5  # an estimate of the memory one booted simulator plus the app under capture takes; sizes `recommendedClones`
FALLBACK_CLONES = 2  # the recommendation when the host's memory cannot be read


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def valid_compare_key(key) -> bool:
    """`<run>__<against>__r<revision>`, the folder name under compares/."""
    if not isinstance(key, str):
        return False
    parts = key.split("__")
    return (len(parts) == 3 and sc.valid_run_id(parts[0]) and sc.valid_run_id(parts[1])
            and re.fullmatch(r"r[0-9]{1,6}", parts[2]) is not None)


def catalog_call(fn, *args, **kwargs):
    """Runs a screencatalog operation; its refusal (CatalogError) becomes 404 for an unknown run and
    409 otherwise, with the module's own message."""
    try:
        return fn(*args, **kwargs)
    except sc.CatalogError as error:
        message = str(error)
        raise RequestError(HTTPStatus.NOT_FOUND if message.startswith("no run ") else HTTPStatus.CONFLICT, message)


BUILD_LOGS_DIR = "build-logs"  # <catalog>/build-logs/<run>.log: a record's full xcodebuild output


class BuildLogSummary:
    """What of xcodebuild's output reaches the job log: every distinct `error:` line (once, with how many
    times it repeated), the `** BUILD … **` verdict lines, and at the end the warning count and where the
    full output is. Warnings and their source excerpts stay in the file."""

    def __init__(self, add) -> None:
        self.add = add
        self.warnings = 0
        self.errors: dict[str, int] = {}

    def line(self, text: str) -> None:
        if "warning:" in text:
            self.warnings += 1
        elif "error:" in text:
            key = re.sub(r"^\S+:\d+:\d+: ", "", text.strip())
            self.errors[key] = self.errors.get(key, 0) + 1
            if self.errors[key] == 1:
                self.add(text.rstrip())
        elif text.startswith("** "):
            self.add(text.rstrip())

    def finish(self, code: int, log_path: Path) -> None:
        repeated = [f"{n} × {key[:80]}" for key, n in self.errors.items() if n > 1]
        if repeated:
            self.add("repeated: " + "; ".join(repeated))
        self.add(f"xcodebuild exit {code}: {self.warnings} warning line{'s' if self.warnings != 1 else ''}, "
                 f"{len(self.errors)} distinct error line{'s' if len(self.errors) != 1 else ''}; full output: {log_path}")


# MARK: - Jobs

class Job:
    """One record or build: its log ring buffer, progress and outcome."""

    def __init__(self, job_id: str, mode: str, params: dict):
        self.id = job_id
        self.mode = mode
        self.params = params
        self.lock = threading.Lock()
        self.lines: collections.deque = collections.deque(maxlen=LINES_KEPT)
        self.seq = 0
        self.done = 0
        self.total = 0
        self.started = time.time()
        self.finished: float | None = None
        self.capture_started: float | None = None
        self.last_progress: float | None = None
        self.exit_code: int | None = None
        self.summary: dict | None = None
        self.rows: list[dict] | None = None
        self.rows_mode: str | None = None
        self.phase: str | None = None
        self.stop_event = threading.Event()
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None

    def add(self, text: str) -> None:
        with self.lock:
            for part in str(text).splitlines() or [""]:
                self.seq += 1
                self.lines.append((self.seq, part))

    def set_progress(self, done: int, total: int) -> None:
        now = time.monotonic()
        with self.lock:
            if self.capture_started is None or done == 0:
                self.capture_started = now
            self.done, self.total, self.last_progress = done, total, now

    @property
    def active(self) -> bool:
        return self.finished is None

    def per_variant_ms(self) -> int | None:
        if self.mode != "record" or not self.done or self.capture_started is None or self.last_progress is None:
            return None
        return int((self.last_progress - self.capture_started) * 1000 / self.done)

    def eta_seconds(self) -> int | None:
        per = self.per_variant_ms()
        if per is None or not self.active or self.mode != "record":
            return None
        since_last = time.monotonic() - (self.last_progress or time.monotonic())
        return max(0, int(per * (self.total - self.done) / 1000 - since_last))

    def status(self, since: int) -> dict:
        with self.lock:
            lines = [{"seq": s, "text": t} for s, t in self.lines if s > since]
            last_seq = self.seq
        return {
            "active": self.active,
            "runId": self.id,
            "mode": self.mode,
            "started": sc.iso(self.started),
            "finished": sc.iso(self.finished) if self.finished else None,
            "done": self.done,
            "total": self.total,
            "perVariantMs": self.per_variant_ms(),
            "eta": self.eta_seconds(),
            "lines": lines,
            "lastSeq": last_seq,
            "exitCode": self.exit_code,
            "summary": self.summary,
            "udids": self.params.get("udids") or None,
            "phase": self.phase,
        }


class JobOutput(sc.Output):
    def __init__(self, job: Job):
        self.job = job

    def line(self, text: str) -> None:
        self.job.add(text)

    def error(self, text: str) -> None:
        self.job.add(text)

    def progress(self, done: int, total: int) -> None:
        self.job.set_progress(done, total)

    def rows(self, mode: str, rows: list[dict]) -> None:
        self.job.rows, self.job.rows_mode = rows, mode

    def phase(self, text: str | None) -> None:
        self.job.phase = text


class CompareTask:
    """One comparison computing in the background: `compare_runs(run, against)`."""

    def __init__(self, run_id: str, against_id: str, force: bool):
        self.run, self.against, self.force = run_id, against_id, force
        self.started = time.time()
        self.finished: float | None = None
        self.error: str | None = None
        self.key: str | None = None
        self.done = threading.Event()
        self.cancel = threading.Event()  # set by POST /api/compare/cancel; compare_runs stops on it
        self.cancelled = False
        self.lines: collections.deque = collections.deque(maxlen=200)
        self.pairs_done = 0
        self.pairs_total: int | None = None
        self.first: tuple[float, int] | None = None  # (when, pairs done) at the first report: identical pairs are free

    def set_progress(self, done: int, total: int) -> None:
        if self.first is None:
            self.first = (time.time(), done)
        self.pairs_done, self.pairs_total = done, total

    def per_pair_ms(self) -> int | None:
        if self.first is None or self.pairs_done <= self.first[1]:
            return None
        return round((time.time() - self.first[0]) * 1000 / (self.pairs_done - self.first[1]))

    def info(self) -> dict:
        end = self.finished or time.time()
        per_pair = self.per_pair_ms()
        eta = (round(per_pair * (self.pairs_total - self.pairs_done) / 1000)
               if per_pair is not None and self.pairs_total is not None else None)
        return {"run": self.run, "against": self.against, "key": self.key, "started": sc.iso(self.started),
                "seconds": round(end - self.started, 1), "active": not self.done.is_set(), "error": self.error,
                "cancelled": self.cancelled, "done": self.pairs_done, "total": self.pairs_total,
                "perPairMs": per_pair, "eta": eta}


class TaskOutput(sc.Output):
    def __init__(self, task: CompareTask):
        self.task = task

    def line(self, text: str) -> None:
        self.task.lines.append(text)

    def error(self, text: str) -> None:
        self.task.lines.append(text)

    def progress(self, done: int, total: int) -> None:
        self.task.set_progress(done, total)


# MARK: - Runs as the page shows them

def type_text(data: dict) -> str:
    """`iPhone 17 · iOS 27.0`: a run's simulator type as the page names it."""
    model, os_name = sc.run_type(data)
    return f"{model or '?'} · {os_name or '?'}"


def run_facts(catalog: Path, run_id: str, data: dict, pins: dict[str, str], history: bool = False) -> dict:
    """What the page shows of one run: run.json's facts, flattened, plus its derived state."""
    described = sc.describe_run(catalog, run_id, data, pins)
    kind = data.get("deviceType") or {}
    app, git, found = data.get("app") or {}, data.get("git") or {}, data.get("filter") or {}
    facts = {
        "id": run_id, "title": "Baseline" if described["pinned"] else "Run", "pinned": described["pinned"],
        "revision": described["revision"], "revisionAt": data.get("revisionAt"),
        "started": data.get("started"), "finished": data.get("finished"), "source": sc.run_source(data),
        "deviceDir": kind.get("deviceDir"), "model": kind.get("model"), "os": kind.get("os"),
        "osBuild": kind.get("osBuild"), "typeLabel": type_text(data),
        "filter": {"only": list(found.get("only") or []), "feature": list(found.get("feature") or []),
                   "label": found.get("label") or sc.filter_label(list(found.get("only") or []),
                                                                   list(found.get("feature") or []))},
        "appearance": data.get("appearance") or "both", "partial": described["partial"],
        "stopped": described["stopped"], "active": described["active"], "complete": described["complete"],
        "counts": dict(sorted((data.get("counts") or {}).items(), key=lambda kv: (kv[0] != "light", kv[0]))),
        "statuses": data.get("statuses") or {},
        "app": app_label(app), "appSha": app.get("sha256"), "appModified": app.get("modified"),
        "git": {"sha": git.get("sha"), "dirty": git.get("dirty")} if git else None,
        "simulators": [{"udid": s.get("udid"), "name": s.get("name")} for s in data.get("simulators") or []
                       if isinstance(s, dict)],
        "elapsedSeconds": data.get("elapsedSeconds"), "perVariantMs": data.get("perVariantMs"),
    }
    if history:
        facts["history"] = sorted((e for e in data.get("history") or [] if isinstance(e, dict)),
                                  key=lambda e: e.get("at") or "", reverse=True)
    return facts


def api_review(review: dict | None) -> dict | None:
    """A screen's review for the page: its state, who, the note or reason, when, its events, and the pair's
    short hashes (what the verdict is keyed by)."""
    if review is None:
        return None
    pair = review.get("pair") or {}
    return {"state": review["state"], "author": review.get("author"), "reviewer": review.get("reviewer"),
            "note": review.get("note"), "reason": review.get("reason"), "at": review.get("at"),
            "events": review.get("events") or [],
            "pair": {"current": pair.get("current", "")[:16], "baseline": pair.get("baseline", "")[:16]}}


# MARK: - Where a run's commit stands against main

main_head, standing = sc.main_head, sc.standing  # from the repository the server runs in; the gate is sc.check_on_main


def os_version_key(os_name: str | None) -> list[int]:
    return [int(p) for p in re.findall(r"\d+", os_name or "")]


def natural_key(text: str | None) -> list[tuple]:
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in re.split(r"(\d+)", text or "") if p]


def type_order(model: str | None, os_name: str | None):
    """Newest iOS first (support for older versions is dropped over time), then natural model order
    (iPhone 17, iPhone 17 Pro, iPhone 18 Pro)."""
    return ([-n for n in os_version_key(os_name)], natural_key(model))


# MARK: - The app

class CatalogUI:
    def __init__(self, catalog_dir: Path, bridge_port: int = sc.DEFAULT_PORT, bundle: str = sc.DEFAULT_BUNDLE,
                 exclude_file: Path = sc.DEFAULT_EXCLUDE_FILE, runner=None, warm_thumbnails: bool = True,
                 thumb_processes: int | None = None, keep_runs: int = sc.DEFAULT_KEEP_RUNS,
                 max_diff_percent: float = DEFAULT_MAX_DIFF_PERCENT,
                 channel_threshold: int = DEFAULT_CHANNEL_THRESHOLD, compare_wait: float = COMPARE_WAIT_SECONDS,
                 sync_prefix: str = sc.SYNC_PREFIX, repo: Path = sc.REPO_ROOT, user: dict | None = None,
                 builder=None, built_app_finder=None, check_main: bool = True):
        self.catalog = catalog_dir.resolve()
        self.builder = builder or self._xcodebuild  # (job, destination udid) -> xcodebuild's exit code
        self.built_app_finder = built_app_finder or built_app  # () -> the Debug simulator .app, or None
        self.repo = repo
        self.check_main = check_main  # the baseline gate (sc.check_on_main); False only in tests of the rest
        self.user_override = user  # who reviews: given here (tests; {} is nobody), else git's user in `repo`
        self.standing_cache: dict[tuple[str, str], dict | None] = {}
        self.keep_runs = keep_runs
        self.bridge_port = bridge_port
        self.bundle = bundle
        self.exclude_file = exclude_file
        self.max_diff_percent = max_diff_percent
        self.channel_threshold = channel_threshold
        self.compare_wait = compare_wait
        # (args, output, stop_event) -> exit code. The page checks for a run of the same build itself
        # (`_duplicate_of`, which names it in the summary), so the capture is told not to repeat that.
        self.runner = runner or (lambda args, output, stop_event: sc.run_catalog(args, output, stop_event, check_duplicate=False))
        self.lock = threading.Lock()
        self.job: Job | None = None
        self.menu_cache: dict[str, list[dict]] = {}
        self.warm_thumbnails = warm_thumbnails
        self.thumb_lock = threading.Lock()
        self.thumb_locks: dict[str, threading.Lock] = {}
        self.warm_queue: collections.deque = collections.deque()
        self.warm_thread: threading.Thread | None = None
        # Decoding a screenshot is ~60 ms through ImageIO, ~1 s in pure Python. In a worker process it neither
        # holds this server's GIL (which starved every other request) nor runs one at a time; comparisons
        # judge their pairs in the same pool. 0: in-process.
        self.thumb_processes = min(4, os.cpu_count() or 1) if thumb_processes is None else thumb_processes
        self.thumb_pool: ProcessPoolExecutor | None = None
        # One writer of comparisons and of the pinned run at a time: a compare, an accept, an update,
        # a pin and a delete each hold it, so none of them reads a run another is changing.
        self.compare_lock = threading.Lock()
        self.tasks_lock = threading.Lock()
        self.tasks: dict[tuple[str, str], CompareTask] = {}
        self.size_cache: dict[str, tuple[tuple, int]] = {}
        self.sync_prefix = sync_prefix
        self.sync_lock = threading.Lock()
        self.sync_cache: tuple[float, dict] | None = None

    # Jobs (record, build): one at a time

    def _start(self, mode: str, params: dict, target, job_id: str | None = None) -> Job:
        with self.lock:
            if self.job is not None and self.job.active:
                raise RequestError(HTTPStatus.CONFLICT, f"run {self.job.id} ({self.job.mode}) is still active")
            job = Job(job_id or new_job_id(mode), mode, params)
            self.job = job
            # What the job was started with, before its thread can change job.params (a record mints its run
            # id there as soon as the capture starts): the 202 answer is built from this, never the live dict.
            job.accepted_params = dict(params)
            job.thread = threading.Thread(target=self._guarded, args=(job, target), name=f"job-{job.id}", daemon=True)
            job.thread.start()
            return job

    def _guarded(self, job: Job, target) -> None:
        try:
            code = target(job)
        except Exception as error:  # surfaced in the log and the summary, never swallowed
            job.add(f"error: {type(error).__name__}: {error}")
            code = 2
        self._finish(job, code)

    def _finish(self, job: Job, code: int) -> None:
        summary = {"exitCode": code, "runId": job.id, "mode": job.mode,
                   "elapsedSeconds": round(time.time() - job.started, 1), "perVariantMs": job.per_variant_ms(),
                   "stopped": job.stop_event.is_set()}
        if job.rows is not None:
            summary["counts"] = counts_of(job.rows)
        if job.mode == "record":
            run_id = job.params.get("run")
            path = self.catalog / sc.RUNS_DIR / run_id if sc.valid_run_id(run_id) else None
            data = sc.read_run(path) if path is not None else None
            if data is not None:
                # The page's own facts: how the run was started and how fast it went.
                params = {k: job.params.get(k) for k in ("deviceType", "clones", "udids", "appearance", "settleMs",
                                                         "budgetMinutes")}
                sc.write_run(path, params=params, perVariantMs=job.per_variant_ms())
                pin = sc.pinned(self.catalog, sc.run_device_dir(data))
                summary.update(run=run_id, deviceDir=sc.run_device_dir(data), typeLabel=type_text(data), pinned=pin,
                               comparable=pin is not None and pin != run_id)
        if job.mode in ("build", "push", "pull"):
            summary.update(job.params.get("result") or {})
        if job.params.get("duplicate"):
            summary["duplicate"] = job.params["duplicate"]
        job.exit_code, job.summary = code, summary
        job.add(f"finished: exit {code}")
        job.finished = time.time()

    def stop(self) -> bool:
        job = self.job
        if job is None or not job.active:
            return False
        job.stop_event.set()
        if job.process is not None and job.process.poll() is None:
            job.process.terminate()
        job.add("stop requested: finishing the captures in flight")
        return True

    def status(self, since: int) -> dict:
        if self.job is None:
            out = {"active": False, "runId": None, "mode": None, "started": None, "finished": None, "done": 0,
                   "total": 0, "perVariantMs": None, "eta": None, "lines": [], "lastSeq": 0, "exitCode": None,
                   "summary": None, "udids": None, "phase": None}
        else:
            out = self.job.status(since)
        with self.tasks_lock:
            out["compares"] = [t.info() for t in self.tasks.values() if not t.done.is_set()]
        return out

    def start_run(self, body: dict) -> Job:
        """A record (the only capture) into a new run folder, runs/ui-<yyyymmdd-hhmmss>/."""
        if body.get("mode") == "compare":
            raise RequestError(HTTPStatus.BAD_REQUEST, "record is the only capture: compare two recorded runs with "
                                                       "POST /api/compare {\"run\": <run>, \"against\": <run>}")
        params = validate_run(body)
        params["run"] = None  # minted when the capture starts, after the build, so the id is the recorded time
        if params["deviceType"]:
            # Answered now, not in the run's log: an unknown type (404); and, when the build is skipped, a
            # template with no app and no build in DerivedData to install (409). With the build, the record
            # builds first and installs what it built, so a bare simulator is fine.
            plan = fleet_plan(params["deviceType"], params["clones"], self.bundle, require_app=False)
            if params["skipBuild"] and plan["app"] is None and self.built_app_finder() is None:
                raise RequestError(HTTPStatus.CONFLICT, f"{plan['template']['name']} ({plan['template']['udid']}) has no "
                                                        f"{self.bundle} and DerivedData has no Debug build to install: record "
                                                        "with the build, or build the app in Xcode first")
        try:
            sc.parse_args(self.run_argv(dict(params, run=sc.new_run_id(self.catalog, "ui"))))
        except SystemExit:
            raise RequestError(HTTPStatus.BAD_REQUEST, "the run's arguments do not parse")

        def target(job: Job) -> int:
            fleet = None
            try:
                if params["deviceType"]:
                    built = self._build_for_record(job, params)
                    if built is False:
                        return 1
                    fleet = resolve_fleet(params["deviceType"], params["clones"], self.bundle, job.add, built=built)
                    params["udids"], params["fleet"] = fleet["udids"], fleet
                    if job.stop_event.is_set():
                        job.add("stopped before capturing")
                        return 1
                    duplicate = self._duplicate_of(job, fleet["template"])
                    if duplicate is not None:
                        params["duplicate"] = duplicate
                        return 1
                params["run"] = sc.new_run_id(self.catalog, "ui")
                job.add(f"run {params['run']}")
                argv = self.run_argv(params)
                job.add("$ screencatalog.py " + shlex.join(argv))
                return self.runner(sc.parse_args(argv), JobOutput(job), job.stop_event)
            finally:
                if fleet and fleet["clones"]:
                    job.add("shutting the clones down; the next run reuses them (Delete clones removes them)")
                    shutdown_clones(fleet["clones"], job.add)

        return self._start("record", params, target)

    def run_argv(self, params: dict) -> list[str]:
        argv = ["record", "--catalog-dir", str(self.catalog), "--bundle", self.bundle,
                "--exclude-file", str(self.exclude_file), "--appearance", params["appearance"],
                "--port", str(self.bridge_port), "--settle-ms", str(params["settleMs"]),
                "--source", "ui", "--keep-runs", str(self.keep_runs)]
        if params.get("run"):
            argv += ["--run-id", params["run"]]
        for udid in params["udids"]:
            argv += ["--udid", udid]
        if params["budgetMinutes"]:
            argv += ["--budget-minutes", repr(params["budgetMinutes"])]
        for value in params["only"]:
            argv += ["--only", value]
        for value in params["feature"]:
            argv += ["--feature", value]
        return argv

    def _duplicate_of(self, job: Job, template_udid: str) -> dict | None:
        """The complete run of the build the template now holds, of its type and this capture mode, in this
        catalog or in the bucket; None when there is none (or the tree is dirty). A record of it would be
        the same pixels again, so the record ends here with the run named in the log and the summary."""
        try:
            device, app = sc.device_info(template_udid), sc.app_binary(template_udid, self.bundle)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
            job.add(f"warning: could not read the template's app for the duplicate check: {error}")
            return None
        git = sc.git_state()
        device_dir = sc.device_dir_name(device["model"], device["os"])
        facts = {"deviceType": {"model": device["model"], "os": device["os"], "deviceDir": device_dir}, "git": git,
                 "app": app, "captureMode": sc.DEFAULT_CAPTURE_MODE}
        found = sc.find_duplicate(sc.iter_runs(self.catalog), device_dir, git, app.get("sha256"), sc.DEFAULT_CAPTURE_MODE)
        where = "local"
        if found is None and sc.sync_available():
            try:
                found = sc.find_duplicate(sc.remote_runs_json(self._remote()), device_dir, git, app.get("sha256"),
                                          sc.DEFAULT_CAPTURE_MODE)
                where = "remote"
            except (RequestError, sc.CatalogError) as error:
                job.add(f"warning: could not check {sc.store_noun()} for a run of this build: {error}")
                found = None
        if found is None:
            return None
        job.add(sc.duplicate_message(found, "in this catalog" if where == "local" else f"in {sc.store_noun()}", facts) + ": nothing captured")
        return {"run": found, "where": where, "git": git.get("sha"), "app": app.get("sha256")}

    def _build_for_record(self, job: Job, params: dict):
        """The build step of a record: an incremental `xcodebuild` of the configured scheme against the shared
        DerivedData (about a minute when nothing changed), unless `skipBuild`. Returns the built .app for
        the install check, None when there is none to check with (the simulators' own apps are used), or
        False when the build failed, which ends the record before any simulator is touched."""
        template = fleet_plan(params["deviceType"], 1, self.bundle, require_app=False)["template"]
        if params["skipBuild"]:
            job.add("build skipped (Skip the build is on): the install check uses the last Debug build in DerivedData")
        else:
            job.phase = "building"
            job.add(f"building {sc.BUILD['scheme'] or 'the app'} for {template['name']} (incremental)")
            started = time.monotonic()
            code = self.builder(job, template["udid"])
            if code != 0 or job.stop_event.is_set():
                job.add(f"xcodebuild exited {code}" + (" (stopped)" if job.stop_event.is_set() else "") + ": nothing captured")
                return False
            job.add(f"built in {time.monotonic() - started:.0f} s")
            job.phase = None
            self.menu_cache.clear()  # a new build can register new variants
        app = self.built_app_finder()
        if app is None:
            job.add(f"no {sc.BUILD['configuration']}-iphonesimulator build of the app in DerivedData" + (
                "; the simulators keep the app they have" if params["skipBuild"] else ": the build left nothing to install"))
            return None if params["skipBuild"] else False
        identity = sc.app_identity(app)
        job.add(f"built app: {app} ({identity['sha256']}, {identity.get('version')} ({identity.get('build')}))")
        return app

    def _xcodebuild(self, job: Job, udid: str) -> int:
        """Runs the build; its full output goes to <catalog>/build-logs/<run>.log and the job log gets a
        summary (`BuildLogSummary`): `-quiet` still prints every compiler warning with its source excerpt,
        thousands of lines that pushed the record's own lines out of the log's ring buffer."""
        build = sc.BUILD
        container = (["-workspace", build["workspace"]] if build["workspace"] else
                     ["-project", build["project"]] if build["project"] else None)
        if container is None or not build["scheme"]:
            job.add(f"no build is configured: set build.project (or build.workspace) and build.scheme in {sc.CONFIG_NAME}, "
                    "or record with Skip the build")
            return 2
        unset = ["env", *[x for name in build["unsetEnv"] for x in ("-u", name)]] if build["unsetEnv"] else []
        derived = (["-derivedDataPath", str(sc.config_path(build["derivedDataPath"]))] if build["derivedDataPath"] else [])
        command = [*unset, "xcodebuild", *container, "-scheme", build["scheme"], "-configuration", build["configuration"],
                   *derived, "-destination", f"platform=iOS Simulator,id={udid}", *build["extraArgs"], "build", "-quiet"]
        job.add("$ " + shlex.join(command))
        log_path = self.catalog / BUILD_LOGS_DIR / f"{job.id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        summary = BuildLogSummary(job.add)
        job.process = subprocess.Popen(command, cwd=str(sc.CONFIG_DIR), stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert job.process.stdout is not None
        with open(log_path, "w") as full:
            for line in job.process.stdout:
                full.write(line)
                summary.line(line.rstrip("\n"))
        code = job.process.wait()
        summary.finish(code, log_path)
        return code

    def delete_clones(self, body: dict) -> dict:
        """Shuts down and deletes every `screencatalog-clone-*` simulator of the device type."""
        device_type = validate_device_type(body.get("deviceType"))
        with self.lock:
            if self.job is not None and self.job.active:
                raise RequestError(HTTPStatus.CONFLICT, "a run is active; it may be using the clones")
            clones = [s for s in list_simulators() if s["deviceType"] == device_type and is_clone(s)]
            deleted = []
            for sim in sorted(clones, key=lambda s: s["name"]):
                sc.simctl("shutdown", sim["udid"], check=False, timeout=120)
                sc.simctl("delete", sim["udid"], timeout=120)
                deleted.append({"udid": sim["udid"], "name": sim["name"]})
        return {"deviceType": device_type, "deleted": deleted}

    # Sync (S3 or a shared folder): push and pull run as jobs, one at a time with records and builds

    def sync_status(self) -> dict:
        """`GET /api/sync/status`: the store (`kind` s3: the bucket, masked; `kind` directory: the folder),
        `label` (where it is) and `store` (what the page's messages call it), whether the aws CLI is on PATH (always true for a folder), and the
        last push / pull times from sync.json."""
        directory = sc.STORAGE_TYPE == "directory"
        if directory:
            folder = sc.config_path(sc.setting("storage", "path"))
            bucket = label = str(folder) if folder else None
        else:
            bucket = sc.mask_bucket(sc.configured_bucket() or None)
            label = f"s3://{bucket}/{self.sync_prefix}/" if bucket else None
        state = sc.read_sync(self.catalog)
        return {"configured": bucket is not None, "kind": "directory" if directory else "s3", "label": label,
                "store": sc.store_noun(),
                "bucket": bucket, "prefix": "" if directory else self.sync_prefix,
                "env": sc.BUCKET_ENV, "aws": directory or shutil.which("aws") is not None, "lastPush": state["lastPush"],
                "lastPull": state["lastPull"], "baselines": state["baselines"], "runs": state["runs"]}

    def _remote(self, output: sc.Output = sc.Quiet(), stop_event: threading.Event | None = None) -> sc.Remote:
        if not sc.sync_configured():
            raise RequestError(HTTPStatus.CONFLICT, f"sync is not configured: set {sc.BUCKET_ENV} in the server's environment")
        return catalog_call(sc.make_remote, None, self.sync_prefix, output, stop_event)

    def sync_remote(self, refresh: bool = False) -> dict:
        """`GET /api/sync/remote`: the remote runs with their facts and how the local copy stands; cached
        SYNC_REMOTE_TTL seconds (`?refresh=1` skips the cache), dropped after a push or a pull."""
        with self.sync_lock:
            cached = self.sync_cache
            if cached is not None and not refresh and time.monotonic() - cached[0] < SYNC_REMOTE_TTL:
                return dict(cached[1], cached=True)
        listing = catalog_call(sc.remote_listing, self.catalog, self._remote())
        listing["fetchedAt"] = sc.iso()
        with self.sync_lock:
            self.sync_cache = (time.monotonic(), listing)
        return dict(listing, cached=False)

    def sync_delete(self, body: dict) -> dict:
        """`POST /api/sync/delete {"runs": [ids]}`: deletes those runs from the shared store, now (a few seconds
        for a run), never the store's baseline of a type. The remote listing is dropped so the next read is fresh."""
        params = self._sync_params(body, ())
        if not params["runs"]:
            raise RequestError(HTTPStatus.BAD_REQUEST, "runs must name at least one run")
        try:
            return catalog_call(sc.sync_delete, self.catalog, self._remote(), params["runs"], sc.Quiet())
        finally:
            with self.sync_lock:
                self.sync_cache = None

    def _sync_params(self, body: dict, keys: tuple[str, ...]) -> dict:
        runs = body.get("runs")
        if runs is None:
            runs = []
        if not isinstance(runs, list) or len(runs) > 500 or not all(isinstance(r, str) and sc.valid_run_id(r) for r in runs):
            raise RequestError(HTTPStatus.BAD_REQUEST, "runs must be a list of run ids")
        params = {"runs": runs}
        for key in keys:
            if body.get(key) not in (None, True, False):
                raise RequestError(HTTPStatus.BAD_REQUEST, f"{key} must be true or false")
            params[key] = body.get(key) is True
        unknown = set(body) - {"runs", *keys}
        if unknown:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"unknown field(s): {', '.join(sorted(unknown))}")
        return params

    def start_sync(self, mode: str, body: dict) -> Job:
        """`POST /api/sync/push {runs?, all?, force?}` and `POST /api/sync/pull {runs?, force?}`: a job whose
        log is the aws output; refusals (the conflict rule) end it with exit 2 and the reason."""
        params = self._sync_params(body, ("all", "force", "replacePins") if mode == "push" else ("force",))
        self._remote()  # not configured (409) or no aws CLI (409) is answered now, not in the log

        def target(job: Job) -> int:
            output = JobOutput(job)
            try:
                remote = self._remote(output, job.stop_event)
                if mode == "push":
                    result = sc.sync_push(self.catalog, remote, params["runs"], include_all=params["all"],
                                          force=params["force"], replace_pins=params["replacePins"], output=output)
                else:
                    with self.compare_lock:  # a pull rewrites the pinned run
                        result = sc.sync_pull(self.catalog, remote, params["runs"], force=params["force"], output=output)
            except (sc.CatalogError, RequestError) as error:
                job.add(f"error: {error}")
                return 2
            finally:
                with self.sync_lock:
                    self.sync_cache = None
            params["result"] = result
            return 0

        return self._start(mode, params, target)

    # Simulators and the variant list

    def menu(self, udid: str, refresh: bool, device_type: str | None = None) -> dict:
        """The app's variant list from `udid`, or from the template of `device_type` (booted if it
        is shut down)."""
        if device_type:
            template = fleet_plan(validate_device_type(device_type), 1, self.bundle)["template"]
            if template["state"] != "Booted":
                with self.lock:
                    if self.job is not None and self.job.active:
                        raise RequestError(HTTPStatus.CONFLICT, "a run is active")
                    boot_all([template], lambda _line: None)
            udid = template["udid"]
        if not UDID_RE.match(udid or ""):
            raise RequestError(HTTPStatus.BAD_REQUEST, "udid is not a simulator UDID")
        if udid in self.menu_cache and not refresh:
            return {"udid": udid, "cached": True, "variants": self.menu_cache[udid]}
        with self.lock:
            if self.job is not None and self.job.active:
                raise RequestError(HTTPStatus.CONFLICT, "a run is active; the variant list launches the app")
            try:
                variants = sc.enumerate_variants(udid, self.bundle, sc.Bridge(self.bridge_port), self.catalog / "logs", 20)
            except (RuntimeError, OSError, KeyError, ValueError, subprocess.TimeoutExpired) as error:
                raise RequestError(HTTPStatus.BAD_GATEWAY, f"cannot read the variant list: {error}")
        self.menu_cache[udid] = variants
        return {"udid": udid, "cached": False, "variants": variants}

    def exclusions(self) -> dict:
        try:
            entries = sc.load_exclusions(self.exclude_file)
        except (ValueError, json.JSONDecodeError) as error:
            return {"path": str(self.exclude_file), "error": str(error), "exclusions": []}
        return {"path": str(self.exclude_file),
                "exclusions": [{"name": n, "reason": r} for n, r in sorted(entries.items())]}

    # Runs

    def _id(self, value, what: str = "run") -> str:
        if not isinstance(value, str) or not sc.valid_run_id(value):
            raise RequestError(HTTPStatus.BAD_REQUEST, f"{what} must be a run id (a runs/<run>/ folder name)")
        return value

    def _load(self, run_id: str) -> dict:
        path = self.catalog / sc.RUNS_DIR / run_id
        data = None if path.is_symlink() else sc.read_run(path)
        if data is None:
            raise RequestError(HTTPStatus.NOT_FOUND, f"no run {run_id}")
        return data

    def _bytes(self, run_id: str) -> int:
        """The run folder's size, cached until its run.json or manifest changes."""
        path = self.catalog / sc.RUNS_DIR / run_id
        try:
            stamp = ((path / sc.RUN_FILE).stat().st_mtime_ns, (path / sc.MANIFEST_FILE).stat().st_mtime_ns)
        except OSError:
            stamp = None
        cached = self.size_cache.get(run_id)
        if cached is not None and stamp is not None and cached[0] == stamp:
            return cached[1]
        size = sc.dir_bytes(path) if path.is_dir() else 0
        self.size_cache[run_id] = (stamp, size)
        return size

    def runs(self) -> dict:
        """`GET /api/runs`: every run newest first, the pins, and the runs grouped by simulator type."""
        listing = sc.catalog_listing(self.catalog)
        pins = listing["baselines"]
        sync = self.sync_status()
        main = main_head(self.repo)
        runs, groups = [], {}
        for run in listing["runs"]:
            facts = run_facts(self.catalog, run["id"], run, pins)
            facts.update(comparable=run["comparable"], baselineCompare=self._with_review(run["baselineCompare"]),
                         bytes=self._bytes(run["id"]), sync=sync["runs"].get(run["id"]),
                         pushBlocked=sc.push_blocker(run, run["pinned"]), main=self._standing(facts, main))
            runs.append(facts)
            device_dir = facts["deviceDir"] or "?"
            group = groups.setdefault(device_dir, {"deviceDir": device_dir, "model": facts["model"], "os": facts["os"],
                                                   "typeLabel": facts["typeLabel"], "pinned": pins.get(device_dir),
                                                   "runs": []})
            group["runs"].append(run["id"])
        ordered = sorted(groups.values(), key=lambda g: type_order(g["model"], g["os"]))
        return {"catalog": str(self.catalog), "runs": runs, "groups": ordered, "baselines": pins, "sync": sync,
                "main": main}

    def _with_review(self, compare: dict | None) -> dict | None:
        """The listing's baseline comparison with its review summary and how many changes block a pin
        (`blockers`: unconfirmed changes and ERROR rows)."""
        if not compare:
            return None
        stored = sc.read_json(self.catalog / sc.COMPARES_DIR / compare["key"] / sc.COMPARE_FILE)
        rows = (stored.get("rows") if isinstance(stored, dict) else None) or []
        reviews = sc.review_rows(self.catalog, rows)
        return dict(compare, review=sc.review_summary(reviews), blockers=len(sc.review_blockers(rows, reviews)))

    def _standing(self, facts: dict, main: dict | None) -> dict | None:
        """`standing` of a run's commit, cached per (commit, main head): four git calls otherwise."""
        sha = (facts.get("git") or {}).get("sha")
        if not sha or not main:
            return None
        key = (sha, main["sha"])
        if key not in self.standing_cache:
            self.standing_cache[key] = standing(self.repo, sha, main)
        return self.standing_cache[key]

    def run_detail(self, run_id) -> dict:
        """`GET /api/run?id=`: one run's facts (with its history) and its screens; when a comparison of
        it with its type's pinned baseline is current, each screen also carries that comparison's
        verdict and layers (`compare`), which the gallery's "show diff against baseline" adds."""
        run_id = self._id(run_id, "id")
        data = self._load(run_id)
        pins = sc.read_baselines(self.catalog)
        facts = run_facts(self.catalog, run_id, data, pins, history=True)
        facts["bytes"] = self._bytes(run_id)
        facts["main"] = self._standing(facts, main_head(self.repo))
        pin = sc.pinned(self.catalog, sc.run_device_dir(data))
        stored = self._current(run_id, pin) if pin and pin != run_id and not facts["active"] else None
        judged = {(r["name"], r["appearance"]): r for r in (stored or {}).get("rows") or []}
        threshold = self._threshold(stored)
        root = f"{sc.RUNS_DIR}/{run_id}"
        by_name: dict[str, dict] = {}
        warm: list[str] = []
        for row in sc.read_rows(self.catalog / root):
            if row.get("appearance") not in sc.APPEARANCES:
                continue
            variant = by_name.setdefault(row["name"], {"name": row["name"],
                                                       "feature": row.get("feature") or sc.feature_of(row["name"]),
                                                       "light": None, "dark": None})
            file = row.get("file") or sc.shot_relpath(row["appearance"], row["name"])
            shot = {"status": row.get("status"), "reason": row_reason(row), "file": file, "udid": row.get("udid"),
                    "url": self.image_url(f"{root}/{file}"), "thumbUrl": self.image_url(f"{root}/{file}", "thumb"),
                    "compare": None}
            if shot["url"]:
                warm.append(f"{root}/{file}")
            other = judged.get((row["name"], row["appearance"]))
            if other is not None:
                layers = self.api_row(other, stored["key"], threshold)
                shot["compare"] = {k: layers[k] for k in ("status", "reason", "baselineUrl", "baselineThumbUrl",
                                                          "diffUrl", "diffThumbUrl", "maskUrl", "maskThumbUrl")}
            variant[row["appearance"]] = shot
        variants = sorted(by_name.values(), key=lambda v: (v["feature"].lower(), v["name"].lower()))
        first = next((v[a]["file"] for v in variants for a in sc.APPEARANCES if v[a] and v[a]["url"]), None)
        if self.warm_thumbnails:
            self._warm(warm)
        pin_data = sc.read_run(self.catalog / sc.RUNS_DIR / pin) if pin else None
        return {"run": facts, "imageSize": png_size(self.catalog / root / first) if first else None,
                "variants": variants,
                "pin": {"id": pin, "revision": sc.run_revision(pin_data), "revisionAt": pin_data.get("revisionAt")}
                if pin and pin != run_id and pin_data else None,
                "against": {"id": pin, "key": stored["key"], "revision": stored.get("againstRevision"),
                            "counts": counts_of(stored.get("rows") or [])} if stored else None}

    # Comparisons

    def _exclusions(self) -> dict[str, str]:
        try:
            return sc.load_exclusions(self.exclude_file)
        except (ValueError, json.JSONDecodeError) as error:
            raise RequestError(HTTPStatus.CONFLICT, f"the exclusions file does not load: {error}")

    def _current(self, run_id: str, against_id: str | None) -> dict | None:
        """The stored comparison when it is current for this page's settings: both runs unchanged since,
        judged with this tolerance, threshold and exclusions file."""
        if not against_id:
            return None
        data = sc.current_compare(self.catalog, run_id, against_id, channel_threshold=self.channel_threshold,
                                  max_diff_percent=self.max_diff_percent)
        if data is None:
            return None
        try:
            digest = sc.exclusions_digest(self._exclusions())
        except RequestError:
            return None
        return data if (data.get("settings") or {}).get("exclusions") == digest else None

    def _threshold(self, stored: dict | None) -> int:
        value = ((stored or {}).get("settings") or {}).get("channelThreshold", self.channel_threshold)
        return value if isinstance(value, int) and not isinstance(value, bool) else DEFAULT_CHANNEL_THRESHOLD

    def _pair(self, run_value, against_value) -> tuple[str, dict, str, dict]:
        """(run, its run.json, against, its run.json); `against` omitted is the pinned baseline of the
        run's type. 400 for a bad id, the run itself or two types, 404 for an unknown run, 409 for a
        type with no pin."""
        run_id = self._id(run_value)
        run = self._load(run_id)
        if against_value in (None, ""):
            against_id = sc.pinned(self.catalog, sc.run_device_dir(run))
            if against_id is None:
                raise RequestError(HTTPStatus.CONFLICT, f"no baseline is pinned for {type_text(run)}: set one from a "
                                                        "run in the list, or pick a run to compare against")
        else:
            against_id = self._id(against_value, "against")
        if against_id == run_id:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"{run_id} cannot be compared with itself")
        against = self._load(against_id)
        try:
            sc.check_comparable(run_id, run, against_id, against)
        except sc.CatalogError as error:
            raise RequestError(HTTPStatus.BAD_REQUEST, str(error))
        return run_id, run, against_id, against

    def _task(self, run_id: str, against_id: str, force: bool) -> CompareTask:
        """The comparison of the pair computing in the background: the one already running, else a new one."""
        with self.tasks_lock:
            task = self.tasks.get((run_id, against_id))
            if task is not None and not task.done.is_set():
                return task
            task = CompareTask(run_id, against_id, force)
            self.tasks[(run_id, against_id)] = task
        exclusions = self._exclusions()

        def work() -> None:
            try:
                with self.compare_lock:
                    result = sc.compare_runs(self.catalog, run_id, against_id, TaskOutput(task),
                                             channel_threshold=self.channel_threshold,
                                             max_diff_percent=self.max_diff_percent, exclusions=exclusions, force=force,
                                             pool=self._pool(), workers=self.thumb_processes, stop=task.cancel)
                task.key = result["key"]
                if self.warm_thumbnails:  # the Failures section's cards, before anyone scrolls to them
                    self._warm([p for r in result["rows"] if r["status"] == sc.FAIL
                                for p in (r.get("baseline"), r.get("current"), r.get("diff")) if p])
            except sc.CompareCancelled:
                task.cancelled = True
            except sc.CatalogError as error:
                task.error = str(error)
            except Exception as error:  # reported on the results page, never swallowed
                task.error = f"{type(error).__name__}: {error}"
            finally:
                task.finished = time.time()
                task.done.set()

        threading.Thread(target=work, name=f"compare-{run_id}-{against_id}", daemon=True).start()
        return task

    def start_compare(self, body: dict) -> tuple[int, dict]:
        """`POST /api/compare {run, against?, force?}`: (200, ready) when the stored comparison is current
        or the computation finishes within `compare_wait`; (202, computing) otherwise."""
        run_id, run, against_id, against = self._pair(body.get("run"), body.get("against"))
        force = body.get("force") is True
        for each_id, each in ((run_id, run), (against_id, against)):
            if sc.run_is_active(each):
                raise RequestError(HTTPStatus.CONFLICT, f"run {each_id} is still capturing")
        stored = None if force else self._current(run_id, against_id)
        if stored is None:
            task = self._task(run_id, against_id, force)
            if not task.done.wait(self.compare_wait):
                return HTTPStatus.ACCEPTED, {"status": "computing", "run": run_id, "against": against_id,
                                             "task": task.info()}
            if task.error:
                raise RequestError(HTTPStatus.CONFLICT, task.error)
            stored = self._current(run_id, against_id) or {"key": task.key}
            reused = False
        else:
            reused = True
        return HTTPStatus.OK, {"status": "ready", "run": run_id, "against": against_id, "key": stored.get("key"),
                               "againstRevision": stored.get("againstRevision"), "reused": reused}

    def cancel_compare(self, body: dict) -> dict:
        """`POST /api/compare/cancel {run, against}`: stops the pair's computing comparison. It ends once
        the pairs in flight finish (well under a second each) and leaves no compare folder behind; the
        stored comparison, if any, is untouched. {"cancelled": true} when a task was stopped, false when
        none was computing."""
        run_id, against_id = self._id(body.get("run")), self._id(body.get("against"), "against")
        with self.tasks_lock:
            task = self.tasks.get((run_id, against_id))
        if task is None or task.done.is_set():
            return {"cancelled": False, "run": run_id, "against": against_id}
        task.cancel.set()
        if not task.done.wait(30):
            raise RequestError(HTTPStatus.CONFLICT, f"the comparison of {run_id} with {against_id} is still stopping")
        return {"cancelled": task.cancelled, "run": run_id, "against": against_id, "error": task.error}

    def results(self, run_value, against_value) -> dict:
        """`GET /api/results?run=&against=`: the comparison when it is current (`status: ready`), else
        `computing` (a task is on it; `task` carries done, total, perPairMs and eta), `error` (the last
        task failed), `cancelled` (the last task was cancelled) or `needed` (POST /api/compare)."""
        run_id, run, against_id, against = self._pair(run_value, against_value)
        pins = sc.read_baselines(self.catalog)
        main = main_head(self.repo)
        head = {"run": run_facts(self.catalog, run_id, run, pins), "against": run_facts(self.catalog, against_id, against, pins),
                "againstPinned": pins.get(sc.run_device_dir(against) or "") == against_id}
        for facts in (head["run"], head["against"]):
            facts["main"] = self._standing(facts, main)
        with self.tasks_lock:
            task = self.tasks.get((run_id, against_id))
        if task is not None and not task.done.is_set():
            return dict(head, status="computing", task=task.info())
        stored = self._current(run_id, against_id)
        if stored is None:
            if task is not None and task.error:
                return dict(head, status="error", error=task.error, task=task.info())
            if task is not None and task.cancelled:
                return dict(head, status="cancelled", task=task.info())
            return dict(head, status="needed")
        threshold = self._threshold(stored)
        reviews = sc.review_rows(self.catalog, stored.get("rows") or [])
        rows = [dict(self.api_row(r, stored["key"], threshold), review=api_review(reviews.get((r["name"], r["appearance"]))))
                for r in stored.get("rows") or []]
        first = next((r["current"] for r in stored.get("rows") or [] if r.get("current")), None)
        return dict(head, status="ready", key=stored["key"], againstRevision=stored.get("againstRevision"),
                    comparedAt=stored.get("comparedAt"), seconds=stored.get("seconds"),
                    settings=stored.get("settings"), notes=stored.get("notes") or [],
                    runStopped=stored.get("runStopped"), runPartial=stored.get("runPartial"),
                    counts=counts_of(stored.get("rows") or []), rows=rows, user=self.user,
                    review=sc.review_summary(reviews), blockers=sc.review_blockers(stored.get("rows") or [], reviews),
                    imageSize=png_size(self.catalog / first) if first else None)

    @property
    def user(self) -> dict | None:
        """Who is marking reviews from this page: the identity given to the server, else git's in its repository."""
        return self.user_override if self.user_override is not None else sc.git_user(self.repo)

    def review(self, body: dict) -> dict:
        """`POST /api/review {run, against?, name, appearance, kind, note?}`: one event (expect, confirm,
        reject, withdraw) on the change of that screen in the run's current comparison, by this page's
        user. The screen's review afterwards, and the comparison's summary."""
        name, appearance, kind = body.get("name"), body.get("appearance"), body.get("kind")
        if not isinstance(name, str) or not name or appearance not in sc.APPEARANCES:
            raise RequestError(HTTPStatus.BAD_REQUEST, "name and appearance (light|dark) are required")
        if kind not in sc.REVIEW_KINDS:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"kind must be one of {', '.join(sc.REVIEW_KINDS)}")
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise RequestError(HTTPStatus.BAD_REQUEST, "note must be a string")
        run_id, _, against_id, _ = self._pair(body.get("run"), body.get("against"))
        stored = self._current(run_id, against_id)
        if stored is None:
            raise RequestError(HTTPStatus.CONFLICT, f"no current comparison of {run_id} with {against_id}: compare first")
        rows = stored.get("rows") or []
        row = next((r for r in rows if r["name"] == name and r["appearance"] == appearance), None)
        pair = sc.review_pair(self.catalog, row) if row else None
        if pair is None:
            raise RequestError(HTTPStatus.CONFLICT, f"{name} ({appearance}) is not a changed or new screen of that comparison")
        if not self.user:
            raise RequestError(HTTPStatus.CONFLICT, "no identity for the review: set git user.name and user.email, "
                                                    f"or {sc.REVIEWER_ENV}=\"Name <email>\" in the server's environment")
        state = catalog_call(sc.add_review_event, self.catalog, pair, kind, self.user, note, run_id=run_id,
                             against_id=against_id, against_revision=stored.get("againstRevision"))
        reviews = sc.review_rows(self.catalog, rows)
        return {"name": name, "appearance": appearance, "review": api_review(state),
                "summary": sc.review_summary(reviews), "blockers": sc.review_blockers(rows, reviews)}

    def api_row(self, row: dict, key: str, threshold: int) -> dict:
        """A judged row with its image URLs. The review figures are drawn 240 px wide, the thumbnail's
        size: loading the full 1206 × 2622 PNG there cost ~0.5 MB and a full decode per figure."""
        current, baseline, diff = row.get("current"), row.get("baseline"), row.get("diff")
        current_url, baseline_url, diff_url = self.image_url(current), self.image_url(baseline), self.image_url(diff)
        mask = bool(diff_url and current_url and baseline_url)  # a FAIL with equal dimensions
        return {"name": row["name"], "feature": row.get("feature") or sc.feature_of(row["name"]),
                "appearance": row["appearance"], "status": row["status"], "reason": row_reason(row),
                "diffPercent": row.get("diffPercent"), "udid": row.get("udid"),
                "file": sc.shot_relpath(row["appearance"], row["name"]),
                "currentUrl": current_url, "baselineUrl": baseline_url, "diffUrl": diff_url,
                "currentThumbUrl": self.image_url(current, "thumb") if current_url else None,
                "baselineThumbUrl": self.image_url(baseline, "thumb") if baseline_url else None,
                "diffThumbUrl": self.image_url(diff, "thumb") if diff_url else None,
                "maskUrl": self.mask_url(key, current, baseline, threshold) if mask else None,
                "maskThumbUrl": self.mask_url(key, current, baseline, threshold, thumb=True) if mask else None}

    def image_url(self, relative: str | None, route: str = "img") -> str | None:
        """`/img/<relative>` (the PNG) or `/thumb/<relative>` (its cached thumbnail) for a catalog-relative
        path (runs/<run>/<appearance>/<name>.png or compares/<key>/diff/…); `?v=` changes when the file does."""
        if not relative:
            return None
        path = self.catalog / relative
        if not path.is_file():
            return None
        return f"/{route}/{urllib.parse.quote(relative)}?v={path.stat().st_mtime_ns}"

    def mask_url(self, key: str, current: str, baseline: str, threshold: int, thumb: bool = False) -> str | None:
        """`/mask/<key>/<appearance>/<name>.png`: the pixels of the run's screen that differ from the other
        run's, red on transparent; `size=thumb` for the card-sized one. `?v=` carries both inputs' mtimes and
        the threshold, everything the mask depends on, so a versioned mask never changes."""
        current_file, baseline_file = self.catalog / current, self.catalog / baseline
        if not current_file.is_file() or not baseline_file.is_file():
            return None
        rest = "/".join(current.split("/")[2:])
        version = f"{baseline_file.stat().st_mtime_ns}.{current_file.stat().st_mtime_ns}.{threshold}"
        return f"/mask/{key}/{urllib.parse.quote(rest)}?v={version}" + ("&size=thumb" if thumb else "")

    # Baseline operations, export, delete

    def accept(self, body: dict) -> dict:
        """Copies one screen of `run` over the pinned baseline (`against` must be that pin): a new
        revision; the comparison is then computed again in the background."""
        name, appearance = body.get("name"), body.get("appearance")
        if not isinstance(name, str) or not name or appearance not in sc.APPEARANCES:
            raise RequestError(HTTPStatus.BAD_REQUEST, "name and appearance (light|dark) are required")
        if body.get("against") in (None, ""):
            raise RequestError(HTTPStatus.BAD_REQUEST, "against is required: the pinned baseline the screen is copied into")
        run_id, run, against_id, _ = self._pair(body.get("run"), body.get("against"))
        pin = sc.pinned(self.catalog, sc.run_device_dir(run))
        if against_id != pin:
            raise RequestError(HTTPStatus.CONFLICT, f"accept copies into the pinned baseline of {type_text(run)}"
                                                    f" ({pin or 'none'}); {against_id} is not it")
        with self.compare_lock:
            event = catalog_call(sc.accept_screen, self.catalog, run_id, name, appearance, sc.Quiet(),
                                 check_main=self.check_main, repo=self.repo, channel_threshold=self.channel_threshold,
                                 max_diff_percent=self.max_diff_percent)
        task = self._task(run_id, against_id, False)
        return {"event": event, "baseline": against_id, "revision": event["revision"], "status": "computing",
                "task": task.info()}

    def set_baseline(self, body: dict) -> dict:
        run_id = self._id(body.get("run"))
        self._load(run_id)
        with self.compare_lock:
            result = catalog_call(sc.set_baseline, self.catalog, run_id, allow_partial=body.get("allowPartial") is True,
                                  allow_stopped=body.get("allowStopped") is True, output=sc.Quiet(),
                                  check_main=self.check_main, repo=self.repo, channel_threshold=self.channel_threshold,
                                  max_diff_percent=self.max_diff_percent)
        return result

    def update_baseline(self, body: dict) -> dict:
        run_id = self._id(body.get("run"))
        self._load(run_id)
        with self.compare_lock:
            return catalog_call(sc.update_baseline, self.catalog, run_id, sc.Quiet(), check_main=self.check_main,
                                repo=self.repo, channel_threshold=self.channel_threshold,
                                max_diff_percent=self.max_diff_percent)

    def export(self, body: dict) -> dict:
        run_id = self._id(body.get("run"))
        self._load(run_id)
        path = catalog_call(sc.export_run, self.catalog, run_id)
        return {"run": run_id, "path": str(path), "sizeKiB": path.stat().st_size // 1024}

    def delete_run(self, body: dict) -> dict:
        """Deletes a run, its thumbnails and every comparison it is part of; refuses a run still capturing.
        The pinned run may go: its type's pin goes with it (`unpinned`, `notice`; the module's rules)."""
        run_id = self._id(body.get("run"))
        self._load(run_id)
        with self.tasks_lock:  # asked before the lock, which that comparison holds
            if any(run_id in pair and not t.done.is_set() for pair, t in self.tasks.items()):
                raise RequestError(HTTPStatus.CONFLICT, f"a comparison of {run_id} is computing; delete when it ends")
        with self.compare_lock:
            result = catalog_call(sc.delete_run, self.catalog, run_id, sc.Quiet())
            with self.tasks_lock:
                for pair in [p for p in self.tasks if run_id in p]:
                    del self.tasks[pair]
            with self.thumb_lock:
                prefix = f"{sc.RUNS_DIR}/{run_id}/"
                self.warm_queue = collections.deque(r for r in self.warm_queue if not r.startswith(prefix))
            self.size_cache.pop(run_id, None)
        return result

    # Images: /img/, /thumb/, /mask/

    def split_image_path(self, raw: str) -> tuple[str, Path, Path]:
        """`runs/<run>/<appearance>/<name>.png` or `compares/<key>/diff/<appearance>/<name>.png` → (its root,
        runs/<run> or compares/<key>/diff; the root resolved; the file), or RequestError: 403 for any
        other path, `..`, a symlink out of its root or a root that resolves outside runs/ or compares/,
        404 for a missing file."""
        rel = urllib.parse.unquote(raw)
        parts = rel.split("/")
        if "\\" in rel or "\x00" in rel or any(p in ("", ".", "..") for p in parts):
            raise RequestError(HTTPStatus.FORBIDDEN, "path outside the catalog")
        if (len(parts) == 4 and parts[0] == sc.RUNS_DIR and sc.valid_run_id(parts[1])
                and parts[2] in sc.APPEARANCES):
            root_parts, top = parts[:2], sc.RUNS_DIR
        elif (len(parts) == 5 and parts[0] == sc.COMPARES_DIR and valid_compare_key(parts[1]) and parts[2] == "diff"
              and parts[3] in sc.APPEARANCES):
            root_parts, top = parts[:3], sc.COMPARES_DIR
        else:
            raise RequestError(HTTPStatus.FORBIDDEN, "path outside the catalog")
        root_rel = "/".join(root_parts)
        root = (self.catalog / root_rel).resolve()
        try:
            root.relative_to(self.catalog / top)
        except ValueError:
            raise RequestError(HTTPStatus.FORBIDDEN, "path outside the catalog")
        path = (root / "/".join(parts[len(root_parts):])).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise RequestError(HTTPStatus.FORBIDDEN, "path outside the catalog")
        if path.suffix != ".png" or not path.is_file():
            raise RequestError(HTTPStatus.NOT_FOUND, "no such image")
        return root_rel, root, path

    def image_path(self, raw: str) -> Path:
        return self.split_image_path(raw)[2]

    def thumb(self, raw: str) -> Path:
        """The cached thumbnail of `/img/`'s image at `raw` (same path checks), made on first request."""
        root_rel, root, source = self.split_image_path(raw)
        target = self.catalog / THUMB_DIR / root_rel / source.relative_to(root)
        with self.thumb_lock:
            lock = self.thumb_locks.setdefault(str(target), threading.Lock())
        with lock:  # one generation per file even when the page and the warmer ask at once
            if not thumbnail_fresh(source, target):
                self._generate(source, target)
        return target

    def mask(self, raw: str, thumb: bool = False) -> Path:
        """The cached overlay mask of a comparison's screen, `<key>/<appearance>/<name>.png`: red where the
        run's screen differs from the other run's by more than the comparison's channel threshold,
        transparent elsewhere. Both screens go through `/img/`'s path checks. The full-size one and the
        thumbnail-sized one are made together on first request, in the thumbnail pool, and kept (under
        .thumbs/compares/<key>/, so they go with the comparison) until either input or the threshold changes."""
        rel = urllib.parse.unquote(raw)
        parts = rel.split("/")
        if ("\\" in rel or "\x00" in rel or any(p in ("", ".", "..") for p in parts) or len(parts) != 3
                or not valid_compare_key(parts[0]) or parts[1] not in sc.APPEARANCES):
            raise RequestError(HTTPStatus.FORBIDDEN, "a mask is of a comparison's screen: <key>/<appearance>/<name>.png")
        key, rest = parts[0], "/".join(parts[1:])
        folder = self.catalog / sc.COMPARES_DIR / key
        data = None if folder.is_symlink() else sc.read_json(folder / sc.COMPARE_FILE)
        if not isinstance(data, dict):
            raise RequestError(HTTPStatus.NOT_FOUND, f"no comparison {key}")
        run_id, against_id, _ = sc.key_pair(key, data)
        if not sc.valid_run_id(run_id) or not sc.valid_run_id(against_id):
            raise RequestError(HTTPStatus.NOT_FOUND, f"no comparison {key}")
        current = self.image_path(f"{sc.RUNS_DIR}/{run_id}/{urllib.parse.quote(rest)}")
        baseline = self.image_path(f"{sc.RUNS_DIR}/{against_id}/{urllib.parse.quote(rest)}")
        if png_size(baseline) != png_size(current):
            raise RequestError(HTTPStatus.CONFLICT, "dimensions differ: no mask")
        threshold = self._threshold(data)
        target = self.catalog / THUMB_DIR / sc.COMPARES_DIR / key / MASK_DIR / rest
        mask_key_ = mask_key(baseline, current, threshold)
        with self.thumb_lock:
            lock = self.thumb_locks.setdefault(str(target), threading.Lock())
        with lock:
            if not mask_fresh(target, mask_key_):
                self._generate(make_mask, baseline, current, folder / "diff" / rest, threshold, target, mask_key_)
        return mask_thumb_path(target) if thumb else target

    def _pool(self) -> ProcessPoolExecutor | None:
        """The worker pool thumbnails, masks and comparisons share (None: in-process)."""
        with self.thumb_lock:
            if self.thumb_pool is None and self.thumb_processes > 0:
                self.thumb_pool = ProcessPoolExecutor(self.thumb_processes, initializer=_ignore_sigint)
            return self.thumb_pool

    def _generate(self, *args) -> None:
        """Runs `thumbnail(source, target)` (the default) or `fn(*rest)` in the worker pool."""
        fn, args = (args[0], args[1:]) if callable(args[0]) else (thumbnail, args)
        pool = self._pool()
        if pool is None:
            fn(*args)
            return
        try:
            pool.submit(fn, *args).result(timeout=120)
        except BrokenProcessPool:
            with self.thumb_lock:
                if self.thumb_pool is pool:
                    self.thumb_pool = None
            fn(*args)

    def close(self) -> None:
        with self.thumb_lock:
            pool, self.thumb_pool = self.thumb_pool, None
            self.warm_queue.clear()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _warm(self, raws: list[str]) -> None:
        """Makes the missing thumbnails of the gallery or comparison just opened in the background, in
        page order. Not while a record is active: it shares this process."""
        with self.thumb_lock:
            if self.job is not None and self.job.active:
                return
            self.warm_queue = collections.deque(raws)
            if self.warm_thread is None:
                self.warm_thread = threading.Thread(target=self._warm_loop, name="thumb-warm", daemon=True)
                self.warm_thread.start()

    def _warm_loop(self) -> None:
        while True:
            with self.thumb_lock:
                if not self.warm_queue or (self.job is not None and self.job.active):
                    self.warm_thread = None
                    return
                raw = self.warm_queue.popleft()
            try:
                self.thumb(raw)
            except (RequestError, OSError, ValueError):
                pass  # the page's own request for it reports the error


def new_job_id(mode: str) -> str:
    """`ui-<local yyyymmdd-hhmmss>-<mode>`: a job's id. A record's run folder gets its own id when the capture
    starts (`summary.run`), after the build, so the folder's name is the recorded time."""
    return f"ui-{time.strftime('%Y%m%d-%H%M%S')}-{mode}"


def row_reason(row: dict) -> str:
    return row.get("error") or row.get("note") or ""


def counts_of(rows: list[dict]) -> dict:
    out: dict[str, dict[str, int]] = {"all": {}}
    for row in rows:
        for bucket in (out["all"], out.setdefault(row["appearance"], {})):
            bucket[row["status"]] = bucket.get(row["status"], 0) + 1
    return out


def png_size(path: Path) -> list[int] | None:
    """[width, height] from the IHDR, without decoding."""
    try:
        with path.open("rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if head[:8] != sc.PNG_SIGNATURE or head[12:16] != b"IHDR":
        return None
    return list(struct.unpack(">II", head[16:24]))


def downscale(image: sc.Image, width: int) -> sc.Image:
    """RGB box-filter downscale by the whole factor nearest to `image.width / width`."""
    image = sc.to_rgb(image)
    k = max(1, round(image.width / width))
    if k == 1:
        return image
    out_w, out_h, stride, data, add = image.width // k, image.height // k, image.stride, image.data, operator.add
    span, n = out_w * k * 3, k * k
    out = bytearray(out_w * out_h * 3)
    for oy in range(out_h):
        base = oy * k * stride
        acc = list(data[base:base + span])
        for dy in range(1, k):  # k source rows summed column-wise
            start = base + dy * stride
            acc = list(map(add, acc, data[start:start + span]))
        line = bytearray(out_w * 3)
        for c in range(3):  # then k neighbouring pixels per channel
            column = acc[c::3]
            total = column[0::k]
            for dx in range(1, k):
                total = list(map(add, total, column[dx::k]))
            line[c::3] = bytes((t + n // 2) // n for t in total)
        out[oy * out_w * 3:(oy + 1) * out_w * 3] = line
    return sc.Image(out_w, out_h, 3, bytes(out))


def thumbnail_fresh(source: Path, target: Path) -> bool:
    try:
        return target.stat().st_mtime_ns == source.stat().st_mtime_ns
    except FileNotFoundError:
        return False


def thumbnail(source: Path, target: Path, width: int = THUMB_WIDTH) -> bool:
    """Writes `target` as a ~`width` px PNG of `source` unless it already carries `source`'s mtime.
    True when it was (re)generated. The mtime is the cache key, so a re-recorded screenshot gets a
    new thumbnail and an unchanged one is never decoded again."""
    if thumbnail_fresh(source, target):
        return False
    stat = source.stat()
    blob = sc.encode_png(downscale(sc.decode_png(source.read_bytes()), width))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{threading.get_ident()}.tmp")
    temp.write_bytes(blob)
    os.utime(temp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(temp, target)
    return True


_IS_FF = bytes([1 if v == 255 else 0 for v in range(256)])
_IS_00 = bytes([1 if v == 0 else 0 for v in range(256)])
_ONE_TO_FF = bytes([255 if v else 0 for v in range(256)])


def mask_key(baseline: Path, current: Path, threshold: int) -> str:
    return f"v{MASK_VERSION} {baseline.stat().st_mtime_ns} {current.stat().st_mtime_ns} {threshold}"


def mask_thumb_path(target: Path) -> Path:
    return target.with_name(target.name[:-len(".png")] + ".thumb.png")


def mask_key_path(target: Path) -> Path:
    return target.with_name(target.name[:-len(".png")] + ".key")


def mask_fresh(target: Path, key: str) -> bool:
    try:
        return (mask_key_path(target).read_text() == key and target.is_file()
                and mask_thumb_path(target).is_file())
    except OSError:
        return False


def red_alpha(rgb: bytes) -> bytes:
    """One byte per pixel of a 3-channel image: 255 where the pixel is pure red (255, 0, 0), else 0.
    Whole-buffer translate and big-int AND, no per-pixel Python loop."""
    n = len(rgb) // 3
    r = int.from_bytes(rgb[0::3].translate(_IS_FF), "big")
    g = int.from_bytes(rgb[1::3].translate(_IS_00), "big")
    b = int.from_bytes(rgb[2::3].translate(_IS_00), "big")
    return (r & g & b).to_bytes(n, "big").translate(_ONE_TO_FF)


def diff_alpha(baseline: Path, current: Path, diff: Path, threshold: int) -> tuple[int, int, bytes]:
    """(width, height, one byte per pixel: 255 where it differs). The diff writer's PNG already is this
    mask, losslessly: it paints exactly the differing pixels pure red over the baseline dimmed to a
    third (at most 85), so where it was written from these very inputs (newer than both, as every
    judge writes it) one cheap decode of it replaces decoding both screenshots and diffing them.
    Otherwise (no diff, or an input changed since): baseline vs current at `threshold`, as the
    capture script judges."""
    size = png_size(current)
    try:
        written = diff.stat().st_mtime_ns
        reuse = (written >= max(baseline.stat().st_mtime_ns, current.stat().st_mtime_ns)
                 and png_size(diff) == size)
    except OSError:
        reuse = False
    if reuse:
        image = sc.to_rgb(sc.decode_png(diff.read_bytes()))
        return image.width, image.height, red_alpha(image.data)
    a, b = sc.decode_png(baseline.read_bytes()), sc.decode_png(current.read_bytes())
    result = sc.diff_images(a, b, threshold)
    if not result.dimensions_match:
        raise ValueError("dimensions differ: no mask")
    alpha = bytearray(a.width * a.height)
    for p in result.pixels:
        alpha[p] = 255
    return a.width, a.height, bytes(alpha)


def red_rgba(width: int, height: int, alpha: bytes) -> sc.Image:
    out = bytearray(width * height * 4)
    out[0::4] = alpha  # red where opaque; a transparent pixel's colour is never seen
    out[3::4] = alpha
    return sc.Image(width, height, 4, bytes(out))


def shrink_alpha(width: int, height: int, alpha: bytes, thumb_width: int = THUMB_WIDTH) -> tuple[int, int, bytes]:
    """The mask at the thumbnail's size (the same whole factor as `downscale`), as a max, not a mean:
    a block with any differing pixel is opaque, so a one-pixel change still shows on a card."""
    k = max(1, round(width / thumb_width))
    if k == 1:
        return width, height, alpha
    out_w, out_h, span = width // k, height // k, (width // k) * k
    out = bytearray(out_w * out_h)
    for oy in range(out_h):
        acc = 0
        for dy in range(k):
            start = (oy * k + dy) * width
            acc |= int.from_bytes(alpha[start:start + span], "big")
        if not acc:
            continue
        row, col = acc.to_bytes(span, "big"), 0
        for dx in range(k):
            col |= int.from_bytes(row[dx::k], "big")
        out[oy * out_w:(oy + 1) * out_w] = col.to_bytes(out_w, "big")
    return out_w, out_h, bytes(out)


def _write_atomically(target: Path, blob: bytes) -> None:
    temp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_bytes(blob)
    os.replace(temp, target)


def make_mask(baseline: Path, current: Path, diff: Path, threshold: int, target: Path, key: str) -> bool:
    """Writes `target` (full size), its `.thumb.png` and the `.key` naming the inputs they were made
    from, unless that key is already there. True when (re)generated."""
    if mask_fresh(target, key):
        return False
    width, height, alpha = diff_alpha(baseline, current, diff, threshold)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(target, sc.encode_png(red_rgba(width, height, alpha)))
    _write_atomically(mask_thumb_path(target), sc.encode_png(red_rgba(*shrink_alpha(width, height, alpha))))
    _write_atomically(mask_key_path(target), key.encode())  # last: the key vouches for both images
    return True


def _ignore_sigint() -> None:
    """Thumbnail workers: ^C is the server's to handle; it shuts the pool down."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)



def validate_udids(value, required: bool) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(u, str) and UDID_RE.match(u) for u in value):
        raise RequestError(HTTPStatus.BAD_REQUEST, "udids must be a list of simulator UDIDs")
    if required and not value:
        raise RequestError(HTTPStatus.BAD_REQUEST, "pick at least one simulator")
    if len(set(value)) != len(value) or len(value) > 8:
        raise RequestError(HTTPStatus.BAD_REQUEST, "udids must be distinct, at most 8")
    return value


def validate_device_type(value) -> str:
    if not isinstance(value, str) or not DEVICE_TYPE_RE.match(value):
        raise RequestError(HTTPStatus.BAD_REQUEST, "deviceType must be an id from /api/device-types")
    return value


def validate_run(body: dict) -> dict:
    if body.get("mode") != "record":
        raise RequestError(HTTPStatus.BAD_REQUEST, "mode must be record, the only capture (compare two runs with "
                                                   "POST /api/compare)")
    device_type, clones = None, None
    if body.get("deviceType") is not None:
        if body.get("udids"):
            raise RequestError(HTTPStatus.BAD_REQUEST, "pass deviceType or udids, not both")
        device_type = validate_device_type(body["deviceType"])
        clones = body.get("clones", 1)
        if isinstance(clones, bool) or not isinstance(clones, int) or not 1 <= clones <= MAX_CLONES:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"clones must be a whole number from 1 to {MAX_CLONES}")
        udids: list[str] = []  # resolved when the run starts
    else:
        udids = validate_udids(body.get("udids"), required=True)
    appearance = body.get("appearance") or "both"
    if appearance not in ("light", "dark", "both"):
        raise RequestError(HTTPStatus.BAD_REQUEST, "appearance must be light, dark or both")
    filters = body.get("filters") or {}
    if not isinstance(filters, dict):
        raise RequestError(HTTPStatus.BAD_REQUEST, "filters must be an object")
    lists = {}
    for key in ("only", "feature"):
        values = filters.get(key) or []
        if not isinstance(values, list) or not all(isinstance(v, str) and 0 < len(v) <= 200 and "\n" not in v
                                                   for v in values):
            raise RequestError(HTTPStatus.BAD_REQUEST, f"filters.{key} must be a list of short strings")
        lists[key] = values

    def number(key: str, default, low: float, high: float, integer: bool = False, optional: bool = False):
        value = body.get(key, default)
        if value is None and optional:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"{key} must be a number from {low} to {high}")
        return int(value) if integer else float(value)

    if body.get("skipBuild") not in (None, True, False):
        raise RequestError(HTTPStatus.BAD_REQUEST, "skipBuild must be true or false")
    return {"mode": "record", "deviceType": device_type, "clones": clones, "udids": udids,
            "skipBuild": body.get("skipBuild") is True,
            "appearance": appearance, "only": lists["only"], "feature": lists["feature"],
            "settleMs": number("settleMs", 1500, 0, 60000, integer=True),
            "budgetMinutes": number("budgetMinutes", None, 0.1, 24 * 60, optional=True)}


# MARK: - Device types and clone fleets

def list_simulators() -> list[dict]:
    """Every available iOS simulator `simctl list` knows, booted or shut down."""
    listing = json.loads(sc.simctl("list", "-j").stdout)
    types = {t["identifier"]: t["name"] for t in listing.get("devicetypes", [])}
    runtimes = {r["identifier"]: r for r in listing.get("runtimes", [])}
    out = []
    for runtime_id, entries in (listing.get("devices") or {}).items():
        runtime = runtimes.get(runtime_id, {})
        if runtime.get("platform", "iOS") != "iOS" or ".iOS-" not in runtime_id:
            continue
        for d in entries:
            if not d.get("isAvailable", True) or not d.get("udid"):
                continue
            type_id = d.get("deviceTypeIdentifier", "")
            out.append({"udid": d["udid"], "name": d.get("name", ""), "state": d.get("state"),
                        "model": types.get(type_id, d.get("name", "")),
                        "os": runtime.get("name") or runtime_id.rsplit(".", 1)[-1],
                        "osBuild": runtime.get("buildversion"), "dataPath": d.get("dataPath"),
                        "deviceType": device_type_id(type_id, runtime_id)})
    return out


def _host_command(*argv: str) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True).stdout


def host_capacity(sims: list[dict] | None = None, run=None) -> dict:
    """`host` of `GET /api/device-types`: what this Mac has for more simulators, read afresh on every call.
    `ramGB` is `hw.memsize`; `freeGB` is free + inactive + speculative pages (`vm_stat`) times the page
    size, the memory that can be handed out without paging; `swapUsedGB` and `swapTotalGB` are
    `vm.swapusage`; `bootedSimulators` counts every booted simulator in `sims`, ours or not, since each
    costs memory. `recommendedClones = clamp(floor(freeGB / GB_PER_SIMULATOR), 1, MAX_CLONES)`, one fewer
    (never below 1) when swap used is over half of swap total. 2.5 GB per simulator is an estimate, not a
    measurement. When memory cannot be read (not macOS, a command fails or prints something unexpected)
    the memory fields are None and `recommendedClones` is FALLBACK_CLONES. `run(*argv)` returns a
    command's stdout (a seam for tests)."""
    run = run or _host_command
    booted = None if sims is None else sum(1 for s in sims if s.get("state") == "Booted")
    out = {"ramGB": None, "freeGB": None, "swapUsedGB": None, "swapTotalGB": None, "bootedSimulators": booted,
           "recommendedClones": FALLBACK_CLONES}
    if sys.platform != "darwin":
        return out
    try:
        ram = int(run("sysctl", "-n", "hw.memsize").strip())
        swap = re.search(r"total = ([\d.]+)([KMG]).*?used = ([\d.]+)([KMG])", run("sysctl", "-n", "vm.swapusage"))
        stat = run("vm_stat")
        page_size = int(re.search(r"page size of (\d+) bytes", stat).group(1))
        pages = sum(int(re.search(rf"^Pages {kind}:\s+(\d+)\.", stat, re.M).group(1))
                    for kind in ("free", "inactive", "speculative"))
        unit = {"K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1.0}
        swap_total = float(swap.group(1)) * unit[swap.group(2)]
        swap_used = float(swap.group(3)) * unit[swap.group(4)]
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return out
    free = pages * page_size / 1024 ** 3
    recommended = max(1, min(MAX_CLONES, int(free // GB_PER_SIMULATOR)))
    if swap_used > 0.5 * swap_total:
        recommended = max(1, recommended - 1)
    out.update({"ramGB": round(ram / 1024 ** 3, 1), "freeGB": round(free, 1), "swapUsedGB": round(swap_used, 1),
                "swapTotalGB": round(swap_total, 1), "recommendedClones": recommended})
    return out


def device_type_id(type_identifier: str, runtime_identifier: str) -> str:
    """`com.apple…SimDeviceType.iPhone-17` + `com.apple…SimRuntime.iOS-27-0` → `iPhone-17@iOS-27-0`."""
    return f"{type_identifier.rsplit('.', 1)[-1]}@{runtime_identifier.rsplit('.', 1)[-1]}"


def is_clone(sim: dict) -> bool:
    return sim["name"].startswith(CLONE_PREFIX)


def installed_app(sim: dict, bundle: str) -> dict | None:
    """The app `bundle` installed on `sim`, read from its data folder so a shut-down simulator answers
    too (`simctl get_app_container` needs it booted): {path, binary, modified}, or None."""
    data_path = sim.get("dataPath")
    if not data_path:
        return None
    for info in sorted(Path(data_path, "Containers", "Bundle", "Application").glob("*/*.app/Info.plist")):
        try:
            plist = plistlib.loads(info.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        if plist.get("CFBundleIdentifier") != bundle:
            continue
        app = info.parent
        executable = plist.get("CFBundleExecutable") or app.stem
        binary = app / f"{executable}.debug.dylib"
        if not binary.exists():
            binary = app / executable
        try:
            modified = sc.iso(binary.stat().st_mtime)
        except OSError:
            modified = None
        return {"path": str(app), "binary": str(binary), "modified": modified}
    return None


def pins_by_type(catalog: Path) -> dict[str, dict]:
    """Device folder → the pinned run's {run, model, os, osBuild, revision, revisionAt, counts} for every
    pin in baselines.json whose run exists."""
    out = {}
    for device_dir, run_id in sc.read_baselines(catalog).items():
        data = sc.read_run(catalog / sc.RUNS_DIR / run_id)
        if data is None:
            continue
        kind = data.get("deviceType") or {}
        out[device_dir] = {"run": run_id, "model": kind.get("model"), "os": kind.get("os"),
                           "osBuild": kind.get("osBuild"), "revision": sc.run_revision(data),
                           "revisionAt": data.get("revisionAt"), "counts": data.get("counts") or {}}
    return out


def device_types(sims: list[dict], catalog: Path) -> dict:
    """`GET /api/device-types`: the simulators grouped by (model, iOS runtime), each with the run pinned
    as its baseline (`pinned`, or null) and its `family` (iPhone, iPad, other); iPhones first, then
    iPads, and within a family by iOS, newest first (support for older versions is dropped over
    time), then natural model order (iPhone 17, iPhone 17 Pro, iPhone 18 Pro). A pinned type is tagged
    and preselected by the page, not moved."""
    pins = pins_by_type(catalog)
    groups: dict[str, dict] = {}
    for sim in sims:
        group = groups.setdefault(sim["deviceType"], {
            "id": sim["deviceType"], "model": sim["model"], "os": sim["os"], "osBuild": sim["osBuild"],
            "deviceDir": sc.device_dir_name(sim["model"], sim["os"]), "available": 0, "booted": 0, "clones": 0})
        if is_clone(sim):
            group["clones"] += 1
        else:
            group["available"] += 1
            group["booted"] += sim["state"] == "Booted"
    out = []
    for group in groups.values():
        pin = pins.get(group["deviceDir"])
        group["pinned"] = pin
        group["isBaseline"] = pin is not None
        group["baselineOsBuild"] = pin.get("osBuild") if pin else None
        group["family"] = device_family(group["model"])
        out.append(group)
    out.sort(key=lambda g: (FAMILY_ORDER.index(g["family"]), type_order(g["model"], g["os"])))
    return {"deviceTypes": out}


FAMILY_ORDER = ("iPhone", "iPad", "other")


def device_family(model: str | None) -> str:
    """`iPhone`, `iPad` or `other`, from the model name; the page's segmented control and the list order."""
    name = (model or "").strip().lower()
    return "iPhone" if name.startswith("iphone") else "iPad" if name.startswith("ipad") else "other"


def fleet_plan(device_type: str, clones: int, bundle: str, sims: list[dict] | None = None,
               require_app: bool = True) -> dict:
    """What `resolve_fleet` will use, without touching a simulator: the template (a booted simulator of
    the type, else the first shut-down one; never one of our clones while another exists), its app,
    and for k in 1…clones-1 the existing `screencatalog-clone-<k>` of the type or None (to be cloned)."""
    sims = list_simulators() if sims is None else sims
    of_type = [s for s in sims if s["deviceType"] == device_type]
    if not of_type:
        raise RequestError(HTTPStatus.NOT_FOUND, f"no simulator of device type {device_type}")
    candidates = [s for s in of_type if not is_clone(s)] or of_type
    apps = {s["udid"]: installed_app(s, bundle) for s in candidates}
    template = min(candidates, key=lambda s: (s["state"] != "Booted", apps[s["udid"]] is None, s["name"], s["udid"]))
    app = apps[template["udid"]]
    if app is None and require_app:
        raise RequestError(HTTPStatus.CONFLICT, f"build & install first: {template['name']} ({template['udid']}), "
                                                f"the {template['model']} {template['os']} simulator, has no {bundle}")
    existing: dict[str, dict] = {}
    for sim in sorted(of_type, key=lambda s: s["udid"]):
        if is_clone(sim) and sim["udid"] != template["udid"]:
            existing.setdefault(sim["name"], sim)
    names = [f"{CLONE_PREFIX}{k}" for k in range(1, clones)]
    return {"template": template, "app": app, "clones": [(name, existing.get(name)) for name in names],
            "allClones": [s for s in of_type if is_clone(s) and s["udid"] != template["udid"]]}


def boot_all(sims: list[dict], log) -> None:
    """Boots the shut-down ones in parallel and waits until each has finished booting."""
    pending = [s for s in sims if s["state"] != "Booted"]
    errors: list[str] = []

    def boot(sim: dict) -> None:
        try:
            sc.simctl("bootstatus", sim["udid"], "-b", timeout=600)
            sim["state"] = "Booted"
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
            errors.append(f"{sim['name']} ({sim['udid']}): {error}")

    for sim in pending:
        log("$ " + shlex.join(["xcrun", "simctl", "bootstatus", sim["udid"], "-b"]) + f"   # {sim['name']}")
    threads = [threading.Thread(target=boot, args=(s,), daemon=True) for s in pending]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise RuntimeError("boot failed: " + "; ".join(errors))


def resolve_fleet(device_type: str, clones: int, bundle: str = sc.DEFAULT_BUNDLE, log=print,
                  built: Path | None = None) -> dict:
    """The simulators a run of `device_type` on `clones` simulators uses, booted and ready: the template
    plus `screencatalog-clone-1`…`screencatalog-clone-<clones-1>` of the same type, each reused when it exists and
    otherwise made with `simctl clone <template>` so it carries the template's installed app. `simctl
    clone` refuses a booted source (code 405, "Unable to clone device in current state: Booted"), so
    when a clone is missing a booted template is shut down for the clone and booted again. With `built`
    (the .app a record just built), the template gets it installed when its own app's binary hash
    differs or it has none, so a record always captures what it built; then a reused clone whose app
    differs from the template's gets the template's app installed. Raises RequestError 404 for an
    unknown type and 409 when the template has no app and nothing was built."""
    if isinstance(clones, bool) or not isinstance(clones, int) or not 1 <= clones <= MAX_CLONES:
        raise RequestError(HTTPStatus.BAD_REQUEST, f"clones must be 1 to {MAX_CLONES}")
    plan = fleet_plan(device_type, clones, bundle, require_app=built is None)
    template, app = plan["template"], plan["app"]
    log(f"template: {template['name']} ({template['udid']}), {template['state'].lower()}; " + (
        f"{Path(app['binary']).name} modified {app['modified']}" if app else f"no {bundle} installed"))
    created = []
    missing = [name for name, sim in plan["clones"] if sim is None]
    if missing and template["state"] == "Booted":
        log(f"simctl clone needs its source shut down: shutting the template down for {len(missing)} clone(s)")
        log("$ " + shlex.join(["xcrun", "simctl", "shutdown", template["udid"]]))
        sc.simctl("shutdown", template["udid"], timeout=120)
        template["state"] = "Shutdown"
    fleet = [template]
    for name, sim in plan["clones"]:
        if sim is None:
            log("$ " + shlex.join(["xcrun", "simctl", "clone", template["udid"], name]))
            udid = sc.simctl("clone", template["udid"], name, timeout=600).stdout.strip().splitlines()[-1].strip()
            if not UDID_RE.match(udid):
                raise RuntimeError(f"simctl clone printed {udid!r}, not a UDID")
            data_path = Path(template["dataPath"]).parent.parent / udid / "data" if template.get("dataPath") else None
            sim = dict(template, udid=udid, name=name, state="Shutdown", dataPath=str(data_path) if data_path else None)
            created.append(udid)
        else:
            log(f"reusing {name} ({sim['udid']}), {sim['state'].lower()}")
        fleet.append(sim)
    boot_all(fleet, log)
    if built is not None:
        wanted = sc.app_identity(built)["sha256"]
        theirs = sc.app_identity(Path(app["path"]))["sha256"] if app else None
        if theirs == wanted:
            log(f"{template['name']} already has this build ({wanted})")
        else:
            log(f"{template['name']} has {'no app' if theirs is None else 'build ' + theirs}: installing {wanted}")
            log("$ " + shlex.join(["xcrun", "simctl", "install", template["udid"], str(built)]))
            sc.simctl("install", template["udid"], str(built), timeout=300)
            app = installed_app(template, bundle)
            if app is None:
                raise RuntimeError(f"{template['name']} has no {bundle} after the install")
            # Clones made just now were cloned before the install, so they are checked like reused ones.
            created = []
    for sim in fleet[1:]:
        if sim["udid"] in created:
            continue
        theirs = installed_app(sim, bundle)
        if theirs is None or theirs["modified"] != app["modified"]:
            log(f"{sim['name']} has {'no app' if theirs is None else 'an app from ' + str(theirs['modified'])}: "
                "installing the template's")
            log("$ " + shlex.join(["xcrun", "simctl", "install", sim["udid"], app["path"]]))
            sc.simctl("install", sim["udid"], app["path"], timeout=300)
    udids = [s["udid"] for s in fleet]
    log(f"fleet: {len(udids)} simulator(s) of {template['model']} {template['os']}: " + ", ".join(
        f"{s['name']} ({s['udid'][:8]})" for s in fleet))
    return {"template": template["udid"], "udids": udids, "clones": udids[1:], "created": created}


def shutdown_clones(udids: list[str], log) -> None:
    for udid in udids:
        log("$ " + shlex.join(["xcrun", "simctl", "shutdown", udid]))
        sc.simctl("shutdown", udid, check=False, timeout=120)



def app_label(app: dict) -> str | None:
    """`<version> (<build>)` from a manifest's app record; whichever half is known; None for neither."""
    version, build = app.get("version"), app.get("build")
    if version and build:
        return f"{version} ({build})"
    return version or (f"({build})" if build else None)


def built_app() -> Path | None:
    """This checkout's simulator build of the configured app: a `.app` with the configured bundle id under
    `<configuration>-iphonesimulator`, from `build.derivedDataPath` when set, else from the DerivedData
    folder whose WorkspacePath is the configured project or workspace (worktrees each get their own),
    else the newest build with that bundle id."""
    build = sc.BUILD
    container = sc.config_path(build["workspace"] or build["project"])
    container = str(container.resolve()) if container else None
    products = f"{build['configuration']}-iphonesimulator"
    explicit = sc.config_path(build["derivedDataPath"])
    folders = [explicit] if explicit else sorted(DERIVED_DATA.glob("*")) if DERIVED_DATA.is_dir() else []
    candidates = []
    for folder in folders:
        apps = sorted((folder / "Build" / "Products" / products).glob("*.app"))
        if not apps:
            continue
        try:
            workspace = plistlib.loads((folder / "info.plist").read_bytes()).get("WorkspacePath")
        except (OSError, ValueError, plistlib.InvalidFileException):
            workspace = None
        ours = explicit is not None or (container is not None and workspace == container)
        for app in apps:
            try:
                bundle = plistlib.loads((app / "Info.plist").read_bytes()).get("CFBundleIdentifier")
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            if sc.DEFAULT_BUNDLE and bundle != sc.DEFAULT_BUNDLE:
                continue
            if not sc.DEFAULT_BUNDLE and not ours:
                continue  # without a bundle id, only a build of the configured project is ours
            candidates.append((ours, app.stat().st_mtime, app))
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c[0], c[1]))[2]

# MARK: - HTTP

class Handler(BaseHTTPRequestHandler):
    app: CatalogUI  # set on the subclass by make_server
    server_version = "ScreenCatalogUI/1"

    def log_message(self, format, *args):  # noqa: A002 - quiet; the page shows what matters
        pass

    def send_json(self, payload, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_png(self, path: Path, cache: str) -> None:
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise RequestError(HTTPStatus.BAD_REQUEST, "body is not JSON")
        if not isinstance(body, dict):
            raise RequestError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return body

    def do_GET(self) -> None:  # noqa: N802
        self.dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self.dispatch("POST")

    def guard(self, method: str) -> None:
        """Only this page may talk to this server. The socket is loopback-only, but a web page open in the
        same browser can still address 127.0.0.1: a cross-origin POST (a delete, a push, a record) is a
        simple request when its body is text/plain, and DNS rebinding reaches a GET under another Host. So
        the Host must be this server's, a POST must be JSON (a cross-origin fetch with that type needs a
        preflight nothing here answers), and an Origin, when sent, must be this server's."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in hosts:
            raise RequestError(HTTPStatus.FORBIDDEN, f"Host {host or '(none)'} is not this server (127.0.0.1:{port})")
        if method == "POST":
            origin = self.headers.get("Origin")
            if origin is not None and origin.strip().lower() not in {f"http://{h}" for h in hosts}:
                raise RequestError(HTTPStatus.FORBIDDEN, f"cross-origin request from {origin} refused")
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                raise RequestError(HTTPStatus.FORBIDDEN, "a POST must carry Content-Type: application/json")

    def dispatch(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        arg = lambda name: (query.get(name) or [None])[0]  # noqa: E731
        app = self.app
        # A versioned URL (?v= carries the file's mtime, or both inputs' and the threshold) never changes.
        cache = "max-age=31536000, immutable" if "v" in query else "no-cache"
        try:
            self.guard(method)
            if method == "GET" and url.path == "/":
                body = render_page(app).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # The page is the UI's code: after a server restart the browser must not keep an old copy.
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif method == "GET" and url.path.startswith("/img/"):
                self.send_png(app.image_path(url.path[len("/img/"):]), cache)
            elif method == "GET" and url.path.startswith("/thumb/"):
                self.send_png(app.thumb(url.path[len("/thumb/"):]), cache)
            elif method == "GET" and url.path.startswith("/mask/"):
                self.send_png(app.mask(url.path[len("/mask/"):], arg("size") == "thumb"), cache)
            elif method == "GET" and url.path == "/api/runs":
                self.send_json(app.runs())
            elif method == "GET" and url.path == "/api/run":
                self.send_json(app.run_detail(arg("id")))
            elif method == "GET" and url.path == "/api/results":
                self.send_json(app.results(arg("run"), arg("against")))
            elif method == "GET" and url.path == "/api/device-types":
                sims = list_simulators()
                self.send_json({**device_types(sims, app.catalog), "host": host_capacity(sims)})
            elif method == "GET" and url.path == "/api/menu":
                self.send_json(app.menu(arg("udid") or "", arg("refresh") == "1", arg("deviceType")))
            elif method == "GET" and url.path == "/api/exclusions":
                self.send_json(app.exclusions())
            elif method == "GET" and url.path == "/api/status":
                try:
                    since = int(arg("since") or "0")
                except ValueError:
                    raise RequestError(HTTPStatus.BAD_REQUEST, "since must be an integer")
                self.send_json(app.status(since))
            elif method == "POST" and url.path == "/api/run":
                job = app.start_run(self.read_json())
                self.send_json({"runId": job.id, "mode": job.mode, "run": job.accepted_params.get("run")}, HTTPStatus.ACCEPTED)
            elif method == "POST" and url.path == "/api/compare":
                status, payload = app.start_compare(self.read_json())
                self.send_json(payload, status)
            elif method == "POST" and url.path == "/api/compare/cancel":
                self.send_json(app.cancel_compare(self.read_json()))
            elif method == "POST" and url.path == "/api/accept":
                self.send_json(app.accept(self.read_json()))
            elif method == "POST" and url.path == "/api/review":
                self.send_json(app.review(self.read_json()))
            elif method == "POST" and url.path == "/api/set-baseline":
                self.send_json(app.set_baseline(self.read_json()))
            elif method == "POST" and url.path == "/api/update-baseline":
                self.send_json(app.update_baseline(self.read_json()))
            elif method == "POST" and url.path == "/api/export":
                self.send_json(app.export(self.read_json()))
            elif method == "POST" and url.path == "/api/runs/delete":
                self.send_json(app.delete_run(self.read_json()))
            elif method == "POST" and url.path == "/api/clones/delete":
                self.send_json(app.delete_clones(self.read_json()))
            elif method == "GET" and url.path == "/api/sync/status":
                self.send_json(app.sync_status())
            elif method == "GET" and url.path == "/api/sync/remote":
                self.send_json(app.sync_remote(arg("refresh") == "1"))
            elif method == "POST" and url.path == "/api/sync/delete":
                self.send_json(app.sync_delete(self.read_json()))
            elif method == "POST" and url.path in ("/api/sync/push", "/api/sync/pull"):
                job = app.start_sync(url.path.rsplit("/", 1)[1], self.read_json())
                self.send_json({"runId": job.id, "mode": job.mode}, HTTPStatus.ACCEPTED)
            elif method == "POST" and url.path == "/api/stop":
                self.send_json({"stopping": app.stop()})
            else:
                self.send_json({"error": f"no route {method} {url.path}"}, HTTPStatus.NOT_FOUND)
        except RequestError as error:
            self.send_json({"error": str(error)}, error.status)
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            self.send_json({"error": f"{type(error).__name__}: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def make_server(app: CatalogUI, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def render_page(app: CatalogUI) -> str:
    return (PAGE.replace("__CATALOG_DIR__", json.dumps(str(app.catalog)).replace("<", "\\u003c"), 1)
            .replace("__STORE_NAME__", json.dumps(sc.store_noun()), 1))


# MARK: - Page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Screen catalog</title>
<style>
:root { color-scheme: light dark; --fg:#1d1d1f; --muted:#6e6e73; --bg:#ffffff; --panel:#f5f5f7; --line:#d2d2d7;
  --accent:#0a66d8; --on-accent:#ffffff; --pass:#1a7f37; --fail:#cf222e; --warn:#9a6700; --sel:#e8f0fe;
  --lb-bg:rgba(10,10,12,.96); --lb-fg:#f5f5f7; --lb-muted:#a1a1a6; --lb-button:rgba(255,255,255,.1); --lb-line:rgba(255,255,255,.2); }
@media (prefers-color-scheme: dark) { :root { --fg:#f5f5f7; --muted:#a1a1a6; --bg:#161618; --panel:#232326;
  --line:#3a3a3c; --accent:#4c9bff; --on-accent:#0b1220; --pass:#3fb950; --fail:#ff7b72; --warn:#d29922; --sel:#1f2a3d; } }
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { overflow-x: hidden; }
body { font: 14px/1.45 -apple-system, system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 0; }
body.noscroll { overflow: hidden; }
h1 { font-size: 22px; margin: 4px 0 4px; } h2 { font-size: 16px; margin: 20px 0 8px; }
.muted { color: var(--muted); } code { font: 12px ui-monospace, Menlo, monospace; }
a { color: var(--accent); }
header.top { position: sticky; top: 0; z-index: 20; background: var(--bg); border-bottom: 1px solid var(--line); }
.topbar { display: flex; align-items: center; gap: 10px; padding: 8px 16px; max-width: 1440px; margin: 0 auto; min-height: 46px; }
.brand { font-weight: 700; color: var(--fg); text-decoration: none; white-space: nowrap; }
.crumbs { flex: 1 1 auto; min-width: 0; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.crumbs a { text-decoration: none; }
#back { padding: 3px 10px; }
#runStrip { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px;
  background: var(--sel); color: var(--fg); text-decoration: none; font-size: 12px; white-space: nowrap; }
#runStrip .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); animation: pulse 1.2s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: .3; } }
main { max-width: 1440px; margin: 0 auto; padding: 16px; }
.lead { color: var(--muted); margin: 0 0 12px; max-width: 70ch; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; margin: 16px 0; }
.card { display: flex; flex-direction: column; gap: 6px; padding: 22px; min-height: 180px; border: 1px solid var(--line);
  border-radius: 14px; background: var(--panel); color: var(--fg); text-decoration: none; transition: border-color .15s, transform .15s; }
.card:hover, .card:focus-visible { border-color: var(--accent); transform: translateY(-1px); outline: none; }
.card .glyph { font-size: 26px; line-height: 1; color: var(--accent); }
.card h2 { font-size: 22px; margin: 6px 0 0; }
.card p { margin: 0; }
.card .status { margin-top: auto; padding-top: 12px; font-size: 13px; color: var(--muted); }
section.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin: 12px 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px 16px; }
label { display: block; font-weight: 600; margin-bottom: 3px; }
input[type=text], input[type=number], input[type=search], select { width: 100%; padding: 6px 8px; border: 1px solid var(--line);
  border-radius: 6px; background: var(--bg); color: var(--fg); font: inherit; }
/* Never clipped: the list used to scroll inside 260 px, and macOS overlay scrollbars (which ignore scrollbar styling in the embedded browser) hid that anything was below the fold. The family segments and the iOS headings keep it short instead. */
.devices { display: flex; flex-direction: column; gap: 2px; padding: 2px 0; }
.segs { display: flex; flex-wrap: wrap; gap: 6px 10px; margin: 0 0 6px; }
label.check { display: flex; align-items: center; gap: 6px; font-weight: 400; margin-top: 8px; cursor: pointer; } .family-seg button { font-size: 13px; }
.devices label { font-weight: 400; display: flex; gap: 6px; align-items: baseline; margin: 0; padding: 2px 4px; border-radius: 6px; cursor: pointer; }
.devices label:has(input:checked) { background: var(--sel); }
.devices .count { color: var(--muted); font-size: 12px; white-space: nowrap; }
.note { font-size: 13px; margin-top: 6px; } .note.warn { color: var(--warn); }
.rgroup h2 .no-pin { color: var(--warn); font-weight: 600; }
.off-main { color: var(--warn); font-weight: 600; } .rgroup h2 .off-main { font-size: 13px; }
.clones { display: flex; align-items: center; gap: 8px; } .clones input { width: 72px; }
.buttons { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
button, a.button { font: inherit; padding: 6px 12px; border-radius: 6px; border: 1px solid var(--line); background: var(--bg);
  color: var(--fg); cursor: pointer; text-decoration: none; display: inline-block; }
button.primary, a.button.primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
button.danger { color: var(--fail); } button:disabled { opacity: .45; cursor: default; }
progress { width: 100%; height: 14px; }
pre#log, pre#syncLog { max-height: 260px; overflow: auto; background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
  padding: 8px; margin: 8px 0 0; font: 12px/1.35 ui-monospace, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
tbody tr.row { cursor: pointer; } tbody tr.row:hover, tr.selected { background: var(--sel); }
.PASS, .RECORDED { color: var(--pass); font-weight: 600; } .FAIL, .ERROR { color: var(--fail); font-weight: 600; }
.MISSING, .SKIPPED { color: var(--warn); font-weight: 600; }
.badge { display: inline-block; font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line); }
.badge.baseline { color: var(--pass); border-color: currentColor; } .badge.current { color: var(--accent); border-color: currentColor; }
.shots { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 10px; }
figure { margin: 0; } .shots figure img, .shots .none { width: 240px; max-width: 100%; border: 1px solid var(--line); border-radius: 6px; display: block; }
.shots .none { height: 160px; display: grid; place-items: center; color: var(--muted); }
figcaption { text-align: center; font-size: 12px; color: var(--muted); }
.swipe { position: relative; width: 300px; max-width: 100%; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.swipe img { display: block; width: 100%; } .swipe img.top { position: absolute; inset: 0; }
.swipe .bar { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--accent); pointer-events: none; }
.swipe-wrap input { width: 300px; max-width: 100%; }
/* Results: the Failures section, one card per FAIL row */
.fail-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin: 18px 0 2px; }
.fail-head h2 { margin: 0; }
.fgroup h3 { font-size: 15px; margin: 16px 0 8px; } .fgroup h3 .muted { font-weight: 400; }
.fcards { display: flex; flex-direction: column; gap: 12px; }
.fcard { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); padding: 10px 12px; }
.fcard.last { outline: 2px solid var(--accent); outline-offset: 1px; }
.fcard-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; }
.fcard-head .title { font-weight: 600; overflow-wrap: anywhere; }
.fcard-head .buttons { margin: 0 0 0 auto; }
.fcard-review { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; margin: 4px 0 8px; font-size: 13px; }
.fcard-review .buttons { margin-left: auto; }
.rv { display: inline-block; padding: 1px 8px; border-radius: 10px; font-weight: 600; font-size: 12px; border: 1px solid transparent; }
.rv-open { color: var(--warn); border-color: var(--warn); } .rv-expected { color: var(--accent); border-color: var(--accent); }
.rv-confirmed { color: var(--pass); border-color: var(--pass); } .rv-rejected { color: var(--fail); border-color: var(--fail); }
.rv-note { color: var(--muted); overflow-wrap: anywhere; }
#reviewSummary.blocked { color: var(--warn); }
.fcard-head button.small { margin-left: 0; }
.fimgs { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; align-items: flex-start; }
.fimgs figure { width: 240px; max-width: 100%; }
.fimgs figure img, .fimgs figure .none { width: 100%; aspect-ratio: var(--ratio); display: block; border: 1px solid var(--line);
  border-radius: 6px; background: var(--bg); }
.fimgs figure .none { display: grid; place-items: center; color: var(--muted); }
.fimgs [data-layer] { cursor: zoom-in; }
.fimgs [data-layer]:focus-visible, .fimgs figure img:hover, .ovl:hover { outline: 2px solid var(--accent); outline-offset: 1px; }
.ovl-wrap { display: flex; flex-direction: column; gap: 6px; width: 240px; max-width: 100%; }
.ovl { position: relative; width: 100%; aspect-ratio: var(--ratio); border: 1px solid var(--line); border-radius: 6px; overflow: hidden; background: var(--bg); }
.ovl img { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }
.ovl-controls { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.ovl-controls .mix { display: flex; align-items: center; gap: 6px; } .ovl-controls input[type=range] { flex: 1 1 auto; min-width: 0; }
.ovl-controls label { font-weight: 400; display: inline-flex; gap: 6px; align-items: center; margin: 0; }
.fimgs .swipe-wrap { display: flex; flex-direction: column; gap: 4px; }
.fimgs .swipe { aspect-ratio: var(--ratio); } .fimgs .swipe img:first-child { height: 100%; }
.fimgs .note { flex-basis: 100%; margin: 0; }
dialog { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); color: var(--fg); max-width: 92vw; }
dialog img { width: 260px; max-width: 100%; border: 1px solid var(--line); border-radius: 6px; }
.counts td, .counts th { text-align: right; } .counts td:first-child, .counts th:first-child { text-align: left; }
/* Results page: read-only facts of one compare run */
.facts { display: flex; flex-wrap: wrap; gap: 4px 18px; margin: 6px 0 4px; font-size: 13px; }
.facts .fact { white-space: nowrap; } .facts .fact .k { color: var(--muted); margin-right: 4px; }
.facts .fact code { overflow-wrap: anywhere; white-space: normal; }
.verdict { font-size: 15px; margin: 4px 0; }
button.small { padding: 2px 8px; font-size: 12px; margin-left: 6px; }
code.rev { font-size: 11px; padding: 0 5px; border: 1px solid var(--line); border-radius: 4px; color: var(--fg); white-space: nowrap; }
/* Gallery */
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; padding: 10px 12px; margin: 8px 0 4px;
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; border-radius: 0; padding: 5px 12px; }
.seg button + button { border-left: 1px solid var(--line); }
.seg button[aria-pressed=true], .chip[aria-pressed=true] { background: var(--accent); color: var(--on-accent); border-color: var(--accent); }
.toolbar input[type=search] { width: 200px; max-width: 100%; }
.toolbar .size { display: inline-flex; align-items: center; gap: 6px; font-weight: 400; margin: 0; }
.toolbar .size input { width: 120px; }
#galleryCount { margin-left: auto; color: var(--muted); white-space: nowrap; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 4px; }
.chip { border-radius: 999px; padding: 2px 10px; font-size: 12px; }
.chip .n { color: inherit; opacity: .65; margin-left: 4px; }
.group h3 { font-size: 15px; margin: 22px 0 8px; } .group h3 .muted { font-weight: 400; }
.tiles { display: flex; flex-wrap: wrap; gap: 16px 14px; }
.tile { width: min(var(--tile), 100%); cursor: pointer; }
.tile .pair { display: flex; gap: 6px; }
.tile img, .tile .none { flex: 1 1 0; min-width: 0; width: 100%; aspect-ratio: var(--ratio); display: block; object-fit: cover;
  border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
.tile .none { display: grid; place-items: center; color: var(--muted); font-size: 12px; cursor: default; }
.tile img:hover, .tile img:focus-visible { border-color: var(--accent); outline: 2px solid var(--accent); outline-offset: 1px; }
.tile.last img { outline: 2px solid var(--accent); }
.tile figcaption { margin-top: 4px; overflow-wrap: anywhere; color: var(--fg); }
.tile .cell { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.tile .cell img { flex: none; }
.tile .cell .layer { font-size: 11px; color: var(--muted); text-align: center; }
.tile figcaption .st { font-size: 11px; margin-left: 4px; white-space: nowrap; }
#galleryHistory { margin: 6px 0 2px; font-size: 13px; color: var(--muted); } #galleryHistory summary { cursor: pointer; }
ul.history { list-style: none; margin: 4px 0 0; padding: 0 0 4px 12px; font-size: 13px; color: var(--muted); max-height: 240px; overflow: auto; }
ul.history li::before { content: "\21B3  "; }
#lbLayers button[aria-pressed=true] { background: var(--accent); color: var(--on-accent); border-color: var(--accent); }
/* Lightbox: a dark viewer in either scheme */
#lightbox { position: fixed; inset: 0; z-index: 50; background: var(--lb-bg); color: var(--lb-fg); display: flex; flex-direction: column; }
.lb-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; padding: 8px 12px; }
.lb-caption { flex: 1 1 220px; min-width: 0; overflow-wrap: anywhere; }
.lb-caption .sub { color: var(--lb-muted); font-size: 12px; }
#lightbox button { background: var(--lb-button); color: var(--lb-fg); border-color: var(--lb-line); }
#lbZoom { min-width: 48px; text-align: center; font-variant-numeric: tabular-nums; color: var(--lb-muted); font-size: 12px; }
.lb-body { position: relative; flex: 1 1 auto; min-height: 0; }
.lb-stage { position: absolute; inset: 0; overflow: hidden; touch-action: none; user-select: none; }
.lb-stage.zoomed { cursor: grab; } .lb-stage.dragging { cursor: grabbing; }
#lbBox { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
#lbBox img { position: absolute; inset: 0; width: 100%; height: 100%; max-width: none; -webkit-user-drag: none; }
.lb-stage.pixelated img { image-rendering: pixelated; }
.lb-ovl { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--lb-muted); }
.lb-ovl input[type=range] { width: 120px; }
.lb-ovl label { font-weight: 400; margin: 0; display: inline-flex; gap: 4px; align-items: center; color: var(--lb-fg); }
#lightbox .lb-nav { position: absolute; top: 50%; transform: translateY(-50%); z-index: 2; width: 44px; height: 72px;
  font-size: 30px; line-height: 1; border-radius: 10px; background: rgba(0,0,0,.5); }
.lb-nav.prev { left: 10px; } .lb-nav.next { right: 10px; }
.lb-hint { color: var(--lb-muted); font-size: 12px; text-align: center; padding: 4px 12px 8px; }
@media (max-width: 600px) {
  main { padding: 12px 16px; } .card { min-height: 0; } #galleryCount { margin-left: 0; }
  .lb-hint { display: none; } .lb-caption { flex-basis: 100%; } #lightbox .lb-nav { width: 36px; height: 56px; font-size: 24px; }
}
/* Recordings: one table per simulator type, the pinned run tagged */
.rgroup h2 { font-size: 17px; margin: 22px 0 6px; } .rgroup h2 .muted { font-weight: 400; font-size: 13px; }
.runs-table td.actions { white-space: normal; }
.runs-table td.actions .buttons { margin: 0; gap: 4px; }
.runs-table td.actions button, .runs-table td.actions a.button { padding: 2px 8px; font-size: 12px; margin: 0; }
.runs-table tr.pinned { background: color-mix(in srgb, var(--pass) 7%, var(--bg)); }
.runs-table .sub { font-size: 12px; color: var(--muted); }
/* Sync: push and pull through the shared store, at the top of Recordings */
#syncPanel .sync-head { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: baseline; }
#syncPanel .buttons { margin: 8px 0 4px; }
#syncPanel pre { max-height: 220px; }
#syncRemote summary { cursor: pointer; margin-top: 6px; }
.runs-table .sub[data-sync] { font-size: 11px; }
/* The Compare… picker */
dialog#pickDialog { width: min(520px, 92vw); }
.picks { display: flex; flex-direction: column; gap: 6px; margin: 8px 0; }
button.pick { text-align: left; display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; padding: 8px 10px; }
button.pick:hover, button.pick:focus-visible { border-color: var(--accent); outline: none; }
button.pick .sub { color: var(--muted); font-size: 12px; }
p.empty { margin: 8px 0; }
/* Results header: the two runs, one line each */
.factline { display: flex; flex-wrap: wrap; gap: 2px 16px; font-size: 13px; margin: 3px 0; }
.factline .who { font-weight: 600; min-width: 64px; }
.factline .fact { white-space: nowrap; } .factline .fact .k { color: var(--muted); margin-right: 4px; }
.factline .fact code { overflow-wrap: anywhere; white-space: normal; }
.gactions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin: 8px 0 0; }
.gactions label { font-weight: 400; display: inline-flex; gap: 6px; align-items: center; margin: 0; }
</style></head><body>
<header class="top"><div class="topbar">
  <button id="back" type="button" aria-label="Back" hidden>&#8249; Back</button>
  <a class="brand" href="#">Screen catalog</a>
  <nav class="crumbs" id="crumbs" aria-label="Breadcrumb"></nav>
  <a id="runStrip" hidden></a>
</div></header>
<main>

<div class="view" id="view-home">
  <div class="cards">
    <a class="card" href="#record" id="cardRecord"><span class="glyph">&#9679;</span><h2>Record</h2>
      <p>Capture every screen, or a filtered set, on a simulator type into a new run. A record never changes a baseline.</p><div class="status" id="stRecord">&nbsp;</div></a>
    <a class="card" href="#recordings" id="cardRecordings"><span class="glyph">&#9638;</span><h2>Recordings</h2>
      <p>Every run by simulator type: open its gallery, compare it with another run, set the baseline.</p><div class="status" id="stRecordings">&nbsp;</div></a>
  </div>
  <div class="muted" id="catalogDir"></div>
</div>

<div class="view" id="view-record" hidden>
  <h1>Record</h1>
  <p class="lead">Captures the selected screens into a new run, <code>runs/&lt;run&gt;/</code>. A record never touches the baseline: compare the run with it afterwards, then set the run as the baseline or copy its screens in from Recordings.</p>
  <section class="panel" id="runPanel">
    <div class="grid">
      <div><label id="deviceLabel">Simulator type</label>
        <div class="segs"><div class="seg family-seg" id="deviceFamilySeg" role="group" aria-label="Simulator family" hidden></div>
          <div class="seg family-seg" id="deviceOsSeg" role="group" aria-label="iOS version" hidden></div></div>
        <div class="devices" id="devices" role="radiogroup" aria-labelledby="deviceLabel">loading…</div>
        <div class="note" id="deviceNote" hidden></div>
        <label for="clones" style="margin-top:10px">Simulators in parallel</label>
        <div class="clones"><input type="number" id="clones" min="1" max="8" step="1" value="4">
          <span class="muted" id="clonesNote"></span></div>
        <div class="note muted" id="hostNote" hidden></div>
        <div class="note warn" id="hostWarn" hidden></div>
        <label class="check" for="skipBuild" title="A record builds the app first (about a minute when nothing changed) and installs it where a simulator's app differs. Skipping only skips the build: the install check still uses the last build in DerivedData."><input type="checkbox" id="skipBuild"> Skip the build (the install check still runs)</label>
        <div class="buttons" style="margin-top:8px"><button id="refreshSims" type="button">Refresh</button>
          <button id="btnDeleteClones" type="button" class="danger">Delete clones</button></div></div>
      <div><label for="appearance">Appearance</label>
        <select id="appearance"><option value="both">both</option><option value="light">light</option><option value="dark">dark</option></select>
        <label for="filter" style="margin-top:8px">Filter: name contains (comma-separated)</label>
        <input type="text" id="filter" placeholder="e.g. Settings, Feed.default">
        <label for="feature" style="margin-top:8px">Feature, exact (comma-separated)</label>
        <input type="text" id="feature" placeholder="e.g. Home">
        <div class="muted" id="matchInfo"></div>
        <button id="loadMenu" type="button">Load variant list</button></div>
      <div><label for="settle">Settle ms</label><input type="number" id="settle" value="1500" step="100" min="0">
        <label for="budget" style="margin-top:8px">Budget minutes (blank: none)</label><input type="number" id="budget" min="0.1" step="1"></div>
    </div>
    <div class="buttons">
      <button class="primary" id="btnRecord" type="button" title="Build the app, install it where a simulator's app differs, then capture">Record</button>
      <button class="danger" id="btnStop" type="button">Stop</button>
    </div>
  </section>
  <section class="panel" id="progressPanel">
    <div id="progressText" class="muted">idle</div>
    <progress id="progress" value="0" max="1"></progress>
    <div class="buttons" id="afterRun" hidden></div>
    <pre id="log"></pre>
  </section>
</div>

<div class="view" id="view-recordings" hidden>
  <h1>Recordings</h1>
  <p class="lead">Every run, newest first, under its simulator type. One run per type is its <strong>baseline</strong>; comparisons are computed from the files when you open them.</p>
  <section class="panel" id="syncPanel">
    <div class="sync-head"><strong>Sync</strong> <span class="muted" id="syncBucket">…</span></div>
    <div class="buttons">
      <button id="btnPushBaseline" type="button" disabled title="Push every pinned run and baselines.json (a pinned run\'s own Push does the same for its type)">Push baseline</button>
      <button id="btnPull" type="button" disabled title="Pull baselines.json and every pinned run missing or behind here">Pull</button>
    </div>
    <div class="muted" id="syncLast"></div>
    <details id="syncRemote"><summary>Remote runs</summary><div id="syncRemoteList"><p class="muted">loading…</p></div></details>
    <pre id="syncLog" hidden></pre>
  </section>
  <div id="recGroups"><p class="muted">loading…</p></div>
</div>

<div class="view" id="view-gallery" hidden>
  <h1 id="galleryTitle">Run</h1>
  <div class="muted" id="gallerySub"></div>
  <div class="gactions" id="galleryActions"></div>
  <details id="galleryHistory" hidden><summary></summary><ul class="history"></ul></details>
  <div class="toolbar" role="toolbar" aria-label="Gallery filters">
    <div class="seg" id="appearanceSeg" role="group" aria-label="Appearance">
      <button type="button" data-a="light">light</button><button type="button" data-a="dark">dark</button><button type="button" data-a="both">both</button></div>
    <input type="search" id="galleryText" placeholder="Filter names" aria-label="Filter variant names">
    <label class="size">size <input type="range" id="gallerySize" min="100" max="400" step="10" aria-label="Thumbnail size"></label>
    <span id="galleryCount"></span>
  </div>
  <div class="chips" id="featureChips" role="group" aria-label="Features"></div>
  <div id="galleryGrid"></div>
</div>

<div class="view" id="view-results" hidden>
  <h1 id="resultsTitle">Comparison</h1>
  <div class="verdict" id="verdict"></div>
  <div id="facts"></div>
  <div class="note warn" id="resultsNotes" hidden></div>
  <div class="buttons" id="resultsActions">
    <a class="button" id="viewRun">View run</a>
    <a class="button" id="viewAgainst">View the other run</a>
    <button id="btnRecompute" type="button" title="Judge every screen again, even though neither run changed (after an exclusions change, say)">Recompute</button>
    <button id="btnCancelCompare" type="button" class="danger" hidden title="Stop this comparison; nothing is stored">Cancel</button>
  </div>
  <div class="note muted" id="resultsStatus" role="status" hidden></div>
  <section id="failSection" hidden aria-labelledby="failTitle">
    <div class="fail-head"><h2 id="failTitle">Failures</h2>
      <div class="seg" id="failViewSeg" role="group" aria-label="How to show each failure">
        <button type="button" data-v="side">Side by side</button><button type="button" data-v="overlay">Overlay</button><button type="button" data-v="swipe">Swipe</button></div></div>
    <div class="note" id="reviewSummary" hidden></div>
    <div id="failures"></div>
  </section>
  <section class="panel" id="resultsPanel" hidden>
    <div class="table-wrap"><table class="counts" id="counts"></table></div>
    <h2>MISSING, ERROR, SKIPPED</h2>
    <div class="table-wrap"><table><thead><tr><th>status</th><th>variant</th><th>appearance</th><th>reason</th><th>simulator</th></tr></thead>
      <tbody id="problems"></tbody></table></div>
    <details id="passDetails"><summary id="passSummary">PASS</summary>
      <div class="table-wrap"><table><tbody id="passes"></tbody></table></div></details>
    <div id="detail"></div>
  </section>
</div>

</main>

<div id="lightbox" hidden role="dialog" aria-modal="true" aria-label="Screenshot viewer">
  <div class="lb-bar">
    <div class="lb-caption"><div id="lbTitle"></div><div class="sub" id="lbSub"></div></div>
    <button type="button" id="lbOut" aria-label="Zoom out">&#8722;</button><span id="lbZoom">100%</span>
    <button type="button" id="lbIn" aria-label="Zoom in">+</button>
    <button type="button" id="lbFit">Fit</button><button type="button" id="lbOne">1:1</button>
    <span class="seg" id="lbLayers" role="group" aria-label="Layer"><button type="button" data-layer="baseline">baseline</button><button type="button" data-layer="current">current</button><button type="button" data-layer="diff">diff</button><button type="button" data-layer="overlay">overlay</button></span>
    <span class="lb-ovl" id="lbOverlay" hidden>baseline<input type="range" id="lbMix" min="0" max="100" aria-label="Overlay: baseline to current">current
      <label><input type="checkbox" id="lbMaskOn"> red mask</label></span>
    <button type="button" id="lbToggle">dark</button>
    <button type="button" id="lbClose" aria-label="Close">&#10005;</button>
  </div>
  <div class="lb-body">
    <div class="lb-stage" id="lbStage"><div id="lbBox"><img id="lbBase" alt="" draggable="false" hidden><img id="lbImg" alt="" draggable="false"><img id="lbMask" alt="" draggable="false" hidden></div></div>
    <button type="button" class="lb-nav prev" id="lbPrev" aria-label="Previous variant">&#8249;</button>
    <button type="button" class="lb-nav next" id="lbNext" aria-label="Next variant">&#8250;</button>
  </div>
  <div class="lb-hint">wheel or pinch to zoom · drag to pan · double-click: fit / 1:1 · &#8592; &#8594; previous / next · T light / dark · B C D O baseline / current / diff / overlay · Esc close</div>
</div>

<dialog id="pickDialog" aria-labelledby="pickTitle"><form method="dialog">
  <p><strong id="pickTitle">Compare against</strong></p>
  <div id="pickBody"></div>
  <div class="buttons"><button value="cancel">Cancel</button></div>
</form></dialog>

<dialog id="acceptDialog"><form method="dialog">
  <p><strong>Accept this screen into the baseline?</strong></p>
  <p id="acceptWhat"></p>
  <p class="muted">Read the diff first: red is every pixel that differs from the baseline. The run's screenshot is copied over the baseline's, which gets a new revision.</p>
  <div class="shots"><figure><img id="acceptDiff" alt="diff"><figcaption>diff</figcaption></figure>
    <figure><img id="acceptCurrent" alt="current"><figcaption>this run (becomes the baseline's)</figcaption></figure></div>
  <div class="buttons"><button value="cancel">Cancel</button><button class="primary" value="accept" id="acceptConfirm">Accept as baseline</button></div>
</form></dialog>

<script>
"use strict";
const CATALOG_DIR = __CATALOG_DIR__;
const $ = (id) => document.getElementById(id);
const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* per-viewer convenience only */ } },
};
const STATUS_ORDER = ["PASS", "FAIL", "MISSING", "ERROR", "SKIPPED", "RECORDED"];
let since = 0, knownRunId = null, wasActive = false, afterShownFor = null, pollTimer = null;
let menu = null, simsLoaded = false, selectedKey = null;
let jobActive = false, statusSeen = false, lastStatus = null, runningCompares = new Set();
let route = {view: "home"};
let runsData = null;  // the last /api/runs
let syncReady = false;  // a store is configured and, for S3, the aws CLI is on the server's PATH
let storeName = __STORE_NAME__;  // what messages call the store: "the bucket", or "the shared folder" for a folder
const capitalised = (text) => text.charAt(0).toUpperCase() + text.slice(1);

async function api(path, body) {
  const options = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.status + " " + response.statusText);
  return data;
}
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v; else if (k.startsWith("on")) node.addEventListener(k.slice(2), v); else node.setAttribute(k, v);
  }
  for (const child of children.flat()) node.append(child === null || child === undefined ? "" : child);
  return node;
}
function list(text) { return text.split(",").map((s) => s.trim()).filter(Boolean); }
let deviceTypes = [], host = null;
function chosenType() { const i = document.querySelector("#devices input:checked"); return i ? deviceTypes.find((t) => t.id === i.value) : null; }
function chosenClones() { const n = Math.round(Number($("clones").value)); return Math.min(8, Math.max(1, n || 1)); }
function typeLabel(t) { return t.model + " · " + t.os + (t.osBuild ? " (" + t.osBuild + ")" : ""); }
function log(text, mode) {
  const pre = $(mode === "push" || mode === "pull" ? "syncLog" : "log");
  pre.hidden = false; pre.textContent += text + "\n"; pre.scrollTop = pre.scrollHeight;
}
function fmtSeconds(s) { if (s === null || s === undefined) return "–"; const m = Math.floor(s / 60); return (m ? m + "m " : "") + (s % 60) + "s"; }
function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
}
function countsText(bucket, sep) {
  return STATUS_ORDER.filter((s) => bucket && bucket[s]).map((s) => bucket[s] + " " + s).join(sep || " / ") || "nothing";
}
function screensText(counts) {
  const entries = Object.entries(counts || {});
  return entries.length ? entries.map(([a, n]) => n + " " + a).join(" · ") : "no screenshots";
}
function screenTotal(counts) { return Object.values(counts || {}).reduce((sum, n) => sum + n, 0); }
function fmtBytes(n) {
  if (!n) return "–";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return (i ? n.toFixed(n >= 10 ? 0 : 1) : n) + " " + units[i];
}
function rev(n) { return el("code", {class: "rev"}, n ? "r" + n : "r?"); }
// Where the run's commit stands against main: [{text, warn}], or [] when the server has no git view of it.
function standingParts(run) {
  const m = run.main, parts = [];
  const n = (count, noun) => count + " " + noun + (count === 1 ? "" : "s");
  if (m) {
    if (m.state === "at") parts.push({text: "at " + m.ref, warn: false});
    else if (m.state === "behind") parts.push({text: n(m.behind, "commit") + " behind " + m.ref, warn: true});
    else if (m.state === "off") parts.push({text: "recorded off " + m.ref + ": " + n(m.ahead, "commit") + " not on it" +
      (m.behind ? ", " + m.behind + " behind" : ""), warn: true});
    else if (m.gitFailed) parts.push({text: "git could not place this commit against " + m.ref, warn: true});
    else parts.push({text: "commit not in this clone: git fetch", warn: true});
  }
  if (run.git && run.git.dirty) parts.push({text: "uncommitted changes", warn: true});
  return parts;
}
// Why a run can never become the baseline, from what the listing already knows: no commit, or uncommitted changes. A
// run off main or with a commit this clone lacks is left to the server, which fetches main first and then refuses or pins.
function mainBlocker(run) {
  if (!run.git || !run.git.sha) return "Cannot be the baseline: no commit recorded";
  if (run.git.dirty) return "Cannot be the baseline: recorded with uncommitted changes";
  return null;
}
function mainFetchNote(run) {
  const m = run.main;
  if (m && m.state === "off") return ". Its commit is not on " + m.ref + " as last fetched: main is fetched first, and the pin is refused unless the commit is on it";
  if (m && m.state === "unknown") return ". Its commit is not in this clone: main is fetched first, and the pin is refused unless the commit is on it";
  return "";
}
function standingNodes(run) {
  return standingParts(run).map((p) => el("span", {class: p.warn ? "off-main" : "muted", "data-standing": p.warn ? "warn" : "ok"}, " · " + p.text));
}
function filterText(run) {
  const parts = [];
  if (run.filter && run.filter.label && run.filter.label !== "all") parts.push(run.filter.label);
  if (run.appearance && run.appearance !== "both") parts.push(run.appearance + " only");
  return parts.join(" · ") || "all";
}
function runTitle(run) {  // a run as a heading names it
  return run.pinned ? "Baseline r" + run.revision + " · " + run.typeLabel : "Run · " + run.typeLabel + " · " + fmtTime(run.started);
}
function againstText(against, pinned, revision) {
  return pinned ? "Baseline r" + (revision || against.revision) + " (" + against.id + ")" : "run " + against.id;
}
function historyRev(e) {
  if (!e.revision) return null;
  if (e.kind === "pin") return "r" + e.revision;
  return (e.revision > 1 ? "r" + (e.revision - 1) + " → " : "") + "r" + e.revision;
}
function historyText(e) {
  const when = " · " + fmtTime(e.at);
  const shots = Array.isArray(e.variants) ? e.variants : [];
  if (e.kind === "pin") return "set as the baseline" + (e.replaced ? " (replaced " + e.replaced + ")" : "") + when;
  if (e.kind === "update") return "updated " + shots.length + " screen" + (shots.length === 1 ? "" : "s") + " from " + e.from + when;
  if (e.kind === "accept") {
    const shot = shots[0] || {name: e.name, appearance: e.appearance};
    return "accepted " + shot.name + " (" + shot.appearance + ") from " + (e.from || e.run) + when;
  }
  const n = e.variantCount !== undefined ? e.variantCount : e.variants;
  return (e.full ? "recorded " : "refreshed ") + n + " variant" + (n === 1 ? "" : "s") + " (" + (e.filterLabel || "all") + ")" + when;
}

// MARK: Routing. #record[/<deviceDir>], #recordings, #recordings/<run>[/<name>/<appearance>[/<layer>]] (a gallery),
// #compare/<run>/<against>[/<name>/<appearance>[/<layer>]] (a comparison of two runs of one type)

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const parts = raw ? raw.split("/").map((p) => { try { return decodeURIComponent(p); } catch (e) { return p; } }) : [];
  if (parts[0] === "record") return {view: "record", deviceDir: parts[1] || null};
  if (parts[0] === "compare" && parts[1]) return {view: "results", run: parts[1], against: parts[2] || null, name: parts[3] || null,
    appearance: parts[4] || null, layer: parts[5] || null};
  if (parts[0] === "recordings") {
    if (parts.length < 2 || !parts[1]) return {view: "recordings"};
    return {view: "gallery", id: parts[1], name: parts[2] || null, appearance: parts[3] || null, layer: parts[4] || null};
  }
  return {view: "home"};
}
function galleryHash(id) { return "#recordings/" + encodeURIComponent(id); }
function shotHash(id, name, appearance, layer) {
  return galleryHash(id) + "/" + encodeURIComponent(name) + "/" + appearance + (layer && layer !== "current" ? "/" + layer : "");
}
function compareHash(run, against) { return "#compare/" + encodeURIComponent(run) + (against ? "/" + encodeURIComponent(against) : ""); }
function resultsShotHash(run, against, name, appearance, layer) {
  return compareHash(run, against) + "/" + encodeURIComponent(name) + "/" + appearance + (layer && layer !== "current" ? "/" + layer : "");
}
function parentHash(r) {
  if (r.view === "gallery") return r.name ? galleryHash(r.id) : "#recordings";
  if (r.view === "results") return r.name ? compareHash(r.run, r.against) : galleryHash(r.run);
  return "#";
}
function renderCrumbs(r) {
  const parts = [];
  if (r.view === "record") parts.push("Record");
  if (r.view === "recordings") parts.push("Recordings");
  if (r.view === "gallery") {
    parts.push(el("a", {href: "#recordings"}, "Recordings"));
    const label = gallery && gallery.id === r.id ? runTitle(gallery.run) : r.id;
    parts.push(r.name ? el("a", {href: galleryHash(r.id)}, label) : label);
    if (r.name) parts.push(r.name + " · " + (lb.appearance || r.appearance || ""));
  }
  if (r.view === "results") {
    const shown = results && results.run.id === r.run ? results : null;
    parts.push(el("a", {href: "#recordings"}, "Recordings"), el("a", {href: galleryHash(r.run)}, shown ? runTitle(shown.run) : r.run));
    const against = shown ? "against " + againstText(shown.against, shown.againstPinned, shown.againstRevision) : "against " + (r.against || "the baseline");
    parts.push(r.name ? el("a", {href: compareHash(r.run, r.against)}, against) : against);
    if (r.name) parts.push(r.name + " · " + (lb.appearance || r.appearance || ""));
  }
  const nodes = [];
  parts.forEach((p, i) => { if (i) nodes.push(" › "); nodes.push(p); });
  $("crumbs").replaceChildren(...nodes);
  $("back").hidden = r.view === "home";
}
async function render() {
  const previous = route;
  route = parseHash();
  for (const view of ["home", "record", "recordings", "gallery", "results"]) $("view-" + view).hidden = view !== route.view;
  if (previous.view !== route.view) window.scrollTo(0, 0);
  renderCrumbs(route);
  if (!route.name) closeLightbox(false);
  if (route.view === "home") renderHome();
  else if (route.view === "record") showRecord(route);
  else if (route.view === "recordings") renderRecordings();
  else if (route.view === "gallery") await showGallery(route, previous);
  else if (route.view === "results") showResults(route, previous);
}
window.addEventListener("hashchange", render);
$("back").onclick = () => { location.hash = parentHash(route); };

// MARK: Home

async function renderHome() {
  $("catalogDir").textContent = "catalog: " + CATALOG_DIR;
  try {
    const data = await api("/api/runs");
    runsData = data;
    const pins = data.runs.filter((r) => r.pinned);
    $("stRecord").replaceChildren(...(pins.length ? pins.map((p) => el("div", {"data-pin": p.id},
      p.model + " " + p.os + ": baseline r" + p.revision + " · " + screensText(p.counts)))
      : ["no baseline set yet"]));
    const active = data.runs.filter((r) => r.active).length;
    $("stRecordings").textContent = data.runs.length + " run" + (data.runs.length === 1 ? "" : "s") + " · " + pins.length + " pinned" +
      (active ? " · " + active + " recording" : "");
  } catch (e) { $("stRecord").textContent = "error: " + e.message; }
}

// MARK: Record

function showRecord(r) {
  if (!simsLoaded) { simsLoaded = true; loadSims(r.deviceDir); return; }
  if (r.deviceDir) pickDeviceDir(r.deviceDir);
  updateDeviceNotes();
}
function pickDeviceDir(deviceDir) {
  const t = deviceTypes.find((x) => x.deviceDir === deviceDir);
  const input = t && document.getElementById("dt-" + t.id);
  if (input) { input.checked = true; store.set("deviceType", t.id); menu = null; updateMatch(); }
}
async function loadSims(preferDir) {
  const box = $("devices"); box.textContent = "loading…";
  try {
    const data = await api("/api/device-types");
    deviceTypes = data.deviceTypes; host = data.host || null;
    // Until a count is chosen, the default is what this Mac's memory allows.
    if (host && host.recommendedClones && store.get("clones", null) === null) $("clones").value = host.recommendedClones;
    box.textContent = "";
    if (!deviceTypes.length) { box.textContent = "No iOS simulator. Make one in Xcode or with simctl create."; updateDeviceNotes(); return; }
    const stored = store.get("deviceType", null);
    const pick = (deviceTypes.find((t) => preferDir && t.deviceDir === preferDir) || deviceTypes.find((t) => t.id === stored) ||
      deviceTypes.find((t) => t.isBaseline) || deviceTypes[0]).id;
    renderDeviceList(pick);
    if (preferDir) store.set("deviceType", pick);
    updateDeviceNotes();
  } catch (e) { box.textContent = "error: " + e.message; }
}
// Two segmented controls over the list: the families present (iPhone, then iPad, then anything else)
// and, within the family, its iOS versions, newest first; each hidden when it has one choice. The
// chosen type decides the segments shown, so a preselected or remembered type opens on its own.
function renderDeviceList(pick) {
  const box = $("devices"), familySeg = $("deviceFamilySeg"), osSeg = $("deviceOsSeg");
  const families = [...new Set(deviceTypes.map((t) => t.family))];
  const chosen = deviceTypes.find((t) => t.id === pick) || deviceTypes[0];
  const family = families.includes(chosen.family) ? chosen.family : families[0];
  const versions = [...new Set(deviceTypes.filter((t) => t.family === family).map((t) => t.os))];
  const os = versions.includes(chosen.os) ? chosen.os : versions[0];
  familySeg.hidden = families.length < 2;
  familySeg.replaceChildren(...families.map((f) => el("button", {type: "button", "data-family": f, "aria-pressed": String(f === family),
    onclick: () => showGroup(f, null)}, f === "other" ? "Other" : f)));
  osSeg.hidden = versions.length < 2;
  osSeg.replaceChildren(...versions.map((v) => {
    const build = deviceTypes.find((t) => t.family === family && t.os === v && t.osBuild);
    return el("button", {type: "button", "data-os": v, "aria-pressed": String(v === os), title: build ? "build " + build.osBuild : null,
      onclick: () => showGroup(family, v)}, v);
  }));
  box.textContent = "";
  for (const t of deviceTypes) {
    if (t.family !== family || t.os !== os) continue;
    const id = "dt-" + t.id;
    const counts = [t.available + " available", t.booted ? t.booted + " booted" : null,
      t.clones ? t.clones + " clone" + (t.clones === 1 ? "" : "s") : null].filter(Boolean).join(" · ");
    box.append(el("label", {for: id}, el("input", {type: "radio", name: "deviceType", id, value: t.id, checked: t.id === pick}),
      el("span", {}, t.model, " ", t.isBaseline ? el("span", {class: "badge baseline", title: "baseline r" + t.pinned.revision}, "baseline") : "", " ",
        el("span", {class: "count"}, counts))));
  }
}
function showGroup(family, os) {
  // Switching a segment picks that group's first type (its baseline, if it has one) so a choice is
  // always made; `os` null means the family's newest version, unless the current type is in the family.
  const current = chosenType();
  const inGroup = (t) => t.family === family && (os ? t.os === os : true);
  const next = current && inGroup(current) ? current
    : deviceTypes.find((t) => inGroup(t) && t.isBaseline) || deviceTypes.find(inGroup);
  if (!next) return;
  renderDeviceList(next.id);
  store.set("deviceType", next.id); menu = null; updateMatch();
  updateDeviceNotes();
}
function updateDeviceNotes() {
  const t = chosenType(), n = chosenClones(), note = $("deviceNote");
  let text = "", warn = false;
  if (t && t.pinned) {
    text = "Baseline r" + t.pinned.revision + " (" + t.pinned.run + "). A record leaves it as it is; compare the new run with it when it finishes.";
    if (t.pinned.osBuild && t.osBuild && t.pinned.osBuild !== t.osBuild) {
      text += " The baseline was recorded on build " + t.pinned.osBuild + "; this runtime is " + t.osBuild + ".";
      warn = true;
    }
  } else if (t) {
    text = "No baseline for " + t.model + " · " + t.os + ": set one from a run in Recordings.";
    warn = true;
  }
  note.textContent = text; note.hidden = !text; note.classList.toggle("warn", warn);
  $("clonesNote").textContent = n === 1 ? "this simulator only" : "this simulator plus " + (n - 1) + (n === 2 ? " clone" : " clones") + " of it (screencatalog-clone-*)";
  const hostNote = $("hostNote"), hostWarn = $("hostWarn");
  if (host) {
    const gb = (x) => Number(x).toFixed(1).replace(/\.0$/, "");
    const parts = ["Recommended: " + host.recommendedClones];
    if (host.freeGB != null) parts.push("free memory " + gb(host.freeGB) + " GB");
    if (host.swapTotalGB != null) parts.push("swap " + gb(host.swapUsedGB) + " of " + gb(host.swapTotalGB) + " GB");
    if (host.bootedSimulators != null) parts.push(host.bootedSimulators + (host.bootedSimulators === 1 ? " simulator" : " simulators") + " already booted");
    hostNote.textContent = parts.join(" · ");
    hostNote.hidden = false;
    hostWarn.textContent = "More than the recommended " + host.recommendedClones + ": this Mac is likely to swap and captures slow from ~7 s to a minute or fail. Shut down other simulators first.";
    hostWarn.hidden = n <= host.recommendedClones;
  } else { hostNote.hidden = true; hostWarn.hidden = true; }
  $("btnDeleteClones").disabled = !t || !t.clones || jobActive;
  $("btnDeleteClones").title = t && t.clones ? "Delete the " + t.clones + " screencatalog-clone-* simulator(s) of " + typeLabel(t) : "No clones of this type";
}
async function deleteClones() {
  const t = chosenType();
  if (!t || !t.clones) return;
  if (!confirm("Delete the " + t.clones + " screencatalog-clone-* simulator(s) of " + typeLabel(t) + "? The next run with clones makes them again.")) return;
  try {
    const r = await api("/api/clones/delete", {deviceType: t.id});
    log("deleted " + r.deleted.length + " clone(s): " + r.deleted.map((d) => d.name + " (" + d.udid + ")").join(", "));
  } catch (e) { alert(e.message); }
  loadSims();
}
function updateMatch() {
  if (!menu) { $("matchInfo").textContent = ""; return; }
  const only = list($("filter").value), features = list($("feature").value);
  const n = menu.filter((v) => (!only.length || only.some((o) => v.name.includes(o))) && (!features.length || features.includes(v.feature))).length;
  $("matchInfo").textContent = n + " of " + menu.length + " variants match";
}
async function loadMenu() {
  const t = chosenType();
  if (!t) { alert("Pick a simulator type first."); return; }
  $("matchInfo").textContent = "reading the variant list…";
  try { menu = (await api("/api/menu?deviceType=" + encodeURIComponent(t.id))).variants; updateMatch(); }
  catch (e) { $("matchInfo").textContent = "error: " + e.message; }
}
function began(r) {
  $("log").textContent = ""; $("syncLog").textContent = ""; since = 0; knownRunId = r.runId; wasActive = true;
  $("afterRun").hidden = true; log("started " + r.runId, r.mode); poll();
}
async function startRun() {
  const budget = $("budget").value.trim();
  const t = chosenType();
  if (!t) { alert("Pick a simulator type first."); return; }
  // A record only ever writes its own new run folder: nothing to confirm.
  const body = {mode: "record", appearance: $("appearance").value,
    filters: {only: list($("filter").value), feature: list($("feature").value)},
    settleMs: Number($("settle").value), budgetMinutes: budget ? Number(budget) : null, deviceType: t.id, clones: chosenClones(),
    skipBuild: $("skipBuild").checked};
  try { began(await api("/api/run", body)); } catch (e) { alert(e.message); }
}
async function stopRun() { try { await api("/api/stop", {}); } catch (e) { alert(e.message); } }
function setActive(isActive) {
  jobActive = isActive;
  for (const id of ["btnRecord", "loadMenu"]) $(id).disabled = isActive;
  for (const id of ["btnPushBaseline", "btnPull"]) $(id).disabled = isActive || !syncReady;
  for (const b of document.querySelectorAll('button[data-action="push"]')) b.disabled = isActive || !syncReady || !!b.dataset.blocked;
  $("btnStop").disabled = !isActive;
  updateDeviceNotes();
}
function updateStrip(s) {
  const strip = $("runStrip");
  const compares = s.compares || [];
  if (s.active) {
    const verb = s.mode === "record" && s.phase === "building" ? "Building" : ({record: "Recording", push: "Pushing", pull: "Pulling"}[s.mode] || s.mode);
    strip.href = s.mode === "push" || s.mode === "pull" ? "#recordings" : "#record";
    strip.title = "run " + s.runId;
    strip.replaceChildren(el("span", {class: "dot"}), verb + " " + s.done + "/" + s.total + (s.eta !== null ? " · ETA " + fmtSeconds(s.eta) : ""));
  } else if (compares.length) {
    const c = compares[0];
    strip.href = compareHash(c.run, c.against);
    strip.title = "comparing " + c.run + " against " + c.against;
    strip.replaceChildren(el("span", {class: "dot"}), "Comparing " + c.run + " · " + Math.round(c.seconds) + " s" +
      (compares.length > 1 ? " · +" + (compares.length - 1) : ""));
  }
  strip.hidden = !s.active && !compares.length;
}
function renderAfterRun(s) {
  const box = $("afterRun");
  box.replaceChildren();
  const sum = s.summary;
  if (s.mode === "record" && sum && sum.duplicate) {
    const d = sum.duplicate;
    box.append(el("span", {class: "note warn", style: "align-self:center"}, "Not recorded: a complete run of this build already exists ",
      d.where === "local" ? el("a", {href: galleryHash(d.run)}, d.run) : el("span", {}, "in " + storeName + ": ", el("code", {}, d.run), " (Pull it from Recordings)"),
      ". To record this build again, delete that run first."));
  }
  if (s.mode === "record" && sum && sum.run) {
    box.append(el("a", {class: "button primary", href: galleryHash(sum.run)}, "View run"));
    if (sum.comparable) box.append(el("a", {class: "button", href: compareHash(sum.run, sum.pinned)}, "Compare with baseline"));
    box.append(el("span", {class: "muted", style: "align-self:center"},
      (sum.counts ? countsText(sum.counts.all) : "nothing captured") + " in " + sum.run +
      (sum.comparable ? "" : " · no baseline for " + sum.typeLabel + " yet: set one from Recordings")));
  }
  box.hidden = !box.childElementCount;
}
async function poll() {
  clearTimeout(pollTimer);
  let s;
  let unseen = false;  // a job that started and maybe finished between two polls
  try {
    s = await api("/api/status?since=" + since);
    if (s.runId !== knownRunId) {  // a run started in another tab, or the page just opened
      unseen = statusSeen;
      $("log").textContent = ""; $("syncLog").textContent = ""; since = 0; knownRunId = s.runId;
      s = await api("/api/status?since=0");
    }
  } catch (e) { pollTimer = setTimeout(poll, 4000); return; }
  statusSeen = true; lastStatus = s;
  for (const line of s.lines) log(line.text, s.mode);
  since = s.lastSeq;
  setActive(s.active);
  const bar = $("progress"); bar.max = Math.max(1, s.total); bar.value = s.done;
  if (s.runId) {
    let text = (s.active ? "running " : "finished ") + s.mode + " · " + s.done + " / " + s.total;
    if (s.perVariantMs) text += " · " + (s.perVariantMs / 1000).toFixed(2) + " s/variant";
    if (s.active && s.eta !== null) text += " · ETA " + fmtSeconds(s.eta);
    if (!s.active && s.exitCode !== null) text += " · exit " + s.exitCode;
    $("progressText").textContent = text;
  }
  updateStrip(s);
  const now = new Set((s.compares || []).map((c) => c.run + "|" + c.against));
  const ended = [...runningCompares].some((k) => !now.has(k));
  runningCompares = now;
  if (ended && (route.view === "recordings" || route.view === "home")) render();
  if (!s.active && s.runId && afterShownFor !== s.runId) {
    afterShownFor = s.runId;
    renderAfterRun(s);
    if (wasActive || unseen) {
      if (simsLoaded) loadSims(route.view === "record" ? route.deviceDir : null);
      if (route.view === "home") renderHome();
      if (route.view === "recordings") renderRecordings();
    }
  }
  wasActive = s.active;
  pollTimer = setTimeout(poll, s.active || now.size ? 1000 : 4000);
}

// MARK: Recordings: one table per simulator type, newest iOS first; one row per run, newest first

async function loadRuns() { runsData = await api("/api/runs"); return runsData; }
async function renderRecordings() {
  let data;
  try { data = await loadRuns(); }
  catch (e) { $("recGroups").replaceChildren(el("p", {}, "error: " + e.message)); return; }
  renderSync(data.sync);
  if (!data.runs.length) {
    $("recGroups").replaceChildren(el("p", {class: "muted"}, "No runs yet. ", el("a", {href: "#record"}, "Record one"), "."));
    return;
  }
  const byId = new Map(data.runs.map((r) => [r.id, r]));
  $("recGroups").replaceChildren(...data.groups.map((g) => {
    const pin = g.pinned ? byId.get(g.pinned) : null;
    return el("section", {class: "rgroup", "data-type": g.deviceDir},
      el("h2", {}, g.typeLabel, " ", pin ? el("span", {class: "muted"}, "baseline r" + pin.revision, ...standingNodes(pin))
        : el("span", {class: "no-pin", "data-no-baseline": ""}, "no baseline: set one from a run below")),
      el("div", {class: "table-wrap"}, el("table", {class: "runs-table"},
        el("thead", {}, el("tr", {}, ...["run", "screenshots", "size", "app", "git", "recorded", ""].map((h) => el("th", {}, h)))),
        el("tbody", {}, ...g.runs.map((id) => runRow(byId.get(id), pin))))));
  }));
}
// MARK: Sync: push and pull through the store (S3 or a folder) as jobs; the server names it in sync.label

function renderSync(sync) {
  syncReady = sync.configured && sync.aws;
  storeName = sync.store || storeName;
  $("syncBucket").textContent = !sync.configured
      ? "not configured: set storage in screencatalog.json, or " + sync.env + " in the server's environment"
    : !sync.aws ? "the aws CLI is not on the server's PATH (brew install awscli)"
    : sync.label;
  $("syncLast").textContent = "last push " + fmtTime(sync.lastPush) + " · last pull " + fmtTime(sync.lastPull);
  setActive(jobActive);
  if ($("syncRemote").open) loadRemote(false);
}
function syncLine(state) {
  if (!state) return "";
  const parts = [];
  if (state.pushedAt) parts.push("pushed " + fmtTime(state.pushedAt) + (state.pushedRevision ? " (r" + state.pushedRevision + ")" : ""));
  if (state.pulledAt) parts.push("pulled " + fmtTime(state.pulledAt) + (state.pulledRevision ? " (r" + state.pulledRevision + ")" : ""));
  return parts.length ? el("div", {class: "sub", "data-sync": ""}, parts.join(" · ")) : "";
}
async function syncPush(runs) {
  const body = runs ? {runs: runs} : {};
  const local = runsData || await loadRuns().catch(() => null);
  const pushesPins = !runs || (local && runs.some((id) => Object.values(local.baselines).includes(id)));
  if (pushesPins) {
    // Pushing the pins (Push baseline, or a pinned run's own Push): when the store pins a type to a run this Mac
    // has never had (deleted here, or pinned by someone else), the push is refused unless the replacement is
    // deliberate. Ask, naming both runs.
    const replaced = await pinsThisPushReplaces();
    if (replaced === null) return;
    if (replaced.length) {
      const lines = replaced.map((r) => r.typeLabel + ": " + storeName + "'s baseline is " + r.remote + ", yours is " + r.local);
      if (!confirm("Replace " + storeName + "'s baseline?\n\n" + lines.join("\n") + "\n\nThe old run stays in " + storeName + " until you delete it; " +
          "a Mac that pulls afterwards gets yours.")) return;
      body.replacePins = true;
    }
  }
  try { began(await api("/api/sync/push", body)); } catch (e) { alert(e.message); }
}
// The types whose remote pin is a run not in this catalog while this Mac pins another: [] when none,
// null when the remote could not be read (the push then reports the refusal itself).
async function pinsThisPushReplaces() {
  let remote, local;
  try { remote = await api("/api/sync/remote?refresh=1"); local = runsData || await loadRuns(); }
  catch (e) { alert(e.message); return null; }
  const here = new Set(local.runs.map((r) => r.id));
  return Object.entries(remote.baselines || {}).filter(([type, run]) => !here.has(run) && local.baselines[type] && local.baselines[type] !== run)
    .map(([type, run]) => ({typeLabel: (local.groups.find((g) => g.deviceDir === type) || {}).typeLabel || type, remote: run, local: local.baselines[type]}));
}
async function syncPull(runs) {
  try { began(await api("/api/sync/pull", runs ? {runs: runs} : {})); } catch (e) { alert(e.message); }
}
async function syncDelete(r) {
  if (!confirm("Delete " + r.id + " (" + (r.model || "?") + " · " + (r.os || "?") + ", " + rev(r.revision) + ") from " + storeName + "? " +
      "A Mac that pulled it keeps its copy; this cannot be undone.")) return;
  try { await api("/api/sync/delete", {runs: [r.id]}); } catch (e) { alert(e.message); return; }
  await loadRemote(true);
  if (route.view === "recordings") await renderRecordings();
}
async function loadRemote(refresh) {
  const box = $("syncRemoteList");
  if (!syncReady) { box.replaceChildren(el("p", {class: "muted"}, "Sync is not configured.")); return; }
  let data;
  try { data = await api("/api/sync/remote" + (refresh ? "?refresh=1" : "")); }
  catch (e) { box.replaceChildren(el("p", {}, "error: " + e.message)); return; }
  if (!data.runs.length) { box.replaceChildren(el("p", {class: "muted"}, "Nothing pushed yet.")); return; }
  const said = {missing: "not here", behind: "behind here", same: "same here", ahead: "newer here", diverged: "changed on both sides"};
  box.replaceChildren(el("div", {class: "table-wrap"}, el("table", {class: "runs-table", id: "remoteTable"},
    el("thead", {}, el("tr", {}, ...["run", "type", "revision", "here", ""].map((h) => el("th", {}, h)))),
    el("tbody", {}, ...data.runs.map((r) => el("tr", {class: r.pinned ? "pinned" : null, "data-remote": r.id},
      el("td", {}, r.pinned ? [el("span", {class: "badge baseline"}, "Baseline"), " "] : "", el("code", {}, r.id)),
      el("td", {}, (r.model || "?") + " · " + (r.os || "?")),
      el("td", {}, rev(r.revision)),
      el("td", {}, said[r.local] || r.local),
      el("td", {class: "actions"}, el("div", {class: "buttons"},
        r.local === "missing" || r.local === "behind"
          ? el("button", {type: "button", "data-action": "pull", disabled: jobActive, onclick: () => syncPull([r.id])}, "Pull") : "",
        el("span", {title: r.pinned ? capitalised(storeName) + "'s baseline of its type: push another baseline for that type first" : "Delete this run from " + storeName},
          el("button", {type: "button", class: "danger", "data-action": "delete-remote", disabled: jobActive || r.pinned,
            onclick: () => syncDelete(r)}, "Delete"))))))))),
    el("p", {class: "muted"}, "Listed " + fmtTime(data.fetchedAt) + (data.cached ? " (cached)" : "") + " · ",
      el("a", {href: "#recordings", onclick: (e) => { e.preventDefault(); loadRemote(true); }}, "refresh")));
}

function runRow(run, pin) {
  const busy = run.active;
  const pushWhy = run.pushBlocked;
  const title = run.pinned
    ? [el("span", {class: "badge baseline"}, "Baseline"), " ", rev(run.revision)]
    : ["Run"];
  const deleteWhy = busy ? "still capturing" : run.pinned ? "Delete the baseline: its type then has no baseline until you set one"
    : "Delete this run, its thumbnails and its comparisons";
  const bc = run.baselineCompare, reviewWhy = pin && bc && bc.blockers ? bc.blockers + " change" + (bc.blockers === 1 ? "" : "s") +
    " not confirmed by a reviewer (" + bc.review.open + " open, " + bc.review.expected + " expected, " + bc.review.rejected + " rejected): open Compare" : null;
  const pinWhy = mainBlocker(run) || reviewWhy;
  return el("tr", {class: run.pinned ? "pinned" : null, "data-run": run.id},
    el("td", {}, el("a", {href: galleryHash(run.id)}, ...title),
      run.partial ? el("div", {class: "sub", "data-filter": ""}, filterText(run)) : "",
      run.stopped && !busy ? el("div", {class: "sub"}, "stopped early") : "",
      busy ? el("div", {class: "sub"}, "capturing…") : "",
      syncLine(run.sync)),
    el("td", {}, screensText(run.counts)),
    el("td", {style: "white-space: nowrap"}, fmtBytes(run.bytes)),
    el("td", {}, run.app || "–", run.appSha ? el("div", {}, el("code", {class: "muted"}, run.appSha)) : ""),
    el("td", {}, run.git && run.git.sha ? el("code", {}, run.git.sha.slice(0, 9) + (run.git.dirty ? " dirty" : "")) : "–"),
    el("td", {style: "white-space: nowrap"}, fmtTime(run.started)),
    el("td", {class: "actions"}, el("div", {class: "buttons"},
      el("a", {class: "button", href: galleryHash(run.id)}, "Open"),
      run.pinned ? "" : el("button", {type: "button", "data-action": "compare", disabled: busy, onclick: () => openPicker(run.id)}, "Compare…"),
      run.pinned ? "" : el("span", {title: pinWhy || (pin ? "Replace the baseline with this run" : "Make this run the baseline") + mainFetchNote(run)},
        el("button", {type: "button", "data-action": "set-baseline", disabled: busy || !!pinWhy, "data-blocked": pinWhy || null,
          onclick: () => setBaseline(run, pin)}, "Set as baseline")),
      run.partial && pin && !run.pinned ? el("span", {title: pinWhy || "Copy this run's screens over the baseline" + mainFetchNote(run)},
        el("button", {type: "button", "data-action": "update-baseline", disabled: busy || !!pinWhy, "data-blocked": pinWhy || null,
          onclick: () => updateBaseline(run, pin)}, "Update baseline")) : "",
      el("button", {type: "button", "data-action": "export", disabled: busy, onclick: () => exportRun(run)}, "Export"),
      el("span", {title: pushWhy ? "Not pushed: " + pushWhy : "Push this run to " + storeName},
        el("button", {type: "button", "data-action": "push", "data-blocked": pushWhy || null,
          disabled: !!pushWhy || !syncReady || jobActive, onclick: () => syncPush([run.id])}, "Push")),
      el("span", {title: deleteWhy}, el("button", {type: "button", class: "danger", "data-action": "delete", disabled: busy,
        title: deleteWhy, onclick: () => deleteRun(run)}, "Delete")))));
}
function setBaselineQuestion(run, pin) {
  return "Set the run recorded " + fmtTime(run.started) + " (" + screensText(run.counts) + ") as the baseline of " + run.typeLabel + "?\n\n" +
    (pin ? "It replaces baseline r" + pin.revision + " (" + pin.id + "), which stays in the list as a run. "
      : "No run is the baseline of this type yet. ") +
    "Comparisons against the new baseline are computed when you open them.";
}
function updateBaselineQuestion(run, pin) {
  const n = screenTotal(run.counts);
  return "Copy this run's " + n + " screenshot" + (n === 1 ? "" : "s") + " (" + filterText(run) + ") over baseline r" + pin.revision +
    " of " + run.typeLabel + "?\n\nThe baseline becomes r" + (pin.revision + 1) + " and its history names this run.";
}
function deleteQuestion(run) {
  if (run.pinned) return "This is the baseline for " + run.typeLabel + ". After deleting it there is no baseline for that type until you set one. Delete?";
  return "Delete the run recorded " + fmtTime(run.started) + " (" + screensText(run.counts) + ", " + fmtBytes(run.bytes) +
    ") with its thumbnails and comparisons? This cannot be undone.";
}
async function afterChange() {
  if (route.view === "recordings") await renderRecordings();
  else if (route.view === "gallery") { gallery = null; await showGallery(route, {}); }
  else if (route.view === "home") renderHome();
}
async function setBaseline(run, pin) {
  // A partial or stopped run is refused by the server with the reason; only a run it would pin asks first.
  if (!run.partial && !run.stopped && !confirm(setBaselineQuestion(run, pin))) return;
  try { await api("/api/set-baseline", {run: run.id}); } catch (e) { alert(e.message); return; }
  await afterChange();
}
async function updateBaseline(run, pin) {
  if (!confirm(updateBaselineQuestion(run, pin))) return;
  try { await api("/api/update-baseline", {run: run.id}); } catch (e) { alert(e.message); return; }
  await afterChange();
}
async function exportRun(run) {
  try { const r = await api("/api/export", {run: run.id}); alert("Exported " + run.id + ": " + r.path + " (" + r.sizeKiB + " KiB)"); }
  catch (e) { alert(e.message); }
}
async function deleteRun(run) {
  if (!confirm(deleteQuestion(run))) return;
  try { await api("/api/runs/delete", {run: run.id}); } catch (e) { alert(e.message); return; }
  if (route.view === "gallery" && route.id === run.id) { location.hash = "#recordings"; return; }
  await afterChange();
}

// MARK: Compare…: the runs a run can be compared with: its type's baseline first, then the others, newest first

function pickOption(other, isPin) {
  return el("button", {type: "button", class: "pick", "data-against": other.id, onclick: () => {
    $("pickDialog").close(); location.hash = compareHash(pickFor, other.id);
  }}, isPin ? [el("span", {class: "badge baseline"}, "Baseline"), " ", rev(other.revision), " · recorded " + fmtTime(other.started)]
    : ["Run · " + fmtTime(other.started)],
  el("span", {class: "sub"}, [screensText(other.counts), other.partial ? filterText(other) : null, other.stopped ? "stopped early" : null]
    .filter(Boolean).join(" · ")));
}
let pickFor = null;
async function openPicker(runId) {
  let data;
  try { data = await loadRuns(); } catch (e) { alert(e.message); return; }
  const run = data.runs.find((r) => r.id === runId);
  if (!run) { alert("no run " + runId); return; }
  pickFor = runId;
  const byId = new Map(data.runs.map((r) => [r.id, r]));
  const pinId = data.baselines[run.deviceDir] || null;
  const others = run.comparable.map((id) => byId.get(id)).filter(Boolean);  // newest first, this type only
  const pin = others.find((r) => r.id === pinId);
  const rest = others.filter((r) => r.id !== pinId);
  $("pickTitle").textContent = "Compare " + (run.pinned ? "baseline r" + run.revision : "the run recorded " + fmtTime(run.started)) + " against";
  const body = [];
  if (!pinId) body.push(el("p", {class: "empty", "data-empty": "no-baseline"}, "No baseline for " + run.typeLabel + ". Set one from a run in the list."));
  const options = (pin ? [pickOption(pin, true)] : []).concat(rest.map((r) => pickOption(r, false)));
  if (options.length) body.push(el("div", {class: "picks"}, ...options));
  else body.push(el("p", {class: "empty", "data-empty": "no-other"}, "No other " + run.typeLabel + " run to compare against. Record one."),
    el("div", {class: "buttons"}, el("a", {class: "button primary", href: "#record/" + encodeURIComponent(run.deviceDir),
      onclick: () => $("pickDialog").close()}, "Record")));
  $("pickBody").replaceChildren(...body);
  $("pickDialog").showModal();
}

// MARK: A comparison: #compare/<run>/<against>. Computed (or reused) on open, recomputed whenever
// either run changed since: there is never a stale verdict to warn about.

let results = null, currentRun = null, resultsToken = 0, resultsTimer = null;
function countsTable(counts) {
  const t = $("counts"); t.textContent = "";
  t.append(el("tr", {}, el("th", {}, "appearance"), ...STATUS_ORDER.map((s) => el("th", {class: s}, s))));
  for (const [appearance, bucket] of Object.entries(counts)) {
    t.append(el("tr", {}, el("td", {}, appearance), ...STATUS_ORDER.map((s) => el("td", {}, String(bucket[s] || 0)))));
  }
}
function rowKey(r) { return r.name + "|" + r.appearance; }
function rowElement(r) {
  return el("tr", {class: "row" + (rowKey(r) === selectedKey ? " selected" : ""), onclick: () => showDetail(r)},
    el("td", {class: r.status}, r.status), el("td", {}, el("code", {}, r.name)), el("td", {}, r.appearance),
    el("td", {}, r.reason), el("td", {}, r.udid ? el("code", {class: "muted"}, r.udid.slice(0, 8)) : ""));
}
function showResults(r, previous) {
  const same = previous && previous.view === "results" && previous.run === r.run && previous.against === r.against;
  if (same && results && currentRun === r.run && (r.name || previous.name)) {
    if (r.name) openResultsLightbox(r, previous);  // only the viewer opened, moved or closed: the page stays as it is
    return;
  }
  if (!same) {
    selectedKey = null; results = null; currentRun = r.run; failRows = [];
    $("resultsTitle").textContent = "Run " + r.run; $("verdict").textContent = ""; $("facts").replaceChildren();
    $("resultsNotes").hidden = true; $("detail").textContent = "";
    $("resultsPanel").hidden = true; $("failSection").hidden = true;
  }
  computeResults(r, false);
}
function progressText(task) {
  // "Comparing 120 / 300 · ETA 40 s": pairs judged of all, and the time left at the rate so far.
  if (!task || task.total == null) return null;
  return "Comparing " + task.done + " / " + task.total + (task.eta != null ? " · ETA " + task.eta + " s" : "");
}
async function cancelCompare() {
  if (route.view !== "results") return;
  $("btnCancelCompare").disabled = true;
  try { await api("/api/compare/cancel", {run: route.run, against: route.against}); }
  catch (e) { setResultsStatus("error: " + e.message, true); }
  finally { $("btnCancelCompare").disabled = false; }
  refreshResults();
}
function refreshResults() { clearTimeout(resultsTimer); const token = ++resultsToken; fetchResults(route, token, true); }
function setResultsStatus(text, warn) {
  const box = $("resultsStatus");
  box.textContent = text || ""; box.hidden = !text; box.classList.toggle("warn", !!warn); box.classList.toggle("muted", !warn);
}
async function computeResults(r, force, retried) {
  const token = ++resultsToken;
  clearTimeout(resultsTimer);
  $("btnRecompute").disabled = true;
  setResultsStatus(force ? "Recomputing…" : "Comparing…");
  let started;
  try { started = await api("/api/compare", {run: r.run, against: r.against || null, force: !!force}); }
  catch (e) { if (token === resultsToken) { setResultsStatus("error: " + e.message, true); $("btnRecompute").disabled = false; } return; }
  if (token !== resultsToken) return;
  if (!r.against) {  // #compare/<run>: the baseline of its type, named in the URL from here on
    history.replaceState(null, "", compareHash(r.run, started.against));
    route = parseHash(); r = route;
  }
  if (started.status === "computing") poll();  // the header strip
  await fetchResults(r, token, retried);
}
async function fetchResults(r, token, retried) {
  let data;
  try { data = await api("/api/results?run=" + encodeURIComponent(r.run) + "&against=" + encodeURIComponent(r.against)); }
  catch (e) { if (token === resultsToken) { setResultsStatus("error: " + e.message, true); $("btnRecompute").disabled = false; } return; }
  if (token !== resultsToken || route.view !== "results" || route.run !== r.run || route.against !== r.against) return;
  renderResultsHead(data);
  $("btnCancelCompare").hidden = data.status !== "computing";
  if (data.status === "computing") {
    setResultsStatus(progressText(data.task) || "Comparing " + data.run.id + " against " +
      againstText(data.against, data.againstPinned) + "… " + Math.round(data.task.seconds) + " s");
    resultsTimer = setTimeout(() => fetchResults(r, token, retried), 1000);
    return;
  }
  $("btnRecompute").disabled = false;
  if (data.status === "cancelled") { setResultsStatus("Cancelled: nothing was stored. Recompute to compare again.", true); return; }
  if (data.status === "error") { setResultsStatus("The comparison failed: " + data.error, true); return; }
  if (data.status === "needed") {  // a run changed while it computed: once more, then say so
    if (retried) setResultsStatus("Not computed: a run keeps changing. Recompute when it settles.", true); else computeResults(r, false, true);
    return;
  }
  setResultsStatus(null);
  results = data; currentRun = data.run.id;
  poll();  // the header strip: the comparison it showed has ended
  renderResultsHead(data);
  renderFailures();
  $("resultsPanel").hidden = false;
  countsTable(data.counts);
  // FAIL rows are the cards above; these tables hold the rest, and a row's screenshots open below it on click.
  const order = {ERROR: 0, MISSING: 1, SKIPPED: 2};
  const carded = new Set(failRows.map(rowKey));
  const problems = data.rows.filter((x) => x.status in order && !carded.has(rowKey(x))).sort((a, b) => order[a.status] - order[b.status] || a.name.localeCompare(b.name));
  const passes = data.rows.filter((x) => !(x.status in order) && x.status !== "FAIL");
  $("problems").replaceChildren(...(problems.length ? problems.map(rowElement) : [el("tr", {}, el("td", {colspan: "5", class: "muted"}, "None."))]));
  $("passes").replaceChildren(...passes.map(rowElement));
  const kinds = passes.map((x) => x.status).filter((v, i, a) => a.indexOf(v) === i).join(" / ") || "PASS";
  $("passSummary").textContent = kinds + " (" + passes.length + ")";
  const again = data.rows.find((x) => rowKey(x) === selectedKey && x.status !== "FAIL");
  if (again) showDetail(again); else $("detail").textContent = "";
  renderCrumbs(route);
  if (route.name) openResultsLightbox(route, null);
}
function fact(key, ...value) { return el("span", {class: "fact"}, el("span", {class: "k"}, key), ...value); }
function runFacts(who, run) {
  const facts = [fact("recorded", fmtTime(run.started)),
    fact("device", run.model + " " + run.os + (run.osBuild ? " (" + run.osBuild + ")" : "")),
    fact("filter", filterText(run))];
  if (run.app) facts.push(fact("app", run.app + (run.appSha ? " " : ""), run.appSha ? el("code", {}, run.appSha) : ""));
  if (run.git && run.git.sha) facts.push(fact("git", el("code", {}, run.git.sha.slice(0, 9) + (run.git.dirty ? " dirty" : ""))));
  const standing = standingParts(run);
  if (standing.length) facts.push(fact("main", el("span", {class: standing.some((p) => p.warn) ? "off-main" : ""}, standing.map((p) => p.text).join(" · "))));
  if (run.simulators && run.simulators.length) facts.push(fact("simulators", String(run.simulators.length)));
  if (run.stopped) facts.push(fact("", el("span", {class: "MISSING"}, "stopped early")));
  facts.push(fact("id", el("code", {class: "muted"}, run.id)));
  return el("div", {class: "factline"}, el("span", {class: "who"}, who), ...facts);
}
function renderResultsHead(data) {
  const run = data.run, against = data.against;
  $("resultsTitle").textContent = "Run " + run.id + " against " + againstText(against, data.againstPinned, data.againstRevision);
  const verdict = $("verdict");
  if (data.status === "ready") {
    verdict.replaceChildren(...STATUS_ORDER.filter((s) => data.counts.all && data.counts.all[s]).flatMap((s, i) =>
      [i ? " · " : "", el("span", {class: s}, data.counts.all[s] + " " + s)]));
    if (!verdict.childElementCount) verdict.textContent = "nothing judged";
  } else verdict.textContent = "";
  const lines = [runFacts("run", run), runFacts(data.againstPinned ? "baseline" : "against", against)];
  if (data.status === "ready") {
    const s = data.settings || {};
    lines.push(el("div", {class: "factline"}, el("span", {class: "who"}, ""),
      fact("tolerance", "FAIL above " + s.maxDiffPercent + "% of pixels · channel threshold " + s.channelThreshold),
      fact("compared", fmtTime(data.comparedAt) + (data.seconds !== undefined && data.seconds !== null ? " in " + data.seconds + " s" : ""))));
  }
  $("facts").replaceChildren(...lines);
  const notes = (data.notes || []).slice();
  if (data.runPartial) notes.push("Partial run (" + filterText(run) + "): only its own screens are judged.");
  if (data.runStopped) notes.push("The run stopped early: only the screens it captured are judged.");
  $("resultsNotes").replaceChildren(...notes.map((n) => el("div", {}, n)));
  $("resultsNotes").hidden = !notes.length;
  $("viewRun").href = galleryHash(run.id);
  $("viewAgainst").href = galleryHash(against.id);
  $("viewAgainst").textContent = data.againstPinned ? "View baseline" : "View the other run";
}

// The Failures section: every FAIL row as a card, grouped by feature in catalog order (the run's
// own row order: the app's registry order for a capture), a variant's light and dark next to each other. Cards
// load thumbnails only; Swipe and the viewer load the full PNGs, lazily.
const FAIL_VIEWS = ["side", "overlay", "swipe"];
let failView = FAIL_VIEWS.includes(store.get("failView", "side")) ? store.get("failView", "side") : "side";
let failRows = [];
const ovState = {mix: 100, mask: true};  // Overlay: 0 = baseline, 100 = current; the red mask on top
function failureOrder(rows) {
  const nameAt = new Map(), featureAt = new Map(), appearanceAt = {light: 0, dark: 1};
  for (const r of rows) {
    if (!nameAt.has(r.name)) nameAt.set(r.name, nameAt.size);
    if (!featureAt.has(r.feature)) featureAt.set(r.feature, featureAt.size);
  }
  const stateAt = {open: 0, rejected: 1, expected: 2, confirmed: 3};
  const stateOf = (r) => r.review ? stateAt[r.review.state] : 0;
  return rows.filter((r) => r.status === "FAIL" || (r.status === "MISSING" && r.currentUrl)).sort((a, b) =>
    stateOf(a) - stateOf(b) || featureAt.get(a.feature) - featureAt.get(b.feature) ||
    nameAt.get(a.name) - nameAt.get(b.name) || (appearanceAt[a.appearance] || 0) - (appearanceAt[b.appearance] || 0));
}
function renderFailures() {
  const data = results;
  failRows = failureOrder(data.rows);
  const section = $("failSection");
  section.hidden = false;
  section.style.setProperty("--ratio", data.imageSize ? data.imageSize[0] + " / " + data.imageSize[1] : "9 / 19.5");
  $("failTitle").textContent = "Changes" + (failRows.length ? " (" + failRows.length + ")" : "");
  $("failViewSeg").hidden = !failRows.length;
  renderReviewSummary(data);
  for (const b of $("failViewSeg").children) b.setAttribute("aria-pressed", String(b.dataset.v === failView));
  if (!failRows.length) { $("failures").replaceChildren(el("p", {class: "muted"}, "None.")); return; }
  const groups = new Map();
  for (const r of failRows) { if (!groups.has(r.feature)) groups.set(r.feature, []); groups.get(r.feature).push(r); }
  $("failures").replaceChildren(...[...groups].map(([feature, rows]) => el("section", {class: "fgroup"},
    el("h3", {}, feature + " ", el("span", {class: "muted"}, String(rows.length))),
    el("div", {class: "fcards"}, ...rows.map(failCard)))));
}
function failCard(r) {
  const confirmed = r.review && r.review.state === "confirmed";
  const accept = r.currentUrl && results && results.againstPinned && confirmed;
  return el("article", {class: "fcard", "data-key": rowKey(r), "data-review": r.review ? r.review.state : null},
    el("div", {class: "fcard-head"}, el("span", {class: "title"}, el("code", {}, r.name), " · " + r.appearance),
      el("span", {class: "muted"}, r.status === "MISSING" ? "new screen: " + r.reason : r.reason),
      accept ? el("div", {class: "buttons"}, el("button", {type: "button", class: "small primary", onclick: () => confirmAccept(r)}, "Accept as baseline…")) : ""),
    reviewStrip(r),
    el("div", {class: "fimgs"}, ...failBody(r)));
}
// MARK: Review: the author marks a change expected, a reviewer confirms or rejects it
function samePerson(a, b) {
  if (!a || !b) return false;
  if (a.email && b.email) return a.email.trim().toLowerCase() === b.email.trim().toLowerCase();
  return !!(a.name || b.name) && (a.name || "").trim().toLowerCase() === (b.name || "").trim().toLowerCase();
}
function personLabel(who) { return (who && (who.name || who.email)) || "someone"; }
function reviewText(rv) {
  if (!rv || rv.state === "open") return "open";
  if (rv.state === "expected") return "expected by " + personLabel(rv.author);
  if (rv.state === "confirmed") return "confirmed by " + personLabel(rv.reviewer);
  return "rejected by " + personLabel(rv.reviewer);
}
function reviewStrip(r) {
  const rv = r.review || {state: "open"}, me = results && results.user, isAuthor = samePerson(me, rv.author);
  const note = rv.state === "rejected" ? rv.reason : rv.note;
  const buttons = [];
  const button = (label, kind, needNote, danger) => el("button", {type: "button", class: "small" + (danger ? " danger" : ""),
    onclick: () => markReview(r, kind, needNote)}, label);
  if (me) {
    if (rv.state === "open" || rv.state === "rejected") buttons.push(button("Mark expected…", "expect", "optional"));
    if (rv.state === "expected" && isAuthor) buttons.push(button("Withdraw", "withdraw"));
    if (rv.state === "expected" && !isAuthor) buttons.push(button("Confirm", "confirm"), button("Reject…", "reject", "required", true));
    if (rv.state === "confirmed") buttons.push(button("Reject…", "reject", "required", true));
  }
  const history = (rv.events || []).map((e) => fmtTime(e.at) + " " + e.kind + " by " + personLabel(e.by) + (e.note ? ": " + e.note : "") +
    (e.ignored ? " (ignored: " + e.ignored + ")" : "")).join("\n");
  return el("div", {class: "fcard-review"},
    el("span", {class: "rv rv-" + rv.state, title: history || "no review yet"}, reviewText(rv)),
    note ? el("span", {class: "rv-note"}, "“" + note + "”") : "",
    rv.state === "expected" && isAuthor ? el("span", {class: "muted"}, "waiting for a reviewer") : "",
    !me ? el("span", {class: "muted"}, "no identity to review with: set git user.name and user.email") : "",
    buttons.length ? el("div", {class: "buttons"}, ...buttons) : "");
}
async function markReview(r, kind, needNote) {
  let note = null;
  if (needNote) {
    note = prompt(needNote === "required" ? "Why is this change rejected? (required)" : "Why is this change expected? (optional)", "");
    if (note === null) return;
    if (needNote === "required" && !note.trim()) { alert("A rejection needs a reason."); return; }
  }
  const at = route;
  try { await api("/api/review", {run: at.run, against: at.against, name: r.name, appearance: r.appearance, kind, note}); }
  catch (e) { alert(e.message); return; }
  if (route.view === "results" && route.run === at.run && route.against === at.against) fetchResults(route, ++resultsToken, false);
}
function renderReviewSummary(data) {
  const box = $("reviewSummary"), rv = data.review;
  if (!rv || !rv.changes) { box.hidden = true; return; }
  const left = rv.changes - rv.confirmed, errors = (data.counts.all && data.counts.all.ERROR) || 0;
  const parts = [rv.changes + " change" + (rv.changes === 1 ? "" : "s"), rv.open + " open", rv.expected + " expected",
    rv.confirmed + " confirmed"].concat(rv.rejected ? [rv.rejected + " rejected"] : []);
  const gate = data.againstPinned ? (left || errors ? " · the run cannot become the baseline until every change is confirmed by a reviewer" +
    (errors ? " and its " + errors + " ERROR screen" + (errors === 1 ? "" : "s") + " recaptured" : "") : " · every change is confirmed: the run can be set as baseline") : "";
  box.textContent = parts.join(" · ") + gate;
  box.className = "note" + (data.againstPinned && (left || errors) ? " blocked" : "");
  box.hidden = false;
}
function thumbFigure(r, layer) {
  const url = r[layer + "Url"];
  return el("figure", {}, url ? el("img", {src: r[layer + "ThumbUrl"] || url, alt: r.name + " " + r.appearance + " " + layer,
    loading: "lazy", decoding: "async", tabindex: "0", title: "open the " + layer + " in the viewer", "data-layer": layer})
    : el("div", {class: "none"}, "no " + layer), el("figcaption", {}, layer));
}
function failBody(r) {
  if (failView === "overlay") {
    if (!r.maskThumbUrl) return [el("p", {class: "note muted"}, "No overlay: " + (r.diffUrl ? "no mask" : "no diff (" + r.reason + ")") + "."),
      thumbFigure(r, "baseline"), thumbFigure(r, "current")];
    const lazy = {loading: "lazy", decoding: "async", alt: ""};
    const top = el("img", Object.assign({src: r.currentThumbUrl || r.currentUrl, style: "opacity: " + ovState.mix / 100}, lazy));
    const mask = el("img", Object.assign({src: r.maskThumbUrl, hidden: ovState.mask ? null : ""}, lazy));
    const stack = el("div", {class: "ovl", tabindex: "0", "data-layer": "overlay", title: "open the overlay in the viewer",
      role: "img", "aria-label": r.name + " " + r.appearance + ": current over baseline, differing pixels in red"},
      el("img", Object.assign({src: r.baselineThumbUrl || r.baselineUrl}, lazy)), top, mask);
    const range = el("input", {type: "range", min: "0", max: "100", value: String(ovState.mix), "aria-label": "baseline to current"});
    range.addEventListener("input", () => { ovState.mix = Number(range.value); top.style.opacity = ovState.mix / 100; });
    const box = el("input", {type: "checkbox"});
    box.checked = ovState.mask;
    box.addEventListener("change", () => { ovState.mask = box.checked; mask.hidden = !box.checked; });
    return [el("div", {class: "ovl-wrap"}, stack, el("div", {class: "ovl-controls"},
      el("div", {class: "mix"}, el("span", {class: "muted"}, "baseline"), range, el("span", {class: "muted"}, "current")),
      el("label", {}, box, "red: differing pixels")))];
  }
  if (failView === "swipe") {
    if (!r.baselineUrl || !r.currentUrl) return [thumbFigure(r, "baseline"), thumbFigure(r, "current")];
    // The full images, lazily: a card scrolled into view loads its two.
    const top = el("img", {class: "top", src: r.currentUrl, alt: "current", loading: "lazy", decoding: "async"});
    const bar = el("div", {class: "bar"});
    const range = el("input", {type: "range", min: "0", max: "100", value: "50", "aria-label": "swipe between baseline and current"});
    const apply = () => { top.style.clipPath = "inset(0 0 0 " + range.value + "%)"; bar.style.left = range.value + "%"; };
    range.addEventListener("input", apply); apply();
    return [el("div", {class: "swipe-wrap"}, el("div", {class: "swipe", "data-layer": "current", tabindex: "0", title: "open in the viewer"},
      el("img", {src: r.baselineUrl, alt: "baseline", loading: "lazy", decoding: "async"}), top, bar), range,
      el("div", {class: "muted", style: "font-size: 12px"}, "baseline left of the line, current right"))];
  }
  return [thumbFigure(r, "baseline"), thumbFigure(r, "current"), thumbFigure(r, "diff")];
}
function openFromCard(event) {
  const target = event.target.closest("[data-layer]"), card = event.target.closest(".fcard");
  const r = card && failRows.find((x) => rowKey(x) === card.dataset.key);
  if (target && r) location.hash = resultsShotHash(route.run, route.against, r.name, r.appearance, target.dataset.layer);
}
$("failures").addEventListener("click", openFromCard);
$("failures").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("[data-layer]")) openFromCard(e); });
for (const b of $("failViewSeg").children) b.onclick = () => {
  failView = b.dataset.v; store.set("failView", failView);
  if (results) renderFailures();
};
function resultsCtx() {
  // The viewer over the failures: each failing variant with both appearances (a passing one included,
  // so T still flips), stepping through the cards' variants in order.
  const data = results, run = route.run, against = route.against, names = new Set(failRows.map((r) => r.name)), byName = new Map();
  for (const r of data.rows) {
    if (!names.has(r.name) || (r.appearance !== "light" && r.appearance !== "dark")) continue;
    if (!byName.has(r.name)) byName.set(r.name, {name: r.name, feature: r.feature, light: null, dark: null});
    byName.get(r.name)[r.appearance] = r.currentUrl ? {status: r.status, reason: r.reason, url: r.currentUrl, thumbUrl: r.currentThumbUrl,
      baselineUrl: r.baselineUrl, baselineThumbUrl: r.baselineThumbUrl, diffUrl: r.diffUrl, diffThumbUrl: r.diffThumbUrl,
      maskUrl: r.maskUrl, maskThumbUrl: r.maskThumbUrl} : null;
  }
  const order = [];
  for (const r of failRows) { const v = byName.get(r.name); if (!order.includes(v)) order.push(v); }
  return {variants: order, order: () => order, hash: (n, a, l) => resultsShotHash(run, against, n, a, l), parent: compareHash(run, against),
    device: data.run.typeLabel, label: "Run " + run + " against " + againstText(data.against, data.againstPinned, data.againstRevision),
    reveal(name, appearance) {
      document.querySelectorAll(".fcard.last").forEach((c) => c.classList.remove("last"));
      const cards = [...document.querySelectorAll(".fcard")];
      const card = cards.find((c) => c.dataset.key === name + "|" + appearance) || cards.find((c) => c.dataset.key.startsWith(name + "|"));
      if (card) { card.classList.add("last"); card.scrollIntoView({block: "nearest"}); }
    }};
}
function openResultsLightbox(r, previous) {
  if (!results) return;
  lbCtx = resultsCtx();
  openLightbox(r.name, r.appearance, previous && previous.view === "results" && previous.run === r.run && previous.against === r.against && !previous.name, r.layer);
}
function figure(url, thumbUrl, label) {
  // The thumbnail at the figure's own size; the full PNG opens in a new tab on click.
  const img = url ? el("a", {href: url, target: "_blank", rel: "noopener", title: "open full size"},
    el("img", {src: thumbUrl || url, alt: label, loading: "lazy", decoding: "async"})) : el("div", {class: "none"}, "no " + label);
  return el("figure", {}, img, el("figcaption", {}, label));
}
function showDetail(r) {
  selectedKey = rowKey(r);
  document.querySelectorAll("tr.row").forEach((tr) => tr.classList.toggle("selected", tr.children[1].textContent === r.name && tr.children[2].textContent === r.appearance));
  const box = $("detail"); box.textContent = "";
  box.append(el("h2", {}, r.name + " · " + r.appearance + " · ", el("span", {class: r.status}, r.status)));
  if (r.reason) box.append(el("div", {class: "muted"}, r.reason));
  box.append(el("div", {class: "shots"}, figure(r.baselineUrl, r.baselineThumbUrl, "baseline"),
    figure(r.currentUrl, r.currentThumbUrl, "current"), figure(r.diffUrl, r.diffThumbUrl, "diff")));
  if (r.baselineUrl && r.currentUrl) {
    // The swipe needs the full images; they load lazily, after the figures above.
    const top = el("img", {class: "top", src: r.currentUrl, alt: "current", loading: "lazy", decoding: "async"});
    const bar = el("div", {class: "bar"});
    const range = el("input", {type: "range", min: "0", max: "100", value: "50", id: "swipeRange", "aria-label": "swipe between baseline and current"});
    const apply = () => { top.style.clipPath = "inset(0 0 0 " + range.value + "%)"; bar.style.left = range.value + "%"; };
    range.addEventListener("input", apply); apply();
    box.append(el("h2", {}, "Swipe: baseline left of the line, current right"),
      el("div", {class: "swipe-wrap"}, el("div", {class: "swipe"}, el("img", {src: r.baselineUrl, alt: "baseline", loading: "lazy", decoding: "async"}), top, bar), range));
  }
}
function confirmAccept(r) {
  const dialog = $("acceptDialog");
  const data = results;
  $("acceptWhat").textContent = r.name + " (" + r.appearance + "): " + r.reason + ". Baseline r" + data.againstRevision +
    " becomes r" + (data.againstRevision + 1) + ".";
  $("acceptDiff").src = r.diffUrl || ""; $("acceptCurrent").src = r.currentUrl;
  dialog.onclose = async () => {
    if (dialog.returnValue !== "accept") return;
    const at = route;
    try { await api("/api/accept", {run: data.run.id, against: data.against.id, name: r.name, appearance: r.appearance}); }
    catch (e) { alert(e.message); return; }
    if (route.view === "results" && route.run === at.run && route.against === at.against) computeResults(route, false);
  };
  dialog.returnValue = "";
  dialog.showModal();
}

// MARK: Gallery

let gallery = null;  // {id, run, raw, variants, against, pin, ratio}
const gstate = {appearance: "light", features: new Set(), text: "", size: store.get("thumbSize", 160), diff: store.get("galleryDiff", true)};

function hasImage(v, a) { return !!(v[a] && v[a].url); }
function shownAppearances() { return gstate.appearance === "both" ? ["light", "dark"] : [gstate.appearance]; }
function inAppearance(v) { return shownAppearances().some((a) => hasImage(v, a)); }
function filtered() {
  const text = gstate.text.trim().toLowerCase();
  return gallery.variants.filter((v) => inAppearance(v) && (!gstate.features.size || gstate.features.has(v.feature))
    && (!text || v.name.toLowerCase().includes(text)));
}
async function showGallery(r, previous) {
  const same = gallery && gallery.id === r.id && previous.view === "gallery" && previous.id === r.id;
  if (!same) {
    closeLightbox(false);
    if (!gallery || gallery.id !== r.id) $("galleryGrid").replaceChildren(el("p", {class: "muted"}, "loading…"));
    let data;
    try { data = await api("/api/run?id=" + encodeURIComponent(r.id)); }
    catch (e) { $("galleryGrid").replaceChildren(el("p", {}, "error: " + e.message)); return; }
    if (route.view !== "gallery" || route.id !== r.id) return;  // the page moved on while this loaded
    const fresh = !gallery || gallery.id !== r.id;
    gallery = {id: r.id, run: data.run, raw: data.variants, against: data.against, pin: data.pin,
      ratio: data.imageSize ? data.imageSize[0] + " / " + data.imageSize[1] : "9 / 19.5"};
    buildGalleryVariants();
    if (fresh) {
      gstate.features.clear(); gstate.text = ""; $("galleryText").value = "";
      const has = (a) => gallery.variants.some((v) => hasImage(v, a));
      gstate.appearance = has("light") ? "light" : "dark";
    }
    const run = gallery.run;
    $("galleryTitle").textContent = runTitle(run);
    const sub = [screensText(run.counts), fmtBytes(run.bytes) + " on disk",
      run.pinned ? "revision r" + run.revision : null, "recorded " + fmtTime(run.started),
      run.partial ? "filter " + filterText(run) : null, run.stopped ? "stopped early" : null,
      run.git && run.git.sha ? "git " + run.git.sha.slice(0, 9) + (run.git.dirty ? " dirty" : "") : null,
      run.app ? "app " + run.app + (run.appSha ? " " + run.appSha : "") : null].filter(Boolean);
    $("gallerySub").replaceChildren(sub.join(" · "), ...standingNodes(run), " · ", el("code", {class: "muted"}, run.id));
    renderGalleryActions();
    renderHistory(run);
    renderToolbar(); renderGrid();
    renderCrumbs(route);
  }
  if (r.name) {
    lbCtx = galleryCtx();
    openLightbox(r.name, r.appearance, previous.view === "gallery" && previous.id === r.id && !previous.name, r.layer);
  }
}
function buildGalleryVariants() {
  // The run's own screenshots; with "show diff against baseline" on, each one the current comparison
  // with the pinned baseline judged also gets its verdict and the baseline, diff and overlay layers.
  const withDiff = !!(gstate.diff && gallery.against);
  gallery.variants = gallery.raw.map((v) => {
    const out = {name: v.name, feature: v.feature, light: null, dark: null};
    for (const a of ["light", "dark"]) {
      const s = v[a];
      if (!s) continue;
      out[a] = {status: s.status, reason: s.reason, url: s.url, thumbUrl: s.thumbUrl, baselineUrl: null, baselineThumbUrl: null,
        diffUrl: null, diffThumbUrl: null, maskUrl: null, maskThumbUrl: null};
      if (withDiff && s.compare) Object.assign(out[a], s.compare);
    }
    return out;
  });
}
function renderGalleryActions() {
  const box = $("galleryActions"), run = gallery.run;
  // The baseline is what runs are compared against, so it has no Compare… of its own.
  const nodes = run.pinned ? [] : [el("button", {type: "button", disabled: run.active, onclick: () => openPicker(run.id)}, "Compare…")];
  if (gallery.against) {
    const box2 = el("input", {type: "checkbox", id: "diffToggle"});
    box2.checked = !!gstate.diff;
    box2.addEventListener("change", () => {
      gstate.diff = box2.checked; store.set("galleryDiff", gstate.diff);
      buildGalleryVariants(); renderToolbar(); renderGrid();
    });
    nodes.push(el("label", {for: "diffToggle"}, box2, "show diff against baseline ", rev(gallery.against.revision)),
      el("span", {class: "muted"}, countsText(gallery.against.counts.all, " · ")),
      el("a", {href: compareHash(run.id, gallery.against.id)}, "Open the comparison"));
  } else if (gallery.pin && !run.active) {
    nodes.push(el("a", {class: "button", href: compareHash(run.id, gallery.pin.id)}, "Compare with baseline"));
  }
  box.replaceChildren(...nodes);
}
function renderHistory(run) {
  // A pinned run is the one run that changes: updates and accepts copy screens in, so its screens can
  // come from different builds. The list says which and when, newest first, with the revision each made.
  const box = $("galleryHistory");
  const events = run.history || [];
  box.hidden = !events.length;
  if (!events.length) return;
  box.querySelector("summary").textContent = "History: " + events.length + (events.length === 1 ? " event" : " events") +
    " · newest " + fmtTime(events[0].at);
  box.querySelector("ul").replaceChildren(...events.map((e) => el("li", {},
    historyRev(e) ? el("code", {class: "rev"}, historyRev(e)) : "", historyRev(e) ? " " : "", historyText(e))));
}
function renderToolbar() {
  for (const b of $("appearanceSeg").children) {
    const a = b.dataset.a;
    b.setAttribute("aria-pressed", String(a === gstate.appearance));
    b.disabled = a !== "both" && !gallery.variants.some((v) => hasImage(v, a));
  }
  $("gallerySize").value = gstate.size;
  const byFeature = new Map();
  for (const v of gallery.variants) if (inAppearance(v)) byFeature.set(v.feature, (byFeature.get(v.feature) || 0) + 1);
  const chip = (label, pressed, n, onclick) => el("button", {type: "button", class: "chip", "aria-pressed": String(pressed), onclick},
    label, n === null ? "" : el("span", {class: "n"}, String(n)));
  $("featureChips").replaceChildren(chip("All", !gstate.features.size, null, () => { gstate.features.clear(); renderToolbar(); renderGrid(); }),
    ...[...byFeature].map(([f, n]) => chip(f, gstate.features.has(f), n, () => {
      if (gstate.features.has(f)) gstate.features.delete(f); else gstate.features.add(f);
      renderToolbar(); renderGrid();
    })));
}
function renderGrid() {
  const grid = $("galleryGrid");
  const size = gstate.size;
  grid.style.setProperty("--ratio", gallery.ratio);
  const items = filtered();
  $("galleryCount").textContent = items.length + " of " + gallery.variants.filter(inAppearance).length;
  const groups = new Map();
  for (const v of items) { if (!groups.has(v.feature)) groups.set(v.feature, []); groups.get(v.feature).push(v); }
  const sections = [];
  for (const [feature, variants] of groups) {
    sections.push(el("section", {class: "group"}, el("h3", {}, feature + " ", el("span", {class: "muted"}, String(variants.length))),
      el("div", {class: "tiles"}, ...variants.map((v) => el("figure", {class: "tile", "data-name": v.name, style: "--tile: " + tileWidth(v, size) + "px"},
        el("div", {class: "pair"}, ...shownAppearances().flatMap((a) => tileCells(v, a))),
        el("figcaption", {title: v.name}, v.name.slice(v.feature.length + 1) || v.name, ...tileStatuses(v)))))));
  }
  grid.replaceChildren(...(sections.length ? sections : [el("p", {class: "muted"}, "No screenshot matches.")]));
}
const LAYERS = ["baseline", "current", "diff"];  // a gallery tile's thumbnails
const LB_LAYERS = LAYERS.concat(["overlay"]);  // the viewer's: overlay = baseline, current at the overlay mix, the red mask on top
function hasDiff(v, a) { return !!(v[a] && v[a].diffUrl); }
function layerUrl(shot, layer, thumb) {
  if (!shot) return null;
  if (layer === "overlay") return shot.maskUrl && shot.baselineUrl ? (thumb ? shot.thumbUrl : shot.url) : null;
  if (layer === "current") return thumb ? shot.thumbUrl : shot.url;
  return thumb ? shot[layer + "ThumbUrl"] : shot[layer + "Url"];
}
function tileLayers(v, a) { return hasDiff(v, a) ? LAYERS.filter((l) => layerUrl(v[a], l)) : ["current"]; }
function tileWidth(v, size) {  // one thumbnail per image in the tile: 1 per appearance, 3 where a diff exists
  const n = shownAppearances().reduce((sum, a) => sum + (hasImage(v, a) ? tileLayers(v, a).length : 1), 0);
  return n * size + (n - 1) * 6;
}
function tileCells(v, a) {
  if (!hasImage(v, a)) return [el("div", {class: "none"}, "no " + a)];
  const layers = tileLayers(v, a);
  return layers.map((l) => {
    const img = el("img", {src: layerUrl(v[a], l, true), loading: "lazy", decoding: "async", alt: v.name + " " + a + " " + l,
      title: v.name + " · " + a + (layers.length > 1 ? " · " + l : ""), tabindex: "0", "data-name": v.name, "data-appearance": a, "data-layer": l});
    return layers.length > 1 ? el("div", {class: "cell"}, img, el("span", {class: "layer"}, l + (shownAppearances().length > 1 ? " · " + a : ""))) : img;
  });
}
function tileStatuses(v) {
  return shownAppearances().filter((a) => v[a] && !["PASS", "RECORDED"].includes(v[a].status))
    .map((a) => el("span", {class: "st " + v[a].status}, v[a].status + (shownAppearances().length > 1 ? " " + a : "")));
}
function openFromGrid(event) {
  const img = event.target.closest("img[data-name]");
  if (img) location.hash = shotHash(gallery.id, img.dataset.name, img.dataset.appearance, img.dataset.layer);
}
$("galleryGrid").addEventListener("click", openFromGrid);
$("galleryGrid").addEventListener("keydown", (e) => { if (e.key === "Enter") openFromGrid(e); });
for (const b of $("appearanceSeg").children) b.onclick = () => { gstate.appearance = b.dataset.a; renderToolbar(); renderGrid(); };
$("galleryText").oninput = () => { gstate.text = $("galleryText").value; renderGrid(); };
$("gallerySize").oninput = () => { gstate.size = Number($("gallerySize").value); store.set("thumbSize", gstate.size); renderGrid(); };

// MARK: Lightbox

const lb = {open: false, name: null, appearance: null, layer: "current", pushed: false, s: 1, fit: 1, tx: 0, ty: 0, w: 0, h: 0, keep: false};
const stage = $("lbStage"), lbBox = $("lbBox"), lbImg = $("lbImg"), lbBase = $("lbBase"), lbMask = $("lbMask");
// What the viewer steps through and where it lives in the URL: a run's gallery, or a comparison's failures.
let lbCtx = null;  // {variants, order(), hash(name, appearance, layer), parent, device, label, reveal(name, appearance)}

function galleryCtx() {
  const id = gallery.id;
  return {variants: gallery.variants, hash: (n, a, l) => shotHash(id, n, a, l), parent: galleryHash(id),
    device: gallery.run.typeLabel, label: runTitle(gallery.run),
    order() {
      const items = filtered();
      return items.some((v) => v.name === lb.name) ? items : gallery.variants.filter((v) => hasImage(v, "light") || hasImage(v, "dark"));
    },
    reveal(name) {
      const tile = [...document.querySelectorAll(".tile")].find((t) => t.dataset.name === name);
      document.querySelectorAll(".tile.last").forEach((t) => t.classList.remove("last"));
      if (tile) { tile.classList.add("last"); tile.scrollIntoView({block: "nearest"}); }
    }};
}
function pickAppearance(v, wanted) { return hasImage(v, wanted) ? wanted : hasImage(v, "light") ? "light" : "dark"; }
function openLightbox(name, appearance, pushed, layer) {
  const v = lbCtx.variants.find((x) => x.name === name);
  if (!v || !(hasImage(v, "light") || hasImage(v, "dark"))) { location.replace(lbCtx.parent); return; }
  const a = pickAppearance(v, appearance === "dark" ? "dark" : "light");
  const l = LB_LAYERS.includes(layer) && layerUrl(v[a], layer) ? layer : "current";
  if (a !== appearance || l !== (layer || "current")) { history.replaceState(null, "", lbCtx.hash(name, a, l)); route = parseHash(); }  // asked for one it lacks
  const sameVariant = lb.open && lb.name === name;
  if (!lb.open) lb.pushed = !!pushed;
  lb.keep = sameVariant;  // flipping appearance or layer keeps the zoom, for comparing them
  lb.open = true; lb.name = name; lb.appearance = a; lb.layer = l;
  $("lightbox").hidden = false; document.body.classList.add("noscroll");
  lbImg.alt = name + " " + a + " " + l;
  const overlay = l === "overlay";
  lbImg.src = layerUrl(v[a], l);  // the overlay's is the current
  if (overlay) { lbBase.src = v[a].baselineUrl; lbMask.src = v[a].maskUrl; }
  else { lbBase.removeAttribute("src"); lbMask.removeAttribute("src"); }
  applyOverlay();
  const order = lbCtx.order(), index = order.findIndex((x) => x.name === name);
  $("lbTitle").textContent = [name, a, l !== "current" || hasDiff(v, a) ? l : null, lbCtx.device].filter(Boolean).join(" · ");
  $("lbSub").textContent = (index + 1) + " of " + order.length + " · " + v[a].status + (v[a].reason ? " (" + v[a].reason + ")" : "") +
    " · " + lbCtx.label;
  $("lbLayers").hidden = !hasDiff(v, a);
  for (const b of $("lbLayers").children) {
    b.setAttribute("aria-pressed", String(b.dataset.layer === l));
    b.disabled = !layerUrl(v[a], b.dataset.layer);
  }
  const other = a === "light" ? "dark" : "light";
  $("lbToggle").textContent = other;
  $("lbToggle").hidden = !hasImage(v, other);
  $("lbPrev").disabled = index <= 0; $("lbNext").disabled = index < 0 || index >= order.length - 1;
  for (const n of [order[index - 1], order[index + 1]]) if (n) { const p = new Image(); p.src = n[pickAppearance(n, a)].url; }
  if (hasDiff(v, a)) for (const other of LAYERS) if (other !== l && layerUrl(v[a], other)) { const p = new Image(); p.src = layerUrl(v[a], other); }
  renderCrumbs(route);
}
function applyOverlay() {
  const overlay = lb.open && lb.layer === "overlay";
  lbBase.hidden = !overlay; lbMask.hidden = !overlay || !ovState.mask;
  lbImg.style.opacity = overlay ? String(ovState.mix / 100) : "";
  $("lbOverlay").hidden = !overlay;
  $("lbMix").value = String(ovState.mix); $("lbMaskOn").checked = ovState.mask;
}
$("lbMix").addEventListener("input", () => { ovState.mix = Number($("lbMix").value); applyOverlay(); });
$("lbMaskOn").addEventListener("change", () => { ovState.mask = $("lbMaskOn").checked; applyOverlay(); });
lbImg.addEventListener("load", () => {
  const sameSize = lb.w === lbImg.naturalWidth && lb.h === lbImg.naturalHeight;
  lb.w = lbImg.naturalWidth; lb.h = lbImg.naturalHeight;
  if (lb.keep && sameSize) { computeFit(); clampPan(); applyTransform(); } else fitView();
});
function closeLightbox(navigate) {
  if (!lb.open) return;
  lb.open = false;
  $("lightbox").hidden = true; document.body.classList.remove("noscroll");
  for (const img of [lbImg, lbBase, lbMask]) img.removeAttribute("src");
  applyOverlay();
  if (lbCtx) lbCtx.reveal(lb.name, lb.appearance);
  if (navigate) { if (lb.pushed) history.back(); else location.hash = lbCtx.parent; }
}
function goTo(name, appearance, layer) {
  history.replaceState(null, "", lbCtx.hash(name, appearance, layer));
  route = parseHash();
  openLightbox(name, appearance, undefined, layer);
}
function step(delta) {
  const order = lbCtx.order(), next = order[order.findIndex((v) => v.name === lb.name) + delta];
  if (next) { const a = pickAppearance(next, lb.appearance); goTo(next.name, a, layerUrl(next[a], lb.layer) ? lb.layer : "current"); }
}
function toggleAppearance() {
  const v = lbCtx.variants.find((x) => x.name === lb.name), other = lb.appearance === "light" ? "dark" : "light";
  if (v && hasImage(v, other)) goTo(v.name, other, layerUrl(v[other], lb.layer) ? lb.layer : "current");
}
function showLayer(layer) {
  const v = lbCtx.variants.find((x) => x.name === lb.name);
  if (v && layerUrl(v[lb.appearance], layer)) goTo(v.name, lb.appearance, layer);
}
function stageSize() { return {w: stage.clientWidth, h: stage.clientHeight}; }
function computeFit() { const r = stageSize(); lb.fit = Math.min(r.w / lb.w, r.h / lb.h) || 1; }
function fitView() { computeFit(); lb.s = lb.fit; clampPan(); applyTransform(); }
function clampPan() {
  const r = stageSize(), w = lb.w * lb.s, h = lb.h * lb.s;
  lb.tx = w <= r.w ? (r.w - w) / 2 : Math.min(0, Math.max(r.w - w, lb.tx));
  lb.ty = h <= r.h ? (r.h - h) / 2 : Math.min(0, Math.max(r.h - h, lb.ty));
}
function zoomAt(factor, px, py) {
  if (!lb.w) return;
  const r = stageSize();
  if (px === undefined) { px = r.w / 2; py = r.h / 2; }
  const s = Math.min(Math.max(lb.s * factor, Math.min(lb.fit, 1)), 16);
  lb.tx = px - (px - lb.tx) * s / lb.s; lb.ty = py - (py - lb.ty) * s / lb.s; lb.s = s;
  clampPan(); applyTransform();
}
function applyTransform() {
  lbBox.style.width = lb.w + "px"; lbBox.style.height = lb.h + "px";
  lbBox.style.transform = "translate(" + lb.tx + "px, " + lb.ty + "px) scale(" + lb.s + ")";
  stage.classList.toggle("pixelated", lb.s >= 2);
  stage.classList.toggle("zoomed", lb.s > lb.fit * 1.001);
  $("lbZoom").textContent = Math.round(lb.s * 100) + "%";
}
stage.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = stage.getBoundingClientRect();
  const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
  zoomAt(Math.exp(-dy * (e.ctrlKey ? 0.01 : 0.002)), e.clientX - rect.left, e.clientY - rect.top);
}, {passive: false});
let drag = null;
stage.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || lb.s <= lb.fit * 1.001) return;
  drag = {x: e.clientX, y: e.clientY, tx: lb.tx, ty: lb.ty};
  stage.setPointerCapture(e.pointerId); stage.classList.add("dragging");
});
stage.addEventListener("pointermove", (e) => {
  if (!drag) return;
  lb.tx = drag.tx + e.clientX - drag.x; lb.ty = drag.ty + e.clientY - drag.y; clampPan(); applyTransform();
});
const endDrag = () => { drag = null; stage.classList.remove("dragging"); };
stage.addEventListener("pointerup", endDrag); stage.addEventListener("pointercancel", endDrag);
stage.addEventListener("dblclick", (e) => {
  const rect = stage.getBoundingClientRect();
  if (lb.s > lb.fit * 1.01) fitView(); else zoomAt(Math.max(1, lb.fit * 2) / lb.s, e.clientX - rect.left, e.clientY - rect.top);
});
$("lbIn").onclick = () => zoomAt(1.25); $("lbOut").onclick = () => zoomAt(0.8);
$("lbFit").onclick = fitView; $("lbOne").onclick = () => zoomAt(1 / lb.s);
$("lbPrev").onclick = () => step(-1); $("lbNext").onclick = () => step(1);
$("lbToggle").onclick = toggleAppearance; $("lbClose").onclick = () => closeLightbox(true);
for (const b of $("lbLayers").children) b.onclick = () => showLayer(b.dataset.layer);
document.addEventListener("keydown", (e) => {
  if (!lb.open || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key !== "Escape" && e.target.closest && e.target.closest("#lbOverlay")) return;  // the slider's own arrow keys
  const actions = {ArrowLeft: () => step(-1), ArrowRight: () => step(1), Escape: () => closeLightbox(true),
    "+": () => zoomAt(1.25), "=": () => zoomAt(1.25), "-": () => zoomAt(0.8), "0": fitView, "1": () => zoomAt(1 / lb.s),
    t: toggleAppearance, T: toggleAppearance, b: () => showLayer("baseline"), B: () => showLayer("baseline"),
    c: () => showLayer("current"), C: () => showLayer("current"), d: () => showLayer("diff"), D: () => showLayer("diff"),
    o: () => showLayer("overlay"), O: () => showLayer("overlay")};
  if (actions[e.key]) { e.preventDefault(); actions[e.key](); }
});
window.addEventListener("resize", () => {
  if (!lb.open || !lb.w) return;
  const wasFit = Math.abs(lb.s - lb.fit) < 1e-6;
  computeFit(); if (wasFit) lb.s = lb.fit; clampPan(); applyTransform();
});

// MARK: Start

$("refreshSims").onclick = () => loadSims(route.view === "record" ? route.deviceDir : null);
$("loadMenu").onclick = loadMenu; $("btnDeleteClones").onclick = deleteClones;
$("clones").value = store.get("clones", 2);  // replaced by the host's recommendation once /api/device-types answers
$("clones").oninput = () => { if ($("clones").value !== "") store.set("clones", chosenClones()); updateDeviceNotes(); };
$("devices").addEventListener("change", (e) => { if (e.target.name === "deviceType") { store.set("deviceType", e.target.value); menu = null; updateMatch(); updateDeviceNotes(); } });
$("filter").oninput = updateMatch; $("feature").oninput = updateMatch;
$("btnRecord").onclick = startRun; $("btnStop").onclick = stopRun;
$("btnPushBaseline").onclick = () => syncPush(null); $("btnPull").onclick = () => syncPull(null);
$("syncRemote").addEventListener("toggle", () => { if ($("syncRemote").open) loadRemote(false); });
$("btnRecompute").onclick = () => { if (route.view === "results") computeResults(route, true); };
$("btnCancelCompare").onclick = cancelCompare;
render();
poll();
</script></body></html>
"""


def _interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=int(sc.setting("ui", "port", default=8899)),
                        help="port for the page, on 127.0.0.1 (default 8899)")
    parser.add_argument("--catalog-dir", type=Path, default=sc.DEFAULT_CATALOG_DIR)
    parser.add_argument("--bridge-port", type=int, default=sc.DEFAULT_PORT,
                        help=f"in-app server base port for records; simulator i uses base + i (default {sc.DEFAULT_PORT})")
    parser.add_argument("--bundle", default=sc.DEFAULT_BUNDLE)
    parser.add_argument("--exclude-file", type=Path, default=sc.DEFAULT_EXCLUDE_FILE)
    parser.add_argument("--keep-runs", type=int, default=sc.DEFAULT_KEEP_RUNS,
                        help=f"after each record, keep the newest N runs per source (ui, cli) and simulator type, "
                             f"never the pinned one; 0 keeps everything (default {sc.DEFAULT_KEEP_RUNS})")
    parser.add_argument("--max-diff-percent", type=float, default=DEFAULT_MAX_DIFF_PERCENT,
                        help=f"comparisons FAIL a screen above this %% of differing pixels (default {DEFAULT_MAX_DIFF_PERCENT})")
    parser.add_argument("--channel-threshold", type=int, default=DEFAULT_CHANNEL_THRESHOLD,
                        help=f"a channel delta above this differs (default {DEFAULT_CHANNEL_THRESHOLD})")
    parser.add_argument("--sync-prefix", default=sc.SYNC_PREFIX,
                        help=f"the key prefix in ${sc.BUCKET_ENV} that Sync pushes to and pulls from (default {sc.SYNC_PREFIX})")
    args = parser.parse_args(argv)
    if args.keep_runs < 0:
        parser.error("--keep-runs must be 0 (keep everything) or more")
    if not args.bundle:
        parser.error(f"no bundle id: set app.bundleId in {sc.CONFIG_NAME} or pass --bundle")
    app = CatalogUI(args.catalog_dir, args.bridge_port, args.bundle, args.exclude_file, keep_runs=args.keep_runs,
                    max_diff_percent=args.max_diff_percent, channel_threshold=args.channel_threshold,
                    sync_prefix=args.sync_prefix)
    server = make_server(app, args.port)
    signal.signal(signal.SIGTERM, _interrupt)  # SIGTERM cleans up as ^C does
    print(f"screen catalog UI: http://127.0.0.1:{server.server_address[1]}/  (catalog {app.catalog})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
        job = app.job
        if job is not None and job.active:
            print("stopping the active run…", flush=True)
            app.stop()
            if job.thread is not None:
                job.thread.join(60)
        if job is not None:
            for udid in job.params.get("udids", []):
                sc.simctl("terminate", udid, app.bundle, check=False)
    return 0


def cli() -> int:
    """Entry point of the `screencatalog-ui` command."""
    return main(sc.apply_config_flag(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(cli())
