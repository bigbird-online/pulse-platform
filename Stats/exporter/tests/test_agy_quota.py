"""agy quota: recorded ``agy -p /quota`` output only, never a real agy call."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from pulse_stats import agy_quota  # noqa: E402
from pulse_stats.live import LiveSnapshotService  # noqa: E402
from test_live import FakeRunner, LiveFixture, ack_result, systemd_result  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("stats_cost_refresh_agy", ROOT / "Scripts/pulse-stats-cost-refresh.py")
refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh)

# Recorded 2026-11-20 on ownermac (agy 1.5, `agy -p /quota --output-format json`).
RECORDED = {
    "conversation_id": "", "status": "SUCCESS",
    "response": "Gemini Models\tWeekly Limit Remaining\t99%\t2026-11-27T04:09:24Z\n",
    "duration_seconds": 0, "num_turns": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0},
    "command": {"name": "usage", "data": {
        "description": "Within each group, models share a weekly limit and a 5-hour limit.",
        "groups": [
            {"name": "Gemini Models", "description": "Models within this group: Gemini 4 Neon",
             "buckets": [
                 {"id": "gemini-weekly", "name": "Weekly Limit Remaining", "description": "refresh in 6 days",
                  "window": "weekly", "remaining_fraction": 0.9910842776298523, "reset_time": "2026-11-27T04:09:24Z"},
                 {"id": "gemini-5h", "name": "Five Hour Limit Remaining", "description": "refresh in 4 hours",
                  "window": "5h", "remaining_fraction": 0.9861072897911072, "reset_time": "2026-11-20T15:30:36Z"}]},
            {"name": "Claude and GPT models", "description": "Models within this group: Claude Opus 5.5",
             "buckets": [
                 {"id": "3p-weekly", "name": "Weekly Limit Remaining", "window": "weekly",
                  "remaining_fraction": 1, "reset_time": "2026-11-27T10:38:17Z"},
                 {"id": "3p-5h", "name": "Five Hour Limit Remaining", "window": "5h",
                  "remaining_fraction": 1, "reset_time": "2026-11-20T15:38:17Z"}]},
            {"name": "Gemini 4 Argon", "description": "For paid Pro plans, Argon quota is separate.",
             "buckets": [
                 {"id": "gemini-argon-wtu-weekly", "name": "Gemini 4 Argon Weekly Limit", "window": "weekly",
                  "remaining_fraction": 1, "reset_time": "2026-11-27T10:38:17Z"}]}]}},
}
NOW = datetime(2026, 11, 20, 10, 38, 24, tzinfo=timezone.utc)
COMMAND = pathlib.Path("/Users/owner/.local/bin/agy")
HOME = pathlib.Path("/Users/owner")


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


EXPECTED_GROUPS = [
    {"name": "Gemini Models", "buckets": [
        {"window": "weekly", "remaining_fraction": 0.9910842776298523, "resets_at": ts("2026-11-27T04:09:24Z")},
        {"window": "5h", "remaining_fraction": 0.9861072897911072, "resets_at": ts("2026-11-20T15:30:36Z")}]},
    {"name": "Claude and GPT models", "buckets": [
        {"window": "weekly", "remaining_fraction": 1.0, "resets_at": ts("2026-11-27T10:38:17Z")},
        {"window": "5h", "remaining_fraction": 1.0, "resets_at": ts("2026-11-20T15:38:17Z")}]},
    {"name": "Gemini 4 Argon", "buckets": [
        {"window": "weekly", "remaining_fraction": 1.0, "resets_at": ts("2026-11-27T10:38:17Z")}]},
]


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def runner(code=0, body=None, *, raw=None, calls=None):
    def run(argv, env, cwd, timeout):
        if calls is not None:
            calls.append((argv, env, cwd, timeout))
        if isinstance(code, BaseException):
            raise code
        return code, raw if raw is not None else json.dumps(RECORDED if body is None else body).encode()
    return run


def changed(mutate):
    body = copy.deepcopy(RECORDED)
    mutate(body)
    return body


def available(observed=NOW):
    return {"schema": agy_quota.SCHEMA, "source": agy_quota.SOURCE, "status": "available",
            "observed_at": observed.isoformat(), "groups": copy.deepcopy(EXPECTED_GROUPS)}


class AgyQuotaReadTests(unittest.TestCase):
    def read(self, run):
        return agy_quota.read_quota(command=COMMAND, home=HOME, runner=run, now=lambda: NOW)

    def test_recorded_output_projects_three_groups(self):
        calls = []
        document = self.read(runner(calls=calls))
        self.assertEqual(document, available())
        argv, env, cwd, timeout = calls[0]
        self.assertEqual(argv, [str(COMMAND), "-p", "/quota", "--output-format", "json"])
        self.assertEqual(env["HOME"], "/Users/owner")
        self.assertFalse(set(env) - {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME"})
        self.assertNotEqual(pathlib.Path(cwd), pathlib.Path.cwd())
        self.assertEqual(timeout, agy_quota.TIMEOUT_SECONDS)
        self.assertEqual(agy_quota.validate(document, NOW), document)

    def test_argon_group_has_only_a_weekly_window(self):
        argon = self.read(runner())["groups"][2]
        self.assertEqual(argon["name"], "Gemini 4 Argon")
        self.assertEqual([b["window"] for b in argon["buckets"]], ["weekly"])

    def test_command_failures_have_stable_codes(self):
        for code, run in (
            ("agy_command_failed", runner(1, raw=b"")),
            ("agy_command_failed", runner(body=changed(lambda b: b.update(status="ERROR")))),
            ("agy_command_unavailable", runner(agy_quota.QuotaError("agy_command_unavailable"))),
            ("agy_command_timeout", runner(agy_quota.QuotaError("agy_command_timeout"))),
            ("quota_output_too_large", runner(raw=b" " * (agy_quota.MAX_BYTES + 1))),
            ("quota_schema_changed", runner(raw=b"proxy said no")),
        ):
            with self.subTest(code), self.assertRaisesRegex(agy_quota.QuotaError, f"^{code}$"):
                self.read(run)

    def test_real_runner_reports_missing_command_and_timeout(self):
        with tempfile.TemporaryDirectory() as cwd:
            with self.assertRaisesRegex(agy_quota.QuotaError, "^agy_command_unavailable$"):
                agy_quota.subprocess_runner([cwd + "/missing-agy"], {"PATH": "/usr/bin:/bin"}, cwd, 5)
            with self.assertRaisesRegex(agy_quota.QuotaError, "^agy_command_timeout$"):
                agy_quota.subprocess_runner(["/bin/sleep", "30"], {"PATH": "/usr/bin:/bin"}, cwd, 0.2)

    def test_timeout_is_bounded_when_a_detached_descendant_holds_the_pipe(self):
        # The command starts a descendant in its own session that inherits
        # stdout, so killing the command's group leaves the pipe open.
        script = (
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
            " start_new_session=True)\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "import time; time.sleep(60)\n"
        )
        with tempfile.TemporaryDirectory() as cwd:
            pid_file = pathlib.Path(cwd) / "descendant.pid"
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(agy_quota.QuotaError, "^agy_command_timeout$"):
                    agy_quota.subprocess_runner([sys.executable, "-c", script, str(pid_file)],
                                                {"PATH": "/usr/bin:/bin"}, cwd, 1)
                self.assertLess(time.monotonic() - started, 1 + agy_quota.KILL_WAIT_SECONDS + 5)
            finally:
                if pid_file.exists() and pid_file.read_text():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except OSError:
                        pass

    def test_timeout_is_bounded_when_the_killed_process_never_exits(self):
        class Stuck:
            pid = 999999
            def __init__(self):
                self.stdout = io.BytesIO()
                self.waits = []
            def communicate(self, timeout=None):
                if timeout is None:
                    raise AssertionError("unbounded wait")
                raise subprocess.TimeoutExpired("agy", timeout)
            def wait(self, timeout=None):
                if timeout is None:
                    raise AssertionError("unbounded wait")
                self.waits.append(timeout)
                raise subprocess.TimeoutExpired("agy", timeout)
        stuck = Stuck()
        with patch.object(agy_quota.subprocess, "Popen", return_value=stuck), \
                patch.object(agy_quota.os, "killpg") as killpg:
            with self.assertRaisesRegex(agy_quota.QuotaError, "^agy_command_timeout$"):
                agy_quota.subprocess_runner(["agy"], {}, "/", 0.1)
        killpg.assert_called_once_with(stuck.pid, signal.SIGKILL)
        self.assertTrue(stuck.stdout.closed)
        self.assertEqual(stuck.waits, [agy_quota.KILL_WAIT_SECONDS])

    def test_missing_or_changed_fields_are_unavailable_not_simulated(self):
        def bucket(group, index):
            return lambda b: b["command"]["data"]["groups"][group]["buckets"][index]
        mutations = {
            "no command": lambda b: b.pop("command"),
            "no groups": lambda b: b["command"]["data"].pop("groups"),
            "empty groups": lambda b: b["command"]["data"].update(groups=[]),
            "no group name": lambda b: b["command"]["data"]["groups"][0].pop("name"),
            "no remaining": lambda b: bucket(0, 0)(b).pop("remaining_fraction"),
            "remaining as text": lambda b: bucket(0, 0)(b).update(remaining_fraction="99%"),
            "remaining over one": lambda b: bucket(0, 0)(b).update(remaining_fraction=99),
            "remaining boolean": lambda b: bucket(0, 0)(b).update(remaining_fraction=True),
            "remaining huge integer": lambda b: bucket(0, 0)(b).update(remaining_fraction=10 ** 400),
            "window as array": lambda b: bucket(0, 1)(b).update(window=["5h"]),
            "window as object": lambda b: bucket(0, 1)(b).update(window={"5h": 1}),
            "no reset": lambda b: bucket(0, 1)(b).pop("reset_time"),
            "reset without zone": lambda b: bucket(0, 1)(b).update(reset_time="2026-11-20T15:30:36"),
            "reset in the past": lambda b: bucket(0, 1)(b).update(reset_time="2026-11-19T15:30:36Z"),
            "unknown window": lambda b: bucket(0, 1)(b).update(window="daily"),
            "no window": lambda b: bucket(0, 1)(b).pop("window"),
            "no weekly": lambda b: b["command"]["data"]["groups"][0]["buckets"].pop(0),
            "duplicate window": lambda b: b["command"]["data"]["groups"][2]["buckets"].append(
                copy.deepcopy(bucket(2, 0)(b))),
        }
        for name, mutate in mutations.items():
            with self.subTest(name), self.assertRaisesRegex(agy_quota.QuotaError, "^quota_schema_changed$"):
                self.read(runner(body=changed(mutate)))

    def test_published_documents_admit_no_extra_field(self):
        for document in (dict(available(), extra=1),
                         dict(agy_quota.failure_document("agy_command_failed", NOW), groups=[]),
                         dict(agy_quota.failure_document("agy_command_failed", NOW), reason="made_up")):
            with self.assertRaises(agy_quota.QuotaError):
                agy_quota.validate(document, NOW)
        document = available()
        document["groups"][0]["buckets"][0]["used"] = 1
        with self.assertRaisesRegex(agy_quota.QuotaError, "invalid_quota_window"):
            agy_quota.validate(document, NOW)

    def test_load_quota_returns_reason_for_each_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "agy-quota-v1.json"
            self.assertEqual(agy_quota.load_quota(path, NOW), (None, "quota_source_unavailable"))
            for document, expected in (
                (available(), None),
                (agy_quota.failure_document("agy_command_timeout", NOW), "agy_command_timeout"),
            ):
                path.write_text(json.dumps(document))
                path.chmod(0o600)
                self.assertEqual(agy_quota.load_quota(path, NOW), (document, expected))
            path.write_text(json.dumps(available(observed=NOW - timedelta(seconds=agy_quota.MAX_AGE_SECONDS))))
            self.assertEqual(agy_quota.load_quota(path, NOW), (None, "quota_reading_stale"))
            path.write_text(json.dumps(available()))
            path.chmod(0o644)
            self.assertEqual(agy_quota.load_quota(path, NOW), (None, "quota_source_unavailable"))


class AgyPublicationTests(unittest.TestCase):
    """The cost-refresh cycle reads once and writes agy-quota-v1.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = pathlib.Path(self.tmp.name) / "state"
        self.state.mkdir(mode=0o700)
        self.destination = {"host": "fixture", "python": "/python", "script": "/script"}
        self.config = {"destination": self.destination,
                       "agy_quota": {"command": str(COMMAND), "home": str(HOME)}}
        patcher = patch.object(refresh, "STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        for module in (refresh, agy_quota):
            clock = patch.object(module, "datetime", FrozenDatetime)
            clock.start()
            self.addCleanup(clock.stop)
        self.target = self.state / "agy-quota-v1.json"

    def run_refresh(self, run, config=None):
        sent = []
        stderr = io.StringIO()
        with patch.object(refresh, "ssh", side_effect=lambda peer, command, data=None: sent.append((command, data)) or b""), \
             redirect_stderr(stderr):
            status = refresh.refresh_agy_quota(self.config if config is None else config, runner=run)
        return status, sent, stderr.getvalue()

    def published(self):
        return json.loads(self.target.read_bytes())

    def test_unconfigured_source_reads_nothing(self):
        def never(*args):
            raise AssertionError("no read without configuration")
        self.assertEqual(self.run_refresh(never, {"destination": self.destination})[0], "source_not_configured")
        self.assertFalse(self.target.exists())

    def test_success_is_written_privately_and_forwarded(self):
        status, sent, _ = self.run_refresh(runner())
        self.assertEqual(status, "published")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sent, [(["/python", "/script", "receive-agy-quota"], self.target.read_bytes())])
        self.assertEqual(self.published(), available())

    def test_any_failure_replaces_the_old_reading_with_reason_and_time(self):
        for code, run in (
            ("agy_command_failed", runner(2, raw=b"")),
            ("agy_command_timeout", runner(agy_quota.QuotaError("agy_command_timeout"))),
            ("quota_schema_changed", runner(body=changed(lambda b: b["command"].pop("data")))),
            ("quota_schema_changed", runner(body=changed(
                lambda b: b["command"]["data"]["groups"][0]["buckets"][1].update(window=["5h"])))),
            ("quota_schema_changed", runner(raw=json.dumps(RECORDED).replace(
                "0.9910842776298523", "1" + "0" * 400).encode())),
            ("quota_schema_changed", runner(RuntimeError("unexpected"))),
            ("agy_command_failed", runner(OSError("io"))),
        ):
            with self.subTest(code):
                self.run_refresh(runner())
                self.assertEqual(self.published()["status"], "available")
                status, sent, log = self.run_refresh(run)
                self.assertEqual(status, "unavailable")
                self.assertEqual(self.published(), {"schema": agy_quota.SCHEMA, "source": agy_quota.SOURCE,
                    "status": "unavailable", "observed_at": NOW.isoformat(), "reason": code})
                self.assertEqual(json.loads(log.splitlines()[-1]),
                                 {"schema": "stats_refresh_progress/v1", "stage": "agy_quota_read_failed",
                                  "error_code": code})
                self.assertEqual(sent[0][0], ["/python", "/script", "receive-agy-quota"])

    def test_a_configured_entry_without_usable_paths_publishes_unavailable(self):
        def never(*args):
            raise AssertionError("no read without usable paths")
        for name, entry in (("no home", {"command": str(COMMAND)}),
                            ("relative command", {"command": "agy", "home": str(HOME)}),
                            ("relative home", {"command": str(COMMAND), "home": "home"})):
            with self.subTest(name):
                self.run_refresh(runner())
                self.assertEqual(self.published()["status"], "available")
                status, sent, _ = self.run_refresh(never, {**self.config, "agy_quota": entry})
                self.assertEqual(status, "unavailable")
                self.assertEqual(self.published()["reason"], "agy_command_unavailable")
                self.assertEqual(sent[0][0], ["/python", "/script", "receive-agy-quota"])

    def test_compare_and_write_hold_one_lock_so_time_only_advances(self):
        import threading
        oldest = available(observed=NOW - timedelta(minutes=9))
        older, newer = available(observed=NOW - timedelta(minutes=5)), available()
        refresh.receive_agy_quota(json.dumps(oldest).encode())
        compared, release = threading.Event(), threading.Event()
        real_observed = agy_quota.observed_at
        calls = []
        def slow_observed(document):
            # The older writer's second look at its own time is the comparison with
            # the previous copy; it pauses there, after reading, before its write.
            if document.get("observed_at") == older["observed_at"] and threading.current_thread().name == "old":
                calls.append(1)
                if len(calls) == 2:
                    compared.set()
                    release.wait(5)
            return real_observed(document)
        with patch.object(agy_quota, "observed_at", side_effect=slow_observed):
            old = threading.Thread(name="old", target=lambda: refresh.receive_agy_quota(json.dumps(older).encode()))
            old.start()
            self.assertTrue(compared.wait(5))
            new = threading.Thread(target=lambda: refresh.receive_agy_quota(json.dumps(newer).encode()))
            new.start()
            new.join(0.3)
            # The newer write waits for the section the older one holds.
            self.assertTrue(new.is_alive())
            release.set()
            old.join(5)
            new.join(5)
        self.assertEqual(self.published(), newer)

    def test_first_receipt_creates_a_private_state_directory(self):
        fresh = pathlib.Path(self.tmp.name) / "fresh-home" / ".local/state/pulse/stats"
        with patch.object(refresh, "STATE", fresh):
            refresh.receive_agy_quota(json.dumps(available()).encode())
            self.assertEqual(fresh.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads((fresh / "agy-quota-v1.json").read_bytes()), available())
        public = pathlib.Path(self.tmp.name) / "public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with patch.object(refresh, "STATE", public), \
                self.assertRaisesRegex(ValueError, "private non-symlink destination required"):
            refresh.receive_agy_quota(json.dumps(available()).encode())
        self.assertFalse((public / "agy-quota.lock").exists())

    def test_receiver_refuses_an_older_publication(self):
        newer, older = available(), available(observed=NOW - timedelta(minutes=5))
        refresh.receive_agy_quota(json.dumps(newer).encode())
        with self.assertRaisesRegex(ValueError, "older quota publication refused"):
            refresh.receive_agy_quota(json.dumps(older).encode())
        self.assertEqual(self.published(), newer)

    def test_manual_mode_runs_one_read(self):
        config = pathlib.Path(self.tmp.name) / "manual.json"
        config.write_text(json.dumps({"agy_quota": self.config["agy_quota"]}))
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["refresh", "agy-quota", "--config", str(config)]), \
             patch.object(agy_quota, "subprocess_runner", runner()), \
             patch("sys.stdout", stdout):
            self.assertEqual(refresh.main(), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"agy_quota": "published"})


class AgyQuotaMetricTests(unittest.TestCase):
    """The snapshot reads the published fact; never runs agy."""

    @staticmethod
    def render(directory, document=None, now=NOW):
        fixture = LiveFixture(pathlib.Path(directory))
        path = fixture.root / "agy-quota-v1.json"
        if document is not None:
            path.write_text(json.dumps(document))
            path.chmod(0o600)
        fixture.paths = replace(fixture.paths, agy_quota=path)
        runner_ = FakeRunner(ack=ack_result(), systemd=systemd_result())
        rendered = fixture.service(runner_, now=now).render_json()
        return {item["id"]: item for item in json.loads(rendered)["metrics"]}

    def test_available_reading_lists_every_current_bucket(self):
        with tempfile.TemporaryDirectory() as directory:
            metric = self.render(directory, available())["relay.agy.quota"]
        self.assertEqual((metric["status"], metric["unit"], metric["observed_at"]),
                         ("available", "buckets", NOW.isoformat()))
        self.assertEqual(metric["value"], [{"group": g["name"], **b} for g in EXPECTED_GROUPS for b in g["buckets"]])
        argon = [row for row in metric["value"] if row["group"] == "Gemini 4 Argon"]
        self.assertEqual([row["window"] for row in argon], ["weekly"])

    def test_a_reset_bucket_is_dropped_not_shown_as_current(self):
        later = datetime(2026, 11, 20, 15, 35, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            metric = self.render(directory, available(observed=later - timedelta(minutes=10)), now=later)["relay.agy.quota"]
        windows = [(row["group"], row["window"]) for row in metric["value"]]
        self.assertNotIn(("Gemini Models", "5h"), windows)
        self.assertIn(("Claude and GPT models", "5h"), windows)

    def test_unavailable_states_are_unknown_with_reason(self):
        for name, document, reason, has_time in (
            ("missing", None, "quota_source_unavailable", False),
            ("stale", available(observed=NOW - timedelta(hours=2)), "quota_reading_stale", False),
            ("failed", agy_quota.failure_document("agy_command_failed", NOW), "agy_command_failed", True),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                metric = self.render(directory, document)["relay.agy.quota"]
                self.assertEqual((metric["status"], metric["value"], metric["reason"]), ("unknown", None, reason))
                self.assertEqual(metric["observed_at"] is not None, has_time)

    def test_without_a_configured_file_no_agy_metric_is_emitted(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner_ = FakeRunner(ack=ack_result(), systemd=systemd_result())
            metrics = json.loads(fixture.service(runner_, now=NOW).render_json())["metrics"]
        self.assertFalse([m["id"] for m in metrics if "agy" in m["id"]])

    def test_production_wiring_reads_the_published_agy_file(self):
        self.assertEqual(LiveSnapshotService.default().paths.agy_quota,
                         pathlib.Path.home() / ".local/state/pulse/stats/agy-quota-v1.json")


if __name__ == "__main__":
    unittest.main()
