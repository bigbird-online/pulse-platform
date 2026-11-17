#!/usr/bin/env python3
"""Sample the actual installed collector/publication path in a private ledger copy.

No model/provider calls, source changes, production writes or raw content output.
Unlike per-component UAT, keep the publisher's exact derived registry and imports.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json
import plistlib
import subprocess
import os
from pathlib import Path
import shutil
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'ComputeCost'), str(ROOT), str(ROOT / 'Stats/exporter')]
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.stats_dashboard import build_dashboard, DEFAULT_FACTS
from pulse_compute_cost.stats_dashboard_wire import dashboard_bytes
from pulse_compute_cost.catalog_source import load_catalog_snapshot
from pulse_compute_cost.receipts import canonical
from pulse_compute_cost.source_layout import source_has_read_failure


def scheduler_probe(output: Path):
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location('stats_deploy_probe', ROOT / 'Stats/deploy.py')
    deploy = module_from_spec(spec); spec.loader.exec_module(deploy)
    code = """import json,hashlib,time,os,subprocess
p={'tokens':{'uncached_input':127,'cache_read':349440,'output':923,'cache_write':0},'model':'synthetic/test','wi':None}
s=json.dumps(p,sort_keys=True,separators=(',',':'));start=time.monotonic()
for i in range(400000):
 d=json.loads(s);result=hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest()
print(json.dumps({'seconds':round(time.monotonic()-start,3),'result':result,'priority':subprocess.run(['ps','-o','pri=','-p',str(os.getpid())],capture_output=True,text=True).stdout.strip()}),flush=True)
"""
    worker = output / 'qos-worker.py'; worker.write_text(code)
    reports = {}
    domain = 'gui/' + str(os.getuid())
    for mode in ('background', 'standard'):
        label = 'io.coevolution.pulse.stats.qos-uat.' + os.environ['GITHUB_RUN_ID'] + '.' + mode
        log = output / ('qos-' + mode + '.json')
        job = {'Label':label, 'ProgramArguments':[sys.executable,'-B',str(worker)],
               'RunAtLoad':True, 'ProcessType':'Background',
               'StandardOutPath':str(log), 'StandardErrorPath':str(output / ('qos-' + mode + '.err'))}
        if mode == 'standard':
            job = deploy.publisher_launchagent(job)
        plist = output / (mode + '.plist'); plist.write_bytes(plistlib.dumps(job))
        loaded = False
        try:
            subprocess.run(['/bin/launchctl','bootstrap',domain,str(plist)],check=True,capture_output=True,timeout=10)
            loaded = True; deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if log.exists() and log.stat().st_size:
                    reports[mode] = json.loads(log.read_bytes()); break
                time.sleep(0.1)
            else:
                raise TimeoutError('isolated launchd policy probe did not finish')
        finally:
            if loaded:
                subprocess.run(['/bin/launchctl','bootout',domain+'/'+label],check=True,capture_output=True,timeout=10)
    assert reports['background']['result'] == reports['standard']['result']
    print(json.dumps({'scheduler_policy_probe':reports,'business_data_read':False}),flush=True)
    return reports



def performance_week_diagnosis(view: dict) -> dict:
    """Keep moving-week attribution gaps visible without making them a perf result.

    This profile blocks on pricing/time/conflict/source-read regressions. The
    stricter zero-unknown attribution gate remains verify_weekly_activity and
    is used by attribution/deployment acceptance when that claim is intended.
    """
    usage=view['summary']['usage']; cost=view['summary']['cost']; groups=view['groups']
    dimensions=('agent','wi','entrypoint','node','runtime','model')
    missing={key:sum(g['usage']['event_count'] for g in groups
                     if g['key'].get(key) in {None,'unassigned','ambiguous'})
             for key in dimensions}
    source_errors=[row for row in usage['source_details'] if source_has_read_failure(row)]
    hard={'unpriced':len(cost['unpriced']),
          'time_unallocated':usage['time_unallocated_event_count'],
          'conflicts':usage['conflicted_event_count'],
          'source_errors':len(source_errors)}
    if any(hard.values()):
        raise AssertionError('performance profile found non-attribution data regression: '+json.dumps(hard,sort_keys=True))
    return {'strict_week_accepted':not any(missing.values()),'missing':missing,**hard}


def copy_registered_receipt_source(path: Path, target: Path) -> None:
    """Copy one registered transport source into Actions-private evidence.

    Production receipt registrations may be either a single JSONL transport or
    a private directory of bounded JSONL facts (for example ChatGPT web
    coverage). The profile must preserve that shape without following symlinks.
    """
    if path.is_symlink() or not path.exists():
        raise ValueError('registered receipt profile source is unsafe or missing')
    if path.is_file():
        shutil.copy2(path, target); target.chmod(0o600); return
    if not path.is_dir():
        raise ValueError('registered receipt profile source has unsupported type')
    for item in path.rglob('*'):
        if item.is_symlink():
            raise ValueError('registered receipt profile source contains symlink')
    shutil.copytree(path, target)
    target.chmod(0o700)
    for item in target.rglob('*'):
        item.chmod(0o700 if item.is_dir() else 0o600)

def run(output: Path):
    allowed = Path(os.environ['RUNNER_TEMP']).resolve()
    if output.exists() or not output.resolve().is_relative_to(allowed) or output.resolve() == allowed:
        raise ValueError('new Actions-private output required')
    output.mkdir(parents=True, mode=0o700)
    qos = scheduler_probe(output)
    state = Path.home() / '.local/state/pulse/stats'
    registry = json.loads((state / 'collector-sources.json').read_bytes())
    original = Path(registry['ledger_root'])
    ledger_dir = output / 'ledger'
    started = time.monotonic()
    with (original / '.usage-ledger.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        shutil.copytree(original, ledger_dir)
    registry['ledger_root'] = str(ledger_dir)
    for number, source in enumerate(registry['sources']):
        if source['kind'] == 'receipts':
            path = Path(source['path'])
            if path.exists():
                target = output / ('import-' + str(number) + ('.jsonl' if path.is_file() else ''))
                copy_registered_receipt_source(path, target)
                source['path'] = str(target)
    catalog_path = Path(registry['catalog_snapshot']) if registry.get('catalog_snapshot') else state / 'openrouter-catalog.json'
    catalog = load_catalog_snapshot(catalog_path)
    report = {'revision': os.environ['PULSE_SOURCE_REVISION'], 'production_modified': False,
              'copy_seconds': round(time.monotonic()-started, 3), 'scheduler':qos, 'stages': []}
    ledger = UsageLedger(ledger_dir)
    main_thread = threading.get_ident()
    stop = threading.Event()
    current = ['setup']
    samples = Counter()
    def sample():
        last = time.monotonic()
        while not stop.wait(0.05):
            frame = sys._current_frames().get(main_thread)
            stack = []
            while frame is not None:
                filename = frame.f_code.co_filename
                if '/pulse_compute_cost/' in filename:
                    stack.append(Path(filename).name + ':' + frame.f_code.co_name)
                frame = frame.f_back
            if stack:
                samples[(current[0], ' <- '.join(stack[:5]))] += 1
            if time.monotonic() - last >= 30:
                print(json.dumps({'stage': current[0], 'sampled_hotspots': samples.most_common(8)}), flush=True)
                last = time.monotonic()
    worker = threading.Thread(target=sample, daemon=True)
    worker.start()
    try:
        for mode in ('cold', 'warm'):
            current[0] = 'collect_' + mode
            start = time.monotonic()
            collected = collect_registered_sources(registry, ledger)
            entry = {'stage': current[0], 'seconds': round(time.monotonic()-start, 3),
                     'watermark': collected['watermark'], 'inserted': collected['inserted'],
                     'parser_cache': collected.get('parser_cache')}
            report['stages'].append(entry)
            print(json.dumps(entry), flush=True)
            current[0] = 'build_' + mode
            start = time.monotonic()
            doc = build_dashboard(ledger, subscriptions=json.loads(DEFAULT_FACTS.read_bytes()),
                source_revision=report['revision'], now=datetime.now(timezone.utc),
                catalog_snapshot=catalog, identity_dimensions=True, activity_table=True, activity_only=True,
                calculation_cache=output / 'calculation-cache')
            payload_bytes=len(dashboard_bytes(doc))
            assert doc['status'] == 'available' and len(doc['views']) == 3 and payload_bytes <= 512*1024
            week = next(v for v in doc['views'] if v['period'] == 'week' and v['dimension'] == 'activity')
            entry = {'stage': current[0], 'seconds': round(time.monotonic()-start, 3),
                     'publication_bytes':payload_bytes,'publication_headroom_bytes':512*1024-payload_bytes,
                     'events': week['summary']['usage']['event_count'],
                     'amount': week['summary']['cost']['partial_machine_cash_usd'],
                     'report_sha256': week['report_sha256'],
                     'weekly_diagnosis':performance_week_diagnosis(week)}
            report['stages'].append(entry)
            print(json.dumps(entry), flush=True)
    finally:
        stop.set()
        worker.join(timeout=2)
        report['hotspots'] = samples.most_common(30)
        (output / 'summary.json').write_bytes(canonical(report))
        shutil.rmtree(ledger_dir)
        shutil.rmtree(output / 'calculation-cache', ignore_errors=True)
        for path in output.glob('import-*.jsonl'):
            path.unlink()
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    run(parser.parse_args().output)
