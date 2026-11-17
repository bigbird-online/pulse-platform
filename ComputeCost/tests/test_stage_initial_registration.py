"""First Host enrollment prepares a candidate, never a new or replacement store."""
import json
import os
from pathlib import Path

import pytest

from pulse_compute_cost import source_registration


def run(monkeypatch, registry, output, root, *extra):
    monkeypatch.setattr('sys.argv', ['register', '--registry', str(registry),
        '--output', str(output), '--stage-semantics', '--stage-ledger-root', str(root), *extra])
    return source_registration.main()


def test_first_enrollment_preserves_existing_root_and_does_not_activate(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'compute-cost'
    root.mkdir(mode=0o700)
    spool = root / 'host-receipts'
    spool.mkdir(mode=0o700)
    original = spool / 'retained.jsonl'
    original.write_bytes(b'original Host evidence; do not import or alter\n')
    before = original.stat()
    registry, output = tmp_path / 'sources.json', tmp_path / 'candidate.json'
    assert run(monkeypatch, registry, output, root) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status'] == 'candidate_only'
    assert receipt['ledger'] == 'existing_unchanged'
    assert receipt['source_count'] == 0
    assert json.loads(output.read_bytes()) == {
        'schema': 'computecost_source_registry/v1', 'ledger_root': str(root),
        'sources': [], 'required_receipt_features': ['stage_semantics.v1']}
    assert not registry.exists()
    assert output.stat().st_mode & 0o077 == 0
    assert output.stat().st_uid == os.getuid()
    assert set(root.iterdir()) == {spool}
    after = original.stat()
    assert (before.st_ino, before.st_size, before.st_mode, before.st_mtime_ns) == (
        after.st_ino, after.st_size, after.st_mode, after.st_mtime_ns)
    assert original.read_bytes() == b'original Host evidence; do not import or alter\n'
    assert run(monkeypatch, registry, output, root) == 2
    assert not registry.exists()


@pytest.mark.parametrize('existing', ['file', 'symlink', 'dangling'])
def test_first_enrollment_cannot_replace_an_existing_registry(tmp_path, monkeypatch, existing):
    root = tmp_path / 'ledger'
    root.mkdir(mode=0o700)
    registry, output = tmp_path / 'sources.json', tmp_path / 'candidate.json'
    old = tmp_path / 'old.json'
    if existing != 'dangling':
        old.write_text('{"retain":"every field and original path"}')
    if existing == 'file':
        registry.write_bytes(old.read_bytes())
    else:
        registry.symlink_to(old)
    assert run(monkeypatch, registry, output, root) == 2
    assert not output.exists()
    assert registry.is_symlink() if existing != 'file' else registry.read_bytes() == old.read_bytes()


@pytest.mark.parametrize('kind', ['missing', 'relative', 'symlink', 'parent-symlink', 'file'])
def test_first_enrollment_requires_the_explicit_existing_physical_directory(tmp_path, monkeypatch, kind):
    actual = tmp_path / 'actual'
    actual.mkdir(mode=0o700)
    root = tmp_path / kind
    if kind == 'relative':
        root = Path('relative')
    elif kind == 'symlink':
        root.symlink_to(actual)
    elif kind == 'parent-symlink':
        root.symlink_to(tmp_path, target_is_directory=True)
        root = root / 'actual'
    elif kind == 'file':
        root.write_bytes(b'not a directory')
    registry, output = tmp_path / 'sources.json', tmp_path / 'candidate.json'
    assert run(monkeypatch, registry, output, root) == 2
    assert not registry.exists() and not output.exists()
    assert list(actual.iterdir()) == []


@pytest.mark.parametrize('extra', [['--pi', '/unreviewed'], ['--runtime-manifest', '/unreviewed'],
    ['--tenant-registry', '/unreviewed'], ['--verification-host', 'unreviewed']])
def test_first_enrollment_cannot_enable_collection_or_infer_history(tmp_path, monkeypatch, extra):
    assert run(monkeypatch, tmp_path / 'sources.json', tmp_path / 'candidate.json', tmp_path, *extra) == 2
    assert list(tmp_path.iterdir()) == []


def test_first_enrollment_cannot_write_the_selected_registry(tmp_path, monkeypatch):
    registry = tmp_path / 'sources.json'
    assert run(monkeypatch, registry, registry, tmp_path) == 2
    assert list(tmp_path.iterdir()) == []


def test_first_enrollment_requires_explicit_stage_selection(tmp_path, monkeypatch):
    monkeypatch.setattr('sys.argv', ['register', '--registry', str(tmp_path / 'sources.json'),
        '--output', str(tmp_path / 'candidate.json'), '--stage-ledger-root', str(tmp_path)])
    assert source_registration.main() == 2
    assert list(tmp_path.iterdir()) == []
