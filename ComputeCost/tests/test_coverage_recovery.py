"""A declared recovery makes the source's fresh snapshot the only successor of a coverage fork."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.coverage_recovery import coverage_entity, declaration_ref, load_recoveries
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources
from pulse_compute_cost.receipts import Receipt, ReceiptError, current_receipts, reference

AT = '2026-10-05T12:00:00Z'
AUTHORIZATION = '49c1b0418f642161f3bdf9c728fe2bc0133418b4b4ae3368a0301071246f1a2f'
SOURCE = reference('source', 'isolated-pi')


def coverage(watermark, status='partial', supersedes=None):
    return Receipt.make('coverage', [SOURCE, 'usage', 'global'], {
        'domain': 'usage', 'wi': None, 'source_ref': SOURCE, 'status': status,
        'expected': None, 'observed': None, 'watermark': watermark,
        'reason': 'synthetic-source-gap', 'source_detail': None},
        occurred_at=None, observed_at=AT, producer='registered_collector.v1', supersedes=supersedes)


def declaration(*abandoned):
    return {'id': 'isolated-pi', 'domain': 'usage', 'wi': None, 'abandoned_heads': [r.fact_ref for r in abandoned],
            'recovered_on': '2026-10-21', 'authorization': AUTHORIZATION}


class CoverageRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        sessions = root / 'sessions'; sessions.mkdir(mode=0o700)
        (sessions / 'session.jsonl').write_text(json.dumps(
            {'type': 'session', 'id': 'synthetic', 'timestamp': AT, 'version': 3}) + '\n')
        self.ledger = UsageLedger(root / 'ledger')
        self.registry = {'schema': REGISTRY_SCHEMA, 'sources': [{'id': 'isolated-pi', 'kind': 'pi', 'path': str(sessions)}]}
        self.root = coverage('root', 'idle')
        # The source's own chain, and a rebased copy of the same history.
        self.own = coverage('own', 'unknown', self.root.fact_ref)
        self.copy = coverage('copy', 'partial', coverage('rebased', 'idle', self.root.fact_ref).fact_ref)
        self.ledger.ingest_receipts([self.root, self.own, coverage('rebased', 'idle', self.root.fact_ref)])
        self.ledger.ingest_receipts([self.copy])
        self.entity = self.root.entity_ref
        self.assertEqual(self.entity, coverage_entity('isolated-pi', 'usage', None))

    def collect(self, *entries):
        recoveries = {coverage_entity(e['id'], e['domain'], e['wi']): e for e in entries}
        return collect_registered_sources(self.registry, self.ledger, retirements={}, recoveries=recoveries)

    def test_fresh_snapshot_succeeds_remaining_head_and_names_declared_head(self):
        before = self.ledger.receipt_snapshot()
        report = query_collaboration(self.ledger, CollaborationQuery())
        self.assertIn('execution_evidence_conflict', report['summary']['cost']['reasons'])
        entry = declaration(self.copy)
        result = self.collect(entry)
        self.assertEqual(result['coverage_revision_conflicts'], [])
        self.assertEqual(result['recovered_coverage_entities'], [self.entity])
        after = self.ledger.receipt_snapshot()
        self.assertEqual(after['facts'][:len(before['facts'])], before['facts'])  # Append only.
        active, conflicts = current_receipts(after['facts'])
        self.assertNotIn(self.entity, conflicts)
        [head] = [r for r in active if r.entity_ref == self.entity]
        self.assertEqual(head.producer, 'registered_collector.v1')
        self.assertEqual(head.supersedes, self.own.fact_ref)
        self.assertEqual(set(head.evidence), {self.copy.fact_ref, declaration_ref(entry)})
        self.assertEqual(head.payload['status'], result['sources'][0]['status'])
        report = query_collaboration(self.ledger, CollaborationQuery())
        self.assertNotIn('execution_evidence_conflict', report['summary']['cost']['reasons'])
        self.assertIn(SOURCE, {d['source_ref'] for d in report['summary']['usage']['source_details']})
        # Old as-of reads keep their fork; an unchanged recollection adds nothing.
        self.assertEqual(self.ledger.receipt_snapshot(watermark=before['watermark'])['sha256'], before['sha256'])
        self.assertEqual(self.collect(entry)['inserted'], 0)

    def test_undeclared_or_unmatched_heads_still_require_recovery(self):
        other = coverage('other', 'unknown', self.root.fact_ref)
        self.ledger.ingest_receipts([other])
        for entries in ((declaration(self.copy),), (declaration(coverage('absent')),), ()):
            with self.subTest(entries=len(entries)):
                versions = lambda: [r for r in self.ledger.receipt_snapshot()['facts'] if r.entity_ref == self.entity]
                before = versions()
                result = self.collect(*entries)
                self.assertEqual(result['recovered_coverage_entities'], [])
                [diagnostic] = result['coverage_revision_conflicts']
                self.assertEqual(diagnostic['head_fact_refs'], sorted([self.own.fact_ref, self.copy.fact_ref, other.fact_ref]))
                self.assertTrue(result['sources'][0]['coverage_recovery_required'])
                self.assertEqual(versions(), before)

    def test_head_appended_after_recovery_conflicts_again(self):
        entry = declaration(self.copy)
        self.collect(entry)
        late = coverage('late', 'unknown', self.own.fact_ref)
        self.ledger.ingest_receipts([late])
        result = self.collect(entry)
        self.assertEqual(result['recovered_coverage_entities'], [])
        self.assertEqual(len(result['coverage_revision_conflicts']), 1)


class RecoveryDeclarationTests(unittest.TestCase):
    def test_repository_declaration_names_the_tcloud_relay_copy_head(self):
        declared = load_recoveries()
        entity = coverage_entity('tcloud-relay', 'usage', None)
        self.assertEqual(list(declared), [entity])
        self.assertEqual(declared[entity]['abandoned_heads'],
                         ['ccf1_2e15744f64d8f743a1e9dffc3afc51f30d314e30c5bc10e7f9b26b1bae526b32'])
        self.assertEqual(declared[entity]['authorization'], AUTHORIZATION)

    def test_malformed_declaration_is_rejected(self):
        entry = declaration(coverage('copy'))
        document = {'schema': 'computecost_coverage_recoveries/v1', 'entities': [entry]}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'recoveries.json'
            path.write_text(json.dumps(document))
            self.assertEqual(list(load_recoveries(path)), [coverage_entity('isolated-pi', 'usage', None)])
            for broken in ({**document, 'schema': 'other'},
                           {**document, 'entities': [entry, entry]},
                           {**document, 'entities': [{**entry, 'authorization': 'operator'}]},
                           {**document, 'entities': [{**entry, 'abandoned_heads': []}]},
                           {**document, 'entities': [{**entry, 'abandoned_heads': ['head']}]},
                           {**document, 'entities': [{**entry, 'wi': ''}]},
                           {**document, 'entities': [{**entry, 'recovered_on': 'today'}]},
                           {**document, 'entities': [{**entry, 'extra': 1}]}):
                with self.subTest(broken=broken):
                    path.write_text(json.dumps(broken))
                    with self.assertRaises((ReceiptError, ValueError)):
                        load_recoveries(path)


if __name__ == '__main__':
    unittest.main()
