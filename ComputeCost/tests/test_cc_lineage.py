from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.adapters import collect_usage_events
from pulse_compute_cost.cc_lineage import cc_transcript_lineage


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def usage_row(
    *,
    message_id: str,
    session_id: str,
    timestamp: str,
    is_sidechain: bool | None,
    agent_id: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "type": "assistant",
        "timestamp": timestamp,
        "sessionId": session_id,
        "uuid": f"row-{message_id}",
        "cwd": "/private/duke-space-must-not-drive-lineage",
        "message": {
            "id": message_id,
            "model": "claude-fixture",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 1,
            },
        },
    }
    if is_sidechain is not None:
        row["isSidechain"] = is_sidechain
    if agent_id is not None:
        row["agentId"] = agent_id
    return row


class CcLineageContractTests(unittest.TestCase):
    def test_main_and_sidechain_share_only_a_private_verified_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects" / "fixture-project"
            root_session_id = "root-session-private"
            child_session_id = "child-session-private"
            agent_id = "child-agent-private"

            write_jsonl(
                projects / f"{root_session_id}.jsonl",
                [usage_row(
                    message_id="main-message-private",
                    session_id=root_session_id,
                    timestamp="2026-08-18T01:00:00Z",
                    is_sidechain=False,
                )],
            )
            write_jsonl(
                projects / root_session_id / "subagents" / f"agent-{agent_id}.jsonl",
                [usage_row(
                    message_id="child-message-private",
                    session_id=child_session_id,
                    timestamp="2026-08-18T02:00:00Z",
                    is_sidechain=True,
                    agent_id=agent_id,
                )],
            )

            events = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-18T03:00:00Z",
                cc_projects_dir=root / "projects",
            ).events
            self.assertEqual(len(events), 2)
            by_role = {event.lineage.session_role: event for event in events}
            main = by_role["main"]
            child = by_role["sub_agent"]

            self.assertRegex(main.lineage.root_run_ref, r"^lineage_v1_[0-9a-f]{64}$")
            self.assertEqual(child.lineage.root_run_ref, main.lineage.root_run_ref)
            self.assertEqual(child.lineage.parent_run_ref, "unknown")
            self.assertEqual(child.lineage.root_agent, "unknown")
            self.assertEqual(child.lineage.execution_mode, "unknown")
            # Existing consumer attribution may use cwd as a fallback, but that
            # must never be promoted into execution lineage.
            self.assertEqual(child.agent, "duke")

            rendered = json.dumps([event.to_dict() for event in events])
            for private in (
                root_session_id,
                child_session_id,
                agent_id,
                "main-message-private",
                "child-message-private",
                str(root),
                "/private/duke-space",
            ):
                self.assertNotIn(private, rendered)

    def test_unverified_or_inconsistent_producer_evidence_stays_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root_id = "root-private"
            main_path = root / f"{root_id}.jsonl"
            main_path.touch()

            missing_role = usage_row(
                message_id="missing-role",
                session_id=root_id,
                timestamp="2026-08-18T01:00:00Z",
                is_sidechain=None,
            )
            mismatched_main = usage_row(
                message_id="mismatched-main",
                session_id="different-private",
                timestamp="2026-08-18T01:00:00Z",
                is_sidechain=False,
            )
            child = usage_row(
                message_id="child",
                session_id="child-private",
                timestamp="2026-08-18T01:00:00Z",
                is_sidechain=True,
                agent_id="agent-private",
            )
            mismatched_child_path = root / root_id / "subagents" / "agent-other.jsonl"
            orphan_child_path = root / "orphan-private" / "subagents" / "agent-agent-private.jsonl"

            for path, row in (
                (main_path, missing_role),
                (main_path, mismatched_main),
                (mismatched_child_path, child),
                (orphan_child_path, child),
            ):
                self.assertEqual(cc_transcript_lineage(path, row).session_role, "unknown")
                self.assertEqual(cc_transcript_lineage(path, row).root_run_ref, "unknown")
                self.assertEqual(cc_transcript_lineage(path, row).parent_run_ref, "unknown")

    def test_nested_main_jsonl_layout_is_supported_without_exposing_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root_id = "nested-root-private"
            path = root / root_id / "main.jsonl"
            path.parent.mkdir(parents=True)
            path.touch()
            row = usage_row(
                message_id="main",
                session_id=root_id,
                timestamp="2026-08-18T01:00:00Z",
                is_sidechain=False,
            )

            lineage = cc_transcript_lineage(path, row)

            self.assertEqual(lineage.session_role, "main")
            self.assertRegex(lineage.root_run_ref, r"^lineage_v1_[0-9a-f]{64}$")
            self.assertNotIn(root_id, json.dumps(lineage.to_dict()))
            self.assertEqual(lineage.parent_run_ref, "unknown")


if __name__ == "__main__":
    unittest.main()
