"""Resolved store invariants and fixed-prefix view equivalence (WI754 step 3)."""
import os
import random

import pytest

from pulse_compute_cost.collaboration_query import CollaborationQuery, CollaborationQuerySession
from pulse_compute_cost.fact_index import PUBLIC, FactIndex, stream_identity
from pulse_compute_cost.incremental_views import IncrementalViews, compare_views, rebuild
from pulse_compute_cost.resolved_store import ResolvedStore, StoreMismatch
from pulse_compute_cost.stats_dashboard import ACTIVITY_DIMENSIONS, _view

from support.synthetic_ledger import generate, split, write

WINDOWS = (('2026-11-02', '2026-11-03'), ('2026-11-04', '2026-11-05'), ('2026-11-02', '2026-11-09'),
           ('2026-11-01', '2026-12-01'), ('2026-11-09', '2026-11-16'))
GROUPINGS = (ACTIVITY_DIMENSIONS, ('agent',), ('wi',), ('model',))


def make_ledger(tmp_path, seed=5, sessions=40, spacing=600):
    rows = generate(seed, sessions=sessions, spacing=spacing)
    ledger = write(tmp_path / 'ledger', split(rows, random.Random(seed)))
    derived = tmp_path / 'derived'
    derived.mkdir(mode=0o700, exist_ok=True)
    return ledger, derived


def open_pair(ledger, derived, name='resolved.sqlite', engine='e'):
    index = FactIndex.open(derived / 'fact-index.sqlite', stream_identity(ledger))
    store = ResolvedStore.open(derived / name, index_identity=index.identity(), engine=engine,
                               agent_tenants_sha256=None)
    return index, store


def slices(store):
    return dict(store._db.execute('SELECT component, slice_sha256 FROM package_slice'))


def resolved_rows(store):
    return sorted(store._db.execute('SELECT * FROM usage_resolved'))


def batch_by_batch(ledger, derived, name='resolved.sqlite'):
    index, store = open_pair(ledger, derived, name)
    for sequence in range(1, ledger.receipt_snapshot()['watermark'] + 1):
        index.catch_up(ledger, upto=sequence)
        store.refresh(index)
    return index, store


def test_another_binding_empties_the_store(tmp_path):
    ledger, derived = make_ledger(tmp_path, sessions=8)
    index, store = batch_by_batch(ledger, derived)
    assert slices(store) and store.meta()['sequence'] != '0'
    store.close()
    for changed in ({'engine': 'other'}, {'agent_tenants_sha256': 'x'}, {'index_identity': 'y'}):
        args = {'index_identity': index.identity(), 'engine': 'e', 'agent_tenants_sha256': None, **changed}
        reopened = ResolvedStore.open(derived / 'resolved.sqlite', **args)
        assert not slices(reopened) and reopened.meta()['sequence'] == '0'
        reopened.close()
        ResolvedStore.open(derived / 'resolved.sqlite', index_identity=index.identity(), engine='e',
                           agent_tenants_sha256=None).refresh(index)
    assert oct((derived / 'resolved.sqlite').stat().st_mode)[-3:] == '600'


def test_a_rebuilt_index_is_another_build(tmp_path):
    ledger, derived = make_ledger(tmp_path, sessions=8)
    index, store = batch_by_batch(ledger, derived)
    index.rebuild(ledger)
    with pytest.raises(StoreMismatch):
        store.refresh(index)


def test_each_usage_lies_in_one_package(tmp_path):
    ledger, derived = make_ledger(tmp_path)
    index, store = batch_by_batch(ledger, derived)
    owners = {}
    for entity, component in store._db.execute('SELECT DISTINCT entity_ref, component FROM usage_resolved'):
        assert owners.setdefault(entity, component) == component
        assert index.component_of(entity) == component
    live = {c for c, _, _ in index.packages()} | {PUBLIC}
    assert set(slices(store)) == live  # Merged-away packages left no slice behind.


def test_batch_by_batch_equals_a_rebuild(tmp_path):
    ledger, derived = make_ledger(tmp_path)
    index, store = batch_by_batch(ledger, derived)
    other = ResolvedStore.open(derived / 'rebuilt.sqlite', index_identity=index.identity(), engine='e',
                               agent_tenants_sha256=None)
    other.rebuild(index)
    assert slices(other) == slices(store)
    assert resolved_rows(other) == resolved_rows(store)


def test_only_changed_packages_are_recomputed(tmp_path):
    ledger, derived = make_ledger(tmp_path, sessions=10)
    index, store = batch_by_batch(ledger, derived)
    assert store.refresh(index)['recomputed'] == 0
    coverage = [r for r in ledger.receipt_snapshot()['facts'] if r.kind == 'coverage'][-1]
    from pulse_compute_cost.receipts import Receipt
    revised = Receipt.make('coverage', None, {**coverage.payload, 'observed': 99}, occurred_at=None,
                           observed_at='2026-11-20T00:00:00Z', producer=coverage.producer,
                           entity_ref=coverage.entity_ref, supersedes=coverage.fact_ref)
    ledger.ingest_receipts([revised])
    index.catch_up(ledger)
    before = slices(store)
    result = store.refresh(index)
    assert result['recomputed'] == 1 and result['public_recomputed']
    after = slices(store)
    assert {c for c in after if after[c] != before.get(c)} == {PUBLIC}


@pytest.mark.parametrize('seed,sessions,spacing', [(5, 40, 600), (7, 30, 1440), (11, 60, 300)])
def test_fixed_prefix_views_equal_the_whole_ledger(tmp_path, seed, sessions, spacing):
    ledger, derived = make_ledger(tmp_path, seed, sessions, spacing)
    head = ledger.receipt_snapshot()['watermark']
    marks = sorted({max(1, head // 3), max(1, 2 * head // 3), head})
    index, store = open_pair(ledger, derived)
    selected = []
    for watermark in marks:
        index.catch_up(ledger, upto=watermark)
        store.refresh(index)
        views = IncrementalViews(index, store, {})
        snapshot = ledger.receipt_snapshot(watermark=watermark)
        assert views.snapshot()['watermark'] == watermark
        assert views.snapshot()['recorded_at'] == snapshot['recorded_at']
        session = CollaborationQuerySession(snapshot, canonical_order=True)
        for window in WINDOWS:
            for grouping in GROUPINGS:
                q = CollaborationQuery(from_date=window[0], to_date=window[1], timezone='Asia/Shanghai',
                                       scope='activity', group_by=grouping, watermark=watermark)
                name = 'activity' if len(grouping) > 1 else grouping[0]
                whole = _view(session.query(q), 'p', name)
                mine = _view(views.query(q, catalog=None, catalog_observed_at=None), 'p', name)
                assert compare_views(whole, mine) == [], (watermark, window, grouping)
                assert store.reconcile(mine) == [], (watermark, window, grouping)
            selected.append(len(store.window_components(*q.window)))
    # Windows really select: some load fewer packages than the store holds.
    assert min(selected) < len(index.packages())


def test_reconcile_names_a_group_that_disagrees(tmp_path):
    ledger, derived = make_ledger(tmp_path, sessions=10, spacing=60)
    index, store = batch_by_batch(ledger, derived)
    views = IncrementalViews(index, store, {})
    q = CollaborationQuery(from_date='2026-11-02', to_date='2026-11-03', timezone='Asia/Shanghai',
                           scope='activity', group_by=ACTIVITY_DIMENSIONS, watermark=views.snapshot()['watermark'])
    view = _view(views.query(q, catalog=None, catalog_observed_at=None), 'today', 'activity')
    assert view['groups'] and store.reconcile(view) == []
    view['groups'][0]['usage']['event_count'] += 1
    found = store.reconcile(view)
    assert len(found) == 1 and found[0]['key'] == view['groups'][0]['key']


def test_attach_never_builds_and_rebuild_does(tmp_path):
    from pulse_compute_cost.incremental_views import IncrementalUnavailable
    ledger, derived = make_ledger(tmp_path, sessions=6)
    with pytest.raises(IncrementalUnavailable) as missing:
        IncrementalViews.attach(ledger, None, derived=derived)
    assert missing.value.reason == 'derived_state_missing'
    assert not (derived / 'resolved.sqlite').exists()
    result = rebuild(ledger, None, derived=derived)
    assert result['sequence'] == ledger.receipt_snapshot()['watermark'] and result['recomputed'] > 1
    attached = IncrementalViews.attach(ledger, None, derived=derived)
    assert attached.timings['recomputed'] == 0
    assert all(oct(p.stat().st_mode)[-3:] == '600' for p in derived.iterdir() if p.suffix == '.sqlite')
    assert oct(os.stat(derived).st_mode)[-3:] == '700'


def run_cli(*argv):
    import contextlib
    import io
    import json
    from pulse_compute_cost.cli import main
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, (json.loads(out.getvalue()) if code == 0 else err.getvalue())


def test_cli_rebuilds_and_audits_only_on_the_publisher(tmp_path):
    import json
    ledger, derived = make_ledger(tmp_path, sessions=12, spacing=600)
    registry = tmp_path / 'sources.json'
    registry.write_text(json.dumps({'ledger_root': str(tmp_path / 'ledger'), 'sources': []}))
    os.chmod(registry, 0o600)
    publisher, reader = tmp_path / 'refresh-publisher.json', tmp_path / 'refresh-reader.json'
    publisher.write_text('{"peers": []}')
    reader.write_text('{"publisher": "oracle-node-1"}')
    head = ledger.receipt_snapshot()['watermark']
    common = ['--registry', str(registry), '--derived', str(derived)]
    code, refused = run_cli('resolved', 'rebuild', *common, '--stats-config', str(reader))
    assert code == 69 and 'publisher' in refused
    assert not (derived / 'resolved.sqlite').exists()
    middle = max(1, head // 2)
    code, built = run_cli('resolved', 'rebuild', *common, '--stats-config', str(publisher),
                          '--watermark', str(middle))
    assert code == 0 and built['sequence'] == middle, built
    code, audited = run_cli('resolved', 'audit', *common, '--stats-config', str(publisher),
                            '--watermark', str(middle), '--now', '2026-11-10T04:00:00Z')
    assert code == 0 and audited['passed'], audited
    assert [v['period'] for v in audited['views']] == ['today', 'week', 'month', 'last_week']
    code, wrong = run_cli('resolved', 'audit', *common, '--stats-config', str(publisher), '--watermark', str(head))
    assert code == 1 and 'rebuild' in wrong
    import sqlite3
    with sqlite3.connect(derived / 'resolved.sqlite') as db:
        db.execute('DELETE FROM package_slice WHERE component = (SELECT MAX(component) FROM package_slice)')
    code, broken = run_cli('resolved', 'audit', *common, '--stats-config', str(publisher),
                           '--watermark', str(middle), '--now', '2026-11-10T04:00:00Z')
    assert code == 0 and not broken['passed'] and broken['missing_rows'], broken


def append_raw(ledger, rows):
    """A transaction as historical ledgers hold it, past today's ingestion checks."""
    from datetime import timedelta
    from pulse_compute_cost.events import parse_timestamp, utc_text
    from pulse_compute_cost.receipts import canonical, digest
    directory = ledger.root / 'receipts'
    last = json_load(sorted(directory.glob('*.json'))[-1])
    batch = {'schema': 'computecost_ledger_transaction/v1', 'sequence': last['sequence'] + 1,
             'recorded_at': utc_text(parse_timestamp(last['recorded_at'], 'at') + timedelta(seconds=1)),
             'previous': last['sha256'], 'facts': [r.to_dict() for r in rows]}
    batch['sha256'] = digest(batch)
    target = directory / f"{batch['sequence']:020d}.json"
    target.write_bytes(canonical(batch) + b'\n')
    os.chmod(target, 0o600)
    return type(ledger)(ledger.root)


def json_load(path):
    import json
    return json.loads(path.read_bytes())


def test_a_fact_ref_repeated_in_a_later_batch_is_judged_like_the_whole_ledger(tmp_path):
    # Oracle's history holds one fact_ref in several batches, differing in observed_at and
    # evidence only (the reference covers neither). The store reads facts_of as the ledger does.
    from pulse_compute_cost.receipts import Receipt
    ledger, derived = make_ledger(tmp_path, seed=7, sessions=12, spacing=600)
    facts = ledger.receipt_snapshot()['facts']
    repeated = []
    for row in [r for r in facts if r.kind == 'usage'][:3] + [r for r in facts if r.kind == 'attribution'][:2]:
        value = row.to_dict()
        value.update(observed_at='2026-11-21T00:00:00Z', evidence=sorted({*row.evidence, 'ccf1_' + '7' * 64}))
        again = Receipt.from_dict(value)
        assert again.fact_ref == row.fact_ref
        repeated.append(again)
    ledger = append_raw(ledger, repeated)
    snapshot = ledger.receipt_snapshot()
    assert sum(r.fact_ref == repeated[0].fact_ref for r in snapshot['facts']) == 2
    index, store = batch_by_batch(ledger, derived)
    for component, _, _ in index.packages():
        assert [r.fact_ref for r in index.facts_of(component)] == [
            r.fact_ref for r in snapshot['facts'] if index.component_of(r.entity_ref) == component]
    views = IncrementalViews(index, store, {})
    session = CollaborationQuerySession(snapshot, canonical_order=True)
    for window in WINDOWS:
        q = CollaborationQuery(from_date=window[0], to_date=window[1], timezone='Asia/Shanghai', scope='activity',
                               group_by=ACTIVITY_DIMENSIONS, watermark=snapshot['watermark'])
        whole = _view(session.query(q), 'p', 'activity')
        mine = _view(views.query(q, catalog=None, catalog_observed_at=None), 'p', 'activity')
        assert compare_views(whole, mine) == [], window
        assert store.reconcile(mine) == [], window


def test_a_publication_reads_one_store_state_while_another_refreshes(tmp_path):
    # Two connections: the second attach moves index and store to the head while the
    # first publication is still reading. The first keeps its watermark and its rows.
    ledger, derived = make_ledger(tmp_path, seed=5, sessions=40, spacing=600)
    head = ledger.receipt_snapshot()['watermark']
    early = head // 2
    rebuild(ledger, None, derived=derived, watermark=early)
    first = IncrementalViews.attach(ledger, None, upto=early, derived=derived)
    q = CollaborationQuery(from_date='2026-11-01', to_date='2026-12-01', timezone='Asia/Shanghai',
                           scope='activity', group_by=ACTIVITY_DIMENSIONS, watermark=early)
    second = IncrementalViews.attach(ledger, None, derived=derived)
    assert second.snapshot()['watermark'] == head and second.timings['recomputed'] > 0
    mine = _view(first.query(q, catalog=None, catalog_observed_at=None), 'month', 'activity')
    at_early = _view(CollaborationQuerySession(ledger.receipt_snapshot(watermark=early), canonical_order=True)
                     .query(q), 'month', 'activity')
    at_head = _view(CollaborationQuerySession(ledger.receipt_snapshot(), canonical_order=True)
                    .query(CollaborationQuery(**{**q.__dict__, 'watermark': head})), 'month', 'activity')
    assert at_early['summary']['usage']['event_count'] != at_head['summary']['usage']['event_count']
    assert first.snapshot()['watermark'] == early
    assert compare_views(at_early, mine) == []
    assert first.store.reconcile(mine) == []
    first.close()
    second.close()


def test_catching_up_with_nothing_new_reads_no_history(tmp_path, monkeypatch):
    from pulse_compute_cost.fact_index import ledger_head
    ledger, derived = make_ledger(tmp_path, sessions=8)
    index = FactIndex.open(derived / 'fact-index.sqlite', stream_identity(ledger))
    index.catch_up(ledger)
    bound = index.binding()
    assert ledger_head(ledger) == (bound.sequence, bound.head_sha256)

    def refuse(*args, **kwargs):
        raise AssertionError('history reread')

    monkeypatch.setattr(type(ledger), 'stream_batches', refuse)
    assert index.catch_up(ledger) == 0
    assert index.catch_up(ledger, upto=bound.sequence) == 0
    monkeypatch.undo()
    from pulse_compute_cost.receipts import Receipt
    execution = next(r for r in ledger.receipt_snapshot()['facts'] if r.kind == 'execution')
    assert ledger.ingest_receipts([Receipt.make('execution', ['another'], execution.payload,
                                                occurred_at=execution.occurred_at, observed_at=execution.observed_at,
                                                producer=execution.producer)])['inserted'] == 1
    assert index.catch_up(ledger) == 1 and index.binding().sequence == bound.sequence + 1


def test_audit_refuses_another_binding_and_leaves_the_store_alone(tmp_path):
    from pulse_compute_cost.incremental_views import audit
    ledger, derived = make_ledger(tmp_path, sessions=8)
    rebuild(ledger, None, derived=derived)
    head = ledger.receipt_snapshot()['watermark']
    before = (derived / 'resolved.sqlite').read_bytes(), (derived / 'fact-index.sqlite').read_bytes()
    tenants = {'pulse': {'id': 'bigbird', 'name': 'Bigbird', 'binding_ref': 'ccf1_' + '1' * 64}}
    with pytest.raises(StoreMismatch):
        audit(ledger, tenants, watermark=head, derived=derived)
    after = (derived / 'resolved.sqlite').read_bytes(), (derived / 'fact-index.sqlite').read_bytes()
    assert after == before
    import sqlite3
    with sqlite3.connect(derived / 'resolved.sqlite') as db:  # Still the rebuilt store, not an emptied one.
        assert dict(db.execute('SELECT key, value FROM meta'))['sequence'] == str(head)
        assert db.execute('SELECT COUNT(*) FROM package_slice').fetchone()[0] > 1
    assert audit(ledger, None, watermark=head, derived=derived, now=__import__('datetime').datetime(
        2026, 11, 10, 4, tzinfo=__import__('datetime').timezone.utc))['passed']


def test_a_refresh_reads_one_index_state_while_another_connection_extends_it(tmp_path):
    ledger, derived = make_ledger(tmp_path, seed=5, sessions=40, spacing=600)
    head = ledger.receipt_snapshot()['watermark']
    middle = head // 2
    index, store = open_pair(ledger, derived)
    index.catch_up(ledger, upto=middle)
    other_index = FactIndex.open(derived / 'fact-index.sqlite', stream_identity(ledger))
    packages = index.packages
    calls = []

    def extend_meanwhile():
        # Between reading the watermark and the packages, another connection moves the index on.
        if not calls:
            calls.append(other_index.catch_up(ledger))
        return packages()

    index.packages = extend_meanwhile
    result = store.refresh(index)
    assert calls and calls[0] > 0 and other_index.binding().sequence == head
    assert result['sequence'] == middle and store.meta()['sequence'] == str(middle)
    views = IncrementalViews(index, store, {})
    q = CollaborationQuery(from_date='2026-11-01', to_date='2026-12-01', timezone='Asia/Shanghai',
                           scope='activity', group_by=ACTIVITY_DIMENSIONS, watermark=middle)
    whole = _view(CollaborationQuerySession(ledger.receipt_snapshot(watermark=middle), canonical_order=True)
                  .query(q), 'month', 'activity')
    mine = _view(views.query(q, catalog=None, catalog_observed_at=None), 'month', 'activity')
    assert compare_views(whole, mine) == [] and store.reconcile(mine) == []
    views.store.end_read()
    # A second store connection refreshes to the head; the first then finds nothing left to do.
    second = ResolvedStore.open(derived / 'resolved.sqlite', index_identity=index.identity(), engine='e',
                                agent_tenants_sha256=None)
    assert second.refresh(other_index)['sequence'] == head
    index.packages = packages
    again = store.refresh(index)
    assert again == {'sequence': head, 'recomputed': 0, 'dropped': 0, 'public_recomputed': False}


def audit_passes(ledger, derived, now):
    from pulse_compute_cost.incremental_views import audit
    head = ledger.receipt_snapshot()['watermark']
    rebuild(ledger, None, derived=derived)
    result = audit(ledger, None, watermark=head, derived=derived, now=now)
    assert result['passed'], result
    return result


def test_the_failed_host_attempt_fixture_is_judged_as_in_the_whole_ledger(tmp_path):
    # Review of #1754: the Host execution and its two Pi requests share only the authenticated input.
    from datetime import datetime, timezone
    from test_host_native_input import HostNativeInputTests
    from pulse_compute_cost.host_native_input import authenticated_input_links
    from pulse_compute_cost.receipts import current_receipts
    fixture = HostNativeInputTests('test_failed_host_uses_real_native_requests_without_new_usage')
    fixture.setUp()
    try:
        fixture.collect()
        ledger = fixture.ledger
        rows, _ = current_receipts(ledger.receipt_snapshot()['facts'])
        links, _ = authenticated_input_links(rows)
        assert len(links) == 2 and {next(iter(v)) for v in links.values()} == {fixture.ex}
        derived = tmp_path / 'derived'
        derived.mkdir(mode=0o700)
        audit_passes(ledger, derived, datetime(2026, 9, 13, 4, tzinfo=timezone.utc))
        index = FactIndex.open(derived / 'fact-index.sqlite', stream_identity(ledger))
        assert {index.component_of(ref) for ref in links} == {index.component_of(fixture.ex)}
    finally:
        fixture.tmp.cleanup()


def test_a_binding_alias_is_judged_with_every_package_and_a_new_one_recomputes_its_dependents(tmp_path):
    from datetime import datetime, timezone
    from support.synthetic_ledger import pi_turn
    turn = pi_turn('aliased', 900, alias=True)
    alias = [r for r in turn if r.producer == 'pi_canonical_binding_alias.v1']
    rows = generate(22, sessions=6) + pi_turn('plain', 1200) + [r for r in turn if r not in alias]
    ledger = write(tmp_path / 'ledger', split(rows, random.Random(22)))
    derived = tmp_path / 'derived'
    derived.mkdir(mode=0o700)
    now = datetime(2026, 11, 2, 23, tzinfo=timezone.utc)
    audit_passes(ledger, derived, now)
    index = FactIndex.open(derived / 'fact-index.sqlite', stream_identity(ledger))
    from pulse_compute_cost.calculation_cache import engine_digest
    store = ResolvedStore.open(derived / 'resolved.sqlite', index_identity=index.identity(),
                               engine=engine_digest(), agent_tenants_sha256=None)
    dependent = index.component_of(next(r for r in turn if r.kind == 'usage').entity_ref)
    before = slices(store)
    ledger.ingest_receipts(alias)  # The alias arrives later, in a batch of its own.
    index.catch_up(ledger)
    result = store.refresh(index)
    after = slices(store)
    assert result['public_recomputed'] and after[dependent] != before[dependent]
    assert {c for c in after if after[c] != before.get(c)} <= {PUBLIC, dependent}
    store.close()
    from pulse_compute_cost.incremental_views import audit
    head = ledger.receipt_snapshot()['watermark']
    assert audit(ledger, None, watermark=head, derived=derived, now=now)['passed']


def test_a_damaged_tail_batch_is_not_taken_for_the_indexed_one(tmp_path, monkeypatch):
    import json
    from pulse_compute_cost.fact_index import ledger_head
    from pulse_compute_cost.ledger import LedgerError
    from pulse_compute_cost.receipts import canonical
    ledger, derived = make_ledger(tmp_path, sessions=8)
    index = FactIndex.open(derived / 'fact-index.sqlite', stream_identity(ledger))
    index.catch_up(ledger)
    tail = sorted((ledger.root / 'receipts').glob('*.json'))[-1]
    batch = json.loads(tail.read_bytes())
    batch['facts'][0]['observed_at'] = '2026-11-30T00:00:00Z'  # Its recorded sha256 now lies.
    tail.write_bytes(canonical(batch) + b'\n')
    assert ledger_head(type(ledger)(ledger.root)) is None
    streamed = []
    original = type(ledger).stream_batches

    def counting(self, **kwargs):
        streamed.append(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(type(ledger), 'stream_batches', counting)
    with pytest.raises(LedgerError):
        index.catch_up(type(ledger)(ledger.root))
    assert streamed  # The fast path declined; the full verified stream reported the damage.
