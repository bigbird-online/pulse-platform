"""Contract tests for the production Harness source consumed by Stats."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


EXPORTER_ROOT = Path(__file__).resolve().parents[1]
PULSE_REPO = Path(__file__).resolve().parents[3]

sys.path.insert(0, str(EXPORTER_ROOT))

from pulse_stats import live_contract, usage_sources  # noqa: E402


class HarnessAuthoritySourceContractTests(unittest.TestCase):
    def test_terminal_authority_cannot_be_shadowed_by_retired_projection(self) -> None:
        """The production path must choose the same authority as deployment.

        The fixture is intentionally generic: a terminal canonical record and a
        stale non-terminal projection of the same item show why selecting the
        retired checkout creates a false in-flight row.
        """
        # The default equals the ownermac authority repository registered in
        # config/scl-surfaces.yaml; production resolves that registration.
        registry = json.loads((PULSE_REPO / "config/scl-surfaces.yaml").read_text())
        expected_repository = Path(next(
            surface["deployment"]["authority_repository"]
            for surface in registry["surfaces"]
            if surface.get("deployment", {}).get("site") == "ownermac"
        ))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "service-repositories/bigbird-online/PULSE/ownermac"
            retired = root / "service-repositories/collaboratewithai/harness/ownermac"
            for repository in (canonical, retired):
                (repository / ".harness/work-items").mkdir(parents=True)
                (repository / ".harness/handoffs").mkdir(parents=True)

            work_item_id = "wi_20260101_001"
            terminal_record = {
                "work_item_id": work_item_id,
                "title": "generic terminal item",
                "owner": "agent",
                "status": "completed",
                "active_executor": None,
                "updated_at": "2026-01-02T00:00:00Z",
            }
            stale_record = {
                **terminal_record,
                "status": "in_progress",
                "active_executor": "agent",
                "updated_at": "2026-01-01T00:00:00Z",
            }
            (canonical / f".harness/work-items/{work_item_id}.yaml").write_text(
                json.dumps(terminal_record), encoding="utf-8"
            )
            (retired / f".harness/work-items/{work_item_id}.yaml").write_text(
                json.dumps(stale_record), encoding="utf-8"
            )

            now = datetime(2026, 1, 3, tzinfo=timezone.utc)
            canonical_rows = usage_sources.collect_in_flight(
                canonical / ".harness/work-items",
                canonical / ".harness/handoffs",
                now,
            )
            retired_rows = usage_sources.collect_in_flight(
                retired / ".harness/work-items",
                retired / ".harness/handoffs",
                now,
            )

        self.assertEqual(canonical_rows, [])
        self.assertEqual(
            [row["work_item_id"] for row in retired_rows or []], [work_item_id]
        )
        self.assertEqual(live_contract.HARNESS_SERVICE_REPO, expected_repository)
        self.assertEqual(live_contract.ALERTS_DIR, expected_repository / ".harness/events")
        self.assertEqual(live_contract.HARNESS_AUTHORITY_REPOSITORY, "bigbird-online/PULSE")
        self.assertEqual(
            live_contract.LivePaths().work_items,
            expected_repository / ".harness/work-items",
        )


if __name__ == "__main__":
    unittest.main()
