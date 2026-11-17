from __future__ import annotations

import unittest

from pulse_compute_cost.attention import build_attention_vector


def retrospective(
    elapsed: int,
    *,
    approvals: int = 1,
    relay_binding: str = "unavailable_no_exact_wi_bound_relay_turn_schema",
) -> dict[str, object]:
    return {
        "status": "partial",
        "cycle_elapsed_seconds": elapsed,
        "category_counts": {"review_approval": approvals},
        "relay_turn_binding": relay_binding,
    }


def prospective(turns: int) -> dict[str, object]:
    return {
        "raw_turn_count": turns,
        "human_attention_units": str(turns),
        "effort_band_counts": {
            "micro": 0,
            "bounded": turns,
            "judgment": 0,
            "framing": 0,
        },
        "category_counts": {
            "human_touch": turns - 1,
            "review_approval": 1,
            "correction": 0,
            "re_explanation": 0,
            "manual_retry": 0,
            "rework": 0,
        },
        "focused_seconds": "unavailable",
        "focused_seconds_coverage": "unavailable",
        "focused_seconds_unknown_event_count": turns,
    }


class AttentionVectorTests(unittest.TestCase):
    def test_elapsed_exposure_scales_for_one_ten_and_one_hundred_hours(self) -> None:
        vectors = [
            build_attention_vector(retrospective=retrospective(hours * 3600))
            for hours in (1, 10, 100)
        ]
        self.assertEqual(
            [item["cycle_elapsed_seconds"] for item in vectors],
            [3600, 36000, 360000],
        )
        self.assertTrue(all(item["focused_seconds"] == "unavailable" for item in vectors))
        self.assertTrue(
            all(
                item["cycle_elapsed_classification"]
                == "attention_exposure_not_focused_time"
                for item in vectors
            )
        )

    def test_more_exact_feedback_reports_more_burden_at_equal_elapsed_time(self) -> None:
        ten = build_attention_vector(
            prospective_scl=prospective(10), retrospective=retrospective(86400)
        )
        hundred = build_attention_vector(
            prospective_scl=prospective(100), retrospective=retrospective(86400)
        )
        self.assertEqual(ten["cycle_elapsed_seconds"], hundred["cycle_elapsed_seconds"])
        self.assertEqual(ten["exact_human_feedback_count"], 10)
        self.assertEqual(hundred["exact_human_feedback_count"], 100)
        self.assertEqual(ten["density_per_elapsed_day"]["feedback_count"], "10.000000")
        self.assertEqual(hundred["density_per_elapsed_day"]["feedback_count"], "100.000000")
        self.assertGreater(
            float(hundred["density_per_elapsed_day"]["human_attention_units"]),
            float(ten["density_per_elapsed_day"]["human_attention_units"]),
        )

    def test_prospective_precedence_deduplicates_retrospective_review_and_projection(self) -> None:
        current = prospective(10)
        vector = build_attention_vector(
            prospective_scl=current,
            prospective_harness=current,
            retrospective=retrospective(86400, approvals=1),
        )
        self.assertEqual(vector["category_counts"]["review_approval"], 1)
        self.assertEqual(vector["exact_human_feedback_count"], 10)
        self.assertEqual(vector["human_attention_units"], "10")
        self.assertEqual(
            vector["suppressed_overlapping_sources"],
            ["prospective_harness_projection"],
        )

    def test_ambiguous_shared_session_retains_only_safe_review_and_elapsed_subset(self) -> None:
        vector = build_attention_vector(
            retrospective=retrospective(86400, relay_binding="ambiguous")
        )
        self.assertEqual(vector["status"], "partial")
        self.assertEqual(vector["cycle_elapsed_seconds"], 86400)
        self.assertEqual(vector["category_counts"]["review_approval"], 1)
        self.assertEqual(vector["exact_human_feedback_count"], "unavailable")
        self.assertEqual(vector["density_per_elapsed_day"]["feedback_count"], "unavailable")
        self.assertEqual(vector["evidence"]["relay_turn_binding"], "ambiguous")

    def test_absence_is_unknown_not_zero(self) -> None:
        vector = build_attention_vector()
        self.assertEqual(vector["status"], "unavailable")
        self.assertEqual(vector["cycle_elapsed_seconds"], "unavailable")
        self.assertEqual(vector["exact_human_feedback_count"], "unavailable")
        self.assertEqual(vector["category_counts"]["review_approval"], "unavailable")
        self.assertEqual(vector["human_attention_units"], "unavailable")


if __name__ == "__main__":
    unittest.main()
