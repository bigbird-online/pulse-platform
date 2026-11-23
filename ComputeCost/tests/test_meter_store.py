from __future__ import annotations

import itertools
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_meter import store  # noqa: E402


def usage(**over):
    row = {"node": "n1", "runtime": "cc", "event_id": "m1", "request_id": "r1", "source": "native",
           "granularity": "request", "session_id": "s1", "session_ref": "ref1", "agent_id": "",
           "turn_id": "t1", "occurred_at": "2026-11-20T00:00:00.000Z", "model": "claude",
           "cwd": "/w", "input_uncached": 1, "cache_read": 2, "cache_write": 3, "output": 4, "final": 0}
    row.update(over)
    return row


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "meter" / "node.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def db(self, name="node.sqlite"):
        return store.connect(Path(self.tmp.name) / "meter" / name, node="n1")

    def test_meta_and_permissions(self):
        conn = self.db()
        self.assertEqual(store.meta(conn, "schema_version"), "1")
        self.assertEqual(store.meta(conn, "node"), "n1")
        self.assertEqual(store.meta(conn, "next_seq"), "1")
        self.assertEqual(len(store.meta(conn, "db_epoch")), 32)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_repeat_write_changes_nothing(self):
        conn = self.db()
        self.assertEqual(store.write_rows(conn, [("usage_events", usage())])["usage_events"], 1)
        seq = store.meta(conn, "next_seq")
        self.assertEqual(store.write_rows(conn, [("usage_events", usage())])["usage_events"], 0)
        self.assertEqual(store.meta(conn, "next_seq"), seq)
        self.assertEqual(conn.execute("SELECT change_seq FROM usage_events").fetchone()[0], 1)

    def test_change_seq_only_advances_on_real_change(self):
        conn = self.db()
        store.write_rows(conn, [("usage_events", usage()), ("usage_events", usage(event_id="m2"))])
        self.assertEqual(store.meta(conn, "next_seq"), "3")
        store.write_rows(conn, [("usage_events", usage(output=9))])
        row = conn.execute("SELECT output, change_seq FROM usage_events WHERE event_id='m1'").fetchone()
        self.assertEqual(tuple(row), (9, 3))
        store.write_rows(conn, [("usage_events", usage(output=2))])  # 较小值不改
        self.assertEqual(store.meta(conn, "next_seq"), "4")

    def test_order_independent_merge(self):
        variants = [
            usage(input_uncached=None, final=0),
            usage(output=7, final=1, occurred_at="2026-11-20T00:00:01.000Z", session_id="s2", cwd="/later"),
            usage(cache_read=None, model=None),
        ]
        results = set()
        for i, order in enumerate(itertools.permutations(variants)):
            conn = self.db(f"perm{i}.sqlite")
            for row in order:
                store.write_rows(conn, [("usage_events", row)])
            got = store.table_rows(conn, "usage_events")
            results.add(tuple(sorted(got[0].items())))
        self.assertEqual(len(results), 1)
        merged = dict(next(iter(results)))
        self.assertEqual((merged["input_uncached"], merged["cache_read"], merged["output"], merged["final"]),
                         (1, 2, 7, 1))
        self.assertEqual((merged["session_id"], merged["model"]), ("s1", "claude"))

    def test_null_means_unknown(self):
        conn = self.db()
        store.write_rows(conn, [("usage_events", usage(input_uncached=None))])
        self.assertIsNone(conn.execute("SELECT input_uncached FROM usage_events").fetchone()[0])

    def test_claim_status_only_moves_forward(self):
        claim = {"node": "n1", "runtime": "cc", "claim_id": "c1", "scope": "turn", "session_ref": "ref",
                 "turn_id": "t", "agent_id": "", "wi": "wi_20261121_413", "occurred_at": "2026-11-20T00:00:00.000Z"}
        for i, order in enumerate([("pending", "accepted"), ("accepted", "pending")]):
            conn = self.db(f"claim{i}.sqlite")
            for status in order:
                store.write_rows(conn, [("wi_claims", {**claim, "status": status})])
            self.assertEqual(conn.execute("SELECT status FROM wi_claims").fetchone()[0], "accepted")
        conn = self.db("claim-r.sqlite")
        store.write_rows(conn, [("wi_claims", {**claim, "status": "rejected"})])
        store.write_rows(conn, [("wi_claims", {**claim, "status": "pending"})])
        self.assertEqual(conn.execute("SELECT status FROM wi_claims").fetchone()[0], "rejected")

    def test_receipt_and_workspace_idempotent(self):
        conn = self.db()
        receipt = {"node": "n1", "receipt_id": "e1", "kind": "execution", "occurred_at": "2026-11-20T00:00:00.000Z",
                   "agent": "pulse", "wi": None, "runtime": "cc", "state": "accepted"}
        ws = {"node": "n1", "workspace_root": "/w", "agent": "pulse"}
        self.assertEqual(sum(store.write_rows(conn, [("launch_receipts", receipt), ("workspaces", ws)]).values()), 2)
        self.assertEqual(sum(store.write_rows(conn, [("launch_receipts", receipt), ("workspaces", ws)]).values()), 0)
        self.assertEqual(store.write_rows(conn, [("workspaces", {**ws, "agent": "hestia"})])["workspaces"], 1)

    def test_single_instance_lock(self):
        with store.single_instance(self.path):
            with self.assertRaises(RuntimeError):
                with store.single_instance(self.path):
                    pass

    def test_launch_agent_not_downgraded_to_empty(self):
        conn = self.db()
        receipt = {"node": "n1", "receipt_id": "s1", "kind": "scheduled_session", "occurred_at": "2026-11-20T00:00:00.000Z",
                   "agent": "aki", "job_id": "j"}
        store.write_rows(conn, [("launch_receipts", receipt)])
        self.assertEqual(store.write_rows(conn, [("launch_receipts", {**receipt, "agent": None})])["launch_receipts"], 0)
        self.assertEqual(conn.execute("SELECT agent FROM launch_receipts").fetchone()[0], "aki")
        store.write_rows(conn, [("launch_receipts", {**receipt, "agent": "hestia"})])
        self.assertEqual(conn.execute("SELECT agent FROM launch_receipts").fetchone()[0], "hestia")

    def test_session_entrypoints_are_a_set(self):
        conn = self.db()
        base = {"node": "n1", "runtime": "cc", "session_ref": "r", "source": "cc_entrypoint"}
        self.assertEqual(store.write_rows(conn, [("session_entrypoints", {**base, "entrypoint": "cli"})])["session_entrypoints"], 1)
        self.assertEqual(store.write_rows(conn, [("session_entrypoints", {**base, "entrypoint": "cli"})])["session_entrypoints"], 0)
        store.write_rows(conn, [("session_entrypoints", {**base, "entrypoint": "sdk"})])
        self.assertEqual(sorted(r[0] for r in conn.execute("SELECT entrypoint FROM session_entrypoints")), ["cli", "sdk"])

if __name__ == "__main__":
    unittest.main()
