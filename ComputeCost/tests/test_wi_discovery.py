from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.wi_discovery import (
    auto_discover_wi_usage,
    _pi_source,
    discover_cc_wi_usage,
    load_wi_window,
)


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def meta(session_id: str) -> dict[str, object]:
    return {"type": "session_meta", "payload": {"id": session_id, "session_id": session_id}}


def child_meta(session_id: str, parent_id: str) -> dict[str, object]:
    return {
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": session_id,
            "parent_thread_id": parent_id,
        },
    }


def user_message(text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def delegation(source: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": f"<codex_delegation><source_thread_id>{source}</source_thread_id></codex_delegation>"}],
        },
    }


def turn(turn_id: str, *, input_tokens: int, cached: int, output: int, timestamp: str) -> list[dict[str, object]]:
    return [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
        {"type": "turn_context", "payload": {"turn_id": turn_id, "cwd": "/private", "model": "gpt-fixture", "effort": "high"}},
        {"type": "event_msg", "timestamp": timestamp, "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": 0,
            "output_tokens": output,
            "reasoning_output_tokens": output // 2,
        }}}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}},
    ]


class WiDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.harness = self.root / ".harness"
        (self.harness / "work-items").mkdir(parents=True)
        (self.harness / "work-items" / "wi_20260822_938.yaml").write_text(
            "work_item_id: wi_20260822_938\n"
            "status: completed\n"
            "owner: pulse\n"
            "created_at: '2026-08-22T13:52:39Z'\n"
            "updated_at: '2026-08-25T14:32:34Z'\n",
            encoding="utf-8",
        )
        self.codex = self.root / ".codex"
        self.sessions = self.codex / "sessions" / "2026" / "08"
        self.catalog = self.root / "models.json"
        self.catalog.write_text(json.dumps({
            "providers": {"openai-chatgpt": {"protocol": "openai"}},
            "models": [{"id": "gpt-fixture", "provider": "openai-chatgpt"}],
        }), encoding="utf-8")

    def write_assignment(
        self,
        *,
        work_item_id: str = "wi_20260822_938",
        assignment_id: str = "ho_20260823_101",
        outcome: bool = True,
    ) -> None:
        (self.harness / "events").mkdir(exist_ok=True)
        outcome_document = (
            f"""---
event_id: ev_outcome_{assignment_id}
event_type: work_item.outcome_accepted
operator: outcome-verifier
payload:
  verification_assignment: {assignment_id}
  verifier: outcome-verifier
  outcome_receipt_status: accepted
  from_status: awaiting_verification
  to_status: awaiting_verification
timestamp: '2026-08-24T00:10:00Z'
work_item_id: {work_item_id}
"""
            if outcome
            else ""
        )
        (self.harness / "events" / f"{work_item_id}.events.yaml").write_text(
            f"""---
event_id: ev_created_{assignment_id}
event_type: work_item.created
operator: pulse
payload:
  title: synthetic
timestamp: '2026-08-22T13:52:39Z'
work_item_id: {work_item_id}
---
event_id: ev_active_{assignment_id}
event_type: work_item.in_progress
operator: pulse
payload:
  from_status: ready
  to_status: in_progress
timestamp: '2026-08-22T14:00:00Z'
work_item_id: {work_item_id}
---
event_id: ev_verify_{assignment_id}
event_type: work_item.awaiting_verification
operator: pulse
payload:
  from_status: in_progress
  to_status: awaiting_verification
timestamp: '2026-08-22T23:59:00Z'
work_item_id: {work_item_id}
---
event_id: ev_created_handoff_{assignment_id}
event_type: handoff.created
operator: pulse
payload:
  handoff_id: {assignment_id}
  purpose: outcome_verification
  to_owner: outcome-verifier
  original_responsible_owner: pulse
  responsibility_transfer: not_transferred
timestamp: '2026-08-23T00:00:00Z'
work_item_id: {work_item_id}
---
event_id: ev_accepted_{assignment_id}
event_type: handoff.accepted
operator: outcome-verifier
payload:
  handoff_id: {assignment_id}
  from_state: pending
  to_state: accepted
timestamp: '2026-08-23T00:01:00Z'
work_item_id: {work_item_id}
{outcome_document}---
event_id: ev_completed_{assignment_id}
event_type: work_item.completed
operator: pulse
payload:
  from_status: awaiting_verification
  to_status: completed
timestamp: '2026-08-25T14:32:34Z'
work_item_id: {work_item_id}
""",
            encoding="utf-8",
        )
        (self.harness / "handoffs").mkdir(exist_ok=True)
        (self.harness / "handoffs" / f"{assignment_id}.yaml").write_text(
            f"""handoff_id: {assignment_id}
work_item_id: {work_item_id}
from_owner: pulse
to_owner: outcome-verifier
purpose: outcome_verification
ack_state: archived
""",
            encoding="utf-8",
        )

    def discover(self):
        return auto_discover_wi_usage(
            "wi_20260822_938",
            machine="fixture-machine",
            observed_at="2026-08-26T00:00:00Z",
            harness_root=self.harness,
            codex_home=self.codex,
            model_catalog_path=self.catalog,
            relay_config_path=self.root / "relay.json",
            relay_state_path=self.root / "relay-state.json",
            relay_pi_manifest_path=self.root / "pi-manifest.json",
            relay_metering_dir=self.root / "metering",
        )

    def test_migrated_work_item_has_a_closed_attribution_window(self) -> None:
        item = self.harness / "work-items" / "wi_20260824_582.yaml"
        item.write_text(
            "work_item_id: wi_20260824_582\n"
            "status: migrated\n"
            "created_at: '2026-08-24T14:16:07Z'\n"
            "updated_at: '2026-08-26T02:03:00Z'\n",
            encoding="utf-8",
        )

        window = load_wi_window(self.harness, "wi_20260824_582")

        self.assertEqual(window.work_item_id, "wi_20260824_582")
        self.assertLess(window.started_at, window.ended_at)

    def test_rollover_title_without_wi_joins_and_unrelated_is_excluded(self) -> None:
        root_id = "root-private"
        child_id = "child-private"
        other_id = "other-private"
        write_jsonl(self.codex / "session_index.jsonl", [
            {"id": root_id, "thread_name": "执行工作项 wi_20260822_938"},
            {"id": child_id, "thread_name": "修复通道呈现"},
            {"id": other_id, "thread_name": "另一个工作"},
        ])
        write_jsonl(
            self.sessions / f"rollout-{root_id}.jsonl",
            [meta(root_id)] + turn("root-turn", input_tokens=100, cached=80, output=10, timestamp="2026-08-23T00:00:00Z"),
        )
        write_jsonl(
            self.sessions / f"rollout-{child_id}.jsonl",
            [meta(child_id), delegation(root_id)] + turn("child-turn", input_tokens=50, cached=40, output=5, timestamp="2026-08-24T00:00:00Z"),
        )
        write_jsonl(
            self.sessions / f"rollout-{other_id}.jsonl",
            [meta(other_id)] + turn("other-turn", input_tokens=999, cached=0, output=999, timestamp="2026-08-24T00:00:00Z"),
        )

        cohort = self.discover()
        result = cohort.to_dict(include_events=True)
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["event_count"], 2)
        self.assertEqual(result["root_thread_count"], 2)
        self.assertEqual(result["tokens"]["input"], 150)
        self.assertEqual(result["tokens"]["cache_read"], 120)
        self.assertEqual(result["tokens"]["net_new_input"], 30)
        self.assertEqual(result["tokens"]["output"], 15)
        self.assertTrue(all(event["work_item_ref"] == "wi_20260822_938" for event in result["events"]))
        serialized = json.dumps(result)
        self.assertNotIn(root_id, serialized)
        self.assertNotIn(child_id, serialized)
        self.assertNotIn(other_id, serialized)
        self.assertEqual(result["cohort_sha256"], cohort.to_dict(include_events=True)["cohort_sha256"])

    def test_structured_assignment_discovers_multiple_no_wi_prompt_children_and_deduplicates_archive(self) -> None:
        assignment = "ho_20260823_101"
        self.write_assignment(assignment_id=assignment)
        root_id = "controller-private"
        children = ("verifier-child-private-a", "verifier-child-private-b")
        write_jsonl(
            self.sessions / f"rollout-{root_id}.jsonl",
            [meta(root_id)] + turn(
                "controller-turn", input_tokens=100, cached=80, output=10,
                timestamp="2026-08-23T00:02:00Z",
            ),
        )
        for index, child_id in enumerate(children):
            rows = [
                child_meta(child_id, root_id),
                user_message(f"Execute exact structured assignment {assignment}."),
                *turn(
                    f"child-turn-{index}", input_tokens=50, cached=40, output=5,
                    timestamp=f"2026-08-23T00:0{index + 3}:00Z",
                ),
            ]
            write_jsonl(self.sessions / f"rollout-{child_id}.jsonl", rows)
            if index == 0:
                write_jsonl(
                    self.codex / "archived_sessions" / f"rollout-{child_id}.jsonl",
                    rows,
                )

        result = self.discover().to_dict(include_events=True)

        # The Codex producer is observed; the combined cohort stays partial
        # because the synthetic terminal WI has no retrospective Pi source.
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["event_count"], 3)
        codex_attempt = next(
            item for item in result["producer_attempts"]
            if item["producer"] == "codex_desktop"
        )
        self.assertEqual(codex_attempt["status"], "observed")
        self.assertEqual(result["tokens"]["input"], 200)
        assignment_evidence = result["diagnostics"]["canonical_verifier_assignment"]
        self.assertEqual(assignment_evidence["assignment_count"], 1)
        self.assertEqual(assignment_evidence["assigned_verifier_count"], 1)
        self.assertEqual(assignment_evidence["outcome_bound_assignment_count"], 1)
        self.assertEqual(assignment_evidence["assigned_session_count"], 2)
        self.assertEqual(assignment_evidence["token_coverage"], "observed")
        self.assertEqual(result["diagnostics"]["archived_rollout_file_count"], 1)
        rendered = json.dumps(result)
        for private in (root_id, *children):
            self.assertNotIn(private, rendered)
        self.assertNotIn(assignment, rendered)

    def test_structured_assignment_without_rollout_preserves_session_count_and_token_gap(self) -> None:
        assignment = "ho_20260823_101"
        session_id = "archived-index-only-private"
        self.write_assignment(assignment_id=assignment)
        write_jsonl(
            self.codex / "session_index.jsonl",
            [{"id": session_id, "thread_name": f"Verifier assignment {assignment}"}],
        )

        result = self.discover().to_dict()

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["tokens"], "unavailable")
        self.assertEqual(result["event_count"], 0)
        assignment_evidence = result["diagnostics"]["canonical_verifier_assignment"]
        self.assertEqual(assignment_evidence["assigned_session_count"], 1)
        self.assertEqual(assignment_evidence["token_coverage"], "unavailable")
        self.assertEqual(
            result["diagnostics"]["candidate_evidence"]["status"],
            "assigned_tokens_unavailable",
        )
        self.assertNotIn(session_id, json.dumps(result))
        self.assertNotIn(assignment, json.dumps(result))

    def test_structured_assignment_mixed_lineage_is_rejected(self) -> None:
        assignment = "ho_20260823_101"
        self.write_assignment(assignment_id=assignment)
        root_id = "mixed-controller-private"
        child_id = "mixed-child-private"
        write_jsonl(
            self.sessions / f"rollout-{root_id}.jsonl",
            [
                meta(root_id),
                user_message("Continue unrelated wi_20260820_136."),
                *turn(
                    "root-turn", input_tokens=100, cached=80, output=5,
                    timestamp="2026-08-23T00:02:00Z",
                ),
            ],
        )
        write_jsonl(
            self.sessions / f"rollout-{child_id}.jsonl",
            [
                child_meta(child_id, root_id),
                user_message(f"Execute exact structured assignment {assignment}."),
                *turn(
                    "child-turn", input_tokens=200, cached=160, output=10,
                    timestamp="2026-08-23T00:03:00Z",
                ),
            ],
        )

        result = self.discover().to_dict()

        self.assertEqual(result["status"], "join_conflict")
        self.assertEqual(result["tokens"], "unavailable")
        self.assertEqual(result["event_count"], 0)
        self.assertEqual(
            result["diagnostics"]["candidate_evidence"]["status"],
            "present_but_unpartitioned",
        )

    def test_direct_wi_cli_auto_discovers_usage_without_app_server_ledger(self) -> None:
        session_id = "root-private"
        write_jsonl(self.codex / "session_index.jsonl", [
            {"id": session_id, "thread_name": "继续 WI938"},
        ])
        write_jsonl(
            self.sessions / f"rollout-{session_id}.jsonl",
            [meta(session_id)] + turn(
                "root-turn", input_tokens=100, cached=80, output=10,
                timestamp="2026-08-23T00:00:00Z",
            ),
        )
        output = io.StringIO()
        with redirect_stdout(output):
            rc = cli_main([
                "wi_20260822_938",
                "--harness-root", str(self.harness),
                "--codex-home", str(self.codex),
                "--model-catalog", str(self.catalog),
                "--machine", "fixture-machine",
                "--no-auto-price",
            ])
        self.assertEqual(rc, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["usage"]["producer"], "usage_event_cohort")
        self.assertEqual(report["usage"]["tokens"]["input_tokens"], 100)
        self.assertEqual(report["coverage"]["producer_discovery"], "observed")
        self.assertNotIn("no_joinable_usage_producer", report["warnings"])

    def test_user_prompt_seed_works_without_session_index_and_keeps_incomplete_usage(self) -> None:
        session_id = "prompt-seeded-private"
        write_jsonl(
            self.sessions / f"rollout-{session_id}.jsonl",
            [
                meta(session_id),
                user_message(
                    "Read-only UAT: run bootstrap --work-item wi_20260822_938 once."
                ),
                *turn(
                    "incomplete-turn",
                    input_tokens=388_588,
                    cached=335_104,
                    output=1_881,
                    timestamp="2026-08-23T00:00:00Z",
                )[:-1],
            ],
        )

        result = self.discover().to_dict(include_events=True)

        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["tokens"]["input"], 388_588)
        self.assertEqual(result["tokens"]["cache_read"], 335_104)
        self.assertEqual(result["tokens"]["output"], 1_881)
        self.assertEqual(result["diagnostics"]["content_seed_count"], 1)
        self.assertFalse(result["diagnostics"]["session_index_available"])
        self.assertIn("incomplete_task", result["events"][0]["quality"]["flags"])

    def test_multi_wi_codex_session_fails_closed_without_partition_evidence(self) -> None:
        session_id = "multi-wi-private"
        write_jsonl(
            self.sessions / f"rollout-{session_id}.jsonl",
            [
                meta(session_id),
                user_message("Continue wi_20260822_938 on this session."),
                user_message("Then inspect wi_20260820_136 in the same session."),
                *turn(
                    "ambiguous-turn",
                    input_tokens=100,
                    cached=80,
                    output=5,
                    timestamp="2026-08-23T00:00:00Z",
                ),
            ],
        )

        result = self.discover().to_dict(include_events=True)

        self.assertEqual(result["status"], "join_conflict")
        self.assertEqual(result["event_count"], 0)
        self.assertEqual(result["root_thread_count"], 0)
        self.assertEqual(result["diagnostics"]["ambiguous_seed_count"], 1)
        self.assertEqual(
            result["diagnostics"]["candidate_evidence"],
            {
                "status": "present_but_unpartitioned",
                "seed_count": 1,
                "component_count": 1,
                "assigned_event_count": 0,
            },
        )
        self.assertIn("multiple WorkItem", result["producer_attempts"][0]["reason"])
        self.assertNotIn(session_id, json.dumps(result))

    def test_short_and_compact_aliases_taint_multi_wi_codex_sessions(self) -> None:
        fixtures = (
            ("Continue WI938.", "Then inspect WI136 in this session."),
            ("Continue wi20260822938.", "Then inspect wi20260820136 here."),
        )
        for index, (target_seed, other_seed) in enumerate(fixtures):
            with self.subTest(target_seed=target_seed, other_seed=other_seed):
                session_id = f"alias-multi-wi-private-{index}"
                write_jsonl(
                    self.sessions / f"rollout-{session_id}.jsonl",
                    [
                        meta(session_id),
                        user_message(target_seed),
                        user_message(other_seed),
                        *turn(
                            f"alias-turn-{index}",
                            input_tokens=100,
                            cached=80,
                            output=5,
                            timestamp="2026-08-23T00:00:00Z",
                        ),
                    ],
                )

                result = self.discover().to_dict(include_events=True)

                self.assertEqual(result["status"], "join_conflict")
                self.assertEqual(result["event_count"], 0)
                self.assertEqual(result["diagnostics"]["ambiguous_seed_count"], 1)
                (self.sessions / f"rollout-{session_id}.jsonl").unlink()

    def test_short_only_seed_fails_closed_for_duplicate_suffix_inventory(self) -> None:
        other_work_item = "wi_20260823_938"
        (self.harness / "work-items" / f"{other_work_item}.yaml").write_text(
            f"work_item_id: {other_work_item}\n"
            "status: completed\n"
            "created_at: '2026-08-23T00:00:00Z'\n"
            "updated_at: '2026-08-25T00:00:00Z'\n",
            encoding="utf-8",
        )
        session_id = "duplicate-short-suffix-private"
        write_jsonl(
            self.sessions / f"rollout-{session_id}.jsonl",
            [
                meta(session_id),
                user_message("Continue WI938."),
                *turn(
                    "overlapping-turn",
                    input_tokens=100,
                    cached=80,
                    output=5,
                    timestamp="2026-08-24T00:00:00Z",
                ),
            ],
        )

        results = [
            auto_discover_wi_usage(
                work_item_id,
                machine="fixture-machine",
                observed_at="2026-08-26T00:00:00Z",
                harness_root=self.harness,
                codex_home=self.codex,
                model_catalog_path=self.catalog,
            ).to_dict(include_events=True)
            for work_item_id in ("wi_20260822_938", other_work_item)
        ]

        self.assertTrue(all(result["status"] == "join_conflict" for result in results))
        self.assertTrue(all(result["event_count"] == 0 for result in results))
        self.assertTrue(
            all(result["diagnostics"]["ambiguous_seed_count"] == 1 for result in results)
        )

    def test_full_identity_disambiguates_duplicate_short_suffix_once(self) -> None:
        other_work_item = "wi_20260823_938"
        (self.harness / "work-items" / f"{other_work_item}.yaml").write_text(
            f"work_item_id: {other_work_item}\n"
            "status: completed\n"
            "created_at: '2026-08-23T00:00:00Z'\n"
            "updated_at: '2026-08-25T00:00:00Z'\n",
            encoding="utf-8",
        )
        session_id = "explicit-short-disambiguation-private"
        write_jsonl(
            self.sessions / f"rollout-{session_id}.jsonl",
            [
                meta(session_id),
                user_message("Continue WI938 for wi_20260822_938."),
                *turn(
                    "disambiguated-turn",
                    input_tokens=100,
                    cached=80,
                    output=5,
                    timestamp="2026-08-24T00:00:00Z",
                ),
            ],
        )

        target = self.discover().to_dict(include_events=True)
        other = auto_discover_wi_usage(
            other_work_item,
            machine="fixture-machine",
            observed_at="2026-08-26T00:00:00Z",
            harness_root=self.harness,
            codex_home=self.codex,
            model_catalog_path=self.catalog,
        ).to_dict(include_events=True)

        self.assertEqual(target["status"], "observed")
        self.assertEqual(target["event_count"], 1)
        self.assertEqual(other["status"], "producer_missing")
        self.assertEqual(other["event_count"], 0)

    def test_index_target_and_rollout_other_wi_taint_the_same_session(self) -> None:
        session_id = "index-content-switch-private"
        write_jsonl(
            self.codex / "session_index.jsonl",
            [{"id": session_id, "thread_name": "Continue WI938"}],
        )
        write_jsonl(
            self.sessions / f"rollout-{session_id}.jsonl",
            [
                meta(session_id),
                user_message("Continue wi_20260820_136 instead."),
                *turn(
                    "switched-turn",
                    input_tokens=100,
                    cached=80,
                    output=5,
                    timestamp="2026-08-23T00:00:00Z",
                ),
            ],
        )

        result = self.discover().to_dict(include_events=True)

        self.assertEqual(result["status"], "join_conflict")
        self.assertEqual(result["event_count"], 0)
        self.assertEqual(result["diagnostics"]["ambiguous_seed_count"], 1)

    def test_ambiguous_child_taints_unique_parent_root_component(self) -> None:
        root_id = "unique-parent-private"
        child_id = "ambiguous-child-private"
        write_jsonl(
            self.codex / "session_index.jsonl",
            [{"id": root_id, "thread_name": "Continue wi_20260822_938"}],
        )
        write_jsonl(
            self.sessions / f"rollout-{root_id}.jsonl",
            [
                meta(root_id),
                *turn(
                    "root-turn",
                    input_tokens=100,
                    cached=80,
                    output=5,
                    timestamp="2026-08-23T00:00:00Z",
                ),
            ],
        )
        write_jsonl(
            self.sessions / f"rollout-{child_id}.jsonl",
            [
                child_meta(child_id, root_id),
                user_message("Continue WI938 and inspect WI136 in this child."),
                *turn(
                    "child-turn",
                    input_tokens=200,
                    cached=160,
                    output=10,
                    timestamp="2026-08-23T00:01:00Z",
                ),
            ],
        )

        result = self.discover().to_dict(include_events=True)

        self.assertEqual(result["status"], "join_conflict")
        self.assertEqual(result["event_count"], 0)
        self.assertEqual(result["root_thread_count"], 0)
        self.assertEqual(result["diagnostics"]["ambiguous_component_count"], 1)

    def test_mixed_unobserved_gaps_do_not_become_partial_zero_usage(self) -> None:
        session_id = "outside-window-private"
        write_jsonl(
            self.sessions / f"rollout-{session_id}.jsonl",
            [
                meta(session_id),
                user_message("Continue wi_20260822_938."),
                *turn(
                    "late-turn",
                    input_tokens=100,
                    cached=80,
                    output=5,
                    timestamp="2026-08-26T00:00:00Z",
                ),
            ],
        )
        cc = self.root / ".claude" / "projects"
        write_jsonl(cc / "unrelated.jsonl", [{"type": "user", "message": {"role": "user", "content": "unrelated"}}])

        result = auto_discover_wi_usage(
            "wi_20260822_938",
            machine="fixture-machine",
            observed_at="2026-08-26T00:00:00Z",
            harness_root=self.harness,
            codex_home=self.codex,
            cc_projects_dir=cc,
            model_catalog_path=self.catalog,
        ).to_dict()

        self.assertEqual(result["status"], "join_conflict")
        self.assertEqual(result["event_count"], 0)
        self.assertEqual(result["tokens"], "unavailable")

    def test_tool_output_wi_reference_is_not_a_user_prompt_seed(self) -> None:
        session_id = "tool-output-private"
        write_jsonl(
            self.sessions / f"rollout-{session_id}.jsonl",
            [
                meta(session_id),
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "output": "work_item_id=wi_20260822_938",
                    },
                },
                *turn(
                    "unrelated-turn",
                    input_tokens=999,
                    cached=0,
                    output=9,
                    timestamp="2026-08-23T00:00:00Z",
                ),
            ],
        )

        result = self.discover().to_dict()

        self.assertEqual(result["status"], "producer_missing")
        self.assertEqual(result["event_count"], 0)

    def test_claude_user_prompt_seed_joins_only_matching_session_usage(self) -> None:
        cc = self.root / ".claude" / "projects"
        session_id = "cc-root-private"
        write_jsonl(cc / f"{session_id}.jsonl", [
            {
                "type": "user",
                "timestamp": "2026-08-23T00:00:00Z",
                "sessionId": session_id,
                "isSidechain": False,
                "message": {
                    "role": "user",
                    "content": "Run bootstrap --work-item wi_20260822_938 exactly once.",
                },
            },
            {
                "type": "assistant",
                "uuid": "cc-response-private",
                "timestamp": "2026-08-23T00:00:01Z",
                "sessionId": session_id,
                "isSidechain": False,
                "cwd": "/private",
                "message": {
                    "id": "cc-message-private",
                    "role": "assistant",
                    "model": "gpt-fixture",
                    "usage": {
                        "input_tokens": 60,
                        "cache_read_input_tokens": 30,
                        "cache_creation_input_tokens": 10,
                        "output_tokens": 5,
                    },
                },
            },
        ])
        window = load_wi_window(self.harness, "wi_20260822_938")

        result = discover_cc_wi_usage(
            work_item_id="wi_20260822_938",
            window=window,
            machine="fixture-machine",
            observed_at="2026-08-26T00:00:00Z",
            cc_projects_dir=cc,
            model_catalog_path=self.catalog,
            work_item_ids=frozenset({"wi_20260822_938"}),
        ).to_dict(include_events=True)

        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["tokens"]["input"], 100)
        self.assertEqual(result["tokens"]["cache_read"], 30)
        self.assertEqual(result["tokens"]["cache_write"], 10)
        self.assertEqual(result["tokens"]["output"], 5)
        self.assertEqual(result["producer_attempts"][0]["producer"], "claude_code")
        self.assertNotIn(session_id, json.dumps(result))

    def test_multi_wi_claude_session_fails_closed_without_partition_evidence(self) -> None:
        cc = self.root / ".claude" / "projects"
        session_id = "cc-multi-wi-private"
        write_jsonl(cc / f"{session_id}.jsonl", [
            {
                "type": "user",
                "timestamp": "2026-08-23T00:00:00Z",
                "sessionId": session_id,
                "isSidechain": False,
                "message": {"role": "user", "content": "Continue wi_20260822_938."},
            },
            {
                "type": "user",
                "timestamp": "2026-08-23T00:00:01Z",
                "sessionId": session_id,
                "isSidechain": False,
                "message": {"role": "user", "content": "Also handle wi_20260820_136 here."},
            },
            {
                "type": "assistant",
                "uuid": "cc-response-private",
                "timestamp": "2026-08-23T00:00:02Z",
                "sessionId": session_id,
                "isSidechain": False,
                "cwd": "/private",
                "message": {
                    "id": "cc-message-private",
                    "role": "assistant",
                    "model": "gpt-fixture",
                    "usage": {
                        "input_tokens": 60,
                        "cache_read_input_tokens": 30,
                        "cache_creation_input_tokens": 10,
                        "output_tokens": 5,
                    },
                },
            },
        ])
        window = load_wi_window(self.harness, "wi_20260822_938")

        result = discover_cc_wi_usage(
            work_item_id="wi_20260822_938",
            window=window,
            machine="fixture-machine",
            observed_at="2026-08-26T00:00:00Z",
            cc_projects_dir=cc,
            model_catalog_path=self.catalog,
            work_item_ids=frozenset({"wi_20260822_938"}),
        ).to_dict(include_events=True)

        self.assertEqual(result["status"], "join_conflict")
        self.assertEqual(result["event_count"], 0)
        self.assertEqual(result["diagnostics"]["ambiguous_seed_count"], 1)
        self.assertNotIn(session_id, json.dumps(result))

    def test_pi_procurement_maps_claude_subscription_without_settlement(self) -> None:
        self.assertEqual(_pi_source("chatgpt_subscription"), "chatgpt_pro")
        self.assertEqual(_pi_source("openrouter_api"), "openrouter_api")
        self.assertEqual(_pi_source("claude_subscription"), "claude_subscription")
        self.assertEqual(_pi_source("cursor_subscription"), "unknown")
        self.assertEqual(_pi_source(None), "unknown")

    def test_existing_codex_without_wi_seed_is_typed_missing(self) -> None:
        write_jsonl(self.codex / "session_index.jsonl", [{"id": "other", "thread_name": "unrelated"}])
        write_jsonl(self.sessions / "rollout-other.jsonl", [meta("other")])
        result = self.discover().to_dict()
        self.assertEqual(result["status"], "producer_missing")
        self.assertEqual(result["producer_attempts"][0]["status"], "producer_missing")
        pi_attempt = next(
            attempt for attempt in result["producer_attempts"]
            if attempt["producer"] == "pi_managed_scl"
        )
        self.assertEqual(pi_attempt["status"], "not_applicable")
        self.assertIn("unmanaged Pi is not measured", pi_attempt["reason"])
        self.assertNotEqual(result["producer_attempts"][0]["reason"], "unavailable")


if __name__ == "__main__":
    unittest.main()
