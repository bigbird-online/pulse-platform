from __future__ import annotations

from decimal import Decimal
import unittest

from pulse_compute_cost.events import TokenUsage, UNKNOWN, UsageEvent
from pulse_compute_cost.openrouter import OpenRouterPriceSnapshot
from pulse_compute_cost.query import QueryError, QueryWindow, query_events


class QueryReferencePriceTests(unittest.TestCase):
    def event(self, suffix: str, *, model: str, source: str = "chatgpt_pro") -> UsageEvent:
        return UsageEvent(
            event_id=f"uev1_reference_{suffix}",
            occurred_at="2026-09-01T00:00:00Z",
            observed_at="2026-09-01T00:00:00Z",
            machine="test-machine",
            agent="Pulse",
            consumer_path="weekly-patrol",
            source=source,
            runtime="codex",
            provider="openai",
            model=model,
            effort="medium",
            tokens=TokenUsage(input=10, output=2, cache_read=4),
        )

    def price(self, model: str = "deepseek/deepseek-v4-flash-0731") -> OpenRouterPriceSnapshot:
        return OpenRouterPriceSnapshot(
            model_id=model,
            canonical_slug=model,
            retrieved_at="2026-09-01T00:00:00Z",
            prompt_per_token=Decimal("0.001"),
            completion_per_token=Decimal("0.002"),
            input_cache_read_per_token=Decimal("0.0005"),
        )

    def test_one_reference_price_values_heterogeneous_actual_models(self) -> None:
        report = query_events(
            events=[
                self.event("gpt", model="gpt-5.3-codex"),
                self.event("claude", model="claude-opus-4-6"),
            ],
            window=QueryWindow.from_text("2026-08-31", "2026-09-07"),
            prices=[self.price()],
        )
        self.assertEqual(report["totals"]["event_count"], 2)
        self.assertEqual(report["totals"]["priced_event_count"], 2)
        self.assertEqual(report["totals"]["unpriced_event_count"], 0)
        self.assertEqual(report["totals"]["reference_cost_usd"], "0.024")
        self.assertEqual(
            {row["key"] for row in report["groups"]["model"]},
            {"gpt-5.3-codex", "claude-opus-4-6"},
        )
        self.assertEqual(len(report["price_evidence"]), 1)
        self.assertEqual(
            report["price_evidence"][0]["model_id"],
            "deepseek/deepseek-v4-flash-0731",
        )

    def test_actual_model_unknown_does_not_change_reference_valuation(self) -> None:
        report = query_events(
            events=[self.event("unknown-model", model=UNKNOWN)],
            window=QueryWindow.from_text("2026-08-31", "2026-09-07"),
            prices=[self.price()],
        )
        self.assertEqual(report["totals"]["priced_event_count"], 1)
        self.assertEqual(report["totals"]["reference_cost_usd"], "0.012")
        self.assertIn("event_evidence_partial", report["quality"]["flags"])

    def test_unknown_source_remains_unpriced_even_with_reference_price(self) -> None:
        report = query_events(
            events=[self.event("unknown-source", model="gpt-5.3-codex", source=UNKNOWN)],
            window=QueryWindow.from_text("2026-08-31", "2026-09-07"),
            prices=[self.price()],
        )
        self.assertEqual(report["totals"]["priced_event_count"], 0)
        self.assertEqual(report["totals"]["unpriced_event_count"], 1)
        self.assertFalse(report["totals"]["reference_cost_complete"])

    def test_multiple_reference_prices_fail_closed(self) -> None:
        with self.assertRaisesRegex(QueryError, "at most one reference price snapshot"):
            query_events(
                events=[self.event("one", model="gpt-5.3-codex")],
                window=QueryWindow.from_text("2026-08-31", "2026-09-07"),
                prices=[self.price("reference-a"), self.price("reference-b")],
            )


if __name__ == "__main__":
    unittest.main()
