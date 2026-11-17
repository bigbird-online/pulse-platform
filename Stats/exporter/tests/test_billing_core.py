"""Reviewed subscription config and integer allocation unit contract."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

from pulse_stats.billing_core import (
    AttributionEvidence, BillingConfigError, SubscriptionComponent,
    allocate_subscription_costs, load_subscription_components,
)


NOW = datetime(2026, 7, 25, 8, tzinfo=timezone.utc)


def component(component_id, cents, attribution, start="2026-07-01", end=None):
    return SubscriptionComponent(
        component_id, cents, "USD", start, end,
        f"owner-reviewed:{component_id}", attribution,
    )


def components():
    return (
        component("claude_code", 25_000, "cc_mtd_output_share"),
        component("codex", 2_000, "unattributed_no_agent_source"),
    )


def payload(items=None):
    items = items or components()
    return {
        "schema_version": 1,
        "components": [
            {
                "component_id": c.component_id, "amount_cents": c.amount_cents,
                "currency": c.currency, "effective_from": c.effective_from,
                "effective_until": c.effective_until, "source_ref": c.source_ref,
                "attribution": c.attribution,
            }
            for c in items
        ],
    }


def evidence(
    agents=(("pulse", 100),), missing=0, full=False, count=1,
    outcome="usable", reason=None,
):
    return AttributionEvidence(
        "2026-07", 25 if full else 0, 25, full, count,
        agents, missing, outcome, reason,
    )


class ConfigTests(unittest.TestCase):
    def _write(self, root, value):
        path = root / "subscriptions.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_loads_current_components_and_dynamic_total(self):
        with tempfile.TemporaryDirectory() as directory:
            found = load_subscription_components(
                self._write(pathlib.Path(directory), payload()), NOW,
            )
        self.assertEqual([c.component_id for c in found], ["claude_code", "codex"])
        self.assertEqual(sum(c.amount_cents for c in found), 27_000)

    def test_invalid_config_is_rejected_strictly(self):
        mutations = (
            lambda p: p["components"].append(dict(p["components"][0])),
            lambda p: p["components"][0].update(amount_cents=True),
            lambda p: p["components"][0].update(amount_cents=-1),
            lambda p: p["components"][0].update(currency="CNY"),
            lambda p: p["components"][0].update(effective_from="July"),
            lambda p: p["components"][0].update(effective_until="2026-06-30"),
            lambda p: p["components"][0].update(source_ref=""),
            lambda p: p["components"][0].update(attribution="token_price"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for mutate in mutations:
                value = payload()
                mutate(value)
                with self.subTest(value=value), self.assertRaises(BillingConfigError):
                    load_subscription_components(self._write(root, value), NOW)
            with self.assertRaises(BillingConfigError):
                load_subscription_components(root / "missing.json", NOW)
            (root / "broken.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(BillingConfigError):
                load_subscription_components(root / "broken.json", NOW)

    def test_inactive_rows_are_filtered_and_dates_are_inclusive(self):
        items = components() + (
            component("expired", 1, "unattributed_no_agent_source",
                      start="2026-07-01", end="2026-07-24"),
            component("future", 1, "unattributed_no_agent_source",
                      start="2026-07-26"),
            component("ends_today", 1, "unattributed_no_agent_source",
                      start="2026-07-25", end="2026-07-25"),
        )
        with tempfile.TemporaryDirectory() as directory:
            found = load_subscription_components(
                self._write(pathlib.Path(directory), payload(items)), NOW,
            )
        self.assertEqual(
            [c.component_id for c in found],
            ["claude_code", "codex", "ends_today"],
        )

    def test_active_empty_is_invalid_but_named_component_may_be_absent(self):
        future = (
            component("codex", 2_000, "unattributed_no_agent_source",
                      start="2026-07-26"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with self.assertRaises(BillingConfigError):
                load_subscription_components(self._write(root, payload(future)), NOW)
            found = load_subscription_components(
                self._write(root, payload((components()[1],))), NOW,
            )
        self.assertEqual([c.component_id for c in found], ["codex"])


class AllocationTests(unittest.TestCase):
    def test_verified_zero_and_component_without_agent_source(self):
        zero = allocate_subscription_costs(
            components(), evidence(agents=(), full=True, count=0),
        )
        self.assertEqual((zero.status, zero.quality), ("available", "validated"))
        self.assertEqual((zero.by_agent, zero.attributed_cents,
                          zero.unattributed_cents, zero.sample_count),
                         ((), 0, 27_000, 0))
        codex = allocate_subscription_costs(
            (components()[1],), evidence(),
        )
        self.assertEqual((codex.by_agent, codex.attributed_cents,
                          codex.unattributed_cents), ((), 0, 2_000))

    def test_largest_remainder_is_stable_and_exact(self):
        result = allocate_subscription_costs(
            components(), evidence(agents=(("c", 1), ("b", 1), ("a", 1)), count=3),
        )
        rows = {row.agent: row.attributed_cents for row in result.by_agent}
        self.assertEqual(rows, {"a": 8_334, "b": 8_333, "c": 8_333})
        self.assertEqual(tuple(row.agent for row in result.by_agent), ("a", "b", "c"))
        self.assertEqual(sum(rows.values()) + result.unattributed_cents, 27_000)

    def test_missing_agent_share_and_codex_are_both_unattributed(self):
        result = allocate_subscription_costs(
            components(), evidence(agents=(("pulse", 75),), missing=25, count=2),
        )
        self.assertEqual(result.by_agent[0].attributed_cents, 18_750)
        self.assertEqual((result.attributed_cents, result.unattributed_cents),
                         (18_750, 8_250))

    def test_partial_and_failure_states_never_fabricate_zero(self):
        positive = allocate_subscription_costs(components(), evidence())
        self.assertEqual((positive.status, positive.quality),
                         ("available", "descriptive"))
        self.assertEqual(positive.by_agent[0].attributed_cents, 25_000)
        cases = (
            evidence(agents=(), count=0),
            evidence(outcome="insufficient", reason="gap"),
            evidence(outcome="unavailable", reason="bad"),
        )
        for item in cases:
            result = allocate_subscription_costs(components(), item)
            self.assertEqual(result.status, "unknown")
            self.assertIsNone(result.by_agent)
            self.assertIsNone(result.attributed_cents)
            self.assertIsNone(result.sample_count)


if __name__ == "__main__":
    unittest.main()
