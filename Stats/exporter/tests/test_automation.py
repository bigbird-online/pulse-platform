from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]))
from pulse_stats import automation as a
from pulse_stats.automation_source import read_site, catalog
from pulse_stats import automation_source

NOW=datetime(2026,9,18,4,30,tzinfo=timezone.utc)
HASH='a'*64


ORACLE='oracle-node-1'


def source_document():
    # Oracle follows the original two nodes, which keep their positions 0 and 1.
    return dict(schema='pulse_stats_automation_sources/v1',sources=[dict(schema=a.SCHEMA,
        site=site,status='available',observed_at=NOW.isoformat(),catalog_digest=a.catalog_digest(),config_digest='a'*64,state_digest='b'*64,jobs=[])
        for site in sorted(a.SITES,key=lambda site:(site==ORACLE,site))])


def job(identifier='aki-19e4fb47ffb', enabled=True, **updates):
    result=dict(runId='run-one',definitionHash=HASH,startedAt='2026-09-18T04:00:01Z',finishedAt='2026-09-18T04:03:00Z',
        scheduledFor='2026-09-18T04:00:00Z',status='ok',executionStatus='ok',deliveryStatus='sent')
    value=dict(id=identifier,owner='aki',enabled=enabled,definition_verified=True,requires_artifact=False,
        delivery='feishu_document',skip_allowed=False,schedule=dict(expr='0 12 * * *',tz='Asia/Shanghai'),
        state=dict(definitionHash=HASH,activatedAt='2026-09-18T00:00:00Z',lastResult=result))
    value.update(updates)
    return value


class AutomationTests(unittest.TestCase):
    def project(self,d=None,now=NOW):
        return a.metrics(d if d is not None else source_document(),now)[0][0]['value']
    def row(self,j,now=NOW):
        d=source_document();d['sources'][0]['jobs']=[j]
        return next(r for r in self.project(d,now) if r['id']=='daily-companion')
    def test_retired_control_entries_have_no_row_and_unregistered_jobs_stay_visible(self):
        d=source_document()
        d["sources"][0]["execution_control"]=dict(automatic_enabled=False,reason="human pause",
            decision_ref="WI219",updated_at=NOW.isoformat(),
            entries={"resource:hestia":dict(owner="hestia",enabled=True),"dispatch:pulse":dict(owner="pulse",enabled=True)})
        d["sources"][0]["jobs"]=[job(identifier="new-background-entry",owner="hestia")]
        rows=self.project(d)
        self.assertFalse(any(r["reference"].startswith(("resource:","dispatch:")) for r in rows))
        self.assertFalse(any(r["title"].startswith(("资源告警","交接与巡检")) for r in rows))
        self.assertEqual(next(r for r in rows if r["reference"]=="job:new-background-entry")["status"],"未运行 · 未获许可")

    def test_all_confirmed_paused_remain_visible_gray_not_failed_or_completed(self):
        rows=self.project();self.assertEqual(len(rows),11)
        self.assertTrue(all(r['status']=='未运行 · 待恢复' and r['color']=='gray' for r in rows))
        self.assertEqual(len({r['id'] for r in rows}),11)
        self.assertEqual([r['id'] for r in rows][-2:],['checkout-follow','collab-inbox'])
    def test_one_service_enables_without_ending_other_services_pause(self):
        d=source_document();d['sources'][0]['jobs']=[job()]
        rows=self.project(d)
        self.assertEqual(next(r for r in rows if r['id']=='daily-companion')['status'],'已送达')
        self.assertEqual(sum(r['status']=='未运行 · 待恢复' for r in rows),10)
    def test_pausing_configuration_does_not_hide_a_still_running_round(self):
        self.assertEqual(self.row(job(enabled=False,current_run_live=True))['status'],'暂停后仍有运行')
    def test_mismatched_display_catalog_is_not_known_paused(self):
        d=source_document();d['sources'][0]['catalog_digest']='b'*64
        self.assertTrue(all(r['color']=='orange' for r in self.project(d)))
    def test_untyped_enabled_and_missing_source_identity_cannot_confirm_pause(self):
        d=source_document();d['sources'][0]['jobs']=[job(enabled='false')]
        self.assertTrue(all(r['color']=='orange' for r in self.project(d)))
        d=source_document();d['sources'][0].pop('config_digest')
        self.assertTrue(all(r['color']=='orange' for r in self.project(d)))
    def test_a_round_the_scheduler_refused_for_permission_is_not_run(self):
        # Relay records a refused round as ok/skipped; it never executed.
        j=job();j['state']['lastResult'].update(skipped=True,reasonCode='automatic_paused',deliveryStatus='skipped')
        row=self.row(j)
        self.assertEqual((row['color'],row['status']),('gray','未运行 · 未获许可'))
    def test_a_wake_job_reads_delivered_or_not_delivered(self):
        j=job(delivery='none');j['state']['lastResult'].update(skipped=True,reasonCode='wake_delivered',deliveryStatus='skipped')
        self.assertEqual((self.row(j)['color'],self.row(j)['status']),('green','已唤醒'))
        j['state']['lastResult'].update(reasonCode='wake_not_delivered',deliveryStatus='sent')
        row=self.row(j);self.assertEqual((row['color'],row['status']),('orange','未送达'));self.assertIn('已提醒负责人',row['reason'])
    def test_old_errors_do_not_override_current_confirmed_pause(self):
        j=job(enabled=False);j['state']=dict(lastRunAt='2026-08-22T04:00:00Z',lastStatus='error',consecutiveErrors=65)
        self.assertEqual(self.row(j)['status'],'未运行 · 待恢复')
    def test_missing_stale_future_corrupt_sources_never_claim_pause_or_all_good(self):
        for mode in ('missing','stale','future','duplicate','bad'):
            d=source_document()
            if mode=='missing':d['sources'].pop()
            if mode=='stale':d['sources'][0]['observed_at']=(NOW-timedelta(hours=1)).isoformat()
            if mode=='future':d['sources'][0]['observed_at']=(NOW+a.CLOCK_SKEW+timedelta(seconds=1)).isoformat()
            if mode=='duplicate':d['sources'][0]=d['sources'][1]
            if mode=='bad':d['sources'][0]['status']='unavailable'
            self.assertTrue(all(r['color']=='orange' for r in self.project(d)),mode)
    def test_a_peer_clock_slightly_ahead_still_counts_as_current(self):
        d=source_document();d['sources'][0]['observed_at']=(NOW+a.CLOCK_SKEW).isoformat()
        self.assertEqual(a.metrics(d,NOW)[1]['freshness'],'fresh')
    def test_reader_sites_are_this_node_tcloud_and_oracle(self):
        self.assertEqual(a.SITES,{'ownermac','tcloud',ORACLE})
        self.assertEqual(set(automation_source.SITES),a.SITES)
        rows=self.project()
        self.assertTrue(all(r['coverage']=='已读 3/3 个节点的任务来源' for r in rows if r['coverage']!='实际任务来源'))
    def test_native_failure_timeout_delivery_failure_not_just_an_old_exit_code(self):
        for status,execution,delivery in [('error','error','skipped'),('timeout','timeout','skipped'),('error','ok','failed')]:
            j=job();j['state']['lastResult'].update(status=status,executionStatus=execution,deliveryStatus=delivery)
            self.assertEqual(self.row(j)['color'],'red')
        j=job();j['state'].pop('lastResult');j['state']['lastStatus']='error'
        self.assertEqual(self.row(j)['color'],'orange')
    def test_running_supported_noop_and_result_missing_are_distinct(self):
        j=job(current_run_live=True);self.assertEqual(self.row(j)['status'],'执行中')
        j=job(skip_allowed=True);j['state']['lastResult'].update(skipped=True,deliveryStatus='skipped')
        self.assertEqual(self.row(j)['status'],'按约定无需执行')
        def follow(summary):
            j=job('pulse-four-end-sync',owner='pulse',skip_allowed=True)
            j['state']['lastResult'].update(skipped=True,deliveryStatus='skipped')
            if summary is not None: j['state']['lastResult']['skipSummary']=summary
            d=source_document();d['sources'][0]['jobs']=[j]
            return next(r for r in self.project(d,NOW) if r['id']=='checkout-follow')
        self.assertEqual(follow('== ownermac\nbigbird-online/PULSE current\n== tcloud-node2\nbigbird-online/PULSE fast_forwarded')['status'],'已同步')
        blocked=follow('== ownermac\nowner/repo blocked (already reported): main diverged')
        self.assertEqual((blocked['color'],blocked['status']),('orange','同步受阻（已告警）'))
        self.assertEqual(follow(None)['status'],'按约定无需执行')
        j=job(delivery='none');self.assertEqual((self.row(j)['color'],self.row(j)['status']),('green','已执行 · 未配结果检查'))
        j['requires_artifact']=True;j['state']['lastResult']['artifactVerified']=True
        self.assertEqual(self.row(j)['status'],'本轮已核验')
    def test_configured_verify_needs_its_own_receipt_from_this_round(self):
        def check(**result):
            j=job(delivery='none',requires_verify=True);j['state']['lastResult'].update(result);return self.row(j)
        missing=check()
        self.assertEqual((missing['color'],missing['status']),('orange','结果待核验'))
        self.assertIn('已配置核验命令，本轮缺核验回执',missing['reason']);self.assertNotIn('尚未配置',missing['reason'])
        passed=check(verifyStatus='passed')
        self.assertEqual((passed['color'],passed['status']),('green','本轮已核验'));self.assertIn('核验命令',passed['reason'])
        self.assertEqual(check(artifactVerified=True)['color'],'orange')
        failed=check(status='error',executionStatus='error',verifyStatus='failed')
        self.assertEqual((failed['color'],failed['status']),('red','核验失败'))
        self.assertEqual(check(status='error',executionStatus='error')['status'],'执行失败')
        limited=check(status='error',executionStatus='error',deliveryStatus='skipped',deferred=True)
        self.assertEqual((limited['color'],limited['status']),('orange','等待重试'))
        old=check(verifyStatus='passed',scheduledFor='2026-09-17T04:00:00Z')
        self.assertEqual(old['status'],'本轮结果待核验')
        j=job(delivery='none',requires_verify=True);j['state']['lastResult'].update(verifyStatus='passed',definitionHash='b'*64)
        self.assertEqual(self.row(j)['color'],'orange')
        j=job(delivery='none',requires_artifact=True,requires_verify=True);j['state']['lastResult']['verifyStatus']='passed'
        self.assertIn('已配置产物检查，本轮缺核验回执',self.row(j)['reason'])
        j=job(delivery='none',requires_artifact=True);self.assertIn('已配置产物检查',self.row(j)['reason'])
    def test_definition_reload_old_result_and_future_result_cannot_turn_green(self):
        j=job(definition_verified=False);self.assertEqual(self.row(j)['color'],'orange')
        j=job();j['state']['lastResult']['definitionHash']='b'*64
        self.assertEqual(self.row(j)['color'],'orange')
        j=job();j['state']['lastResult']['finishedAt']='2026-09-19T04:00:00Z'
        self.assertEqual(self.row(j)['color'],'orange')
    def test_old_unfulfilled_slot_is_history_beside_the_current_round(self):
        j=job();old=deepcopy(j['state']['lastResult']);old.update(runId='old',status='error',deliveryStatus='failed',scheduledFor='2026-09-17T04:00:00Z')
        j['state']['unresolvedResults']=[old]
        row=self.row(j)
        # A later success is this round; it neither hides nor settles the earlier failure.
        self.assertEqual((row['color'],row['status']),('green','已送达'))
        self.assertIn('上期欠交 1 轮（09-17 12:00 交付失败）',row['history'])
        self.assertTrue(row['attention']);self.assertIn('不计入本期状态',row['note'])
        self.assertIn('历史（不计入本期）',row['progress'])
        j['state']['unresolvedResults']=[];j['state']['resultHistoryIncomplete']=True
        row=self.row(j)
        self.assertEqual(row['status'],'已送达');self.assertIn('历史摘要不完整',row['history'])
        self.assertNotIn('收敛任务',row['history'])
    def test_morning_news_is_its_current_single_task(self):
        news=next(t for t in catalog()['services'] if t['id']=='morning-news')
        self.assertEqual([s['job'] for s in news['stages']],['aki-19dad28dcce'])
        d=source_document();d['sources'][2]['jobs']=[job('aki-19dad28dcce')]
        row=self.project(d)[0]
        self.assertEqual((row['color'],row['status'],row['node'],row['mapping']),('green','已送达',ORACLE,''))
    # A real multi-stage pattern: a draft stage without its own check, proven by its delivering stage.
    STAGED=('svc-draft','svc-send')
    def staged(self,*jobs,stages=None,site=0):
        cfg=deepcopy(catalog())
        cfg['services'].append(dict(id='two-stage',title='两阶段服务',owner='aki',period='daily',expectation='成稿后送达',
            stages=stages or [dict(job=self.STAGED[0],proven_by=self.STAGED[1]),dict(job=self.STAGED[1])]))
        d=source_document();d['sources'][site]['jobs']=[deepcopy(j) for j in jobs]
        with patch.object(a,'catalog',return_value=cfg):
            return next(r for r in self.project(d) if r['id']=='two-stage')
    def test_two_stages_one_row_partial_enabling_and_confirmed_delivery(self):
        first=job(self.STAGED[0],delivery='none');second=job(self.STAGED[1])
        partial=self.staged(first)
        self.assertEqual(partial['status'],'部分阶段未启用');self.assertIn('svc-send 未在任何节点配置',partial['reason'])
        self.assertEqual(self.staged(first,second)['status'],'已送达')
        # Without the map naming its proof, a draft is not inferred from a delivery.
        self.assertEqual(self.staged(first,second,stages=[dict(job=j) for j in self.STAGED])['status'],'已执行 · 未配结果检查')
        first['state']['lastResult']['status']='error'
        self.assertEqual(self.staged(first,second)['color'],'red')
    def variants(self):
        first=job(self.STAGED[0],delivery='none');second=job(self.STAGED[1])
        yield 'draft and delivery',first,second
        first=job(self.STAGED[0],current_run_live=True);second=job(self.STAGED[1],delivery='none')
        yield 'running and unverified',first,second
        first=job(self.STAGED[0]);second=job(self.STAGED[1],delivery='none',requires_verify=True)
        second['state']['lastResult']['verifyStatus']='passed'
        yield 'delivered and verified',first,second
        first=job(self.STAGED[0]);second=job(self.STAGED[1])
        first['state']['lastResult'].update(status='timeout',executionStatus='timeout')
        second['state']['lastResult'].update(status='error',deliveryStatus='failed')
        yield 'two failures',first,second
        first=job(self.STAGED[0]);second=job(self.STAGED[1])
        second['state']['activatedAt']='2026-09-18T04:01:00Z';second['state'].pop('lastResult')
        yield 'delivered and still due',first,second
        first=job(self.STAGED[0]);second=job(self.STAGED[1],caption='text')
        yield 'delivered and caption without receipt',first,second
        first=job(self.STAGED[0],delivery='none',requires_verify=True);second=job(self.STAGED[1],delivery='none',skip_allowed=True)
        first['state']['lastResult']['verifyStatus']='passed'
        second['state']['lastResult'].update(skipped=True,deliveryStatus='skipped')
        yield 'recorded with unproven notice and a silent round',first,second
        first=job(self.STAGED[0]);second=job(self.STAGED[1])
        second['state']['lastResult']['scheduledFor']='2026-09-17T04:00:00Z'
        yield 'delivered and previous round',first,second
        first=job(self.STAGED[0]);second=job(self.STAGED[1])
        first['state']['activatedAt']='2026-09-18T04:01:00Z';first['state'].pop('lastResult')
        second['state']['lastResult'].update(skipped=True,reasonCode='automatic_paused',deliveryStatus='skipped')
        yield 'still due and refused',first,second
    def test_service_result_does_not_depend_on_stage_or_source_order(self):
        from itertools import permutations
        plain=[dict(job=self.STAGED[0],proven_by=self.STAGED[1]),dict(job=self.STAGED[1])]
        notice=[dict(job=self.STAGED[0],unproven=['应发通知送达']),dict(job=self.STAGED[1])]
        results={}
        for name,first,second in self.variants():
            specs=notice if 'notice' in name else plain
            seen=set()
            for stages in permutations([first,second]):
                for order in permutations(specs):
                    for site in (0,1,2):
                        row=self.staged(*stages,stages=[dict(s) for s in order],site=site)
                        seen.add((row['color'],row['status'],row['reason']))
            self.assertEqual(len(seen),1,(name,seen))
            results[name]=next(iter(seen))[:2]
        self.assertEqual(results,{'draft and delivery':('green','已送达'),'running and unverified':('orange','执行中'),
            'delivered and verified':('green','已送达'),'two failures':('red','执行超时'),
            'delivered and still due':('green','按计划待执行'),'delivered and caption without receipt':('green','已送达'),
            'recorded with unproven notice and a silent round':('green','部分已核验'),
            'delivered and previous round':('orange','本轮结果待核验'),
            'still due and refused':('gray','未运行 · 未获许可')})
    def test_a_manual_success_does_not_make_a_still_due_stage_complete(self):
        first=job(self.STAGED[0]);second=job(self.STAGED[1],delivery='none')
        second['state']['activatedAt']='2026-09-18T04:10:00Z'
        second['state']['lastResult'].update(startedAt='2026-09-18T04:11:00Z',finishedAt='2026-09-18T04:11:30Z',
                                             deliveryStatus='skipped')
        second['state']['lastResult'].pop('scheduledFor')
        self.assertEqual(a.stage(second,NOW)[1],'按计划待执行')
        self.assertEqual(self.staged(first,second,stages=[dict(job=j) for j in self.STAGED])['status'],'按计划待执行')
    def test_final_delivery_does_not_claim_an_unverified_draft_is_current(self):
        first=job(self.STAGED[0],delivery='none',definition_verified=False)
        second=job(self.STAGED[1])
        self.assertEqual(self.staged(first,second)['color'],'orange')
    def test_invalid_unresolved_timestamp_is_not_a_confirmed_incident(self):
        j=job();prior=deepcopy(j['state']['lastResult']);prior.update(runId='old',status='error',finishedAt='future')
        j['state']['unresolvedResults']=[prior]
        row=self.row(j)
        self.assertEqual(row['status'],'已送达')
        self.assertIn('1 条未核销记录不完整，不能确认是否欠交',row['history'])
    def test_missing_oracle_keeps_known_results_and_marks_only_affected_services(self):
        d=source_document();d['sources'][0]['jobs']=[job()]
        d['sources'][2].update(status='unavailable',jobs=[])
        rows={r['id']:r for r in self.project(d)}
        companion=rows['daily-companion']
        self.assertEqual((companion['color'],companion['status']),('green','已送达'))
        self.assertIn(f'{ORACLE} · 来源不可读取',companion['progress'])
        self.assertEqual(companion['coverage'],'已读 2/3 个节点的任务来源')
        news=rows['morning-news']
        self.assertEqual((news['color'],news['status']),('orange','来源待核验'))
        self.assertIn(ORACLE,news['reason'])
        self.assertFalse(any(r['status']=='待恢复' for r in rows.values()))
        disabled=job('aki-19dad28dcce',enabled=False)
        d['sources'][0]['jobs'].append(disabled)
        self.assertEqual(next(r for r in self.project(d) if r['id']=='morning-news')['status'],'来源待核验')
    def test_duplicate_enabled_copy_and_wrong_owner_are_unknown_not_merged(self):
        d=source_document();d['sources'][0]['jobs']=[job()];d['sources'][1]['jobs']=[job()]
        self.assertEqual(self.project(d)[1]['status'],'运行归属冲突')
        self.assertEqual(self.row(job(owner='other'))['status'],'运行归属冲突')
    def test_daily_weekly_activation_and_day_boundary(self):
        last,nxt=a.slots('0 16 * * 0','Asia/Shanghai',NOW.isoformat(),'2026-09-18T00:00:00Z')
        self.assertIsNone(last);self.assertEqual(nxt.isoformat(),'2026-09-20T08:00:00+00:00')
        j=job();j['state']['activatedAt']='2026-09-18T04:01:00Z';j['state'].pop('lastResult')
        self.assertEqual(self.row(j)['status'],'按计划待执行')
        self.assertEqual(a.stage(job(),NOW+timedelta(days=1))[1],'本轮结果待核验')
    def test_live_collector_is_independent_of_cost_and_retains_history(self):
        from Stats.exporter.tests.test_live import LiveFixture,FakeRunner,systemd_result,CommandResult
        with tempfile.TemporaryDirectory() as temp:
            fixture=LiveFixture(Path(temp))
            fixture.paths=replace(fixture.paths,retire_legacy_probes=True)
            fixture.registry.write_text(json.dumps({'schema_version':1,'sources':[{'source':'hestia-p0-probe','enabled':True,'max_age_seconds':1500}]}))
            fixture.heartbeats.write_text(json.dumps({'hestia-p0-probe':{'receivedAt':NOW.isoformat()}}))
            fixture.ledger.unlink()
            service=fixture.service(FakeRunner(ack=CommandResult(1,"retired"),systemd=systemd_result()),now=NOW,automation_fetch=source_document)
            document=json.loads(service.render_json())
            tasks=next(r for r in document['metrics'] if r['id']==a.METRIC_ID)
            self.assertEqual(len(tasks['value']),11)
            self.assertFalse(any(m['id']=='automation.collaboration_contracts' for m in document['metrics']))
            self.assertFalse(any(s['id']=='ack-probe' for s in document['provenance']['sources']))



class ServiceMapTests(unittest.TestCase):
    """One service map names each job once; facts that disagree with it are named, not tidied."""
    def rows(self,d,cfg=None,now=NOW):
        with patch.object(a,'catalog',return_value=cfg or catalog()):
            return {r['id']:r for r in a.metrics(d,now)[0][0]['value']}
    def test_shipped_map_is_consistent_and_each_difference_is_named(self):
        self.assertEqual(automation_source.check_catalog(catalog()),[])
        cfg=deepcopy(catalog())
        cfg['services'][1]['stages'].append(dict(job='aki-19dad28dcce'))
        cfg['services'][2]['stages'].append(dict(job='aki-collab-inbox'))
        cfg['services'][3]['stages'][0]['proven_by']='elsewhere'
        cfg['services'].append(deepcopy(cfg['services'][4]))
        diffs=automation_source.check_catalog(cfg)
        self.assertIn('任务 aki-19dad28dcce 同时映射到服务 morning-news 与 daily-companion',diffs)
        self.assertIn('任务 aki-collab-inbox 同时属于服务 s1-daily 与家族 collab-inbox',diffs)
        self.assertTrue(any('由本服务外的 elsewhere 证明' in d for d in diffs))
        self.assertIn('服务 id 缺失或重复：oikos-daily',diffs)
        with patch.object(automation_source.json,'loads',return_value=cfg), self.assertRaisesRegex(ValueError,'映射校验失败'):
            automation_source.catalog()
    def test_a_new_family_folds_by_the_map_without_code(self):
        cfg=deepcopy(catalog())
        cfg['families'].append(dict(cfg['families'][0],id='heartbeat',title='心跳',pattern='([a-z][a-z0-9]*)-heartbeat'))
        d=source_document();d['sources'][1]['jobs']=[job('hestia-heartbeat',owner='hestia',delivery='none',skip_allowed=True)]
        d['sources'][1]['jobs'][0]['state']['lastResult'].update(skipped=True,deliveryStatus='skipped')
        rows=self.rows(d,cfg)
        self.assertEqual(rows['heartbeat']['title'],'心跳 · 1 个 Agent')
        self.assertFalse(any(k.startswith('job:') for k in rows))
    def test_duplicate_owner_retired_and_permission_differences(self):
        d=source_document();d['sources'][0]['jobs']=[job()];d['sources'][2]['jobs']=[job()]
        row=self.rows(d)['daily-companion']
        self.assertEqual(row['status'],'运行归属冲突')
        self.assertIn('aki-19e4fb47ffb 在 oracle-node-1、tcloud 同时启用',row['mapping'])
        d=source_document();d['sources'][0]['jobs']=[job(owner='amanda')]
        self.assertIn('负责人：目录为 aki，tcloud 实际为 amanda',self.rows(d)['daily-companion']['reason'])
        d=source_document();d['sources'][2]['jobs']=[job('aki-19dad28dcce'),job('aki-19da9ee9105')]
        row=self.rows(d)['morning-news']
        self.assertEqual((row['color'],row['status']),('orange','映射待核对'))
        self.assertIn('已退役的 aki-19da9ee9105 仍在 oracle-node-1 启用',row['mapping'])
        self.assertNotIn('job:oracle-node-1:aki-19da9ee9105',self.rows(d))
        d=source_document();d['sources'][2]['jobs']=[job()]
        d['sources'][2]['execution_control']=dict(automatic_enabled=True,reason='r',decision_ref='d',updated_at=NOW.isoformat(),entries={})
        row=self.rows(d)['daily-companion']
        self.assertEqual((row['color'],row['status']),('gray','未运行 · 未获许可'))
        self.assertIn('执行登记缺 job:aki-19e4fb47ffb',row['reason'])
    def test_migration_keeps_the_old_nodes_failures_as_history(self):
        moved=job();old=job(enabled=False);failed=deepcopy(old['state']['lastResult'])
        failed.update(runId='tcloud-run',status='error',deliveryStatus='failed',scheduledFor='2026-09-17T04:00:00Z')
        old['state']['unresolvedResults']=[failed]
        d=source_document();d['sources'][0]['jobs']=[old];d['sources'][2]['jobs']=[moved]
        row=self.rows(d)['daily-companion']
        self.assertEqual((row['color'],row['status'],row['node']),('green','已送达',ORACLE))
        self.assertIn('tcloud · aki-19e4fb47ffb · 上期欠交 1 轮（09-17 12:00 交付失败）',row['history'])
    def test_definition_switch_keeps_old_definition_results_apart(self):
        j=job();old=deepcopy(j['state']['lastResult']);old.update(runId='old',definitionHash='b'*64,status='error',scheduledFor='2026-09-17T04:00:00Z')
        j['state']['unresolvedResults']=[old]
        d=source_document();d['sources'][2]['jobs']=[j]
        row=self.rows(d)['daily-companion']
        self.assertEqual(row['status'],'已送达');self.assertIn('旧定义未核销 1 轮',row['history'])
        j['state']['lastResult']['definitionHash']='b'*64
        self.assertEqual(self.rows(d)['daily-companion']['status'],'结果待核验')
    def test_rows_stay_scalar_for_the_app(self):
        d=source_document();j=job();j['state']['resultHistoryIncomplete']=True;d['sources'][2]['jobs']=[j]
        for row in self.rows(d).values():
            self.assertTrue(all(v is None or isinstance(v,(str,bool,int,float)) for v in row.values()),row['id'])


class CompleteResultTests(unittest.TestCase):
    """Each stage is judged by the producer receipts its own completion needs."""
    def row(self,jobs,service,site=2):
        d=source_document();d['sources'][site]['jobs']=jobs
        return next(r for r in a.metrics(d,NOW)[0][0]['value'] if r['id']==service)
    def test_oikos_record_and_silent_triage_do_not_settle_the_owed_notice(self):
        daily=job('hestia-19f9254e179',owner='hestia',delivery='none',requires_verify=True)
        daily['state']['lastResult']['verifyStatus']='passed'
        triage=job('hestia-19f9254e40c',owner='hestia',delivery='none',skip_allowed=True,requires_verify=True)
        triage['state']['lastResult'].update(skipped=True,deliveryStatus='skipped',skipSummary='ALERT_GATE_SKIP')
        row=self.row([daily,triage],'oikos-daily',site=1)
        self.assertEqual((row['color'],row['status']),('green','部分已核验'))
        self.assertIn('核验命令本轮通过；应发通知送达没有生产者回执（已知缺口）',row['reason'])
        daily['state']['lastResult'].update(status='error',executionStatus='error',verifyStatus='failed')
        self.assertEqual(self.row([daily,triage],'oikos-daily',site=1)['status'],'核验失败')
    def test_caption_gap_does_not_hide_a_missing_configured_check(self):
        for key in ('requires_verify','requires_artifact'):
            r=self.row([job(caption='text',**{key:True})],'daily-companion')
            self.assertEqual((r['color'],r['status']),('orange','结果待核验'),key)
        j=job(caption='text',requires_verify=True);j['state']['lastResult']['verifyStatus']='passed'
        r=self.row([j],'daily-companion')
        self.assertEqual((r['color'],r['status']),('green','文档已送达'));self.assertIn('核验命令本轮通过',r['reason'])
    def test_document_caption_is_part_of_the_delivery(self):
        def status(**updates):
            r=self.row([job(**updates)],'daily-companion');return r['color'],r['status'],r['reason']
        color,state,reason=status(caption='text')
        self.assertEqual((color,state),('green','文档已送达'));self.assertIn('同段说明文字',reason);self.assertIn('已知缺口',reason)
        self.assertEqual(status(caption='file')[:2],('green','已送达'));self.assertIn('含说明文字',status(caption='file')[2])
        self.assertEqual(status(delivery='telegram_document',caption='text')[:2],('green','已送达'))
        j=job(caption='file');j['state']['lastResult'].update(status='error',deliveryStatus='failed')
        self.assertEqual(self.row([j],'daily-companion')['status'],'交付失败')
    def test_delivery_with_a_configured_verify_needs_its_receipt(self):
        j=job('aki-19dad28dcce',requires_verify=True)
        self.assertEqual(self.row([j],'morning-news')['status'],'结果待核验')
        j['state']['lastResult']['verifyStatus']='passed'
        row=self.row([j],'morning-news')
        self.assertEqual(row['status'],'已送达');self.assertIn('核验命令本轮通过',row['reason'])
    def test_previous_round_failure_is_not_this_rounds_result(self):
        j=job();j['state']['lastResult'].update(status='error',deliveryStatus='failed',scheduledFor='2026-09-17T04:00:00Z')
        j['state']['unresolvedResults']=[deepcopy(j['state']['lastResult'])]
        row=self.row([j],'daily-companion')
        self.assertEqual((row['color'],row['status']),('orange','本轮结果待核验'))
        self.assertIn('上一轮（09-17 12:00 交付失败）',row['reason'])
        self.assertIn('上期欠交 1 轮',row['history'])
        j['state']['lastResult']['scheduledFor']='2026-09-18T04:00:00Z'
        j['state']['unresolvedResults'][0]['scheduledFor']='2026-09-18T04:00:00Z'
        row=self.row([j],'daily-companion')
        self.assertEqual((row['color'],row['history']),('red',''))
    def test_convergent_success_proves_the_present_not_the_truncated_past(self):
        j=job('pulse-four-end-sync',owner='pulse',skip_allowed=True,delivery='none',convergent=True)
        j['state']['lastResult'].update(skipped=True,deliveryStatus='skipped',skipSummary='bigbird-online/PULSE current')
        j['state']['resultHistoryIncomplete']=True
        row=self.row([j],'checkout-follow',site=1)
        self.assertEqual((row['color'],row['status']),('green','已同步'))
        self.assertIn('收敛任务的成功轮重读全部状态，只证明现态，不补证更早欠交',row['history'])
        self.assertTrue(row['attention'])
    def test_reader_names_the_caption_and_convergence(self):
        reader=SiteReaderTests('test_empty_configuration_is_known_empty_not_missing');reader.setUp();self.addCleanup(reader.tmp.cleanup)
        base=dict(enabled=True,owner='aki',schedule=dict(expr='0 12 * * *',tz='Asia/Shanghai'))
        jobs=[dict(base,id='a-file',delivery=dict(mode='feishu_document',captionPath='x.txt',caption='c')),
              dict(base,id='a-text',delivery=dict(mode='feishu_document',caption='c'),convergent=True),
              dict(base,id='a-none',delivery=dict(mode='announce',caption='c'))]
        reader.config.write_text(json.dumps(dict(version=2,jobs=jobs)))
        read={j['id']:j for j in reader.read()['jobs']}
        self.assertEqual((read['a-file']['caption'],read['a-text']['caption']),('file','text'))
        self.assertNotIn('caption',read['a-none'])
        self.assertEqual((read['a-text']['convergent'],read['a-file']['convergent']),(True,False))


class HistoryScopeTests(unittest.TestCase):
    """Unsettled rounds outside this round's judged copy stay visible, wherever they are kept."""
    def failed(self,j,slot='2026-09-18T04:00:00Z'):
        j['state']['lastResult'].update(runId='run-'+slot,status='error',deliveryStatus='failed',scheduledFor=slot)
        j['state']['unresolvedResults']=[deepcopy(j['state']['lastResult'])]
        return j
    def rows(self,d):
        return {r['id']:r for r in a.metrics(d,NOW)[0][0]['value']}
    def test_a_deleted_retired_job_keeps_its_scheduler_history(self):
        reader=SiteReaderTests('test_empty_configuration_is_known_empty_not_missing');reader.setUp();self.addCleanup(reader.tmp.cleanup)
        owed=self.failed(job('aki-19da9ee9105'))['state']['unresolvedResults'][0]
        reader.state.write_text(json.dumps(dict(version=1,jobs={'aki-19da9ee9105':dict(definitionHash=HASH,unresolvedResults=[owed]),'other-deleted':dict(unresolvedResults=[owed])})))
        value=reader.read()
        self.assertEqual([j['id'] for j in value['jobs']],['aki-19da9ee9105'])
        self.assertTrue(value['jobs'][0]['retired_state_only']);self.assertFalse(value['jobs'][0]['enabled'])
        d=source_document();d['sources'][0]['jobs']=value['jobs']
        row=self.rows(d)['morning-news']
        self.assertIn('tcloud · aki-19da9ee9105 · 上期欠交 1 轮（09-18 12:00 交付失败）（已退役任务）',row['history'])
        self.assertIn('交付失败',row['progress']);self.assertTrue(row['attention'])
        self.assertFalse(any(k.startswith('job:') for k in self.rows(d)))
        d['sources'][2].update(status='unavailable',jobs=[])
        self.assertIn('oracle-node-1 来源不可读取：已退役任务aki-19da9ee9105的历史欠交不可得',self.rows(d)['morning-news']['history'])
    def test_an_unjudged_copy_keeps_its_current_round_failure(self):
        d=source_document()
        d['sources'][0]['jobs']=[self.failed(job(enabled=False)),self.failed(job('aki-19da9ee9105',enabled=False))]
        d['sources'][2]['jobs']=[job()]
        rows=self.rows(d)
        companion=rows['daily-companion']
        self.assertEqual((companion['status'],companion['node']),('已送达',ORACLE))
        self.assertIn('tcloud · aki-19e4fb47ffb · 上期欠交 1 轮（09-18 12:00 交付失败）',companion['history'])
        self.assertNotIn('oracle-node-1 · aki-19e4fb47ffb',companion['history']);self.assertTrue(companion['attention'])
        self.assertIn('tcloud · aki-19da9ee9105 · 上期欠交 1 轮',rows['morning-news']['history'])
        # The judged copy's own current failure is this round, not history.
        d['sources'][2]['jobs']=[self.failed(job())]
        companion=self.rows(d)['daily-companion']
        self.assertEqual(companion['color'],'red');self.assertNotIn('oracle-node-1',companion['history'])
    def test_a_failure_the_row_does_not_show_stays_in_history(self):
        def companion(*copies):
            d=source_document()
            for site,j in copies:d['sources'][site]['jobs'].append(j)
            return self.rows(d)['daily-companion']
        for name,updates in (('unverified',dict(definition_verified=False)),('running',dict(current_run_live=True))):
            row=companion((2,self.failed(job(**updates))))
            self.assertNotEqual(row['color'],'red',name)
            self.assertIn('上期欠交 1 轮（09-18 12:00 交付失败）',row['history'],name);self.assertTrue(row['attention'],name)
        row=companion((0,self.failed(job())),(2,self.failed(job())))
        self.assertEqual(row['status'],'运行归属冲突')
        self.assertIn('tcloud · aki-19e4fb47ffb · 上期欠交',row['history']);self.assertIn('oracle-node-1 · aki-19e4fb47ffb · 上期欠交',row['history'])
        row=companion((2,self.failed(job(owner='amanda'))))
        self.assertEqual(row['status'],'运行归属冲突');self.assertIn('上期欠交 1 轮',row['history'])
        # A missing node still shows the readable copy's failure, so it is this round.
        d=source_document();d['sources'][2]['jobs']=[self.failed(job())];d['sources'][0].update(status='unavailable')
        row=self.rows(d)['daily-companion']
        self.assertEqual((row['color'],row['history']),('red',''))
        self.assertIn('tcloud 来源不可读取：已退役任务',self.rows(d)['morning-news']['history'])
    def test_folded_members_keep_history_unless_their_failure_is_shown(self):
        def inbox(**updates):
            j=self.failed(inbox_job('aki-collab-inbox','aki','2026-09-18T04:20:00Z','2026-09-18T04:21:00Z',**updates),'2026-09-18T04:20:00Z')
            d=source_document();d['sources'][0]['jobs']=[j]
            return self.rows(d)['collab-inbox']
        shown=inbox()
        self.assertEqual((shown['color'],shown['history']),('red',''))
        for updates in (dict(definition_verified=False),dict(enabled=False)):
            row=inbox(**updates)
            self.assertIn('Aki · tcloud · aki-collab-inbox · 上期欠交 1 轮',row['progress'],updates);self.assertTrue(row['attention'])
    def test_unpermitted_withdrawn_and_stopped_jobs_keep_their_history(self):
        def rows(entries,**updates):
            j=self.failed(job('hestia-heartbeat',owner='hestia',delivery='none',**updates))
            d=source_document();d['sources'][0]['jobs']=[j]
            d['sources'][0]['execution_control']=dict(automatic_enabled=True,reason='r',decision_ref='d',updated_at=NOW.isoformat(),entries=entries)
            return self.rows(d)
        for name,entries in (('missing',{}),('withdrawn',{'job:hestia-heartbeat':dict(owner='hestia',enabled=False)})):
            row=rows(entries)['job:tcloud:hestia-heartbeat']
            self.assertEqual((row['color'],row['status']),('gray','未运行 · 未获许可'),name)
            self.assertIn('tcloud · hestia-heartbeat · 上期欠交 1 轮（09-18 12:00 交付失败）',row['progress'],name)
            self.assertTrue(row['attention'],name);self.assertIn('不计入本期状态',row['note'],name)
        row=rows({})['job:tcloud:hestia-heartbeat']
        incomplete=job('hestia-heartbeat',owner='hestia',delivery='none');incomplete['state']['resultHistoryIncomplete']=True
        d=source_document();d['sources'][0]['jobs']=[incomplete]
        d['sources'][0]['execution_control']=dict(automatic_enabled=True,reason='r',decision_ref='d',updated_at=NOW.isoformat(),entries={})
        self.assertIn('历史摘要不完整',self.rows(d)['job:tcloud:hestia-heartbeat']['history'])
        stopped=rows({},enabled=False)['job:tcloud:hestia-heartbeat']
        self.assertEqual((stopped['status'],stopped['attention']),('未运行 · 已停用',True));self.assertIn('上期欠交 1 轮',stopped['progress'])
        permitted=rows({'job:hestia-heartbeat':dict(owner='hestia',enabled=True)})['job:tcloud:hestia-heartbeat']
        self.assertEqual((permitted['color'],permitted['history']),('red',''))
    def test_folded_and_uncatalogued_rows_show_history_on_expanding(self):
        inbox=inbox_job('aki-collab-inbox','aki','2026-09-18T04:20:00Z','2026-09-18T04:21:00Z')
        prior=deepcopy(inbox['state']['lastResult'])
        prior.update(runId='prior',status='error',skipped=False,deliveryStatus='failed',scheduledFor='2026-09-18T04:00:00Z',
                     startedAt='2026-09-18T04:00:01Z',finishedAt='2026-09-18T04:01:00Z')
        inbox['state']['unresolvedResults']=[prior]
        other=job('hestia-heartbeat',owner='hestia',delivery='none');other['state']['unresolvedResults']=[deepcopy(prior)]
        d=source_document();d['sources'][0]['jobs']=[inbox,other]
        d['sources'][0]['execution_control']=dict(automatic_enabled=True,reason='r',decision_ref='d',updated_at=NOW.isoformat(),
            entries={'job:aki-collab-inbox':dict(owner='aki',enabled=True),'job:hestia-heartbeat':dict(owner='hestia',enabled=True)})
        rows=self.rows(d)
        row=rows['collab-inbox']
        self.assertEqual((row['color'],row['status']),('green','12:21 全部按时'))
        self.assertIn('Aki · tcloud · aki-collab-inbox · 上期欠交 1 轮（09-18 12:00 交付失败）',row['progress'])
        self.assertTrue(row['attention'])
        self.assertIn('tcloud · hestia-heartbeat · 上期欠交 1 轮（09-18 12:00 交付失败）',rows['job:tcloud:hestia-heartbeat']['progress'])


def inbox_job(identifier, owner, started, finished, **updates):
    result=job(identifier,owner=owner,delivery='none',skip_allowed=True,schedule=dict(expr='*/20 * * * *',tz='Asia/Shanghai'),**updates)
    result['state']['lastResult'].update(startedAt=started,finishedAt=finished,scheduledFor='2026-09-18T04:20:00Z',
                                         deliveryStatus='skipped',skipped=True)
    return result


class CollaborationInboxTests(unittest.TestCase):
    AGENTS=('aki','amanda','hestia','pulse')
    def setUp(self):
        self.document=source_document()
        self.assertEqual(self.document['sources'][1]['site'],'ownermac')
        self.document['sources'][1]['execution_control']=dict(automatic_enabled=True,reason='逐项登记',
            decision_ref='WI422',updated_at=NOW.isoformat(),
            entries={f'job:{agent}-collab-inbox':dict(owner=agent,enabled=True) for agent in self.AGENTS})
        self.document['sources'][1]['jobs']=[inbox_job(f'{agent}-collab-inbox',agent,'2026-09-18T04:20:00Z',
            f'2026-09-18T04:2{i}:00Z') for i,agent in enumerate(self.AGENTS)]
    def project(self, now=NOW):
        rows=a.metrics(self.document,now)[0][0]['value']
        return next(r for r in rows if r['id']=='collab-inbox'),rows
    def test_every_inbox_job_folds_into_one_row_and_never_into_new_task_rows(self):
        row,rows=self.project()
        self.assertEqual(row['title'],'协作收件箱巡检 · 4 个 Agent')
        self.assertEqual((row['color'],row['status'],row['attention']),('green','12:23 全部按时',False))
        self.assertEqual(row['progress'],'没有未按时或失败的 Agent。')
        self.assertFalse(any('collab-inbox' in r['id'] and r['id']!='collab-inbox' for r in rows))
        self.assertFalse(any(r['title'].startswith('新增任务') for r in rows))
    def test_detail_names_only_late_or_failed_agents(self):
        jobs=self.document['sources'][1]['jobs']
        jobs[0]['state']['lastResult'].update(status='error',executionStatus='error')
        jobs[1]['state']['lastResult']['scheduledFor']='2026-09-18T03:00:00Z'
        jobs[2]['current_run_live']=True
        row,_=self.project()
        self.assertEqual((row['color'],row['status'],row['attention']),('red','1 个 Agent 有失败，1 个待核验',True))
        self.assertEqual(row['progress'],'Aki · ownermac · 执行失败\nAmanda · ownermac · 本轮结果待核验')
        self.assertNotIn('Hestia',row['progress']);self.assertNotIn('Pulse',row['progress'])
        jobs[0]['state']['lastResult'].update(status='ok',executionStatus='ok')
        row,_=self.project()
        self.assertEqual((row['color'],row['status']),('orange','Amanda 本轮结果待核验'))
    def test_unverified_evidence_is_never_summarized_as_late_or_failed(self):
        jobs=self.document['sources'][1]['jobs']
        for j in jobs:
            j.update(definition_verified=False,definition_reason='缺少定义校验依赖：工具链登记的 Node 不可用。')
        row,_=self.project()
        self.assertEqual((row['color'],row['status']),('orange','4 个 Agent 配置待核验'))
        self.assertNotIn('未按时',row['reason']);self.assertNotIn('失败',row['reason'])
        self.assertIn('配置待核验',row['reason']);self.assertIn('缺少定义校验依赖',row['reason'])
        self.assertEqual(row['progress'].count('配置待核验'),4)
    def test_mixed_problems_are_named_separately_and_failure_only_where_proven(self):
        jobs=self.document['sources'][1]['jobs']
        jobs[0]['state']['lastResult'].update(status='error',executionStatus='error')
        jobs[1]['definition_verified']=False
        jobs[2]['definition_verified']=False
        jobs[3]['current_run_live']=True
        row,_=self.project()
        self.assertEqual((row['color'],row['status']),('red','1 个 Agent 有失败，2 个待核验'))
        self.assertEqual(row['reason'],'1 个 Agent 有失败证据，2 个证据待核验：执行失败 1 个；配置待核验 2 个。')
        self.assertNotIn('Pulse',row['progress'])
        jobs[0]['state']['lastResult'].update(status='ok',executionStatus='ok')
        jobs[1]['state']['lastResult']['scheduledFor']='2026-09-18T03:00:00Z';jobs[1]['definition_verified']=True
        row,_=self.project()
        self.assertEqual((row['color'],row['status']),('orange','2 个 Agent 待核验'))
        self.assertEqual(row['reason'],'2 个 Agent 证据待核验，尚无失败证据：本轮结果待核验 1 个；配置待核验 1 个。')
        self.document['sources'][0]['status']='unavailable'
        row,_=self.project()
        self.assertEqual((row['color'],row['status']),('orange','来源待核验'))
        self.assertIn('尚无失败证据',row['reason']);self.assertIn('tcloud 来源不可读取',row['reason'])
    def test_thirteen_agents_report_failed_and_unverified_counts_separately(self):
        agents = [f'agent{i}' for i in range(13)]
        site = self.document['sources'][1]
        site.pop('execution_control')
        site['jobs'] = [inbox_job(f'{owner}-collab-inbox', owner,
            '2026-09-18T04:20:00Z', '2026-09-18T04:21:00Z', definition_verified=False)
            for owner in agents]
        row, _ = self.project()
        self.assertEqual((row['color'], row['status']), ('orange', '13 个 Agent 配置待核验'))
        first = site['jobs'][0]
        first['definition_verified'] = True
        first['state']['lastResult'].update(status='error', executionStatus='error')
        row, _ = self.project()
        self.assertEqual(row['status'], '1 个 Agent 有失败，12 个待核验')
        self.assertIn('1 个 Agent 有失败证据，12 个证据待核验', row['reason'])
        self.assertIn('配置待核验 12 个', row['reason'])
        self.assertEqual(row['progress'].count('配置待核验'), 12)
        self.assertEqual(row['progress'].count('执行失败'), 1)
        for j in site['jobs']:
            j['definition_verified'] = True
            j['state']['lastResult'].update(status='error', executionStatus='error')
        row, _ = self.project()
        self.assertEqual((row['color'], row['status']), ('red', '13 个 Agent 执行失败'))
        # Distinct proven failure types still count only the failed Agents.
        first['state']['lastResult']['status'] = 'timeout'
        row, _ = self.project()
        self.assertEqual(row['status'], '13 个 Agent 有失败')
        for j in site['jobs']:
            j['current_run_live'] = True
        row, _ = self.project()
        self.assertFalse(row['attention'])
        self.assertNotIn('失败', row['status'])
        self.assertNotIn('待核验', row['status'])

    def test_paused_conflicting_and_unreadable_sources_are_not_all_on_time(self):
        self.document['sources'][1]['execution_control']['entries'].pop('job:pulse-collab-inbox')
        row,_=self.project()
        self.assertEqual(row['title'],'协作收件箱巡检 · 3 个 Agent')
        self.assertEqual(row['progress'],'未启用：Pulse')
        self.document['sources'][1]['jobs'][0]['owner']='pulse'
        row,_=self.project()
        self.assertEqual(row['status'],'Aki 运行归属冲突')
        self.setUp();self.document['sources'][0]['status']='unavailable'
        row,_=self.project()
        self.assertEqual((row['color'],row['status']),('orange','来源待核验'))


class ContractDeliveryTests(unittest.TestCase):
    """Deliver-only contract gates on a conversation-only node fold into one row."""
    AGENTS=('pulse','steward-vultr')
    def setUp(self):
        self.document=source_document()
        oracle=self.document['sources'][2]
        oracle['execution_control']=dict(automatic_enabled=True,reason='逐项登记',decision_ref='WI422',updated_at=NOW.isoformat(),
            entries={f'job:{agent}-collab-deliver':dict(owner=agent,enabled=True) for agent in self.AGENTS})
        oracle['jobs']=[self.deliver(agent) for agent in self.AGENTS]
    def deliver(self,agent,owner=None):
        value=job(f'{agent}-collab-deliver',owner=owner or agent,delivery='none',skip_allowed=True,
                  schedule=dict(expr='0-59/10 * * * *',tz='Asia/Shanghai'))
        value['state']['lastResult'].update(startedAt='2026-09-18T04:30:01Z',finishedAt='2026-09-18T04:30:05Z',
            scheduledFor='2026-09-18T04:30:00Z',deliveryStatus='skipped',skipped=True)
        return value
    def rows(self):
        return {r['id']:r for r in a.metrics(self.document,NOW+timedelta(seconds=30))[0][0]['value']}
    def test_every_agents_deliver_gate_folds_into_one_row(self):
        rows=self.rows()
        row=rows[a.DELIVER_ID]
        self.assertEqual(row['title'],'协作合同投递 · 2 个 Agent')
        self.assertEqual((row['color'],row['status']),('green','12:30 全部按时'))
        self.assertEqual(row['node'],ORACLE)
        self.assertFalse(any('collab-deliver' in key and key!=a.DELIVER_ID for key in rows))
    def test_a_gate_not_named_for_its_owner_keeps_its_own_row(self):
        self.document['sources'][1]['execution_control']=dict(automatic_enabled=True,reason='r',decision_ref='d',
            updated_at=NOW.isoformat(),entries={'job:tcloud-pulse-collab-deliver':dict(owner='pulse',enabled=True)})
        self.document['sources'][1]['jobs']=[self.deliver('tcloud-pulse',owner='pulse')]
        rows=self.rows()
        self.assertIn('job:ownermac:tcloud-pulse-collab-deliver',rows)
        self.assertEqual(rows[a.DELIVER_ID]['title'],'协作合同投递 · 2 个 Agent')
    def test_without_deliver_gates_there_is_no_row(self):
        self.document['sources'][2]['jobs']=[]
        self.assertNotIn(a.DELIVER_ID,self.rows())
    def assert_unpermitted_gate_keeps_its_notice(self):
        rows=self.rows()
        notice=rows['job:'+ORACLE+':steward-vultr-collab-deliver']
        self.assertEqual((notice['title'],notice['status'],notice['reason']),
            ('新增任务 · steward-vultr-collab-deliver','未运行 · 未获许可','任务已启用，但未登记执行许可；调度器到点拦下，不会执行。'))
        return rows
    def test_a_gate_without_permission_keeps_the_notice(self):
        del self.document['sources'][2]['execution_control']['entries']['job:steward-vultr-collab-deliver']
        rows=self.assert_unpermitted_gate_keeps_its_notice()
        self.assertEqual(rows[a.DELIVER_ID]['title'],'协作合同投递 · 1 个 Agent')
    def test_a_disabled_permission_keeps_the_notice(self):
        self.document['sources'][2]['execution_control']['entries']['job:steward-vultr-collab-deliver']['enabled']=False
        self.assert_unpermitted_gate_keeps_its_notice()
    def test_a_global_pause_folds_no_gate(self):
        self.document['sources'][2]['execution_control']['automatic_enabled']=False
        rows=self.rows()
        self.assertNotIn(a.DELIVER_ID,rows)
        self.assertIn('job:'+ORACLE+':pulse-collab-deliver',rows)


class ReviewDispatchTests(unittest.TestCase):
    """Permitted PR review dispatch jobs fold into one row judged by each job's own results."""
    IDS=('pulse-pr-review-codex','pulse-pr-review-cc')
    LATER=NOW+timedelta(seconds=40)
    def setUp(self):
        self.document=source_document()
        self.document['sources'][1]['execution_control']=dict(automatic_enabled=True,reason='逐项登记',
            decision_ref='WI422',updated_at=NOW.isoformat(),
            entries={f'job:{i}':dict(owner='pulse',enabled=True) for i in self.IDS})
        self.document['sources'][1]['jobs']=[self.review(i,'pulse') for i in self.IDS]
        self.jobs=self.document['sources'][1]['jobs']
    def review(self,ident,owner):
        value=job(ident,owner=owner,delivery='none',skip_allowed=True,schedule=dict(expr='* * * * *',tz='Asia/Shanghai'))
        value['state']['lastResult'].update(startedAt='2026-09-18T04:30:01Z',finishedAt='2026-09-18T04:30:31Z',
            scheduledFor='2026-09-18T04:30:00Z',deliveryStatus='skipped',skipped=True,skipSummary='PR_REVIEW_SKIP')
        return value
    def rows(self):
        return {r['reference'] if r['reference'].startswith('job:') else r['id']:r
                for r in a.metrics(self.document,self.LATER)[0][0]['value']}
    def refused(self,j):
        j['state']['lastResult'].update(skipped=True,reasonCode='automatic_paused',skipSummary=None)
        j['state']['lastResult'].pop('executionStatus',None)
    def test_rounds_the_gate_refused_are_not_folded_into_on_time(self):
        for j in self.jobs: self.refused(j)
        row=self.rows()[a.REVIEW_ID]
        self.assertEqual((row['color'],row['status'],row['attention']),('gray','2 项未运行 · 未获许可',True))
        self.jobs[1]['state']['lastResult'].update(skipped=True,reasonCode=None,skipSummary='PR_REVIEW_SKIP',executionStatus='ok')
        row=self.rows()[a.REVIEW_ID]
        self.assertEqual((row['color'],row['attention']),('gray',True));self.assertIn('未运行',row['status'])
        self.assertNotIn('全部按时',row['status'])
    def test_every_review_job_folds_into_one_quiet_row(self):
        rows=self.rows()
        row=rows[a.REVIEW_ID]
        self.assertEqual(row['title'],'PR 异构审核派发 · 1 个 Agent')
        self.assertEqual((row['color'],row['status'],row['attention']),('green','12:30 全部按时',False))
        self.assertEqual(row['progress'],'没有未按时或失败的任务。')
        self.assertIn('不代表任何 PR 已审核通过',row['reason'])
        self.assertIn('不等于代码审核已完成',row['expectation'])
        self.assertEqual(row['next_at'],'2026-09-18T04:31:00+00:00')
        self.assertFalse(any('pr-review' in key and key!=a.REVIEW_ID for key in rows))
    def test_detail_names_only_failed_or_unverified_review_jobs(self):
        self.jobs[0]['state']['lastResult'].update(status='error',executionStatus='error',skipped=False,skipSummary=None)
        self.jobs[1]['current_run_live']=True
        row=self.rows()[a.REVIEW_ID]
        self.assertEqual((row['color'],row['status'],row['attention']),('red','Pulse（codex） 执行失败',True))
        self.assertEqual(row['progress'],'Pulse（codex） · ownermac · 执行失败')
        self.jobs[0]['state']['lastResult'].update(status='ok',executionStatus='ok',skipped=True,skipSummary='PR_REVIEW_SKIP')
        self.jobs[1]['current_run_live']=False;self.jobs[1]['definition_verified']=False
        row=self.rows()[a.REVIEW_ID]
        self.assertEqual((row['color'],row['status']),('orange','Pulse（cc） 配置待核验'))
    def test_a_dispatched_round_is_not_a_completed_code_review(self):
        self.jobs[0]['state']['lastResult'].update(skipped=False,skipSummary=None)
        row=self.rows()[a.REVIEW_ID]
        self.assertEqual(row['color'],'green');self.assertIn('全部按时',row['status'])
        self.assertIn('派发成功不等于代码审核已完成',row['expectation'])
        self.assertNotIn('审核已完成',row['status']+row['reason'])
    def test_every_agents_review_pair_counts_once_per_agent(self):
        control=self.document['sources'][1]['execution_control']['entries']
        for owner,ident in (('hestia','hestia-pr-review-cc'),('hestia','hestia-pr-review-codex'),('pulse','vigil-pr-review-cc')):
            control['job:'+ident]=dict(owner=owner,enabled=True)
            self.jobs.append(self.review(ident,owner))
        rows=self.rows()
        self.assertEqual(rows[a.REVIEW_ID]['title'],'PR 异构审核派发 · 2 个 Agent')
        # A review job owned by another Agent is not that Agent's review pair; it stays its own row.
        self.assertEqual(rows['job:vigil-pr-review-cc']['title'],'vigil-pr-review-cc')
        self.assertNotIn('vigil-pr-review-cc',rows[a.REVIEW_ID]['reference'])
    def test_unpermitted_review_jobs_keep_the_permission_notice(self):
        self.document['sources'][1]['execution_control']['entries']['job:pulse-pr-review-cc']['enabled']=False
        self.jobs.append(job('pulse-unregistered-entry',owner='pulse'))
        rows=self.rows()
        self.assertEqual(rows[a.REVIEW_ID]['reference'],'pulse-pr-review-codex')
        for reference in ('job:pulse-pr-review-cc','job:pulse-unregistered-entry'):
            self.assertTrue(rows[reference]['title'].startswith('新增任务 · '))
            self.assertEqual(rows[reference]['reason'],'任务已启用，但未登记执行许可；调度器到点拦下，不会执行。')
            self.assertEqual((rows[reference]['color'],rows[reference]['status']),('gray','未运行 · 未获许可'))
        self.document['sources'][1]['execution_control']['entries']['job:pulse-pr-review-codex']['enabled']=False
        self.assertNotIn(a.REVIEW_ID,self.rows())


class UncataloguedJobTests(unittest.TestCase):
    """A permitted job without a service row shows its own name; a disabled one is omitted."""
    def setUp(self):
        self.document=source_document()
        self.document['sources'][1]['execution_control']=dict(automatic_enabled=True,reason='逐项登记',
            decision_ref='WI422',updated_at=NOW.isoformat(),
            entries={'job:hestia-heartbeat':dict(owner='hestia',enabled=True,decision_ref='WI418')})
        self.document['sources'][1]['jobs']=[job('hestia-heartbeat',owner='hestia',name='Hestia 心跳',delivery='none'),
                                             job('hestia-retired',owner='hestia',enabled=False)]
    def rows(self):
        return {r['reference']:r for r in a.metrics(self.document,NOW)[0][0]['value'] if r['reference'].startswith('job:')}
    def test_permitted_job_is_named_and_judged_by_its_own_result(self):
        row=self.rows()['job:hestia-heartbeat']
        self.assertEqual((row['title'],row['owner'],row['decision_ref']),('Hestia 心跳','hestia','WI418'))
        self.assertEqual((row['color'],row['status']),('green','已执行 · 未配结果检查'))
        self.assertNotIn('新增',row['title']);self.assertNotIn('未登记执行许可',row['reason'])
        self.document['sources'][1]['jobs'][0].pop('name')
        self.assertEqual(self.rows()['job:hestia-heartbeat']['title'],'hestia-heartbeat')
    def test_disabled_job_without_a_service_row_is_omitted(self):
        self.assertNotIn('job:hestia-retired',self.rows())


class SiteReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.root=self.home/'.local/share/pulse'
        (self.root/'config/relay').mkdir(parents=True);(self.root/'data/relay').mkdir(parents=True)
        self.config=self.root/'config/relay/cron-jobs.json';self.state=self.root/'data/relay/cron-jobs-state.json'
        self.map=self.root/'config/relay/paths.json'
        self.map.write_text(json.dumps(dict(schema='pulse_relay_paths/v1',site='tcloud',root=str(self.root),paths=dict(cronConfig=str(self.config),cronState=str(self.state)))))
        self.config.write_text(json.dumps(dict(version=2,jobs=[])))
        self.state.write_text(json.dumps(dict(version=1,jobs={})))
    def read(self):return read_site(self.home,'tcloud',NOW.isoformat())
    def test_empty_configuration_is_known_empty_not_missing(self):
        self.assertEqual(self.read()['status'],'available');self.assertEqual(self.read()['jobs'],[])
        self.config.unlink();self.assertEqual(self.read()['status'],'unavailable')
    def test_inventory_never_exports_prompt_or_secrets(self):
        private=dict(id='not-a-selected-job',enabled=True,owner='hestia',schedule=dict(expr='*/5 * * * *',tz='Asia/Shanghai'),prompt='private-needle')
        selected=dict(id='aki-19e4fb47ffb',enabled=False,owner='aki',schedule=dict(expr='0 12 * * *',tz='Asia/Shanghai'),agent=dict(prompt='private-needle',workspace='/private/workspace'),delivery=dict(mode='none',token='private-needle'))
        self.config.write_text(json.dumps(dict(version=2,jobs=[private,selected])))
        value=self.read();self.assertEqual(value['status'],'available');self.assertEqual(len(value['jobs']),2)
        self.assertNotIn('private-needle',json.dumps(value));self.assertNotIn('/private/workspace',json.dumps(value))
    def test_reader_exports_a_job_name_only_when_one_is_set(self):
        named=dict(id='hestia-heartbeat',enabled=True,owner='hestia',name='Hestia 心跳',schedule=dict(expr='30 7 * * 1',tz='Asia/Shanghai'))
        bare=dict(id='hestia-other',enabled=True,owner='hestia',name=' ',schedule=dict(expr='30 7 * * 1',tz='Asia/Shanghai'))
        self.config.write_text(json.dumps(dict(version=2,jobs=[named,bare])))
        jobs={j['id']:j for j in self.read()['jobs']}
        self.assertEqual(jobs['hestia-heartbeat']['name'],'Hestia 心跳');self.assertNotIn('name',jobs['hestia-other'])
    def test_a_gate_skip_summary_reaches_the_checkout_row_through_the_reader(self):
        job=dict(id='pulse-four-end-sync',enabled=True,owner='pulse',schedule=dict(expr='*/30 * * * *',tz='Asia/Shanghai'))
        self.config.write_text(json.dumps(dict(version=2,jobs=[job])))
        result=dict(runId='r',skipped=True,status='ok',skipSummary='owner/repo blocked (already reported): main diverged')
        self.state.write_text(json.dumps(dict(version=1,jobs={'pulse-four-end-sync':dict(lastResult=result)})))
        read=next(j for j in self.read()['jobs'] if j['id']=='pulse-four-end-sync')
        self.assertEqual(read['state']['lastResult']['skipSummary'],result['skipSummary'])
    def test_reader_names_a_configured_verify_and_keeps_its_receipt(self):
        job=dict(id='aki-19e4fb47ffb',enabled=True,owner='aki',schedule=dict(expr='0 12 * * *',tz='Asia/Shanghai'),
                 agent=dict(verify=dict(command='/fake/record-check',args=['daily'])))
        plain=dict(id='aki-19dad28dcce',enabled=True,owner='aki',schedule=dict(expr='0 12 * * *',tz='Asia/Shanghai'))
        self.config.write_text(json.dumps(dict(version=2,jobs=[job,plain])))
        self.state.write_text(json.dumps(dict(version=1,jobs={'aki-19e4fb47ffb':dict(lastResult=dict(runId='r',status='ok',verifyStatus='passed'))})))
        read={j['id']:j for j in self.read()['jobs']}
        self.assertTrue(read['aki-19e4fb47ffb']['requires_verify']);self.assertFalse(read['aki-19dad28dcce']['requires_verify'])
        self.assertEqual(read['aki-19e4fb47ffb']['state']['lastResult']['verifyStatus'],'passed')
        self.assertNotIn('/fake/record-check',json.dumps(read))
    def test_missing_map_invalid_schema_duplicate_jobs_and_path_escape(self):
        self.map.unlink();self.assertEqual(self.read()['status'],'unavailable')
    def test_undeclared_cron_state_follows_the_schedulers_data_dir(self):
        # Oracle's map declares dataDir only; Relay then keeps state in dataDir/cron-jobs-state.json.
        selected=dict(id='aki-19e4fb47ffb',enabled=False,owner='aki',schedule=dict(expr='0 12 * * *',tz='Asia/Shanghai'))
        self.config.write_text(json.dumps(dict(version=2,jobs=[selected])))
        self.state.write_text(json.dumps(dict(version=1,jobs={'aki-19e4fb47ffb':dict(lastStatus='ok')})))
        self.map.write_text(json.dumps(dict(schema='pulse_relay_paths/v1',site=ORACLE,root=str(self.root),
            paths=dict(cronConfig=str(self.config),dataDir=str(self.root/'data/relay')))))
        value=read_site(self.home,ORACLE,NOW.isoformat())
        self.assertEqual(value['status'],'available')
        self.assertEqual(value['jobs'][0]['state']['lastStatus'],'ok')
        self.map.write_text(json.dumps(dict(schema='pulse_relay_paths/v1',site=ORACLE,root=str(self.root),
            paths=dict(cronConfig=str(self.config),dataDir=str(self.home/'elsewhere')))))
        self.assertEqual(read_site(self.home,ORACLE,NOW.isoformat())['status'],'unavailable')
    def test_symlink_outside_registered_root_is_not_read(self):
        outside=self.home/'outside.json';outside.write_text('{"version":2,"jobs":[]}')
        self.config.unlink();self.config.symlink_to(outside)
        self.assertEqual(self.read()['status'],'unavailable')

def host_node():
    """Any real Node for the test to exercise; the reader itself never searches for one."""
    import platform, pwd
    try:
        template=json.loads(automation_source.TOOLCHAIN_RUNTIMES.read_bytes())['node'][platform.system()]
        declared=template.format(home=pwd.getpwuid(os.getuid()).pw_dir)
    except (AttributeError,OSError,ValueError,KeyError):
        declared=None
    return shutil.which('node') or (declared if declared and os.access(declared,os.X_OK) else None)


class DefinitionNodeTests(unittest.TestCase):
    """The App starts the reader with PATH=/usr/bin:/bin:/usr/sbin:/sbin and no Node on it."""
    APP_PATH='/usr/bin:/bin:/usr/sbin:/sbin'
    JOB=dict(id='aki-19e4fb47ffb',enabled=True,owner='aki',schedule=dict(expr='0 12 * * *',tz='Asia/Shanghai'))
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);root=self.home/'.local/share/pulse'
        (root/'config/relay').mkdir(parents=True);(root/'data/relay').mkdir(parents=True)
        config=root/'config/relay/cron-jobs.json';state=root/'data/relay/cron-jobs-state.json'
        (root/'config/relay/paths.json').write_text(json.dumps(dict(schema='pulse_relay_paths/v1',site='tcloud',root=str(root),paths=dict(cronConfig=str(config),cronState=str(state)))))
        config.write_text(json.dumps(dict(version=2,jobs=[self.JOB])))
        digest=hashlib.sha256(json.dumps(self.JOB,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        state.write_text(json.dumps(dict(version=1,jobs={self.JOB['id']:dict(definitionHash=digest,activatedAt='2026-09-18T00:00:00Z')})))
        self.declaration=self.home/'agent-runtimes.json'
    def declare(self,template):
        import platform
        self.declaration.write_text(json.dumps(dict(node={platform.system():template})))
    def read(self,which=None):
        # Account home stands in for the passwd home `{home}` expands to.
        real=getattr(automation_source,'definition_node',None)
        with patch.dict(os.environ,{'PATH':self.APP_PATH}), \
             patch.object(automation_source,'definition_node',lambda site:real(site,self.declaration,str(self.home)),create=True), \
             patch('shutil.which',return_value=which):
            return read_site(self.home,'tcloud',NOW.isoformat())
    def test_registered_node_verifies_definitions_without_node_on_path(self):
        node=host_node()
        if node is None:self.skipTest('no Node on this host')
        (self.home/'bin').mkdir();(self.home/'bin/node').symlink_to(node)
        self.declare('{home}/bin/node')
        value=self.read()
        self.assertEqual(value['status'],'available')
        self.assertTrue(value['jobs'][0]['definition_verified'])
        self.assertNotIn('definition_reason',value['jobs'][0]);self.assertNotIn('definition_check',value)
    def test_missing_registered_node_stays_unverified_with_a_readable_reason(self):
        node=host_node()
        self.declare('{home}/missing/node')
        # A Node found on some PATH is not the scheduler's declared runtime.
        value=self.read(which=node)
        job=value['jobs'][0]
        self.assertFalse(job['definition_verified'])
        self.assertIn('缺少定义校验依赖',job['definition_reason'])
        self.assertEqual(value['definition_check'],job['definition_reason'])
        d=source_document();d['sources'][0]['jobs']=[dict(job,**{k:v for k,v in globals()['job']().items() if k not in job})]
        row=next(r for r in a.metrics(d,NOW)[0][0]['value'] if r['id']=='daily-companion')
        self.assertEqual((row['color'],row['status']),('orange','配置待核验'))
        self.assertIn('缺少定义校验依赖',row['reason'])
        self.assertIsNone(row['last_result_at'])
    def test_unreadable_declaration_is_never_a_verified_definition(self):
        self.declaration.write_text('not json')
        self.assertFalse(self.read()['jobs'][0]['definition_verified'])


class InstalledControlReadTests(unittest.TestCase):
    def _site(self, home):
        config=home/".local/share/pulse/config/relay"; config.mkdir(parents=True)
        state=home/".local/share/pulse/data/relay"; state.mkdir(parents=True)
        paths={"cronConfig":str(config/"cron-jobs.json"),"cronState":str(state/"cron-state.json")}
        (config/"paths.json").write_text(json.dumps(dict(schema="pulse_relay_paths/v1",site="tcloud",root=str(home/".local/share/pulse"),paths=paths)))
        Path(paths["cronConfig"]).write_text(json.dumps(dict(version=2,jobs=[])))
        Path(paths["cronState"]).write_text(json.dumps(dict(version=1,jobs={})))
        return config/"automation-control.json"

    def test_reads_the_scheduler_control_file_directly(self):
        from pulse_stats.automation_source import read_site
        with tempfile.TemporaryDirectory() as t:
            home=Path(t); path=self._site(home)
            control=dict(schema="pulse_automation_control/v1",automatic_enabled=True,revision=2,reason="r",decision_ref="d",
                         updated_at="2026-10-19T05:00:00+00:00",entries={"job:aki-1":dict(owner="aki",enabled=True)})
            path.write_text(json.dumps(control)); path.chmod(0o600)
            with patch("pulse_stats.automation_source.subprocess.run") as run:
                result=read_site(home,"tcloud",NOW.isoformat())
            self.assertEqual(result["execution_control"],control)
            run.assert_not_called()

    def test_writable_or_malformed_control_is_unavailable(self):
        from pulse_stats.automation_source import read_site
        with tempfile.TemporaryDirectory() as t:
            home=Path(t); path=self._site(home)
            path.write_text(json.dumps(dict(schema="pulse_automation_control/v1",automatic_enabled=True)))
            path.chmod(0o600)
            self.assertEqual(read_site(home,"tcloud",NOW.isoformat())["execution_control"]["status"],"unavailable")
            path.write_text(json.dumps(dict(schema="pulse_automation_control/v1",automatic_enabled=True,revision=1,reason="r",
                                            decision_ref="d",updated_at="2026-10-19T05:00:00+00:00",entries={})))
            path.chmod(0o666)
            self.assertEqual(read_site(home,"tcloud",NOW.isoformat())["execution_control"]["status"],"unavailable")


class GatherTests(unittest.TestCase):
    def test_reads_this_node_locally_and_only_registered_peers(self):
        from pulse_stats import automation_source
        with tempfile.TemporaryDirectory() as t:
            home=Path(t);state=home/'.local/state/pulse/stats';state.mkdir(parents=True)
            peer=dict(id='tcloud',host='tcloud-node2',prefix=['runuser','-u','pulse','--'],python=automation_source.PEER_PYTHON,script='/x')
            (state/'refresh.json').write_text(json.dumps(dict(peers=[peer])))
            local=dict(schema=a.SCHEMA,site='ownermac',status='available',jobs=[])
            remote=dict(schema=a.SCHEMA,site='tcloud',status='available',jobs=[])
            with patch.object(automation_source,'read_site',return_value=local) as site, \
                 patch.object(automation_source,'read_peer',return_value=remote) as peers:
                value=automation_source.gather(home)
            self.assertEqual([s['site'] for s in value['sources']],['ownermac','tcloud',ORACLE])
            self.assertEqual(site.call_args.args[:2],(home,'ownermac'))
            self.assertEqual(peers.call_args.args[:2],(peer,'tcloud'))
    def test_oracle_is_read_over_its_registered_host_identity(self):
        from pulse_stats import automation_source
        with tempfile.TemporaryDirectory() as t:
            home=Path(t);state=home/'.local/state/pulse/stats';state.mkdir(parents=True)
            oracle=dict(id=ORACLE,host='pulse@oracle-node-1',python=automation_source.PEER_PYTHON,script='/x',transport='shared_ledger')
            (state/'refresh.json').write_text(json.dumps(dict(peers=[oracle])))
            report=dict(schema=a.SCHEMA,site=ORACLE,status='available',jobs=[])
            done=type('Done',(),dict(returncode=0,stdout=json.dumps(report)))
            with patch.object(automation_source,'read_site',return_value=dict(site='ownermac')), \
                 patch.object(automation_source.subprocess,'run',return_value=done) as run:
                value=automation_source.gather(home)
            self.assertEqual(value['sources'][2],report)
            self.assertEqual(value['sources'][1]['status'],'unavailable')
            command=run.call_args.args[0]
            self.assertEqual(command[-2],'pulse@oracle-node-1')
            self.assertEqual(command[-1].split()[:2],[automation_source.PEER_PYTHON,automation_source.PEER_SNAPSHOT])
            self.assertEqual(command[-1].split()[-2:],['--site',ORACLE])
            # Each node's registered prefix is exact: TCloud's account switch is not Oracle's.
            run.reset_mock()
            self.assertEqual(automation_source.read_peer(dict(oracle,prefix=['runuser','-u','pulse','--']),ORACLE,NOW.isoformat())['status'],'unavailable')
            run.assert_not_called()
    def test_each_node_declares_its_own_toolchain_checkout(self):
        from pulse_stats import automation_source
        seen=[]
        def read(path):
            seen.append(path);raise OSError('absent')
        with patch.object(Path,'read_bytes',autospec=True,side_effect=read):
            for site in automation_source.SITES:
                self.assertIsNone(automation_source.definition_node(site))
        self.assertEqual([str(p) for p in seen],['/Users/owner/Coevolution/ssot/toolchain/agent-runtimes.json',
            '/Users/owner/Coevolution/ssot/toolchain/agent-runtimes.json','/home/pulse/Coevolution/ssot/toolchain/agent-runtimes.json'])
    def test_unregistered_peer_is_unavailable_without_a_connection(self):
        from pulse_stats import automation_source
        with tempfile.TemporaryDirectory() as t:
            home=Path(t);state=home/'.local/state/pulse/stats';state.mkdir(parents=True)
            (state/'refresh.json').write_text(json.dumps(dict(peers=[])))
            with patch.object(automation_source,'read_site',return_value=dict(site='ownermac')), \
                 patch.object(automation_source.subprocess,'run') as run:
                value=automation_source.gather(home)
            run.assert_not_called()
            self.assertEqual(value['sources'][1]['status'],'unavailable')


class LayoutTests(unittest.TestCase):
    """Purpose groups and the two-line plan are reading aids; they never change a row's result."""
    def setUp(self):
        self.document=source_document()
        self.document['sources'][1]['execution_control']=dict(automatic_enabled=True,reason='逐项登记',
            decision_ref='WI422',updated_at=NOW.isoformat(),
            entries={'job:hypnos':dict(owner='amanda',enabled=True),'job:ken-heartbeat':dict(owner='ken',enabled=True),
                     'job:pulse-collab-inbox':dict(owner='pulse',enabled=True)})
        self.document['sources'][1]['jobs']=[
            job('hypnos',owner='amanda',delivery='none',schedule=dict(expr='0 1 * * *',tz='Asia/Shanghai')),
            job('ken-heartbeat',owner='ken',delivery='none',schedule=dict(expr='30 9 * * 1',tz='Asia/Shanghai')),
            job('aki-podcast-weekly-collect',owner='aki',schedule=dict(expr='0 22 * * 5',tz='Asia/Shanghai')),
            inbox_job('pulse-collab-inbox','pulse','2026-09-18T04:20:00Z','2026-09-18T04:21:00Z')]
    def rows(self):
        return {r['id']:r for r in a.metrics(self.document,NOW)[0][0]['value']}
    def test_shipped_groups_are_consistent_and_each_difference_is_named(self):
        from pulse_stats import automation_layout as layout
        cfg=layout.groups()
        self.assertEqual(layout.check_groups(cfg,catalog()),[])
        self.assertEqual([g['title'] for g in cfg['groups']],['资讯交付','巡检与异常处理','睡眠与整理','报告与复盘','同步与协作保障'])
        bad=deepcopy(cfg)
        bad['groups'][1]['rows'].append('morning-news');bad['groups'][2]['rows'].append('no-such-row')
        bad['groups'][3]['jobs'].append('(');bad['groups'].append(deepcopy(bad['groups'][0]))
        diffs=layout.check_groups(bad,catalog())
        self.assertIn('morning-news 同时归入用途 content 与 patrol',diffs)
        self.assertIn('用途 metabolism 的 no-such-row 不在服务目录',diffs)
        self.assertIn('用途 reports 的任务规则无效：(',diffs)
        self.assertIn('用途 id 缺失或重复：content',diffs)
    def test_each_row_has_one_group_and_unknown_purpose_stays_visible(self):
        rows=self.rows()
        got={k:(r['category'],r['category_title']) for k,r in rows.items()}
        self.assertEqual(got['morning-news'],('content','资讯交付'))
        self.assertEqual(got['collab-inbox'],('patrol','巡检与异常处理'))
        self.assertEqual(got['job:ownermac:hypnos'],('metabolism','睡眠与整理'))
        self.assertEqual(got['s3-weekly'],('reports','报告与复盘'))
        self.assertEqual(got['checkout-follow'],('coordination','同步与协作保障'))
        self.assertEqual(got['job:ownermac:ken-heartbeat'],('ungrouped','用途待确认'))
        self.assertEqual(rows['job:ownermac:ken-heartbeat']['category_rank'],5)
        # Grouping does not lift a permission gap: the podcast stays gray in its own purpose.
        podcast=rows['job:ownermac:aki-podcast-weekly-collect']
        self.assertEqual((podcast['category'],podcast['color'],podcast['status']),('content','gray','未运行 · 未获许可'))
        self.assertTrue(all(isinstance(v,(str,int,float,bool)) or v is None for r in rows.values() for v in r.values()))
    def test_s1_weekly_reads_the_registered_job_and_ken_wakes_are_content(self):
        site=self.document['sources'][1]
        site['execution_control']['entries'].update({f'job:{j}':dict(owner=o,enabled=True)
            for j,o in (('amanda-s1-weekly','amanda'),('ken-wake-morning','ken'),('ken-wake-publish','ken'))})
        old=job('amanda-19dc81d7811',owner='amanda',enabled=False,retired_state_only=True)
        old['state']['lastResult'].update(status='error',deliveryStatus='failed')
        old['state']['unresolvedResults']=[deepcopy(old['state']['lastResult'])]
        pending=dict(state=dict(definitionHash=HASH,activatedAt='2026-09-18T00:00:00Z'),delivery='none')
        site['jobs']+=[old,job('amanda-s1-weekly',owner='amanda',schedule=dict(expr='0 18 * * 0',tz='Asia/Shanghai'),**pending),
            job('ken-wake-morning',owner='ken',schedule=dict(expr='0 10 * * *',tz='Asia/Shanghai'),**pending),
            job('ken-wake-publish',owner='ken',schedule=dict(expr='45 11 * * *',tz='Asia/Shanghai'),**pending)]
        rows=self.rows()
        weekly=rows['s1-weekly']
        self.assertEqual((weekly['reference'],weekly['node'],weekly['category']),('amanda-s1-weekly','ownermac','reports'))
        self.assertEqual((weekly['plan_head'],weekly['plan_sub'],weekly['mapping']),('每周日','18:00',''))
        # The replaced definition's failure stays in history; it is neither a row nor settled.
        self.assertIn('ownermac · amanda-19dc81d7811 · 上期欠交 1 轮',weekly['history'])
        self.assertIn('（已退役任务）',weekly['history'])
        self.assertFalse(any(k.endswith((':amanda-s1-weekly',':amanda-19dc81d7811')) for k in rows))
        for name in ('ken-wake-morning','ken-wake-publish'):
            self.assertEqual(rows['job:ownermac:'+name]['category'],'content')
    def test_plan_has_two_lines_from_the_actual_schedule(self):
        from pulse_stats.automation_layout import describe, plan_lines
        cases={'15 6 * * *':('每日','06:15'),'0 22 * * 5':('每周五','22:00'),'30 8 * 11,12 1':('每周一','08:30 · 11、12月'),
               '17,47 * * * *':('每小时',':17 / :47'),'*/30 * * * *':('每30分',''),'1-59/10 * * * *':('每10分',':01 起'),
               '0 9 1 * *':('0 9 1 * *','')}
        for expr,lines in cases.items():
            self.assertEqual(describe(dict(expr=expr,tz='Asia/Shanghai')),lines)
        self.assertEqual(describe(dict(expr='0 12 * * *',tz='UTC')),('每日','12:00 (UTC)'))
        self.assertEqual(plan_lines([dict(expr='0 3 * * *',tz='Asia/Shanghai'),dict(expr='*/30 * * * *',tz='Asia/Shanghai')],('',''))
                         ,dict(plan_head='每日 03:00',plan_sub='每30分'))
        rows=self.rows()
        self.assertEqual((rows['job:ownermac:hypnos']['plan_head'],rows['job:ownermac:hypnos']['plan_sub']),('每日','01:00'))
        self.assertEqual((rows['s1-daily']['plan_head'],rows['s1-daily']['plan_sub']),('计划未定','未部署 · 暂停'))
        self.assertEqual((rows['collab-inbox']['plan_head'],rows['collab-inbox']['plan_sub']),('每20分',''))
        self.document['sources'].pop()
        self.document['sources'].append(dict(self.document['sources'][0],site='oracle-node-1',status='unavailable'))
        self.assertEqual((self.rows()['s1-daily']['plan_head'],self.rows()['s1-daily']['plan_sub']),('计划未知','来源待核验'))


if __name__=='__main__':unittest.main()
