from __future__ import annotations

from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.cli import main as cli_main


WI = "wi_20260906_342"
ASSIGNMENTS = ("ho_20260907_101", "ho_20260907_102")
RAW_SESSIONS = ("private-verifier-session-a", "private-verifier-session-b")
RAW_PROMPT = "Execute the assigned verifier handoff; the Work Item text is intentionally absent."


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


class Wi342ShapedEndToEndUat(unittest.TestCase):
    def test_structured_children_attention_and_flat_price_are_visible_without_privacy_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = root / ".harness"
            (harness / "work-items").mkdir(parents=True)
            (harness / "events").mkdir()
            (harness / "handoffs").mkdir()
            (harness / "work-items" / f"{WI}.yaml").write_text(
                f"work_item_id: {WI}\nstatus: completed\nowner: pulse\n"
                "created_at: '2026-09-06T00:00:00Z'\n"
                "updated_at: '2026-09-07T00:00:00Z'\n",
                encoding="utf-8",
            )
            documents = [f"""---
event_id: ev_created
event_type: work_item.created
operator: pulse
payload:
  title: synthetic
timestamp: '2026-09-06T00:00:00Z'
work_item_id: {WI}
---
event_id: ev_active
event_type: work_item.in_progress
operator: pulse
payload:
  from_status: ready
  to_status: in_progress
timestamp: '2026-09-06T00:01:00Z'
work_item_id: {WI}
---
event_id: ev_verify
event_type: work_item.awaiting_verification
operator: pulse
payload:
  from_status: in_progress
  to_status: awaiting_verification
timestamp: '2026-09-06T01:00:00Z'
work_item_id: {WI}
"""]
            for index, assignment in enumerate(ASSIGNMENTS):
                documents.append(f"""---
event_id: ev_handoff_{index}
event_type: handoff.created
operator: pulse
payload:
  handoff_id: {assignment}
  purpose: outcome_verification
  to_owner: outcome-verifier
  original_responsible_owner: pulse
  responsibility_transfer: not_transferred
timestamp: '2026-09-06T0{index + 2}:00:00Z'
work_item_id: {WI}
---
event_id: ev_accept_{index}
event_type: handoff.accepted
operator: outcome-verifier
payload:
  handoff_id: {assignment}
  from_state: pending
  to_state: accepted
timestamp: '2026-09-06T0{index + 2}:01:00Z'
work_item_id: {WI}
---
event_id: ev_outcome_{index}
event_type: work_item.outcome_accepted
operator: outcome-verifier
payload:
  verification_assignment: {assignment}
  verifier: outcome-verifier
  outcome_receipt_status: accepted
  from_status: awaiting_verification
  to_status: awaiting_verification
timestamp: '2026-09-06T0{index + 2}:30:00Z'
work_item_id: {WI}
""")
                (harness / "handoffs" / f"{assignment}.yaml").write_text(
                    f"handoff_id: {assignment}\nwork_item_id: {WI}\n"
                    "from_owner: pulse\nto_owner: outcome-verifier\n"
                    "purpose: outcome_verification\nack_state: archived\n",
                    encoding="utf-8",
                )
            documents.append(f"""---
event_id: ev_review
event_type: work_item.review_approved
operator: human-reviewer
payload:
  candidate_fingerprint: sha256:{'a' * 64}
timestamp: '2026-09-06T20:00:00Z'
work_item_id: {WI}
---
event_id: ev_completed
event_type: work_item.completed
operator: pulse
payload:
  from_status: awaiting_verification
  to_status: completed
timestamp: '2026-09-07T00:00:00Z'
work_item_id: {WI}
""")
            (harness / "events" / f"{WI}.events.yaml").write_text(
                "".join(documents), encoding="utf-8"
            )

            attention_events = []
            for index in range(10):
                event = {
                    "event_ref": f"human_attention_v1_{index + 1:064x}",
                    "category": "review_approval" if index == 0 else "human_touch",
                    "effort_band": "bounded",
                    "occurred_at": f"2026-09-06T10:{index:02d}:00Z",
                    "source_ref": f"evidence/attention/event-{index}.json",
                    "source_sha256": "sha256:" + f"{index + 10:064x}",
                    "timing_basis": "unavailable",
                    "measured_seconds": None,
                }
                event["event_sha256"] = "sha256:" + sha256(canonical(event)).hexdigest()
                attention_events.append(event)
            attention_path = harness / "evidence/measurements" / WI / "human-attention.json"
            attention_path.parent.mkdir(parents=True)
            attention_path.write_text(json.dumps({
                "schema_version": "s1_human_attention_evidence/v1",
                "work_item_id": WI,
                "events": attention_events,
            }), encoding="utf-8")

            codex = root / "codex"
            write_jsonl(codex / "session_index.jsonl", [
                {"id": RAW_SESSIONS[0], "thread_name": f"assignment {ASSIGNMENTS[0]}"},
                {"id": RAW_SESSIONS[1], "thread_name": f"assignment {ASSIGNMENTS[1]}"},
            ])
            write_jsonl(codex / "archived_sessions" / f"rollout-{RAW_SESSIONS[0]}.jsonl", [
                {"type": "session_meta", "payload": {"id": RAW_SESSIONS[0]}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{
                            "type": "input_text",
                            "text": f"{RAW_PROMPT} {ASSIGNMENTS[0]}",
                        }],
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "private-turn"}},
                {"type": "turn_context", "payload": {"turn_id": "private-turn", "cwd": "/private/synthetic", "model": "gpt-fixture", "effort": "high"}},
                {"type": "event_msg", "timestamp": "2026-09-06T02:10:00Z", "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 300_000,
                    "cached_input_tokens": 280_000,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 2_000,
                    "reasoning_output_tokens": 1_000,
                }}}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "private-turn"}},
            ])
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({
                "providers": {"openai-chatgpt": {"protocol": "openai"}},
                "models": [{"id": "gpt-fixture", "provider": "openai-chatgpt"}],
            }), encoding="utf-8")
            prices = root / "openrouter-models.json"
            prices.write_text(json.dumps({"data": [{
                "id": "openai/gpt-fixture",
                "canonical_slug": "openai/gpt-fixture-standard-snapshot",
                "pricing": {
                    "prompt": "0.000002",
                    "input_cache_read": "0.0000002",
                    "input_cache_write": "0.0000025",
                    "completion": "0.00001",
                    "overrides": [{
                        "min_prompt_tokens": 272_000,
                        "prompt": "9",
                        "input_cache_read": "9",
                        "input_cache_write": "9",
                        "completion": "9",
                    }],
                },
            }]}), encoding="utf-8")

            output = io.StringIO()
            with redirect_stdout(output):
                result = cli_main([
                    "wi-report", WI,
                    "--harness-root", str(harness),
                    "--codex-home", str(codex),
                    "--model-catalog", str(catalog),
                    "--openrouter-models-snapshot", str(prices),
                    "--machine", "synthetic-macos",
                ])
            self.assertEqual(result, 0)
            report = json.loads(output.getvalue())

            assignment = report["usage"]["usage_event_cohort"]["diagnostics"][
                "canonical_verifier_assignment"
            ]
            self.assertEqual(assignment["assignment_count"], 2)
            self.assertEqual(assignment["assigned_session_count"], 2)
            self.assertEqual(assignment["token_coverage"], "partial")
            self.assertEqual(
                report["usage"]["usage_event_cohort"]["diagnostics"][
                    "missing_assigned_rollout_count"
                ],
                1,
            )
            codex_attempt = next(
                item for item in report["usage"]["usage_event_cohort"]["producer_attempts"]
                if item["producer"] == "codex_desktop"
            )
            self.assertEqual(codex_attempt["status"], "partial")
            self.assertNotEqual(codex_attempt["status"], "producer_missing")

            attention = report["cost"]["human_attention"]
            self.assertEqual(attention["cycle_elapsed_seconds"], 86_400)
            self.assertEqual(attention["exact_human_feedback_count"], 10)
            self.assertEqual(attention["category_counts"]["review_approval"], 1)
            self.assertEqual(attention["human_attention_units"], "10")
            self.assertEqual(attention["density_per_elapsed_day"]["feedback_count"], "10.000000")
            self.assertEqual(attention["focused_seconds"], "unavailable")

            valuation = report["cost"]["openrouter_cash_valuation"]
            self.assertEqual(valuation["total_usd"], "0.116")
            self.assertEqual(valuation["partitions"][0]["tier"], "standard")
            self.assertEqual(valuation["basis"], "openrouter_standard_token_cash")
            self.assertEqual(
                valuation["pricing_policy"],
                "openrouter_standard_rates_no_overrides_no_subscription_settlement",
            )
            self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
            self.assertEqual(report["cost"]["partial_machine_cash_usd"], "0.116")
            self.assertEqual(report["cost"]["cash_basis"], "openrouter_standard_token_cash")
            self.assertEqual(
                report["cost"]["cash_coverage"],
                "partial_usage",
            )
            self.assertEqual(
                report["cost"]["subscription_settlement"],
                "excluded_from_wi_report_and_roi",
            )
            self.assertEqual(report["coverage"]["cash"], "partial_usage")
            self.assertEqual(report["schema"], "computecost_wi_report/v4")

            rendered = json.dumps(report)
            for private in (*RAW_SESSIONS, RAW_PROMPT, "private-turn", str(root), "/private/synthetic"):
                self.assertNotIn(private, rendered)
            self.assertNotIn(ASSIGNMENTS[0], rendered)
            self.assertNotIn(ASSIGNMENTS[1], rendered)


if __name__ == "__main__":
    unittest.main()
