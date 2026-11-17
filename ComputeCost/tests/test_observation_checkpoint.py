"""Persisted verified prefix: incremental reads equal complete reads, or fall back."""
from datetime import datetime, timedelta, timezone
import json
import os
import random
import zlib

import pytest
from harness_rewrite.model import digest
from harness_rewrite.observations import ObservationStore, PREFIX
from tests_rewrite.test_production_ledger import Rig, git
from pulse_compute_cost import observation_checkpoint as checkpoint_module
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.observation_authority import InstalledAuthority
from test_stage_record import capture, command, record_stage, query, link, WI

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def node(rig, root, client=None, **kwargs):
    """A fresh process: new Git binding, core store, bridge and ledger."""
    store = ObservationStore(rig.backend(client or rig.a))
    return UsageLedger(root, authority=InstalledAuthority(store),
                       clock=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc), **kwargs)


def private(path):
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def fact(rnd, size=300):
    def ref():
        return 'ccf1_' + rnd.randbytes(32).hex()

    body = {'schema': 'computecost_collaboration_fact/v1', 'kind': rnd.choice(['usage', 'coverage', 'attribution']),
            'entity_ref': ref(), 'occurred_at': rnd.choice([None, '2026-10-01T00:00:00Z']),
            'producer': 'test.v1', 'supersedes': rnd.choice([None, ref()]),
            'payload': {'n': rnd.randint(0, 10**9), 'note': 'é,"payload":"x' * (size // 14), 'nested': {
                'producer': 'inner', 'list': [1.5, None, True, '中文']}}}
    body['fact_ref'] = 'ccf1_' + digest(['fact', body])
    body['observed_at'] = '2026-10-01T00:00:00+00:00'
    body['evidence'] = sorted({ref() for _ in range(rnd.randint(0, 3))})
    return body


def append(rig, count, *, seed, big=()):
    """Publish `count` synthetic batches; indexes in `big` exceed the inline limit."""
    rnd = random.Random(seed)
    store = ObservationStore(rig.backend(rig.a))
    batches = store.read()
    previous = batches[-1]['sha256'] if batches else None
    added = []
    for i in range(count):
        sequence = len(batches) + len(added) + 1
        facts = ([fact(rnd, 600_000)] if i in big else []) + [fact(rnd) for _ in range(rnd.randint(1, 5))]
        batch = {'schema': 'computecost_ledger_transaction/v1', 'sequence': sequence,
                 'recorded_at': (T0 + timedelta(minutes=sequence)).isoformat().replace('+00:00', 'Z'),
                 'previous': previous, 'facts': facts}
        batch['sha256'] = digest(batch)
        previous = batch['sha256']
        added.append(batch)
    store.append_many(added)
    return store.sequence


def view(ledger):
    """Every derived value a consumer can observe from the ledger."""
    head = ledger.receipt_snapshot()
    batches = ledger._receipt_cache_batches
    marks = {w: ledger.receipt_snapshot(watermark=w) for w in range(0, len(batches) + 1)}
    cut = batches[len(batches) // 2]['recorded_at'] if batches else None
    return {'head': head, 'batches': list(batches), 'rows': [list(r) for r in ledger._receipt_cache_rows],
            'marks': {w: (s['sha256'], s['watermark'], s['recorded_at'], s['facts']) for w, s in marks.items()},
            'as_of': ledger.receipt_snapshot(as_of=cut) if cut else None}


def resumed(ledger):
    return ledger.authority._store.resumed


def manifest_path(root):
    return root / 'observation-checkpoint' / 'manifest.json'


def test_incremental_reads_equal_complete_reads_across_appends_and_restarts(tmp_path):
    rig = Rig(tmp_path)
    root = private(tmp_path / 'cache')
    append(rig, 6, seed=1, big={2})
    first = node(rig, root)
    assert view(first) == view(node(rig, tmp_path / 'absent', checkpoint=False))
    assert resumed(first) == 0 and manifest_path(root).exists()
    for round_, count in enumerate([1, 4, 0, 7, 2], 2):
        before = json.loads(manifest_path(root).read_bytes())['prefix']['sequence']
        head = append(rig, count, seed=round_, big={1} if count > 3 else set()) if count else before
        incremental = node(rig, root)
        expected = view(node(rig, tmp_path / 'absent', checkpoint=False))
        assert view(incremental) == expected
        # Only the batches after the persisted prefix were read and validated.
        assert resumed(incremental) == before
        assert json.loads(manifest_path(root).read_bytes())['prefix']['sequence'] == head
    # A peer node reading the same Git authority derives the same state.
    assert view(node(rig, private(tmp_path / 'peer'), rig.b)) == expected


def test_record_stage_retry_and_queries_match_complete_read(tmp_path):
    rig = Rig(tmp_path)
    root = private(tmp_path / 'cache')
    source = capture()
    saved = record_stage(node(rig, root), source, command(source))
    # The process's own append advanced the checkpoint to its new head.
    assert json.loads(manifest_path(root).read_bytes())['prefix']['sequence'] == saved['watermark']
    retry = node(rig, root)
    repeated = record_stage(retry, source, command(source))
    assert resumed(retry) == saved['watermark']
    assert repeated['inserted'] == 0 and repeated['watermark'] == saved['watermark']
    full_retry = record_stage(node(rig, tmp_path / 'absent', checkpoint=False), source, command(source))
    assert full_retry == repeated
    record_stage(node(rig, root), source, link(source, saved['context_ref'],
        [saved['changes'][0]['entity_ref'], source['usage'][0].entity_ref]))
    for kwargs in ({}, {'work_items': (WI,)}, {'watermark': saved['watermark']}):
        assert query(node(rig, root), **kwargs) == query(node(rig, tmp_path / 'absent', checkpoint=False), **kwargs)


def _rewrite(rig, path, content):
    git(rig.a, 'pull', '-q', 'origin', 'main')
    target = rig.a / path
    target.write_bytes(content)
    git(rig.a, 'add', path)
    git(rig.a, 'commit', '-qm', 'rewrite history')
    git(rig.a, 'push', '-q', 'origin', 'main')


def test_changed_historical_object_is_read_completely_and_rejected(tmp_path):
    rig = Rig(tmp_path)
    root = private(tmp_path / 'cache')
    append(rig, 5, seed=3)
    view(node(rig, root))
    path = f'{PREFIX}{2:020d}.json'
    wrapper = json.loads(git(rig.remote, 'show', 'main:' + path))
    wrapper['record']['facts'][0]['payload']['n'] += 1
    _rewrite(rig, path, json.dumps(wrapper).encode())
    with pytest.raises(Exception) as complete:
        node(rig, tmp_path / 'absent', checkpoint=False).receipt_snapshot()
    ledger = node(rig, root)
    with pytest.raises(type(complete.value)):
        ledger.receipt_snapshot()
    assert resumed(ledger) == 0


def test_reencoded_historical_object_rebuilds_from_a_complete_read(tmp_path):
    rig = Rig(tmp_path)
    root = private(tmp_path / 'cache')
    append(rig, 4, seed=4)
    view(node(rig, root))
    path = f'{PREFIX}{3:020d}.json'
    wrapper = json.loads(git(rig.remote, 'show', 'main:' + path))
    _rewrite(rig, path, json.dumps(wrapper, indent=1).encode())  # Same batch, new object id.
    ledger = node(rig, root)
    assert view(ledger) == view(node(rig, tmp_path / 'absent', checkpoint=False))
    assert resumed(ledger) == 0
    again = node(rig, root)
    view(again)
    assert resumed(again) == 4


def _manifest(root):
    return json.loads(manifest_path(root).read_bytes())


def _store_manifest(root, manifest, *, resign=True):
    if resign:
        manifest = {k: v for k, v in manifest.items() if k != 'digest'}
        manifest['digest'] = digest(manifest)
    manifest_path(root).write_bytes(json.dumps(manifest).encode())


def _segment(root, entry):
    return root / 'observation-checkpoint' / f"seg-{entry['sha256']}.z"


def _rewrite_segment(root, edit):
    """Replace the single segment with edited decompressed lines; re-sign everything."""
    manifest = _manifest(root)
    [entry] = manifest['segments']
    lines = zlib.decompress(_segment(root, entry).read_bytes()).split(b'\n')
    data = zlib.compress(b'\n'.join(edit(lines)))
    from hashlib import sha256
    entry = {**entry, 'sha256': sha256(data).hexdigest(), 'size': len(data)}
    path = _segment(root, entry)
    path.write_bytes(data)
    os.chmod(path, 0o600)
    manifest['segments'] = [entry]
    return manifest


def _forge_fact(lines):
    """Change one fact and recompute every batch digest, chain and count."""
    meta = json.loads(lines[0])
    fact_line = json.loads(lines[1])
    fact_line['payload'] = {'forged': True}
    lines[1] = json.dumps(fact_line, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    out, i, previous = [], 0, None
    while i < len(lines) and lines[i]:
        meta = json.loads(lines[i])
        facts = lines[i + 1:i + 1 + meta['count']]
        meta['previous'] = previous
        meta['sha256'] = checkpoint_module.batch_digest(meta, facts)
        previous = meta['sha256']
        out += [json.dumps(meta, sort_keys=True, separators=(',', ':')).encode(), *facts]
        i += 1 + meta['count']
    return out + [b'']


TAMPERING = {
    'manifest_unsigned_edit': lambda root: _store_manifest(root, {**_manifest(root), 'logic': 'x'}, resign=False),
    'watermark': lambda root: _store_manifest(root, {**_manifest(root), 'prefix': {
        **_manifest(root)['prefix'], 'watermark': 'f' * 64}}),
    'objects': lambda root: _store_manifest(root, {**_manifest(root), 'prefix': {
        **_manifest(root)['prefix'], 'objects': 'f' * 64}}),
    'sequence_beyond_head': lambda root: _store_manifest(root, {**_manifest(root),
        'prefix': {**_manifest(root)['prefix'], 'sequence': 9},
        'segments': [{**_manifest(root)['segments'][0], 'last': 9}]}),
    'segment_bytes': lambda root: _segment(root, _manifest(root)['segments'][0]).write_bytes(b'garbage'),
    'segment_missing': lambda root: _segment(root, _manifest(root)['segments'][0]).unlink(),
    'segment_truncated': lambda root: _segment(root, _manifest(root)['segments'][0]).write_bytes(
        _segment(root, _manifest(root)['segments'][0]).read_bytes()[:-9]),
    'fact_without_digest': lambda root: _store_manifest(root, _rewrite_segment(root,
        lambda lines: [lines[0], lines[1].replace(b'"n":', b'"n":1'), *lines[2:]])),
    'fact_with_forged_chain': lambda root: _store_manifest(root, {**(m := _rewrite_segment(root, _forge_fact)),
        'prefix': {**m['prefix'], 'watermark': json.loads(
            [line for line in zlib.decompress(_segment(root, m['segments'][0]).read_bytes()).split(b'\n')
             if line.startswith(b'{"count"')][-1])['sha256']}}),
    'logic_version': lambda root: _store_manifest(root, {**_manifest(root), 'logic': 'f' * 64}),
    'namespace': lambda root: _store_manifest(root, {**_manifest(root), 'binding': {
        **_manifest(root)['binding'], 'namespace': 'another'}}),
    'manifest_symlink': lambda root: (manifest_path(root).rename(root / 'elsewhere.json'),
                                      manifest_path(root).symlink_to(root / 'elsewhere.json')),
    'manifest_permissions': lambda root: os.chmod(manifest_path(root), 0o644),
    'segment_permissions': lambda root: os.chmod(_segment(root, _manifest(root)['segments'][0]), 0o640),
    'directory_permissions': lambda root: os.chmod(root / 'observation-checkpoint', 0o755),
    'garbage_manifest': lambda root: manifest_path(root).write_bytes(b'{not json'),
}


@pytest.mark.parametrize('case', sorted(TAMPERING))
def test_untrusted_checkpoint_falls_back_to_complete_read(tmp_path, case):
    rig = Rig(tmp_path)
    root = private(tmp_path / 'cache')
    append(rig, 3, seed=5)
    view(node(rig, root))
    append(rig, 1, seed=6)
    TAMPERING[case](root)
    ledger = node(rig, root)
    assert view(ledger) == view(node(rig, tmp_path / 'absent', checkpoint=False))
    assert resumed(ledger) == 0
    if case != 'directory_permissions':
        # The complete read replaced the untrusted checkpoint.
        again = node(rig, root)
        view(again)
        assert resumed(again) == 4


def test_unusable_state_directory_only_costs_speed(tmp_path):
    rig = Rig(tmp_path)
    append(rig, 2, seed=7)
    shared = tmp_path / 'shared'
    shared.mkdir(mode=0o777)
    os.chmod(shared, 0o777)
    ledger = node(rig, shared)
    assert view(ledger) == view(node(rig, tmp_path / 'absent', checkpoint=False))
    assert not (shared / 'observation-checkpoint').exists()


def test_older_core_without_prefix_resume_reads_completely(tmp_path):
    rig = Rig(tmp_path)
    root = private(tmp_path / 'cache')
    append(rig, 2, seed=8)

    class OldStore(ObservationStore):
        prefix_identity = property(lambda self: (_ for _ in ()).throw(AttributeError))  # type: ignore

    ledger = UsageLedger(root, authority=InstalledAuthority(OldStore(rig.backend(rig.a))))
    assert view(ledger) == view(node(rig, tmp_path / 'absent', checkpoint=False))
    assert not manifest_path(root).exists()


def test_compaction_preserves_equivalence(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, 'MAX_SEGMENTS', 3)
    rig = Rig(tmp_path)
    root = private(tmp_path / 'cache')
    append(rig, 1, seed=9)
    view(node(rig, root))
    for i in range(6):
        append(rig, 1, seed=10 + i)
        ledger = node(rig, root)
        assert view(ledger) == view(node(rig, tmp_path / 'absent', checkpoint=False))
        assert resumed(ledger) == i + 1
        assert len(_manifest(root)['segments']) <= 3
    files = {p.name for p in (root / 'observation-checkpoint').glob('seg-*.z')}
    assert len(files) <= 2 * 3  # Unreferenced generations are removed.


def test_checkpoint_never_regresses_to_an_older_prefix(tmp_path):
    rig = Rig(tmp_path)
    root = private(tmp_path / 'cache')
    append(rig, 2, seed=20)
    stale = node(rig, root)
    view(stale)
    older = _manifest(root)
    append(rig, 2, seed=21)
    view(node(rig, root))
    newer = _manifest(root)
    assert newer['prefix']['sequence'] == 4
    # A slower process that verified only the older prefix publishes late.
    assert stale._checkpoint().commit(None, older['prefix'], older) is None
    assert _manifest(root) == newer
    # Unless the newer manifest is exactly the one it found untrustworthy.
    assert stale._checkpoint().commit(None, older['prefix'], older, rejected=newer['digest']) is not None
    assert _manifest(root)['prefix'] == older['prefix']


def test_removed_head_never_keeps_checkpoint_facts(tmp_path):
    """Review repro: history objects unchanged but head.json gone; the complete read decides."""
    rig = Rig(tmp_path)
    root = private(tmp_path / 'cache')
    append(rig, 3, seed=11)
    view(node(rig, root))
    assert json.loads(manifest_path(root).read_bytes())['prefix']['sequence'] == 3
    git(rig.a, 'pull', '-q', 'origin', 'main')
    git(rig.a, 'rm', '-q', PREFIX + 'head.json')
    git(rig.a, 'commit', '-qm', 'drop observation head')
    git(rig.a, 'push', '-q', 'origin', 'main')
    complete = view(node(rig, tmp_path / 'absent', checkpoint=False))
    assert complete['head']['watermark'] == 0 and not complete['batches']
    incremental = node(rig, root)
    assert view(incremental) == complete
    assert resumed(incremental) == 0
