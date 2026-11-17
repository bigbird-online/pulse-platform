#!/usr/bin/env python3
"""Replay user-confirmed non-WI work against a private production-ledger copy."""
from __future__ import annotations
import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"ComputeCost"))
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import canonical, current_receipts
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.activity_classification import CONFIRMATIONS, FEATURE
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.stats_dashboard import _view, matches_query_cost


def run(output:Path):
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    home=Path.home();state=home/".local/state/pulse"
    original=json.loads((state/"compute-cost/sources.json").read_bytes())
    catalog_path=state/"stats/openrouter-catalog.json"
    catalog_bytes=catalog_path.read_bytes();catalog=json.loads(catalog_bytes)
    source=Path(original["ledger_root"])
    with tempfile.TemporaryDirectory(prefix="non-wi-replay-",dir=output) as tmp:
        target=Path(tmp)/"ledger"
        with (source/".usage-ledger.lock").open("rb") as lock:
            fcntl.flock(lock,fcntl.LOCK_SH);shutil.copytree(source,target)
        ledger=UsageLedger(target);snap=ledger.receipt_snapshot()
        now=datetime.now(timezone.utc);day=now.astimezone(ZoneInfo("Asia/Shanghai")).date()
        query=CollaborationQuery(scope="activity",from_date=(day-timedelta(days=day.weekday())).isoformat(),
            to_date=(day+timedelta(days=1)).isoformat(),timezone="Asia/Shanghai",watermark=snap["watermark"],
            group_by=("agent","wi","entrypoint","node","runtime","model"))
        def report(mark):
            return query_collaboration(ledger,replace(query,watermark=mark),catalog=catalog["catalog"],catalog_observed_at=catalog["retrieved_at"])
        before=report(snap["watermark"])
        declared=json.loads(CONFIRMATIONS.read_bytes())["items"][0]
        original_fact=next(r for r in snap["facts"] if r.fact_ref==declared["target_fact_ref"])
        assert original_fact.kind=="usage" and original_fact.payload["wi"] is None
        registry={"schema":"computecost_source_registry/v1","ledger_root":str(target),"sources":[],
                  "required_receipt_features":[FEATURE]}
        first=collect_registered_sources(registry,ledger)
        after=report(first["watermark"])
        second=collect_registered_sources(registry,ledger)
        assert second["inserted"]==0, "same reviewed correction was not idempotent"
        assert report(snap["watermark"])==before, "old watermark changed"
        assert ledger.receipt_snapshot(watermark=snap["watermark"])["sha256"]==snap["sha256"]
        for field in ("event_count","known_total_tokens","tokens"):
            assert before["summary"]["usage"][field]==after["summary"]["usage"][field],field
        assert before["summary"]["cost"]["partial_machine_cash_usd"]==after["summary"]["cost"]["partial_machine_cash_usd"]
        rows=[g for g in after["groups"] if original_fact.fact_ref in g["usage"]["fact_refs"]]
        assert len(rows)==1
        row=rows[0]
        assert row["key"]=={"agent":"duke","wi":"cron","entrypoint":"automation","node":"ownermac","runtime":"codex","model":"gpt-6-astra"}
        assert row["usage"]["event_count"]==1 and row["usage"]["known_total_tokens"]==410376
        assert row["cost"]["partial_machine_cash_usd"]=="1.06336"
        assert row["usage"]["wi_unassigned_event_count"]==0 and row["usage"]["agent_unassigned_event_count"]==0
        assert row["work_items"]==[]
        assert row["usage"]["wi_inventory_coverage"]=="not_applicable"
        assert "wi_execution_inventory_unknown" not in row["cost"]["reasons"]
        view=_view(after,"week","activity")
        published=next(g for g in view["groups"] if g["key"]==row["key"])
        assert published["usage"]["event_count"]==1 and matches_query_cost(published["cost"],row["cost"])
        assert catalog_path.read_bytes()==catalog_bytes
        verified=[]
        # Verify every reviewed historical correction which is present, not just
        # the initial cron example. No other empty WI/Agent is reclassified.
        for item in json.loads(CONFIRMATIONS.read_bytes())["items"]:
            fact=next((r for r in snap["facts"] if r.fact_ref==item["target_fact_ref"]),None)
            if fact is None: continue
            matched=[g for g in after["groups"] if fact.fact_ref in g["usage"]["fact_refs"]]
            if not matched: continue  # This correction belongs to another week.
            assert len(matched)==1
            group=matched[0]
            expected={"agent":item["agent"],"entrypoint":item["entrypoint"],"node":item["node"]}
            if "work_kind" in item: expected["wi"]=item["work_kind"]
            if "work_item_id" in item: expected["wi"]=item["work_item_id"]
            for key,value in expected.items():
                assert group["key"][key]==value,(key,value,group["key"])
            verified.append({"fact_ref":fact.fact_ref,"key":group["key"],"tokens":fact.payload["tokens"],
                             "row_events":group["usage"]["event_count"],"row_cost":group["cost"]["partial_machine_cash_usd"]})
        today_q=replace(query,from_date=day.isoformat(),watermark=second["watermark"])
        today=query_collaboration(ledger,today_q,catalog=catalog["catalog"],catalog_observed_at=catalog["retrieved_at"])
        for checked in (today,after):
            for field in ("agent_unassigned_event_count","wi_unassigned_event_count","conflicted_event_count","time_unallocated_event_count"):
                assert checked["summary"]["usage"][field]==0,(field,checked["summary"]["usage"][field])
            assert all(g["key"]["entrypoint"] not in {"unassigned","ambiguous"} for g in checked["groups"] if g["usage"]["event_count"])
        from pulse_compute_cost.stats_dashboard import build_dashboard, DEFAULT_FACTS
        doc=build_dashboard(ledger,subscriptions=json.loads(DEFAULT_FACTS.read_bytes()),
            source_revision=__import__("os").environ["PULSE_SOURCE_REVISION"],now=datetime.now(timezone.utc),
            catalog_snapshot=catalog,identity_dimensions=True,activity_table=True)
        sys.path.insert(0,str(ROOT/"Stats/exporter/tests"))
        from prepare_cost_consumer_replay import record_case
        replay=output.parent/"consumer-replay"
        case=record_case(replay,"real-reviewed-activity",doc,datetime.fromisoformat(doc["generated_at"]))
        index=replay/"index.json";cases=json.loads(index.read_bytes());cases.append(case);index.write_text(json.dumps(cases))
        result={"source_revision":__import__("os").environ.get("PULSE_SOURCE_REVISION"),"scope":"reviewed_activity_and_published_week_projection_not_new_three_node_collection",
            "verified_activities":verified,"today_events":today["summary"]["usage"]["event_count"],"week_events":after["summary"]["usage"]["event_count"],"today_week_identity_clean":True,
            "observed_at":now.isoformat(),"window":[query.from_date,query.to_date],"original_watermark":snap["watermark"],
            "corrected_watermark":first["watermark"],"query_group":row,
            "before_identity":{k:v for k,v in before["summary"]["usage"].items() if "unassigned" in k},
            "after_identity":{k:v for k,v in after["summary"]["usage"].items() if "unassigned" in k or k in {"non_wi_event_count","cron_event_count","no_wi_event_count"}},
            "events_tokens_and_amount_unchanged":True,"old_watermark_unchanged":True,"idempotent":True,
            "publisher_matches_query":True,"catalog_sha256":hashlib.sha256(catalog_bytes).hexdigest(),"production_modified":False}
        (output/"summary.json").write_bytes(canonical(result)+b"\n")
        print(json.dumps(result,ensure_ascii=False),flush=True)
    return result

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--output",required=True,type=Path);a=p.parse_args();run(a.output)
