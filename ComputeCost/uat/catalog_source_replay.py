#!/usr/bin/env python3
"""Read-only price-input UAT on a frozen ledger copy, not a whole-WI acceptance.

Run only in an Actions temporary directory. No collection, price fetch, source
registration, deployment or production write. Only bounded summaries leave it.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ComputeCost'))
from pulse_compute_cost.catalog_source import managed_state_root, resolve_catalog_path, load_catalog_snapshot
from pulse_compute_cost.stats_dashboard_wire import expand_dashboard


def sha(data):
    return hashlib.sha256(data).hexdigest()


def summarize(report):
    u, c = report['summary']['usage'], report['summary']['cost']
    return {'watermark': report['watermark'], 'report_sha256': report['report_sha256'],
            'catalog_sha256': report['catalog_sha256'], 'catalog_observed_at': report['catalog_observed_at'],
            'events': u['event_count'], 'known_tokens': u['known_total_tokens'],
            'partial_usd': c['partial_machine_cash_usd'], 'complete_usd': c['machine_cash_usd'],
            'unpriced_events': len(c['unpriced']), 'cost_reasons': c['reasons'],
            'quality_status': report['summary']['quality']['status'],
            'roi': report['summary']['roi']['ratio']}


def verify_restored_source_diagnostics(before, after, facts):
    """Only permit the independently evidenced repair of node-local ID clashes."""
    from pulse_compute_cost.receipts import current_receipts, reference
    previous = {row['source_ref']: row for row in before}
    current = {row['source_ref']: row for row in after}
    assert len(previous) == len(before) and len(current) == len(after), 'duplicate source diagnostics'
    assert all(current.get(key) == value for key, value in previous.items()), 'existing source diagnostics changed'
    _, conflicts = current_receipts(facts)
    expected = {}
    rank = {'unreachable': 5, 'partial': 4, 'unknown': 3, 'complete': 2, 'idle': 1, 'excluded': 0}
    for heads in conflicts.values():
        sources = [r for r in heads if r.kind == 'coverage' and r.producer == 'registered_collector.v1'
                   and r.payload.get('wi') is None and (r.payload.get('source_detail') or {}).get('kind') == 'harness_root']
        labels = {(r.payload.get('source_detail') or {}).get('label') for r in sources}
        if len(labels) < 2 or any(not isinstance(label, str) or '.' not in label for label in labels):
            continue
        for row in sources:
            payload = row.payload; detail = payload['source_detail']
            key = reference('qualified_collector_source', [payload['source_ref'], detail['label']])
            value = dict(detail, source_ref=key, status=payload['status'], reason=payload['reason'])
            if key not in expected or rank[value['status']] > rank[expected[key]['status']]:
                expected[key] = value
    additions = {key: value for key, value in current.items() if key not in previous}
    assert additions == expected, 'new diagnostics must exactly match retained conflicting node records'
    return sorted(row['label'] for row in additions.values())


def verify_published_projection(before, after, facts):
    """Keep exact quantities/money; prove, rather than freeze, old diagnostics."""
    for field in ('event_count', 'known_total_tokens', 'tokens'):
        assert before['usage'][field] == after['usage'][field], ('published usage changed', field)
    for field in ('machine_cash_usd', 'partial_machine_cash_usd', 'cash_basis', 'unpriced'):
        assert before['cost'][field] == after['cost'][field], ('published amount changed', field)
    old_reasons = set(before['cost']['reasons']); new_reasons = set(after['cost']['reasons'])
    removed = old_reasons - new_reasons
    assert not new_reasons - old_reasons, 'unexpected diagnostic addition'
    assert removed <= {'execution_evidence_conflict', 'time_unallocated'}, 'unproved diagnostic removal'
    if 'execution_evidence_conflict' in removed:
        assert verify_restored_source_diagnostics(before['usage']['source_details'],
            after['usage']['source_details'], facts)
    if 'time_unallocated' in removed:
        assert before['usage']['time_unallocated_event_count'] > 0
        assert after['usage']['time_unallocated_event_count'] == 0
    return sorted(removed)


def replay(work: Path, wi: str):
    if not re.fullmatch(r'wi_\d{8}_\d{3}', wi):
        raise ValueError('one explicit WI required')
    allowed = Path(os.environ['RUNNER_TEMP']).resolve()
    if work.exists() or not work.resolve().is_relative_to(allowed) or work.resolve() == allowed:
        raise ValueError('new Actions-private output required')
    work.mkdir(mode=0o700, parents=True)
    state = managed_state_root()
    registry_path = state / 'compute-cost/sources.json'
    registry_bytes = registry_path.read_bytes(); registry = json.loads(registry_bytes)
    catalog_path = resolve_catalog_path(registry=registry, registry_path=registry_path)
    if catalog_path is None:
        raise ValueError('saved price evidence must exist for this UAT')
    catalog_bytes = catalog_path.read_bytes(); load_catalog_snapshot(catalog_path)
    catalog_copy = work / 'catalog.json'; catalog_copy.write_bytes(catalog_bytes); catalog_copy.chmod(0o600)
    publication_path = state / 'stats/cost-dashboard-v1.json'
    publication_bytes = publication_path.read_bytes()
    publication = expand_dashboard(json.loads(publication_bytes))
    (work / 'publication.json').write_bytes(publication_bytes)
    (work / 'publication.json').chmod(0o600)
    view = next(v for v in publication['views'] if v['period'] == 'week' and v['dimension'] == 'wi')
    watermark = view['watermark']
    production = Path(registry['ledger_root']); copied = work / 'ledger'; (copied / 'receipts').mkdir(parents=True, mode=0o700)
    # Lock and copy only already-committed transactions up to the published watermark.
    with (production / '.usage-ledger.lock').open('rb') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        for number in range(1, watermark + 1):
            path = production / 'receipts' / f'{number:020d}.json'
            if path.is_symlink(): raise ValueError('unexpected receipt symlink')
            shutil.copy2(path, copied / 'receipts' / path.name)
    original_copy = {p.name: sha(p.read_bytes()) for p in (copied / 'receipts').glob('*.json')}
    env = dict(os.environ, PULSE_COMPUTECOST_SOURCES=str(registry_path), PYTHONDONTWRITEBYTECODE='1')
    def command(source, args):
        child_env = dict(env, PYTHONPATH=str(source / 'ComputeCost'))
        argv = [sys.executable, '-B', '-m', 'pulse_compute_cost', *args,
                '--ledger-root', str(copied), '--watermark', str(watermark)]
        process = subprocess.run(argv, env=child_env, capture_output=True, timeout=240)
        if process.returncode:
            raise RuntimeError('CLI failed; stderr sha256=' + sha(process.stderr))
        return json.loads(process.stdout)
    # Derive HOME via the same OS identity as managed_state_root, not the job's HOME env.
    baseline = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/share/pulse/stats/current'
    old = command(baseline, [wi])
    fixed = command(ROOT, [wi])
    explicit = command(ROOT, [wi, '--catalog-snapshot', str(catalog_copy)])
    alias = command(ROOT, ['wi-report', wi])
    assert fixed == explicit == alias, 'default, explicit and wi-report must be identical'
    assert fixed['summary']['cost']['partial_machine_cash_usd'] is not None, 'known subset must be priced'
    assert fixed['summary']['cost']['unpriced'] == explicit['summary']['cost']['unpriced']
    assert old['facts_sha256'] == fixed['facts_sha256'], 'price selection cannot rewrite usage'
    # Identity-priced subtotals may legitimately gain a value; quantities and
    # membership must remain identical to the original unpriced read.
    usage_differences = {key: {'installed': value, 'candidate': fixed['summary']['usage'].get(key)}
                         for key, value in old['summary']['usage'].items()
                         if not key.endswith('_priced_usd') and fixed['summary']['usage'].get(key) != value}
    if usage_differences:
        print('CATALOG_USAGE_DIFFERENCES ' + json.dumps(usage_differences, sort_keys=True), flush=True)
    restored_sources = []
    if 'source_details' in usage_differences:
        from pulse_compute_cost.ledger import UsageLedger
        restored_sources = verify_restored_source_diagnostics(
            old['summary']['usage']['source_details'], fixed['summary']['usage']['source_details'],
            UsageLedger(copied).receipt_snapshot(watermark=watermark)['facts'])
        del usage_differences['source_details']
    assert not usage_differences, 'quantity, membership or non-diagnostic usage fields changed'
    assert old['summary']['quality'] == fixed['summary']['quality']
    q = view['query']
    week = command(ROOT, ['query', '--work-item', wi, '--scope', 'activity', '--group-by', 'wi',
                         '--from', q['from_date'], '--to', q['to_date'], '--timezone', q['timezone']])
    group = next(g for g in view['groups'] if g['key'] == {'wi': wi})
    assert week['catalog_sha256'] == view['catalog_sha256']
    assert week['catalog_observed_at'] == view['catalog_observed_at']
    for key in ('machine_cash_usd', 'partial_machine_cash_usd'):
        assert week['summary']['cost'][key] == group['cost'][key], 'published Stats price differs'
    for key in ('event_count', 'known_total_tokens', 'tokens'):
        assert week['summary']['usage'][key] == group['usage'][key]
    assert registry_path.read_bytes() == registry_bytes, 'registry changed during UAT'
    assert catalog_path.read_bytes() == catalog_bytes, 'catalog changed during UAT'
    assert original_copy == {p.name: sha(p.read_bytes()) for p in (copied / 'receipts').glob('*.json')}
    result = {'work_item': wi, 'candidate_revision': os.environ['PULSE_SOURCE_REVISION'],
              'baseline_revision': (baseline / '.stats-source-revision').read_text().strip(),
              'observed_at': datetime.now(timezone.utc).isoformat(), 'watermark': watermark,
              'frozen_facts_sha256': fixed['facts_sha256'], 'catalog_file_sha256': sha(catalog_bytes),
              'baseline': summarize(old), 'default': summarize(fixed), 'week': summarize(week),
              'default_equals_explicit_and_alias': True, 'published_stats_week_equal': True,
              'published_source_revision': publication['source_revision'],
              'publication_changed_during_read': publication_path.read_bytes() != publication_bytes,
              'usage_quality_unchanged': True, 'restored_source_diagnostics': restored_sources,
              'registry_catalog_copy_unchanged': True,
              'production_writes': False, 'whole_wi_completeness_claimed': False}
    (work / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--work-item', required=True)
    a = p.parse_args(); replay(a.output, a.work_item)
