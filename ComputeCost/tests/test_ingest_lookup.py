"""Ingest by fact-index point reads decides exactly what the whole-ledger tables decide (WI754 P1)."""
import dataclasses
import random
import shutil

import pytest

from pulse_compute_cost import fact_index as fact_index_module
from pulse_compute_cost.fact_index import FactIndex, index_path, ingest_index_provider, stream_identity
from pulse_compute_cost.incremental_modes import incremental_modes
from pulse_compute_cost.ingest_lookup import IndexLookup, TableLookup
from pulse_compute_cost.ledger import LedgerError, SnapshotChanged, UsageLedger, _decide_ingest
from pulse_compute_cost.receipts import Receipt, ReceiptError, reference

from support.synthetic_ledger import OBSERVED, T0, generate, split, write

LATER = '2026-11-21T00:00:00Z'


@pytest.fixture
def provider():
    UsageLedger.index_provider = ingest_index_provider()
    yield
    UsageLedger.index_provider = None


def ticking(root):
    from datetime import timedelta
    clock = [T0]

    def tick():
        clock[0] += timedelta(seconds=1)
        return clock[0]
    return UsageLedger(root, clock=tick)


def remake(row, *, payload=None, evidence=None, supersedes=None, producer=None):
    return Receipt.make(row.kind, None, row.payload if payload is None else payload, occurred_at=row.occurred_at,
                        observed_at=LATER, producer=producer or row.producer,
                        evidence=row.evidence if evidence is None else evidence,
                        entity_ref=row.entity_ref, supersedes=row.supersedes if supersedes is None else supersedes)


def changed_payload(row, rnd):
    if row.kind == 'usage':
        tokens = dict(row.payload['tokens'], output=row.payload['tokens']['output'] + rnd.randint(1, 50))
        return dict(row.payload, tokens=tokens)
    if row.kind == 'attribution' and row.payload.get('policy') and 'context' not in row.payload:
        wi = f'wi_20261102_{rnd.randint(4, 9):03d}'
        return dict(row.payload, wi=wi, state='assigned')
    if row.kind == 'coverage':
        return dict(row.payload, observed=rnd.randint(10, 99))
    return None


def mutations(sent, rnd, *, forge=False):
    """Candidates that exercise every ingest decision against rows sent so far."""
    out = []
    for _ in range(rnd.randint(1, 6)):
        try:
            out += mutation(sent, rnd, forge)
        except ReceiptError:
            pass  # Not a valid fact of this kind; producers never send it.
    return out


def mutation(sent, rnd, forge):
    """One decision case on a random row sent so far."""
    out = []
    row = sent[rnd.randrange(len(sent))]
    choice = rnd.random()
    payload = changed_payload(row, rnd)
    if choice < 0.2:  # Replay of history, possibly a superseded assertion.
        out.append(row)
    elif choice < 0.4:  # Pure evidence supplement: same fact ref, new provenance.
        out.append(remake(row, evidence=(*row.evidence, reference('extra', [row.fact_ref, rnd.random()]))))
    elif choice < 0.6 and payload is not None:  # A revision, perhaps of an old predecessor.
        revision = remake(row, payload=payload, supersedes=row.fact_ref)
        out.append(revision)
        if rnd.random() < 0.5:  # P->Q->R inside one batch.
            again = changed_payload(revision, rnd)
            out.append(remake(revision, payload=again, supersedes=revision.fact_ref))
    elif choice < 0.7 and payload is not None:  # Concurrent heads of one predecessor.
        out.append(remake(row, payload=payload, supersedes=row.fact_ref))
        out.append(remake(row, payload=changed_payload(row, rnd), supersedes=row.fact_ref, producer='fork.v1'))
    elif choice < 0.78 and forge:  # Identity conflict: a forged body under a stored fact ref.
        out.append(dataclasses.replace(row, occurred_at=None if row.occurred_at else '2026-11-03T00:00:00Z'))
    elif choice < 0.86 and payload is not None:  # Missing predecessor.
        out.append(remake(row, payload=payload, supersedes=reference('fact', ['missing', rnd.random()])))
    elif choice < 0.92 and payload is not None:  # Wrong-kind predecessor.
        other = next((r for r in sent if r.kind != row.kind), None)
        if other is not None:
            out.append(remake(row, payload=payload, supersedes=other.fact_ref))
    else:  # Evidence supplement of a revision's predecessor: rebased onto the successor.
        out.append(remake(row, evidence=(*row.evidence, reference('late', rnd.random()))))
        if payload is not None:
            out.append(remake(row, payload=payload, supersedes=row.fact_ref))
    return out


def outcome(call):
    try:
        return call()
    except (LedgerError, ReceiptError) as exc:
        return (type(exc).__name__, str(exc))


def candidate_stream(seed, *, forge=False):
    rnd = random.Random(seed)
    rows = generate(seed, sessions=6 + seed % 5)
    stream, sent = [], []
    for batch in split(rows, rnd):
        stream.append(batch)
        sent += batch
        if rnd.random() < 0.7:
            extra = mutations(sent, rnd, forge=forge)
            if rnd.random() < 0.5:
                stream.append(extra)
            else:
                stream[-1] = stream[-1] + extra
            sent += extra
    return stream


@pytest.mark.parametrize('seed', range(12))
def test_index_and_tables_decide_every_candidate_alike(tmp_path, seed):
    table, indexed = ticking(tmp_path / 'table'), ticking(tmp_path / 'index')
    index = FactIndex.open(tmp_path / 'index.sqlite', stream_identity(indexed))
    indexed._index = index
    for candidates in candidate_stream(seed):
        # Decisions on the same ledger state: rows, duplicates and the rewritten refs.
        indexed._receipt_batches()
        assert index.binding().sequence == len(indexed._receipt_cache_rows)
        by_table = outcome(lambda: _decide_ingest(candidates, TableLookup(indexed._receipt_cache_rows)))
        by_index = outcome(lambda: _decide_ingest(candidates, IndexLookup(index)))
        assert by_index == by_table
        # Whole ingests: results, errors and the appended bytes.
        expected = outcome(lambda: table.ingest_receipts(candidates))
        assert outcome(lambda: indexed.ingest_receipts(candidates)) == expected
        assert indexed.last_ingest_lookup == 'index'
    assert [b for b in table.stream_batches()] == [b for b in indexed.stream_batches()]
    fresh = FactIndex.open(tmp_path / 'fresh.sqlite', stream_identity(indexed))
    fresh.catch_up(indexed)
    assert index.facts_digest() == fresh.facts_digest() == indexed.receipt_snapshot()['sha256']


def test_the_streams_reach_every_ingest_decision(tmp_path):
    seen = {}
    for seed in range(12):
        ledger = ticking(tmp_path / str(seed))
        for candidates in candidate_stream(seed):
            ledger._receipt_batches()
            decided = outcome(lambda: _decide_ingest(candidates, TableLookup(ledger._receipt_cache_rows)))
            if isinstance(decided, tuple) and len(decided) == 2:
                seen[decided[1]] = True
            else:
                pending, duplicates, rewritten = decided
                sent = {r.fact_ref for r in candidates}
                seen['duplicate'] = seen.get('duplicate') or duplicates > 0
                seen['remade'] = seen.get('remade') or any(r.fact_ref not in sent for r in pending)
                seen['rewritten'] = seen.get('rewritten') or any(k != v for k, v in rewritten.items())
            outcome(lambda: ledger.ingest_receipts(candidates))
    assert {k for k, v in seen.items() if v} >= {
        'duplicate', 'remade', 'rewritten', 'receipt revision has no matching predecessor'}


@pytest.mark.parametrize('seed', range(4))
def test_decisions_agree_on_forged_identity_conflicts(tmp_path, seed):
    # Ingest rejects a forged fact ref before deciding; the decision itself must agree too.
    ledger = ticking(tmp_path / 'ledger')
    index = FactIndex.open(tmp_path / 'index.sqlite', stream_identity(ledger))
    ledger._index = index
    conflicts = 0
    for candidates in candidate_stream(seed, forge=True):
        ledger._receipt_batches()
        by_table = outcome(lambda: _decide_ingest(candidates, TableLookup(ledger._receipt_cache_rows)))
        assert outcome(lambda: _decide_ingest(candidates, IndexLookup(index))) == by_table
        conflicts += by_table == ('LedgerError', 'immutable receipt identity conflict')
        outcome(lambda: ledger.ingest_receipts(candidates))
    assert conflicts or seed


def test_off_opens_and_creates_no_index(tmp_path):
    assert UsageLedger.index_provider is None
    ledger = write(tmp_path / 'ledger', [generate(1, sessions=3)])
    assert ledger.last_ingest_lookup == 'table'
    assert not (tmp_path / 'ledger' / 'derived').exists()


def test_provider_builds_a_missing_index_then_ingest_keeps_it_current(tmp_path, provider):
    rows = generate(2, sessions=6)
    UsageLedger.index_provider = None
    write(tmp_path / 'ledger', [rows[:20]])
    UsageLedger.index_provider = ingest_index_provider()
    ledger = ticking(tmp_path / 'ledger')
    assert ledger.ingest_receipts(rows[20:40])['inserted'] == 20
    assert ledger.last_ingest_lookup == 'index'
    index = ledger._index
    assert index.path == index_path(ledger) and index.binding().sequence == 2
    # Another writer appends; this process's next ingest catches the index up from its read.
    UsageLedger.index_provider = None
    other = ticking(tmp_path / 'ledger')
    other.ingest_receipts(rows[40:60])
    UsageLedger.index_provider = ingest_index_provider()
    assert ledger.ingest_receipts(rows[60:])['inserted'] == len(rows) - 60
    assert index.binding().sequence == 4
    assert index.facts_digest() == ledger.receipt_snapshot()['sha256']


def test_crash_between_ledger_and_index_write_is_caught_up(tmp_path, provider, monkeypatch):
    rows = generate(3, sessions=6)
    ledger = ticking(tmp_path / 'ledger')
    ledger.ingest_receipts(rows[:30])
    index = ledger._index

    def killed(meta, lines):
        raise RuntimeError('process died after the ledger append')

    monkeypatch.setattr(index, 'apply_batch', killed)
    assert ledger.ingest_receipts(rows[30:60])['inserted'] == 30  # The ledger write stands.
    monkeypatch.undo()
    assert index.binding().sequence == 1
    index.close()
    reopened = ticking(tmp_path / 'ledger')  # A new process: the index is one batch behind.
    assert reopened.ingest_receipts(rows[60:])['inserted'] == len(rows) - 60
    assert reopened.last_ingest_lookup == 'index'
    assert reopened._index.binding().sequence == 3
    assert reopened._index.facts_digest() == reopened.receipt_snapshot()['sha256']


def test_an_index_of_another_history_is_emptied_and_tables_decide(tmp_path, provider):
    rows = generate(4, sessions=5)
    ledger = ticking(tmp_path / 'ledger')
    ledger.ingest_receipts(rows[:10])
    stale = ledger._index
    stale.close()
    # The same root now holds another history (e.g. restored from elsewhere).
    shutil.rmtree(tmp_path / 'ledger' / 'receipts')
    UsageLedger.index_provider = None
    write(tmp_path / 'ledger', [rows[:5], rows[5:10]])
    UsageLedger.index_provider = ingest_index_provider()
    again = ticking(tmp_path / 'ledger')
    assert again.ingest_receipts(rows[10:])['inserted'] == len(rows) - 10
    assert again.last_ingest_lookup == 'index_stale' and again._index is None
    assert FactIndex.open(index_path(again), stream_identity(again)).binding().sequence == 0
    # The next process rebuilds it before ingest.
    third = ticking(tmp_path / 'ledger')
    assert third.ingest_receipts(rows[:3])['inserted'] == 0
    assert third.last_ingest_lookup == 'index' and third._index.binding().sequence == 3


def test_a_failed_index_read_leaves_the_decision_to_the_tables(tmp_path, provider, monkeypatch):
    rows = generate(5, sessions=5)
    ledger = ticking(tmp_path / 'ledger')
    ledger.ingest_receipts(rows[:20])
    monkeypatch.setattr(ledger._index, 'get', lambda ref: (_ for _ in ()).throw(RuntimeError('disk')))
    assert ledger.ingest_receipts(rows)['inserted'] == len(rows) - 20
    assert ledger.last_ingest_lookup == 'index_unavailable' and ledger._index is None


def test_an_unavailable_index_never_blocks_ingest(tmp_path):
    def broken(ledger):
        raise OSError('no space')
    UsageLedger.index_provider = broken
    try:
        ledger = write(tmp_path / 'ledger', [generate(6, sessions=2)])
    finally:
        UsageLedger.index_provider = None
    assert ledger.last_ingest_lookup == 'index_unavailable'


def test_snapshot_change_is_still_rejected_before_any_lookup(tmp_path, provider):
    rows = generate(7, sessions=3)
    ledger = ticking(tmp_path / 'ledger')
    ledger.ingest_receipts(rows[:5])
    with pytest.raises(SnapshotChanged):
        ledger.ingest_receipts(rows[5:], expected_watermark=0)
    assert ledger.ingest_receipts(rows[5:], expected_watermark=1)['watermark'] == 2


def test_the_stored_assertion_digest_is_checked_against_the_whole_assertion(tmp_path, monkeypatch):
    rows = generate(8, sessions=3)
    ledger = write(tmp_path / 'ledger', [rows])
    index = FactIndex.open(tmp_path / 'index.sqlite', stream_identity(ledger))
    index.catch_up(ledger)
    usage = next(r for r in rows if r.kind == 'usage')
    lookup = IndexLookup(index)
    assert [r.fact_ref for r in lookup.active(usage)] == [r.fact_ref for r in
                                                         TableLookup([rows]).active(usage)]
    monkeypatch.setattr(lookup, '_key', lambda row: fact_index_module.assertion_key(usage))
    other = next(r for r in rows if r.kind == 'execution')
    assert lookup.active(other) == []  # A digest collision is not the same assertion.


@pytest.mark.parametrize('config, modes', [
    ({}, {'ingest': 'off', 'views': 'off', 'collection': 'off', 'close': 'off'}),
    ({'incremental': {'ingest': 'on'}}, {'ingest': 'on', 'views': 'off', 'collection': 'off', 'close': 'off'}),
    ({'incremental': {'views': 'shadow', 'collection': 'on'}}, {'ingest': 'off', 'views': 'shadow',
                                                                 'collection': 'on', 'close': 'off'}),
    ({'incremental': {'views': 'on', 'close': 'on'}}, {'ingest': 'off', 'views': 'on', 'collection': 'off',
                                                        'close': 'on'}),
])
def test_incremental_switches(config, modes):
    assert incremental_modes(config) == modes


@pytest.mark.parametrize('section', [[], {'ingest': 'shadow'}, {'ingest': True}, {'other': 'on'}, 'on'])
def test_malformed_incremental_switch_is_an_error(section):
    with pytest.raises(ValueError):
        incremental_modes({'incremental': section})


# -- one fact ref in several batches ---------------------------------------
# History holds the same fact ref in different batches with other bytes: the
# ref omits observed_at and evidence. The ledger keeps every occurrence.

def append_raw(root, rows):
    """Append one batch as an older writer did, without ingest's duplicate check."""
    import os
    from pulse_compute_cost.receipts import canonical, digest
    directory = root / 'receipts'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    present = sorted(directory.glob('*.json'))
    previous = __import__('json').loads(present[-1].read_bytes()) if present else None
    batch = {'schema': 'computecost_ledger_transaction/v1', 'sequence': len(present) + 1,
             'recorded_at': f'2026-11-02T02:{len(present):02d}:00Z',
             'previous': previous['sha256'] if previous else None, 'facts': [r.to_dict() for r in rows]}
    batch['sha256'] = digest(batch)
    path = directory / f"{batch['sequence']:020d}.json"
    path.write_bytes(canonical(batch) + b'\n')
    os.chmod(path, 0o600)


def repeated_history(seed):
    """Batches where some facts recur later with new observed_at and evidence."""
    rnd = random.Random(seed)
    rows = generate(seed, sessions=6)
    cut = split(rows, rnd)
    while len(cut) < 3:
        cut = split(rows, rnd)
    batches, sent = [], []
    for batch in cut:
        if sent:
            for row in rnd.sample(sent, min(len(sent), rnd.randint(1, 4))):
                batch = batch + [Receipt.make(
                    row.kind, None, row.payload, occurred_at=row.occurred_at,
                    observed_at=f'2026-11-2{rnd.randint(1, 9)}T00:00:00Z', producer=row.producer,
                    evidence=(*row.evidence, reference('repeat', rnd.random())) if rnd.random() < 0.7 else row.evidence,
                    entity_ref=row.entity_ref, supersedes=row.supersedes)]
        batches.append(batch)
        sent += batch
    return batches, sent


class RepeatIndex:
    """The index read contract with repeats: every occurrence in ledger order, `get` the latest."""

    def __init__(self, rows):
        self.rows = list(rows)

    def get(self, ref):
        return next((r for r in reversed(self.rows) if r.fact_ref == ref), None)

    def children(self, ref):
        return [r for r in self.rows if r.supersedes == ref]

    def active_by_assertion(self, key):
        revised = {r.supersedes for r in self.rows if r.supersedes}
        return [r for r in self.rows if fact_index_module.assertion_key(r) == key and r.fact_ref not in revised]


@pytest.mark.parametrize('seed', range(8))
def test_repeated_fact_refs_decide_alike(tmp_path, seed):
    history, sent = repeated_history(seed)
    for batch in history:
        append_raw(tmp_path / 'ledger', batch)
    ledger = ticking(tmp_path / 'ledger')
    rows = ledger.receipt_snapshot()['facts']
    assert len(rows) > len({r.fact_ref for r in rows})  # The history does repeat refs.
    rnd = random.Random(seed)
    for _ in range(6):
        candidates = mutations(sent, rnd) + rnd.sample(sent, 3)
        ledger._receipt_batches()
        stored = [r for batch in ledger._receipt_cache_rows for r in batch]
        by_table = outcome(lambda: _decide_ingest(candidates, TableLookup(ledger._receipt_cache_rows)))
        assert outcome(lambda: _decide_ingest(candidates, IndexLookup(RepeatIndex(stored)))) == by_table
        outcome(lambda: ledger.ingest_receipts(candidates))
        sent += candidates



def again(row, tag):
    """The same fact ref observed again later with other provenance."""
    return remake(row, evidence=(*row.evidence, reference('again', tag))) if tag else row


def test_a_repeated_evidence_successor_is_still_one_successor(tmp_path):
    usage = next(r for r in generate(1, sessions=2) if r.kind == 'usage')
    successor = remake(usage, evidence=(reference('refined', 1),), supersedes=usage.fact_ref)
    for batch in ([usage], [successor], [again(successor, 'x')]):
        append_raw(tmp_path / 'ledger', batch)
    ledger = UsageLedger(tmp_path / 'ledger')
    revision = remake(usage, payload=changed_payload(usage, random.Random(1)), supersedes=usage.fact_ref)
    ledger._receipt_batches()
    stored = [r for batch in ledger._receipt_cache_rows for r in batch]
    by_table = _decide_ingest([revision], TableLookup(ledger._receipt_cache_rows))
    assert by_table[0][0].supersedes == successor.fact_ref  # Rebased onto the one evidence successor.
    assert _decide_ingest([revision], IndexLookup(RepeatIndex(stored))) == by_table


def test_a_repeated_active_assertion_keeps_its_first_place(tmp_path):
    usage = next(r for r in generate(1, sessions=2) if r.kind == 'usage')
    other = remake(usage, payload=changed_payload(usage, random.Random(2)), supersedes=None)
    head = remake(usage, supersedes=other.fact_ref, evidence=(reference('head', 1),))
    for batch in ([other, usage], [head], [again(usage, 'y')]):
        append_raw(tmp_path / 'ledger', batch)
    ledger = UsageLedger(tmp_path / 'ledger')
    candidate = again(usage, 'z')
    ledger._receipt_batches()
    stored = [r for batch in ledger._receipt_cache_rows for r in batch]
    by_table = _decide_ingest([candidate], TableLookup(ledger._receipt_cache_rows))
    assert by_table[0][0].supersedes == head.fact_ref  # The latest-placed active head, not the repeat.
    assert _decide_ingest([candidate], IndexLookup(RepeatIndex(stored))) == by_table


def test_repeated_fact_refs_ingest_alike_on_the_fact_index(tmp_path):
    history, sent = repeated_history(3)
    for name in ('table', 'index'):
        for batch in history:
            append_raw(tmp_path / name, batch)
    table, indexed = ticking(tmp_path / 'table'), ticking(tmp_path / 'index')
    index = FactIndex.open(tmp_path / 'index.sqlite', stream_identity(indexed))
    index.catch_up(indexed)
    indexed._index = index
    rnd = random.Random(3)
    for _ in range(6):
        candidates = mutations(sent, rnd) + rnd.sample(sent, 3)
        expected = outcome(lambda: table.ingest_receipts(candidates))
        assert outcome(lambda: indexed.ingest_receipts(candidates)) == expected
        assert indexed.last_ingest_lookup == 'index'
        sent += candidates
    assert list(table.stream_batches()) == list(indexed.stream_batches())


# -- another process resets the index while this one ingests ---------------

def test_a_reset_committed_during_the_decision_hides_no_stored_fact(tmp_path, provider, monkeypatch):
    rows = generate(11, sessions=4)
    ledger = ticking(tmp_path / 'ledger')
    ledger.ingest_receipts(rows)
    path = ledger._index.path
    original = IndexLookup.__init__

    def reset_by_another_process(self, index):
        original(self, index)
        # A second connection judges the index foreign and empties it, after
        # this ingest checked the prefix and before it reads a single fact.
        other = FactIndex.open(path, 'another stream')
        assert other.binding().sequence == 0
        other.close()

    monkeypatch.setattr(IndexLookup, '__init__', reset_by_another_process)
    replay = ledger.ingest_receipts(rows[:5])
    assert ledger.last_ingest_lookup == 'index'
    assert replay == {'inserted': 0, 'duplicates': 5, 'watermark': 1}
    assert len(list(ledger.stream_batches())) == 1


def test_open_judges_and_resets_in_the_transaction_that_blocks_other_openers(tmp_path, monkeypatch):
    import threading
    import time
    rows = generate(12, sessions=4)
    ledger = write(tmp_path / 'ledger', split(rows, random.Random(12)))
    path = index_path(ledger)
    judged, original = threading.Event(), FactIndex._meta
    opener = []

    def slow_judgment(self):
        meta = original(self)
        if threading.current_thread() in opener and not judged.is_set():
            judged.set()
            time.sleep(0.5)  # The first opener pauses right after reading the empty file's meta.
        return meta

    monkeypatch.setattr(FactIndex, '_meta', slow_judgment)
    first = threading.Thread(target=lambda: FactIndex.open(path, stream_identity(ledger)).close())
    opener.append(first)
    first.start()
    assert judged.wait(5)
    second = FactIndex.open(path, stream_identity(ledger))  # Waits for the first opener's transaction.
    second.catch_up(ledger)
    first.join()
    assert FactIndex.open(path, stream_identity(ledger)).binding().sequence == len(list(ledger.stream_batches()))


def test_an_authority_batch_repeating_a_fact_ref_is_indexed_and_ingests_alike(tmp_path):
    """Review repro: the observation store accepts one fact ref twice in one verified batch."""
    from test_observation_checkpoint import Rig, append_rows, node, private
    rows = generate(13, sessions=5)
    rnd = random.Random(13)
    usage = [r for r in rows[10:] if r.kind == 'usage'][:2]
    repeated = rows[10:30] + [again(usage[0], 'a'), again(usage[1], 'b'), again(usage[0], 'c')]
    ledgers = {}
    for name in ('table', 'index'):
        (tmp_path / name).mkdir()
        rig = Rig(tmp_path / name)
        append_rows(rig, [rows[:10], repeated])
        ledgers[name] = node(rig, private(tmp_path / f'{name}-root'))
    table, indexed = ledgers['table'], ledgers['index']
    index = FactIndex.open(tmp_path / 'index.sqlite', stream_identity(indexed))
    assert index.catch_up(indexed) == 2
    assert index.facts_digest() == indexed.receipt_snapshot()['sha256']
    assert index.get(usage[0].fact_ref) == again(usage[0], 'c')  # Latest by position inside the batch.
    indexed._index = index
    sent = rows[:30] + repeated[-3:]
    for step in range(4):
        candidates = (rows[30 + 10 * step:40 + 10 * step] + mutations(sent, rnd)
                      + [again(usage[step % 2], f'later{step}')] + rnd.sample(sent, 3))
        expected = outcome(lambda: table.ingest_receipts(candidates))
        assert outcome(lambda: indexed.ingest_receipts(candidates)) == expected
        assert indexed.last_ingest_lookup == 'index'
        sent += candidates
    assert index.binding().sequence == len(indexed._receipt_cache_batches) > 2
    assert list(table.stream_batches()) == list(indexed.stream_batches())


# -- a derived index that lost rows never changes what ingest appends -------

def corrupted(tmp_path, damage):
    rows = generate(14, sessions=4)
    UsageLedger.index_provider = ingest_index_provider()
    ledger = ticking(tmp_path / 'ledger')
    ledger.ingest_receipts(rows)
    victim = rows[7]
    damage(ledger._index._db, fact_index_module.session_packages.node(victim.fact_ref))
    table = ticking(tmp_path / 'table')
    UsageLedger.index_provider = None
    table.ingest_receipts(rows)
    UsageLedger.index_provider = ingest_index_provider()
    return ledger, table, victim


@pytest.mark.parametrize('damage', [
    # The reviewed injection: a fact row gone, binding and batch rows kept.
    lambda db, ref: db.execute('DELETE FROM fact WHERE fact_ref = ?', (ref,)),
    # The batch row adjusted to match: declared and stored counts agree, the ledger's does not.
    lambda db, ref: (db.execute('DELETE FROM fact WHERE fact_ref = ?', (ref,)),
                     db.execute('UPDATE batch SET fact_count = fact_count - 1')),
    # Counts all intact: another row stands in for the lost one.
    lambda db, ref: (db.execute('INSERT INTO fact SELECT NULL, fact_ref, entity_ref, kind, producer, sequence, '
                                'supersedes, assertion_key, component, body FROM fact WHERE fact_ref != ? '
                                'ORDER BY id LIMIT 1', (ref,)),
                     db.execute('DELETE FROM fact WHERE fact_ref = ?', (ref,))),
], ids=['row_deleted', 'counts_adjusted', 'row_replaced'])
def test_an_index_missing_a_fact_falls_back_to_the_tables_and_rebuilds(tmp_path, provider, damage):
    ledger, table, victim = corrupted(tmp_path, damage)
    assert ledger._index._db.execute('PRAGMA quick_check').fetchone() == ('ok',)
    expected = table.ingest_receipts([victim])
    assert expected == {'inserted': 0, 'duplicates': 1, 'watermark': 1}
    assert ledger.ingest_receipts([victim]) == expected
    assert ledger.last_ingest_lookup == 'index_stale' and ledger._index is None
    assert len(list(ledger.stream_batches())) == 1
    # The next process rebuilds the emptied index and decides by it again.
    again_ = ticking(tmp_path / 'ledger')
    assert again_.ingest_receipts([victim]) == expected
    assert again_.last_ingest_lookup == 'index'
    assert again_._index.facts_digest() == again_.receipt_snapshot()['sha256']


# -- the index write follows the publication section a deployment waits for --

def recorded_order(ledger, monkeypatch, write_target, write_name):
    from contextlib import contextmanager
    events = []

    @contextmanager
    def section():
        events.append('enter_section')
        yield
        events.append('exit_section')

    original_write = getattr(write_target, write_name)

    def write(*args, **kwargs):
        result = original_write(*args, **kwargs)
        events.append('ledger_write')
        return result

    monkeypatch.setattr(write_target, write_name, write)
    index = ledger._index
    original_apply = index.apply_batch

    def apply(meta, lines):
        events.append('index_write')
        return original_apply(meta, lines)

    monkeypatch.setattr(index, 'apply_batch', apply)
    monkeypatch.setattr(UsageLedger, 'append_section', section)
    return events


def test_local_index_write_follows_the_publication_section(tmp_path, provider, monkeypatch):
    from pulse_compute_cost import ledger as ledger_module
    rows = generate(15, sessions=3)
    ledger = ticking(tmp_path / 'ledger')
    ledger.ingest_receipts(rows[:5])
    events = recorded_order(ledger, monkeypatch, ledger_module.os, 'replace')
    assert ledger.ingest_receipts(rows[5:])['inserted'] == len(rows) - 5
    assert events == ['enter_section', 'ledger_write', 'exit_section', 'index_write']
    assert ledger._index.binding().sequence == 2


def test_authority_index_write_follows_the_publication_section(tmp_path, provider, monkeypatch):
    from test_observation_checkpoint import Rig, append_rows, node, private
    rows = generate(16, sessions=3)
    rig = Rig(tmp_path)
    append_rows(rig, [rows[:5]])
    ledger = node(rig, private(tmp_path / 'root'))
    ledger.ingest_receipts(rows[:5])
    assert ledger.last_ingest_lookup == 'index'
    events = recorded_order(ledger, monkeypatch, ledger.authority, 'append')
    assert ledger.ingest_receipts(rows[5:])['inserted'] == len(rows) - 5
    assert events == ['enter_section', 'ledger_write', 'exit_section', 'index_write']
    assert ledger._index.binding().sequence == 2
