import json
import unittest
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.native_attribution import bind_pi_boundaries,execution_bindings
from pulse_compute_cost.receipts import ReceiptError
import test_native_bound_attribution as fixture

class NativePrefixMembershipTests(unittest.TestCase):
    setUp=fixture.NativeBoundAttributionTests.setUp
    append=fixture.NativeBoundAttributionTests.append
    bind=fixture.NativeBoundAttributionTests.bind
    query=fixture.NativeBoundAttributionTests.query
    def prepare(self):
        self.earlier=self.native/'earlier.jsonl'
        self.earlier.write_bytes(self.file.read_bytes());self.earlier.chmod(0o600)
        self.append();self.bind()
    def test_same_session_earlier_file_does_not_poison_exact_later_source(self):
        self.prepare();collect_registered_sources(self.registry,self.ledger)
        report=self.query(work_items=(fixture.WI,))
        details=report['summary']['usage']['source_details']
        self.assertFalse(any(s['status']=='partial' for s in details),details)
        self.assertEqual(report['summary']['usage']['event_count'],1)
        self.assertEqual(report['summary']['usage']['known_total_tokens'],130)
        old=self.ledger.receipt_snapshot()
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)
        self.assertEqual(self.ledger.receipt_snapshot(watermark=old['watermark'])['sha256'],old['sha256'])
    def test_unregistered_other_file_cannot_suppress_prefix_failure(self):
        self.prepare();data=self.earlier.read_bytes()
        with self.assertRaises(ReceiptError):
            bind_pi_boundaries(self.earlier,data,[],
                execution_bindings([self.harness]),fixture.LATER,registered_native_paths=[self.earlier])
    def test_missing_last_matching_original_remains_a_source_failure(self):
        self.prepare();self.file.rename(self.root/'retained-outside-source.jsonl')
        collect_registered_sources(self.registry,self.ledger)
        self.assertTrue(any(s['reason']=='native_attribution_boundary_invalid' for s in self.query()['summary']['usage']['source_details']))
    def test_foreign_native_session_never_matches_by_filename(self):
        self.prepare();self.file.write_bytes(self.file.read_bytes().replace(b'native-session',b'foreign-native'))
        collect_registered_sources(self.registry,self.ledger)
        self.assertTrue(any(s['reason']=='native_attribution_boundary_invalid' for s in self.query()['summary']['usage']['source_details']))

if __name__=='__main__':unittest.main()
