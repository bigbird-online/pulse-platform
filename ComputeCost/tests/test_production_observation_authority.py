"""Saved stage facts survive loss of every local calculation directory."""
from datetime import datetime, timezone
import json

import pytest
from harness_rewrite.observations import ObservationStore
from harness_rewrite.model import Rejected
from tests_rewrite.test_production_ledger import Rig, git
from pulse_compute_cost.ledger import UsageLedger
from test_stage_record import capture, command, record_stage, query, link, WI, ASSISTANT


def projected(rig, path, client=None):
    authority = ObservationStore(rig.backend(client or rig.a))
    return UsageLedger(path, authority=authority,
        clock=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc))


def test_pre_wi_save_rebuild_other_node_same_watermark_link_and_new_usage(tmp_path):
    rig = Rig(tmp_path)
    ledger = projected(rig, tmp_path / 'node-a-cache')
    source = capture()
    saved = record_stage(ledger, source, command(source))
    assert saved['status'] == 'saved' and saved['execution_authority'] is False
    before = query(ledger)
    historical = query(ledger, watermark=saved['watermark'])
    assert before['stages']['goals'][0]['wi'] is None
    assert before['summary']['usage']['known_total_tokens'] == 35
    peer = projected(rig, tmp_path / 'empty-node-b-cache', rig.b)
    assert query(peer) == before
    assert not list(ledger.root.glob('receipts/*.json'))  # No local authoritative copy.
    repeated = record_stage(peer, source, command(source))
    assert repeated['inserted'] == 0 and repeated['watermark'] == saved['watermark']
    record_stage(peer, source, link(source, saved['context_ref'],
        [saved['changes'][0]['entity_ref'], source['usage'][0].entity_ref]))
    real_retry = capture([{**ASSISTANT, 'id': 'another-model-invocation'}], start=100)
    record_stage(peer, real_retry, command(real_retry, changes=[]))
    rebuilt = projected(rig, tmp_path / 'another-empty-cache')
    assert query(rebuilt)['summary']['usage']['known_total_tokens'] == 70
    assert query(rebuilt, work_items=(WI,))['summary']['usage']['known_total_tokens'] == 35
    assert query(rebuilt, watermark=saved['watermark']) == historical
    files = git(rig.remote, 'ls-tree', '-r', '--name-only', 'main', '--', '.harness/core').splitlines()
    public = '\n'.join(git(rig.remote, 'show', 'main:' + p) for p in files)
    assert 'private raw' not in public and 'synthetic-private-session' not in public
    assert not any('/work-items/' in p for p in files)  # No fake WI or execution grant.


def test_two_clients_cannot_overwrite_the_same_observation_revision(tmp_path):
    rig = Rig(tmp_path)
    first = projected(rig, tmp_path / 'a')
    record_stage(first, capture(), command(capture()))
    a, b = ObservationStore(rig.backend(rig.a)), ObservationStore(rig.backend(rig.b))
    original = a.read(); b.read()
    from harness_rewrite.model import digest
    def next_batch(label):
        value = {**original[-1], 'sequence': 2, 'previous': original[-1]['sha256'], 'recorded_at': label}
        value['sha256'] = digest({k:v for k,v in value.items() if k!='sha256'})
        return value
    a.append(next_batch('2026-10-13T01:00:00Z'))
    with pytest.raises(Rejected, match='dependency changed'):
        b.append(next_batch('2026-10-13T02:00:00Z'))
    assert b.read()[-1]['recorded_at'] == '2026-10-13T01:00:00Z'


def test_installed_bridge_reads_back_lost_success_without_recount(tmp_path):
    from pulse_compute_cost.observation_authority import InstalledAuthority
    from harness_rewrite.model import digest
    rig = Rig(tmp_path)
    ledger = projected(rig, tmp_path / 'cache')
    saved = record_stage(ledger, capture(), command(capture()))
    store = ObservationStore(rig.backend(rig.a))
    first = store.read()[0]
    batch = {**first, 'sequence': 2, 'previous': first['sha256'], 'recorded_at': '2026-10-14T00:00:00Z'}
    batch['sha256'] = digest({k:v for k,v in batch.items() if k != 'sha256'})
    append = store.append
    def lost(value):
        append(value)
        raise OSError('reply lost')
    store.append = lost
    InstalledAuthority(store).append(batch)
    assert len(store.read()) == saved['watermark'] + 1


def test_raw_content_cannot_enter_the_shared_fact_boundary(tmp_path):
    from copy import deepcopy
    from harness_rewrite.model import digest
    rig = Rig(tmp_path)
    ledger = projected(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    store = ObservationStore(rig.backend(rig.a))
    before = store.read()
    row = deepcopy(before[0]['facts'][0])
    row['payload']['prompt'] = 'private original transcript'
    row['fact_ref'] = 'ccf1_' + digest(['fact', {k:v for k,v in row.items() if k not in {'fact_ref','observed_at','evidence'}}])
    batch = {**before[0], 'sequence':2, 'previous':before[0]['sha256'], 'recorded_at':'2026-10-14T00:00:00Z', 'facts':[row]}
    batch['sha256'] = digest({k:v for k,v in batch.items() if k != 'sha256'})
    with pytest.raises(Rejected, match='Private source'):
        store.append(batch)
    assert store.read() == before


def test_adoption_publishes_original_transactions_before_atomic_reader_selection(tmp_path, monkeypatch):
    from pulse_compute_cost import observation_authority as module
    rig = Rig(tmp_path)
    root = tmp_path / 'original'; root.mkdir(mode=0o700)
    local = UsageLedger(root, clock=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc))
    record_stage(local, capture(), command(capture()))
    originals = {p.name: p.read_bytes() for p in (root / 'receipts').glob('*.json')}
    private = tmp_path / 'config'; private.mkdir(mode=0o700)
    path = private / 'sources.json'
    registry = {'ledger_root': str(root), 'observation_core_entrypoint': '/registered/active/harness.pyz'}
    after = json.dumps(registry) + '\n'
    selection = {'path': str(path), 'before': None, 'after': after}
    real = module.InstalledAuthority(ObservationStore(rig.backend(rig.a)))
    monkeypatch.setattr(module, 'installed_store', lambda entry: real)
    original_append = real.append_many
    def lose_reply(batch):
        original_append(batch)
        raise OSError('operator transport interrupted')
    monkeypatch.setattr(real, 'append_many', lose_reply)
    with pytest.raises(OSError):
        module.adopt_original_receipts(registry, selection=selection)
    assert not path.exists()  # No new reader selected while adoption is unknown.
    assert {p.name: p.read_bytes() for p in (root / 'receipts').glob('*.json')} == originals
    monkeypatch.setattr(real, 'append_many', original_append)
    receipt = module.adopt_original_receipts(registry, selection=selection)
    assert receipt['original_transactions'] == receipt['watermark'] == 1
    assert path.read_text() == after and path.stat().st_mode & 0o077 == 0
    inode = path.stat().st_ino
    assert module.adopt_original_receipts(registry, selection=selection) == receipt
    assert path.stat().st_ino == inode
    rebuilt = projected(rig, tmp_path / 'new-empty-projection', rig.b)
    assert query(rebuilt) == query(local)


def test_many_original_transactions_publish_once_without_prefix_rereads(tmp_path, monkeypatch):
    from pulse_compute_cost import observation_authority as module
    from pulse_compute_cost.receipts import digest
    rig = Rig(tmp_path)
    root = tmp_path / 'original'; root.mkdir(mode=0o700)
    local = UsageLedger(root, clock=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc))
    record_stage(local, capture(), command(capture()))
    first = json.loads((root / 'receipts/00000000000000000001.json').read_bytes())
    previous = first['sha256']
    for index in range(2, 21):
        batch = {**first, 'sequence': index, 'previous': previous,
                 'recorded_at': f'2026-10-13T00:00:{index:02d}Z'}
        batch['sha256'] = digest({k: v for k, v in batch.items() if k != 'sha256'})
        (root / 'receipts' / f'{index:020d}.json').write_text(json.dumps(batch))
        previous = batch['sha256']
    store = ObservationStore(rig.backend(rig.a))
    real = module.InstalledAuthority(store)
    monkeypatch.setattr(module, 'installed_store', lambda entry: real)
    before = git(rig.remote, 'rev-list', '--count', 'main')
    registry = {'ledger_root': str(root), 'observation_core_entrypoint': '/registered/active/harness.pyz'}
    preview = module.preview_original_receipts(registry, real)
    assert preview['status'] == 'prepared' and preview['original_transactions'] == 20
    assert git(rig.remote, 'rev-list', '--count', 'main') == before
    receipt = module.adopt_original_receipts(registry)
    assert receipt['original_transactions'] == 20
    assert int(git(rig.remote, 'rev-list', '--count', 'main')) == int(before) + 1
    assert [b['sha256'] for b in store.read()] == [json.loads(p.read_bytes())['sha256']
        for p in sorted((root / 'receipts').glob('*.json'))]
    assert module.verify_original_receipts(registry) == receipt


def test_git_snapshot_cache_never_skips_changed_facts(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    ledger = projected(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    before = query(ledger)
    calls = 0
    iterate = ledger.authority.iter_batches
    def tracked(**kwargs):
        nonlocal calls
        calls += 1
        yield from iterate(**kwargs)
    monkeypatch.setattr(ledger.authority, 'iter_batches', tracked)
    assert query(ledger) == before
    assert calls == 0
    peer = projected(rig, tmp_path / 'peer', rig.b)
    new_call = capture([{**ASSISTANT, 'id': 'separate-call'}], start=100)
    record_stage(peer, new_call, command(new_call, changes=[]))
    assert query(ledger)['summary']['usage']['known_total_tokens'] == 70
    assert calls == 1


def test_unrelated_main_commit_does_not_decode_observations_again(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    ledger = projected(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    before = query(ledger)
    git(rig.b, 'pull', '--ff-only')
    (rig.b / 'unrelated-source.txt').write_text('another PR')
    git(rig.b, 'add', '.')
    git(rig.b, 'commit', '-m', 'Unrelated main change')
    git(rig.b, 'push', 'origin', 'main')
    def forbidden(**kwargs):
        raise AssertionError('unchanged observations decoded')
        yield  # stream bodies run only when decoded
    monkeypatch.setattr(ledger.authority, 'iter_batches', forbidden)
    assert query(ledger) == before


def test_cold_shared_read_uses_bounded_git_batches_and_preserves_every_fact(tmp_path, monkeypatch):
    import subprocess
    from harness_rewrite.model import digest
    rig = Rig(tmp_path)
    ledger = projected(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    source = ObservationStore(rig.backend(rig.a))
    first = source.read()[0]
    batches, previous = [], first['sha256']
    for i in range(2, 22):
        batch = {**first, 'sequence': i, 'previous': previous,
                 'recorded_at': f'2026-10-13T01:00:{i:02d}Z',
                 'facts': first['facts'] * (512 if i == 21 else 1)}
        batch['sha256'] = digest({k: v for k, v in batch.items() if k != 'sha256'})
        batches.append(batch)
        previous = batch['sha256']
    source.append_many(batches)
    reader = ObservationStore(rig.backend(rig.b))
    invocations, popen = [], subprocess.Popen
    def tracked(argv, **kwargs):
        if 'cat-file' in argv:
            invocations.append(argv)
        return popen(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', tracked)
    assert reader.read() == [first, *batches]
    assert len(invocations) <= 3, 'Git process count must not grow per receipt or chunk'
    assert reader.sequence == 21 and reader.watermark == previous
    assert not list((tmp_path / 'cache').glob('receipts/*.json'))


def test_partial_or_invalid_shared_read_cannot_authorize_an_append(tmp_path):
    from harness_rewrite.model import digest
    rig = Rig(tmp_path)
    ledger = projected(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    reader = ObservationStore(rig.backend(rig.a))
    original = reader.read()[0]
    batch = {**original, 'sequence': 2, 'previous': original['sha256'],
             'recorded_at': '2026-10-13T01:00:00Z'}
    batch['sha256'] = digest({k: v for k, v in batch.items() if k != 'sha256'})
    pending = reader.iter_batches()
    assert next(pending) == original
    pending.close()
    with pytest.raises(Rejected, match='Read original observations'):
        reader.append(batch)
    assert reader.read() == [original]
    reader.append(batch)
    assert reader.read() == [original, batch]


def test_stats_export_collects_through_selected_authority_and_preserves_old_receipts(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from pulse_compute_cost import observation_authority as module
    spec = importlib.util.spec_from_file_location('stats_export_under_test',
        Path(__file__).resolve().parents[2] / 'Scripts/pulse-stats-cost-refresh.py')
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    rig = Rig(tmp_path)
    private = tmp_path / 'private'; private.mkdir(mode=0o700)
    registry_path = private / 'sources.json'
    registry = {'ledger_root': str(private / 'ledger'),
        'observation_core_entrypoint': '/registered/active/harness.pyz'}
    registry_path.write_text(json.dumps(registry)); registry_path.chmod(0o600)
    old = UsageLedger(private / 'ledger')
    record_stage(old, capture(), command(capture()))
    originals = {p.name: p.read_bytes() for p in (old.root / 'receipts').glob('*.json')}
    authority = module.InstalledAuthority(ObservationStore(rig.backend(rig.a)))
    monkeypatch.setattr(module, 'installed_store', lambda entry, **kw: authority)
    module.adopt_original_receipts(registry)
    monkeypatch.setattr(exporter, 'REGISTRY', registry_path)
    next_source = capture([{**ASSISTANT, 'id': 'real-next-call'}], start=100)
    def collect(registry, selected, **kwargs):
        record_stage(selected, next_source, command(next_source, changes=[]))
        return {}
    monkeypatch.setattr(exporter, 'collect_registered_sources', collect)
    exported = exporter.collect_export_facts()
    assert {p.name: p.read_bytes() for p in (old.root / 'receipts').glob('*.json')} == originals
    rebuilt = projected(rig, tmp_path / 'remote-empty-cache', rig.b)
    assert exported == rebuilt.receipt_snapshot()['facts']
    assert query(rebuilt)['summary']['usage']['known_total_tokens'] == 70


def test_preview_checks_shared_privacy_boundary_before_any_publication(tmp_path):
    from pulse_compute_cost.observation_authority import InstalledAuthority, preview_original_receipts
    from pulse_compute_cost.receipts import Receipt, digest
    rig = Rig(tmp_path)
    root = tmp_path / 'original'; root.mkdir(mode=0o700)
    ledger = UsageLedger(root, clock=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc))
    value = capture()['usage'][0].to_dict()
    value['payload']['model'] = 'synthetic ghp_example_credential'
    value['fact_ref'] = 'ccf1_' + digest(['fact', {k:v for k,v in value.items()
        if k not in {'fact_ref', 'observed_at', 'evidence'}}])
    ledger.ingest_receipts([Receipt.from_dict(value)])
    before = git(rig.remote, 'rev-parse', 'main')
    registry = {'ledger_root': str(root), 'observation_core_entrypoint': '/registered/active/harness.pyz'}
    with pytest.raises(Rejected, match='Private source'):
        preview_original_receipts(registry, InstalledAuthority(ObservationStore(rig.backend(rig.a))))
    assert git(rig.remote, 'rev-parse', 'main') == before


def test_second_node_adopts_its_original_facts_without_replacing_shared_history(tmp_path, monkeypatch):
    from pulse_compute_cost import observation_authority as module
    rig = Rig(tmp_path)
    shared = projected(rig, tmp_path / 'shared')
    record_stage(shared, capture(), command(capture()))
    original = tmp_path / 'second-node'; original.mkdir(mode=0o700)
    local = UsageLedger(original, clock=lambda: datetime(2026, 10, 14, tzinfo=timezone.utc))
    # Same source observation, but a different local transaction clock/prefix.
    record_stage(local, capture(), command(capture()))
    second = capture([{**ASSISTANT, 'id': 'second-node-new-call'}], start=100)
    record_stage(local, second, command(second, changes=[]))
    files = {p.name: p.read_bytes() for p in (original / 'receipts').glob('*.json')}
    registry = {'ledger_root': str(original), 'observation_core_entrypoint': '/registered/active/harness.pyz'}
    bridge = module.InstalledAuthority(ObservationStore(rig.backend(rig.b)))
    monkeypatch.setattr(module, 'installed_store', lambda *a, **k: bridge)
    before = git(rig.remote, 'rev-parse', 'main')
    module.preview_original_receipts(registry, bridge)
    assert git(rig.remote, 'rev-parse', 'main') == before
    adopted = module.adopt_original_receipts(registry)
    assert adopted['status'] == 'verified'
    head = git(rig.remote, 'rev-parse', 'main')
    assert module.verify_original_receipts(registry) == adopted
    assert module.adopt_original_receipts(registry) == adopted
    assert git(rig.remote, 'rev-parse', 'main') == head
    assert {p.name: p.read_bytes() for p in (original / 'receipts').glob('*.json')} == files
    assert query(projected(rig, tmp_path / 'rebuilt'))['summary']['usage']['known_total_tokens'] == 70


def test_preview_uses_fixed_prefix_without_holding_live_writer_lock(tmp_path, monkeypatch):
    import fcntl
    from pulse_compute_cost import observation_authority as module
    rig = Rig(tmp_path)
    root = tmp_path / 'live'; root.mkdir(mode=0o700)
    local = UsageLedger(root, clock=lambda: datetime(2026, 10, 14, tzinfo=timezone.utc))
    record_stage(local, capture(), command(capture()))
    first = (root / 'receipts/00000000000000000001.json').read_bytes()
    registry = {'ledger_root': str(root), 'observation_core_entrypoint': '/registered/active/harness.pyz'}
    bridge = module.InstalledAuthority(ObservationStore(rig.backend(rig.a)))
    original_validate = bridge.validate_batch
    grew = []
    def validate(*args):
        if not grew:
            with (root / '.usage-ledger.lock').open('a+') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            grew.append(True)
            fresh = capture([{**ASSISTANT, 'id': 'concurrent-live-call'}], start=100)
            record_stage(local, fresh, command(fresh, changes=[]))
        return original_validate(*args)
    monkeypatch.setattr(bridge, 'validate_batch', validate)
    before = git(rig.remote, 'rev-parse', 'main')
    preview = module.preview_original_receipts(registry, bridge)
    assert grew and preview['original_transactions'] == 1
    assert len(list((root / 'receipts').glob('*.json'))) == 2
    assert module.preview_original_receipts(registry, bridge)['original_transactions'] == 2
    assert (root / 'receipts/00000000000000000001.json').read_bytes() == first
    assert git(rig.remote, 'rev-parse', 'main') == before
    with (root / '.usage-ledger.lock').open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(module.ReceiptError, match='defer preparation'):
            module.preview_original_receipts(registry, bridge)


def installed(rig, path, client=None):
    from pulse_compute_cost.observation_authority import InstalledAuthority
    authority = InstalledAuthority(ObservationStore(rig.backend(client or rig.a)))
    return UsageLedger(path, authority=authority,
        clock=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc))


def counted_cold_reads(monkeypatch, ledger):
    store = ledger.authority._store
    calls = []
    iterate = store.iter_batches
    def tracked(**kwargs):
        calls.append(1)
        yield from iterate(**kwargs)
    monkeypatch.setattr(store, 'iter_batches', tracked)
    return calls


def same_snapshot(left, right):
    a, b = left.receipt_snapshot(), right.receipt_snapshot()
    assert a['facts'] == b['facts']
    assert {k: v for k, v in a.items() if k != 'facts'} == {k: v for k, v in b.items() if k != 'facts'}


def test_own_confirmed_append_continues_the_verified_cache(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    ledger = installed(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    query(ledger)
    calls = counted_cold_reads(monkeypatch, ledger)
    new_call = capture([{**ASSISTANT, 'id': 'separate-call'}], start=100)
    record_stage(ledger, new_call, command(new_call, changes=[]))
    warm = query(ledger)
    assert calls == []  # Neither the append nor the following reads decoded the stream.
    cold = installed(rig, tmp_path / 'cold')
    assert warm == query(cold) and warm['summary']['usage']['known_total_tokens'] == 70
    same_snapshot(ledger, cold)
    signature = ledger._receipt_cache_signature
    assert signature == cold._receipt_cache_signature == ('production_git', cold.authority.scan()[0])


def test_append_by_another_node_after_own_append_is_read_cold(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    ledger = installed(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    calls = counted_cold_reads(monkeypatch, ledger)
    own = capture([{**ASSISTANT, 'id': 'own-call'}], start=100)
    record_stage(ledger, own, command(own, changes=[]))
    assert calls == []
    peer = installed(rig, tmp_path / 'peer', rig.b)
    other = capture([{**ASSISTANT, 'id': 'peer-call'}], start=200)
    record_stage(peer, other, command(other, changes=[]))
    assert query(ledger)['summary']['usage']['known_total_tokens'] == 105
    assert calls == [1]
    same_snapshot(ledger, installed(rig, tmp_path / 'cold'))


def test_reply_loss_readback_or_foreign_head_stays_exact(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    ledger = installed(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    store = ledger.authority._store
    append = store.append
    def lost(value):
        append(value)
        raise OSError('reply lost')
    monkeypatch.setattr(store, 'append', lost)
    new_call = capture([{**ASSISTANT, 'id': 'separate-call'}], start=100)
    record_stage(ledger, new_call, command(new_call, changes=[]))
    assert query(ledger)['summary']['usage']['known_total_tokens'] == 70
    same_snapshot(ledger, installed(rig, tmp_path / 'cold'))
    head = store.read()[-1]
    assert ledger.authority.appended_signature({**head, 'sha256': '0' * 64}) is None
    assert ledger.authority.appended_signature({**head, 'sequence': head['sequence'] + 1}) is None


def commit_core_object(rig, path, content):
    git(rig.b, 'pull', '--ff-only')
    target = rig.b / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    git(rig.b, 'add', '.')
    git(rig.b, 'commit', '-m', 'SCL: record native production decision')
    git(rig.b, 'push', 'origin', 'main')


def test_workitem_decision_objects_do_not_decode_observations_again(tmp_path, monkeypatch):
    # Production main changes .harness/core/objects/ with every WI decision.
    rig = Rig(tmp_path)
    ledger = installed(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    before = query(ledger)
    calls = counted_cold_reads(monkeypatch, ledger)
    commit_core_object(rig, '.harness/core/objects/ab/' + 'c' * 64, b'unrelated decision segment')
    assert query(ledger) == before
    assert calls == []
    # The next own append still publishes over the unrelated decision commit.
    new_call = capture([{**ASSISTANT, 'id': 'separate-call'}], start=100)
    record_stage(ledger, new_call, command(new_call, changes=[]))
    assert calls == []
    cold = installed(rig, tmp_path / 'cold')
    assert query(ledger) == query(cold)
    same_snapshot(ledger, cold)


def test_changed_consumed_chunk_object_invalidates_the_verified_cache(tmp_path, monkeypatch):
    from harness_rewrite.observations import PREFIX
    rig = Rig(tmp_path)
    ledger = installed(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    store = ledger.authority._store
    first = store.read()[0]
    from harness_rewrite.model import digest
    big = {**first, 'sequence': 2, 'previous': first['sha256'], 'recorded_at': '2026-10-13T01:00:02Z',
           'facts': first['facts'] * 2048}
    big['sha256'] = digest({k: v for k, v in big.items() if k != 'sha256'})
    store.append_many([big])
    query(ledger)
    chunks = sorted(ledger.authority._verified[1])
    assert chunks and not any(p.startswith(PREFIX) for p in chunks)
    calls = counted_cold_reads(monkeypatch, ledger)
    commit_core_object(rig, chunks[0], b'rewritten history')
    with pytest.raises(Exception):
        query(ledger)
    assert calls == [1]


def test_conflicting_append_by_another_node_is_a_recollection_not_a_readback(tmp_path, monkeypatch):
    from pulse_compute_cost.ledger import SnapshotChanged
    rig = Rig(tmp_path)
    ledger = installed(rig, tmp_path / 'cache')
    record_stage(ledger, capture(), command(capture()))
    stale = ledger.receipt_snapshot()
    peer = installed(rig, tmp_path / 'peer', rig.b)
    other = capture([{**ASSISTANT, 'id': 'peer-call'}], start=200)
    record_stage(peer, other, command(other, changes=[]))
    store = ledger.authority._store
    reads = []
    monkeypatch.setattr(store, 'read', lambda **kw: reads.append(1))
    # Bypass the ingest-time watermark check to reach the core's own lease.
    monkeypatch.setattr(ledger, '_receipt_batches', lambda: ledger._receipt_cache_batches)
    from pulse_compute_cost.receipts import Receipt
    row = stale['facts'][0]
    fresh = Receipt.make(row.kind, 'conflict-probe', row.payload, occurred_at=row.occurred_at,
                         observed_at=row.observed_at, producer=row.producer, evidence=row.evidence)
    before = git(rig.remote, 'rev-parse', 'main')
    with pytest.raises(SnapshotChanged):
        ledger.ingest_receipts([fresh])
    assert reads == [] and git(rig.remote, 'rev-parse', 'main') == before


def _coverage(source, watermark, supersedes=None, evidence=()):
    from pulse_compute_cost.receipts import Receipt, reference
    ref = reference('source', source)
    return Receipt.make('coverage', [ref, 'usage', 'global'], dict(domain='usage', wi=None, source_ref=ref,
        status='unknown', expected=None, observed=None, watermark=watermark, reason=None, source_detail=None),
        occurred_at=None, observed_at='2026-09-13T12:00:00Z', producer='registered_collector.v1',
        supersedes=supersedes, evidence=evidence)


def _coverage_heads(store):
    from pulse_compute_cost.receipts import Receipt, current_receipts
    facts = [Receipt.from_dict(row) for batch in store.read() for row in batch['facts']]
    active, conflicts = current_receipts(facts)
    return {r.entity_ref: r.fact_ref for r in active if r.kind == 'coverage'}, conflicts


def _adopt(tmp_path, monkeypatch, rig, root):
    from pulse_compute_cost import observation_authority as module
    store = ObservationStore(rig.backend(rig.a))
    monkeypatch.setattr(module, 'installed_store', lambda entry: module.InstalledAuthority(store))
    registry = {'ledger_root': str(root), 'observation_core_entrypoint': '/registered/active/harness.pyz'}
    return store, module.adopt_original_receipts(registry), registry, module


def test_adoption_never_forks_coverage_with_a_rebased_transport_copy(tmp_path, monkeypatch):
    from pulse_compute_cost.receipts import reference
    rig = Rig(tmp_path)
    shared = projected(rig, tmp_path / 'cache')
    root, own = _coverage('remote', 'root'), None
    shared.ingest_receipts([root])
    own = _coverage('remote', 'next', supersedes=root.fact_ref)
    shared.ingest_receipts([own])
    # Another node received the same two facts by transport into its own
    # ledger. A second copy of the root with new evidence became an evidence
    # revision, and the ledger rebased the next version onto it.
    original = tmp_path / 'original'; original.mkdir(mode=0o700)
    local = UsageLedger(original, clock=lambda: datetime(2026, 9, 14, tzinfo=timezone.utc))
    local.ingest_receipts([root.__class__.make(root.kind, None, root.payload, occurred_at=None,
        observed_at=root.observed_at, producer=root.producer, entity_ref=root.entity_ref,
        evidence=(reference('transport', 'first'),))])
    local.ingest_receipts([root.__class__.make(root.kind, None, root.payload, occurred_at=None,
        observed_at=root.observed_at, producer=root.producer, entity_ref=root.entity_ref,
        evidence=(reference('transport', 'second'),)), own])
    copies = [r for r in local.receipt_snapshot()['facts'] if r.fact_ref not in {root.fact_ref, own.fact_ref}]
    assert len(copies) == 2 and own.fact_ref not in {r.fact_ref for r in local.receipt_snapshot()['facts']}
    store, receipt, registry, module = _adopt(tmp_path, monkeypatch, rig, original)
    heads, conflicts = _coverage_heads(store)
    assert heads == {root.entity_ref: own.fact_ref} and not conflicts
    assert receipt['coverage_versions_retained_in_originals'] == 3  # Known root, revision, rebased copy.
    assert module.verify_original_receipts(registry)['watermark'] == receipt['watermark']


def test_adoption_extends_current_coverage_heads_and_adds_new_entities(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    shared = projected(rig, tmp_path / 'cache')
    root = _coverage('remote', 'root')
    shared.ingest_receipts([root])
    original = tmp_path / 'original'; original.mkdir(mode=0o700)
    local = UsageLedger(original, clock=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc))
    child, fresh = _coverage('remote', 'child', supersedes=root.fact_ref), _coverage('local', 'first')
    local.ingest_receipts([root]); local.ingest_receipts([child, fresh])
    store, receipt, _, _ = _adopt(tmp_path, monkeypatch, rig, original)
    heads, conflicts = _coverage_heads(store)
    assert heads == {root.entity_ref: child.fact_ref, fresh.entity_ref: fresh.fact_ref} and not conflicts
    assert receipt['coverage_versions_retained_in_originals'] == 0
