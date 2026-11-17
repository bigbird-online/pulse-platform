"""A declared retirement is the only successor of a source's coverage heads."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pulse_compute_cost.collaboration_query import CollaborationQuery, _qualified_collector_sources, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources
from pulse_compute_cost.receipts import Receipt, ReceiptError, current_receipts, reference
from pulse_compute_cost.source_retirement import declaration_ref, load_retirements
from pulse_compute_cost import source_registration

AT = '2026-10-05T12:00:00Z'
AUTHORIZATION = '5c2cc85c10dfc238806e51f01eaf20207a2d2f8484d34b07a1c46f1bdcdeb3c3'
RETIRED = {'old-harness': {'id': 'old-harness', 'kind': 'harness_root', 'retired_on': '2026-10-21',
                           'authorization': AUTHORIZATION}}
SOURCE = reference('source', 'old-harness')


def coverage(label, reason, *, domain='execution', supersedes=None):
    detail = dict(label=label, kind='harness_root', layout='registered_recursive.v1', files_seen=1,
                  files_read=1, files_invalid=0, excluded_directories=0, latest_usage_at=None, errors={})
    return Receipt.make('coverage', [SOURCE, domain, 'global'], dict(domain=domain, wi=None, source_ref=SOURCE,
        status='unknown', expected=None, observed=None, watermark=None, reason=reason, source_detail=detail),
        occurred_at=None, observed_at=AT, producer='registered_collector.v1', supersedes=supersedes)


class SourceRetirementTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ledger = UsageLedger(self.root / 'ledger')
        sessions = self.root / 'sessions'; sessions.mkdir(mode=0o700)
        (sessions / 'session.jsonl').write_text(json.dumps(
            {'type': 'session', 'id': 'synthetic', 'timestamp': AT, 'version': 3}) + '\n')
        self.pi = {'id': 'live-pi', 'kind': 'pi', 'path': str(sessions)}
        self.registry = {'schema': REGISTRY_SCHEMA, 'sources': [self.pi]}
        # Two nodes shared one source ID: concurrent heads of one entity.
        self.heads = [coverage('one.harness_root', 'one'), coverage('two.harness_root', 'two')]
        self.ledger.ingest_receipts(self.heads)
        self.entity = self.heads[0].entity_ref

    def collect(self, registry=None):
        return collect_registered_sources(registry or self.registry, self.ledger, retirements=RETIRED)

    def test_retirement_is_the_only_successor_of_concurrent_heads(self):
        self.assertIn(self.entity, current_receipts(self.ledger.receipt_snapshot()['facts'])[1])
        before = self.ledger.receipt_snapshot()
        result = self.collect()
        self.assertEqual(result['retired_sources']['retired_coverage_entities'], [self.entity])
        after = self.ledger.receipt_snapshot()
        self.assertEqual(after['facts'][:len(before['facts'])], before['facts'])
        active, conflicts = current_receipts(after['facts'])
        self.assertNotIn(self.entity, conflicts)
        [head] = [r for r in active if r.entity_ref == self.entity]
        self.assertEqual(head.producer, 'source_retirement.v1')
        self.assertEqual((head.payload['status'], head.payload['reason']), ('excluded', 'source_retired'))
        self.assertNotIn('source_detail', head.payload)
        self.assertEqual(head.supersedes, min(r.fact_ref for r in self.heads))
        self.assertEqual(set(head.evidence), {*(r.fact_ref for r in self.heads), declaration_ref(RETIRED['old-harness'])})
        # Idempotent on this node and on any other node collecting the same stream.
        self.assertEqual(self.collect()['inserted'], 0)
        self.assertEqual(self.ledger.receipt_snapshot(watermark=before['watermark'])['sha256'], before['sha256'])

    def test_unnamed_later_head_still_conflicts_and_is_retired_again(self):
        self.collect()
        late = coverage('three.harness_root', 'late')
        self.ledger.ingest_receipts([late])
        _, conflicts = current_receipts(self.ledger.receipt_snapshot()['facts'])
        self.assertIn(self.entity, conflicts)
        self.collect()
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()['facts'])
        self.assertNotIn(self.entity, conflicts)
        [head] = [r for r in active if r.entity_ref == self.entity]
        self.assertIn(late.fact_ref, head.evidence)

    def test_evidence_cannot_replace_another_entity(self):
        other = coverage('one.harness_root', 'other', domain='value')
        refs = sorted(r.fact_ref for r in self.heads)
        retirement = Receipt.make('coverage', None, dict(domain='execution', wi=None, source_ref=SOURCE,
            status='excluded', expected=None, observed=None, watermark=None, reason='source_retired'),
            occurred_at=None, observed_at=AT, producer='source_retirement.v1',
            evidence=tuple(sorted({*refs, other.fact_ref})), entity_ref=self.entity, supersedes=refs[0])
        active, conflicts = current_receipts([*self.heads, other, retirement])
        self.assertFalse(conflicts)
        self.assertEqual({r.fact_ref for r in active}, {retirement.fact_ref, other.fact_ref})

    def test_registered_retired_source_is_not_read_and_grants_no_authority(self):
        registry = {'schema': REGISTRY_SCHEMA, 'sources': [self.pi, {'id': 'old-harness', 'kind': 'harness_root',
            'runtime_manifest': str(self.root / 'missing-manifest.json'), 'profiles': ['retired']}]}
        result = self.collect(registry)
        self.assertEqual(result['retired_sources']['registered_source_refs'], [SOURCE])
        self.assertEqual([s['source_ref'] for s in result['sources']], [reference('source', 'live-pi')])
        self.assertNotIn('authority_binding_unavailable', result['sources'][0]['diagnostics'])
        facts = self.ledger.receipt_snapshot()['facts']
        self.assertFalse([r for r in facts if r.producer == 'registered_collector.v1'
                          and r.payload.get('source_ref') == SOURCE and r.fact_ref not in {h.fact_ref for h in self.heads}])

    def test_query_has_no_retired_source_detail_or_conflict(self):
        _, conflicts = current_receipts(_qualified_collector_sources(self.ledger.receipt_snapshot()['facts']))
        self.assertFalse(conflicts)  # Labels split the old node-local heads.
        self.collect()
        facts = self.ledger.receipt_snapshot()['facts']
        active, conflicts = current_receipts(_qualified_collector_sources(facts))
        self.assertFalse(conflicts)
        self.assertEqual([r.producer for r in active if r.kind == 'coverage' and r.payload['source_ref'] == SOURCE],
                         ['source_retirement.v1'])
        report = query_collaboration(self.ledger, CollaborationQuery())
        details = report['summary']['usage']['source_details']
        self.assertNotIn(SOURCE, {d['source_ref'] for d in details})
        self.assertNotIn('execution_evidence_conflict', report['summary']['cost']['reasons'])


class RetirementDeclarationTests(unittest.TestCase):
    def test_repository_declaration_names_the_retired_sources(self):
        declared = load_retirements()
        self.assertEqual({i: e['kind'] for i, e in declared.items()}, {
            'canonical-harness': 'harness_root', 'ownermac-canonical-harness': 'harness_root',
            # Vultr node: stats-transport-vultr, vultr.relay_host, vultr.runtime.pi.
            'stats-transport-vultr': 'receipts', 'vultr-relay': 'relay_host', 'native-pi-9f7364c2516f2b70': 'pi'})

    def test_malformed_declaration_is_rejected(self):
        document = {'schema': 'computecost_retired_sources/v1', 'sources': [RETIRED['old-harness']]}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'retired.json'
            for broken in ({**document, 'schema': 'other'},
                           {**document, 'sources': [{**document['sources'][0], 'authorization': 'operator'}]},
                           {**document, 'sources': [document['sources'][0]] * 2},
                           {**document, 'sources': [{**document['sources'][0], 'extra': 'x'}]}):
                path.write_text(json.dumps(broken))
                with self.assertRaises(ReceiptError):
                    load_retirements(path)
            path.write_text(json.dumps({**document, 'sources': [{**document['sources'][0], 'note': '说明'}]}))
            self.assertEqual(set(load_retirements(path)), {'old-harness'})

class RetiredRegistrationTests(unittest.TestCase):
    def setUp(self):
        retired = patch('pulse_compute_cost.source_retirement.load_retirements', return_value=RETIRED)
        retired.start(); self.addCleanup(retired.stop)

    def test_candidate_registry_drops_retired_sources(self):
        with TemporaryDirectory() as directory:
            registry = {'schema': REGISTRY_SCHEMA, 'ledger_root': directory, 'sources': [
                {'id': 'old-harness', 'kind': 'harness_root', 'path': directory},
                {'id': 'live-host', 'kind': 'relay_host', 'path': directory}]}
            result = source_registration.candidate_registry(registry, {})
            self.assertEqual([s['id'] for s in result['sources']], ['live-host'])

    def test_runtime_registry_without_harness_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sessions, host, old = root / 'sessions', root / 'host', root / 'old' / '.harness'
            for path in (sessions, host, old):
                path.mkdir(parents=True, mode=0o700)
            manifest = root / 'manifest.json'
            registry = {'schema': REGISTRY_SCHEMA, 'ledger_root': directory, 'sources': [
                {'id': 'old-harness', 'kind': 'harness_root', 'path': str(old)},
                {'id': 'live-host', 'kind': 'relay_host', 'path': str(host)}]}
            # New kernel profile: no computeCostHost; an old Relay may still export the retired root.
            manifest.write_text(json.dumps({'profiles': {'resident': {'sessionRoot': str(sessions)}}}))
            for harness_root in (None, old):
                candidate = source_registration.runtime_candidate_registry(
                    registry, manifest, harness_root=harness_root, receipt_root=host)
                self.assertEqual(sorted(s['kind'] for s in candidate['sources']), ['pi', 'relay_host'])
            live = {**registry, 'sources': [{**registry['sources'][0], 'id': 'live-harness'}, registry['sources'][1]]}
            with self.assertRaises(ReceiptError):
                source_registration.runtime_candidate_registry(live, manifest, harness_root=None, receipt_root=host)


if __name__ == '__main__':
    unittest.main()
