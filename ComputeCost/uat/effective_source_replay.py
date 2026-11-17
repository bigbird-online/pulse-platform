#!/usr/bin/env python3
"""Candidate-only replay of the inspected runtime registry into a private copy.

No production mutation, source permission changes, business turn, or paid call.
The report separates source facts, query inclusion and attribution. Receipts are
written privately for the central original-query/Stats comparison.
"""
from __future__ import annotations
import argparse, datetime, fcntl, gzip, json, os, pathlib, plistlib, shutil, sys, time
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from pulse_compute_cost.source_registration import runtime_candidate_registry
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import canonical, current_receipts, digest
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.native_attribution import execution_bindings
from zoneinfo import ZoneInfo


def runtime_environment():
    keys=('HARNESS_ROOT','RELAY_PI_RUNTIME_MANIFEST_PATH','RELAY_CONFIG_PATH','PULSE_COMPUTECOST_RECEIPT_DIR','SCL_KERNEL_ROOT','SCL_HUMAN_DECISION_KEY_FILE')
    if sys.platform=='darwin':
        p=pathlib.Path.home()/'Library/LaunchAgents/com.pulse.relay.plist'
        env=plistlib.loads(p.read_bytes()).get('EnvironmentVariables',{})
        return {k:env[k] for k in keys if k in env}
    found=[]
    for proc in pathlib.Path('/proc').glob('[0-9]*'):
        try:
            if proc.stat().st_uid!=os.getuid() or not pathlib.Path(os.readlink(proc/'exe')).name.startswith('node'):continue
            raw=dict(v.split(b'=',1) for v in (proc/'environ').read_bytes().split(b'\0') if b'=' in v)
            if b'RELAY_PI_RUNTIME_MANIFEST_PATH' not in raw:continue
            found.append({k:raw[k.encode()].decode() for k in keys if k.encode() in raw})
        except (OSError,UnicodeError):continue
    unique={json.dumps(e,sort_keys=True):e for e in found}
    if not unique:
        # No live Relay was found. This exact installed manifest remains a
        # historical source declaration, not evidence of a running service.
        manifest=pathlib.Path.home()/'.relay/pi-runtime-manifest.json'
        if manifest.is_file() and not manifest.is_symlink():
            data=json.loads(manifest.read_bytes())
            roots={p.get('computeCostHost',{}).get('sclHarnessRoot') for p in data.get('profiles',{}).values()}
            roots.discard(None)
            if len(roots)==1:
                return {'HARNESS_ROOT':next(iter(roots)), 'RELAY_PI_RUNTIME_MANIFEST_PATH':str(manifest),
                    'PULSE_COMPUTECOST_RECEIPT_DIR':str(pathlib.Path.home()/'.local/state/pulse/compute-cost/host-receipts'),
                    'runtime_observation':'configured_inactive'}
    if len(unique)!=1:raise ValueError('one inspected runtime environment is required')
    return next(iter(unique.values()))


def replay(work: pathlib.Path, node: str, day: str, *, repeat: bool=True):
    started=time.monotonic();home=pathlib.Path.home()
    work.mkdir(mode=0o700,parents=True,exist_ok=False)
    registry=json.loads((home/'.local/state/pulse/compute-cost/sources.json').read_bytes())
    env=runtime_environment()
    candidate=runtime_candidate_registry(registry,pathlib.Path(env['RELAY_PI_RUNTIME_MANIFEST_PATH']),
        harness_root=pathlib.Path(env['HARNESS_ROOT']),receipt_root=pathlib.Path(env['PULSE_COMPUTECOST_RECEIPT_DIR']),
        relay_config=pathlib.Path(env['RELAY_CONFIG_PATH']) if env.get('RELAY_CONFIG_PATH') else None)
    for source in candidate['sources']:
        source['label']=node+'.'+('runtime.pi' if source.get('runtime_manifest') and source['kind']=='pi' else source['kind'])
    production=pathlib.Path(registry['ledger_root']);target=work/'ledger'
    with (production/'.usage-ledger.lock').open('rb') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH)
        shutil.copytree(production,target)
    candidate['ledger_root']=str(target)
    (work/'candidate-registry.json').write_bytes(canonical(candidate));(work/'candidate-registry.json').chmod(0o600)
    ledger=UsageLedger(target);old=ledger.receipt_snapshot();previous=old['sha256']
    import yaml
    raw_bindings=[yaml.safe_load(p.read_bytes()) for p in (pathlib.Path(env['HARNESS_ROOT'])/'executions').glob('wi_*.yaml')]
    print('CANONICAL_CLASSES '+json.dumps({'node':node,'bindings':[{k:r.get(k) for k in
        ('work_item_id','agent_id','epoch','phase','runtime','lifecycle_state','session_ref')} for r in raw_bindings]}),flush=True)
    binding=execution_bindings([pathlib.Path(env['HARNESS_ROOT'])])
    print('CANONICAL_PI '+json.dumps({'node':node,'bindings':[{'wi':k[0],'agent':k[1],'epoch':k[2]} for k in binding]}),flush=True)
    collection=collect_registered_sources(candidate,ledger)
    snap=ledger.receipt_snapshot();active,conflicts=current_receipts(snap['facts'])
    assert ledger.receipt_snapshot(watermark=old['watermark'])['sha256']==previous
    catalog_path=home/'.local/state/pulse/stats/openrouter-catalog.json'
    catalog=json.loads(catalog_path.read_bytes()) if catalog_path.exists() else {}
    reports={}
    for selected in sorted({day,datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()}):
        end=(datetime.date.fromisoformat(selected)+datetime.timedelta(days=1)).isoformat()
        q=CollaborationQuery(from_date=selected,to_date=end,scope='activity',group_by=('wi',),watermark=snap['watermark'])
        report=query_collaboration(ledger,q,catalog=catalog.get('catalog'),catalog_observed_at=catalog.get('retrieved_at'))
        reports[selected]={'usage':{k:v for k,v in report['summary']['usage'].items() if k in
            ('event_count','known_total_tokens','last_usage_at','unassigned_event_count','unassigned_known_total_tokens')},
            'cost':{k:v for k,v in report['summary']['cost'].items() if k!='unpriced'},
            'groups':[{'key':g['key'],'events':g['usage']['event_count'],'tokens':g['usage']['known_total_tokens'],
                'amount':g['cost']['machine_cash_usd'] or g['cost']['partial_machine_cash_usd']} for g in report['groups']],
            'overlap_conflicts':len(report['reconciliation']['conflicting_refs'])}
    result={'node':node,'runtime_observation':env.get('runtime_observation','loaded_configuration'),'sources':collection['sources'],'inserted':collection['inserted'],
        'usage_facts':sum(r.kind=='usage' for r in active),'assigned_usage_facts':sum(r.kind=='attribution' and r.payload['state']=='assigned' for r in active),
        'revision_conflicts':len(conflicts),'old_watermark_unchanged':True,'reports':reports,'elapsed_seconds':round(time.monotonic()-started,2),
        'production_changed':False}
    print('EFFECTIVE_SOURCE_REPLAY '+json.dumps(result,sort_keys=True),flush=True)
    (work/'result.json').write_bytes(canonical(result));(work/'result.json').chmod(0o600)
    data=b''.join(canonical(r.to_dict())+b'\n' for r in snap['facts'])
    (work/'receipts.jsonl.gz').write_bytes(gzip.compress(data));(work/'receipts.jsonl.gz').chmod(0o600)
    if repeat:
        second=collect_registered_sources(candidate,ledger)
        print('LIVE_RECOLLECTION '+json.dumps({'node':node,'inserted':second['inserted'],
              'old_watermark_unchanged':ledger.receipt_snapshot(watermark=old['watermark'])['sha256']==previous}),flush=True)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--work',type=pathlib.Path,required=True);p.add_argument('--node',required=True)
    p.add_argument('--day',default='2026-09-14');p.add_argument('--no-repeat',action='store_true');a=p.parse_args()
    replay(a.work,a.node,a.day,repeat=not a.no_repeat)
