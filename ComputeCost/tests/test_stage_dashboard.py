"""Synthetic receipts -> original query -> publisher -> strict Stats reader."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import digest, reference
from pulse_compute_cost.stats_dashboard import build_dashboard
from test_stage_facts import contribution, goal, link, OBJECT, WI, OTHER
from test_collaboration_receipts import usage

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'Stats/exporter'))
from pulse_stats.cost_dashboard import _validate

NOW = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
TODAY = '2026-10-10T01:00:00Z'
FACTS = {'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []}


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / 'ledger', clock=lambda: NOW - timedelta(seconds=1))


def dashboard(ledger):
    return build_dashboard(ledger, subscriptions=FACTS, source_revision='a' * 40,
                           now=NOW, activity_table=True, activity_only=True)


def today(doc):
    return next(v for v in doc['views'] if v['period'] == 'today')


def resign(doc):
    doc['sha256'] = digest({k: v for k, v in doc.items() if k != 'sha256'})
    return doc


def test_stages_are_same_watermark_query_without_cost_or_acceptance(ledger):
    old = contribution()
    improved = contribution('improvement', change='improved', at=TODAY,
                            version=reference('version', 'v2'))
    repeated_version = contribution('another-version', change='improved', at=TODAY,
                                    version=reference('version', 'v3'))
    ledger.ingest_receipts([goal(), old, improved, repeated_version, link([old, improved, repeated_version])])
    doc = dashboard(ledger)
    _validate(doc)
    view = today(doc)
    original = query_collaboration(ledger, CollaborationQuery(**{
        **view['query'], 'group_by': tuple(view['query']['group_by']),
        'work_items': tuple(view['query']['work_items'])}))
    assert view['stages']['outputs'] == original['stages']['outputs']
    assert view['report_sha256'] == original['report_sha256']
    assert view['stages']['outputs']['formed'][0]['quantity'] == '1'
    assert view['stages']['outputs']['cumulative']['object_count'] == 1
    assert len(view['stages']['outputs']['items']) == 3
    assert view['stages']['outputs']['accepted']['status'] == 'unavailable'
    assert view['groups'] == []  # No invented usage row or zero-priced output.
    assert view['summary']['cost']['machine_cash_usd'] is None
    assert view['stages']['execution_authority'] is False


def test_unknown_shared_conflict_and_old_formation_remain_distinct(ledger):
    old = contribution()
    unknown = contribution('unknown', at=TODAY, payload_changes={'quantity': None})
    ledger.ingest_receipts([old, unknown, link([unknown]), link([unknown], OTHER, 'other')])
    view = today(dashboard(ledger))
    _validate(dashboard(ledger))
    stage = view['stages']
    assert stage['status'] == 'conflict'
    assert stage['outputs']['formed'][0]['quantity'] is None
    assert stage['outputs']['formed'][0]['known_quantity'] is None
    assert stage['outputs']['formed'][0]['unresolved_object_refs'] == [OBJECT]
    assert stage['diagnostics']['membership_conflicts'] == [unknown.entity_ref]
    assert {o['wi'] for o in stage['outputs']['items']} == {None, 'ambiguous'}


def test_repeated_observation_and_late_link_preserve_old_dashboard(ledger):
    o = contribution(at=TODAY)
    ledger.ingest_receipts([o])
    before = dashboard(ledger)
    assert ledger.ingest_receipts([o])['inserted'] == 0
    assert dashboard(UsageLedger(ledger.root, clock=lambda: NOW)) == before
    ledger.ingest_receipts([link([o])])
    after = dashboard(ledger)
    assert today(before)['stages']['outputs']['items'][0]['wi'] is None
    assert today(after)['stages']['outputs']['items'][0]['wi'] == WI
    assert today(before)['stages']['outputs']['formed'] == today(after)['stages']['outputs']['formed']
    assert today(before)['watermark'] < today(after)['watermark']


@pytest.mark.parametrize('mutation', [
    'authority', 'acceptance', 'unknown_status', 'bad_quantity', 'object_count',
    'duplicate_group', 'foreign_ref', 'wrong_period', 'raw_path', 'duplicate_item',
    'zero_for_unknown', 'missing_group', 'bad_epistemic', 'future_item'])
def test_reader_rejects_false_semantics_even_with_resigned_envelope(ledger, mutation):
    ledger.ingest_receipts([contribution(at=TODAY, payload_changes={'quantity': None})])
    doc = dashboard(ledger)
    stage = today(doc)['stages']
    outputs = stage['outputs']
    if mutation == 'authority': stage['execution_authority'] = True
    elif mutation == 'acceptance': outputs['accepted']['status'] = 'accepted'
    elif mutation == 'unknown_status': stage['status'] = 'complete'
    elif mutation == 'bad_quantity': outputs['items'][0]['quantity'] = 'NaN'
    elif mutation == 'object_count': outputs['cumulative']['object_count'] = 2
    elif mutation == 'duplicate_group': outputs['formed'] *= 2
    elif mutation == 'foreign_ref': outputs['formed'][0]['fact_refs'] = [reference('fact', 'foreign')]
    elif mutation == 'wrong_period': outputs['items'][0]['occurred_at'] = '2026-10-01T01:00:00Z'
    elif mutation == 'raw_path': outputs['items'][0]['evidence'] = ['/private/transcript.jsonl']
    elif mutation == 'duplicate_item': outputs['items'] *= 2
    elif mutation == 'zero_for_unknown': outputs['formed'][0]['quantity'] = '0'
    elif mutation == 'missing_group': outputs['formed'] = []
    elif mutation == 'bad_epistemic': outputs['items'][0]['epistemic'] = 'human_confirmed'
    elif mutation == 'future_item': outputs['items'][0]['occurred_at'] = '2026-11-01T01:00:00Z'
    with pytest.raises(ValueError):
        _validate(resign(doc))


def test_old_carrier_without_stage_section_is_readable_not_zero(ledger):
    ledger.ingest_receipts([usage(at=TODAY)])
    doc = dashboard(ledger)
    for view in doc['views']:
        view.pop('stages', None)
    _validate(resign(doc))


def test_oversized_stage_detail_does_not_hide_machine_usage(ledger):
    ledger.ingest_receipts([usage(at=TODAY)] + [
        contribution(str(i), at=TODAY, payload_changes={
            'object_ref': reference('object', i), 'extent': 'x' * 1024})
        for i in range(100)])
    doc = dashboard(ledger)
    _validate(doc)
    assert doc['status'] == 'available'
    view = today(doc)
    assert view['summary']['usage']['known_total_tokens'] == 100
    assert view['stages']['status'] == 'unavailable'
    assert view['stages']['reason'] == 'stage_payload_too_large'
    assert view['stages']['outputs'] is None
    assert view['stages']['diagnostics'] is None


def test_mixed_categories_and_reuse_are_not_one_grand_total(ledger):
    ledger.ingest_receipts([contribution(at=TODAY),
        contribution('reuse', at=TODAY, change='reused'),
        contribution('article', at=TODAY, payload_changes={
            'object_ref': reference('object', 'article'), 'category': 'article',
            'unit': 'paragraph', 'comparison_basis': 'reviewed-paragraph', 'quantity': '3'})])
    stage = today(dashboard(ledger))['stages']
    assert {(r['category'], r['unit'], r['contribution'], r['quantity'])
            for r in stage['outputs']['formed']} == {
                ('skill', 'capability', 'new', '1'), ('skill', 'capability', 'reused', '1'),
                ('article', 'paragraph', 'new', '3')}
    assert 'quantity' not in stage['outputs']['cumulative']
