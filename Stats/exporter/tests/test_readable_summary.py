"""Bounded receipt transport and truthful outstanding-age regressions."""
from datetime import datetime, timezone
from hashlib import sha256
import gzip
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
ROOT=Path(__file__).parents[3]
sys.path[:0]=[str(ROOT/'Stats/exporter'),str(ROOT/'ComputeCost'),str(ROOT/'ComputeCost/tests')]
from pulse_stats import receipt_transport as t
from pulse_stats.usage_sources import collect_in_flight
from pulse_compute_cost.receipts import canonical
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from test_collaboration_receipts import complete,CATALOG,AT,PERIOD,WI

class FramedReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.root.chmod(0o700);self.target=self.root/'peer.jsonl'
        self.rows=complete();self.raw=b''.join(canonical(r.to_dict())+b'\n' for r in self.rows)
    def wire(self, rows=None, raw=b''):
        return t.encode(self.rows if rows is None else rows,len(raw),sha256(raw).hexdigest())
    def seed(self, raw=None):
        self.target.write_bytes(self.raw if raw is None else raw);self.target.chmod(0o600)
    def test_full_import_append_and_unchanged_round_preserve_exact_bytes(self):
        first=self.rows[:2];data=b''.join(canonical(r.to_dict())+b'\n' for r in first)
        result=t.install(self.target,self.wire(first),None)
        self.assertEqual(self.target.read_bytes(),data);self.assertGreater(result['frames'],0)
        base,size,digest=t.cursor(self.target)
        t.install(self.target,t.encode(self.rows,size,digest),base)
        self.assertEqual(self.target.read_bytes(),self.raw)
        base,size,digest=t.cursor(self.target)
        result=t.install(self.target,t.encode(self.rows,size,digest),base)
        self.assertEqual(result['frames'],0);self.assertEqual(self.target.read_bytes(),self.raw)
        self.assertEqual(self.target.stat().st_mode&0o777,0o600)
    def test_total_history_larger_than_wire_limit_bootstraps_in_bounded_frames(self):
        rows=[self.rows[0]]*160
        raw=b''.join(canonical(r.to_dict())+b'\n' for r in rows)
        with patch.multiple(t,FRAME_BYTES=8192,WIRE_BYTES=32768):
            self.assertGreater(len(raw),t.WIRE_BYTES)
            wire=self.wire(rows);self.assertLess(len(wire),t.WIRE_BYTES)
            result=t.install(self.target,wire,None)
            self.assertGreater(result['frames'],1);self.assertEqual(self.target.read_bytes(),raw)
            base,size,digest=t.cursor(self.target)
            self.assertGreater(size,t.WIRE_BYTES)
            self.assertEqual(t.install(self.target,t.encode(rows,size,digest),base)['frames'],0)
    def test_previous_last_good_larger_than_wire_limit_remains_a_valid_cursor(self):
        rows=[self.rows[0]]*160;raw=b''.join(canonical(r.to_dict())+b'\n' for r in rows)
        last=self.target.with_suffix('.last-good');last.write_bytes(raw);last.chmod(0o600)
        with patch.multiple(t,FRAME_BYTES=8192,WIRE_BYTES=32768):
            base,size,digest=t.cursor(self.target)
            t.install(self.target,t.encode(rows+self.rows,size,digest),base)
        self.assertEqual(self.target.read_bytes(),raw+self.raw);self.assertEqual(last.read_bytes(),raw)
    def test_wrong_prefix_gets_complete_replacement_not_wrong_append(self):
        self.seed();wire=t.encode(self.rows,10,'f'*64)
        t.install(self.target,wire,self.target)
        self.assertEqual(self.target.read_bytes(),self.raw)
    def test_malformed_or_truncated_stream_never_replaces_good_input(self):
        self.seed();wire=self.wire()
        bad=bytearray(wire);bad[-9]^=1
        for value in (wire[:-1],wire+b'junk',bytes(bad),wire.replace(t.SCHEMA.encode(),b'future-schema')):
            with self.subTest(case=len(value)),self.assertRaises(ValueError):t.install(self.target,value,self.target)
            self.assertEqual(self.target.read_bytes(),self.raw)
            self.assertFalse(list(self.root.glob('.receipt-stage-*')))
    def test_local_suffix_growth_after_cursor_is_ignored_but_prefix_mutation_is_rejected(self):
        self.seed();wire=self.wire(raw=self.raw)
        # A concurrent append after the proved cursor does not invalidate its
        # first N bytes. The remote full identity remains authoritative.
        self.target.write_bytes(self.raw+b'{}\n')
        t.install(self.target,wire,self.target)
        self.assertEqual(self.target.read_bytes(),self.raw)
        # A changed byte inside the proved N-byte prefix must still fail closed.
        changed=b'X'+self.raw[1:]
        self.target.write_bytes(changed)
        with self.assertRaises(ValueError):t.install(self.target,wire,self.target)
        self.assertEqual(self.target.read_bytes(),changed)
    def test_each_compressed_and_expanded_frame_remains_bounded(self):
        with patch.object(t,'FRAME_BYTES',64):
            with self.assertRaises(ValueError):t._inflate(gzip.compress(b'x'*65))
            with self.assertRaises(ValueError):t.encode(self.rows,0,sha256(b'').hexdigest())
        with self.assertRaises(ValueError):t._inflate(gzip.compress(b'a')+gzip.compress(b'b'))
        with patch.object(t,'WIRE_BYTES',10):
            with self.assertRaises(ValueError):t.install(self.target,b'x'*11,None)
    def test_disk_reserve_and_unsafe_prefix_reject_without_mutation(self):
        self.seed()
        with patch.object(t.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaises(ValueError):t.install(self.target,self.wire(),self.target)
        self.assertEqual(self.target.read_bytes(),self.raw)
        self.target.chmod(0o644)
        with self.assertRaises(ValueError):t.cursor(self.target)
    def test_same_original_receipts_keep_query_values_and_identity(self):
        ledger=UsageLedger(self.root/'ledger');ledger.ingest_receipts(self.rows)
        q=CollaborationQuery(work_items=(WI,),**PERIOD)
        before=query_collaboration(ledger,q,catalog=CATALOG,catalog_observed_at=AT)['summary']
        t.install(self.target,self.wire(),None)
        registry={'schema':'computecost_source_registry/v1','ledger_root':str(ledger.root),
            'sources':[{'id':'stats-transport-fixture','kind':'receipts','path':str(self.target)}]}
        collect_registered_sources(registry,ledger)
        after=query_collaboration(ledger,q,catalog=CATALOG,catalog_observed_at=AT)['summary']
        self.assertEqual(after['usage']['event_count'],before['usage']['event_count'])
        self.assertEqual(after['cost']['machine_cash_usd'],before['cost']['machine_cash_usd'])

class OutstandingAgeTests(unittest.TestCase):
    def test_created_age_does_not_relabel_record_inactivity(self):
        now=datetime(2026,9,18,13,44,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);items=root/'work-items';items.mkdir();handoffs=root/'handoffs';handoffs.mkdir()
            value=dict(work_item_id='wi_19990101_001',owner='pulse',status='in_progress',
                created_at='2026-09-06T09:32:00Z',updated_at='2026-09-06T14:28:00Z')
            path=items/'wi.yaml';path.write_text(json.dumps(value))
            row=collect_in_flight(items,handoffs,now)[0]
            self.assertEqual(row['open_days'],12);self.assertEqual(row['days'],11)
            for bad in (None,'invalid','2027-01-01T00:00:00Z'):
                value['created_at']=bad;path.write_text(json.dumps(value))
                self.assertIsNone(collect_in_flight(items,handoffs,now)[0]['open_days'])

if __name__=='__main__':unittest.main()


class WorkItemPresentationTests(unittest.TestCase):
    def test_creation_order_is_oldest_first_not_latest_edit_and_keeps_unknown(self):
        now=datetime(2026,9,19,12,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);items=root/'items';items.mkdir();handoffs=root/'handoffs';handoffs.mkdir()
            def record(wid, created, updated):
                data=dict(work_item_id=wid,owner='pulse',status='in_progress',created_at=created,updated_at=updated)
                (items/(wid+'.yaml')).write_text(json.dumps(data))
            record('wi_new','2026-09-17T00:00:00Z','2026-01-01T00:00:00Z')
            record('wi_old','2026-09-07T00:00:00Z','2026-09-19T11:00:00Z')
            record('wi_a_later','2026-09-07T01:00:00Z','2026-09-19T11:00:00Z')
            record('wi_missing',None,'2026-09-19T11:00:00Z')
            record('wi_future','2027-01-01T00:00:00Z','2026-09-19T11:00:00Z')
            before={f:f.read_bytes() for f in items.iterdir()}
            rows=collect_in_flight(items,handoffs,now)
            self.assertEqual([r['work_item_id'] for r in rows],['wi_old','wi_a_later','wi_new','wi_future','wi_missing'])
            self.assertEqual(rows[0]['open_days'],12);self.assertEqual(rows[0]['days'],0)
            self.assertEqual(rows[0]['created_at'],'2026-09-07T00:00:00+00:00')
            self.assertTrue(all(r['created_at'] is None for r in rows[-2:]))
            self.assertEqual(before,{f:f.read_bytes() for f in items.iterdir()})

    def test_explicit_wi_relation_survives_collector_reducer_and_snapshot(self):
        from Stats.exporter.tests.test_live import LiveFixture,FakeRunner,ack_result,systemd_result
        now=datetime(2026,9,19,12,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            f=LiveFixture(Path(directory))
            wid='wi_20260906_779'
            (f.work_items/(wid+'.yaml')).write_text(json.dumps(dict(work_item_id=wid,owner='pulse',
                status='in_progress',main_only=True,title='original title',created_at='2026-09-06T00:00:00Z',updated_at='2026-09-19T00:00:00Z')))
            (f.handoffs/'handoff.yaml').write_text(json.dumps(dict(handoff_id='ho_20260919_001',
                work_item_id=wid,main_only=True,ack_state='pending',from_owner='harness',to_owner='pulse',
                created_at='2026-09-19T00:00:00Z',expected_output='original action')))
            snap=json.loads(f.service(FakeRunner(ack=ack_result(),systemd=systemd_result()),now=now).render_json())
            alert=next(a for a in snap['alerts'] if a.get('work_item_id')==wid)
            row=next(r for m in snap['metrics'] if m['id']=='harness.wi.in_flight' for r in m['value'] if r['work_item_id']==wid)
            self.assertEqual(alert['tier'],'act');self.assertEqual(alert['title'],'original title')
            self.assertIn('original action',alert['reason'])
            self.assertEqual(row['open_days'],13);self.assertEqual(row['days'],0)
            self.assertTrue(row['needs_owner'])

    def test_alert_text_cannot_invent_a_work_item_relation(self):
        from pulse_stats.reducer import IncidentEvent,reduce_incident_events
        from pulse_stats.producer import _alert_dict
        event=IncidentEvent(event_id='test',kind='event_only',capability_id='notify.action_package',
            condition_key='event_only',scope='test',normalized_scope_key='test',severity='warning',
            title='wi_20260906_779',occurred_at=datetime.now(timezone.utc),reason='pulse wi_20260906_779',tier='act')
        _,alerts=reduce_incident_events([event])
        self.assertNotIn('work_item_id',_alert_dict(alerts[0]))
