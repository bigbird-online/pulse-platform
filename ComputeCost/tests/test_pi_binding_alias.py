import copy
import json
import unittest
from pulse_compute_cost.pi_binding_alias import binding_alias_receipts, assignment_equivalences
from pulse_compute_cost.scl_session_attribution import pi_host_ref
from pulse_compute_cost.receipts import ReceiptError, reference

class PiBindingAliasTests(unittest.TestCase):
    def setUp(self):
        self.claim={'schema':'pi_host_usage_capture_receipt/v1','work_item_id':'wi_20260922_001','epoch':5,
            'lifecycle_state':'open','binding_sha256':'a'*64,'active_runtime_binding_sha256':'b'*64,
            'snapshot_sha256':'c'*64,'artifact_ref':'wi_20260922_001-'+('a'*64)+'-open.json'}
        key=('wi_20260922_001','pulse',5,'implementation','pi',pi_host_ref('native-session'))
        self.measurements={key:{'native_binding_snapshots':{'c'*64:'a'*64},
            'not_before':'2026-09-22T01:00:00Z','evidence_ref':reference('measurement','accepted')}}
    def read(self,claim=None,measurements=None,session='native-session'):
        entries=[{'type':'session','id':session},{'type':'custom','customType':'pulse-computecost-usage',
            'id':'entry','timestamp':'2026-09-22T01:30:00Z','data':self.claim if claim is None else claim}]
        data=b''.join(json.dumps(r).encode()+b'\n' for r in entries)
        return binding_alias_receipts(data,self.measurements if measurements is None else measurements,'2026-09-22T02:00:00Z')
    def test_exact_native_adoption_and_canonical_snapshot_prove_alias(self):
        rows=self.read();self.assertEqual(len(rows),1)
        key=(reference('assignment','b'*64),'wi_20260922_001','pulse')
        self.assertEqual(assignment_equivalences(rows)[key],reference('assignment','a'*64))
        self.assertEqual(rows,self.read())
        self.assertIsNone(rows[0].payload['effective_at'])
    def test_foreign_scope_epoch_or_snapshot_never_grants_equivalence(self):
        for key,value in [('work_item_id','wi_20260922_002'),('epoch',6),('snapshot_sha256','d'*64)]:
            claim=dict(self.claim,**{key:value})
            if key=='work_item_id':claim['artifact_ref']=value+'-'+('a'*64)+'-open.json'
            self.assertEqual(self.read(claim),[])
        self.assertEqual(self.read(session='foreign'),[])
        self.assertEqual(self.read(measurements={}),[])
    def test_malformed_claim_is_a_source_error_not_an_alias(self):
        for mutation in ({'work_item_id':'../escape'},{'epoch':True},{'artifact_ref':'../escape.json'},{'binding_sha256':'wrong'},{'extra':'field'}):
            with self.assertRaises(ReceiptError):self.read(dict(self.claim,**mutation))
    def test_two_disagreeing_alias_receipts_cannot_choose_a_winner(self):
        first=self.read()
        other=dict(self.claim,binding_sha256='d'*64,artifact_ref='wi_20260922_001-'+('d'*64)+'-open.json')
        measurements=copy.deepcopy(self.measurements)
        next(iter(measurements.values()))['native_binding_snapshots']['c'*64]='d'*64
        second=self.read(other,measurements)
        self.assertEqual(assignment_equivalences(first+second),{})
    def test_missing_baseline_mapping_cannot_be_upgraded(self):
        value=copy.deepcopy(self.measurements);next(iter(value.values())).pop('native_binding_snapshots')
        self.assertEqual(self.read(measurements=value),[])
    def test_retained_measurement_timing_does_not_fork_identity_evidence(self):
        before=self.read()[0]
        next(iter(self.measurements.values()))['not_before']='2026-09-22T01:10:00Z'
        after=self.read()[0]
        self.assertEqual(before.fact_ref,after.fact_ref)
        self.assertEqual(before.entity_ref,after.entity_ref)

    def test_legacy_capture_without_alias_is_ignored(self):
        claim=dict(self.claim);claim.pop('active_runtime_binding_sha256')
        self.assertEqual(self.read(claim),[])

if __name__=='__main__':unittest.main()
