"""Tests for the v2-A metric layer (spec pulse-stats-menubar-v2 §3, §7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

from pulse_stats import usage_sources
from pulse_stats.live_contract import _metric


NOW = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)


class RelayHttpAuthTests(unittest.TestCase):
    def test_fetch_json_uses_internal_token_from_relay_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = pathlib.Path(td) / "relay.env"
            env_path.write_text("RELAY_INTERNAL_TOKEN=stats-secret\n", encoding="utf-8")
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.return_value = b'{"status":"ok"}'

            with mock.patch.object(
                usage_sources.urllib.request,
                "urlopen",
                return_value=response,
            ) as urlopen:
                payload = usage_sources.fetch_json(
                    "http://127.0.0.1:8787/internal/health",
                    relay_env_path=env_path,
                )

        self.assertEqual(payload, {"status": "ok"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-relay-token"), "stats-secret")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 2)

    def test_internal_token_parser_handles_quotes_without_exposing_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = pathlib.Path(td) / "relay.env"
            env_path.write_text(
                "OTHER=1\nRELAY_INTERNAL_TOKEN='quoted-secret'\n",
                encoding="utf-8",
            )
            self.assertEqual(
                usage_sources.read_relay_internal_token(env_path),
                "quoted-secret",
            )


def _quota_line(timestamp: str, used_percent: float, resets_at: int = 1785292489) -> str:
    return json.dumps({
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {"used_percent": used_percent, "window_minutes": 10080, "resets_at": resets_at},
                "plan_type": "plus",
            },
        },
    })


def _spent_quota_line(timestamp: str) -> str:
    """`token_count` as it arrives once the allowance is gone.

    Copied from a real 2026-07-31 session log: no usage is reported because the
    server refused to run the turn, and `limit_id` has flipped to the empty
    `premium` bucket. On its own this line is indistinguishable from "unknown".
    """
    return json.dumps({
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": None,
            "rate_limits": {
                "limit_id": "premium",
                "limit_name": None,
                "primary": None,
                "secondary": None,
                "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                "individual_limit": None,
                "plan_type": None,
                "rate_limit_reached_type": None,
            },
        },
    })


def _exhausted_line(timestamp: str) -> str:
    """The adjacent `task_complete` that says why -- also from 2026-07-31."""
    return json.dumps({
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": "019fb5ca-bfbc-7b80-9f1a-d2b7414ca4e0",
            "last_agent_message": None,
            "error": {
                "message": (
                    "You've hit your usage limit. Upgrade to Pro "
                    "(https://chatgpt.com/explore/pro), visit "
                    "https://chatgpt.com/codex/settings/usage to purchase more "
                    "credits or try again at Aug 5th, 2026 4:10 AM."
                ),
                "codex_error_info": "usage_limit_exceeded",
            },
        },
    })


class CodexExhaustionTests(unittest.TestCase):
    """The 2026-07-31 finding: a spent allowance reads as 100%, not as unknown.

    When the weekly quota runs out the server stops executing the turn, so
    `token_count` carries no usage at all. Reading only that payload, the
    exporter published "unknown" while the true state -- completely spent, back
    on Aug 5 -- sat one line further down the same file in `task_complete`.
    Showing "unknown" for the one state the owner most needs to act on is the
    failure these tests exist to prevent.
    """

    def test_a_refused_turn_reads_as_one_hundred_percent(self) -> None:
        quota = usage_sources.parse_codex_remote(
            _spent_quota_line("2026-07-31T01:29:52.157Z") + "\n"
            + _exhausted_line("2026-07-31T01:29:52.200Z")
        )
        self.assertIsNotNone(quota, "an exhausted allowance must not read as unknown")
        self.assertEqual(quota["used_percent"], 100.0)
        self.assertTrue(quota["exhausted"])

    def test_the_empty_payload_alone_is_still_unknown(self) -> None:
        """Without the `task_complete` line there is genuinely nothing to report.

        Guards the obvious over-correction: inferring exhaustion from a null
        `primary` would turn every malformed or truncated record into a
        confident 100%.
        """
        self.assertIsNone(
            usage_sources.parse_codex_remote(_spent_quota_line("2026-07-31T01:29:52.157Z"))
        )

    def test_an_unrelated_task_error_is_not_exhaustion(self) -> None:
        line = json.dumps({
            "timestamp": "2026-07-31T01:29:52.200Z",
            "payload": {
                "type": "task_complete",
                "error": {"message": "network unreachable", "codex_error_info": "io_error"},
            },
        })
        self.assertIsNone(usage_sources.parse_codex_remote(line))

    def test_reset_time_carries_over_from_the_last_good_reading(self) -> None:
        """100% is only half the fact; the owner acts on when it lifts.

        A refusal carries no `resets_at` of its own, so it has to borrow the
        one from the last successful reading in the same weekly window.
        """
        quota = usage_sources.parse_codex_remote(
            _quota_line("2026-07-30T08:26:26.332Z", 95.0, resets_at=1785903014) + "\n"
            + _spent_quota_line("2026-07-31T01:29:52.157Z") + "\n"
            + _exhausted_line("2026-07-31T01:29:52.200Z")
        )
        self.assertEqual(quota["used_percent"], 100.0)
        self.assertEqual(quota["resets_at"], 1785903014)
        self.assertEqual(quota["plan_type"], "plus")

    def test_reset_time_crosses_between_machines(self) -> None:
        """The allowance is one account-wide window, so either box may hold it.

        Production on 2026-07-31 looked exactly like this: the newest record was
        this box's refusal (no reset time) while the figure lived in the peer's
        last good reading.
        """
        local = usage_sources.parse_codex_remote(
            _spent_quota_line("2026-07-31T02:00:40.829Z") + "\n"
            + _exhausted_line("2026-07-31T02:00:40.900Z")
        )
        remote = usage_sources.parse_codex_remote(
            _quota_line("2026-07-30T08:45:02.724Z", 49.0, resets_at=1785903014)
        )
        merged = usage_sources.merge_codex(local, remote)
        self.assertEqual(merged["used_percent"], 100.0)
        self.assertEqual(merged["resets_at"], 1785903014)

    def test_a_later_good_reading_supersedes_the_exhaustion(self) -> None:
        """Once the window resets, normal readings must win again.

        This is what makes the exhaustion path self-retiring rather than a
        sticky 100% that outlives the outage.
        """
        spent = usage_sources.parse_codex_remote(
            _spent_quota_line("2026-07-31T01:29:52.157Z") + "\n"
            + _exhausted_line("2026-07-31T01:29:52.200Z")
        )
        recovered = usage_sources.parse_codex_remote(
            _quota_line("2026-08-05T05:00:00.000Z", 3.0)
        )
        merged = usage_sources.merge_codex(spent, recovered)
        self.assertEqual(merged["used_percent"], 3.0)
        self.assertFalse(merged.get("exhausted", False))

    def test_local_scan_reads_exhaustion_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            nested = root / "2026" / "07" / "31"
            nested.mkdir(parents=True)
            nested.joinpath("rollout.jsonl").write_text(
                _spent_quota_line("2026-07-31T01:29:52.157Z") + "\n"
                + _exhausted_line("2026-07-31T01:29:52.200Z") + "\n",
                encoding="utf-8",
            )
            quota = usage_sources.collect_codex_local(root)
        self.assertEqual(quota["used_percent"], 100.0)
        self.assertTrue(quota["exhausted"])

    def test_exhaustion_is_fresh_enough_to_publish(self) -> None:
        """The point of reading the refusal is that it is stamped *now*.

        The old behaviour fell back to the last good reading, which aged past
        the staleness gate and blanked the panel. A refusal minted this minute
        must not.
        """
        from pulse_stats.live_contract import CODEX_QUOTA_MAX_AGE_SECONDS

        now = datetime(2026, 7, 31, 2, 30, tzinfo=timezone.utc)
        quota = usage_sources.parse_codex_remote(
            _spent_quota_line("2026-07-31T02:00:40.829Z") + "\n"
            + _exhausted_line("2026-07-31T02:00:40.900Z")
        )
        age = (now - quota["observed_at"]).total_seconds()
        self.assertLess(age, CODEX_QUOTA_MAX_AGE_SECONDS)


class MetricSemanticsTests(unittest.TestCase):
    def test_absent_value_is_unknown_and_zero_stays_available(self) -> None:
        """A missing reading must never be indistinguishable from a real zero."""
        absent = _metric("x", "c", value=None, unit="count", observed_at=None)
        real_zero = _metric("x", "c", value=0, unit="count", observed_at=NOW)
        self.assertEqual(absent["status"], "unknown")
        self.assertIsNone(absent["value"])
        self.assertEqual(real_zero["status"], "available")
        self.assertEqual(real_zero["value"], 0)


class CCStatusTests(unittest.TestCase):
    def test_live_counts_shape_is_normalised(self) -> None:
        payload = {"ok": True, "total": 2, "counts": {"starting": 0, "ready": 0, "busy": 2, "idle": 0, "dead": 0}}
        self.assertEqual(
            usage_sources.collect_cc_status(payload),
            {"processes": 2, "busy": 2, "idle": 0},
        )

    def test_unavailable_endpoint_yields_all_unknown_not_zero(self) -> None:
        self.assertEqual(
            usage_sources.collect_cc_status(None),
            {"processes": None, "busy": None, "idle": None},
        )

    def test_health_reads_bots_and_uptime(self) -> None:
        self.assertEqual(
            usage_sources.collect_health({"status": "ok", "uptime": 6872, "feishuBots": 15}),
            {"bots_online": 15, "uptime_seconds": 6872},
        )


class CCRateLimitTests(unittest.TestCase):
    def test_recent_429_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            project = root / "proj"
            project.mkdir()
            recent = (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            project.joinpath("a.jsonl").write_text(
                json.dumps({"timestamp": recent, "apiErrorStatus": 429, "error": "rate_limit"}) + "\n",
                encoding="utf-8",
            )
            self.assertIs(usage_sources.detect_cc_rate_limited(root, NOW), True)

    def test_old_429_in_recently_touched_file_is_not_counted(self) -> None:
        """Guards the real 2026-07-25 case: files appended to today still held
        429 records from 06-11, which must not raise a red light."""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            project = root / "proj"
            project.mkdir()
            path = project / "a.jsonl"
            path.write_text(
                json.dumps({"timestamp": "2026-06-11T07:02:00.025Z", "apiErrorStatus": 429, "error": "rate_limit"})
                + "\n",
                encoding="utf-8",
            )
            # File mtime is now, but the record inside is six weeks old.
            self.assertIs(usage_sources.detect_cc_rate_limited(root, NOW), False)

    def test_missing_directory_is_unknown_not_false(self) -> None:
        self.assertIsNone(usage_sources.detect_cc_rate_limited(pathlib.Path("/nonexistent-cc-dir"), NOW))


class CodexQuotaTests(unittest.TestCase):
    def test_missing_session_files_yield_unknown_never_zero_percent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(usage_sources.collect_codex_local(pathlib.Path(directory)))
        self.assertIsNone(usage_sources.collect_codex_local(pathlib.Path("/nonexistent-codex")))
        self.assertIsNone(usage_sources.merge_codex(None, None))

    def test_newest_record_wins_across_both_machines(self) -> None:
        older = usage_sources.parse_codex_remote(_quota_line("2026-07-20T13:22:20.884Z", 28.0))
        newer = usage_sources.parse_codex_remote(_quota_line("2026-07-25T04:11:12.700Z", 98.0))
        self.assertEqual(usage_sources.merge_codex(older, newer)["used_percent"], 98.0)
        self.assertEqual(usage_sources.merge_codex(newer, older)["used_percent"], 98.0)

    def test_one_machine_available_is_still_usable(self) -> None:
        only = usage_sources.parse_codex_remote(_quota_line("2026-07-25T04:11:12.700Z", 98.0))
        merged = usage_sources.merge_codex(None, only)
        self.assertEqual(merged["used_percent"], 98.0)
        self.assertEqual(merged["plan_type"], "plus")
        self.assertEqual(merged["resets_at"], 1785292489)

    def test_local_scan_picks_newest_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            nested = root / "2026" / "07"
            nested.mkdir(parents=True)
            nested.joinpath("a.jsonl").write_text(
                _quota_line("2026-07-20T13:22:20.884Z", 28.0) + "\n"
                + _quota_line("2026-07-21T13:22:20.884Z", 44.0) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(usage_sources.collect_codex_local(root)["used_percent"], 44.0)

    def test_garbage_remote_output_is_ignored(self) -> None:
        self.assertIsNone(usage_sources.parse_codex_remote("not json at all\n{token_count broken\n"))


class CodexProbeTests(unittest.TestCase):
    """The 2026-07-28 finding: Codex only writes quota when it does work.

    With both read-only routes dead (ownermac has no `codex` binary and its
    newest session file is from March; the local directory had stopped at
    07-20) the exporter published an 8-day-old figure as current. The probe
    exists to mint a reading rather than wait for one.
    """

    def _dir_with(self, root: pathlib.Path, timestamp: str, used: float) -> None:
        nested = root / "2026" / "07"
        nested.mkdir(parents=True, exist_ok=True)
        nested.joinpath(f"{timestamp.replace(':', '-')}.jsonl").write_text(
            _quota_line(timestamp, used) + "\n", encoding="utf-8"
        )

    def test_probe_is_due_when_nothing_has_ever_been_observed(self) -> None:
        self.assertTrue(usage_sources.codex_probe_is_due(None, NOW))

    def test_probe_is_due_once_the_reading_passes_the_interval(self) -> None:
        stale = {"observed_at": NOW - timedelta(hours=7)}
        fresh = {"observed_at": NOW - timedelta(hours=1)}
        self.assertTrue(usage_sources.codex_probe_is_due(stale, NOW))
        self.assertFalse(usage_sources.codex_probe_is_due(fresh, NOW))

    def test_a_future_timestamp_does_not_suppress_the_probe_forever(self) -> None:
        skewed = {"observed_at": NOW + timedelta(days=2)}
        self.assertTrue(usage_sources.codex_probe_is_due(skewed, NOW))

    def test_a_fresh_reading_is_returned_without_spending_a_turn(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._dir_with(root, "2026-07-25T07:30:00.000Z", 12.0)
            quota = usage_sources.collect_codex(
                root, NOW, probe=lambda: calls.append(1) or True
            )
        self.assertEqual(quota["used_percent"], 12.0)
        self.assertEqual(calls, [], "a same-hour reading must not cost quota")

    def test_a_stale_reading_triggers_a_probe_and_the_new_record_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            # The exact shape of the bug: last seen 8 days ago, at 28%.
            self._dir_with(root, "2026-07-17T13:22:20.884Z", 28.0)

            def probe() -> bool:
                # A real turn lands in a brand new session file, which is why
                # collect_codex has to re-read after probing rather than reuse
                # the scan it already did.
                self._dir_with(root, "2026-07-25T07:59:00.000Z", 6.0)
                return True

            quota = usage_sources.collect_codex(root, NOW, probe=probe)
        self.assertEqual(quota["used_percent"], 6.0)

    def test_a_failed_probe_keeps_the_older_reading_rather_than_losing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._dir_with(root, "2026-07-17T13:22:20.884Z", 28.0)
            quota = usage_sources.collect_codex(
                root, NOW, probe=lambda: False
            )
        # Still stale, and the freshness gate will withhold it downstream --
        # but degrading to None here would discard evidence we actually have.
        self.assertEqual(quota["used_percent"], 28.0)

    def test_a_throwing_probe_never_breaks_collection(self) -> None:
        def boom() -> bool:
            raise RuntimeError("codex exploded")

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._dir_with(root, "2026-07-17T13:22:20.884Z", 28.0)
            quota = usage_sources.collect_codex(root, NOW, probe=boom)
        self.assertEqual(quota["used_percent"], 28.0)

    def test_no_record_and_a_failing_probe_is_unknown_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            quota = usage_sources.collect_codex(
                pathlib.Path(directory), NOW, probe=lambda: False
            )
        self.assertIsNone(quota)

    def test_probe_runner_reports_failure_instead_of_raising(self) -> None:
        """Three outcomes, three distinct causes -- named by the interpreter.

        The previous form used `/bin/true` and `/bin/false`, neither of which
        exists on macOS. The non-zero-exit case therefore passed for the wrong
        reason (a missing binary, not a failing command), and the clean-exit
        case simply failed. `sys.executable` is the one command guaranteed to
        be present and to control its own exit code on every platform we run.
        """
        nonzero = (sys.executable, "-c", "raise SystemExit(3)")
        clean = (sys.executable, "-c", "raise SystemExit(0)")

        self.assertFalse(usage_sources.run_codex_probe(nonzero, 30))
        self.assertFalse(usage_sources.run_codex_probe(("/nonexistent/binary",), 30))
        self.assertTrue(usage_sources.run_codex_probe(clean, 30))

    def test_codex_probe_none_is_a_direct_noop(self) -> None:
        with mock.patch("pulse_stats.usage_sources.subprocess.run") as run:
            self.assertFalse(usage_sources.run_codex_probe(None, 30))
        run.assert_not_called()

    def test_the_codex_probe_never_runs_by_default(self) -> None:
        """Opting in is production's job; a forgetful test must cost nothing.

        Wiring the probe in without this default billed 118 live Codex turns
        from a single `pytest` run on 2026-07-28. The guarantee under test is
        that constructing a collector without mentioning the probe cannot
        execute anything.
        """
        from pulse_stats.live_contract import LivePaths
        from pulse_stats.live_sources import LiveCollector

        collector = LiveCollector(
            paths=LivePaths(),
            runner=object(),
            now=NOW,
        )
        self.assertFalse(collector.codex_probe(), "the default probe must be inert")


class TicketClosureRecoveryTests(unittest.TestCase):
    """A closed ticket must stop demanding action.

    The 07-27 fix added a "the ticket answers for itself" recovery route but
    put it behind the same `> last_seen` comparison as the liveness routes, so
    on handoffs it could never fire: wrap_missing and ledger findings are
    emitted *during* closeout, so the complaint always postdates the closure.
    Observed 07-28 in production: ho_20260727_294 closed 07:48, alert last seen
    08:43, still counted in "5 件要你出手".
    """

    def _group(self, subject: str, last_seen: datetime, label: str = "autonomous_wrap_missing"):
        from pulse_stats.live_sources import _AlertGroup

        return _AlertGroup(
            match_label=label,
            subject=subject,
            capability_id="notify.action_package",
            title="Autonomous closeout evidence is missing",
            severity="warning",
            first_seen=last_seen - timedelta(hours=1),
            last_seen=last_seen,
            message="autonomous session wrap_missing",
            occurrence_count=1,
        )

    def test_a_closure_clears_the_alert_even_though_it_predates_the_complaint(self) -> None:
        # The exact production numbers.
        closed = datetime(2026, 7, 27, 7, 48, 52, tzinfo=timezone.utc)
        last_seen = datetime(2026, 7, 27, 8, 43, 33, tzinfo=timezone.utc)
        group = self._group("ho_20260727_294", last_seen)

        recovered = group.recovered_at(None, None, None, {"ho_20260727_294": closed})
        self.assertEqual(recovered, closed, "a terminal state is a state, not a heartbeat")
        self.assertEqual(
            group.tier(last_seen + timedelta(hours=1), None, None, None,
                       {"ho_20260727_294": closed}),
            "watch",
        )

    def test_an_open_ticket_is_not_treated_as_recovered(self) -> None:
        last_seen = datetime(2026, 7, 27, 8, 43, 33, tzinfo=timezone.utc)
        group = self._group("ho_20260727_999", last_seen)
        # Not in the closure map at all — nothing proves it is done.
        self.assertIsNone(group.recovered_at(None, None, None, {"ho_other": last_seen}))
        # `watch`, not `act`: wrap_missing 是台账没记全,不是要大鸟裁决的事
        # （大鸟 2026-07-28 重定 act 语义）。本用例守的是"没有闭环证据就不许
        # 算恢复",那部分不受语义变更影响。
        self.assertEqual(
            group.tier(last_seen + timedelta(hours=1), None, None, None, {}),
            "watch",
        )

    def test_liveness_evidence_still_has_to_postdate_the_complaint(self) -> None:
        """The fix must not loosen the other three routes.

        A heartbeat from before the failure proves nothing about now, so it
        must still be rejected — only *closure* is exempt from the comparison.
        """
        last_seen = datetime(2026, 7, 27, 8, 43, 33, tzinfo=timezone.utc)
        group = self._group("pulse", last_seen, label="degen_critical")
        stale_beat = {"vultr-degen-probe": last_seen - timedelta(hours=2)}
        self.assertIsNone(group.recovered_at(stale_beat))

    def test_the_later_of_closure_and_liveness_wins(self) -> None:
        last_seen = datetime(2026, 7, 27, 8, 43, 33, tzinfo=timezone.utc)
        closed = last_seen - timedelta(hours=1)
        fresh_beat = last_seen + timedelta(hours=3)
        # `ack_probe_down` is the label actually wired to this heartbeat source
        # (RECOVERY_HEARTBEAT_SOURCES); `ack_overdue` has no heartbeat route.
        group = self._group("wi_20260713_043", last_seen, label="ack_probe_down")
        recovered = group.recovered_at(
            {"vultr-ack-probe": fresh_beat}, None, None, {"wi_20260713_043": closed}
        )
        self.assertEqual(recovered, fresh_beat)


class MetricFreshnessTests(unittest.TestCase):
    """A value that was true once must stop being presented as true now.

    Before 2026-07-28 `status` was decided only by `value is None`, so the
    07-20 Codex figure shipped as `available`/`validated` for eight days and
    the menu bar rendered it exactly like a live reading.
    """

    def test_a_reading_inside_the_limit_is_untouched(self) -> None:
        metric = _metric(
            "relay.codex.used_percent", "c", value=28.0, unit="percent",
            observed_at=NOW - timedelta(hours=2), max_age_seconds=12 * 3600, now=NOW,
        )
        self.assertEqual(metric["status"], "available")
        self.assertEqual(metric["value"], 28.0)
        self.assertIsNone(metric["reason"])

    def test_an_overage_reading_becomes_unknown_and_withholds_its_value(self) -> None:
        metric = _metric(
            "relay.codex.used_percent", "c", value=28.0, unit="percent",
            observed_at=NOW - timedelta(days=8), max_age_seconds=12 * 3600, now=NOW,
        )
        self.assertEqual(metric["status"], "unknown")
        self.assertIsNone(metric["value"], "a stale number must not reach the menu bar")
        self.assertIn("8天", metric["reason"])
        # The timestamp survives so a reader can still see *when* it was true.
        self.assertIsNotNone(metric["observed_at"])

    def test_metrics_without_a_policy_keep_their_existing_behaviour(self) -> None:
        metric = _metric(
            "x", "c", value=28.0, unit="percent",
            observed_at=NOW - timedelta(days=800), now=NOW,
        )
        self.assertEqual(metric["status"], "available")
        self.assertEqual(metric["value"], 28.0)

    def test_a_genuine_zero_is_never_confused_with_staleness(self) -> None:
        metric = _metric(
            "x", "c", value=0, unit="count",
            observed_at=NOW, max_age_seconds=3600, now=NOW,
        )
        self.assertEqual(metric["status"], "available")
        self.assertEqual(metric["value"], 0)


class UsageAggregationTests(unittest.TestCase):
    def _write(self, root: pathlib.Path, lines):
        root.joinpath("degen-2026-07-25.jsonl").write_text(
            "\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8"
        )

    def test_beijing_day_spans_two_utc_files_and_conserves_every_counter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            root.joinpath("degen-2026-07-24.jsonl").write_text(
                "\n".join((
                    json.dumps({
                        "ts": "2026-07-24T15:59:00Z", "evt": "turn.end",
                        "agent": "excluded-before", "session": "before",
                        "inputTokens": 1_000, "outputTokens": 1_000,
                    }),
                    json.dumps({
                        "occurred_at": "2026-07-24T16:00:00Z", "evt": "turn.end",
                        "agent": "midnight", "session": "s-midnight",
                        "inputTokens": 10, "outputTokens": 100,
                    }),
                    json.dumps({
                        "ts": "2026-07-24T23:59:00Z", "evt": "req.inbound",
                        "msgType": "text", "channel": "feishu",
                    }),
                    "{malformed-in-first-candidate",
                )) + "\n",
                encoding="utf-8",
            )
            root.joinpath("degen-2026-07-25.jsonl").write_text(
                "\n".join((
                    json.dumps({
                        "timestamp": "2026-07-25T00:00:00Z", "evt": "turn.end",
                        "agent": "morning", "session": "s-morning",
                        "inputTokens": 20, "outputTokens": 200,
                    }),
                    json.dumps({
                        "ts": "2026-07-25T15:59:00Z", "evt": "delivery.receipt",
                        "status": "sent", "attempts": 1,
                    }),
                    json.dumps({
                        "ts": "2026-07-25T16:00:00Z", "evt": "turn.end",
                        "agent": "excluded-after", "session": "after",
                        "inputTokens": 2_000, "outputTokens": 2_000,
                    }),
                    json.dumps({
                        "ts": "2026-07-25T16:00:01Z", "evt": "delivery.receipt",
                        "status": "failed", "attempts": 3,
                    }),
                    "{malformed-in-second-candidate",
                )) + "\n",
                encoding="utf-8",
            )

            usage = usage_sources.collect_usage(root, NOW)

            self.assertEqual(usage["turns"], 2)
            self.assertEqual(usage["input_tokens"], 30)
            self.assertEqual(usage["output_tokens"], 300)
            self.assertEqual(usage["hourly_output_tokens"][0], 100)
            self.assertEqual(usage["hourly_output_tokens"][8], 200)
            self.assertEqual(sum(usage["hourly_output_tokens"]), 300)
            self.assertEqual(usage["inbound"], 1)
            self.assertEqual(usage["inbound_types"], {"text": 1})
            self.assertEqual(usage["delivery_sent"], 1)
            self.assertEqual(usage["delivery_total"], 1)
            self.assertEqual(usage["delivery_attempts_histogram"], {"1": 1})
            # Malformed rows have no timestamp, so their honest meaning is
            # "unparseable rows in the two candidate UTC source files". Each
            # candidate file must be opened once, not counted once per boundary.
            self.assertEqual(usage["malformed_lines"], 2)

    def test_empty_obs_directory_is_unknown_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(usage_sources.collect_usage(pathlib.Path(directory), NOW))

    def test_input_and_output_tokens_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._write(root, [
                {"ts": "2026-07-25T01:00:00Z", "evt": "turn.end", "agent": "pulse", "session": "s1",
                 "inputTokens": 9, "outputTokens": 1947, "durationMs": 1000, "isError": False},
                {"ts": "2026-07-25T02:00:00Z", "evt": "turn.end", "agent": "pulse", "session": "s1",
                 "inputTokens": 11, "outputTokens": 53, "durationMs": 3000, "isError": True},
            ])
            usage = usage_sources.collect_usage(root, NOW)
            self.assertEqual(usage["input_tokens"], 20)
            self.assertEqual(usage["output_tokens"], 2000)
            self.assertEqual(usage["turns"], 2)
            self.assertEqual(usage["failures"], 1)

    def test_input_tokens_counts_cache_not_just_the_bare_field(self) -> None:
        # 真实长上下文会话里缓存读能到裸 input 的上万倍（实测 1802 vs 2.78 亿）。
        # 只累加裸 inputTokens 会把"吃进去多少"少报几个数量级,成本与容量判断随之全错。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._write(root, [
                {"ts": "2026-07-25T01:00:00Z", "evt": "turn.end", "agent": "pulse", "session": "s1",
                 "inputTokens": 10, "cacheReadTokens": 5_000, "cacheCreationTokens": 300,
                 "outputTokens": 70, "durationMs": 1000, "isError": False},
            ])
            usage = usage_sources.collect_usage(root, NOW)
            self.assertEqual(usage["input_tokens"], 5_310)
            self.assertEqual(usage["input_tokens_uncached"], 10)
            self.assertEqual(usage["cache_read_tokens"], 5_000)
            self.assertEqual(usage["cache_creation_tokens"], 300)

    def test_events_without_cache_fields_still_aggregate(self) -> None:
        # 缓存埋点 2026-07-27 才加,更早的 obs 事件没有这两个字段。缺失必须记 0
        # 而不是让整条记录作废——老数据只反映裸 input(偏低),是已知历史断层。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._write(root, [
                {"ts": "2026-07-25T01:00:00Z", "evt": "turn.end", "agent": "pulse", "session": "s1",
                 "inputTokens": 7, "outputTokens": 70},
            ])
            usage = usage_sources.collect_usage(root, NOW)
            self.assertEqual(usage["input_tokens"], 7)
            self.assertEqual(usage["cache_read_tokens"], 0)
            self.assertEqual(usage["cache_creation_tokens"], 0)

    def test_by_agent_input_tokens_include_cache(self) -> None:
        # 分 agent 归因也必须同口径,否则总量与明细对不上账。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._write(root, [
                {"ts": "2026-07-25T01:00:00Z", "evt": "turn.end", "agent": "hestia", "session": "s1",
                 "inputTokens": 2, "cacheReadTokens": 900, "cacheCreationTokens": 98, "outputTokens": 40},
            ])
            usage = usage_sources.collect_usage(root, NOW)
            row = next(r for r in usage["by_agent"] if r["agent"] == "hestia")
            self.assertEqual(row["input_tokens"], 1_000)

    def test_duration_uses_median_not_mean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            # Mean would be 100_400; the median must resist the long tail.
            self._write(root, [
                {"ts": "2026-07-25T01:00:00Z", "evt": "turn.end", "agent": "a", "session": "s",
                 "inputTokens": 0, "outputTokens": 0, "durationMs": 100},
                {"ts": "2026-07-25T01:01:00Z", "evt": "turn.end", "agent": "a", "session": "s",
                 "inputTokens": 0, "outputTokens": 0, "durationMs": 200},
                {"ts": "2026-07-25T01:02:00Z", "evt": "turn.end", "agent": "a", "session": "s",
                 "inputTokens": 0, "outputTokens": 0, "durationMs": 300000},
            ])
            self.assertEqual(usage_sources.collect_usage(root, NOW)["duration_p50_ms"], 200)

    def test_hourly_buckets_use_beijing_hour(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._write(root, [
                {"ts": "2026-07-25T03:30:00Z", "evt": "turn.end", "agent": "a", "session": "s",
                 "inputTokens": 0, "outputTokens": 500, "durationMs": 10},
            ])
            hourly = usage_sources.collect_usage(root, NOW)["hourly_output_tokens"]
            self.assertEqual(len(hourly), 24)
            self.assertEqual(hourly[11], 500)
            self.assertEqual(sum(hourly), 500)

    def test_agent_ranking_and_multi_session_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._write(root, [
                {"ts": "2026-07-25T01:00:00Z", "evt": "turn.end", "agent": "low", "session": "x",
                 "inputTokens": 0, "outputTokens": 10, "durationMs": 1},
                {"ts": "2026-07-25T01:00:00Z", "evt": "turn.end", "agent": "high", "session": "aaaaaaaaaaaa",
                 "inputTokens": 0, "outputTokens": 900, "durationMs": 1},
                {"ts": "2026-07-25T01:00:00Z", "evt": "turn.end", "agent": "high", "session": "bbbbbbbbbbbb",
                 "inputTokens": 0, "outputTokens": 100, "durationMs": 1},
            ])
            usage = usage_sources.collect_usage(root, NOW)
            self.assertEqual([row["agent"] for row in usage["by_agent"]], ["high", "low"])
            self.assertEqual(usage["by_agent"][0]["session_count"], 2)
            self.assertEqual(usage["by_session"][0]["session_prefix"], "aaaaaaaa")

    def test_sender_identity_never_reaches_the_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._write(root, [
                {"ts": "2026-07-25T01:00:00Z", "evt": "req.inbound", "who": "ou_secret_open_id",
                 "msgType": "text", "channel": "feishu"},
            ])
            usage = usage_sources.collect_usage(root, NOW)
            self.assertEqual(usage["inbound"], 1)
            self.assertEqual(usage["inbound_types"], {"text": 1})
            self.assertNotIn("ou_secret_open_id", json.dumps(usage, ensure_ascii=False))

    def test_malformed_lines_are_skipped_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            root.joinpath("degen-2026-07-25.jsonl").write_text(
                "{not json\n"
                + json.dumps({"ts": "2026-07-25T01:00:00Z", "evt": "turn.end", "agent": "a",
                              "session": "s", "inputTokens": 1, "outputTokens": 2, "durationMs": 5})
                + "\n",
                encoding="utf-8",
            )
            usage = usage_sources.collect_usage(root, NOW)
            self.assertEqual(usage["malformed_lines"], 1)
            self.assertEqual(usage["turns"], 1)

    def test_event_in_previous_utc_file_is_included_when_beijing_day_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            root.joinpath("degen-2026-07-24.jsonl").write_text(
                json.dumps(
                {"ts": "2026-07-24T23:59:59Z", "evt": "turn.end", "agent": "a", "session": "s",
                 "inputTokens": 5, "outputTokens": 5, "durationMs": 1}
                ) + "\n",
                encoding="utf-8",
            )
            usage = usage_sources.collect_usage(root, NOW)
            self.assertEqual(usage["turns"], 1)
            self.assertEqual(usage["output_tokens"], 5)

    def test_delivery_receipts_are_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._write(root, [
                {"ts": "2026-07-25T01:00:00Z", "evt": "delivery.receipt", "status": "sent", "attempts": 1},
                {"ts": "2026-07-25T01:00:01Z", "evt": "delivery.receipt", "status": "failed", "attempts": 3},
            ])
            usage = usage_sources.collect_usage(root, NOW)
            self.assertEqual(usage["delivery_sent"], 1)
            self.assertEqual(usage["delivery_total"], 2)
            self.assertEqual(usage["delivery_attempts_histogram"], {"1": 1, "3": 1})


class HarnessTests(unittest.TestCase):
    def _wi(self, directory: pathlib.Path, name: str, body: str) -> None:
        directory.joinpath(name).write_text(body, encoding="utf-8")

    def test_counts_stalled_and_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi = root / "work-items"
            ho = root / "handoffs"
            wi.mkdir()
            ho.mkdir()
            self._wi(wi, "a.yaml", "work_item_id: wi_1\nstatus: completed\n"
                                   "created_at: '2026-07-25T02:00:00Z'\nupdated_at: '2026-07-25T02:00:00Z'\n")
            self._wi(wi, "b.yaml", "work_item_id: wi_2\nstatus: in_progress\n"
                                   "created_at: '2026-07-25T03:00:00Z'\nupdated_at: '2026-07-25T03:00:00Z'\n")
            self._wi(wi, "c.yaml", "work_item_id: wi_3\nstatus: in_progress\ntitle: old one\n"
                                   "created_at: '2026-07-01T00:00:00Z'\nupdated_at: '2026-07-01T00:00:00Z'\n")
            self._wi(ho, "h1.yaml", "handoff_id: ho_1\nack_state: pending\n")
            self._wi(ho, "h2.yaml", "handoff_id: ho_2\nack_state: archived\n")

            result = usage_sources.collect_harness(wi, ho, NOW)
            self.assertEqual(result["day_created"], 2)
            self.assertEqual(result["day_completed"], 1)
            self.assertEqual(result["handoff_pending"], 1)
            stalled = result["stalled"]
            self.assertEqual(len(stalled), 1)
            self.assertEqual(stalled[0]["work_item_id"], "wi_3")
            self.assertEqual(stalled[0]["days"], 24)

    def test_identifier_field_is_work_item_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi = root / "work-items"
            ho = root / "handoffs"
            wi.mkdir()
            ho.mkdir()
            # `id` is a decoy: the real schema uses `work_item_id`.
            self._wi(wi, "a.yaml", "id: WRONG\nwork_item_id: wi_right\nstatus: ready\n"
                                   "created_at: '2026-07-01T00:00:00Z'\n")
            stalled = usage_sources.collect_harness(wi, ho, NOW)["stalled"]
            self.assertEqual(stalled[0]["work_item_id"], "wi_right")

    def test_zero_work_items_today_reports_zero_not_unknown(self) -> None:
        """0/0 is a real observation; the灰 light is the App's job, not a null."""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi = root / "work-items"
            ho = root / "handoffs"
            wi.mkdir()
            ho.mkdir()
            result = usage_sources.collect_harness(wi, ho, NOW)
            self.assertEqual(result["day_created"], 0)
            self.assertEqual(result["day_completed"], 0)
            self.assertEqual(result["stalled"], [])

    def test_bad_yaml_is_skipped_and_counted_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi = root / "work-items"
            ho = root / "handoffs"
            wi.mkdir()
            ho.mkdir()
            self._wi(wi, "bad.yaml", "work_item_id: [unclosed\n  : : :\n")
            # updated_at 是收口时刻的判据（全库 463 条都有），须跟着写。
            self._wi(wi, "good.yaml", "work_item_id: wi_ok\nstatus: completed\n"
                                      "created_at: '2026-07-25T01:00:00Z'\n"
                                      "updated_at: '2026-07-25T02:00:00Z'\n")
            result = usage_sources.collect_harness(wi, ho, NOW)
            self.assertEqual(result["unreadable_work_items"], 1)
            self.assertEqual(result["day_completed"], 1)

    def test_missing_directories_yield_unknown_not_zero(self) -> None:
        result = usage_sources.collect_harness(
            pathlib.Path("/nonexistent-wi"), pathlib.Path("/nonexistent-ho"), NOW
        )
        self.assertIsNone(result["week_created"])
        self.assertIsNone(result["day_created"])
        self.assertIsNone(result["stalled"])
        self.assertIsNone(result["handoff_pending"])

    def test_stalled_falls_back_to_created_at_when_updated_at_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi = root / "work-items"
            ho = root / "handoffs"
            wi.mkdir()
            ho.mkdir()
            self._wi(wi, "a.yaml", "work_item_id: wi_x\nstatus: draft\ncreated_at: '2026-07-01T00:00:00Z'\n")
            stalled = usage_sources.collect_harness(wi, ho, NOW)["stalled"]
            self.assertEqual(stalled[0]["days"], 24)

    def test_completed_items_are_never_stalled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi = root / "work-items"
            ho = root / "handoffs"
            wi.mkdir()
            ho.mkdir()
            self._wi(wi, "a.yaml", "work_item_id: wi_done\nstatus: completed\n"
                                   "created_at: '2026-01-01T00:00:00Z'\nupdated_at: '2026-01-01T00:00:00Z'\n")
            self.assertEqual(usage_sources.collect_harness(wi, ho, NOW)["stalled"], [])


class RemoteProbeTests(unittest.TestCase):
    def test_failed_remote_command_returns_none(self) -> None:
        self.assertIsNone(usage_sources.run_codex_remote(("/bin/false",), 5))

    def test_missing_binary_returns_none_rather_than_raising(self) -> None:
        self.assertIsNone(usage_sources.run_codex_remote(("/nonexistent/binary",), 5))


# Real header sample captured from api.anthropic.com on 2026-07-25.
LIVE_CC_HEADERS = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-reset": "1784986200",
    "anthropic-ratelimit-unified-5h-utilization": "0.08",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-reset": "1785394800",
    "anthropic-ratelimit-unified-7d-utilization": "0.42",
}


class CCQuotaHeaderTests(unittest.TestCase):
    """No test here performs a network call; the fetch seam is always stubbed."""

    def test_live_header_sample_converts_fraction_to_percent(self) -> None:
        quota = usage_sources.parse_cc_quota_headers(LIVE_CC_HEADERS)
        # 0.42 is a fraction and must reach the snapshot as 42, matching the
        # 0-100 scale the Codex quota metric already uses.
        self.assertEqual(quota["used_percent"], 42.0)
        self.assertEqual(quota["five_hour_used_percent"], 8.0)
        self.assertEqual(quota["resets_at"], 1785394800)
        self.assertEqual(quota["five_hour_resets_at"], 1784986200)

    def test_header_names_are_matched_case_insensitively(self) -> None:
        upper = {key.upper(): value for key, value in LIVE_CC_HEADERS.items()}
        self.assertEqual(usage_sources.parse_cc_quota_headers(upper)["used_percent"], 42.0)

    def test_zero_utilization_is_preserved_and_not_confused_with_unknown(self) -> None:
        quota = usage_sources.parse_cc_quota_headers(
            {"anthropic-ratelimit-unified-7d-utilization": "0.0"}
        )
        self.assertEqual(quota["used_percent"], 0.0)

    def test_full_utilization_maps_to_one_hundred(self) -> None:
        quota = usage_sources.parse_cc_quota_headers(
            {"anthropic-ratelimit-unified-7d-utilization": "1.0"}
        )
        self.assertEqual(quota["used_percent"], 100.0)

    def test_missing_weekly_header_yields_none_not_zero(self) -> None:
        self.assertIsNone(usage_sources.parse_cc_quota_headers(
            {"anthropic-ratelimit-unified-5h-utilization": "0.08"}
        ))

    def test_absent_and_malformed_header_maps_yield_none(self) -> None:
        for payload in (None, {}, "not-a-mapping", {"unrelated-header": "1"}):
            self.assertIsNone(usage_sources.parse_cc_quota_headers(payload))

    def test_unparseable_or_out_of_contract_utilization_yields_none(self) -> None:
        for bad in ("", "abc", "42%", "-0.1", "1.7", "NaN"):
            self.assertIsNone(
                usage_sources.parse_cc_quota_headers(
                    {"anthropic-ratelimit-unified-7d-utilization": bad}
                ),
                bad,
            )

    def test_weekly_value_survives_missing_or_bad_secondary_fields(self) -> None:
        quota = usage_sources.parse_cc_quota_headers({
            "anthropic-ratelimit-unified-7d-utilization": "0.42",
            "anthropic-ratelimit-unified-7d-reset": "not-a-number",
            "anthropic-ratelimit-unified-5h-utilization": "bad",
        })
        self.assertEqual(quota["used_percent"], 42.0)
        self.assertIsNone(quota["resets_at"])
        self.assertIsNone(quota["five_hour_used_percent"])

    def test_collect_returns_none_when_fetch_fails_or_times_out(self) -> None:
        def timeout():
            raise TimeoutError("simulated network timeout")

        self.assertIsNone(usage_sources.collect_cc_quota(timeout))
        self.assertIsNone(usage_sources.collect_cc_quota(lambda: None))

    def test_collect_parses_a_successful_fetch(self) -> None:
        quota = usage_sources.collect_cc_quota(lambda: dict(LIVE_CC_HEADERS))
        self.assertEqual(quota["used_percent"], 42.0)


class CCCredentialTests(unittest.TestCase):
    def test_missing_credentials_file_yields_none(self) -> None:
        self.assertIsNone(usage_sources.read_cc_oauth_token(
            pathlib.Path("/nonexistent/.credentials.json"),
            relay_env_path=pathlib.Path("/nonexistent/relay.env"),
        ))

    def test_malformed_or_incomplete_credentials_yield_none(self) -> None:
        bodies = (
            "not json at all",
            json.dumps([1, 2, 3]),
            json.dumps({}),
            json.dumps({"claudeAiOauth": {}}),
            json.dumps({"claudeAiOauth": {"accessToken": ""}}),
            json.dumps({"claudeAiOauth": {"accessToken": 12345}}),
            json.dumps({"claudeAiOauth": "not-a-mapping"}),
        )
        absent = pathlib.Path("/nonexistent/relay.env")
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "creds.json"
            for body in bodies:
                path.write_text(body, encoding="utf-8")
                self.assertIsNone(
                    usage_sources.read_cc_oauth_token(path, relay_env_path=absent),
                    body[:40],
                )

    def test_valid_credentials_yield_the_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "creds.json"
            path.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "sk-test-token"}}), encoding="utf-8"
            )
            self.assertEqual(
                usage_sources.read_cc_oauth_token(
                    path, relay_env_path=pathlib.Path("/nonexistent/relay.env")
                ),
                "sk-test-token",
            )

    def test_fetch_without_a_token_makes_no_request(self) -> None:
        # An empty token must short-circuit before any socket is opened.
        self.assertIsNone(usage_sources.fetch_cc_quota_headers(token=""))

    def test_rate_limit_header_filter_drops_every_other_header(self) -> None:
        class Headers:
            def items(self):
                return [
                    ("authorization", "Bearer SECRET-TOKEN-VALUE"),
                    ("set-cookie", "session=SECRET"),
                    ("anthropic-ratelimit-unified-7d-utilization", "0.42"),
                ]

        filtered = usage_sources._rate_limit_headers(Headers())
        self.assertEqual(filtered, {"anthropic-ratelimit-unified-7d-utilization": "0.42"})
        self.assertNotIn("SECRET-TOKEN-VALUE", json.dumps(filtered))


if __name__ == "__main__":
    unittest.main()


class SubjectClosureTests(unittest.TestCase):
    """收口的票据必须能自己澄清告警——否则已完成的 wi 会永远喊「要你出手」。"""

    def _write(self, root, name, payload):
        (root / name).write_text(yaml.safe_dump(payload), encoding="utf-8")

    def test_terminal_work_item_reports_its_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi = root / "work-items"
            ho = root / "handoffs"
            wi.mkdir()
            ho.mkdir()
            self._write(wi, "wi_1.yaml", {
                "work_item_id": "wi_1", "status": "completed",
                "updated_at": "2026-07-27T05:00:00Z",
            })
            self._write(wi, "wi_2.yaml", {
                "work_item_id": "wi_2", "status": "in_progress",
                "updated_at": "2026-07-27T05:00:00Z",
            })
            closures = usage_sources.collect_subject_closures(wi, ho)
            self.assertIn("wi_1", closures)
            # 仍在进行中的不得出现——否则会把还没办完的活判成已恢复
            self.assertNotIn("wi_2", closures)

    def test_accepted_handoff_is_not_terminal(self) -> None:
        # accepted = 已接但没办完。算作恢复会在活还在做的时候灭掉告警。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi = root / "work-items"
            ho = root / "handoffs"
            wi.mkdir()
            ho.mkdir()
            self._write(ho, "ho_1.yaml", {
                "handoff_id": "ho_1", "ack_state": "accepted",
                "created_at": "2026-07-27T05:00:00Z",
            })
            self._write(ho, "ho_2.yaml", {
                "handoff_id": "ho_2", "ack_state": "archived",
                "created_at": "2026-07-27T05:00:00Z",
            })
            closures = usage_sources.collect_subject_closures(wi, ho)
            self.assertNotIn("ho_1", closures)
            self.assertIn("ho_2", closures)

    def test_unreadable_dirs_are_unknown_not_empty(self) -> None:
        # 读不到时返回 None（未知），而非空字典（「查过了，什么都没收口」）。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.assertIsNone(
                usage_sources.collect_subject_closures(root / "nope", root / "nah")
            )


class MainOnlyGistTests(unittest.TestCase):
    """「要你出手」卡片的「大意」——2026-07-28 大鸟「不容易理解，且没有用」。

    这个采集器此前零测试覆盖，所以面板铺了一屏读不懂的 expected_output
    而 150 条测试全绿。大意取自 wi title（人手写的一句话主旨），取不到
    才退回 expected_output。
    """

    def _write(self, directory: pathlib.Path, name: str, payload: dict) -> None:
        (directory / name).write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    def _dirs(self, root: pathlib.Path):
        wi = root / "work-items"
        ho = root / "handoffs"
        wi.mkdir()
        ho.mkdir()
        return wi, ho

    def _handoff(self, **overrides) -> dict:
        payload = {
            "handoff_id": "ho_1", "main_only": True, "ack_state": "pending",
            "from_owner": "amanda", "to_owner": "duke",
            "work_item_id": "wi_1",
            "expected_output": "①对三条发现逐条认或驳（驳要附证据，不接受「已知悉」式回复）；②自诊根因",
            "created_at": "2026-07-24T08:00:00Z",
        }
        payload.update(overrides)
        return payload

    def test_gist_comes_from_wi_title_not_expected_output(self) -> None:
        # 标题该是「这是件什么事」，不是「收方要交什么」。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(ho, "ho_1.yaml", self._handoff())
            self._write(wi, "wi_1.yaml", {
                "work_item_id": "wi_1",
                "title": "S1 成果符合性裁决：duke 7月 major——请自诊并出整改方向",
            })
            rows = usage_sources.collect_main_only_pending(ho, NOW, wi)
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0]["gist"],
                "S1 成果符合性裁决：duke 7月 major——请自诊并出整改方向",
            )
            # expected_output 仍在，供正文用；不是被顶掉而是被降级。
            self.assertTrue(rows[0]["asks"].startswith("①对三条发现"))

    def test_missing_wi_leaves_gist_empty_for_caller_fallback(self) -> None:
        # 读不到 wi 时留空,由渲染侧退回 expected_output——
        # 宁可显示难读的真话,不显示编出来的好话。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(ho, "ho_1.yaml", self._handoff(work_item_id="wi_missing"))
            rows = usage_sources.collect_main_only_pending(ho, NOW, wi)
            self.assertEqual(rows[0]["gist"], "")
            self.assertNotEqual(rows[0]["asks"], "")

    def test_work_items_dir_optional_and_unreadable_is_not_fatal(self) -> None:
        # 不传 / 传不存在的目录都不许炸——大意没了,红点不能跟着没。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(ho, "ho_1.yaml", self._handoff())
            for arg in (None, root / "nope"):
                rows = usage_sources.collect_main_only_pending(ho, NOW, arg)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["gist"], "")

    def test_subagent_ack_must_not_hide_the_card_from_the_owner(self) -> None:
        """ho_20260726_251 实况回放——这是 2026-07-29 大鸟发现的真缺陷。

        `main_only: true` 的意思是分身不能代答;而原判据看 `ack_state`,
        那恰恰是分身能改的字段。日巡分身 07-27 ack 一下,这单就静默移出
        大鸟视野,直到 07-29 wi 才真关票——大鸟三天没看见过它,却以为
        自己看见并放过了。事没办完就得一直在。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(ho, "ho_1.yaml", self._handoff(ack_state="accepted"))
            self._write(wi, "wi_1.yaml", {
                "work_item_id": "wi_1", "status": "in_progress",
                "title": "邀请：spec-anchor 内环试用 + 装备",
            })
            rows = usage_sources.collect_main_only_pending(ho, NOW, wi)
            self.assertEqual(len(rows), 1, "分身 ack 过,但 wi 还在飞——必须仍可见")
            self.assertEqual(rows[0]["gist"], "邀请：spec-anchor 内环试用 + 装备")

    def test_card_clears_when_the_work_item_actually_closes(self) -> None:
        # 出口在 wi 终态,不在 ack。办完了就该消失,否则红点永不归零。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(ho, "ho_1.yaml", self._handoff(ack_state="pending"))
            self._write(wi, "wi_1.yaml", {
                "work_item_id": "wi_1", "status": "completed", "title": "办完了",
            })
            self.assertEqual(usage_sources.collect_main_only_pending(ho, NOW, wi), [])

    def test_unreadable_wi_status_stays_visible_rather_than_silently_hidden(self) -> None:
        # 读不到 wi 时判为仍开着:宁可多报一条让大鸟看见,不可因读不到而隐藏。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(ho, "ho_1.yaml", self._handoff(
                work_item_id="wi_missing", ack_state="archived"))
            rows = usage_sources.collect_main_only_pending(ho, NOW, wi)
            self.assertEqual(len(rows), 1)

    def test_cross_tenant_still_excluded_regardless_of_wi_status(self) -> None:
        # 跨租户那个「人」是 Ken 不是大鸟;换判据不能把这一族放回来。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(ho, "ho_1.yaml", self._handoff(cross_tenant=True))
            self._write(wi, "wi_1.yaml", {
                "work_item_id": "wi_1", "status": "in_progress", "title": "对外部真人",
            })
            self.assertEqual(usage_sources.collect_main_only_pending(ho, NOW, wi), [])

    def test_blank_wi_title_is_treated_as_absent(self) -> None:
        # 空白 title 不是大意,别把一行空格当标题铺上去。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(ho, "ho_1.yaml", self._handoff())
            self._write(wi, "wi_1.yaml", {"work_item_id": "wi_1", "title": "   "})
            rows = usage_sources.collect_main_only_pending(ho, NOW, wi)
            self.assertEqual(rows[0]["gist"], "")

    def test_same_work_item_folds_into_one_row(self) -> None:
        """wi_20260729_419 实况回放——2026-08-02 大鸟「重复且无用」那件。

        aki↔ken 谈同一件事，07-29 起往返 7 张单，每张都 `main_only` 且
        `accepted`。折叠前面板 8 行里 7 行 `gist` 一字不差；折叠后这 7 张
        应收成 1 行，并说清「挂 3 天、往返 7 轮」。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            # 最早 07-22,最新 07-24(都早于 NOW=07-25);中间那张乱序写入,
            # 证明取最早/最新靠时间戳,不靠文件名排序。
            for name, created, sender in (
                ("ho_a", "2026-07-22T08:00:00Z", "aki"),
                ("ho_c", "2026-07-24T05:00:00Z", "pulse"),
                ("ho_b", "2026-07-23T11:00:00Z", "pulse"),
            ):
                self._write(ho, f"{name}.yaml", self._handoff(
                    handoff_id=name, ack_state="accepted", from_owner=sender,
                    created_at=created, expected_output=f"{name} 的要求",
                ))
            self._write(wi, "wi_1.yaml", {
                "work_item_id": "wi_1", "status": "in_progress",
                "title": "认知地图经营系统：补写入方的结构可见性工具",
            })
            rows = usage_sources.collect_main_only_pending(ho, NOW, wi)
            self.assertEqual(len(rows), 1, "同一件事必须只占一行")
            row = rows[0]
            self.assertEqual(row["rounds"], 3)
            # 挂多久看最早那张(07-22,距 NOW 3 天),不因为刚回了一句就归零。
            self.assertEqual(row["days"], 3)
            # 要动手从最新那张进。
            self.assertEqual(row["id"], "ho_c")
            self.assertEqual(row["asks"], "ho_c 的要求")

    def test_distinct_work_items_stay_separate(self) -> None:
        # 折叠只并同一件事;两件事并成一行就是把活弄丢了。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(ho, "ho_1.yaml", self._handoff(handoff_id="ho_1"))
            self._write(ho, "ho_2.yaml", self._handoff(
                handoff_id="ho_2", work_item_id="wi_2"))
            for wi_id, title in (("wi_1", "第一件"), ("wi_2", "第二件")):
                self._write(wi, f"{wi_id}.yaml", {
                    "work_item_id": wi_id, "status": "in_progress", "title": title,
                })
            rows = usage_sources.collect_main_only_pending(ho, NOW, wi)
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["rounds"] for r in rows}, {1})
            self.assertEqual({r["gist"] for r in rows}, {"第一件", "第二件"})

    def test_handoff_without_work_item_is_never_folded(self) -> None:
        # 判不出是不是同一件事就别合——宁可多一行,不可合错把活盖住。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            for name in ("ho_1", "ho_2"):
                self._write(ho, f"{name}.yaml", self._handoff(
                    handoff_id=name, work_item_id=None))
            rows = usage_sources.collect_main_only_pending(ho, NOW, wi)
            self.assertEqual(len(rows), 2, "无 work_item_id 的单各自成行")
            self.assertEqual({r["rounds"] for r in rows}, {1})

    def test_bare_main_only_wi_without_any_handoff_is_still_visible(self) -> None:
        """wi_20260804_851 实况回放——2026-08-05 大鸟报的那件。

        wi 自己写了 `main_only: true` 且 `status: draft`,但名下零 handoff。
        判据没错(draft 在 WI_OPEN_STATUSES 里),错在取数范围只扫 handoffs/,
        于是这单对面板永不可见。`main_only` 的真源有两处,采集器要都认。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "wi_851.yaml", {
                "work_item_id": "wi_851", "status": "draft", "owner": "ken",
                "main_only": True, "cross_tenant": False,
                "title": "彻底迭代 product-maturation：补上「产出物形态合规」缺环",
                "created_at": "2026-08-04T08:46:39Z",
            })
            rows = usage_sources.collect_main_only_pending(ho, NOW, wi)
            self.assertEqual(len(rows), 1, "裸 main_only wi 必须自己成行")
            self.assertEqual(rows[0]["id"], "wi_851")
            self.assertEqual(rows[0]["ack_state"], "no_handoff")
            self.assertEqual(rows[0]["from_owner"], "ken")
            self.assertTrue(rows[0]["gist"].startswith("彻底迭代 product-maturation"))

    def test_bare_wi_channel_does_not_double_report_one_already_on_a_handoff(self) -> None:
        # 同一件事只能出一张卡。有 handoff 载着就走 handoff 那条路。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(ho, "ho_1.yaml", self._handoff(work_item_id="wi_1"))
            self._write(wi, "wi_1.yaml", {
                "work_item_id": "wi_1", "status": "in_progress", "owner": "ken",
                "main_only": True, "title": "同一件事",
            })
            rows = usage_sources.collect_main_only_pending(ho, NOW, wi)
            self.assertEqual(len(rows), 1, "有 handoff 的 wi 不得被裸通道二次捞出")
            self.assertEqual(rows[0]["id"], "ho_1")
            # 只看行数不够:折叠会把两行并成一行,数量仍是 1,但 `rounds` 会
            # 从 1 涨到 2——凭空多算一轮往返。面板上就是「这事谈了 2 个来回」
            # 而实际一个来回都没有。去重失效必须在这里被逮住。
            self.assertEqual(rows[0]["rounds"], 1, "裸 wi 不是一轮往返,不得计入 rounds")
            self.assertEqual(rows[0]["ack_state"], "pending", "应保留 handoff 的真实状态")

    def test_closed_bare_wi_does_not_come_back_through_the_new_channel(self) -> None:
        # 出口仍是 wi 终态。办完的单不能因为「没有在飞的 handoff」而复活。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "wi_done.yaml", {
                "work_item_id": "wi_done", "status": "completed", "owner": "ken",
                "main_only": True, "title": "办完了",
            })
            self.assertEqual(usage_sources.collect_main_only_pending(ho, NOW, wi), [])

    def test_bare_wi_without_main_only_is_not_swept_in(self) -> None:
        """阴性对照:新通道不得把普通 wi 一并捞进来。

        面板每多一行都在花大鸟的注意力;判据仍然只有 `main_only` 一个字段。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "wi_plain.yaml", {
                "work_item_id": "wi_plain", "status": "in_progress",
                "owner": "pulse", "title": "普通在飞 wi,不该出现",
            })
            self._write(wi, "wi_false.yaml", {
                "work_item_id": "wi_false", "status": "in_progress", "owner": "pulse",
                "main_only": False, "title": "显式 false,更不该出现",
            })
            self.assertEqual(usage_sources.collect_main_only_pending(ho, NOW, wi), [])

    def test_cross_tenant_bare_wi_stays_excluded(self) -> None:
        # 跨租户那个「人」是 Ken 不是大鸟——换了载体不能把这一族放回来。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "wi_ct.yaml", {
                "work_item_id": "wi_ct", "status": "in_progress", "owner": "ken",
                "main_only": True, "cross_tenant": True, "title": "ken 跟 Eric 谈进度",
            })
            self.assertEqual(usage_sources.collect_main_only_pending(ho, NOW, wi), [])


class CCTokenExpiryTests(unittest.TestCase):
    """令牌过期必须能自证——2026-07-28「CC 未知」被我误判成自己的回归。

    真相：accessToken 13:23 到期（有效期约 8 小时），我那笔无关的 commit
    13:33 落地，晚 10 分钟。面板只说「未知」，不说因为什么，于是「刚改完就
    坏了」看起来就是因果。`expiresAt` 一直躺在同一个凭证文件里，没有任何
    一层去读它。这与 CODEX 陈值那条同型：数据自己带着证据，无人交叉核。
    """

    def _creds(self, root: pathlib.Path, payload: object) -> pathlib.Path:
        path = root / "credentials.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_expiry_is_read_without_touching_the_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            moment = datetime(2026, 7, 28, 13, 23, 26, tzinfo=timezone.utc)
            path = self._creds(root, {"claudeAiOauth": {
                "accessToken": "secret-must-not-leak",
                "expiresAt": moment.timestamp() * 1000,
            }})
            self.assertEqual(
            usage_sources.cc_token_expiry(
                path, relay_env_path=pathlib.Path("/nonexistent/relay.env")
            ),
            moment,
        )

    def test_unusable_files_are_cannot_tell_not_valid(self) -> None:
        """None = 说不好，不是「没问题」——缺戳不能当作令牌有效的证据。"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.assertIsNone(usage_sources.cc_token_expiry(root / "absent.json"))
            for payload in (
                {},                                              # 没有 oauth 段
                {"claudeAiOauth": {"accessToken": "x"}},          # 有令牌没戳
                {"claudeAiOauth": {"expiresAt": "not-a-number"}},  # 戳不是数字
                {"claudeAiOauth": {"expiresAt": True}},           # bool 是 int 的子类，须排除
            ):
                self.assertIsNone(
                    usage_sources.cc_token_expiry(self._creds(root, payload)),
                    f"{payload} 该判成说不好",
                )


class DayCompletedCountTests(unittest.TestCase):
    """「今日完成」数的是今天收口的活，不是「今天建且今天完」的活。

    2026-07-28 大鸟截图三个数，面板报 5、真值 6。判据把 completed 嵌在了
    `created.date() == today` 里面，于是「昨天建、今天完」的一件都不认——
    挂了八天今天终于收口的活，这个格子当它没发生。标签写「今日完成」，
    数的却是「今日新建里完成了几件」。
    """

    def _wi(self, directory: pathlib.Path, name: str, body: str) -> None:
        (directory / name).write_text(body, encoding="utf-8")

    def _dirs(self, root: pathlib.Path):
        wi = root / "work-items"
        ho = root / "handoffs"
        wi.mkdir()
        ho.mkdir()
        return wi, ho

    def test_completed_today_counts_regardless_of_when_it_was_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            # 八天前建的，今天收口——旧判据漏的正是这一族。
            self._wi(wi, "old.yaml", "work_item_id: wi_old\nstatus: completed\n"
                                     "created_at: '2026-07-17T01:00:00Z'\n"
                                     "updated_at: '2026-07-25T03:00:00Z'\n")
            # 今天建、今天完，两套判据都该数到。
            self._wi(wi, "new.yaml", "work_item_id: wi_new\nstatus: completed\n"
                                     "created_at: '2026-07-25T01:00:00Z'\n"
                                     "updated_at: '2026-07-25T02:00:00Z'\n")
            result = usage_sources.collect_harness(wi, ho, NOW)
            self.assertEqual(result["day_completed"], 2)
            self.assertEqual(result["day_created"], 1, "「今日新建」不受影响")

    def test_completed_on_another_day_is_not_todays_number(self) -> None:
        """昨天收口的不许算进今天，否则这个数只会涨不会落。"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._wi(wi, "y.yaml", "work_item_id: wi_y\nstatus: completed\n"
                                   "created_at: '2026-07-24T01:00:00Z'\n"
                                   "updated_at: '2026-07-24T05:00:00Z'\n")
            result = usage_sources.collect_harness(wi, ho, NOW)
            self.assertEqual(result["day_completed"], 0)

    def test_today_follows_beijing_not_utc(self) -> None:
        """「今日」按大鸟看表的时区算。

        北京 00:00-08:00 那八小时里 UTC 还停在昨天，按 UTC 判会让这三个数
        每天有三分之一时间说的不是它标的那天。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            # 北京 07-25 09:00 = UTC 07-25 01:00，两种算法都在 25 号。
            self._wi(wi, "a.yaml", "work_item_id: wi_a\nstatus: completed\n"
                                   "created_at: '2026-07-25T01:00:00Z'\n"
                                   "updated_at: '2026-07-25T01:00:00Z'\n")
            # 北京 07-25 07:00 = UTC 07-24 23:00：按 UTC 判会漏掉。
            self._wi(wi, "b.yaml", "work_item_id: wi_b\nstatus: completed\n"
                                   "created_at: '2026-07-24T23:00:00Z'\n"
                                   "updated_at: '2026-07-24T23:00:00Z'\n")
            # 北京时间 07-25 10:00 那一刻去看。
            moment = datetime(2026, 7, 25, 2, 0, tzinfo=timezone.utc)
            result = usage_sources.collect_harness(wi, ho, moment)
            self.assertEqual(result["day_completed"], 2)
            self.assertEqual(result["day_created"], 2)

    def test_iso_week_starts_monday_and_is_not_the_rolling_window(self) -> None:
        """「当周」= 自然周(周一清零),不是「近 7 天」(大鸟 2026-07-29)。

        两者必然不等,且滚动窗口没法跟上周比。NOW = 北京 07-25(周六),
        本周起点是 07-20(周一);07-19 那件落在滚动 7 天内但不在本周内——
        正是区分两个窗口的那一条。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            # 07-19(上周日):滚动 7 天数得到,自然周数不到。
            self._wi(wi, "prev.yaml", "work_item_id: wi_prev\nstatus: completed\n"
                                      "created_at: '2026-07-19T01:00:00Z'\n"
                                      "updated_at: '2026-07-19T01:00:00Z'\n")
            # 07-20(本周一):两个窗口都数得到。
            self._wi(wi, "mon.yaml", "work_item_id: wi_mon\nstatus: completed\n"
                                     "created_at: '2026-07-20T01:00:00Z'\n"
                                     "updated_at: '2026-07-20T01:00:00Z'\n")
            result = usage_sources.collect_harness(wi, ho, NOW)
            self.assertEqual(result["iso_week_created"], 1, "07-19 那件不属本周")
            self.assertEqual(result["iso_week_completed"], 1)
            self.assertEqual(result["week_created"], 2, "滚动 7 天两件都算")
            self.assertEqual(result["iso_week_start"], "2026-07-20")

    def test_iso_week_counts_completion_by_close_day_not_create_day(self) -> None:
        # 上周建、本周收口的,该记在本周的收口账上——与 day_completed 同判据。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._wi(wi, "carry.yaml", "work_item_id: wi_carry\nstatus: completed\n"
                                       "created_at: '2026-07-14T01:00:00Z'\n"
                                       "updated_at: '2026-07-22T01:00:00Z'\n")
            result = usage_sources.collect_harness(wi, ho, NOW)
            self.assertEqual(result["iso_week_completed"], 1)
            self.assertEqual(result["iso_week_created"], 0)


class InFlightSectionTests(unittest.TestCase):
    """「在飞项」保留全部非终态 WI，人工与自动属性在行内投影。"""

    def _write(self, directory: pathlib.Path, name: str, payload: dict) -> None:
        (directory / name).write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    def _dirs(self, root: pathlib.Path):
        wi = root / "work-items"
        ho = root / "handoffs"
        wi.mkdir()
        ho.mkdir()
        return wi, ho

    def test_open_items_carry_who_is_blocking_and_how_long(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "a.yaml", {
                "work_item_id": "wi_a", "status": "in_progress", "owner": "duke",
                "main_only": True,
                "title": "8-02 检查点备料包",
                "updated_at": "2026-07-17T08:00:00Z",
            })
            self._write(wi, "b.yaml", {
                "work_item_id": "wi_b", "status": "completed", "owner": "ken",
                "title": "收了的活不该出现", "updated_at": "2026-07-24T08:00:00Z",
            })
            rows = usage_sources.collect_in_flight(wi, ho, NOW)
            self.assertEqual([r["work_item_id"] for r in rows], ["wi_a"])
            self.assertEqual(rows[0]["blocked_by"], "duke")
            self.assertEqual(rows[0]["days"], 8)
            self.assertTrue(rows[0]["needs_owner"])
            self.assertEqual(rows[0]["disposition"], "manual_required")

    def test_every_nonterminal_status_is_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            for status in ("in_progress", "ready", "draft", "awaiting_verification"):
                self._write(wi, f"{status}.yaml", {
                    "work_item_id": f"wi_{status}", "status": status,
                    "owner": "pulse", "main_only": False,
                    "title": "普通 agent 队列", "updated_at": "2026-07-24T08:00:00Z",
                })
            rows = usage_sources.collect_in_flight(wi, ho, NOW)
            self.assertEqual(
                {row["status"] for row in rows},
                {"in_progress", "ready", "draft", "awaiting_verification"},
            )
            self.assertTrue(all(row["needs_owner"] is False for row in rows))
            self.assertTrue(all(row["disposition"] == "sweep_watching" for row in rows))

    def test_blocked_without_main_only_is_kept_for_human_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "blocked.yaml", {
                "work_item_id": "wi_blocked", "status": "blocked", "owner": "hestia",
                "main_only": False, "title": "等待人工更换电池",
                "blocked_reason": "需要现场操作",
                "updated_at": "2026-07-24T08:00:00Z",
            })

            rows = usage_sources.collect_in_flight(wi, ho, NOW)

            self.assertEqual([row["work_item_id"] for row in rows], ["wi_blocked"])
            self.assertFalse(rows[0]["needs_owner"])
            self.assertEqual(rows[0]["disposition"], "blocked_needs_read")

    def test_bare_main_only_work_item_is_included(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "manual.yaml", {
                "work_item_id": "wi_manual", "status": "draft", "owner": "pulse",
                "main_only": True, "title": "必须主身拍板",
                "updated_at": "2026-07-24T08:00:00Z",
            })
            rows = usage_sources.collect_in_flight(wi, ho, NOW)
            self.assertEqual([row["work_item_id"] for row in rows], ["wi_manual"])
            self.assertTrue(rows[0]["needs_owner"])

    def test_manual_and_automatic_rows_share_one_lossless_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "bare.yaml", {
                "work_item_id": "wi_bare", "status": "blocked", "owner": "pulse",
                "main_only": True, "title": "裸人工项",
                "updated_at": "2026-07-23T08:00:00Z",
            })
            self._write(wi, "handoff.yaml", {
                "work_item_id": "wi_handoff", "status": "in_progress", "owner": "pulse",
                "main_only": False, "title": "handoff 人工项",
                "updated_at": "2026-07-24T08:00:00Z",
            })
            self._write(ho, "manual.yaml", {
                "handoff_id": "ho_manual", "work_item_id": "wi_handoff",
                "ack_state": "pending", "from_owner": "pulse", "to_owner": "duke",
                "main_only": True, "created_at": "2026-07-24T08:00:00Z",
            })
            self._write(wi, "blocked.yaml", {
                "work_item_id": "wi_blocked", "status": "blocked", "owner": "hestia",
                "main_only": False, "title": "阻塞待人看",
                "updated_at": "2026-07-22T08:00:00Z",
            })
            self._write(wi, "ordinary.yaml", {
                "work_item_id": "wi_ordinary", "status": "ready", "owner": "pulse",
                "main_only": False, "title": "普通非终态",
                "updated_at": "2026-07-21T08:00:00Z",
            })
            rows = usage_sources.collect_in_flight(wi, ho, NOW)
            self.assertEqual(
                {row["work_item_id"] for row in rows},
                {"wi_bare", "wi_handoff", "wi_blocked", "wi_ordinary"},
            )
            by_id = {row["work_item_id"]: row for row in rows}
            self.assertTrue(by_id["wi_bare"]["needs_owner"])
            self.assertEqual(by_id["wi_bare"]["disposition"], "blocked_needs_read")
            self.assertEqual(by_id["wi_handoff"]["disposition"], "manual_required")
            self.assertFalse(by_id["wi_ordinary"]["needs_owner"])
            self.assertEqual(by_id["wi_ordinary"]["disposition"], "sweep_overdue")

    def test_completed_items_are_excluded_from_in_flight_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            for status in ("completed", "cancelled"):
                self._write(wi, f"{status}.yaml", {
                    "work_item_id": f"wi_{status}", "status": status,
                    "owner": "pulse", "main_only": True,
                    "title": "终态票据", "updated_at": "2026-07-24T08:00:00Z",
                })
            self.assertEqual(usage_sources.collect_in_flight(wi, ho, NOW), [])

    def test_canonical_work_item_read_uses_typed_harness_normal_path(self) -> None:
        cli = pathlib.Path("/opt/agent-harness/bin/harness")
        expected = {
            "authority_status": "verified",
            "record_status": "found",
            "canonical_as_of": "a" * 40,
            "coverage": "complete",
            "work_item": {"work_item_id": "wi_read", "status": "in_progress"},
        }
        completed = mock.Mock(
            returncode=0,
            stdout=json.dumps({"ok": True, "error": None, "read": expected}),
        )

        with mock.patch.object(
            usage_sources.subprocess,
            "run",
            return_value=completed,
        ) as run:
            observed = usage_sources.read_canonical_work_item(
                "wi_read",
                harness_cli=cli,
                timeout_seconds=7,
            )

        self.assertEqual(observed, expected)
        run.assert_called_once_with(
            [str(cli), "converge", "read-work-item", "wi_read"],
            check=False,
            capture_output=True,
            text=True,
            timeout=7,
        )

    def test_canonical_work_item_read_fails_closed_on_invalid_output(self) -> None:
        with mock.patch.object(
            usage_sources.subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout="not-json"),
        ):
            observed = usage_sources.read_canonical_work_item(
                "wi_read",
                harness_cli=pathlib.Path("/opt/agent-harness/bin/harness"),
            )

        self.assertIsNone(observed)

    def test_terminal_projection_probe_uses_real_in_flight_consumer_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "wi_terminal.yaml", {
                "work_item_id": "wi_terminal", "status": "completed", "owner": "pulse",
                "title": "terminal", "updated_at": "2026-07-24T08:00:00Z",
            })
            self._write(wi, "wi_open.yaml", {
                "work_item_id": "wi_open", "status": "awaiting_verification", "owner": "pulse",
                "title": "open", "updated_at": "2026-07-24T08:00:00Z",
            })

            terminal = usage_sources.probe_terminal_work_item_projection(
                wi,
                ho,
                "wi_terminal",
                NOW,
                authority_read={
                    "authority_status": "verified",
                    "record_status": "found",
                    "canonical_as_of": "a" * 40,
                    "coverage": "complete",
                    "source_ref": "github:org/repo@" + "a" * 40,
                    "work_item": {"work_item_id": "wi_terminal", "status": "completed"},
                },
            )
            open_item = usage_sources.probe_terminal_work_item_projection(
                wi,
                ho,
                "wi_open",
                NOW,
                authority_read={
                    "authority_status": "verified",
                    "record_status": "found",
                    "canonical_as_of": "a" * 40,
                    "coverage": "complete",
                    "source_ref": "github:org/repo@" + "a" * 40,
                    "work_item": {
                        "work_item_id": "wi_open",
                        "status": "awaiting_verification",
                    },
                },
            )

            self.assertTrue(terminal["passed"])
            self.assertFalse(terminal["in_flight_present"])
            self.assertEqual(terminal["consumer_id"], "pulse-stats-in-flight")
            self.assertTrue(open_item["passed"])
            self.assertTrue(open_item["in_flight_present"])

    def test_terminal_projection_probe_fails_closed_when_item_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            probe = usage_sources.probe_terminal_work_item_projection(
                wi,
                ho,
                "wi_missing",
                NOW,
                authority_read={
                    "authority_status": "unavailable",
                    "record_status": "unknown",
                    "canonical_as_of": None,
                    "coverage": "unknown",
                    "source_ref": None,
                    "work_item": None,
                },
            )
            self.assertFalse(probe["passed"])
            self.assertEqual(probe["reason"], "canonical_authority_unavailable")
            self.assertIsNone(probe["in_flight_present"])

    def test_terminal_projection_probe_names_stale_local_projection_without_global_absence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            probe = usage_sources.probe_terminal_work_item_projection(
                wi,
                ho,
                "wi_fresh",
                NOW,
                authority_read={
                    "authority_status": "verified",
                    "record_status": "found",
                    "canonical_as_of": "b" * 40,
                    "coverage": "complete",
                    "source_ref": "github:org/repo@" + "b" * 40,
                    "work_item": {"work_item_id": "wi_fresh", "status": "in_progress"},
                },
            )

            self.assertFalse(probe["passed"])
            self.assertEqual(probe["reason"], "local_projection_stale")
            self.assertEqual(probe["canonical_as_of"], "b" * 40)
            self.assertEqual(probe["surface_coverage"], "complete")
            self.assertIsNone(probe["in_flight_present"])

    def test_active_executor_is_stronger_than_owner_and_pending_handoff(self) -> None:
        """WI 级执行锁是「谁正在做」的强证据，不能被责任 owner 或待接单覆盖。"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "a.yaml", {
                "work_item_id": "wi_a", "status": "in_progress", "owner": "aki",
                "active_executor": "amanda", "title": "执行中",
                "updated_at": "2026-07-24T08:00:00Z",
            })
            self._write(ho, "h.yaml", {
                "handoff_id": "ho_a", "work_item_id": "wi_a",
                "ack_state": "pending", "from_owner": "aki", "to_owner": "ken",
                "responsibility_transfer": "partial",
                "created_at": "2026-07-25T07:00:00Z",
            })

            rows = usage_sources.collect_in_flight(wi, ho, NOW)

            self.assertEqual(rows[0]["blocked_by"], "amanda")

    def test_latest_live_handoff_wins_and_terminal_attempts_do_not(self) -> None:
        """pending/accepted 才持有行动权；失败或归档的后续改派不能盖掉它。"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "a.yaml", {
                "work_item_id": "wi_a", "status": "in_progress", "owner": "aki",
                "title": "多次改派", "updated_at": "2026-07-24T08:00:00Z",
            })
            handoffs = (
                ("01.yaml", "ho_01", "accepted", "duke", "partial", "2026-07-21T00:00:00Z"),
                ("02.yaml", "ho_02", "pending", "ken", "full", "2026-07-22T00:00:00Z"),
                ("03.yaml", "ho_03", "rejected", "hestia", "partial", "2026-07-23T00:00:00Z"),
                ("04.yaml", "ho_04", "archived", "amanda", "full", "2026-07-24T00:00:00Z"),
                ("05.yaml", "ho_05", "timed_out", "pulse", "not_transferred", "2026-07-25T00:00:00Z"),
            )
            for filename, handoff_id, state, receiver, transfer, created in handoffs:
                self._write(ho, filename, {
                    "handoff_id": handoff_id, "work_item_id": "wi_a",
                    "ack_state": state, "from_owner": "aki", "to_owner": receiver,
                    "responsibility_transfer": transfer, "created_at": created,
                })

            rows = usage_sources.collect_in_flight(wi, ho, NOW)

            self.assertEqual(rows[0]["blocked_by"], "ken")

    def test_accepted_receiver_remains_current_after_later_rejection(self) -> None:
        """accepted 是「已接并执行」，后续被拒的改派不把行动权凭空退给 owner。"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "a.yaml", {
                "work_item_id": "wi_a", "status": "in_progress", "owner": "ken",
                "title": "跨租户协作", "updated_at": "2026-07-24T08:00:00Z",
            })
            self._write(ho, "accepted.yaml", {
                "handoff_id": "ho_01", "work_item_id": "wi_a",
                "ack_state": "accepted", "from_owner": "ken", "to_owner": "cz-pulse",
                "responsibility_transfer": "partial",
                "created_at": "2026-07-23T00:00:00Z",
            })
            self._write(ho, "rejected.yaml", {
                "handoff_id": "ho_02", "work_item_id": "wi_a",
                "ack_state": "rejected", "from_owner": "cz-pulse", "to_owner": "duke",
                "responsibility_transfer": "full",
                "created_at": "2026-07-24T00:00:00Z",
            })

            rows = usage_sources.collect_in_flight(wi, ho, NOW)

            self.assertEqual(rows[0]["blocked_by"], "cz-pulse")

    def test_reassign_and_fanout_keep_every_current_receiver(self) -> None:
        """改派读当前 to_owner；同刻 fan-out/并行活跃接收人全部保留。"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "reassign.yaml", {
                "work_item_id": "wi_reassign", "status": "in_progress", "owner": "aki",
                "title": "重新派发", "updated_at": "2026-07-24T08:00:00Z",
            })
            self._write(ho, "reassign.yaml", {
                "handoff_id": "ho_reassign", "work_item_id": "wi_reassign",
                "ack_state": "pending", "from_owner": "aki",
                "to_owner_prev": "duke", "to_owner": "ken", "to_owners": ["ken"],
                "responsibility_transfer": "partial",
                "created_at": "2026-07-25T00:00:00Z",
            })
            self._write(wi, "fanout.yaml", {
                "work_item_id": "wi_fanout", "status": "in_progress", "owner": "aki",
                "title": "并行会签", "updated_at": "2026-07-24T08:00:00Z",
            })
            self._write(ho, "fanout-a.yaml", {
                "handoff_id": "ho_fanout_a", "work_item_id": "wi_fanout",
                "ack_state": "pending", "from_owner": "aki",
                "to_owner": "ken", "to_owners": ["ken", "hestia"],
                "responsibility_transfer": "not_transferred",
                "created_at": "2026-07-25T00:00:00Z",
            })
            self._write(ho, "fanout-b.yaml", {
                "handoff_id": "ho_fanout_b", "work_item_id": "wi_fanout",
                "ack_state": "accepted", "from_owner": "aki", "to_owner": "amanda",
                "responsibility_transfer": "partial",
                "created_at": "2026-07-25T00:00:00Z",
            })

            rows = {
                row["work_item_id"]: row
                for row in usage_sources.collect_in_flight(wi, ho, NOW)
            }

            self.assertEqual(rows["wi_reassign"]["blocked_by"], "ken")
            self.assertEqual(
                rows["wi_fanout"]["blocked_by"], "ken、hestia、amanda",
                "同刻活跃 handoff 必须合并，fan-out 不得静默只取 to_owner",
            )

    def test_unordered_live_handoffs_remain_candidates_but_broken_yaml_does_not(self) -> None:
        """缺时间无法证明先后但仍持有行动权；坏 YAML 才应完全忽略。"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            for work_item_id, owner in (("wi_owner", "aki"), ("wi_timed", "ken")):
                self._write(wi, f"{work_item_id}.yaml", {
                    "work_item_id": work_item_id, "status": "in_progress",
                    "owner": owner, "title": work_item_id,
                    "updated_at": "2026-07-24T08:00:00Z",
                })
            self._write(ho, "owner-undated.yaml", {
                "handoff_id": "ho_undated_owner", "work_item_id": "wi_owner",
                "ack_state": "pending", "from_owner": "aki", "to_owner": "duke",
            })
            self._write(ho, "timed.yaml", {
                "handoff_id": "ho_timed", "work_item_id": "wi_timed",
                "ack_state": "accepted", "from_owner": "ken", "to_owner": "cz-pulse",
                "created_at": "2026-07-23T00:00:00Z",
            })
            self._write(ho, "timed-undated.yaml", {
                "handoff_id": "ho_undated_timed", "work_item_id": "wi_timed",
                "ack_state": "pending", "from_owner": "cz-pulse", "to_owner": "duke",
            })
            (ho / "broken.yaml").write_text("[unterminated", encoding="utf-8")

            rows = {
                row["work_item_id"]: row
                for row in usage_sources.collect_in_flight(wi, ho, NOW)
            }

            self.assertEqual(rows["wi_owner"]["blocked_by"], "duke")
            self.assertEqual(rows["wi_timed"]["blocked_by"], "cz-pulse、duke")

    def test_items_already_in_act_are_marked_not_hidden(self) -> None:
        """与「要你出手」是包含关系，不是并列——不标记的话两段加起来会被
        读成两批不同的活。"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "a.yaml", {
                "work_item_id": "wi_a", "status": "in_progress", "owner": "aki",
                "title": "默认浏览器基准", "updated_at": "2026-07-21T08:00:00Z",
            })
            self._write(ho, "h.yaml", {
                "handoff_id": "ho_a", "work_item_id": "wi_a", "main_only": True,
                "ack_state": "pending", "from_owner": "aki", "to_owner": "hestia",
                "created_at": "2026-07-21T08:00:00Z",
            })
            rows = usage_sources.collect_in_flight(wi, ho, NOW)
            self.assertEqual(len(rows), 1, "已升的仍要留在在飞项里")
            self.assertTrue(rows[0]["needs_owner"])
            self.assertEqual(
                rows[0]["disposition"], "manual_required",
                "main_only 人工信号必须优先于 pending handoff 的自动推进态",
            )

    def test_oldest_created_first_so_longest_open_are_seen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            for name, wid, created in (
                ("new.yaml", "wi_new", "2026-07-25T08:00:00Z"),
                ("old.yaml", "wi_old", "2026-07-17T08:00:00Z"),
                ("mid.yaml", "wi_mid", "2026-07-22T08:00:00Z"),
            ):
                self._write(wi, name, {
                    "work_item_id": wid, "status": "in_progress",
                    "owner": "x", "title": wid,
                    "created_at": created, "updated_at": "2026-07-25T08:00:00Z",
                })
            rows = usage_sources.collect_in_flight(wi, ho, NOW)
            self.assertEqual([r["work_item_id"] for r in rows], ["wi_old", "wi_mid", "wi_new"])

    def test_unreadable_directory_is_unknown_not_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.assertIsNone(
                usage_sources.collect_in_flight(root / "nope", root / "nah", NOW)
            )



    def test_in_flight_projects_only_explicit_workflow_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho = self._dirs(root)
            self._write(wi, "light.yaml", {
                "work_item_id": "wi_light", "status": "in_progress", "owner": "pulse",
                "main_only": False, "title": "light", "updated_at": "2026-07-24T08:00:00Z",
            })
            self._write(wi, "legacy.yaml", {
                "work_item_id": "wi_legacy", "status": "in_progress", "owner": "pulse",
                "main_only": False, "title": "legacy", "updated_at": "2026-07-24T08:00:00Z",
            })

            rows = usage_sources.collect_in_flight(
                wi,
                ho,
                NOW,
                workflow_profiles={
                    "wi_light": "lightweight",
                    "wi_legacy": "unknown",
                },
            )

            by_id = {row["work_item_id"]: row for row in rows}
            self.assertEqual(by_id["wi_light"]["workflow_profile"], "lightweight")
            self.assertNotIn("workflow_profile", by_id["wi_legacy"])

class InFlightDispositionTests(unittest.TestCase):
    """在飞项的推进态分档（spec stats-exporter-collection §6.7.1）。

    起因：大鸟 2026-08-07 问「在飞项还是很多，这些 wi 分身都无法处理？」。
    查证结论是**不是处理不了，是大部分没有任何机制在推**，而面板把「在等人」
    与「没人管」混成一段，这个区别读不出来。

    本类的每条断言都对应一个曾把我骗过的真实形态，不是构造出来的边界。
    """

    def _dirs(self, root: pathlib.Path):
        wi, ho = root / "work-items", root / "handoffs"
        pol, ev = root / "policies", root / "events"
        for d in (wi, ho, pol, ev):
            d.mkdir()
        return wi, ho, pol, ev

    def _wi(self, directory: pathlib.Path, name: str, payload: dict) -> None:
        (directory / name).write_text(
            yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8"
        )

    def _policy(self, directory: pathlib.Path, agent: str, *, sweep: bool) -> None:
        items = [{"type": "pending_handoffs"}]
        if sweep:
            items.append({"type": "stale_work_items", "stale_age_days": 3})
        (directory / f"{agent}.yaml").write_text(
            yaml.safe_dump({"agent_id": agent, "scan_items": items}, allow_unicode=True),
            encoding="utf-8",
        )

    def _events(self, directory: pathlib.Path, name: str, events: list) -> None:
        (directory / name).write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
            encoding="utf-8",
        )

    def _rows(self, root: pathlib.Path) -> dict:
        wi, ho, pol, ev = (root / n for n in ("work-items", "handoffs", "policies", "events"))
        rows = usage_sources.collect_in_flight(
            wi, ho, NOW, policies_dir=pol, events_dir=ev
        )
        return {r["work_item_id"]: r for r in (rows or ())}

    def test_active_executor_alone_is_not_waiting_peer(self) -> None:
        """executor 非空但持有者数日没开工 → 不算「在等人」。

        这是 2026-08-07 实测最主要的假绿形态：6 件 active_executor 非空的 wi 里
        5 件的持有者已数日未开工。该字段是 ack 时写入、干完不清的遗留值，记的是
        「谁曾经接过」而非「谁在做」——被判方能自己改的字段不得单独作为判据。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho, pol, ev = self._dirs(root)
            self._wi(wi, "a.yaml", {
                "work_item_id": "wi_a", "status": "in_progress", "owner": "ken",
                "active_executor": "ken", "title": "executor 挂着但人没在",
                "updated_at": "2026-07-22T08:00:00Z",
            })
            self._policy(pol, "ken", sweep=True)
            # ken 最近一次真开工在 6 天前，远超 IN_FLIGHT_ACTIVE_DAYS。
            self._events(ev, "autonomous_execution_event-2026-07.jsonl", [
                {"event_type": "autonomous_execution_event", "agent_id": "ken",
                 "occurred_at": "2026-07-19T08:00:00Z"},
            ])

            row = self._rows(root)["wi_a"]

            self.assertNotEqual(row["disposition"], "waiting_peer")
            self.assertEqual(row["disposition"], "sweep_overdue")

    def test_skipped_dispatch_events_do_not_count_as_activity(self) -> None:
        """每小时一条 skipped_* 不是心跳——不得让 wi 看起来活跃。

        实测 wi_20260725_586 连续 6 小时每小时一条 skipped_autonomous_covered，
        而该 wi 实际六天无人推进。不过滤 skip，被反复跳过的 wi 反而显得最活跃。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho, pol, ev = self._dirs(root)
            self._wi(wi, "a.yaml", {
                "work_item_id": "wi_a", "status": "in_progress", "owner": "ken",
                "active_executor": "ken", "title": "只被跳过，没被处理",
                "updated_at": "2026-07-22T08:00:00Z",
            })
            self._policy(pol, "ken", sweep=True)
            self._events(ev, "autonomous_execution_event-2026-07.jsonl", [
                {"event_type": "autonomous_execution_event", "agent_id": "ken",
                 "occurred_at": "2026-07-25T07:00:00Z"},
            ])
            # 刚刚才「有事件」，但全是 skip。
            self._events(ev, "dispatch_event-2026-07.jsonl", [
                {"event_type": "dispatch_event", "agent_id": "ken",
                 "occurred_at": "2026-07-25T07:30:00Z",
                 "dispositions": [{"ref_id": "wi_a", "outcome": "skipped_autonomous_covered"}]},
            ])

            row = self._rows(root)["wi_a"]

            self.assertIsNone(row["quiet_days"], "全是 skip 不该产生「有动静」")
            self.assertNotEqual(row["disposition"], "waiting_peer")

    def test_never_touched_is_distinct_from_all_skipped(self) -> None:
        """「从无真事件」与「有过真事件、距今 N 天」是两种语义，不得合并。

        前者多为刚建单（正常），后者是停了 N 天（可能危险）。都渲染成
        「静默 N 天」会把两者读成一样。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho, pol, ev = self._dirs(root)
            self._wi(wi, "fresh.yaml", {
                "work_item_id": "wi_fresh", "status": "in_progress", "owner": "pulse",
                "title": "刚建单", "updated_at": "2026-07-25T07:00:00Z",
            })
            self._wi(wi, "stale.yaml", {
                "work_item_id": "wi_stale", "status": "in_progress", "owner": "pulse",
                "title": "动过一次然后停了", "updated_at": "2026-07-22T08:00:00Z",
            })
            self._policy(pol, "pulse", sweep=True)
            self._events(ev, "dispatch_event-2026-07.jsonl", [
                {"event_type": "dispatch_event", "agent_id": "pulse",
                 "occurred_at": "2026-07-23T08:00:00Z",
                 "dispositions": [{"ref_id": "wi_stale", "outcome": "spawned"}]},
            ])

            rows = self._rows(root)

            self.assertIsNone(rows["wi_fresh"]["quiet_days"])
            self.assertEqual(rows["wi_stale"]["quiet_days"], 2)

    def test_owner_without_stale_sweep_is_unowned(self) -> None:
        """owner 没开兜底扫描 → 真没人管；开了 → 等兜底。判据同源、只差这一项。

        dispatcher 是 handoff 驱动的，handoff 全终态后唯一还会主动碰它的就是
        stale_work_items 扫描，而该扫描按 agent opt-in。2026-08-07 实测
        amanda/duke/hume/reeve/cz-pulse 五家未声明。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho, pol, ev = self._dirs(root)
            for name, owner in (("a.yaml", "duke"), ("b.yaml", "hestia")):
                self._wi(wi, name, {
                    "work_item_id": f"wi_{owner}", "status": "in_progress",
                    "owner": owner, "title": "无 handoff 无 executor",
                    "updated_at": "2026-07-22T08:00:00Z",
                })
            self._policy(pol, "duke", sweep=False)
            self._policy(pol, "hestia", sweep=True)

            rows = self._rows(root)

            self.assertEqual(rows["wi_duke"]["disposition"], "unowned")
            self.assertEqual(rows["wi_hestia"]["disposition"], "sweep_overdue")

    def test_unreadable_policy_dir_degrades_to_watching_not_unowned(self) -> None:
        """policy 读不到 ≠ 没开扫描——不得误报「真没人管」。

        误报这一档比漏报更糟：它会让大鸟去给一个本来就有扫描的 agent「补配置」。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho, pol, ev = self._dirs(root)
            self._wi(wi, "a.yaml", {
                "work_item_id": "wi_a", "status": "in_progress", "owner": "duke",
                "title": "policy 目录不存在", "updated_at": "2026-07-25T07:00:00Z",
            })

            rows = usage_sources.collect_in_flight(
                wi, ho, NOW, policies_dir=root / "does-not-exist", events_dir=ev
            )

            self.assertEqual(rows[0]["disposition"], "sweep_watching")

    def test_circuit_broken_is_marked_separately(self) -> None:
        """熔断的活机制已经放弃了，不能混在「等兜底」里当作还有人管。

        实测 wi_20260802_282 被扫描捡起、连试两次失败后从 12:40Z 空转到 23:40Z。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho, pol, ev = self._dirs(root)
            self._wi(wi, "a.yaml", {
                "work_item_id": "wi_a", "status": "in_progress", "owner": "hestia",
                "title": "捡起来了但一直失败", "updated_at": "2026-07-22T08:00:00Z",
            })
            self._policy(pol, "hestia", sweep=True)
            self._events(ev, "dispatch_event-2026-07.jsonl", [
                {"event_type": "dispatch_event", "agent_id": "hestia",
                 "occurred_at": "2026-07-24T08:00:00Z",
                 "dispositions": [{"ref_id": "wi_a", "outcome": "circuit_broken"}]},
            ])

            row = self._rows(root)["wi_a"]

            self.assertTrue(row["circuit_broken"])

    def test_pending_handoff_makes_it_waiting_peer(self) -> None:
        """有人还没接 → 真的在等人，dispatcher 每小时会因它醒。"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho, pol, ev = self._dirs(root)
            self._wi(wi, "a.yaml", {
                "work_item_id": "wi_a", "status": "in_progress", "owner": "pulse",
                "title": "发出去了等验收", "updated_at": "2026-07-22T08:00:00Z",
            })
            self._wi(ho, "h.yaml", {
                "handoff_id": "ho_a", "work_item_id": "wi_a", "ack_state": "pending",
                "from_owner": "pulse", "to_owner": "duke",
                "created_at": "2026-07-22T09:00:00Z",
            })
            self._policy(pol, "pulse", sweep=True)

            self.assertEqual(self._rows(root)["wi_a"]["disposition"], "waiting_peer")

    def test_blocked_and_awaiting_verification_are_in_flight(self) -> None:
        """在飞比开放宽：这两种状态此前被 WI_OPEN_STATUSES 挡在面板外。

        2026-08-07 实测面板只显示 11 件、实际摊着 17 件——hestia 那件
        「卡真人换电池」从未出现在大鸟眼前。大鸟定：纳入。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho, pol, ev = self._dirs(root)
            self._wi(wi, "b.yaml", {
                "work_item_id": "wi_blocked", "status": "blocked", "owner": "hestia",
                "blocked_reason": "卡真人物理换电池，Agent 侧无可做动作",
                "title": "阻塞的活", "updated_at": "2026-07-22T08:00:00Z",
            })
            self._wi(wi, "v.yaml", {
                "work_item_id": "wi_verify", "status": "awaiting_verification",
                "owner": "pulse", "title": "等验收", "updated_at": "2026-07-22T08:00:00Z",
            })
            self._policy(pol, "hestia", sweep=True)
            self._policy(pol, "pulse", sweep=True)

            rows = self._rows(root)

            self.assertIn("wi_blocked", rows)
            self.assertIn("wi_verify", rows)
            self.assertEqual(rows["wi_blocked"]["disposition"], "blocked_needs_read")
            self.assertIn("换电池", rows["wi_blocked"]["blocked_reason"])

    def test_groups_partition_without_loss(self) -> None:
        """分组是切分不是筛选：每件活恰好落一个档，总数不得因分档而变。"""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            wi, ho, pol, ev = self._dirs(root)
            for index, (status, owner) in enumerate((
                ("in_progress", "duke"), ("blocked", "hestia"),
                ("awaiting_verification", "pulse"), ("ready", "ken"),
                ("draft", "steward"),
            )):
                self._wi(wi, f"{index}.yaml", {
                    "work_item_id": f"wi_{index}", "status": status, "owner": owner,
                    "blocked_reason": "理由" if status == "blocked" else None,
                    "title": f"第 {index} 件", "updated_at": "2026-07-22T08:00:00Z",
                })
            for agent in ("hestia", "pulse", "ken", "steward"):
                self._policy(pol, agent, sweep=True)
            self._policy(pol, "duke", sweep=False)

            rows = self._rows(root)
            known = {
                "waiting_peer", "blocked_needs_read",
                "sweep_overdue", "sweep_watching", "unowned",
            }

            self.assertEqual(len(rows), 5)
            for row in rows.values():
                self.assertIn(row["disposition"], known)


class RelayEnvTokenPrecedenceTests(unittest.TestCase):
    """生产凭证在 relay.env，不在 credentials.json(2026-07-30 断根)。

    「CC 额度未知」发作三次，三次都是探针读了 `~/.claude/.credentials.json`
    那份 **交互登录副产物**：8 小时就过期，vultr 上没人去 TUI 重登，所以它
    几乎总是过期的。而生产的 CC 进程从 relay.env 拿 `CLAUDE_CODE_OAUTH_TOKEN`，
    一直好着。前两次我改的都是呈现，没碰这个根。
    """

    def _env(self, root: pathlib.Path, body: str) -> pathlib.Path:
        path = root / "relay.env"
        path.write_text(body, encoding="utf-8")
        return path

    def test_relay_env_wins_over_credentials_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = self._env(root, "FOO=bar\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-live\nBAZ=1\n")
            creds = root / "creds.json"
            creds.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "sk-stale-tui-token"}}),
                encoding="utf-8",
            )
            self.assertEqual(
                usage_sources.read_cc_oauth_token(creds, relay_env_path=env),
                "sk-ant-oat-live",
            )

    def test_quoted_values_are_unwrapped(self) -> None:
        for body, want in (
            ('CLAUDE_CODE_OAUTH_TOKEN="sk-quoted"\n', "sk-quoted"),
            ("CLAUDE_CODE_OAUTH_TOKEN='sk-single'\n", "sk-single"),
            ("CLAUDE_CODE_OAUTH_TOKEN=sk-bare\n", "sk-bare"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                env = self._env(pathlib.Path(directory), body)
                self.assertEqual(usage_sources.read_relay_env_token(env), want)

    def test_falls_back_to_credentials_when_relay_env_absent(self) -> None:
        # ownermac 等没有 relay.env 的机器上，交互登录那份才是唯一来源。
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            creds = root / "creds.json"
            creds.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "sk-local"}}), encoding="utf-8"
            )
            self.assertEqual(
                usage_sources.read_cc_oauth_token(creds, relay_env_path=root / "nope.env"),
                "sk-local",
            )

    def test_empty_or_missing_key_is_none_not_empty_string(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for body in ("CLAUDE_CODE_OAUTH_TOKEN=\n", "OTHER=1\n", ""):
                self.assertIsNone(usage_sources.read_relay_env_token(self._env(root, body)), body)

    def test_expiry_reports_unknown_when_production_token_is_in_use(self) -> None:
        """生产走 relay.env 时不许拿隔壁文件的过期戳解释状态。

        面板曾写「令牌 17 小时前过期」——听着像根因，其实指着一份闲置文件。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = self._env(root, "CLAUDE_CODE_OAUTH_TOKEN=sk-live\n")
            creds = root / "creds.json"
            creds.write_text(
                json.dumps({"claudeAiOauth": {"expiresAt": 1785317846885}}), encoding="utf-8"
            )
            self.assertIsNone(usage_sources.cc_token_expiry(creds, relay_env_path=env))
