"""Private saved usage can recover its original Host header without that process."""
from dataclasses import replace
import base64

import pytest

from harness_rewrite import Query, SourceInput
from harness_rewrite.domain import INTENTS
from harness_rewrite.model import digest, encoded
from tests_rewrite.test_lightweight import light
from tests_rewrite.test_measurements import HEADER, admission, log, row, values
from tests_rewrite.test_workflow import BASE, original, error


def captured(tmp_path, change=None, messages=None, production=False, bounded=False):
    rig = light(tmp_path)
    if production:
        # Canonical WI assignment through an isolated real Git backend, not a
        # relaxed ComputeCost validator or a renamed in-memory WI projection.
        from harness_rewrite import Command, Kernel
        from tests_rewrite.test_production_ledger import Rig as GitRig
        from tests_rewrite.test_workflow import DEFINITION
        root = tmp_path / 'isolated-git'
        root.mkdir()
        git_rig = GitRig(root)
        rig.kernel = Kernel(root / 'must-not-exist.sqlite', rig.contexts.__getitem__,
                            storage=git_rig.ledger, clock=lambda: '2026-10-09T00:00:00Z')
        rig.wi = original(rig.kernel.execute(Command('light', 'create_lightweight', DEFINITION),
                                           'create'))['work_item_id']
        original(rig.approve('record_affiliation', {'type': 'operating_area', 'ref': 'OA-PULSE',
            'work_class': 'improvement', 'reason': 'synthetic measurement'},
            actor=replace(BASE, allowed_intents=INTENTS)))
        original(rig.send('start_work', {}))
    # The old actual Host principal is reconstructed exactly as NativeHost does.
    old = replace(BASE, session='session.' + digest([
        BASE.namespace, BASE.actor, HEADER['id'], BASE.surface]), allowed_intents=INTENTS)
    rig.contexts['owner'] = old
    # Keep the fixture's original execution holder, but grant no new production authority.
    # Reinitialize through the public human-control decision in this isolated test.
    original(rig.approve('assume_control', {'reason': 'synthetic original session'}, actor=old))
    old = replace(old, generation=rig.state()['work_item']['generation'])
    rig.contexts['owner'] = old
    command = {'intent': 'open_usage_interval', 'work_item_id': rig.wi,
               'input': {'task_attempt': None}}
    document = {'native_header': HEADER, 'command': command}
    if change == 'header':
        document = {**document, 'native_header': {**HEADER, 'id': 'other'}}
    elif change == 'command':
        document = {**document, 'command': {**command, 'work_item_id': 'wi_other'}}
    source = None if change == 'missing' else SourceInput('open-source', 'native_agent', encoded(document))
    rig.contexts['open'] = replace(old, native_cursor=0, source=source)
    original(rig.send('open_usage_interval', command['input'], handle='open'))
    path = log(tmp_path, *(messages or [row({'input': 7, 'output': 3})]))
    data = admission(rig, path, context=old, start=len(encoded(HEADER)) + 1 if bounded else 0)
    result = original(rig.send('record_usage', data, handle='usage'))
    item = values(rig)['usage'][0]
    return rig, path, result, item


def test_successor_reads_private_original_header_without_old_process_or_mutation(tmp_path):
    rig, path, result, item = captured(tmp_path)
    path.unlink()
    rig.contexts['successor'] = replace(BASE, session='new-session')
    before = rig.kernel._database.read_bytes()
    response = rig.state(query=Query(rig.wi, view='artifact', artifact_ref=item['evidence']), handle='successor')
    assert response['status'] == 'read_only'
    artifact = response['result']['artifact']
    proof, = artifact['native_usage_sources']
    assert proof == {'schema': 'harness.saved-usage-source/v1', 'status': 'verified',
        'record_id': result['record_id'], 'interval_id': item['interval_id'],
        'session': item['session'], 'actor': BASE.actor, 'native_header': HEADER,
        'source_sha256': item['source_sha256'],
        'interval_source_sha256': digest({'native_header': HEADER, 'command': {
            'intent': 'open_usage_interval', 'work_item_id': rig.wi, 'input': {'task_attempt': None}}})}
    assert b'PRIVATE_MESSAGE_NOT_PUBLIC' in base64.b64decode(artifact['base64'])
    assert rig.kernel._database.read_bytes() == before
    assert b'private-provider-native-id' not in encoded(values(rig))
    rig.contexts['outsider'] = replace(BASE, actor='not-owner')
    assert error(rig.state(query=Query(rig.wi, view='artifact', artifact_ref=item['evidence']),
                           handle='outsider')) == 'authority_denied'


@pytest.mark.parametrize('production', [False, True], ids=['sqlite-fixture', 'isolated-production-git'])
def test_source_reader_returns_exact_blob_bytes_from_each_backend(tmp_path, monkeypatch, production):
    from harness_rewrite import measurements

    rig, path, _, item = captured(tmp_path, production=production, bounded=True)
    path.unlink()
    rig.contexts['successor'] = replace(BASE, session='new-session')
    expected = encoded({'native_header': HEADER, 'command': {
        'intent': 'open_usage_interval', 'work_item_id': rig.wi,
        'input': {'task_attempt': None}}})
    project = measurements.saved_usage_sources
    observed = []

    def observe_reader(state, history, ref, read_original):
        content = read_original(digest({'native_header': HEADER, 'command': {
            'intent': 'open_usage_interval', 'work_item_id': rig.wi,
            'input': {'task_attempt': None}}}))
        assert type(content) is bytes
        assert content == expected
        observed.append(content)
        return project(state, history, ref, read_original)

    monkeypatch.setattr(measurements, 'saved_usage_sources', observe_reader)
    before = rig.state()['work_item']['local_revision']
    response = rig.state(query=Query(rig.wi, view='artifact', artifact_ref=item['evidence']),
                         handle='successor')
    assert response['status'] == 'read_only'
    proof, = response['result']['artifact']['native_usage_sources']
    assert proof['status'] == 'verified'
    assert proof['native_header'] == HEADER
    assert observed == [expected]
    assert rig.state()['work_item']['local_revision'] == before


@pytest.mark.parametrize('problem', ['absent', 'corrupt'])
def test_private_interval_custody_failure_does_not_revoke_saved_decisions(tmp_path, problem):
    rig, path, _, item = captured(tmp_path, production=True, bounded=True)
    path.unlink()
    rig.contexts['successor'] = replace(BASE, session='new-session')
    source_sha = digest({'native_header': HEADER, 'command': {
        'intent': 'open_usage_interval', 'work_item_id': rig.wi,
        'input': {'task_attempt': None}}})
    source = rig.kernel._storage.evidence.root / source_sha
    assert source.is_file()
    if problem == 'absent':
        source.unlink()  # Isolated fixture custody only, never a production original.
    else:
        source.write_bytes(b'changed isolated fixture')
    before = rig.state()['work_item']
    response = rig.state(query=Query(rig.wi, view='artifact', artifact_ref=item['evidence']),
                         handle='successor')
    assert error(response) == ('evidence_unavailable' if problem == 'absent' else 'evidence_invalid')
    state = rig.state()
    assert state['status'] == 'read_only'
    assert state['work_item'] == before
    assert values(rig)['usage'][0] == item
    assert any(c['name'] == 'local_work' and c['state'] == 'available' for c in state['capabilities'])


@pytest.mark.parametrize('change', ['missing', 'header', 'command'])
def test_missing_or_mismatched_interval_source_never_becomes_provenance(tmp_path, change):
    rig, _, _, item = captured(tmp_path, change)
    response = rig.state(query=Query(rig.wi, view='artifact', artifact_ref=item['evidence']))
    assert response['status'] == 'read_only'
    proof, = response['result']['artifact']['native_usage_sources']
    assert proof['status'] == 'unavailable'
    assert 'native_header' not in proof
