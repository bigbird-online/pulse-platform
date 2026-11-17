from __future__ import annotations

import unittest

from pulse_compute_cost.events import Lineage, TokenUsage, UsageEvent, UsageEventError
from pulse_compute_cost.receipt_sources import normalize_usage
from pulse_compute_cost.receipts import ReceiptError


def event(**changes: object) -> UsageEvent:
    values: dict[str, object] = {
        "event_id": "uev1_fixture_123",
        "occurred_at": "2026-07-31T16:01:00Z",
        "observed_at": "2026-08-01T00:00:00Z",
        "machine": "vultr",
        "agent": "unknown",
        "consumer_path": "direct/codex",
        "source": "chatgpt_pro",
        "runtime": "codex",
        "provider": "openai-chatgpt",
        "model": "gpt-fixture",
        "effort": "high",
        "tokens": TokenUsage(input=100, output=10, cache_read=50, reasoning=3),
    }
    values.update(changes)
    return UsageEvent(**values)  # type: ignore[arg-type]


class UsageEventContractTests(unittest.TestCase):
    def test_cache_classes_cannot_exceed_inclusive_input_on_either_active_path(self) -> None:
        with self.assertRaisesRegex(UsageEventError, "cache classes exceed inclusive input"):
            TokenUsage(input=10, output=0, cache_read=9, cache_write=9)
        with self.assertRaisesRegex(ReceiptError, "cache classes exceed inclusive input"):
            normalize_usage("codex", {
                "input_tokens": 10, "cached_input_tokens": 9,
                "cache_write_input_tokens": 9, "output_tokens": 0,
            })
        with self.assertRaisesRegex(ReceiptError, "cache classes exceed inclusive input"):
            normalize_usage("codex", {
                "input_tokens": 10, "cached_input_tokens": 11,
                "output_tokens": 0,
            })

    def test_beijing_month_and_late_event_follow_event_time(self) -> None:
        usage = event()
        self.assertEqual(usage.product_month, "2026-08")
        self.assertEqual(usage.observed_month, "2026-08")
        self.assertFalse(usage.late_event)

        late = event(
            occurred_at="2026-07-31T15:59:59Z",
            observed_at="2026-08-01T00:00:00Z",
        )
        self.assertEqual(late.product_month, "2026-07")
        self.assertEqual(late.observed_month, "2026-08")
        self.assertTrue(late.late_event)

    def test_missing_attribution_is_explicit_unknown(self) -> None:
        usage = event()
        rendered = usage.to_dict()
        self.assertEqual(rendered["agent"], "unknown")
        self.assertIn("agent", rendered["quality"]["missing_fields"])
        self.assertEqual(rendered["feedback"]["outcome_status"], "unknown")
        self.assertEqual(usage.has_missing_fields, bool(usage.missing_fields))

    def test_nullable_or_padded_identity_fields_are_canonicalized(self) -> None:
        usage = event(agent=None, consumer_path="  direct/codex  ")
        self.assertEqual(usage.agent, "unknown")
        self.assertEqual(usage.consumer_path, "direct/codex")
        self.assertIn("agent", usage.missing_fields)

    def test_observation_cannot_precede_usage(self) -> None:
        with self.assertRaisesRegex(UsageEventError, "cannot precede"):
            event(
                occurred_at="2026-08-01T00:00:01Z",
                observed_at="2026-08-01T00:00:00Z",
            )

    def test_round_trip_preserves_semantic_event(self) -> None:
        usage = event(
            lineage=Lineage(
                root_agent="pulse",
                execution_mode="automatic",
                session_role="sub_agent",
                root_run_ref="lineage_v1_" + "a" * 64,
                parent_run_ref="lineage_v1_" + "b" * 64,
                cron_job_ref="cron_v1_" + "c" * 64,
                scan_ref="scan_v1_" + "d" * 64,
            ),
            quality_flags=("fixture",),
        )
        restored = UsageEvent.from_dict(usage.to_dict())
        self.assertEqual(restored.to_dict(), usage.to_dict())

    def test_new_event_always_serializes_complete_lineage_shape(self) -> None:
        rendered = event().to_dict()
        self.assertEqual(
            rendered["lineage"],
            {
                "root_agent": "unknown",
                "execution_mode": "unknown",
                "session_role": "unknown",
                "root_run_ref": "unknown",
                "parent_run_ref": "unknown",
                "cron_job_ref": "unknown",
                "scan_ref": "unknown",
            },
        )
        self.assertIn("lineage.root_run_ref", rendered["quality"]["missing_fields"])
        self.assertTrue(event().has_missing_fields)

    def test_legacy_v1_without_lineage_reads_as_unknown_and_partial(self) -> None:
        current = event().to_dict()
        current.pop("lineage")
        current.pop("task_family")
        current.pop("risk_class")
        current["quality"]["missing_fields"] = [
            name for name in current["quality"]["missing_fields"]
            if not name.startswith("lineage.")
            and name not in {"task_family", "risk_class"}
        ]
        restored = UsageEvent.from_dict(current)
        self.assertEqual(restored.lineage, Lineage())
        self.assertEqual(restored.task_family, "unknown")
        self.assertEqual(restored.risk_class, "unknown")
        rendered = restored.to_dict()
        self.assertEqual(rendered["schema_version"], 1)
        self.assertEqual(rendered["task_family"], "unknown")
        self.assertEqual(rendered["risk_class"], "unknown")
        self.assertEqual(rendered["quality"]["status"], "partial")

    def test_present_lineage_requires_exactly_seven_known_fields(self) -> None:
        rendered = event().to_dict()
        rendered["lineage"].pop("scan_ref")
        with self.assertRaisesRegex(UsageEventError, "exactly the seven"):
            UsageEvent.from_dict(rendered)

        rendered = event().to_dict()
        rendered["lineage"]["raw_session_id"] = "private"
        with self.assertRaisesRegex(UsageEventError, "exactly the seven"):
            UsageEvent.from_dict(rendered)

        rendered = event().to_dict()
        rendered["lineage"] = None
        with self.assertRaisesRegex(UsageEventError, "lineage must be an object"):
            UsageEvent.from_dict(rendered)

    def test_lineage_rejects_raw_or_malformed_references(self) -> None:
        for raw_ref in (
            "raw-session-id",
            "lineage_v1_not-a-digest",
            "lineage_v1_" + "A" * 64,
            "lineage_v1_" + "a" * 63,
        ):
            with self.subTest(raw_ref=raw_ref):
                with self.assertRaisesRegex(UsageEventError, "lineage.root_run_ref"):
                    event(lineage=Lineage(root_run_ref=raw_ref))
        with self.assertRaisesRegex(UsageEventError, "lineage.cron_job_ref"):
            Lineage(cron_job_ref="raw-cron-id")
        with self.assertRaisesRegex(UsageEventError, "lineage.scan_ref"):
            Lineage(scan_ref="lineage_v1_" + "e" * 64)

    def test_lineage_enums_fail_closed(self) -> None:
        with self.assertRaisesRegex(UsageEventError, "execution_mode"):
            Lineage(execution_mode="cron")
        with self.assertRaisesRegex(UsageEventError, "session_role"):
            Lineage(session_role="child")

    def test_semantic_dict_matches_full_event_without_observation_placement(self) -> None:
        usage = event(quality_flags=("fixture",))
        expected = usage.to_dict()
        for field in ("observed_at", "observed_month", "late_event", "provenance"):
            expected.pop(field)
        self.assertEqual(usage.semantic_dict(), expected)


if __name__ == "__main__":
    unittest.main()
