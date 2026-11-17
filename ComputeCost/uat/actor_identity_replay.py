#!/usr/bin/env python3
"""Read-only business replay of the installed monetary set into a private ledger.

Uses existing registered native logs and workspace registry. Only actor facts and explicitly proven paginated counter corrections
are added to the private copy; original assertions remain in history. No model call, source reconfiguration, production
ledger write, current-WI inference or prompt output is performed.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'ComputeCost'), str(ROOT / 'Stats/exporter')]
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import canonical, digest, current_receipts
from pulse_compute_cost.receipt_sources import read_codex, paginated_counter_correction
from pulse_compute_cost.source_registration import actor_candidate_registry
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration, _value_cost
from pulse_compute_cost.stats_dashboard import build_dashboard, publish_dashboard, DEFAULT_FACTS
from pulse_compute_cost.stats_dashboard_wire import expand_dashboard
from pulse_stats.cost_dashboard import load_cost_dashboard


def verify_task_expectations(groups: list, expectations: list) -> list:
    """Assert only caller-declared fields; always retain the full observed context.

    WI attribution and source/channel completeness are different checks. A
    caller supplying node/model/entrypoint still requires their exact values.
    """
    verified = []
    for expected in expectations:
        if (not isinstance(expected, dict)
                or not {'agent', 'runtime', 'wi'} <= set(expected) <= {'agent', 'node', 'runtime', 'model', 'entrypoint', 'wi'}
                or not __import__('re').fullmatch(r'wi_\d{8}_\d{3}', str(expected.get('wi')))):
            raise ValueError('expected task requires Agent, Runtime and canonical WI')
        selectors = ('agent', 'runtime', 'node') if 'node' in expected else ('agent', 'runtime')
        actual = [g for g in groups if g['usage']['event_count'] > 0
                  and all(g['key'][k] == expected[k] for k in selectors)]
        assert actual and all(all(g['key'][k] == value for k, value in expected.items()) for g in actual), 'business path does not match the confirmed task'
        verified.append({'expected': expected, 'events': sum(g['usage']['event_count'] for g in actual),
                         'observed': [g['key'] for g in actual]})
    return verified


def weekly_monetary_view(document: dict) -> dict:
    """Select the current joint activity fact, never reconstruct it from marginals."""
    matches = [v for v in document.get('views', [])
               if v.get('period') == 'week' and v.get('dimension') == 'activity']
    if len(matches) != 1:
        raise ValueError('current weekly joint activity view is required')
    return matches[0]


def bounded_baseline(summary: dict) -> dict:
    """Report old-vs-candidate semantics without making old diagnostics authoritative."""
    usage, cost = summary['usage'], summary['cost']
    return {
        'events': usage['event_count'],
        'known_tokens': usage['known_total_tokens'],
        'conflicted_events': usage['conflicted_event_count'],
        'agent_unlinked': usage['agent_unassigned_event_count'],
        'wi_unlinked': usage['wi_unassigned_event_count'],
        'time_unallocated': usage['time_unallocated_event_count'],
        'partial_usd': cost['partial_machine_cash_usd'],
        'complete_usd': cost['machine_cash_usd'],
        'unpriced_events': len(cost['unpriced']),
        'cost_reasons': cost['reasons'],
    }


def replay(output: Path, revision: str, *, require_task_links: bool = False, expected_tasks: list | None = None):
    home = Path.home()
    state = home / '.local/state/pulse/stats'
    registry_path = home / '.local/state/pulse/compute-cost/sources.json'
    before_registry = registry_path.read_bytes()
    registry = json.loads(before_registry)
    published = expand_dashboard(json.loads((state / 'cost-dashboard-v1.json').read_bytes()))
    view = weekly_monetary_view(published)
    catalog = json.loads((state / 'openrouter-catalog.json').read_bytes())
    if digest(catalog['catalog']) != view['catalog_sha256']:
        raise ValueError('published monetary set uses a different price catalog')
    production = Path(registry['ledger_root'])
    if not production.is_dir():
        raise ValueError('registered ledger must already exist')
    original = UsageLedger(production)
    snapshot = original.receipt_snapshot(watermark=view['watermark'])
    active, _ = current_receipts(snapshot['facts'])
    targets = {r.entity_ref: r for r in active if r.kind == 'usage' and r.payload['runtime'] == 'codex'}
    surfaces = json.loads((ROOT / 'config/scl-surfaces.yaml').read_text())
    candidate = actor_candidate_registry(registry, surfaces, site='ownermac')
    now = datetime.now(timezone.utc)
    actors, tasks, corrections = [], [], []
    native_files = {}
    for source in candidate['sources']:
        if source['kind'] != 'codex':
            continue
        paths = [Path(p) for p in source.get('paths', [source.get('path')])]
        for directory in paths:
            for path in directory.rglob('*.jsonl'):
                if path.is_symlink():
                    raise ValueError('native source cannot be a symlink')
                native_files[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        diagnostics = []
        native = read_codex(paths, now.isoformat(), source_identity=source, diagnostic_out=diagnostics)
        if diagnostics:
            print(json.dumps({'native_readback_diagnostics': sorted(set(diagnostics))}), flush=True)
        actors.extend(r for r in native if r.kind == 'attribution' and r.payload.get('scope') == 'actor'
                      and r.payload['target_ref'] in targets)
        tasks.extend(r for r in native if r.kind == 'attribution' and r.payload.get('scope') != 'actor'
                     and r.payload['target_ref'] in targets)
        for row in native:
            if row.kind == 'usage' and row.entity_ref in targets:
                prior = targets[row.entity_ref]
                corrected = paginated_counter_correction(prior, row)
                if corrected is not None:
                    if corrected.payload['native_tokens'] != prior.payload['native_tokens'] or corrected.occurred_at != prior.occurred_at:
                        raise ValueError('native source advanced beyond the frozen publication; a new snapshot is required')
                    corrections.append(corrected)
    if not actors:
        raise ValueError('no actor evidence for the actual registered Codex set')
    if output.exists() or not output.is_absolute():
        raise ValueError('new private output path required')
    output.mkdir(mode=0o700, parents=True)
    with tempfile.TemporaryDirectory(prefix='ledger-', dir=output) as temp:
        ledger = UsageLedger(temp)
        ledger.ingest_receipts(snapshot['facts'])
        before = ledger.receipt_snapshot()
        q = dict(view['query']); q['watermark'] = before['watermark']
        q['group_by'] = tuple(q['group_by']); q['work_items'] = tuple(q['work_items'])
        options = {'catalog': catalog['catalog'], 'catalog_observed_at': catalog['retrieved_at']}
        base = query_collaboration(ledger, CollaborationQuery(**q), **options)
        # This replay validates actor/task enrichment, not compatibility with the
        # previously installed query engine. A reconciliation repair may safely
        # reinterpret the same immutable facts (for example by resolving old
        # Host/native join conflicts), so the old published summary is diagnostic
        # context only. Quantity/money conservation is enforced below from this
        # candidate baseline through the actor/task projection itself.
        baseline_comparison = {
            'published': bounded_baseline(view['summary']),
            'candidate': bounded_baseline(base['summary']),
        }
        print(json.dumps({'baseline_period': 'week', 'baseline_comparison': baseline_comparison}), flush=True)
        ledger.ingest_receipts(corrections + actors + tasks)
        after = ledger.receipt_snapshot()
        q['watermark'] = after['watermark']
        q['group_by'] = ('agent', 'node', 'runtime', 'model', 'entrypoint', 'wi')
        result = query_collaboration(ledger, CollaborationQuery(**q), **options)
        from decimal import Decimal
        assert result['summary']['usage']['event_count'] == base['summary']['usage']['event_count']
        expected_tokens = {k:base['summary']['usage']['tokens'][k]['known'] for k in ('uncached_input','cache_read','cache_write','output')}
        expected_amount = Decimal(base['summary']['cost']['partial_machine_cash_usd'] or base['summary']['cost']['machine_cash_usd'])
        for corrected in corrections:
            prior = targets[corrected.entity_ref]
            if prior.fact_ref not in base['summary']['usage']['fact_refs']:
                continue
            for key in expected_tokens:
                expected_tokens[key] += corrected.payload['tokens'][key] - prior.payload['tokens'][key]
            old_amount, old_reason = _value_cost(prior,catalog['catalog'])
            new_amount, new_reason = _value_cost(corrected,catalog['catalog'])
            assert old_reason is None and new_reason is None
            expected_amount += new_amount - old_amount
        assert {k:v['known'] for k,v in result['summary']['usage']['tokens'].items()} == expected_tokens
        assert result['summary']['usage']['known_total_tokens'] == sum(expected_tokens.values())
        assert Decimal(result['summary']['cost']['partial_machine_cash_usd'] or result['summary']['cost']['machine_cash_usd']) == expected_amount
        old_usage = [r.to_dict() for r in before['facts'] if r.kind == 'usage']
        new_usage = [r.to_dict() for r in after['facts'] if r.kind == 'usage']
        new_by_ref = {r["fact_ref"]:r for r in new_usage}
        assert all(new_by_ref[r["fact_ref"]] == r for r in old_usage)
        assert ledger.receipt_snapshot(watermark=before['watermark'])['sha256'] == before['sha256']
        assert ledger.ingest_receipts(corrections + actors + tasks)['inserted'] == 0
        local = [g for g in result['groups'] if g['key']['agent'] == 'pulse'
                 and g['key']['node'] == 'ownermac' and g['key']['runtime'] == 'codex'
                 and g['key']['entrypoint'] == 'codex_app' and g['usage']['event_count'] > 0]
        if not local:
            raise AssertionError('actual Codex startup workspace did not identify Local Pulse')
        proved_targets = {r.payload['target_ref'] for r in actors if r.payload['executor']=='pulse'
                         and r.payload['context']['node']=='ownermac' and r.payload['context']['entrypoint']=='codex_app'}
        proved_scopes = {targets[key].payload['native_scope_ref'] for key in proved_targets}
        expected_local = [r for r in targets.values() if r.fact_ref in base['summary']['usage']['fact_refs']
                          and r.payload['native_scope_ref'] in proved_scopes]
        assert sum(g['usage']['event_count'] for g in local) == len(expected_local), 'same native thread still partially unidentified'
        if require_task_links:
            assert tasks, 'registered SCL binding reader emitted no task evidence'
            assert all(g['usage']['wi_unassigned_event_count'] == 0 for g in local), 'Local Codex still lacks its existing WI'
        print(json.dumps({'task_readback': {'original_watermark': view['watermark'],
            'event_count': result['summary']['usage']['event_count'],
            'wi_unlinked_before': base['summary']['usage']['wi_unassigned_event_count'],
            'wi_unlinked_after': result['summary']['usage']['wi_unassigned_event_count'],
            'task_binding_receipts': len(tasks),
            'paths': [{'key': g['key'], 'events': g['usage']['event_count'],
                       'known_tokens': g['usage']['known_total_tokens']}
                      for g in result['groups'] if g['usage']['event_count'] > 0]}}, ensure_ascii=False), flush=True)
        verified_tasks = verify_task_expectations(result['groups'], expected_tasks or [])
        document = build_dashboard(ledger, subscriptions=json.loads(DEFAULT_FACTS.read_text()),
            source_revision=revision, now=datetime.now(timezone.utc), catalog_snapshot=catalog,
            identity_dimensions=True)
        assert document['status'] == 'available' and len(document['views']) == 18
        publish_dashboard(output / 'dashboard.json', document, allow_row_contexts=True)
        assert load_cost_dashboard(output / 'dashboard.json') == document
        report = {'source_revision': revision, 'original_watermark': view['watermark'],
            'period': [q['from_date'], q['to_date']], 'event_count': result['summary']['usage']['event_count'],
            'baseline_comparison': baseline_comparison,
            'amount_before_usd': base['summary']['cost']['partial_machine_cash_usd'],
            'amount_usd': result['summary']['cost']['partial_machine_cash_usd'],
            'paginated_counter_corrections': len(corrections),
            'removed_historical_counter_usd': str(Decimal(base['summary']['cost']['partial_machine_cash_usd']) - expected_amount),
            'agent_unknown_before': base['summary']['usage']['agent_unassigned_event_count'],
            'agent_unknown_after': result['summary']['usage']['agent_unassigned_event_count'],
            'wi_unlinked_before': base['summary']['usage']['wi_unassigned_event_count'],
            'wi_unlinked_after': result['summary']['usage']['wi_unassigned_event_count'],
            'task_binding_receipts': len(tasks), 'verified_task_paths': verified_tasks,
            'identity_unavailable_events': {dimension: sum(g['usage']['event_count'] for g in result['groups']
                if g['key'][dimension] in {'unassigned','ambiguous'}) for dimension in ('agent','node','runtime','model','entrypoint')},
            'local_codex': [{'key': g['key'], 'events': g['usage']['event_count'],
                'amount_usd': g['cost']['partial_machine_cash_usd'],
                'wi_unlinked': g['usage']['wi_unassigned_event_count']} for g in local],
            'original_usage_facts_retained': True, 'only_proven_paginated_counter_correction': True,
            'identity_only_changes_amount': False, 'old_watermark_unchanged': True,
            'query_publisher_exporter_equal': True, 'production_modified': False,
            'source_files_changed_during_read': sum(hashlib.sha256(p.read_bytes()).hexdigest() != h
                                                   for p, h in native_files.items())}
    assert registry_path.read_bytes() == before_registry
    assert original.receipt_snapshot(watermark=view['watermark'])['sha256'] == snapshot['sha256']
    (output / 'report.json').write_bytes(canonical(report) + b'\n')
    (output / 'report.json').chmod(0o600)
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--require-task-links', action='store_true')
    parser.add_argument('--expected-tasks-json', default='[]')
    args = parser.parse_args()
    expected = json.loads(args.expected_tasks_json)
    if not isinstance(expected, list) or len(expected) > 20:
        parser.error('expected tasks must be a bounded list')
    replay(args.output, args.revision, require_task_links=args.require_task_links, expected_tasks=expected)
