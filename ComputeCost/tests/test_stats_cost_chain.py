"""WI568: growing receipt transport and native calendar regressions. Synthetic only."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pulse_compute_cost.ledger import UsageLedger, LedgerError
from pulse_compute_cost.receipts import Receipt, current_receipts, reference, canonical
from pulse_compute_cost.receipt_sources import collect_registered_sources, read_codex
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration

AT = "2026-09-13T10:00:00Z"
WI = "wi_20260820_568"

class StatsCostChainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = UsageLedger(self.root / "source")
        self.target = UsageLedger(self.root / "target")
        self.transport = self.root / "transport.jsonl"
        self.registry = {"schema":"computecost_source_registry/v1", "sources":[
            {"id":"peer", "kind":"receipts", "path":str(self.transport)}]}

    def execution(self, state="accepted", supersedes=None):
        return Receipt.make("execution", "dispatch", {
            "wi":WI, "executor":"pulse", "responsible":None, "delegator":None,
            "runtime":"pi", "assignment_ref":None, "parent_ref":None,
            "state":state, "expected_usage":True}, occurred_at=AT,
            observed_at=AT, producer="relay_host.v1", supersedes=supersedes)

    def ship(self, rows=None):
        rows = self.source.receipt_snapshot()["facts"] if rows is None else rows
        self.transport.write_bytes(b"".join(canonical(r.to_dict())+b"\n" for r in rows))
        self.transport.chmod(0o600)
        return collect_registered_sources(self.registry, self.target)

    def test_source_evidence_revisions_survive_growth_replay_and_old_watermark(self):
        original = self.execution()
        self.source.ingest_receipts([original])
        self.ship()
        old = self.target.receipt_snapshot()
        self.source.ingest_receipts([replace(original, evidence=(reference("source_snapshot", "grown"),))])
        active, _ = current_receipts(self.source.receipt_snapshot()["facts"])
        final = self.execution("completed", supersedes=active[0].fact_ref)
        self.source.ingest_receipts([final])
        self.ship()
        for _ in range(3):
            self.assertEqual(self.ship()["inserted"], 0)
        active, conflicts = current_receipts(self.target.receipt_snapshot()["facts"])
        self.assertEqual(conflicts, {})
        self.assertEqual([r.payload["state"] for r in active if r.kind == "execution"], ["completed"])
        self.assertEqual(self.target.receipt_snapshot(watermark=old["watermark"])["sha256"], old["sha256"])
        self.ship([original])  # stale complete-prefix transport cannot revive accepted
        active, conflicts = current_receipts(self.target.receipt_snapshot()["facts"])
        self.assertEqual(conflicts, {})
        self.assertEqual([r.payload["state"] for r in active if r.kind == "execution"], ["completed"])

    def test_same_process_reuses_full_snapshot_hash_until_transaction_change(self):
        self.target.ingest_receipts([self.execution()])
        first=self.target.receipt_snapshot()
        with mock.patch.object(Receipt, 'payload', new_callable=mock.PropertyMock,
                               side_effect=AssertionError('unchanged history decoded')):
            second=self.target.receipt_snapshot()
        self.assertEqual(second['sha256'],first['sha256'])
        self.target.ingest_receipts([self.execution('completed',first['facts'][0].fact_ref)])
        # The durable append extends the prefix hash; the next snapshot must
        # neither reuse the old digest nor decode all original payloads again.
        with mock.patch.object(Receipt, 'payload', new_callable=mock.PropertyMock,
                               side_effect=AssertionError('history decoded after append')):
            third=self.target.receipt_snapshot()
        self.assertNotEqual(third['sha256'],first['sha256'])
        from pulse_compute_cost.receipts import digest_sequence
        self.assertEqual(third['sha256'],digest_sequence(r.to_dict() for r in third['facts']))
        self.assertEqual(third,UsageLedger(self.target.root).receipt_snapshot())

    def test_same_process_reuses_verified_transactions_and_invalidates_on_change(self):
        row=self.execution()
        self.target.ingest_receipts([row])
        first=self.target.receipt_snapshot()
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('unchanged transaction reread')):
            second=self.target.receipt_snapshot()
        self.assertEqual(second['sha256'],first['sha256'])
        tx=next((self.target.root/'receipts').glob('*.json'))
        tx.write_text('{broken')
        with self.assertRaises(LedgerError):
            self.target.receipt_snapshot()

    def test_receipt_transport_parses_only_validated_suffix_after_growth(self):
        self.target.root.mkdir(parents=True,exist_ok=True); self.target.root.chmod(0o700)
        a=self.execution(); self.source.ingest_receipts([a])
        first=self.ship()
        self.assertEqual(first["parser_cache"]["parsed_files"],1)
        current=self.source.receipt_snapshot()["facts"][-1]
        b=self.execution("completed",current.fact_ref); self.source.ingest_receipts([b])
        grown=self.ship()
        self.assertEqual(grown["parser_cache"]["reused_files"],1)
        self.assertEqual(grown["parser_cache"]["parsed_files"],0)
        warm=self.ship()
        self.assertEqual(warm["parser_cache"]["reused_files"],1)
        self.assertEqual(warm["inserted"],0)

    def test_complete_revision_chain_first_import_then_repeated_import(self):
        a = self.execution()
        b = self.execution(supersedes=a.fact_ref)
        c = self.execution("completed", supersedes=b.fact_ref)
        self.source.ingest_receipts([a,b,c])
        self.ship()
        for _ in range(3):
            self.assertEqual(self.ship()["inserted"], 0)
        active, conflicts = current_receipts(self.target.receipt_snapshot()["facts"])
        self.assertEqual(conflicts, {})
        self.assertEqual([r.payload["state"] for r in active if r.kind == "execution"], ["completed"])

    def test_missing_predecessor_still_rejected_atomically(self):
        orphan = self.execution("completed", reference("fact", "absent"))
        with self.assertRaises(LedgerError):
            self.ship([self.execution(), orphan])
        self.assertEqual(self.target.receipt_snapshot()["watermark"], 0)

    def codex(self, start, end):
        folder=self.root/"codex"; folder.mkdir(exist_ok=True)
        rows=[{"type":"session_meta","timestamp":start,"payload":{"id":"test-session"}},
              {"type":"event_msg","timestamp":start,"payload":{"type":"task_started","turn_id":"t"}},
              {"type":"turn_context","timestamp":start,"payload":{"model":"openai/test"}},
              {"type":"event_msg","timestamp":end,"payload":{"type":"token_count","info":{"total_token_usage":
                  {"input_tokens":100,"cached_input_tokens":20,"cache_write_input_tokens":0,"output_tokens":10}}}},
              {"type":"event_msg","timestamp":end,"payload":{"type":"task_complete","turn_id":"t"}}]
        (folder/"test-session.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
        return read_codex(folder, "2026-09-14T12:00:00Z")

    def activity(self, start, end):
        return query_collaboration(self.target, CollaborationQuery(scope="activity", from_date=start,
            to_date=end, timezone="Asia/Shanghai"))

    def test_codex_same_day_native_turn_is_in_calendar_query(self):
        rows=self.codex("2026-09-13T10:00:00Z", "2026-09-13T10:05:00Z")
        self.assertEqual(rows[0].payload["interval_start"], "2026-09-13T10:00:00Z")
        self.target.ingest_receipts(rows)
        for window in [("2026-09-13","2026-09-14"),("2026-09-07","2026-09-14"),("2026-09-01","2026-10-01")]:
            result=self.activity(*window)
            self.assertEqual(result["summary"]["usage"]["known_total_tokens"],110)
            self.assertEqual(result["summary"]["usage"]["event_count"],1)

    def test_codex_midnight_crossing_is_monthly_but_not_forced_into_either_day(self):
        rows=self.codex("2026-09-13T15:59:00Z", "2026-09-13T16:01:00Z")
        self.target.ingest_receipts(rows)
        self.assertEqual(self.activity("2026-09-01","2026-10-01")["summary"]["usage"]["known_total_tokens"],110)
        for day in [("2026-09-13","2026-09-14"),("2026-09-14","2026-09-15")]:
            self.assertEqual(self.activity(*day)["summary"]["usage"]["event_count"],0)

    def test_codex_empty_interruption_keeps_the_wider_midnight_interval(self):
        self.codex("2026-09-13T15:45:00Z", "2026-09-13T15:50:00Z")
        path = self.root / "codex/test-session.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows.extend([
            {"type":"event_msg","timestamp":"2026-09-13T15:55:00Z",
             "payload":{"type":"task_started","turn_id":"empty"}},
            {"type":"event_msg","timestamp":"2026-09-13T16:05:00Z",
             "payload":{"type":"task_started","turn_id":"later"}},
            {"type":"turn_context","timestamp":"2026-09-13T16:05:00Z",
             "payload":{"model":"openai/test"}},
            {"type":"event_msg","timestamp":"2026-09-13T16:10:00Z","payload":{"type":"token_count","info":{"total_token_usage":
             {"input_tokens":200,"cached_input_tokens":40,"cache_write_input_tokens":0,"output_tokens":20}}}},
            {"type":"event_msg","timestamp":"2026-09-13T16:10:00Z",
             "payload":{"type":"task_complete","turn_id":"later"}},
        ])
        path.write_text("".join(json.dumps(row)+"\n" for row in rows))
        receipts = read_codex(path.parent, AT)
        self.assertEqual(receipts[1].payload["interval_start"], "2026-09-13T15:50:00Z")
        self.assertEqual(receipts[1].payload["interval_end"], "2026-09-13T16:10:00Z")
        self.target.ingest_receipts(receipts)
        self.assertEqual(self.activity("2026-09-14", "2026-09-15")["summary"]["usage"]["event_count"], 0)

    def test_codex_recovered_boundary_refines_existing_final_without_changing_old_view(self):
        rows=self.codex("2026-09-13T10:00:00Z", "2026-09-13T10:05:00Z")
        row=rows[0]
        old=Receipt.make(row.kind, None, {**row.payload,"interval_start":None,"interval_end":None},
            occurred_at=row.occurred_at, observed_at=row.observed_at, producer=row.producer, entity_ref=row.entity_ref)
        self.target.ingest_receipts([old]); before=self.target.receipt_snapshot()
        registry={"schema":"computecost_source_registry/v1","sources":[
            {"id":"native", "kind":"codex", "path":str(self.root/"codex")}]}
        collect_registered_sources(registry,self.target)
        self.assertEqual(current_receipts(self.target.receipt_snapshot()["facts"])[1],{})
        self.assertEqual(self.activity("2026-09-13","2026-09-14")["summary"]["usage"]["known_total_tokens"],110)
        self.assertEqual(self.target.receipt_snapshot(watermark=before["watermark"])["sha256"],before["sha256"])
        self.assertEqual(collect_registered_sources(registry,self.target)["inserted"],0)

    def test_missing_native_start_is_not_replaced_with_end_or_file_timestamp(self):
        self.codex("2026-09-13T10:00:00Z", "2026-09-13T10:05:00Z")
        path=self.root/"codex/test-session.jsonl"
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        del rows[1]["timestamp"]
        path.write_text("".join(json.dumps(r)+"\n" for r in rows))
        usage=read_codex(path.parent, AT)[0]
        self.assertIsNone(usage.payload["interval_start"])
        self.assertIsNone(usage.payload["interval_end"])

    def test_preexisting_local_evidence_chain_does_not_strand_remote_successors(self):
        a=self.execution()
        self.source.ingest_receipts([a]); self.ship()
        self.target.ingest_receipts([replace(a,evidence=(reference("source_snapshot","local-second"),))])
        self.source.ingest_receipts([replace(a,evidence=(reference("source_snapshot","remote-second"),))])
        current=current_receipts(self.source.receipt_snapshot()["facts"])[0][0]
        self.source.ingest_receipts([self.execution("completed", current.fact_ref)])
        self.ship()
        for _ in range(3): self.assertEqual(self.ship()["inserted"],0)
        active,conflicts=current_receipts(self.target.receipt_snapshot()["facts"])
        self.assertEqual(conflicts,{})
        self.assertEqual([r.payload["state"] for r in active if r.kind=="execution"],["completed"])

    def test_codex_model_switch_keeps_quantity_but_not_last_model(self):
        self.codex("2026-09-13T10:00:00Z", "2026-09-13T10:05:00Z")
        path=self.root/"codex/test-session.jsonl"
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        rows.insert(-1,{"type":"turn_context","timestamp":"2026-09-13T10:04:00Z","payload":{"model":"other"}})
        path.write_text("".join(json.dumps(r)+"\n" for r in rows))
        usage=read_codex(path.parent, AT)[0]
        self.assertIsNone(usage.payload["model"])
        self.assertEqual(usage.payload["reported_total_tokens"],110)

    def test_codex_active_archive_roots_are_deduplicated_together(self):
        import shutil
        self.codex("2026-09-13T10:00:00Z", "2026-09-13T10:05:00Z")
        archived=self.root/"archive"; shutil.copytree(self.root/"codex", archived)
        registry={"schema":"computecost_source_registry/v1","sources":[
            {"id":"codex","kind":"codex","paths":[str(self.root/"codex"),str(archived)]}]}
        collect_registered_sources(registry,self.target)
        self.assertEqual(self.activity("2026-09-01","2026-10-01")["summary"]["usage"]["known_total_tokens"],110)
        self.assertEqual(collect_registered_sources(registry,self.target)["inserted"],0)

if __name__ == "__main__":
    unittest.main()
