from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_meter import tail  # noqa: E402
from pulse_compute_cost.receipts import reference  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def write(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def user(prompt, content="hi", **extra):
    return {"type": "user", "sessionId": SID, "promptId": prompt, "timestamp": "2026-11-20T01:00:00Z",
            "message": {"role": "user", "content": content}, **extra}


def assistant(mid, *, usage=None, model="claude-opus-5-5", content=None, stop="end_turn", **extra):
    return {"type": "assistant", "sessionId": SID, "requestId": "req_" + mid, "uuid": "u" + mid,
            "timestamp": "2026-11-20T01:00:01Z", "cwd": "/w",
            "message": {"id": mid, "model": model, "stop_reason": stop, "content": content or [],
                        "usage": usage or {"input_tokens": 2, "cache_read_input_tokens": 30,
                                           "cache_creation_input_tokens": 40, "output_tokens": 5}}, **extra}


def declare(tool_id, wi):
    return [{"type": "tool_use", "id": tool_id, "name": "mcp__pulse-request-purpose__declare_session_wi",
             "input": {"wi": wi}}]


def result(tool_id, prompt, error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
    if error:
        block["is_error"] = True
    return user(prompt, [block])


def signals(db):
    """session_entrypoints 是信号集合：(会话, 入口, 出处)。"""
    conn = sqlite3.connect(db)
    try:
        return set(conn.execute("SELECT session_ref, entrypoint, source FROM session_entrypoints"))
    finally:
        conn.close()


def final(db):
    """与 views.sql 同口径：每个会话取 (entrypoint, source) 最小的信号。"""
    picked = {}
    for ref, entrypoint, source in sorted(signals(db), key=lambda r: (r[0], r[1], r[2])):
        picked.setdefault(ref, entrypoint)
    return picked


class CcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.db = Path(self.tmp.name) / "state" / "node.sqlite"
        self.registry = {"sources": [{"kind": "cc", "node": "n1", "path": str(self.root)}]}

    def tearDown(self):
        self.tmp.cleanup()

    def run_tail(self):
        out = tail.run(self.db, self.registry, since=None)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return out, conn

    def test_same_message_counted_once_and_synthetic_skipped(self):
        write(self.root / "p" / f"{SID}.jsonl", [
            user("t1"), assistant("m1", stop=None), assistant("m1"),
            assistant("m2", model="<synthetic>")])
        _, conn = self.run_tail()
        rows = conn.execute("SELECT * FROM usage_events").fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["input_uncached"], row["cache_read"], row["cache_write"], row["output"]), (2, 30, 40, 5))
        self.assertEqual((row["final"], row["turn_id"], row["request_id"], row["agent_id"]), (1, "t1", "req_m1", ""))
        self.assertEqual(row["session_ref"], reference("native_session", ["cc", SID]))

    def test_turn_switch(self):
        write(self.root / "p" / f"{SID}.jsonl", [
            user("t1"), assistant("m1"), user("t2", isMeta=True, origin={"kind": "coordinator"}), assistant("m2")])
        _, conn = self.run_tail()
        turns = dict(conn.execute("SELECT event_id, turn_id FROM usage_events").fetchall())
        self.assertEqual(turns, {"m1": "t1", "m2": "t2"})

    def test_declaration_pending_then_accepted_or_rejected(self):
        path = self.root / "p" / f"{SID}.jsonl"
        write(path, [user("t1"), assistant("m1", content=declare("toolu_a", "wi_20261121_413")),
                     assistant("m2", content=declare("toolu_b", "none"))])
        _, conn = self.run_tail()
        self.assertEqual({tuple(r) for r in conn.execute("SELECT claim_id, status FROM wi_claims")},
                         {("toolu_a", "pending"), ("toolu_b", "pending")})
        with path.open("a") as handle:
            handle.write(json.dumps(result("toolu_a", "t1")) + "\n" + json.dumps(result("toolu_b", "t1", True)) + "\n")
        self.run_tail()
        rows = {r["claim_id"]: (r["status"], r["wi"], r["turn_id"], r["scope"])
                for r in conn.execute("SELECT * FROM wi_claims")}
        self.assertEqual(rows, {"toolu_a": ("accepted", "wi_20261121_413", "t1", "turn"),
                                "toolu_b": ("rejected", "none", "t1", "turn")})

    def test_subagent_marker_and_inherited_turn(self):
        write(self.root / "p" / f"{SID}.jsonl", [user("t1"), assistant("m1")])
        sub = self.root / "p" / SID / "subagents" / "agent-a1.jsonl"
        write(sub, [
            {**user("t1", "[WI: wi_20261121_413]\n\nwork"), "parentUuid": None, "isSidechain": True, "agentId": "a1"},
            assistant("m2", isSidechain=True, agentId="a1")])
        plain = self.root / "p" / SID / "subagents" / "agent-a2.jsonl"
        write(plain, [{**user("t1", "no marker"), "parentUuid": None, "isSidechain": True, "agentId": "a2"},
                      assistant("m3", isSidechain=True, agentId="a2")])
        _, conn = self.run_tail()
        claims = conn.execute("SELECT * FROM wi_claims").fetchall()
        self.assertEqual(len(claims), 1)
        self.assertEqual((claims[0]["scope"], claims[0]["agent_id"], claims[0]["wi"], claims[0]["status"],
                          claims[0]["claim_id"]), ("subagent", "a1", "wi_20261121_413", "accepted", f"marker:{SID}:a1"))
        rows = {r["event_id"]: (r["agent_id"], r["turn_id"], r["session_id"])
                for r in conn.execute("SELECT * FROM usage_events")}
        self.assertEqual(rows, {"m1": ("", "t1", SID), "m2": ("a1", "t1", SID), "m3": ("a2", "t1", SID)})

    def test_unknown_tokens_kept_as_null(self):
        write(self.root / "p" / f"{SID}.jsonl", [user("t1"), assistant("m1", usage={"output_tokens": 3})])
        _, conn = self.run_tail()
        row = conn.execute("SELECT input_uncached, cache_read, output, cwd FROM usage_events").fetchone()
        self.assertEqual(tuple(row), (None, None, 3, "/w"))

    def test_turn_origins(self):
        write(self.root / "p" / f"{SID}.jsonl", [
            user("t1", "修一下"), assistant("m1"), user("t1", [{"type": "tool_result", "tool_use_id": "x"}]),
            user("t2", "<task-notification>done</task-notification>"),
            user("t3", "继续", isMeta=True, origin={"kind": "coordinator"}),
            user("t4", "[系统投递] 定时")])
        write(self.root / "p" / SID / "subagents" / "agent-a1.jsonl", [
            {**user("t1", "[WI: wi_20261121_413]"), "parentUuid": None, "isSidechain": True, "agentId": "a1"},
            {**user("t9", "主代理追加", isMeta=True, origin={"kind": "coordinator"}), "isSidechain": True, "agentId": "a1"}])
        _, conn = self.run_tail()
        got = {r["turn_id"]: r["origin"] for r in conn.execute("SELECT * FROM turn_origins")}
        self.assertEqual(got, {"t1": "human", "t2": "continuation", "t3": "continuation", "t4": "delivery",
                               "t9": "continuation"})

    def test_session_entrypoint_from_native_field(self):
        other = "22222222-2222-3333-4444-555555555555"
        write(self.root / "p" / f"{SID}.jsonl", [{**user("t1"), "entrypoint": "cli", "userType": "external"},
                                                 {**assistant("m1"), "entrypoint": "cli"}])
        write(self.root / "p" / f"{other}.jsonl", [{**user("t1"), "sessionId": other, "entrypoint": "sdk-cli"}])
        write(self.root / "p" / SID / "subagents" / "agent-a1.jsonl", [
            {**user("t1", "x"), "parentUuid": None, "isSidechain": True, "agentId": "a1", "entrypoint": "cli"}])
        _, conn = self.run_tail()
        got = {r["session_ref"]: (r["entrypoint"], r["source"]) for r in conn.execute("SELECT * FROM session_entrypoints")}
        self.assertEqual(got, {reference("native_session", ["cc", SID]): ("cli", "cc_entrypoint"),
                               reference("native_session", ["cc", other]): ("sdk", "cc_entrypoint")})

    def test_entrypoint_only_line_appended_later_merges_like_one_pass(self):
        meta_line = {"type": "system", "sessionId": SID, "timestamp": "2026-11-20T01:00:02Z", "entrypoint": "cli"}
        rows = [{**user("t1"), "entrypoint": "sdk-cli"}, assistant("m1"), meta_line]
        path = self.root / "p" / f"{SID}.jsonl"
        write(path, rows[:2])
        self.run_tail()
        with path.open("a") as handle:                    # 只带入口、没有 promptId/usage 的元数据行
            handle.write(json.dumps(meta_line) + "\n")
        self.run_tail()
        one_pass_db = Path(self.tmp.name) / "once" / "node.sqlite"
        tail.run(one_pass_db, self.registry, since=None)
        self.assertEqual(signals(self.db), signals(one_pass_db))
        self.assertEqual(final(self.db), {reference("native_session", ["cc", SID]): "cli"})

    def test_nested_entrypoint_is_ignored(self):
        nested = {**user("t1", [{"type": "tool_result", "tool_use_id": "x",
                                 "content": json.dumps({"entrypoint": "cli"})}]), "entrypoint": "sdk-cli"}
        inner = {"type": "attachment", "sessionId": SID, "attachment": {"entrypoint": "cli"}}   # 只有嵌套、没有顶层字段
        write(self.root / "p" / f"{SID}.jsonl", [nested, inner])
        self.run_tail()
        self.assertEqual(signals(self.db), {(reference("native_session", ["cc", SID]), "sdk", "cc_entrypoint")})

    def test_entrypoint_signals_follow_current_sources(self):
        ref = reference("native_session", ["cc", SID])
        main = self.root / "p" / f"{SID}.jsonl"
        sub = self.root / "p" / SID / "subagents" / "agent-a1.jsonl"
        write(main, [{**user("t1"), "entrypoint": "cli"}])
        self.run_tail()
        self.assertEqual(final(self.db), {ref: "cli"})
        write(main, [{**user("t1"), "entrypoint": "sdk-cli"}, {**user("t2"), "entrypoint": "sdk-cli"}])   # 唯一来源改了
        self.run_tail()
        self.assertEqual(final(self.db), {ref: "sdk"})                                 # cli 信号撤销
        write(sub, [{**user("t1", "x"), "parentUuid": None, "isSidechain": True, "agentId": "a1", "entrypoint": "cli"}])
        self.run_tail()
        self.assertEqual(final(self.db), {ref: "cli"})
        write(sub, [{**user("t1", "x"), "parentUuid": None, "isSidechain": True, "agentId": "a1",
                     "entrypoint": "sdk-cli"}, {**user("t1", "y"), "isSidechain": True, "agentId": "a1"}])
        self.run_tail()
        self.assertEqual(final(self.db), {ref: "sdk"})
        clean = Path(self.tmp.name) / "clean" / "node.sqlite"
        tail.run(clean, self.registry, since=None)
        self.assertEqual(signals(self.db), signals(clean))

    def test_interrupted_reread_restart_equals_clean_rebuild(self):
        from unittest.mock import patch
        main = self.root / "p" / f"{SID}.jsonl"
        write(main, [{**user("t1"), "entrypoint": "cli"}, assistant("m1")])
        self.run_tail()
        write(main, [{**user("t1"), "entrypoint": "sdk-cli"}, assistant("m1"), assistant("m2")])   # 重写触发重读

        def crash(*args, **kwargs):
            raise RuntimeError("killed mid reread")
        with patch.object(tail, "_finish_reread", crash):
            with self.assertRaises(RuntimeError):
                self.run_tail()
        with patch.object(tail, "_finish_reread", crash):               # 再启动又中断一次
            with self.assertRaises(RuntimeError):
                self.run_tail()
        self.run_tail()
        clean = Path(self.tmp.name) / "clean" / "node.sqlite"
        tail.run(clean, self.registry, since=None)
        self.assertEqual(signals(self.db), signals(clean))
        self.assertEqual(final(self.db), {reference("native_session", ["cc", SID]): "sdk"})

    def test_entrypoint_order_independent(self):
        ref = reference("native_session", ["cc", SID])
        rows = [{**user("t1"), "entrypoint": "sdk-cli"}, {**user("t2"), "entrypoint": "cli"}]
        results = []
        for i, order in enumerate((rows, rows[::-1])):
            root = Path(self.tmp.name) / f"o{i}" / "projects"
            write(root / "p" / f"{SID}.jsonl", order)
            db = Path(self.tmp.name) / f"o{i}" / "node.sqlite"
            tail.run(db, {"sources": [{"kind": "cc", "node": "n1", "path": str(root)}]}, since=None)
            results.append(signals(db))
            self.assertEqual(final(db), {ref: "cli"})
        self.assertEqual(results[0], results[1])

if __name__ == "__main__":
    unittest.main()
