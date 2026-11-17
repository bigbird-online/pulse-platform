"""Regressions for the real dashboard faults; all account responses synthetic."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'Stats/exporter'),str(ROOT/'ComputeCost')]
from pulse_stats.codex_quota import account_ref, projection, validate, QuotaError, read_account_quota, read_account_identity
from pulse_stats.cost_dashboard import attach_cost_dashboard
from pulse_compute_cost.stats_dashboard import build_dashboard, publish_dashboard
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import usage_receipt

NOW=datetime(2026,9,15,0,0,tzinfo=timezone.utc)

class AccountQuotaTests(unittest.TestCase):
    def account(self, email='fixture@example.invalid'):
        return {'account':{'type':'chatgpt','email':email,'planType':'pro'}}
    def limits(self):
        return {'rateLimitsByLimitId':{'codex':{
            'limitId':'codex','planType':'pro',
            'primary':{'usedPercent':20,'windowDurationMins':300,'resetsAt':int(NOW.timestamp())+3600},
            'secondary':{'usedPercent':61,'windowDurationMins':10080,'resetsAt':int(NOW.timestamp())+86400}}}}
    def test_weekly_is_selected_by_duration_not_primary(self):
        result=projection(self.limits(),account_ref(self.account()),NOW)
        self.assertEqual(result['weekly']['used_percent'],61)
        self.assertNotIn('fixture@example.invalid',json.dumps(result))
        self.assertEqual(validate(result,NOW),result)
    def test_weekly_can_be_primary(self):
        native=self.limits();bucket=native['rateLimitsByLimitId']['codex']
        bucket['primary']=bucket.pop('secondary')
        result=projection(native,account_ref(self.account()),NOW)
        self.assertEqual(result['weekly']['used_percent'],61)
    def test_other_limit_cannot_supply_reset_or_percent(self):
        native=self.limits();bucket=native['rateLimitsByLimitId'].pop('codex')
        native['rateLimitsByLimitId']['other']=bucket
        with self.assertRaisesRegex(QuotaError,'codex_bucket'):projection(native,'a'*64,NOW)
    def test_duplicate_week_windows_are_not_silently_chosen(self):
        native=self.limits();bucket=native['rateLimitsByLimitId']['codex'];bucket['primary']=bucket['secondary']
        with self.assertRaisesRegex(QuotaError,'weekly_window'):projection(native,'a'*64,NOW)
    def test_reading_stays_current_inside_its_weekly_window_only(self):
        value=projection(self.limits(),'a'*64,NOW)
        # Older than the five-hour window and the publisher interval, still this week.
        for later in (timedelta(minutes=45),timedelta(hours=3),timedelta(hours=23)):
            self.assertEqual(validate(value,NOW+later),value)
        with self.assertRaisesRegex(QuotaError,'quota_window_elapsed'):validate(value,NOW+timedelta(days=1))
        with self.assertRaisesRegex(QuotaError,'quota_from_future'):validate(value,NOW-timedelta(minutes=2))
        earlier=dict(value,observed_at=(NOW-timedelta(days=6,hours=1)).isoformat())
        with self.assertRaisesRegex(QuotaError,'quota_outside_current_window'):validate(earlier,NOW)
        native=self.limits();native['rateLimitsByLimitId']['codex']['secondary']['resetsAt']=int(NOW.timestamp())
        with self.assertRaisesRegex(QuotaError,'invalid_quota_window'):projection(native,'a'*64,NOW)
    def test_missing_week_or_bad_quantities_stay_unknown(self):
        for key,value in [('usedPercent',True),('usedPercent',float('nan')),('resetsAt',None),('windowDurationMins',0)]:
            native=self.limits();native['rateLimitsByLimitId']['codex']['secondary'][key]=value
            with self.subTest(key=key),self.assertRaises(QuotaError):projection(native,'a'*64,NOW)
    def test_no_api_key_account_or_anonymous_identity(self):
        for account in ({'type':'apikey'},{'type':'chatgpt'}):
            with self.assertRaises(QuotaError):account_ref({'account':account})
    def test_cached_week_is_bound_to_matching_window(self):
        doc=projection(self.limits(),'a'*64,NOW);doc['weekly']=dict(doc['weekly'],used_percent=0)
        with self.assertRaisesRegex(QuotaError,'quota_window_mismatch'):validate(doc,NOW)
    def test_rpc_uses_only_read_methods_and_closes_process(self):
        # The stub errors on any thread/turn method. It has no auth or network.
        script='''import json,sys,time
for line in sys.stdin:
 r=json.loads(line); m=r['method']
 assert m in {'initialize','initialized','account/read','account/rateLimits/read'}
 if m=='initialized':continue
 if m=='initialize':v={}
 elif m=='account/read':v={'account':{'type':'chatgpt','email':'fixture@example.invalid'}}
 else:v={'rateLimits':{'limitId':'codex','primary':{'usedPercent':37,'windowDurationMins':10080,'resetsAt':int(time.time())+86400}}}
 print(json.dumps({'id':r['id'],'result':v}),flush=True)
'''
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'server.py';p.write_text(script)
            result=read_account_quota([sys.executable,str(p)],codex_home=Path(temp),timeout=5)
        self.assertEqual(result['weekly']['used_percent'],37)
    def test_identity_check_does_not_read_limits_or_refresh_tokens(self):
        script = "import json,sys\nfor line in sys.stdin:\n r=json.loads(line);m=r['method']\n assert m in {'initialize','initialized','account/read'}\n if m=='initialized':continue\n if m=='account/read':\n  assert r['params']=={'refreshToken':False}\n  v={'account':{'type':'chatgpt','email':'fixture@example.invalid'}}\n else:v={}\n print(json.dumps({'id':r['id'],'result':v}),flush=True)\n"
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'server.py';p.write_text(script)
            result=read_account_identity([sys.executable,str(p)],codex_home=Path(temp),timeout=5)
        self.assertEqual(result, account_ref(self.account()))

    def test_rpc_rejects_account_switch_during_read(self):
        script='''import json,sys,time
n=0
for line in sys.stdin:
 r=json.loads(line);m=r['method']
 if m=='initialized':continue
 if m=='account/read':
  n+=1;v={'account':{'type':'chatgpt','email':str(n)+'@example.invalid'}}
 elif m=='account/rateLimits/read':v={}
 else:v={}
 print(json.dumps({'id':r['id'],'result':v}),flush=True)
'''
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'server.py';p.write_text(script)
            with self.assertRaisesRegex(QuotaError,'account_changed'):
                read_account_quota([sys.executable,str(p)],codex_home=Path(temp),timeout=5)

class DailyUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.ledger=UsageLedger(self.root/'ledger',clock=lambda:NOW-timedelta(seconds=1))
        self.path=self.root/'private/cost.json'
        self.base={'metrics':[{'id':'usage.today.output_tokens','value':99999},
                {'id':'usage.mtd.subscription_usd','value':270}, {'id':'harness.wi.week_created','value':12}],
                'incidents':[{'keep':'same'}],'alerts':[{'keep':'same'}]}
    def build(self,rows):
        self.ledger.ingest_receipts(rows)
        doc=build_dashboard(self.ledger,subscriptions={'schema_version':'pulse_stats_procurement_facts/v1','subscriptions':[]},source_revision='a'*40,now=NOW)
        publish_dashboard(self.path,doc)
        return doc
    def row(self,agent='pulse'):
        return usage_receipt('pi',['fixture',agent],{'input':100,'cacheRead':1000,'cacheWrite':20,'output':40},
                model='fixture/model',at=(NOW-timedelta(seconds=30)).isoformat(),observed=NOW.isoformat(),
                common={'wi':'wi_20260825_300','executor':agent,'responsible':None,'delegator':None})
    def render(self,now=NOW):
        result=json.loads(attach_cost_dashboard(json.dumps(self.base),self.path,now=now))
        return result,{m['id']:m for m in result['metrics']}
    def test_raw_query_token_classes_are_not_discarded_or_legacy_added(self):
        self.build([self.row()]);result,metrics=self.render()
        self.assertEqual(metrics['usage.today.output_tokens']['value'],40)
        self.assertEqual(metrics['usage.today.input_tokens_uncached']['value'],100)
        self.assertEqual(metrics['usage.today.cache_creation_tokens']['value'],20)
        self.assertEqual(metrics['usage.today.cache_read_tokens']['value'],1000)
        self.assertNotIn('usage.mtd.subscription_usd',metrics)
        self.assertEqual(result['incidents'],self.base['incidents'])
        self.assertEqual(result['alerts'],self.base['alerts'])
        self.assertEqual(metrics['harness.wi.week_created']['value'],12)
    def test_agent_names_and_output_are_same_query_not_token_inferred_turns(self):
        self.build([self.row(),self.row('duke')]);_,m=self.render()
        rows=m['usage.by_agent']['value']
        self.assertEqual({r['agent'] for r in rows},{'pulse','duke'})
        self.assertTrue(all(r['output_tokens']==40 and r['turns'] is None and r['session_count'] is None for r in rows))
    def test_missing_source_never_falls_back_to_legacy_and_expired_keeps_its_time(self):
        _,m=self.render()
        self.assertIsNone(m['usage.today.output_tokens']['value'])
        self.assertIsNone(m['usage.by_agent']['value'])
        doc=self.build([self.row()]);_,m=self.render(NOW+timedelta(hours=1))
        # The same day's last publication, never the legacy value, at its own time.
        self.assertEqual(m['usage.today.output_tokens']['value'],40)
        self.assertEqual(m['usage.today.output_tokens']['observed_at'],doc['generated_at'])
    def test_empty_unknown_coverage_is_not_fabricated_zero(self):
        self.build([]);_,m=self.render()
        self.assertIsNone(m['usage.today.output_tokens']['value'])
        self.assertIsNone(m['usage.by_agent']['value'])
        self.assertIn('不能判定没有用量',m['usage.today.source_note']['reason'])
    def test_independent_class_unknown_does_not_erase_known_output(self):
        self.build([usage_receipt('pi','missing-cache',{'input':100,'output':40},
            model='fixture/model',at=(NOW-timedelta(seconds=30)).isoformat(),observed=NOW.isoformat())])
        _,m=self.render()
        self.assertEqual(m['usage.today.output_tokens']['value'],40)
        self.assertIsNone(m['usage.today.cache_creation_tokens']['value'])

    def test_old_date_does_not_become_today_after_midnight(self):
        from pulse_stats.cost_dashboard import usage_metrics
        doc=self.build([self.row()])
        at=NOW+timedelta(days=1)
        metrics={m['id']:m for m in usage_metrics(doc,now=at)}
        self.assertIsNone(metrics['usage.today.output_tokens']['value'])
        self.assertIn('日界',metrics['usage.today.source_note']['reason'])
    def test_oversize_source_removal_also_removes_derived_token_values(self):
        self.build([self.row()])
        self.base['large_existing_payload']='x'*(1024*1024)
        result,metrics=self.render()
        self.assertEqual(result['cost_dashboard']['reason'],'query_payload_too_large')
        self.assertIsNone(metrics['usage.today.output_tokens']['value'])

class MonitoringActionTests(unittest.TestCase):
    def test_mac_and_cron_incidents_do_not_suggest_linux_commands(self):
        from pulse_stats.live_sources import _heartbeat_action
        mac=_heartbeat_action({'service':'com.pulse.heartbeat-send'},'ownermac-probe')
        cron=_heartbeat_action({'service':'hestia p0-health-probe/probe.sh'},'hestia-p0-probe')
        self.assertIn('launchctl',mac);self.assertIn('cron',cron)
        self.assertNotIn('journalctl',mac+cron)
        # Hestia's heartbeat receiver is the TCloud Relay.
        self.assertIn('TCloud',mac);self.assertIn('TCloud',cron)
    def test_linux_probe_points_to_actual_service_and_timer_not_source_label(self):
        from pulse_stats.live_sources import _heartbeat_action
        value=_heartbeat_action({'service':'ack-probe.service','timer':'ack-probe.timer'},'vultr-ack-probe')
        self.assertIn('systemctl --user status ack-probe.timer',value)
        self.assertIn('journalctl --user -u ack-probe.service',value)
        self.assertNotIn('-u vultr-ack-probe',value)

if __name__=='__main__':unittest.main()
