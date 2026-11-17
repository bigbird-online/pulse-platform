"""One late declaration owns its native session, not only the declaring turn."""

from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_scl_session_attribution as f
from pulse_compute_cost import mcp_request_purpose as m
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.receipts import reference

TURN1 = "00000000-0000-4000-8000-000000000021"
TURN2 = "00000000-0000-4000-8000-000000000022"
TURN3 = "00000000-0000-4000-8000-000000000023"
CALL_AT = "2026-01-01T01:02:30Z"


def metadata(thread=f.THREAD, turn=TURN2, root=None):
    return {
        "sessionId": root or thread,
        "threadId": thread,
        "callId": "call-1",
        "itemId": "item-1",
        "x-codex-turn-metadata": {
            "session_id": root or thread,
            "thread_id": thread,
            "turn_id": turn,
            "turn_started_at_unix_ms": 1767229320000,
        },
    }


def call(wi=f.WI, meta=None):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "declare_session_wi",
            "arguments": {"wi": wi},
            "_meta": metadata() if meta is None else meta,
        },
    }


class SessionWiTests(unittest.TestCase):
    query = f.SessionAttributionTests.query
    native = f.SessionAttributionTests.native
    bind = f.SessionAttributionTests.bind

    def setUp(self):
        f.SessionAttributionTests.setUp(self)
        for table in (
            "source_attachments",
            "source_attachment_history",
            "measurement_epochs",
            "checkpoints",
            "work_items",
        ):
            self.db.execute("DELETE FROM " + table)
        self.db.commit()
        self.source["session_wi_mcp_since"] = f.BEFORE
        self.path = next(self.logs.glob("*.jsonl"))
        self.header = json.loads(self.path.read_text().splitlines()[0])
        self.turns = [self.turn(TURN1, 0, 1), self.turn(TURN2, 2, 2)]
        self.save()

    def turn(self, turn, minute, multiple):
        at = f"2026-01-01T01:{minute:02}:00Z"
        end = f"2026-01-01T01:{minute + 1:02}:00Z"
        counts = {
            "input_tokens": 120,
            "cached_input_tokens": 20,
            "cache_write_input_tokens": 0,
            "output_tokens": 10,
        }
        return [
            {
                "type": "event_msg",
                "timestamp": at,
                "payload": {"type": "task_started", "turn_id": turn},
            },
            {
                "type": "turn_context",
                "timestamp": at,
                "payload": {"model": "openai/test"},
            },
            {
                "type": "event_msg",
                "timestamp": end,
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": counts,
                        "total_token_usage": {
                            k: v * multiple for k, v in counts.items()
                        },
                    },
                },
            },
            {
                "type": "event_msg",
                "timestamp": end,
                "payload": {"type": "task_complete", "turn_id": turn},
            },
        ]

    def completion(self, wi=f.WI, item="item-1", turn=TURN2, thread=f.THREAD, at=CALL_AT):
        result = m.handle(call(wi, metadata(thread, turn)), clock=lambda: at)
        self.assertFalse(result["isError"], result)
        stamp = int(datetime.fromisoformat(at).timestamp() * 1000)
        return {
            "type": "event_msg",
            "timestamp": at,
            "payload": {
                "type": "item_completed",
                "thread_id": thread,
                "turn_id": turn,
                "started_at_ms": stamp,
                "completed_at_ms": stamp,
                "item": {
                    "type": "McpToolCall",
                    "id": item,
                    "server": "pulse-request-purpose",
                    "tool": "declare_session_wi",
                    "arguments": {"wi": wi},
                    "status": "completed",
                    "result": result,
                },
            },
        }

    def save(self):
        self.path.write_text(
            "".join(json.dumps(r) + "\n" for r in [self.header, *sum(self.turns, [])])
        )

    def collect(self):
        collect_registered_sources(self.registry, self.ledger)
        return self.query()

    def declare(self, **kwargs):
        self.turns[1].insert(2, self.completion(**kwargs))
        self.save()

    def test_prompt_and_tool_accept_only_session_wi(self):
        tools = m.handle({"method": "tools/list"}, clock=lambda: CALL_AT)["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "declare_session_wi")
        self.assertEqual(set(tools[0]["inputSchema"]["properties"]), {"wi"})
        self.assertEqual(tools[0]["description"], m.DESCRIPTION)
        self.assertIn("none", m.DESCRIPTION)
        self.assertEqual(tools[0]["inputSchema"]["properties"]["wi"]["pattern"],
                         "^(?:wi_[0-9]{8}_[0-9]{3}|none)$")

    def test_late_one_call_covers_prior_and_future_turns(self):
        before = self.collect()
        mark = self.ledger.receipt_snapshot()["watermark"]
        self.assertEqual(before["summary"]["usage"]["wi_unassigned_event_count"], 2)
        before = self.query(watermark=mark)
        self.declare()
        middle = self.collect()
        self.assertEqual(middle["summary"]["usage"]["wi_unassigned_event_count"], 0)
        self.assertEqual(middle["groups"][0]["key"]["wi"], f.WI)
        self.assertEqual(
            middle["summary"]["usage"]["tokens"], before["summary"]["usage"]["tokens"]
        )
        self.assertEqual(
            middle["summary"]["cost"]["partial_machine_cash_usd"],
            before["summary"]["cost"]["partial_machine_cash_usd"],
        )
        self.turns.append(self.turn(TURN3, 4, 3))
        self.save()
        after = self.collect()
        self.assertEqual(after["summary"]["usage"]["event_count"], 3)
        self.assertEqual(after["summary"]["usage"]["wi_unassigned_event_count"], 0)
        self.assertEqual(after["summary"]["usage"]["known_total_tokens"], 390)
        self.assertEqual(self.query(watermark=mark), before)

    def test_unknown_session_does_not_call_or_become_none(self):
        q = self.collect()
        self.assertEqual(q["summary"]["usage"]["wi_unassigned_event_count"], 2)
        self.assertNotIn("none", [g["key"]["wi"] for g in q["groups"]])

    def test_replay_and_identical_second_call_are_idempotent(self):
        self.declare()
        self.collect()
        mark = self.ledger.receipt_snapshot()["watermark"]
        self.collect()
        self.assertEqual(self.ledger.receipt_snapshot()["watermark"], mark)
        before = [
            r.fact_ref
            for r in self.ledger.receipt_snapshot()["facts"]
            if r.kind == "usage" or r.producer == m.POLICY
        ]
        self.declare(item="item-2")
        self.collect()
        after = [
            r.fact_ref
            for r in self.ledger.receipt_snapshot()["facts"]
            if r.kind == "usage" or r.producer == m.POLICY
        ]
        self.assertEqual(
            after, before
        )  # source coverage may version its new file digest

    def test_contrary_session_wi_remains_conflict(self):
        self.declare()
        self.collect()
        mark = self.ledger.receipt_snapshot()["watermark"]
        old = self.query(watermark=mark)
        self.declare(wi=f.OTHER_WI, item="item-2")
        after = self.collect()
        self.assertEqual(after["groups"][0]["key"]["wi"], "ambiguous")
        self.assertEqual(after["summary"]["usage"]["known_total_tokens"], 260)
        self.assertEqual(self.query(watermark=mark), old)

    def test_concurrent_session_does_not_inherit(self):
        self.declare()
        self.native(f.SECOND, f.START, f.END)
        q = self.collect()
        self.assertEqual(q["summary"]["usage"]["wi_unassigned_event_count"], 1)

    def test_multiple_inputs_do_not_require_repeat_session_declaration(self):
        self.declare()
        for i in range(2):
            self.turns[1].insert(
                2,
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T01:02:05Z",
                    "payload": {
                        "type": "item_completed",
                        "thread_id": f.THREAD,
                        "turn_id": TURN2,
                        "item": {
                            "type": "UserMessage",
                            "id": f"input-{i}",
                            "content": [{"text": "same"}],
                        },
                    },
                },
            )
        self.save()
        q = self.collect()
        self.assertEqual(q["summary"]["usage"]["wi_unassigned_event_count"], 0)

    def test_missing_or_failed_call_does_not_assign(self):
        self.declare()
        self.turns[1][2]["payload"]["item"]["status"] = "failed"
        self.save()
        self.assertEqual(
            self.collect()["summary"]["usage"]["wi_unassigned_event_count"], 2
        )

    def test_agent_identity_and_null_arguments_are_rejected(self):
        for args in (
            {"wi": f.WI, "session": f.THREAD},
            {"wi": None},
            {"wi": "None"},
            {"work_kind": "wi", "wi": f.WI},
        ):
            request = call()
            request["params"]["arguments"] = args
            self.assertTrue(m.handle(request, clock=lambda: CALL_AT)["isError"])

    def test_wrong_argument_name_is_refused_with_the_accepted_shape(self):
        request = call()
        request["params"]["arguments"] = {"wi_id": f.WI}
        value = m.handle(request, clock=lambda: CALL_AT)
        self.assertTrue(value["isError"])
        self.assertEqual(value["structuredContent"]["retry_arguments"], {"wi": f.WI})
        request["params"]["arguments"] = {"wi": "wi_1"}
        value = m.handle(request, clock=lambda: CALL_AT)["structuredContent"]
        self.assertEqual(value["retry_arguments"], {"wi": "wi_YYYYMMDD_NNN"})

    def test_a_none_declaration_is_non_wi_work_not_unassigned(self):
        self.declare(wi="none")
        usage = self.collect()["summary"]["usage"]
        self.assertEqual(usage["unassigned_event_count"], 0)
        self.assertEqual(usage["no_wi_event_count"], 2)
        self.assertEqual(usage["wi_unassigned_event_count"], 0)

    def test_session_none_then_wi_splits_usage_at_the_wi_declaration(self):
        # As CC and Pi: usage before the first WI declaration stays none.
        self.turns[0].insert(2, self.completion(wi="none", item="item-0", turn=TURN1, at="2026-01-01T01:00:30Z"))
        self.declare()
        self.turns.append(self.turn(TURN3, 4, 3))
        self.save()
        usage = self.collect()["summary"]["usage"]
        self.assertEqual((usage["event_count"], usage["no_wi_event_count"], usage["unassigned_event_count"]), (3, 2, 0))
        wis = {g["key"]["wi"] for g in self.query()["groups"]}
        self.assertIn(f.WI, wis)

    def test_same_time_none_and_wi_in_a_session_take_the_wi(self):
        self.declare(wi="none", item="item-0")
        self.declare()
        middle = self.collect()
        self.assertEqual(middle["summary"]["usage"]["no_wi_event_count"], 0)
        self.assertEqual(middle["groups"][0]["key"]["wi"], f.WI)

    def test_tool_has_no_filesystem_or_ledger_write(self):
        with (
            patch("builtins.open", side_effect=AssertionError("tool opened file")),
            patch.object(Path, "open", side_effect=AssertionError("tool opened file")),
        ):
            a = m.handle(call(), clock=lambda: CALL_AT)["structuredContent"]
            b = m.handle(call(meta=metadata(turn=TURN3)), clock=lambda: CALL_AT)[
                "structuredContent"
            ]
        self.assertEqual(a, b)
        self.assertNotIn("target_ref", a)
        self.assertEqual(
            a["native_scope_ref"], reference("native_session", ["codex", f.THREAD])
        )

    def test_real_stdio_roundtrip_is_session_scoped(self):
        script = (
            Path(__file__).resolve().parents[1] / "runtime/codex-mcp-request-purpose.py"
        )
        data = "".join(
            json.dumps(c) + "\n" for c in [call(), call(meta=metadata(turn=TURN3))]
        )
        result = subprocess.run(
            [sys.executable, str(script)],
            input=data,
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )
        rows = [json.loads(line)["result"] for line in result.stdout.splitlines()]
        self.assertFalse(rows[0]["isError"])
        self.assertEqual(rows[0], rows[1])

    def test_transport_host_conflict_is_rejected(self):
        bad = metadata()
        bad["threadId"] = f.SECOND
        self.assertTrue(m.handle(call(meta=bad), clock=lambda: CALL_AT)["isError"])

    def test_native_owner_and_result_mismatch_are_rejected(self):
        for field in ("thread_id", "result"):
            with self.subTest(field=field):
                self.turns = [self.turn(TURN1, 0, 1), self.turn(TURN2, 2, 2)]
                self.declare()
                event = self.turns[1][2]["payload"]
                if field == "thread_id":
                    event[field] = f.SECOND
                else:
                    event["item"][field]["structuredContent"]["wi"] = f.OTHER_WI
                self.save()
                self.assertEqual(
                    self.collect()["summary"]["usage"]["wi_unassigned_event_count"], 2
                )

    def test_source_not_enabled_does_not_accept_candidate(self):
        self.declare()
        del self.source["session_wi_mcp_since"]
        self.assertEqual(
            self.collect()["summary"]["usage"]["wi_unassigned_event_count"], 2
        )

    def test_old_request_tool_is_not_session_evidence(self):
        self.declare()
        self.turns[1][2]["payload"]["item"]["tool"] = "declare_request_purpose"
        self.save()
        self.assertEqual(
            self.collect()["summary"]["usage"]["wi_unassigned_event_count"], 2
        )

    def test_independent_contrary_assignment_is_preserved(self):
        self.bind(f.OTHER_WI, f.THREAD, f.BEFORE)
        self.declare()
        q = self.collect()
        self.assertEqual(q["groups"][0]["key"]["wi"], "ambiguous")

    def test_later_cutoff_does_not_admit_older_declaration(self):
        self.declare()
        self.source["session_wi_mcp_since"] = f.AFTER
        self.assertEqual(
            self.collect()["summary"]["usage"]["wi_unassigned_event_count"], 2
        )

    def test_disappearing_declaration_retracts_without_changing_old_watermark(self):
        self.declare()
        self.collect()
        mark = self.ledger.receipt_snapshot()["watermark"]
        old = self.query(watermark=mark)
        self.turns[1] = self.turn(TURN2, 2, 2)
        self.save()
        q = self.collect()
        self.assertEqual(q["summary"]["usage"]["wi_unassigned_event_count"], 2)
        self.assertEqual(
            q["summary"]["usage"]["tokens"], old["summary"]["usage"]["tokens"]
        )
        self.assertEqual(self.query(watermark=mark), old)

    def test_parent_root_metadata_does_not_relabel_current_child_session(self):
        value = m.handle(
            call(meta=metadata(thread=f.SECOND, root=f.THREAD)), clock=lambda: CALL_AT
        )["structuredContent"]
        self.assertEqual(
            value["native_scope_ref"], reference("native_session", ["codex", f.SECOND])
        )

    def test_native_cli_metadata_without_desktop_aliases(self):
        meta = metadata()
        del meta["sessionId"]
        del meta["threadId"]
        value = m.handle(call(meta=meta), clock=lambda: CALL_AT)
        self.assertFalse(value["isError"])

    def test_new_ledger_reader_recovers_once_registered_session(self):
        from pulse_compute_cost.ledger import UsageLedger

        self.declare()
        self.collect()
        self.ledger = UsageLedger(self.ledger.root)
        self.turns.append(self.turn(TURN3, 4, 3))
        self.save()
        self.assertEqual(
            self.collect()["summary"]["usage"]["wi_unassigned_event_count"], 0
        )

    def test_initialization_exposes_the_same_once_per_session_rule(self):
        result = m.handle({"method": "initialize"}, clock=lambda: CALL_AT)
        self.assertEqual(result["instructions"], f"使用 {m.TOOL}：{m.DESCRIPTION}")

    def test_session_declaration_resolves_weak_mid_turn_binding(self):
        self.bind(f.WI, f.THREAD, "2026-01-01T01:02:10Z")
        self.collect()
        mark = self.ledger.receipt_snapshot()["watermark"]
        before = self.query(watermark=mark)
        self.declare()
        after = self.collect()
        self.assertEqual(after["summary"]["usage"]["wi_unassigned_event_count"], 0)
        self.assertEqual(after["groups"][0]["key"]["wi"], f.WI)
        self.assertEqual(after["summary"]["usage"]["known_total_tokens"], 260)
        self.assertEqual(self.query(watermark=mark), before)
        self.turns[1] = self.turn(TURN2, 2, 2)
        self.save()
        withdrawn = self.collect()
        self.assertIn("ambiguous", [g["key"]["wi"] for g in withdrawn["groups"]])
        self.assertEqual(self.query(watermark=mark), before)

    def test_no_admitted_call_does_not_report_unrelated_thread_history(self):
        from types import SimpleNamespace

        usage = SimpleNamespace(
            kind="usage",
            payload={
                "runtime": "codex",
                "native_scope_ref": reference("native_session", ["codex", f.THREAD]),
            },
        )
        self.turns[1].insert(1, self.header)
        self.save()
        diagnostics = []
        found = m.native_completions([self.path], [usage], diagnostics, since=f.BEFORE)
        self.assertEqual(diagnostics, [])
        self.assertEqual(found[usage.payload["native_scope_ref"]], [])

    def test_pre_cutover_call_does_not_admit_invalid_historical_thread(self):
        from types import SimpleNamespace

        usage = SimpleNamespace(
            kind="usage",
            payload={
                "runtime": "codex",
                "native_scope_ref": reference("native_session", ["codex", f.THREAD]),
            },
        )
        self.declare()
        self.turns[1].insert(1, self.header)
        self.save()
        diagnostics = []
        found = m.native_completions([self.path], [usage], diagnostics, since=f.AFTER)
        self.assertEqual(diagnostics, [])
        self.assertEqual(found[usage.payload["native_scope_ref"]], [])

    def test_admitted_call_still_rejects_invalid_thread_history(self):
        from types import SimpleNamespace

        usage = SimpleNamespace(
            kind="usage",
            payload={
                "runtime": "codex",
                "native_scope_ref": reference("native_session", ["codex", f.THREAD]),
            },
        )
        self.declare()
        self.turns[1].insert(1, self.header)
        self.save()
        diagnostics = []
        found = m.native_completions([self.path], [usage], diagnostics, since=f.BEFORE)
        self.assertIn("native_mcp_thread_identity_conflict", diagnostics)
        self.assertEqual(found[usage.payload["native_scope_ref"]], [])

    def test_bad_duplicate_copy_cannot_hide_behind_no_call_prefilter(self):
        from types import SimpleNamespace

        usage = SimpleNamespace(
            kind="usage",
            payload={
                "runtime": "codex",
                "native_scope_ref": reference("native_session", ["codex", f.THREAD]),
            },
        )
        self.declare()
        archive = self.root / "archive"
        archive.mkdir()
        copied = archive / self.path.name
        copied.write_text(
            json.dumps(self.header)
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": f.END,
                    "payload": {"type": "task_complete", "turn_id": TURN1},
                }
            )
            + "\n"
        )
        diagnostics = []
        found = m.native_completions(
            [self.path, copied], [usage], diagnostics, since=f.BEFORE
        )
        self.assertIn("native_mcp_thread_identity_conflict", diagnostics)
        self.assertEqual(found[usage.payload["native_scope_ref"]], [])
