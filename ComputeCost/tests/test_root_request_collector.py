"""The real collector cannot ignore an existing contrary root-request review."""
import json
import unittest
import test_scl_session_attribution as f
from pulse_compute_cost.receipt_sources import read_codex,collect_registered_sources
from pulse_compute_cost.receipts import non_wi_attribution,reference,current_receipts
from pulse_compute_cost.codex_request_purpose import POLICY
ROOT_TURN='00000000-0000-4000-8000-000000000003'
CHILD_TURN='00000000-0000-4000-8000-000000000004'

class RootCollectorTests(unittest.TestCase):
    query=f.SessionAttributionTests.query
    collect=f.SessionAttributionTests.collect
    native=f.SessionAttributionTests.native
    bind=f.SessionAttributionTests.bind
    def setUp(self):
        f.SessionAttributionTests.setUp(self)
        parent=self.native(f.THREAD,f.START,f.END)
        child=self.native(f.SECOND,'2026-01-01T01:00:10Z','2026-01-01T01:00:50Z')
        for path,turn in ((parent,ROOT_TURN),(child,CHILD_TURN)):
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            if path==child:
                rows[0]['payload']['source']={'subagent':{'thread_spawn':{'parent_thread_id':f.THREAD}}}
                rows[0]['payload']['parent_thread_id']=f.THREAD
            for row in rows:
                p=row['payload']
                if 'turn_id' in p:p['turn_id']=turn
                if p.get('type')=='task_started':p['root_turn_id']=ROOT_TURN
            path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        parsed=read_codex(self.logs,f.END,source_identity=self.source)
        self.parent=next(r for r in parsed if r.kind=='usage' and r.payload['native_scope_ref']==reference('native_session',['codex',f.THREAD]))
        self.child=next(r for r in parsed if r.kind=='usage' and r.payload['native_scope_ref']==reference('native_session',['codex',f.SECOND]))
    def test_existing_contrary_parent_review_is_seen_before_child_derivation(self):
        proof=reference('fixture','independent original-purpose review')
        prior=non_wi_attribution(self.parent,work_kind='none',observed_at=f.END,evidence=(proof,))
        self.ledger.ingest_receipts([self.parent,prior])
        self.collect()
        active,_=current_receipts(self.ledger.receipt_snapshot()['facts'])
        inferred=[r for r in active if r.kind=='attribution' and r.producer==POLICY and r.payload['target_ref']==self.child.entity_ref]
        self.assertEqual(inferred,[])
    def test_normal_full_collector_derives_once_and_keeps_old_watermark(self):
        result=self.collect()
        self.assertEqual(result['summary']['usage']['event_count'],2)
        self.assertEqual(result['summary']['usage']['wi_unassigned_event_count'],0)
        mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(watermark=mark)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)
        self.assertEqual(self.query(watermark=mark),before)

if __name__=='__main__':unittest.main()
