"""Per-package slices merged back equal the whole-ledger preparation (WI754 step 3)."""
import random

import pytest

from pulse_compute_cost import package_slices
from pulse_compute_cost.collaboration_query import (CollaborationQuery, CollaborationQuerySession, Prepared,
                                                    _prepare_collaboration, _query_snapshot)
from pulse_compute_cost.fact_index import FactIndex, stream_identity
from pulse_compute_cost.incremental_views import compare_views
from pulse_compute_cost.receipts import Receipt
from pulse_compute_cost.stats_dashboard import ACTIVITY_DIMENSIONS, _view

from support.synthetic_ledger import generate, split, write

WINDOWS = (('2026-11-02', '2026-11-03'), ('2026-11-02', '2026-11-09'), ('2026-11-01', '2026-12-01'),
           ('2026-11-09', '2026-11-16'))
GROUPINGS = (ACTIVITY_DIMENSIONS, ('agent',), ('wi',), ('model',))


def indexed(tmp_path, seed, sessions, spacing):
    rows = generate(seed, sessions=sessions, spacing=spacing)
    ledger = write(tmp_path / 'ledger', split(rows, random.Random(seed)))
    derived = tmp_path / 'derived'
    derived.mkdir(mode=0o700)
    index = FactIndex.open(derived / 'fact-index.sqlite', stream_identity(ledger))
    index.catch_up(ledger)
    return ledger, index


def query(window, grouping, watermark):
    return CollaborationQuery(from_date=window[0], to_date=window[1], timezone='Asia/Shanghai', scope='activity',
                              group_by=grouping, watermark=watermark)


def dimension(grouping):
    return 'activity' if len(grouping) > 1 else grouping[0]


def test_prepared_is_the_positional_tuple():
    prepared = _prepare_collaboration({'facts': []})
    assert isinstance(prepared, tuple) and len(prepared) == 19
    assert prepared.projected is prepared[7] and prepared.reconciliation is prepared[11]


def test_a_slice_decodes_to_the_values_it_encoded(tmp_path):
    _, index = indexed(tmp_path, 3, 12, 30)
    component = index.packages()[0][0]
    piece = package_slices.prepare_package(list(index.facts_of(component)))
    data = package_slices.encode_slice(piece)
    again = package_slices.decode_slice(data)
    assert again == piece
    assert package_slices.encode_slice(again) == data
    row = again['projected'][0]
    assert isinstance(row, Receipt) and row == piece['projected'][0]
    assert isinstance(again['attr_conflicts'], set)


@pytest.mark.parametrize('seed,sessions,spacing', [(3, 24, 30), (5, 40, 600), (7, 30, 1440)])
def test_every_slice_merged_gives_the_whole_ledger_views(tmp_path, seed, sessions, spacing):
    ledger, index = indexed(tmp_path, seed, sessions, spacing)
    snapshot = ledger.receipt_snapshot()
    pieces = [package_slices.decode_slice(package_slices.encode_slice(
        package_slices.prepare_public(list(index.public_facts()))))]
    pieces += [package_slices.decode_slice(package_slices.encode_slice(
        package_slices.prepare_package(list(index.facts_of(c))))) for c, _, _ in index.packages()]
    prepared, history = package_slices.merge_slices(pieces, package_slices.merge_globals(pieces))
    session = CollaborationQuerySession(snapshot, canonical_order=True)
    for window in WINDOWS:
        for grouping in GROUPINGS:
            q = query(window, grouping, snapshot['watermark'])
            whole = _view(session.query(q), 'p', dimension(grouping))
            merged = _view(_query_snapshot({**snapshot, 'facts': history}, q, prepared, catalog=None,
                                           catalog_observed_at=None, canonical_order=True), 'p', dimension(grouping))
            assert compare_views(whole, merged) == [], (window, grouping)
    whole = Prepared(*_prepare_collaboration(snapshot))
    assert prepared.all_wis == whole.all_wis and prepared.accepted_at == whole.accepted_at
    assert prepared.attribution_conflict_wis == whole.attribution_conflict_wis


def test_canonical_order_changes_only_row_order(tmp_path):
    ledger, _ = indexed(tmp_path, 5, 40, 600)
    snapshot = ledger.receipt_snapshot()
    q = query(WINDOWS[2], ACTIVITY_DIMENSIONS, snapshot['watermark'])
    plain = CollaborationQuerySession(snapshot).query(q)
    ordered = CollaborationQuerySession(snapshot, canonical_order=True).query(q)
    for report in (plain, ordered):
        for group in report['groups']:
            group['cost']['unpriced'] = sorted(group['cost']['unpriced'], key=lambda u: u['fact_ref'])
        report['summary']['cost']['unpriced'] = sorted(report['summary']['cost']['unpriced'],
                                                       key=lambda u: u['fact_ref'])
    assert compare_views({k: v for k, v in plain.items() if k != 'report_sha256'},
                         {k: v for k, v in ordered.items() if k != 'report_sha256'}) == []


def test_bounds_open_a_package_with_public_facts_or_no_time():
    rows = generate(3, sessions=2)
    usage = next(r for r in rows if r.kind == 'usage' and r.payload['granularity'] == 'request')
    low, high, open_ended = package_slices.bounds([usage])
    assert low == high == package_slices.instant_text(usage.occurred_at) and not open_ended
    counter = next(r for r in rows if r.payload.get('granularity') == 'turn_aggregate')
    assert package_slices.bounds([counter])[0] is not None  # It has an interval start.
    unstarted = Receipt(counter.kind, counter.entity_ref, counter.fact_ref, counter.occurred_at, counter.observed_at,
                        counter.producer, counter.evidence, counter.supersedes,
                        counter._payload_json.replace(counter.payload['interval_start'].encode(), b'x', 1)
                        .replace(b'"x"', b'null', 1))
    assert package_slices.bounds([unstarted])[0] is None
    coverage = next(r for r in rows if r.kind == 'coverage')
    assert package_slices.bounds([usage, coverage])[2]
    assert package_slices.bounds([])[2]
