#!/usr/bin/env python3
"""Consume the existing price replay's frozen copy. No production reads/writes."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'ComputeCost'),str(ROOT/'Stats/exporter'),str(ROOT/'Stats/exporter/tests')]
from pulse_compute_cost.collaboration_query import CollaborationQuery, CollaborationQuerySession
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import digest
from pulse_compute_cost.stats_dashboard import build_dashboard, DEFAULT_FACTS, matches_query_cost
from pulse_stats.cost_dashboard import _validate
from prepare_cost_consumer_replay import record_case


def replay(work: Path, output: Path):
    allowed = Path(os.environ['RUNNER_TEMP']).resolve()
    for path in (work, output):
        if path.is_symlink() or not path.resolve().is_relative_to(allowed) or path.resolve()==allowed:
            raise ValueError('existing private Actions replay directories required')
    prior = json.loads((work/'summary.json').read_bytes())
    published = json.loads((work/'publication.json').read_bytes())
    catalog = json.loads((work/'catalog.json').read_bytes())
    frozen = UsageLedger(work/'ledger').receipt_snapshot(watermark=prior['watermark'])
    assert frozen['sha256']==prior['frozen_facts_sha256']
    session = CollaborationQuerySession(frozen)
    at = datetime.fromisoformat(published['generated_at'])
    candidate = build_dashboard(UsageLedger(work/'ledger'), subscriptions=json.loads(DEFAULT_FACTS.read_bytes()),
        source_revision=os.environ['PULSE_SOURCE_REVISION'], now=at, catalog_snapshot=catalog,
        identity_dimensions=True,activity_table=True)
    assert candidate['status']=='available', candidate.get('reason')
    _validate(candidate)
    changes = []
    for period in ('today','week','month'):
        view = next(v for v in candidate['views'] if v['dimension']=='activity' and v['period']==period)
        original = next(v for v in published['views'] if v['dimension']=='agent' and v['period']==period)
        for field in ('event_count','known_total_tokens','tokens'):
            assert view['summary']['usage'][field] == original['summary']['usage'][field], (period,field)
        from catalog_source_replay import verify_published_projection
        removed = verify_published_projection(original['summary'], view['summary'], frozen['facts'])
        assert view['catalog_sha256'] == original['catalog_sha256']
        assert view['catalog_observed_at'] == original['catalog_observed_at']
        assert sum(g['usage']['event_count'] for g in view['groups']) == view['summary']['usage']['event_count']
        assert all(g['usage']['event_count']>0 for g in view['groups'])
        args = dict(view['query']);args['group_by']=tuple(args['group_by']);args['work_items']=tuple(args['work_items'])
        raw = session.query(CollaborationQuery(**args),catalog=catalog['catalog'],catalog_observed_at=catalog['retrieved_at'])
        assert raw['report_sha256']==view['report_sha256']
        source_groups = [g for g in raw['groups'] if g['usage']['event_count']>0]
        assert [g['key'] for g in source_groups] == [g['key'] for g in view['groups']]
        assert all(matches_query_cost(b['cost'], a['cost']) for a, b in zip(source_groups, view['groups']))
        changes.append({'period':period,'event_count':view['summary']['usage']['event_count'],
            'actual_joint_rows':len(view['groups']),
            'old_empty_agent_groups':[g['key'] for g in original['groups'] if g.get('usage',{}).get('event_count')==0],
            'actual_agents':sorted({g['key']['agent'] for g in view['groups']}),
            'actual_work_items':sorted({g['key']['wi'] for g in view['groups']}),
            'known_amount_usd':view['summary']['cost']['machine_cash_usd'] or view['summary']['cost']['partial_machine_cash_usd'],
            'summary_amounts_and_quantities_unchanged':True,
            'verified_diagnostic_removals':sorted(removed)})
    case = record_case(output,'real-activity-table',candidate,at)
    index = json.loads((output/'index.json').read_bytes());index.append(case)
    (output/'index.json').write_text(json.dumps(index,ensure_ascii=False))
    summary = {'candidate_revision':os.environ['PULSE_SOURCE_REVISION'],
        'publication_revision':published['source_revision'],'publication_time':published['generated_at'],
        'watermark':frozen['watermark'],'facts_sha256':frozen['sha256'],
        'periods':changes,'production_writes':False,'historical_attribution_not_repaired':True}
    (work/'activity-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();replay(a.input,a.output)
