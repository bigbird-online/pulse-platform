#!/usr/bin/env python3
"""Read the registered Harness documents; no ingestion or production mutation."""
from pathlib import Path
import argparse
from collections import Counter
import hashlib
import json
import sys
from datetime import datetime, timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pulse_compute_cost.receipt_sources import read_harness_root
from pulse_compute_cost.source_layout import source_detail, manifest_paths
from pulse_compute_cost.receipts import digest


def inspect(root, label):
    if not root.is_dir() or root.is_symlink():
        raise ValueError('registered Harness root unavailable')
    note=root/'evidence/measurements/wi_20260820_272/terminal-authority-reconciliation.yaml'
    before=hashlib.sha256(note.read_bytes()).hexdigest() if note.is_file() else None
    rows=[];detail=source_detail({'id':label,'kind':'harness_root','label':label})
    files=read_harness_root(root,datetime.now(timezone.utc).isoformat(),rows,detail=detail)
    after=hashlib.sha256(note.read_bytes()).hexdigest() if note.is_file() else None
    assert before==after, 'historical source bytes changed during read'
    report={'label':label,**detail,'valid_facts':len(rows),'fact_kinds':dict(Counter(r.kind for r in rows)),
            'reference_sha256':after,'reference_in_inventory':before is None or note in files,
            'later_wi730_read':any('wi_20260822_730' in p.parts for p in files),
            'observed_at':datetime.now(timezone.utc).isoformat(),'ledger_modified':False}
    print(json.dumps(report,sort_keys=True),flush=True)
    if detail['files_invalid'] or not report['reference_in_inventory']:
        raise ValueError('Harness source still has invalid documents')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path);p.add_argument('--label',default='registered.harness_root')
    args=p.parse_args()
    if args.root:
        inspect(args.root,args.label);return
    registry=json.loads((Path.home()/'.local/state/pulse/compute-cost/sources.json').read_bytes())
    sources=[s for s in registry['sources'] if s['kind']=='harness_root']
    if len(sources)!=1:raise ValueError('one registered Harness root required')
    source=sources[0];roots=manifest_paths(source) or [Path(source['path'])]
    for root in roots:inspect(root,source.get('label',source['id']))

if __name__=='__main__':main()
