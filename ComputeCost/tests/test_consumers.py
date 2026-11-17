from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import unittest

from pulse_compute_cost.consumers import (
    ConsumerError,
    consume_duke_recommendation,
    consume_weekly_query,
)
from pulse_compute_cost.events import Lineage, TokenUsage, UsageEvent
from pulse_compute_cost.openrouter import OpenRouterPriceSnapshot
from pulse_compute_cost.query import QueryWindow, query_events
from pulse_compute_cost.recommendations import build_recommendation_artifact


class S3ConsumerTests(unittest.TestCase):
    def weekly_query(self):
        event = UsageEvent(
            event_id="uev1_weekly-consumer",
            occurred_at="2026-09-01T00:00:00Z",
            observed_at="2026-09-01T00:00:00Z",
            machine="test-machine",
            agent="Hestia",
            consumer_path="hestia_weekly_review",
            source="chatgpt_pro",
            runtime="codex",
            provider="openai",
            model="model-a",
            effort="medium",
            tokens=TokenUsage(input=10, output=2, cache_read=4),
            task_ref="task-s3",
            outcome_ref="outcome-s3",
            retry_count=0,
            escalation_count=0,
            outcome_status="accepted",
            lineage=Lineage(
                root_agent="Hestia",
                execution_mode="automatic",
                session_role="main",
            ),
        )
        price = OpenRouterPriceSnapshot(
            model_id="model-a",
            canonical_slug="provider/model-a",
            retrieved_at="2026-09-07T00:00:00Z",
            prompt_per_token=Decimal("0.001"),
            completion_per_token=Decimal("0.002"),
            input_cache_read_per_token=Decimal("0.0005"),
        )
        return query_events(
            events=[event],
            window=QueryWindow.from_text("2026-08-31", "2026-09-07"),
            prices=[price],
            generated_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        )

    def recommendation(self):
        facts = {
            "requested_status": "promote",
            "target_consumer": "hestia_weekly_review",
            "route_name": "hestia_weekly_review",
            "current_preset": "current_safe",
            "target_preset": "phase2_stats_canary",
            "rollback_preset": "current_safe",
            "approved_presets": ["current_safe", "phase2_stats_canary"],
            "allowed_transitions": [["current_safe", "phase2_stats_canary"]],
            "evidence_window": {"from": "2026-08-31", "to": "2026-09-07"},
            "coverage": {
                "event_count": 1,
                "priced_event_count": 1,
                "outcome_linked_event_count": 1,
                "conservation_status": "pass",
            },
            "expected_reference_cost_change_usd": "-0.01",
            "accepted_outcome_change": 0,
            "risk_floor": "standard",
            "critical_route": False,
            "frontier_satisfied": True,
            "price_fresh": True,
            "active_route_digest": "routecfg1_" + "a" * 64,
            "plan_digest": "",
            "generated_at": "2026-09-07T00:00:00Z",
            "expires_at": "2026-09-08T00:00:00Z",
        }
        plan = {
            "schema": "runtime_priority_plan.v1",
            "schema_version": 1,
            "mode": "plan_only",
            "current_preset": "current_safe",
            "target_preset": "phase2_stats_canary",
            "active_route_digest": facts["active_route_digest"],
            "target_route_digest": "routecfg1_" + "b" * 64,
            "current_runtime_profile_digest": "runtimecfg1_" + "c" * 64,
            "target_runtime_profile_digest": "runtimecfg1_" + "c" * 64,
            "global_compute_profile_changed": False,
            "route_diff": [{
                "route_name": "hestia_weekly_review",
                "consumer_path": "hestia_weekly_review",
                "from_effective_profile": "platform_default",
                "to_effective_profile": "media",
                "effective_profile_changed": True,
            }],
            "expected_changed_routes": ["hestia_weekly_review"],
            "single_consumer_candidate": True,
            "approved_presets": ["current_safe", "phase2_stats_canary"],
            "allowed_transitions": [["current_safe", "phase2_stats_canary"]],
            "precondition": {
                "active_route_digest": facts["active_route_digest"],
                "current_preset": "current_safe",
            },
        }
        encoded = json.dumps(
            plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        plan["plan_digest"] = "plan1_" + hashlib.sha256(encoded).hexdigest()
        facts["plan_digest"] = plan["plan_digest"]
        facts["manager_plan"] = plan
        return build_recommendation_artifact(facts)

    def test_weekly_consumer_projects_query_without_recalculation(self):
        query = self.weekly_query()
        consumed = consume_weekly_query(query)
        self.assertEqual(consumed["schema"], "weekly_cost_consumption.v1")
        self.assertEqual(consumed["mode"], "shadow_read_only")
        self.assertFalse(consumed["manual_recalculation_required"])
        self.assertEqual(consumed["totals"], query["totals"])
        self.assertEqual(consumed["by_agent"], query["by_agent"])
        self.assertEqual(consumed["by_consumer"], query["by_consumer"])
        self.assertEqual(consumed["by_outcome"], query["by_outcome"])
        self.assertEqual(consumed["driver_delta"]["status"], "insufficient_evidence")

    def test_weekly_consumer_refuses_tampered_partition_or_bridge(self):
        query = self.weekly_query()
        query["by_agent"] = []
        with self.assertRaisesRegex(ConsumerError, "agent partition"):
            consume_weekly_query(query)

        query = self.weekly_query()
        query["driver_delta"] = {"status": "complete"}
        with self.assertRaisesRegex(ConsumerError, "S4 driver bridge"):
            consume_weekly_query(query)

    def test_duke_consumer_is_review_only_and_does_not_recalculate(self):
        artifact = self.recommendation()
        consumed = consume_duke_recommendation(artifact)
        self.assertEqual(consumed["schema"], "duke_recommendation_consumption.v1")
        self.assertEqual(consumed["mode"], "recommend_only_review")
        self.assertFalse(consumed["production_apply_authorized"])
        self.assertFalse(consumed["manual_recalculation_required"])
        self.assertEqual(consumed["plan_digest"], artifact["plan_digest"])
        self.assertEqual(consumed["coverage"], artifact["coverage"])
        self.assertEqual(consumed["duke_decision"], "pending")

    def test_duke_consumer_refuses_tampered_apply_authorization(self):
        artifact = deepcopy(self.recommendation())
        artifact["authorization"]["production_apply_authorized"] = True
        with self.assertRaisesRegex(ConsumerError, "cannot authorize"):
            consume_duke_recommendation(artifact)


if __name__ == "__main__":
    unittest.main()
