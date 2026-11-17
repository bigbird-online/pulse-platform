from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

from pulse_stats.reducer import IncidentEvent, reduce_incident_events


BASE = datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc)


def event(
    event_id: str,
    kind: str,
    minute: int,
    *,
    severity: str = "warning",
    title: str = "Handoff overdue",
) -> IncidentEvent:
    return IncidentEvent(
        event_id=event_id,
        kind=kind,
        capability_id="s2.ack_overdue",
        condition_key="handoff_overdue",
        scope="HO Fixture 01",
        normalized_scope_key="ho-fixture-01",
        severity=severity,
        title=title,
        occurred_at=BASE + timedelta(minutes=minute),
        reason=f"fixture reason {event_id}",
        impact="fixture work waits",
        suggested_action="inspect fixture owner",
    )


class IncidentReducerTests(unittest.TestCase):
    def test_breach_opens_incident_with_stable_key(self) -> None:
        incidents, alerts = reduce_incident_events([event("breach-1", "breach", 1)])
        self.assertEqual(alerts, [])
        self.assertEqual(incidents[0].incident_key, "s2.ack_overdue:handoff_overdue:ho-fixture-01")
        self.assertEqual(incidents[0].state, "open")
        self.assertEqual(incidents[0].occurrence_count, 1)

    def test_repeated_breach_updates_one_incident(self) -> None:
        incidents, _ = reduce_incident_events(
            [event("breach-1", "breach", 1), event("breach-2", "breach", 2, severity="critical", title="Escalated")]
        )
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].occurrence_count, 2)
        self.assertEqual(incidents[0].severity, "critical")
        self.assertEqual(incidents[0].title, "Escalated")

    def test_same_event_is_idempotent(self) -> None:
        duplicate = event("breach-1", "breach", 1)
        incidents, _ = reduce_incident_events([duplicate, duplicate])
        self.assertEqual(incidents[0].occurrence_count, 1)
        self.assertEqual(incidents[0].evidence, ("event:breach-1",))

    def test_positive_recovery_resolves_only_when_later_than_breach(self) -> None:
        incidents, _ = reduce_incident_events(
            [event("breach-1", "breach", 3), event("recovery-old", "recovery", 2)]
        )
        self.assertEqual(incidents[0].state, "open")

        incidents, _ = reduce_incident_events(
            [event("recovery-new", "recovery", 4)], existing=incidents
        )
        self.assertEqual(incidents[0].state, "resolved")
        self.assertEqual(incidents[0].resolved_at, BASE + timedelta(minutes=4))

    def test_breach_after_resolution_reopens_new_episode(self) -> None:
        incidents, _ = reduce_incident_events(
            [
                event("breach-1", "breach", 1),
                event("recovery-1", "recovery", 2),
                event("breach-2", "breach", 3),
            ]
        )
        self.assertEqual(incidents[0].state, "open")
        self.assertEqual(incidents[0].reopen_count, 1)
        self.assertTrue(incidents[0].episode_id.endswith("#episode-2"))
        self.assertEqual(incidents[0].occurrence_count, 1)

    def test_late_old_breach_does_not_reopen_resolved_incident(self) -> None:
        incidents, _ = reduce_incident_events(
            [event("breach-new", "breach", 3), event("recovery-new", "recovery", 4)]
        )
        incidents, _ = reduce_incident_events(
            [event("breach-late-old", "breach", 2)], existing=incidents
        )
        self.assertEqual(incidents[0].state, "resolved")
        self.assertEqual(incidents[0].reopen_count, 0)

    def test_batch_order_does_not_change_projection(self) -> None:
        chronological, _ = reduce_incident_events(
            [event("breach-1", "breach", 1), event("recovery-1", "recovery", 2)]
        )
        reversed_input, _ = reduce_incident_events(
            [event("recovery-1", "recovery", 2), event("breach-1", "breach", 1)]
        )
        self.assertEqual(chronological, reversed_input)

    def test_event_only_is_alert_and_never_open_incident(self) -> None:
        only = event("flush-1", "event_only", 1, severity="info", title="Daily flush completed")
        incidents, alerts = reduce_incident_events(
            [only, only]
        )
        self.assertEqual(incidents, [])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].projection_status, "event_only")

    def test_scope_key_must_be_explicitly_safe(self) -> None:
        unsafe = event("breach-unsafe", "breach", 1)
        unsafe = IncidentEvent(
            event_id=unsafe.event_id,
            kind=unsafe.kind,
            capability_id=unsafe.capability_id,
            condition_key=unsafe.condition_key,
            scope="display-safe",
            normalized_scope_key="User Raw Value",
            severity=unsafe.severity,
            title=unsafe.title,
            occurred_at=unsafe.occurred_at,
            reason=unsafe.reason,
        )
        with self.assertRaises(ValueError):
            reduce_incident_events([unsafe])


if __name__ == "__main__":
    unittest.main()
