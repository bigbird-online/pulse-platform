from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from pulse_compute_cost.adapters import CollectionDiagnostics, UsageCollection
from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.events import TokenUsage, UsageEvent
from pulse_compute_cost.managed_snapshot import (
    ManagedCostSnapshot,
    ManagedCostSnapshotError,
    build_managed_cost_snapshot,
)
from pulse_compute_cost.openrouter import OpenRouterPriceSnapshot


def price() -> OpenRouterPriceSnapshot:
    return OpenRouterPriceSnapshot(
        model_id="deepseek/reference",
        canonical_slug="deepseek/reference-202608",
        retrieved_at="2026-08-17T00:00:00Z",
        prompt_per_token=Decimal("0.001"),
        completion_per_token=Decimal("0.002"),
        input_cache_read_per_token=Decimal("0.0001"),
    )


def event(event_id: str, *, source: str, agent: str, input: int, output: int) -> UsageEvent:
    runtime = "codex" if source == "chatgpt_pro" else "cc"
    return UsageEvent(
        event_id=event_id,
        occurred_at="2026-08-10T08:00:00Z",
        observed_at="2026-08-10T08:01:00Z",
        machine="vultr",
        agent=agent,
        consumer_path="fixture",
        source=source,
        runtime=runtime if source != "unknown" else "unknown",
        provider="fixture" if source != "unknown" else "unknown",
        model="fixture" if source != "unknown" else "unknown",
        effort="medium" if source != "unknown" else "unknown",
        tokens=TokenUsage(input=input, output=output),
        task_ref="task-1",
        work_item_ref="wi-1",
        outcome_ref="outcome-1",
        retry_count=0,
        escalation_count=0,
        base_route="economy",
        risk_floor="none",
        effective_route="economy",
        verification_result="pass",
        outcome_status="accepted",
        adapter="fixture",
        source_partition="fixture.jsonl",
        source_position=1,
    )


def cash_evidence(coverage: str = "partial") -> dict[str, object]:
    return {
        "source": "openrouter.activity",
        "snapshot_digest": "oractivity1_" + "a" * 64,
        "window_start_utc_day": "2026-07-18",
        "window_end_utc_day": "2026-08-16",
        "coverage": coverage,
    }


def reidentify(prefix: str, value: dict) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return prefix + hashlib.sha256(payload).hexdigest()


class ManagedCostSnapshotTests(unittest.TestCase):
    def test_cli_materializes_current_versioned_snapshot_atomically(self) -> None:
        usage = UsageCollection(
            events=[
                event(
                    "uev1_real_consumer",
                    source="chatgpt_pro",
                    agent="pulse",
                    input=1_000,
                    output=100,
                )
            ],
            diagnostics=CollectionDiagnostics(),
        )
        models = {
            "data": [
                {
                    "id": "deepseek/deepseek-v4-flash-0731",
                    "canonical_slug": "deepseek/deepseek-v4-flash-0731-202608",
                    "pricing": {
                        "prompt": "0.0000001",
                        "completion": "0.0000002",
                        "input_cache_read": "0.00000001",
                    },
                }
            ]
        }
        with tempfile.TemporaryDirectory() as raw_temp:
            temp = Path(raw_temp)
            relay = temp / "relay"
            relay.mkdir()
            output = temp / "state" / "managed-cost-current.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("pulse_compute_cost.cli.collect_usage_events", return_value=usage),
                patch("pulse_compute_cost.cli.fetch_openrouter_models", return_value=models),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = cli_main(
                    [
                        "managed-snapshot",
                        "--product-month", "2026-08",
                        "--machine", "vultr",
                        "--relay-metering-dir", str(relay),
                        "--output", str(output),
                    ]
                )

            self.assertEqual(result, 0, stderr.getvalue())
            rendered = json.loads(stdout.getvalue())
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(persisted, rendered)
            self.assertEqual(rendered["schema"], "managed_cost_snapshot.v1")
            self.assertEqual(rendered["coverage"]["event_count"], 1)
            self.assertEqual(rendered["quality"]["status"], "partial")
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

    def test_dual_ledgers_and_agent_partitions_conserve(self) -> None:
        snapshot = build_managed_cost_snapshot(
            product_month="2026-08",
            events=[
                event("uev1_pro_a", source="chatgpt_pro", agent="a", input=1_000, output=0),
                event("uev1_pro_b", source="chatgpt_pro", agent="b", input=500, output=0),
                event("uev1_api_a", source="openrouter_api", agent="a", input=100, output=10),
            ],
            price=price(),
            generated_at="2026-08-17T00:00:00Z",
            actual_openrouter_usd="99.99",
            actual_openrouter_evidence=cash_evidence(),
            source_coverage_complete=True,
        ).to_dict()

        totals = snapshot["totals"]
        self.assertEqual(totals["pro_reference_cost_usd"], "1.5")
        self.assertEqual(totals["api_reference_cost_usd"], "0.12")
        self.assertEqual(totals["gross_reference_cost_usd"], "1.62")
        self.assertEqual(totals["pro_allocated_cents"], 20_000)
        self.assertEqual(totals["unallocated_cents"], 0)
        self.assertEqual(totals["managed_cost_usd"], "200.12")
        self.assertEqual(totals["actual_openrouter_usd"], "99.99")
        self.assertEqual(snapshot["coverage"]["actual_openrouter"], "partial")
        self.assertEqual(
            sum(Decimal(row["managed_cost_usd"]) for row in snapshot["by_agent"]),
            Decimal(totals["managed_cost_usd"]),
        )
        allocations = {row["agent"]: row["pro_allocated_cents"] for row in snapshot["by_agent"]}
        self.assertEqual(allocations, {"a": 13_333, "b": 6_667})
        self.assertIn("actual_openrouter_cash_partial", snapshot["quality"]["flags"])

    def test_zero_pro_weight_keeps_subscription_in_unallocated_partition(self) -> None:
        snapshot = build_managed_cost_snapshot(
            product_month="2026-08",
            events=[event("uev1_api", source="openrouter_api", agent="pulse", input=100, output=0)],
            price=price(),
            generated_at="2026-08-17T00:00:00Z",
            actual_openrouter_usd="0.1",
            actual_openrouter_evidence=cash_evidence(),
            source_coverage_complete=True,
        ).to_dict()
        self.assertEqual(snapshot["totals"]["pro_allocated_cents"], 0)
        self.assertEqual(snapshot["totals"]["unallocated_cents"], 20_000)
        self.assertEqual(snapshot["by_agent"][-1]["agent"], "unallocated")
        self.assertEqual(snapshot["by_agent"][-1]["managed_cost_usd"], "200")
        self.assertEqual(snapshot["totals"]["managed_cost_usd"], "200.1")

    def test_unknown_source_is_unpriced_partial_and_not_folded_into_zero_evidence(self) -> None:
        snapshot = build_managed_cost_snapshot(
            product_month="2026-08",
            events=[event("uev1_unknown", source="unknown", agent="unknown", input=9_999, output=9_999)],
            price=price(),
            generated_at="2026-08-17T00:00:00Z",
        ).to_dict()
        self.assertEqual(snapshot["coverage"]["priced_event_count"], 0)
        self.assertEqual(snapshot["coverage"]["unpriced_event_count"], 1)
        self.assertIn("unpriced_events", snapshot["quality"]["flags"])
        unattributed = next(row for row in snapshot["by_agent"] if row["agent"] == "unattributed")
        self.assertEqual(unattributed["unpriced_event_count"], 1)
        self.assertEqual(snapshot["totals"]["actual_openrouter_usd"], None)

    def test_real_scale_prices_serialize_without_scientific_notation(self) -> None:
        tiny = OpenRouterPriceSnapshot(
            model_id="deepseek/reference",
            canonical_slug="deepseek/reference-202608",
            retrieved_at="2026-08-17T00:00:00Z",
            prompt_per_token=Decimal("0.00000005"),
            completion_per_token=Decimal("0.00000008"),
            input_cache_read_per_token=Decimal("0.00000001"),
        )
        snapshot = build_managed_cost_snapshot(
            product_month="2026-08",
            events=[event("uev1_tiny", source="openrouter_api", agent="pulse", input=1, output=0)],
            price=tiny,
            generated_at="2026-08-17T01:00:00Z",
        ).to_dict()
        self.assertEqual(snapshot["totals"]["api_reference_cost_usd"], "0.00000005")
        self.assertNotIn("E", snapshot["totals"]["gross_reference_cost_usd"])

    def test_reserved_agent_partition_and_future_evidence_fail_closed(self) -> None:
        with self.assertRaisesRegex(ManagedCostSnapshotError, "reserved cost partition"):
            build_managed_cost_snapshot(
                product_month="2026-08",
                events=[event("uev1_reserved", source="chatgpt_pro", agent="unallocated", input=1, output=0)],
                price=price(), generated_at="2026-08-17T01:00:00Z",
            )
        with self.assertRaisesRegex(ManagedCostSnapshotError, "cannot precede"):
            build_managed_cost_snapshot(
                product_month="2026-08",
                events=[event("uev1_future", source="chatgpt_pro", agent="a", input=1, output=0)],
                price=price(), generated_at="2026-08-10T08:00:30Z",
            )

    def test_claude_subscription_is_not_settled_as_api_or_pro(self) -> None:
        with self.assertRaisesRegex(ManagedCostSnapshotError, "claude_subscription"):
            build_managed_cost_snapshot(
                product_month="2026-08",
                events=[event("uev1_claude", source="claude_subscription", agent="a", input=1, output=0)],
                price=price(), generated_at="2026-08-17T01:00:00Z",
            )

    def test_invalid_cash_amount_uses_snapshot_error_boundary(self) -> None:
        with self.assertRaisesRegex(ManagedCostSnapshotError, "actual_openrouter_usd"):
            build_managed_cost_snapshot(
                product_month="2026-08", events=[], price=price(),
                generated_at="2026-08-17T01:00:00Z",
                actual_openrouter_usd="-1",
                actual_openrouter_evidence=cash_evidence(),
            )

    def test_cash_amount_without_sanitized_evidence_fails_closed(self) -> None:
        with self.assertRaisesRegex(ManagedCostSnapshotError, "evidence is required"):
            build_managed_cost_snapshot(
                product_month="2026-08", events=[], price=price(),
                generated_at="2026-08-17T01:00:00Z",
                actual_openrouter_usd="1",
            )

    def test_complete_cash_coverage_requires_closed_full_month_window(self) -> None:
        with self.assertRaisesRegex(ManagedCostSnapshotError, "closed monthly"):
            build_managed_cost_snapshot(
                product_month="2026-08", events=[], price=price(),
                generated_at="2026-09-02T01:00:00Z",
                actual_openrouter_usd="1",
                actual_openrouter_coverage="complete",
                actual_openrouter_evidence=cash_evidence("complete"),
            )
        complete = cash_evidence("complete")
        complete["window_start_utc_day"] = "2026-07-31"
        complete["window_end_utc_day"] = "2026-08-31"
        built = build_managed_cost_snapshot(
            product_month="2026-08", events=[], price=price(),
            generated_at="2026-09-02T01:00:00Z", settlement_status="closed",
            actual_openrouter_usd="1", actual_openrouter_coverage="complete",
            actual_openrouter_evidence=complete, source_coverage_complete=True,
        )
        self.assertEqual(built.to_dict()["quality"], {"status": "complete", "flags": []})

    def test_event_outside_product_month_fails_closed(self) -> None:
        with self.assertRaisesRegex(ManagedCostSnapshotError, "all events must belong"):
            build_managed_cost_snapshot(
                product_month="2026-07",
                events=[event("uev1_august", source="chatgpt_pro", agent="a", input=1, output=0)],
                price=price(),
                generated_at="2026-08-17T00:00:00Z",
            )

    def test_snapshot_id_and_conservation_tampering_fail_validation(self) -> None:
        valid = build_managed_cost_snapshot(
            product_month="2026-08",
            events=[event("uev1_pro", source="chatgpt_pro", agent="a", input=1, output=0)],
            price=price(),
            generated_at="2026-08-17T00:00:00Z",
        ).to_dict()
        self.assertEqual(ManagedCostSnapshot.from_dict(valid).to_dict(), valid)

        for mutate, message in (
            (lambda value: value["totals"].__setitem__("unallocated_cents", 1), "20000"),
            (lambda value: value["by_agent"][0].__setitem__("managed_cost_usd", "0"), "inconsistent"),
            (lambda value: value.__setitem__("product_month", "August"), "product_month"),
        ):
            damaged = deepcopy(valid)
            mutate(damaged)
            with self.assertRaisesRegex(ManagedCostSnapshotError, message):
                ManagedCostSnapshot.from_dict(damaged)

    def test_validator_rejects_future_price_even_with_recomputed_identities(self) -> None:
        damaged = build_managed_cost_snapshot(
            product_month="2026-08", events=[], price=price(),
            generated_at="2026-08-17T00:00:00Z",
        ).to_dict()
        damaged["price_snapshot"]["retrieved_at"] = "2026-08-17T00:00:01Z"
        price_content = dict(damaged["price_snapshot"])
        price_content.pop("snapshot_id")
        damaged["price_snapshot"]["snapshot_id"] = reidentify(
            "orprice1_", price_content,
        )
        snapshot_content = dict(damaged)
        snapshot_content.pop("snapshot_id")
        damaged["snapshot_id"] = reidentify("mcs1_", snapshot_content)

        with self.assertRaisesRegex(ManagedCostSnapshotError, "cannot precede price"):
            ManagedCostSnapshot.from_dict(damaged)

    def test_equal_aggregates_from_different_event_sets_have_distinct_identity(self) -> None:
        left = build_managed_cost_snapshot(
            product_month="2026-08",
            events=[event("uev1_left", source="chatgpt_pro", agent="a", input=10, output=0)],
            price=price(), generated_at="2026-08-17T01:00:00Z",
        ).to_dict()
        right = build_managed_cost_snapshot(
            product_month="2026-08",
            events=[event("uev1_right", source="chatgpt_pro", agent="a", input=10, output=0)],
            price=price(), generated_at="2026-08-17T01:00:00Z",
        ).to_dict()
        self.assertEqual(left["totals"], right["totals"])
        self.assertNotEqual(
            left["input_identity"]["event_set_digest"],
            right["input_identity"]["event_set_digest"],
        )
        self.assertNotEqual(left["snapshot_id"], right["snapshot_id"])

    def test_equal_event_id_with_changed_semantic_fact_changes_event_set_identity(self) -> None:
        original = event(
            "uev1_same_upstream", source="chatgpt_pro", agent="a", input=10, output=0
        )
        corrected = replace(original, outcome_status="rejected")
        common = {
            "product_month": "2026-08",
            "price": price(),
            "generated_at": "2026-08-17T00:00:00Z",
        }
        left = build_managed_cost_snapshot(events=[original], **common).to_dict()
        right = build_managed_cost_snapshot(events=[corrected], **common).to_dict()
        self.assertNotEqual(
            left["input_identity"]["event_set_digest"],
            right["input_identity"]["event_set_digest"],
        )


if __name__ == "__main__":
    unittest.main()
