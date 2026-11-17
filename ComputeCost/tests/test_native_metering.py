"""Calendar metering conserves exact native quantities and prior receipts."""
import copy
from pathlib import Path
from types import SimpleNamespace
import json
import unittest
import test_non_wi_activity as f
from pulse_compute_cost.receipts import Receipt,ReceiptError,reference
from pulse_compute_cost.native_metering import BASIS,NATIVE,extract_metering,validate_metering,metered_window
from pulse_compute_cost.collaboration_query import CollaborationQuery,query_collaboration
from pulse_compute_cost.receipt_sources import _revision

A='2026-09-25T15:59:37.826Z'
B='2026-09-25T16:00:14.548Z'
START='2026-09-25T15:58:00Z'
OBS='2026-09-26T03:00:00Z'
class NativeMeteringTests(unittest.TestCase):
    setUp=f.NonWiActivityTests.setUp
    def prepare(self):
        p={**self.row.payload,'interval_start':START,'interval_end':B,'final':True,
           'granularity':'turn_aggregate','native_scope_ref':reference('native_session','test'),
           'native_tokens':dict(zip(NATIVE,(100,0,0,2))),
           'tokens':{'uncached_input':100,'cache_read':0,'cache_write':0,'output':2}}
        p['reported_total_tokens']=102
        self.old=Receipt.make('usage','timed',p,occurred_at=B,observed_at=OBS,producer='codex_rollout.v1')
        rows=[{'type':'event_msg','timestamp':at,'payload':{'type':'token_count','info':{'total_token_usage':dict(zip(NATIVE,values))}}}
              for at,values in ((A,(40,0,0,1)),(B,(100,0,0,2)))]
        self.path=Path(self.tmp.name)/'native.jsonl'
        self.path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        self.observation=SimpleNamespace(source_path=self.path,source_position=2,
            previous_counter=dict.fromkeys(NATIVE,0),previous_fields=frozenset(NATIVE))
        self.proof=extract_metering(self.observation,p,{})
        self.assertIsNotNone(self.proof)
        return p
    def query(self,day=None,mark=None):
        q=CollaborationQuery(scope='activity' if day else 'wi_lifetime',from_date=day,to_date='2026-09-27' if day=='2026-09-26' else '2026-09-26' if day else None,
                             timezone='Asia/Shanghai',watermark=mark,group_by=('wi',))
        return query_collaboration(self.ledger,q,catalog=f.CATALOG,catalog_observed_at=f.AT)
    def test_original_unknown_and_exact_midnight_refinement(self):
        p=self.prepare();self.ledger.ingest_receipts([self.old]);mark=self.ledger.receipt_snapshot()['watermark']
        before=self.query('2026-09-26',mark)
        self.assertEqual(before['summary']['usage']['time_unallocated_event_count'],1)
        rows=[self.old];_revision(rows,Receipt.make('usage',None,{**p,'metering':self.proof},entity_ref=self.old.entity_ref,
            occurred_at=B,observed_at=OBS,producer=self.old.producer))
        self.assertEqual(rows[-1].supersedes,self.old.fact_ref)
        self.ledger.ingest_receipts(rows)
        left=self.query('2026-09-25');right=self.query('2026-09-26');whole=self.query()
        self.assertEqual(left['summary']['usage']['known_total_tokens'],41)
        self.assertEqual(right['summary']['usage']['known_total_tokens'],61)
        self.assertEqual(whole['summary']['usage']['known_total_tokens'],102)
        self.assertEqual(right['summary']['usage']['time_unallocated_event_count'],0)
        self.assertEqual(right['summary']['usage']['event_count'],1)
        self.assertEqual(self.query('2026-09-26',mark),before)
        self.assertEqual(self.ledger.ingest_receipts([rows[-1]])['inserted'],0)
    def test_tampered_amount_time_or_duplicate_proof_rejected(self):
        p=self.prepare()
        for mutate in (lambda m:m['points'][0]['tokens'].update(output=2),
                       lambda m:m['points'][0].update(at='2026-09-24T00:00:00Z'),
                       lambda m:m['points'].append(m['points'][0]),
                       lambda m:m.update(basis='uniform_proration')):
            proof=copy.deepcopy(self.proof);mutate(proof)
            with self.assertRaises(ReceiptError):validate_metering({**p,'metering':proof})
    def test_missing_counter_negative_delta_and_excess_cache_remain_unknown(self):
        p=self.prepare();rows=[json.loads(line) for line in self.path.read_text().splitlines()]
        for values in ({'input_tokens':40},dict(zip(NATIVE,(101,0,0,1))),
                       dict(zip(NATIVE,(40,30,11,1)))):
            rows[0]['payload']['info']['total_token_usage']=values
            self.path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            self.assertIsNone(extract_metering(self.observation,p,{}))
    def test_future_native_records_are_not_read_into_old_observation(self):
        p=self.prepare()
        with self.path.open('a') as h:h.write(json.dumps({'type':'event_msg','timestamp':'2026-09-27T00:00:00Z',
            'payload':{'type':'token_count','info':{'total_token_usage':dict(zip(NATIVE,(900,0,0,9)))}}})+'\n')
        self.assertEqual(extract_metering(self.observation,p,{}),self.proof)
    def test_zero_increment_requires_unchanged_native_counter(self):
        p=self.prepare();self.observation.previous_counter=dict(zip(NATIVE,(40,0,0,1)))
        p['tokens']={'uncached_input':60,'cache_read':0,'cache_write':0,'output':1};p['reported_total_tokens']=61
        result=extract_metering(self.observation,p,{});
        self.assertIsNotNone(result);self.assertEqual(sum(result['points'][0]['tokens'].values()),0)
    def test_known_timeline_cannot_be_replaced_by_another_claim(self):
        p=self.prepare();one=Receipt.make('usage',None,{**p,'metering':self.proof},entity_ref=self.old.entity_ref,
            occurred_at=B,observed_at=OBS,producer=self.old.producer)
        other=copy.deepcopy(self.proof);other['points'][0]['at']='2026-09-25T16:00:00Z'
        two=Receipt.make('usage',None,{**p,'metering':other},entity_ref=self.old.entity_ref,
            occurred_at=B,observed_at=OBS,producer=self.old.producer)
        revised=[one];_revision(revised,two);self.assertIsNone(revised[-1].supersedes)

if __name__=='__main__':unittest.main()
