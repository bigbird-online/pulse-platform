"""Fixed-plan counterexamples. All identities/amounts here are synthetic."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost.receipts import Receipt, ReceiptError, reference, current_receipts, TOKEN_FIELDS
from pulse_compute_cost.ledger import UsageLedger, LedgerError
from pulse_compute_cost.receipt_sources import (normalize_usage, usage_receipt, read_host, read_pi,
    read_cc, collect_registered_sources, import_harness_bundle)
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration, reconcile_usage

WI = "wi_20260913_101"
OTHER = "wi_20260913_102"
AT = "2026-09-13T01:00:00Z"
LATER = "2026-09-14T01:00:00Z"
PERIOD = {"value_from": "2026-09-01T00:00:00Z", "value_to": "2026-10-01T00:00:00Z"}
CATALOG = {"data": [{"id": "openai/test", "pricing": {"prompt": "1", "completion": "1",
    "input_cache_read": "0.1", "input_cache_write": "2"}}]}


def fact(kind, identity, payload, at=AT, **kwargs):
    return Receipt.make(kind, identity, payload, occurred_at=at, observed_at=AT,
                        producer="synthetic_test.v1", **kwargs)


def execution(wi=WI, identity="exec", state="completed", agent="pulse"):
    return fact("execution", identity, dict(wi=wi, executor=agent, responsible=agent, delegator=None,
        runtime="cc", assignment_ref=reference("assignment", [wi, identity]), parent_ref=None,
        state=state, expected_usage=True))


def usage(wi=WI, identity="usage", n=100, *, ex=None, at=AT, agent="pulse", **kwargs):
    return usage_receipt("cc", identity, {"input_tokens": n, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, at=at, observed=AT,
        model="openai/test", execution_ref=(ex or execution(wi=wi)).entity_ref,
        common=dict(wi=wi, executor=agent, responsible=agent, delegator=None), **kwargs)


def coverage(domain="usage", wi=WI, expected=1, observed=1, status="complete"):
    return fact("coverage", [domain, wi], dict(domain=domain, wi=wi,
        source_ref=reference("source", [domain, wi]), status=status, expected=expected,
        observed=observed, watermark="synthetic", reason=None), at=None)


def delivery(wi=WI, candidate="v1", status="accepted", at=AT, identity=None, **kwargs):
    return fact("delivery", identity or wi, dict(wi=wi, responsible="pulse",
        candidate_ref=reference("candidate", [wi, candidate]), artifact_ref=reference("artifact", [wi, candidate]),
        acceptance_ref=reference("acceptance", wi), status=status,
        accepted_at=at if status=="accepted" else None), at=at, **kwargs)


def review(d=None, identity="r1", decision="approved", first=True, **changes):
    d = d or delivery()
    payload = dict(wi=d.payload["wi"], delivery_ref=d.entity_ref, candidate_ref=d.payload["candidate_ref"],
        reviewer="human", decision=decision, first_pass=first, rework_rounds=0,
        post_approval_corrections=0, rubric="synthetic_rubric.v1", score=None,
        verification="human_review", critical=[])
    payload.update(changes)
    return fact("review", [d.entity_ref, identity], payload)


def value(d=None, identity="val1", n="110", mode="cumulative", start=None, end=None, **changes):
    d = d or delivery()
    payload = dict(wi=d.payload["wi"], delivery_ref=d.entity_ref, artifact_ref=d.payload["artifact_ref"],
        effect_ref=reference("effect", d.entity_ref), consumer="consumer", verifier="independent",
        amount=n, unit="USD", mode=mode, period_start=start or PERIOD["value_from"],
        period_end=end or PERIOD["value_to"], realization="observed", complete=True, allocations=[])
    payload.update(changes)
    return fact("value", [d.entity_ref, identity], payload)


def complete(wi=WI, cost=100, benefit="110", agent="pulse"):
    e, d = execution(wi, identity=[wi,"exec"], agent=agent), delivery(wi)
    return [e, usage(wi, identity=[wi,"usage"], n=cost, ex=e, agent=agent),
            coverage("usage",wi), coverage("execution",wi), coverage("value",wi),
            d, review(d), value(d, n=benefit)]


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026,9,13,2,tzinfo=timezone.utc)
        self.ledger = UsageLedger(self.root/"ledger", clock=lambda:self.now)
    def tearDown(self):
        self.temp.cleanup()
    def query(self, rows=(), **kwargs):
        self.ledger.ingest_receipts(rows)
        return query_collaboration(self.ledger, CollaborationQuery(**kwargs), catalog=CATALOG, catalog_observed_at=AT)
    def write(self, name, rows):
        p = self.root/name
        p.write_text("".join(json.dumps(r)+"\n" for r in rows))
        return p

    def test_A01_explicit_overlap_independent_and_unknown(self):
        e=execution()
        a,b=usage(identity="a",n=110,ex=e),usage(identity="b",n=110,ex=e)
        agg=usage(identity="agg",n=220,ex=e,granularity="turn_aggregate",covers=[a.entity_ref,b.entity_ref])
        kept,diagnostic=reconcile_usage([a,b,agg])
        self.assertEqual(sum(r.payload["tokens"]["uncached_input"] for r in kept),220)
        self.assertEqual(diagnostic["suppressed_aggregate_refs"],[agg.entity_ref])
        independent=usage(identity="agg2",n=220,ex=execution(identity="separate"),granularity="turn_aggregate")
        kept,_=reconcile_usage([a,b,independent]); self.assertEqual(len(kept),3)
        unknown=usage(identity="agg3",n=220,ex=e,granularity="turn_aggregate")
        kept,diagnostic=reconcile_usage([a,b,unknown]); self.assertEqual(kept,[])
        self.assertEqual(diagnostic["status"],"conflict")

    def test_A01_mismatch_and_partial_overlap_not_valid_subtotal(self):
        a=usage(identity="a",n=110)
        agg=usage(identity="agg",n=220,granularity="turn_aggregate",covers=[a.entity_ref,reference("usage","missing")])
        kept,diagnostic=reconcile_usage([a,agg]); self.assertEqual(kept,[])
        self.assertEqual(diagnostic["status"],"conflict")

    def test_A02_missing_not_zero_and_cache_mapping(self):
        self.assertEqual(normalize_usage("cc",None),dict.fromkeys(TOKEN_FIELDS))
        self.assertEqual(normalize_usage("codex",{"input_tokens":100,"cached_input_tokens":80,"output_tokens":2},mapping="codex_cli.v1"),
            dict(uncached_input=20,cache_read=80,cache_write=0,output=2))
        with self.assertRaises(ReceiptError): normalize_usage("codex",{"input_tokens":10,"cached_input_tokens":11},mapping="codex_cli.v1")
        with self.assertRaises(ReceiptError): normalize_usage("cc",{"input_tokens":True})
        with self.assertRaises(ReceiptError): normalize_usage("cc",{"input_tokens":1,"inputTokens":2})

    def test_A02_pi_compaction_does_not_inherit_model(self):
        p=self.write("pi.jsonl",[{"type":"session","id":"session"},
            {"type":"model_change","id":"switch","modelId":"openai/test"},
            {"type":"compaction","id":"compact","timestamp":AT,"usage":{"input":10,"cacheRead":0,"cacheWrite":0,"output":2}}])
        rows=read_pi(p,AT); self.assertEqual(len(rows),1); self.assertIsNone(rows[0].payload["model"])
        self.assertEqual(rows[0].payload["tokens"]["uncached_input"],10)

    def test_A03_cc_growth_replay_and_changed_final_conflict(self):
        def row(n, final=False):
            return {"type":"assistant","sessionId":"s","timestamp":AT,"message":{"id":"m","model":"openai/test",
                "usage":{"input_tokens":n,"output_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0},
                "stop_reason":"end_turn" if final else None}}
        p=self.write("cc.jsonl",[row(10),row(10),row(20,True)])
        rows=read_cc(p,AT); self.assertEqual(len(rows),2); self.assertEqual(rows[1].supersedes,rows[0].fact_ref)
        self.ledger.ingest_receipts(rows); self.assertEqual(self.ledger.ingest_receipts(read_cc(p,LATER))["inserted"],0)
        with p.open("a") as h:h.write(json.dumps(row(30,True))+"\n")
        self.ledger.ingest_receipts(read_cc(p,LATER)); active,conflicts=current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertIn(rows[0].entity_ref,conflicts)

    def test_A04_atomic_failure_retry_and_private_files(self):
        r=usage()
        with patch("pulse_compute_cost.ledger.os.replace",side_effect=OSError("disk full")):
            with self.assertRaises(LedgerError):self.ledger.ingest_receipts([r])
        self.assertEqual(self.ledger.receipt_snapshot()["facts"],[])
        self.assertEqual(self.ledger.ingest_receipts([r])["inserted"],1)
        self.assertEqual(self.ledger.ingest_receipts([r])["inserted"],0)
        for p in (self.ledger.root/"receipts").glob("*.json"):self.assertEqual(p.stat().st_mode&0o077,0)

    def test_A04_missing_attempt_usage_preserves_gap(self):
        result=self.query([execution(state="failed"),coverage(),coverage("execution")],work_items=(WI,))
        self.assertEqual(len(result["summary"]["usage"]["missing_execution_refs"]),1)
        self.assertIsNone(result["summary"]["cost"]["machine_cash_usd"])

    def test_A06_ingestion_asof_not_backdated_source_time(self):
        u=usage(wi=None)
        self.ledger.ingest_receipts([u]); old_time=self.ledger.receipt_snapshot()["recorded_at"]
        self.now=datetime(2026,9,15,tzinfo=timezone.utc)
        a=fact("attribution","assign",dict(target_ref=u.entity_ref,wi=WI,executor="pulse",responsible="pulse",delegator=None,
            state="assigned",policy="human_assignment.v1",effective_at="2026-09-12T00:00:00Z"))
        self.ledger.ingest_receipts([a])
        old=self.query(as_of=old_time,group_by=("wi",)); new=self.query(group_by=("wi",))
        self.assertEqual(old["groups"][0]["key"]["wi"],"unassigned")
        self.assertEqual(new["groups"][0]["key"]["wi"],WI)
        self.assertEqual(old["summary"]["usage"]["known_total_tokens"],new["summary"]["usage"]["known_total_tokens"])

    def test_A07_join_does_not_multiply_cost_delivery_or_value(self):
        d=delivery()
        rows=complete()[:-2]+[review(d,identity="r1"),review(d,identity="r2"),review(d,identity="r3"),
            value(d,identity="v1",n="40",mode="increment",end="2026-09-15T00:00:00Z"),
            value(d,identity="v2",n="70",mode="increment",start="2026-09-15T00:00:00Z")]
        r=self.query(rows,work_items=(WI,),**PERIOD)["summary"]
        self.assertEqual(r["cost"]["machine_cash_usd"],"100")
        self.assertEqual(r["quality"]["delivery_count"],1); self.assertEqual(r["quality"]["review_activity_count"],3)
        self.assertEqual(r["business_value"]["known_usd"],"110");self.assertEqual(r["roi"]["ratio"],"0.1")

    def test_A08_quality_denominators_preserve_pending(self):
        ds=[delivery(identity=str(i)) for i in range(3)]
        rows=ds+[review(ds[0]),review(ds[1],decision="changes_requested",first=False)]
        q=self.query(rows,work_items=(WI,))["summary"]["quality"]
        self.assertEqual((q["approved"]["numerator"],q["approved"]["denominator"],q["approved"]["eligible"]),(1,2,3))
        self.assertEqual(q["pending"],1); self.assertEqual(q["first_pass"]["value"],"0.5")

    def test_A09_new_candidate_not_old_approval(self):
        d=delivery(); self.ledger.ingest_receipts([d,review(d)])
        self.ledger.ingest_receipts([delivery(candidate="v2",supersedes=d.fact_ref)])
        q=self.query(work_items=(WI,))["summary"]["quality"]
        self.assertEqual(q["delivery_count"],1);self.assertEqual(q["approved"]["denominator"],0)

    def test_A09_conflicting_review_not_last_writer_wins(self):
        d=delivery();q=self.query([d,review(d),review(d,identity="other",decision="changes_requested",reviewer="other")],work_items=(WI,))["summary"]["quality"]
        self.assertEqual(q["conflicts"],1);self.assertIsNone(q["approved"]["value"])

    def test_A10_combined_roi_not_average(self):
        r=self.query(complete(cost=10,benefit="20")+complete(OTHER,cost=90,benefit="90"),work_items=(WI,OTHER),group_by=("wi",),**PERIOD)
        self.assertEqual(r["summary"]["roi"]["ratio"],"0.1")
        self.assertEqual({g["roi"]["ratio"] for g in r["groups"]},{"1","0"})

    def test_A10_unknown_wi_not_dropped(self):
        r=self.query(complete(),work_items=(WI,OTHER),**PERIOD)["summary"]
        self.assertIsNone(r["roi"]["ratio"]);self.assertEqual(r["roi"]["cohort_count"],2)

    def test_A11_zero_negative_unknown_values_distinct(self):
        for amount,expected in (("0","-1"),("-10","-1.1"),(None,None)):
            with self.subTest(amount=amount):
                ledger=UsageLedger(self.root/("test"+str(amount)))
                ledger.ingest_receipts(complete(benefit=amount))
                r=query_collaboration(ledger,CollaborationQuery(work_items=(WI,),**PERIOD),catalog=CATALOG,catalog_observed_at=AT)
                self.assertEqual(r["summary"]["roi"]["ratio"],expected)

    def test_A11_zero_cost_not_infinite_roi(self):
        r=self.query(complete(cost=0),work_items=(WI,),**PERIOD)["summary"]
        self.assertIsNone(r["roi"]["ratio"]);self.assertIn("zero_denominator",r["roi"]["reasons"])

    def test_A12_agent_allocation_required_and_conserving(self):
        rows=complete();r=self.query(rows,work_items=(WI,),agent="pulse",**PERIOD)["summary"]
        self.assertIsNone(r["roi"]["ratio"]);self.assertIn("allocation_unknown",r["roi"]["reasons"])
        old=rows[-1];p=old.payload;p["allocations"]=[dict(role="executor",agent="pulse",share="1",policy="human-approved.v1")]
        revised=fact("value",None,p,entity_ref=old.entity_ref,supersedes=old.fact_ref)
        r=self.query([revised],work_items=(WI,),group_by=("agent",),**PERIOD)
        self.assertEqual(r["groups"][0]["roi"]["ratio"],"0.1")

    def test_A13_cumulative_not_sum_or_replay(self):
        rows=complete();d=delivery()
        rows.extend([value(d,identity="early",n="40",end="2026-09-15T00:00:00Z"),value(d,identity="repeat",n="110")])
        r=self.query(rows,work_items=(WI,),**PERIOD)["summary"]
        self.assertEqual(r["business_value"]["known_usd"],"110")

    def test_A14_crossmonth_activity_vs_accepted_cohort(self):
        rows=complete(cost=60,benefit="120");e=rows[0]
        rows.append(usage(identity="august",n=40,ex=e,at="2026-08-20T00:00:00Z"))
        r=self.query(rows,work_items=(WI,),scope="activity",from_date="2026-09-01",to_date="2026-10-01",**PERIOD)["summary"]
        self.assertEqual(r["usage"]["known_total_tokens"],60);self.assertIsNone(r["roi"]["ratio"])
        r=self.query(work_items=(WI,),scope="accepted_wi",from_date="2026-09-01",to_date="2026-10-01",**PERIOD)["summary"]
        self.assertEqual(r["cost"]["machine_cash_usd"],"100");self.assertEqual(r["roi"]["ratio"],"0.2")

    def test_A15_crossmidnight_aggregate_not_all_at_end(self):
        row=usage(granularity="turn_aggregate",interval=("2026-09-12T15:50:00Z","2026-09-12T16:10:00Z"))
        r=self.query([row],scope="activity",from_date="2026-09-13",to_date="2026-09-14",timezone="Asia/Shanghai")
        self.assertEqual(r["summary"]["usage"]["event_count"],0)
        self.assertEqual(r["time_unallocated"]["fact_refs"],[row.fact_ref])

    def test_A15_dst_not_24_hours(self):
        q=CollaborationQuery(scope="activity",from_date="2026-11-01",to_date="2026-11-02",timezone="America/Los_Angeles")
        self.assertEqual((q.window[1].astimezone(timezone.utc)-q.window[0].astimezone(timezone.utc)).total_seconds(),25*3600)

    def test_A16_human_redelivery_not_new_input(self):
        r=dict(schema="pulse_collaboration_host_event/v1",event_ref=reference("event","human"),kind="human",at=AT,
            human_ref=reference("human","source"))
        p=self.write("host.jsonl",[r,r]); rows=read_host(p,AT);self.assertEqual(len(rows),1)
        # A resend differing only in emission time keeps the earliest event.
        r2={**r,"at":LATER};p=self.write("resend.jsonl",[r,r2])
        rows=read_host(p,AT);self.assertEqual([x.occurred_at for x in rows],["2026-09-13T01:00:00Z"])
        r3={**r2,"human_ref":reference("human","other")};p=self.write("conflict.jsonl",[r,r3])
        with self.assertRaises(ReceiptError):read_host(p,AT)


    def test_settlement_may_only_withdraw_a_usage_expectation(self):
        def event(state, expected, n):
            return dict(schema="pulse_collaboration_host_event/v1", event_ref=reference("event", [state, str(expected)]),
                kind="execution", at=AT, started_at=AT, attempt_ref=reference("attempt", "queued"), runtime="cc",
                wi=None, executor="pulse", responsible=None, delegator=None, assignment_ref=None, parent_ref=None,
                state=state, expected_usage=expected, writer_ref=reference("writer", "w"), sequence=n, lost_events=0)
        rows = read_host(self.write("withdrawn.jsonl", [event("accepted", True, 1), event("completed", False, 2)]), AT)
        self.assertEqual([(r.payload["state"], r.payload["expected_usage"], bool(r.supersedes)) for r in rows],
                         [("accepted", True, False), ("completed", False, True)])
        # An expectation is never added, and never withdrawn without settlement.
        for first, later in ((event("accepted", False, 1), event("completed", True, 2)),
                             (event("accepted", True, 1), event("running", False, 2))):
            rows = read_host(self.write("other.jsonl", [first, later]), AT)
            self.assertFalse(rows[-1].supersedes)

    def test_registered_host_principal_revision_is_append_only_and_actor_safe(self):
        host = self.root / "host"; host.mkdir()
        attempt = reference("attempt", "alias-revision")
        scope = reference("native_session", ["pi", "alias-revision"])
        events = [
            dict(schema="pulse_collaboration_host_event/v1",
                event_ref=reference("event", "alias-execution"), kind="execution",
                at=AT, started_at=AT, attempt_ref=attempt,
                runtime="pi", state="completed", expected_usage=True,
                wi=WI, executor="tcloud-pulse", responsible=None, delegator=None),
            dict(schema="pulse_collaboration_host_event/v1",
                event_ref=reference("event", "alias-usage"), kind="usage",
                at=LATER, started_at=AT, attempt_ref=attempt, runtime="pi",
                usage={"input_tokens":10,"cache_read_input_tokens":20,
                       "cache_creation_input_tokens":0,"output_tokens":2},
                mapping="relay_pi.v1", granularity="turn_aggregate",
                native_scope_ref=scope, wi=WI, executor="tcloud-pulse",
                responsible=None, delegator=None),
        ]
        path = host / "events.jsonl"
        path.write_text("".join(json.dumps(row)+"\n" for row in events))
        registry = {"schema":"computecost_source_registry/v1",
            "ledger_root":str(self.ledger.root), "sources":[{
                "id":"host","kind":"relay_host","path":str(host),
                "node":"tcloud","actor_attribution":True}]}
        collect_registered_sources(registry, self.ledger)
        old = self.ledger.receipt_snapshot()["watermark"]
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertFalse(conflicts)
        old_execution = next(r for r in active if r.kind=="execution" and r.entity_ref==attempt)
        old_usage = next(r for r in active if r.kind=="usage" and r.payload.get("execution_ref")==attempt)
        self.assertEqual(old_execution.payload["executor"], "tcloud-pulse")
        self.assertEqual(old_usage.payload["executor"], "tcloud-pulse")
        old_tokens = old_usage.payload["tokens"]

        history = [{"alias":"tcloud-pulse","principal":"pulse",
            "effective_from":"2026-09-13T00:59:00Z","config_sha256":"b"*64}]
        registry["sources"][0]["principal_alias_history"] = history
        collect_registered_sources(registry, self.ledger)
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertFalse(conflicts)
        execution_now = next(r for r in active if r.kind=="execution" and r.entity_ref==attempt)
        usage_now = next(r for r in active if r.kind=="usage" and r.payload.get("execution_ref")==attempt)
        self.assertEqual(execution_now.payload["executor"], "pulse")
        self.assertEqual(usage_now.payload["executor"], "pulse")
        self.assertEqual(usage_now.payload["tokens"], old_tokens)
        actor_executors = {
            r.payload["executor"] for r in active
            if r.kind=="attribution" and r.payload.get("scope")=="actor"
            and r.payload.get("target_ref") in {attempt, usage_now.entity_ref}
        }
        self.assertEqual(actor_executors, {"pulse"})
        # The next timer tick and a reopened collector must not restore alias
        # identities from retained raw source history or mint another revision.
        stable = self.ledger.receipt_snapshot()["sha256"]
        for _ in range(3):
            reopened = UsageLedger(self.ledger.root, clock=lambda:self.now)
            self.assertEqual(collect_registered_sources(registry, reopened)["inserted"], 0)
            self.assertEqual(reopened.receipt_snapshot()["sha256"], stable)

        historical, historical_conflicts = current_receipts(
            self.ledger.receipt_snapshot(watermark=old)["facts"])
        self.assertFalse(historical_conflicts)
        self.assertEqual(next(r for r in historical if r.kind=="execution" and r.entity_ref==attempt).payload["executor"],
                         "tcloud-pulse")
        self.assertEqual(next(r for r in historical if r.kind=="usage" and r.payload.get("execution_ref")==attempt).payload["executor"],
                         "tcloud-pulse")

    def test_host_principal_cold_import_and_lifecycle_progression_do_not_fork(self):
        host = self.root / "cold-host"; host.mkdir()
        path = host / "events.jsonl"
        attempt = reference("attempt", "cold-principal")
        common = dict(schema="pulse_collaboration_host_event/v1", kind="execution",
            started_at=AT, attempt_ref=attempt, runtime="pi", expected_usage=True,
            wi=WI, executor="tcloud-pulse", responsible=None, delegator=None)
        rows = [dict(common, event_ref=reference("event", "cold-accepted"),
                     at=AT, state="accepted")]
        path.write_text("".join(json.dumps(r)+"\n" for r in rows))
        history = [{"alias":"tcloud-pulse", "principal":"pulse",
            "effective_from":"2026-09-13T00:59:00Z", "config_sha256":"c"*64}]
        registry = {"schema":"computecost_source_registry/v1",
            "ledger_root":str(self.ledger.root), "sources":[{
                "id":"host", "kind":"relay_host", "path":str(host),
                "node":"tcloud", "actor_attribution":True, "principal_alias_history":history}]}
        collect_registered_sources(registry, self.ledger)
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertFalse(conflicts)
        first = next(r for r in active if r.kind=="execution" and r.entity_ref==attempt)
        self.assertEqual(first.payload["executor"], "pulse")
        self.assertEqual(first.payload["state"], "accepted")
        old = self.ledger.receipt_snapshot()["watermark"]
        # A writer upgrade changes subsequent events, not the original line.
        rows.append(dict(common, event_ref=reference("event", "cold-completed"),
                         at=LATER, state="completed", executor="pulse"))
        path.write_text("".join(json.dumps(r)+"\n" for r in rows))
        collect_registered_sources(registry, self.ledger)
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertFalse(conflicts)
        final = next(r for r in active if r.kind=="execution" and r.entity_ref==attempt)
        self.assertEqual(final.payload["executor"], "pulse")
        self.assertEqual(final.payload["state"], "completed")
        for _ in range(3):
            self.assertEqual(collect_registered_sources(registry, self.ledger)["inserted"], 0)
        historical, conflicts = current_receipts(self.ledger.receipt_snapshot(watermark=old)["facts"])
        self.assertFalse(conflicts)
        self.assertEqual(next(r for r in historical if r.entity_ref==attempt).payload["state"], "accepted")

    def test_A17_model_query_does_not_claim_model_roi(self):
        r=self.query(complete(),work_items=(WI,),model="openai/test",**PERIOD)["summary"]
        self.assertIsNone(r["roi"]["ratio"]);self.assertIn("unsupported_dimension",r["roi"]["reasons"])

    def test_A18_change_catalog_not_usage_and_rebuild(self):
        self.ledger.ingest_receipts(complete());q=CollaborationQuery(work_items=(WI,),**PERIOD)
        r=query_collaboration(self.ledger,q,catalog=CATALOG,catalog_observed_at=AT)
        cat=deepcopy(CATALOG);cat["data"][0]["pricing"]["prompt"]="2"
        r2=query_collaboration(self.ledger,q,catalog=cat,catalog_observed_at=AT)
        self.assertEqual(r["summary"]["usage"],r2["summary"]["usage"])
        self.assertEqual(r2["summary"]["cost"]["machine_cash_usd"],"200")
        self.assertEqual(r,query_collaboration(UsageLedger(self.ledger.root),q,catalog=CATALOG,catalog_observed_at=AT))

    def test_immutable_payload_and_forged_dataclass_rejected(self):
        row=usage();p=row.payload;p["tokens"]["output"]=777
        self.assertEqual(row.payload["tokens"]["output"],0)
        with self.assertRaises(ReceiptError):self.ledger.ingest_receipts([replace(row,_payload_json=b'{}')])

    def test_read_watermark_must_be_integer_and_unknown_revision_rejected(self):
        with self.assertRaises(LedgerError):self.ledger.receipt_snapshot(watermark=1.1)
        row=usage(); forged=Receipt.make(row.kind,None,row.payload,occurred_at=AT,observed_at=AT,
            producer=row.producer,entity_ref=row.entity_ref,supersedes=reference("fact","absent"))
        with self.assertRaises(LedgerError):self.ledger.ingest_receipts([forged])

    def test_no_execution_inventory_cannot_claim_full_wi(self):
        rows=[r for r in complete() if r.kind!="coverage" or r.payload["domain"]!="execution"]
        r=self.query(rows,work_items=(WI,),**PERIOD)["summary"]
        self.assertEqual(r["cost"]["partial_machine_cash_usd"],"100")
        self.assertIsNone(r["cost"]["machine_cash_usd"])

    def test_unreachable_registered_source_visible_and_idempotent(self):
        registry={"schema":"computecost_source_registry/v1","sources":[{"id":"missing","kind":"cc","path":str(self.root/"missing")} ]}
        a=collect_registered_sources(registry,self.ledger);b=collect_registered_sources(registry,self.ledger)
        self.assertEqual(a["sources"][0]["status"],"unreachable");self.assertEqual(b["inserted"],0)

    def test_existing_harness_validators_reused(self):
        from test_delivery_value import review_snapshot
        from test_harness_measurement import measurement_snapshot
        r=review_snapshot();bundle={"schema":"computecost_harness_import/v1","work_item_id":r["work_item_id"],
            "review":r,"measurement":measurement_snapshot()}
        rows=import_harness_bundle(bundle,AT);self.assertEqual({r.kind for r in rows},{"delivery","review"})
        bundle["review"]["candidate_fingerprint"]="sha256:"+"0"*64
        with self.assertRaises(ValueError):import_harness_bundle(bundle,AT)


if __name__ == "__main__":unittest.main()
