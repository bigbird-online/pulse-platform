from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost.app_server_observer import AppServerEpochObserver
from pulse_compute_cost.app_server_usage import (
    AppServerTokenUpdate,
    AppServerUsageError,
    AppServerUsageLedger,
    EpochUsageContext,
)
from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.runtime_binding import build_runtime_usage_binding


def usage(
    *,
    thread: str = "provider-thread-secret",
    turn: str = "provider-turn-secret-1",
    last: tuple[int, int, int, int, int] = (10, 2, 5, 1, 15),
    total: tuple[int, int, int, int, int] = (100, 20, 50, 10, 150),
    window: int | None = 258_400,
    last_cache_write: int = 0,
    total_cache_write: int = 0,
) -> dict[str, object]:
    fields = (
        "inputTokens",
        "cachedInputTokens",
        "outputTokens",
        "reasoningOutputTokens",
        "totalTokens",
    )
    return {
        "jsonrpc": "2.0",
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "tokenUsage": {
                "last": {
                    **dict(zip(fields, last, strict=True)),
                    "cacheWriteInputTokens": last_cache_write,
                },
                "total": {
                    **dict(zip(fields, total, strict=True)),
                    "cacheWriteInputTokens": total_cache_write,
                },
                "modelContextWindow": window,
            },
        },
    }


def completed(
    *, thread: str = "provider-thread-secret", turn: str = "provider-turn-secret-1"
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "turn/completed",
        "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}},
    }


class AppServerUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "app-server-usage.jsonl"
        self.ledger = AppServerUsageLedger(self.path)
        self.context = EpochUsageContext(
            work_item_id="wi_20260822_730",
            phase="compute-cost-v2",
            epoch=1,
            runtime="chatgpt_work",
            procurement_source="chatgpt_subscription",
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
        )
        self.observer = AppServerEpochObserver(self.ledger, self.context)

    def bind(self) -> None:
        result = self.observer.bind(
            usage(last=(0, 0, 0, 0, 0)), observed_at="2026-08-25T00:00:00Z"
        )
        self.assertTrue(result.inserted)

    def test_prospective_epoch_binds_updates_completes_and_seals(self) -> None:
        self.bind()
        update = usage(
            turn="provider-turn-secret-2",
            last=(30, 5, 15, 3, 45),
            total=(130, 25, 65, 13, 195),
        )
        observed = self.observer.observe(update, observed_at="2026-08-25T00:01:00Z")
        self.assertEqual(
            observed.fact["epoch_delta"],
            {
                "input_tokens": 30,
                "cached_input_tokens": 5,
                "cache_write_input_tokens": 0,
                "output_tokens": 15,
                "reasoning_output_tokens": 3,
                "total_tokens": 45,
            },
        )
        self.observer.complete_turn(
            completed(turn="provider-turn-secret-2"),
            observed_at="2026-08-25T00:01:01Z",
        )
        sealed = self.observer.seal_source(
            operation_ref="operation_v1_rollover_1",
            observed_at="2026-08-25T00:01:02Z",
        )
        self.assertTrue(sealed.inserted)
        self.assertEqual(sealed.fact["kind"], "source_seal")
        self.assertEqual(sealed.fact["epoch_delta"]["total_tokens"], 45)
        self.assertEqual([fact["kind"] for fact in self.ledger.read()], [
            "bind_baseline", "turn_usage", "turn_completed", "source_seal"
        ])

    def test_raw_thread_turn_and_operation_ids_never_persist(self) -> None:
        self.bind()
        self.observer.observe(
            usage(
                turn="provider-turn-secret-2",
                last=(1, 0, 1, 0, 2),
                total=(101, 20, 51, 10, 152),
            ),
            observed_at="2026-08-25T00:01:00Z",
        )
        self.observer.complete_turn(
            completed(turn="provider-turn-secret-2"),
            observed_at="2026-08-25T00:01:01Z",
        )
        self.observer.seal_source(
            operation_ref="private-operation-value",
            observed_at="2026-08-25T00:01:02Z",
        )
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("provider-thread-secret", raw)
        self.assertNotIn("provider-turn-secret", raw)
        self.assertNotIn("private-operation-value", raw)
        self.assertNotIn("threadId", raw)
        self.assertNotIn("turnId", raw)
        self.assertIn("thread_v1_", raw)
        self.assertIn("turn_v1_", raw)

    def test_duplicate_delivery_and_restart_are_idempotent(self) -> None:
        baseline = usage(last=(0, 0, 0, 0, 0))
        self.observer.bind(baseline, observed_at="2026-08-25T00:00:00Z")
        duplicate = self.observer.bind(
            baseline, observed_at="2026-08-25T00:00:05Z"
        )
        self.assertTrue(duplicate.duplicate)
        update = usage(
            turn="provider-turn-secret-2",
            last=(1, 0, 1, 0, 2),
            total=(101, 20, 51, 10, 152),
        )
        self.observer.observe(update, observed_at="2026-08-25T00:01:00Z")
        restarted = AppServerEpochObserver(
            AppServerUsageLedger(self.path), self.context
        )
        replay = restarted.observe(update, observed_at="2026-08-25T00:01:10Z")
        self.assertTrue(replay.duplicate)
        self.assertEqual(len(self.ledger.read()), 2)

    def test_cumulative_reset_fails_before_append(self) -> None:
        self.bind()
        with self.assertRaisesRegex(AppServerUsageError, "moved backwards"):
            self.observer.observe(
                usage(
                    turn="provider-turn-secret-2",
                    last=(1, 0, 1, 0, 2),
                    total=(99, 19, 51, 10, 150),
                ),
                observed_at="2026-08-25T00:01:00Z",
            )
        self.assertEqual(len(self.ledger.read()), 1)

    def test_provider_last_must_reconcile_with_total(self) -> None:
        self.bind()
        with self.assertRaisesRegex(AppServerUsageError, "does not reconcile"):
            self.observer.observe(
                usage(
                    turn="provider-turn-secret-2",
                    last=(2, 0, 1, 0, 3),
                    total=(101, 20, 51, 10, 152),
                ),
                observed_at="2026-08-25T00:01:00Z",
            )
        self.assertEqual(len(self.ledger.read()), 1)

    def test_source_seal_waits_for_matching_completed_turn(self) -> None:
        self.bind()
        self.observer.observe(
            usage(
                turn="provider-turn-secret-2",
                last=(1, 0, 1, 0, 2),
                total=(101, 20, 51, 10, 152),
            ),
            observed_at="2026-08-25T00:01:00Z",
        )
        self.observer.complete_turn(
            completed(turn="provider-turn-secret-1"),
            observed_at="2026-08-25T00:01:01Z",
        )
        with self.assertRaisesRegex(AppServerUsageError, "final usage"):
            self.observer.seal_source(
                operation_ref="operation_v1_rollover_1",
                observed_at="2026-08-25T00:01:02Z",
            )

    def test_source_seal_retry_keeps_first_observation(self) -> None:
        self.bind()
        self.observer.complete_turn(
            completed(), observed_at="2026-08-25T00:00:01Z"
        )
        first = self.observer.seal_source(
            operation_ref="operation_v1_rollover_1",
            observed_at="2026-08-25T00:00:02Z",
        )
        retry = self.observer.seal_source(
            operation_ref="operation_v1_rollover_1",
            observed_at="2026-08-25T00:00:10Z",
        )
        self.assertTrue(first.inserted)
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.fact["observed_at"], "2026-08-25T00:00:02Z")

    def test_other_thread_and_unbound_updates_fail_closed(self) -> None:
        with self.assertRaisesRegex(AppServerUsageError, "bind a baseline"):
            self.observer.observe(usage(), observed_at="2026-08-25T00:00:00Z")
        self.bind()
        with self.assertRaisesRegex(AppServerUsageError, "another thread"):
            self.observer.observe(
                usage(
                    thread="other-secret-thread",
                    turn="other-turn",
                    last=(1, 0, 1, 0, 2),
                    total=(101, 20, 51, 10, 152),
                ),
                observed_at="2026-08-25T00:01:00Z",
            )

    def test_notification_schema_and_subset_counts_fail_closed(self) -> None:
        malformed = usage()
        malformed["method"] = "account/usage/read"
        with self.assertRaisesRegex(AppServerUsageError, "method"):
            AppServerTokenUpdate.from_notification(malformed)
        with self.assertRaisesRegex(AppServerUsageError, "cached_input"):
            AppServerTokenUpdate.from_notification(
                usage(last=(1, 2, 0, 0, 1))
            )

    def test_current_app_server_schema_preserves_cache_write_and_nullable_window(self) -> None:
        parsed = AppServerTokenUpdate.from_notification(
            usage(
                last_cache_write=4,
                total_cache_write=9,
                window=None,
            )
        )
        self.assertEqual(parsed.last.cache_write_input_tokens, 4)
        self.assertEqual(parsed.total.cache_write_input_tokens, 9)
        self.assertIsNone(parsed.model_context_window)

    def test_ledger_rejects_tampering(self) -> None:
        self.bind()
        facts = self.ledger.read()
        facts[0]["cumulative"]["total_tokens"] += 1
        self.path.write_text(json.dumps(facts[0]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(AppServerUsageError, "malformed"):
            self.ledger.read()

    def test_cli_consumes_host_binding_without_manual_thread_attribution(self) -> None:
        notification = Path(self.temp.name) / "notification.json"
        notification.write_text(
            json.dumps(usage(last=(0, 0, 0, 0, 0))), encoding="utf-8"
        )
        binding = build_runtime_usage_binding(
            work_item_id="wi_20260822_730",
            agent_id="pulse",
            phase="compute-cost-v2",
            epoch=1,
            runtime="chatgpt_work",
            procurement_source="chatgpt_subscription",
            issued_at="2026-08-25T00:00:00Z",
        )
        output = io.StringIO()
        with patch.dict(
            "os.environ",
            {"PULSE_RUNTIME_USAGE_BINDING": json.dumps(binding.to_dict())},
            clear=False,
        ), redirect_stdout(output):
            rc = cli_main([
                "app-server-epoch",
                "bind",
                "--ledger-path", str(self.path),
                "--model", "gpt-5.6-sol",
                "--reasoning-effort", "xhigh",
                "--notification", str(notification),
                "--observed-at", "2026-08-25T00:00:00Z",
            ])
        self.assertEqual(rc, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(
            receipt["fact"]["context"]["runtime_binding_sha256"],
            binding.binding_sha256,
        )
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn("provider-thread-secret", persisted)
        self.assertNotIn("provider-turn-secret", persisted)

    def test_cli_persists_host_notification_without_raw_identity(self) -> None:
        notification = Path(self.temp.name) / "notification.json"
        notification.write_text(
            json.dumps(usage(last=(0, 0, 0, 0, 0))), encoding="utf-8"
        )
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            rc = cli_main([
                "app-server-epoch",
                "bind",
                "--ledger-path", str(self.path),
                "--work-item", "wi_20260822_730",
                "--phase", "compute-cost-v2",
                "--epoch", "1",
                "--runtime", "chatgpt_work",
                "--procurement-source", "chatgpt_subscription",
                "--model", "gpt-5.6-sol",
                "--reasoning-effort", "xhigh",
                "--notification", str(notification),
                "--observed-at", "2026-08-25T00:00:00Z",
            ])
        self.assertEqual(rc, 0)
        receipt = json.loads(output.getvalue())
        self.assertTrue(receipt["inserted"])
        self.assertEqual(receipt["fact"]["kind"], "bind_baseline")
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn("provider-thread-secret", persisted)
        self.assertNotIn("provider-turn-secret", persisted)


if __name__ == "__main__":
    unittest.main()
