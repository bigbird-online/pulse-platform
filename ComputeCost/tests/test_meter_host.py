from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_meter import tail  # noqa: E402
from pulse_compute_cost.receipts import reference  # noqa: E402

S = "pulse_collaboration_host_event/v1"
AT = "2026-11-20T01:00:00.000Z"


class HostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.spool = base / "host-receipts"
        self.spool.mkdir()
        self.inputs = base / "human-inputs"
        self.inputs.mkdir()
        self.stem = hashlib.sha256(b"x").hexdigest()
        (self.inputs / f"{self.stem}.json").write_text(json.dumps({"source": {"channel": "feishu"}}))
        self.db = base / "state" / "node.sqlite"
        self.registry = {"sources": [{"kind": "relay_host", "id": "h", "node": "n1", "path": str(self.spool),
                                      "human_input_directory": str(self.inputs)}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_kinds_land_and_others_ignored(self):
        rows = [
            {"schema": S, "kind": "execution", "at": AT, "started_at": AT, "event_ref": "e1", "attempt_ref": "att1",
             "runtime": "cc", "wi": "wi_20261121_413", "executor": "pulse", "state": "accepted",
             "human_ref": reference("human", self.stem)},
            {"schema": S, "kind": "scheduled_session", "at": AT, "started_at": AT, "event_ref": "e2",
             "native_scope_ref": "scope2", "job_id": "job-a", "job_name": "Job A", "wi": None},
            {"schema": S, "kind": "usage", "at": "2026-11-20T01:00:05.000Z", "started_at": AT, "event_ref": "e3",
             "attempt_ref": "att1", "runtime": "cc", "usage_ref": "u1", "model": "claude-opus-5-5",
             "usage": {"input_tokens": 2, "output_tokens": 8, "cache_read_input_tokens": 100,
                       "cache_creation_input_tokens": 5},
             "mapping": "cc_message.v1", "granularity": "request", "native_scope_ref": "scope1", "final": True},
            {"schema": S, "kind": "usage", "at": AT, "started_at": AT, "event_ref": "e4", "attempt_ref": "att1",
             "runtime": "cc", "usage_ref": "u2", "model": "claude-opus-5-5",
             "usage": {"inputTokens": 20, "outputTokens": 80, "cacheReadInputTokens": 1000,
                       "cacheCreationInputTokens": 50},
             "mapping": "cc_sdk_result.v1", "granularity": "turn_aggregate", "native_scope_ref": "scope1"},
            {"schema": S, "kind": "human", "at": AT, "event_ref": "e5", "human_ref": "h"},
            {"schema": S, "kind": "attribution", "at": AT, "event_ref": "e6", "target_ref": "t"},
            {"schema": S, "kind": "gap", "at": AT, "event_ref": "e7", "reason": "unpaired_result"},
        ]
        (self.spool / "w.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        tail.run(self.db, self.registry, since=None)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        receipts = {r["receipt_id"]: dict(r) for r in conn.execute("SELECT * FROM launch_receipts")}
        self.assertEqual(set(receipts), {"e1", "e2", "bind:att1:scope1"})
        self.assertEqual((receipts["e1"]["kind"], receipts["e1"]["agent"], receipts["e1"]["wi"],
                          receipts["e1"]["entrypoint"], receipts["e1"]["attempt_ref"]),
                         ("execution", "pulse", "wi_20261121_413", "feishu", "att1"))
        self.assertEqual((receipts["e2"]["kind"], receipts["e2"]["session_ref"], receipts["e2"]["job_id"],
                          receipts["e2"]["job_name"], receipts["e2"]["entrypoint"]),
                         ("scheduled_session", "scope2", "job-a", "Job A", "automation"))
        bind = receipts["bind:att1:scope1"]
        self.assertEqual((bind["kind"], bind["attempt_ref"], bind["session_ref"]), ("session_binding", "att1", "scope1"))
        usage = {r["event_id"].split(":")[0]: dict(r) for r in conn.execute("SELECT * FROM usage_events")}
        self.assertEqual(set(usage), {"u1", "u2"})
        self.assertEqual((usage["u1"]["source"], usage["u1"]["granularity"], usage["u1"]["session_ref"],
                          usage["u1"]["input_uncached"], usage["u1"]["cache_read"], usage["u1"]["cache_write"],
                          usage["u1"]["output"], usage["u1"]["final"], usage["u1"]["occurred_at"]),
                         ("host", "request", "scope1", 2, 100, 5, 8, 1, "2026-11-20T01:00:05.000Z"))
        self.assertEqual((usage["u2"]["granularity"], usage["u2"]["input_uncached"], usage["u2"]["cache_read"]),
                         ("turn", 20, 1000))
        self.assertEqual(conn.execute("SELECT count(*) FROM wi_claims").fetchone()[0], 0)

    def test_invalid_usage_kept_with_unknown_tokens(self):
        row = {"schema": S, "kind": "usage", "at": AT, "event_ref": "e1", "attempt_ref": "att", "runtime": "cc",
               "usage_ref": "u1", "usage": {"input_tokens": -1}, "mapping": "cc_message.v1", "granularity": "request"}
        (self.spool / "w.jsonl").write_text(json.dumps(row) + "\n")
        tail.run(self.db, self.registry, since=None)
        conn = sqlite3.connect(self.db)
        got = conn.execute("SELECT session_ref, input_uncached, output FROM usage_events").fetchone()
        self.assertEqual(tuple(got), ("att", None, None))

    def sdk(self, ref, at, cr, *, out=10, model="claude-opus-5-5", scope="scope1", runtime="cc"):
        return {"schema": S, "kind": "usage", "at": at, "started_at": at, "event_ref": "e" + ref, "attempt_ref": "a" + ref,
                "runtime": runtime, "usage_ref": ref, "model": model, "mapping": "cc_sdk_result.v1",
                "granularity": "turn_aggregate", "native_scope_ref": scope, "final": True,
                "usage": {"inputTokens": 1, "outputTokens": out, "cacheReadInputTokens": cr, "cacheCreationInputTokens": 5}}

    def write(self, name, rows, mtime):
        import os
        path = self.spool / name
        with path.open("a") as handle:
            handle.write("".join(json.dumps(r) + "\n" for r in rows))
        os.utime(path, (mtime, mtime))

    def usage_rows(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        # 累计计数的增量行 event_id 为 usage_ref:四类增量，这里按 usage_ref 取
        return {r["event_id"].split(":")[0]: dict(r) for r in conn.execute("SELECT * FROM usage_events")}

    def test_sdk_result_is_session_cumulative_and_differenced_across_files(self):
        # 第二个 Relay 进程的文件名排序在前，但写得晚：必须按写入先后取差
        self.write("b.jsonl", [self.sdk("u1", "2026-11-20T01:00:00.000Z", 1000, out=10),
                               self.sdk("u2", "2026-11-20T02:00:00.000Z", 1500, out=30),
                               self.sdk("h1", "2026-11-20T02:30:00.000Z", 70, model="claude-haiku")], 1000)
        self.write("a.jsonl", [self.sdk("u3", "2026-11-20T03:00:00.000Z", 1800, out=31, runtime="cc_sdk"),
                               self.sdk("u4", "2026-11-20T04:00:00.000Z", 900, out=40),       # 下降：不出事件
                               self.sdk("u5", "2026-11-20T05:00:00.000Z", 1000, out=45),
                               self.sdk("u6", "2026-11-20T06:00:00.000Z", 1000, out=45)], 2000)  # 不变：不出事件
        out = tail.run(self.db, self.registry, since=None)
        rows = self.usage_rows()
        got = {k: (v["cache_read"], v["output"], v["cache_write"], v["input_uncached"], v["runtime"]) for k, v in rows.items()}
        self.assertEqual(got, {"u1": (1000, 10, 5, 1, "cc"), "u2": (500, 20, 0, 0, "cc"), "h1": (70, 10, 5, 1, "cc"),
                               "u3": (300, 1, 0, 0, "cc"), "u5": (100, 5, 0, 0, "cc")})
        self.assertEqual(out["diagnostics"].get("host_counter_decrease"), 1)
        # 追加只读新增，基线从节点库续上
        self.write("a.jsonl", [self.sdk("u7", "2026-11-20T07:00:00.000Z", 1200, out=50)], 3000)
        tail.run(self.db, self.registry, since=None)
        self.assertEqual(self.usage_rows()["u7"]["cache_read"], 200)
        # 重读（升版本）得到同样的差值，总数不变
        before = self.usage_rows()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE file_cursors SET parser_version=0")
        conn.commit()
        conn.close()
        tail.run(self.db, self.registry, since=None)
        self.assertEqual({k: v["cache_read"] for k, v in self.usage_rows().items()},
                         {k: v["cache_read"] for k, v in before.items()})

    def test_rebuild_gives_same_deltas(self):
        self.write("b.jsonl", [self.sdk("u1", "2026-11-20T01:00:00.000Z", 1000)], 1000)
        self.write("a.jsonl", [self.sdk("u2", "2026-11-20T02:00:00.000Z", 1600)], 2000)
        tail.run(self.db, self.registry, since=None)
        first = {k: v["cache_read"] for k, v in self.usage_rows().items()}
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)
        tail.run(self.db, self.registry, since=None)
        self.assertEqual({k: v["cache_read"] for k, v in self.usage_rows().items()}, first)
        self.assertEqual(first, {"u1": 1000, "u2": 600})

    def test_turn_increments_and_request_snapshots_taken_as_is(self):
        rows = [{"schema": S, "kind": "usage", "at": f"2026-11-20T0{i}:00:00.000Z", "event_ref": f"e{i}",
                 "attempt_ref": f"a{i}", "runtime": "codex", "usage_ref": f"c{i}", "model": "gpt",
                 "mapping": "codex_cli.v1", "granularity": "turn_aggregate", "native_scope_ref": "scope9",
                 "usage": {"input_tokens": 100 * i, "cached_input_tokens": 50 * i, "output_tokens": i}}
                for i in (1, 2)]
        self.write("c.jsonl", rows, 1000)
        tail.run(self.db, self.registry, since=None)
        got = {k: (v["input_uncached"], v["cache_read"], v["output"]) for k, v in self.usage_rows().items()}
        self.assertEqual(got, {"c1": (50, 50, 1), "c2": (100, 100, 2)})

    def test_empty_usage_without_requests_is_not_an_event(self):
        rows = [{"schema": S, "kind": "usage", "at": AT, "started_at": AT, "event_ref": "e1", "attempt_ref": "a1",
                 "runtime": "cc", "usage_ref": "u1", "model": None, "usage": {}, "mapping": "cc_result.v1",
                 "granularity": "turn_aggregate", "native_scope_ref": "scope1", "covers": [], "final": True},
                {"schema": S, "kind": "usage", "at": AT, "started_at": AT, "event_ref": "e2", "attempt_ref": "a2",
                 "runtime": "codex", "usage_ref": "u2", "model": "gpt", "mapping": "codex_cli.v1",
                 "usage": {"input_tokens": None, "cached_input_tokens": None, "output_tokens": None},
                 "granularity": "turn_aggregate", "native_scope_ref": "scope2", "covers": [], "final": True}]
        (self.spool / "w.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        out = tail.run(self.db, self.registry, since=None)
        got = {k: (v["input_uncached"], v["output"]) for k, v in self.usage_rows().items()}
        self.assertEqual(got, {"u2": (None, None)})            # 键为 null：未知，照常入库
        self.assertEqual(out["diagnostics"].get("host_usage_empty"), 1)
        conn = sqlite3.connect(self.db)                          # 会话绑定照写，线索不丢
        self.assertEqual(conn.execute("SELECT count(*) FROM launch_receipts WHERE kind='session_binding'").fetchone()[0], 2)

    def rows_by_content(self, db):
        conn = sqlite3.connect(db)
        try:
            return sorted(conn.execute(
                "SELECT event_id, session_ref, occurred_at, model, input_uncached, cache_read, cache_write, output"
                " FROM usage_events WHERE source='host'").fetchall())
        finally:
            conn.close()

    def test_late_counter_rewrites_follower_increment(self):
        # 计数 1000（t1）、2500（t3）先到；1500（t2）迟到插在中间：t3 的增量从 1500 改为 1000
        self.write("a.jsonl", [self.sdk("u1", "2026-11-20T01:00:00.000Z", 1000),
                               self.sdk("u3", "2026-11-20T03:00:00.000Z", 2500)], 1000)
        tail.run(self.db, self.registry, since=None)
        self.write("b.jsonl", [self.sdk("u2", "2026-11-20T02:00:00.000Z", 1500)], 2000)
        tail.run(self.db, self.registry, since=None)
        got = {k: v["cache_read"] for k, v in self.usage_rows().items()}
        self.assertEqual(got, {"u1": 1000, "u2": 500, "u3": 1000})
        self.assertEqual(sum(got.values()), 2500)
        conn = sqlite3.connect(self.db)
        tombs = conn.execute("SELECT row_key FROM retractions").fetchall()
        self.assertEqual(len(tombs), 1)
        self.assertIn("u3:", tombs[0][0])

    def test_out_of_order_arrival_equals_clean_rebuild(self):
        import itertools, os, shutil
        counters = [("u1", "2026-11-20T01:00:00.000Z", 1000, 10), ("u2", "2026-11-20T02:00:00.000Z", 1500, 20),
                    ("u3", "2026-11-20T03:00:00.000Z", 900, 25),      # 下降：不出事件
                    ("u4", "2026-11-20T04:00:00.000Z", 1400, 25), ("u5", "2026-11-20T05:00:00.000Z", 1400, 25)]
        clean_db = Path(self.tmp.name) / "clean" / "node.sqlite"
        spool = self.spool
        self.write("all.jsonl", [self.sdk(r, at, cr, out=o) for r, at, cr, o in counters], 1000)
        tail.run(clean_db, self.registry, since=None)
        expected = self.rows_by_content(clean_db)
        self.assertEqual(len(expected), 3)
        for order in itertools.permutations(range(len(counters))):
            shutil.rmtree(spool)
            spool.mkdir()
            db = Path(self.tmp.name) / "perm" / "node.sqlite"
            for suffix in ("", "-wal", "-shm"):
                Path(str(db) + suffix).unlink(missing_ok=True)
            for step, i in enumerate(order):          # 每条计数单独一个文件、逐轮到达
                r, at, cr, o = counters[i]
                self.write(f"f{step}.jsonl", [self.sdk(r, at, cr, out=o)], 1000 + step)
                tail.run(db, self.registry, since=None)
            self.assertEqual(self.rows_by_content(db), expected, order)
            # 再整体重读一遍（升解析版本），结果不变
            conn = sqlite3.connect(db)
            conn.execute("UPDATE file_cursors SET parser_version=0")
            conn.commit()
            conn.close()
            tail.run(db, self.registry, since=None)
            self.assertEqual(self.rows_by_content(db), expected, order)

    def host_rows(self):
        return [{"schema": S, "kind": "usage", "at": AT, "started_at": AT, "event_ref": "e1", "attempt_ref": "a1",
                 "runtime": "agy", "usage_ref": "g1", "model": "gemini-4-neon-high", "mapping": "agy_result.v1",
                 "usage": {"input_tokens": 10150, "cache_read_tokens": 0, "output_tokens": 457},
                 "granularity": "turn_aggregate", "native_scope_ref": "scope1", "covers": [], "final": True},
                {"schema": S, "kind": "usage", "at": AT, "started_at": AT, "event_ref": "e2", "attempt_ref": "a2",
                 "runtime": "agy", "usage_ref": "g2", "model": "claude-via-agy", "mapping": "agy_result.v1",
                 "usage": {"input_tokens": 1, "cache_read_tokens": 0, "output_tokens": 1},
                 "granularity": "turn_aggregate", "native_scope_ref": "scope2", "covers": [], "final": True},
                {"schema": S, "kind": "scheduled_session", "at": AT, "started_at": AT, "event_ref": "s1",
                 "native_scope_ref": "scope3", "job_id": "aki-19dad28dcce", "job_name": "早报", "wi": None},
                {"schema": S, "kind": "scheduled_session", "at": AT, "started_at": AT, "event_ref": "s2",
                 "native_scope_ref": "scope4", "job_id": "gone-job", "job_name": "已删", "wi": None}]

    def cron_file(self, name, owner="aki"):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps({"jobs": [{"id": "aki-19dad28dcce", "owner": owner}]}))
        return path

    def path_map(self, cron):
        path = Path(self.tmp.name) / "paths.json"
        path.write_text(json.dumps({"schema": "pulse_relay_paths/v1", "root": self.tmp.name,
                                    "paths": {"cronConfig": str(cron)}}))
        return path

    def agents_after_tail(self, env):
        import os
        (self.spool / "w.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.host_rows()))
        home = Path(self.tmp.name) / "home"               # 账户主目录下没有受管路径图
        clean = {k: v for k, v in os.environ.items() if k not in ("PULSE_RELAY_PATHS", "CRON_JOBS_PATH")}
        with patch.dict(os.environ, {**clean, **env}, clear=True), patch.object(tail.store, "home", lambda: home):
            tail.run(self.db, self.registry, since=None)
        conn = sqlite3.connect(self.db)
        try:
            return dict(conn.execute("SELECT receipt_id, agent FROM launch_receipts WHERE kind='scheduled_session'"))
        finally:
            conn.close()

    def test_agy_gemini_cache_write_is_zero(self):
        self.agents_after_tail({})
        got = {k: (v["input_uncached"], v["cache_read"], v["cache_write"], v["output"]) for k, v in self.usage_rows().items()}
        self.assertEqual(got, {"g1": (10150, 0, 0, 457), "g2": (1, 0, None, 1)})   # 非 Gemini 仍未知

    def test_job_owner_from_relay_path_map(self):
        cron = self.cron_file("managed-cron.json")
        agents = self.agents_after_tail({"PULSE_RELAY_PATHS": str(self.path_map(cron))})
        self.assertEqual(agents, {"s1": "aki", "s2": None})

    def test_job_owner_from_cron_jobs_path(self):
        agents = self.agents_after_tail({"CRON_JOBS_PATH": str(self.cron_file("env-cron.json"))})
        self.assertEqual(agents, {"s1": "aki", "s2": None})

    def test_path_map_wins_over_cron_jobs_path(self):
        managed = self.cron_file("managed-cron.json", owner="aki")
        other = self.cron_file("other-cron.json", owner="hestia")
        agents = self.agents_after_tail({"PULSE_RELAY_PATHS": str(self.path_map(managed)), "CRON_JOBS_PATH": str(other)})
        self.assertEqual(agents["s1"], "aki")

    def test_default_managed_map_under_account_home(self):
        import os
        cron = self.cron_file("home-cron.json")
        home = Path(self.tmp.name) / "acct"
        (home / ".local/share/pulse/config/relay").mkdir(parents=True)
        (home / ".local/share/pulse/config/relay/paths.json").write_text(json.dumps({"paths": {"cronConfig": str(cron)}}))
        clean = {k: v for k, v in os.environ.items() if k not in ("PULSE_RELAY_PATHS", "CRON_JOBS_PATH")}
        with patch.dict(os.environ, clean, clear=True), patch.object(tail.store, "home", lambda: home):
            self.assertEqual(tail.cron_jobs_path(), cron)

    def jobs_file(self, name, *owners):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps({"jobs": [{"id": "aki-19dad28dcce", "owner": o} for o in owners]}))
        return path

    def test_duplicate_job_ids_with_different_owners_stay_unassigned(self):
        agents = self.agents_after_tail({"CRON_JOBS_PATH": str(self.jobs_file("dup.json", "aki", "hestia"))})
        self.assertEqual(agents["s1"], None)
        agents = self.agents_after_tail({"CRON_JOBS_PATH": str(self.jobs_file("same.json", "aki", "aki"))})
        self.assertEqual(agents["s1"], "aki")                  # 重复但 owner 一致：照补

    def test_fixed_cron_config_rejudges_already_read_receipts(self):
        cron = self.jobs_file("cron.json", "aki", "hestia")
        env = {"CRON_JOBS_PATH": str(cron)}
        self.assertIsNone(self.agents_after_tail(env)["s1"])
        self.assertIsNone(self.agents_after_tail(env)["s1"])   # 配置没变：不重读，结果不变
        conn = sqlite3.connect(self.db)
        versions = {r[0] for r in conn.execute("SELECT parser_version FROM file_cursors WHERE runtime='host'")}
        conn.close()
        cron.write_text(json.dumps({"jobs": [{"id": "aki-19dad28dcce", "owner": "aki"}]}))   # 修正配置
        self.assertEqual(self.agents_after_tail(env)["s1"], "aki")
        conn = sqlite3.connect(self.db)
        self.assertNotEqual({r[0] for r in conn.execute("SELECT parser_version FROM file_cursors WHERE runtime='host'")},
                            versions)
        self.assertEqual(conn.execute("SELECT count(*) FROM retractions").fetchone()[0], 0)   # 只改 agent，不撤销
        conn.close()

    def test_known_agent_is_kept_when_job_deleted_or_ambiguous(self):
        cron = self.jobs_file("cron.json", "aki")
        env = {"CRON_JOBS_PATH": str(cron)}
        self.assertEqual(self.agents_after_tail(env)["s1"], "aki")
        cron.write_text(json.dumps({"jobs": []}))                                   # 任务删了：保留原 agent
        self.assertEqual(self.agents_after_tail(env)["s1"], "aki")
        self.jobs_file("cron.json", "aki", "hestia")                                # 歧义：保留原值
        self.assertEqual(self.agents_after_tail(env)["s1"], "aki")
        self.jobs_file("cron.json", "hestia")                                       # 改为另一个唯一 owner：更正
        self.assertEqual(self.agents_after_tail(env)["s1"], "hestia")
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT count(*) FROM retractions").fetchone()[0], 0)
        conn.close()

    def test_unreread_stale_files_keep_node_incomplete(self):
        import os
        from datetime import datetime, timezone
        from pulse_meter import publish, ship, store
        for name, mtime in (("a.jsonl", 1000), ("b.jsonl", 2000)):
            self.write(name, [{"schema": S, "kind": "scheduled_session", "at": AT, "started_at": AT,
                               "event_ref": "s-" + name, "native_scope_ref": "scope", "job_id": "aki-19dad28dcce",
                               "job_name": "早报", "wi": None}], mtime)
        cron = self.jobs_file("cron.json", "aki")
        central = Path(self.tmp.name) / "central" / "central.sqlite"
        clean = {k: v for k, v in os.environ.items() if k not in ("PULSE_RELAY_PATHS", "CRON_JOBS_PATH")}
        home = Path(self.tmp.name) / "home"

        def round_(**kw):
            with patch.dict(os.environ, {**clean, "CRON_JOBS_PATH": str(cron)}, clear=True), \
                    patch.object(tail.store, "home", lambda: home):
                out = tail.run(self.db, self.registry, since=None, **kw)
                backlog = tail.backlog(self.db, self.registry, since=None)
            ship.apply_local(self.db, central)
            conn = store.connect(central)
            try:
                _, _, reasons = publish.node_coverage(conn, datetime.now(timezone.utc), {"n1"})
            finally:
                conn.close()
            return out, backlog, reasons
        out, _, reasons = round_()
        self.assertEqual((out["backlog_files"], reasons), (0, {}))
        self.jobs_file("cron.json", "hestia")              # owner 映射变了：两个文件都要重读
        out, backlog, reasons = round_(deadline_seconds=-1)   # 时限到，只重读了一个
        self.assertEqual(out["backlog_files"], 1)
        self.assertGreater(out["backlog_bytes"], 0)
        self.assertEqual(backlog["backlog_files"], 1)
        self.assertEqual(reasons, {"n1": "backlog"})       # 发布端不判完整
        out, backlog, reasons = round_()
        self.assertEqual((out["backlog_bytes"], out["backlog_files"], backlog["backlog_files"], reasons), (0, 0, 0, {}))


if __name__ == "__main__":
    unittest.main()
