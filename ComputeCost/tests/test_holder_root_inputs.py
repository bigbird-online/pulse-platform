"""Holder sessions and Codex root links recorded by the node that holds the files."""
import os
import unittest

from pulse_compute_cost import codex_request_purpose, holder_supplement
from pulse_compute_cost.holder_supplement import read_session_writes, session_candidates
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.receipts import ReceiptError, current_receipts, reference
from pulse_compute_cost.request_inputs import (HOLDER_SESSION, ROOT_LINK, holder_values, recorded_root_links,
    root_values, snapshot_inputs)

READ_AT = '2026-11-15T00:00:00Z'


def heads(ledger, producer):
    active, conflicts = current_receipts(ledger.receipt_snapshot()['facts'])
    assert not conflicts
    return {r.entity_ref: (r.payload, r.evidence) for r in active if r.producer == producer}


def relay(test, registry, ledger, path, producer, inputs):
    """Ledger A without the producer's claims, plus inputs, judged by a collector with no sources."""
    kept = [r for r in ledger.receipt_snapshot()['facts'] if r.producer != producer]
    other = UsageLedger(path)
    other.ingest_receipts(kept + inputs)
    collect_registered_sources({**registry, 'sources': []}, other, retirements={}, recoveries={})
    expected = heads(ledger, producer)
    test.assertTrue(expected)
    test.assertEqual(heads(other, producer), expected)
    test.assertEqual(other.ingest_receipts(inputs)['inserted'], 0)
    return other


class HolderInputTests(unittest.TestCase):
    def setUp(self):
        from test_holder_supplement import CcRequestSupplementTests
        self.case = CcRequestSupplementTests('test_declaration_first_then_single_holder_then_ambiguous')
        self.case.setUp(); self.addCleanup(self.case.doCleanups)

    def test_recorded_candidates_meet_the_export_without_native_ids(self):
        from test_holder_supplement import OTHER_WI, SURFACE, THIRD_WI, WI
        case = self.case
        case.request('b', 10); case.held(OTHER_WI, 12)
        case.request('c', 20); case.held(WI, 22); case.held(THIRD_WI, 23)
        case.export(); case.collect()
        namespace, _, actors = read_session_writes(case.facts)
        candidates = session_candidates('cc', sorted(case.logs.rglob('*.jsonl')), namespace, actors, SURFACE)
        self.assertEqual(len(candidates), 1)
        inputs = snapshot_inputs(HOLDER_SESSION, holder_values(candidates),
                                 source_ref=reference('source', 'cc'), read_at=READ_AT)
        relay(self, case.registry, case.ledger, case.root / 'relay', holder_supplement.POLICY, inputs)

    def test_a_write_exported_after_the_session_settled_still_supplements(self):
        from test_holder_supplement import OTHER_WI, SURFACE
        case = self.case
        case.request('b', 10); case.export(); case.collect()
        namespace = 'pulse-production'
        candidates = session_candidates('cc', sorted(case.logs.rglob('*.jsonl')), namespace, {'pulse'}, SURFACE)
        inputs = snapshot_inputs(HOLDER_SESSION, holder_values(candidates),
                                 source_ref=reference('source', 'cc'), read_at=READ_AT)
        # The transcript no longer changes; only the core export learns the write.
        case.held(OTHER_WI, 12); case.export()
        other = UsageLedger(case.root / 'late')
        other.ingest_receipts(case.ledger.receipt_snapshot()['facts'] + inputs)
        collect_registered_sources({**case.registry, 'sources': []}, other, retirements={}, recoveries={})
        found = {p['target_ref']: (p['state'], p['wi']) for p, _ in heads(other, holder_supplement.POLICY).values()}
        self.assertEqual(found[case.target('b-before')], ('assigned', OTHER_WI))


    def test_a_session_read_here_is_never_overridden_by_a_recorded_snapshot(self):
        from test_holder_supplement import NAMESPACE, OTHER_WI, SURFACE, WI, facts_document
        case = self.case
        case.request('b', 10)
        old = session_candidates('cc', sorted(case.logs.rglob('*.jsonl')), NAMESPACE, {'pulse'}, SURFACE)
        case.ledger.ingest_receipts(snapshot_inputs(HOLDER_SESSION, holder_values(old),
                                                    source_ref=reference('source', 'cc'), read_at=READ_AT))
        # Two actors wrote from this one native session: ambiguous here, no supplement.
        document = facts_document({OTHER_WI: [(case.path.stem, '2026-01-01T01:12:40Z')]})
        other = facts_document({WI: [(case.path.stem, '2026-01-01T01:13:40Z')]})
        for write in other['items'][0]['session_writes']:
            write.update(actor='other', session=holder_supplement.session_key(NAMESPACE, 'other', case.path.stem, SURFACE))
        document['items'] += other['items']
        case.facts.write_text(__import__('json').dumps(document)); os.chmod(case.facts, 0o600)
        case.collect()
        self.assertEqual(heads(case.ledger, holder_supplement.POLICY), {})

    def test_malformed_holder_and_root_values_cannot_enter_the_stream(self):
        key = reference('native_session', ['cc', 'fixture'])
        for input_class, data in ((HOLDER_SESSION, {}), (HOLDER_SESSION, {'mode': 'x', 'starts': [], 'keys': []}),
                                  (HOLDER_SESSION, {'mode': 'session', 'starts': [READ_AT], 'keys': []}),
                                  (ROOT_LINK, {}), (ROOT_LINK, {'parent': key, 'child_scope': key,
                                                              'parent_scope': key, 'evidence': [key] * 3})):
            with self.subTest(input_class=input_class, data=data), self.assertRaises(ReceiptError):
                snapshot_inputs(input_class, {key: data}, source_ref=reference('source', 'cc'), read_at=READ_AT)


class RootLinkInputTests(unittest.TestCase):
    def setUp(self):
        from test_root_request_collector import RootCollectorTests
        self.case = RootCollectorTests('test_normal_full_collector_derives_once_and_keeps_old_watermark')
        self.case.setUp(); self.addCleanup(self.case.doCleanups)

    def inputs(self, source_id='codex'):
        links = codex_request_purpose.native_root_requests(sorted(self.case.logs.glob('*.jsonl')))
        self.assertEqual(len(links), 1)
        return snapshot_inputs(ROOT_LINK, root_values(links), source_ref=reference('source', source_id),
                               read_at=READ_AT)

    def test_recorded_link_derives_the_same_child_purpose(self):
        case = self.case
        case.collect()
        relay(self, case.registry, case.ledger, case.root / 'relay', codex_request_purpose.POLICY, self.inputs())

    def test_link_counts_only_when_both_current_usages_carry_its_source(self):
        case = self.case
        case.collect()
        facts = case.ledger.receipt_snapshot()['facts']
        active, _ = current_receipts(facts)
        self.assertEqual(len(recorded_root_links(self.inputs(), active, set())), 1)
        self.assertEqual(recorded_root_links(self.inputs('another-source'), active, set()), {})
        self.assertEqual(recorded_root_links(self.inputs(), active, {case.child.entity_ref}), {})


if __name__ == '__main__':
    unittest.main()
