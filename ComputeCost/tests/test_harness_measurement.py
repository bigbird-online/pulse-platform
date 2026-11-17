from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.app_server_usage import AppServerUsageLedger
from pulse_compute_cost.harness_measurement import (
    HarnessMeasurementError,
    measurement_join_coverage,
    validate_harness_measurement_snapshot,
)
from pulse_compute_cost.wi_report import build_wi_report


METRICS = (
    "quality.first_pass_review",
    "quality.review_rework_rounds",
    "quality.final_review_status",
    "quality.delivery_status",
    "quality.effect_status",
    "quality.post_approval_corrections",
    "efficiency.execution_started_at",
    "efficiency.delivery_accepted_at",
    "efficiency.result_cycle_seconds",
    "efficiency.session_segment_count",
    "efficiency.planned_rollover_count",
    "efficiency.runtime_recovery_count",
    "efficiency.human_reexplanation_count",
    "cost.token_hard_cost",
    "cost.human_attention",
)


def measurement_snapshot(*, work_item: str = "wi_20260822_730") -> dict[str, object]:
    quality = {
        "first_pass_review": True,
        "review_rework_rounds": 1,
        "final_review_status": "approved",
        "delivery_status": "accepted",
        "effect_status": "adopted_by_consumer",
        "post_approval_corrections": 2,
    }
    efficiency = {
        "execution_started_at": "2026-08-22T00:00:00Z",
        "delivery_accepted_at": "2026-08-25T00:00:00Z",
        "result_cycle_seconds": 259200,
        "session_segment_count": 3,
        "planned_rollover_count": 2,
        "runtime_recovery_count": 1,
        "human_reexplanation_count": 0,
    }
    token_cost = {
        "total_tokens": 1000,
        "input_tokens": 800,
        "cached_input_tokens": 600,
        "output_tokens": 200,
        "actual_cash_usd": "0",
        "source_ref": ".harness/evidence/compute-cost/wi730.json",
    }
    values = {
        **{f"quality.{key}": value for key, value in quality.items()},
        **{f"efficiency.{key}": value for key, value in efficiency.items()},
        "cost.token_hard_cost": 1000,
        "cost.human_attention": None,
    }
    readouts: dict[str, object] = {}
    for metric in METRICS:
        unavailable = metric == "cost.human_attention"
        readouts[metric] = {
            "window": {
                "start": "2026-08-22T00:00:00Z",
                "end": "2026-08-25T00:00:00Z",
            },
            "as_of": "2026-08-25T00:00:00Z",
            "eligible_denominator": 1,
            "observed_denominator": 0 if unavailable else 1,
            "coverage": "0" if unavailable else "1",
            "status": "unavailable" if unavailable else "computable",
            "value": None if unavailable else values[metric],
            "unknown_count": 1 if unavailable else 0,
            "source_refs": [] if unavailable else [".harness/events/wi730.events.yaml"],
        }
    return {
        "schema": "s1_measurement_pilot.v1",
        "work_item_id": work_item,
        "measurement_eligibility": {
            "eligible": True,
            "result_class": "behavior_change",
        },
        "quality": quality,
        "efficiency": efficiency,
        "cost": {
            "token_hard_cost": token_cost,
            "human_attention": "unknown",
        },
        "evidence": {
            "as_of": "2026-08-25T00:00:00Z",
            "readouts": readouts,
            "missing_producers": ["cost.human_attention"],
            "authority_gaps": [],
            "sensor_health": "degraded",
        },
    }


class HarnessMeasurementTests(unittest.TestCase):
    def test_valid_projection_normalizes_cash_and_is_digest_bound(self) -> None:
        first = validate_harness_measurement_snapshot(
            measurement_snapshot(), expected_work_item_id="wi_20260822_730"
        )
        second = validate_harness_measurement_snapshot(
            measurement_snapshot(), expected_work_item_id="wi_20260822_730"
        )
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertEqual(first["normalized_actual_cash_usd"], "0")
        self.assertEqual(measurement_join_coverage(first), "exact")
        self.assertEqual(
            first["safe_source_refs"],
            [
                ".harness/events/wi730.events.yaml",
                ".harness/evidence/compute-cost/wi730.json",
            ],
        )

    def test_wi_report_keeps_native_zero_cash_non_authoritative_under_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = build_wi_report(
                AppServerUsageLedger(Path(directory) / "usage.jsonl"),
                "wi_20260822_730",
                measurement_snapshot=measurement_snapshot(),
            )
        self.assertEqual(report["outcome"]["quality"]["delivery_status"], "accepted")
        self.assertEqual(
            report["outcome"]["quality"]["human_review"]["final_status"],
            "approved",
        )
        self.assertEqual(report["outcome"]["downstream_effect"], "adopted_by_consumer")
        self.assertEqual(report["outcome"]["efficiency"]["result_cycle_seconds"], 259200)
        # The Harness native 0 is visible context only; it never becomes the WI
        # machine cash, never selects a basis, and never claims cash coverage.
        self.assertEqual(report["cost"]["provider_native_cash_usd"], "0")
        self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
        self.assertEqual(
            report["cost"]["cash_basis"],
            "openrouter_standard_token_cash",
        )
        self.assertEqual(report["cost"]["cash_coverage"], "unavailable")
        self.assertEqual(report["coverage"]["cash"], "unavailable")
        self.assertEqual(report["coverage"]["outcome_join"], "exact")
        self.assertEqual(report["cost"]["human_attention"]["status"], "unavailable")
        self.assertEqual(
            report["cost"]["human_attention"]["exact_human_feedback_count"],
            "unavailable",
        )
        self.assertEqual(report["coverage"]["human_attention"], "unavailable")
        self.assertEqual(report["coverage"]["terminal_measurement"], "incomplete")
        self.assertEqual(
            report["outcome"]["measurement_readiness"]["final_token_seal"],
            "unavailable",
        )
        self.assertIn("roi_eligible_measurement_incomplete", report["warnings"])
        self.assertIn("provider_native_cash_not_wi_cash_authority", report["warnings"])
        self.assertRegex(
            report["evidence"]["harness_measurement_snapshot_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_noneligible_legacy_wi_remains_reportable_but_measurement_is_ineligible(self) -> None:
        snapshot = measurement_snapshot()
        snapshot["measurement_eligibility"] = {
            "eligible": False,
            "result_class": "other",
        }
        with tempfile.TemporaryDirectory() as directory:
            report = build_wi_report(
                AppServerUsageLedger(Path(directory) / "usage.jsonl"),
                "wi_20260822_730",
                measurement_snapshot=snapshot,
            )
        self.assertEqual(report["coverage"]["terminal_measurement"], "ineligible")
        self.assertEqual(
            report["outcome"]["measurement_readiness"]["status"], "ineligible"
        )
        self.assertEqual(report["usage"]["tokens"], "unavailable")
        self.assertIn("measurement_ineligible", report["warnings"])

    def test_wi_report_exposes_observed_human_obligations_without_inventing_time_or_cash(self) -> None:
        snapshot = measurement_snapshot()
        attention = {
            "raw_turn_count": 3,
            "human_attention_units": "3.25",
            "effort_band_counts": {
                "micro": 1,
                "bounded": 1,
                "judgment": 1,
                "framing": 0,
            },
            "contract_version": "wi028-g0-v1",
            "source_ref": ".harness/evidence/measurements/wi_20260822_730/human-attention.json",
        }
        snapshot["cost"]["human_attention"] = attention  # type: ignore[index]
        readout = snapshot["evidence"]["readouts"]["cost.human_attention"]  # type: ignore[index]
        readout.update(
            {
                "observed_denominator": 1,
                "coverage": "1",
                "status": "computable",
                "value": "3.25",
                "unknown_count": 0,
                "source_refs": [attention["source_ref"]],
            }
        )
        snapshot["evidence"]["missing_producers"] = []  # type: ignore[index]
        snapshot["evidence"]["sensor_health"] = "healthy"  # type: ignore[index]

        with tempfile.TemporaryDirectory() as directory:
            report = build_wi_report(
                AppServerUsageLedger(Path(directory) / "usage.jsonl"),
                "wi_20260822_730",
                measurement_snapshot=snapshot,
            )
        observed = report["cost"]["human_attention"]
        self.assertEqual(observed["status"], "observed")
        self.assertEqual(observed["raw_turn_count"], 3)
        self.assertEqual(observed["human_attention_units"], "3.25")
        self.assertEqual(observed["focused_seconds"], "unavailable")
        self.assertEqual(observed["cycle_elapsed_seconds"], "unavailable")
        self.assertEqual(
            report["coverage"]["human_attention"], "observed"
        )

        tampered = deepcopy(snapshot)
        tampered["cost"]["human_attention"]["human_attention_units"] = "99"  # type: ignore[index]
        tampered["evidence"]["readouts"]["cost.human_attention"]["value"] = "99"  # type: ignore[index]
        with self.assertRaisesRegex(HarnessMeasurementError, "units do not conserve"):
            validate_harness_measurement_snapshot(
                tampered, expected_work_item_id="wi_20260822_730"
            )

    def test_work_item_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(HarnessMeasurementError, "another work item"):
            validate_harness_measurement_snapshot(
                measurement_snapshot(work_item="wi_20260820_272"),
                expected_work_item_id="wi_20260822_730",
            )

    def test_quality_tamper_that_disagrees_with_readout_fails_closed(self) -> None:
        value = measurement_snapshot()
        value["quality"]["delivery_status"] = "rejected"  # type: ignore[index]
        with self.assertRaisesRegex(HarnessMeasurementError, "does not match"):
            validate_harness_measurement_snapshot(
                value, expected_work_item_id="wi_20260822_730"
            )

    def test_extra_raw_identity_field_is_rejected_by_closed_schema(self) -> None:
        value = measurement_snapshot()
        value["thread_id"] = "provider-private-thread"
        with self.assertRaisesRegex(HarnessMeasurementError, "fields"):
            validate_harness_measurement_snapshot(
                value, expected_work_item_id="wi_20260822_730"
            )

    def test_absolute_authority_ref_is_rejected(self) -> None:
        value = deepcopy(measurement_snapshot())
        value["cost"]["token_hard_cost"]["source_ref"] = "/private/raw.json"  # type: ignore[index]
        with self.assertRaisesRegex(HarnessMeasurementError, "repository-relative"):
            validate_harness_measurement_snapshot(
                value, expected_work_item_id="wi_20260822_730"
            )


if __name__ == "__main__":
    unittest.main()
