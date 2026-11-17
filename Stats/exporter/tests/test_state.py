from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

from pulse_stats.reducer import IncidentEvent, reduce_incident_events
from pulse_stats.state import IncidentStateStore, StateLockBusy


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)


def projections(count: int = 1):
    events = [
        IncidentEvent(
            event_id=f"event-{index}", kind="breach", capability_id="s2.ack_overdue",
            condition_key="handoff_overdue", scope="fixture", normalized_scope_key="fixture",
            severity="warning", title="Fixture breach", occurred_at=NOW + timedelta(seconds=index),
            reason="fixture",
        )
        for index in range(count)
    ]
    return reduce_incident_events(events)[0]


class IncidentStateStoreTests(unittest.TestCase):
    def test_atomic_0600_round_trip_with_bounded_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "incident-state-v1.json"
            store = IncidentStateStore(path)
            store.save(
                projections(200),
                source_watermarks={f"source-{index}": str(index) for index in range(70)},
                source_cursors={"alerts": "file:12"},
            )
            loaded = store.load()
            self.assertEqual(loaded.status, "ok")
            self.assertEqual(len(loaded.incidents[0].evidence), 32)
            self.assertEqual(len(loaded.incidents[0].seen_event_ids), 128)
            self.assertEqual(len(loaded.source_watermarks), 64)
            self.assertEqual(loaded.source_cursors, {"alerts": "file:12"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_single_writer_lock_rejects_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "incident-state-v1.json"
            first = IncidentStateStore(path)
            second = IncidentStateStore(path)
            with first.single_writer():
                with self.assertRaises(StateLockBusy):
                    with second.single_writer():
                        pass

    def test_invalid_enum_or_naive_datetime_makes_whole_state_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "incident-state-v1.json"
            store = IncidentStateStore(path)
            store.save(projections())
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["incidents"][0]["state"] = "healed"
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(store.load().status, "corrupt")
            raw["incidents"][0]["state"] = "open"
            raw["incidents"][0]["severity"] = "urgent"
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(store.load().status, "corrupt")
            raw["incidents"][0]["severity"] = "warning"
            raw["incidents"][0]["opened_at"] = "2026-07-22T08:00:00"
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(store.load().status, "corrupt")


if __name__ == "__main__":
    unittest.main()
