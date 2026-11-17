from __future__ import annotations

import json
import pathlib
import unittest


FIXTURES = pathlib.Path(__file__).parents[2] / "PulseStats" / "Resources" / "Fixtures"
PROHIBITED_KEYS = {"message", "prompt", "token", "user_id", "session_id", "command"}
VALID_METRIC_STATUSES = {"available", "warming_up", "unknown", "not_applicable"}
VALID_METRIC_QUALITIES = {"validated", "descriptive", "insufficient", "unavailable"}


def all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_keys(child)


class FixtureContractTests(unittest.TestCase):
    def test_all_v2_fixtures_share_required_shape_and_no_sensitive_fields(self) -> None:
        paths = sorted(FIXTURES.glob("*.json"))
        self.assertGreaterEqual(len(paths), 8)
        for path in paths:
            if path.name == "invalid.json":
                continue
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            if path.name == "unknown-major.json":
                self.assertEqual(snapshot["schema_version"], "pulse.monitoring.snapshot.v3")
                continue
            self.assertEqual(snapshot["schema_version"], "pulse.monitoring.snapshot.v2", path.name)
            # Hand-written fixtures are pinned to the baseline they were authored
            # against. The `live-*.json` files are captured production payloads
            # instead, so their baseline moves with production (0.2.2 at capture
            # time) and pinning them would turn every baseline bump into a red
            # test. Every other clause below — shape, enums, and above all the
            # prohibited-key scan — still applies to them, and matters more for
            # real data.
            if not path.name.startswith("live-"):
                self.assertEqual(snapshot["baseline_version"], "0.2.1", path.name)
            # `alert_tiers` is the v3 triage addition (producer.py): every
            # snapshot the producer emits now carries it, but the fixtures
            # captured before it exists must not be retro-fitted, so it is
            # optional here while every other key stays exact.
            self.assertEqual(
                set(snapshot) - {"alert_tiers", "global_authority", "cost_dashboard"},
                {"schema_version", "generated_at", "expires_at", "baseline_version", "overall", "capabilities", "incidents", "metrics", "alerts", "provenance"},
                path.name,
            )
            self.assertIn(snapshot["overall"]["runtime_health"], {"ok", "warning", "critical", "stale", "unknown"})
            self.assertIn(snapshot["overall"]["coverage"], {"full", "partial", "unknown"})
            self.assertEqual(
                snapshot["overall"]["open_incident_count"],
                sum(item["state"] == "open" for item in snapshot["incidents"]),
            )
            for metric in snapshot["metrics"]:
                self.assertIn(metric["status"], VALID_METRIC_STATUSES, (path.name, metric["id"]))
                self.assertIn(metric["quality"], VALID_METRIC_QUALITIES, (path.name, metric["id"]))
            if "cost_dashboard" in snapshot:
                self.assertEqual(snapshot["cost_dashboard"]["schema_version"], "pulse_stats_cost_dashboard/v1")
                self.assertEqual(len(snapshot["cost_dashboard"]["views"]), 9)
            self.assertTrue(PROHIBITED_KEYS.isdisjoint(set(all_keys(snapshot))), path.name)


if __name__ == "__main__":
    unittest.main()
