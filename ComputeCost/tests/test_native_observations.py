"""A pushed observation bundle is judged like a source the collector read itself."""
import gzip
import json
import os
import unittest
from unittest.mock import patch

from pulse_compute_cost.ledger import LedgerError, UsageLedger
from pulse_compute_cost.native_inputs import launch_inputs
from pulse_compute_cost.native_observations import (KIND, SCHEMA, bundle_config, decode, encode, pushed_core_marker,
                                                     spool_path, store)
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

    def test_a_backlog_is_judged_in_bounded_collections_in_sequence_order(self):
        from pulse_compute_cost import native_observations
        summary, coverage = self.observed()
        data = [self.bundle(summary, coverage, sequence=i) for i in (1, 2, 3)]
        for item in data:
            store(self.spool, PEER, item)
        size = int.from_bytes(data[0][-4:], 'little')
        self.assertEqual(size, len(gzip.decompress(data[0])))
        other = UsageLedger(self.case.root / 'judge')

        def push():
            return next(s for s in collect_registered_sources(self.judge, other, retirements={}, recoveries={})['sources']
                        if s['kind'] == KIND)
        # Room for one bundle and a half: the first is read, the later ones wait.
        with patch.object(native_observations, 'MAX_COLLECTION_DOCUMENT_BYTES', size + size // 2):
            first = push()
            self.assertEqual((first['status'], first['reason']), ('partial', 'observation_backlog'))
            self.assertEqual((first['source_detail']['bundles'], first['source_detail']['deferred_bundles']), (1, 2))
            self.assertEqual([spool_path(self.spool, i).exists() for i in (1, 2, 3)], [False, True, True])
            self.assertEqual(heads(other, {'usage'}), heads(self.case.ledger, {'usage'}))
            push()
            self.assertEqual([spool_path(self.spool, i).exists() for i in (2, 3)], [False, True])
        # Even a budget below one bundle reads one, so the spool always advances.
        with patch.object(native_observations, 'MAX_COLLECTION_DOCUMENT_BYTES', 1):
            last = push()
        self.assertEqual((last['status'], last['reason']), ('complete', None))
        self.assertNotIn('deferred_bundles', last['source_detail'])
        self.assertFalse(spool_path(self.spool, 3).exists())

    def test_a_round_may_read_fewer_bundles_and_the_rest_wait(self):
        from pulse_compute_cost.native_observations import expand_pushed
        summary, coverage = self.observed()
        for i in (1, 2, 3):
            store(self.spool, PEER, self.bundle(summary, coverage, sequence=i))
        source = self.judge['sources'][0]['id']

        def read(**kwargs):
            pushed, = expand_pushed(self.judge['sources'], {}, **kwargs)['summaries']
            return pushed['source_detail']['bundles'], pushed['source_detail'].get('deferred_bundles', 0)
        self.assertEqual(read(limits={source: 2}), (2, 1))
        seen = []
        # A visitor refusing every bundle still gets the first: the spool always advances.
        self.assertEqual(read(visit=lambda i, document: seen.append(document['sequence'])), (1, 2))
        self.assertEqual(seen, [1, 2])

    def test_a_split_backlog_judges_a_pushed_outcome_as_one_collection_does(self):
        from pulse_compute_cost import native_observations
        from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
        from test_kernel_outcomes import completion, facts, item
        q = CollaborationQuery(scope='activity', from_date='2026-10-19', to_date='2026-10-20')

        def judged(budget):
            self.setUp()
            summary, coverage = self.observed()
            self.judge_with_local_core(None)  # The judge's own export is missing.
            data = [self.core_bundle(summary, coverage, facts(item(completions=[completion(title=t)]),
                                     generated_at=AT), sequence=i)
                    for i, t in enumerate(('first copy', 'later copy'), start=1)]
            for item_data in data:
                store(self.spool, PEER, item_data)
            other = UsageLedger(self.case.root / 'judge')
            rounds = 0
            with patch.object(native_observations, 'MAX_COLLECTION_DOCUMENT_BYTES', budget):
                while list(self.spool.glob('*.json.gz')):
                    collect_registered_sources(self.judge, other, retirements={}, recoveries={})
                    rounds += 1
            _, conflicts = current_receipts(other.receipt_snapshot()['facts'])
            self.assertFalse(conflicts)
            items = query_collaboration(other, q)['summary']['outcomes']['items']
            return rounds, [i['title'] for i in items]
        self.assertEqual(judged(1 << 40), (1, ['later copy']))
        self.assertEqual(judged(1), (2, ['later copy']))

    def test_a_multi_member_bundle_is_sized_by_all_its_members(self):
        from pulse_compute_cost import native_observations
        summary, coverage = self.observed()
        for i in (1, 2):
            raw = gzip.decompress(self.bundle(summary, coverage, sequence=i))
            # Two members: the trailer names only the small last one.
            data = gzip.compress(raw[:-8], mtime=0) + gzip.compress(raw[-8:], mtime=0)
            self.assertEqual(decode(data, PEER)['sequence'], i)
            self.assertEqual(int.from_bytes(data[-4:], 'little'), 8)
            store(self.spool, PEER, data)
        other = UsageLedger(self.case.root / 'judge')
        with patch.object(native_observations, 'MAX_COLLECTION_DOCUMENT_BYTES', len(raw) + len(raw) // 2):
            push = next(s for s in collect_registered_sources(self.judge, other, retirements={}, recoveries={})['sources']
                        if s['kind'] == KIND)
        self.assertEqual((push['source_detail']['bundles'], push['source_detail']['deferred_bundles']), (1, 1))
        self.assertTrue(spool_path(self.spool, 2).exists())

    def test_an_invalid_bundle_and_a_backlog_are_both_reported(self):
        from pulse_compute_cost import native_observations
        from pulse_compute_cost.stats_dashboard import collection_backlog
        summary, coverage = self.observed()
        self.spool.mkdir(mode=0o700)
        spool_path(self.spool, 1).write_bytes(b'damaged')
        os.chmod(spool_path(self.spool, 1), 0o600)
        for i in (2, 3, 4):
            store(self.spool, PEER, self.bundle(summary, coverage, sequence=i))
        other = UsageLedger(self.case.root / 'judge')
        with patch.object(native_observations, 'MAX_COLLECTION_DOCUMENT_BYTES', 1):
            collected = collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        push = next(s for s in collected['sources'] if s['kind'] == KIND)
        self.assertEqual((push['status'], push['reason']), ('partial', 'observation_bundle_invalid'))
        self.assertEqual((push['source_detail']['invalid_bundles'], push['source_detail']['deferred_bundles']), (1, 2))
        self.assertEqual(collection_backlog(self.judge, collected),
                         {'status': 'partial', 'reason': 'observation_backlog',
                          'peers': [{'id': PEER, 'deferred_bundles': 2}]})

    def test_rows_of_one_source_across_bundles_are_all_judged(self):
        from pulse_compute_cost.native_observations import expand_pushed
        summary, coverage = self.observed()
        for i in (1, 2, 3):
            store(self.spool, PEER, self.bundle(summary, coverage, sequence=i))
        expanded = expand_pushed(self.judge['sources'], {})
        source, = expanded['sources']
        self.assertEqual(len(source['_pushed']['rows']), 3 * len(read_cc(self.case.path, AT)))
        self.assertEqual(len(expanded['paths']), 3)

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

    def core_bundle(self, summary, coverage, core_document, sequence=1, kind='harness_core'):
        """The observing node's bundle with its CC source and its copy of the core facts source."""
        from pulse_compute_cost.kernel_outcomes import read_kernel_outcomes
        from pulse_compute_cost.source_layout import source_detail
        core = {'id': 'harness-core-outcomes', 'kind': kind, 'label': 'ownermac.harness_core'}
        rows, detail = [], source_detail({**core, 'kind': 'harness_core'})
        status, reason, expected, observed = read_kernel_outcomes(
            self.case.root / 'pushed-facts.json', json.dumps(core_document).encode(), AT, rows, detail)
        document = json.loads(gzip.decompress(self.bundle(summary, coverage, sequence=sequence)))
        document['sources'].append({'config': bundle_config(core), 'rows': [r.to_dict() for r in rows],
            'inventories': [], 'appended': [], 'coverage': {'status': status, 'reason': reason, 'expected': expected,
                'observed': observed, 'watermark': 'ownermac-core-snapshot', 'raw_count': len(rows), 'detail': detail,
                'diagnostics': {}}})
        return encode(document)

    def judge_with_local_core(self, document=None):
        """The judge registers its own core facts source; ``document`` None leaves its file missing."""
        local = self.case.root / 'wi-facts-v1.json'
        if document is not None:
            local.write_text(json.dumps(document))
        self.judge['sources'][0]['source_ids'].append('harness-core-outcomes')
        self.judge['sources'].append({'id': 'harness-core-outcomes', 'kind': 'harness_core', 'path': str(local),
                                      'label': 'oracle-node-1.harness_core'})

    def judged(self):
        other = UsageLedger(self.case.root / 'judge')
        result = collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        by_kind = {s['kind']: s for s in result['sources']}
        self.assertEqual(set(by_kind), {KIND, 'cc', 'harness_core'})
        self.assertEqual(by_kind[KIND]['source_detail']['merged_into_local'], ['harness-core-outcomes'])
        self.assertFalse(spool_path(self.spool, 1).exists())  # Consumed: every row it carried is in the stream.
        deliveries = {r.entity_ref: r for r in other.receipt_snapshot()['facts'] if r.kind == 'delivery'}
        core_ref = reference('source', 'harness-core-outcomes')
        coverage = [p['status'] for p in heads(other, {'coverage'}).values() if p.get('source_ref') == core_ref]
        return deliveries, coverage

    def test_the_judges_own_core_export_decides_an_outcome_both_copies_hold(self):
        from test_kernel_outcomes import completion, facts, item
        summary, coverage = self.observed()
        self.judge_with_local_core(facts(item(completions=[completion()]), generated_at=AT))
        # The observing node's copy names the same completion with another title.
        store(self.spool, PEER, self.core_bundle(summary, coverage,
              facts(item(completions=[completion(title='older title')]), generated_at=AT)))
        deliveries, core_coverage = self.judged()
        self.assertEqual([d.payload['outcome_context']['title'] for d in deliveries.values()], ['日推与伴读'])
        self.assertEqual(core_coverage, ['complete'])
        self.assertEqual(heads(UsageLedger(self.case.root / 'judge'), {'usage'}), heads(self.case.ledger, {'usage'}))

    def test_a_pushed_outcome_the_judges_export_lacks_still_enters(self):
        from pulse_compute_cost.native_observations import pushed_fill_marker
        from test_kernel_outcomes import completion, facts, item
        from pulse_compute_cost.kernel_outcomes import completion_receipt
        summary, coverage = self.observed()
        # The judge's own export is valid but older: it does not hold revision 40 yet.
        self.judge_with_local_core(facts(item(completions=[completion()]), generated_at=AT))
        store(self.spool, PEER, self.core_bundle(summary, coverage,
              facts(item(completions=[completion(), completion(revision=40)]), generated_at=AT)))
        deliveries, core_coverage = self.judged()
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(core_coverage, ['complete'])
        # Its snapshot witness is the bundle's, not the judge's file.
        own = reference('source_snapshot', [reference('source', 'harness-core-outcomes'), 'ownermac-core-snapshot'])
        witnessed = [d for d in deliveries.values() if own in d.evidence]
        self.assertEqual(len(witnessed), 1)
        original = completion_receipt('pulse-production', item(), completion(revision=40), AT)
        self.assertEqual(witnessed[0].entity_ref, original.entity_ref)
        self.assertEqual(set(witnessed[0].evidence),
                         set(original.evidence) | {reference('source', 'harness-core-outcomes'), own,
                                                   pushed_core_marker(reference('source', 'harness-core-outcomes')),
                                                   pushed_fill_marker(reference('source', 'harness-core-outcomes'))})

    def test_a_pushed_outcome_enters_while_the_judges_export_is_missing(self):
        from test_kernel_outcomes import completion, facts, item
        summary, coverage = self.observed()
        self.judge_with_local_core(None)
        store(self.spool, PEER, self.core_bundle(summary, coverage, facts(item(completions=[completion()]), generated_at=AT)))
        deliveries, core_coverage = self.judged()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(core_coverage, ['unreachable'])  # Coverage stays the judge's own read.

    def outcome_rounds(self, steps, enroll_at=0, seed=None):
        """Judge collections over one ledger: each step sets the judge's own export
        (None: the file is missing) and pushes the observing node's copy (None: no bundle).
        The judge registers its own core source from step ``enroll_at`` on; before
        it the pushed copy is judged as the only core source.
        ``seed`` titles a pushed fill already in the stream from before fills were
        marked as such (it carries only the pushed-row marker).
        Returns the Outcome view after each step as (status, count, title)."""
        from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
        from test_kernel_outcomes import completion, facts, item
        summary, coverage = self.observed()
        self.judge['sources'][0]['source_ids'].append('harness-core-outcomes')
        local = self.case.root / 'wi-facts-v1.json'
        other = UsageLedger(self.case.root / 'judge')
        if seed is not None:
            from dataclasses import replace
            from pulse_compute_cost.kernel_outcomes import completion_receipt
            core = reference('source', 'harness-core-outcomes')
            row = completion_receipt('pulse-production', item(), completion(title=seed), AT)
            other.ingest_receipts([replace(row, evidence=tuple(sorted(set(row.evidence) | {
                core, pushed_core_marker(core)})))])
        q = CollaborationQuery(scope='activity', from_date='2026-10-19', to_date='2026-10-20')
        views, sequence = [], 0
        for step, (own, copy) in enumerate(steps):
            if step == enroll_at:
                self.judge['sources'].append({'id': 'harness-core-outcomes', 'kind': 'harness_core',
                                              'path': str(local), 'label': 'oracle-node-1.harness_core'})
            if own is None:
                local.unlink(missing_ok=True)
            else:
                local.write_text(json.dumps(facts(item(completions=[completion(title=own)]), generated_at=AT)))
            if copy is not None:
                sequence += 1
                store(self.spool, PEER, self.core_bundle(summary, coverage,
                      facts(item(completions=[completion(title=copy)]), generated_at=AT), sequence=sequence))
            collect_registered_sources(self.judge, other, retirements={}, recoveries={})
            _, conflicts = current_receipts(other.receipt_snapshot()['facts'])
            self.assertFalse([e for e, rows in conflicts.items() if rows[0].kind == 'delivery'])
            outcomes = query_collaboration(other, q)['summary']['outcomes']
            views.append((outcomes['status'], outcomes['count'],
                          outcomes['items'][0]['title'] if outcomes['items'] else None))
        self.assertFalse(list(self.spool.glob('*.json.gz')))  # Every bundle was consumed.
        return views

    def test_the_judges_export_replaces_a_pushed_outcome_when_it_returns(self):
        # (a) The judge's export is missing, the pushed row enters; the export
        # returns with another title for the same completion revision.
        self.assertEqual(self.outcome_rounds([(None, 'pushed'), ('own', None), ('own', None)]),
                         [('observed', 1, 'pushed'), ('observed', 1, 'own'), ('observed', 1, 'own')])

    def test_a_pushed_copy_never_versions_an_outcome_the_judges_export_supplied(self):
        # (b) The judge's export enters first; while it is missing another title arrives.
        self.assertEqual(self.outcome_rounds([('own', None), (None, 'pushed'), ('own', None)]),
                         [('observed', 1, 'own')] * 3)

    def test_a_pushed_outcome_stays_until_the_judges_export_and_then_stays_the_exports(self):
        # A later pushed copy versions a row only pushes filled; once the judge's
        # export supplied it, its missing and returning again keeps its own version.
        self.assertEqual(self.outcome_rounds([
            (None, 'first copy'), (None, 'second copy'), ('own', 'third copy'), (None, 'fourth copy'),
            ('own', None), (None, 'fifth copy'), ('own', 'sixth copy')]),
            [('observed', 1, 'first copy'), ('observed', 1, 'second copy')] + [('observed', 1, 'own')] * 5)

    def test_the_judges_newly_enrolled_export_replaces_outcomes_judged_from_the_push_alone(self):
        # The pushed copy is the only core source first; then the judge enrolls its own.
        self.assertEqual(self.outcome_rounds([(None, 'pushed'), ('own', None)], enroll_at=1),
                         [('observed', 1, 'pushed'), ('observed', 1, 'own')])

    def test_after_enrolling_its_own_export_the_judge_keeps_its_version_through_every_order(self):
        self.assertEqual(self.outcome_rounds([
            (None, 'pushed'), ('own', 'pushed'), (None, 'another copy'), ('own', None)], enroll_at=1),
            [('observed', 1, 'pushed')] + [('observed', 1, 'own')] * 3)
        # Enrolled while its export is still missing: a row judged from the push alone,
        # not filled beside the export, is not provably unconfirmed, so it stays until the export arrives.
        self.setUp()
        self.assertEqual(self.outcome_rounds([(None, 'pushed'), (None, 'another copy'), ('own', 'third')],
                                             enroll_at=1),
                         [('observed', 1, 'pushed')] * 2 + [('observed', 1, 'own')])

    def test_a_fill_from_before_the_upgrade_is_never_replaced_by_a_push(self):
        # It may have been confirmed by the judge's export without a record of that.
        self.assertEqual(self.outcome_rounds([(None, 'pushed copy'), (None, 'another copy')], seed='old fill'),
                         [('observed', 1, 'old fill')] * 2)
        self.setUp()
        self.assertEqual(self.outcome_rounds([('old fill', None), (None, 'pushed copy')], seed='old fill'),
                         [('observed', 1, 'old fill')] * 2)
        # The judge's export still decides it.
        self.setUp()
        self.assertEqual(self.outcome_rounds([(None, 'pushed copy'), ('own', None)], seed='old fill'),
                         [('observed', 1, 'old fill'), ('observed', 1, 'own')])

    def test_pushed_core_rows_are_matched_one_lookup_each_and_skipped_without_rows(self):
        from pulse_compute_cost import native_observations
        from pulse_compute_cost.native_observations import merge_pushed_core
        from test_kernel_outcomes import completion, facts, item
        from pulse_compute_cost.kernel_outcomes import completion_receipt
        rows = [completion_receipt('pulse-production', item(), completion(revision=r), AT) for r in (22, 40, 41)]
        asked = []
        kept = merge_pushed_core(reference('source', 'core'), rows[:1],
                                 lambda entity: asked.append(entity) or entity == rows[1].entity_ref,
                                 [(r, 'w') for r in rows])
        # The local row is not looked up; each other pushed row is asked once, and no key set is copied.
        self.assertEqual(asked, [rows[1].entity_ref, rows[2].entity_ref])
        self.assertEqual([r.entity_ref for r, _ in kept], [rows[2].entity_ref])
        # A bundle whose core facts source carries no rows never reaches the merge.
        summary, coverage = self.observed()
        self.judge_with_local_core(facts(item(completions=[completion()]), generated_at=AT))
        store(self.spool, PEER, self.core_bundle(summary, coverage, facts(item(), generated_at=AT)))
        with patch.object(native_observations, 'merge_pushed_core', side_effect=AssertionError('merged')):
            deliveries, core_coverage = self.judged()
        self.assertEqual((len(deliveries), core_coverage), (1, ['complete']))

    def test_a_pushed_outcome_the_judges_export_confirms_then_changes_is_the_exports(self):
        self.assertEqual(self.outcome_rounds([(None, 'same'), ('same', None), (None, 'pushed'), ('changed', None)]),
                         [('observed', 1, 'same')] * 3 + [('observed', 1, 'changed')])

    def test_a_pushed_core_source_without_a_local_one_is_judged(self):
        from test_kernel_outcomes import completion, facts, item
        summary, coverage = self.observed()
        self.judge['sources'][0]['source_ids'].append('harness-core-outcomes')
        store(self.spool, PEER, self.core_bundle(summary, coverage, facts(item(completions=[completion()]), generated_at=AT)))
        other = UsageLedger(self.case.root / 'judge')
        result = collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        push = next(s for s in result['sources'] if s['kind'] == KIND)
        self.assertEqual(push['source_detail']['merged_into_local'], [])
        self.assertEqual(sum(r.kind == 'delivery' for r in other.receipt_snapshot()['facts']), 1)

    def test_a_bundle_that_changes_a_sources_kind_is_not_read(self):
        from test_kernel_outcomes import facts, item
        summary, coverage = self.observed()
        self.judge_with_local_core(facts(item(), generated_at=AT))
        # The first bundle names the ID as a CC source, the second as the core facts source.
        first = json.loads(gzip.decompress(self.bundle(summary, coverage)))
        first['sources'][0]['config']['id'] = 'harness-core-outcomes'
        store(self.spool, PEER, encode(first))
        second = json.loads(gzip.decompress(self.core_bundle(summary, coverage, facts(item(), generated_at=AT), sequence=2)))
        second['sources'] = second['sources'][1:]
        store(self.spool, PEER, encode(second))
        other = UsageLedger(self.case.root / 'judge')
        with self.assertRaisesRegex(ReceiptError, 'source IDs must be present and unique'):
            collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        self.assertTrue(spool_path(self.spool, 1).exists() and spool_path(self.spool, 2).exists())
        # Without the clash with the judge's own source, the second bundle alone is refused.
        self.judge['sources'].pop()
        result = collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        push = next(s for s in result['sources'] if s['kind'] == KIND)
        self.assertEqual((push['status'], push['reason'], push['source_detail']['invalid_bundles']),
                         ('partial', 'observation_bundle_invalid', 1))
        self.assertFalse(spool_path(self.spool, 1).exists())
        self.assertTrue(spool_path(self.spool, 2).exists())

    def test_a_pushed_source_sharing_another_registered_id_is_refused(self):
        summary, coverage = self.observed()
        store(self.spool, PEER, self.bundle(summary, coverage))
        self.judge['sources'].append(dict(self.case.source))  # The collector reads a CC source of this ID itself.
        other = UsageLedger(self.case.root / 'judge')
        with self.assertRaisesRegex(ReceiptError, 'source IDs must be present and unique'):
            collect_registered_sources(self.judge, other, retirements={}, recoveries={})
        self.assertTrue(spool_path(self.spool, 1).exists())

    def test_spool_must_be_private(self):
        self.spool.mkdir(mode=0o755); os.chmod(self.spool, 0o755)
        summary, coverage = self.observed()
        with self.assertRaises(ReceiptError):
            store(self.spool, PEER, self.bundle(summary, coverage))


if __name__ == '__main__':
    unittest.main()
