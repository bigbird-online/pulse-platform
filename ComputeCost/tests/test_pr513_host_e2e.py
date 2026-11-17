"""PR513: actual TypeScript Host bytes -> registered collector -> query.

Synthetic model messages only; never launch a provider/runtime or read production.
"""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration


class HostReceiptRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = UsageLedger(self.root / 'ledger')

    def tearDown(self):
        self.tmp.cleanup()

    def host(self, runtime, body):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for real Host transport tests')
        module = Path(__file__).resolve().parents[2] / 'Relay/src/collaboration-capture.ts'
        spool = self.root / ('host-' + runtime)
        code = f"""
import {{createCollaborationCapture}} from {json.dumps(module.as_uri())};
let tick=0;
const capture=createCollaborationCapture({json.dumps(str(spool))},{{
 now:()=>new Date(Date.UTC(2026,8,13,1,0,0,tick++)).toISOString()
}});
const a=capture.begin({json.dumps(runtime)},{{sessionId:'synthetic-session',meteringAgentId:'pulse'}},'dispatch');
const raw={{input_tokens:100,output_tokens:10,cached_input_tokens:0,
 cache_read_input_tokens:0,cache_creation_input_tokens:0}};
{body}
"""
        subprocess.run([node, '--experimental-strip-types', '--input-type=module', '-e', code],
                       check=True, capture_output=True, text=True, timeout=20)
        return next(spool.glob('*.jsonl'))

    def collect(self, sources):
        collect_registered_sources({'schema': REGISTRY_SCHEMA, 'sources': sources}, self.ledger)
        return query_collaboration(self.ledger, CollaborationQuery())

    def test_cc_incremental_real_host_stream_and_duplicate_versions(self):
        source = self.host('cc', """
const message={type:'usage_observation',session_id:'synthetic-session',message:{
 id:'m1',model:'openai/test',usage:{...raw,output_tokens:0},stop_reason:null}};
capture.response(a,message);
capture.response(a,{...message,type:'assistant'});
const final={...message,message:{...message.message,usage:raw,stop_reason:'end_turn'}};
capture.response(a,final);capture.response(a,{...final,type:'assistant'});
const result={type:'result',modelUsage:{'openai/test':raw}};
capture.response(a,result);capture.response(a,result);
""")
        full = source.read_bytes()
        source.write_bytes(b''.join(full.splitlines(keepends=True)[:2]))
        registry = [{'id':'cc-host','kind':'relay_host','path':str(source)}]
        first = self.collect(registry)
        first_watermark = self.ledger.receipt_snapshot()['watermark']
        self.assertEqual(first['summary']['usage']['known_total_tokens'], 100)
        source.write_bytes(full)
        final = self.collect(registry)
        self.assertEqual(final['revision_conflicts'], [])
        self.assertEqual(final['summary']['usage']['known_total_tokens'], 110)
        self.assertEqual(final['reconciliation']['status'], 'reconciled')
        again = self.collect(registry)
        self.assertEqual(again['summary']['usage']['known_total_tokens'], 110)
        old = query_collaboration(self.ledger, CollaborationQuery(watermark=first_watermark))
        self.assertEqual(old['summary']['usage']['known_total_tokens'], 100)

    def test_cancelled_attempt_keeps_late_usage_once(self):
        source = self.host('pi', """
capture.finish(a,'cancelled');
const result={type:'result',session_id:'synthetic-session',usage:raw};
capture.response(a,result);capture.response(a,result);
""")
        report = self.collect([{'id':'pi-host','kind':'relay_host','path':str(source)}])
        self.assertEqual(report['summary']['usage']['known_total_tokens'],110)
        self.assertEqual(report['revision_conflicts'],[])

    def test_native_overlap_requires_the_explicit_source_inventory_witness(self):
        for runtime in ('pi','codex'):
            with self.subTest(runtime=runtime):
                self.ledger = UsageLedger(self.root / ('ledger-' + runtime))
                host = self.host(runtime, "capture.response(a,{type:'result',session_id:'synthetic-session',usage:raw});")
                native_dir = self.root / ('native-' + runtime)
                native_dir.mkdir()
                native = native_dir / 'source.jsonl'
                at = '2026-09-13T01:00:00Z'
                if runtime == 'pi':
                    rows = [{'type':'session','id':'synthetic-session'}, {'type':'message','id':'m1',
                        'timestamp':at,'message':{'role':'assistant','model':'openai/test','provider':'openai',
                            'usage':{'input':100,'output':10,'cacheRead':0,'cacheWrite':0,'totalTokens':110}}}]
                else:
                    rows = [{'type':'session_meta','timestamp':at,'payload':{'id':'synthetic-session'}},
                        {'type':'event_msg','timestamp':at,'payload':{'type':'task_started','turn_id':'t1'}},
                        {'type':'turn_context','timestamp':at,'payload':{'turn_id':'t1','model':'openai/test'}},
                        {'type':'event_msg','timestamp':at,'payload':{'type':'token_count','info':{'total_token_usage':{
                            'input_tokens':100,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':10}}}},
                        {'type':'event_msg','timestamp':at,'payload':{'type':'task_complete','turn_id':'t1'}}]
                native.write_text(''.join(json.dumps(r)+'\n' for r in rows));native.chmod(0o600)
                result = self.collect([{'id':'host','kind':'relay_host','path':str(host)},
                                       {'id':'native','kind':runtime,'path':str(native_dir)}])
                self.assertEqual(result['reconciliation']['status'], 'reconciled' if runtime == 'pi' else 'conflict')
                if runtime == 'pi':
                    self.assertEqual(result['summary']['usage']['known_total_tokens'], 110)
                    self.assertEqual(len(result['reconciliation']['native_boundary_supersession']), 1)
                # Removing the physical-source witness must retain the old
                # conservative behavior; mere same-scope counters are not proof.
                from pulse_compute_cost.receipts import current_receipts
                from pulse_compute_cost.collaboration_query import reconcile_usage
                active, _ = current_receipts(self.ledger.receipt_snapshot()['facts'])
                _, no_witness = reconcile_usage([r for r in active if r.kind == 'usage'])
                self.assertEqual(no_witness['status'], 'conflict')
                self.assertNotEqual(result['summary']['usage']['known_total_tokens'],220)
                self.assertIsNone(result['summary']['cost']['machine_cash_usd'])

    def test_scoped_native_model_proof_survives_actual_host_collector_and_pricing(self):
        from test_collaboration_receipts import CATALOG, AT
        for runtime in ('pi','codex'):
            with self.subTest(runtime=runtime):
                self.ledger=UsageLedger(self.root/('priced-'+runtime))
                source=self.host(runtime,"""
// A Codex result carries its thread's prior total; a new thread's is zero.
const result={type:'result',session_id:'synthetic-session',usage:raw,
 ...(a.runtime==='codex'?{thread_usage_baseline:Object.fromEntries(Object.keys(raw).map(k=>[k,0]))}:{}),
 native_model_usage:{schema:'pulse_native_model_usage/v1',runtime:a.runtime,
 models:[{model:'openai/test',provider:'openai',usage:raw}]}};
capture.response(a,result);capture.response(a,result);
""")
                registry={'schema':REGISTRY_SCHEMA,'sources':[{'id':'priced','kind':'relay_host','path':str(source)}]}
                collect_registered_sources(registry,self.ledger)
                out=query_collaboration(self.ledger,CollaborationQuery(scope='activity',from_date='2026-09-13',
                    to_date='2026-09-14',timezone='UTC'),catalog=CATALOG,catalog_observed_at=AT)
                self.assertEqual(out['summary']['usage']['known_total_tokens'],110)
                self.assertEqual(out['summary']['usage']['event_count'],1)
                self.assertEqual(out['summary']['cost']['partial_machine_cash_usd'],'110')
                self.assertEqual(collect_registered_sources(registry,self.ledger)['inserted'],0)
