"""Fact index: batching-independent content and partition, resume, prefix binding."""
import json
import random
import shutil
import sqlite3
import stat
import time

from datetime import timedelta

import pytest

from pulse_compute_cost import fact_index as fact_index_module
from pulse_compute_cost import session_packages
from pulse_compute_cost.fact_index import PUBLIC, FactIndex, IndexMismatch, assertion_key, stream_identity

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, canonical, digest, reference
from support.synthetic_ledger import T0, generate, split, write


def build(tmp_path, name, rows, rnd, *, upto=None):
    ledger = write(tmp_path / name, split(rows, rnd))
    index = FactIndex.open(tmp_path / name / 'derived' / 'fact-index.sqlite', stream_identity(ledger))
    index.catch_up(ledger, upto=upto)
    return ledger, index


def content(index):
    return sorted(index._db.execute('SELECT fact_ref, entity_ref, kind, producer, supersedes, assertion_key, body '
                                    'FROM fact').fetchall())


def partition(index):
    groups = {}
    for ref, component in index._db.execute('SELECT fact_ref, component FROM fact'):
        groups.setdefault(component, set()).add(ref)
    public = frozenset(groups.pop(PUBLIC, ()))
    return public, {frozenset(refs) for refs in groups.values()}


def check_invariants(ledger, index):
    # Every ledger fact (by batch and position) has exactly one row, in ledger order.
    lines = [line for _, batch in ledger.stream_batches() for line in batch]
    stored = [fact_index_module.zlib.decompress(body) for (body,) in index._db.execute('SELECT body FROM fact ORDER BY id')]
    assert stored == lines and len(lines) == index.stats()['facts']
    db = index._db
    assert not db.execute('SELECT entity_ref FROM fact GROUP BY entity_ref '
                          'HAVING COUNT(DISTINCT component) > 1').fetchall()
    assert not db.execute('SELECT 1 FROM fact c JOIN fact p ON c.supersedes = p.fact_ref '
                          'WHERE c.component != p.component').fetchall()
    counts = dict(db.execute('SELECT component, COUNT(*) FROM fact WHERE component != 0 GROUP BY component'))
    assert counts == {c: n for c, n, _ in index.packages()}
    assert index.binding().sequence == len(list(ledger.stream_batches()))


@pytest.mark.parametrize('seed', range(5))
def test_any_batching_gives_the_same_content_and_partition(tmp_path, seed):
    rows = generate(seed, sessions=10 + seed)
    rnd = random.Random(seed)
    built = [build(tmp_path, f'ledger{i}', rows, rnd) for i in range(3)]
    whole = write(tmp_path / 'whole', [rows])
    one = FactIndex.open(tmp_path / 'whole.sqlite', stream_identity(whole))
    one.catch_up(whole)
    for ledger, index in built:
        check_invariants(ledger, index)
        assert content(index) == content(one)
        assert partition(index) == partition(one)
    public, packages = partition(one)
    assert public
    # Subcalls and parent executions joined some sessions; shared assignment refs joined none.
    executions = {}
    for row in rows:
        if row.kind == 'execution':
            executions.setdefault(one.component_of(row.entity_ref), []).append(row)
    assert max(len(group) for group in executions.values()) > 1 and len(executions) > 1


def test_interrupted_catch_up_resumes_to_the_same_index(tmp_path, monkeypatch):
    rows = generate(7, sessions=12)
    rnd = random.Random(7)
    batches = split(rows, rnd)
    while len(batches) < 4:
        batches = split(rows, rnd)
    ledger = write(tmp_path / 'ledger', batches)
    path = tmp_path / 'derived' / 'fact-index.sqlite'
    index = FactIndex.open(path, stream_identity(ledger))
    assert index.catch_up(ledger, upto=2) == 2
    calls = [0]
    original = session_packages.link_nodes

    def crash(row):
        calls[0] += 1
        if calls[0] == len(batches[2]) // 2 + 1:
            raise RuntimeError('killed mid-batch')
        return original(row)

    monkeypatch.setattr(session_packages, 'link_nodes', crash)
    with pytest.raises(RuntimeError):
        index.catch_up(ledger)
    monkeypatch.setattr(session_packages, 'link_nodes', original)
    index.close()
    index = FactIndex.open(path, stream_identity(ledger))
    assert index.binding().sequence == 2  # The broken batch left nothing behind.
    assert index.catch_up(ledger) == len(batches) - 2
    assert index.catch_up(ledger) == 0
    fresh = FactIndex.open(tmp_path / 'fresh.sqlite', stream_identity(ledger))
    fresh.rebuild(ledger)
    check_invariants(ledger, index)
    assert content(index) == content(fresh) and partition(index) == partition(fresh)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_rewritten_ledger_is_not_the_indexed_prefix(tmp_path):
    rows = generate(8, sessions=8)
    ledger, index = build(tmp_path, 'ledger', rows, random.Random(8))
    with pytest.raises(IndexMismatch):
        index.catch_up(ledger, upto=0)
    shutil.rmtree(tmp_path / 'ledger' / 'receipts')
    # Same facts, other batches: every batch digest changes.
    write(tmp_path / 'ledger', [rows[:3], rows[3:]])
    with pytest.raises(IndexMismatch):
        index.catch_up(ledger)
    shutil.rmtree(tmp_path / 'ledger' / 'receipts')
    write(tmp_path / 'ledger', [rows[:3]])
    with pytest.raises(IndexMismatch):
        index.catch_up(ledger)
    assert index.rebuild(ledger) == 1 and index.stats()['facts'] == 3


def test_changed_link_logic_or_stream_empties_the_index(tmp_path, monkeypatch):
    rows = generate(9, sessions=4)
    ledger, index = build(tmp_path, 'ledger', rows, random.Random(9))
    path = index.path
    index.close()
    assert FactIndex.open(path, stream_identity(ledger)).binding().sequence > 0
    assert FactIndex.open(path, 'another stream').binding().sequence == 0
    FactIndex.open(path, stream_identity(ledger)).catch_up(ledger)
    monkeypatch.setattr(session_packages, 'LINK_LOGIC', 'changed')
    assert FactIndex.open(path, stream_identity(ledger)).stats()['facts'] == 0


def test_point_reads_match_the_ledger(tmp_path):
    rows = generate(10, sessions=16)
    _, index = build(tmp_path, 'ledger', rows, random.Random(10))
    superseded = {r.supersedes for r in rows if r.supersedes}
    for row in rows:
        assert index.get(row.fact_ref) == row
        assert index.versions(row.entity_ref) == [r for r in rows if r.entity_ref == row.entity_ref]
        assert index.children(row.fact_ref) == [r for r in rows if r.supersedes == row.fact_ref]
        assert index.active_by_assertion(assertion_key(row)) == [
            r for r in rows if assertion_key(r) == assertion_key(row) and r.fact_ref not in superseded]
        nodes = session_packages.link_nodes(row)
        if nodes:
            members = list(index.facts_of(index.component_of(nodes[0])))
            assert row in members and all(index.component_of(n) == index.component_of(nodes[0]) for n in nodes)
        else:
            assert row in list(index.public_facts())
    assert index.get('ccf1_' + '0' * 64) is None
    assert set(index.components_changed_since(0)) == {c for c, _, _ in index.packages()}
    assert index.components_changed_since(index.binding().sequence) == []
    assert list(index.public_facts(upto_sequence=0)) == []


def append_raw(root, batch_rows):
    """Append batches verbatim, as historical ingest did, bypassing today's duplicate rewrite."""
    directory = root / 'receipts'
    batches = sorted(directory.glob('*.json'))
    previous = json.loads(batches[-1].read_bytes())['sha256'] if batches else None
    for n, rows in enumerate(batch_rows, len(batches) + 1):
        batch = {'schema': 'computecost_ledger_transaction/v1', 'sequence': n,
                 'recorded_at': (T0 + timedelta(hours=1, seconds=n)).isoformat().replace('+00:00', 'Z'),
                 'previous': previous, 'facts': [r.to_dict() for r in rows]}
        batch['sha256'] = previous = digest(batch)
        path = directory / f'{n:020d}.json'
        path.write_bytes(canonical(batch) + b'\n')
        path.chmod(0o600)
    return UsageLedger(root)


def restated(row, n):
    """The same fact ref with a later observation and more evidence: different bytes."""
    again = Receipt.make(row.kind, None, row.payload, occurred_at=row.occurred_at,
                         observed_at=f'2026-11-2{n}T00:00:00Z', producer=row.producer,
                         evidence=tuple(sorted({*row.evidence, reference('proof', ['restated', n])})), entity_ref=row.entity_ref,
                         supersedes=row.supersedes)
    assert again.fact_ref == row.fact_ref and canonical(again.to_dict()) != canonical(row.to_dict())
    return again


def test_a_fact_ref_repeated_across_batches_keeps_every_occurrence(tmp_path):
    rows = generate(11, sessions=8)
    rnd = random.Random(11)
    picked = rnd.sample(rows, 6)
    batches = split(rows, rnd) + [[restated(r, 1) for r in picked[:4]], [restated(r, 2) for r in picked[2:]]]
    (tmp_path / 'ledger' / 'receipts').mkdir(parents=True, mode=0o700)
    ledger = append_raw(tmp_path / 'ledger', batches)
    snapshot = ledger.receipt_snapshot()['facts']
    assert len({r.fact_ref for r in snapshot}) < len(snapshot)

    index = FactIndex.open(tmp_path / 'one.sqlite', stream_identity(ledger))
    index.catch_up(ledger)
    check_invariants(ledger, index)
    assert index.facts_digest() == ledger.receipt_snapshot()['sha256']
    in_order = [r for c in [PUBLIC, *(c for c, _, _ in index.packages())] for r in index.facts_of(c, ids=True)]
    assert [r for _, r in sorted(in_order, key=lambda p: p[0])] == snapshot
    assert not index._db.execute('SELECT fact_ref FROM fact GROUP BY fact_ref '
                                 'HAVING COUNT(DISTINCT component) > 1').fetchall()

    known = {r.fact_ref: r for r in snapshot}  # Ingest's view: first position, latest occurrence.
    superseded = {r.supersedes for r in known.values() if r.supersedes}
    for row in picked:
        assert index.get(row.fact_ref) == known[row.fact_ref] != row
        assert index.versions(row.entity_ref) == [r for r in known.values() if r.entity_ref == row.entity_ref]
        assert index.children(row.fact_ref) == [r for r in known.values() if r.supersedes == row.fact_ref]
        assert index.active_by_assertion(assertion_key(row)) == [
            r for r in known.values() if assertion_key(r) == assertion_key(row) and r.fact_ref not in superseded]

    stepwise = FactIndex.open(tmp_path / 'step.sqlite', stream_identity(ledger))
    for upto in range(1, len(batches) + 1):
        stepwise.catch_up(ledger, upto=upto)
    assert content(stepwise) == content(index) and partition(stepwise) == partition(index)


def test_a_fact_ref_repeated_within_one_batch_keeps_every_occurrence(tmp_path):
    rows = generate(12, sessions=2)
    (tmp_path / 'ledger' / 'receipts').mkdir(parents=True, mode=0o700)
    first, second = restated(rows[3], 1), restated(rows[3], 2)
    ledger = append_raw(tmp_path / 'ledger', [rows[:3], [rows[3], first, *rows[4:6], second]])
    index = FactIndex.open(tmp_path / 'index.sqlite', stream_identity(ledger))
    assert index.catch_up(ledger) == 2
    check_invariants(ledger, index)
    assert index.facts_digest() == ledger.receipt_snapshot()['sha256']
    assert index.get(rows[3].fact_ref) == second  # Latest by position inside the batch.
    assert index.versions(rows[3].entity_ref) == [second]


def test_an_index_of_another_schema_is_dropped_for_rebuild(tmp_path):
    path = tmp_path / 'derived' / 'fact-index.sqlite'
    path.parent.mkdir(mode=0o700)
    old = sqlite3.connect(path)
    old.executescript(fact_index_module._TABLES.replace('fact_ref BLOB NOT NULL,', 'fact_ref BLOB NOT NULL UNIQUE,'))
    old.executemany('INSERT INTO meta VALUES (?, ?)', [('schema_version', '1'), ('stream_identity', 's'),
                                                      ('link_logic', session_packages.LINK_LOGIC),
                                                      ('sequence', '5'), ('head_sha256', 'x')])
    old.commit()
    old.close()
    path.chmod(0o600)
    index = FactIndex.open(path, 's')
    assert index.binding().sequence == 0
    sql = index._db.execute("SELECT sql FROM sqlite_master WHERE name = 'fact'").fetchone()[0]
    assert 'UNIQUE' not in sql


def test_a_failed_schema_migration_rolls_back_and_retries(tmp_path, monkeypatch):
    path = tmp_path / 'derived' / 'fact-index.sqlite'
    path.parent.mkdir(mode=0o700)
    old = sqlite3.connect(path)
    old.executescript(fact_index_module._TABLES.replace('fact_ref BLOB NOT NULL,', 'fact_ref BLOB NOT NULL UNIQUE,'))
    old.executemany('INSERT INTO meta VALUES (?, ?)', [('schema_version', '1'), ('stream_identity', 's'),
                                                      ('link_logic', session_packages.LINK_LOGIC),
                                                      ('sequence', '5'), ('head_sha256', 'x')])
    old.commit()
    old.close()
    path.chmod(0o600)

    class FailingDrop(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith('DROP TABLE IF EXISTS fact'):
                raise sqlite3.OperationalError('injected')
            return super().execute(sql, *args)

    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: connect(*a, factory=FailingDrop, **k))
    with pytest.raises(sqlite3.OperationalError, match='injected'):
        FactIndex.open(path, 's')
    monkeypatch.setattr(sqlite3, 'connect', connect)
    check = sqlite3.connect(path)
    assert dict(check.execute('SELECT key, value FROM meta'))['schema_version'] == '1'  # Nothing half-done.
    check.close()
    index = FactIndex.open(path, 's')
    assert index.binding().sequence == 0
    assert 'UNIQUE' not in index._db.execute("SELECT sql FROM sqlite_master WHERE name = 'fact'").fetchone()[0]


# Statement family (substrings of the executed SQL) -> the index it must search.
PLANS = {
    ('UPDATE fact', 'WHERE entity_ref'): 'fact_entity',
    ('UPDATE fact', 'WHERE component'): 'fact_component',
    ('UPDATE link', 'WHERE component'): 'link_component',
    ('FROM link WHERE node =',): 'PRIMARY KEY',
    ('FROM link WHERE node IN',): 'PRIMARY KEY',
    ('FROM component WHERE id IN',): 'INTEGER PRIMARY KEY',
    ('SELECT body FROM fact', 'WHERE fact_ref'): 'fact_by_ref',
    ('SELECT component FROM fact', 'WHERE fact_ref'): 'fact_by_ref',
    ('SELECT component FROM fact', 'WHERE entity_ref'): 'fact_entity',
    ('MIN(id)', 'WHERE entity_ref'): 'fact_entity',
    ('MIN(id)', 'WHERE supersedes'): 'fact_supersedes',
    ('MIN(id)', 'WHERE assertion_key'): 'fact_assertion',
    ('SELECT id, body FROM fact', 'WHERE component'): 'fact_component',
}


def test_every_lookup_searches_its_index_without_statistics(tmp_path):
    rows = generate(13, sessions=12)
    ledger = write(tmp_path / 'ledger', split(rows, random.Random(13)))
    index = FactIndex.open(tmp_path / 'index.sqlite', stream_identity(ledger))
    seen = []
    index._db.set_trace_callback(seen.append)
    index.catch_up(ledger)
    for row in rows[:20]:
        index.get(row.fact_ref), index.versions(row.entity_ref), index.children(row.fact_ref)
        index.active_by_assertion(assertion_key(row)), index.component_of(row.entity_ref)
        index.component_of(row.fact_ref)
    for component, _, _ in index.packages()[:3]:
        list(index.facts_of(component, upto_sequence=1))
    index._db.set_trace_callback(None)
    assert not index._db.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone()
    covered = set()
    for sql in set(seen):
        family = next((k for k in PLANS if all(part in sql for part in k)), None)
        if family is None:
            continue
        covered.add(family)
        plan = [r[3] for r in index._db.execute('EXPLAIN QUERY PLAN ' + sql)]
        searches = [p for p in plan if p.startswith(('SEARCH', 'SCAN'))]
        assert searches and searches[0].startswith('SEARCH') and PLANS[family] in searches[0], (sql, plan)
        assert not any(p.startswith('SCAN fact') or p.startswith('SCAN link') for p in plan), (sql, plan)
    # Merges and the public move must have run, or the check proves nothing for them.
    assert set(PLANS) <= covered


def test_linking_next_to_a_large_public_package_stays_linear(tmp_path):
    index = FactIndex.open(tmp_path / 'index.sqlite', 'smoke')
    public = [Receipt.make('coverage', ['smoke', n], {
        'domain': 'usage', 'wi': None, 'source_ref': reference('source', n), 'status': 'unknown', 'expected': None,
        'observed': n, 'watermark': str(n), 'reason': None}, occurred_at=None, observed_at='2026-11-20T00:00:00Z',
        producer='registered_collector.v1') for n in range(20000)]
    linked = [Receipt.make('execution', ['smoke', n], {
        'wi': 'wi_20261113_754', 'executor': 'pulse', 'responsible': 'pulse', 'delegator': None, 'runtime': 'cc',
        'assignment_ref': None, 'parent_ref': reference('execution', ['parent', n]), 'state': 'completed',
        'expected_usage': True}, occurred_at='2026-11-20T00:00:00Z', observed_at='2026-11-20T00:00:00Z',
        producer='relay_host.v1') for n in range(1000)]
    with index._transaction():
        for row in public:
            index._insert(row, canonical(row.to_dict()), 1)
    assert index.stats()['public_package'] == 20000
    started = time.perf_counter()
    with index._transaction():
        for row in linked:
            index._insert(row, canonical(row.to_dict()), 2)
    assert time.perf_counter() - started < 2.0
    assert index.stats()['public_package'] == 20000 and len(index.packages()) == 1000


def test_judgement_state_outlives_a_rebuild_unless_packages_waited(tmp_path):
    ledger, index = build(tmp_path, 'a', generate(9, sessions=8), random.Random(9))
    assert not index.judgement_started() and index.judged_sequence() == 0
    sequence = index.binding().sequence
    index.record_judgement(sequence, [])
    index.set_rule_state('standing_non_wi', {'*': 'x'})
    index.rebuild(ledger)
    # A ledger position and rule digests name no package: a rebuild keeps them.
    assert index.judged_sequence() == sequence and index.rule_state('standing_non_wi') == {'*': 'x'}
    first, second = [c for c, _, _ in index.packages()][:2]
    index.record_judgement(sequence, [second, first, second])
    assert index.deferred() == [second, first]  # Oldest first, once.
    index.rebuild(ledger)
    # Waiting package ids mean nothing in a new build: everything is judged again.
    assert index.judged_sequence() == 0 and index.deferred() == []
    assert index.rule_state('standing_non_wi') == {'*': 'x'}


def test_a_queued_package_stays_queued_under_the_package_absorbing_it(tmp_path):
    rows = generate(2, sessions=6)
    ledger, index = build(tmp_path, 'a', rows, random.Random(2))
    usages = [r for r in rows if r.kind == 'usage' and r.payload['granularity'] == 'request']
    a, b = usages[0], next(u for u in usages if index.component_of(u.entity_ref) != index.component_of(usages[0].entity_ref))
    small = min(index.component_of(a.entity_ref), index.component_of(b.entity_ref),
                key=lambda c: (index.package_size(c), -c))
    assert index.enqueue([small], 'rule_changed', 1) == 1
    assert index.queued(0) == [small]  # A budget of 0 still takes the first.
    # One claim naming both usages' packages merges them.
    bridge = Receipt.make('usage', ['bridge'], {**a.payload, 'covers': [a.entity_ref, b.entity_ref],
                                                 'native_scope_ref': None, 'execution_ref': None},
                          occurred_at=a.occurred_at, observed_at=a.observed_at, producer='bridge.v1')
    ledger.ingest_receipts([bridge])
    index.catch_up(ledger)
    winner = index.component_of(a.entity_ref)
    assert winner == index.component_of(b.entity_ref) and index.queued(10 ** 6) == [winner]
    index.dequeue([winner])
    assert index.queue_length() == 0
