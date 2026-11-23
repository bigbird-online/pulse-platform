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

SID = "01a1c8e9-a885-7170-a01c-95945eb49350"
TS = "2026-11-20T01:00:00.000Z"


def msg(rid, role, **message):
    return {"type": "message", "id": rid, "timestamp": TS, "message": {"role": role, **message}}


USAGE = {"input": 220, "output": 5, "cacheRead": 7040, "cacheWrite": 3, "totalTokens": 7268}


class PiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"
        self.db = Path(self.tmp.name) / "state" / "node.sqlite"
        self.registry = {"sources": [{"kind": "pi", "node": "n1", "path": str(self.root)}]}

    def tearDown(self):
        self.tmp.cleanup()

    def run_rows(self, rows):
        path = self.root / "--w--" / f"2026-11-20T01-00-00-000Z_{SID}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        tail.run(self.db, self.registry, since=None)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_four_token_classes(self):
        conn = self.run_rows([
            {"type": "session", "id": SID, "timestamp": TS, "cwd": "/w"},
            {"type": "model_change", "id": "mc", "timestamp": TS, "modelId": "gpt-6-sol"},
            msg("u1", "user", content=[{"type": "text", "text": "hi"}]),
            msg("a1", "assistant", content=[], usage=USAGE, model="gpt-6-sol"),
            msg("r1", "toolResult", toolCallId="x", content=[], usage={"input": 1, "output": 2, "cacheRead": 0,
                                                                      "cacheWrite": 0}),
            {"type": "compaction", "id": "c1", "timestamp": TS, "usage": {"input": 9, "output": 1, "cacheRead": 0,
                                                                         "cacheWrite": 0}},
            msg("a2", "assistant", content=[]),  # 无 usage，不出事件
        ])
        rows = {r["event_id"]: r for r in conn.execute("SELECT * FROM usage_events")}
        self.assertEqual(set(rows), {f"{SID}:a1", f"{SID}:r1", f"{SID}:c1"})
        a1 = rows[f"{SID}:a1"]
        self.assertEqual((a1["input_uncached"], a1["cache_read"], a1["cache_write"], a1["output"]), (220, 7040, 3, 5))
        self.assertEqual((a1["turn_id"], a1["model"], a1["cwd"], a1["session_ref"]),
                         ("u1", "gpt-6-sol", "/w", reference("native_session", ["pi", SID])))

    def test_declaration_pairing(self):
        call = {"type": "toolCall", "id": "call_a", "name": "declare_session_wi", "arguments": {"wi": "none"}}
        call_b = {"type": "toolCall", "id": "call_b", "name": "declare_session_wi", "arguments": {"wi": "wi_20261121_413"}}
        conn = self.run_rows([
            {"type": "session", "id": SID, "timestamp": TS, "cwd": "/w"},
            msg("u1", "user", content=[]),
            msg("a1", "assistant", content=[call, call_b]),
            msg("r1", "toolResult", toolCallId="call_a", toolName="declare_session_wi", content=[]),
            msg("r2", "toolResult", toolCallId="call_b", toolName="declare_session_wi", content=[], isError=True),
        ])
        self.assertEqual([tuple(r) for r in conn.execute("SELECT turn_id, origin FROM turn_origins")], [("u1", "human")])
        got = {tuple(r) for r in conn.execute("SELECT claim_id, wi, status, turn_id FROM wi_claims")}
        self.assertEqual(got, {("call_a", "none", "accepted", "u1"), ("call_b", "wi_20261121_413", "rejected", "u1")})

    def test_session_entrypoint_from_path(self):
        from pulse_meter import extract_pi
        relay = Path(self.tmp.name) / "pi-sessions"
        cases = {"pi_cli": self.root / "--w--" / "s.jsonl",
                 "feishu": relay / "owner_aki_feishu-ea8ffbe00457" / "s.jsonl",
                 "relay": relay / "owner_aki-d9a129bb0ed9" / "s.jsonl"}
        for expected, path in cases.items():
            ctx = type("C", (), {"source": {}, "path": path})()
            self.assertEqual(extract_pi.pi_entrypoint(ctx), expected)
        ctx = type("C", (), {"source": {"runtime_manifest": "/m.json"}, "path": Path("/x/tenant/s.jsonl")})()
        self.assertEqual(extract_pi.pi_entrypoint(ctx), "relay")
        conn = self.run_rows([{"type": "session", "id": SID, "timestamp": TS, "cwd": "/w"}])
        self.assertEqual([tuple(r) for r in conn.execute("SELECT entrypoint, source FROM session_entrypoints")],
                         [("pi_cli", "pi_path")])


if __name__ == "__main__":
    unittest.main()
