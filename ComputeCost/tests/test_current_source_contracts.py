"""WI568: actual runtime layout, per-file failure and truthful time diagnostics."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources, REGISTRY_SCHEMA
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration

AT='2026-09-14T12:00:00Z'
def pi_bytes(session='s',entry='m'):
    return ''.join(json.dumps(r)+'\n' for r in [
        {'type':'session','id':session,'timestamp':AT,'version':3},
        {'type':'message','id':entry,'timestamp':AT,'message':{'role':'assistant','model':'openai/test',
         'provider':'openai','usage':{'input':100,'cacheRead':20,'cacheWrite':0,'output':10,'totalTokens':130}}}])

class CurrentSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.source=self.root/'sessions';self.source.mkdir(mode=0o700)
        self.ledger=UsageLedger(self.root/'ledger')
    def write(self,name,data):
        p=self.source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data);p.chmod(0o600);return p
    def collect(self,**extra):
        return collect_registered_sources({'schema':REGISTRY_SCHEMA,'sources':[{'id':'local-relay-pi','kind':'pi',
            'path':str(self.source),**extra}]},self.ledger)
    def test_invalid_file_never_truncates_remaining_native_files(self):
        self.write('a-bad.jsonl','{"type":"alien"}\n');self.write('z-good.jsonl',pi_bytes())
        result=self.collect();out=query_collaboration(self.ledger,CollaborationQuery())
        self.assertEqual(out['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(result['sources'][0]['status'],'partial')
        self.assertEqual(result['sources'][0]['source_detail']['files_invalid'],1)
    def test_damaged_cc_transcript_is_local_and_all_diagnostics_are_counted(self):
        good={'type':'assistant','sessionId':'good','uuid':'u','timestamp':AT,'message':{'id':'m','model':'openai/test',
              'stop_reason':'end_turn','usage':{'input_tokens':100,'output_tokens':10,'cache_read_input_tokens':20,
              'cache_creation_input_tokens':0}}}
        self.write('a-bad.jsonl','{"type":"assistant","message":{}}\n');self.write('z-good.jsonl',json.dumps(good)+'\n')
        result=collect_registered_sources({'schema':REGISTRY_SCHEMA,'sources':[{'id':'cc','kind':'cc',
            'path':str(self.source)}]},self.ledger)
        source=result['sources'][0]
        self.assertEqual(query_collaboration(self.ledger,CollaborationQuery())['summary']['usage']['known_total_tokens'],130)
        self.assertEqual((source['status'],source['reason']),('partial','native_files_incomplete'))
        self.assertEqual(source['diagnostics'],{'native_files_incomplete':1})
        self.assertEqual(source['source_detail']['errors'],{'invalid_native_record':1})
    def test_runtime_layout_never_descends_into_tenant_agent_home(self):
        self.write('tenant/live.jsonl',pi_bytes())
        self.write('tenant/agent/.cache/package.jsonl','{"etag":"not-a-session"}')
        self.write('tenant/agent/.codex/sessions/copied.jsonl','{"type":"session_meta"}\n')
        self.write('tenant/agent/copied-pi.jsonl',pi_bytes('copied'))
        result=self.collect(layout='pi_session_root.v1')
        self.assertEqual(result['sources'][0]['fact_count'],1)
        self.assertEqual(result['sources'][0]['source_detail']['files_invalid'],0)
        self.assertEqual(result['sources'][0]['source_detail']['excluded_directories'],1)
    def test_append_after_partial_tail_recovers_and_does_not_recount(self):
        p=self.write('t/live.jsonl',pi_bytes()+'{"type":')
        self.write('u/live.jsonl',pi_bytes('second'))
        first=self.collect(layout='pi_session_root.v1');self.assertEqual(first['sources'][0]['source_detail']['files_invalid'],1)
        self.assertEqual(query_collaboration(self.ledger,CollaborationQuery())['summary']['usage']['known_total_tokens'],260)
        p.write_text(pi_bytes())
        self.collect(layout='pi_session_root.v1'); again=self.collect(layout='pi_session_root.v1')
        self.assertEqual(again['inserted'],0)
    def test_usage_time_and_unassigned_scale_survive_query_and_publisher(self):
        self.write('t/live.jsonl',pi_bytes());self.collect(layout='pi_session_root.v1')
        from pulse_compute_cost.stats_dashboard import _view
        q=CollaborationQuery(from_date='2026-09-14',to_date='2026-09-15',scope='activity',group_by=('wi',),watermark=self.ledger.receipt_snapshot()['watermark'])
        report=query_collaboration(self.ledger,q);usage=report['summary']['usage']
        self.assertEqual(usage['last_usage_at'],AT)
        self.assertEqual(usage['unassigned_event_count'],1)
        self.assertEqual(usage['unassigned_known_total_tokens'],130)
        view=_view(report,'today','wi')
        self.assertEqual(view['summary']['usage']['last_usage_at'],AT)
        self.assertEqual(view['summary']['usage']['source_details'][0]['label'],'local-relay-pi')

    def test_unknown_compaction_timestamp_does_not_advance_effective_usage_time(self):
        self.write('t/live.jsonl',pi_bytes()+json.dumps({'type':'compaction','id':'c','timestamp':'2026-09-14T14:00:00Z'})+'\n')
        self.collect(layout='pi_session_root.v1')
        report=query_collaboration(self.ledger,CollaborationQuery())
        self.assertEqual(report['summary']['usage']['last_usage_at'],AT)

    def test_past_wi_conflict_does_not_poison_a_proven_later_window(self):
        from pulse_compute_cost.receipt_sources import usage_receipt
        from pulse_compute_cost.receipts import reference
        common={'wi':'wi_20260914_123','executor':'pulse','responsible':None,'delegator':None}
        old='2026-09-13T01:00:00Z';scope=reference('native_session',['pi','past-conflict'])
        base={'input_tokens':100,'output_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':0}
        first=usage_receipt('pi','old-detail',base,at=old,observed=old,model='openai/test',mapping='relay_pi.v1',common=common,native_scope_ref=scope)
        second=usage_receipt('pi','old-summary',{**base,'input_tokens':999},at=old,observed=old,model='openai/test',mapping='relay_pi.v1',common=common,
            native_scope_ref=scope,granularity='turn_aggregate',interval=('2026-09-13T00:59:00Z','2026-09-13T01:01:00Z'))
        today=usage_receipt('pi','today',base,at=AT,observed=AT,model='openai/test',mapping='relay_pi.v1',common=common)
        self.ledger.ingest_receipts([first,second,today])
        q=CollaborationQuery(from_date='2026-09-14',to_date='2026-09-15',scope='activity',work_items=(common['wi'],))
        out=query_collaboration(self.ledger,q)
        self.assertEqual(out['summary']['usage']['known_total_tokens'],130)
        self.assertNotIn('join_conflict',out['summary']['cost']['reasons'])
        self.assertEqual(out['summary']['usage']['conflicted_event_count'],0)
    def test_source_diagnostics_include_harness_without_duplicate_domain_rows(self):
        from pulse_compute_cost.receipts import Receipt, reference
        detail={'label':'mac.harness','kind':'harness_root','layout':'registered_recursive.v1','files_seen':2,'files_read':1,
                'files_invalid':1,'excluded_directories':0,'latest_usage_at':None,'errors':{'invalid_native_record':1}}
        for domain in ('execution','human','quality','value'):
            self.ledger.ingest_receipts([Receipt.make('coverage',['harness',domain],{
                'domain':domain,'wi':None,'source_ref':reference('source','harness'),'status':'partial','expected':None,'observed':None,
                'watermark':None,'reason':'source_unreadable_or_invalid','source_detail':detail},occurred_at=None,observed_at=AT,producer='registered_collector.v1')])
        out=query_collaboration(self.ledger,CollaborationQuery())
        sources=out['summary']['usage']['source_details']
        self.assertEqual(len(sources),1)
        self.assertEqual(sources[0]['label'],'mac.harness')
        self.assertEqual(sources[0]['status'],'partial')

if __name__=='__main__':unittest.main()
