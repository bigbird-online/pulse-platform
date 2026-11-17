import unittest
from test_host_native_agent_inheritance import HostNativeAgentInheritanceTests,WI,AT,END
from pulse_compute_cost.receipts import Receipt,reference

def observed(row,payload):
    return Receipt.make(row.kind,None,payload,entity_ref=row.entity_ref,occurred_at=row.occurred_at,observed_at=row.observed_at,producer=row.producer,evidence=row.evidence)

class HostPurposeInferenceTests(HostNativeAgentInheritanceTests):
    def inference(self,state='ambiguous',wi=None,executor=None,policy='scl_host_session_binding.v1'):
        return Receipt.make('attribution',['inferred',self.native.entity_ref],{
            'target_ref':self.native.entity_ref,'wi':wi,'executor':executor,
            'responsible':None,'delegator':None,'state':state,'policy':policy,'effective_at':AT},
            occurred_at=AT,observed_at=END,producer=policy,evidence=(reference('binding','history'),))

    def test_exact_host_purpose_refines_ambiguous_session_inference(self):
        self.put();self.ledger.ingest_receipts([self.inference()]);before=self.ledger.receipt_snapshot()['sha256']
        result=self.read(group_by=('agent','wi'))
        self.assertEqual(result['summary']['usage']['event_count'],1)
        self.assertEqual(result['groups'][0]['key'],{'agent':'aki','wi':WI})
        self.assertNotIn('join_conflict',result['summary']['cost']['reasons'])
        self.assertEqual(self.ledger.receipt_snapshot()['sha256'],before)

    def test_known_other_work_item_is_not_overwritten(self):
        self.put();self.ledger.ingest_receipts([self.inference('assigned','wi_20260915_002','aki')])
        result=self.read(group_by=('wi',))
        self.assertIn('join_conflict',result['summary']['cost']['reasons'])

    def test_independent_explicit_conflict_is_not_overwritten(self):
        self.put();self.ledger.ingest_receipts([self.inference(policy='reviewed_activity.v1')])
        result=self.read(group_by=('wi',))
        self.assertIn('join_conflict',result['summary']['cost']['reasons'])

    def test_same_purpose_different_binding_requires_explicit_adoption(self):
        p={**self.native.payload,'wi':WI,'executor':'aki','assignment_ref':reference('assignment','native-boundary')}
        self.native=observed(self.native,p)
        self.put()
        self.assertIn('join_conflict',self.read()['summary']['cost']['reasons'])
        from pulse_compute_cost.pi_binding_alias import POLICY
        alias=Receipt.make('attribution','adoption',{
            'target_ref':self.execution.payload['assignment_ref'],
            'assignment_ref':p['assignment_ref'],'wi':WI,'executor':'aki',
            'responsible':None,'delegator':None,'state':'assigned','policy':POLICY,'effective_at':AT},
            occurred_at=AT,observed_at=END,producer=POLICY,evidence=(reference('canonical','proof'),))
        self.ledger.ingest_receipts([alias]);result=self.read(group_by=('wi',))
        self.assertEqual(result['summary']['usage']['event_count'],1)
        self.assertEqual(result['summary']['usage']['known_total_tokens'],130)
        self.assertNotIn('join_conflict',result['summary']['cost']['reasons'])

    def test_unknown_purpose_different_assignment_still_refused(self):
        self.native=observed(self.native,{**self.native.payload,'assignment_ref':reference('assignment','unidentified')})
        self.put();result=self.read()
        self.assertIn('join_conflict',result['summary']['cost']['reasons'])

    def test_missing_quantity_proof_does_not_resolve_inference(self):
        self.host=observed(self.host,{**self.host.payload,'tokens':{**self.host.payload['tokens'],'output':11}})
        self.put();self.ledger.ingest_receipts([self.inference()]);result=self.read()
        self.assertIn('join_conflict',result['summary']['cost']['reasons'])

if __name__=='__main__':unittest.main()
