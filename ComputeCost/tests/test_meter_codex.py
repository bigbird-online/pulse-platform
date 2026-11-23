from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_meter import tail  # noqa: E402

CHILD = "01a1ee21-0e14-78e3-acc1-14774c564523"
PARENT = "01a1ee20-1611-7493-9116-2bb07195278d"
TS = "2026-11-20T01:00:00.000Z"


def meta(sid, parent=None, *, spawn=True):
    payload = {"id": sid, "cwd": "/w", "forked_from_id": parent, "parent_thread_id": parent if spawn else None}
    if parent and spawn:
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}}
    return {"timestamp": TS, "type": "session_meta", "payload": payload}


def started(tid, trigger="exec", parent_turn=None):
    return {"timestamp": TS, "type": "event_msg", "payload": {"type": "task_started", "turn_id": tid,
            "turn_attribution": {"turn_id": tid, "turn_trigger": trigger, "parent_turn_id": parent_turn}}}


def user_msg(text):
    return {"timestamp": TS, "type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}}


def turn(tid):
    return {"timestamp": TS, "type": "event_msg", "payload": {"type": "task_started", "turn_id": tid}}


def context(model="gpt-6-sol"):
    return {"timestamp": TS, "type": "turn_context", "payload": {"model": model, "cwd": "/w/sub"}}


def tokens(inp, cached, out, *, last=None, cache_write=None):
    total = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}
    if cache_write is not None:
        total["cache_write_input_tokens"] = cache_write
    info = {"total_token_usage": total, "last_token_usage": last or {"input_tokens": 1, "cached_input_tokens": 0,
                                                                     "output_tokens": 1}}
    return {"timestamp": TS, "type": "event_msg", "payload": {"type": "token_count", "info": info}}


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"
        self.db = Path(self.tmp.name) / "state" / "node.sqlite"
        self.registry = {"sources": [{"kind": "codex", "node": "n1", "calendar_root": str(self.root)}]}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, sid, rows):
        path = self.root / "2026" / "11" / "20" / f"rollout-2026-11-20T01-00-00-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    def events(self):
        out = tail.run(self.db, self.registry, since=None)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM usage_events ORDER BY event_id")]
        return out, rows

    def test_cumulative_difference_and_cached_input_split(self):
        self.write(CHILD, [meta(CHILD), turn("t1"), context(), tokens(100, 60, 5),
                           tokens(250, 200, 9, cache_write=10)])
        _, rows = self.events()
        self.assertEqual(len(rows), 2)
        second, first = sorted(rows, key=lambda r: r["output"])
        self.assertEqual((first["input_uncached"], first["cache_read"], first["cache_write"], first["output"]),
                         (40, 60, 0, 5))
        # 第二次：Δinput 150，Δcached 140，Δcache_write 10 → 未缓存 0
        self.assertEqual((second["input_uncached"], second["cache_read"], second["cache_write"], second["output"]),
                         (0, 140, 10, 4))
        self.assertEqual(first["event_id"], f"{CHILD}:100-60-0-5")
        self.assertEqual((first["turn_id"], first["model"], first["cwd"], first["request_id"]),
                         ("t1", "gpt-6-sol", "/w/sub", ""))

    def test_repeated_cumulative_emits_nothing(self):
        self.write(CHILD, [meta(CHILD), turn("t1"), tokens(100, 60, 5), tokens(100, 60, 5)])
        _, rows = self.events()
        self.assertEqual(len(rows), 1)

    def test_counter_decrease_two_cases(self):
        reset = {"input_tokens": 30, "cached_input_tokens": 10, "output_tokens": 2}
        self.write(CHILD, [meta(CHILD), turn("t1"), tokens(100, 60, 5),
                           tokens(30, 10, 2, last=reset),          # 重新从 0 计：出事件
                           tokens(20, 5, 1),                        # 下降且不等于 last：不出事件
                           tokens(25, 5, 2)])                       # 以下降后的值为基线继续
        out, rows = self.events()
        got = sorted((r["input_uncached"], r["cache_read"], r["output"]) for r in rows)
        self.assertEqual(got, sorted([(40, 60, 5), (20, 10, 2), (5, 0, 1)]))
        self.assertEqual(out["diagnostics"].get("codex_counter_decrease"), 1)

    def test_fork_copy_segment_not_counted(self):
        self.write(CHILD, [meta(CHILD, PARENT, spawn=False), meta(PARENT), turn("01a1ee20-23ec-70e1-bd64-5ec7bf234e8c"),
                           tokens(5000, 4000, 100),                  # 父会话的累计，只作基线
                           turn("01a1ee21-0f63-7160-a2dd-da3541c9ef17"),
                           tokens(300, 200, 7, last={"input_tokens": 300, "cached_input_tokens": 200,
                                                     "output_tokens": 7})])
        _, rows = self.events()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["input_uncached"], rows[0]["cache_read"], rows[0]["output"], rows[0]["turn_id"],
                          rows[0]["session_id"]), (100, 200, 7, "01a1ee21-0f63-7160-a2dd-da3541c9ef17", CHILD))

    def test_archive_copy_merges(self):
        rows = [meta(CHILD), turn("t1"), tokens(100, 60, 5)]
        self.write(CHILD, rows)
        archived = Path(self.tmp.name) / "archived"
        archived.mkdir()
        (archived / f"rollout-2026-11-20T01-00-00-{CHILD}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.registry["sources"][0]["paths"] = [str(archived)]
        _, got = self.events()
        self.assertEqual(len(got), 1)

    def test_declarations(self):
        mcp = {"type": "custom_tool_call", "name": "exec", "call_id": "c1",
               "input": 'text(await tools.mcp__pulse_request_purpose__declare_session_wi({wi:"wi_20261121_413"}));\n'}
        ok = {"type": "custom_tool_call_output", "call_id": "c1", "output": [
            {"type": "input_text", "text": "Script completed"},
            {"type": "input_text", "text": '{"structuredContent":{"schema":"codex_session_wi/v1","wi":"wi_20261121_413"}}'}]}
        bad_call = dict(mcp, call_id="c2")
        bad = {"type": "custom_tool_call_output", "call_id": "c2", "output": [{"type": "input_text", "text": "Error"}]}
        self.write(CHILD, [meta(CHILD), turn("t1")] + [{"timestamp": TS, "type": "response_item", "payload": p}
                                                       for p in (mcp, ok, bad_call, bad)])
        self.events()
        conn = sqlite3.connect(self.db)
        got = set(conn.execute("SELECT claim_id, wi, status, turn_id, scope FROM wi_claims"))
        self.assertEqual(got, {("c1", "wi_20261121_413", "accepted", "t1", "turn"),
                               ("c2", "wi_20261121_413", "rejected", "t1", "turn")})

    def test_compressed_archive_is_read(self):
        from compression import zstd
        rows = [meta(CHILD), turn("t1"), context(), tokens(100, 60, 5), tokens(250, 200, 9)]
        archived = Path(self.tmp.name) / "archived"
        archived.mkdir()
        target = archived / f"rollout-2026-11-20T01-00-00-{CHILD}.jsonl.zst"
        target.write_bytes(zstd.compress("".join(json.dumps(r) + "\n" for r in rows).encode()))
        self.registry["sources"][0]["paths"] = [str(archived)]
        out, got = self.events()
        self.assertEqual(len(got), 2)
        self.assertEqual({r["session_id"] for r in got}, {CHILD})
        self.assertEqual(sum(r["cache_read"] for r in got), 200)
        self.assertEqual((out["backlog_bytes"], out["backlog_files"]), (0, 0))
        # 已读完的压缩文件下一轮不再读；同一会话的未压缩副本按主键合并
        self.write(CHILD, rows)
        out, again = self.events()
        self.assertEqual((len(again), out["rows_changed"]), (2, 0))

    def test_spawned_thread_counts_under_root_turn(self):
        grand = "01a1ee22-0000-7000-8000-000000000000"
        self.write(PARENT, [meta(PARENT), started("01a1ee20-aaaa-7000-8000-000000000000", "composer")])
        self.write(CHILD, [meta(CHILD, PARENT), meta(PARENT),
                           started("01a1ee21-0f63-7160-a2dd-da3541c9ef17", "composer",
                                   "01a1ee20-aaaa-7000-8000-000000000000"),
                           tokens(100, 60, 5)])
        self.write(grand, [meta(grand, CHILD), started("01a1ee22-1111-7000-8000-000000000000", "composer",
                                                       "01a1ee21-0f63-7160-a2dd-da3541c9ef17"),
                           tokens(10, 0, 1),
                           {"timestamp": TS, "type": "response_item", "payload": {
                               "type": "custom_tool_call", "name": "exec", "call_id": "g1",
                               "input": 'text(await tools.mcp__pulse_request_purpose__declare_session_wi({wi:"wi_20261121_413"}));'}}])
        _, rows = self.events()
        got = {r["event_id"].split(":")[0]: (r["session_id"], r["agent_id"], r["turn_id"]) for r in rows}
        self.assertEqual(got[CHILD], (PARENT, CHILD, "01a1ee20-aaaa-7000-8000-000000000000"))
        self.assertEqual(got[grand][:2], (PARENT, grand))            # 孙线程上溯到根线程
        conn = sqlite3.connect(self.db)
        claim = conn.execute("SELECT scope, agent_id FROM wi_claims").fetchone()
        self.assertEqual(tuple(claim), ("subagent", grand))
        origins = conn.execute("SELECT turn_id, origin FROM turn_origins").fetchall()
        self.assertEqual(origins, [("01a1ee20-aaaa-7000-8000-000000000000", "human")])   # 子线程不写回合来源

    def test_turn_origins_from_first_user_message(self):
        self.write(CHILD, [meta(CHILD),
                           started("t1", "composer"), user_msg("<environment_context>x"),
                           started("t2", "exec"), user_msg("<environment_context>x"), user_msg("# AGENTS.md instructions for /w"),
                           user_msg("[系统投递] 定时任务"),
                           started("t3", "exec"), user_msg("<system-reminder>后台任务完成"),
                           started("t4", "exec"), user_msg("请修一下"),
                           started("t5", "exec")])
        self.events()
        conn = sqlite3.connect(self.db)
        got = dict(conn.execute("SELECT turn_id, origin FROM turn_origins"))
        self.assertEqual(got, {"t1": "human", "t2": "delivery", "t3": "continuation", "t4": "human"})
        # t5 还没有用户消息：等到下一回合开始或用户消息到达才判定
        with (self.root / "2026" / "11" / "20" / f"rollout-2026-11-20T01-00-00-{CHILD}.jsonl").open("a") as h:
            h.write(json.dumps(user_msg("<task-notification>done")) + "\n")
        self.events()
        self.assertEqual(dict(conn.execute("SELECT turn_id, origin FROM turn_origins"))["t5"], "continuation")

    def test_session_entrypoint_from_originator(self):
        from pulse_compute_cost.receipts import reference
        app, exe = meta(PARENT), meta(CHILD, PARENT)
        app["payload"]["originator"] = "Codex Desktop"
        exe["payload"]["originator"] = "codex_exec"            # 子代理线程：归根线程，不单记
        other = "01a1ee30-0000-7000-8000-000000000000"
        plain = meta(other)
        plain["payload"]["originator"] = "codex_exec"
        self.write(PARENT, [app]); self.write(CHILD, [exe]); self.write(other, [plain])
        self.events()
        conn = sqlite3.connect(self.db)
        got = dict(conn.execute("SELECT session_ref, entrypoint FROM session_entrypoints"))
        self.assertEqual(got, {reference("native_session", ["codex", PARENT]): "codex_app",
                               reference("native_session", ["codex", other]): "codex_exec"})


if __name__ == "__main__":
    unittest.main()
