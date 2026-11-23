"""The cost publication's engines: full stays as it was, shadow compares, incremental equals (WI754 step 3)."""
import contextlib
import io
import json
import os
import random

import pytest

from pulse_compute_cost import stats_dashboard
from pulse_compute_cost.incremental_modes import incremental_modes
from pulse_compute_cost.incremental_views import IncrementalViews, rebuild
from pulse_compute_cost.stats_dashboard import LAST_WEEK, PublicationWithheld, build_dashboard

from support.synthetic_ledger import generate, split, write

FACTS = {'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []}
NOW = __import__('datetime').datetime(2026, 11, 20, 4, tzinfo=__import__('datetime').timezone.utc)


def ledger_with_store(tmp_path, sessions=40, spacing=600):
    ledger = write(tmp_path / 'ledger', split(generate(5, sessions=sessions, spacing=spacing), random.Random(5)))
    derived = tmp_path / 'derived'
    derived.mkdir(mode=0o700)
    rebuild(ledger, None, derived=derived)
    return ledger, derived


def publish(ledger, derived=None, **kwargs):
    factory = (lambda upto: IncrementalViews.attach(ledger, None, upto=upto, derived=derived)) if derived else None
    timings = {}
    result = build_dashboard(ledger, subscriptions=FACTS, source_revision='a' * 40, now=NOW,
                             identity_dimensions=True, activity_table=True, timings=timings,
                             incremental=factory, **kwargs)
    return result, timings


def views(document):
    return [{k: v for k, v in view.items() if k != 'report_sha256'} for view in document['views']]


def original_queries(ledger, document, catalog=None):
    """Each incremental view is the original query at its watermark read in canonical order, digest included.

    What `verify_installed_dashboard.verify_original_queries` checks after a deployment.
    """
    from pulse_compute_cost.collaboration_query import CollaborationQuery, CollaborationQuerySession
    from pulse_compute_cost.stats_dashboard import _view
    sessions = {}
    for view in document['views']:
        q = dict(view['query'], group_by=tuple(view['query']['group_by']),
                 work_items=tuple(view['query']['work_items']))
        if q['watermark'] not in sessions:
            sessions[q['watermark']] = CollaborationQuerySession(
                ledger.receipt_snapshot(watermark=q['watermark']), canonical_order=True)
        raw = sessions[q['watermark']].query(CollaborationQuery(**q), catalog=catalog,
                                             catalog_observed_at=view['catalog_observed_at'])
        assert raw['report_sha256'] == view['report_sha256'], (view['period'], view['dimension'])
        assert raw['facts_sha256'] == ledger.receipt_snapshot(watermark=q['watermark'])['sha256']
        whole = _view(raw, view['period'], view['dimension'])
        assert {k: v for k, v in view.items() if k in whole} == whole


def same_views(published, mine):
    from pulse_compute_cost.incremental_views import compare_views
    assert len(published['views']) == len(mine['views'])
    for full, incremental in zip(published['views'], mine['views']):
        assert [full[k] for k in ('period', 'dimension', 'query', 'watermark', 'recorded_at')] == \
               [incremental[k] for k in ('period', 'dimension', 'query', 'watermark', 'recorded_at')]


def test_views_switch_names_the_engine():
    assert stats_dashboard.VIEW_ENGINES[incremental_modes({})['views']] == 'full'
    assert stats_dashboard.VIEW_ENGINES[incremental_modes({'incremental': {'views': 'shadow'}})['views']] == 'shadow'
    assert stats_dashboard.VIEW_ENGINES[incremental_modes({'incremental': {'views': 'on'}})['views']] == 'incremental'
    assert set(stats_dashboard.VIEW_ENGINES.values()) == set(stats_dashboard.ENGINES)


def test_full_engine_opens_no_derived_state(tmp_path):
    ledger = write(tmp_path / 'ledger', split(generate(3, sessions=6), random.Random(3)))
    before = sorted(p.name for p in (tmp_path / 'ledger').iterdir())
    result, timings = publish(ledger)
    assert result['status'] == 'available'
    assert sorted(p.name for p in (tmp_path / 'ledger').iterdir()) == before
    assert 'incremental' not in timings and 'incremental_shadow' not in timings


def test_incremental_equals_shadow_full_and_shadow_reports_equal(tmp_path):
    ledger, derived = ledger_with_store(tmp_path)
    shadow, shadow_timings = publish(ledger, derived, engine='shadow')
    record = shadow_timings['incremental_shadow'][-1]
    assert record['status'] == 'equal', record
    assert len(record['views']) == len(shadow['views']) and all(v['equal'] for v in record['views'])
    incremental, timings = publish(ledger, derived, engine='incremental')
    assert timings['incremental']['status'] == 'attached'
    assert 'ledger_snapshot_seconds' in timings
    same_views(shadow, incremental)
    original_queries(ledger, incremental)
    last_shadow, _ = publish(ledger, derived, engine='shadow', periods=LAST_WEEK, activity_only=True)
    last_incremental, _ = publish(ledger, derived, engine='incremental', periods=LAST_WEEK, activity_only=True)
    same_views(last_shadow, last_incremental)
    original_queries(ledger, last_incremental)


def test_shadow_publishes_exactly_the_full_document(tmp_path):
    ledger, derived = ledger_with_store(tmp_path)
    full, _ = publish(ledger)
    shadow, timings = publish(ledger, derived, engine='shadow')
    assert timings['incremental_shadow'][-1]['status'] == 'equal'
    assert len(full['views']) == 21
    sampled = [v for v in full['views'] if 'unpriced_sha256' in v['summary']['cost']]
    assert sampled  # The order-dependent samples exist and are published unchanged.
    assert shadow == full
    assert [v['report_sha256'] for v in shadow['views']] == [v['report_sha256'] for v in full['views']]


def nine_unpriced(refs):
    """A report and its published view whose summary lists nine unpriced facts of one reason."""
    from pulse_compute_cost.stats_dashboard import project_cost
    cost = {'machine_cash_usd': None, 'partial_machine_cash_usd': None, 'cash_basis': 'openrouter_standard_token_cash',
            'reasons': ['incomplete_price_or_quantity'],
            'unpriced': [{'fact_ref': ref, 'reason': 'model_unavailable'} for ref in refs]}
    report = {'summary': {'cost': cost}, 'groups': []}
    return report, {'summary': {'cost': project_cost(cost)}, 'groups': []}


NINE = ['ccf1_' + str(n) * 64 for n in range(1, 10)]


def test_nine_unpriced_of_one_reason_in_another_order_are_equal():
    from pulse_compute_cost.incremental_views import compare_views, unpriced_witness
    full, full_view = nine_unpriced(NINE)
    mine, mine_view = nine_unpriced(NINE[::-1])
    assert full_view['summary']['cost']['unpriced_count'] == 9  # Sampled: two shown, the rest by count.
    assert full_view != mine_view  # The published samples and row-order digest differ ...
    assert compare_views(full_view, mine_view, unpriced_witness(full), unpriced_witness(mine)) == []  # ... the facts do not.


def test_nine_unpriced_of_one_reason_with_one_other_fact_differ():
    from pulse_compute_cost.incremental_views import compare_views, unpriced_witness
    full, full_view = nine_unpriced(NINE)
    swapped = NINE[:-1] + ['ccf1_' + 'a' * 64]  # Same count, same reason, same two samples.
    mine, mine_view = nine_unpriced(swapped)
    assert full_view['summary']['cost']['unpriced'] == mine_view['summary']['cost']['unpriced']
    assert compare_views(full_view, mine_view, unpriced_witness(full), unpriced_witness(mine)) == [
        '/summary/cost/unpriced_witness']
    # Without witnesses a sampled list is compared as published, never dropped.
    assert compare_views(full_view, mine_view) == ['/summary/cost/unpriced_sha256']


def test_missing_store_falls_back_to_full_and_shadow_never_withholds(tmp_path):
    ledger = write(tmp_path / 'ledger', split(generate(3, sessions=6), random.Random(3)))
    derived = tmp_path / 'derived'
    derived.mkdir(mode=0o700)
    full, _ = publish(ledger)
    fallback, timings = publish(ledger, derived, engine='incremental')
    assert timings['incremental'] == {'status': 'unavailable', 'reason': 'derived_state_missing',
                                      'seconds': timings['incremental']['seconds']}
    assert views(fallback) == views(full)
    shadow, timings = publish(ledger, derived, engine='shadow')
    assert shadow['status'] == 'available'
    assert timings['incremental_shadow'][-1]['status'] == 'unavailable'


def test_a_view_the_store_does_not_reconcile_is_withheld(tmp_path, monkeypatch):
    ledger, derived = ledger_with_store(tmp_path, sessions=8, spacing=60)
    from pulse_compute_cost.resolved_store import ResolvedStore
    monkeypatch.setattr(ResolvedStore, 'reconcile', lambda self, view: [{'key': {}, 'resolved': None, 'view': {}}])
    with pytest.raises(PublicationWithheld) as withheld:
        publish(ledger, derived, engine='incremental')
    assert withheld.value.reason == 'resolved_view_mismatch'
    assert stats_dashboard.failure_reason(withheld.value) == 'resolved_view_mismatch'
    shadow, timings = publish(ledger, derived, engine='shadow')
    assert shadow['status'] == 'available' and timings['incremental_shadow'][-1]['status'] == 'different'


def test_main_passes_the_engine(tmp_path):
    ledger, derived = ledger_with_store(tmp_path, sessions=6, spacing=60)
    os.rename(derived, tmp_path / 'ledger' / 'derived')
    out = tmp_path / 'out' / 'cost.json'
    out.parent.mkdir(mode=0o700)
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
        code = stats_dashboard.main(['--ledger-root', str(tmp_path / 'ledger'), '--source-revision', 'a' * 40,
                                     '--output', str(out), '--engine', 'shadow'])
    assert code == 0, stderr.getvalue()
    timing = json.loads(stderr.getvalue().splitlines()[0])
    assert timing['incremental_shadow'][-1]['status'] == 'equal', timing['incremental_shadow']


def test_a_missing_key_differs_from_null():
    from pulse_compute_cost.incremental_views import compare_views
    assert compare_views({'a': None}, {}) == ['/a']
    assert compare_views({'a': {'b': None}}, {'a': {}}) == ['/a/b']
    assert compare_views({'a': None, 'report_sha256': 'x'}, {'a': None, 'report_sha256': 'y'}) == []


CATALOG = {'data': [{'id': 'opus', 'canonical_slug': 'anthropic/opus',
                     'pricing': {'prompt': '0.000002', 'completion': '0.00001',
                                 'input_cache_read': '0.0000002', 'input_cache_write': '0.000002'}}]}


def test_shadow_prices_at_the_published_observation_time(tmp_path):
    # A later retrieval of the same catalog reuses the cached full view (older observation time).
    ledger, derived = ledger_with_store(tmp_path, sessions=8, spacing=600)
    cache = tmp_path / 'cache'
    for retrieved in ('2026-11-20T01:00:00+00:00', '2026-11-20T03:00:00+00:00'):
        _, timings = publish(ledger, derived, engine='shadow', calculation_cache=cache,
                             catalog_snapshot={'catalog': CATALOG, 'retrieved_at': retrieved})
        assert timings['incremental_shadow'][-1]['status'] == 'equal', timings['incremental_shadow']
    assert any(v['cache_hit'] for v in timings['views'])


def test_a_cached_incremental_view_is_still_reconciled(tmp_path, monkeypatch):
    ledger, derived = ledger_with_store(tmp_path, sessions=8, spacing=60)
    cache = tmp_path / 'cache'
    publish(ledger, derived, engine='incremental', calculation_cache=cache)
    _, timings = publish(ledger, derived, engine='incremental', calculation_cache=cache)
    assert all(v['cache_hit'] for v in timings['views'])
    from pulse_compute_cost.resolved_store import ResolvedStore
    monkeypatch.setattr(ResolvedStore, 'reconcile', lambda self, view: [{'key': {}, 'resolved': None, 'view': {}}])
    with pytest.raises(PublicationWithheld):
        publish(ledger, derived, engine='incremental', calculation_cache=cache)
