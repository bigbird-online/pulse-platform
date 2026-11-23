"""Incremental collection judges as the full collection does (WI754 step 4, invariant 2)."""
import json
import random

import pytest

from pulse_compute_cost.ledger import UsageLedger

from pulse_compute_cost import incremental_collect
from pulse_compute_cost.collect_replay import Replay, run
from pulse_compute_cost.incremental_collect import (collect_incremental, open_index, record_rules, rule_changes,
                                                    rule_entries)
from pulse_compute_cost.native_observations import SCHEMA, encode, store
from pulse_compute_cost.receipts import reference
from pulse_compute_cost.tenant_context import FEATURE as TENANT_FEATURE, SCHEMA as TENANT_SCHEMA

from support.synthetic_ledger import claim, generate, pi_turn, split, write

PEER = 'ownermac'
SOURCE = 'synthetic-receipts'
FEATURES = ['non_wi_activity.v1', 'runtime_subcall.v1']
NOW = '2026-11-20T00:00:00Z'


def registry(root, tenants=0):
    """With `tenants`, the first synthetic CC sessions are declared members of one tenant."""
    result = {'schema': 'computecost_source_registry/v1', 'ledger_root': str(root),
              'sources': [{'id': SOURCE, 'kind': 'receipts', 'path': str(root / 'unused.jsonl')}],
              'required_receipt_features': list(FEATURES)}
    if tenants:
        path = root.parent / 'tenant-registry.json'
        path.write_text(json.dumps({'schema': TENANT_SCHEMA, 'bindings': [
            {'tenant_id': 'team-a', 'tenant_name': 'Team A', 'agent': 'pulse', 'node': 'ownermac', 'runtime': 'cc',
             'native_scope_ref': reference('native_session', ['cc', 'synthetic', i]),
             'effective_from': '2026-11-01T00:00:00Z', 'effective_to': None} for i in range(tenants)]}))
        path.chmod(0o600)
        result.update(tenant_registry_path=str(path), required_receipt_features=[*FEATURES, TENANT_FEATURE])
    return result


def bundle(sequence, rows, kind='receipts', source=SOURCE):
    return encode({'schema': SCHEMA, 'peer': PEER, 'sequence': sequence, 'created_at': NOW,
                   'sources': [{'config': {'id': source, 'kind': kind}, 'rows': [r.to_dict() for r in rows],
                                'inventories': [], 'appended': [],
                                'coverage': {'status': 'complete', 'reason': None, 'expected': None, 'observed': None,
                                             'watermark': None, 'raw_count': len(rows), 'detail': {},
                                             'diagnostics': {}}}],
                   'inputs': []})


def empty(sequence):
    return encode({'schema': SCHEMA, 'peer': PEER, 'sequence': sequence, 'created_at': NOW,
                   'sources': [], 'inputs': []})


def corpus(seed, sessions, cut=0.4, tail=1):
    """A ledger prefix and the bundles that carry the rest, in random cuts (some rows sent twice)."""
    rnd = random.Random(seed)
    rows = generate(seed, sessions=sessions) + pi_turn('replay-a', 7, alias=True) + pi_turn('replay-b', 9)
    head = int(len(rows) * cut)
    bundles, sequence = [], 0
    for chunk in split(rows[head:], rnd):
        sequence += 1
        repeat = rnd.sample(rows[:head], min(3, head)) if rnd.random() < 0.3 else []
        bundles.append(bundle(sequence, repeat + chunk))
    # Last rounds with nothing new: the round after an ingest still judges its packages.
    bundles += [empty(sequence + n) for n in range(1, tail + 1)]
    return rows[:head], bundles


def prepared(tmp_path, seed, sessions=16, tenants=0, tail=1):
    prefix, bundles = corpus(seed, sessions, tail=tail)
    source = write(tmp_path / 'source', split(prefix, random.Random(seed)))
    watermark = len(list((tmp_path / 'source' / 'receipts').glob('*.json')))
    return source, watermark, registry(tmp_path / 'source', tenants), bundles


@pytest.mark.parametrize('seed,tenants', [(5, 0), (11, 16)])
def test_each_round_gains_the_facts_a_full_collection_gains(tmp_path, seed, tenants):
    source, watermark, reg, bundles = prepared(tmp_path, seed, tenants=tenants)
    out = tmp_path / 'replay'
    state = run(reg, out, 'prepare', watermark=watermark, ledger=source, now=lambda: NOW)
    assert state['settled'][-1] == [0, 0]
    corpus_dir = tmp_path / 'corpus'
    corpus_dir.mkdir()
    for n, data in enumerate(bundles, 1):
        (corpus_dir / f'{n:020d}.json.gz').write_bytes(data)
    assert run(reg, out, 'replay', corpus=corpus_dir, rounds=2, now=lambda: NOW)['replayed'] == 2
    run(reg, out, 'replay', corpus=corpus_dir, now=lambda: NOW)  # Resumes after the first two.
    rounds = [json.loads(line) for line in (out / 'rounds.jsonl').read_text().splitlines()]
    assert len(rounds) == len(bundles)
    assert [r['symmetric_difference'] for r in rounds] == [0] * len(rounds), rounds
    assert sum(r['full_new'] for r in rounds) > 0  # Not vacuous.
    report = run(reg, out, 'report')
    assert report['ledger_difference'] == 0 and not report['rounds_with_difference']
    if tenants:  # The tenant stage proved usage versions a round after their ingest.
        full = UsageLedger(out / 'full' / 'ledger').receipt_snapshot()['facts']
        assert any(r.kind == 'attribution' and 'tenant' in (r.payload.get('context') or {}) for r in full)
    assert all(oct(p.stat().st_mode)[-3:] == '600' for p in out.iterdir() if p.is_file())


def test_a_round_logged_before_an_interruption_is_not_replayed_or_counted_twice(tmp_path, monkeypatch):
    # Review r3 repro: interrupted between the log line and replay-state, the resumed
    # replay judged the bundle again and the report counted one bundle as two rounds.
    from pulse_compute_cost import collect_replay
    source, watermark, reg, bundles = prepared(tmp_path, 5)
    out, corpus_dir = tmp_path / 'replay', tmp_path / 'corpus'
    run(reg, out, 'prepare', watermark=watermark, ledger=source, now=lambda: NOW)
    corpus_dir.mkdir()
    for n, data in enumerate(bundles[:2], 1):
        (corpus_dir / f'{n:020d}.json.gz').write_bytes(data)
    written = collect_replay._write_json

    def interrupted(path, value):
        if path.name == 'replay-state.json':
            raise KeyboardInterrupt
        written(path, value)
    monkeypatch.setattr(collect_replay, '_write_json', interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(reg, out, 'replay', corpus=corpus_dir, now=lambda: NOW)
    monkeypatch.setattr(collect_replay, '_write_json', written)
    assert run(reg, out, 'replay', corpus=corpus_dir, now=lambda: NOW) == {'replayed': 1, 'total': 2}
    logged = [json.loads(line)['bundle'] for line in (out / 'rounds.jsonl').read_text().splitlines()]
    assert logged == [f'{1:020d}.json.gz', f'{2:020d}.json.gz']
    first, second = (out / 'rounds.jsonl').read_text().splitlines(keepends=True)
    (out / 'rounds.jsonl').write_text(first + first + second)
    assert run(reg, out, 'report')['rounds'] == 2  # A line written twice is still one round.


def replayed(tmp_path, seed, *, tail=1, limit=None, sessions=16):
    """Prepare and replay a synthetic corpus; the round records and the report."""
    source, watermark, reg, bundles = prepared(tmp_path, seed, sessions=sessions, tail=tail)
    out, corpus_dir = tmp_path / 'replay', tmp_path / 'corpus'
    extra = {} if limit is None else {'limit': limit}
    run(reg, out, 'prepare', watermark=watermark, ledger=source, now=lambda: NOW)
    corpus_dir.mkdir()
    for n, data in enumerate(bundles, 1):
        (corpus_dir / f'{n:020d}.json.gz').write_bytes(data)
    run(reg, out, 'replay', corpus=corpus_dir, now=lambda: NOW, **extra)
    rounds = [json.loads(line) for line in (out / 'rounds.jsonl').read_text().splitlines()]
    return rounds, run(reg, out, 'report')


def test_a_working_set_without_the_spooled_packages_grows_to_them(tmp_path, monkeypatch):
    monkeypatch.setattr(incremental_collect, 'spooled_components', lambda *args: (set(), {}))
    rounds, report = replayed(tmp_path, 5)
    assert any(r['incremental']['growths'] for r in rounds)
    assert not report['rounds_with_difference'] and report['ledger_difference'] == 0
    assert not report['unsettled_rounds']


def test_unsettled_packages_wait_in_the_queue_and_their_bundles_in_the_spool(tmp_path, monkeypatch):
    monkeypatch.setattr(incremental_collect, 'spooled_components', lambda *args: (set(), {}))
    monkeypatch.setattr(incremental_collect, 'GROWTHS', 0)
    rounds, report = replayed(tmp_path, 5, tail=3)
    unsettled = [r for r in rounds if r['incremental'].get('working_set_unsettled')]
    assert unsettled and report['rounds_with_difference']
    assert any(r['spooled']['incremental'] for r in unsettled)  # Kept for the round that judges them.
    assert any(r['incremental']['queue'] for r in unsettled)
    assert report['ledger_difference'] == 0  # The queue caught up.


def test_packages_past_the_limit_wait_for_later_rounds(tmp_path):
    rounds, report = replayed(tmp_path, 11, tail=6, limit=1)
    assert any(r['incremental']['deferred_components'] for r in rounds)
    assert report['ledger_difference'] == 0
    assert rounds[-1]['spooled'] == {'full': 0, 'incremental': 0}


def test_another_nodes_batch_makes_its_packages_dirty(tmp_path):
    source, watermark, reg, bundles = prepared(tmp_path, 5)
    out = tmp_path / 'replay'
    run(reg, out, 'prepare', watermark=watermark, ledger=source, now=lambda: NOW)
    sides = Replay(reg, out, PEER)
    try:
        assert sides.round(bundles[0], NOW)['symmetric_difference'] == 0
        usage = next(r for r in sides.ledgers['full'].receipt_snapshot()['facts']
                     if r.kind == 'usage' and r.payload['granularity'] == 'request')
        other = [claim(usage, 'wi_20261102_002', 'other_node.v1', tag='other')]
        for ledger in sides.ledgers.values():  # Appended by another node, not by these collections.
            assert ledger.ingest_receipts(other)['inserted'] == 1
        result = sides.round(empty(len(bundles) + 1), NOW)
        assert result['incremental']['changed'] >= 1
        assert result['symmetric_difference'] == 0
    finally:
        sides.close()


def test_the_incremental_round_never_reads_the_whole_ledger(tmp_path, monkeypatch):
    source, watermark, reg, bundles = prepared(tmp_path, 5)
    out = tmp_path / 'replay'
    run(reg, out, 'prepare', watermark=watermark, ledger=source, now=lambda: NOW)
    sides = Replay(reg, out, PEER)
    try:
        store(out / 'incremental' / 'spool', PEER, bundles[0])
        ledger = sides.ledgers['incremental']

        def refused(*args, **kwargs):
            raise AssertionError('receipt_snapshot read')
        monkeypatch.setattr(UsageLedger, 'receipt_snapshot', refused)
        judged = collect_incremental(sides.registries['incremental'], ledger, sides.index, publisher=True,
                                     limit=10**6, judge_only=True, now=NOW)
        assert judged['new_refs'] and judged['inserted'] == 0
        assert list((out / 'incremental' / 'spool').glob('*.json.gz'))  # Judge only consumes nothing.
        before = sides.index.binding().sequence
        result = collect_incremental(sides.registries['incremental'], ledger, sides.index, publisher=True,
                                     limit=10**6, now=NOW)
        assert result['inserted'] == len(judged['new_refs'])
        added = {r.fact_ref for r in sides.index._rows("SELECT body FROM fact WHERE sequence = ?", (before + 1,))}
        assert added == set(judged['new_refs'])
    finally:
        sides.close()


def test_snapshot_changed_catches_up_and_judges_again(tmp_path, monkeypatch):
    source, watermark, reg, bundles = prepared(tmp_path, 5)
    out = tmp_path / 'replay'
    run(reg, out, 'prepare', watermark=watermark, ledger=source, now=lambda: NOW)
    sides = Replay(reg, out, PEER)
    try:
        ledger = sides.ledgers['incremental']
        store(out / 'incremental' / 'spool', PEER, bundles[0])
        original, calls = UsageLedger.ingest_receipts, []

        def racing(self, rows, **kwargs):
            if not calls:  # Another node appends first.
                calls.append(1)
                original(self, [claim(next(r for r in rows if r.kind == 'usage'), None, 'racer.v1')])
            return original(self, rows, **kwargs)
        monkeypatch.setattr(UsageLedger, 'ingest_receipts', racing)
        timings = {}
        result = collect_incremental(sides.registries['incremental'], ledger, sides.index, publisher=True,
                                     limit=10**6, timings=timings, now=NOW)
        assert timings['attempts'] == 2 and result['inserted'] > 0
    finally:
        sides.close()


def test_a_changed_rule_entry_queues_only_its_package_on_the_publisher(tmp_path, monkeypatch):
    source, watermark, reg, bundles = prepared(tmp_path, 5)
    index = open_index(source, cache_mib=16, path=tmp_path / 'index.sqlite')
    try:
        facts = source.receipt_snapshot()['facts']
        target = next(r for r in facts if r.kind == 'usage')
        base = rule_entries(reg, {}, {}, [])
        record_rules(index, base)
        assert rule_changes(index, base) == (set(), [])
        changed = {**base, 'reviewed_activity': {**base['reviewed_activity'],
                                                 target.fact_ref: ('new', [target.fact_ref])}}
        assert rule_changes(index, changed) == ({index.component_of(target.entity_ref)}, [])
        whole = {**base, 'standing_non_wi': {'*': ('other', [])}}
        assert rule_changes(index, whole) == (set(), ['standing_non_wi'])
        # Retirements and recoveries decide coverage: the public package, judged every round.
        moved = {**base, 'retirements': {'old-source': ('d', [0])}}
        assert rule_changes(index, moved) == (set(), [])
    finally:
        index.close()


@pytest.mark.parametrize('publisher', [True, False])
def test_only_the_publisher_keeps_the_rejudge_queue(tmp_path, monkeypatch, publisher):
    source, watermark, reg, bundles = prepared(tmp_path, 5)
    out = tmp_path / 'replay'
    run(reg, out, 'prepare', watermark=watermark, ledger=source, now=lambda: NOW)
    sides = Replay(reg, out, PEER)
    try:
        ledger = sides.ledgers['incremental']
        collect_incremental(sides.registries['incremental'], ledger, sides.index, publisher=publisher,
                            limit=10**6, now=NOW)  # Starts judgement and records the rules.
        entries = incremental_collect.rule_entries
        monkeypatch.setattr(incremental_collect, 'rule_entries', lambda *args: {
            **entries(*args), 'standing_non_wi': {'*': ('changed', [])}})
        monkeypatch.setattr(incremental_collect, 'REJUDGE_BUDGET', 0)
        timings = {}
        collect_incremental(sides.registries['incremental'], ledger, sides.index, publisher=publisher,
                            limit=10**6, timings=timings, now=NOW)
        assert timings['incremental']['rules_changed'] == ['standing_non_wi']
        packages = len(sides.index.usage_packages())
        # A budget of 0 still takes one queued package per round.
        assert timings['incremental']['queue'] == (packages - 1 if publisher else 0)
        assert sides.index.rule_state('standing_non_wi') == {'*': 'changed'}
    finally:
        sides.close()


def test_switching_a_peer_to_incremental_needs_its_index_and_an_empty_outbox(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('stats_deploy', Path(__file__).resolve().parents[2] / 'Stats/deploy.py')
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    source, watermark, reg, bundles = prepared(tmp_path, 5)
    home = tmp_path / 'home'
    registry_path = home / '.local/state/pulse/compute-cost/sources.json'
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(json.dumps(reg))
    registry_path.chmod(0o600)
    with pytest.raises(ValueError, match='fact index'):
        deploy.require_incremental_collection_ready(home)
    open_index(source, cache_mib=16).close()
    deploy.require_incremental_collection_ready(home)
    outbox = home / '.local/state/pulse/stats/observe/outbox'
    outbox.mkdir(parents=True)
    (outbox / f'{1:020d}.json.gz').write_bytes(bundles[0])
    with pytest.raises(ValueError, match='unacknowledged'):
        deploy.require_incremental_collection_ready(home)


def test_the_cli_builds_the_index_and_runs_the_replay(tmp_path):
    import contextlib
    import io
    from pulse_compute_cost.cli import main as cli_main
    source, watermark, reg, bundles = prepared(tmp_path, 5)
    path = tmp_path / 'sources.json'
    path.write_text(json.dumps(reg))
    path.chmod(0o600)

    def cli(*argv):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            assert cli_main(list(argv)) == 0
        return json.loads(stdout.getvalue())
    built = cli('fact-index', '--registry', str(path))
    assert built['sequence'] == watermark and built['judged_sequence'] == 0
    corpus_dir = tmp_path / 'corpus'
    corpus_dir.mkdir()
    for n, data in enumerate(bundles[:3], 1):
        (corpus_dir / f'{n:020d}.json.gz').write_bytes(data)
    out = tmp_path / 'replay'
    cli('collect-replay', '--registry', str(path), '--out', str(out), '--phase', 'prepare', '--watermark', str(watermark))
    assert cli('collect-replay', '--registry', str(path), '--out', str(out), '--phase', 'replay',
               '--corpus', str(corpus_dir))['replayed'] == 3
    report = cli('collect-replay', '--registry', str(path), '--out', str(out), '--phase', 'report')
    assert report['rounds'] == 3 and not report['rounds_with_difference'] and report['gate'] is False


def publisher(tmp_path, seed=5):
    """A settled ledger whose registry judges one peer spool, and the corpus bundles."""
    from pulse_compute_cost.receipt_sources import collect_registered_sources
    source, watermark, reg, bundles = prepared(tmp_path, seed)
    spool = tmp_path / 'peer-spool'
    reg = {**reg, 'sources': [{'id': 'native-push-ownermac', 'kind': 'native_push', 'peer': PEER,
                               'path': str(spool), 'source_ids': [SOURCE]}]}
    while collect_registered_sources(reg, source)['inserted']:
        pass
    return source, reg, spool, bundles


def test_a_shadow_compares_the_full_collection_with_the_incremental_judgement(tmp_path):
    from pulse_compute_cost.incremental_cycle import shadow_round
    from pulse_compute_cost.receipt_sources import collect_registered_sources
    ledger, reg, spool, bundles = publisher(tmp_path)
    for n, data in enumerate(bundles[:3]):
        store(spool, PEER, data)
        timings = {}
        result = shadow_round(reg, ledger, lambda now: collect_registered_sources(reg, ledger, now=now),
                              timings=timings)
        shadow = timings['incremental_collect_shadow']
        assert shadow['status'] == 'compared', shadow
        assert shadow['symmetric_difference'] == 0 and shadow['full'] == result['inserted']
        assert not list(spool.glob('*.json.gz'))  # The full collection consumed the bundle.
    assert (ledger.root / 'derived' / 'fact-index.sqlite').exists()


def test_a_failing_shadow_never_fails_the_full_collection(tmp_path, monkeypatch):
    from pulse_compute_cost import incremental_cycle
    from pulse_compute_cost.receipt_sources import collect_registered_sources
    ledger, reg, spool, bundles = publisher(tmp_path)
    store(spool, PEER, bundles[0])
    monkeypatch.setattr(incremental_cycle, 'collect_incremental', lambda *a, **k: 1 / 0)
    timings = {}
    result = incremental_cycle.shadow_round(reg, ledger, lambda now: collect_registered_sources(reg, ledger, now=now),
                                            timings=timings)
    assert result['inserted'] > 0
    assert timings['incremental_collect_shadow'] == {'status': 'failed', 'error_type': 'ZeroDivisionError'}


def test_a_shadow_leaves_the_local_spool_backlog_alone(tmp_path):
    # Review r3 repro: the shadow consumed the local spool of `on` before the full
    # collection; when that failed, pending bundles were gone with the ledger unchanged.
    from pulse_compute_cost.incremental_cycle import local_spool, shadow_round
    ledger, reg, spool, bundles = publisher(tmp_path)
    backlog = local_spool(ledger)
    store(backlog, PEER, bundles[0])
    waiting = sorted(backlog.glob('*.json.gz'))
    assert waiting

    def failing(now):
        raise RuntimeError('full collection failed')
    with pytest.raises(RuntimeError):
        shadow_round(reg, ledger, failing, timings={})
    assert sorted(backlog.glob('*.json.gz')) == waiting
    assert not (backlog / '.consumed.json').exists()


def test_the_publisher_round_ingests_what_a_full_collection_would(tmp_path):
    from pulse_compute_cost.incremental_cycle import publisher_round
    from pulse_compute_cost.receipt_sources import collect_registered_sources
    ledger, reg, spool, bundles = publisher(tmp_path)
    twin, twin_spool = UsageLedger(tmp_path / 'twin'), tmp_path / 'twin-spool'
    import shutil
    shutil.copytree(ledger.root / 'receipts', twin.root / 'receipts')
    twin_reg = {**reg, 'ledger_root': str(twin.root), 'sources': [{**reg['sources'][0], 'path': str(twin_spool)}]}
    for data in bundles:
        store(spool, PEER, data)
        store(twin_spool, PEER, data)
        timings = {}
        publisher_round(reg, ledger, timings=timings)
        collect_registered_sources(twin_reg, twin, now=NOW)
        assert 'incremental' in timings and not list(spool.glob('*.json.gz'))
    refs = [{r.fact_ref for r in side.receipt_snapshot()['facts']} for side in (ledger, twin)]
    assert refs[0] == refs[1]


def test_a_trial_judges_the_bundles_this_node_pushed_as_the_publisher_would(tmp_path):
    # 2026-11-20 review repro: the copies carry the pushing peer's name; judged as
    # `local` every one was refused and the trial judged nothing.
    import shutil
    from pulse_compute_cost.incremental_cycle import trial_round
    from pulse_compute_cost.receipt_sources import collect_registered_sources
    ledger, reg, spool, bundles = publisher(tmp_path)
    own = {**reg, 'sources': [{'id': SOURCE, 'kind': 'receipts', 'path': str(tmp_path / 'unused.jsonl')}]}
    twin = UsageLedger(tmp_path / 'twin')
    shutil.copytree(ledger.root / 'receipts', twin.root / 'receipts')
    trial_spool = tmp_path / 'trial-spool'
    for data in bundles[:3]:
        store(trial_spool, PEER, data)
        store(spool, PEER, data)
    out = tmp_path / 'trial'
    before = {r.fact_ref for r in twin.receipt_snapshot()['facts']}
    record = trial_round(own, UsageLedger(ledger.root, read_only=True), spool=trial_spool, out=out, peer=PEER)
    assert (len(record['bundles']), record['invalid_bundles']) == (3, 0)
    assert not list(trial_spool.glob('*.json.gz'))  # Judged, then removed from the trial spool.
    twin_reg = {**reg, 'ledger_root': str(twin.root)}
    collect_registered_sources(twin_reg, twin)
    gained = {r.fact_ref for r in twin.receipt_snapshot()['facts']} - before
    recorded = json.loads((out / 'rounds.jsonl').read_text().splitlines()[-1])
    assert gained and set(recorded['new_refs']) == gained
    assert {r.fact_ref for r in ledger.receipt_snapshot()['facts']} == before  # Nothing ingested.


def test_a_deferred_package_is_not_starved_by_busy_spools(tmp_path):
    # 2026-11-20 review: deferred packages only got what the bundles left; a spool
    # that filled the limit every round kept a small package waiting forever.
    from pulse_compute_cost.incremental_collect import plan, reserved
    source, watermark, reg, bundles = prepared(tmp_path, 5)
    index = open_index(source, cache_mib=16, path=tmp_path / 'index.sqlite')
    try:
        packages = [c for c, _, _ in index.packages()]
        waiting, busy = packages[-1], set(packages[:-1])
        index.record_judgement(index.binding().sequence, [waiting])
        full = index.package_size(0) + sum(index.package_size(c) for c in busy)
        first = reserved(index, full)
        assert first == [waiting]
        chosen = plan(index, busy, limit=full, queued=[], first=first)
        assert waiting in chosen.admitted and not chosen.deferred
        # Without the reservation the busy spool takes the whole limit.
        assert plan(index, busy, limit=full, queued=[], first=[]).deferred == [waiting]
    finally:
        index.close()


def local_bundle(sequence, rows, peer='local'):
    return encode({'schema': SCHEMA, 'peer': peer, 'sequence': sequence, 'created_at': NOW,
                   'sources': [{'config': {'id': SOURCE, 'kind': 'receipts'}, 'rows': [r.to_dict() for r in rows],
                                'inventories': [], 'appended': [],
                                'coverage': {'status': 'complete', 'reason': None, 'expected': None,
                                             'observed': None, 'watermark': None, 'raw_count': len(rows),
                                             'detail': {}, 'diagnostics': {}}}],
                   'inputs': []})


def test_the_publishers_own_backlog_reaches_the_publication(tmp_path):
    # Review r2 repro: two local bundles past the read limit; the collection left one
    # waiting but the publication summary said nothing was waiting.
    from pulse_compute_cost.incremental_collect import LOCAL_SOURCE_ID, judging_registry
    from pulse_compute_cost.stats_dashboard import collection_backlog
    ledger, reg, spool, bundles = publisher(tmp_path)
    own = {**reg, 'sources': [{'id': SOURCE, 'kind': 'receipts', 'path': str(tmp_path / 'unused.jsonl')}]}
    local = tmp_path / 'local-spool'
    rows = generate(31, sessions=4)
    store(local, 'local', local_bundle(1, rows[:10]))
    store(local, 'local', local_bundle(2, rows[10:20]))
    judged = judging_registry(own, local, {})
    assert [s['id'] for s in judged['sources']] == [LOCAL_SOURCE_ID]
    index = open_index(ledger, cache_mib=16)
    try:
        result = collect_incremental(judged, ledger, index, publisher=True, limit=1, retirements={},
                                     recoveries={}, rules_registry=own)
    finally:
        index.close()
    backlog = collection_backlog(own, result)
    assert backlog == {'status': 'partial', 'reason': 'observation_backlog',
                       'peers': [{'id': 'local', 'deferred_bundles': 1}]}


def test_a_changed_alias_history_queues_the_packages_it_may_change(tmp_path):
    # Review r2 repro: the round digested the spool registry, which carries no alias
    # history, so a changed history never queued the old packages.
    from pulse_compute_cost.incremental_collect import judging_registry
    ledger, reg, spool, bundles = publisher(tmp_path)
    source = {'id': SOURCE, 'kind': 'receipts', 'path': str(tmp_path / 'unused.jsonl'),
              'principal_alias_history': [{'alias': 'old', 'principal': 'pulse', 'from': '2026-11-01'}]}
    own = {**reg, 'sources': [source]}
    local = tmp_path / 'local-spool'
    index = open_index(ledger, cache_mib=16)
    try:
        def round_(registry):
            timings = {}
            collect_incremental(judging_registry(registry, local, {}), ledger, index, publisher=True,
                                limit=10 ** 6, retirements={}, recoveries={}, timings=timings,
                                rules_registry=registry)
            return timings['incremental']
        round_(own)
        assert round_(own)['rules_changed'] == []
        changed = {**own, 'sources': [{**source, 'principal_alias_history': [
            {'alias': 'old', 'principal': 'duke', 'from': '2026-11-01'}]}]}
        rejudged = round_(changed)
        assert rejudged['rules_changed'] == ['principal_history']
        # Every package with usage was queued and, within the budget, judged this round.
        assert rejudged['queued'] == len(index.usage_packages()) and index.queue_length() == 0
    finally:
        index.close()


def test_a_trial_records_and_removes_exactly_the_bundles_it_judged(tmp_path):
    # Review r2 repro: a refused bundle 1 and a valid bundle 2; the trial recorded
    # and removed 1 by counting a prefix, and left the judged 2.
    from pulse_compute_cost.incremental_cycle import trial_round
    from pulse_compute_cost.native_observations import spool_path
    ledger, reg, spool, bundles = publisher(tmp_path)
    own = {**reg, 'sources': [{'id': SOURCE, 'kind': 'receipts', 'path': str(tmp_path / 'unused.jsonl')}]}
    trial_spool = tmp_path / 'trial-spool'
    store(trial_spool, PEER, bundles[0])  # Sequence 1 ...
    store(trial_spool, PEER, bundles[1])
    spool_path(trial_spool, 1).write_bytes(b'not a bundle')  # ... damaged after it was spooled.
    out = tmp_path / 'trial'
    record = trial_round(own, UsageLedger(ledger.root, read_only=True), spool=trial_spool, out=out, peer=PEER)
    assert (record['bundles'], record['invalid_bundles']) == ([spool_path(trial_spool, 2).name], 1)
    assert spool_path(trial_spool, 1).exists() and not spool_path(trial_spool, 2).exists()
