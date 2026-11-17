"""Verify installed Stats against committed publications and original queries.

A generated file is not a completed publication. Wait for the publisher's
successful cycle receipt before refreshing the actual App.
Never collect sources, edit ledgers, mint quota or clear incidents here.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'ComputeCost'),str(ROOT/'Stats/exporter')]
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, digest, digest_sequence
from pulse_compute_cost.collaboration_query import CollaborationQuery, CollaborationQuerySession
from pulse_stats.cost_dashboard import expand_transport_cost_dashboard, load_cost_dashboard, usage_metrics
from pulse_stats.codex_quota import load_quota
from pulse_stats.cc_quota import load_quota as load_cc_quota


def publisher_ledger(registry):
    """The receipt source the installed publisher actually queried.

    A registry that names the installed core publishes from its observation
    stream; local ledger files are then only original inputs.
    """
    if registry.get('observation_core_entrypoint'):
        from pulse_compute_cost.observation_authority import registered_ledger
        return registered_ledger(registry)
    ledger=UsageLedger(registry['ledger_root'])
    with (Path(registry['ledger_root'])/'.usage-ledger.lock').open('rb') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_SH);batches=ledger._receipt_batches()
    class FrozenLedger:
        def receipt_snapshot(self, *, as_of=None, watermark=None):
            indexes=[i for i,b in enumerate(batches) if watermark is None or b['sequence']<=watermark]
            selected=[batches[i] for i in indexes]
            facts=[r for i in indexes for r in ledger._receipt_cache_rows[i]]
            last=selected[-1] if selected else None
            return {'facts':facts,'watermark':last['sequence'] if last else 0,
                    'recorded_at':last['recorded_at'] if last else None,
                    'sha256':digest_sequence(r.to_dict() for r in facts)}
    return FrozenLedger()


def retained_catalog_witness(view, catalog):
    # A cache hit retains the original query witness; a later retrieval of
    # identical content is not permission to relabel its report digest.
    from pulse_compute_cost.receipts import digest
    from pulse_compute_cost.events import parse_timestamp
    observed = view['catalog_observed_at']
    if (view['catalog_sha256'] != digest(catalog['catalog']) or
            parse_timestamp(observed, 'view catalog') > parse_timestamp(catalog['retrieved_at'], 'saved catalog')):
        raise AssertionError('published calculation price witness differs')
    return observed


# Two existing refreshes, one half-hour scheduler gap, then query/restart.
# This verifier budget does not raise the production latency or freshness limit.
DEFAULT_READBACK_TIMEOUT = 55 * 60
PUBLISHER_LABEL = 'io.coevolution.pulse.stats.cost-refresh'
# Natural acceptance looks for one scheduled cycle after the deployment's own
# triggered publication: the publisher interval plus a grace period.
NATURAL_AFTER = 20 * 60
# A waiting readback reports at least this often, so a long cycle shows progress.
WAIT_PROGRESS_SECONDS = 60
NATURAL_DEADLINE = 70 * 60


class Pending(Exception):
    """Natural acceptance has no scheduled publication to judge yet."""


def at(value):return datetime.fromisoformat(value.replace('Z','+00:00'))


def matrix(document):
    return {(v['period'],v['dimension']):(v['watermark'],v['report_sha256'],
            v['summary']['cost'].get('machine_cash_usd'),v['summary']['cost'].get('partial_machine_cash_usd'),
            [(g['key'],g['cost'].get('machine_cash_usd'),g['cost'].get('partial_machine_cash_usd'),g.get('usage')) for g in v['groups']],v['summary'].get('usage'))
            for v in document.get('views',[])}


def restart_installed(revision):
    spec=importlib.util.spec_from_file_location('deployer',ROOT/'Stats/deploy.py')
    deployer=importlib.util.module_from_spec(spec);spec.loader.exec_module(deployer)
    app=Path('/Applications/PulseStats.app');deployer.validate_app(app,revision)
    deployer.stop_installed_app(app)
    deployer.start_installed_app(app)


def app_dashboard(cache):
    """The App cache's cost carrier, expanded when it arrived in the snapshot's gzip form."""
    return expand_transport_cost_dashboard(cache.get('cost_dashboard') or {})


def first_publication_requires_refresh(accepted, already_refreshed, cache, document):
    return not accepted and not already_refreshed and matrix(app_dashboard(cache))!=matrix(document)


def publication_committed(document, receipt):
    """The receipt is written only after the cycle's publication completes.

    The ownermac reader consumes the local publication; a configured remote
    destination must also have returned. A preceding success, a different
    revision, or an incomplete publication may not trigger an App restart.
    Missing receipt is not treated as zero latency.
    """
    try:
        return (receipt['source_revision']==document['source_revision']
                and receipt['destination'] in ('published','not_configured')
                and at(receipt['generated_at'])>=at(document['generated_at']))
    except (KeyError,TypeError,ValueError):
        return False


def verify_activity_period(view, period):
    """Check observed activity; do not require every Agent to be active."""
    if period not in {'today', 'week'} or view.get('period') != period or view.get('dimension') != 'activity':
        raise AssertionError('the current '+period+' joint activity view is required')
    from pulse_compute_cost.source_layout import source_has_read_failure, source_has_pending_usage
    usage, cost = view['summary']['usage'], view['summary']['cost']
    groups = [g for g in view['groups'] if g['usage']['event_count'] > 0]
    missing = {key: sum(g['usage']['event_count'] for g in groups
                       if g['key'].get(key) in {None, '', 'unknown', 'unassigned', 'ambiguous'})
               for key in ('agent', 'wi', 'entrypoint', 'node', 'runtime', 'model')}
    unpriced_count = cost.get('unpriced_count', len(cost['unpriced']))
    if type(unpriced_count) is not int or unpriced_count < len(cost['unpriced']):
        raise AssertionError('invalid '+period+' complete unpriced count')
    if (sum(g['usage']['event_count'] for g in groups) != usage['event_count']
            or any(missing.values()) or unpriced_count
            or usage['time_unallocated_event_count'] or usage['conflicted_event_count']
            or 'execution_evidence_conflict' in cost['reasons']
            or any(source_has_read_failure(s) for s in usage['source_details'])):
        raise AssertionError('installed '+period+' activity is not reconciled: ' + json.dumps({'missing':missing,'unpriced':unpriced_count,
            'time_unallocated':usage['time_unallocated_event_count'],'conflicts':usage['conflicted_event_count'],
            'source_errors':[{k:s.get(k) for k in ('label','kind','status','reason','files_invalid')}
                             for s in usage['source_details'] if source_has_read_failure(s)]}))
    return {'period': period, 'events': usage['event_count'], 'missing': missing,
            'unpriced': 0, 'time_unallocated': 0, 'conflicts': 0, 'source_read_errors': 0,
            'pending_host_sources':sum(source_has_pending_usage(s) for s in usage['source_details'])}


def verify_weekly_activity(view):
    return verify_activity_period(view, 'week')

def verify_current_activity(today, week):
    if today.get('watermark') is None or today.get('watermark') != week.get('watermark'):
        raise AssertionError('today/week must share one publication watermark')
    results, errors = {}, {}
    for period, view in (('today', today), ('week', week)):
        try:
            results[period] = verify_activity_period(view, period)
        except AssertionError as exc:
            errors[period] = str(exc)
    if errors:
        raise AssertionError('current activity is not reconciled: '+json.dumps(errors))
    return results


def app_cache_difference(cache, document, quota, cc_quota, now):
    """The first way the App cache differs from this publication, or None."""
    if matrix(app_dashboard(cache))!=matrix(document):
        return 'same_published_query_in_app_cache'
    metrics={m['id']:m for m in cache['metrics']}
    if any(metrics.get(m['id'],{}).get('value')!=m['value'] for m in usage_metrics(document,now=now)):
        return 'same_token_projection_in_app_cache'
    if metrics.get('relay.codex.used_percent',{}).get('value')!=quota['weekly']['used_percent']:
        return 'same_account_quota_in_app_cache'
    if metrics.get('relay.codex.resets_at',{}).get('value')!=quota['weekly']['resets_at']:
        return 'same_quota_reset_in_app_cache'
    # CC quota exists only after a real CC turn: absent locally means absent in the App, never 0.
    if metrics.get('relay.cc.used_percent',{}).get('value')!=(cc_quota['weekly']['used_percent'] if cc_quota else None):
        return 'same_cc_quota_in_app_cache'
    if any(k.startswith(('billing.','usage.mtd.','compute_cost.shadow.')) for k in metrics):
        raise AssertionError('obsolete billing projection remains')
    return None


def verify(revision, *, observations=2, timeout=DEFAULT_READBACK_TIMEOUT, required_wi=None, require_actors=False,
           publisher_revision=None, trigger=False, natural=False):
    """Read back the installed publisher, queries and App.

    ``trigger`` starts the registered publisher once and accepts only a
    publication generated after this readback began. ``natural`` judges once,
    without waiting or restarting the App: a scheduled publication after the
    deployment must exist and reproduce its queries; too early is Pending.
    """
    home=Path.home();state=home/'.local/state/pulse/stats';started=time.monotonic()
    accepted=[];restarted=False;initial_refreshed=False;last_wait=None;last_report=started
    publisher_revision=publisher_revision or revision
    observations=1 if natural else observations
    mode='natural' if natural else 'triggered' if trigger else 'scheduled'
    threshold=None
    if natural:
        deployment=json.loads((state/'deployment.json').read_text())
        completed=at(deployment['completed_at'])
        threshold=completed+timedelta(seconds=NATURAL_AFTER)
        if datetime.now(timezone.utc)<threshold:
            raise Pending('deployment_too_recent')
    elif trigger:
        threshold=datetime.now(timezone.utc)
        subprocess.run(['/bin/launchctl','kickstart',f'gui/{os.getuid()}/{PUBLISHER_LABEL}'],check=True,timeout=30)
    def wait(reason):
        nonlocal last_wait,last_report
        if natural:
            if threshold is not None and datetime.now(timezone.utc)>completed+timedelta(seconds=NATURAL_DEADLINE):
                raise AssertionError('no accepted scheduled publication after deployment: '+reason)
            raise Pending(reason)
        now=time.monotonic()
        if last_wait!=reason or now-last_report>=WAIT_PROGRESS_SECONDS:
            print(json.dumps({'awaiting':reason,'waited_seconds':round(now-started),'limit_seconds':timeout}),flush=True)
            last_wait=reason;last_report=now
        time.sleep(5)
    while time.monotonic()-started<timeout:
        now=datetime.now(timezone.utc)
        document=load_cost_dashboard(state/'cost-dashboard-v1.json',now=now)
        quota,reason=load_quota(state/'codex-quota-v1.json',now)
        expected_views={(period,'activity') for period in ('today','week','month')}
        if document.get('source_revision')!=publisher_revision or {(v['period'],v['dimension']) for v in document['views']} != expected_views or not quota:
            wait('fresh_installed_revision_and_account_quota');continue
        try: receipt=json.loads((state/'last-refresh.json').read_text())
        except (OSError,ValueError): receipt={}
        if not publication_committed(document,receipt):
            wait('publication_acknowledgement');continue
        generated=document['generated_at']
        if threshold is not None and at(generated)<threshold:
            wait('publication_after_deployment');continue
        if require_actors:
            activation=json.loads((state/'actor-activation.json').read_bytes())
            if activation['revision']!=revision or activation['status']!='active_pending_consumer_readback':
                raise AssertionError('actor source activation is incomplete')
            if at(generated)<at(activation['observed_at']):
                wait('first_publication_after_actor_activation');continue
        if any(r['generated_at']==generated for r in accepted):
            wait('next_natural_publication');continue
        cc_quota,_=load_cc_quota(state/'cc-quota-v1.json',now)
        cache=None
        if not natural:
            if accepted and not restarted:
                if (at(generated)-at(accepted[0]['generated_at'])).total_seconds()<600:
                    wait('next_scheduled_publication_not_manual_retry');continue
                restart_installed(revision)
                restarted=True;wait('restarted_consumer');continue
            cache=json.loads((home/'Library/Application Support/PulseStats/snapshot-v2.json').read_text())
            if first_publication_requires_refresh(accepted,initial_refreshed,cache,document):
                restart_installed(revision);initial_refreshed=True
                print(json.dumps({'first_committed_publication_refresh':True,'generated_at':generated,'revision':revision}),flush=True)
                wait('initial_consumer_refresh');continue
            difference=app_cache_difference(cache,document,quota,cc_quota,now)
            if difference:
                wait(difference);continue
        registry=json.loads((home/'.local/state/pulse/compute-cost/sources.json').read_text())
        frozen_ledger=publisher_ledger(registry)
        from pulse_compute_cost.catalog_source import resolve_catalog_path, load_catalog_snapshot
        # The publisher binds registry tenants to Agents; the original query
        # must too, or every tenant-keyed group differs.
        agent_tenants=None
        if "tenant_context.v1" in registry.get("required_receipt_features", []):
            from pulse_compute_cost.agent_tenants import load_agent_tenants
            agent_tenants=load_agent_tenants(registry)
        frozen_queries = {}
        def frozen_for(query):
            key = (query.get('as_of'), query.get('watermark'))
            if key not in frozen_queries:
                frozen_queries[key] = CollaborationQuerySession(frozen_ledger.receipt_snapshot(
                    as_of=key[0], watermark=key[1]), agent_tenants=agent_tenants)
            return frozen_queries[key]
        catalog=load_catalog_snapshot(resolve_catalog_path(registry=registry,
            registry_path=home/'.local/state/pulse/compute-cost/sources.json'))
        if catalog is None:
            raise AssertionError('installed publication price evidence is unavailable')
        from pulse_compute_cost.stats_dashboard import project_cost
        for view in document['views']:
            q=dict(view['query']);q['group_by']=tuple(q['group_by']);q['work_items']=tuple(q['work_items'])
            raw=frozen_for(q).query(CollaborationQuery(**q),catalog=catalog['catalog'],catalog_observed_at=retained_catalog_witness(view,catalog))
            if raw['report_sha256']!=view['report_sha256'] or project_cost(raw['summary']['cost'])!=view['summary']['cost']:
                raise AssertionError('original query and publisher differ')
            if raw['summary']['usage']['tokens']!=view['summary']['usage']['tokens']:
                raise AssertionError('publisher Token facts differ')
            if [(g['key'],project_cost(g['cost'])) for g in raw['groups'] if view['dimension']!='activity' or g['usage']['event_count']>0]!=[(g['key'],g['cost']) for g in view['groups']]:
                raise AssertionError('published groups differ from query')
        today=next(v for v in document['views'] if v['period']=='today' and v['dimension']=='activity')
        week=next(v for v in document['views'] if v['period']=='week' and v['dimension']=='activity')
        period_acceptance=verify_current_activity(today,week) if require_actors else None
        weekly_acceptance=period_acceptance['week'] if period_acceptance else None
        business = None
        if required_wi:
            q = dict(week['query']); q['group_by']=('agent',); q['work_items']=(required_wi,)
            raw=frozen_for(q).query(CollaborationQuery(**q),catalog=catalog['catalog'],catalog_observed_at=catalog['retrieved_at'])
            usage=raw['summary']['usage']; cost=raw['summary']['cost']
            if (usage['event_count']<=0 or usage['unassigned_event_count']!=0
                    or (cost['machine_cash_usd'] is None and cost['partial_machine_cash_usd'] is None)
                    or 'join_conflict' in cost['reasons']):
                raise AssertionError('required live WI still lacks reconciled attributed cost')
            for label in ('ownermac.runtime.pi','tcloud.runtime.pi'):
                sources=[r for r in usage['source_details'] if r['label']==label]
                if len(sources)!=1 or sources[0]['files_invalid'] or sources[0]['status'] in {'partial','unreachable'}:
                    raise AssertionError('an agreed native runtime source is missing or invalid')
            business={'wi':required_wi,'events':usage['event_count'],'tokens':usage['known_total_tokens'],
                'last_usage_at':usage['last_usage_at'],'amount':cost['machine_cash_usd'] or cost['partial_machine_cash_usd'],
                'agents':[g['key']['agent'] for g in raw['groups'] if g['usage']['event_count']]}
            if business['agents']!=['pulse']:
                raise AssertionError('required WI executor attribution differs')
        identities = None
        if require_actors:
            q=dict(week['query']); q['group_by']=('agent','node','runtime','model','entrypoint'); q['work_items']=()
            raw=frozen_for(q).query(CollaborationQuery(**q),catalog=catalog['catalog'],catalog_observed_at=catalog['retrieved_at'])
            identities=[{'key':g['key'],'events':g['usage']['event_count'],
                'amount':g['cost']['machine_cash_usd'] or g['cost']['partial_machine_cash_usd'],
                'wi_unlinked':g['usage']['wi_unassigned_event_count']} for g in raw['groups'] if g['usage']['event_count']]
            expected={('pulse','ownermac','codex'),('pulse','tcloud','pi'),('aki','tcloud','pi')}
            actual={(r['key']['agent'],r['key']['node'],r['key']['runtime']) for r in identities}
            if not expected<=actual:
                print(json.dumps({'actor_groups':identities}),flush=True)
                raise AssertionError('one of the three actual Agent/node/runtime routes is not identified')
            if not any(r['key']['agent']=='pulse' and r['key']['node']=='ownermac' and r['key']['runtime']=='codex' and r['key']['entrypoint']=='codex_app' for r in identities):
                raise AssertionError('Local Pulse Codex App entrypoint is still unrecognized')
        report={'observed_at':now.isoformat(),'generated_at':generated,'revision':revision,
                'watermark':today['watermark'],'events':today['summary']['usage']['event_count'],
                'tokens':today['summary']['usage']['tokens'],'partial_usd':today['summary']['cost']['partial_machine_cash_usd'],
                'quota_percent':quota['weekly']['used_percent'],'quota_observed_at':quota['observed_at'],
                'cc_quota_percent':cc_quota['weekly']['used_percent'] if cc_quota else None,
                'cc_quota_observed_at':cc_quota['observed_at'] if cc_quota else None,
                'publisher_revision':publisher_revision,'natural':natural,
                'query_publication_cache_parity':not natural,'after_app_restart':restarted,
                'weekly_acceptance':weekly_acceptance,
                'today_acceptance':period_acceptance['today'] if period_acceptance else None,
                'open_incidents':cache['overall']['open_incident_count'] if cache else None,
                'coverage':today['summary']['usage']['source_coverage'],'required_business':business,
                'actor_groups':identities,'agent_unidentified':today['summary']['usage']['agent_unassigned_event_count'],
                'wi_unlinked':today['summary']['usage']['wi_unassigned_event_count']}
        accepted.append(report);print(json.dumps(report),flush=True)
        if len(accepted)>=observations:
            print(json.dumps({'installed_consumer_verified':True,'mode':mode,'publications':len(accepted),
                              'app_restart_verified':restarted,'whole_platform_coverage_not_inferred':True}),flush=True)
            return
        wait('next_natural_publication')
    raise TimeoutError('installed consumer did not converge: '+str(last_wait))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--observations',type=int,default=2)
    parser.add_argument('--timeout',type=int,default=DEFAULT_READBACK_TIMEOUT)
    parser.add_argument('--required-wi')
    parser.add_argument('--require-actors',action='store_true')
    parser.add_argument('--publisher-revision',help='revision of the installed publisher, when it differs from the readers')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--trigger',action='store_true',help='start one publication now and accept only it or a later one')
    mode.add_argument('--natural',action='store_true',help='judge once whether a scheduled publication after deployment reproduces its queries')
    a=parser.parse_args()
    try:
        verify(a.revision,observations=a.observations,timeout=a.timeout,required_wi=a.required_wi,require_actors=a.require_actors,
               publisher_revision=a.publisher_revision,trigger=a.trigger,natural=a.natural)
    except Pending as pending:
        print(json.dumps({'natural_acceptance':'pending','reason':str(pending)}),flush=True)
