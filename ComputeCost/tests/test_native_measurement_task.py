"""Observed native task identity remains usable before quantity sealing."""
import hashlib
import json
import unittest
from datetime import datetime, timezone
import test_scl_session_attribution as f
from pulse_compute_cost.receipts import canonical
from pulse_compute_cost.scl_session_attribution import codex_host_ref,read_bindings
from pulse_compute_cost.receipt_sources import read_codex,collect_registered_sources

class MeasurementTaskTests(unittest.TestCase):
    query=f.SessionAttributionTests.query
    collect=f.SessionAttributionTests.collect
    native=f.SessionAttributionTests.native
    bind=f.SessionAttributionTests.bind
    def setUp(self):
        f.SessionAttributionTests.setUp(self)
        self.db.execute('DELETE FROM source_attachments')
        self.db.execute('DELETE FROM checkpoints')
        self.db.execute('DROP TABLE measurement_epochs')
        self.db.execute('CREATE TABLE measurement_epochs (measurement_epoch_ref TEXT,work_item_id TEXT,execution_epoch INTEGER,host_session_ref TEXT,logical_source_ref TEXT,surface_id TEXT,runtime_class TEXT,agent_id TEXT,status TEXT,created_at TEXT,attribution_json TEXT,attribution_digest TEXT)')
        self.measure()
    def measure(self,wi=f.WI,session=f.THREAD,start=f.BEFORE,end=None,epoch=2,status='baseline_pending'):
        ref='measurement_epoch_v1_'+hashlib.sha256((wi+session+str(epoch)).encode()).hexdigest()
        host=codex_host_ref(session)
        payload={'schema':'work_item_cost_attribution/v1','work_item_id':wi,'execution_epoch':epoch,
            'agent_id':'pulse','measurement_epoch_ref':ref,'boundary_policy':'prospective_exact_lineage_no_backfill',
            'execution_runtime':'resident_harness','started_at':start,'ended_at':end}
        text=canonical(payload).decode()
        self.db.execute('INSERT INTO measurement_epochs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (ref,wi,epoch,host,host,f.SURFACE,'resident_harness','pulse',status,start,text,hashlib.sha256(text.encode()).hexdigest()))
        self.db.commit()
    def test_current_native_measurement_without_primary_attachment_is_attributed(self):
        before=self.database.read_bytes()
        result=self.collect()
        self.assertEqual(result['groups'][0]['key'],{'wi':f.WI,'agent':'pulse'})
        self.assertEqual(result['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(self.database.read_bytes(),before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM source_attachments').fetchone()[0],0)
    def test_new_native_turn_uses_recorded_binding_without_another_review(self):
        self.collect()
        path=self.native(f.THREAD,f.AFTER,'2026-01-01T02:01:00Z')
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        rows[1]['payload']['turn_id']='next-real-turn'
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        result=self.collect()
        self.assertEqual(result['summary']['usage']['event_count'],2)
        self.assertEqual(result['summary']['usage']['wi_unassigned_event_count'],0)
    def test_measurement_does_not_backfill_or_split_mid_turn_quantities(self):
        self.db.execute('DELETE FROM measurement_epochs');self.db.commit()
        self.measure(start='2026-01-01T01:00:30Z')
        result=self.collect()
        self.assertEqual(result['groups'][0]['key']['wi'],'ambiguous')
        self.assertEqual(result['summary']['usage']['known_total_tokens'],130)
    def test_other_native_host_is_not_assigned(self):
        self.native(f.SECOND,f.START,f.END)
        result=self.collect()
        self.assertEqual(result['summary']['usage']['wi_unassigned_event_count'],1)
    def test_two_recorded_WIs_remain_a_real_ambiguity(self):
        self.bind(f.OTHER_WI,f.THREAD,f.BEFORE)
        self.measure(wi=f.OTHER_WI)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'],'ambiguous')
    def test_corrupted_measurement_cannot_supply_task_identity(self):
        self.db.execute("UPDATE measurement_epochs SET attribution_digest='broken'");self.db.commit()
        diagnostics=[]
        rows=read_codex(self.logs,f.END,source_identity=self.source,diagnostic_out=diagnostics)
        self.assertIn('scl_session_binding_unavailable',diagnostics)
        self.assertFalse(any(r.kind=='attribution' and r.payload.get('wi') for r in rows))
        self.assertEqual(next(r for r in rows if r.kind=='usage').payload['reported_total_tokens'],130)
    def test_sealed_end_does_not_lend_identity_to_later_reuse(self):
        self.db.execute('DELETE FROM measurement_epochs');self.db.commit()
        self.measure(end='2026-01-01T00:30:00Z',status='sealed')
        self.assertEqual(self.collect()['summary']['usage']['wi_unassigned_event_count'],1)
    def test_old_watermark_and_repeated_collection_are_unchanged(self):
        self.db.execute('DELETE FROM measurement_epochs');self.db.commit()
        self.collect();mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(watermark=mark)
        self.measure();self.collect()
        self.assertEqual(self.query(watermark=mark),before)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)

if __name__=='__main__':unittest.main()
