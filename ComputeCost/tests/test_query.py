from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.events import Lineage, TokenUsage, UNKNOWN, UsageEvent
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.openrouter import OpenRouterPriceSnapshot
from pulse_compute_cost.query import (
    QueryError,
    QueryWindow,
    query_events,
    query_ledger,
    supported_dimensions,
)


class CostQueryTests(unittest.TestCase):
    def price(self) -> OpenRouterPriceSnapshot:
        return OpenRouterPriceSnapshot(
            model_id="model-a",
            canonical_slug="provider/model-a",
            retrieved_at="2026-08-30T00:00:00Z",
            prompt_per_token=Decimal("0.001"),
            completion_per_token=Decimal("0.002"),
            input_cache_read_per_token=Decimal("0.0005"),
        )

    def event(
        self,
        suffix: str,
        occurred_at: str,
        *,
        observed_at: str | None = None,
        agent: str = "Pulse",
        consumer: str = "weekly-patrol",
        source: str = "chatgpt_pro",
        model: str = "model-a",
        outcome: str = "accepted",
        input_tokens: int = 10,
        output_tokens: int = 2,
        cache_read: int = 4,
        retry_count: int = 0,
        escalation_count: int = 0,
        effort: str = "medium",
        task_family: str = "compute-cost-s4",
        risk_class: str = "standard",
        base_route: str = UNKNOWN,
        risk_floor: str = UNKNOWN,
        effective_route: str = UNKNOWN,
        human_correction: bool | None = None,
        root_run_ref: str = UNKNOWN,
        cron_job_ref: str = UNKNOWN,
        scan_ref: str = UNKNOWN,
    ) -> UsageEvent:
        return UsageEvent(
            event_id=f"uev1_{suffix}",
            occurred_at=occurred_at,
            observed_at=observed_at or occurred_at,
            machine="test-machine",
            agent=agent,
            consumer_path=consumer,
            source=source,
            runtime="codex",
            provider="openai",
            model=model,
            effort=effort,
            tokens=TokenUsage(
                input=input_tokens,
                output=output_tokens,
                cache_read=cache_read,
            ),
            task_family=task_family,
            risk_class=risk_class,
            task_ref="task-s3",
            outcome_ref="outcome-s3" if outcome != UNKNOWN else UNKNOWN,
            retry_count=retry_count,
            escalation_count=escalation_count,
            base_route=base_route,
            risk_floor=risk_floor,
            effective_route=effective_route,
            outcome_status=outcome,
            human_correction=human_correction,
            lineage=Lineage(
                root_agent="Pulse",
                execution_mode="automatic",
                session_role="main",
                root_run_ref=root_run_ref,
                cron_job_ref=cron_job_ref,
                scan_ref=scan_ref,
            ),
        )

    def test_iso_week_crosses_month_and_reads_late_correction(self) -> None:
        # 2026-08-31 is Monday. The first event is Aug 31 Beijing time but is
        # observed in September, so it resides in the August corrections file.
        august_late = self.event(
            "aug",
            "2026-08-31T15:59:59Z",
            observed_at="2026-09-01T00:00:00Z",
        )
        september = self.event("sep", "2026-08-31T16:00:00Z", agent="Duke")
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(Path(tmp))
            result = ledger.ingest([august_late, september])
            self.assertEqual(result.corrections_inserted, 1)
            self.assertEqual(result.usage_inserted, 1)

            report = query_ledger(
                ledger=ledger,
                window=QueryWindow.from_text("2026-08-31", "2026-09-07"),
                prices=[self.price()],
                generated_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
            )

        self.assertEqual(report["schema"], "cost_query.v1")
        self.assertEqual(report["mode"], "shadow")
        self.assertEqual(report["window"]["iso_week"], "2026-W36")
        self.assertEqual(report["totals"]["event_count"], 2)
        self.assertEqual(report["totals"]["reference_cost_usd"], "0.024")
        self.assertEqual(report["weekly_capacity"]["chatgpt_pro_capacity_cents"], 5000)
        self.assertEqual(report["weekly_capacity"]["quota_pressure"], "unknown")
        self.assertEqual(
            report["managed_cost"]["status"], "not_settled_in_query_window"
        )
        self.assertEqual(report["conservation"]["status"], "pass")
        self.assertEqual(
            sum(row["event_count"] for row in report["by_agent"]), 2
        )

    def test_all_required_dimensions_conserve_without_manual_recalculation(self) -> None:
        window = QueryWindow.from_text("2026-08-31", "2026-09-07")
        events = [
            self.event(
                "one",
                "2026-09-01T00:00:00Z",
                root_run_ref="lineage_v1_" + "a" * 64,
                cron_job_ref="cron_v1_" + "b" * 64,
                scan_ref="scan_v1_" + "c" * 64,
            ),
            self.event("two", "2026-09-02T00:00:00Z", agent="Duke"),
        ]
        report = query_events(
            events=events,
            window=window,
            prices=[self.price()],
            generated_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        )
        expected = {
            "agent",
            "consumer",
            "task",
            "model",
            "effort",
            "retry",
            "escalation",
            "outcome",
            "root_agent",
            "execution_mode",
            "session_role",
            "root_task",
            "cron_job",
            "scan",
            "root_origin",
            "day",
            "week",
            "month",
        }
        self.assertEqual(set(supported_dimensions()), expected)
        self.assertEqual(set(report["groups"]), expected)
        self.assertTrue(all(report["conservation"]["dimensions"].values()))
        self.assertTrue(all(report["conservation"]["baseline_dimensions"].values()))
        self.assertIn(
            "lineage_v1_" + "a" * 64,
            {row["key"] for row in report["groups"]["root_task"]},
        )
        self.assertIn(
            "cron_v1_" + "b" * 64,
            {row["key"] for row in report["groups"]["cron_job"]},
        )
        self.assertIn(
            "scan_v1_" + "c" * 64,
            {row["key"] for row in report["groups"]["scan"]},
        )
        for rows in report["groups"].values():
            self.assertEqual(sum(row["event_count"] for row in rows), 2)
            self.assertEqual(sum(row["total_tokens"] for row in rows), 24)
            self.assertEqual(
                sum(Decimal(row["reference_cost_usd"]) for row in rows),
                Decimal("0.024"),
            )

    def test_unknown_evidence_is_explicit_and_cost_share_fails_closed(self) -> None:
        window = QueryWindow.from_text("2026-08-31", "2026-09-07")
        known = self.event("known", "2026-09-01T00:00:00Z")
        unknown = self.event(
            "unknown",
            "2026-09-02T00:00:00Z",
            agent=UNKNOWN,
            source=UNKNOWN,
            model=UNKNOWN,
            outcome=UNKNOWN,
        )
        report = query_events(
            events=[known, unknown],
            window=window,
            prices=[self.price()],
        )
        self.assertEqual(report["totals"]["unpriced_event_count"], 1)
        self.assertFalse(report["totals"]["reference_cost_complete"])
        self.assertIn("unpriced_events", report["quality"]["flags"])
        unattributed = next(
            row for row in report["by_agent"] if row["key"] == "unattributed"
        )
        self.assertEqual(unattributed["event_count"], 1)
        self.assertTrue(
            all(row["reference_cost_share"] is None for row in report["by_agent"])
        )

    def test_previous_same_length_window_is_the_relative_baseline(self) -> None:
        window = QueryWindow.from_text("2026-08-31", "2026-09-07")
        current = self.event("current", "2026-09-01T00:00:00Z")
        baseline = self.event(
            "baseline",
            "2026-08-25T00:00:00Z",
            input_tokens=5,
            output_tokens=1,
            cache_read=2,
        )
        report = query_events(
            events=[current],
            window=window,
            prices=[self.price()],
            baseline_events=[baseline],
            baseline_window=window.previous(),
        )
        self.assertEqual(report["baseline"]["window"]["from"], "2026-08-24")
        self.assertEqual(report["baseline"]["delta"]["reference_cost_usd"], "0.006")
        self.assertEqual(report["baseline"]["delta"]["reference_cost_relative"], "1")
        pulse = next(row for row in report["by_agent"] if row["key"] == "Pulse")
        self.assertEqual(pulse["baseline"]["reference_cost_usd"], "0.006")
        self.assertEqual(pulse["delta"]["reference_cost_usd"], "0.006")
        self.assertEqual(pulse["delta"]["reference_cost_relative"], "1")
        self.assertEqual(report["driver_delta"]["status"], "insufficient_evidence")

    def test_s4_driver_bridge_explains_retry_model_and_outcome_changes(self) -> None:
        window = QueryWindow.from_text("2026-08-31", "2026-09-07")
        baseline = self.event(
            "bridge-baseline",
            "2026-08-25T00:00:00Z",
            source="openrouter_api",
            model="model-a",
            outcome="accepted",
            input_tokens=10,
            output_tokens=2,
            cache_read=0,
            retry_count=0,
            base_route="media",
            risk_floor="standard",
            effective_route="media",
        )
        current = self.event(
            "bridge-current",
            "2026-09-01T00:00:00Z",
            source="openrouter_api",
            model="model-b",
            outcome="rejected",
            input_tokens=20,
            output_tokens=4,
            cache_read=0,
            retry_count=1,
            base_route="frontier",
            risk_floor="standard",
            effective_route="frontier",
            human_correction=True,
        )
        p0_a = OpenRouterPriceSnapshot(
            model_id="model-a",
            canonical_slug="provider/model-a",
            retrieved_at="2026-08-24T00:00:00Z",
            prompt_per_token=Decimal("0.001"),
            completion_per_token=Decimal("0.002"),
            input_cache_read_per_token=Decimal("0.0005"),
        )
        p0_b = OpenRouterPriceSnapshot(
            model_id="model-b",
            canonical_slug="provider/model-b",
            retrieved_at="2026-08-24T00:00:00Z",
            prompt_per_token=Decimal("0.002"),
            completion_per_token=Decimal("0.004"),
            input_cache_read_per_token=Decimal("0.001"),
        )
        p1_a = OpenRouterPriceSnapshot(
            model_id="model-a",
            canonical_slug="provider/model-a",
            retrieved_at="2026-08-31T00:00:00Z",
            prompt_per_token=Decimal("0.0015"),
            completion_per_token=Decimal("0.003"),
            input_cache_read_per_token=Decimal("0.00075"),
        )
        p1_b = OpenRouterPriceSnapshot(
            model_id="model-b",
            canonical_slug="provider/model-b",
            retrieved_at="2026-08-31T00:00:00Z",
            prompt_per_token=Decimal("0.003"),
            completion_per_token=Decimal("0.006"),
            input_cache_read_per_token=Decimal("0.0015"),
        )
        report = query_events(
            events=[current],
            window=window,
            prices=[self.price()],
            bridge_baseline_prices=[p0_a, p0_b],
            bridge_current_prices=[p1_a, p1_b],
            baseline_events=[baseline],
            baseline_window=window.previous(),
        )

        bridge = report["driver_delta"]
        self.assertEqual(bridge["status"], "complete")
        self.assertEqual(bridge["ordering"], ["demand", "route", "execution", "price", "residual"])
        self.assertEqual(bridge["demand_delta_usd"], "0")
        self.assertEqual(bridge["route_delta_usd"], "0.014")
        self.assertEqual(bridge["execution_delta_usd"], "0.028")
        self.assertEqual(bridge["price_delta_usd"], "0.028")
        self.assertEqual(bridge["residual_delta_usd"], "0")
        self.assertEqual(bridge["observed_delta_usd"], "0.07")
        self.assertEqual(bridge["conservation"], "pass")
        self.assertEqual(bridge["execution_evidence"]["delta"]["retry_count"], 1)
        self.assertEqual(bridge["outcome_constraint"]["accepted_change"], -1)
        self.assertEqual(bridge["outcome_constraint"]["current"]["rejected"], 1)
        self.assertEqual(
            bridge["outcome_constraint"]["note"],
            "outcome_is_quality_constraint_not_fifth_cost_driver",
        )

    def test_s4_driver_bridge_fails_closed_without_pro_settlement_evidence(self) -> None:
        window = QueryWindow.from_text("2026-08-31", "2026-09-07")
        report = query_events(
            events=[self.event("pro-current", "2026-09-01T00:00:00Z")],
            window=window,
            prices=[self.price()],
            baseline_events=[self.event("pro-baseline", "2026-08-25T00:00:00Z")],
            baseline_window=window.previous(),
        )
        self.assertEqual(report["driver_delta"]["status"], "insufficient_evidence")
        self.assertIn(
            "pro_settlement_unavailable_for_window", report["driver_delta"]["reasons"]
        )
        self.assertIsNone(report["driver_delta"]["route_delta_usd"])

    def test_s4_driver_bridge_names_missing_claude_subscription_settlement(self) -> None:
        window = QueryWindow.from_text("2026-08-31", "2026-09-07")
        report = query_events(
            events=[self.event("claude-current", "2026-09-01T00:00:00Z", source="claude_subscription")],
            window=window,
            prices=[self.price()],
            baseline_events=[self.event("claude-baseline", "2026-08-25T00:00:00Z", source="claude_subscription")],
            baseline_window=window.previous(),
        )
        self.assertEqual(report["driver_delta"]["status"], "insufficient_evidence")
        self.assertIn(
            "claude_subscription_settlement_unavailable_for_window",
            report["driver_delta"]["reasons"],
        )
        self.assertNotIn("source_evidence_incomplete", report["driver_delta"]["reasons"])

    def test_baseline_only_partition_is_retained_for_negative_delta(self) -> None:
        window = QueryWindow.from_text("2026-08-31", "2026-09-07")
        current = self.event("current-only", "2026-09-01T00:00:00Z", agent="Pulse")
        baseline = self.event(
            "baseline-only",
            "2026-08-25T00:00:00Z",
            agent="Duke",
            input_tokens=5,
            output_tokens=1,
            cache_read=2,
        )
        report = query_events(
            events=[current],
            window=window,
            prices=[self.price()],
            baseline_events=[baseline],
            baseline_window=window.previous(),
        )
        duke = next(row for row in report["by_agent"] if row["key"] == "Duke")
        self.assertEqual(duke["event_count"], 0)
        self.assertEqual(duke["baseline"]["event_count"], 1)
        self.assertEqual(duke["delta"]["event_count"], -1)
        self.assertEqual(duke["delta"]["reference_cost_usd"], "-0.006")
        self.assertEqual(duke["delta"]["reference_cost_relative"], "-1")
        self.assertEqual(report["conservation"]["status"], "pass")

    def test_event_outside_window_is_rejected(self) -> None:
        with self.assertRaisesRegex(QueryError, "outside query window"):
            query_events(
                events=[self.event("old", "2026-08-20T00:00:00Z")],
                window=QueryWindow.from_text("2026-08-31", "2026-09-07"),
                prices=[self.price()],
            )


if __name__ == "__main__":
    unittest.main()
