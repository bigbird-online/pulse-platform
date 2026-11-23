from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_meter import extract_cc, tail  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def line(mid, out=5):
    return json.dumps({"type": "assistant", "sessionId": SID, "timestamp": "2026-11-20T01:00:01Z",
                       "message": {"id": mid, "model": "m", "stop_reason": "end_turn",
                                   "usage": {"input_tokens": 1, "cache_read_input_tokens": 2,
                                             "cache_creation_input_tokens": 3, "output_tokens": out}}}) + "\n"


class TailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.file = self.root / "p" / f"{SID}.jsonl"
        self.file.parent.mkdir(parents=True)
        self.db = Path(self.tmp.name) / "state" / "node.sqlite"
        self.registry = {"sources": [{"kind": "cc", "node": "n1", "path": str(self.root),
                                      "workspace_actors": [{"agent": "pulse", "node": "n1", "workspace_root": "/w"}]}]}
        self.fed = 0
        original = extract_cc.Extractor.feed

        def counting(extractor, raw):
            self.fed += 1
            return original(extractor, raw)
        patcher = patch.object(extract_cc.Extractor, "feed", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_tail(self, **kw):
        self.fed = 0
        return tail.run(self.db, self.registry, since=None, **kw)

    def totals(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT count(*), sum(output) FROM usage_events").fetchone()
        finally:
            conn.close()

    def cursor(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT offset, size FROM file_cursors").fetchone()
        finally:
            conn.close()

    def test_half_line_not_consumed(self):
        whole = line("m1")
        self.file.write_text(whole + line("m2")[:20])
        out = self.run_tail()
        self.assertEqual(self.totals(), (1, 5))
        self.assertEqual(self.cursor()[0], len(whole.encode()))
        self.assertEqual(out["backlog_files"], 1)
        self.assertEqual(out["backlog_bytes"], 20)
        with self.file.open("a") as handle:
            handle.write(line("m2")[20:])
        out = self.run_tail()
        self.assertEqual(self.totals(), (2, 10))
        self.assertEqual((out["backlog_bytes"], out["backlog_files"]), (0, 0))

    def test_append_reads_only_new_lines(self):
        self.file.write_text(line("m1") + line("m2"))
        self.run_tail()
        self.assertEqual(self.fed, 2)
        with self.file.open("a") as handle:
            handle.write(line("m3"))
        out = self.run_tail()
        self.assertEqual(self.fed, 1)
        self.assertEqual(out["rows_changed"], 1)
        self.run_tail()
        self.assertEqual(self.fed, 0)
        self.assertEqual(self.totals(), (3, 15))

    def assert_reread(self, mutate):
        self.file.write_text(line("m1") + line("m2"))
        self.run_tail()
        before = self.totals()
        mutate()
        self.run_tail()
        self.assertGreaterEqual(self.fed, 2)
        self.assertEqual(self.totals(), before)

    def test_inode_change_rereads(self):
        def replace():
            copy = self.file.with_suffix(".tmp")
            copy.write_bytes(self.file.read_bytes())
            os.replace(copy, self.file)
        self.assert_reread(replace)

    def test_truncation_rereads(self):
        self.file.write_text(line("m1") + line("m2"))
        self.run_tail()
        before = self.totals()
        self.assertEqual(before, (2, 10))
        self.file.write_text(line("m1"))          # size < offset
        self.run_tail()
        self.assertEqual(self.fed, 1)
        self.assertEqual(self.cursor(), (len(line("m1")), len(line("m1"))))
        self.assertEqual(self.totals(), (1, 5))   # m1 不重复；文件里已没有的 m2 撤销

    def test_head_change_rereads(self):
        def rewrite_head():
            self.file.write_text(line("m1").replace('"m"', '"x"') + line("m2"))
        self.file.write_text(line("m1") + line("m2"))
        self.run_tail()
        rewrite_head()
        self.run_tail()
        self.assertEqual(self.fed, 2)
        self.assertEqual(self.totals(), (2, 10))

    def test_parser_version_bump_rereads(self):
        def bump():
            patcher = patch.object(extract_cc.Extractor, "PARSER_VERSION", 99)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assert_reread(bump)

    def test_symlink_skipped_and_workspaces_written(self):
        self.file.write_text(line("m1"))
        (self.root / "p" / "link.jsonl").symlink_to(self.file)
        out = self.run_tail()
        self.assertEqual(out["files_seen"], 1)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT node, workspace_root, agent FROM workspaces").fetchall(),
                         [("n1", "/w", "pulse")])

    def test_since_skips_old_files(self):
        self.file.write_text(line("m1"))
        os.utime(self.file, (0, 0))
        out = tail.run(self.db, self.registry)
        self.assertEqual((out["files_seen"], self.totals()[0]), (0, 0))

    def test_deadline_leaves_backlog(self):
        other = self.root / "p" / "22222222-2222-3333-4444-555555555555.jsonl"
        self.file.write_text(line("m1"))
        other.write_text(line("m2"))
        out = self.run_tail(deadline_seconds=-1)
        self.assertEqual(out["backlog_files"], 1)
        out = self.run_tail()
        self.assertEqual((out["backlog_files"], self.totals()[0]), (0, 2))

    def test_codex_sessions_beside_registered_cc_home(self):
        home = Path(self.tmp.name) / "agent-home"
        cc = home / ".claude" / "projects"
        cc.mkdir(parents=True)
        rollout = home / ".codex" / "sessions" / "2026" / "11" / "11" / "rollout-2026-11-11T00-00-00-01a1c8f7-cdd3-7b21-88f3-1f04ec04bc02.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("")
        self.registry["sources"].append({"kind": "cc", "node": "n1", "path": str(cc)})
        found = {(r, p) for r, p, _ in tail.enumerate_sources(self.registry)}
        self.assertIn(("codex", rollout), found)

    def tombstones(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT table_name, row_key FROM retractions").fetchall()
        finally:
            conn.close()

    def test_parser_upgrade_retracts_events_no_longer_produced(self):
        self.file.write_text(line("m1") + line("m2"))
        self.run_tail()
        original = extract_cc.Extractor.feed

        def stricter(extractor, raw):        # 新规则不再认 m2
            return [] if b'"m2"' in raw else original(extractor, raw)
        with patch.object(extract_cc.Extractor, "feed", stricter), \
                patch.object(extract_cc.Extractor, "PARSER_VERSION", 99):
            out = self.run_tail()
        self.assertEqual(self.totals(), (1, 5))
        self.assertEqual(out["rows_changed"], 1)
        self.assertEqual(self.tombstones(), [("usage_events", '["n1","cc","m2",""]')])
        # 回到旧规则：m2 再次产生，墓碑随之删除
        self.run_tail()
        self.assertEqual(self.totals(), (2, 10))
        self.assertEqual(self.tombstones(), [])

    def test_row_still_produced_by_another_file_is_kept(self):
        copy = self.root / "p" / "22222222-2222-3333-4444-555555555555.jsonl"
        self.file.write_text(line("m1") + line("m2"))
        copy.write_text(line("m2"))                # 恢复会话的历史拷贝也有 m2
        self.run_tail()
        self.file.write_text(line("m1"))
        self.run_tail()
        self.assertEqual(self.totals(), (2, 10))
        self.assertEqual(self.tombstones(), [])

    def test_deleted_file_does_not_retract(self):
        self.file.write_text(line("m1"))
        self.run_tail()
        self.file.unlink()
        self.run_tail()
        self.assertEqual(self.totals(), (1, 5))

    def test_legacy_database_sweeps_orphans_after_full_reread(self):
        from pulse_meter import store
        self.file.write_text(line("m1"))
        self.run_tail()
        conn = sqlite3.connect(self.db)               # 模拟旧库：没有来源登记，且留有一条旧规则产生的行
        conn.execute("DROP TABLE file_rows")
        conn.execute("INSERT INTO usage_events(node, runtime, event_id, request_id, source, granularity, session_ref,"
                     " occurred_at, change_seq) VALUES('n1','cc','stale','','native','request','r','2026-11-20T00:00:00Z', 99)")
        conn.commit()
        conn.close()
        self.run_tail()
        self.assertEqual(self.totals()[0], 1)
        self.assertEqual(self.tombstones(), [("usage_events", '["n1","cc","stale",""]')])
        self.assertEqual(store.meta(store.connect(self.db), "orphan_sweep"), "done")

    def test_interrupted_reread_still_retracts(self):
        self.file.write_text(line("m1") + line("m2"))
        self.run_tail()
        self.file.write_text(line("m1") + "\n")   # 重写：只余 m1（改头触发整文件重读）

        def crash(*args, **kwargs):
            raise RuntimeError("killed between cursor and retraction")
        with patch.object(tail, "_finish_reread", crash):
            with self.assertRaises(RuntimeError):
                self.run_tail()
        self.assertEqual(self.totals(), (2, 10))      # 中断：游标与撤销一起回滚
        self.run_tail()
        self.assertEqual(self.totals(), (1, 5))
        self.assertEqual(len(self.tombstones()), 1)

    def test_eof_fast_path_recovers_pending_retraction(self):
        self.file.write_text(line("m1") + line("m2"))
        self.run_tail()
        conn = sqlite3.connect(self.db)              # 旧版本留下的半成品：游标已到文件尾，旧登记仍挂着
        conn.execute("UPDATE file_rows SET path=? WHERE path=?", (tail.STALE + str(self.file), str(self.file)))
        conn.execute("INSERT INTO file_rows(path, table_name, row_key) VALUES(?, 'usage_events', '[\"n1\",\"cc\",\"m1\",\"\"]')",
                     (str(self.file),))
        conn.commit()
        conn.close()
        self.run_tail()
        self.assertEqual(self.fed, 0)
        self.assertEqual(self.totals(), (1, 5))
        self.assertEqual(self.tombstones(), [("usage_events", '["n1","cc","m2",""]')])


if __name__ == "__main__":
    unittest.main()
