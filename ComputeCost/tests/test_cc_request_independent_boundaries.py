"""Independent WI654 request-boundary checks; synthetic files in private temp dirs."""
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch
import unittest

import test_cc_request_purpose as fixture
from pulse_compute_cost import mcp_request_purpose as m
from pulse_compute_cost.cc_request_purpose import POLICY, request_bindings, request_attributions
from pulse_compute_cost.receipt_sources import read_cc
from pulse_compute_cost.receipts import Receipt, current_receipts

@contextmanager
def case():
    c = fixture.RequestPurposeTests('runTest')
    c.setUp()
    try:
        yield c
    finally:
        c.doCleanups()

class IndependentBoundaries(unittest.TestCase):
    def test_partial_legacy_proof_cannot_assign_a_new_undeclared_request(self):
        with case() as c:
            c.request('a', 0, fixture.WI, legacy=True)
            c.request('b', 10, fixture.WI, legacy=True)
            # A valid historical MCP ACK has no promptId. It proves a declared
            # WI, but cannot prove which request it belongs to.
            c.main[8].pop('promptId')
            c.request('c', 20)
            c.collect()
            active, _ = current_receipts(c.ledger.receipt_snapshot()['facts'])
            for name in ('c-before', 'c-after'):
                claims = [r for r in active if r.kind == 'attribution'
                          and r.payload['target_ref'] == c.target(name)]
                self.assertFalse(any(r.payload['state'] == 'assigned' and r.payload['wi'] == fixture.WI
                                     for r in claims), 'undeclared request inherited legacy session WI')

    def test_overlapping_request_clocks_follow_native_ancestry(self):
        with case() as c:
            c.request('a', 0, fixture.WI)
            c.request('b', 2, fixture.OTHER_WI)
            c.collect()
            states = c.states()
            self.assertEqual(states[c.target('a-after')], ('assigned', fixture.WI))
            self.assertEqual(states[c.target('b-before')], ('assigned', fixture.OTHER_WI))
            self.assertEqual(c.collect()['inserted'], 0)

    def test_late_declaration_does_not_backfill_a_prior_distinct_input(self):
        with case() as c:
            c.request('unclassified', 0)
            c.request('declared', 10, fixture.WI)
            c.collect()
            states = c.states()
            self.assertEqual(states[c.target('unclassified-before')], ('unassigned', None))
            self.assertEqual(states[c.target('unclassified-after')], ('unassigned', None))
            self.assertEqual(states[c.target('declared-before')], ('assigned', fixture.WI))

    def test_same_producer_concurrent_request_claims_are_not_retracted(self):
        with case() as c:
            c.request('a', 0, fixture.WI)
            c.collect()
            facts = c.ledger.receipt_snapshot()['facts']
            original = next(r for r in facts if r.kind == 'attribution' and r.producer == POLICY
                            and r.payload['target_ref'] == c.target('a-before'))
            payload = {**original.payload, 'wi': fixture.OTHER_WI}
            contrary = Receipt.make('attribution', None, payload, entity_ref=original.entity_ref,
                producer=POLICY, occurred_at=original.occurred_at, observed_at=fixture.OBSERVED,
                evidence=original.evidence)
            raw = read_cc(c.path, fixture.OBSERVED)
            ds = m.cc_native_completions([c.path], raw, [], since=fixture.BEFORE)
            bindings = request_bindings([c.path], raw, ds)
            out = request_attributions(facts + [contrary], raw, bindings, ds, fixture.OBSERVED)
            self.assertFalse([r for r in out if r.payload['target_ref'] == c.target('a-before')])
            _, conflicts = current_receipts(facts + [contrary])
            self.assertEqual(len(conflicts[original.entity_ref]), 2)

    def test_legacy_revision_requires_complete_same_producer_evidence(self):
        for fault in ('foreign_producer', 'missing_usage_witness', 'missing_declaration_proof',
                      'invented_boundary', 'nonempty_executor'):
            with self.subTest(fault=fault), case() as c:
                c.request('a', 0, fixture.WI, legacy=True)
                c.request('b', 10, fixture.OTHER_WI, legacy=True)
                with patch('pulse_compute_cost.cc_request_purpose.request_bindings', return_value={}):
                    c.collect()
                facts = c.ledger.receipt_snapshot()['facts']
                target = c.target('a-before')
                old = next(r for r in facts if r.kind == 'attribution'
                           and r.producer == m.POLICY and r.payload['target_ref'] == target)
                raw = read_cc(c.path, fixture.OBSERVED)
                ds = m.cc_native_completions([c.path], raw, [], since=fixture.BEFORE)
                bindings = request_bindings([c.path], raw, ds)
                p, evidence, producer = deepcopy(old.payload), list(old.evidence), old.producer
                from pulse_compute_cost.receipts import reference
                witness = reference('native_session_usage', [raw[0].payload['native_scope_ref'], target])
                native_proofs = {reference('native_session_wi', [scope, d['wi'], d['native_evidence_ref']])
                                 for scope, declarations in ds.items() for d in declarations}
                if fault == 'foreign_producer': producer = 'independent_review.v1'
                elif fault == 'missing_usage_witness': evidence = [e for e in evidence if e != witness]
                elif fault == 'missing_declaration_proof': evidence = [e for e in evidence if e not in native_proofs]
                elif fault == 'invented_boundary': p['effective_at'] = '2026-01-01T01:59:00Z'
                else: p['executor'] = 'other-agent'
                broken = Receipt.make('attribution', None, p, entity_ref=old.entity_ref,
                    producer=producer, occurred_at=p['effective_at'], observed_at=fixture.OBSERVED,
                    evidence=tuple(evidence))
                altered = [r for r in facts if r.fact_ref != old.fact_ref] + [broken]
                out = request_attributions(altered, raw, bindings, ds, fixture.OBSERVED)
                self.assertFalse([r for r in out if r.payload['target_ref'] == target])

if __name__ == '__main__':
    unittest.main()
