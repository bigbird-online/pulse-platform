from copy import deepcopy
from datetime import datetime,timezone,timedelta
import base64
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'ComputeCost'))
sys.path.insert(0,str(ROOT/'Stats/exporter'))
from pulse_compute_cost.stats_dashboard import PublicationWithheld,build_dashboard,publish_dashboard
from pulse_compute_cost.receipts import canonical, digest
from pulse_compute_cost.stats_dashboard_wire import (
    COMPRESSED_FIELD, COMPRESSION_METADATA, MAX_BYTES, MAX_EXPANDED_BYTES, expand_dashboard,
)
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_stats.cost_dashboard import (load_cost_dashboard,attach_cost_dashboard,usage_metrics,
    load_refresh_status,REFRESH_STATUS_NAME)

NOW=datetime(2026,9,13,8,tzinfo=timezone.utc)
FACTS={'schema_version':'pulse_stats_procurement_facts/v1','subscriptions':[]}

class CostDashboardReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'private'/'cost.json'
        self.doc=build_dashboard(None,subscriptions=FACTS,source_revision='c'*40,now=NOW)
        publish_dashboard(self.path,self.doc)

    def test_compact_activity_only_carrier_is_valid(self):
        ledger=UsageLedger(Path(self.tmp.name)/'ledger',clock=lambda:NOW)
        ledger.ingest_receipts([usage_receipt('pi','compact',{'input':1,'output':1,'cacheRead':0,'cacheWrite':0},
            at=NOW.isoformat(),observed=NOW.isoformat(),model='openai/test',
            common={'wi':'wi_20260825_300','executor':'pulse','responsible':'pulse','delegator':None})])
        document=build_dashboard(ledger,subscriptions=FACTS,source_revision='c'*40,now=NOW,
            identity_dimensions=True,activity_table=True,activity_only=True)
        self.assertEqual([(v['period'],v['dimension']) for v in document['views']],
            [('today','activity'),('week','activity'),('month','activity')])
        self.path.unlink()
        publish_dashboard(self.path,document)
        self.assertEqual(load_cost_dashboard(self.path,now=NOW)['status'],'available')

    def large_activity(self, count):
        ledger = UsageLedger(Path(self.tmp.name)/'ledger', clock=lambda:NOW)
        def receipt(index):
            return usage_receipt('pi', f'compact-size-{index}',
                {'input': 1, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0},
                at=NOW.isoformat(), observed=NOW.isoformat(), model='openai/test',
                common={'wi': f'wi_20260913_{index:03}', 'executor': 'pulse',
                        'responsible': 'pulse', 'delegator': None})

        options = dict(subscriptions=FACTS, source_revision='c'*40, now=NOW,
                       identity_dimensions=True, activity_table=True, activity_only=True)
        ledger.ingest_receipts([receipt(index) for index in range(count)])
        return build_dashboard(ledger, **options)

    def test_large_activity_carrier_keeps_all_rows_and_the_original_digest(self):
        document = self.large_activity(144)
        self.assertEqual(document['status'], 'available')
        self.assertGreater(len(canonical(document)), MAX_BYTES)
        previous = self.path.read_bytes()
        with self.assertRaises(PublicationWithheld) as withheld:
            publish_dashboard(self.path, document)
        self.assertEqual(withheld.exception.reason, 'cost_reader_update_required')
        self.assertEqual(self.path.read_bytes(), previous)
        path = self.path.with_name('large-cost.json')
        publish_dashboard(path, document, allow_row_contexts=True)
        self.assertLessEqual(path.stat().st_size, MAX_BYTES)
        packed = json.loads(path.read_bytes())
        self.assertIn('row_contexts', packed)
        self.assertEqual(load_cost_dashboard(path, now=NOW), document)
        snapshot = json.loads(attach_cost_dashboard('{"metrics":[]}', path=path, now=NOW))
        self.assertEqual(snapshot['cost_dashboard'], document)

        packed['views'][0]['groups'][0]['outcomes'] = -1
        path.write_bytes(canonical(packed))
        self.assertEqual(load_cost_dashboard(path, now=NOW)['reason'], 'invalid_cost_snapshot')

    @patch('pulse_compute_cost.stats_dashboard_wire.MAX_BYTES', MAX_BYTES // 3)
    @patch('pulse_compute_cost.stats_dashboard.MAX_BYTES', MAX_BYTES // 3)
    @patch('pulse_stats.cost_dashboard.MAX_BYTES', MAX_BYTES // 3)
    def test_gzip_fallback_preserves_larger_activity_and_the_received_bytes(self):
        document = self.large_activity(212)
        self.assertEqual(document['status'], 'available')
        previous = self.path.read_bytes()
        with self.assertRaises(PublicationWithheld) as withheld:
            publish_dashboard(self.path, document, allow_row_contexts=True)
        self.assertEqual(withheld.exception.reason, 'cost_reader_update_required')
        self.assertEqual(self.path.read_bytes(), previous)
        output = self.path.with_name('gzip-cost.json')
        publish_dashboard(output, document, allow_compression=True)
        self.assertLessEqual(output.stat().st_size, MAX_BYTES // 3)
        packed = json.loads(output.read_bytes())
        self.assertIn(COMPRESSED_FIELD, packed)
        self.assertEqual(load_cost_dashboard(output, now=NOW), document)
        receiver = output.with_name('received.json')
        publish_dashboard(receiver, packed, allow_compression=True)
        self.assertEqual(receiver.read_bytes(), output.read_bytes())
        packed['source_revision'] = 'd' * 40
        with self.assertRaisesRegex(ValueError, 'metadata differs'):
            expand_dashboard(packed)

    def test_gzip_rejects_extra_members_and_excess_expansion(self):
        metadata = dict.fromkeys(COMPRESSION_METADATA, 'fixture')
        compressed = gzip.compress(canonical({**metadata, 'views': []}), mtime=0)
        cases = (
            (compressed + compressed, 'invalid compressed cost snapshot stream'),
            (gzip.compress(b'x' * (MAX_EXPANDED_BYTES + 1), mtime=0), 'expanded cost snapshot exceeds'),
        )
        for encoded, error in cases:
            with self.subTest(error=error):
                packed = {**metadata, COMPRESSED_FIELD: base64.b64encode(encoded).decode('ascii')}
                self.assertLess(len(canonical(packed)), MAX_BYTES)
                with self.assertRaisesRegex(ValueError, error):
                    expand_dashboard(packed)

    def test_rejects_a_cost_context_that_expands_past_the_transport_limit(self):
        packed = {'views': [{'groups': [{'outcomes': 0} for _ in range(11)]}],
                  'row_contexts': {'outcomes': [{'evidence': 'x' * 400_000}]}}
        self.assertLess(len(canonical(packed)), MAX_BYTES)
        with self.assertRaisesRegex(ValueError, 'expanded cost snapshot exceeds'):
            expand_dashboard(packed)

    def transported(self):
        document = self.large_activity(144)
        path = self.path.with_name('large-cost.json')
        publish_dashboard(path, document, allow_row_contexts=True)
        expanded = attach_cost_dashboard('{"metrics":[]}', path=path, now=NOW)
        # A snapshot too small for the expanded carrier, as the real one is near month end.
        with patch('pulse_stats.cost_dashboard.SNAPSHOT_MAX_BYTES', len(canonical(document)) // 2):
            snapshot = json.loads(attach_cost_dashboard('{"metrics":[]}', path=path, now=NOW))
        return document, json.loads(expanded), snapshot

    def test_snapshot_carries_gzip_when_the_expanded_carrier_does_not_fit(self):
        from pulse_stats import query
        from pulse_stats.cost_dashboard import TRANSPORT_DIGEST_FIELD, expand_transport_cost_dashboard
        document, expanded, snapshot = self.transported()
        self.assertEqual(expanded['cost_dashboard'], document)
        carrier = snapshot['cost_dashboard']
        self.assertEqual(set(carrier), {*COMPRESSION_METADATA, COMPRESSED_FIELD, TRANSPORT_DIGEST_FIELD})
        self.assertEqual(carrier[TRANSPORT_DIGEST_FIELD],
                         hashlib.sha256(base64.b64decode(carrier[COMPRESSED_FIELD])).hexdigest())
        self.assertLess(len(canonical(snapshot)), len(canonical(document)) // 2)
        # Metrics come from the verified expanded carrier, identical to the uncompressed snapshot.
        self.assertEqual(snapshot['metrics'], expanded['metrics'])
        self.assertEqual(expand_transport_cost_dashboard(carrier), document)
        self.assertEqual(query.embedded_cost(snapshot), (document, None))

    def test_gzip_transport_rejects_a_changed_hash_payload_or_metadata(self):
        from pulse_stats import query
        from pulse_stats.cost_dashboard import TRANSPORT_DIGEST_FIELD, expand_transport_cost_dashboard
        _, _, snapshot = self.transported()
        carrier = snapshot['cost_dashboard']
        raw = gzip.decompress(base64.b64decode(carrier[COMPRESSED_FIELD]))
        other = gzip.compress(raw.replace(b'"status":"available"', b'"status":"unavailable"', 1), mtime=0)
        cases = (
            ({**carrier, TRANSPORT_DIGEST_FIELD: '0' * 64}, 'digest mismatch'),
            ({**carrier, COMPRESSED_FIELD: base64.b64encode(other).decode('ascii')}, 'digest mismatch'),
            ({**carrier, COMPRESSED_FIELD: base64.b64encode(other).decode('ascii'),
              TRANSPORT_DIGEST_FIELD: hashlib.sha256(other).hexdigest()}, 'metadata differs'),
            ({**carrier, COMPRESSED_FIELD: '%%'}, 'invalid cost transport'),
        )
        for changed, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    expand_transport_cost_dashboard(changed)
                self.assertEqual(query.embedded_cost({'cost_dashboard': changed}), (None, 'invalid_cost_snapshot'))

    def test_query_reads_a_gzip_carrier_larger_than_the_publication_limit(self):
        # Random references repeated across the three views: row_contexts stores them
        # once and fits 512KiB, gzip cannot share them, so its carrier exceeds 512KiB
        # while the whole App snapshot still fits 1MiB.
        from random import Random
        from pulse_stats import query
        from pulse_stats.cost_dashboard import SNAPSHOT_MAX_BYTES
        document, rng, refs = self.large_activity(144), Random(192), {}
        for view in document['views']:
            for row in view['groups']:
                key = canonical(row['key'])
                refs.setdefault(key, [['ccf1_%064x' % rng.getrandbits(256) for _ in range(15)] for _ in range(2)])
                detail = dict(row['detail'])
                for field, values in zip(('fact', 'attribution'), refs[key]):
                    detail[field + '_refs'] = values
                    detail[field + '_ref_count'] = max(detail[field + '_ref_count'], len(values))
                row['detail'] = detail
        document['sha256'] = digest({k: v for k, v in document.items() if k != 'sha256'})
        path = self.path.with_name('random-refs-cost.json')
        publish_dashboard(path, document, allow_row_contexts=True)
        self.assertLessEqual(path.stat().st_size, MAX_BYTES)
        rendered = attach_cost_dashboard('{"metrics":[]}', path=path, now=NOW)
        snapshot = json.loads(rendered)
        self.assertGreater(len(snapshot['cost_dashboard'][COMPRESSED_FIELD]), MAX_BYTES)
        self.assertLessEqual(len(rendered.encode()), SNAPSHOT_MAX_BYTES)
        self.assertEqual(query.embedded_cost(snapshot), (document, None))

    def test_scheduled_job_row_carries_its_name_and_attribution_sources(self):
        from pulse_compute_cost.receipts import Receipt, reference
        import hashlib
        from pulse_stats.cost_dashboard import _canonical,_validate
        ledger=UsageLedger(Path(self.tmp.name)/'ledger',clock=lambda:NOW)
        scope=reference('native_session',['codex','thread-1'])
        usage=usage_receipt('pi','scheduled',{'input':1,'output':1,'cacheRead':0,'cacheWrite':0},
            at=NOW.isoformat(),observed=NOW.isoformat(),model='openai/test',native_scope_ref=scope)
        job=Receipt.make('attribution',None,{'target_ref':scope,'wi':None,'executor':None,'responsible':None,
            'delegator':None,'state':'assigned','policy':'scheduled_job.v1','effective_at':'2026-09-13T07:00:00Z',
            'work_kind':'cron','job':{'id':'hestia-triage','name':'Oikos 30 分钟异常分流'}},
            occurred_at='2026-09-13T07:00:00Z',observed_at=NOW.isoformat(),producer='relay_host.v1',
            evidence=(reference('scheduler_run',['run']),))
        ledger.ingest_receipts([usage,job])
        document=build_dashboard(ledger,subscriptions=FACTS,source_revision='c'*40,now=NOW,
            identity_dimensions=True,activity_table=True,activity_only=True)
        [row]=document['views'][0]['groups']
        self.assertEqual(row['key']['wi'],'cron:hestia-triage')
        self.assertEqual(row['attribution']['job'],{'id':'hestia-triage','name':'Oikos 30 分钟异常分流'})
        self.assertEqual(row['attribution']['sources']['scheduled_job']['event_count'],1)
        self.path.unlink(); publish_dashboard(self.path,document)
        self.assertEqual(load_cost_dashboard(self.path,now=NOW)['status'],'available')
        for change in (lambda r: r['attribution']['job'].update(id='other'),
                       lambda r: r['attribution']['sources']['scheduled_job'].update(event_count=2),
                       lambda r: r['attribution']['sources'].update(guessed={'event_count':1,'priced_machine_cash_usd':'0'})):
            broken=deepcopy(document); change(broken['views'][0]['groups'][0])
            broken['sha256']=hashlib.sha256(_canonical({k:v for k,v in broken.items() if k!='sha256'})).hexdigest()
            with self.assertRaisesRegex(ValueError,'attribution|scheduled job'):
                _validate(broken)

    def test_stage_projection_failure_keeps_verified_usage_and_exposes_unknown_output(self):
        from unittest.mock import patch
        ledger=UsageLedger(Path(self.tmp.name)/'ledger',clock=lambda:NOW)
        ledger.ingest_receipts([usage_receipt('pi','stage-fault',{'input':2,'output':3,'cacheRead':0,'cacheWrite':0},
            at=NOW.isoformat(),observed=NOW.isoformat(),model='openai/test',
            common={'wi':'wi_20260825_300','executor':'pulse','responsible':'pulse','delegator':None})])
        with patch('pulse_compute_cost.stage_facts.stage_report', side_effect=ValueError('private corrupt detail')):
            document=build_dashboard(ledger,subscriptions=FACTS,source_revision='c'*40,now=NOW,
                identity_dimensions=True,activity_table=True,activity_only=True)
        self.assertEqual(document['status'],'available')
        self.assertEqual(len(document['views']),3)
        for view in document['views']:
            self.assertEqual(view['stages']['status'],'unavailable')
            self.assertEqual(view['stages']['reason'],'stage_projection_failed')
            self.assertIsNone(view['stages']['outputs'])
        self.assertNotIn('private corrupt detail',json.dumps(document))
        self.path.unlink(); publish_dashboard(self.path,document)
        self.assertEqual(load_cost_dashboard(self.path,now=NOW)['status'],'available')

    def test_roundtrip_and_missing_source(self):
        self.assertEqual(load_cost_dashboard(self.path,now=NOW),self.doc)
        self.path.unlink()
        self.assertEqual(load_cost_dashboard(self.path,now=NOW)['reason'],'source_not_configured')

    def test_changed_bytes_permissions_and_future_do_not_pass(self):
        self.path.chmod(0o644)
        self.assertEqual(load_cost_dashboard(self.path,now=NOW)['reason'],'invalid_cost_snapshot')
        self.path.chmod(0o600)
        self.doc['source_revision']='b'*40
        self.path.write_text(json.dumps(self.doc))
        self.assertEqual(load_cost_dashboard(self.path,now=NOW)['reason'],'invalid_cost_snapshot')
        self.doc['sha256']=digest({k:v for k,v in self.doc.items() if k!='sha256'})
        self.path.write_text(json.dumps(self.doc))
        self.assertEqual(load_cost_dashboard(self.path,now=NOW-timedelta(seconds=1))['reason'],'snapshot_from_future')
        self.assertEqual(load_cost_dashboard(self.path,now=NOW+timedelta(hours=1))['reason'],'snapshot_stale')
        # Display keeps the verified expired publication whole, under its own time.
        self.assertEqual(load_cost_dashboard(self.path,now=NOW+timedelta(hours=1),retain_expired=True),self.doc)
        self.assertEqual(load_cost_dashboard(self.path,now=NOW-timedelta(seconds=1),retain_expired=True)['reason'],
                         'snapshot_from_future')

    def test_cost_failure_and_retired_bills_leave_other_sections_unchanged(self):
        base={'metrics':[{'id':'billing.mtd.total_cents','value':27000},{'id':'usage.today.output_tokens','value':10}],
              'incidents':[{'id':'preserve'}],'alerts':[{'id':'act'}],
              'alert_tiers':{'act':1,'watch':2,'done':3},'global_authority':{'status':'validated'}}
        result=json.loads(attach_cost_dashboard(json.dumps(base),self.path,now=NOW+timedelta(hours=1)))
        self.assertEqual(result['cost_dashboard'],self.doc)
        self.assertNotIn('cost_refresh',result)
        self.assertEqual(result['metrics'],usage_metrics(result['cost_dashboard']))
        for name in ['incidents','alerts','alert_tiers','global_authority']:
            self.assertEqual(result[name],base[name])

    def refresh_status(self, **fields):
        path=self.path.parent/REFRESH_STATUS_NAME
        path.write_text(json.dumps({'schema':'pulse_stats_cost_refresh_status/v1','state':'failed',
            'started_at':(NOW+timedelta(minutes=50)).isoformat(),'finished_at':(NOW+timedelta(minutes=52)).isoformat(),
            'reason':'production_ledger_unavailable','peers':[],'last_completed_seconds':1300,**fields}))
        path.chmod(0o600)
        return path

    def test_refresh_status_rides_beside_the_retained_publication(self):
        self.refresh_status()
        result=json.loads(attach_cost_dashboard('{}',self.path,now=NOW+timedelta(hours=1)))
        self.assertEqual(result['cost_dashboard'],self.doc)
        self.assertEqual(result['cost_refresh'],{'state':'failed','started_at':(NOW+timedelta(minutes=50)).isoformat(),
            'finished_at':(NOW+timedelta(minutes=52)).isoformat(),'reason':'production_ledger_unavailable','peers':[]})
        self.refresh_status(state='completed',reason=None,peers=[
            {'id':'tcloud','status':'unreachable_or_invalid','reason':'peer_production_ledger_unavailable'},
            {'id':'Bad Id','status':'read'},{'id':'mac','status':'read','reason':'/private/path'}])
        result=json.loads(attach_cost_dashboard('{}',self.path,now=NOW+timedelta(hours=1)))
        self.assertEqual(result['cost_refresh']['peers'],
            [{'id':'tcloud','status':'unreachable_or_invalid','reason':'peer_production_ledger_unavailable'}])
        # A peer paused by decision reaches the App as paused, with its reason.
        self.refresh_status(state='completed',reason=None,peers=[
            {'id':'tcloud','status':'paused','reason':'paused_by_decision'},{'id':'oracle-node-1','status':'read'}])
        result=json.loads(attach_cost_dashboard('{}',self.path,now=NOW+timedelta(hours=1)))
        self.assertEqual(result['cost_refresh']['peers'],
            [{'id':'tcloud','status':'paused','reason':'paused_by_decision'},
             {'id':'oracle-node-1','status':'read','reason':None}])

    def test_refresh_status_is_bounded_and_never_invents_progress(self):
        later=NOW+timedelta(hours=1)
        path=self.refresh_status(state='running',finished_at=None,reason=None)
        self.assertEqual(load_refresh_status(path,now=later)['state'],'running')
        # Running far past any cycle means it was stopped without a word.
        stopped=load_refresh_status(path,now=NOW+timedelta(hours=3))
        self.assertEqual((stopped['state'],stopped['reason']),('failed','cycle_interrupted'))
        self.refresh_status(reason='C:/Users/secret path')
        self.assertEqual(load_refresh_status(path,now=later)['reason'],'publication_failed')
        self.refresh_status(started_at=(later+timedelta(minutes=1)).isoformat())
        self.assertIsNone(load_refresh_status(path,now=later))
        self.refresh_status(state='completed',finished_at=None)
        self.assertIsNone(load_refresh_status(path,now=later))
        self.refresh_status(); path.chmod(0o644)
        self.assertIsNone(load_refresh_status(path,now=later))

    def test_real_query_fixture_validates_and_wrong_period_is_rejected(self):
        fixture = ROOT/'Stats/PulseStats/Resources/Fixtures/cost-dashboard-v1.json'
        doc = json.loads(fixture.read_text())['cost_dashboard']
        self.path.write_text(json.dumps(doc))
        self.assertEqual(load_cost_dashboard(self.path,now=NOW)['status'],'available')
        doc['views'][0]['query']['from_date']='2020-01-01'
        doc['sha256']=digest({k:v for k,v in doc.items() if k!='sha256'})
        self.path.write_text(json.dumps(doc))
        self.assertEqual(load_cost_dashboard(self.path,now=NOW)['reason'],'invalid_cost_snapshot')

if __name__=='__main__':
    unittest.main()


class BoundedDiagnosticsReaderTests(unittest.TestCase):
    def test_reader_keeps_full_counts_and_rejects_inconsistent_summary(self):
        from pulse_compute_cost.stats_dashboard import project_cost
        from pulse_stats.cost_dashboard import _cost
        raw = {'machine_cash_usd': None, 'partial_machine_cash_usd': '1',
               'cash_basis': 'openrouter_standard_token_cash', 'reasons': ['incomplete_price_or_quantity'],
               'unpriced': [{'fact_ref': 'ccf1_' + str(i).zfill(64), 'reason': 'missing_or_invalid_price'} for i in range(1500)]}
        value = project_cost(raw); _cost(value)
        self.assertEqual(value['unpriced_count'], 1500)
        self.assertEqual(len(value['unpriced']), 2)
        for key, bad in [('unpriced_count', 2), ('unpriced_count', True),
                         ('unpriced_sha256', 'bad'), ('unpriced_reason_counts', {'other': 1500}),
                         ('unpriced_reason_counts', {'missing_or_invalid_price': 1499})]:
            altered = deepcopy(value); altered[key] = bad
            with self.subTest(field=key, bad=bad), self.assertRaises(ValueError):
                _cost(altered)
        for key in ('unpriced_count', 'unpriced_reason_counts', 'unpriced_sha256'):
            altered = deepcopy(value); del altered[key]
            with self.assertRaises(ValueError): _cost(altered)
        legacy = deepcopy(raw); legacy['unpriced'] = legacy['unpriced'][:2]; _cost(legacy)

    def test_reason_classes_must_partition_reasons(self):
        from pulse_compute_cost.stats_dashboard import project_cost
        from pulse_stats.cost_dashboard import _cost
        raw = {'machine_cash_usd': None, 'partial_machine_cash_usd': '1', 'cash_basis': 'openrouter_standard_token_cash',
               'reasons': ['attribution_unknown', 'source_coverage_unknown'], 'unpriced': []}
        value = project_cost(raw); _cost(value)
        self.assertEqual((value['anomalies'], value['proof_gaps']), (['attribution_unknown'], ['source_coverage_unknown']))
        _cost(raw)  # Older publications without classes stay readable.
        for anomalies, gaps in [(['attribution_unknown'], []), (['attribution_unknown'], ['attribution_unknown', 'source_coverage_unknown']),
                                (['attribution_unknown', 'other'], ['source_coverage_unknown']), ('attribution_unknown', ['source_coverage_unknown'])]:
            altered = dict(value, anomalies=anomalies, proof_gaps=gaps)
            with self.subTest(anomalies=anomalies, gaps=gaps), self.assertRaises(ValueError):
                _cost(altered)
        half = dict(value); del half['proof_gaps']
        with self.assertRaises(ValueError): _cost(half)
