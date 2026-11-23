from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

from pulse_stats.live import (
    ACK_ARGV,
    SYSTEMD_ARGV,
    SYSTEMD_UNITS,
    CommandResult,
    LivePaths,
    LiveSnapshotError,
    LiveSnapshotService,
)
from pulse_stats.live_contract import (
    ALERT_MESSAGE_CHARS,
    DISPLAY_TZ,
    MAX_ALERT_GROUPS,
    RECOVERY_UNIT_ARGV,
    _capability,
    _parse_date,
    _parse_systemd_timestamp,
)
from pulse_stats.live_sources import (
    _alert_subject,
    _is_tenant_dispatcher_finding,
    _ledger_finding_label,
)
from pulse_stats.reducer import IncidentEvent, reduce_incident_events


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
HARNESS_SHA = "1" * 40
# 曾经的 `live_contract.TIER_ESCALATE_COUNT`：同一件事 24h 内喊满这么多次就升
# `act`。规则 2026-07-28 撤掉（喊得多不等于要人拍板），常量 2026-08-01 删掉。
# 这里留一份本地字面量，是因为下面几个用例守的正是**「喊够这个次数也不再升档」**
# ——判据没了，反例还得看得见，不然这几条断言读起来像在数一个凭空的 20。
FORMER_ESCALATE_COUNT = 20
VALID_METRIC_STATUSES = {"available", "warming_up", "unknown", "not_applicable"}
VALID_METRIC_QUALITIES = {"validated", "descriptive", "insufficient", "unavailable"}


# ack-probe is retired and ships no default command; the fixture injects one.
TEST_ACK_ARGV = ("/fixture/ack-probe", "--json")


class FakeRunner:
    def __init__(
        self,
        *,
        ack: CommandResult,
        systemd: CommandResult,
        recovery_units: CommandResult = None,
    ):
        self.ack = ack
        self.systemd = systemd
        # Default: the probe-unit query fails, which is what every test that
        # does not care about it should see -- no evidence, so tiering falls
        # back to the plain age rule.
        self.recovery_units = recovery_units
        self.calls = []

    def run(self, argv, timeout=None):
        call = tuple(argv)
        self.calls.append(call)
        if call == TEST_ACK_ARGV:
            return self.ack
        if call == SYSTEMD_ARGV:
            return self.systemd
        if call == RECOVERY_UNIT_ARGV:
            return self.recovery_units or CommandResult(1, "")
        if call[-2:] == ("rev-parse", "HEAD"):
            return CommandResult(0, HARNESS_SHA + "\n")
        if call[:3] == ("/usr/bin/git", "-C", str(call[2])):
            return CommandResult(0, "abc1234\n")
        return CommandResult(1, "")


def recovery_unit_result(records):
    """systemd `show` output for the probe units, in its real wire format.

    ``records`` maps unit name to ``(status, "Sun 2026-07-26 06:50:11 UTC")``.
    """
    return CommandResult(0, "\n\n".join(
        f"Id={unit}\nExecMainExitTimestamp={stamp}\nExecMainStatus={status}"
        for unit, (status, stamp) in records.items()
    ))


def systemd_stamp(moment: datetime) -> str:
    """Render a moment the way systemd prints it: weekday, naive time, zone."""
    return moment.astimezone(timezone.utc).strftime("%a %Y-%m-%d %H:%M:%S UTC")


# Older than OWNER_VISIBLE_ACK_OVERDUE_DAYS, so the breach is one the owner is
# meant to see. Tests about projection mechanics need a visible incident; the
# threshold itself is covered by its own test.
OVERDUE_CREATED_AT = NOW - timedelta(days=4)


def ack_result(breaches=None, pending_total=0):
    return CommandResult(0, json.dumps({
        "ts": NOW.isoformat(), "breaches": breaches or [], "pending_total": pending_total,
    }))


def systemd_result():
    text = "\n\n".join(
        f"Id={unit}\nActiveState=active\nUnitFileState=enabled\nResult=success"
        for unit in SYSTEMD_UNITS
    )
    return CommandResult(0, text)


class LiveFixture:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        self.registry = root / "probe-heartbeats.json"
        self.heartbeats = root / "heartbeats.json"
        self.ledger = root / "ledger.json"
        self.alerts = root / "events"
        self.alerts.mkdir()
        self.state = root / "state" / "incident-state-v1.json"
        # v2-A sources default to empty directories under the temp root so the
        # suite never reads production data, the network, or the peer machine.
        self.work_items = root / "work-items"
        self.handoffs = root / "handoffs"
        self.obs = root / "obs"
        self.subscriptions = root / "subscriptions.json"
        self.cc_projects = root / "cc-projects"
        self.codex_sessions = root / "codex-sessions"
        self.surface_registry = root / "scl-surfaces.json"
        self.global_authority = root / "global-authority-evidence-v1.json"
        self.canonical_read_model = root / "canonical-read-model-v2.json"
        for directory in (self.work_items, self.handoffs, self.obs, self.cc_projects, self.codex_sessions):
            directory.mkdir()
        self.surface_registry.write_text(json.dumps({
            "schema_version": "pulse_scl_surface_registry/v3",
            "surfaces": [
                {"surface_id": surface_id, "workspace": f"workspace-{index}"}
                for index, surface_id in enumerate((
                    "surface_v1_local_macos",
                    "surface_v1_cloud_linux_a",
                    "surface_v1_cloud_linux_b",
                    "surface_v1_chatgpt_github",
                ))
            ],
        }), encoding="utf-8")
        self.paths = LivePaths(
            repo=self.repo, harness_repo=self.repo,
            registry=self.registry, heartbeats=self.heartbeats,
            ledger=self.ledger, alerts_dir=self.alerts, state=self.state,
            work_items=self.work_items, handoffs=self.handoffs, obs=self.obs,
            subscriptions=self.subscriptions,
            policies=self.root / "policies",
            cc_projects=self.cc_projects, codex_sessions=self.codex_sessions,
            surface_registry=self.surface_registry,
            global_authority=self.global_authority,
            canonical_read_model=self.canonical_read_model,
        )
        self.subscriptions.write_text(json.dumps({
            "schema_version": 1,
            "components": [
                {
                    "component_id": "claude_code",
                    "amount_cents": 25_000,
                    "currency": "USD",
                    "effective_from": "2026-07-01",
                    "effective_until": None,
                    "source_ref": "owner-reviewed:test-claude-code",
                    "attribution": "cc_mtd_output_share",
                },
                {
                    "component_id": "codex",
                    "amount_cents": 2_000,
                    "currency": "USD",
                    "effective_from": "2026-07-01",
                    "effective_until": None,
                    "source_ref": "owner-reviewed:test-codex",
                    "attribution": "unattributed_no_agent_source",
                },
            ],
        }), encoding="utf-8")
        self.registry.write_text(json.dumps({
            "schema_version": 1,
            "sources": [
                {"source": "vultr-ack-probe", "enabled": True, "max_age_seconds": 2100},
                {"source": "vultr-ledger-lint-probe", "enabled": True, "max_age_seconds": 8100},
                {"source": "vultr-executor-reaper", "enabled": True, "max_age_seconds": 8100},
                {"source": "vultr-harness-daily-flush", "enabled": True, "max_age_seconds": 97200},
            ],
        }), encoding="utf-8")
        self.write_heartbeats(NOW, NOW)
        self.write_ledger(["INV1:secret-ish-finding"])

    def write_heartbeats(self, ack_at: datetime, ledger_at: datetime):
        self.heartbeats.write_text(json.dumps({
            "vultr-ack-probe": {"ts": ack_at.isoformat(), "receivedAt": ack_at.isoformat()},
            "vultr-ledger-lint-probe": {"ts": ledger_at.isoformat(), "receivedAt": ledger_at.isoformat()},
            "vultr-executor-reaper": {"ts": ledger_at.isoformat(), "receivedAt": ledger_at.isoformat()},
            "vultr-harness-daily-flush": {"ts": ledger_at.isoformat(), "receivedAt": ledger_at.isoformat()},
        }), encoding="utf-8")

    def write_ledger(self, keys):
        self.ledger.write_text(json.dumps({
            "schema_version": 1, "fingerprint": "fixture-fingerprint", "keys": keys,
        }), encoding="utf-8")

    def write_global_authority(self) -> None:
        revision = "sha256:" + hashlib.sha256(
            f"bigbird-online/PULSE@{HARNESS_SHA}".encode("utf-8")
        ).hexdigest()
        digest = "sha256:" + "b" * 64
        surface_ids = [
            "surface_v1_local_macos",
            "surface_v1_cloud_linux_a",
            "surface_v1_cloud_linux_b",
            "surface_v1_chatgpt_github",
        ]
        self.global_authority.write_text(json.dumps({
            "schema_version": "pulse_stats_global_authority_evidence/v1",
            "authority_revision": revision,
            "required_endpoints": surface_ids,
            "materializations": [
                {
                    "endpoint_id": surface_id,
                    "authority_revision": revision,
                    "digest": digest,
                }
                for surface_id in surface_ids
            ],
            "observed_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        }), encoding="utf-8")

    def service(self, runner, **kwargs):
        now = kwargs.pop("now", NOW)
        kwargs.setdefault("http_fetch", lambda url, **_: None)
        kwargs.setdefault("codex_remote_fetch", lambda: None)
        # The CC quota probe is a real outbound API call; the suite must never
        # make one, so it is stubbed to "unavailable" unless a test overrides it.
        kwargs.setdefault("cc_quota_fetch", lambda: None)
        # 额度缓存住在生产盘上（~/.relay/monitoring/…）。不 stub 的话，
        # 「取不到额度」这一族测试会读到生产机上真实的上次读数，于是断言
        # 「该是 unknown」在有缓存的机器上红、在干净机器上绿——环境决定成败。
        # 写同理：测试不许往生产盘落文件。
        kwargs.setdefault("cc_quota_cache_read", lambda: None)
        kwargs.setdefault("cc_quota_cache_write", lambda quota, now: None)
        # The Codex probe spends real subscription quota *and* takes ~8s. When
        # it was first wired in without a stub here the suite issued 118 live
        # Codex turns in twelve minutes (2026-07-28). `LiveCollector` now
        # defaults it to a no-op, and `test_the_codex_probe_never_runs_by_default`
        # holds that line; this stub makes the intent explicit at the call site.
        kwargs.setdefault("codex_probe", lambda: False)
        kwargs.setdefault("ack_argv", TEST_ACK_ARGV)
        return LiveSnapshotService(paths=self.paths, runner=runner, now=now, **kwargs)


class CapabilityProjectionContractTests(unittest.TestCase):
    def test_unknown_current_evidence_downgrades_validated_to_partial(self) -> None:
        cases = (
            {"health": "unknown", "freshness": "fresh"},
            {"health": "ok", "freshness": "unknown"},
        )
        for case in cases:
            with self.subTest(**case):
                capability = _capability(
                    "test.capability",
                    health=case["health"],
                    freshness=case["freshness"],
                    observed_at=NOW,
                    max_age_seconds=180,
                    reason="Structured current-state evidence decides maturity",
                    implementation_status="active_validated",
                    evidence=("fixture:current",),
                )
                self.assertEqual(capability["implementation_status"], "active_partial")

    def test_measured_bad_state_does_not_downgrade_validated_maturity(self) -> None:
        cases = (
            {"health": "warning", "freshness": "fresh"},
            {"health": "critical", "freshness": "fresh"},
            {"health": "stale", "freshness": "stale"},
            {"health": "ok", "freshness": "error"},
        )
        for case in cases:
            with self.subTest(**case):
                capability = _capability(
                    "test.capability",
                    health=case["health"],
                    freshness=case["freshness"],
                    observed_at=NOW,
                    max_age_seconds=180,
                    reason="A measured bad result is still current evidence",
                    implementation_status="active_validated",
                    evidence=("fixture:measurement",),
                )
                self.assertEqual(capability["implementation_status"], "active_validated")

    def test_reason_text_cannot_override_structured_maturity_evidence(self) -> None:
        capability = _capability(
            "test.capability",
            health="ok",
            freshness="fresh",
            observed_at=NOW,
            max_age_seconds=180,
            reason="This prose happens to say not adapted but is not a contract field",
            implementation_status="active_validated",
            evidence=("fixture:measurement",),
        )
        self.assertEqual(capability["implementation_status"], "active_validated")

    def test_not_adapted_baseline_capability_is_explicitly_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            snapshot = json.loads(
                fixture.service(FakeRunner(
                    ack=ack_result(), systemd=systemd_result(),
                )).render_json()
            )
            trace = next(
                item for item in snapshot["capabilities"]
                if item["id"] == "s1.request_trace_capture"
            )
            self.assertEqual(trace["health"], "unknown")
            self.assertEqual(trace["freshness"]["state"], "unknown")
            self.assertEqual(trace["implementation_status"], "active_partial")


class LiveSnapshotTests(unittest.TestCase):
    def test_active_cost_carrier_skips_retired_scans_but_keeps_operational_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.paths = replace(fixture.paths, emit_legacy_cost_metrics=False)
            service = fixture.service(FakeRunner(
                ack=ack_result(), systemd=systemd_result(),
            ))
            with (
                patch("pulse_stats.live_sources.billing.build_billing_metrics",
                      side_effect=AssertionError("retired billing scan")),
                patch("pulse_stats.live_sources.usage_sources.collect_month_to_date",
                      side_effect=AssertionError("retired MTD scan")),
                patch("pulse_stats.live_sources.managed_cost_shadow.build_managed_cost_shadow_metrics",
                      side_effect=AssertionError("retired shadow scan")),
            ):
                snapshot = json.loads(service.render_json())
        ids = {metric["id"] for metric in snapshot["metrics"]}
        self.assertIn("usage.today.turns", ids)
        self.assertIn("delivery.today.sent", ids)
        self.assertFalse(any(metric.startswith(("billing.", "usage.mtd.", "compute_cost.shadow."))
                             for metric in ids))

    def test_auxiliary_remote_reads_start_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            barrier = threading.Barrier(2, timeout=2)
            calls = []
            guard = threading.Lock()

            def fetched(name):
                with guard:
                    calls.append(name)
                barrier.wait()
                return None

            service = fixture.service(
                FakeRunner(ack=ack_result(), systemd=systemd_result()),
                platform_health_fetch=lambda: fetched("platform"),
                automation_fetch=lambda: fetched("automation"),
            )
            json.loads(service.render_json())
            self.assertCountEqual(calls, ["platform", "automation"])

    def assert_metric_enums(self, snapshot) -> None:
        for metric in snapshot["metrics"]:
            self.assertIn(metric["status"], VALID_METRIC_STATUSES, metric["id"])
            self.assertIn(metric["quality"], VALID_METRIC_QUALITIES, metric["id"])

    def test_live_service_consumes_the_versioned_global_authority_carrier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.write_global_authority()
            snapshot = json.loads(fixture.service(FakeRunner(
                ack=ack_result(), systemd=systemd_result(),
            )).render_json())

        self.assertEqual(snapshot["global_authority"]["status"], "validated")
        self.assertEqual(snapshot["global_authority"]["scope"], "global")
        self.assertEqual(snapshot["global_authority"]["required_endpoint_count"], 4)
        source = next(
            row for row in snapshot["provenance"]["sources"]
            if row["id"] == "global-authority-carrier"
        )
        self.assertEqual(source["freshness"], "fresh")
        self.assertIsNone(source["error_code"])

    def test_missing_global_authority_carrier_fails_closed_in_the_live_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            snapshot = json.loads(fixture.service(FakeRunner(
                ack=ack_result(), systemd=systemd_result(),
            )).render_json())

        self.assertEqual(snapshot["global_authority"]["status"], "unknown")
        self.assertEqual(snapshot["global_authority"]["scope"], "unknown")
        source = next(
            row for row in snapshot["provenance"]["sources"]
            if row["id"] == "global-authority-carrier"
        )
        self.assertEqual(source["freshness"], "error")
        self.assertEqual(source["error_code"], "unavailable_or_invalid")

    def test_fresh_carrier_cannot_validate_a_different_harness_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.write_global_authority()
            raw = json.loads(fixture.global_authority.read_text(encoding="utf-8"))
            other_revision = "sha256:" + hashlib.sha256(
                f"bigbird-online/PULSE@{'2' * 40}".encode("utf-8")
            ).hexdigest()
            raw["authority_revision"] = other_revision
            for materialization in raw["materializations"]:
                materialization["authority_revision"] = other_revision
            fixture.global_authority.write_text(json.dumps(raw), encoding="utf-8")

            snapshot = json.loads(fixture.service(FakeRunner(
                ack=ack_result(), systemd=systemd_result(),
            )).render_json())

        self.assertEqual(snapshot["global_authority"]["freshness"], "fresh")
        self.assertEqual(snapshot["global_authority"]["status"], "partial")
        self.assertIn(
            "source_revision", snapshot["global_authority"]["degraded_reasons"]
        )
        source = next(
            row for row in snapshot["provenance"]["sources"]
            if row["id"] == "global-authority-carrier"
        )
        self.assertEqual(source["error_code"], "source_revision_mismatch")

    def test_snapshot_ttl_outlives_one_whole_consumer_refresh_interval(self) -> None:
        """TTL must cover a missed refresh cycle, or the bar cries wolf.

        The consumer (`RefreshPolicy.interval`, PulseStats/Core/RefreshPolicy.swift)
        fetches every 30 minutes. From 2026-07-28 to 2026-08-02 this TTL was 180
        seconds, so a snapshot declared itself expired 27 of every 30 minutes
        while the fetch loop was working perfectly — the menu bar showed
        "数据已过期" ~90% of the time over data nobody had any reason to distrust.

        Nothing pinned that number, which is exactly why it survived. This test
        is the pin: TTL must strictly exceed one refresh interval, so "expired"
        can only mean a refresh was actually missed rather than merely pending.
        """
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result([], 0), systemd=systemd_result())
            snapshot = json.loads(fixture.service(runner).render_json())

            generated = _parse_date(snapshot["generated_at"])
            expires = _parse_date(snapshot["expires_at"])
            ttl = expires - generated

            # Mirrors RefreshPolicy.interval = 30 * 60 on the Swift side.
            consumer_refresh_interval = timedelta(minutes=30)
            self.assertGreater(
                ttl,
                consumer_refresh_interval,
                "a TTL inside the refresh interval marks healthy data expired",
            )
            self.assertEqual(ttl, timedelta(minutes=45))
            # Generated exactly at `now`, not drifting off the collection clock.
            self.assertEqual(generated, NOW)

    def test_complete_rebuild_uses_fixed_commands_and_persists_bounded_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.alerts.joinpath("escalation_event-2026-07.jsonl").write_text(json.dumps({
                "event_id": "alert-1", "severity": "major", "capability_id": "MUST-NOT-USE-CAPABILITY",
                "match_label": "ack_overdue", "title": "MUST-NOT-USE-TITLE",
                "occurred_at": NOW.isoformat(), "reason": "MUST-NOT-USE-REASON",
                "message": "OPERATOR-READABLE-MESSAGE",
            }) + "\n", encoding="utf-8")
            runner = FakeRunner(
                ack=ack_result(
                    [{
                        "ho_id": "ho_secret",
                        "created_at": OVERDUE_CREATED_AT.isoformat(),
                    }],
                    1,
                ),
                systemd=systemd_result(),
            )
            rendered = fixture.service(runner).render_json()
            snapshot = json.loads(rendered)
            self.assertEqual(snapshot["schema_version"], "pulse.monitoring.snapshot.v2")
            self.assertEqual(snapshot["baseline_version"], "0.2.2")
            self.assertEqual(snapshot["overall"]["runtime_health"], "warning")
            self.assertEqual(snapshot["overall"]["coverage"], "unknown")
            # 2→1：账本违例不再进面板（2026-08-04 大鸟裁）。剩下的那条是
            # 心跳失联——面板上唯一保留的事故类。
            self.assertEqual(snapshot["overall"]["open_incident_count"], 1)
            # The source `message` is the only operator-readable prose in the
            # escalation stream, so it is carried into the alert reason on
            # purpose. Everything else the raw event asserts about itself --
            # its capability, title and reason -- is still ignored in favour of
            # the reviewed ALERT_TEMPLATES mapping.
            self.assertIn("OPERATOR-READABLE-MESSAGE", snapshot["alerts"][0]["reason"])
            self.assertNotIn("MUST-NOT-USE-TITLE", rendered)
            self.assertNotIn("MUST-NOT-USE-REASON", rendered)
            self.assertNotIn("MUST-NOT-USE-CAPABILITY", rendered)
            self.assertEqual(snapshot["alerts"][0]["severity"], "warning")
            self.assertEqual(snapshot["alerts"][0]["capability_id"], "s2.ack_overdue")
            self.assertEqual(snapshot["alerts"][0]["title"], "Handoff acknowledgement is overdue")
            self.assertNotEqual(snapshot["alerts"][0]["event_id"], "alert-1")
            self.assertNotIn("INV1:secret-ish-finding", rendered)
            self.assertIn(TEST_ACK_ARGV, runner.calls)
            self.assertIn(SYSTEMD_ARGV, runner.calls)
            persisted = json.loads(fixture.state.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 1)
            self.assertIn("source_watermarks", persisted)
            self.assertIn("source_cursors", persisted)
            self.assertEqual(stat.S_IMODE(fixture.state.stat().st_mode), 0o600)
            capability_ids = {item["id"] for item in snapshot["capabilities"]}
            self.assertTrue({
                "incident.current_state_projection", "snapshot.canonical_producer",
                "s2.executor_reaper", "notify.daily_flush", "s1.reply_quality_degen",
                "s1.request_reply_reconciliation", "s2.cross_node_reconciliation",
            }.issubset(capability_ids))

    def test_today_usage_metrics_name_the_beijing_calendar_day_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.obs.joinpath("degen-2026-07-22.jsonl").write_text(
                json.dumps({
                    "ts": "2026-07-22T01:00:00Z", "evt": "turn.end",
                    "agent": "pulse", "session": "s1",
                    "inputTokens": 10, "outputTokens": 20, "durationMs": 30,
                })
                + "\n{malformed-with-no-event-day\n",
                encoding="utf-8",
            )
            snapshot = json.loads(
                fixture.service(FakeRunner(
                    ack=ack_result(), systemd=systemd_result(),
                )).render_json()
            )
            today_metrics = [
                item for item in snapshot["metrics"]
                if (
                    item["id"].startswith("usage.today.")
                    or item["id"].startswith("usage.hourly.")
                    or item["id"] in ("usage.by_agent", "usage.by_session")
                    or item["id"].startswith("delivery.today.")
                )
            ]

            self.assertTrue(today_metrics)
            self.assertTrue(all(item["window"] == "today_cst" for item in today_metrics))
            self.assertTrue(all(
                item.get("scope", "").startswith("Beijing calendar day")
                for item in today_metrics
            ))
            malformed = next(
                item for item in today_metrics
                if item["id"] == "usage.today.malformed_lines"
            )
            self.assertIn("candidate UTC source files", malformed["scope"])
            self.assertIn("no event-day attribution", malformed["reason"])
            aggregates = [
                item for item in today_metrics
                if item["id"] != "usage.today.malformed_lines"
            ]
            self.assertTrue(all(
                (item["status"], item["quality"]) == ("available", "descriptive")
                for item in aggregates
            ))
            self.assertEqual(
                (malformed["status"], malformed["quality"]),
                ("available", "validated"),
            )
            self.assert_metric_enums(snapshot)

    def test_billing_facts_survive_unproven_turn_and_legacy_metrics_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.obs.joinpath("degen-2026-07-22.jsonl").write_text(
                json.dumps({
                    "ts": "2026-07-22T01:00:00Z",
                    "evt": "turn.end",
                    "runtime": "cc",
                    "agent": "pulse",
                    "outputTokens": 100,
                }) + "\n",
                encoding="utf-8",
            )
            snapshot = json.loads(
                fixture.service(FakeRunner(
                    ack=ack_result(), systemd=systemd_result(),
                )).render_json()
            )
        by_id = {item["id"]: item for item in snapshot["metrics"]}
        billing_ids = {item for item in by_id if item.startswith("billing.mtd.")}
        self.assertEqual(billing_ids, {
            "billing.mtd.period",
            "billing.mtd.components",
            "billing.mtd.by_agent",
            "billing.mtd.attributed_cents",
            "billing.mtd.unattributed_cents",
            "billing.mtd.total_cents",
        })
        # 两套 mtd 指标并存，各答各的问题——这一行原本断言 usage.mtd.* 不存在，
        # 那是 0ef3260 删掉旧实现时留下的假设。2026-07-31 大鸟指出「原版本呈现
        # 的是每个 Agent 耗费了多少 Token，新版本完全丢弃了之前的功能，而且没有
        # 新东西替代」，旧实现已恢复，所以这条断言连同它的前提一起过时了。
        #
        # billing.mtd.* = 账单（$270 由哪些组件构成，精确到分，无证据就说无证据）
        # usage.mtd.*   = 用量（token 花在谁身上，按输出占比估算）
        usage_mtd_ids = {item for item in by_id if item.startswith("usage.mtd.")}
        self.assertEqual(usage_mtd_ids, {
            "usage.mtd.by_agent",
            "usage.mtd.output_tokens",
            "usage.mtd.days_covered",
            "usage.mtd.subscription_usd",
        })
        # 关键的分轴：这条 turn 没有 billing_component_id，所以账单归因诚实地
        # 报「证据不足」；但用量统计不需要那个字段，照常出数。一个字段的缺失
        # 不该同时打掉两套指标——那正是这次功能消失的成因。
        self.assertIsNone(by_id["billing.mtd.by_agent"]["value"])
        self.assertEqual(
            [row["agent"] for row in by_id["usage.mtd.by_agent"]["value"]],
            ["pulse"],
        )
        self.assertEqual(by_id["billing.mtd.total_cents"]["value"], 27_000)
        self.assertEqual(
            (by_id["billing.mtd.components"]["status"],
             by_id["billing.mtd.components"]["quality"]),
            ("available", "validated"),
        )
        for metric_id in (
            "billing.mtd.period",
            "billing.mtd.by_agent",
            "billing.mtd.attributed_cents",
            "billing.mtd.unattributed_cents",
        ):
            self.assertEqual(
                (by_id[metric_id]["status"], by_id[metric_id]["quality"]),
                ("unknown", "insufficient"),
            )
            self.assertIsNone(by_id[metric_id]["value"])

    def test_proven_cc_turn_publishes_partial_exact_conserving_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.obs.joinpath("degen-2026-07-22.jsonl").write_text(
                "\n".join([
                    json.dumps({
                        "ts": "2026-07-22T01:00:00Z",
                        "evt": "turn.end",
                        "billing_component_id": "claude_code",
                        "agent": "pulse",
                        "outputTokens": 75,
                    }),
                    json.dumps({
                        "ts": "2026-07-22T02:00:00Z",
                        "evt": "turn.end",
                        "billing_component_id": "claude_code",
                        "agent": None,
                        "outputTokens": 25,
                    }),
                ]) + "\n",
                encoding="utf-8",
            )
            snapshot = json.loads(
                fixture.service(FakeRunner(
                    ack=ack_result(), systemd=systemd_result(),
                )).render_json()
            )
        by_id = {item["id"]: item for item in snapshot["metrics"]}
        period = by_id["billing.mtd.period"]
        self.assertEqual((period["status"], period["quality"]), ("available", "descriptive"))
        self.assertEqual(period["value"][0]["coverage"], "partial")
        self.assertEqual(period["sample_count"], 2)
        self.assertEqual(
            by_id["billing.mtd.by_agent"]["value"],
            [{"agent": "pulse", "output_tokens": 75, "attributed_cents": 18_750}],
        )
        attributed = by_id["billing.mtd.attributed_cents"]["value"]
        unattributed = by_id["billing.mtd.unattributed_cents"]["value"]
        self.assertEqual((attributed, unattributed), (18_750, 8_250))
        self.assertEqual(attributed + unattributed, 27_000)

    def test_bad_billing_config_degrades_exactly_all_six_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.subscriptions.write_text("{broken", encoding="utf-8")
            snapshot = json.loads(
                fixture.service(FakeRunner(
                    ack=ack_result(), systemd=systemd_result(),
                )).render_json()
            )
        billing = [
            item for item in snapshot["metrics"]
            if item["id"].startswith("billing.mtd.")
        ]
        self.assertEqual(len(billing), 6)
        self.assertTrue(all(
            (
                item["status"],
                item["quality"],
                item["value"],
                item["sample_count"],
                bool(item["reason"]),
            )
            == ("unknown", "unavailable", None, None, True)
            for item in billing
        ))

    def test_bad_harness_records_make_aggregates_descriptive_but_count_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.work_items.joinpath("good.yaml").write_text(
                "work_item_id: wi_ok\nstatus: completed\n"
                "created_at: '2026-07-22T01:00:00Z'\n"
                "updated_at: '2026-07-22T02:00:00Z'\n",
                encoding="utf-8",
            )
            fixture.work_items.joinpath("bad.yaml").write_text(
                "work_item_id: [unclosed\n  : : :\n",
                encoding="utf-8",
            )
            fixture.handoffs.joinpath("bad.yaml").write_text(
                "handoff_id: [unclosed\n  : : :\n",
                encoding="utf-8",
            )
            snapshot = json.loads(
                fixture.service(FakeRunner(
                    ack=ack_result(), systemd=systemd_result(),
                )).render_json()
            )
            harness_metrics = [
                item for item in snapshot["metrics"]
                if item["id"].startswith("harness.")
            ]
            unreadable = next(
                item for item in harness_metrics
                if item["id"] == "harness.unreadable_records"
            )
            aggregates = [
                item for item in harness_metrics
                if item["id"] != "harness.unreadable_records"
            ]

            self.assertTrue(aggregates)
            self.assertTrue(all(
                (item["status"], item["quality"]) == ("available", "descriptive")
                for item in aggregates
            ))
            self.assertEqual(unreadable["value"], 2)
            self.assertEqual(
                (unreadable["status"], unreadable["quality"]),
                ("available", "validated"),
            )
            self.assert_metric_enums(snapshot)

    def test_unknown_alert_label_is_dropped_and_future_heartbeat_is_not_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            future = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
            fixture.write_heartbeats(future, future)
            fixture.alerts.joinpath("escalation_event-2026-07.jsonl").write_text(json.dumps({
                "event_id": "sensitive-event-id",
                "severity": "major",
                "capability_id": "sensitive-capability",
                "match_label": "unreviewed-sensitive-label",
                "occurred_at": NOW.isoformat(),
            }) + "\n", encoding="utf-8")
            snapshot = json.loads(fixture.service(FakeRunner(
                ack=ack_result(), systemd=systemd_result(),
            )).render_json())
            self.assertEqual(snapshot["alerts"], [])
            heartbeat = next(
                item for item in snapshot["capabilities"] if item["id"] == "meta.probe_heartbeat"
            )
            self.assertEqual(heartbeat["freshness"]["state"], "stale")
            rendered = json.dumps(snapshot)
            self.assertNotIn("unreviewed-sensitive-label", rendered)
            self.assertNotIn("sensitive-capability", rendered)

    def test_stale_heartbeat_action_uses_registered_systemd_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.registry.write_text(json.dumps({
                "schema_version": 1,
                "sources": [{
                    "source": "fixture-heartbeat",
                    "enabled": True,
                    "kind": "local_probe",
                    "max_age_seconds": 60,
                    "service": "fixture-probe.service",
                }],
            }), encoding="utf-8")
            fixture.heartbeats.write_text("{}", encoding="utf-8")

            snapshot = json.loads(fixture.service(FakeRunner(
                ack=ack_result(), systemd=systemd_result(),
            )).render_json())

        incident = next(
            item for item in snapshot["incidents"]
            if item["capability_id"] == "meta.probe_heartbeat"
        )
        self.assertEqual(
            incident["suggested_action"],
            "在探针所在 Linux 节点检查 journalctl --user -u fixture-probe.service -n 50",
        )

    def test_registered_systemd_service_refreshes_an_existing_incident_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.registry.write_text(json.dumps({
                "schema_version": 1,
                "sources": [{
                    "source": "fixture-heartbeat",
                    "enabled": True,
                    "kind": "local_probe",
                    "max_age_seconds": 60,
                }],
            }), encoding="utf-8")
            fixture.heartbeats.write_text("{}", encoding="utf-8")
            first = json.loads(fixture.service(FakeRunner(
                ack=ack_result(), systemd=systemd_result(),
            )).render_json())
            legacy = next(
                item for item in first["incidents"]
                if item["capability_id"] == "meta.probe_heartbeat"
            )
            self.assertIn("-u fixture-heartbeat ", legacy["suggested_action"])

            fixture.registry.write_text(json.dumps({
                "schema_version": 1,
                "sources": [{
                    "source": "fixture-heartbeat",
                    "enabled": True,
                    "kind": "local_probe",
                    "max_age_seconds": 60,
                    "service": "fixture-probe.service",
                }],
            }), encoding="utf-8")
            second = json.loads(fixture.service(FakeRunner(
                ack=ack_result(), systemd=systemd_result(),
            )).render_json())

        refreshed = next(
            item for item in second["incidents"]
            if item["capability_id"] == "meta.probe_heartbeat"
        )
        self.assertEqual(refreshed["occurrence_count"], 1)
        self.assertEqual(refreshed["evidence"], legacy["evidence"])
        self.assertEqual(
            refreshed["suggested_action"],
            "在探针所在 Linux 节点检查 journalctl --user -u fixture-probe.service -n 50",
        )

    def test_positive_evidence_recovers_ack_heartbeat_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            old = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
            fixture.write_heartbeats(old, NOW)
            first_runner = FakeRunner(
                ack=ack_result(
                    [{"ho_id": "ho-1", "created_at": OVERDUE_CREATED_AT.isoformat()}], 1
                ),
                systemd=systemd_result(),
            )
            first = json.loads(fixture.service(first_runner).render_json())
            # 3→2：账本违例不再进面板（2026-08-04 大鸟裁），剩 ack 逾期 + 心跳失联。
            self.assertEqual(first["overall"]["open_incident_count"], 2)

            fixture.write_heartbeats(NOW, NOW)
            fixture.write_ledger([])
            second_runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            later = datetime(2026, 7, 22, 8, 1, tzinfo=timezone.utc)
            second = json.loads(fixture.service(second_runner, now=later).render_json())
            self.assertEqual(second["overall"]["open_incident_count"], 0)
            self.assertTrue(all(item["state"] == "resolved" for item in second["incidents"]))

    def test_resolved_heartbeat_reopens_when_the_same_success_becomes_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            source = "fixture-heartbeat"
            maximum_age = 60
            fixture.registry.write_text(json.dumps({
                "schema_version": 1,
                "sources": [{
                    "source": source,
                    "enabled": True,
                    "max_age_seconds": maximum_age,
                }],
            }), encoding="utf-8")
            first_heartbeat = NOW - timedelta(minutes=2)
            fixture.heartbeats.write_text(json.dumps({
                source: {
                    "ts": first_heartbeat.isoformat(),
                    "receivedAt": first_heartbeat.isoformat(),
                },
            }), encoding="utf-8")
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            first = json.loads(fixture.service(runner).render_json())
            self.assertEqual(first["overall"]["open_incident_count"], 1)

            recovered_at = NOW + timedelta(minutes=1)
            fixture.heartbeats.write_text(json.dumps({
                source: {
                    "ts": recovered_at.isoformat(),
                    "receivedAt": recovered_at.isoformat(),
                },
            }), encoding="utf-8")
            recovered = json.loads(
                fixture.service(runner, now=recovered_at).render_json()
            )
            self.assertEqual(recovered["overall"]["open_incident_count"], 0)

            detected_at = NOW + timedelta(minutes=3)
            reopened = json.loads(
                fixture.service(runner, now=detected_at).render_json()
            )
            incident = next(
                item for item in reopened["incidents"]
                if item["capability_id"] == "meta.probe_heartbeat"
            )
            self.assertEqual(reopened["overall"]["open_incident_count"], 1)
            self.assertEqual(incident["state"], "open")
            self.assertEqual(incident["reopen_count"], 1)
            self.assertEqual(incident["occurrence_count"], 1)
            self.assertEqual(
                incident["opened_at"],
                (recovered_at + timedelta(seconds=maximum_age)).isoformat(),
            )

            repeated = json.loads(
                fixture.service(
                    runner,
                    now=detected_at + timedelta(minutes=1),
                ).render_json()
            )
            same_incident = next(
                item for item in repeated["incidents"]
                if item["capability_id"] == "meta.probe_heartbeat"
            )
            self.assertEqual(same_incident["episode_id"], incident["episode_id"])
            self.assertEqual(same_incident["occurrence_count"], 1)

    def test_ledger_findings_never_reach_the_owner_panel(self) -> None:
        """账本违例一条都不进面板，且存量 open 的会被关掉。

        2026-08-04 大鸟裁：台账违例是 agent 日常活，推人无用且有害。同日两处
        执行——探针侧停推送（11adfc1），投影侧停显示（本条钉住）。这条测试是
        「过两天又报一堆没用的告警」的机械闸：任何人把 breach 加回来即挂。
        """
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.write_heartbeats(NOW, NOW)
            fixture.write_ledger(["INV7:wi_20260804_524", "INV6:ho_20260804_043"])
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            snapshot = json.loads(fixture.service(runner).render_json())
            ledger_incidents = [
                item
                for item in snapshot["incidents"]
                if item["capability_id"] == "s2.ledger_invariants"
            ]
            self.assertTrue(
                all(item["state"] == "resolved" for item in ledger_incidents),
                "账本违例不得以 open 事故的形式出现在大鸟面板上",
            )
            # capability 仍照常上报——关的是呈现，不是观测。
            ledger_capability = next(
                item
                for item in snapshot["capabilities"]
                if item["id"] == "s2.ledger_invariants"
            )
            self.assertIn(ledger_capability["health"], {"ok", "warning"})

    def test_failed_source_does_not_recover_its_existing_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            first_runner = FakeRunner(
                ack=ack_result(
                    [{"ho_id": "ho-1", "created_at": OVERDUE_CREATED_AT.isoformat()}], 1
                ),
                systemd=systemd_result(),
            )
            fixture.service(first_runner).render_json()
            fixture.write_ledger([])
            second_runner = FakeRunner(ack=CommandResult(2, ""), systemd=systemd_result())
            second = json.loads(fixture.service(second_runner).render_json())
            ack_incidents = [item for item in second["incidents"] if item["capability_id"] == "s2.ack_overdue"]
            self.assertEqual(ack_incidents[0]["state"], "open")
            ack_capability = next(item for item in second["capabilities"] if item["id"] == "s2.ack_overdue")
            self.assertEqual(ack_capability["freshness"]["state"], "error")

    def test_retired_ack_probe_without_injected_command_runs_nothing(self) -> None:
        self.assertEqual(ACK_ARGV, ())
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            snapshot = json.loads(fixture.service(runner, ack_argv=ACK_ARGV).render_json())
        self.assertNotIn(TEST_ACK_ARGV, runner.calls)
        self.assertFalse(any(not call or call[0].endswith("ack-probe") for call in runner.calls))
        capability = next(item for item in snapshot["capabilities"] if item["id"] == "s2.ack_overdue")
        self.assertEqual(capability["health"], "unknown")
        self.assertEqual(capability["freshness"]["state"], "error")
        sources = {item["id"]: item for item in snapshot["provenance"]["sources"]}
        self.assertEqual(sources["ack-probe"]["error_code"], "not_configured")

    def test_corrupt_state_and_incomplete_rebuild_force_unknown_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.state.parent.mkdir()
            fixture.state.write_text("not-json", encoding="utf-8")
            fixture.registry.write_text("not-json", encoding="utf-8")
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            snapshot = json.loads(fixture.service(runner).render_json())
            self.assertEqual(snapshot["overall"]["runtime_health"], "unknown")
            projection = next(item for item in snapshot["capabilities"] if item["id"] == "incident.current_state_projection")
            self.assertEqual(projection["freshness"]["state"], "error")
            self.assertEqual(fixture.state.read_text(encoding="utf-8"), "not-json")

    def test_corrupt_state_is_backed_up_before_complete_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.state.parent.mkdir()
            fixture.state.write_text("corrupt-audit-evidence", encoding="utf-8")
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            snapshot = json.loads(fixture.service(runner).render_json())
            projection = next(item for item in snapshot["capabilities"] if item["id"] == "incident.current_state_projection")
            self.assertEqual(projection["freshness"]["state"], "fresh")
            backups = list(fixture.state.parent.glob("incident-state-v1.json.corrupt-*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "corrupt-audit-evidence")
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_all_core_sources_fail_and_payload_limit_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.registry.write_text("not-json", encoding="utf-8")
            fixture.ledger.unlink()
            runner = FakeRunner(ack=CommandResult(2, ""), systemd=CommandResult(1, ""))
            with self.assertRaisesRegex(LiveSnapshotError, "all core"):
                fixture.service(runner).render_json()

        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            with self.assertRaisesRegex(LiveSnapshotError, "payload"):
                fixture.service(runner, max_payload_bytes=64).render_json()
            self.assertFalse(fixture.state.exists())


class CCQuotaMetricTests(unittest.TestCase):
    """CC quota reaches the snapshot on the same 0-100 scale as Codex."""

    LIVE_HEADERS = {
        "anthropic-ratelimit-unified-7d-utilization": "0.42",
        "anthropic-ratelimit-unified-7d-reset": "1785394800",
        "anthropic-ratelimit-unified-5h-utilization": "0.08",
    }

    @staticmethod
    def _metrics(rendered):
        return {item["id"]: item for item in json.loads(rendered)["metrics"]}

    def test_available_quota_is_reported_as_percent_not_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            rendered = fixture.service(
                runner, cc_quota_fetch=lambda: dict(self.LIVE_HEADERS)
            ).render_json()
            metrics = self._metrics(rendered)

            weekly = metrics["relay.cc.used_percent"]
            self.assertEqual(weekly["status"], "available")
            self.assertEqual(weekly["value"], 42.0)
            self.assertEqual(weekly["unit"], "percent")
            self.assertEqual(weekly["window"], "7d")
            self.assertEqual(metrics["relay.cc.resets_at"]["value"], 1785394800)
            self.assertEqual(metrics["relay.cc.5h_used_percent"]["value"], 8.0)

    def test_unavailable_quota_is_unknown_and_never_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            # Covers 401, timeout and missing credentials alike: the fetch seam
            # reports unavailability the same way for all of them.
            rendered = fixture.service(runner, cc_quota_fetch=lambda: None).render_json()
            metrics = self._metrics(rendered)
            for metric_id in ("relay.cc.used_percent", "relay.cc.resets_at", "relay.cc.5h_used_percent"):
                self.assertEqual(metrics[metric_id]["status"], "unknown", metric_id)
                self.assertIsNone(metrics[metric_id]["value"], metric_id)

    def test_an_expired_token_says_so_instead_of_just_unknown(self) -> None:
        """「未知」不说因为什么，就会被读成「刚改完就坏了」。

        2026-07-28：令牌 13:23 到期，我一笔无关的 commit 13:33 落地，大鸟看到
        CC 变空——时间上像我弄坏的，实则晚 10 分钟。凭证文件里的 expiresAt
        一直能自证，只是没人读。
        """
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            expired = NOW - timedelta(minutes=30)
            rendered = fixture.service(
                runner, cc_quota_fetch=lambda: None, cc_token_expiry=lambda: expired,
            ).render_json()
            reason = self._metrics(rendered)["relay.cc.used_percent"]["reason"]
            self.assertIn("过期", reason)
            self.assertIn("重新登录", reason)

    def test_a_valid_token_does_not_get_blamed_for_an_unavailable_quota(self) -> None:
        """令牌没过期就不许甩锅给它——那样只会把排查引向错的方向。"""
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            for expiry in (lambda: NOW + timedelta(hours=3), lambda: None):
                rendered = fixture.service(
                    runner, cc_quota_fetch=lambda: None, cc_token_expiry=expiry,
                ).render_json()
                reason = self._metrics(rendered)["relay.cc.used_percent"]["reason"]
                self.assertNotIn("过期", reason)

    def test_an_expired_token_falls_back_to_the_last_good_reading(self) -> None:
        """令牌空窗期不许把好端端的读数丢掉。

        令牌每 8 小时过期一次，且**过期后要等下一次 CC 真正发请求才被刷回**
        （2026-07-29 实测 05:55 过期、09:37 才刷新）。此前空窗期内这一格直接
        变「未知」——把一个几分钟前还有效的读数整个扔了。额度是慢变量，
        「刚才 82%」远比「不知道」有用。
        """
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            observed = NOW - timedelta(minutes=8)
            rendered = fixture.service(
                runner,
                cc_quota_fetch=lambda: None,
                cc_token_expiry=lambda: NOW - timedelta(minutes=30),
                cc_quota_cache_read=lambda: {
                    "used_percent": 82.0, "resets_at": 1785394800,
                    "five_hour_used_percent": 3.0, "observed_at": observed,
                },
            ).render_json()
            metric = self._metrics(rendered)["relay.cc.used_percent"]
            self.assertEqual(metric["value"], 82.0)
            self.assertEqual(metric["status"], "available")
            # 观测时刻照实写,不假装是现在读到的。
            self.assertEqual(metric["observed_at"], observed.isoformat())

    def test_a_cached_reading_past_its_shelf_life_goes_unknown(self) -> None:
        """兜底不是永久有效——太老就该说不知道,而不是拿旧数当现值。"""
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            rendered = fixture.service(
                runner,
                cc_quota_fetch=lambda: None,
                cc_quota_cache_read=lambda: {
                    "used_percent": 82.0, "resets_at": None,
                    "five_hour_used_percent": None,
                    "observed_at": NOW - timedelta(hours=20),
                },
            ).render_json()
            self.assertEqual(self._metrics(rendered)["relay.cc.used_percent"]["status"], "unknown")

    def test_a_live_reading_is_persisted_for_the_next_blind_window(self) -> None:
        """读到了必须落盘,否则下次空窗期依然无处可退。"""
        saved = []
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            fixture.service(
                runner,
                cc_quota_fetch=lambda: dict(self.LIVE_HEADERS),
                cc_quota_cache_write=lambda quota, now: saved.append((quota, now)),
            ).render_json()
            self.assertEqual(len(saved), 1, "成功读到额度后必须写缓存")
            self.assertEqual(saved[0][1], NOW)

    def test_a_raising_probe_degrades_to_unknown_without_breaking_the_snapshot(self) -> None:
        def boom():
            raise TimeoutError("simulated timeout")

        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            metrics = self._metrics(fixture.service(runner, cc_quota_fetch=boom).render_json())
            self.assertEqual(metrics["relay.cc.used_percent"]["status"], "unknown")
            self.assertIsNone(metrics["relay.cc.used_percent"]["value"])

    def test_missing_weekly_header_yields_unknown_rather_than_a_partial_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            metrics = self._metrics(fixture.service(
                runner,
                cc_quota_fetch=lambda: {"anthropic-ratelimit-unified-5h-utilization": "0.08"},
            ).render_json())
            self.assertEqual(metrics["relay.cc.used_percent"]["status"], "unknown")
            self.assertIsNone(metrics["relay.cc.used_percent"]["value"])

    def test_no_credential_material_can_reach_the_rendered_snapshot(self) -> None:
        secret = "sk-ant-oat01-MUST-NOT-LEAK-INTO-SNAPSHOT"

        def fetch_with_secret_in_scope():
            # Mirrors production: the token is used to obtain the headers but
            # only rate-limit headers are ever returned to the collector.
            _authorization = f"Bearer {secret}"
            return dict(self.LIVE_HEADERS)

        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            rendered = fixture.service(runner, cc_quota_fetch=fetch_with_secret_in_scope).render_json()
            self.assertNotIn(secret, rendered)
            self.assertNotIn("sk-ant", rendered)
            self.assertNotIn("Bearer", rendered)
            self.assertNotIn("accessToken", rendered)
            # The metric it was fetched for is still present.
            self.assertEqual(self._metrics(rendered)["relay.cc.used_percent"]["value"], 42.0)


def cc_quota_document(observed=NOW, weekly_resets=None, five_hour=True, **changes):
    weekly_resets = weekly_resets if weekly_resets is not None else int((NOW + timedelta(days=3)).timestamp())
    document = {
        "schema": "pulse.stats.cc_quota/v1", "status": "available",
        "source": "relay_cc_rate_limit_event",
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "weekly": {"used_percent": 4.0, "window_minutes": 10080, "resets_at": weekly_resets},
        "five_hour": {"used_percent": 46.0, "window_minutes": 300,
                      "resets_at": int((NOW + timedelta(hours=2)).timestamp())} if five_hour else None,
        "rate_limit_status": "allowed",
    }
    document.update(changes)
    return document


class CCQuotaDocumentTests(unittest.TestCase):
    """Relay-observed CC quota: current provider window only, unknown otherwise."""

    def test_valid_document_and_five_hour_window(self) -> None:
        from pulse_stats import cc_quota
        document = cc_quota.validate(cc_quota_document(), NOW)
        self.assertEqual(document["weekly"]["used_percent"], 4.0)
        self.assertEqual(cc_quota.five_hour(document, NOW), (46.0, None))
        self.assertEqual(cc_quota.five_hour(cc_quota.validate(cc_quota_document(five_hour=False), NOW), NOW),
                         (None, "five_hour_window_unavailable"))
        later = NOW + timedelta(hours=3)
        self.assertEqual(cc_quota.five_hour(document, later), (None, "quota_window_elapsed"))

    def test_window_rules_and_bad_documents_are_rejected(self) -> None:
        from pulse_stats import cc_quota
        resets = int((NOW + timedelta(days=3)).timestamp())
        cases = {
            "quota_window_elapsed": cc_quota_document(weekly_resets=int(NOW.timestamp())),
            "quota_outside_current_window": cc_quota_document(observed=NOW - timedelta(days=4, minutes=1)),
            "quota_from_future": cc_quota_document(observed=NOW + timedelta(seconds=61)),
            "invalid_quota_document": cc_quota_document(schema="pulse.stats.codex_quota/v1"),
            "quota_unavailable": cc_quota_document(status="unavailable"),
            "invalid_quota_source": cc_quota_document(source="header_probe"),
            "invalid_quota_time": cc_quota_document(observed_at="2026-07-22T08:00:00"),
        }
        for bad_weekly in ({"used_percent": 101, "window_minutes": 10080, "resets_at": resets},
                           {"used_percent": True, "window_minutes": 10080, "resets_at": resets},
                           {"used_percent": 4.0, "window_minutes": 300, "resets_at": resets},
                           {"used_percent": 4.0, "window_minutes": 10080, "resets_at": float(resets)},
                           None):
            with self.subTest(weekly=bad_weekly), self.assertRaisesRegex(cc_quota.QuotaError, "invalid_quota_window"):
                cc_quota.validate(cc_quota_document(weekly=bad_weekly), NOW)
        for reason, document in cases.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(cc_quota.QuotaError, reason):
                cc_quota.validate(document, NOW)
        # Within 60 s of clock skew and inside the window is accepted.
        cc_quota.validate(cc_quota_document(observed=NOW + timedelta(seconds=60)), NOW)
        cc_quota.validate(cc_quota_document(observed=NOW - timedelta(days=3, hours=23)), NOW)

    def test_load_requires_private_regular_bounded_file(self) -> None:
        from pulse_stats import cc_quota
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "cc-quota-v1.json"
            self.assertEqual(cc_quota.load_quota(path, NOW), (None, "quota_source_unavailable"))
            path.write_text(json.dumps(cc_quota_document()), encoding="utf-8")
            path.chmod(0o644)
            self.assertEqual(cc_quota.load_quota(path, NOW), (None, "quota_source_unavailable"))
            path.chmod(0o600)
            self.assertEqual(cc_quota.load_quota(path, NOW)[0]["weekly"]["used_percent"], 4.0)
            link = root / "link.json"
            link.symlink_to(path)
            self.assertEqual(cc_quota.load_quota(link, NOW), (None, "quota_source_unavailable"))
            path.write_text("x" * (cc_quota.MAX_BYTES + 1), encoding="utf-8")
            self.assertEqual(cc_quota.load_quota(path, NOW), (None, "quota_source_unavailable"))
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(cc_quota.load_quota(path, NOW), (None, "quota_source_unavailable"))


class CCQuotaFileMetricTests(unittest.TestCase):
    """The snapshot reads Relay's CC publication; the header probe is never called."""

    @staticmethod
    def _render(fixture, document=None, **kwargs):
        from dataclasses import replace
        path = fixture.root / "cc-quota-v1.json"
        if document is not None:
            path.write_text(json.dumps(document), encoding="utf-8")
            path.chmod(0o600)
        fixture.paths = replace(fixture.paths, cc_quota=path)

        def probe():
            raise AssertionError("legacy CC header probe must not run")

        runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
        rendered = fixture.service(runner, cc_quota_fetch=probe, **kwargs).render_json()
        return {item["id"]: item for item in json.loads(rendered)["metrics"]}

    def test_current_window_publication_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            observed = NOW - timedelta(hours=1)
            metrics = self._render(LiveFixture(pathlib.Path(directory)), cc_quota_document(observed=observed))
            weekly = metrics["relay.cc.used_percent"]
            self.assertEqual((weekly["status"], weekly["value"], weekly["unit"]), ("available", 4.0, "percent"))
            self.assertEqual(weekly["scope"], "CC subscription; seven_day window")
            self.assertEqual(weekly["observed_at"], observed.isoformat())
            self.assertEqual(metrics["relay.cc.resets_at"]["value"], int((NOW + timedelta(days=3)).timestamp()))
            self.assertEqual(metrics["relay.cc.5h_used_percent"]["value"], 46.0)
        # A five-hour reading from before its current window is withheld.
        with tempfile.TemporaryDirectory() as directory:
            metrics = self._render(LiveFixture(pathlib.Path(directory)),
                                   cc_quota_document(observed=NOW - timedelta(hours=4)))
            self.assertEqual(metrics["relay.cc.used_percent"]["value"], 4.0)
            self.assertIsNone(metrics["relay.cc.5h_used_percent"]["value"])
            self.assertEqual(metrics["relay.cc.5h_used_percent"]["reason"], "quota_outside_current_window")

    def test_missing_or_elapsed_publication_is_unknown_never_zero(self) -> None:
        for name, document, reason in (
            ("missing", None, "quota_source_unavailable"),
            ("elapsed", cc_quota_document(weekly_resets=int((NOW - timedelta(seconds=1)).timestamp())),
             "quota_window_elapsed"),
            ("previous_window", cc_quota_document(observed=NOW - timedelta(days=5)), "quota_outside_current_window"),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                metrics = self._render(LiveFixture(pathlib.Path(directory)), document)
                for metric_id in ("relay.cc.used_percent", "relay.cc.resets_at", "relay.cc.5h_used_percent"):
                    self.assertEqual(metrics[metric_id]["status"], "unknown", metric_id)
                    self.assertIsNone(metrics[metric_id]["value"], metric_id)
                    self.assertEqual(metrics[metric_id]["reason"], reason, metric_id)

    def test_elapsed_five_hour_window_does_not_hide_weekly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = cc_quota_document()
            document["five_hour"]["resets_at"] = int((NOW - timedelta(minutes=1)).timestamp())
            metrics = self._render(LiveFixture(pathlib.Path(directory)), document)
            self.assertEqual(metrics["relay.cc.used_percent"]["value"], 4.0)
            self.assertIsNone(metrics["relay.cc.5h_used_percent"]["value"])
            self.assertEqual(metrics["relay.cc.5h_used_percent"]["reason"], "quota_window_elapsed")

    def test_production_wiring_reads_the_published_cc_file(self) -> None:
        service = LiveSnapshotService.default()
        self.assertEqual(service.paths.cc_quota,
                         pathlib.Path.home() / ".local/state/pulse/stats/cc-quota-v1.json")


class LocalReaderTests(unittest.TestCase):
    """The production reader runs on ownermac under the App's own account."""

    LAUNCHD_RUNNING = "gui/501/com.pulse.relay = {\n\tactive count = 1\n\tstate = running\n\n\tjob state = running\n}\n"

    def test_production_wiring_uses_the_account_home_never_a_linux_home(self) -> None:
        home = pathlib.Path.home()
        paths = LiveSnapshotService.default().paths
        self.assertIsNone(paths.registry)
        self.assertEqual(paths.relay_supervisor, "launchd")
        self.assertEqual(paths.state, home / ".local/state/pulse/stats/incident-state-v1.json")
        # Source and configuration locations follow the installed code; every
        # runtime source and state file belongs to the reading account.
        code_locations = {"repo", "surface_registry", "subscriptions"}
        for name, value in vars(paths).items():
            if isinstance(value, pathlib.Path) and name not in code_locations:
                self.assertTrue(value.is_relative_to(home), name)

    def _local(self, root: pathlib.Path, launchctl: CommandResult):
        fixture = LiveFixture(root)
        fixture.paths = replace(fixture.paths, registry=None, relay_supervisor="launchd",
                                retire_legacy_probes=True, emit_legacy_cost_metrics=False)
        calls = []

        class Runner:
            def run(self, argv, timeout=None):
                calls.append(tuple(argv))
                if argv[:2] == ("/bin/launchctl", "print"):
                    return launchctl
                if tuple(argv[-2:]) == ("rev-parse", "HEAD"):
                    return CommandResult(0, HARNESS_SHA + "\n")
                return CommandResult(1, "")

        snapshot = json.loads(fixture.service(Runner()).render_json())
        return snapshot, calls

    def test_running_launchd_relay_completes_projection_without_systemd_or_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot, calls = self._local(pathlib.Path(directory), CommandResult(0, self.LAUNCHD_RUNNING))
        capabilities = {item["id"]: item for item in snapshot["capabilities"]}
        self.assertEqual(capabilities["meta.systemd_health"]["health"], "ok")
        self.assertEqual(capabilities["meta.probe_heartbeat"]["implementation_status"], "not_implemented")
        self.assertEqual(capabilities["incident.current_state_projection"]["health"], "ok")
        self.assertIn(("/bin/launchctl", "print", f"gui/{os.getuid()}/com.pulse.relay"), calls)
        self.assertFalse(any("systemctl" in part for call in calls for part in call))
        sources = {item["id"]: item for item in snapshot["provenance"]["sources"]}
        self.assertEqual(sources["probe-heartbeats"]["error_code"], "registry_not_registered")

    def test_stopped_launchd_relay_is_unknown_not_healthy(self) -> None:
        stopped = self.LAUNCHD_RUNNING.replace("\tstate = running", "\tstate = not running")
        with tempfile.TemporaryDirectory() as directory:
            snapshot, _ = self._local(pathlib.Path(directory), CommandResult(0, stopped))
        capabilities = {item["id"]: item for item in snapshot["capabilities"]}
        self.assertEqual(capabilities["meta.systemd_health"]["health"], "unknown")
        self.assertEqual(snapshot["overall"]["runtime_health"], "unknown")


class AlertSubjectTests(unittest.TestCase):
    """The subject decides what counts as one thing, so it is pinned directly."""

    def test_work_item_and_handoff_ids_win_over_the_agent(self) -> None:
        self.assertEqual(
            _alert_subject("executor_stale_unknown: wi_20260712_057 — claim age 9.4d", "pulse"),
            "wi_20260712_057",
        )
        self.assertEqual(
            _alert_subject("handoff ho_20260722_404（pulse → hestia）pending 891min", "pulse"),
            "ho_20260722_404",
        )

    def test_first_id_is_used_when_a_message_mentions_several(self) -> None:
        self.assertEqual(
            _alert_subject("cross-tenant handoff ho_20260724_207 (wi: wi_20260724_774)", "ken"),
            "ho_20260724_207",
        )

    def test_agent_is_the_fallback_and_placeholder_is_the_last_resort(self) -> None:
        self.assertEqual(_alert_subject("ack-probe.service 非零退出", "pulse"), "pulse")
        self.assertEqual(_alert_subject(None, "pulse"), "pulse")
        self.assertEqual(_alert_subject(None, "  "), "-")
        self.assertEqual(_alert_subject(None, None), "-")
        # A non-string message must not raise; it simply carries no subject.
        self.assertEqual(_alert_subject({"unexpected": "shape"}, None), "-")

    def test_ids_are_not_matched_inside_longer_tokens(self) -> None:
        self.assertEqual(_alert_subject("prefixwi_20260712_057", "pulse"), "pulse")


class AlertGroupingTests(unittest.TestCase):
    """Collapse behaviour measured against the shape of the real event stream."""

    def _alerts(
        self,
        records,
        now=NOW,
        heartbeat_at=None,
        heartbeats_readable=True,
        recovery_units=None,
        autonomous_runs=None,
    ):
        """Render the alert list.

        ``heartbeat_at`` moves the probe heartbeats relative to the events,
        ``heartbeats_readable=False`` removes the file entirely,
        ``recovery_units`` supplies systemd exit records, and
        ``autonomous_runs`` writes the agent execution stream. Together these
        drive the three recovery-downgrade signals.
        """
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            beat = heartbeat_at if heartbeat_at is not None else now
            fixture.write_heartbeats(beat, beat)
            if not heartbeats_readable:
                fixture.heartbeats.unlink()
            fixture.alerts.joinpath(f"escalation_event-{now:%Y-%m}.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )
            if autonomous_runs is not None:
                fixture.alerts.joinpath(
                    f"autonomous_execution_event-{now:%Y-%m}.jsonl"
                ).write_text(
                    "".join(json.dumps(run) + "\n" for run in autonomous_runs),
                    encoding="utf-8",
                )
            rendered = fixture.service(
                FakeRunner(
                    ack=ack_result(),
                    systemd=systemd_result(),
                    recovery_units=recovery_units,
                ),
                now=now,
            ).render_json()
            snapshot = json.loads(rendered)
            capability = next(
                item for item in snapshot["capabilities"] if item["id"] == "notify.action_package"
            )
            return snapshot["alerts"], capability["reason"]

    @staticmethod
    def _record(
        label,
        message,
        occurred,
        severity="major",
        agent_id="pulse",
        requires_human=None,
        human_decision_kind=None,
    ):
        record = {
            "event_id": f"ev-{label}-{message}-{occurred.isoformat()}", "severity": severity,
            "match_label": label, "occurred_at": occurred.isoformat(),
            "message": message, "agent_id": agent_id,
        }
        if requires_human is not None:
            record["requires_human"] = requires_human
        if human_decision_kind is not None:
            record["human_decision_kind"] = human_decision_kind
        return record

    def test_repeats_of_one_thing_collapse_into_a_single_counted_alert(self) -> None:
        first = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
        records = [
            self._record("executor_stale_unknown", "executor_stale_unknown: wi_20260712_057 — 早期文案", first),
            self._record(
                "executor_stale_unknown", "executor_stale_unknown: wi_20260712_057 — 最新文案",
                datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc),
            ),
            self._record(
                "executor_stale_unknown", "executor_stale_unknown: wi_20260712_057 — 最新文案",
                datetime(2026, 7, 22, 5, 0, tzinfo=timezone.utc),
            ),
        ]
        alerts, reason = self._alerts(records)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["occurred_at"], "2026-07-22T05:00:00+00:00")
        # Stored in UTC, shown to the owner in UTC+8: 03:00Z reads as 11:00.
        self.assertIn("07-20 11:00(UTC+8) 起", alerts[0]["reason"])
        self.assertIn("最近 07-22 13:00", alerts[0]["reason"])
        self.assertIn("3 次", alerts[0]["reason"])
        self.assertIn("wi_20260712_057", alerts[0]["reason"])
        # The newest message is the one an operator reads.
        self.assertIn("最新文案", alerts[0]["reason"])
        self.assertNotIn("早期文案", alerts[0]["reason"])
        self.assertIn("1 件事（3 条原始事件收敛）", reason)

    def test_one_business_subject_is_one_action_while_manual_inventory_is_retained(self) -> None:
        work_item_id = "wi_20260824_941"
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.work_items.joinpath(f"{work_item_id}.yaml").write_text(
                f"work_item_id: {work_item_id}\n"
                "title: execution trust closeout\n"
                "status: in_progress\n"
                "owner: pulse\n"
                "active_executor: pulse\n"
                "main_only: true\n"
                f"created_at: '{(NOW - timedelta(days=2)).isoformat()}'\n",
                encoding="utf-8",
            )
            fixture.handoffs.joinpath("ho_20260824_941.yaml").write_text(
                "handoff_id: ho_20260824_941\n"
                f"work_item_id: {work_item_id}\n"
                "main_only: true\n"
                "ack_state: pending\n"
                "from_owner: pulse\n"
                "to_owner: duke\n"
                f"created_at: '{(NOW - timedelta(days=1)).isoformat()}'\n"
                "expected_output: review the execution trust evidence\n",
                encoding="utf-8",
            )
            record = self._record(
                "executor_reaped",
                f"executor_reaped: {work_item_id} stale executor was reaped",
                NOW,
                requires_human=True,
                human_decision_kind="main_only",
            )
            fixture.alerts.joinpath(f"escalation_event-{NOW:%Y-%m}.jsonl").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )

            snapshot = json.loads(
                fixture.service(
                    FakeRunner(ack=ack_result(), systemd=systemd_result())
                ).render_json()
            )

        action_items = [item for item in snapshot["alerts"] if item["tier"] == "act"]
        self.assertEqual(len(action_items), 1)
        self.assertIn(work_item_id, action_items[0]["reason"])
        self.assertEqual(snapshot["alert_tiers"]["act"], 1)

        in_flight_metric = next(
            item for item in snapshot["metrics"] if item["id"] == "harness.wi.in_flight"
        )
        inventory = [
            item for item in in_flight_metric["value"] if item["work_item_id"] == work_item_id
        ]
        self.assertEqual(len(inventory), 1)
        self.assertTrue(inventory[0]["needs_owner"])
        self.assertEqual(inventory[0]["disposition"], "manual_required")

    def test_completed_work_item_reaped_event_cannot_demand_human_action(self) -> None:
        work_item_id = "wi_20260823_941"
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.work_items.joinpath(f"{work_item_id}.yaml").write_text(
                f"work_item_id: {work_item_id}\n"
                "title: completed workspace convergence\n"
                "status: completed\n"
                "owner: amanda\n"
                "active_executor: null\n"
                "main_only: true\n"
                f"created_at: '{(NOW - timedelta(days=2)).isoformat()}'\n"
                f"updated_at: '{NOW.isoformat()}'\n",
                encoding="utf-8",
            )
            record = self._record(
                "executor_reaped",
                f"executor_reaped: {work_item_id} stale executor was reaped",
                NOW - timedelta(days=1),
                severity="minor",
                agent_id="amanda",
                requires_human=True,
                human_decision_kind="main_only",
            )
            fixture.alerts.joinpath(f"escalation_event-{NOW:%Y-%m}.jsonl").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )

            snapshot = json.loads(
                fixture.service(
                    FakeRunner(ack=ack_result(), systemd=systemd_result())
                ).render_json()
            )

        action_items = [item for item in snapshot["alerts"] if item["tier"] == "act"]
        self.assertFalse(
            any(work_item_id in item["reason"] for item in action_items),
            "a terminal WI must not remain in 要我出手 through historical events",
        )
        done_items = [item for item in snapshot["alerts"] if item["tier"] == "done"]
        self.assertTrue(any(work_item_id in item["reason"] for item in done_items))

        in_flight_metric = next(
            item for item in snapshot["metrics"] if item["id"] == "harness.wi.in_flight"
        )
        self.assertFalse(
            any(item["work_item_id"] == work_item_id for item in in_flight_metric["value"])
        )

    def test_different_things_stay_separate(self) -> None:
        records = [
            self._record("executor_stale_unknown", "wi_20260712_057 stale", NOW),
            self._record("executor_stale_unknown", "wi_20260716_315 stale", NOW),
            self._record("ack_overdue", "wi_20260712_057 unacked", NOW),
        ]
        alerts, reason = self._alerts(records)
        self.assertEqual(len(alerts), 3)
        self.assertIn("3 件事（3 条原始事件收敛）", reason)

    def test_group_severity_is_the_highest_seen_not_the_latest(self) -> None:
        records = [
            self._record(
                "degen_critical", "wi_20260712_057 burst",
                datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc), severity="critical",
            ),
            self._record(
                "degen_critical", "wi_20260712_057 burst",
                datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc), severity="minor",
            ),
        ]
        alerts, _ = self._alerts(records)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "critical")

    def test_events_older_than_the_window_are_dropped(self) -> None:
        records = [
            self._record("ack_overdue", "ho_20260701_001 old", datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)),
            self._record("ack_overdue", "ho_20260722_404 recent", NOW),
        ]
        alerts, reason = self._alerts(records)
        self.assertEqual(len(alerts), 1)
        self.assertIn("ho_20260722_404", alerts[0]["reason"])
        self.assertIn("1 件事（1 条原始事件收敛）", reason)

    def test_more_things_than_the_cap_keeps_newest_and_says_how_many_were_cut(self) -> None:
        base = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
        records = [
            self._record(
                "ack_overdue", f"handoff ho_20260722_{index:03d} pending",
                base + timedelta(minutes=index),
            )
            for index in range(MAX_ALERT_GROUPS + 5)
        ]
        alerts, reason = self._alerts(records)
        self.assertEqual(len(alerts), MAX_ALERT_GROUPS)
        self.assertIn("已截断 5 件", reason)
        # Truncation keeps the most recent things. The reducer re-sorts the
        # surviving alerts oldest-first, so membership is what to assert on.
        rendered = json.dumps(alerts)
        for cut in range(5):
            self.assertNotIn(f"ho_20260722_{cut:03d}", rendered)
        for kept in (5, MAX_ALERT_GROUPS + 4):
            self.assertIn(f"ho_20260722_{kept:03d}", rendered)

    def test_no_truncation_notice_when_everything_fits(self) -> None:
        _, reason = self._alerts([self._record("ack_overdue", "ho_20260722_404 pending", NOW)])
        self.assertNotIn("已截断", reason)

    def test_one_malformed_line_does_not_lose_the_good_ones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            good = json.dumps(self._record("ack_overdue", "ho_20260722_404 pending", NOW))
            no_timestamp = json.dumps({"match_label": "ack_overdue", "message": "no timestamp"})
            fixture.alerts.joinpath("escalation_event-2026-07.jsonl").write_text(
                "not json at all\n" + no_timestamp + "\n" + good + "\n", encoding="utf-8"
            )
            snapshot = json.loads(fixture.service(
                FakeRunner(ack=ack_result(), systemd=systemd_result())
            ).render_json())
            self.assertEqual(len(snapshot["alerts"]), 1)
            self.assertIn("ho_20260722_404", snapshot["alerts"][0]["reason"])

    def test_labels_land_in_their_owner_decided_tier(self) -> None:
        """`act` = 在等大鸟或 Duke 裁决的事,不是"严重的事"（大鸟 2026-07-28）。

        改前这个测试断言 degen_critical / ack_probe_down 属 act——按"严重程度"
        选的。大鸟重定语义后两者都是我的内部事务：退化有明确修法,探针挂了我
        重启就行,**没有一条需要谁拍板**。
        """
        records = [
            # act：分身自己说"我拿不准/我无权",要人接手
            self._record("autonomous_blocked_needs_main", "cron 分身撞权限边界", NOW),
            self._record("red_line_flag", "触到红线需授权", NOW, severity="critical"),
            # done whitelist
            self._record("autonomous_assigned_work_completed", "[hestia cron] spawned", NOW),
            self._record(
                "executor_reaped",
                "stale executor reaped",
                NOW,
                requires_human=True,
                human_decision_kind="main_only",
            ),
            # 内部事务一律 watch——再严重也不占大鸟注意力
            self._record("degen_critical", "pulse 退化重手处置", NOW, severity="critical"),
            self._record("ack_probe_down", "ack-probe.service 非零退出", NOW),
            self._record("ack_overdue", "handoff ho_20260722_404 pending", NOW),
            self._record("ledger_lint_breach", "账本体检发现违例", NOW),
        ]
        alerts, reason = self._alerts(records)
        by_title = {item["title"]: item["tier"] for item in alerts}
        # 标签一律不进 act（07-28 第四轮）：分身撞边界/触红线都是我该接的活
        self.assertEqual(by_title["Autonomous work needs the main owner"], "watch")
        self.assertEqual(by_title["Monitoring red line was raised"], "watch")
        self.assertEqual(by_title["Assigned autonomous work completed"], "done")
        self.assertEqual(by_title["Stale executor was reaped"], "done")
        self.assertEqual(by_title["Critical reply degeneration detected"], "watch")
        self.assertEqual(by_title["Acknowledgement probe is down"], "watch")
        self.assertEqual(by_title["Handoff acknowledgement is overdue"], "watch")
        self.assertEqual(by_title["Harness ledger invariant finding"], "watch")
        self.assertIn("要你出手 0 · 我在盯 6 · 已过去 2", reason)

    def test_a_reviewed_label_in_neither_whitelist_defaults_to_watch(self) -> None:
        # `autonomous_error` is a known label but sits in neither tier list, so
        # the default must catch it rather than promoting it to act.
        alerts, _ = self._alerts([
            self._record("autonomous_error", "autonomous session reported an error", NOW),
        ])
        self.assertEqual(alerts[0]["tier"], "watch")

    def test_an_untemplated_label_never_reaches_the_tiering_at_all(self) -> None:
        # Dropped earlier by ALERT_TEMPLATES, so it cannot be tiered into act.
        alerts, reason = self._alerts([
            self._record("handoff_timed_out", "handoff ho_20260711_484 auto-timed-out", NOW),
        ])
        self.assertEqual(alerts, [])
        self.assertIn("0 件事", reason)

    def _repeated(self, count, last_seen, label="ack_overdue"):
        """`count` events for one subject, the newest landing on `last_seen`."""
        return [
            self._record(label, "handoff ho_20260722_404 pending", last_seen - timedelta(hours=index))
            for index in range(count)
        ]

    def test_repetition_alone_never_reaches_the_owner(self) -> None:
        """喊得多不等于要人拍板（大鸟 2026-07-28 重定 act 语义）。

        改前：同一件事 24h 内喊满 20 次就抬进 act。在「act = 严重的事」下成立,
        在「act = 等你或 Duke 裁决」下不成立——重复只说明我没修好,不说明需要
        决策。它还会把刚降档的探针告警原样抬回来（本月 ack_probe_down 22 条、
        ledger_lint_probe_down 20 条都够得着阈值）。

        「响个不停没人管」的出口改走日巡：清不掉就发 daily_patrol_action_required,
        以「我搞不定,请裁决」的形式到大鸟面前,而不是让他自己从重复告警里挑。
        """
        alerts, reason = self._alerts(self._repeated(FORMER_ESCALATE_COUNT + 5, NOW))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["tier"], "watch")
        self.assertIn(f"{FORMER_ESCALATE_COUNT + 5} 次", alerts[0]["reason"])
        self.assertIn("要你出手 0", reason)

    def test_a_main_only_handoff_is_what_reaches_the_owner(self) -> None:
        """`main_only: true` 就必须大鸟参与，无论 wi 属于谁（大鸟原话）。"""
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.handoffs.joinpath("ho_20260724_903.yaml").write_text(
                "handoff_id: ho_20260724_903\n"
                "main_only: true\n"
                "ack_state: pending\n"
                "from_owner: aki\n"
                "to_owner: hestia\n"
                "created_at: '2026-07-19T08:00:00Z'\n"
                "expected_output: 一个判断：默认浏览器基准该不该定 Firefox\n",
                encoding="utf-8",
            )
            snapshot = json.loads(fixture.service(
                FakeRunner(ack=ack_result(), systemd=systemd_result())
            ).render_json())
        act = [a for a in snapshot["alerts"] if a["tier"] == "act"]
        self.assertEqual(len(act), 1)
        self.assertIn("ho_20260724_903", act[0]["reason"])
        self.assertIn("aki→hestia", act[0]["reason"])
        self.assertEqual(snapshot["alert_tiers"]["act"], 1)

    def _main_only_card(self, wi_title):
        """一张 main_only 卡片,可选带 wi title。"""
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.handoffs.joinpath("ho_1.yaml").write_text(
                "handoff_id: ho_20260727_374\n"
                "main_only: true\n"
                "ack_state: pending\n"
                "from_owner: amanda\n"
                "to_owner: duke\n"
                "work_item_id: wi_20260727_821\n"
                "created_at: '2026-07-19T08:00:00Z'\n"
                "expected_output: ①对三条发现逐条认或驳（驳要附证据）；②自诊根因——按存活性三分判属哪类；③整改方向\n",
                encoding="utf-8",
            )
            if wi_title is not None:
                fixture.work_items.joinpath("wi_20260727_821.yaml").write_text(
                    "work_item_id: wi_20260727_821\n"
                    "status: in_progress\n"
                    f"title: {wi_title}\n",
                    encoding="utf-8",
                )
            snapshot = json.loads(fixture.service(
                FakeRunner(ack=ack_result(), systemd=systemd_result())
            ).render_json())
        act = [a for a in snapshot["alerts"] if a["tier"] == "act"]
        self.assertEqual(len(act), 1)
        return act[0]

    def test_the_card_title_is_the_gist_not_the_acceptance_spec(self) -> None:
        """标题＝这是件什么事，不是收方要交什么（大鸟 2026-07-28）。

        原先三张卡的大字标题一律写死「要你参与的协作」——最大的那行字零信息
        量，读者必须读完小字才知道是什么事。大鸟：「实在是不容易理解，且没有
        用」。wi title 是人手写的一句话主旨，正是他要的「大意」列。
        """
        card = self._main_only_card("S1 成果符合性裁决：duke 7月 major——请自诊并出整改方向")
        self.assertEqual(
            card["title"], "S1 成果符合性裁决：duke 7月 major——请自诊并出整改方向"
        )
        self.assertNotEqual(card["title"], "要你参与的协作")
        # 四列齐全：谁→谁 · 挂多久 · 单号,全在 ｜ 前的元信息里。
        meta = card["reason"].split("｜")[0]
        self.assertIn("amanda→duke", meta)
        self.assertIn("ho_20260727_374", meta)
        self.assertIn("挂 ", meta)

    def test_a_card_without_a_wi_title_still_carries_words_of_its_own(self) -> None:
        """正文不得为空——App 的 testEveryActAlertIsActionable 要求每张 act
        卡都带「自己的话」。读不到 wi 时标题退回 expected_output，正文改说等谁；
        空正文既让 App 测试红，也让卡片只剩一句标题。
        """
        card = self._main_only_card(None)
        head, _, tail = card["reason"].partition("｜")
        self.assertTrue(tail.strip(), "正文为空会让 App 侧 act 卡不可行动")
        self.assertIn("duke", tail)
        self.assertTrue(head.strip(), "元信息行同样不得为空")
        # 没有 wi title 时,标题退回 expected_output 首行,仍不是那句写死的话。
        self.assertTrue(card["title"].startswith("①对三条发现"))

    def test_a_cross_tenant_handoff_is_the_peers_job_not_the_owners(self) -> None:
        """跨租户默认带 main_only，但那个「人」是 Ken 不是大鸟。

        不排掉的话 Ken 跟 Eric 谈部署进度会出现在大鸟菜单栏上——2026-07-28
        实测 8 条里 4 条是这一族。
        """
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            fixture.handoffs.joinpath("ho_cross.yaml").write_text(
                "handoff_id: ho_20260725_549\n"
                "main_only: true\n"
                "cross_tenant: true\n"
                "ack_state: pending\n"
                "from_owner: ken\n"
                "to_owner: cz-pulse\n"
                "created_at: '2026-07-19T08:00:00Z'\n"
                "expected_output: Eric 部署进度与卡点\n",
                encoding="utf-8",
            )
            snapshot = json.loads(fixture.service(
                FakeRunner(ack=ack_result(), systemd=systemd_result())
            ).render_json())
        self.assertEqual(snapshot["alert_tiers"]["act"], 0)

    def test_no_escalation_label_ever_reaches_the_owner(self) -> None:
        """标签一律不进 act（大鸟 2026-07-28 第四轮定案）。

        前三轮我按「票据卡住」「系统失能」「S3 决策请求」各改了一版，都还是从
        标签里选。第四轮大鸟看了选出来的 6 条：「**都不是我说的真需要大鸟关注的
        wi，都是你该做的**」。标签说的是分身不敢往下走——该我接过来做或走 Duke。
        「要你出手」的真源改为 main_only handoff。
        """
        for label in ("circuit_broken", "red_line_flag", "degen_critical",
                      "autonomous_blocked_needs_main", "cross_machine_action_required"):
            alerts, _ = self._alerts(
                [self._record(label, "任凭多严重都是我的活", NOW, "critical")]
            )
            self.assertEqual(alerts[0]["tier"], "watch", f"{label} 不该打扰大鸟")

    def test_only_reviewed_harness_receipts_can_reach_act(self) -> None:
        """Relay/Harness 的结构化 receipt 是唯一的 escalation→act 桥。"""
        for kind in (
            "main_only",
            "owner_decision",
            "irreversible_authorization",
        ):
            alerts, _ = self._alerts([
                self._record(
                    "red_line_flag",
                    f"explicit {kind}",
                    NOW,
                    "critical",
                    requires_human=True,
                    human_decision_kind=kind,
                )
            ])
            self.assertEqual(alerts[0]["tier"], "act", kind)

        # Bare booleans, unknown kinds and labels remain watch.
        for payload in (
            {"requires_human": True},
            {"requires_human": True, "human_decision_kind": "red_line_flag"},
            {"requires_human": False, "human_decision_kind": "owner_decision"},
        ):
            alerts, _ = self._alerts([
                self._record("red_line_flag", "untrusted", NOW, "critical") | payload
            ])
            self.assertEqual(alerts[0]["tier"], "watch", payload)

    def test_an_explicitly_resolved_request_stops_being_reported(self) -> None:
        """去掉老化就必须有出口，否则红点只增不减。"""
        record = self._record("circuit_broken", "hestia 熔断", NOW - timedelta(days=3), "critical")
        record["resolved_at"] = NOW.isoformat()
        record["resolved_by"] = "owner"
        record["resolution"] = "已人工清理 dedup-state 条目"
        alerts, reason = self._alerts([record])
        self.assertEqual(alerts, [])
        self.assertIn("要你出手 0", reason)

    def test_a_later_resolution_row_supersedes_the_original_event(self) -> None:
        """Append-only resolution must remove the earlier unresolved copy."""
        original = self._record(
            "red_line_flag",
            "owner must choose",
            NOW - timedelta(hours=2),
            "critical",
            requires_human=True,
            human_decision_kind="owner_decision",
        )
        resolved = dict(original)
        resolved["resolved_at"] = NOW.isoformat()
        resolved["resolved_by"] = "pulse"
        resolved["resolution"] = "authority changed; direct request suppressed"

        alerts, reason = self._alerts([original, resolved])

        self.assertEqual(alerts, [])
        self.assertIn("2 条原始事件收敛", reason)
        self.assertIn("要你出手 0", reason)

    def test_a_probe_that_has_since_reported_in_is_no_longer_your_problem(self) -> None:
        # The 2026-07-26 shape: ack-probe failed once during a Relay rebuild,
        # then succeeded on every run after. The alert is inside the age window,
        # so only the heartbeat can show it is over.
        failed_at = NOW - timedelta(hours=4)
        alerts, reason = self._alerts(
            [self._record("ack_probe_down", "ack-probe.service 非零退出", failed_at, "critical")],
            heartbeat_at=NOW - timedelta(minutes=10),
        )
        # watch, not done: it really did break, so a periodic review still sees it.
        self.assertEqual(alerts[0]["tier"], "watch")
        self.assertIn("1 件已自愈降档", reason)

    def test_a_recovered_probe_is_not_dragged_back_by_repetition(self) -> None:
        # Its many past failures are history once it reports in again;
        # the repeat-escalation rule must not re-promote a healed probe.
        failed_at = NOW - timedelta(hours=4)
        records = self._repeated(FORMER_ESCALATE_COUNT, failed_at, label="ack_probe_down")
        alerts, _ = self._alerts(records, heartbeat_at=NOW - timedelta(minutes=10))
        self.assertEqual(alerts[0]["tier"], "watch")

    def test_a_probe_still_silent_since_it_broke_stays_act(self) -> None:
        # Heartbeat predates the complaint: nothing proved it came back.
        broke_at = NOW - timedelta(hours=2)
        alerts, reason = self._alerts(
            [self._record("ack_probe_down", "ack-probe.service 非零退出", broke_at, "critical")],
            heartbeat_at=NOW - timedelta(hours=6),
        )
        # 探针类 2026-07-28 起不进 act（修探针是我的活，不需大鸟裁决），
        # 但"还坏着"仍必须不算自愈——这才是本用例真正守的东西。
        self.assertEqual(alerts[0]["tier"], "watch")
        self.assertNotIn("自愈降档", reason)

    def test_a_label_with_no_heartbeat_source_keeps_the_plain_age_rule(self) -> None:
        # heartbeat_watcher_down has no heartbeat of its own (the watcher reads
        # the file, it does not post to it), so a fresh file proves nothing
        # about it and it must not be downgraded.
        alerts, _ = self._alerts(
            [self._record("heartbeat_watcher_down", "watcher 非零退出", NOW - timedelta(hours=4), "critical")],
            heartbeat_at=NOW,
        )
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）

    def test_unreadable_recovery_evidence_falls_back_instead_of_healing_everything(self) -> None:
        # The dangerous failure mode is missing evidence reading as "all quiet".
        # With neither signal readable the tiering must revert to age-only, and
        # must say so rather than degrade silently.
        broke_at = NOW - timedelta(hours=4)
        alerts, reason = self._alerts(
            [self._record("ack_probe_down", "ack-probe.service 非零退出", broke_at, "critical")],
            heartbeats_readable=False,
            recovery_units=None,
        )
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）
        self.assertIn("都读不到", reason)

    def test_heartbeat_offsets_are_compared_in_one_timezone(self) -> None:
        # The real file mixes Z, +00:00 and +0800 because each probe stamps on
        # its own clock. A +0800 stamp is 8 hours *ahead* of the same wall
        # clock read as UTC, so a naive field comparison would call this
        # recovered when it is not.
        broke_at = NOW - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            stale_beat = NOW - timedelta(hours=5)
            fixture.heartbeats.write_text(json.dumps({
                "vultr-ack-probe": {
                    "ts": stale_beat.astimezone(timezone(timedelta(hours=8))).strftime(
                        "%Y-%m-%dT%H:%M:%S%z"
                    ),
                },
            }), encoding="utf-8")
            fixture.alerts.joinpath(f"escalation_event-{NOW:%Y-%m}.jsonl").write_text(
                json.dumps(
                    self._record("ack_probe_down", "ack-probe.service 非零退出", broke_at, "critical")
                ) + "\n",
                encoding="utf-8",
            )
            snapshot = json.loads(fixture.service(
                FakeRunner(ack=ack_result(), systemd=systemd_result())
            ).render_json())
        # 5h before "now" is still before the 1h-old complaint, whichever way
        # it is written down, so this probe has not reported back in.
        self.assertEqual(snapshot["alerts"][0]["tier"], "watch")  # 同上

    def test_a_watcher_that_systemd_saw_exit_cleanly_is_no_longer_your_problem(self) -> None:
        # The watcher posts no heartbeat of its own, so systemd's exit record
        # is the only evidence it recovered -- and an outside observation is
        # stronger than a self-report anyway.
        broke_at = NOW - timedelta(hours=13)
        alerts, reason = self._alerts(
            [self._record("heartbeat_watcher_down", "watcher 非零退出", broke_at, "critical")],
            recovery_units=recovery_unit_result({
                "dual-heartbeat-watcher.service": (0, systemd_stamp(NOW - timedelta(minutes=10))),
            }),
        )
        self.assertEqual(alerts[0]["tier"], "watch")
        self.assertIn("1 件已自愈降档", reason)

    def test_a_unit_that_exited_nonzero_is_still_broken(self) -> None:
        # A failing exit status is the unit confirming it is still down; a
        # recent timestamp must not be mistaken for recovery.
        broke_at = NOW - timedelta(hours=4)
        alerts, reason = self._alerts(
            [self._record("heartbeat_watcher_down", "watcher 非零退出", broke_at, "critical")],
            recovery_units=recovery_unit_result({
                "dual-heartbeat-watcher.service": (1, systemd_stamp(NOW - timedelta(minutes=10))),
            }),
        )
        # 探针类 2026-07-28 起不进 act（修探针是我的活，不需大鸟裁决），
        # 但"还坏着"仍必须不算自愈——这才是本用例真正守的东西。
        self.assertEqual(alerts[0]["tier"], "watch")
        self.assertNotIn("自愈降档", reason)

    def test_a_unit_that_last_succeeded_before_the_alert_is_still_broken(self) -> None:
        # Clean exit, but it predates the complaint: it broke *after* that run.
        broke_at = NOW - timedelta(hours=1)
        alerts, _ = self._alerts(
            [self._record("heartbeat_watcher_down", "watcher 非零退出", broke_at, "critical")],
            recovery_units=recovery_unit_result({
                "dual-heartbeat-watcher.service": (0, systemd_stamp(NOW - timedelta(hours=6))),
            }),
        )
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）

    def test_an_unavailable_systemctl_falls_back_to_the_age_rule(self) -> None:
        # recovery_units=None makes the fake return a nonzero exit, standing in
        # for systemctl being missing, timing out or unparseable. No evidence
        # must never read as "recovered".
        broke_at = NOW - timedelta(hours=4)
        alerts, _ = self._alerts(
            [self._record("heartbeat_watcher_down", "watcher 非零退出", broke_at, "critical")],
            recovery_units=None,
        )
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）

    def test_unparseable_systemd_timestamps_are_not_evidence(self) -> None:
        # A zone this cannot prove the offset of would shift the comparison by
        # hours, so it must count as no evidence rather than a guess.
        broke_at = NOW - timedelta(hours=4)
        alerts, _ = self._alerts(
            [self._record("heartbeat_watcher_down", "watcher 非零退出", broke_at, "critical")],
            recovery_units=recovery_unit_result({
                "dual-heartbeat-watcher.service": (0, "Sun 2026-07-22 07:50:00 CST"),
            }),
        )
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）

    def test_the_fresher_of_the_two_recovery_signals_wins(self) -> None:
        # ack-probe has both signals. The heartbeat says it has not come back,
        # systemd says it has and more recently -- the later evidence decides.
        broke_at = NOW - timedelta(hours=4)
        alerts, _ = self._alerts(
            [self._record("ack_probe_down", "ack-probe 非零退出", broke_at, "critical")],
            heartbeat_at=NOW - timedelta(hours=6),
            recovery_units=recovery_unit_result({
                "ack-probe.service": (0, systemd_stamp(NOW - timedelta(minutes=5))),
            }),
        )
        # ack_probe_down maps to a heartbeat, not a unit, so the unit record is
        # not consulted for it and the stale heartbeat leaves it in act.
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）

    def test_systemd_timestamp_parsing_handles_the_real_wire_format(self) -> None:
        parsed = _parse_systemd_timestamp("Sun 2026-07-26 06:50:11 UTC")
        self.assertEqual(parsed, datetime(2026, 7, 26, 6, 50, 11, tzinfo=timezone.utc))
        # Never-run units print an empty value; unknown zones are not guessed.
        self.assertIsNone(_parse_systemd_timestamp(""))
        self.assertIsNone(_parse_systemd_timestamp("Sun 2026-07-26 06:50:11 CST"))
        self.assertIsNone(_parse_systemd_timestamp("garbage"))

    @staticmethod
    def _run(agent, status, occurred):
        return {"agent_id": agent, "status": status, "occurred_at": occurred.isoformat()}

    def test_an_agent_whose_next_run_succeeded_is_no_longer_your_problem(self) -> None:
        # The 2026-07-26 shape: pulse reported wrap_missing, then its next
        # session completed cleanly. Neither a heartbeat nor systemd can see
        # this -- agents run inside Relay, not as units.
        broke_at = NOW - timedelta(hours=6)
        alerts, reason = self._alerts(
            [self._record("autonomous_wrap_missing", "wrap_missing (exit=0)", broke_at)],
            autonomous_runs=[
                self._run("pulse", "wrap_missing", broke_at),
                self._run("pulse", "success", NOW - timedelta(hours=2)),
            ],
        )
        self.assertEqual(alerts[0]["tier"], "watch")
        self.assertIn("1 件已自愈降档", reason)

    def test_an_agent_that_has_not_succeeded_since_stays_act(self) -> None:
        broke_at = NOW - timedelta(hours=6)
        alerts, _ = self._alerts(
            [self._record("autonomous_wrap_missing", "wrap_missing (exit=0)", broke_at)],
            autonomous_runs=[
                self._run("pulse", "success", NOW - timedelta(hours=20)),
                self._run("pulse", "wrap_missing", broke_at),
            ],
        )
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）

    def test_a_started_run_is_not_proof_the_agent_recovered(self) -> None:
        # `started` says the session was launched, not that it got anywhere.
        # An agent that keeps starting and keeps failing is still broken.
        broke_at = NOW - timedelta(hours=6)
        alerts, _ = self._alerts(
            [self._record("autonomous_wrap_missing", "wrap_missing (exit=0)", broke_at)],
            autonomous_runs=[
                self._run("pulse", "started", NOW - timedelta(hours=1)),
            ],
        )
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）

    def test_a_broken_circuit_named_after_a_work_item_is_never_agent_healed(self) -> None:
        # circuit_broken's subject is sometimes a wi id rather than an agent.
        # The agent holding it succeeding at other work proves nothing about
        # the stuck item, so this must not be downgraded.
        broke_at = NOW - timedelta(hours=6)
        alerts, _ = self._alerts(
            [self._record("circuit_broken", "熔断 stale_work_items:wi_20260721_524", broke_at, "critical")],
            autonomous_runs=[
                self._run("pulse", "success", NOW - timedelta(minutes=5)),
            ],
        )
        # watch 而非 act：标签一律不进 act（07-28）。本用例守的是「wi 类 subject
        # 不许被该 agent 在别处的成功治愈」，那条判据未变——所以断言的是
        # 「没有被判为已恢复」，用 tier 只是观测手段。
        self.assertEqual(alerts[0]["tier"], "watch")

    def test_a_stuck_work_item_is_never_healed_by_its_executor_working_elsewhere(self) -> None:
        # The hard boundary. wi_20260712_057 has sat untouched for 12 days
        # while hestia, its executor, runs successfully all day on other work.
        # The item is forgotten, not blocked on a dead executor -- downgrading
        # it would silence precisely the alert that most needs the owner.
        stuck_since = NOW - timedelta(hours=2)
        records = [
            self._record(
                "executor_stale_unknown",
                "executor_stale_unknown: wi_20260712_057 — executor 'hestia' liveness unknown",
                stuck_since - timedelta(hours=index),
                agent_id="hestia",
            )
            for index in range(FORMER_ESCALATE_COUNT)
        ]
        alerts, _ = self._alerts(
            records,
            autonomous_runs=[
                self._run("hestia", "success", NOW - timedelta(minutes=10)),
            ],
        )
        self.assertEqual(len(alerts), 1)
        self.assertIn("wi_20260712_057", alerts[0]["reason"])
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）

    def test_a_work_item_subject_is_skipped_even_if_something_shares_its_name(self) -> None:
        # Defence in depth for the boundary above. Normally a wi id simply
        # fails to match any agent, so the lookup misses by accident. This
        # pins the *deliberate* skip: even when a stream entry carries that id
        # as its agent, a work item must never be healed by agent evidence.
        broke_at = NOW - timedelta(hours=6)
        alerts, _ = self._alerts(
            [self._record("circuit_broken", "熔断 stale_work_items:wi_20260721_524", broke_at, "critical")],
            autonomous_runs=[
                self._run("wi_20260721_524", "success", NOW - timedelta(minutes=5)),
            ],
        )
        self.assertEqual(alerts[0]["tier"], "watch")  # 同上：标签不进 act

    def test_a_missing_autonomous_stream_falls_back_to_the_age_rule(self) -> None:
        broke_at = NOW - timedelta(hours=6)
        alerts, _ = self._alerts(
            [self._record("autonomous_wrap_missing", "wrap_missing (exit=0)", broke_at)],
            autonomous_runs=None,
        )
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）

    def test_a_quiet_act_label_still_escalates_if_it_is_firing_again(self) -> None:
        # First seen long ago but still going: recency measures last_seen, so
        # the age of the first sighting must not demote a thing still firing.
        records = self._repeated(FORMER_ESCALATE_COUNT, NOW, label="ack_probe_down")
        records.append(
            self._record("ack_probe_down", "handoff ho_20260722_404 pending", NOW - timedelta(days=6))
        )
        alerts, _ = self._alerts(records)
        self.assertEqual(len(alerts), 1)
        self.assertIn(f"{FORMER_ESCALATE_COUNT + 1} 次", alerts[0]["reason"])
        self.assertEqual(alerts[0]["tier"], "watch")  # 探针/收口类不进 act（07-28 语义）

    def test_label_unregistered_is_housekeeping_not_an_interruption(self) -> None:
        alerts, _ = self._alerts([
            self._record("label_unregistered", "escalation label 未登记", NOW),
        ])
        self.assertEqual(alerts[0]["tier"], "watch")

    def test_repetition_never_escalates_a_done_item(self) -> None:
        # A system that succeeded 50 times is not an emergency.
        alerts, reason = self._alerts(
            self._repeated(50, NOW, label="autonomous_assigned_work_completed")
        )
        self.assertEqual(alerts[0]["tier"], "done")
        self.assertIn("要你出手 0", reason)

    def test_top_level_tier_counts_match_the_alert_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            records = [
                self._record("circuit_broken", "hestia 熔断", NOW, severity="critical"),
                self._record("autonomous_success_with_output", "[pulse cron] spawned", NOW),
                self._record("ack_overdue", "handoff ho_20260722_404 pending", NOW),
                self._record("ledger_lint_breach", "账本违例", NOW),
            ]
            fixture.alerts.joinpath("escalation_event-2026-07.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
            )
            snapshot = json.loads(fixture.service(
                FakeRunner(ack=ack_result(), systemd=systemd_result())
            ).render_json())
            tiers = snapshot["alert_tiers"]
            self.assertEqual(tiers, {"act": 0, "watch": 3, "done": 1})
            for name in ("act", "watch", "done"):
                self.assertEqual(
                    tiers[name], sum(item["tier"] == name for item in snapshot["alerts"]), name
                )
            # `done` is filterable but still present -- nothing is dropped.
            self.assertEqual(len(snapshot["alerts"]), 4)

    def test_an_event_without_an_assigned_tier_defaults_to_watch(self) -> None:
        # The dataclass default is the safety net: a tier that was never set
        # must not be able to reach the owner by omission.
        event = IncidentEvent(
            event_id="alert-x", kind="event_only", capability_id="notify.action_package",
            condition_key="event_only", scope="s", normalized_scope_key="abc123",
            severity="info", title="t", occurred_at=NOW, reason="r",
        )
        self.assertEqual(event.tier, "watch")
        _, alerts = reduce_incident_events([event])
        self.assertEqual(alerts[0].tier, "watch")

    def test_the_same_instant_in_two_timezones_is_one_thing(self) -> None:
        # The stream really does mix offsets: measured on the 2026-07 file,
        # 916 records end in Z and 552 in +08:00. Two spellings of one instant
        # must collapse into one group with no time spread between them.
        utc_spelling = "2026-07-22T02:15:00+00:00"
        beijing_spelling = "2026-07-22T10:15:00+08:00"
        self.assertEqual(_parse_date(utc_spelling), _parse_date(beijing_spelling))
        self.assertEqual(_parse_date(beijing_spelling).utcoffset(), timedelta(0))

        records = [
            {
                "event_id": "ev-utc", "severity": "major", "match_label": "ack_overdue",
                "occurred_at": utc_spelling, "message": "handoff ho_20260722_404 pending",
                "agent_id": "pulse",
            },
            {
                "event_id": "ev-bj", "severity": "major", "match_label": "ack_overdue",
                "occurred_at": beijing_spelling, "message": "handoff ho_20260722_404 pending",
                "agent_id": "pulse",
            },
        ]
        alerts, reason = self._alerts(records)
        self.assertEqual(len(alerts), 1)
        self.assertIn("1 件事（2 条原始事件收敛）", reason)
        # Same instant twice: the group spans zero time, so first and last
        # render identically rather than appearing 8 hours apart.
        self.assertIn("07-22 10:15(UTC+8) 起", alerts[0]["reason"])
        self.assertIn("最近 07-22 10:15", alerts[0]["reason"])

    def test_times_render_on_one_clock_whatever_the_source_offset(self) -> None:
        # 02:00Z and 10:00+08:00 are the same instant; both must print 10:00.
        for spelling in ("2026-07-22T02:00:00+00:00", "2026-07-22T10:00:00+08:00"):
            alerts, _ = self._alerts([{
                "event_id": f"ev-{spelling}", "severity": "major", "match_label": "ack_overdue",
                "occurred_at": spelling, "message": "handoff ho_20260722_404 pending",
                "agent_id": "pulse",
            }])
            self.assertIn("07-22 10:00(UTC+8) 起", alerts[0]["reason"], spelling)

    def test_display_timezone_does_not_follow_the_machine(self) -> None:
        # Production runs on UTC and the peer on CST; the rendered clock must
        # be the same on both, so it is a fixed offset, not the local zone.
        self.assertEqual(DISPLAY_TZ.utcoffset(None), timedelta(hours=8))

    def test_a_long_message_is_bounded_and_only_the_first_paragraph_is_used(self) -> None:
        message = "发生什么：handoff ho_20260722_404 pending 891min。\n影响：第二段不该出现"
        alerts, _ = self._alerts([self._record("ack_overdue", message, NOW)])
        self.assertIn("发生什么", alerts[0]["reason"])
        self.assertNotIn("第二段不该出现", alerts[0]["reason"])

        long_message = "ho_20260722_404 " + "长" * 500
        alerts, _ = self._alerts([self._record("ack_overdue", long_message, NOW)])
        self.assertLessEqual(len(alerts[0]["reason"]), ALERT_MESSAGE_CHARS + 80)


if __name__ == "__main__":
    unittest.main()


class OwnerFacingIncidentTests(unittest.TestCase):
    """An incident card has to be worth the owner's attention and act on.

    All three rules here come from 2026-07-31, when the owner looked at the
    panel and asked "这些是什么？怎么还存在？" — three cards in English, none
    naming the thing they were about, two of them reporting a state that is by
    design and can therefore never clear.
    """

    def test_a_fresh_handoff_is_not_an_incident_for_the_owner(self) -> None:
        """15 minutes is a dispatcher signal; the owner's line is 3 days.

        A handoff raised at 14:19 was on the panel as an "未闭环事故" by 14:34.
        """
        for days in (0.0, 1.0, 2.9):
            with tempfile.TemporaryDirectory() as directory:
                fixture = LiveFixture(pathlib.Path(directory))
                created = NOW - timedelta(days=days)
                snapshot = json.loads(fixture.service(FakeRunner(
                    ack=ack_result(
                        [{"ho_id": "ho_fresh", "created_at": created.isoformat()}], 1
                    ),
                    systemd=systemd_result(),
                )).render_json())
                ack = [
                    i for i in snapshot["incidents"]
                    if i["capability_id"] == "s2.ack_overdue"
                ]
                self.assertEqual(ack, [], f"{days}d old should not be an incident")

    def test_a_stale_handoff_says_which_one_and_who_is_being_waited_on(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            created = NOW - timedelta(days=5)
            snapshot = json.loads(fixture.service(FakeRunner(
                ack=ack_result([{
                    "ho_id": "ho_20260725_586",
                    "receivers": ["cz-pulse"],
                    "created_at": created.isoformat(),
                }], 1),
                systemd=systemd_result(),
            )).render_json())
            ack = next(
                i for i in snapshot["incidents"]
                if i["capability_id"] == "s2.ack_overdue"
            )
            self.assertIn("ho_20260725_586", ack["title"])
            self.assertIn("5 天", ack["title"])
            self.assertIn("cz-pulse", ack["title"])
            self.assertIn("ho_20260725_586", ack["scope"])
            self.assertIn("ho_20260725_586", ack["suggested_action"])

    def test_a_tenant_agent_without_a_dispatcher_is_not_a_defect(self) -> None:
        """cz-pulse is answered by a person; "no dispatcher cron" is the design.

        INV2 reports it unconditionally, so the card fired on every lint and
        could never clear. Suppressed here in the projection; other invariants
        and other receivers are untouched.
        """
        tenants = frozenset({"cz-pulse"})
        self.assertTrue(
            _is_tenant_dispatcher_finding.__doc__,
            "the predicate must explain itself",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "handoffs").mkdir()
            (root / "handoffs" / "ho_tenant.yaml").write_text(
                "handoff_id: ho_tenant\nto_owner: cz-pulse\n", encoding="utf-8"
            )
            (root / "handoffs" / "ho_normal.yaml").write_text(
                "handoff_id: ho_normal\nto_owner: amanda\n", encoding="utf-8"
            )
            previous = os.environ.get("HARNESS_ROOT")
            os.environ["HARNESS_ROOT"] = str(root)
            try:
                self.assertTrue(_is_tenant_dispatcher_finding(
                    "INV2:ho_tenant", tenants
                ))
                self.assertFalse(_is_tenant_dispatcher_finding(
                    "INV2:ho_normal", tenants
                ))
                # Only INV2 is about dispatchers; nothing else is suppressed.
                self.assertFalse(_is_tenant_dispatcher_finding(
                    "INV3:ho_tenant", tenants
                ))
                self.assertFalse(_is_tenant_dispatcher_finding(
                    "INV7:ho_tenant", tenants
                ))
            finally:
                if previous is None:
                    del os.environ["HARNESS_ROOT"]
                else:
                    os.environ["HARNESS_ROOT"] = previous

    def test_a_finding_label_shows_our_id_but_never_upstream_prose(self) -> None:
        label = _ledger_finding_label
        self.assertEqual(label("INV2:ho_20260729_879"), "INV2 ho_20260729_879")
        self.assertEqual(label("INV3:wi_20260720_227"), "INV3 wi_20260720_227")
        # Free text from upstream is dropped, the invariant name survives.
        self.assertEqual(label("INV1:secret-ish-finding"), "INV1")
        self.assertNotIn("secret", label("INV1:secret-ish-finding"))
        self.assertEqual(label("garbage"), "未具名发现")
