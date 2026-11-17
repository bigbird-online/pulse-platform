"""Read the published ledger watermark through the new Outcome consumer.

No collection, source changes, lifecycle calls, paid requests or publication to
production. Output holds bounded metadata; the private replay snapshot is not
uploaded and does not become a ledger or a source of truth.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'ComputeCost'), str(ROOT/'Stats/exporter')]
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import current_receipts
from pulse_compute_cost.stats_dashboard import build_dashboard, DEFAULT_FACTS, REASON_CLASSES, without_reason_classes
from pulse_compute_cost.stats_dashboard_wire import expand_dashboard
from pulse_compute_cost.collaboration_query import classify_cost_reasons
from pulse_compute_cost.catalog_source import load_catalog_snapshot, resolve_catalog_path
from pulse_stats.cost_dashboard import _validate


def verify(output: Path) -> None:
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError('a new private evidence directory is required')
    output.mkdir(mode=0o700)
    home = Path.home(); state = home/'.local/state/pulse/stats'
    registry_path = state/'collector-sources.json'
    registry = json.loads(registry_path.read_bytes())
    original = expand_dashboard(json.loads((state/'cost-dashboard-v1.json').read_bytes()))
    _validate(original)
    watermarks = {v['watermark'] for v in original['views']}
    if len(watermarks) != 1: raise ValueError('published views must share a watermark')
    watermark = next(iter(watermarks))
    # Frozen read is permitted even if another publisher appends while we test.
    ledger = UsageLedger(Path(registry['ledger_root']))
    snapshot = ledger.receipt_snapshot(watermark=watermark)
    class FrozenSource:
        def receipt_snapshot(self, *, as_of=None, watermark=None):
            if as_of is not None or watermark not in (None, snapshot['watermark']):
                raise ValueError('replay escaped original watermark')
            return {**snapshot, 'facts':list(snapshot['facts'])}
    frozen = FrozenSource()
    catalog = load_catalog_snapshot(resolve_catalog_path(explicit=None,registry=registry,registry_path=registry_path))
    revision = subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    replay = build_dashboard(frozen, subscriptions=json.loads(DEFAULT_FACTS.read_bytes()),
        source_revision=revision, now=datetime.fromisoformat(original['generated_at']),
        catalog_snapshot=catalog, identity_dimensions=True, activity_table=True)
    _validate(replay)
    assert replay['status']=='available', replay.get('reason')
    actual = {(v['period'],v['dimension']):v for v in replay['views']}
    for old in original['views']:
        new = actual[old['period'],old['dimension']]
        assert old['watermark']==new['watermark']
        assert old['catalog_sha256']==new['catalog_sha256'], 'saved price changed; cannot claim same-price parity'
        # A publication from before the reason classes still proves amount parity.
        def same(a, b):
            return without_reason_classes(a['cost']) == without_reason_classes(b['cost']) and {
                k: v for k, v in a.items() if k != 'cost'} == {k: v for k, v in b.items() if k != 'cost'}
        assert same(old['summary'], new['summary']), 'existing cost/usage changed'
        assert [g['key'] for g in old['groups']] == [g['key'] for g in new['groups']]
        assert all(a['usage'] == b['usage'] and without_reason_classes(a['cost']) == without_reason_classes(b['cost'])
                   for a, b in zip(old['groups'], new['groups'])), 'existing group cost/usage changed'
        assert all({k: c[k] for k in REASON_CLASSES} == classify_cost_reasons(c['reasons'])
                   for c in [new['summary']['cost']] + [g['cost'] for g in new['groups']])
    active, conflicts = current_receipts(snapshot['facts'])
    states=Counter(r.payload['status'] for r in active if r.kind=='delivery')
    windows=[]
    for period in ('today','week','month'):
        view=actual[period,'activity']; o=view['outcomes']
        windows.append({'period':period,'query':view['query'],'usage_events':view['summary']['usage']['event_count'],
            'outcome_status':o['status'],'outcome_count':o['count'],'known_count':o['known_count'],
            'review_only_objects':sum(not i['counted'] for i in o['items']),
            'outcome_only_rows':len(view['outcome_only_groups']), 'active_agents':view['active_agents'],
            'outcome_wis':sorted({i['wi'] for i in o['items'] if i['counted']}),
            'outcome_conflicts':len(o['conflict_refs']), 'outcome_undated':len(o['unallocated_refs'])})
    assert ledger.receipt_snapshot(watermark=watermark)['sha256']==snapshot['sha256']
    from pulse_compute_cost.source_layout import manifest_paths
    inventories=[]
    for source in registry['sources']:
        if source['kind'] != 'harness_root': continue
        paths=manifest_paths(source)
        if paths is None: paths=[Path(source['path'])]
        if len(paths)!=1: raise ValueError('one registered Harness root expected')
        root=paths[0]
        counts={name:len([p for p in (root/subdir).rglob(pattern) if p.is_file() and not p.is_symlink()])
                for name,subdir,pattern in [('canonical_outcome_files','evidence/outcomes','*.md'),
                    ('task_outcome_files','evidence/task-outcomes','*.json'),
                    ('measurement_files','evidence/measurements','*.json'),('review_files','reviews','*.json')]}
        import yaml
        schema_counts, status_counts = Counter(), Counter()
        samples=[]
        for path in sorted((root/'evidence/outcomes').rglob('*.md')):
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024*1024: continue
            raw=path.read_text(); parts=raw.split('---',2)
            try: data=yaml.safe_load(parts[1]) if raw.startswith('---') and len(parts)==3 else None
            except yaml.YAMLError: data=None
            if not isinstance(data,dict):
                schema_counts['unstructured']+=1; continue
            schema_counts[str(data.get('schema',data.get('schema_version','missing')))]+=1
            status_counts[str(data.get('status','missing'))]+=1
            at=str(data.get('verified_at',''))
            if '2026-09-14' <= at[:10] < '2026-09-18' and len(samples)<12:
                samples.append({k: data.get(k) for k in ('schema','claim_id','status','verified_at','producer','verifier','verification_level','acceptance_fingerprint')}
                    | {'frontmatter_fields':sorted(data), 'body_headings':[line for line in parts[2].splitlines() if line.startswith('#')][:12],
                       'claim_characters':len(str(data.get('claim',''))), 'canonical_filename_matches':path.stem==data.get('claim_id'),
                       'target_keys':sorted(data.get('target',{})), 'check_names':sorted(data.get('checks',{})),
                       'has_bundle':bool(data.get('evidence_bundle'))})
        inventories.append({'source_id':source['id'],**counts, 'outcome_schemas':dict(schema_counts),
            'outcome_statuses':dict(status_counts),'this_week_metadata_samples':samples,
            'scope':'current registered source metadata only; not accepted outcome count'})
    # Import the native canonical Outcome source into an isolated ledger only.
    # The old publication is an immutable baseline, not the data source of truth
    # for the new field. No source counter/price/production pointer is edited.
    from tempfile import TemporaryDirectory
    from pulse_compute_cost.receipt_sources import collect_registered_sources
    from pulse_compute_cost.canonical_outcomes import FEATURE
    with TemporaryDirectory(prefix='stats-outcome-native-') as temporary:
        private=UsageLedger(Path(temporary)/'ledger')
        private.ingest_receipts(snapshot['facts'])
        native_registry={**registry,'ledger_root':str(private.root),
            'sources':[s for s in registry['sources'] if s['kind']=='harness_root'],
            'required_receipt_features':sorted(set(registry.get('required_receipt_features',[]))|{FEATURE})}
        collection=collect_registered_sources(native_registry,private)
        native=build_dashboard(private, subscriptions=json.loads(DEFAULT_FACTS.read_bytes()),
            source_revision=revision, now=datetime.now(timezone.utc), catalog_snapshot=catalog,
            identity_dimensions=True,activity_table=True)
        _validate(native)
        assert native['status']=='available',native.get('reason')
        native_windows=[]
        for view in native['views']:
            original_view=actual[view['period'],view['dimension']]
            assert view['query']['from_date']==original_view['query']['from_date'], 'calendar changed during replay'
            assert view['query']['to_date']==original_view['query']['to_date'], 'calendar changed during replay'
            old_rows=[(g['key'],g['cost'],g['usage']) for g in original_view['groups'] if g['usage']['event_count']]
            new_rows=[(g['key'],g['cost'],g['usage']) for g in view['groups'] if g['usage']['event_count']]
            assert old_rows==new_rows, 'canonical Outcome import changed priced consumption'
            for field in ('machine_cash_usd','partial_machine_cash_usd'):
                assert view['summary']['cost'][field]==original_view['summary']['cost'][field]
            assert view['summary']['usage']['tokens']==original_view['summary']['usage']['tokens']
            if view['dimension']=='activity':
                o=view['outcomes']
                native_windows.append({'period':view['period'],'count':o['count'],'known_count':o['known_count'],
                    'status':o['status'],'outcome_only_rows':len(view['outcome_only_groups']),
                    'named_outcomes':sum(i['title'] is not None for i in o['items']),
                    'outcome_wis':sorted({i['wi'] for i in o['items'] if i['counted']}),
                    'conflicts':len(o['conflict_refs']),'undated':len(o['unallocated_refs'])})
        native_active,_=current_receipts(private.receipt_snapshot()['facts'])
        native_count=sum(r.kind=='delivery' and r.producer=='canonical_outcome.v1' for r in native_active)
        assert all(w['status'] != 'conflict' for w in native_windows), 'native Outcome import manufactured unresolved presentation conflicts'
        assert any((w['count'] or 0)>0 and w['named_outcomes']>0 for w in native_windows), 'real Outcome names and counts must reach at least one period'
        native_evidence={'canonical_deliveries':native_count,'windows':native_windows,
            'source_results':[{'status':x['status'],'detail':x['source_detail']} for x in collection['sources']],
            'cost_and_tokens_preserved':True,'production_modified':False}
        assert native_count>0, 'no real canonical Outcomes imported; source hookup is not complete'
    assert ledger.receipt_snapshot(watermark=watermark)['sha256']==snapshot['sha256']
    summary={'schema':'stats_outcome_readback/v1','candidate':revision,'observed_at':datetime.now(timezone.utc).isoformat(),
        'source_revision':original['source_revision'],'source_generated_at':original['generated_at'],'watermark':watermark,
        'all_21_cost_usage_views_unchanged':True, 'original_facts_unchanged':True, 'production_modified':False,
        'native_outcome_import':native_evidence,'existing_delivery_states':dict(states),'windows':windows,'registered_source_presence':inventories}
    target=output/'summary.json'; target.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');target.chmod(0o600)
    # Reuse the actual whole-snapshot exporter/Swift test transport. This is an
    # isolated replay labelled real, never a synthetic replacement of the App.
    sys.path.insert(0, str(ROOT/'Stats/exporter/tests'))
    from prepare_cost_consumer_replay import record_case
    replay_root=output.parent/'consumer-replay'
    if not replay_root.is_dir(): raise ValueError('existing isolated consumer replay directory required')
    case=record_case(replay_root,'real-outcome-readback',replay,datetime.fromisoformat(replay['generated_at']))
    index=replay_root/'index.json'; items=json.loads(index.read_bytes());items.append(case);items.append(record_case(replay_root,'real-native-outcome-readback',native,datetime.fromisoformat(native['generated_at'])));index.write_text(json.dumps(items))
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    verify(parser.parse_args().output)
