from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.pi_usage import (
    PiUsageBinding,
    PiUsageError,
    build_pi_session_snapshot,
    collect_pi_session_usage,
    validate_pi_session_snapshot,
    verify_pi_snapshot_source_prefix,
)
from pulse_compute_cost.runtime_binding import build_runtime_usage_binding


def usage(
    *,
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int = 50,
    cache_write: int = 5,
    reasoning: int | None = 4,
    cost: str = "0.0123",
) -> dict[str, object]:
    value: dict[str, object] = {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "totalTokens": input_tokens + output_tokens + cache_read + cache_write,
        "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": cost,
        },
    }
    if reasoning is not None:
        value["reasoning"] = reasoning
    return value


class PiUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private-session-id.jsonl"
        self.binding = PiUsageBinding(
            work_item_id="wi_20260822_730",
            phase="work_item",
            epoch=1,
            procurement_source="chatgpt_subscription",
        )

    def write(self, entries: list[dict[str, object]], *, newline: bool = True) -> None:
        text = "\n".join(json.dumps(entry) for entry in entries)
        if newline:
            text += "\n"
        self.path.write_text(text, encoding="utf-8")

    def fixture(self) -> list[dict[str, object]]:
        return [
            {
                "type": "session",
                "version": 3,
                "id": "raw-private-session-id",
                "timestamp": "2026-08-25T00:00:00Z",
                "cwd": "/private/project",
            },
            {
                "type": "model_change",
                "id": "model-1",
                "parentId": None,
                "timestamp": "2026-08-25T00:00:01Z",
                "provider": "openai-codex",
                "modelId": "gpt-5.6-sol",
            },
            {
                "type": "thinking_level_change",
                "id": "effort-1",
                "parentId": "model-1",
                "timestamp": "2026-08-25T00:00:02Z",
                "thinkingLevel": "high",
            },
            {
                "type": "message",
                "id": "assistant-1",
                "parentId": "effort-1",
                "timestamp": "2026-08-25T00:00:03Z",
                "message": {
                    "role": "assistant",
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                    "responseId": "raw-private-response-id",
                    "content": [{"type": "text", "text": "private answer"}],
                    "usage": usage(),
                    "stopReason": "toolUse",
                },
            },
            {
                "type": "message",
                "id": "tool-1",
                "parentId": "assistant-1",
                "timestamp": "2026-08-25T00:00:04Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "raw-private-tool-id",
                    "toolName": "nested-agent",
                    "content": [{"type": "text", "text": "private output"}],
                    "usage": usage(
                        input_tokens=10,
                        output_tokens=2,
                        cache_read=3,
                        cache_write=0,
                        reasoning=1,
                        cost="0.001",
                    ),
                    "isError": False,
                },
            },
            {
                "type": "compaction",
                "id": "compact-1",
                "parentId": "tool-1",
                "timestamp": "2026-08-25T00:00:05Z",
                "summary": "private summary",
                "tokensBefore": 1000,
                "usage": usage(
                    input_tokens=20,
                    output_tokens=4,
                    cache_read=0,
                    cache_write=0,
                    reasoning=None,
                    cost="0.002",
                ),
            },
        ]

    def test_native_session_usage_includes_assistant_nested_and_compaction(self) -> None:
        self.write(self.fixture())
        collection = collect_pi_session_usage(self.path, self.binding)
        self.assertEqual(len(collection.facts), 3)
        self.assertEqual(
            [fact.activity for fact in collection.facts],
            ["assistant_turn", "nested_tool_llm", "compaction"],
        )
        summary = collection.summary()
        self.assertEqual(summary["totals"]["total_tokens"], 214)
        self.assertEqual(summary["totals"]["reasoning"], 5)
        self.assertNotIn("provider_estimated_cost_usd", summary)
        self.assertEqual(summary["pricing_contract"], "tokens_only_openrouter_authority_required")
        self.assertEqual(len(summary["request_partitions"]), 3)
        self.assertEqual(
            summary["by_provider_model_effort"]["openai-codex/gpt-5.6-sol/high"],
            {
                "event_count": 3,
                "input": 130,
                "cache_read": 53,
                "cache_write": 5,
                "output": 26,
                "reasoning": 5,
                "total_tokens": 214,
            },
        )

    def test_header_only_new_session_is_an_exact_zero_usage_prefix(self) -> None:
        self.write(self.fixture()[:3])
        snapshot = build_pi_session_snapshot(self.path, self.binding)

        self.assertEqual(snapshot["summary"]["event_count"], 0)
        self.assertEqual(snapshot["summary"]["totals"]["total_tokens"], 0)
        self.assertEqual(snapshot["summary"]["request_partitions"], [])
        self.assertEqual(snapshot["summary"]["source_bytes"], self.path.stat().st_size)
        self.assertNotIn("raw-private-session-id", json.dumps(snapshot))

    def test_procurement_is_explicit_and_not_inferred_from_provider(self) -> None:
        self.write(self.fixture())
        binding = PiUsageBinding(
            work_item_id="wi_20260822_730",
            phase="work_item",
            epoch=2,
            procurement_source="unknown",
        )
        facts = collect_pi_session_usage(self.path, binding).facts
        self.assertTrue(facts)
        self.assertTrue(all(fact.procurement_source == "unknown" for fact in facts))

    def test_output_omits_raw_session_response_content_and_paths(self) -> None:
        self.write(self.fixture())
        collection = collect_pi_session_usage(self.path, self.binding)
        serialized = json.dumps(
            {
                "facts": [fact.to_dict() for fact in collection.facts],
                "summary": collection.summary(),
            }
        )
        for forbidden in (
            "raw-private-session-id",
            "raw-private-response-id",
            "raw-private-tool-id",
            "private answer",
            "private output",
            "private summary",
            "/private/project",
            "private-session-id.jsonl",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("pi_session_v1_", serialized)
        self.assertIn("pi_usage_v1_", serialized)

    def test_same_session_is_byte_stable_across_replay(self) -> None:
        self.write(self.fixture())
        first = collect_pi_session_usage(self.path, self.binding)
        second = collect_pi_session_usage(self.path, self.binding)
        self.assertEqual(first, second)

    def test_stable_duplicate_is_deduped_but_changed_identity_and_counter_fail_closed(self) -> None:
        entries = self.fixture()
        entries.append({**entries[-1]})
        self.write(entries)
        collection = collect_pi_session_usage(self.path, self.binding)
        self.assertEqual(len(collection.facts), 3)
        self.assertEqual(collection.deduplicated_entry_count, 1)

        changed = self.fixture()
        changed.append({**changed[-1], "summary": "changed immutable content"})
        self.write(changed)
        with self.assertRaisesRegex(PiUsageError, "duplicate.*changed"):
            collect_pi_session_usage(self.path, self.binding)

        entries = self.fixture()
        entries[3]["message"]["usage"]["totalTokens"] = 999  # type: ignore[index]
        self.write(entries)
        with self.assertRaisesRegex(PiUsageError, "does not conserve"):
            collect_pi_session_usage(self.path, self.binding)

    def test_live_partial_tail_fails_instead_of_dropping_usage(self) -> None:
        self.write(self.fixture(), newline=False)
        with self.assertRaisesRegex(PiUsageError, "unterminated"):
            collect_pi_session_usage(self.path, self.binding)

    def test_reasoning_cannot_exceed_output(self) -> None:
        entries = self.fixture()
        entries[3]["message"]["usage"]["reasoning"] = 999  # type: ignore[index]
        self.write(entries)
        with self.assertRaisesRegex(PiUsageError, "reasoning"):
            collect_pi_session_usage(self.path, self.binding)

    def test_cli_uses_host_injected_session_path_without_echoing_it(self) -> None:
        self.write(self.fixture())
        output = io.StringIO()
        with patch.dict(
            os.environ,
            {"PI_SESSION_FILE": str(self.path)},
            clear=True,
        ), redirect_stdout(output):
            rc = cli_main([
                "pi-session-probe",
                "--work-item", "wi_20260822_730",
                "--phase", "work_item",
                "--epoch", "1",
                "--procurement-source", "chatgpt_subscription",
            ])
        self.assertEqual(rc, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["schema"], "pi_session_usage_snapshot/v1")
        self.assertEqual(receipt["summary"]["event_count"], 3)
        self.assertEqual(receipt["lifecycle_state"], "open")
        self.assertEqual(len(receipt["snapshot_sha256"]), 64)
        self.assertNotIn(str(self.path), output.getvalue())

    def test_cli_uses_exact_host_binding_and_immutable_baseline(self) -> None:
        entries = self.fixture()
        self.write(entries[:3])
        binding = build_runtime_usage_binding(
            work_item_id="wi_20260822_730",
            agent_id="pulse",
            phase="work_item",
            epoch=12,
            runtime="pi",
            procurement_source="chatgpt_subscription",
            authority_ref="session_v1_" + "a" * 64,
            execution_surface="surface_v1_local_macos",
            issued_at="2026-08-25T01:00:00Z",
        )
        env = {
            "PI_SESSION_FILE": str(self.path),
            "PULSE_RUNTIME_USAGE_BINDING": json.dumps(binding.to_dict()),
        }
        baseline_output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), redirect_stdout(baseline_output):
            rc = cli_main(["pi-session-probe", "--measurement-role", "baseline"])
        self.assertEqual(rc, 0)
        baseline = json.loads(baseline_output.getvalue())
        self.assertEqual(baseline["schema"], "pi_session_usage_snapshot/v2")
        self.assertEqual(baseline["summary"]["event_count"], 0)
        baseline_path = Path(self.temp.name) / "baseline.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        baseline_path.chmod(0o600)
        self.write(entries)

        output = io.StringIO()
        with patch.dict(
            os.environ,
            {**env, "PULSE_PI_USAGE_BASELINE_SNAPSHOT": str(baseline_path)},
            clear=True,
        ), redirect_stdout(output):
            rc = cli_main([
                "pi-session-probe", "--lifecycle-state", "sealed",
                "--measurement-role", "final",
            ])
        self.assertEqual(rc, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["work_item_id"], "wi_20260822_730")
        self.assertEqual(receipt["epoch"], 12)
        self.assertEqual(receipt["lifecycle_state"], "sealed")
        self.assertEqual(receipt["summary"]["event_count"], 3)
        self.assertEqual(receipt["runtime_binding_sha256"], binding.binding_sha256)
        self.assertNotIn(str(self.path), output.getvalue())

    def test_cli_rejects_manual_attribution_conflicting_with_host_binding(self) -> None:
        self.write(self.fixture())
        binding = build_runtime_usage_binding(
            work_item_id="wi_20260822_730",
            agent_id="pulse",
            phase="work_item",
            epoch=12,
            runtime="pi",
            procurement_source="chatgpt_subscription",
            issued_at="2026-08-25T01:00:00Z",
        )
        with patch.dict(
            os.environ,
            {
                "PI_SESSION_FILE": str(self.path),
                "PULSE_RUNTIME_USAGE_BINDING": json.dumps(binding.to_dict()),
            },
            clear=True,
        ):
            rc = cli_main(["pi-session-probe", "--epoch", "13"])
        self.assertEqual(rc, 1)

    def test_unpersisted_origin_survives_restart_and_counts_the_first_answer(self) -> None:
        binding = PiUsageBinding(
            work_item_id="wi_20260822_730", phase="work_item", epoch=2,
            procurement_source="chatgpt_subscription", runtime_binding_sha256="7" * 64,
            agent_id="pulse", execution_surface="surface_v1_local_macos",
            authority_ref="session_v1_" + "8" * 64,
        )
        before = [e for e in self.fixture() if e["type"] in
                  {"session", "model_change", "thinking_level_change"}]
        self.write(before)
        baseline = build_pi_session_snapshot(self.path, binding,
            measurement_role="baseline", unpersisted_baseline=True)
        validate_pi_session_snapshot(baseline)
        self.assertEqual(baseline["attribution_boundary"]["baseline"]["source_bytes"], 0)
        restarted = self.fixture()
        restarted[0]["timestamp"] = "2026-08-25T01:00:00Z"
        restarted[1]["id"] = "restarted-model"
        self.write(restarted)
        final = build_pi_session_snapshot(self.path, binding,
            measurement_role="final", lifecycle_state="sealed", baseline_snapshot=baseline)
        self.assertGreater(final["summary"]["event_count"], 0)
        self.assertEqual(final["summary"]["totals"], collect_pi_session_usage(self.path, binding).summary()["totals"])
        self.assertEqual(final["attribution_boundary"]["excluded_pre_baseline_usage_events"], 0)
        with self.assertRaisesRegex(PiUsageError, "non-configuration"):
            build_pi_session_snapshot(self.path, binding,
                measurement_role="baseline", unpersisted_baseline=True)
        with self.assertRaisesRegex(PiUsageError, "only valid for a baseline"):
            build_pi_session_snapshot(self.path, binding,
                measurement_role="final", lifecycle_state="sealed",
                baseline_snapshot=baseline, unpersisted_baseline=True)

    def test_invalid_historical_counters_do_not_block_a_new_managed_interval(self) -> None:
        entries = self.fixture()
        for entry in entries:
            message = entry.get("message", {})
            if message.get("role") == "assistant" and message.get("usage"):
                message["usage"]["reasoning"] = 2681
                message["usage"]["output"] = 2496
        self.write(entries)
        binding = PiUsageBinding(
            work_item_id="wi_20260822_730", phase="work_item", epoch=2,
            procurement_source="openrouter_api", runtime_binding_sha256="7" * 64,
            agent_id="pulse", execution_surface="surface_v1_local_macos",
            authority_ref="session_v1_" + "8" * 64,
        )
        baseline = build_pi_session_snapshot(self.path, binding,
            generated_at="2026-08-25T00:01:00Z", measurement_role="baseline")
        self.assertEqual(baseline["summary"]["event_count"], 0)
        final = build_pi_session_snapshot(self.path, binding,
            generated_at="2026-08-25T00:02:00Z", measurement_role="final",
            lifecycle_state="sealed", baseline_snapshot=baseline)
        self.assertEqual(final["summary"]["event_count"], 0)
        bad = {"type":"message", "id":"bad-current", "timestamp":"2026-08-25T00:03:00Z",
            "message":{"role":"assistant", "provider":"openrouter", "model":"deepseek/test",
                "usage":usage(output_tokens=2496, reasoning=2681)}}
        self.write([*entries, bad])
        with self.assertRaisesRegex(PiUsageError, "reasoning cannot exceed output"):
            build_pi_session_snapshot(self.path, binding,
                generated_at="2026-08-25T00:04:00Z", measurement_role="final",
                lifecycle_state="sealed", baseline_snapshot=baseline)

    def test_branched_reused_session_charges_only_baseline_to_final_delta(self) -> None:
        before = self.fixture()
        before[4]["message"]["content"] = [{  # type: ignore[index]
            "type": "text",
            "text": "tool output mentions wi_20260822_730 and wi_20260820_568",
        }]
        self.write(before)
        binding = PiUsageBinding(
            work_item_id="wi_20260822_730",
            phase="work_item",
            epoch=2,
            procurement_source="openrouter_api",
            runtime_binding_sha256="7" * 64,
            agent_id="pulse",
            execution_surface="surface_v1_local_macos",
            authority_ref="session_v1_" + "8" * 64,
        )
        baseline = build_pi_session_snapshot(
            self.path,
            binding,
            generated_at="2026-08-25T00:01:00Z",
            measurement_role="baseline",
        )
        self.assertEqual(baseline["summary"]["event_count"], 0)
        self.assertEqual(
            baseline["attribution_boundary"]["excluded_pre_baseline_usage_events"],
            3,
        )
        bounded_entry = {
            "type": "message",
            "id": "bounded-assistant",
            "parentId": "compact-1",
            "timestamp": "2026-08-25T00:02:00Z",
            "message": {
                "role": "assistant",
                "provider": "openrouter",
                "model": "openai/gpt-fixture",
                "content": [{"type": "text", "text": "private bounded answer"}],
                "usage": usage(
                    input_tokens=20,
                    cache_read=80,
                    cache_write=5,
                    output_tokens=4,
                    reasoning=2,
                ),
            },
        }
        self.write([*before, bounded_entry, dict(bounded_entry)])
        final = build_pi_session_snapshot(
            self.path,
            binding,
            generated_at="2026-08-25T00:03:00Z",
            lifecycle_state="sealed",
            measurement_role="final",
            baseline_snapshot=baseline,
        )
        self.assertEqual(final["summary"]["event_count"], 1)
        self.assertEqual(final["summary"]["totals"]["total_tokens"], 109)
        self.assertEqual(final["summary"]["request_partitions"][0]["input"], 105)
        self.assertEqual(final["attribution_boundary"]["deduplicated_entries"], 1)
        serialized = json.dumps({"baseline": baseline, "final": final})
        for private in (
            "raw-private-session-id", "private bounded answer",
            "wi_20260822_730 and wi_20260820_568", str(self.path),
        ):
            self.assertNotIn(private, serialized)

        baseline_path = Path(self.temp.name) / "managed-baseline.json"
        final_path = Path(self.temp.name) / "managed-final.json"
        for artifact_path, artifact in ((baseline_path, baseline), (final_path, final)):
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            artifact_path.chmod(0o600)
            self.assertEqual(artifact_path.stat().st_mode & 0o777, 0o600)

        post_seal = {
            **bounded_entry,
            "id": "post-seal-assistant",
            "timestamp": "2026-08-25T00:04:00Z",
        }
        self.write([*before, bounded_entry, dict(bounded_entry), post_seal])
        verified = verify_pi_snapshot_source_prefix(self.path, final, position="final")
        self.assertGreater(verified["excluded_suffix_bytes"], 0)
        self.assertEqual(verified["excluded_suffix_records"], 1)
        text = self.path.read_text(encoding="utf-8").replace(
            "raw-private-session-id", "changed-private-session",
            1,
        )
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(PiUsageError, "changed before the sealed final"):
            verify_pi_snapshot_source_prefix(self.path, final, position="final")

    def test_snapshot_digest_rejects_tampering(self) -> None:
        self.write(self.fixture())
        snapshot = build_pi_session_snapshot(
            self.path,
            self.binding,
            generated_at="2026-08-25T00:00:00Z",
        )
        validate_pi_session_snapshot(snapshot)
        snapshot["summary"]["totals"]["total_tokens"] += 1
        with self.assertRaisesRegex(PiUsageError, "digest"):
            validate_pi_session_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
