"""Native reverted-rollout counter continuation and startup workspace fixtures."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import read_codex, collect_registered_sources, paginated_counter_correction
from pulse_compute_cost.receipts import Receipt, reference, current_receipts

THREAD='00000000-0000-4000-8000-000000000001'
ROLLOUT='00000000-0000-4000-8000-000000000002'
AT='2026-09-15T01:00:00Z';END='2026-09-15T01:01:00Z'
BASE={'input_tokens':10000,'cached_input_tokens':9000,'cache_write_input_tokens':0,'output_tokens':1000}
LAST={'input_tokens':120,'cached_input_tokens':20,'cache_write_input_tokens':0,'output_tokens':10}
TOTAL={k:BASE[k]+LAST[k] for k in BASE}

class CodexPaginatedActorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.logs=self.root/'logs';self.logs.mkdir()
        self.parent=self.logs/f'rollout-2026-09-14T01-00-00-{THREAD}.jsonl'
        rows=[{'type':'session_meta','timestamp':'2026-09-14T01:00:00Z','ordinal':0,
               'payload':{'id':THREAD,'session_id':THREAD,'history_mode':'paginated'}},
              {'type':'event_msg','timestamp':'2026-09-14T01:01:00Z','ordinal':1,
               'payload':{'type':'token_count','info':{'total_token_usage':BASE}}}]
        self.parent.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        self.history={'thread_id':THREAD,'end_ordinal_exclusive':2,'end_byte_offset':self.parent.stat().st_size}
        self.child=self.logs/f'rollout-2026-09-15T01-00-00-{THREAD}_{ROLLOUT}.jsonl'
        self.rows=[{'type':'session_meta','timestamp':AT,'ordinal':2,'payload':{
                    'id':THREAD,'session_id':THREAD,'history_mode':'paginated','history_base':self.history,
                    'cwd':'/registered/PULSE/local-pulse-space','originator':'Codex Desktop','source':'vscode'}},
                   {'type':'event_msg','timestamp':AT,'ordinal':3,'payload':{'type':'task_started','turn_id':'new-turn'}},
                   {'type':'turn_context','timestamp':AT,'ordinal':4,'payload':{'model':'gpt-6-astra','cwd':'/project/task-worktree'}},
                   {'type':'event_msg','timestamp':END,'ordinal':5,'payload':{'type':'token_count','info':{
                       'total_token_usage':TOTAL,'last_token_usage':LAST}}},
                   {'type':'event_msg','timestamp':END,'ordinal':6,'payload':{'type':'task_complete'}}]
        self.write()
        self.source={'id':'codex','kind':'codex','path':str(self.logs),'node':'ownermac','actor_attribution':True,
            'workspace_actors':[{'workspace_root':'/registered/PULSE/local-pulse-space','agent':'pulse',
                                'node':'ownermac','runtime':'codex','registry_ref':reference('registry','fixture')}]}
    def write(self):self.child.write_text(''.join(json.dumps(r)+'\n' for r in self.rows))
    def read(self):return read_codex(self.logs,END,source_identity=self.source)
    def usage(self):return next(r for r in self.read() if r.kind=='usage')
    def test_same_thread_new_rollout_keeps_actor_and_counts_only_increment(self):
        before={p:p.read_bytes() for p in (self.parent,self.child)}
        rows=self.read();usage=next(r for r in rows if r.kind=='usage');actor=next(r for r in rows if r.kind=='attribution')
        self.assertEqual(usage.payload['tokens'],{'uncached_input':100,'cache_read':20,'cache_write':0,'output':10})
        self.assertEqual(usage.payload['native_tokens'],TOTAL)
        self.assertEqual(usage.payload['reported_total_tokens'],130)
        self.assertEqual(usage.payload['mapping'],'codex_rollout_paginated.v1');self.assertTrue(usage.evidence)
        self.assertEqual(actor.payload['executor'],'pulse');self.assertEqual(actor.payload['context']['entrypoint'],'codex_app')
        self.assertIsNone(actor.payload['wi'])
        for p,data in before.items():self.assertEqual(p.read_bytes(),data)
    def test_later_turn_uses_its_own_previous_counter_not_the_base_twice(self):
        more={k:TOTAL[k]+LAST[k] for k in TOTAL}
        self.rows.extend([{'type':'event_msg','timestamp':END,'ordinal':7,'payload':{'type':'task_started','turn_id':'second'}},
                          {'type':'turn_context','timestamp':END,'ordinal':8,'payload':{'model':'gpt-6-astra'}},
                          {'type':'event_msg','timestamp':END,'ordinal':9,'payload':{'type':'token_count','info':{'total_token_usage':more,'last_token_usage':LAST}}},
                          {'type':'event_msg','timestamp':END,'ordinal':10,'payload':{'type':'task_complete'}}])
        self.write();rows=[r for r in self.read() if r.kind=='usage']
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r.payload['reported_total_tokens']==130 for r in rows))
    def test_missing_parent_keeps_agent_but_cannot_treat_total_as_delta(self):
        self.parent.unlink();rows=self.read();usage=next(r for r in rows if r.kind=='usage')
        self.assertTrue(all(v is None for v in usage.payload['tokens'].values()))
        self.assertEqual(next(r for r in rows if r.kind=='attribution').payload['executor'],'pulse')
    def test_wrong_byte_or_ordinal_boundary_is_not_a_baseline(self):
        for key in ('end_byte_offset','end_ordinal_exclusive'):
            original=self.history[key];self.history[key]-=1;self.write()
            self.assertTrue(all(v is None for v in self.usage().payload['tokens'].values()))
            self.history[key]=original
    def test_first_total_minus_base_must_equal_native_last(self):
        self.rows[3]['payload']['info']['last_token_usage']={**LAST,'input_tokens':121};self.write()
        self.assertIsNone(self.usage().payload['reported_total_tokens'])
    def test_original_counter_interpretation_is_versioned_not_erased(self):
        correct=self.usage();p=correct.payload
        p.update(mapping='codex_rollout_cumulative.v1',tokens={'uncached_input':TOTAL['input_tokens']-TOTAL['cached_input_tokens'],
                 'cache_read':TOTAL['cached_input_tokens'],'cache_write':0,'output':TOTAL['output_tokens']},
                 reported_total_tokens=TOTAL['input_tokens']+TOTAL['output_tokens'])
        wrong=Receipt.make('usage',None,p,entity_ref=correct.entity_ref,occurred_at=correct.occurred_at,
                           observed_at=correct.observed_at,producer=correct.producer)
        ledger=UsageLedger(self.root/'ledger',clock=lambda:datetime(2026,9,15,4,tzinfo=timezone.utc))
        ledger.ingest_receipts([wrong]);before=ledger.receipt_snapshot()
        result=collect_registered_sources({'schema':'computecost_source_registry/v1','sources':[self.source]},ledger)
        active,conflicts=current_receipts(ledger.receipt_snapshot()['facts'])
        self.assertFalse(conflicts)
        current=next(r for r in active if r.kind=='usage')
        self.assertEqual(current.payload['tokens'],correct.payload['tokens']);self.assertEqual(current.supersedes,wrong.fact_ref)
        self.assertEqual(ledger.receipt_snapshot(watermark=before['watermark'])['sha256'],before['sha256'])
        self.assertEqual(collect_registered_sources({'schema':'computecost_source_registry/v1','sources':[self.source]},ledger)['inserted'],0)
    def test_missing_baseline_can_recover_without_mutating_the_unknown_history(self):
        parent=self.parent.read_bytes();self.parent.unlink()
        ledger=UsageLedger(self.root/'recovery-ledger',clock=lambda:datetime(2026,9,15,4,tzinfo=timezone.utc))
        registry={'schema':'computecost_source_registry/v1','sources':[self.source]}
        collect_registered_sources(registry,ledger);before=ledger.receipt_snapshot()
        self.parent.write_bytes(parent);collect_registered_sources(registry,ledger)
        active,conflicts=current_receipts(ledger.receipt_snapshot()['facts'])
        self.assertFalse(conflicts)
        self.assertEqual(next(r for r in active if r.kind=='usage').payload['reported_total_tokens'],130)
        self.assertEqual(ledger.receipt_snapshot(watermark=before['watermark'])['sha256'],before['sha256'])
    def test_correction_rejects_unrelated_model_scope_or_counter(self):
        correct=self.usage();p=correct.payload
        p.update(mapping='codex_rollout_cumulative.v1',tokens={'uncached_input':TOTAL['input_tokens']-TOTAL['cached_input_tokens'],
            'cache_read':TOTAL['cached_input_tokens'],'cache_write':0,'output':TOTAL['output_tokens']},reported_total_tokens=sum((TOTAL['input_tokens'],TOTAL['output_tokens'])))
        p['model']='another-model'
        wrong=Receipt.make('usage',None,p,entity_ref=correct.entity_ref,occurred_at=correct.occurred_at,
                           observed_at=correct.observed_at,producer=correct.producer)
        self.assertIsNone(paginated_counter_correction(wrong,correct))
    def test_active_archive_copies_still_emit_one_increment(self):
        archive=self.root/'archive';archive.mkdir();(archive/self.child.name).write_bytes(self.child.read_bytes())
        rows=read_codex([self.logs,archive],END,source_identity=self.source)
        self.assertEqual(sum(r.kind=='usage' for r in rows),1)
        self.assertEqual(next(r for r in rows if r.kind=='usage').payload['reported_total_tokens'],130)

if __name__=='__main__':unittest.main()
