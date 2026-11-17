"""Saved SCL originals join exact Pi calls, never add their aggregate again."""
import base64
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pulse_compute_cost.native_stage_host import NativeStageHost
from pulse_compute_cost.receipts import ReceiptError, digest, current_receipts
from pulse_compute_cost.native_scl_usage import import_saved_usage
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from test_stage_record import HEADER, ASSISTANT, OBSERVED, encoded
from test_stage_usage_reconciliation import SUMMARY, MODEL, ledger
from harness_rewrite.native_usage import capture as native_capture

WI = 'wi_20261008_550'
RECORD = 'op_saved_usage'
PRINCIPAL = {'actor': 'pulse', 'native': 'synthetic'}


@pytest.fixture
def saved(tmp_path):
    path = tmp_path / 'pi.jsonl'
    # Prefix intentionally excludes model context from the saved range.
    prefix = encoded([HEADER, MODEL])
    raw = encoded([ASSISTANT, SUMMARY])
    path.write_bytes(prefix + raw)
    _, packet = native_capture(path, HEADER, len(prefix), len(prefix + raw))
    session = 'measurement-session.' + digest(PRINCIPAL)
    record = {**packet, 'kind': 'usage', 'record_id': RECORD, 'recorded_at': OBSERVED,
              'generation': 1, 'interval_id': 'op_interval', 'task_attempt': 'op_attempt',
              'session': session, 'actor': 'pulse'}
    data = {'source': 'journal', 'coverage': 'recorded_intervals_only', 'usage': [record],
            'attention': [], 'quality': [], 'outcomes': [], 'business_progress': 'active',
            'terminal': None, 'intervals': [{'interval_id': 'op_interval',
                'task_attempt': 'op_attempt', 'start_byte': len(prefix), 'end_byte': None,
                'closed': False, 'end_boundary': 'open', 'phase': 'active',
                'generation': 1, 'session': session}]}
    query = {'schema': 'harness.response/v1', 'status': 'read_only', 'error': None,
             'work_item': {'id': WI, 'generation': 1, 'local_revision': 8, 'business_progress': 'active'},
             'capabilities': [{'name': 'local_read', 'state': 'available', 'source_revision': 8}],
             'result': {'items': data, 'current_observation': {'source': 'journal',
                 'generation': 1, 'business_progress': 'active', 'measurements': data}}}
    return path, raw, query


def consume(ledger, saved):
    path, raw, query = saved
    return import_saved_usage(ledger, query, WI, RECORD, raw, path=path,
        header=HEADER, principal=PRINCIPAL, observed_at=OBSERVED)


def query(ledger, **kwargs):
    return query_collaboration(ledger, CollaborationQuery(**kwargs))


@pytest.mark.parametrize('native_first', [True, False])
def test_saved_original_import_joins_calls_and_survives_reopen(ledger, saved, native_first):
    from pulse_compute_cost.receipt_sources import read_pi
    path, _, _ = saved
    before = query(ledger, watermark=0)
    if not native_first:
        ledger.ingest_receipts(read_pi(path, OBSERVED))
    result = consume(ledger, saved)
    if native_first:
        # Use the real collector's correction chain, rather than mint a new head.
        from test_stage_usage_reconciliation import collect
        collect(ledger, path, [MODEL, ASSISTANT, SUMMARY])
    report = query(ledger, work_items=(WI,))
    assert report['summary']['usage']['known_total_tokens'] == 70
    assert report['summary']['usage']['event_count'] == 2
    assert result['aggregate_usage_added'] is False
    assert result['execution_authority'] is False
    assert result['scl_assistant_messages'] == 1
    assert result['request_count'] == 2
    active, conflicts = current_receipts(ledger.receipt_snapshot()['facts'])
    assert not conflicts
    assert len([r for r in active if r.kind == 'usage']) == 2
    assert query(ledger, watermark=before['watermark']) == before
    from pulse_compute_cost.ledger import UsageLedger
    reopened = UsageLedger(ledger.root)
    watermark = reopened.receipt_snapshot()['watermark']
    assert consume(reopened, saved)['inserted'] == 0
    assert reopened.receipt_snapshot()['watermark'] == watermark
    persisted = [p for p in (ledger.root / 'receipts').iterdir() if p.is_file()]
    assert persisted
    assert b'"type":"message"' not in b''.join(p.read_bytes() for p in persisted)


@pytest.mark.parametrize('change', ['bytes', 'session', 'range', 'missing', 'wi', 'count', 'status'])
def test_invalid_saved_source_fails_without_writing(ledger, saved, change):
    path, raw, original = saved
    doc = deepcopy(original)
    record = doc['result']['items']['usage'][0]
    if change == 'bytes':
        raw = raw.replace(b'a1', b'a2')
    elif change == 'session':
        record['session'] = 'measurement-session.' + '0' * 64
        doc['result']['items']['intervals'][0]['session'] = record['session']
    elif change == 'range':
        record['end_byte'] += 1
    elif change == 'missing':
        record['record_id'] = 'op_other'
    elif change == 'wi':
        doc['work_item']['id'] = 'wi_20261008_551'
    elif change == 'count':
        record['groups'][0]['tokens']['output']['value'] = -1
    else:
        doc['status'] = 'unknown'
    with pytest.raises(ReceiptError):
        consume(ledger, (path, raw, doc))
    assert ledger.receipt_snapshot()['watermark'] == 0


def test_no_interval_spill_into_other_calls(ledger, saved):
    from pulse_compute_cost.receipt_sources import read_pi
    path, _, _ = saved
    extra = deepcopy(ASSISTANT)
    extra['id'] = 'unrelated'
    ledger.ingest_receipts(read_pi(path, OBSERVED, data=encoded([HEADER, extra])))
    consume(ledger, saved)
    assert query(ledger)['summary']['usage']['known_total_tokens'] == 105
    assert query(ledger, work_items=(WI,))['summary']['usage']['known_total_tokens'] == 70


def test_adapter_reads_exact_artifact_and_checks_it_before_save(ledger, saved):
    path, raw, doc = saved
    calls = []
    def inspect(wi, view, artifact_ref=None):
        calls.append((wi, view, artifact_ref))
        if view == 'measurements':
            return deepcopy(doc)
        ref = doc['result']['items']['usage'][0]['evidence']
        return {'status': 'read_only', 'error': None, 'work_item': doc['work_item'],
                'result': {'artifact': {'reference': ref, 'base64': base64.b64encode(raw).decode()}}}
    host = SimpleNamespace(session_file=path, native_header=HEADER,
        base=SimpleNamespace(principal=lambda: PRINCIPAL),
        profile={'role': 'producer'}, execution={'mode': 'automatic'})
    adapter = NativeStageHost(host, native_capture, inspect=inspect)
    adapter.ledger = lambda: ledger
    result = adapter.import_usage(WI, RECORD)
    assert result['status'] == 'saved'
    assert [c[1] for c in calls] == ['measurements', 'artifact']
    assert calls[1][2] == doc['result']['items']['usage'][0]['evidence']
    assert query(ledger, work_items=(WI,))['summary']['usage']['known_total_tokens'] == 70
    host.execution['mode'] = 'audit'
    with pytest.raises(ReceiptError, match='unavailable'):
        adapter.import_usage(WI, RECORD)
    assert len(calls) == 2


@pytest.mark.parametrize('change', ['reference', 'bytes', 'denied', 'other_wi'])
def test_adapter_rejects_unreadable_or_changed_original(ledger, saved, change):
    path, raw, doc = saved
    ref = doc['result']['items']['usage'][0]['evidence']
    original = {'status': 'read_only', 'error': None, 'work_item': deepcopy(doc['work_item']),
                'result': {'artifact': {'reference': deepcopy(ref), 'base64': base64.b64encode(raw).decode()}}}
    if change == 'reference':
        original['result']['artifact']['reference']['sha256'] = '0' * 64
    elif change == 'bytes':
        original['result']['artifact']['base64'] = base64.b64encode(raw + b'\n').decode()
    elif change == 'denied':
        original['status'] = 'rejected'
    else:
        original['work_item']['id'] = 'wi_20261008_551'
    host = SimpleNamespace(session_file=path, native_header=HEADER,
        base=SimpleNamespace(principal=lambda: PRINCIPAL),
        profile={'role': 'producer'}, execution={'mode': 'automatic'})
    adapter = NativeStageHost(host, native_capture,
        inspect=lambda wi, view, artifact_ref=None: deepcopy(doc if view == 'measurements' else original))
    adapter.ledger = lambda: ledger
    with pytest.raises(ReceiptError):
        adapter.import_usage(WI, RECORD)
    assert ledger.receipt_snapshot()['watermark'] == 0


def test_competing_attribution_remains_visible_not_last_import_wins(ledger, saved):
    consume(ledger, saved)
    path, raw, doc = saved
    other = deepcopy(doc)
    other_wi = 'wi_20261008_551'
    other['work_item']['id'] = other_wi
    import_saved_usage(ledger, other, other_wi, RECORD, raw, path=path,
        header=HEADER, principal=PRINCIPAL, observed_at=OBSERVED)
    assert query(ledger)['attribution_conflicts']
    assert query(ledger)['summary']['usage']['known_total_tokens'] == 70


def test_binding_failure_precedes_any_saved_original_read(saved):
    path, _, _ = saved
    def forbidden(*args):
        raise AssertionError('private original must not be read without destination admission')
    host = SimpleNamespace(profile={'role': 'producer'}, execution={'mode': 'automatic'})
    adapter = NativeStageHost(host, native_capture, inspect=forbidden)
    def unbound():
        raise ReceiptError('native stage ledger is not safely registered by the Host')
    adapter.ledger = unbound
    with pytest.raises(ReceiptError, match='registered'):
        adapter.import_usage(WI, RECORD)


@pytest.mark.parametrize('change', [None, 'record', 'session', 'actor', 'interval', 'digest',
                                  'header', 'missing', 'unavailable', 'duplicate'])
def test_successor_import_uses_original_kernel_proof_not_current_session(ledger, tmp_path, change):
    from dataclasses import replace
    from harness_rewrite import Query
    from tests_rewrite.test_workflow import BASE
    from tests_rewrite.test_saved_usage_source import captured
    rig, path, result, item = captured(tmp_path, messages=[ASSISTANT, SUMMARY],
                                       production=True, bounded=True)
    from pulse_compute_cost.receipt_sources import read_pi
    full_observation = read_pi(path, OBSERVED)
    path.unlink()  # Neither native process nor old session file is needed.
    successor = replace(BASE, session='successor-session')
    rig.contexts['successor'] = successor
    reads = []
    def inspect(wi, view, artifact_ref=None):
        reads.append(view)
        response = rig.state(query=Query(wi, view=view, artifact_ref=artifact_ref), handle='successor')
        if view == 'artifact' and change:
            proofs = response['result']['artifact']['native_usage_sources']
            proof = proofs[0]
            fields = {'record': 'record_id', 'session': 'session', 'actor': 'actor',
                      'interval': 'interval_id', 'digest': 'source_sha256'}
            if change in fields:
                proof[fields[change]] = 'different'
            elif change == 'header':
                proof['native_header'] = {'type': 'not-session', 'id': 'not-original'}
            elif change == 'missing':
                del response['result']['artifact']['native_usage_sources']
            elif change == 'unavailable':
                proofs[0] = {'schema': proof['schema'], 'status': 'unavailable',
                             'record_id': proof['record_id'], 'reason': 'original_interval_header_unavailable'}
            else:
                proofs.append(deepcopy(proof))
        return response
    host = SimpleNamespace(session_file=tmp_path / 'successor.jsonl',
        native_header={'type': 'session', 'id': 'successor'},
        base=SimpleNamespace(principal=successor.principal),
        profile={'role': 'producer'}, execution={'mode': 'automatic'})
    adapter = NativeStageHost(host, native_capture, inspect=inspect)
    adapter.ledger = lambda: ledger
    if change:
        with pytest.raises(ReceiptError):
            adapter.import_usage(rig.wi, result['record_id'])
        assert ledger.receipt_snapshot()['watermark'] == 0
        return
    before = query(ledger, watermark=0)
    imported = adapter.import_usage(rig.wi, result['record_id'])
    assert reads == ['measurements', 'artifact']
    assert imported['request_count'] == 2 and imported['aggregate_usage_added'] is False
    assert ledger.ingest_receipts(full_observation)['inserted'] == 0
    assert query(ledger, work_items=(rig.wi,))['summary']['usage']['known_total_tokens'] == 70
    from pulse_compute_cost.ledger import UsageLedger
    reopened = UsageLedger(ledger.root)
    adapter.ledger = lambda: reopened
    assert adapter.import_usage(rig.wi, result['record_id'])['inserted'] == 0
    assert query(reopened, watermark=0) == before
    assert b'private-provider-native-id' not in b''.join(
        p.read_bytes() for p in (ledger.root / 'receipts').iterdir() if p.is_file())


def test_cli_has_no_store_or_identity_override(monkeypatch, capsys):
    from pulse_compute_cost import cli, native_stage_host
    calls = []
    adapter = SimpleNamespace(import_usage=lambda wi, record_id: calls.append((wi, record_id)) or {'status': 'saved'})
    monkeypatch.setattr(native_stage_host, 'installed_host', lambda: adapter)
    assert cli.main(['import-scl-usage', '--work-item', WI, '--record-id', RECORD]) == 0
    assert calls == [(WI, RECORD)]
    with pytest.raises(SystemExit):
        cli.main(['import-scl-usage', '--work-item', WI, '--record-id', RECORD, '--ledger-root', '/other'])
    assert len(calls) == 1
    capsys.readouterr()
