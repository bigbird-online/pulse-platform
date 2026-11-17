"""Installed read/write binding, with synthetic parent and private existing store."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from pulse_compute_cost import cli, native_stage_host
from pulse_compute_cost.native_stage_host import NativeStageHost
from pulse_compute_cost.receipts import ReceiptError
from pulse_compute_cost.source_registration import stage_candidate_registry
from test_stage_record import capture, command, record_stage, link, WI


@pytest.fixture
def bound(tmp_path, monkeypatch):
    root = tmp_path / 'existing-ledger'
    root.mkdir(mode=0o700)
    registry = tmp_path / 'sources.json'
    registry.write_text(json.dumps({'schema': 'computecost_source_registry/v1',
        'ledger_root': str(root), 'sources': [], 'required_receipt_features': ['stage_semantics.v1']}))
    registry.chmod(0o600)
    proc = tmp_path / 'proc' / '101'
    proc.mkdir(parents=True)
    (proc / 'environ').write_bytes(b'PULSE_COMPUTECOST_SOURCES=' + str(registry).encode() + b'\0')
    host = SimpleNamespace(proc=proc.parent, native_pid=101, profile={'role': 'producer'},
                           execution={'mode': 'automatic'})
    adapter = NativeStageHost(host, None)
    monkeypatch.setenv('PULSE_WORK_ITEM_CORE_HOST', '{"entrypoint":"test-bound-host"}')
    monkeypatch.delenv('PULSE_COMPUTECOST_SOURCES', raising=False)
    monkeypatch.setattr(native_stage_host, 'installed_host', lambda: adapter)
    return adapter, registry, proc


def invoke(args, capsys):
    assert cli.main(args) == 0
    return json.loads(capsys.readouterr().out)


def test_native_default_wi_and_query_use_the_saved_watermark(bound, capsys):
    host, _, _ = bound
    source = capture()
    first = record_stage(host.ledger(), source, command(source))
    saved = record_stage(host.ledger(), source, link(source, first['context_ref'],
        [first['changes'][0]['entity_ref'], source['usage'][0].entity_ref]))
    args = ['--watermark', str(saved['watermark'])]
    report = invoke(['wi-report', WI, *args], capsys)
    same = invoke(['query', '--work-item', WI, *args], capsys)
    assert report == same
    assert report['stages']['goals'][0]['name'] == '协作计量'
    assert report['summary']['usage']['known_total_tokens'] == 35
    assert host.ledger().receipt_snapshot()['watermark'] == saved['watermark']


def test_native_live_query_reads_saved_facts_without_scanning_all_sources(bound, capsys, monkeypatch):
    host, _, _ = bound
    source = capture()
    record_stage(host.ledger(), source, command(source))
    def forbidden(*a, **k):
        raise AssertionError('normal native read must not trigger full history collection')
    monkeypatch.setattr(cli, 'collect_registered_sources', forbidden)
    report = invoke(['query'], capsys)
    assert 'report' not in report
    assert report['stages']['goals']
    assert report['stages']['execution_authority'] is False


def test_context_continuation_uses_same_native_binding_without_collection(bound, capsys, monkeypatch):
    host, _, _ = bound
    source = capture()
    first = record_stage(host.ledger(), source, command(source))
    associate = link(source, first['context_ref'], [source['usage'][0].entity_ref])
    associate['changes'][0]['payload'].update(wi=None, state='unknown')
    saved = record_stage(host.ledger(), source, associate)
    def forbidden(*a, **k):
        raise AssertionError('continuation must not rescan sources')
    monkeypatch.setattr(cli, 'collect_registered_sources', forbidden)
    result = invoke(['query', '--context-ref', first['context_ref']], capsys)
    assert result['watermark'] == saved['watermark']
    assert result['stages']['goals'][0]['name'] == '协作计量'
    assert result['summary']['usage']['known_total_tokens'] == 35
    assert result['stages']['execution_authority'] is False
    assert host.ledger().receipt_snapshot()['watermark'] == saved['watermark']


def test_invalid_context_fails_before_registry_or_ledger_read(bound, capsys, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('invalid selection must fail before source access')
    monkeypatch.setattr(cli, '_registry', forbidden)
    assert cli.main(['query', '--context-ref', 'not-a-context']) == 1
    assert 'context_ref' in capsys.readouterr().err


def test_child_registry_env_cannot_redirect_native_default_read(bound, tmp_path, monkeypatch, capsys):
    host, _, _ = bound
    source = capture()
    record_stage(host.ledger(), source, command(source))
    monkeypatch.setenv('PULSE_COMPUTECOST_SOURCES', str(tmp_path / 'forged.json'))
    report = invoke(['query'], capsys)
    assert report['stages']['goals']
    assert not (tmp_path / 'forged.json').exists()


@pytest.mark.parametrize('options', [
    ['--ledger-root', '/different'], ['--source-registry', '/different'],
    ['--catalog-snapshot', '/different'], ['--price-snapshot', '/different'],
])
def test_native_read_rejects_store_or_price_override(bound, options, capsys):
    assert cli.main(['query', *options]) == 1
    assert 'native query cannot override' in capsys.readouterr().err


def test_native_report_does_not_silently_bypass_binding_with_snapshot(bound, tmp_path, capsys):
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text('{}')
    assert cli.main(['wi-report', WI, '--harness-query-snapshot', str(snapshot)]) == 1
    assert 'native' in capsys.readouterr().err.lower()


def test_absent_parent_binding_is_not_permission_to_use_child_env(bound, monkeypatch, capsys):
    _, registry, proc = bound
    (proc / 'environ').write_bytes(b'')
    monkeypatch.setenv('PULSE_COMPUTECOST_SOURCES', str(registry))
    assert cli.main(['query']) == 1
    assert 'registered' in capsys.readouterr().err


def test_unregistered_store_is_not_created_by_native_adapter(bound, tmp_path):
    host, path, _ = bound
    registry = json.loads(path.read_text())
    root = tmp_path / 'not-yet-installed'
    registry['ledger_root'] = str(root)
    path.write_text(json.dumps(registry))
    with pytest.raises(ReceiptError):
        host.ledger()
    assert not root.exists()


@pytest.mark.parametrize('features', [['stage_semantics.v1', 'unknown.future'], ['stage_semantics.v1', {}]])
def test_unknown_consumer_requirements_fail_closed(bound, features, capsys):
    _, path, _ = bound
    registry = json.loads(path.read_text())
    registry['required_receipt_features'] = features
    path.write_text(json.dumps(registry))
    assert cli.main(['query']) == 1
    assert 'registered' in capsys.readouterr().err
    with pytest.raises(ReceiptError):
        stage_candidate_registry(registry)


def test_selected_price_failure_does_not_fall_back(bound, tmp_path, capsys):
    _, path, _ = bound
    registry = json.loads(path.read_text())
    registry['catalog_snapshot'] = str(tmp_path / 'missing-price.json')
    path.write_text(json.dumps(registry))
    assert cli.main(['query']) == 0
    out = capsys.readouterr()
    assert 'catalog_snapshot_unavailable' in out.err
    assert json.loads(out.out)['catalog_observed_at'] is None


def test_candidate_cli_preserves_registry_and_never_activates(bound, tmp_path, monkeypatch, capsys):
    from pulse_compute_cost import source_registration
    _, path, _ = bound
    original = path.read_bytes()
    output = tmp_path / 'reviewed-candidate.json'
    monkeypatch.setattr('sys.argv', ['register', '--registry', str(path), '--output', str(output), '--stage-semantics'])
    assert source_registration.main() == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'candidate_only'
    assert path.read_bytes() == original
    assert output.stat().st_mode & 0o077 == 0
    assert json.loads(output.read_text()) == json.loads(original)
    assert source_registration.main() == 2  # exclusive creation, never overwrite
    capsys.readouterr()
    monkeypatch.setattr('sys.argv', ['register', '--registry', str(path), '--output', str(path), '--stage-semantics'])
    assert source_registration.main() == 2
    capsys.readouterr()
    assert path.read_bytes() == original


def test_symlink_store_is_not_a_candidate(bound, tmp_path):
    _, path, _ = bound
    registry = json.loads(path.read_text())
    link = tmp_path / 'redirected-store'
    link.symlink_to(registry['ledger_root'])
    registry['ledger_root'] = str(link)
    with pytest.raises(ReceiptError):
        stage_candidate_registry(registry)


def test_audit_can_query_but_cannot_save(bound, capsys):
    host, _, _ = bound
    host.host.execution['mode'] = 'audit'
    assert invoke(['query'], capsys)['stages']['status'] == 'unavailable'
    with pytest.raises(ReceiptError, match='profile'):
        host.record({})


def test_stage_registration_candidate_preserves_original_data(bound):
    _, path, _ = bound
    original = json.loads(path.read_text())
    original['required_receipt_features'] = ['actor_context.v1']
    original['sources'] = [{'id': 'existing', 'kind': 'receipts', 'path': '/private/old-receipts'}]
    original['catalog_snapshot'] = '/private/original-catalog.json'
    before = deepcopy(original)
    candidate = stage_candidate_registry(original)
    assert original == before
    assert candidate == {**original, 'required_receipt_features': ['actor_context.v1', 'stage_semantics.v1']}
    assert stage_candidate_registry(candidate) == candidate
    assert json.loads(path.read_text()) != candidate  # no activation


@pytest.mark.parametrize('root', ['', 'relative', '/unregistered-stage-store'])
def test_stage_registration_does_not_invent_a_ledger(root):
    with pytest.raises(ReceiptError):
        stage_candidate_registry({'schema': 'computecost_source_registry/v1', 'sources': [], 'ledger_root': root})
