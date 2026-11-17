"""Synthetic channel evidence; only identity metadata is queried."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.receipts import Receipt, ReceiptError, reference
from pulse_compute_cost.receipt_sources import host_entrypoint_receipts
AT='2026-09-15T01:00:01.000Z'

class HostEntrypointMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.inputs=self.root/'inputs';self.inputs.mkdir(mode=0o700)
        self.execution=Receipt.make('execution','fixture-attempt',{
            'wi':'wi_20260915_001','executor':'aki','responsible':None,'delegator':None,
            'runtime':'pi','assignment_ref':reference('assignment','fixture'),
            'parent_ref':None,'state':'completed','expected_usage':True},
            occurred_at=AT,observed_at=AT,producer='relay_host.v1')
        self.record={'source':{'channel':'feishu','eventId':'fixture-event',
                    'humanRef':'fixture-human','message':'synthetic fixture only'},
            'poolKey':'fixture-pool','observedAtMs':int(datetime.fromisoformat('2026-09-15T01:00:00+00:00').timestamp()*1000)}
        values=['feishu','fixture-event','fixture-human','fixture-pool']
        self.input_id=hashlib.sha256(json.dumps(values,separators=(',',':')).encode()).hexdigest()
        self.path=self.inputs/(self.input_id+'.json')
        self.path.write_text(json.dumps(self.record));self.path.chmod(0o600)
        target=reference('human',self.input_id)
        self.claim={'schema':'pulse_collaboration_host_event/v1','kind':'attribution',
            'policy':'authenticated_dispatch_assignment.v1','target_ref':target,
            'event_ref':reference('host_event',['human_assignment',self.execution.entity_ref,target]),
            'attribution_ref':reference('attribution',['human_assignment',target,self.execution.payload['assignment_ref']]),
            'effective_at':'2026-09-15T01:00:00.000Z','wi':'wi_20260915_001',
            'executor':'aki','responsible':None,'delegator':None}
        self.host=self.root/'host.jsonl'
        self.source={'node':'tcloud','human_input_directory':str(self.inputs)}
    def read(self):
        self.host.write_text(json.dumps(self.claim)+'\n')
        return host_entrypoint_receipts(self.source,[self.host],[self.execution],AT)
    def test_exact_input_and_dispatch_supply_channel_without_task_or_charge(self):
        before=self.path.read_bytes();rows=self.read();self.assertEqual(len(rows),1)
        p=rows[0].payload
        self.assertEqual(p['target_ref'],self.execution.entity_ref)
        self.assertEqual(p['context']['entrypoint'],'feishu');self.assertEqual(p['context']['node'],'tcloud')
        self.assertEqual(p['executor'],'aki');self.assertEqual(p['scope'],'actor');self.assertIsNone(p['wi'])
        self.assertNotIn('synthetic fixture only',str(rows[0].to_dict()))
        self.assertNotIn('fixture-human',str(rows[0].to_dict()));self.assertEqual(self.path.read_bytes(),before)
    def test_same_agent_or_wi_is_not_a_dispatch_identity(self):
        self.claim['event_ref']=reference('host_event','other-dispatch');self.assertEqual(self.read(),[])
    def test_wrong_assignment_or_time_cannot_supply_channel(self):
        self.claim['attribution_ref']=reference('attribution','other-assignment');self.assertEqual(self.read(),[])
        self.claim['effective_at']='2026-09-15T01:00:00.001Z'
        with self.assertRaises(ReceiptError):self.read()
    def test_changed_channel_with_old_reference_is_rejected(self):
        self.record['source']['channel']='telegram';self.path.write_text(json.dumps(self.record))
        with self.assertRaises(ReceiptError):self.read()
    def test_missing_input_is_unknown_not_current_route_default(self):
        self.path.unlink();self.assertEqual(self.read(),[])
    def test_unrelated_or_public_files_are_not_trusted(self):
        (self.inputs/('a'*64+'.json')).write_text('not-json');self.assertEqual(len(self.read()),1)
        self.path.chmod(0o644)
        with self.assertRaises(ReceiptError):self.read()

if __name__=='__main__':unittest.main()
