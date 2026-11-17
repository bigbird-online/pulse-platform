"""Historical exclusive Relay routing can identify channel, never task authority."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.pi_context import PiAncestry, tenant_directory
from pulse_compute_cost.receipts import ReceiptError, canonical
from pulse_compute_cost.receipt_sources import read_pi

AT = "2026-09-14T01:00:00Z"
class ResumePoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs = self.root / "inputs"; self.inputs.mkdir(mode=0o700)
        self.pool = "fixture-member:pulse:feishu"
        self.path = self.root / tenant_directory(self.pool) / "native.jsonl"
        self.source = {"human_input_directory": str(self.inputs), "node": "fixture",
                       "runtime_manifest": str(self.root / "declared-manifest.json")}

    def evidence(self, *, pool=None, channel="feishu", when=1789344000000, event="historical"):
        pool = pool or self.pool
        values = [channel, event, "fixture-human", pool]
        token = hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        obj = {"source": {"channel": channel, "eventId": event, "humanRef": "fixture-human", "message": "/wi wi_20260901_001"},
               "poolKey": pool, "observedAtMs": when}
        target = self.inputs / (token + ".json")
        target.write_bytes(canonical(obj)); target.chmod(0o600)
        return target

    def control(self, identity="resume", **overrides):
        packet = {"schema_version": "scl_local_resume_packet/v1", "agent_id": "pulse",
                  "work_item_id": "wi_20260901_001", "execution_authority": True}
        packet.update(overrides)
        return {"id": identity, "parentId": None, "type": "message", "timestamp": AT,
                "message": {"role": "user", "content": "SCL_LOCAL_RESUME_PACKET/v1\nProducer wrapper\n" + json.dumps(packet)}}

    def test_existing_exclusive_pool_proves_resume_channel_without_a_footer(self):
        self.evidence()
        graph = PiAncestry(self.path, self.source)
        context = graph.consume(self.control())
        self.assertEqual(context.channel, "feishu"); self.assertIsNotNone(context.input_ref)
        answer = graph.consume({"id": "answer", "parentId": "resume", "type": "message", "message": {"role": "assistant"}})
        self.assertEqual(answer.channel, "feishu")

    def test_bare_pool_is_not_proven_exclusive_even_with_only_feishu_history(self):
        self.pool = "fixture-member:pulse"
        self.path = self.root / tenant_directory(self.pool) / "native.jsonl"
        self.evidence()
        self.assertIsNone(PiAncestry(self.path, self.source).consume(self.control()).channel)

    def test_other_pool_and_future_records_cannot_color_this_control(self):
        self.evidence(pool="other:pulse:feishu")
        self.evidence(event="future", when=1789520400000)
        self.assertIsNone(PiAncestry(self.path, self.source).consume(self.control()).channel)

    def test_prompt_format_does_not_change_an_exclusive_proven_route(self):
        self.evidence(); graph = PiAncestry(self.path, self.source)
        for index, text in enumerate(("plain input", "quote\n" + self.control()["message"]["content"])):
            row = {"id": str(index), "parentId": None, "type": "message", "timestamp": AT,
                   "message": {"role": "user", "content": text}}
            self.assertEqual(graph.consume(row).channel, "feishu")
            wrong = self.root / "not-this-pool" / "native.jsonl"
            self.assertIsNone(PiAncestry(wrong, self.source).consume(row).channel)

    def test_task_authority_and_packet_claims_are_not_route_evidence(self):
        for fields in ({"agent_id": "aki"}, {"work_item_id": None}, {"execution_authority": False}):
            self.assertIsNone(PiAncestry(self.path, self.source).consume(self.control(**fields)).channel)
        self.evidence()
        for fields in ({"agent_id": "aki"}, {"work_item_id": None}, {"execution_authority": False}):
            self.assertEqual(PiAncestry(self.path, self.source).consume(self.control(**fields)).channel, "feishu")
        source = {k: v for k, v in self.source.items() if k != "runtime_manifest"}
        self.assertIsNone(PiAncestry(self.path, source).consume(self.control()).channel)

    def test_directory_name_without_existing_host_evidence_is_insufficient(self):
        self.assertIsNone(PiAncestry(self.path, self.source).consume(self.control()).channel)

    def test_changed_digest_or_conflicting_channel_cannot_choose_feishu(self):
        self.evidence()
        other = self.evidence(event="contradiction", channel="telegram")
        with self.assertRaises(ReceiptError):
            PiAncestry(self.path, self.source).consume(self.control())
        other.unlink()
        stored = self.evidence(event="changed")
        data = json.loads(stored.read_bytes()); data["source"]["eventId"] = "tampered"
        stored.write_bytes(canonical(data))
        with self.assertRaises(ReceiptError):
            PiAncestry(self.path, self.source).consume(self.control())

    def test_real_reader_adds_only_channel_evidence_not_wi_or_quantity(self):
        self.evidence()
        records = [{"type": "session", "id": "fixture-native", "version": 3, "timestamp": AT},
                   self.control(), {"id": "answer", "parentId": "resume", "type": "message", "timestamp": AT,
                       "message": {"role": "assistant", "model": "test", "provider": "fixture",
                                   "usage": {"input": 10, "output": 2, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 12}}}]
        data = b"".join(canonical(row) + b"\n" for row in records)
        old = read_pi(self.path, AT, data=data)
        new = read_pi(self.path, AT, data=data, source_identity=self.source)
        self.assertEqual([r.to_dict() for r in old if r.kind == "usage"], [r.to_dict() for r in new if r.kind == "usage"])
        actors = [r for r in new if r.kind == "attribution"]
        self.assertEqual(len(actors), 1)
        self.assertEqual(actors[0].payload["context"]["entrypoint"], "feishu")
        self.assertIsNone(actors[0].payload["wi"]); self.assertIsNone(actors[0].payload["executor"])
        self.assertNotIn("fixture-member", str([r.to_dict() for r in new]))

if __name__ == "__main__": unittest.main()
