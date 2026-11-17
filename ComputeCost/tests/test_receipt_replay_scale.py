"""Replay work must scale with facts, not scan every fact per duplicate."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.receipts import Receipt

class ReceiptReplayScaleTests(unittest.TestCase):
    def test_duplicate_replay_uses_transaction_local_indexes(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=UsageLedger(Path(root));n=400
            facts=[usage_receipt('cc',['scale',i],{'input_tokens':1,'output_tokens':0,'cache_read_input_tokens':0,'cache_creation_input_tokens':0},
                at='2026-09-14T01:00:00Z',observed='2026-09-14T01:00:00Z',model='openai/test') for i in range(n)]
            ledger.ingest_receipts(facts)
            original=Receipt.__getattribute__;reads=0
            def tracked(value,name):
                nonlocal reads
                if name=='fact_ref':reads+=1
                return original(value,name)
            with patch.object(Receipt,'__getattribute__',tracked):
                result=ledger.ingest_receipts(facts)
            self.assertEqual(result['inserted'],0)
            self.assertEqual(result['duplicates'],n)
            self.assertLess(reads,40*n,'duplicate replay rescans the complete receipt set')
    def test_inventory_does_not_decode_all_requests_for_each_session(self):
        from pulse_compute_cost.native_inventory import pi_prefix_inventory, reconcile_host_inventory
        from pulse_compute_cost.receipt_sources import read_pi
        import json
        rows=[];inventories=[]
        for i in range(60):
            data=(json.dumps({'type':'session','id':str(i)})+'\n'+json.dumps({'type':'message','id':'m',
                'timestamp':'2026-09-14T01:00:00Z','message':{'role':'assistant','model':'test','provider':'openai',
                'usage':{'input':1,'output':0,'cacheRead':0,'cacheWrite':0}}})+'\n').encode()
            native=read_pi(Path('unused'),'2026-09-14T01:00:00Z',data=data)
            rows.extend(native)
            inventories.append(pi_prefix_inventory(data,native,'source-'+str(i),'2026-09-14T01:00:00Z'))
        original=Receipt.payload.fget;reads=0
        def tracked(value):
            nonlocal reads
            reads+=1
            return original(value)
        with patch.object(Receipt,'payload',property(tracked)):
            result=reconcile_host_inventory(rows,inventories,[])
        self.assertEqual(result,{})
        self.assertLess(reads,5*(len(rows)+len(inventories)))

    def test_price_lookup_is_query_local_and_keeps_uniqueness_checks(self):
        from pulse_compute_cost import collaboration_query as q
        catalog={'data':[{'id':'openai/test','pricing':{'prompt':'1','completion':'1'}}]}
        with tempfile.TemporaryDirectory() as root:
            ledger=UsageLedger(Path(root))
            ledger.ingest_receipts([usage_receipt('cc',str(i),{'input_tokens':1,'output_tokens':0,
                'cache_read_input_tokens':0,'cache_creation_input_tokens':0},at='2026-09-14T01:00:00Z',
                observed='2026-09-14T01:00:00Z',model='openai/test') for i in range(200)])
            with patch.object(q,'_model',wraps=q._model) as lookup:
                first=q.query_collaboration(ledger,q.CollaborationQuery(group_by=('model',)),catalog=catalog)
            self.assertEqual(lookup.call_count,1)
            self.assertEqual(first['summary']['cost']['partial_machine_cash_usd'],'200')
            catalog['data'][0]['pricing']['prompt']='2'
            second=q.query_collaboration(ledger,q.CollaborationQuery(),catalog=catalog)
            self.assertEqual(second['summary']['cost']['partial_machine_cash_usd'],'400')
            catalog['data'].append(catalog['data'][0])
            third=q.query_collaboration(ledger,q.CollaborationQuery(),catalog=catalog)
            self.assertIsNone(third['summary']['cost']['partial_machine_cash_usd'])
            self.assertEqual(len(third['summary']['cost']['unpriced']),200)

    def test_replaying_existing_history_never_merges_old_provenance_into_current_head(self):
        from pulse_compute_cost.receipts import reference
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as root:
            ledger=UsageLedger(Path(root))
            base=usage_receipt('cc','historic',{'input_tokens':1,'output_tokens':0,
                'cache_read_input_tokens':0,'cache_creation_input_tokens':0},
                at='2026-09-14T01:00:00Z',observed='2026-09-14T01:00:00Z',model='openai/test')
            base=replace(base,evidence=(reference('source','old'),))
            successor=Receipt.make(base.kind,None,base.payload,occurred_at=base.occurred_at,
                observed_at=base.observed_at,producer=base.producer,entity_ref=base.entity_ref,
                supersedes=base.fact_ref,evidence=(reference('source','current'),))
            ledger.ingest_receipts([base,successor]);before=ledger.receipt_snapshot()
            for _ in range(2):
                self.assertEqual(ledger.ingest_receipts(before['facts'])['inserted'],0)
            self.assertEqual(ledger.receipt_snapshot()['sha256'],before['sha256'])

if __name__=='__main__':unittest.main()
