from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_meter import ship, store  # noqa: E402

PEER = {"id": "oracle", "host": "pulse@oracle", "python": "/py", "script": "/rel/Scripts/pulse-stats-cost-refresh.py"}


def usage(i, node="n1"):
    return ("usage_events", {"node": node, "runtime": "cc", "event_id": f"m{i}", "request_id": "", "source": "native",
                             "granularity": "request", "session_id": "s", "session_ref": "ref", "occurred_at":
                             "2026-11-20T00:00:00.000Z", "input_uncached": i, "cache_read": 0, "cache_write": 0,
                             "output": 1, "final": 1})


class ShipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.node_db, self.central_db = base / "n" / "node.sqlite", base / "c" / "central.sqlite"
        self.calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, n=7):
        conn = store.connect(self.node_db, node="n1")
        store.write_rows(conn, [usage(i) for i in range(n)] + [
            ("workspaces", {"node": "n1", "workspace_root": "/w", "agent": "pulse"}),
            ("turn_origins", {"node": "n1", "runtime": "cc", "session_ref": "ref", "turn_id": "t",
                              "origin": "human", "occurred_at": "2026-11-20T00:00:00.000Z"})])
        conn.close()

    def good(self, data, timeout):
        self.calls += 1
        ack = ship.receive(self.central_db, data, peer="n1", allowed_peers={"n1"})
        return (json.dumps(ack) + "\n").encode()

    def central(self, sql):
        conn = sqlite3.connect(self.central_db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_batches_and_ack_advances(self):
        self.seed()
        with patch.object(ship, "MAX_ROWS", 3):
            out = ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual((out["sent"], out["applied"]), (9, 9))
        self.assertEqual(self.calls, 3)   # 3 + 3 + 2；取空即停
        self.assertEqual(self.central("SELECT count(*) FROM usage_events"), [(7,)])
        conn = sqlite3.connect(self.node_db)
        self.assertEqual(dict(conn.execute("SELECT table_name, acked_seq FROM ship_cursors WHERE dest='oracle'")),
                         {"usage_events": 7, "workspaces": 8, "turn_origins": 9})
        cursors = self.central("SELECT table_name, last_seq FROM transfer_cursors ORDER BY table_name")
        self.assertIn(("usage_events", 7), cursors)
        # 再发一轮：只发空批报到，不重复
        out = ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual((out["sent"], out["applied"], out["batches"]), (0, 0, 1))

    def test_failure_does_not_advance_and_resend_applies_zero(self):
        self.seed()

        def lost_ack(data, timeout):
            self.good(data, timeout)          # 对端已写入，但 ack 丢了
            raise ship.ShipError("ssh timeout")
        out = ship.send(self.node_db, PEER, "n1", transport=lost_ack)
        self.assertEqual(out["sent"], 0)
        self.assertIn("error", out)
        conn = sqlite3.connect(self.node_db)
        self.assertEqual(conn.execute("SELECT count(*) FROM ship_cursors").fetchone()[0], 0)
        for bad in (b"not json", json.dumps({"schema": "x"}).encode(),
                    json.dumps({"schema": ship.ACK_SCHEMA, "acked": {"usage_events": 1}, "applied": {}}).encode()):
            out = ship.send(self.node_db, PEER, "n1", transport=lambda d, t, bad=bad: bad)
            self.assertEqual(out["sent"], 0)
        out = ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual((out["sent"], out["applied"]), (9, 0))

    def test_rebuilt_node_resend_applies_zero(self):
        self.seed()
        ship.send(self.node_db, PEER, "n1", transport=self.good)
        before = self.central("SELECT * FROM usage_events ORDER BY event_id")
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.node_db) + suffix).unlink(missing_ok=True)
        self.seed()
        out = ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual((out["sent"], out["applied"]), (9, 0))
        self.assertEqual(self.central("SELECT * FROM usage_events ORDER BY event_id"), before)
        self.assertEqual(self.central("SELECT count(DISTINCT db_epoch) FROM transfer_cursors"), [(2,)])

    def test_node_must_match_peer(self):
        conn = store.connect(self.node_db, node="n1")
        store.write_rows(conn, [usage(1, node="other")])
        conn.close()
        out = ship.send(self.node_db, PEER, "n1", transport=self.good_or_error)
        self.assertIn("error", out)
        self.assertEqual(self.central("SELECT count(*) FROM usage_events"), [(0,)])
        data, _, _ = ship.build_batch(store.connect(self.node_db), "n1", {t: 0 for t in store.FACT_TABLES})
        with self.assertRaises(ship.ShipError):
            ship.receive(self.central_db, data, peer="n1", allowed_peers={"n1"})
        with self.assertRaises(ship.ShipError):
            ship.receive(self.central_db, data, peer="n2", allowed_peers={"n2"})
        data, _, _ = ship.build_batch(store.connect(self.node_db), "n1", {t: 0 for t in store.FACT_TABLES})
        with self.assertRaises(ship.ShipError):
            ship.receive(self.central_db, data, peer="n1", allowed_peers={"tcloud"})

    def good_or_error(self, data, timeout):
        try:
            return self.good(data, timeout)
        except ship.ShipError as exc:
            raise ship.ShipError(str(exc)) from exc

    def test_apply_local_uses_same_writer(self):
        self.seed(4)
        out = ship.apply_local(self.node_db, self.central_db)
        self.assertEqual((out["sent"], out["applied"]), (6, 6))
        self.assertEqual(ship.apply_local(self.node_db, self.central_db), {"sent": 0, "applied": 0})
        # 汇总库的 change_seq 用自己的计数器
        self.assertEqual(self.central("SELECT value FROM meta WHERE key='next_seq'"), [("7",)])

    def test_ssh_command_shape(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return type("R", (), {"returncode": 0, "stdout": b"{}", "stderr": b""})()
        with patch.object(ship.subprocess, "run", fake_run):
            ship.ssh_transport(PEER, "ownermac")(b"", 30)
        argv = captured["argv"]
        self.assertEqual(argv[:5], ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"])
        self.assertEqual(argv[-1], "/py -I -B /rel/Scripts/pulse-meter.py receive --peer ownermac")

    def test_cli_tail_ship_local_receive_and_rebuild(self):
        import subprocess
        base = Path(self.tmp.name)
        script = Path(__file__).resolve().parents[2] / "Scripts" / "pulse-meter.py"
        projects = base / "projects" / "p"
        projects.mkdir(parents=True)
        (projects / "11111111-2222-3333-4444-555555555555.jsonl").write_text(json.dumps(
            {"type": "assistant", "sessionId": "s", "timestamp": "2026-11-20T00:00:00Z",
             "message": {"id": "m1", "model": "m", "usage": {"input_tokens": 1, "output_tokens": 2}}}) + "\n")
        registry = base / "sources.json"
        registry.write_text(json.dumps({"sources": [{"kind": "cc", "node": "n1", "path": str(base / "projects")}]}))
        refresh = base / "refresh.json"
        refresh.write_text(json.dumps({"peers": [{"id": "n1"}, {"id": "tcloud"}], "paused_peers": {"tcloud": "x"}}))
        common = [sys.executable, "-I", "-B", str(script), "--db", str(self.node_db), "--central", str(self.central_db),
                  "--registry", str(registry), "--refresh", str(refresh), "--since", ""]

        def cli(*argv, data=None):
            done = subprocess.run(common + list(argv), input=data, capture_output=True)
            return done.returncode, done.stdout, done.stderr
        code, out, _ = cli("tail")
        self.assertEqual((code, json.loads(out)["rows_changed"]), (0, 1))
        code, out, _ = cli("ship", "--local")
        self.assertEqual((code, json.loads(out)), (0, {"sent": 1, "applied": 1}))
        data, _, _ = ship.build_batch(store.connect(self.node_db), "n1", {t: 0 for t in store.FACT_TABLES})
        code, out, _ = cli("receive", "--peer", "n1", data=data)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["applied"], {"usage_events": 0})
        code, _, err = cli("receive", "--peer", "tcloud", data=data)
        self.assertEqual(code, 2)
        code, out, _ = cli("rebuild", "--yes", "--local")
        result = json.loads(out)
        self.assertEqual((code, result["rows"]["usage_events"], result["ship"]["applied"]), (0, 1, 0))
        # 只重建汇总库：节点游标还在，但汇总库代次变了，必须全量重放
        Path(str(self.central_db)).unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(self.central_db) + suffix).unlink(missing_ok=True)
        code, out, _ = cli("ship", "--local")
        self.assertEqual((code, json.loads(out)), (0, {"sent": 1, "applied": 1}))
        code, out, _ = cli("rebuild", "--yes", "--local", "--with-central")
        self.assertEqual(json.loads(out)["ship"]["applied"], 1)
        code, out, _ = cli("backlog")
        self.assertEqual(json.loads(out), {"backlog_bytes": 0, "backlog_files": 0})
        code, out, _ = cli("duplicates")
        self.assertEqual(json.loads(out)["count"], 0)
        conn = store.connect(self.central_db)
        store.write_rows(conn, [usage(1, node="n1"), usage(1, node="n2")])
        conn.close()
        code, out, _ = cli("duplicates")
        self.assertEqual(json.loads(out), {"count": 1, "rows": [["cc", "m1", "", "n1,n2"]]})

    def pending(self):
        return dict(self.central("SELECT key, value FROM meta WHERE key LIKE 'pending_unacked:%'"))

    def test_unacked_rows_reported_until_all_confirmed(self):
        self.seed()                                   # 7 条用量 + 1 工作区 + 1 回合来源 = 9 行
        with patch.object(ship, "MAX_ROWS", 3):
            out = ship.send(self.node_db, PEER, "n1", transport=self.good, deadline_seconds=-1)
        # 时限到只发了一批 3 行：汇总端记下还有 6 行未确认，不能判完整
        self.assertEqual(out["sent"], 3)
        self.assertEqual(self.pending(), {"pending_unacked:n1": "6"})
        ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual(self.pending(), {"pending_unacked:n1": "0"})
        # 新增一行：批头 pending_rows 只计本批之外的待确认量，node_seq 是节点最新序号
        conn = store.connect(self.node_db)
        store.write_rows(conn, [usage(99)])
        conn.close()
        conn = store.connect(self.node_db)
        data, _, _ = ship.build_batch(conn, "n1", ship._acked(conn, "oracle"))
        conn.close()
        head = json.loads(__import__("gzip").decompress(data).decode().splitlines()[0])
        self.assertEqual((head["pending_rows"], head["node_seq"]), (0, 10))

    def test_central_rebuild_triggers_full_replay(self):
        self.seed()
        out = ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual((out["sent"], out["epoch_resets"]), (9, 0))
        before = self.central("SELECT * FROM usage_events ORDER BY event_id")
        for suffix in ("", "-wal", "-shm"):                  # 汇总库重建：代次变了
            Path(str(self.central_db) + suffix).unlink(missing_ok=True)
        out = ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual((out["epoch_resets"], out["sent"], out["applied"]), (1, 9, 9))
        self.assertEqual(self.central("SELECT * FROM usage_events ORDER BY event_id"), before)
        self.assertEqual(self.pending(), {"pending_unacked:n1": "0"})
        out = ship.send(self.node_db, PEER, "n1", transport=self.good)   # 代次稳定后不再重放
        self.assertEqual((out["epoch_resets"], out["sent"]), (0, 0))

    def test_central_rebuild_replays_local_merge(self):
        self.seed(3)
        self.assertEqual(ship.apply_local(self.node_db, self.central_db)["sent"], 5)
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.central_db) + suffix).unlink(missing_ok=True)
        self.assertEqual(ship.apply_local(self.node_db, self.central_db), {"sent": 5, "applied": 5})
        self.assertEqual(self.central("SELECT count(*) FROM usage_events"), [(3,)])

    def test_failed_second_batch_leaves_central_incomplete(self):
        self.seed()
        calls = []

        def flaky(data, timeout):
            calls.append(1)
            if len(calls) > 1:
                raise ship.ShipError("ssh timeout")
            return self.good(data, timeout)
        with patch.object(ship, "MAX_ROWS", 3):
            out = ship.send(self.node_db, PEER, "n1", transport=flaky)
        self.assertEqual((out["sent"], "error" in out), (3, True))
        self.assertEqual(self.pending(), {"pending_unacked:n1": "6"})

    def test_retraction_reaches_central_and_resend_is_zero(self):
        self.seed(3)
        ship.send(self.node_db, PEER, "n1", transport=self.good)
        conn = store.connect(self.node_db)
        with store.transaction(conn) as writer:
            writer.retract("usage_events", ["n1", "cc", "m1", ""])
        conn.close()
        out = ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual((out["sent"], out["applied"]), (1, 1))
        self.assertEqual(self.central("SELECT event_id FROM usage_events ORDER BY 1"), [("m0",), ("m2",)])
        self.assertEqual(ship.send(self.node_db, PEER, "n1", transport=self.good)["sent"], 0)
        # 汇总库重建后全量重放：墓碑重放删不到行，applied 只算真变
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.central_db) + suffix).unlink(missing_ok=True)
        out = ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual(self.central("SELECT event_id FROM usage_events ORDER BY 1"), [("m0",), ("m2",)])
        self.assertEqual(out["applied"], 4)        # m0、m2、workspace、turn_origin；墓碑 0

    def test_tombstone_only_batch_advances_central_watermark(self):
        self.seed(2)
        ship.send(self.node_db, PEER, "n1", transport=self.good)
        before = self.central("SELECT value FROM meta WHERE key='next_seq'")[0][0]
        conn = store.connect(self.node_db)
        with store.transaction(conn) as writer:
            writer.retract("usage_events", ["n1", "cc", "m1", ""])
        conn.close()
        ship.send(self.node_db, PEER, "n1", transport=self.good)
        after = self.central("SELECT value FROM meta WHERE key='next_seq'")[0][0]
        self.assertEqual(int(after), int(before) + 1)
        ship.send(self.node_db, PEER, "n1", transport=self.good)          # 重放删不到：水位不动
        self.assertEqual(self.central("SELECT value FROM meta WHERE key='next_seq'")[0][0], after)

    def test_malformed_tombstones_reject_whole_batch(self):
        import gzip
        self.seed(1)
        ship.send(self.node_db, PEER, "n1", transport=self.good)
        head = {"schema": ship.BATCH_SCHEMA, "peer": "n1", "db_epoch": "e", "pending_rows": 0}
        good_row = {"t": "usage_events", **dict(zip(store.COLUMNS["usage_events"], [None] * 99)),
                    "node": "n1", "runtime": "cc", "event_id": "new", "request_id": "", "source": "native",
                    "granularity": "request", "session_ref": "r", "occurred_at": "2026-11-20T00:00:00.000Z",
                    "final": 0, "session_id": "", "agent_id": "", "turn_id": "", "change_seq": 50}
        bad_keys = [("workspaces", '["n1","/w"]'), ("usage_events", '["n2","cc","m0",""]'),
                    ("usage_events", '["n1","cc","m0"]'), ("usage_events", '["n1","cc","m0",1]'),
                    ("usage_events", "not json"), ("meta", '["n1"]')]
        for table, key in bad_keys:
            tomb = {"t": "retractions", "node": "n1", "table_name": table, "row_key": key, "change_seq": 51}
            data = gzip.compress(("\n".join(json.dumps(r) for r in (head, good_row, tomb)) + "\n").encode())
            with self.assertRaises(ship.ShipError, msg=key):
                ship.receive(self.central_db, data, peer="n1", allowed_peers={"n1"})
        # 整批拒收：同批的合法行也没写入，原有行没被删
        self.assertEqual(self.central("SELECT event_id FROM usage_events ORDER BY 1"), [("m0",)])

    def ep(self, value, ref="r"):
        return ("session_entrypoints", {"node": "n1", "runtime": "cc", "session_ref": ref, "entrypoint": value,
                                        "source": "cc_entrypoint"})

    def view_entrypoint(self):
        from datetime import date
        from pulse_meter import publish
        conn = publish.connect(self.central_db)
        try:
            return {e["event_id"]: e["entrypoint"] for e in publish.events(conn, date(2026, 11, 1), date(2026, 12, 1))}
        finally:
            conn.close()

    def test_session_entrypoints_ship_and_view_falls_back_after_tombstone(self):
        conn = store.connect(self.node_db, node="n1")
        store.write_rows(conn, [("usage_events", {**usage(1)[1], "session_ref": "r"}), self.ep("sdk"), self.ep("cli")])
        conn.close()
        ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual(self.view_entrypoint(), {"m1": "cli"})               # 集合里取最小
        conn = store.connect(self.node_db)
        with store.transaction(conn) as writer:
            writer.retract("session_entrypoints", ["n1", "cc", "r", "cli", "cc_entrypoint"])
        conn.close()
        ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertEqual(self.central("SELECT entrypoint FROM session_entrypoints"), [("sdk",)])
        self.assertEqual(self.view_entrypoint(), {"m1": "sdk"})               # 墓碑后回落到剩余信号

    def test_late_or_reordered_entrypoint_batches_give_same_set(self):
        import gzip as _gzip
        rows = [{"t": "session_entrypoints", "node": "n1", "runtime": "cc", "session_ref": "r", "entrypoint": v,
                 "source": "cc_entrypoint", "change_seq": n} for n, v in ((1, "sdk"), (2, "cli"))]
        head = {"schema": ship.BATCH_SCHEMA, "peer": "n1", "db_epoch": "e", "pending_rows": 0}
        results = []
        for i, order in enumerate((rows, rows[::-1], rows + rows[:1])):     # 正序、乱序、迟到重发
            db = Path(self.tmp.name) / f"c{i}" / "central.sqlite"
            for r in order:
                ship.receive(db, _gzip.compress((json.dumps(head) + "\n" + json.dumps(r) + "\n").encode()),
                             peer="n1", allowed_peers={"n1"})
            conn = sqlite3.connect(db)
            results.append(sorted(conn.execute("SELECT session_ref, entrypoint, source FROM session_entrypoints")))
            conn.close()
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])

    def v1_receiver(self, data, timeout):
        """升级前（v1）的接收端：批头 schema 不是 v1 即整批拒收（远端非零退出）。"""
        import gzip as _gzip
        head = json.loads(_gzip.decompress(data).decode().splitlines()[0])
        if head.get("schema") != "pulse_meter_batch/v1":
            raise ship.ShipError("remote exit 2: receive rejected: batch header mismatch")
        raise AssertionError("new sender must not send v1")

    def test_new_sender_old_receiver_rejected_then_backfilled(self):
        from datetime import datetime, timedelta, timezone
        from pulse_meter import publish
        self.seed(3)
        ship.send(self.node_db, PEER, "n1", transport=self.good)                # 升级前已收齐
        conn = store.connect(self.node_db)
        store.write_rows(conn, [usage(7), self.ep("cli")])
        conn.close()
        before = dict(sqlite3.connect(self.node_db).execute("SELECT table_name, acked_seq FROM ship_cursors"))
        out = ship.send(self.node_db, PEER, "n1", transport=self.v1_receiver)   # 接收端回滚到 v1
        self.assertIn("batch header mismatch", out["error"])
        node = sqlite3.connect(self.node_db)
        self.assertEqual(dict(node.execute("SELECT table_name, acked_seq FROM ship_cursors")), before)   # 游标不动
        self.assertEqual(json.loads(node.execute("SELECT value FROM meta WHERE key='ship_state:oracle'").fetchone()[0])["state"],
                         "failed")
        self.assertEqual(node.execute("SELECT count(*) FROM usage_events").fetchone()[0], 4)            # 数据不丢
        node.close()
        # 汇总端收不到报到：超过 STALE_PEER 即判该节点不完整
        conn = store.connect(self.central_db)
        later = datetime.now(timezone.utc) + publish.STALE_PEER + timedelta(minutes=1)
        _, _, reasons = publish.node_coverage(conn, later, {"n1"})
        conn.close()
        self.assertEqual(reasons, {"n1": "stale"})
        # 接收端升级回 v2：下一轮从原游标补齐
        out = ship.send(self.node_db, PEER, "n1", transport=self.good)
        self.assertNotIn("error", out)
        self.assertEqual(out["sent"], 2)
        self.assertEqual(self.central("SELECT count(*) FROM usage_events"), [(4,)])
        self.assertEqual(self.central("SELECT entrypoint FROM session_entrypoints"), [("cli",)])
        self.assertEqual(json.loads(sqlite3.connect(self.node_db).execute(
            "SELECT value FROM meta WHERE key='ship_state:oracle'").fetchone()[0])["state"], "ok")

    def test_receiver_rejects_v1_batches(self):
        import gzip as _gzip
        head = {"schema": "pulse_meter_batch/v1", "peer": "n1", "db_epoch": "e", "pending_rows": 0}
        with self.assertRaises(ship.ShipError):
            ship.receive(self.central_db, _gzip.compress((json.dumps(head) + "\n").encode()), peer="n1",
                         allowed_peers={"n1"})


if __name__ == "__main__":
    unittest.main()
