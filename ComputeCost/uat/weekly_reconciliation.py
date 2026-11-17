#!/usr/bin/env python3
"""Real weekly UAT in private copies. Never install, call a model or edit sources."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'ComputeCost'), str(ROOT/'Stats'), str(ROOT/'Stats/exporter/tests'), str(ROOT/'Stats/exporter')]
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import canonical, digest
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.stats_dashboard import build_dashboard, DEFAULT_FACTS
from pulse_compute_cost.stats_dashboard_wire import expand_dashboard
from activate_actor_sources import plan
from deploy import paused_peers


def private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


def copy_ledger(original: Path, destination: Path) -> None:
    if original.is_symlink() or not original.is_dir():
        raise ValueError('registered ledger is not a regular directory')
    with (original/'.usage-ledger.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        shutil.copytree(original, destination)


def candidate_registry(site: str) -> dict:
    home=Path.home();state=home/'.local/state/pulse/stats'
    original=json.loads((home/'.local/state/pulse/compute-cost/sources.json').read_bytes())
    surfaces=json.loads((ROOT/'config/scl-surfaces.yaml').read_bytes())
    directories={s.get('human_input_directory') for s in original['sources'] if s.get('human_input_directory')}
    if len(directories)>1:
        raise ValueError('ambiguous already registered Host input directories')
    from effective_source_replay import runtime_environment
    env=runtime_environment()
    configured=env.get('SCL_HUMAN_DECISION_KEY_FILE')
    directory=(Path(configured).parent/'human-inputs') if configured else (Path(next(iter(directories))) if directories else None)
    if directory is not None and (directory.is_symlink() or not directory.is_dir()):
        raise ValueError('current runtime input directory is unavailable')
    refresh=json.loads((state/'refresh.json').read_bytes()) if (state/'refresh.json').exists() else {}
    return plan(original,surfaces,site,directory,codex_home=refresh.get('codex_quota',{}).get('home'))


def collect_copy(work: Path, site: str, imports: list[dict] | None = None, *, incremental: bool = False) -> tuple[dict, dict]:
    started=time.monotonic();registry=candidate_registry(site)
    production=Path(registry['ledger_root']);target=work/'ledger'
    copy_ledger(production,target);registry['ledger_root']=str(target)
    registry['sources'].extend(imports or [])
    ledger=UsageLedger(target);before=ledger.receipt_snapshot()
    measurements=[]
    for mode in (('cold','warm','forced') if incremental else ('cold',)):
        phase=time.monotonic()
        collection=collect_registered_sources(registry,ledger,force_recompute=(mode=='forced'))
        measurements.append({'mode':mode,'seconds':round(time.monotonic()-phase,3),
            'inserted':collection['inserted'],**collection.get('parser_cache',{})})
    after=ledger.receipt_snapshot()
    assert ledger.receipt_snapshot(watermark=before['watermark'])['sha256']==before['sha256']
    repeated=ledger.ingest_receipts(after['facts'])
    assert repeated['inserted']==0, 'replaying identical retained facts changed the ledger'
    result={'site':site,'seconds':round(time.monotonic()-started,3),
        'original_watermark':before['watermark'],'original_facts_sha256':before['sha256'],
        'watermark':after['watermark'],'facts_sha256':after['sha256'],
        'sources':collection['sources'],'inserted':collection['inserted'],'collection_cycles':measurements,
        'original_watermark_unchanged':True,'identical_receipts_idempotent':True,'production_modified':False}
    private(work/'collection.json',canonical(result))
    return after,result


# This receiver materializes only this pinned repository's test source in a
# unique temporary directory. It never replaces a running peer installation.
RECEIVER = r'''import io,json,os,pathlib,subprocess,sys,tarfile,tempfile
with tempfile.TemporaryDirectory(prefix='pulse-weekly-uat-') as tmp:
 root=pathlib.Path(tmp);source=root/'source';source.mkdir(mode=0o700)
 payload=sys.stdin.buffer.read(32*1024*1024+1)
 if len(payload)>32*1024*1024:raise ValueError('source archive exceeds bound')
 with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
  for member in archive.getmembers():
   path=pathlib.PurePosixPath(member.name)
   if path.is_absolute() or '..' in path.parts or not (member.isdir() or member.isfile()):raise ValueError('unsafe test archive')
   dest=source/path;dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
   if member.isdir():dest.mkdir(mode=0o700,exist_ok=True)
   else:dest.write_bytes(archive.extractfile(member).read());dest.chmod(0o600)
 out=root/'replay';out.mkdir(mode=0o700)
 command=[sys.executable,'-B',str(source/'ComputeCost/uat/weekly_reconciliation.py'),'--site',sys.argv[1],'--output',str(out),'--peer']
 if len(sys.argv)>2:command.append('--incremental')
 run=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=720)
 if run.returncode:
  sys.stderr.buffer.write(run.stderr);raise SystemExit(run.returncode)
 if len(sys.argv)>2:
  import gzip,importlib.util
  sys.path[:0]=[str(source/'ComputeCost'),str(source/'Stats/exporter')]
  spec=importlib.util.spec_from_file_location('stats_transport',source/'Scripts/pulse-stats-cost-refresh.py')
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  document=json.loads(gzip.decompress((out/'peer.json.gz').read_bytes()))
  rows=[module.Receipt.from_dict(r) for r in document['facts']]
  size=0;whole=module.sha256()
  for row in rows:
   raw=module.canonical(row.to_dict())+b'\n';size+=len(raw);whole.update(raw)
  wire=module.receipt_transport.encode(rows,int(sys.argv[2]),sys.argv[3])
  document['result']['delta_wire_bytes']=len(wire)
  document['result']['full_receipt_bytes']=size
  unchanged=module.receipt_transport.encode(rows,size,whole.hexdigest())
  document['result']['unchanged_wire_bytes']=len(unchanged)
  assert len(unchanged)<512
  sys.stdout.buffer.write(module.canonical(document['result'])+b'\n'+wire)
 else:
  sys.stdout.buffer.write((out/'peer.json.gz').read_bytes())
'''


def peer_replay(peer: dict, archive: bytes, output: Path, *, incremental: bool = False) -> tuple[dict,dict]:
    if (peer['id'],peer['host']) not in {('vultr','vultr-pulse'),('tcloud','tcloud-node2')}:
        raise ValueError('unexpected registered peer')
    prefix=peer.get('prefix',[])
    if prefix not in ([],['runuser','-u','pulse','--']):
        raise ValueError('unexpected registered service account')
    command=prefix+[peer['python'],'-c',RECEIVER,peer['id']]
    if incremental:
        import importlib.util
        spec=importlib.util.spec_from_file_location('stats_transport',ROOT/'Scripts/pulse-stats-cost-refresh.py')
        transport=importlib.util.module_from_spec(spec);spec.loader.exec_module(transport)
        base_path,base_size,base_hash=transport.receipt_transport.cursor(
            Path.home()/'.local/state/pulse/stats/imports'/(peer['id']+'.jsonl'))
        command += [str(base_size),base_hash]
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',peer['host'],shlex.join(command)],
        input=archive,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=780)
    if result.returncode:
        # Receiver errors are test-source exceptions, never a live service log.
        raise RuntimeError('peer replay failed: '+peer['id']+'; stderr sha256='+hashlib.sha256(result.stderr).hexdigest())
    target=output/(peer['id']+'.jsonl')
    if incremental:
        summary,_,wire=result.stdout.partition(b'\n')
        installed=transport.receipt_transport.install(target,wire,base_path)
        document={'result':json.loads(summary)}
        document['result']['installed_frames']=installed['frames']
        document['result']['retained_bytes']=installed['retained_bytes']
    else:
        document=json.loads(gzip.decompress(result.stdout))
        data=b''.join(canonical(row)+b'\n' for row in document['facts'])
        private(target,data)
    return {'id':'stats-transport-'+peer['id'],'kind':'receipts','path':str(target)},document['result']


def run(output: Path, revision: str, *, diagnose: bool=False, period: str="week", incremental: bool=False) -> dict:
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    home=Path.home();state=home/'.local/state/pulse/stats'
    config=json.loads((state/'refresh.json').read_bytes())
    publication=expand_dashboard(json.loads((state/'cost-dashboard-v1.json').read_bytes()))
    catalog=json.loads((state/'openrouter-catalog.json').read_bytes())
    archive=subprocess.run(['git','-C',str(ROOT),'archive',revision,'ComputeCost','Stats','config/scl-surfaces.yaml','Scripts/pulse-stats-cost-refresh.py'],check=True,stdout=subprocess.PIPE).stdout
    reports=[]
    with tempfile.TemporaryDirectory(prefix='weekly-replay-',dir=output) as tmp:
        work=Path(tmp);sources=[]
        # A peer paused by decision runs no Stats replay either.
        for peer in [p for p in config['peers'] if p['id'] not in paused_peers(config)]:
            source,report=peer_replay(peer,archive,work,incremental=incremental);sources.append(source);reports.append(report)
            print(json.dumps({'stage':'peer_collected','site':peer['id'],'seconds':report['seconds']}),flush=True)
        snapshot,local=collect_copy(work,'ownermac',sources,incremental=incremental);reports.append(local)
        private(output/'collection-local.json',canonical(local))
        now=datetime.now(timezone.utc);today=now.astimezone(ZoneInfo('Asia/Shanghai')).date()
        start=today.replace(day=1) if period=="month" else today-timedelta(days=today.weekday());end=today+timedelta(days=1)
        class Frozen:
            def receipt_snapshot(self,**kwargs):return snapshot
        query=CollaborationQuery(scope='activity',from_date=start.isoformat(),to_date=end.isoformat(),timezone='Asia/Shanghai',
            group_by=('agent','wi','entrypoint','node','runtime','model'),watermark=snapshot['watermark'])
        report=query_collaboration(Frozen(),query,catalog=catalog['catalog'],catalog_observed_at=catalog['retrieved_at'])
        from dataclasses import replace
        baseline=query_collaboration(UsageLedger(work/'ledger'),replace(query,watermark=local['original_watermark']),
            catalog=catalog['catalog'],catalog_observed_at=catalog['retrieved_at'])
        private(output/(period+'-baseline.json'),canonical(baseline))

        usage=report['summary']['usage'];cost=report['summary']['cost']
        groups=[g for g in report['groups'] if g['usage']['event_count']]
        missing={key:sum(g['usage']['event_count'] for g in groups if g['key'].get(key) in {None,'unassigned','ambiguous'})
                 for key in ('agent','wi','entrypoint','node','runtime','model')}
        from pulse_compute_cost.source_layout import source_has_read_failure
        source_errors=[d for d in usage['source_details'] if source_has_read_failure(d)]
        clean=not (any(missing.values()) or cost['unpriced'] or usage['time_unallocated_event_count']
                   or usage['conflicted_event_count'] or source_errors or 'execution_evidence_conflict' in cost['reasons'])
        result={'revision':revision,'observed_at':now.isoformat(),'period':period,'window':[start.isoformat(),end.isoformat()],
            'baseline_published_watermark':publication['views'][0]['watermark'],'watermark':snapshot['watermark'],
            'facts_sha256':snapshot['sha256'],'usage':usage,'cost':cost,'missing':missing,
            'groups':[{'key':g['key'],'events':g['usage']['event_count'],'known_tokens':g['usage']['known_total_tokens'],
                       'cost':g['cost'],'fact_refs':g['usage']['fact_refs']} for g in groups],
            'source_errors':source_errors,'collection':reports,period+'_accepted':clean,'production_modified':False}
        private(output/(period+'-summary.json'),canonical(result))
        private(output/(period+'-query.json'),canonical(report))
        print(json.dumps({period+'_accepted':clean,'events':usage['event_count'],'missing':missing,
              'unpriced':len(cost['unpriced']),'time_unallocated':usage['time_unallocated_event_count'],
              'source_errors':len(source_errors),'known_usd':cost['partial_machine_cash_usd']}),flush=True)
        if not diagnose:
            assert clean, period+' native reconciliation is not complete; see exact missing references'
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--revision');p.add_argument('--site');p.add_argument('--peer',action='store_true');p.add_argument('--diagnose',action='store_true');p.add_argument('--incremental',action='store_true');p.add_argument('--period',choices=['week','month'],default='week');a=p.parse_args()
    if a.peer:
        snapshot,result=collect_copy(a.output,a.site,incremental=a.incremental)
        private(a.output/'peer.json.gz',gzip.compress(canonical({'facts':[r.to_dict() for r in snapshot['facts']],'result':result})))
    else:
        run(a.output,a.revision,diagnose=a.diagnose,period=a.period,incremental=a.incremental)
