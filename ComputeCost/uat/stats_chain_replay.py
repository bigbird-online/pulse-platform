#!/usr/bin/env python3
"""Bounded, read-only WI568 native/transport replay. Run via trusted Actions.

No production writes, provider calls, credentials, raw transcripts or receipt
bodies in output. The caller owns a private temporary work directory and cleanup.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'ComputeCost'))
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources, read_pi, read_codex
from pulse_compute_cost.receipts import Receipt, canonical, current_receipts
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.source_registration import candidate_registry
from pulse_compute_cost.stats_dashboard import build_dashboard


def log(name, value):
    print(name+' '+json.dumps(value,sort_keys=True),flush=True)


def private_file(path, data):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as out: out.write(data)


def frozen_native(root, target, budget, kind):
    copied=skipped=observations=used=0
    digests=[]
    if not root.is_dir() or root.is_symlink():
        return {'copied_files':0,'skipped_files':0,'status':'unavailable','bytes':0,'observations':0}
    target.mkdir(mode=0o700,parents=True,exist_ok=True)
    for path in sorted(root.rglob('*.jsonl')):
        if path.is_symlink() or any((root/p).is_symlink() for p in path.relative_to(root).parents if p!=Path('.')):
            skipped+=1;continue
        st=path.stat()
        if st.st_size>128*1024*1024 or used+st.st_size>budget:
            skipped+=1;continue
        data=path.read_bytes()
        # Copy one immutable complete prefix; a concurrent incomplete tail stays
        # outside this evidence set, and its presence is reported explicitly.
        if not data.endswith(b'\n'):
            skipped+=1;continue
        try:
            rows=[json.loads(line) for line in data.splitlines() if line.strip()]
        except (ValueError,TypeError):
            skipped+=1;continue
        if kind=='pi':
            count=sum(r.get('type')=='message' and isinstance(r.get('message'),dict)
                and r['message'].get('role')=='assistant' and isinstance(r['message'].get('usage'),dict) for r in rows)
        else:
            count=sum(isinstance(r.get('payload'),dict) and r['payload'].get('type')=='token_count' for r in rows)
        if not count: continue
        dest=target/path.relative_to(root)
        private_file(dest,data)
        copied+=1; observations+=count;used+=len(data);digests.append(sha256(data).hexdigest())
    return {'copied_files':copied,'skipped_files':skipped,'status':'bounded_private_copy',
            'bytes':used,'observations':observations,'set_sha256':sha256(canonical(sorted(digests))).hexdigest()}


def run(work):
    home=Path.home();stats=home/'.local/state/pulse/stats'
    registry=json.loads((home/'.local/state/pulse/compute-cost/sources.json').read_text())
    production=UsageLedger(Path(registry['ledger_root']))
    snapshot=production.receipt_snapshot()
    now=datetime.now(timezone.utc).isoformat()
    # Rebuild only the existing validated snapshot in a PRIVATE ledger, keeping
    # original fact identities. Nothing is written back to its source ledger.
    for peer in ('vultr','tcloud'):
        file=stats/'imports'/f'{peer}.jsonl'
        if not file.is_file():
            log('TRANSPORT',{'peer':peer,'status':'not_available'});continue
        raw=file.read_bytes()
        if len(raw)>64*1024*1024: raise ValueError('bounded transport size exceeded')
        rows=[Receipt.from_dict(json.loads(line)) for line in raw.splitlines() if line.strip()]
        transport=work/f'{peer}.jsonl';private_file(transport,raw)
        reg={'schema':'computecost_source_registry/v1','sources':[{'id':'stats-transport-'+peer,'kind':'receipts','path':str(transport)}]}
        isolated=UsageLedger(work/f'{peer}-existing');isolated.ingest_receipts(snapshot['facts'])
        old=isolated.receipt_snapshot()
        first=collect_registered_sources(reg,isolated)
        repeats=[collect_registered_sources(reg,isolated) for _ in range(2)]
        assert all(r['inserted']==0 for r in repeats), 'transport repeat changed facts'
        assert isolated.receipt_snapshot(watermark=old['watermark'])['sha256']==old['sha256']
        fresh=UsageLedger(work/f'{peer}-fresh')
        # A source ledger snapshot is ordered; use its real complete prefix as
        # a prior read, then append the rest to prove growing import.
        split=max(1,len(rows)//2)
        transport.write_bytes(b''.join(canonical(r.to_dict())+b'\n' for r in rows[:split]))
        collect_registered_sources(reg,fresh)
        prefix=fresh.receipt_snapshot()
        transport.write_bytes(raw)
        growth=collect_registered_sources(reg,fresh)
        again=collect_registered_sources(reg,fresh)
        assert again['inserted']==0
        assert fresh.receipt_snapshot(watermark=prefix['watermark'])['sha256']==prefix['sha256']
        log('TRANSPORT',{'peer':peer,'real_source_receipts':len(rows),'source_sha256':sha256(raw).hexdigest(),
            'existing_ledger_facts':len(snapshot['facts']),'inserted':first['inserted'],
            'repeat_inserted':[r['inserted'] for r in repeats], 'real_prefix_growth_inserted':growth['inserted'],
            'old_watermark_unchanged':True})
    native={}
    sources=[('pi',home/'.pi/agent/sessions','pi'),
             ('codex',home/'.codex/sessions/2026/09','codex-active'),
             ('codex',home/'.codex/archived_sessions','codex-archive')]
    budget=2*1024*1024*1024
    for kind,original,name in sources:
        dest=work/name
        report=frozen_native(original,dest,min(budget,1024*1024*1024),kind)
        budget-=report['bytes']
        if report['copied_files']: native.setdefault(kind,[]).append(str(dest))
        log('NATIVE_SOURCE',{'source':name,**report})
    if not native: raise ValueError('no native real source copied')
    ledger=UsageLedger(work/'native-ledger')
    candidate=candidate_registry({'schema':'computecost_source_registry/v1','ledger_root':str(ledger.root),'sources':[]},native)
    # Count adapter output separately from original observations and transactions.
    parsed=[]
    for root in native.get('pi',[]):
        for path in Path(root).rglob('*.jsonl'): parsed.extend(read_pi(path,now))
    if native.get('codex'): parsed.extend(read_codex([Path(p) for p in native['codex']],now))
    collection=collect_registered_sources(candidate,ledger)
    frozen=ledger.receipt_snapshot()
    second=collect_registered_sources(candidate,ledger)
    assert second['inserted']==0, 'native replay not idempotent'
    assert ledger.receipt_snapshot(watermark=frozen['watermark'])['sha256']==frozen['sha256']
    catalog_snapshot=json.loads((stats/'openrouter-catalog.json').read_text())
    catalog=catalog_snapshot['catalog'];catalog_at=catalog_snapshot['retrieved_at']
    periods=[('day','2026-09-13','2026-09-14'),('week','2026-09-07','2026-09-14'),('month','2026-09-01','2026-10-01')]
    for label,start,end in periods:
        query=CollaborationQuery(scope='activity',from_date=start,to_date=end,timezone='Asia/Shanghai',group_by=('runtime',),watermark=frozen['watermark'])
        result=query_collaboration(ledger,query,catalog=catalog,catalog_observed_at=catalog_at)
        usage=result['summary']['usage']; cost=result['summary']['cost']
        log('PERIOD_QUERY',{'period':label,'watermark':result.get('watermark'),
            'events':usage['event_count'],'known_total_tokens':usage['known_total_tokens'],
            'tokens':usage['tokens'], 'partial_machine_cash_usd':cost['partial_machine_cash_usd'],
            'machine_cash_usd':cost['machine_cash_usd'], 'reasons':cost['reasons'],
            'unpriced_count':len(cost['unpriced']), 'group_count':len(result.get('groups',[]))})
        for runtime in ('pi','codex'):
            subset=query_collaboration(ledger,CollaborationQuery(scope='activity',from_date=start,to_date=end,
                timezone='Asia/Shanghai',runtime=runtime,watermark=frozen['watermark']),catalog=catalog,catalog_observed_at=catalog_at)
            log('RUNTIME_PERIOD',{'runtime':runtime,'period':label,
                'events':subset['summary']['usage']['event_count'],
                'known_total_tokens':subset['summary']['usage']['known_total_tokens'],
                'partial_machine_cash_usd':subset['summary']['cost']['partial_machine_cash_usd']})
            if label=='month' and runtime=='codex' and native.get('codex'):
                assert subset['summary']['usage']['event_count']>0, 'real Codex not in month query'
        if label=='month':
            assert result['summary']['usage']['event_count']>0
            assert result['summary']['cost']['partial_machine_cash_usd'] is not None
    document=build_dashboard(ledger,subscriptions=json.loads((ROOT/'ComputeCost/config/procurement-facts.json').read_text()),
        source_revision=os.environ['SOURCE_SHA'],catalog_snapshot=catalog_snapshot)
    assert len(document['views'])==9
    log('NATIVE_RECONCILIATION',{'adapter_usage_receipts':len(parsed),'by_runtime':dict(Counter(r.payload['runtime'] for r in parsed)),
        'ledger_inserted_facts':collection['inserted'],'repeat_inserted':second['inserted'],
        'query_views':len(document['views']),'source_set_is_bounded':True,'production_changed':False})
    private_file(work/'private-dashboard.json',canonical(document))
    log('REPLAY_FINISHED',{'source_revision':os.environ['SOURCE_SHA'],'production_changed':False,'no_provider_calls':True})

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',required=True,type=Path)
    args=parser.parse_args()
    if not args.work.is_dir() or args.work.is_symlink() or args.work.stat().st_mode & 0o077:
        raise SystemExit('private work directory required')
    run(args.work)
