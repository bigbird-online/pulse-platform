from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

from pulse_stats.producer import build_snapshot, reduce_coverage, reduce_runtime_health
from pulse_stats.reducer import IncidentEvent, reduce_incident_events


NOW = datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc)


def capability(
    capability_id: str,
    *,
    status: str = "active_validated",
    freshness: str = "fresh",
    role: str = "runtime",
) -> dict[str, object]:
    return {
        "id": capability_id,
        "implementation_status": status,
        "health": "ok" if freshness == "fresh" else freshness,
        "aggregation_role": role,
        "scope": "fixture",
        "reason": "fixture capability",
        "freshness": {
            "state": freshness,
            "observed_at": NOW.isoformat() if freshness == "fresh" else None,
            "max_age_seconds": 600,
        },
        "evidence": [f"fixture:{capability_id}"],
    }


class ProducerTests(unittest.TestCase):
    def test_runtime_uses_open_incident_severity_before_source_freshness(self) -> None:
        breach = IncidentEvent(
            event_id="critical-1",
            kind="breach",
            capability_id="s2.ledger_invariants",
            condition_key="inv1",
            scope="fixture",
            normalized_scope_key="fixture",
            severity="critical",
            title="Invariant breach",
            occurred_at=NOW,
            reason="fixture breach",
        )
        incidents, _ = reduce_incident_events([breach])
        self.assertEqual(
            reduce_runtime_health([capability("s2.ledger_invariants", freshness="stale")], incidents),
            "critical",
        )

    def test_runtime_is_stale_or_unknown_instead_of_false_ok(self) -> None:
        self.assertEqual(reduce_runtime_health([capability("a", freshness="stale")], []), "stale")
        self.assertEqual(reduce_runtime_health([capability("a", freshness="error")], []), "unknown")
        self.assertEqual(reduce_runtime_health([], []), "unknown")

    def test_coverage_requires_every_required_capability_validated_and_fresh(self) -> None:
        self.assertEqual(reduce_coverage([capability("a"), capability("b")], ["a", "b"]), "full")
        self.assertEqual(reduce_coverage([capability("a")], ["a", "b"]), "partial")
        self.assertEqual(
            reduce_coverage([capability("a"), capability("b", status="active_partial")], ["a", "b"]),
            "partial",
        )
        self.assertEqual(reduce_coverage([], ["a"]), "unknown")
        self.assertEqual(reduce_coverage([capability("a")], []), "unknown")
        self.assertEqual(reduce_coverage([capability("a", freshness="unknown")], ["a"]), "unknown")
        self.assertEqual(reduce_coverage([capability("a", freshness="error")], ["a"]), "unknown")
        self.assertEqual(reduce_coverage([capability("a", freshness="stale")], ["a"]), "partial")

    def test_one_shot_snapshot_is_v2_json_and_event_only_does_not_increment_open_count(self) -> None:
        event_only = IncidentEvent(
            event_id="flush-1",
            kind="event_only",
            capability_id="notify.daily_flush",
            condition_key="daily_flush",
            scope="fixture",
            normalized_scope_key="fixture",
            severity="info",
            title="Daily flush completed",
            occurred_at=NOW,
            reason="fixture event",
        )
        incidents, alerts = reduce_incident_events([event_only])
        snapshot = build_snapshot(
            generated_at=NOW,
            expires_at=NOW + timedelta(minutes=3),
            capabilities=[capability("notify.daily_flush")],
            required_capability_ids=["notify.daily_flush"],
            incidents=incidents,
            alerts=alerts,
            provenance={
                "producer": {"name": "pulse-stats-exporter", "version": "0.1.0"},
                "source_revisions": {"fixture": "producer-test"},
                "sources": [{"id": "fixture", "freshness": "fresh", "observed_at": NOW.isoformat(), "error_code": None}],
            },
            headline="Fixture snapshot",
        )
        self.assertEqual(snapshot["schema_version"], "pulse.monitoring.snapshot.v2")
        self.assertEqual(snapshot["overall"]["open_incident_count"], 0)
        self.assertEqual(snapshot["alerts"][0]["projection_status"], "event_only")
        json.dumps(snapshot)


if __name__ == "__main__":
    unittest.main()
