"""A physical continuation segment need not inherit a nonzero counter."""
from pathlib import Path
import tempfile
import unittest
from pulse_compute_cost.receipts import Receipt, canonical, TOKEN_FIELDS
from pulse_compute_cost.receipt_sources import read_codex, collect_registered_sources
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from test_collaboration_receipts import AT, CATALOG

THREAD='00000000-0000-4000-8000-000000000001'
PART='00000000-0000-4000-8000-000000000002'


class CodexZeroOriginTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.logs=self.root/'sessions';self.logs.mkdir()
        self.path=self.logs/f'rollout-2026-09-13T01-00-00-{THREAD}_{PART}.jsonl'
        self.totals={'input_tokens':100,'output_tokens':2,'cached_input_tokens':20,'cache_write_input_tokens':0,'reasoning_output_tokens':0}
        self.ledger=UsageLedger(self.root/'ledger')

    def write(self,last=None):
        rows=[{'type':'session_meta','timestamp':AT,'payload':{'id':THREAD,'history_mode':'paginated','cwd':str(self.root),'originator':'Codex Desktop','source':'vscode'}},
              {'type':'event_msg','timestamp':AT,'payload':{'type':'task_started','turn_id':'turn-1'}},
              {'type':'turn_context','timestamp':AT,'payload':{'model':'openai/test'}},
              {'type':'event_msg','timestamp':AT,'payload':{'type':'token_count','info':{'total_token_usage':self.totals,'last_token_usage':self.totals if last is None else last}}},
              {'type':'event_msg','timestamp':AT,'payload':{'type':'task_complete','turn_id':'turn-1'}}]
        self.path.write_bytes(b''.join(canonical(r)+b'\n' for r in rows))

    def test_native_total_equal_last_proves_zero_without_a_parent_file(self):
        self.write();rows=read_codex(self.logs,AT)
        row=next(r for r in rows if r.kind=='usage')
        self.assertEqual(row.payload['reported_total_tokens'],102)
        self.assertEqual(row.payload['tokens'],{'uncached_input':80,'cache_read':20,'cache_write':0,'output':2})
        self.assertTrue(row.evidence)

    def test_unequal_native_last_does_not_prove_zero(self):
        self.write({**self.totals,'input_tokens':90})
        row=next(r for r in read_codex(self.logs,AT) if r.kind=='usage')
        self.assertIsNone(row.payload['reported_total_tokens'])

    def test_conflicting_known_quantity_is_not_overridden_by_a_valid_origin(self):
        self.write();actual=next(r for r in read_codex(self.logs,AT) if r.kind=='usage')
        changed={**actual.payload,'mapping':'codex_rollout_cumulative.v1',
                 'tokens':{**actual.payload['tokens'],'output':99},'reported_total_tokens':199,
                 'native_tokens':{**actual.payload['native_tokens'],'output_tokens':99}}
        self.ledger.ingest_receipts([Receipt.make('usage',None,changed,entity_ref=actual.entity_ref,
             occurred_at=AT,observed_at=AT,producer=actual.producer),actual])
        report=query_collaboration(self.ledger,CollaborationQuery(),catalog=CATALOG,catalog_observed_at=AT)
        self.assertGreater(report['summary']['usage']['conflicted_event_count'],0)

    def test_changed_model_is_not_reconciled_by_counter_equality(self):
        self.write();actual=next(r for r in read_codex(self.logs,AT) if r.kind=='usage')
        changed={**actual.payload,'mapping':'codex_rollout_cumulative.v1','model':'different'}
        self.ledger.ingest_receipts([Receipt.make('usage',None,changed,entity_ref=actual.entity_ref,
             occurred_at=AT,observed_at=AT,producer=actual.producer),actual])
        report=query_collaboration(self.ledger,CollaborationQuery(),catalog=CATALOG,catalog_observed_at=AT)
        self.assertGreater(report['summary']['usage']['conflicted_event_count'],0)

    def test_verified_origin_resolves_old_counted_and_unknown_versions_without_rewriting(self):
        self.write();actual=next(r for r in read_codex(self.logs,AT) if r.kind=='usage')
        counted={**actual.payload,'mapping':'codex_rollout_cumulative.v1','tokens':{'uncached_input':80,'cache_read':20,'cache_write':0,'output':2},'reported_total_tokens':102}
        unknown={**counted,'tokens':dict.fromkeys(TOKEN_FIELDS),'reported_total_tokens':None}
        for p in (counted,unknown):
            self.ledger.ingest_receipts([Receipt.make('usage',None,p,entity_ref=actual.entity_ref,occurred_at=actual.occurred_at,observed_at=AT,producer=actual.producer)])
        watermark=self.ledger.receipt_snapshot()['watermark']
        q=lambda mark=None:query_collaboration(self.ledger,CollaborationQuery(watermark=mark),catalog=CATALOG,catalog_observed_at=AT)
        before=q(watermark)
        self.assertGreater(before['summary']['usage']['conflicted_event_count'],0)
        reg={'schema':'computecost_source_registry/v1','sources':[{'id':'codex','kind':'codex','path':str(self.logs)}]}
        collect_registered_sources(reg,self.ledger)
        after=q()
        self.assertEqual(after['summary']['usage']['conflicted_event_count'],0)
        self.assertEqual(after['summary']['usage']['known_total_tokens'],102)
        self.assertEqual(q(watermark)['report_sha256'],before['report_sha256'])


if __name__=='__main__':unittest.main()
