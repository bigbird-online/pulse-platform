from __future__ import annotations

from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.app_server_observer import AppServerEpochObserver
from pulse_compute_cost.app_server_usage import (
    AppServerUsageLedger,
    EpochUsageContext,
)
from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.wi_report import WiReportError, build_wi_report


_FIELDS = (
    "inputTokens",
    "cachedInputTokens",
    "outputTokens",
    "reasoningOutputTokens",
    "totalTokens",
)


def usage(
    thread: str,
    turn: str,
    last: tuple[int, int, int, int, int],
    total: tuple[int, int, int, int, int],
) -> dict[str, object]:
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "tokenUsage": {
                "last": dict(zip(_FIELDS, last, strict=True)),
                "total": dict(zip(_FIELDS, total, strict=True)),
                "modelContextWindow": 258_400,
            },
        },
    }


def completed(thread: str, turn: str) -> dict[str, object]:
    return {
        "method": "turn/completed",
        "params": {"threadId": thread, "turn": {"id": turn}},
    }


def pi_snapshot() -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "pi_session_usage_snapshot/v1",
        "generated_at": "2026-08-25T00:00:00Z",
        "lifecycle_state": "open",
        "work_item_id": "wi_20260822_730",
        "phase": "work_item",
        "epoch": 1,
        "runtime": "pi",
        "procurement_source": "chatgpt_subscription",
        "summary": {
            "schema": "pi_native_usage_summary/v1",
            "event_count": 2,
            "totals": {
                "input": 100,
                "output": 20,
                "cache_read": 50,
                "cache_write": 5,
                "reasoning": 4,
                "total_tokens": 175,
            },
            "provider_estimated_cost_usd": "0.0123",
            "by_provider_model_effort": {
                "openai-codex/gpt-5.6-sol/high": {
                    "event_count": 2,
                    "total_tokens": 175,
                }
            },
            "last_entry_ref": "pi_entry_v1_" + "a" * 64,
            "source_prefix_sha256": "b" * 64,
            "source_bytes": 1000,
            "privacy": "raw_session_entry_response_content_and_path_omitted",
        },
    }
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    value["snapshot_sha256"] = sha256(raw).hexdigest()
    return value


def account_snapshot() -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "app_server_account_snapshot/v1",
        "generated_at": "2026-08-25T00:00:00Z",
        "producer": "openai_app_server",
        "usage": {
            "summary": {"lifetime_tokens": 1000},
            "daily_usage": None,
            "coverage": "account_activity_not_wi_attribution",
        },
        "rate_limits": {
            "plan_type": "pro",
            "limit_id": "codex",
            "limit_name": "Codex",
            "primary": {
                "used_percent": 27,
                "resets_at": 1788137010,
                "window_duration_mins": 10080,
            },
            "secondary": None,
            "credits": {"balance": "0", "has_credits": False, "unlimited": False},
            "individual_limit": None,
            "spend_control_reached": False,
            "rate_limit_reached_type": None,
            "reset_credits": None,
            "coverage": "account_quota_not_wi_attribution",
        },
        "privacy": "account_user_thread_identity_and_codex_home_omitted",
    }
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    value["snapshot_sha256"] = sha256(raw).hexdigest()
    return value


class WiReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "usage.jsonl"
        self.ledger = AppServerUsageLedger(self.path)

    def observer(
        self,
        *,
        work_item: str = "wi_20260822_730",
        epoch: int = 1,
        runtime: str = "chatgpt_work",
        model: str = "gpt-5.6-sol",
        procurement: str = "chatgpt_subscription",
    ) -> AppServerEpochObserver:
        return AppServerEpochObserver(
            self.ledger,
            EpochUsageContext(
                work_item_id=work_item,
                phase="work_item",
                epoch=epoch,
                runtime=runtime,
                procurement_source=procurement,
                model=model,
                reasoning_effort="xhigh",
            ),
        )

    def seal_epoch(
        self,
        observer: AppServerEpochObserver,
        *,
        thread: str,
        baseline: tuple[int, int, int, int, int],
        delta: tuple[int, int, int, int, int],
        minute: int,
    ) -> None:
        observer.bind(
            usage(thread, "baseline", (0, 0, 0, 0, 0), baseline),
            observed_at=f"2026-08-25T00:{minute:02d}:00Z",
        )
        total = tuple(left + right for left, right in zip(baseline, delta, strict=True))
        observer.observe(
            usage(thread, "final", delta, total),
            observed_at=f"2026-08-25T00:{minute:02d}:01Z",
        )
        observer.complete_turn(
            completed(thread, "final"),
            observed_at=f"2026-08-25T00:{minute:02d}:02Z",
        )
        observer.seal_source(
            operation_ref=f"operation-{thread}",
            observed_at=f"2026-08-25T00:{minute:02d}:03Z",
        )

    def test_report_sums_only_sealed_epoch_deltas_and_conserves(self) -> None:
        self.seal_epoch(
            self.observer(epoch=1),
            thread="private-thread-a",
            baseline=(100, 20, 50, 10, 150),
            delta=(30, 5, 15, 3, 45),
            minute=1,
        )
        self.seal_epoch(
            self.observer(epoch=2, runtime="codex_app", model="gpt-5.6-codex"),
            thread="private-thread-b",
            baseline=(10, 1, 5, 1, 15),
            delta=(7, 2, 4, 1, 11),
            minute=2,
        )
        report = build_wi_report(self.ledger, "wi_20260822_730")
        self.assertEqual(report["usage"]["status"], "exact")
        self.assertEqual(report["usage"]["tokens"]["total_tokens"], 56)
        self.assertEqual(report["usage"]["tokens"]["input_tokens"], 37)
        self.assertEqual(
            sum(
                row["tokens"]["total_tokens"]
                for row in report["usage"]["by_runtime_model_procurement"]
            ),
            56,
        )
        self.assertEqual(report["usage"]["conservation"], "pass")
        self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
        self.assertEqual(
            report["cost"]["cash_basis"],
            "openrouter_standard_token_cash",
        )
        self.assertEqual(report["cost"]["cash_coverage"], "unavailable")
        self.assertEqual(
            report["cost"]["subscription_settlement"],
            "excluded_from_wi_report_and_roi",
        )

    def test_open_epoch_is_excluded_and_report_is_partial(self) -> None:
        sealed = self.observer(epoch=1)
        self.seal_epoch(
            sealed,
            thread="private-thread-a",
            baseline=(0, 0, 0, 0, 0),
            delta=(3, 1, 2, 1, 5),
            minute=1,
        )
        open_observer = self.observer(epoch=2)
        open_observer.bind(
            usage("private-thread-b", "baseline", (0, 0, 0, 0, 0), (9, 1, 4, 1, 13)),
            observed_at="2026-08-25T00:02:00Z",
        )
        report = build_wi_report(self.ledger, "wi_20260822_730")
        self.assertEqual(report["usage"]["status"], "partial")
        self.assertEqual(report["usage"]["tokens"]["total_tokens"], 5)
        self.assertEqual(len(report["usage"]["open_epochs"]), 1)
        self.assertIn("unsealed_epoch_excluded", report["warnings"])

    def test_no_evidence_is_unavailable_not_zero_cost(self) -> None:
        report = build_wi_report(self.ledger, "wi_missing")
        self.assertEqual(report["usage"]["status"], "unavailable")
        self.assertEqual(report["usage"]["tokens"], "unavailable")
        self.assertEqual(report["usage"]["conservation"], "unavailable")
        self.assertEqual(report["cost"]["token_resource"], "unavailable")
        self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
        self.assertEqual(report["cost"]["cash_basis"], "openrouter_standard_token_cash")
        self.assertEqual(report["cost"]["cash_coverage"], "unavailable")
        self.assertIn("no_app_server_usage_evidence", report["warnings"])

    def test_join_conflict_zero_cohort_remains_unavailable(self) -> None:
        cohort: dict[str, object] = {
            "schema": "computecost_wi_usage_cohort/v1",
            "work_item_id": "wi_20260822_730",
            "status": "join_conflict",
            "window": {
                "started_at": "2026-08-25T00:00:00Z",
                "ended_at": "2026-08-25T01:00:00Z",
                "source_ref": "work-items/wi_20260822_730.yaml",
            },
            "event_count": 0,
            "root_thread_count": 0,
            "root_thread_refs": [],
            "tokens": "unavailable",
            "by_model": {},
            "request_partitions": [],
            "quality_flags": {},
            "producer_attempts": [],
            "diagnostics": {},
        }
        cohort["cohort_sha256"] = sha256(
            json.dumps(
                cohort,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()

        report = build_wi_report(None, "wi_20260822_730", usage_cohort=cohort)

        self.assertEqual(report["coverage"]["producer_discovery"], "join_conflict")
        self.assertEqual(report["usage"]["status"], "unavailable")
        self.assertEqual(report["usage"]["tokens"], "unavailable")
        self.assertEqual(report["cost"]["token_resource"], "unavailable")

    def test_incomplete_task_usage_is_retained_and_warned(self) -> None:
        cohort: dict[str, object] = {
            "schema": "computecost_wi_usage_cohort/v1",
            "work_item_id": "wi_20260822_730",
            "status": "observed",
            "window": {
                "started_at": "2026-08-25T00:00:00Z",
                "ended_at": "2026-08-25T01:00:00Z",
                "source_ref": "work-items/wi_20260822_730.yaml",
            },
            "event_count": 1,
            "root_thread_count": 1,
            "root_thread_refs": ["lineage_v1_fixture"],
            "tokens": {
                "input": 100,
                "cache_read": 80,
                "cache_write": 0,
                "net_new_input": 20,
                "output": 5,
                "reasoning": 0,
                "total_resource_tokens": 105,
            },
            "by_model": {},
            "quality_flags": {"incomplete_task": 1},
            "producer_attempts": [],
            "diagnostics": {},
        }
        cohort["cohort_sha256"] = sha256(
            json.dumps(
                cohort,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()

        report = build_wi_report(
            None,
            "wi_20260822_730",
            usage_cohort=cohort,
        )

        self.assertEqual(report["usage"]["tokens"]["total_tokens"], 105)
        self.assertEqual(
            report["usage"]["usage_event_cohort"]["quality_flags"],
            {"incomplete_task": 1},
        )
        self.assertIn("incomplete_task_usage_included", report["warnings"])

    def test_other_work_item_is_not_joined(self) -> None:
        self.seal_epoch(
            self.observer(work_item="wi_other"),
            thread="other-private-thread",
            baseline=(0, 0, 0, 0, 0),
            delta=(5, 1, 2, 1, 7),
            minute=1,
        )
        report = build_wi_report(self.ledger, "wi_20260822_730")
        self.assertEqual(report["usage"]["status"], "unavailable")
        self.assertEqual(report["usage"]["tokens"], "unavailable")

    def test_multiple_source_seals_for_one_epoch_fail_closed(self) -> None:
        observer = self.observer()
        self.seal_epoch(
            observer,
            thread="private-thread-a",
            baseline=(0, 0, 0, 0, 0),
            delta=(3, 1, 2, 1, 5),
            minute=1,
        )
        observer.seal_source(
            operation_ref="second-operation",
            observed_at="2026-08-25T00:01:04Z",
        )
        with self.assertRaisesRegex(WiReportError, "multiple source seals"):
            build_wi_report(self.ledger, "wi_20260822_730")

    def test_report_and_evidence_do_not_expose_provider_identity(self) -> None:
        self.seal_epoch(
            self.observer(),
            thread="provider-private-thread",
            baseline=(0, 0, 0, 0, 0),
            delta=(3, 1, 2, 1, 5),
            minute=1,
        )
        serialized = json.dumps(build_wi_report(self.ledger, "wi_20260822_730"))
        self.assertNotIn("provider-private-thread", serialized)
        self.assertNotIn("operation-provider-private-thread", serialized)
        self.assertIn("source_seal_content_sha256", serialized)

    def test_pi_open_epoch_is_visible_but_not_final_cash(self) -> None:
        report = build_wi_report(
            self.ledger,
            "wi_20260822_730",
            pi_snapshot=pi_snapshot(),
        )
        native = report["usage"]["pi_native"]
        self.assertEqual(native["status"], "unbounded_session_diagnostic")
        self.assertFalse(native["cost_eligible"])
        self.assertEqual(report["coverage"]["pi_usage"], "diagnostic_unbounded")
        self.assertEqual(native["totals"]["total_tokens"], 175)
        self.assertNotIn("provider_estimated_cost_usd", report["cost"])
        self.assertEqual(report["cost"]["machine_pricing_authority"], "openrouter.models")
        self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
        self.assertEqual(report["cost"]["provider_native_cash_usd"], "unavailable")
        self.assertIn("pi_native_price_discarded_non_authoritative", report["warnings"])
        self.assertIn("open_pi_epoch_not_final_cost", report["warnings"])
        self.assertIn("unbounded_pi_snapshot_not_wi_cost", report["warnings"])

    def test_unbounded_whole_session_sealed_snapshot_never_becomes_wi_cost(self) -> None:
        snapshot = pi_snapshot()
        snapshot["lifecycle_state"] = "sealed"
        unsigned = {
            key: value for key, value in snapshot.items() if key != "snapshot_sha256"
        }
        snapshot["snapshot_sha256"] = sha256(
            json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        report = build_wi_report(
            self.ledger,
            "wi_20260822_730",
            pi_snapshot=snapshot,
        )
        self.assertEqual(report["usage"]["tokens"], "unavailable")
        self.assertEqual(
            report["usage"]["pi_native"]["status"],
            "unbounded_session_diagnostic",
        )
        self.assertFalse(report["usage"]["pi_native"]["cost_eligible"])
        self.assertIn("unbounded_pi_snapshot_not_wi_cost", report["warnings"])

    def test_pi_snapshot_work_item_and_digest_must_match(self) -> None:
        snapshot = pi_snapshot()
        snapshot["work_item_id"] = "wi_other"
        raw = {
            key: value for key, value in snapshot.items() if key != "snapshot_sha256"
        }
        snapshot["snapshot_sha256"] = sha256(
            json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        with self.assertRaisesRegex(WiReportError, "another work item"):
            build_wi_report(
                self.ledger,
                "wi_20260822_730",
                pi_snapshot=snapshot,
            )

    def test_account_quota_is_context_not_wi_cost(self) -> None:
        report = build_wi_report(
            self.ledger,
            "wi_20260822_730",
            account_snapshot=account_snapshot(),
        )
        quota = report["cost"]["quota_pressure"]
        self.assertEqual(quota["scope"], "account_not_work_item")
        self.assertEqual(quota["primary"]["used_percent"], 27)
        self.assertEqual(report["coverage"]["quota"], "observed_account_context")
        self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
        self.assertEqual(
            report["evidence"]["account_snapshot_sha256"],
            account_snapshot()["snapshot_sha256"],
        )

    def test_tampered_account_snapshot_is_rejected(self) -> None:
        snapshot = account_snapshot()
        snapshot["rate_limits"]["primary"]["used_percent"] = 99  # type: ignore[index]
        with self.assertRaisesRegex(WiReportError, "digest"):
            build_wi_report(
                self.ledger,
                "wi_20260822_730",
                account_snapshot=snapshot,
            )

    def test_direct_wi_cli_alias_returns_report_without_manual_join(self) -> None:
        self.seal_epoch(
            self.observer(),
            thread="private-thread-a",
            baseline=(0, 0, 0, 0, 0),
            delta=(3, 1, 2, 1, 5),
            minute=1,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            rc = cli_main([
                "wi_20260822_730",
                "--app-server-ledger", str(self.path),
            ])
        self.assertEqual(rc, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["schema"], "computecost_wi_report/v4")
        self.assertEqual(report["usage"]["tokens"]["total_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
