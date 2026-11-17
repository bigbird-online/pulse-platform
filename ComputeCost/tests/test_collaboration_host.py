"""Actual TypeScript Host writer -> Python registry/ledger/CLI, using synthetic calls.

No provider request, production file, secret or natural-business claim is made.
"""
from __future__ import annotations
from dataclasses import replace
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import current_receipts
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from test_collaboration_receipts import (usage, delivery, review, complete, WI, OTHER,
    AT, PERIOD, CATALOG, coverage, value, reference)

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


class SourceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = UsageLedger(self.root/"ledger")
    def tearDown(self):
        self.tmp.cleanup()
    def registry(self, kind, path):
        return {"schema":"computecost_source_registry/v1", "ledger_root":str(self.ledger.root),
                "sources":[{"id":"source", "kind":kind, "path":str(path)}]}
    def cli(self, registry, *args):
        p=self.root/"sources.json"
        p.write_text(json.dumps(registry));p.chmod(0o600)
        result=subprocess.run([sys.executable,"-m","pulse_compute_cost",*args], cwd=ROOT,
            env={**os.environ,"PYTHONPATH":str(ROOT/"ComputeCost"),"PULSE_COMPUTECOST_SOURCES":str(p)},
            capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    @unittest.skipUnless(NODE, "Node 22+ required for actual Host source integration")
    def test_typescript_host_writer_to_cli_and_restart_idempotence(self):
        source=self.root/"host"
        module=(ROOT/"Relay/src/collaboration-capture.ts").as_uri()
        script=f'''
import {{ createCollaborationCapture }} from {json.dumps(module)};
let i=0;
const c=createCollaborationCapture({json.dumps(str(source))},{{now:()=>"2026-09-13T01:00:00Z",nonce:()=>String(i++)}});
for (const [wi,session] of [["{WI}","private-session-one"],["{OTHER}","private-session-two"]]) {{
  const binding={{work_item_id:wi,agent_id:"pulse",binding_sha256:"bound-"+wi}};
  const a=c.begin('cc',{{sessionId:session,computeCostBinding:binding}},'private-correlation');
  c.response(a,{{type:'usage_observation',message:{{id:'private-message',model:'openai/test',stop_reason:'end_turn',
    content:[{{type:'text',text:'NEVER-PUBLISH-CONTENT'}}],
    usage:{{input_tokens:20,output_tokens:10,cache_read_input_tokens:80,cache_creation_input_tokens:0}}}}}});
  c.response(a,{{type:'result',usage:{{input_tokens:20,output_tokens:10,cache_read_input_tokens:80,cache_creation_input_tokens:0}}}});
}}
c.human('private-human-id',Date.parse("2026-09-13T01:00:00Z"));
c.human('private-human-id',Date.parse("2026-09-13T01:00:00Z"));
'''
        result=subprocess.run([NODE,"--experimental-strip-types","--input-type=module","-e",script],
            capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        raw="".join(f.read_text() for f in source.glob("*.jsonl"))
        for forbidden in ('private-session','private-message','private-correlation','private-human','NEVER-PUBLISH'):
            self.assertNotIn(forbidden,raw)
        for f in source.glob("*.jsonl"):
            self.assertEqual(f.stat().st_mode&0o077,0)
        reg=self.registry("relay_host",source)
        first=self.cli(reg,"query","--from","2026-09-13","--to","2026-09-14","--timezone","UTC","--group-by","wi")
        self.assertEqual(first["report"]["summary"]["usage"]["known_total_tokens"],220)
        self.assertEqual(first["report"]["summary"]["human_attention"]["interaction_count"],1)
        self.assertEqual(len(first["report"]["reconciliation"]["suppressed_aggregate_refs"]),2)
        second=self.cli(reg,WI)
        self.assertEqual(second["collection"]["inserted"],0)
        self.assertEqual(second["report"]["summary"]["usage"]["known_total_tokens"],110)
        # Host stream proves its own attempts, not a complete WI or real business benefit.
        self.assertIsNone(second["report"]["summary"]["roi"]["ratio"])
        third=self.cli(reg,"query","--collaboration","--watermark",str(first["report"]["watermark"]),
                       "--from","2026-09-13","--to","2026-09-14")
        self.assertNotIn("collection",third)

    @unittest.skipUnless(NODE, "Node 22+ required for actual Host source integration")
    def test_host_native_and_authenticated_human_links_through_original_cli(self):
        source=self.root/"linked-host"
        module=(ROOT/"Relay/src/collaboration-capture.ts").as_uri()
        script=f'''
import {{ createCollaborationCapture,bindHumanMessageProof,humanMessageProof }} from {json.dumps(module)};
let i=0;
const c=createCollaborationCapture({json.dumps(str(source))},{{now:()=>"2026-09-13T01:00:02Z",nonce:()=>String(i++)}});
for (const [wi,session,key] of [["{WI}","native-one","a"],["{OTHER}","native-two","b"]]) {{
  const proof={{ref:key.repeat(64),observedAtMs:Date.parse("2026-09-13T01:00:00Z")}};
  const message={{type:'human',content:'PRIVATE USER CONTENT'}};
  bindHumanMessageProof(message,proof);c.human(proof.ref,proof.observedAtMs);
  c.human(proof.ref,proof.observedAtMs);
  const a=c.begin('cc',{{sessionId:session,computeCostBinding:{{work_item_id:wi,agent_id:'pulse',binding_sha256:key.repeat(64)}}}},'correlation');
  c.linkHuman(a,humanMessageProof(message));
  c.response(a,{{type:'assistant',message:{{id:'msg',model:'openai/test',stop_reason:'end_turn',
    usage:{{input_tokens:20,output_tokens:10,cache_read_input_tokens:80,cache_creation_input_tokens:0}}}}}});
  c.response(a,{{type:'result',usage:{{input_tokens:20,output_tokens:10,cache_read_input_tokens:80,cache_creation_input_tokens:0}}}});
}}
'''
        result=subprocess.run([NODE,"--experimental-strip-types","--input-type=module","-e",script],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        native=self.root/"native.jsonl"
        native.write_text(json.dumps({"type":"assistant","sessionId":"native-one","timestamp":"2026-09-13T01:00:02Z",
            "message":{"id":"msg","model":"openai/test","stop_reason":"end_turn",
            "usage":{"input_tokens":20,"output_tokens":10,"cache_read_input_tokens":80,"cache_creation_input_tokens":0}}})+"\n")
        reg=self.registry("relay_host",source)
        reg["sources"].append({"id":"cc-native","kind":"cc","path":str(native)})
        first=self.cli(reg,WI)["report"]
        self.assertEqual(first["summary"]["usage"]["known_total_tokens"],110)
        self.assertEqual(first["summary"]["human_attention"]["interaction_count"],1)
        second=self.cli(reg,OTHER)["report"]
        self.assertEqual(second["summary"]["human_attention"]["interaction_count"],1)
        all_rows=self.cli(reg,"query","--from","2026-09-13","--to","2026-09-14","--timezone","UTC")["report"]
        self.assertEqual(all_rows["summary"]["usage"]["known_total_tokens"],220)
        self.assertEqual(all_rows["summary"]["human_attention"]["interaction_count"],2)
        self.assertEqual(all_rows["revision_conflicts"],[])
        self.assertNotIn("PRIVATE USER CONTENT",json.dumps(all_rows))
        self.assertEqual(self.cli(reg,WI)["collection"]["inserted"],0)

    def test_valid_prefix_survives_torn_native_file_and_retry_completes(self):
        source=self.root/"cc.jsonl"
        row={"type":"assistant","sessionId":"private","timestamp":AT,"message":{"id":"first","model":"openai/test",
            "usage":{"input_tokens":10,"output_tokens":1,"cache_read_input_tokens":0,"cache_creation_input_tokens":0},"stop_reason":"end_turn"}}
        source.write_text(json.dumps(row)+"\n"+'{"type":')
        reg=self.registry("cc",source)
        result=collect_registered_sources(reg,self.ledger)
        self.assertEqual(result["sources"][0]["status"],"partial")
        active,_=current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertEqual(len([r for r in active if r.kind=="usage"]),1)
        row["message"]["id"]="second"
        source.write_text(source.read_text().splitlines()[0]+"\n"+json.dumps(row)+"\n")
        collect_registered_sources(reg,self.ledger)
        active,_=current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertEqual(len([r for r in active if r.kind=="usage"]),2)

    def test_canonical_harness_review_discovery_no_manual_bundle(self):
        from test_delivery_value import review_snapshot
        p=self.root/"harness/reviews";p.mkdir(parents=True)
        r=review_snapshot();(p/(r["work_item_id"]+".json")).write_text(json.dumps(r))
        result=self.cli(self.registry("harness_root",p.parent),r["work_item_id"])
        q=result["report"]["summary"]["quality"]
        self.assertEqual(q["delivery_count"],1);self.assertEqual(q["approved"]["numerator"],1)
        self.assertIsNone(q["first_pass"]["value"])
        self.assertIsNone(result["report"]["summary"]["roi"]["ratio"])
        # A review is not a new delivery acceptance. Pending acceptance remains explicit.
        self.assertEqual(q["accepted"]["denominator"],0)
        again=self.cli(self.registry("harness_root",p.parent),r["work_item_id"])
        self.assertEqual(again["collection"]["inserted"],0)

    def test_calendar_groups_do_not_replicate_delivery_quality(self):
        rows=[delivery(WI,at="2026-09-13T01:00:00Z"),delivery(OTHER,at="2026-09-14T01:00:00Z")]
        self.ledger.ingest_receipts(rows+[review(r) for r in rows])
        q=CollaborationQuery(scope="activity",from_date="2026-09-13",to_date="2026-09-15",timezone="UTC",group_by=("day",))
        r=query_collaboration(self.ledger,q)
        self.assertEqual(r["summary"]["quality"]["delivery_count"],2)
        self.assertEqual([g["quality"]["delivery_count"] for g in r["groups"]],[1,1])
        self.assertTrue(all(g["cost"]["machine_cash_usd"] is None for g in r["groups"]))

    def test_cross_day_aggregate_in_full_window_has_uncertain_group(self):
        r=usage(granularity="turn_aggregate",interval=("2026-09-13T23:00:00Z","2026-09-14T01:00:00Z"))
        self.ledger.ingest_receipts([r])
        q=CollaborationQuery(scope="activity",from_date="2026-09-13",to_date="2026-09-15",timezone="UTC",group_by=("day",))
        out=query_collaboration(self.ledger,q)
        self.assertEqual(out["groups"][0]["key"]["day"],"time_unallocated")
        self.assertIsNone(out["groups"][0]["roi"]["ratio"])

    def test_short_benefit_window_cannot_fill_a_long_query_period(self):
        rows=complete(); rows[-1]=value(end="2026-09-15T00:00:00Z")
        self.ledger.ingest_receipts(rows)
        q=CollaborationQuery(work_items=(WI,),**PERIOD)
        out=query_collaboration(self.ledger,q,catalog=CATALOG,catalog_observed_at=AT)["summary"]
        self.assertIsNone(out["roi"]["ratio"])
        self.assertIn("incomplete_value_period",out["roi"]["reasons"])

    def test_conflicting_assignments_preserve_quantity_in_ambiguous_bucket(self):
        from test_collaboration_receipts import fact
        u=usage()
        assignments=[fact("attribution",i,dict(target_ref=u.entity_ref,wi=wi,executor="pulse",responsible="pulse",
            delegator=None,state="assigned",policy="explicit.v1",effective_at="2026-09-01T00:00:00Z"))
            for i,wi in enumerate((WI,OTHER))]
        self.ledger.ingest_receipts([u]+assignments)
        out=query_collaboration(self.ledger,CollaborationQuery(group_by=("wi",)))
        self.assertEqual(out["groups"][0]["key"]["wi"],"ambiguous")
        self.assertEqual(out["summary"]["usage"]["known_total_tokens"],100)
        self.assertTrue(out["attribution_conflicts"])

    def test_human_absence_is_not_measured_zero(self):
        out=query_collaboration(self.ledger,CollaborationQuery(work_items=(WI,)))
        self.assertIsNone(out["summary"]["human_attention"]["interaction_count"])
        self.assertIsNone(out["summary"]["human_attention"]["focused_seconds_known"])

if __name__=="__main__":unittest.main()
