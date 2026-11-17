"""Prepare private inputs for the actual Swift consumer tests, not production data.

The oracle is the original ComputeCost query at the same ledger watermark and
catalog. No pricing/attribution logic lives here. Real replay is explicit and
read-only; all ingests/publications target this invocation's temporary directory.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'ComputeCost'), str(ROOT / 'Stats/exporter')]
from pulse_compute_cost.collaboration_query import CollaborationQuery, classify_cost_reasons, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, reference, canonical, digest
from pulse_compute_cost.receipt_sources import read_pi, usage_receipt, actor_receipt, import_harness_bundle
from pulse_compute_cost.stats_dashboard import build_dashboard, publish_dashboard, DEFAULT_FACTS, matches_query_cost
from pulse_stats.cost_dashboard import attach_cost_dashboard, load_cost_dashboard, usage_metrics, REPLACED_USAGE_IDS

FIXTURES = ROOT / 'Stats/PulseStats/Resources/Fixtures'
AT = datetime(2026, 9, 14, 4, tzinfo=timezone.utc)


def private_json(path, value):
    data = canonical(value) + b'\n'
    with path.open('xb') as handle:
        os.chmod(path, 0o600)
        handle.write(data)


def expected_views(document):
    return [{'period': v['period'], 'dimension': v['dimension'],
             'watermark': v['watermark'], 'report_sha256': v['report_sha256'],
             'query': v['query'], 'summary': v['summary'], 'groups': v['groups']}
            for v in document['views']]


def assert_query_parity(ledger, document, catalog):
    """Compare every group AND summary with raw queries, not a sum of rows."""
    for view in document['views']:
        args = dict(view['query'])
        args['group_by'] = tuple(args['group_by'])
        args['work_items'] = tuple(args.get('work_items', []))
        raw = query_collaboration(ledger, CollaborationQuery(**args),
            catalog=catalog['catalog'], catalog_observed_at=catalog['retrieved_at'])
        assert raw['report_sha256'] == view['report_sha256']
        assert matches_query_cost(view['summary']['cost'], raw['summary']['cost'])
        source_groups = [g for g in raw['groups'] if view['dimension'] != 'activity' or g['usage']['event_count'] > 0]
        assert [g['key'] for g in source_groups] == [g['key'] for g in view['groups']]
        assert all(matches_query_cost(b['cost'], a['cost']) for a, b in zip(source_groups, view['groups']))
        assert raw['summary']['usage']['tokens'] == view['summary']['usage']['tokens']
        for source, projected in zip(source_groups, view['groups']):
            assert source['usage']['tokens'] == projected['usage']['tokens']
        assert raw['watermark'] == view['watermark']
        if view['dimension'] == 'activity':
            assert raw['summary']['outcomes'] == view['outcomes']
            assert raw['stages']['outputs'] == view['stages']['outputs']
            for a,b in zip(source_groups,view['groups']):
                assert [x['outcome_ref'] for x in a['outcomes']['items']] == b['outcomes']['refs']
                assert a['outcomes']['count'] == b['outcomes']['count']


def record_case(output, name, document, at, expected_status='available', reason=None):
    case_dir = output / name
    case_dir.mkdir(mode=0o700)
    source = case_dir / 'source.json'
    publish_dashboard(source, document)
    loaded = load_cost_dashboard(source, now=at)
    assert loaded['status'] == expected_status, (name, loaded.get('reason'))
    if reason is not None:
        assert loaded['reason'] == reason
    if expected_status == 'available':
        assert loaded == document, 'reader changed the original query projection'
    # The App keeps an expired publication whole, marked with its own time.
    displayed = load_cost_dashboard(source, now=at, retain_expired=True)
    if loaded.get('reason') == 'snapshot_stale':
        assert displayed == document, 'display changed the retained publication'
    else:
        assert displayed == loaded
    base = json.loads((FIXTURES / 'ok-full.json').read_text())
    # Freeze the envelope clock independently of the cost source clock.
    base['generated_at'] = at.isoformat()
    base['expires_at'] = (at + timedelta(minutes=45)).isoformat()
    rendered = json.loads(attach_cost_dashboard(json.dumps(base), source, now=at))
    assert rendered['cost_dashboard'] == displayed
    for field in base:
        if field not in {'metrics', 'cost_dashboard'}:
            assert rendered[field] == base[field], 'cost changed non-cost section: ' + field
    assert rendered['metrics'] == [m for m in base['metrics']
        if not m['id'].startswith(('billing.', 'compute_cost.shadow.', 'usage.mtd.'))
        and m['id'] not in REPLACED_USAGE_IDS] + usage_metrics(displayed, now=at)
    private_json(output / (name + '.json'), {'name': name, 'at': at.isoformat(),
        'snapshot': rendered, 'expected_status': displayed['status'],
        'expected': expected_views(displayed), 'reason': displayed.get('reason')})
    return {'name': name, 'status': displayed['status'], 'views': len(displayed['views']),
            'priced_views': sum(v['summary']['cost']['machine_cash_usd'] is not None or
                               v['summary']['cost']['partial_machine_cash_usd'] is not None
                               for v in displayed['views'])}


def prepare(output, revision, native_pi=None, catalog_path=None):
    # Caller must allocate a NEW private directory, never reuse a production root.
    output = Path(output)
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError('a new absolute replay directory is required')
    output.mkdir(mode=0o700)
    facts = json.loads(DEFAULT_FACTS.read_text())
    catalog = {'retrieved_at': (AT-timedelta(seconds=2)).isoformat(), 'catalog': {'data': [
        {'id': 'openai/gpt-5', 'canonical_slug': 'openai/gpt-5', 'pricing': {
            'prompt': '0.000001', 'completion': '0.00001',
            'input_cache_read': '0.0000001', 'input_cache_write': '0.000001'}}]}}
    cases = []
    with tempfile.TemporaryDirectory(prefix='ledger-', dir=output) as temp:
        ledger = UsageLedger(temp, clock=lambda: AT-timedelta(seconds=1))
        rows = []
        for i in range(36):
            common = {'wi': f'wi_20260914_{i:03d}', 'executor': f'agent-{i:02d}',
                      'responsible': None, 'delegator': None}
            rows.append(usage_receipt('cc', ['consumer-fixture', i],
                {'input_tokens': 100+i, 'output_tokens': 20,
                 'cache_read_input_tokens': 50, 'cache_creation_input_tokens': 0},
                at=(AT-timedelta(seconds=10)).isoformat(), observed=(AT-timedelta(seconds=5)).isoformat(),
                model='openai/gpt-5', common=common, final=True))
        ledger.ingest_receipts(rows)
        doc = build_dashboard(ledger, subscriptions=facts, source_revision=revision, now=AT,
                              catalog_snapshot=catalog)
        assert_query_parity(ledger, doc, catalog)
        assert all(v['summary']['cost']['partial_machine_cash_usd'] is not None for v in doc['views'])
        cases.append(record_case(output, 'partial-many-groups', doc, AT))
        cases.append(record_case(output, 'expired', doc, AT+timedelta(minutes=45),
                                 'unavailable', 'snapshot_stale'))
        cases.append(record_case(output, 'future', doc, AT-timedelta(seconds=1),
                                 'unavailable', 'snapshot_from_future'))
        # A new original query, with an additional real-shaped fixture receipt.
        ledger.ingest_receipts([usage_receipt('cc', ['consumer-fixture', 'next'],
            {'input_tokens': 999, 'output_tokens': 50, 'cache_read_input_tokens': 0,
             'cache_creation_input_tokens': 0}, at=AT.isoformat(), observed=AT.isoformat(),
            model='openai/gpt-5', final=True)])
        next_doc = build_dashboard(ledger, subscriptions=facts, source_revision=revision,
            now=AT+timedelta(minutes=1), catalog_snapshot=catalog)
        assert_query_parity(ledger, next_doc, catalog)
        cases.append(record_case(output, 'next-watermark', next_doc, AT+timedelta(minutes=1)))
        for label, mutate in [('unknown', 'unknown'), ('conflict-with-priced-groups', 'conflict'),
                              ('known-zero', 'zero'), ('complete', 'complete')]:
            variant = deepcopy(doc)
            for view in variant['views']:
                cost = view['summary']['cost']
                if mutate in {'unknown', 'conflict'}:
                    cost.update(machine_cash_usd=None, partial_machine_cash_usd=None,
                        reasons=['join_conflict'] if mutate=='conflict' else ['no_usage_observations'])
                else:
                    cost.update(machine_cash_usd='0' if mutate=='zero' else '123.123456789012345678',
                                partial_machine_cash_usd=None, reasons=[], unpriced=[])
                # These cases are deliberately synthetic consumer contracts,
                # never described as original-query or production arithmetic.
                # Without priced rows they read like publications predating
                # the priced total.
                cost.pop('priced_machine_cash_usd', None); cost.pop('priced_event_count', None)
                cost.update(classify_cost_reasons(cost['reasons']))
            variant['sha256'] = digest({k:v for k,v in variant.items() if k!='sha256'})
            cases.append(record_case(output, label, variant, AT))
        identity_ledger = UsageLedger(Path(temp) / 'identity', clock=lambda: AT-timedelta(seconds=1))
        identity_rows = []
        for index, (agent, node, runtime, entrypoint) in enumerate([
                ('pulse','ownermac','codex','codex_app'),
                ('pulse','tcloud','pi','feishu'), ('aki','tcloud','pi','feishu')]):
            quantity = ({'input_tokens':120,'cached_input_tokens':20,'cache_write_input_tokens':0,'output_tokens':10}
                        if runtime=='codex' else {'input':100,'cacheRead':20,'cacheWrite':0,'output':10})
            row = usage_receipt(runtime, ['five-dimensional-fixture',index], quantity,
                at=(AT-timedelta(seconds=10)).isoformat(), observed=(AT-timedelta(seconds=5)).isoformat(),
                model='openai/gpt-5', mapping='codex_cli.v1' if runtime=='codex' else 'pi_native.v1',
                common={'wi':None if index==0 else f'wi_20260914_{index:03d}',
                        'executor':None,'responsible':None,'delegator':None})
            identity_rows.extend([row, actor_receipt(row,node=node,executor=agent,entrypoint=entrypoint,
                observed=(AT-timedelta(seconds=5)).isoformat())])
        identity_ledger.ingest_receipts(identity_rows)
        identity_doc = build_dashboard(identity_ledger,subscriptions=facts,source_revision=revision,
            now=AT,catalog_snapshot=catalog,identity_dimensions=True)
        assert len(identity_doc['views']) == 18
        assert_query_parity(identity_ledger,identity_doc,catalog)
        cases.append(record_case(output,'five-dimensions',identity_doc,AT))
        # One actual Hestia usage plus a Pulse execution expectation reproduces
        # the user's phantom daily row without assigning any new WI evidence.
        activity_at = datetime(2026, 9, 16, 2, tzinfo=timezone.utc)
        activity_ledger = UsageLedger(Path(temp) / 'activity', clock=lambda: activity_at-timedelta(seconds=1))
        actual_at = (activity_at-timedelta(seconds=5)).isoformat()
        activity_rows = []
        for index, (agent, wi, node, entry, timestamp) in enumerate([
                ('hestia','wi_20260826_825','ownermac','codex_app',actual_at),
                ('pulse','wi_20260827_282','ownermac','codex_app','2026-09-15T01:00:00Z')]):
            row = usage_receipt('codex', ['activity-fixture',index],
                {'input_tokens':530000,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':0},
                at=timestamp, observed=actual_at, model='openai/gpt-5', mapping='codex_cli.v1',
                common={'wi':wi,'executor':agent,'responsible':agent,'delegator':None})
            activity_rows.extend([row,actor_receipt(row,node=node,executor=agent,entrypoint=entry,observed=actual_at)])
        empty_execution = Receipt.make('execution', 'idle-pulse-activity', {
            'wi':'wi_20260820_568','executor':'pulse','responsible':'pulse','delegator':None,
            'runtime':'codex','assignment_ref':reference('assignment','idle-pulse'),
            'parent_ref':None,'state':'running','expected_usage':True},
            occurred_at=actual_at,observed_at=actual_at,producer='synthetic_test.v1')
        activity_ledger.ingest_receipts([*activity_rows,empty_execution])
        activity_doc = build_dashboard(activity_ledger,subscriptions=facts,source_revision=revision,
            now=activity_at,catalog_snapshot=catalog,identity_dimensions=True,activity_table=True)
        assert_query_parity(activity_ledger,activity_doc,catalog)
        cases.append(record_case(output,'activity-today',activity_doc,activity_at))
        many = build_dashboard(ledger,subscriptions=facts,source_revision=revision,
            now=activity_at,catalog_snapshot=catalog,identity_dimensions=True,activity_table=True)
        assert_query_parity(ledger,many,catalog)
        cases.append(record_case(output,'activity-many',many,activity_at))
        empty_ledger = UsageLedger(Path(temp) / 'activity-empty', clock=lambda: activity_at-timedelta(seconds=1))
        empty_ledger.ingest_receipts([empty_execution])
        empty = build_dashboard(empty_ledger,subscriptions=facts,source_revision=revision,
            now=activity_at,catalog_snapshot=catalog,identity_dimensions=True,activity_table=True)
        cases.append(record_case(output,'activity-empty',empty,activity_at))
        # Accepted design v3: real-shaped Harness bundle -> original query ->
        # exporter -> Swift. No hand-built output count or GUI-only fake items.
        sys.path.insert(0, str(ROOT / 'ComputeCost/tests'))
        from test_delivery_value import review_snapshot
        from test_harness_measurement import measurement_snapshot
        outcome_ledger = UsageLedger(Path(temp) / 'outcomes', clock=lambda: activity_at-timedelta(seconds=1))
        r, m = review_snapshot(), measurement_snapshot()
        r['reviewed_at'] = '2026-09-16T01:10:00Z'
        m['efficiency']['delivery_accepted_at'] = '2026-09-16T01:00:00Z'
        m['evidence']['readouts']['efficiency.delivery_accepted_at']['value'] = '2026-09-16T01:00:00Z'
        m['evidence']['as_of'] = r['reviewed_at']
        for readout in m['evidence']['readouts'].values():
            readout['as_of'] = r['reviewed_at']; readout['window']['end'] = r['reviewed_at']
        delivered = import_harness_bundle({'schema':'computecost_harness_import/v1',
            'work_item_id':r['work_item_id'], 'responsible':'pulse', 'review':r, 'measurement':m}, actual_at)
        outcome_rows = list(delivered)
        for i, entry in enumerate(('codex_app','telegram')):
            u = usage_receipt('codex', ['outcome-demo',i],
                {'input_tokens':123456,'cached_input_tokens':65432,'cache_write_input_tokens':0,'output_tokens':1378},
                at=actual_at,observed=actual_at,model='openai/gpt-5',mapping='codex_cli.v1',
                common={'wi':r['work_item_id'],'executor':'pulse','responsible':'pulse','delegator':None})
            outcome_rows.extend([u,actor_receipt(u,node='ownermac',executor='pulse',entrypoint=entry,observed=actual_at)])
        # Another real-shaped Outcome with no usage in the period remains an
        # output-only row, rather than an empty execution expectation.
        d = next(item for item in delivered if item.kind=='delivery')
        other = Receipt.make('delivery','outcome-only-demo',
            {**d.payload,'wi':'wi_20260916_777','responsible':'aki'},
            occurred_at=d.occurred_at,observed_at=actual_at,producer='synthetic_test.v1')
        outcome_ledger.ingest_receipts([*outcome_rows,other,empty_execution])
        # Kernel-merged PRs of the same WI, and one GitHub merge plus one direct
        # push the core has no record of, through the real publisher join.
        from pulse_compute_cost.pull_request_outputs import read_merged
        kernel = {'schema':'harness.stats_facts/v1','read_only':True,'source':'journal','namespace':'replay',
            'coverage':'complete','unavailable_items':[],'items':[
                {'id':r['work_item_id'],'owner':'pulse','merged_pull_requests':[
                    {'repository':'bigbird-online/PULSE','number':number,'head_sha':'a'*40,'merge_commit':sha*40,
                     'merged_at':actual_at,'observed_at':actual_at,'score':score,'reviewed_rounds':rounds,
                     'decision':'review'} for number,sha,score,rounds in ((1417,'e',92,1),(1421,'f',88,2))]},
                {'id':'wi_20260916_777','owner':'aki','merged_pull_requests':[]}]}
        audit = {'schema':'pulse_stats_pull_request_audit/v1','observed_at':actual_at,
            'since':(activity_at-timedelta(days=7)).isoformat(),'window_days':7,'repositories':[
                {'repository':'bigbird-online/PULSE','merges':'read','commits':'read','reason':None,
                 'merged_pull_requests':[{'number':1417,'merged_at':actual_at,'merge_commit':'e'*40,'title':'Kernel merge'},
                     {'number':1300,'merged_at':actual_at,'merge_commit':'d'*40,'title':'Merged in the GitHub web page'}],
                 'pr_commits':[],'direct_commits':[{'sha':'c'*40,'committed_at':actual_at,
                     'title':'Pushed straight to main','author':'example-owner'}]}]}
        outcome_doc=build_dashboard(outcome_ledger,subscriptions=facts,source_revision=revision,
            now=activity_at,catalog_snapshot=catalog,identity_dimensions=True,activity_table=True,
            pull_requests=read_merged(kernel),pull_request_audit=audit,audit_pull_requests=True,
            kernel_merge_enforced_since='2026-09-16')
        assert_query_parity(outcome_ledger,outcome_doc,catalog)
        cases.append(record_case(output,'outcome-dashboard',outcome_doc,activity_at))
        # WI550: real receipt/query/publisher path; no production WI is created.
        stage_at = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
        stage_ledger = UsageLedger(Path(temp) / 'stages', clock=lambda: stage_at-timedelta(seconds=1))
        context = reference('context', 'synthetic-stage'); activity = reference('activity', 'synthetic-stage')
        stage_rows = []
        for key, contribution, when, obj, category, unit, quantity in [
                ('original', 'new', '2026-10-09T01:00:00Z', 'skill', 'skill', 'capability', '1'),
                ('v2', 'improved', '2026-10-10T01:00:00Z', 'skill', 'skill', 'capability', '1'),
                ('v3', 'improved', '2026-10-10T02:00:00Z', 'skill', 'skill', 'capability', '1'),
                ('reuse', 'reused', '2026-10-10T03:00:00Z', 'skill', 'skill', 'capability', '1'),
                ('unknown', 'new', '2026-10-10T04:00:00Z', 'article', 'article', 'paragraph', None)]:
            stage_rows.append(Receipt.make('stage_output', key, {
                'wi': None, 'context_ref': context, 'activity_refs': [activity], 'epistemic': 'agent_judgment',
                'object_ref': reference('object', obj), 'version_ref': reference('version', key),
                'name': '合成计量方法' if obj == 'skill' else '合成文章', 'category': category,
                'unit': unit, 'comparison_basis': 'synthetic-object-basis', 'contribution': contribution,
                'extent': '测试声明，不是自然使用证据', 'quantity': quantity},
                occurred_at=when, observed_at=(stage_at-timedelta(seconds=2)).isoformat(),
                producer='synthetic_stage.v1', evidence=(activity,)))
        stage_ledger.ingest_receipts(stage_rows)
        for name, target in [('stage-outputs', None), ('stage-linked', 'wi_20261008_550'),
                             ('stage-conflict', 'wi_20261008_551')]:
            if target:
                stage_ledger.ingest_receipts([Receipt.make('stage_link', name, {
                    'wi': target, 'context_ref': context,
                    'target_refs': [r.entity_ref for r in (stage_rows if name == 'stage-linked' else stage_rows[1:2])],
                    'state': 'assigned', 'basis': '合成有界活动关联', 'epistemic': 'agent_judgment'},
                    occurred_at=(stage_at-timedelta(seconds=2)).isoformat(),
                    observed_at=(stage_at-timedelta(seconds=2)).isoformat(),
                    producer='synthetic_stage.v1', evidence=(activity,))])
            stage_doc = build_dashboard(stage_ledger, subscriptions=facts, source_revision=revision,
                now=stage_at, catalog_snapshot=catalog, activity_table=True, activity_only=True)
            assert_query_parity(stage_ledger, stage_doc, catalog)
            cases.append(record_case(output, name, stage_doc, stage_at))
        if native_pi is not None:
            if catalog_path is None:
                raise ValueError('real replay requires its actual catalog snapshot')
            native_pi, catalog_path = Path(native_pi), Path(catalog_path)
            if native_pi.is_symlink() or not native_pi.is_dir() or catalog_path.is_symlink():
                raise ValueError('read-only source must be a regular directory/file')
            real_catalog = json.loads(catalog_path.read_text())
            now = datetime.now(timezone.utc)
            observations, source_hashes = [], []
            for path in sorted(native_pi.rglob('*.jsonl')):
                if path.is_symlink():
                    raise ValueError('source symlink refused')
                data = path.read_bytes()
                source_hashes.append(hashlib.sha256(data).hexdigest())
                read_pi(path, now.isoformat(), into=observations, data=data)
            with tempfile.TemporaryDirectory(prefix='real-replay-ledger-', dir=output) as real_temp:
                real = UsageLedger(real_temp)
                real.ingest_receipts(observations)
                real_doc = build_dashboard(real, subscriptions=facts, source_revision=revision,
                                           catalog_snapshot=real_catalog)
                assert real_doc['status']=='available' and len(real_doc['views'])==9
                assert_query_parity(real, real_doc, real_catalog)
                case = record_case(output, 'real-pi-query', real_doc,
                    datetime.fromisoformat(real_doc['generated_at']))
                assert case['priced_views'] > 0, 'known real Pi source did not yield any priced view'
                case.update(scope='explicit_read_only_pi_source_not_full_platform',
                    source_files=len(source_hashes), source_digest=digest(source_hashes),
                    production_mutated=False)
                cases.append(case)
    private_json(output/'index.json', cases)
    print(json.dumps({'consumer_replays': cases, 'production_mutated': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--native-pi-dir', type=Path)
    parser.add_argument('--catalog-snapshot', type=Path)
    args = parser.parse_args()
    prepare(args.output, args.source_revision, args.native_pi_dir, args.catalog_snapshot)
