from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.adapters import collect_usage_events


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def root_ref(domain: str, session_id: str) -> str:
    material = f"pulse.compute_cost.lineage.v1\0{domain}\0{session_id}".encode()
    return f"lineage_v1_{sha256(material).hexdigest()}"


def interactive_lineage(agent: str, session_id: str) -> dict[str, str]:
    return {
        "root_agent": agent,
        "execution_mode": "human",
        "session_role": "main",
        "root_run_ref": root_ref("cc_root_session", session_id),
        "parent_run_ref": "unknown",
        "cron_job_ref": "unknown",
        "scan_ref": "unknown",
    }


class RelayInteractiveLineageTests(unittest.TestCase):
    def test_reconciled_cc_event_inherits_explicit_interactive_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_id = "interactive-session-private"
            tenant = "human-private:pulse:feishu"
            write_jsonl(root / "relay" / "2026-08-18.jsonl", [{
                "ts": "2026-08-18T01:00:01Z",
                "tenant": tenant,
                "session_id": session_id,
                "model": "claude-fixture",
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 1,
                "lineage": interactive_lineage("pulse", session_id),
            }])
            write_jsonl(root / "cc" / "project" / "legacy-layout.jsonl", [{
                "type": "assistant",
                "timestamp": "2026-08-18T01:00:00Z",
                "sessionId": session_id,
                "uuid": "row-private",
                "cwd": "/private/path",
                "message": {
                    "id": "message-private",
                    "model": "claude-fixture",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 1,
                    },
                },
            }])
            catalog = root / "models.json"
            catalog.write_text(json.dumps({
                "providers": {"openrouter": {"protocol": "anthropic"}},
                "models": [{"id": "claude-fixture", "provider": "openrouter"}],
            }), encoding="utf-8")

            event = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-18T02:00:00Z",
                relay_metering_dir=root / "relay",
                cc_projects_dir=root / "cc",
                model_catalog_path=catalog,
            ).events[0]

            self.assertEqual(event.adapter, "cc_transcript")
            self.assertEqual(event.lineage.root_agent, "pulse")
            self.assertEqual(event.lineage.execution_mode, "human")
            self.assertEqual(event.lineage.session_role, "main")
            self.assertEqual(
                event.lineage.root_run_ref,
                root_ref("cc_root_session", session_id),
            )
            rendered = json.dumps(event.to_dict())
            for private in (session_id, tenant, "message-private", "/private/path"):
                self.assertNotIn(private, rendered)

    def test_invalid_relay_lineage_preserves_usage_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_jsonl(root / "relay" / "2026-08-18.jsonl", [{
                "ts": "2026-08-18T01:00:00Z",
                "tenant": "human-private:pulse:feishu",
                "session_id": "private-session",
                "model": "unknown-model",
                "input_tokens": 4,
                "output_tokens": 1,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "lineage": {
                    "root_agent": "pulse",
                    "execution_mode": "human",
                    "session_role": "main",
                    "root_run_ref": "raw-private-session",
                    "parent_run_ref": "unknown",
                    "cron_job_ref": "unknown",
                    "scan_ref": "unknown",
                },
            }])

            collection = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-18T02:00:00Z",
                relay_metering_dir=root / "relay",
            )
            event = collection.events[0]

            self.assertEqual(event.tokens.input, 4)
            self.assertEqual(event.lineage.session_role, "unknown")
            self.assertEqual(event.lineage.root_run_ref, "unknown")
            self.assertIn("relay_lineage_invalid", event.quality_flags)
            self.assertEqual(collection.diagnostics.malformed_rows["relay_lineage"], 1)
            self.assertNotIn("raw-private-session", json.dumps(event.to_dict()))


if __name__ == "__main__":
    unittest.main()
