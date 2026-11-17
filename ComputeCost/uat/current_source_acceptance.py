#!/usr/bin/env python3
"""One fixed-source, private three-node actual-business acceptance run.

Uses the installed transport routes, original ledger, original query/publisher
and Swift reader. No production input, source file or installation is modified.
"""
from __future__ import annotations
import argparse, concurrent.futures, datetime, gzip, hashlib, json, os
from pathlib import Path
import shlex, subprocess, sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from effective_source_replay import replay
from pulse_compute_cost.receipts import Receipt, canonical, current_receipts
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.stats_dashboard import build_dashboard, publish_dashboard, _view
from zoneinfo import ZoneInfo

# Resolve the real exporter before starting any remote/private replay.
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'Stats/exporter'))
from pulse_stats.cost_dashboard import load_cost_dashboard

WI='wi_20260823_184'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--sha',required=True)
    a=parser.parse_args();work=a.work;work.mkdir(mode=0o700,parents=True,exist_ok=False)
    archive=a.archive.read_bytes();home=Path.home()
    remote="""set -euo pipefail
root=$(mktemp -d /tmp/wi568-acceptance-XXXXXXXX)
trap 'case "$root" in /tmp/wi568-acceptance-*) rm -rf -- "$root";; esac' EXIT
tar -xzf - -C "$root"
if ! PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 "$root/ComputeCost/uat/effective_source_replay.py" --work "$root/private" --node NODE --no-repeat > "$root/replay.log" 2>&1; then
  cat "$root/replay.log" >&2; exit 1
fi
cat "$root/replay.log" >&2
cat "$root/private/receipts.jsonl.gz"
"""
    def peer(node,host,prefix):
        args=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2',host,
            shlex.join(prefix+['/bin/bash','-c',remote.replace('NODE',node)])]
        result=subprocess.run(args,input=archive,capture_output=True,timeout=900)
        if result.returncode:
            print('PEER_FAILED '+json.dumps({'node':node,'code':result.returncode,'error_sha256':hashlib.sha256(result.stderr).hexdigest()}),flush=True)
            # Retain private debugging material; never raw credentials/content in logs.
            p=work/(node+'-error.log');p.write_bytes(result.stderr);p.chmod(0o600)
            return node,None
        data=gzip.decompress(result.stdout)
        if len(data)>64*1024*1024:raise ValueError('candidate receipt transport exceeds bound')
        rows=[Receipt.from_dict(json.loads(line)) for line in data.splitlines() if line.strip()]
        target=work/(node+'.jsonl');target.write_bytes(b''.join(canonical(r.to_dict())+b'\n' for r in rows));target.chmod(0o600)
        summaries=[line.split('EFFECTIVE_SOURCE_REPLAY ',1)[1] for line in result.stderr.decode().splitlines() if line.startswith('EFFECTIVE_SOURCE_REPLAY ')]
        if len(summaries)!=1:raise ValueError('node replay result missing')
        summary=json.loads(summaries[0]);print('PEER_COLLECTION '+json.dumps({'node':node,'runtime_observation':summary.get('runtime_observation'),
            'sources':summary['sources'],'receipt_count':len(rows),'old_watermark_unchanged':summary['old_watermark_unchanged']}),flush=True)
        return node,target
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        tcloud=pool.submit(peer,'tcloud','tcloud-node2',['runuser','-u','pulse','--'])
        vultr=pool.submit(peer,'vultr','vultr-pulse',[])
        mac=pool.submit(replay,work/'mac','ownermac','2026-09-14',repeat=False)
        mac.result();peers=[tcloud.result(),vultr.result()]
    if any(path is None for _,path in peers):raise ValueError('required source replay failed')
    registry=json.loads((work/'mac/candidate-registry.json').read_bytes())
    registry['sources'] += [{'id':'stats-transport-'+node,'kind':'receipts','path':str(path),'label':node+'.transport'} for node,path in peers]
    ledger=UsageLedger(registry['ledger_root']);before=ledger.receipt_snapshot()
    result=collect_registered_sources(registry,ledger);snap=ledger.receipt_snapshot()
    assert ledger.receipt_snapshot(watermark=before['watermark'])['sha256']==before['sha256']
    catalog=json.loads((home/'.local/state/pulse/stats/openrouter-catalog.json').read_bytes())
    subscriptions=json.loads((Path(__file__).resolve().parents[1]/'config/procurement-facts.json').read_bytes())
    now=datetime.datetime.now(datetime.timezone.utc)
    document=build_dashboard(ledger,subscriptions=subscriptions,source_revision=a.sha,now=now,catalog_snapshot=catalog)
    assert document['status']=='available' and len(document['views'])==9
    for view in document['views']:
        q=CollaborationQuery(**view['query'])
        original=query_collaboration(ledger,q,catalog=catalog['catalog'],catalog_observed_at=catalog['retrieved_at'])
        assert _view(original,view['period'],view['dimension'])==view
    assert document['status']=='available' and len(document['views'])==9
    publish_dashboard(work/'dashboard.json',document,allow_row_contexts=True)
    decoded=load_cost_dashboard(work/'dashboard.json',now=now)
    assert decoded==document, 'actual exporter rejected or altered publication'
    business=[]
    for day in sorted({'2026-09-14',now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()}):
        end=(datetime.date.fromisoformat(day)+datetime.timedelta(days=1)).isoformat()
        q=CollaborationQuery(from_date=day,to_date=end,scope='activity',work_items=(WI,),group_by=('agent',),watermark=snap['watermark'])
        out=query_collaboration(ledger,q,catalog=catalog['catalog'],catalog_observed_at=catalog['retrieved_at'])
        summary=out['summary'];row={'day':day,'wi':WI,'events':summary['usage']['event_count'],
            'known_total_tokens':summary['usage']['known_total_tokens'],'last_usage_at':summary['usage']['last_usage_at'],
            'unassigned':summary['usage']['unassigned_event_count'],'cost':summary['cost'],
            'groups':[{'key':g['key'],'cost':g['cost']} for g in out['groups']],
            'suppressed_host_count':len(out['reconciliation'].get('native_boundary_supersession',{}))}
        business.append(row);print('REAL_BOUND_BUSINESS '+json.dumps(row,sort_keys=True),flush=True)
        if 'join_conflict' in row['cost']['reasons']:
            from collections import Counter
            current, conflicts=current_receipts(snap['facts'])
            bad=set(out['reconciliation']['conflicting_refs'])
            grouped=Counter((r.kind,r.producer,r.payload.get('mapping'),r.payload.get('wi')) for r in current if r.entity_ref in bad)
            print('RECONCILIATION_BREAKDOWN '+json.dumps({'day':day,'groups':[{ 'kind':k[0],'producer':k[1],'mapping':k[2],'wi':k[3],'count':n} for k,n in grouped.items()],
                'revision_kinds':dict(Counter(r.kind for values in conflicts.values() for r in values))}),flush=True)
            from pulse_compute_cost.events import parse_timestamp
            for host in [r for r in current if r.entity_ref in bad and r.kind=='usage' and r.payload.get('mapping')=='relay_pi.v1'][:12]:
                hp=host.payload; low=hp['interval_start']; high=hp['interval_end']
                native=[r for r in current if r.kind=='usage' and r.payload.get('mapping')=='pi_native.v1'
                    and r.payload.get('native_scope_ref')==hp.get('native_scope_ref') and r.occurred_at
                    and low and high and parse_timestamp(low,'start')<=parse_timestamp(r.occurred_at,'at')<=parse_timestamp(high,'end')]
                inventories=[r for r in current if r.producer=='pi_native_inventory.v1' and r.payload['source_ref']==hp.get('native_scope_ref')]
                print('UNRESOLVED_HOST_BOUNDARY '+json.dumps({'host_ref':host.entity_ref,'wi':hp['wi'],
                    'start':low,'end':high,'tokens':hp['tokens'],'native_count':len(native),
                    'native_tokens':{k:sum(r.payload['tokens'][k] or 0 for r in native) for k in hp['tokens']},
                    'inventory_sizes':[(r.payload['observed'],len(r.evidence)) for r in inventories]}),flush=True)
        # Independent source-to-query reconciliation: enumerate the exact
        # natively observed requests named by authoritative attribution facts.
        # A nonempty WI-filtered report alone would make unassigned=0 tautological.
        from decimal import Decimal
        current,_=current_receipts(snap['facts'])
        assigned={r.payload['target_ref'] for r in current if r.kind=='attribution'
            and r.payload['wi']==WI and r.payload['state']=='assigned'
            and r.payload['policy']=='scl_verified_native_boundary.v1'}
        native=[r for r in current if r.kind=='usage' and r.entity_ref in assigned
            and r.payload['mapping']=='pi_native.v1' and r.occurred_at
            and datetime.datetime.fromisoformat(r.occurred_at.replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()==day]
        expected={k:sum(r.payload['tokens'][k] for r in native) for k in ('uncached_input','cache_read','cache_write','output')}
        assert native and row['events']==len(native) and row['known_total_tokens']==sum(expected.values())
        assert {k:summary['usage']['tokens'][k]['known'] for k in expected}==expected
        subtotal=Decimal(0);unpriced=0
        for fact in native:
            p=fact.payload;model=p['model'];model=('openai/'+model) if model and model.startswith(('gpt-','o1','o3','o4')) else model
            prices=[r['pricing'] for r in catalog['catalog']['data'] if r['id']==model]
            if len(prices)!=1:unpriced+=1;continue
            rates={'uncached_input':'prompt','cache_read':'input_cache_read','cache_write':'input_cache_write','output':'completion'}
            subtotal+=sum(Decimal(p['tokens'][k])*Decimal(prices[0][rate]) for k,rate in rates.items() if p['tokens'][k])
        assert Decimal(row['cost']['partial_machine_cash_usd'] or row['cost']['machine_cash_usd'])==subtotal
        assert len(row['cost']['unpriced'])==unpriced
        print('RAW_BOUNDARY_RECONCILIATION '+json.dumps({'day':day,'native_requests':len(native),
            'tokens':expected,'priced_usd':str(subtotal),'unpriced_requests':unpriced,'source_query_equal':True}),flush=True)
        assert row['events']>0 and row['unassigned']==0
        assert row['cost']['partial_machine_cash_usd'] is not None and 'join_conflict' not in row['cost']['reasons']
        assert any(g['key']=={'agent':'pulse'} for g in out['groups'])
    print('SAME_SET_ACCEPTANCE '+json.dumps({'watermark':snap['watermark'],'view_count':9,'query_publisher_equal':True,
        'source_revision':a.sha,'catalog_sha256':document['views'][0]['catalog_sha256'],'production_changed':False}),flush=True)
    (work/'acceptance.json').write_bytes(canonical({'business':business,'watermark':snap['watermark'],'source_revision':a.sha}))
    # All facts are frozen for replay: replaying a different live root is not an
    # idempotence assertion. Re-submit exactly the accepted facts twice.
    for _ in range(2):
        check=ledger.ingest_receipts(snap['facts']);assert check['inserted']==0
    print('FROZEN_REPEAT inserted=[0,0] old_watermark_unchanged=true',flush=True)

if __name__=='__main__':main()
