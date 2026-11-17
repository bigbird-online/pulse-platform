"""A pushed observation bundle is judged like a source the collector read itself."""
import gzip
import json
import os
import unittest
from unittest.mock import patch

from pulse_compute_cost.ledger import LedgerError, UsageLedger
from pulse_compute_cost.native_inputs import launch_inputs
from pulse_compute_cost.native_observations import KIND, SCHEMA, bundle_config, decode, encode, spool_path, store
from pulse_compute_cost.receipt_sources import collect_registered_sources, read_cc
from pulse_compute_cost.receipts import ReceiptError, current_receipts, reference

PEER = 'ownermac'
AT = '2026-11-15T00:00:00Z'


def heads(ledger, kinds):
    active, conflicts = current_receipts(ledger.receipt_snapshot()['facts'])
    assert not conflicts
    return {r.entity_ref: r.payload for r in active if r.kind in kinds}


class ObservationBundleTests(unittest.TestCase):
    def setUp(self):
        from test_cc_request_purpose import RequestPurposeTests, WI
        self.case = RequestPurposeTests('test_reused_session_has_each_request_and_no_ambient_wi')
        self.case.setUp(); self.addCleanup(self.case.doCleanups)
        self.case.request('a', 0, WI); self.case.request('b', 10)
        self.case.write()
        self.spool = self.case.root / 'spool'
        self.judge = {'schema': 'computecost_source_registry/v1', 'sources': [
            {'id': 'ownermac-observations', 'kind': KIND, 'peer': PEER, 'path': str(self.spool),
             'source_ids': [self.case.source['id']]}]}

    def observed(self):
        """What the observing node collects locally: ledger A and its coverage."""
        result = collect_registered_sources(self.case.registry, self.case.ledger, retirements={}, recoveries={})
        summary, = result['sources']
        coverage, = [r for r in self.case.ledger.receipt_snapshot()['facts'] if r.kind == 'coverage']
        return summary, coverage

    def bundle(self, summary, coverage, sequence=1, inputs=()):
        rows = read_cc(self.case.path, AT)
        return encode({'schema': SCHEMA, 'peer': PEER, 'sequence': sequence, 'created_at': AT,
            'sources': [{'config': bundle_config(self.case.source), 'rows': [r.to_dict() for r in rows],
                         'inventories': [], 'appended': [], 'coverage': {'status': summary['status'], 'reason': summary['reason'],
                             'expected': None, 'observed': None, 'watermark': coverage.payload['watermark'],
                             'raw_count': summary['fact_count'], 'detail': summary['source_detail'],
                             'diagnostics': summary['diagnostics']}}],
            'inputs': [r.to_dict() for r in inputs]})

    def test_judging_collector_reaches_the_observing_nodes_usage_and_coverage(self):
        summary, coverage = self.observed()
        data = self.bundle(summary, coverage)
        self.assertEqual(store(self.spool, PEER, data), 1)
        self.assertEqual(store(self.spool, PEER, data), 1)  # The same bundle again is one bundle.
        other = UsageLedger(self.case.root / 'judge')
        result = collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        self.assertEqual(heads(other, {'usage', 'coverage'}), heads(self.case.ledger, {'usage', 'coverage'}))
        self.assertEqual({s['kind'] for s in result['sources']}, {KIND, 'cc'})
        self.assertFalse(spool_path(self.spool, 1).exists())  # Ingested: it leaves the spool.
        self.assertEqual(collect_registered_sources(self.judge, other, retirements={}, recoveries={})['inserted'], 0)
        # Pushing the same rows again later changes nothing.
        store(self.spool, PEER, self.bundle(summary, coverage, sequence=2))
        self.assertEqual(collect_registered_sources(self.judge, other, retirements={}, recoveries={})['inserted'], 0)

    def test_bundle_stays_spooled_until_its_collection_is_ingested(self):
        summary, coverage = self.observed()
        store(self.spool, PEER, self.bundle(summary, coverage))
        other = UsageLedger(self.case.root / 'judge')
        with patch.object(UsageLedger, 'ingest_receipts', side_effect=LedgerError('offline')):
            with self.assertRaises(LedgerError):
                collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        self.assertTrue(spool_path(self.spool, 1).exists())

    def test_bundled_inputs_enter_the_stream_once(self):
        from test_native_inputs import NativeInputTests
        launches = NativeInputTests('test_merge_keeps_local_launches_and_adds_recorded_ones_once')
        launches.setUp(); self.addCleanup(launches.doCleanups)
        inputs = launch_inputs(launches.launches, source_ref=reference('source', 'cc'), observed=AT)
        summary, coverage = self.observed()
        store(self.spool, PEER, self.bundle(summary, coverage, inputs=inputs))
        other = UsageLedger(self.case.root / 'judge')
        collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        stored = [r for r in other.receipt_snapshot()['facts'] if r.kind == 'native_input']
        self.assertEqual([r.fact_ref for r in stored], [r.fact_ref for r in inputs])

    def test_request_inputs_in_a_bundle_are_judged_in_the_same_round(self):
        from test_request_inputs import PRODUCERS, claims, recorded_inputs
        from pulse_compute_cost import cc_request_purpose, mcp_request_purpose, native_request_purpose, pi_request_purpose
        with patch.object(cc_request_purpose, 'request_attributions', wraps=cc_request_purpose.request_attributions) as cc, \
             patch.object(mcp_request_purpose, 'session_attributions', wraps=mcp_request_purpose.session_attributions) as s, \
             patch.object(pi_request_purpose, 'request_attributions', wraps=pi_request_purpose.request_attributions) as pi, \
             patch.object(native_request_purpose, 'purpose_attributions', wraps=native_request_purpose.purpose_attributions) as codex:
            summary, coverage = self.observed()
        inputs = recorded_inputs((cc, s, pi, codex))
        self.assertTrue(inputs)
        store(self.spool, PEER, self.bundle(summary, coverage, inputs=inputs))
        other = UsageLedger(self.case.root / 'judge')
        collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        expected = claims(self.case.ledger)
        self.assertTrue(expected)
        self.assertEqual({e: p for e, (p, _) in claims(other).items()}, {e: p for e, (p, _) in expected.items()})

    def test_an_ingested_bundle_stays_known_by_sequence_and_digest(self):
        summary, coverage = self.observed()
        first, second = self.bundle(summary, coverage), self.bundle(summary, coverage, sequence=2)
        store(self.spool, PEER, first)
        other = UsageLedger(self.case.root / 'judge')
        collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        self.assertEqual(store(self.spool, PEER, first), 1)        # A retry of the same bundle: duplicate.
        self.assertFalse(spool_path(self.spool, 1).exists())       # ... and not read again.
        with self.assertRaises(ReceiptError):
            store(self.spool, PEER, self.bundle({**summary, 'status': 'partial'}, coverage))
        store(self.spool, PEER, second)
        collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        late = self.bundle({**summary, 'status': 'partial', 'reason': 'old_gap'}, coverage, sequence=1)
        with self.assertRaises(ReceiptError):
            store(self.spool, PEER, late)
        with self.assertRaises(ReceiptError):
            store(self.spool, PEER, self.bundle(summary, coverage, sequence=2)[:-1] + b'x')

    def test_concurrent_receivers_of_one_sequence_admit_exactly_one_content(self):
        import threading
        summary, coverage = self.observed()
        variants = [self.bundle({**summary, 'reason': f'variant-{i}'}, coverage) for i in range(8)]
        start, outcomes = threading.Barrier(len(variants)), []

        def receive(data):
            start.wait()
            try:
                store(self.spool, PEER, data); outcomes.append(data)
            except ReceiptError:
                outcomes.append(None)
        threads = [threading.Thread(target=receive, args=(v,)) for v in variants]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        admitted = [o for o in outcomes if o is not None]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(spool_path(self.spool, 1).read_bytes(), admitted[0])

    def test_an_unresolved_bundle_is_reported_until_resolved(self):
        summary, coverage = self.observed()
        self.spool.mkdir(mode=0o700)
        spool_path(self.spool, 1).write_bytes(b'damaged')
        os.chmod(spool_path(self.spool, 1), 0o600)
        store(self.spool, PEER, self.bundle(summary, coverage, sequence=2))
        other = UsageLedger(self.case.root / 'judge')
        for _ in range(2):  # Before and after the valid later bundle is consumed.
            push = next(s for s in collect_registered_sources(self.judge, other, retirements={}, recoveries={})['sources']
                        if s['kind'] == KIND)
            self.assertEqual((push['status'], push['reason']), ('partial', 'observation_bundle_invalid'))
        self.assertFalse(spool_path(self.spool, 2).exists())
        self.assertTrue(spool_path(self.spool, 1).exists())

    def test_read_marks_persist_between_judging_collections(self):
        from pulse_compute_cost.request_inputs import SESSION_DECLARATION, recorded, snapshot_inputs
        key = reference('native_session', ['cc', 'fixture'])

        def declared(wi, at):
            return snapshot_inputs(SESSION_DECLARATION, {key: [{'schema': 'cc_session_wi/v1', 'wi': wi,
                'origin': 'agent_declaration', 'native_scope_ref': key, 'declared_at': '2026-11-14T00:00:00Z',
                'native_evidence_ref': reference('fixture', wi)}]}, source_ref=reference('source', 'cc'), read_at=at)
        summary, coverage = self.observed()
        other = UsageLedger(self.case.root / 'judge')
        for sequence, (wi, at) in enumerate((('wi_20261113_754', '2026-11-15T00:00:00Z'),
                                             ('wi_20261113_754', '2026-11-15T02:00:00Z'),
                                             ('none', '2026-11-15T01:00:00Z')), start=1):
            store(self.spool, PEER, self.bundle(summary, coverage, sequence=sequence, inputs=declared(wi, at)))
            collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        found = recorded(other.receipt_snapshot()['facts'], SESSION_DECLARATION)
        self.assertEqual(found[key][0]['wi'], 'wi_20261113_754')

    def test_invalid_or_conflicting_bundles_are_refused(self):
        summary, coverage = self.observed()
        data = self.bundle(summary, coverage)
        with self.assertRaises(ReceiptError):
            decode(data, 'another-peer')
        document = json.loads(gzip.decompress(data))
        document['sources'][0]['config']['path'] = '/private/source'
        with self.assertRaises(ReceiptError):
            decode(gzip.compress(json.dumps(document).encode()), PEER)
        store(self.spool, PEER, data)
        with self.assertRaises(ReceiptError):
            store(self.spool, PEER, self.bundle({**summary, 'status': 'partial'}, coverage))
        # A bundle for a source the peer is not registered for is reported, not read.
        self.judge['sources'][0]['source_ids'] = []
        other = UsageLedger(self.case.root / 'judge')
        result = collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        push, = result['sources']
        self.assertEqual((push['status'], push['reason']), ('partial', 'observation_bundle_invalid'))
        self.assertTrue(spool_path(self.spool, 1).exists())

    def test_spool_must_be_private(self):
        self.spool.mkdir(mode=0o755); os.chmod(self.spool, 0o755)
        summary, coverage = self.observed()
        with self.assertRaises(ReceiptError):
            store(self.spool, PEER, self.bundle(summary, coverage))


if __name__ == '__main__':
    unittest.main()
