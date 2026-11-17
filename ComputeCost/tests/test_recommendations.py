from __future__ import annotations

import hashlib
import json
import unittest

from pulse_compute_cost.recommendations import (
    RecommendationError,
    build_recommendation_artifact,
    validate_recommendation_artifact,
)


class RecommendationArtifactTests(unittest.TestCase):
    def facts(self, **overrides):
        value = {
            "requested_status": "promote",
            "target_consumer": "weekly-patrol",
            "route_name": "weekly-patrol",
            "current_preset": "standard",
            "target_preset": "high",
            "rollback_preset": "standard",
            "approved_presets": ["low", "standard", "high"],
            "allowed_transitions": [["standard", "high"], ["standard", "low"]],
            "evidence_window": {"from": "2026-08-31", "to": "2026-09-07"},
            "coverage": {
                "event_count": 10,
                "priced_event_count": 10,
                "outcome_linked_event_count": 10,
                "conservation_status": "pass",
            },
            "expected_reference_cost_change_usd": "-1.25",
            "accepted_outcome_change": 0,
            "risk_floor": "standard",
            "critical_route": False,
            "frontier_satisfied": True,
            "price_fresh": True,
            "active_route_digest": "routecfg1_" + "a" * 64,
            "plan_digest": "plan1_" + "0" * 64,
            "generated_at": "2026-09-07T00:00:00Z",
            "expires_at": "2026-09-08T00:00:00Z",
        }
        manager_override = overrides.pop("manager_plan", None)
        explicit_plan_digest = "plan_digest" in overrides
        value.update(overrides)
        candidate_target = (
            value["rollback_preset"]
            if value["requested_status"] == "rollback"
            else value["target_preset"]
        )
        changed = candidate_target != value["current_preset"]
        plan = {
            "schema": "runtime_priority_plan.v1",
            "schema_version": 1,
            "mode": "plan_only",
            "current_preset": value["current_preset"],
            "target_preset": candidate_target,
            "active_route_digest": value["active_route_digest"],
            "target_route_digest": "routecfg1_" + "c" * 64,
            "current_runtime_profile_digest": "runtimecfg1_" + "d" * 64,
            "target_runtime_profile_digest": "runtimecfg1_" + "d" * 64,
            "global_compute_profile_changed": False,
            "route_diff": (
                [{
                    "route_name": value["route_name"],
                    "consumer_path": value["target_consumer"],
                    "from_effective_profile": value["current_preset"],
                    "to_effective_profile": candidate_target,
                    "effective_profile_changed": True,
                }]
                if changed else []
            ),
            "expected_changed_routes": [value["route_name"]] if changed else [],
            "single_consumer_candidate": changed,
            "approved_presets": sorted(value["approved_presets"]),
            "allowed_transitions": sorted(value["allowed_transitions"]),
            "precondition": {
                "active_route_digest": value["active_route_digest"],
                "current_preset": value["current_preset"],
            },
        }
        encoded = json.dumps(
            plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        plan["plan_digest"] = "plan1_" + hashlib.sha256(encoded).hexdigest()
        if not explicit_plan_digest:
            value["plan_digest"] = plan["plan_digest"]
        value["manager_plan"] = plan if manager_override is None else manager_override
        return value

    def test_promote_is_recommend_only_and_changes_exactly_one_route(self) -> None:
        artifact = build_recommendation_artifact(self.facts())
        validate_recommendation_artifact(artifact)
        self.assertEqual(artifact["status"], "promote")
        self.assertEqual(artifact["mode"], "recommend_only")
        self.assertEqual(artifact["expected_changed_routes"], ["weekly-patrol"])
        self.assertFalse(artifact["authorization"]["production_apply_authorized"])
        self.assertEqual(artifact["authorization"]["duke_decision"], "pending")
        self.assertEqual(artifact["validity"]["ttl_seconds"], 86400)

    def test_stale_price_fails_closed_to_insufficient_evidence(self) -> None:
        artifact = build_recommendation_artifact(self.facts(price_fresh=False))
        validate_recommendation_artifact(artifact)
        self.assertEqual(artifact["status"], "insufficient_evidence")
        self.assertEqual(artifact["target_preset"], "standard")
        self.assertEqual(artifact["expected_changed_routes"], [])
        self.assertIn("price_stale", artifact["reasons"])

    def test_evidence_failure_can_emit_only_an_approved_rollback_transition(self) -> None:
        artifact = build_recommendation_artifact(
            self.facts(
                requested_status="rollback",
                current_preset="high",
                target_preset="high",
                rollback_preset="standard",
                allowed_transitions=[["high", "standard"]],
                price_fresh=False,
            )
        )
        validate_recommendation_artifact(artifact)
        self.assertEqual(artifact["status"], "rollback")
        self.assertEqual(artifact["current_preset"], "high")
        self.assertEqual(artifact["target_preset"], "standard")
        self.assertEqual(artifact["rollback_preset"], "standard")
        self.assertEqual(artifact["expected_changed_routes"], ["weekly-patrol"])
        self.assertIn("price_stale", artifact["reasons"])
        self.assertFalse(artifact["authorization"]["production_apply_authorized"])

    def test_outcome_and_conservation_gaps_cannot_promote(self) -> None:
        artifact = build_recommendation_artifact(
            self.facts(
                coverage={
                    "event_count": 10,
                    "priced_event_count": 10,
                    "outcome_linked_event_count": 9,
                    "conservation_status": "failed",
                }
            )
        )
        validate_recommendation_artifact(artifact)
        self.assertEqual(artifact["status"], "insufficient_evidence")
        self.assertIn("outcome_coverage_incomplete", artifact["reasons"])
        self.assertIn("conservation_failed", artifact["reasons"])

    def test_critical_route_below_frontier_cannot_promote(self) -> None:
        artifact = build_recommendation_artifact(
            self.facts(critical_route=True, frontier_satisfied=False)
        )
        validate_recommendation_artifact(artifact)
        self.assertEqual(artifact["status"], "insufficient_evidence")
        self.assertIn("critical_route_below_frontier", artifact["reasons"])

    def test_unapproved_target_is_not_echoed_into_artifact(self) -> None:
        artifact = build_recommendation_artifact(
            self.facts(target_preset="arbitrary/model-string")
        )
        validate_recommendation_artifact(artifact)
        self.assertEqual(artifact["status"], "insufficient_evidence")
        self.assertEqual(artifact["target_preset"], "standard")
        self.assertIn("no_approved_preset", artifact["reasons"])
        self.assertNotIn("arbitrary/model-string", json.dumps(artifact, sort_keys=True))

    def test_disallowed_transition_fails_closed(self) -> None:
        artifact = build_recommendation_artifact(
            self.facts(allowed_transitions=[["standard", "low"]])
        )
        validate_recommendation_artifact(artifact)
        self.assertEqual(artifact["status"], "insufficient_evidence")
        self.assertEqual(artifact["target_preset"], "standard")
        self.assertIn("no_approved_preset", artifact["reasons"])
        self.assertIn("transition_not_allowed", artifact["reasons"])

    def test_promote_without_manager_plan_fails_closed(self) -> None:
        facts = self.facts()
        facts.pop("manager_plan")
        artifact = build_recommendation_artifact(facts)
        validate_recommendation_artifact(artifact)
        self.assertEqual(artifact["status"], "insufficient_evidence")
        self.assertIn("manager_plan_unbound", artifact["reasons"])
        self.assertFalse(artifact["quality_constraints"]["manager_plan_bound"])

    def test_manager_plan_digest_or_route_diff_mismatch_fails_closed(self) -> None:
        facts = self.facts()
        facts["manager_plan"]["plan_digest"] = "plan1_" + "0" * 64
        artifact = build_recommendation_artifact(facts)
        self.assertEqual(artifact["status"], "insufficient_evidence")
        self.assertIn("manager_plan_digest_mismatch", artifact["reasons"])

        facts = self.facts()
        facts["manager_plan"]["expected_changed_routes"] = ["another-route"]
        facts["manager_plan"]["single_consumer_candidate"] = False
        artifact = build_recommendation_artifact(facts)
        self.assertEqual(artifact["status"], "insufficient_evidence")
        self.assertIn("manager_plan_route_diff_mismatch", artifact["reasons"])
        self.assertIn("manager_plan_not_single_consumer", artifact["reasons"])

    def test_tampered_apply_authorization_is_rejected(self) -> None:
        artifact = build_recommendation_artifact(self.facts())
        artifact["authorization"]["production_apply_authorized"] = True
        with self.assertRaisesRegex(RecommendationError, "cannot authorize"):
            validate_recommendation_artifact(artifact)

    def test_current_preset_must_come_from_manager_catalog(self) -> None:
        with self.assertRaisesRegex(RecommendationError, "current_preset"):
            build_recommendation_artifact(
                self.facts(approved_presets=["low", "high"])
            )


if __name__ == "__main__":
    unittest.main()
