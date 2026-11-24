"""Tests for the parts that make the tool app-agnostic: screencatalog.json discovery and precedence,
the folder storage backend, and the wire protocol of the ScreenCatalog Swift package.

Run from tool/: python3 -m unittest test_screencatalog_config
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_screencatalog as tsc  # first: it pins the config the modules load
import screencatalog as sc

TOOL = Path(__file__).resolve().parent


def run_cli(*args: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    """The CLI in a fresh process, so the config is read as a user's would be."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SCREENCATALOG_")}
    return subprocess.run([sys.executable, str(TOOL / "screencatalog.py"), *args], cwd=str(cwd), text=True,
                          capture_output=True, env={**clean, **(env or {})}, timeout=60)


class ConfigDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def write(self, folder: Path, data: dict) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / sc.CONFIG_NAME
        path.write_text(json.dumps(data))
        return path

    def test_walks_up_from_the_working_directory(self) -> None:
        config = self.write(self.root / "app", {"app": {"bundleId": "com.example.a"}})
        deep = self.root / "app" / "Sources" / "Feature"
        deep.mkdir(parents=True)
        self.assertEqual(sc.find_config(deep, environ={}), config)
        self.assertIsNone(sc.find_config(self.root, environ={}))

    def test_the_environment_variable_wins_over_the_walk(self) -> None:
        self.write(self.root / "app", {})
        other = self.write(self.root / "other", {})
        self.assertEqual(sc.find_config(self.root / "app", environ={sc.CONFIG_ENV: str(other)}), other)

    def test_setting_returns_the_default_for_missing_and_null_keys(self) -> None:
        with mock.patch.object(sc, "CONFIG", {"record": {"port": 9000, "capture": None}, "app": "not a dict"}):
            self.assertEqual(sc.setting("record", "port", default=1), 9000)
            self.assertEqual(sc.setting("record", "capture", default="app"), "app")
            self.assertEqual(sc.setting("record", "missing", default=7), 7)
            self.assertIsNone(sc.setting("app", "bundleId"))

    def test_the_test_config_is_the_one_loaded(self) -> None:
        self.assertEqual(sc.CONFIG_PATH, TOOL / "testdata" / sc.CONFIG_NAME)
        self.assertEqual(sc.DEFAULT_BUNDLE, "com.example.sample")
        self.assertEqual(sc.DEFAULT_CATALOG_DIR, TOOL / "testdata" / ".screencatalog")

    def test_paths_and_values_come_from_the_file_found_from_the_working_directory(self) -> None:
        self.write(self.root, {"app": {"bundleId": "com.example.a"}, "catalogDir": "shots",
                               "record": {"port": 9100, "minShownMs": 1234, "appearances": ["dark"]},
                               "compare": {"maxDiffPercent": 0.5}, "git": {"mainBranch": "trunk", "remote": "upstream"}})
        probe = ("import json, screencatalog as sc; a = sc.parse_args(['record']); c = sc.parse_args(['compare', 'r']);"
                 "print(json.dumps([str(a.catalog_dir), a.bundle, a.port, a.min_shown_ms, a.appearance, c.max_diff_percent,"
                 " list(sc.MAIN_REFS)]))")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("SCREENCATALOG_")}
        out = subprocess.run([sys.executable, "-c", probe], cwd=str(self.root), text=True, capture_output=True,
                             env={**clean, "PYTHONPATH": str(TOOL)}, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [str(self.root / "shots"), "com.example.a", 9100, 1234, "dark", 0.5,
                                                  ["upstream/trunk", "trunk"]])

    def test_the_config_flag_and_the_catalog_dir_variable(self) -> None:
        config = self.write(self.root / "elsewhere", {"catalogDir": "from-config"})
        elsewhere = self.root / "elsewhere"
        for args, env, expected in (
                (["--config", str(config), "list"], {}, elsewhere / "from-config"),
                (["list", f"--config={config}"], {}, elsewhere / "from-config"),
                (["--config", str(config), "list"], {sc.CATALOG_DIR_ENV: str(self.root / "from-env")}, self.root / "from-env"),
                (["--config", str(config), "list", "--catalog-dir", str(self.root / "from-flag")],
                 {sc.CATALOG_DIR_ENV: str(self.root / "from-env")}, self.root / "from-flag")):
            with self.subTest(args=args, env=env):
                out = run_cli(*args, cwd=self.root, env=env)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(json.loads(out.stdout)["catalog"], str(expected))

    def test_a_missing_or_broken_config_is_an_error(self) -> None:
        out = run_cli("--config", str(self.root / "nope.json"), "list", cwd=self.root)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no config file at", out.stderr)
        (self.root / sc.CONFIG_NAME).write_text("{not json")
        out = run_cli("list", cwd=self.root)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("is not valid JSON", out.stderr)

    def test_record_without_a_bundle_id_is_refused(self) -> None:
        self.write(self.root, {})
        out = run_cli("record", "--udid", "U1", cwd=self.root)
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("no bundle id: set app.bundleId in screencatalog.json or pass --bundle", out.stderr)

    def test_no_churn_paths_means_nothing_is_ignored(self) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
            subprocess.run(["git", "-C", str(self.root), "config", key, value], check=True)
        (self.root / "a.txt").write_text("a")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "a"], check=True)
        with mock.patch.object(sc, "BUILD_CHURN_PATHS", ()):
            self.assertNotIn("ignored", sc.git_state(self.root))
            (self.root / "a.txt").write_text("b")
            state = sc.git_state(self.root)
        self.assertEqual((state["dirty"], "ignored" in state), (True, False))


class DirectoryRemoteTests(tsc.CatalogCase):
    """The folder backend through the same push and pull the S3 backend goes through."""

    def setUp(self) -> None:
        super().setUp()
        self.store = self.root / "store"
        self.remote = sc.DirectoryRemote(self.store, tsc.QUIET)

    def test_a_run_round_trips_and_logs_stay_behind(self) -> None:
        path = self.make_run("r1", {("A.one", "light"): tsc.solid_png(10), ("A.one", "dark"): tsc.solid_png(20)})
        (path / "logs").mkdir()
        (path / "logs" / "x.stderr.log").write_text("noise")
        sc.sync_push(self.catalog, self.remote, ["r1"], include_all=False, force=False, output=tsc.QUIET)
        self.assertEqual(self.remote.run_ids(), ["r1"])
        self.assertTrue((self.store / "runs" / "r1" / "light" / "A.one.png").is_file())
        self.assertTrue((self.store / "runs" / "r1" / "run.json").is_file())
        self.assertFalse((self.store / "runs" / "r1" / "logs").exists())

        other = self.root / "other-catalog"
        other.mkdir()
        sc.sync_pull(other, self.remote, ["r1"], force=False, output=tsc.QUIET)
        for look in ("light", "dark"):
            self.assertEqual((other / "runs" / "r1" / look / "A.one.png").read_bytes(),
                             (path / look / "A.one.png").read_bytes())
        self.assertEqual(sc.load_run(other, "r1")["id"], "r1")

    def test_an_unchanged_file_is_not_copied_again(self) -> None:
        path = self.make_run("r1", {("A.one", "light"): tsc.solid_png(10)})
        lines = tsc.Lines()
        remote = sc.DirectoryRemote(self.store, lines)
        remote.push_dir(path, "runs", "r1", excludes=("run.json",))
        remote.push_dir(path, "runs", "r1", excludes=("run.json",))
        copied = [line for line in lines.lines if line.endswith("copied")]
        self.assertEqual(copied[-1], "0 files copied")
        self.assertNotEqual(copied[0], "0 files copied")

    def test_json_files_missing_and_unparsable(self) -> None:
        self.assertIsNone(self.remote.get_json("baselines.json"))
        self.assertEqual(self.remote.run_ids(), [])
        self.store.mkdir()
        (self.store / "baselines.json").write_text("{nope")
        with self.assertRaisesRegex(sc.CatalogError, "does not parse"):
            self.remote.get_json("baselines.json")

    def test_remove_deletes_only_that_run(self) -> None:
        for run_id in ("r1", "r2"):
            self.make_run(run_id, {("A.one", "light"): tsc.solid_png(10)})
            sc.sync_push(self.catalog, self.remote, [run_id], include_all=False, force=False, output=tsc.QUIET)
        self.remote.remove("runs", "r1")
        self.assertEqual(self.remote.run_ids(), ["r2"])

    def test_delete_messages_name_the_shared_folder(self) -> None:
        self.make_run("r1", {("A.one", "light"): tsc.solid_png(10)})
        sc.set_baseline(self.catalog, "r1", check_main=False, output=tsc.QUIET)
        sc.sync_push(self.catalog, self.remote, [], include_all=False, force=False, output=tsc.QUIET)
        with self.assertRaisesRegex(sc.CatalogError, "r1 is the shared folder's baseline of"):
            sc.sync_delete(self.catalog, self.remote, ["r1"], output=tsc.QUIET)
        self.make_run("r2", {("A.one", "light"): tsc.solid_png(10)})
        sc.sync_push(self.catalog, self.remote, ["r2"], include_all=False, force=False, output=tsc.QUIET)
        lines = tsc.Lines()
        sc.sync_delete(self.catalog, self.remote, ["r2"], output=lines)
        self.assertEqual(lines.lines[-1], "deleted 1 run from the shared folder; a Mac that pulled one keeps its copy")
        with mock.patch.object(sc, "STORAGE_TYPE", "directory"):
            self.assertEqual(sc.store_noun(), "the shared folder")
        self.assertEqual(sc.store_noun(), "the bucket")

    def test_make_remote_follows_the_configured_storage(self) -> None:
        with mock.patch.object(sc, "STORAGE_TYPE", "directory"), \
                mock.patch.object(sc, "CONFIG", {"storage": {"type": "directory", "path": str(self.store)}}):
            self.assertTrue(sc.sync_configured())
            self.assertTrue(sc.sync_available())
            remote = sc.make_remote(None, None, tsc.QUIET)
            self.assertIsInstance(remote, sc.DirectoryRemote)
            self.assertEqual(remote.root, self.store)
        with mock.patch.object(sc, "STORAGE_TYPE", "directory"), mock.patch.object(sc, "CONFIG", {"storage": {}}):
            self.assertFalse(sc.sync_configured())
            with self.assertRaisesRegex(sc.CatalogError, "no storage folder"):
                sc.make_remote(None, None, tsc.QUIET)

    def test_a_bucket_in_the_config_is_used_when_the_environment_has_none(self) -> None:
        with mock.patch.object(sc, "CONFIG", {"storage": {"bucket": "from-config"}}), \
                mock.patch.dict(os.environ, {sc.BUCKET_ENV: ""}):
            self.assertEqual(sc.configured_bucket(), "from-config")
        with mock.patch.object(sc, "CONFIG", {"storage": {"bucket": "from-config"}}), \
                mock.patch.dict(os.environ, {sc.BUCKET_ENV: "from-env"}):
            self.assertEqual(sc.configured_bucket(), "from-env")


class FakeServer:
    """Answers `try_get` / `get` from a script of {path prefix: [responses…]}; the last response repeats."""

    base = "http://127.0.0.1:8766"

    def __init__(self, script: dict):
        self.script = {key: list(values) for key, values in script.items()}
        self.calls: list[str] = []

    def try_get(self, path: str):
        self.calls.append(path)
        for prefix, responses in self.script.items():
            if path.startswith(prefix):
                return responses.pop(0) if len(responses) > 1 else responses[0]
        return None

    def get(self, path: str, timeout: float = 1.5):
        return self.try_get(path)


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.launch = sc.Launch(42, Path(tmp.name, "x.log"))
        patcher = mock.patch.object(sc, "process_alive", lambda pid: True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_status_answers_health_for_this_process_only(self) -> None:
        self.assertIsNone(sc.wait_for_health(FakeServer({"/status": [{"protocol": 1, "pid": 42}]}), self.launch, 1))
        problem = sc.wait_for_health(FakeServer({"/status": [{"protocol": 1, "pid": 7}]}), self.launch, 0.3)
        self.assertIn("did not answer", problem)

    def test_a_different_protocol_version_is_refused_with_a_reason(self) -> None:
        problem = sc.wait_for_health(FakeServer({"/status": [{"protocol": 2, "pid": 42}]}), self.launch, 1)
        self.assertIn("protocol 2, this tool speaks 1", problem)

    def test_shown_is_reported_by_status(self) -> None:
        server = FakeServer({"/status": [{"shown": False}, {"shown": False}, {"shown": True}]})
        self.assertIsNotNone(sc.wait_for_shown(server, 2000, 42))
        self.assertIsNone(sc.wait_for_shown(FakeServer({"/status": [{"shown": False}]}), 200, 42))

    def test_settle_waits_for_a_quiet_sequence(self) -> None:
        server = FakeServer({"/activity": [{"seq": 3, "busy": 0, "entries": []}]})
        settled, _ms, problem = sc.wait_for_settle(server, 200, 2000, 42)
        self.assertEqual((settled, problem), (True, None))
        self.assertEqual(server.calls[0], "/activity?since=0")
        self.assertTrue(all(call == "/activity?since=3" for call in server.calls[1:]), server.calls)

    def test_settle_does_not_pass_while_work_is_in_flight(self) -> None:
        server = FakeServer({"/activity": [{"seq": 1, "busy": 1, "entries": []}]})
        settled, _ms, problem = sc.wait_for_settle(server, 200, 600, 42)
        self.assertEqual((settled, problem), (False, None))

    def test_a_zero_quiet_period_still_waits_for_work_in_flight(self) -> None:
        in_flight = [{"seq": 1, "busy": 1}, {"seq": 1, "busy": 1}, {"seq": 1, "busy": 0}]
        server = FakeServer({"/activity": in_flight})
        settled, _ms, problem = sc.wait_for_settle(server, 0, 3000, 42)
        self.assertEqual((settled, problem), (True, None))
        self.assertEqual(len(server.calls), 3)  # not settled until a poll saw nothing in flight
        self.assertEqual(sc.wait_for_settle(FakeServer({"/activity": [{"seq": 1, "busy": 1}]}), 0, 400, 42)[0], False)
        self.assertEqual(sc.wait_for_settle(FakeServer({"/activity": [{"seq": 1, "busy": 0}]}), 0, 400, 42)[0], True)

    def test_a_launch_that_reports_no_process_id_fails(self) -> None:
        outputs = iter(["", "com.example.app: 4242", "", "launched"])

        def simctl(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, stdout=next(outputs), stderr="")

        with mock.patch.object(sc, "simctl", simctl):
            log_dir = self.launch.stderr_path.parent
            self.assertEqual(sc.launch("UDID", "com.example.app", [], log_dir, "ok").pid, 4242)
            with self.assertRaisesRegex(RuntimeError, "no process id"):
                sc.launch("UDID", "com.example.app", [], log_dir, "bad")

    def test_health_is_not_answered_by_a_process_the_launch_cannot_name(self) -> None:
        unnamed = sc.Launch(None, self.launch.stderr_path)
        self.assertIn("did not answer", sc.wait_for_health(FakeServer({"/status": [{"protocol": 1, "pid": 7}]}), unnamed, 0.3))

    def test_settle_restarts_the_quiet_period_on_new_activity(self) -> None:
        busy_then_quiet = [{"seq": 1, "busy": 0}] + [{"seq": n, "busy": 0} for n in (2, 3, 4)] + [{"seq": 4, "busy": 0}]
        settled, ms, _problem = sc.wait_for_settle(FakeServer({"/activity": busy_then_quiet}), 250, 3000, 42)
        self.assertTrue(settled)
        self.assertGreaterEqual(ms, 200)  # three polls saw new activity before the quiet period began

    def test_variants_are_listed_from_a_list_launch(self) -> None:
        launches = []

        def launch(udid, bundle, arguments, log_dir, tag):
            launches.append((arguments, tag))
            return self.launch

        server = FakeServer({"/status": [{"protocol": 1, "pid": 42}],
                             "/variants": [{"protocol": 1, "variants": [{"name": "Home.default", "feature": "Home"},
                                                                         {"name": "Home.empty"}]}]})
        with mock.patch.object(sc, "launch", launch), mock.patch.object(sc, "simctl", lambda *a, **k: None):
            variants = sc.enumerate_variants("U1", "com.example.sample", server, Path("/tmp"), 1)
        self.assertEqual(launches, [(["-ScreenCatalogList", "-ScreenCatalogPort", "8766"], "_list")])
        self.assertEqual(variants, [{"name": "Home.default", "feature": "Home"}, {"name": "Home.empty", "feature": "Home"}])


if __name__ == "__main__":
    unittest.main()
