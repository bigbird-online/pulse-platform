"""Retries preserve a proven native counter even before any new response."""
import json
import unittest
from pathlib import Path
from datetime import datetime, timezone
import test_codex_paginated_actor as f
from pulse_compute_cost.receipt_sources import read_codex, collect_registered_sources, _codex_paginated_context
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import current_receipts

class PaginatedRetryTests(unittest.TestCase):
    setUp=f.CodexPaginatedActorTests.setUp
    write=f.CodexPaginatedActorTests.write
    read=f.CodexPaginatedActorTests.read
    usage=f.CodexPaginatedActorTests.usage

    def test_unchanged_first_counter_is_a_verified_heartbeat(self):
        self.rows[3]['payload']['info']['total_token_usage']=dict(f.BASE)
        self.write()
        usage=self.usage()
        self.assertEqual(usage.payload['reported_total_tokens'],0)
        self.assertEqual(usage.payload['tokens'],dict.fromkeys(('uncached_input','cache_read','cache_write','output'),0))
        self.assertTrue(usage.evidence)

    def bridge(self):
        middle=self.logs/f'rollout-2026-09-15T00-00-00-{f.THREAD}_{f.ROLLOUT}.jsonl'
        metadata=dict(self.rows[0]['payload'])
        metadata['history_base']=dict(self.history)
        middle.write_text(json.dumps({'type':'session_meta','timestamp':f.AT,'ordinal':2,'payload':metadata})+'\n')
        self.child.unlink()
        self.child=self.logs/f'rollout-2026-09-15T01-00-00-{f.THREAD}_00000000-0000-4000-8000-000000000003.jsonl'
        self.rows[0]['payload']['history_base']={'thread_id':f.ROLLOUT,'end_ordinal_exclusive':3,'end_byte_offset':middle.stat().st_size}
        for i,row in enumerate(self.rows):row['ordinal']=3+i
        self.write()
        return middle

    def test_counter_free_intermediate_segment_uses_exact_ancestor(self):
        self.bridge()
        before={p:p.read_bytes() for p in self.logs.iterdir()}
        usage=self.usage()
        self.assertEqual(usage.payload['reported_total_tokens'],130)
        self.assertEqual(usage.payload['tokens'],{'uncached_input':100,'cache_read':20,'cache_write':0,'output':10})
        self.assertEqual(usage.payload['native_tokens'],f.TOTAL)
        self.assertTrue(all(p.read_bytes()==raw for p,raw in before.items()))

    def test_missing_ancestor_does_not_use_later_counter_or_zero(self):
        middle=self.bridge()
        self.parent.unlink()
        with middle.open('a') as handle:
            handle.write(json.dumps({'ordinal':3,'type':'event_msg','timestamp':f.AT,'payload':{'type':'token_count','info':{'total_token_usage':f.BASE}}})+'\n')
        self.assertTrue(all(v is None for v in self.usage().payload['tokens'].values()))

    def test_wrong_intermediate_boundary_stays_unknown(self):
        self.bridge()
        self.rows[0]['payload']['history_base']['end_byte_offset']-=1
        self.write()
        self.assertIsNone(self.usage().payload['reported_total_tokens'])

    def test_foreign_logical_thread_cannot_supply_the_counter(self):
        self.bridge()
        rows=[json.loads(line) for line in self.parent.read_text().splitlines()]
        rows[0]['payload']['id']='00000000-0000-4000-8000-000000000009'
        self.parent.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.assertIsNone(self.usage().payload['reported_total_tokens'])

    def test_heartbeat_does_not_hide_a_conflicting_advanced_counter(self):
        self.rows[3]['payload']['info']['total_token_usage']={**f.BASE,'output_tokens':f.BASE['output_tokens']+1}
        self.write()
        self.assertIsNone(self.usage().payload['reported_total_tokens'])

    def test_unknown_history_recovers_once_without_changing_old_watermark(self):
        self.bridge()
        prior=self.parent.read_bytes()
        self.parent.unlink()
        ledger=UsageLedger(self.root/'ledger',clock=lambda:datetime(2026,9,15,4,tzinfo=timezone.utc))
        registry={'schema':'computecost_source_registry/v1','sources':[self.source]}
        collect_registered_sources(registry,ledger)
        old=ledger.receipt_snapshot()
        self.parent.write_bytes(prior)
        collect_registered_sources(registry,ledger)
        active,conflicts=current_receipts(ledger.receipt_snapshot()['facts'])
        self.assertFalse(conflicts)
        rows=[r for r in active if r.kind=='usage']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].payload['reported_total_tokens'],130)
        self.assertEqual(ledger.receipt_snapshot(watermark=old['watermark'])['sha256'],old['sha256'])
        self.assertEqual(collect_registered_sources(registry,ledger)['inserted'],0)

if __name__=='__main__':unittest.main()
