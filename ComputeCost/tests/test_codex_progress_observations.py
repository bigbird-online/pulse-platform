"""Native counter progress is not an independent bill or a final contradiction."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from unittest import TestCase

from pulse_compute_cost.receipts import Receipt, reference, current_receipts, digest
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration, _request_observation_projection
from pulse_compute_cost.ledger import UsageLedger

WI = 'wi_20261001_297'
SOURCE = reference('source', 'original-native')
SCOPE = reference('native_session', ['codex', 'original'])
START = '2026-10-01T01:00:00Z'

def observation(identity='same-turn', n=10, at='2026-10-01T01:01:00Z', final=False, **changes):
    r = usage_receipt('codex', identity, {'input_tokens':n+20,'cached_input_tokens':20,
        'cache_write_input_tokens':0,'output_tokens':n}, at=at, observed='2026-10-04T00:00:00Z',
        model='openai/test', final=final, mapping='codex_rollout_cumulative.v1',
        common={'wi':WI,'executor':'pulse','responsible':'pulse','delegator':None},
        granularity='turn_aggregate', interval=(START,at), native_scope_ref=SCOPE)
    p = r.payload
    p.update(native_tokens={'input_tokens':1000+n+20,'cached_input_tokens':820,
        'cache_write_input_tokens':0,'output_tokens':100+n}, reported_total_tokens=2*n+20)
    p.update(changes)
    return Receipt.make('usage', None, p, entity_ref=r.entity_ref, occurred_at=at,
        observed_at=r.observed_at, producer='codex_rollout.v1', evidence=(SOURCE,))

def project(rows):
    active, conflicts = current_receipts(rows)
    return _request_observation_projection(active, conflicts)

class NativeProgressTests(TestCase):
    def test_missing_revision_edges_keep_latest_native_progress_once(self):
        rows=[observation(n=10),observation(n=20,at='2026-10-01T01:02:00Z'),
              observation(n=30,at='2026-10-01T01:03:00Z',final=True)]
        before=[r.to_dict() for r in rows]
        active, conflicts, groups, _=project(rows)
        self.assertFalse(conflicts)
        self.assertEqual(len(active),1)
        self.assertEqual(active[0].payload,rows[-1].payload)
        self.assertEqual(groups[active[0].entity_ref],sorted(r.fact_ref for r in rows))
        self.assertEqual(before,[r.to_dict() for r in rows])
        self.assertEqual(project(list(reversed(rows)))[0],active)

    def test_interrupted_progress_does_not_become_final(self):
        rows=[observation(n=10),observation(n=20,at='2026-10-01T01:02:00Z')]
        active, conflicts, _, _=project(rows)
        self.assertFalse(conflicts)
        self.assertEqual(len(active),1)
        self.assertFalse(active[0].payload['final'])

    def test_true_contradictions_keep_every_conflict(self):
        for mode in ('final','regression','baseline','model','wi','scope','source','missing','interval','counter_time'):
            with self.subTest(mode=mode):
                first=observation(n=10,final=mode=='final')
                p={}
                if mode=='baseline':p['native_tokens']={**first.payload['native_tokens'],'input_tokens':99999,'output_tokens':120}
                if mode=='model':p['model']='another'
                if mode=='wi':p['wi']='wi_20261001_001'
                if mode=='scope':p['native_scope_ref']=reference('native_session','other')
                if mode=='missing':p['tokens']={**first.payload['tokens'],'uncached_input':None}
                if mode=='interval':p['interval_start']='2026-10-01T00:00:00Z'
                if mode=='counter_time':p['interval_end']='2026-10-01T01:09:00Z'
                last=observation(n=5 if mode=='regression' else 20,at='2026-10-01T01:02:00Z',final=True,**p)
                if mode=='source':last=replace(last,evidence=(reference('source','other'),))
                active, conflicts, _, _=project([first,last])
                self.assertFalse(active);self.assertEqual(len(conflicts[first.entity_ref]),2)

    def test_identical_final_with_two_graph_predecessors_is_one_observation(self):
        first=observation(n=10)
        final=observation(n=20,at='2026-10-01T01:02:00Z',final=True)
        another=Receipt.make('usage',None,final.payload,entity_ref=final.entity_ref,
            occurred_at=final.occurred_at,observed_at=final.observed_at,producer=final.producer,
            evidence=final.evidence,supersedes=first.fact_ref)
        active, conflicts, _, _=project([first,final,another])
        self.assertFalse(conflicts);self.assertEqual(len(active),1)

    def test_same_time_different_counts_never_merge(self):
        _, conflicts, _, _=project([observation(n=10),observation(n=20)])
        self.assertEqual(len(conflicts),1)

    def test_full_query_preserves_other_rows_prices_and_raw_ledger(self):
        import tempfile
        rows=[observation(n=10),observation(n=20,at='2026-10-01T01:02:00Z',final=True)]
        other=observation(identity='independent',n=7,final=True)
        with tempfile.TemporaryDirectory() as tmp:
            ledger=UsageLedger(tmp,clock=lambda:datetime(2026,10,4,tzinfo=timezone.utc));ledger.ingest_receipts([*rows,other])
            before=ledger.receipt_snapshot()
            catalog={'data':[{'id':'openai/test','pricing':{'prompt':'0.01','completion':'0.02','input_cache_read':'0.001'}}]}
            report=query_collaboration(ledger,CollaborationQuery(from_date='2026-09-28',to_date='2026-10-05',scope='activity',group_by=('wi',)),catalog=catalog,catalog_observed_at=START)
            self.assertEqual(report['summary']['usage']['event_count'],2)
            self.assertEqual(report['summary']['usage']['known_total_tokens'],94)
            self.assertEqual(report['summary']['usage']['conflicted_event_count'],0)
            self.assertIsNotNone(report['summary']['cost']['partial_machine_cash_usd'])
            self.assertEqual(before['sha256'],ledger.receipt_snapshot()['sha256'])
