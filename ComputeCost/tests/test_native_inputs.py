"""A launch another node read from its transcript is judged by any stream reader."""
from pathlib import Path
import json
import tempfile
import unittest

from pulse_compute_cost.receipts import Receipt, ReceiptError, SUPPORTED_RECEIPT_FEATURES, reference
from pulse_compute_cost.receipt_sources import actor_receipt, collect_registered_sources, usage_receipt
from pulse_compute_cost.runtime_subcalls import (cc_native_launches, launch_attributions,
    subcall_purpose_attributions, PURPOSE_POLICY, REQUEST_POLICY)
from pulse_compute_cost.native_inputs import FEATURE, launch_inputs, merge_launches
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from test_collaboration_receipts import AT, CATALOG
from test_runtime_subcalls_cc import COMMAND, SESSION, CHILD, STARTED, WI, assistant, result

LATER = '2026-09-13T02:00:00Z'
SOURCE = reference('source', 'native-cc-ownermac')


class NativeInputTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.parent = usage_receipt('cc', [SESSION, 'msg_launch'], {'input_tokens': 1, 'output_tokens': 1,
            'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}, at='2026-09-13T00:59:59Z',
            observed=AT, model='openai/test', native_scope_ref=reference('native_session', ['cc', SESSION]))
        self.child = usage_receipt('codex', 'native-child', {'input_tokens': 100, 'cached_input_tokens': 0,
            'cache_write_input_tokens': 0, 'output_tokens': 2}, at=AT, observed=AT, model='openai/test',
            native_scope_ref=reference('native_session', ['codex', CHILD]))
        self.actor = actor_receipt(self.parent, node='ownermac', executor='ken', observed=AT)
        self.claim = Receipt.make('attribution', None, {'target_ref': self.parent.entity_ref, 'wi': WI,
            'executor': None, 'responsible': None, 'delegator': None, 'state': 'assigned',
            'policy': REQUEST_POLICY, 'effective_at': '2026-09-13T00:59:59Z'},
            entity_ref=reference('attribution', [REQUEST_POLICY, self.parent.entity_ref]),
            occurred_at='2026-09-13T00:59:59Z', observed_at=AT, producer=REQUEST_POLICY)
        path = self.home / f'{SESSION}.jsonl'
        path.write_text(''.join(json.dumps(r) + '\n' for r in [
            assistant('u-launch', 'toolu_1', COMMAND, '2026-09-13T00:59:59Z'),
            result('u-result', 'toolu_1', 'u-launch', STARTED, '2026-09-13T01:00:01Z')]))
        self.launches = cc_native_launches(path, {'node': 'ownermac'})
        self.assertEqual(len(self.launches), 1)
        self.facts = [self.parent, self.child, self.actor, self.claim]

    def test_reader_supports_the_feature_and_an_input_is_content_addressed(self):
        self.assertIn(FEATURE, SUPPORTED_RECEIPT_FEATURES)
        first, = launch_inputs(self.launches, source_ref=SOURCE, observed=AT)
        again, = launch_inputs(self.launches, source_ref=SOURCE, observed=LATER)
        self.assertEqual(first.fact_ref, again.fact_ref)
        self.assertEqual(Receipt.from_dict(first.to_dict()), first)
        self.assertIn(SOURCE, first.evidence)
        ledger = UsageLedger(self.home / 'ledger')
        self.assertEqual(ledger.ingest_receipts([first])['inserted'], 1)
        self.assertEqual(ledger.ingest_receipts([again])['inserted'], 0)

    def test_closed_contract_rejects_raw_or_revised_inputs(self):
        good = launch_inputs(self.launches, source_ref=SOURCE, observed=AT)[0].to_dict()
        def rejected(change):
            value = json.loads(json.dumps(good)); change(value)
            with self.assertRaises(ReceiptError):
                Receipt.make(value['kind'], None, value['payload'], entity_ref=value['entity_ref'],
                             occurred_at=value['occurred_at'], observed_at=AT, producer=value['producer'],
                             evidence=tuple(value['evidence']), supersedes=value['supersedes'])
        rejected(lambda v: v['payload'].update(input_class='unknown.v1'))
        rejected(lambda v: v['payload']['value'].update(evidence=['/Users/raw/transcript.jsonl']))
        rejected(lambda v: v['payload']['value'].update(session_id=SESSION))
        rejected(lambda v: v['payload']['value'].update(ended_at='2026-09-13T00:00:00Z'))
        rejected(lambda v: v['payload']['value']['context'].update(basis='reviewed_launch'))
        rejected(lambda v: v.update(supersedes=v['fact_ref']))

    def test_merge_keeps_local_launches_and_adds_recorded_ones_once(self):
        inputs = launch_inputs(self.launches, source_ref=SOURCE, observed=AT)
        self.assertEqual(merge_launches([], inputs + inputs), self.launches)
        self.assertEqual(merge_launches(self.launches, inputs), self.launches)

    def test_collector_without_the_transcript_judges_the_recorded_launch(self):
        expected_actors = launch_attributions(self.facts, self.launches, AT)
        expected_purposes = subcall_purpose_attributions(self.facts + expected_actors, self.launches, AT)
        ledger = UsageLedger(self.home / 'ledger')
        ledger.ingest_receipts(self.facts + launch_inputs(self.launches, source_ref=SOURCE, observed=AT))
        registry = {'schema': 'computecost_source_registry/v1', 'sources': [],
                    'required_receipt_features': ['runtime_subcall.v1']}
        collect_registered_sources(registry, ledger, retirements={}, recoveries={})
        facts = ledger.receipt_snapshot()['facts']
        actors = [r for r in facts if r.kind == 'attribution' and r.payload.get('scope') == 'actor'
                  and r.payload['target_ref'] == self.child.entity_ref]
        purposes = [r for r in facts if r.producer == PURPOSE_POLICY]
        self.assertEqual([(r.entity_ref, r.payload) for r in actors],
                         [(r.entity_ref, r.payload) for r in expected_actors])
        self.assertEqual([r.payload['executor'] for r in actors], ['ken'])
        self.assertEqual([(r.entity_ref, r.payload) for r in purposes],
                         [(r.entity_ref, r.payload) for r in expected_purposes])
        self.assertEqual([r.payload['wi'] for r in purposes], [WI])

    def test_queries_do_not_report_inputs(self):
        def report(facts):
            ledger = UsageLedger(self.home / f'ledger-{len(facts)}')
            ledger.ingest_receipts(facts)
            out = query_collaboration(ledger, CollaborationQuery(scope='activity', from_date='2026-09-13',
                to_date='2026-09-14', group_by=('agent', 'wi', 'runtime')), catalog=CATALOG, catalog_observed_at=AT)
            # Only the stream identity differs: its digest, time and the report digest over them.
            return {k: v for k, v in out.items() if k not in {'facts_sha256', 'recorded_at', 'report_sha256'}}
        inputs = launch_inputs(self.launches, source_ref=SOURCE, observed=AT)
        self.assertEqual(json.dumps(report(self.facts), sort_keys=True),
                         json.dumps(report(self.facts + inputs), sort_keys=True))


if __name__ == '__main__':
    unittest.main()
