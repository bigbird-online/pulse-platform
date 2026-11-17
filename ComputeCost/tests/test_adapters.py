from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost import adapters
from pulse_compute_cost.adapters import collect_usage_events


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def codex_meta(session_id: str, parent_id: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"id": session_id, "session_id": session_id}
    if parent_id is not None:
        payload["parent_thread_id"] = parent_id
        payload["source"] = {"subagent": {"thread_spawn": {
            "parent_thread_id": parent_id,
            "depth": 1,
        }}}
    return {"type": "session_meta", "payload": payload}


def codex_turn(
    turn_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
    reasoning_output_tokens: int = 0,
    timestamp: str,
) -> list[dict[str, object]]:
    return [
        {"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": turn_id,
        }},
        {"type": "turn_context", "payload": {
            "turn_id": turn_id,
            "cwd": "/private/path-must-not-drive-lineage",
            "model": "gpt-fixture",
            "effort": "high",
        }},
        {"type": "event_msg", "timestamp": timestamp, "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "cache_write_input_tokens": cache_write_input_tokens,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
            }},
        }},
        {"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": turn_id,
        }},
    ]


def write_catalog(path: Path) -> None:
    path.write_text(json.dumps({
        "providers": {"openai-chatgpt": {"protocol": "openai"}},
        "models": [{"id": "gpt-fixture", "provider": "openai-chatgpt"}],
    }), encoding="utf-8")


class AdapterContractTests(unittest.TestCase):
    def test_codex_parent_lineage_is_hashed_and_copy_all_conserves_depth_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "codex"
            catalog = root / "models.json"
            write_catalog(catalog)
            root_id = "root-session-private"
            child_id = "child-session-private"
            grandchild_id = "grandchild-session-private"
            root_rows = [codex_meta(root_id)] + codex_turn(
                "root-turn-private",
                input_tokens=100,
                output_tokens=10,
                cached_input_tokens=40,
                cache_write_input_tokens=4,
                reasoning_output_tokens=2,
                timestamp="2026-08-17T01:00:00Z",
            )
            child_rows = [codex_meta(child_id, root_id)] + root_rows + codex_turn(
                "child-turn-private",
                input_tokens=130,
                output_tokens=14,
                cached_input_tokens=50,
                cache_write_input_tokens=6,
                reasoning_output_tokens=3,
                timestamp="2026-08-17T02:00:00Z",
            )
            grandchild_rows = (
                [codex_meta(grandchild_id, child_id)]
                + child_rows
                + codex_turn(
                    "grandchild-turn-private",
                    input_tokens=150,
                    output_tokens=17,
                    cached_input_tokens=55,
                    cache_write_input_tokens=7,
                    reasoning_output_tokens=5,
                    timestamp="2026-08-17T03:00:00Z",
                )
            )
            write_jsonl(sessions / f"rollout-{root_id}.jsonl", root_rows)
            write_jsonl(sessions / f"rollout-{child_id}.jsonl", child_rows)
            write_jsonl(sessions / f"rollout-{grandchild_id}.jsonl", grandchild_rows)

            events = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-17T04:00:00Z",
                codex_sessions_dir=sessions,
                model_catalog_path=catalog,
            ).events
            self.assertEqual([event.tokens.input for event in events], [100, 30, 20])
            self.assertEqual([event.tokens.output for event in events], [10, 4, 3])
            self.assertEqual([event.tokens.cache_read for event in events], [40, 10, 5])
            self.assertEqual([event.tokens.cache_write for event in events], [4, 2, 1])
            self.assertEqual([event.tokens.reasoning for event in events], [2, 1, 2])
            self.assertEqual(sum(event.tokens.input for event in events), 150)
            self.assertEqual([event.lineage.session_role for event in events], [
                "main", "sub_agent", "sub_agent",
            ])
            root_ref = events[0].lineage.root_run_ref
            self.assertRegex(root_ref, r"^lineage_v1_[0-9a-f]{64}$")
            self.assertEqual(events[1].lineage.parent_run_ref, root_ref)
            self.assertEqual(events[1].lineage.root_run_ref, root_ref)
            self.assertEqual(events[2].lineage.root_run_ref, root_ref)
            self.assertRegex(
                events[2].lineage.parent_run_ref,
                r"^lineage_v1_[0-9a-f]{64}$",
            )
            rendered = json.dumps([event.to_dict() for event in events])
            for raw in (root_id, child_id, grandchild_id, "turn-private", "/private/path"):
                self.assertNotIn(raw, rendered)
            self.assertNotEqual(events[2].lineage.parent_run_ref, root_ref)
            self.assertEqual(events[0].lineage.root_agent, "unknown")
            self.assertEqual(events[1].lineage.execution_mode, "unknown")
            self.assertEqual(events[1].lineage.cron_job_ref, "unknown")
            self.assertEqual(events[1].lineage.scan_ref, "unknown")

    def test_codex_fork_none_emits_child_usage_without_parent_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "codex"
            catalog = root / "models.json"
            write_catalog(catalog)
            root_id = "none-root-private"
            child_id = "none-child-private"
            root_rows = [codex_meta(root_id)] + codex_turn(
                "root", input_tokens=100, output_tokens=10,
                timestamp="2026-08-17T01:00:00Z",
            )
            child_rows = [codex_meta(child_id, root_id)] + codex_turn(
                "child", input_tokens=30, output_tokens=4,
                timestamp="2026-08-17T02:00:00Z",
            )
            write_jsonl(sessions / f"rollout-{root_id}.jsonl", root_rows)
            write_jsonl(sessions / f"rollout-{child_id}.jsonl", child_rows)
            events = collect_usage_events(
                machine="fixture", observed_at="2026-08-17T03:00:00Z",
                codex_sessions_dir=sessions, model_catalog_path=catalog,
            ).events
            self.assertEqual([event.tokens.input for event in events], [100, 30])

    def test_codex_copy_all_resume_uses_prefix_once_then_normal_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "codex"
            catalog = root / "models.json"
            write_catalog(catalog)
            root_id = "resume-root-private"
            child_id = "resume-child-private"
            root_rows = [codex_meta(root_id)] + codex_turn(
                "root", input_tokens=100, output_tokens=10,
                timestamp="2026-08-17T01:00:00Z",
            )
            child_rows = (
                [codex_meta(child_id, root_id)]
                + root_rows
                + codex_turn(
                    "child-first", input_tokens=130, output_tokens=14,
                    timestamp="2026-08-17T02:00:00Z",
                )
                + codex_turn(
                    "child-resume", input_tokens=145, output_tokens=16,
                    timestamp="2026-08-17T03:00:00Z",
                )
            )
            write_jsonl(sessions / f"rollout-{root_id}.jsonl", root_rows)
            write_jsonl(sessions / f"rollout-{child_id}.jsonl", child_rows)
            events = collect_usage_events(
                machine="fixture", observed_at="2026-08-17T04:00:00Z",
                codex_sessions_dir=sessions, model_catalog_path=catalog,
            ).events
            self.assertEqual([event.tokens.input for event in events], [100, 30, 15])
            self.assertEqual([event.tokens.output for event in events], [10, 4, 2])

    def test_codex_parent_nonprefix_match_is_not_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "codex"
            catalog = root / "models.json"
            write_catalog(catalog)
            root_id = "nonprefix-root-private"
            child_id = "nonprefix-child-private"
            root_rows = [codex_meta(root_id)] + codex_turn(
                "root", input_tokens=100, output_tokens=10,
                timestamp="2026-08-17T02:00:00Z",
            )
            child_rows = (
                [codex_meta(child_id, root_id)]
                + codex_turn(
                    "genuine-child", input_tokens=10, output_tokens=1,
                    timestamp="2026-08-17T01:00:00Z",
                )
                + root_rows
            )
            write_jsonl(sessions / f"rollout-{root_id}.jsonl", root_rows)
            write_jsonl(sessions / f"rollout-{child_id}.jsonl", child_rows)
            child_events = [
                event for event in collect_usage_events(
                    machine="fixture", observed_at="2026-08-17T03:00:00Z",
                    codex_sessions_dir=sessions, model_catalog_path=catalog,
                ).events
                if event.lineage.session_role == "sub_agent"
            ]
            self.assertEqual([event.tokens.input for event in child_events], [10, 90])

    def test_codex_unverified_parent_chain_keeps_child_evidence_and_root_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "codex"
            catalog = root / "models.json"
            write_catalog(catalog)
            child_id = "orphan-child-private"
            write_jsonl(
                sessions / f"rollout-{child_id}.jsonl",
                [codex_meta(child_id, "missing-parent-private")]
                + codex_turn(
                    "child", input_tokens=30, output_tokens=4,
                    timestamp="2026-08-17T02:00:00Z",
                ),
            )
            events = collect_usage_events(
                machine="fixture", observed_at="2026-08-17T03:00:00Z",
                codex_sessions_dir=sessions, model_catalog_path=catalog,
            ).events
            self.assertEqual([event.tokens.input for event in events], [30])
            self.assertEqual(events[0].lineage.session_role, "sub_agent")
            self.assertEqual(events[0].lineage.root_run_ref, "unknown")
            self.assertRegex(events[0].lineage.parent_run_ref, r"^lineage_v1_[0-9a-f]{64}$")

    def test_codex_metadata_must_match_rollout_file_for_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "codex"
            catalog = root / "models.json"
            write_catalog(catalog)
            raw_id = "mismatched-session-private"
            write_jsonl(
                sessions / "rollout-different-id.jsonl",
                [codex_meta(raw_id)] + codex_turn(
                    "turn-private",
                    input_tokens=12,
                    output_tokens=2,
                    timestamp="2026-08-17T02:00:00Z",
                ),
            )
            event = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-17T03:00:00Z",
                codex_sessions_dir=sessions,
                model_catalog_path=catalog,
            ).events[0]
            self.assertEqual(event.lineage.session_role, "unknown")
            self.assertEqual(event.lineage.root_run_ref, "unknown")
            self.assertNotIn(raw_id, json.dumps(event.to_dict()))

    def test_codex_parent_evidence_accepts_either_source_and_rejects_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "codex"
            catalog = root / "models.json"
            write_catalog(catalog)
            root_id = "evidence-root-private"
            direct_id = "direct-child-private"
            nested_id = "nested-child-private"
            mismatch_id = "mismatch-child-private"
            root_rows = [codex_meta(root_id)] + codex_turn(
                "root",
                input_tokens=100,
                output_tokens=10,
                timestamp="2026-08-17T01:00:00Z",
            )
            direct_meta = codex_meta(direct_id, root_id)
            direct_meta["payload"].pop("source")  # type: ignore[union-attr]
            nested_meta = codex_meta(nested_id, root_id)
            nested_meta["payload"].pop("parent_thread_id")  # type: ignore[union-attr]
            mismatch_meta = codex_meta(mismatch_id, root_id)
            mismatch_meta["payload"]["source"]["subagent"]["thread_spawn"][  # type: ignore[index]
                "parent_thread_id"
            ] = "different-parent-private"
            write_jsonl(sessions / f"rollout-{root_id}.jsonl", root_rows)
            write_jsonl(
                sessions / f"rollout-{direct_id}.jsonl",
                [direct_meta] + root_rows + codex_turn(
                    "direct",
                    input_tokens=110,
                    output_tokens=11,
                    timestamp="2026-08-17T02:00:00Z",
                ),
            )
            write_jsonl(
                sessions / f"rollout-{nested_id}.jsonl",
                [nested_meta] + codex_turn(
                    "nested",
                    input_tokens=20,
                    output_tokens=2,
                    timestamp="2026-08-17T03:00:00Z",
                ),
            )
            write_jsonl(
                sessions / f"rollout-{mismatch_id}.jsonl",
                [mismatch_meta] + codex_turn(
                    "mismatch",
                    input_tokens=30,
                    output_tokens=3,
                    timestamp="2026-08-17T04:00:00Z",
                ),
            )
            events = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-17T05:00:00Z",
                codex_sessions_dir=sessions,
                model_catalog_path=catalog,
            ).events
            by_input = {event.tokens.input: event for event in events}
            self.assertEqual(by_input[10].lineage.session_role, "sub_agent")
            self.assertEqual(by_input[20].lineage.session_role, "sub_agent")
            self.assertEqual(by_input[30].lineage.session_role, "unknown")
            self.assertEqual(by_input[30].lineage.parent_run_ref, "unknown")

    def test_codex_since_hashes_only_active_rollout_and_required_parent_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "codex"
            catalog = root / "models.json"
            write_catalog(catalog)
            root_id = "old-required-parent-private"
            child_id = "active-child-private"
            unrelated_id = "old-unrelated-private"
            root_rows = [codex_meta(root_id)] + codex_turn(
                "root",
                input_tokens=100,
                output_tokens=10,
                timestamp="2026-08-16T01:00:00Z",
            )
            parent_path = sessions / f"rollout-{root_id}.jsonl"
            child_path = sessions / f"rollout-{child_id}.jsonl"
            unrelated_path = sessions / f"rollout-{unrelated_id}.jsonl"
            write_jsonl(parent_path, root_rows)
            write_jsonl(
                child_path,
                [codex_meta(child_id, root_id)] + root_rows + codex_turn(
                    "child",
                    input_tokens=130,
                    output_tokens=14,
                    timestamp="2026-08-17T02:00:00Z",
                ),
            )
            write_jsonl(unrelated_path, [codex_meta(unrelated_id)])
            os.utime(parent_path, (1, 1))
            os.utime(unrelated_path, (1, 1))

            with patch.object(
                adapters,
                "_index_codex_rollout",
                wraps=adapters._index_codex_rollout,
            ) as indexed:
                events = collect_usage_events(
                    machine="fixture",
                    observed_at="2026-08-18T00:00:00Z",
                    codex_sessions_dir=sessions,
                    model_catalog_path=catalog,
                    since="2026-08-17T00:00:00Z",
                ).events
            indexed_paths = {call.args[0] for call in indexed.call_args_list}
            self.assertEqual(indexed_paths, {child_path, parent_path})
            self.assertEqual([event.tokens.input for event in events], [30])

    def test_runtime_evidence_wins_and_relay_only_attributes_or_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relay = root / "relay"
            cc = root / "cc" / "-home-pulse-Coevolution-Bigbird-PULSE-vultr-pulse-space"
            codex = root / "codex" / "2026" / "08" / "17"
            catalog = root / "models.json"
            catalog.write_text(json.dumps({
                "providers": {
                    "openrouter": {"protocol": "anthropic"},
                    "openai-chatgpt": {"protocol": "openai"},
                },
                "models": [
                    {"id": "deepseek-fixture", "provider": "openrouter"},
                    {"id": "gpt-fixture", "provider": "openai-chatgpt"},
                ],
            }), encoding="utf-8")
            write_jsonl(relay / "2026-08-17.jsonl", [
                {
                    "ts": "2026-08-17T00:00:01Z", "tenant": "owner:pulse:feishu",
                    "session_id": "cc-session-private", "model": "deepseek-fixture",
                    "input_tokens": 10, "output_tokens": 2,
                    "cache_read_input_tokens": 30, "cache_creation_input_tokens": 5,
                },
                {
                    "ts": "2026-08-17T01:00:01Z", "tenant": "owner:pulse:feishu",
                    "session_id": "relay-codex-private", "model": "gpt-fixture",
                    "input_tokens": 100, "output_tokens": 10,
                    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                },
                {
                    "ts": "2026-08-17T02:00:01Z", "tenant": "owner:duke:telegram",
                    "session_id": "relay-codex-private", "model": "gpt-fixture",
                    "input_tokens": 180, "output_tokens": 18,
                    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                },
                {
                    "ts": "2026-08-17T03:00:00Z", "tenant": "owner:aki:feishu",
                    "session_id": "unmatched-private", "model": "missing-model",
                    "input_tokens": 4, "output_tokens": 1,
                    "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2,
                },
            ])
            write_jsonl(cc / "session-private.jsonl", [
                {
                    "type": "assistant", "timestamp": "2026-08-17T00:00:00Z",
                    "sessionId": "cc-session-private", "uuid": "row-old",
                    "cwd": "/home/pulse/Coevolution/Bigbird/PULSE/vultr-pulse-space",
                    "effort": "low",
                    "message": {"id": "message-private", "model": "deepseek-fixture", "usage": {
                        "input_tokens": 1, "output_tokens": 1,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                    }},
                },
                {
                    "type": "assistant", "timestamp": "2026-08-17T00:00:00Z",
                    "sessionId": "cc-session-private", "uuid": "row-final",
                    "cwd": "/home/pulse/Coevolution/Bigbird/PULSE/vultr-pulse-space",
                    "effort": "low",
                    "message": {"id": "message-private", "model": "deepseek-fixture", "usage": {
                        "input_tokens": 10, "output_tokens": 2,
                        "cache_read_input_tokens": 30, "cache_creation_input_tokens": 5,
                    }},
                },
            ])
            write_jsonl(codex / "rollout-private.jsonl", [
                {"type": "session_meta", "timestamp": "2026-08-17T00:59:00Z", "payload": {
                    "id": "codex-session-private", "model_provider": "openai",
                }},
                {"type": "event_msg", "timestamp": "2026-08-17T00:59:01Z", "payload": {
                    "type": "task_started", "turn_id": "turn-private-1",
                }},
                {"type": "turn_context", "timestamp": "2026-08-17T00:59:02Z", "payload": {
                    "turn_id": "turn-private-1", "cwd": "/tmp/unmapped",
                    "model": "gpt-fixture", "effort": "high",
                }},
                {"type": "event_msg", "timestamp": "2026-08-17T01:00:00Z", "payload": {
                    "type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 50,
                        "output_tokens": 10, "reasoning_output_tokens": 3,
                    }},
                }},
                {"type": "event_msg", "timestamp": "2026-08-17T01:00:00Z", "payload": {
                    "type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 50,
                        "output_tokens": 10, "reasoning_output_tokens": 3,
                    }},
                }},
                {"type": "event_msg", "timestamp": "2026-08-17T01:00:02Z", "payload": {
                    "type": "task_complete", "turn_id": "turn-private-1",
                }},
                {"type": "event_msg", "timestamp": "2026-08-17T01:59:01Z", "payload": {
                    "type": "task_started", "turn_id": "turn-private-2",
                }},
                {"type": "turn_context", "timestamp": "2026-08-17T01:59:02Z", "payload": {
                    "turn_id": "turn-private-2", "cwd": "/tmp/unmapped",
                    "model": "gpt-fixture", "effort": "medium",
                }},
                {"type": "event_msg", "timestamp": "2026-08-17T02:00:00Z", "payload": {
                    "type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": 180, "cached_input_tokens": 90,
                        "cache_write_input_tokens": 4, "output_tokens": 18,
                        "reasoning_output_tokens": 5,
                    }},
                }},
                {"type": "event_msg", "timestamp": "2026-08-17T02:00:02Z", "payload": {
                    "type": "task_complete", "turn_id": "turn-private-2",
                }},
            ])

            collection = collect_usage_events(
                machine="fixture-machine",
                observed_at="2026-08-17T04:00:00Z",
                relay_metering_dir=relay,
                cc_projects_dir=root / "cc",
                codex_sessions_dir=root / "codex",
                model_catalog_path=catalog,
            )

            self.assertEqual(len(collection.events), 4)
            cc_event = next(event for event in collection.events if event.adapter == "cc_transcript")
            self.assertEqual(cc_event.tokens.input, 45)
            self.assertEqual(cc_event.source, "openrouter_api")
            self.assertEqual(cc_event.agent, "pulse")
            self.assertEqual(cc_event.consumer_path, "relay/pulse/feishu")

            codex_events = [event for event in collection.events if event.adapter == "codex_rollout"]
            self.assertEqual([event.tokens.input for event in codex_events], [100, 80])
            self.assertEqual([event.tokens.output for event in codex_events], [10, 8])
            self.assertEqual([event.tokens.cache_read for event in codex_events], [50, 40])
            self.assertEqual([event.source for event in codex_events], ["chatgpt_pro"] * 2)
            self.assertEqual([event.agent for event in codex_events], ["pulse", "duke"])

            fallback = next(event for event in collection.events if event.adapter == "relay_metering")
            self.assertEqual(fallback.tokens.input, 9)
            self.assertEqual(fallback.runtime, "unknown")
            self.assertIn("relay_fallback_usage_semantics", fallback.quality_flags)
            rendered = json.dumps([event.to_dict() for event in collection.events])
            for private in (
                "owner", "cc-session-private", "relay-codex-private",
                "codex-session-private", "message-private", "turn-private",
            ):
                self.assertNotIn(private, rendered)
            self.assertEqual(
                collection.diagnostics.reconciled_relay_rows,
                {"cc_transcript": 1, "codex_rollout": 2},
            )

    def test_incomplete_codex_task_flushes_last_usage_at_eof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "models.json"
            write_catalog(catalog)
            rows = [codex_meta("incomplete-session-private")] + codex_turn(
                "incomplete-turn-private",
                input_tokens=388_588,
                output_tokens=1_881,
                cached_input_tokens=335_104,
                reasoning_output_tokens=0,
                timestamp="2026-08-17T02:00:00Z",
            )[:-1]
            write_jsonl(root / "codex" / "rollout-incomplete.jsonl", rows)

            collection = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-17T03:00:00Z",
                codex_sessions_dir=root / "codex",
                model_catalog_path=catalog,
            )

            self.assertEqual(len(collection.events), 1)
            event = collection.events[0]
            self.assertEqual(event.tokens.input, 388_588)
            self.assertEqual(event.tokens.cache_read, 335_104)
            self.assertEqual(event.tokens.output, 1_881)
            self.assertIn("incomplete_task", event.quality_flags)
            self.assertEqual(collection.diagnostics.emitted_events["codex_rollout"], 1)

    def test_unmatched_codex_relay_rows_are_cumulative_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "models.json"
            catalog.write_text(json.dumps({
                "providers": {"openai-chatgpt": {"protocol": "openai"}},
                "models": [{"id": "gpt-fixture", "provider": "openai-chatgpt"}],
            }), encoding="utf-8")
            write_jsonl(root / "relay" / "2026-08-17.jsonl", [
                {
                    "ts": "2026-08-17T01:00:00Z", "tenant": "owner:pulse:feishu",
                    "session_id": "private", "model": "gpt-fixture",
                    "input_tokens": 100, "output_tokens": 10,
                    "cache_read_input_tokens": 50, "cache_creation_input_tokens": 5,
                },
                {
                    "ts": "2026-08-17T02:00:00Z", "tenant": "owner:pulse:feishu",
                    "session_id": "private", "model": "gpt-fixture",
                    "input_tokens": 180, "output_tokens": 18,
                    "cache_read_input_tokens": 90, "cache_creation_input_tokens": 9,
                },
            ])
            collection = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-17T03:00:00Z",
                relay_metering_dir=root / "relay",
                model_catalog_path=catalog,
            )
            self.assertEqual([event.tokens.input for event in collection.events], [100, 80])
            self.assertEqual([event.tokens.output for event in collection.events], [10, 8])
            self.assertEqual([event.tokens.cache_read for event in collection.events], [50, 40])
            self.assertEqual([event.tokens.cache_write for event in collection.events], [5, 4])
            self.assertEqual([event.runtime for event in collection.events], ["codex", "codex"])
            self.assertTrue(all(
                "relay_cumulative_delta" in event.quality_flags
                for event in collection.events
            ))

    def test_invalid_cumulative_cache_row_does_not_poison_next_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "models.json"
            catalog.write_text(json.dumps({
                "providers": {"openai-chatgpt": {"protocol": "openai"}},
                "models": [{"id": "gpt-fixture", "provider": "openai-chatgpt"}],
            }), encoding="utf-8")
            write_jsonl(root / "relay" / "2026-08-17.jsonl", [
                {"ts": "2026-08-17T01:00:00Z", "tenant": "owner:pulse:feishu",
                 "session_id": "private", "model": "gpt-fixture",
                 "input_tokens": 100, "output_tokens": 10,
                 "cache_read_input_tokens": 50, "cache_creation_input_tokens": 0},
                {"ts": "2026-08-17T02:00:00Z", "tenant": "owner:pulse:feishu",
                 "session_id": "private", "model": "gpt-fixture",
                 "input_tokens": 110, "output_tokens": 11,
                 "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20},
                {"ts": "2026-08-17T03:00:00Z", "tenant": "owner:pulse:feishu",
                 "session_id": "private", "model": "gpt-fixture",
                 "input_tokens": 130, "output_tokens": 13,
                 "cache_read_input_tokens": 60, "cache_creation_input_tokens": 5},
            ])
            collection = collect_usage_events(machine="fixture",
                observed_at="2026-08-17T04:00:00Z",
                relay_metering_dir=root / "relay", model_catalog_path=catalog)
            self.assertEqual([event.tokens.input for event in collection.events], [100, 30])
            self.assertEqual([event.tokens.cache_read for event in collection.events], [50, 10])
            self.assertEqual(collection.diagnostics.malformed_rows["relay_metering"], 1)

    def test_claimed_invalid_relay_counter_still_advances_deduplication_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "models.json"
            write_catalog(catalog)
            write_jsonl(root / "codex" / "native.jsonl", [codex_meta("private")] + codex_turn(
                "first", input_tokens=100, output_tokens=10, cached_input_tokens=50,
                timestamp="2026-08-17T01:00:00Z",
            ) + codex_turn(
                "claimed", input_tokens=110, output_tokens=11, cached_input_tokens=50,
                timestamp="2026-08-17T02:00:00Z",
            ))
            write_jsonl(root / "relay" / "2026-08-17.jsonl", [
                {"ts": f"2026-08-17T0{hour}:00:00Z", "tenant": "owner:pulse:feishu",
                 "session_id": "private", "model": "gpt-fixture",
                 "input_tokens": inp, "output_tokens": out,
                 "cache_read_input_tokens": read, "cache_creation_input_tokens": write}
                for hour, inp, out, read, write in [(1, 100, 10, 50, 0),
                    (2, 110, 11, 100, 20), (3, 210, 21, 120, 25)]
            ])
            collection = collect_usage_events(machine="fixture",
                observed_at="2026-08-17T04:00:00Z", codex_sessions_dir=root / "codex",
                relay_metering_dir=root / "relay", model_catalog_path=catalog)
            self.assertEqual(collection.diagnostics.reconciled_relay_rows["codex_rollout"], 2)
            fallback, = [event for event in collection.events if event.adapter == "relay_metering"]
            self.assertEqual((fallback.tokens.input, fallback.tokens.output,
                fallback.tokens.cache_read, fallback.tokens.cache_write), (100, 10, 20, 5))

    def test_relay_counter_resets_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "models.json"
            catalog.write_text(json.dumps({
                "providers": {"openai-chatgpt": {"protocol": "openai"}},
                "models": [{"id": "gpt-fixture", "provider": "openai-chatgpt"}],
            }), encoding="utf-8")
            write_jsonl(root / "relay" / "2026-08-17.jsonl", [
                {
                    "ts": "2026-08-17T01:00:00Z", "tenant": "owner:pulse:feishu",
                    "session_id": "private", "model": "gpt-fixture",
                    "input_tokens": 100, "output_tokens": 10,
                    "cache_read_input_tokens": 50,
                },
                {
                    "ts": "2026-08-17T02:00:00Z", "tenant": "owner:pulse:feishu",
                    "session_id": "private", "model": "gpt-fixture",
                    "input_tokens": 180, "output_tokens": 18,
                    "cache_read_input_tokens": 10,
                },
            ])
            collection = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-17T03:00:00Z",
                relay_metering_dir=root / "relay",
                model_catalog_path=catalog,
            )
            self.assertEqual([event.tokens.input for event in collection.events], [100, 80])
            self.assertEqual([event.tokens.output for event in collection.events], [10, 8])
            self.assertEqual([event.tokens.cache_read for event in collection.events], [50, 10])
            self.assertIn(
                "relay_cumulative_counter_reset",
                collection.events[1].quality_flags,
            )

    def test_relay_recovery_copy_keeps_one_stable_event_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "models.json"
            catalog.write_text(json.dumps({
                "providers": {"openai-chatgpt": {"protocol": "openai"}},
                "models": [{"id": "gpt-fixture", "provider": "openai-chatgpt"}],
            }), encoding="utf-8")
            duplicate = {
                "ts": "2026-08-17T01:00:00Z", "tenant": "owner:pulse:feishu",
                "session_id": "private", "model": "gpt-fixture",
                "input_tokens": 100, "output_tokens": 10,
                "cache_read_input_tokens": 50,
            }
            write_jsonl(root / "relay" / "2026-08-17.jsonl", [duplicate])
            write_jsonl(root / "relay" / "recovery.jsonl", [duplicate])
            collection = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-17T03:00:00Z",
                relay_metering_dir=root / "relay",
                model_catalog_path=catalog,
            )
            self.assertEqual(len(collection.events), 1)
            self.assertEqual(collection.events[0].tokens.input, 100)
            self.assertEqual(
                collection.diagnostics.deduplicated_events["relay_metering"],
                1,
            )

    def test_matched_relay_counter_precedes_unmatched_fallback_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "models.json"
            catalog.write_text(json.dumps({
                "providers": {"openai-chatgpt": {"protocol": "openai"}},
                "models": [{"id": "gpt-fixture", "provider": "openai-chatgpt"}],
            }), encoding="utf-8")
            write_jsonl(root / "relay" / "2026-08-17.jsonl", [
                {
                    "ts": "2026-08-17T01:00:00Z", "tenant": "owner:pulse:feishu",
                    "session_id": "relay-private", "model": "gpt-fixture",
                    "input_tokens": 100, "output_tokens": 10,
                    "cache_read_input_tokens": 50, "cache_creation_input_tokens": 5,
                },
                {
                    "ts": "2026-08-17T02:00:00Z", "tenant": "owner:pulse:feishu",
                    "session_id": "relay-private", "model": "gpt-fixture",
                    "input_tokens": 180, "output_tokens": 18,
                    "cache_read_input_tokens": 90, "cache_creation_input_tokens": 9,
                },
            ])
            write_jsonl(root / "codex" / "rollout.jsonl", [
                {"type": "session_meta", "payload": {"id": "codex-private"}},
                {"type": "event_msg", "payload": {
                    "type": "task_started", "turn_id": "turn-private",
                }},
                {"type": "turn_context", "payload": {
                    "model": "gpt-fixture", "cwd": "/tmp/unmapped",
                }},
                {"type": "event_msg", "timestamp": "2026-08-17T01:00:00Z", "payload": {
                    "type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": 100, "output_tokens": 10,
                        "cached_input_tokens": 50, "cache_write_input_tokens": 5,
                    }},
                }},
                {"type": "event_msg", "payload": {"type": "task_complete"}},
            ])
            collection = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-17T03:00:00Z",
                relay_metering_dir=root / "relay",
                codex_sessions_dir=root / "codex",
                model_catalog_path=catalog,
            )
            fallback = next(
                event for event in collection.events if event.adapter == "relay_metering"
            )
            self.assertEqual(fallback.tokens.input, 80)
            self.assertEqual(fallback.tokens.output, 8)
            self.assertEqual(fallback.tokens.cache_read, 40)
            self.assertEqual(fallback.tokens.cache_write, 4)

    def test_unmapped_codex_model_does_not_infer_provider_from_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "models.json"
            catalog.write_text(json.dumps({
                "providers": {"openai-chatgpt": {"protocol": "openai"}},
                "models": [],
            }), encoding="utf-8")
            write_jsonl(root / "codex" / "rollout.jsonl", [
                {"type": "session_meta", "payload": {
                    "id": "codex-private", "model_provider": "openai-chatgpt",
                }},
                {"type": "event_msg", "payload": {
                    "type": "task_started", "turn_id": "turn-private",
                }},
                {"type": "turn_context", "payload": {
                    "model": "unmapped-model", "cwd": "/tmp/unmapped",
                }},
                {"type": "event_msg", "timestamp": "2026-08-17T01:00:00Z", "payload": {
                    "type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": 100, "output_tokens": 10,
                    }},
                }},
                {"type": "event_msg", "payload": {"type": "task_complete"}},
            ])
            collection = collect_usage_events(
                machine="fixture",
                observed_at="2026-08-17T03:00:00Z",
                codex_sessions_dir=root / "codex",
                model_catalog_path=catalog,
            )
            self.assertEqual(len(collection.events), 1)
            event = collection.events[0]
            self.assertEqual(event.provider, "unknown")
            self.assertEqual(event.source, "unknown")
            self.assertEqual(event.runtime, "unknown")


if __name__ == "__main__":
    unittest.main()
