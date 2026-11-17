"""PR513 collector-path regressions; all source bytes are synthetic."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.adapters import collect_usage_events
from pulse_compute_cost.receipts import current_receipts, reference
from pulse_compute_cost.receipt_sources import (REGISTRY_SCHEMA, _revision, collect_registered_sources,
                                                 read_cc, read_codex, read_host, read_pi, usage_receipt)

AT = "2026-09-13T01:00:00Z"


class PR513SourceRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = UsageLedger(self.root / "ledger", clock=lambda: datetime(2026, 9, 13, tzinfo=timezone.utc))

    def tearDown(self):
        self.temp.cleanup()

    def jsonl(self, name, rows):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        return path

    def test_native_scope_is_written_and_host_only_propagates_it(self):
        pi = self.jsonl("pi.jsonl", [{"type":"session", "id":"p"}, {"type":"message", "id":"m", "timestamp":AT,
            "message":{"role":"assistant", "usage":{"input":1}}}])
        cc = self.jsonl("cc.jsonl", [{"type":"assistant", "sessionId":"c", "timestamp":AT,
            "message":{"id":"m", "usage":{"input_tokens":1}}}])
        self.assertEqual(read_pi(pi, AT)[0].payload["native_scope_ref"], reference("native_session", ["pi", "p"]))
        self.assertEqual(read_cc(cc, AT)[0].payload["native_scope_ref"], reference("native_session", ["cc", "c"]))
        scope = reference("native_session", ["pi", "p"])
        host = self.jsonl("host.jsonl", [{"schema":"pulse_collaboration_host_event/v1", "event_ref":reference("event", "h"),
            "kind":"usage", "at":AT, "runtime":"pi", "usage":{"input":1}, "mapping":"pi_native.v1",
            "granularity":"request", "native_scope_ref":scope}])
        self.assertEqual(read_host(host, AT)[0].payload["native_scope_ref"], scope)

    def test_codex_live_growth_revises_but_missing_native_fields_stay_unknown(self):
        source = self.root / "codex" / "synthetic-codex.jsonl"
        source.parent.mkdir()
        rows = [
            {"type":"session_meta", "timestamp":AT, "payload":{"id":"synthetic-codex"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_started", "turn_id":"turn1"}},
            {"type":"turn_context", "timestamp":AT, "payload":{"turn_id":"turn1", "model":"openai/test"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":100}}}},
        ]
        source.write_text("".join(json.dumps(row) + "\n" for row in rows)); source.chmod(0o600)
        registry = {"schema":REGISTRY_SCHEMA, "sources":[{"id":"codex", "kind":"codex", "path":str(source.parent)}]}
        collect_registered_sources(registry, self.ledger)
        initial = read_codex(source.parent, AT)[0]
        self.assertIsNone(initial.payload["tokens"]["output"])
        self.assertNotIn("output_tokens", initial.payload["native_tokens"])
        rows += [
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":100, "output_tokens":10}}}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_complete", "turn_id":"turn1"}},
        ]
        source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        collect_registered_sources(registry, self.ledger)
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()["facts"])
        usage = [row for row in active if row.kind == "usage"]
        self.assertEqual(len(usage), 1)
        # This is the first turn after session origin. Its present cumulative
        # output has a zero origin even though the earlier open snapshot omitted
        # output; missing cache classes remain unknown and cannot be priced.
        self.assertEqual(usage[0].payload["tokens"]["output"], 10)
        self.assertEqual(usage[0].payload["native_tokens"]["output_tokens"], 10)
        self.assertEqual(usage[0].payload["reported_total_tokens"], 110)
        self.assertEqual(conflicts, {})

    def test_nonfinal_to_same_final_counter_is_a_revision(self):
        rows = []
        _revision(rows, usage_receipt("cc", "m", {"input_tokens": 100}, at=AT, observed=AT,
                                      model=None, final=False, mapping="cc_message.v1", entity_ref=reference("usage", "m")))
        _revision(rows, usage_receipt("cc", "m", {"input_tokens": 100}, at=AT, observed=AT,
                                      model=None, final=True, mapping="cc_message.v1", entity_ref=reference("usage", "m")))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1].supersedes, rows[0].fact_ref)

    def test_cc_old_nonfinal_copy_cannot_replace_final(self):
        rows = []
        entity = reference("usage", "m")
        _revision(rows, usage_receipt("cc", "m", {"input_tokens": 100}, at=AT, observed=AT,
                                      model=None, final=True, mapping="cc_message.v1", entity_ref=entity))
        _revision(rows, usage_receipt("cc", "m", {"input_tokens": 100}, at=AT, observed=AT,
                                      model=None, final=False, mapping="cc_message.v1", entity_ref=entity))
        self.assertEqual(len(rows), 1)

    def test_final_cc_counter_change_remains_a_conflict(self):
        rows = []
        entity = reference("usage", "m")
        for value in (100, 101):
            _revision(rows, usage_receipt("cc", "m", {"input_tokens": value}, at=AT, observed=AT,
                                          model=None, final=True, mapping="cc_message.v1", entity_ref=entity))
        _, conflicts = current_receipts(rows)
        self.assertIn(entity, conflicts)

    def test_same_fact_new_evidence_is_durable_revision(self):
        entity = reference("usage", "evidence")
        row = usage_receipt("cc", "evidence", {"input_tokens": 1}, at=AT, observed=AT,
                            model=None, final=True, mapping="cc_message.v1", entity_ref=entity)
        first = row.__class__.make(row.kind, None, row.payload, occurred_at=row.occurred_at, observed_at=AT,
                                   producer=row.producer, evidence=(reference("source_snapshot", "one"),), entity_ref=entity)
        second = row.__class__.make(row.kind, None, row.payload, occurred_at=row.occurred_at, observed_at=AT,
                                    producer=row.producer, evidence=(reference("source_snapshot", "two"),), entity_ref=entity)
        self.ledger.ingest_receipts([first])
        self.ledger.ingest_receipts([second])
        old = self.ledger.receipt_snapshot(watermark=1)["facts"]
        active, _ = current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertEqual(len(old), 1)
        self.assertEqual(len(active), 1)
        self.assertEqual(len(active[0].evidence), 2)

    def test_cc_session_scope_does_not_depend_on_file_name(self):
        path = self.jsonl("old-copy.jsonl", [{"type":"assistant", "sessionId":"real", "timestamp":AT,
            "message":{"id":"m", "usage":{"input_tokens":1}}}])
        self.assertEqual(read_cc(path, AT)[0].payload["native_scope_ref"], reference("native_session", ["cc", "real"]))

    def test_codex_first_turn_uses_session_input_baseline_and_missing_output_never_drops_turn(self):
        source = self.root / "codex-baseline" / "s.jsonl"; source.parent.mkdir()
        rows = [
            {"type":"session_meta", "timestamp":AT, "payload":{"id":"s"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_started", "turn_id":"a"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":100}}}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_complete", "turn_id":"a"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_started", "turn_id":"b"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":110}}}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_complete", "turn_id":"b"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_started", "turn_id":"c"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":150}}}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_complete", "turn_id":"c"}},
        ]
        source.write_text("".join(json.dumps(row) + "\n" for row in rows)); source.chmod(0o600)
        events = collect_usage_events(machine="fixture", observed_at=AT, codex_sessions_dir=source.parent).events
        self.assertEqual(sum(event.tokens.input for event in events), 150)  # 100 + 10 + 40
        facts = read_codex(source.parent, AT)
        third = next(fact for fact in facts if fact.payload["native_tokens"].get("input_tokens") == 150)
        self.assertEqual(third.payload["native_tokens"]["input_tokens"], 150)
        self.assertNotIn("output_tokens", third.payload["native_tokens"])

    def test_nonfinal_counter_or_model_contradiction_is_not_superseded(self):
        rows, entity = [], reference("usage", "contradiction")
        _revision(rows, usage_receipt("cc", "m", {"input_tokens":100}, at=AT, observed=AT,
                                      model="model-a", final=True, mapping="cc_message.v1", entity_ref=entity))
        _revision(rows, usage_receipt("cc", "m", {"input_tokens":90}, at=AT, observed=AT,
                                      model="model-a", final=False, mapping="cc_message.v1", entity_ref=entity))
        _revision(rows, usage_receipt("cc", "m", {"input_tokens":100}, at=AT, observed=AT,
                                      model="model-b", final=False, mapping="cc_message.v1", entity_ref=entity))
        self.assertTrue(all(row.supersedes is None for row in rows[1:]))

    def test_codex_native_total_last_proves_between_turn_counter_restart(self):
        from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
        for proven in (True,False):
            with self.subTest(proven=proven):
                rows=[{'type':'session_meta','timestamp':AT,'payload':{'id':'reset-s'}}]
                def count(i,o):return dict(input_tokens=i,output_tokens=o,cached_input_tokens=0,cache_write_input_tokens=0)
                for turn,updates in [('a',[(100,10,100,10)]),('b',[(20,2,20,2),(30,3,10,1)]),('c',[(50,5,20,2)])]:
                    rows.append({'type':'event_msg','timestamp':AT,'payload':{'type':'task_started','turn_id':turn}})
                    for i,o,li,lo in updates:
                        info={'total_token_usage':count(i,o)}
                        if proven:info['last_token_usage']=count(li,lo)
                        rows.append({'type':'event_msg','timestamp':AT,'payload':{'type':'token_count','info':info}})
                    rows.append({'type':'event_msg','timestamp':AT,'payload':{'type':'task_complete','turn_id':turn}})
                path=self.jsonl(f'reset-{proven}/s.jsonl',rows)
                ledger=UsageLedger(self.root/f'ledger-reset-{proven}')
                reg={'schema':REGISTRY_SCHEMA,'sources':[{'id':'reset','kind':'codex','path':str(path.parent)}]}
                collection=collect_registered_sources(reg,ledger)
                out=query_collaboration(ledger,CollaborationQuery())
                self.assertEqual(out['summary']['usage']['event_count'],3)
                self.assertEqual(out['summary']['usage']['known_total_tokens'],165 if proven else 132)
                self.assertEqual(collection['sources'][0]['reason'],
                    'execution_inventory_not_observed' if proven else 'codex_counter_scope_unresolved')
                again=collect_registered_sources(reg,ledger)
                self.assertEqual(again['inserted'],0)

    def test_codex_explicit_null_field_remains_an_observed_unknown(self):
        rows=[{'type':'session_meta','timestamp':AT,'payload':{'id':'null-s'}},
              {'type':'event_msg','timestamp':AT,'payload':{'type':'task_started','turn_id':'t'}},
              {'type':'event_msg','timestamp':AT,'payload':{'type':'token_count','info':{'total_token_usage':
                  {'input_tokens':100,'output_tokens':None,'cached_input_tokens':None}}}}]
        path=self.jsonl('null/s.jsonl',rows)
        result=read_codex(path.parent,AT)
        self.assertEqual(len(result),1)
        self.assertIsNone(result[0].payload['tokens']['output'])
        self.assertIsNone(result[0].payload['native_tokens'].get('output_tokens'))
        self.assertEqual(result[0].payload['native_tokens']['input_tokens'],100)

    def test_open_codex_input_growth_survives_unknown_cache_partitions(self):
        from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
        rows=[{'type':'session_meta','timestamp':AT,'payload':{'id':'s'}},
              {'type':'event_msg','timestamp':AT,'payload':{'type':'task_started','turn_id':'t'}}]
        path=self.jsonl('open/s.jsonl', rows)
        registry={'schema':REGISTRY_SCHEMA,'sources':[{'id':'open','kind':'codex','path':str(path.parent)}]}
        for n in (100,110):
            rows.append({'type':'event_msg','timestamp':AT,'payload':{'type':'token_count',
                         'info':{'total_token_usage':{'input_tokens':n,'output_tokens':0}}}})
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            collect_registered_sources(registry,self.ledger)
            out=query_collaboration(self.ledger,CollaborationQuery())
            self.assertEqual(out['summary']['usage']['known_total_tokens'],n)
            self.assertEqual(out['revision_conflicts'],[])
        collect_registered_sources(registry,self.ledger)
        self.assertEqual(query_collaboration(self.ledger,CollaborationQuery())['summary']['usage']['known_total_tokens'],110)

    def test_receipt_source_recovery_withdraws_gap_without_inventing_coverage(self):
        from test_collaboration_receipts import complete, WI, CATALOG, PERIOD
        from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
        path=self.root/'recover.jsonl'
        registry={'schema':REGISTRY_SCHEMA,'sources':[{'id':'recover','kind':'receipts','path':str(path),'work_items':[WI]}]}
        collect_registered_sources(registry,self.ledger)
        path.write_text(''.join(json.dumps(r.to_dict())+'\n' for r in complete()));path.chmod(0o600)
        collect_registered_sources(registry,self.ledger)
        q=CollaborationQuery(work_items=(WI,),**PERIOD)
        out=query_collaboration(self.ledger,q,catalog=CATALOG,catalog_observed_at=AT)
        self.assertEqual(out['summary']['roi']['ratio'],'0.1')
        path.unlink()
        collect_registered_sources(registry,self.ledger)
        out=query_collaboration(self.ledger,q,catalog=CATALOG,catalog_observed_at=AT)
        self.assertIsNone(out['summary']['roi']['ratio'])
        self.assertIn('source_coverage_unknown',out['summary']['roi']['reasons'])

    def test_pi_compaction_is_independent_but_child_summary_is_not_added(self):
        from pulse_compute_cost.collaboration_query import reconcile_usage
        def raw(n):
            return dict(input=n, output=0, cacheRead=0, cacheWrite=0, totalTokens=n)
        parent = self.jsonl('parent.jsonl', [
            {'type':'session','id':'parent'},
            {'type':'message','id':'a','timestamp':AT,'message':{'role':'assistant','usage':raw(100)}},
            {'type':'compaction','id':'c','timestamp':AT,'usage':raw(50)},
            {'type':'message','id':'s','timestamp':AT,'message':{'role':'toolResult','usage':raw(70)}}])
        child = self.jsonl('child.jsonl', [{'type':'session','id':'child'},
            {'type':'message','id':'a','timestamp':AT,'message':{'role':'assistant','usage':raw(70)}}])
        facts = read_pi(parent, AT) + read_pi(child, AT)
        kept, diagnostic = reconcile_usage(facts)
        self.assertEqual(sum(r.payload['tokens']['uncached_input'] for r in kept), 220)
        self.assertEqual(diagnostic['status'], 'conflict')
        self.assertEqual(len(diagnostic['conflicting_refs']), 1)
        self.assertTrue(all(r.payload['granularity'] == 'request' for r in kept))

    def test_codex_native_counter_is_not_delta(self):
        source = self.root / "codex-counter" / "s.jsonl"
        source.parent.mkdir()
        source.write_text("\n".join(json.dumps(x) for x in [
            {"type":"session_meta", "timestamp":AT, "payload":{"id":"s"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_started", "turn_id":"a"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":100}}}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_complete", "turn_id":"a"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_started", "turn_id":"b"}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":150}}}},
            {"type":"event_msg", "timestamp":AT, "payload":{"type":"task_complete", "turn_id":"b"}},
        ]) + "\n")
        rows = read_codex(source.parent, AT)
        second = next(row for row in rows if row.payload["native_tokens"].get("input_tokens") == 150)
        self.assertEqual(second.payload["native_tokens"]["input_tokens"], 150)
        self.assertEqual(second.payload["tokens"]["uncached_input"], None)  # cache watermarks absent



if __name__ == "__main__":
    unittest.main()
