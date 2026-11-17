"""Native Pi + existing SCL measurement, without a parent session attachment."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import unittest
import test_scl_session_attribution as existing
WI, SURFACE, START, END = existing.WI, existing.SURFACE, existing.START, existing.END
from pulse_compute_cost.pi_usage import PiUsageBinding, build_pi_session_snapshot
from pulse_compute_cost.receipts import canonical
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.scl_session_attribution import pi_host_ref


class ScopedMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.f = existing.SessionAttributionTests(methodName="runTest")
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        for p in self.f.logs.iterdir(): p.unlink()
        self.session = "native-scoped-test-session"
        self.host = pi_host_ref(self.session)
        self.file = self.f.logs/"scoped.jsonl"
        self.file.write_text(json.dumps({"type":"session","id":self.session,"version":3,
                                       "timestamp":"2026-01-01T00:59:00Z"})+"\n")
        self.file.chmod(0o600)
        self.append("warmup", "2026-01-01T00:59:50Z")
        self.binding = PiUsageBinding(WI,"work_item",1,"chatgpt_subscription","a"*64,
                                      "reeve",SURFACE,self.host)
        self.base = build_pi_session_snapshot(self.file,self.binding,generated_at=START,
                                             measurement_role="baseline")
        self.append("work-1", "2026-01-01T01:00:20Z")
        self.append("work-2", "2026-01-01T01:00:40Z")
        self.final = build_pi_session_snapshot(self.file,self.binding,generated_at=END,
                    measurement_role="final",lifecycle_state="sealed",baseline_snapshot=self.base)
        self.append("after", "2026-01-01T01:02:00Z")
        self.f.source.update(id="native-pi",kind="pi",workspace_actors=[])
        self.f.db.executescript("""
            DROP TABLE measurement_epochs;
            CREATE TABLE measurement_epochs (work_item_id TEXT, execution_epoch INTEGER,
                host_session_ref TEXT, surface_id TEXT, agent_id TEXT, attribution_json TEXT,
                attribution_digest TEXT, measurement_epoch_ref TEXT, runtime_class TEXT,
                status TEXT, created_at TEXT, logical_source_ref TEXT);
            CREATE TABLE measurement_native_facts (measurement_epoch_ref TEXT,
                lifecycle_state TEXT, native_json TEXT, native_digest TEXT);
        """)
        self.measurement = "measurement_epoch_v1_" + "a"*64
        self.identity = {"schema":"work_item_cost_attribution/v1","schema_version":1,
                "work_item_id":WI,"execution_epoch":1,"execution_phase":"work_item",
                "execution_runtime":"pi","agent_id":"reeve","measurement_epoch_ref":self.measurement,
                "boundary_policy":"prospective_exact_lineage_no_backfill","started_at":START,"ended_at":None}
        self.register()

    def append(self, name, at):
        with self.file.open("a") as out:
            out.write(json.dumps({"type":"message","id":name,"timestamp":at,"message":{
                "role":"assistant","model":"openai/test","provider":"openai",
                "usage":{"input":100,"cacheRead":20,"cacheWrite":0,"output":10,"totalTokens":130}}})+"\n")

    def register(self, status="sealed"):
        self.f.db.execute("DELETE FROM measurement_epochs")
        raw=canonical(self.identity).decode()
        self.f.db.execute("INSERT INTO measurement_epochs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (WI,1,self.host,SURFACE,"reeve",raw,hashlib.sha256(raw.encode()).hexdigest(),
             self.measurement,"resident_harness",status,START,self.host))
        self.f.db.execute("DELETE FROM measurement_native_facts")
        snapshot=self.final if status=="sealed" else build_pi_session_snapshot(
            self.file,self.binding,generated_at="2026-01-01T01:03:00Z",measurement_role="progress",baseline_snapshot=self.base)
        raw=canonical(snapshot).decode()
        self.f.db.execute("INSERT INTO measurement_native_facts VALUES (?,?,?,?)",
            (self.measurement,status,raw,hashlib.sha256(raw.encode()).hexdigest()))
        self.f.db.commit()
        folder=self.file.parent/'compute-cost';folder.mkdir(exist_ok=True)
        for name,value in [('baseline',self.base),(status,snapshot)]:
            path=folder/f"{WI}-{'a'*64}-{name}.json"
            path.write_text(json.dumps(value));path.chmod(0o600)

    def collect(self): return self.f.collect()
    def assigned(self, result):
        return [g for g in result['groups'] if g['key']=={'wi':WI,'agent':'reeve'}]

    def test_grantee_without_parent_attachment_is_read_but_warmup_and_after_are_not(self):
        before=self.f.database.read_bytes();r=self.collect()
        self.assertEqual(self.assigned(r)[0]['usage']['event_count'],2)
        self.assertEqual(r['summary']['usage']['agent_unassigned_event_count'],2)
        self.assertEqual(r['summary']['usage']['wi_unassigned_event_count'],2)
        self.assertEqual(r['summary']['usage']['known_total_tokens'],520)
        self.assertEqual(self.f.database.read_bytes(),before)

    def test_open_progress_is_enough_no_token_seal_required(self):
        self.register("open")
        self.assertEqual(self.assigned(self.collect())[0]['usage']['event_count'],3)

    def test_parent_epoch_does_not_close_independent_scoped_epoch(self):
        state=json.loads(self.f.db.execute("SELECT state_json FROM work_items").fetchone()[0])
        state['epoch']=13;state['active_executor']='pulse'
        self.f.db.execute("UPDATE work_items SET state_json=?",(json.dumps(state),));self.f.db.commit()
        self.assertEqual(self.assigned(self.collect())[0]['usage']['event_count'],2)

    def test_declared_epoch_without_native_evidence_remains_unknown(self):
        self.f.db.execute("DELETE FROM measurement_native_facts");self.f.db.commit()
        self.assertEqual(self.assigned(self.collect()),[])

    def test_pending_baseline_is_not_usage_identity(self):
        self.f.db.execute("UPDATE measurement_epochs SET status='baseline_pending'");self.f.db.commit()
        self.assertEqual(self.assigned(self.collect()),[])

    def test_missing_or_other_surface_host_does_not_match(self):
        for column,value in [('host_session_ref','session_v1_'+'b'*64),('surface_id','surface_v1_other')]:
            self.register();self.f.db.execute('UPDATE measurement_epochs SET '+column+'=?',(value,));self.f.db.commit()
            self.assertEqual(self.assigned(self.collect()),[])

    def test_corrupt_digest_preserves_quantities_without_assignment(self):
        for table,field in [('measurement_epochs','attribution_digest'),('measurement_native_facts','native_digest')]:
            self.register();self.f.db.execute('UPDATE '+table+' SET '+field+"='corrupt'");self.f.db.commit()
            r=self.collect();self.assertEqual(self.assigned(r),[])
            self.assertEqual(r['summary']['usage']['known_total_tokens'],520)

    def test_valid_native_snapshot_for_wrong_agent_is_not_trusted(self):
        from copy import deepcopy
        wrong=deepcopy(self.final);wrong['agent_id']='pulse'
        wrong['snapshot_sha256']=hashlib.sha256(canonical({k:v for k,v in wrong.items() if k!='snapshot_sha256'})).hexdigest()
        raw=canonical(wrong).decode()
        self.f.db.execute("UPDATE measurement_native_facts SET native_json=?,native_digest=?",(raw,hashlib.sha256(raw.encode()).hexdigest()));self.f.db.commit()
        self.assertEqual(self.assigned(self.collect()),[])

    def test_terminal_boundary_does_not_inherit_authority_forever(self):
        state=json.loads(self.f.db.execute("SELECT state_json FROM work_items").fetchone()[0]);state['lifecycle_status']='completed'
        self.f.db.execute("UPDATE work_items SET state_json=?,updated_at=?",(json.dumps(state),'2026-01-01T01:00:30Z'));self.f.db.commit()
        self.assertEqual(self.assigned(self.collect())[0]['usage']['event_count'],1)

    def test_late_identity_preserves_usage_price_history_and_replay_is_idempotent(self):
        self.f.db.execute("UPDATE measurement_epochs SET status='baseline_pending'");self.f.db.commit()
        first=self.collect();old=self.f.ledger.receipt_snapshot();usage=[r.to_dict() for r in old['facts'] if r.kind=='usage']
        self.register();fixed=self.collect()
        self.assertEqual(self.assigned(fixed)[0]['usage']['event_count'],2)
        self.assertEqual(first['summary']['usage']['tokens'],fixed['summary']['usage']['tokens'])
        self.assertEqual(first['summary']['cost']['partial_machine_cash_usd'],fixed['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(old['sha256'],self.f.ledger.receipt_snapshot(watermark=old['watermark'])['sha256'])
        self.assertEqual(usage,[r.to_dict() for r in self.f.ledger.receipt_snapshot()['facts'] if r.kind=='usage'])
        self.assertEqual(collect_registered_sources(self.f.registry,self.f.ledger)['inserted'],0)

    def test_other_runtime_class_cannot_relabel_pi(self):
        self.f.db.execute("UPDATE measurement_epochs SET runtime_class='codex'");self.f.db.commit()
        self.assertEqual(self.assigned(self.collect()),[])

    def test_conflicting_parent_attachment_is_not_silently_overruled(self):
        self.f.db.execute("UPDATE source_attachments SET host_session_ref=?",(self.host,));self.f.db.commit()
        result=self.collect()
        self.assertEqual(self.assigned(result),[])
        self.assertTrue(any(g['key']['wi']=='ambiguous' for g in result['groups']))
        self.assertEqual(result['summary']['usage']['known_total_tokens'],520)

    def test_malformed_identity_fails_only_attribution_not_the_known_usage(self):
        raw='[]'
        self.f.db.execute("UPDATE measurement_epochs SET attribution_json=?,attribution_digest=?",(raw,hashlib.sha256(raw.encode()).hexdigest()));self.f.db.commit()
        result=self.collect();self.assertEqual(self.assigned(result),[])
        self.assertEqual(result['summary']['usage']['known_total_tokens'],520)

    def test_same_time_but_outside_verified_native_prefix_is_not_assigned(self):
        self.append("late-same-time", "2026-01-01T01:00:30Z")
        result=self.collect()
        self.assertEqual(self.assigned(result)[0]['usage']['event_count'],2)
        self.assertEqual(result['summary']['usage']['event_count'],5)

    def test_native_prefix_rewrite_does_not_use_time_window_as_fallback(self):
        self.file.write_text(self.file.read_text().replace('work-1','changed-request'))
        self.assertEqual(self.assigned(self.collect()),[])

    def test_normal_manifest_readoption_preserves_scoped_identity_and_actor_context(self):
        from pulse_compute_cost.source_registration import runtime_candidate_registry
        harness=self.f.root/'canonical';harness.mkdir()
        host=self.f.root/'host';host.mkdir()
        manifest=self.f.root/'manifest.json'
        manifest.write_text(json.dumps({'profiles':{'resident':{'sessionRoot':str(self.file.parent),
            'computeCostHost':{'sclHarnessRoot':str(harness)}}}}))
        source=self.f.source.copy()
        registry={**self.f.registry,'ledger_root':str(self.f.ledger.root),'sources':[source,
            {'id':'harness','kind':'harness_root','path':str(harness),'node':'testmac','label':'retained-harness'},
            {'id':'host','kind':'relay_host','path':str(host)}]}
        candidate=runtime_candidate_registry(registry,manifest,harness_root=harness,receipt_root=host)
        current=next(s for s in candidate['sources'] if s['kind']=='pi')
        for key in ('actor_attribution','scl_kernel_root','scl_surface_id','workspace_actors','node'):
            self.assertEqual(current[key],source[key],key)
        self.assertEqual(next(s for s in candidate['sources'] if s['kind']=='harness_root')['label'],'retained-harness')
        self.assertEqual(runtime_candidate_registry(candidate,manifest,harness_root=harness,receipt_root=host),candidate)
        self.f.registry=candidate
        self.assertEqual(self.assigned(self.collect())[0]['usage']['event_count'],2)
        self.assertEqual(collect_registered_sources(candidate,self.f.ledger)['inserted'],0)


    def legacy_measurement(self, *, replace_current=False, corrupt=False):
        """Use the actual v1 builder, not a malformed v2 or a prefilled attribution."""
        session = self.session if replace_current else "legacy-neighbor"
        path = self.file if replace_current else self.f.logs/"legacy.jsonl"
        if not replace_current:
            rows = [json.loads(line) for line in self.file.read_text().splitlines()[:2]]
            rows[0]["id"] = session
            path.write_text("".join(json.dumps(row)+"\n" for row in rows))
            path.chmod(0o600)
        legacy = build_pi_session_snapshot(path, PiUsageBinding(WI,"work_item",1,"unknown"),
                                           generated_at=END, lifecycle_state="sealed")
        self.assertEqual(legacy["schema"], "pi_session_usage_snapshot/v1")
        ref = self.measurement if replace_current else "measurement_epoch_v1_"+"b"*64
        if replace_current:
            self.f.db.execute("DELETE FROM measurement_native_facts")
        else:
            identity = {**self.identity, "measurement_epoch_ref":ref}
            raw = canonical(identity).decode()
            self.f.db.execute("INSERT INTO measurement_epochs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (WI,1,pi_host_ref(session),SURFACE,"reeve",raw,hashlib.sha256(raw.encode()).hexdigest(),
                 ref,"resident_harness","sealed",START,pi_host_ref(session)))
        if corrupt:
            legacy["snapshot_sha256"] = "f"*64
        raw=canonical(legacy).decode()
        self.f.db.execute("INSERT INTO measurement_native_facts VALUES (?,?,?,?)",
            (ref,"sealed",raw,hashlib.sha256(raw.encode()).hexdigest()))
        self.f.db.commit()

    def test_valid_legacy_neighbor_does_not_block_verified_native_binding(self):
        from pulse_compute_cost.source_layout import source_has_read_failure
        self.legacy_measurement()
        before=self.f.database.read_bytes()
        result=self.collect()
        self.assertEqual(self.assigned(result)[0]["usage"]["event_count"],2)
        self.assertEqual(result["summary"]["usage"]["event_count"],5)
        self.assertEqual(result["summary"]["usage"]["known_total_tokens"],650)
        self.assertEqual(result["summary"]["usage"]["agent_unassigned_event_count"],3)
        self.assertFalse(any(source_has_read_failure(s) for s in result["summary"]["usage"]["source_details"]))
        self.assertEqual(self.f.database.read_bytes(),before)
        self.assertEqual(collect_registered_sources(self.f.registry,self.f.ledger)["inserted"],0)

    def test_valid_legacy_snapshot_does_not_supply_request_scope_or_fake_full_coverage(self):
        from pulse_compute_cost.source_layout import source_has_read_failure
        self.legacy_measurement(replace_current=True)
        result=self.collect()
        self.assertEqual(self.assigned(result),[])
        self.assertEqual(result["summary"]["usage"]["known_total_tokens"],520)
        self.assertEqual(result["summary"]["usage"]["source_coverage"],"unknown")
        self.assertFalse(any(source_has_read_failure(s) for s in result["summary"]["usage"]["source_details"]))

    def test_corrupt_legacy_snapshot_is_not_silently_skipped_as_compatible(self):
        from pulse_compute_cost.source_layout import source_has_read_failure
        self.legacy_measurement(corrupt=True)
        result=self.collect()
        self.assertEqual(result["summary"]["usage"]["known_total_tokens"],650)
        self.assertTrue(any(source_has_read_failure(s) for s in result["summary"]["usage"]["source_details"]))

if __name__=='__main__':unittest.main()
