import json
import unittest
from pulse_compute_cost.pi_usage import PiUsageBinding, PiUsageError, build_pi_session_snapshot
from pulse_compute_cost.pi_binding_alias import binding_alias_receipts
from pulse_compute_cost.receipts import ReceiptError
from pulse_compute_cost.receipt_sources import collect_registered_sources
import test_native_bound_attribution as fixture

class RetainedPiBindingAliasTests(unittest.TestCase):
    setUp=fixture.NativeBoundAttributionTests.setUp
    append=fixture.NativeBoundAttributionTests.append
    bind=fixture.NativeBoundAttributionTests.bind
    add_host=fixture.NativeBoundAttributionTests.add_host
    query=fixture.NativeBoundAttributionTests.query
    def prepare(self):
        binding=PiUsageBinding(fixture.WI,'work_item',1,'chatgpt_subscription','b'*64,'pulse','surface_v1_local_macos',self.authority)
        active=build_pi_session_snapshot(self.file,binding,generated_at='2026-09-14T10:30:00Z',measurement_role='baseline')
        self.append();self.bind();self.add_host(assignment='b'*64)
        folder=self.native/'compute-cost'
        self.active_file=folder/f"{fixture.WI}-{'b'*64}-baseline.json"
        self.active_file.write_text(json.dumps(active));self.active_file.chmod(0o600)
        collect_registered_sources(self.registry,self.ledger)
        self.old=self.ledger.receipt_snapshot()
        opened=json.loads((folder/f"{fixture.WI}-{'a'*64}-open.json").read_text())
        claim={'schema':'pi_host_usage_capture_receipt/v1','work_item_id':fixture.WI,'epoch':1,'lifecycle_state':'open',
            'binding_sha256':'a'*64,'active_runtime_binding_sha256':'b'*64,'snapshot_sha256':opened['snapshot_sha256'],
            'artifact_ref':f"{fixture.WI}-{'a'*64}-open.json"}
        event={'type':'custom','id':'alias','customType':'pulse-computecost-usage','timestamp':'2026-09-14T11:02:00Z','data':claim}
        with self.file.open('a') as out:out.write(json.dumps(event)+'\n')
    def test_collector_recovers_retained_baseline_and_reopens_without_forks(self):
        self.prepare();before=self.query(watermark=self.old['watermark'])
        self.assertIn('join_conflict',before['summary']['cost']['reasons'])
        (self.harness/'executions'/f'{fixture.WI}.yaml').unlink()
        first=collect_registered_sources(self.registry,self.ledger)
        self.assertGreater(first['inserted'],0);after=self.query()
        self.assertNotIn('join_conflict',after['summary']['cost']['reasons'])
        self.assertEqual(after['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(self.query(watermark=self.old['watermark']),before)
        for _ in range(2):self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)
    def test_foreign_scope_and_changed_native_prefix_still_fail_closed(self):
        self.prepare();original=self.file.read_bytes()
        with self.assertRaises((ReceiptError, PiUsageError)):
            binding_alias_receipts(b' '+original,{},fixture.LATER,path=self.file,verified_boundaries=self.old['facts'])
        value=json.loads(self.active_file.read_text());value['phase']='unrelated'
        self.active_file.write_text(json.dumps(value))
        with self.assertRaises((ReceiptError, PiUsageError)):
            binding_alias_receipts(original,{},fixture.LATER,path=self.file,verified_boundaries=self.old['facts'])
    def test_accepted_original_snapshot_cannot_bypass_missing_active_baseline(self):
        self.prepare()
        claim=json.loads(self.file.read_bytes().splitlines()[-1])['data']
        key=(fixture.WI,'pulse',1,'work_item','pi',self.authority)
        measurements={key:{'native_binding_snapshots':{claim['snapshot_sha256']:'a'*64},
            'not_before':fixture.AT,'evidence_ref':self.old['facts'][0].fact_ref}}
        self.active_file.unlink()
        self.assertEqual(binding_alias_receipts(self.file.read_bytes(),measurements,fixture.LATER,
            path=self.file,verified_boundaries=self.old['facts']),[])

    def test_missing_retained_files_do_not_invent_an_alias(self):
        self.prepare();self.active_file.unlink()
        self.assertEqual(binding_alias_receipts(self.file.read_bytes(),{},fixture.LATER,path=self.file,
            verified_boundaries=self.old['facts']),[])

if __name__=='__main__':unittest.main()
