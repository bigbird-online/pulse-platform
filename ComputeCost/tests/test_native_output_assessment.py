"""Isolated native decisions -> exact versions -> the existing ledger/query.

No production WI, human approval, installation, or natural UAT is performed.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from harness_rewrite import Command, Kernel, Query
from harness_rewrite.domain import INTENTS
from tests_rewrite.test_workflow import Rig, BASE, original, graph, node, edge
from tests_rewrite.test_production_ledger import Rig as GitRig
from tests_rewrite.test_review import VERIFIER, ARTIFACT, produce, assign, verify, approve
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import ReceiptError, reference
from pulse_compute_cost.native_stage_host import NativeStageHost
from test_stage_facts import contribution, link, query, WI, CONTEXT

AT = '2026-10-09T00:00:00Z'
OBSERVED = '2026-10-10T00:00:00Z'


class NativeRig(Rig):
    def adopt(self, value, *, text='Synthetic plan'):
        original(self.approve('adopt_plan', {'graph': value, 'plan_text': text, 'resources': ['repo']}))
        original(self.send('start_work', {'graph_revision': 1}))


@pytest.fixture
def native(tmp_path):
    root = tmp_path / 'isolated-git'
    root.mkdir()
    git = GitRig(root)
    rig = NativeRig.__new__(NativeRig)
    rig.contexts = {'owner': replace(BASE, allowed_intents=INTENTS),
                    'verifier': replace(VERIFIER, allowed_intents=INTENTS)}
    rig.kernel = Kernel(root / 'must-not-exist.sqlite', rig.contexts.__getitem__,
                        storage=git.ledger, clock=lambda: AT)
    rig.wi, rig.counter = WI, 0
    original(rig.approve('record_affiliation', {'type': 'operating_area', 'ref': 'OA-PULSE',
        'work_class': 'improvement', 'reason': 'Isolated synthetic measurement'},
        actor=rig.contexts['owner']))
    # The imported fixture has two criteria; its true final verdict covers both.
    return rig


def finish(rig, *, intermediate=False):
    if intermediate:
        nodes = [node('work', outputs=['result']),
                 node('verify', kind='verification', role='verifier', outcomes=['accepted'], success=['accepted']),
                 node('review', kind='review', role='human', outcomes=['approve'], success=['approve']),
                 node('later')]
        rig.adopt(graph(rig, nodes, [edge('work', 'verify'), edge('verify', 'review', 'accepted'),
                                   edge('review', 'later', 'approve')]))
        attempt = original(rig.claim('work'))['attempt_id']
        original(rig.result(attempt, artifacts={'result': ARTIFACT}))
        candidate = rig.state()['result']['current_observation']['candidates']['active']
    else:
        candidate = produce(rig)
    assignment = assign(rig, candidate)
    # Use the real independently authenticated receipt path, including AC-2 for final scope.
    checked = verify(rig, candidate, assignment)
    if intermediate:
        original(checked)
    else:
        assert checked['error']['code'] == 'domain_conflict'  # final requires AC-2 too
        from harness_rewrite.model import SourceInput, digest
        from tests_rewrite.test_review import PROOF
        data = {'candidate_id': candidate, 'assignment_id': assignment, 'verdict': 'accepted',
            'checks': [{'criterion': c, 'verdict': 'passed', 'evidence': [PROOF.reference()]}
                       for c in ('AC-1', 'AC-2')], 'reason': 'Exact synthetic final checks'}
        rig.contexts['receipt'] = replace(rig.contexts['receipt'], source=SourceInput(
            'all-checks', 'independent_result', b'Synthetic final checks', 'record_verification',
            digest({'intent': 'record_verification', 'work_item_id': WI, 'input': data})))
        original(rig.send('record_verification', data, handle='receipt'))
    original(approve(rig, candidate))
    return candidate


def measurement(rig):
    return rig.state(query=Query(WI, view='measurements'))


def store(tmp_path):
    return UsageLedger(tmp_path / 'cost', clock=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))


def consume(ledger, response, candidate):
    from pulse_compute_cost.native_output_assessment import import_assessments
    return import_assessments(ledger, response, WI, candidate, observed_at=OBSERVED)


@pytest.mark.parametrize('intermediate', [False, True])
def test_native_exact_version_scope_time_and_replay(native, tmp_path, intermediate):
    from pulse_compute_cost.native_output_assessment import artifact_version_ref
    candidate = finish(native, intermediate=intermediate)
    response = measurement(native)
    assert response['result']['outcome_record_times']
    ledger = store(tmp_path)
    output = contribution(version=artifact_version_ref(ARTIFACT.reference()))
    other = contribution('later-version', version=reference('version', 'not-approved'))
    ledger.ingest_receipts([output, other, link([output, other])])
    old = query(ledger, watermark=ledger.receipt_snapshot()['watermark'])
    result = consume(ledger, response, candidate)
    assert result['inserted'] == 2 and result['execution_authority'] is False
    saved = query(ledger, work_items=(WI,), context_ref=CONTEXT)
    assessments = saved['stages']['assessments']
    assert len(assessments) == 2
    assert {r['assessment'] for r in assessments} == {'verification', 'human_review'}
    assert all(r['scope'] == ('intermediate' if intermediate else 'final') for r in assessments)
    assert all(r['output_fact_refs'] == [output.fact_ref] for r in assessments)
    assert all(r['occurred_at'] == AT for r in assessments)
    assert saved['stages']['outputs']['cumulative'] == old['stages']['outputs']['cumulative']
    accepted = saved['stages']['outputs']['accepted']
    assert accepted['status'] == ('unavailable' if intermediate else 'recorded_subset')
    if not intermediate:
        assert accepted['object_count'] == 1
        assert accepted['items'][0]['output_fact_ref'] == output.fact_ref
        from pulse_stats.stage_view import validate_accepted
        validate_accepted(accepted, saved['stages']['outputs']['items'], OBSERVED)
    assert query(ledger, watermark=old['watermark']) == old
    from pulse_compute_cost.native_output_assessment import import_assessments
    assert import_assessments(UsageLedger(ledger.root), measurement(native), WI, candidate,
                              observed_at='2026-10-12T00:00:00Z')['inserted'] == 0
    raw = b''.join(p.read_bytes() for p in (ledger.root / 'receipts').iterdir() if p.is_file())
    assert b'private-verifier' not in raw and b'original independent' not in raw.lower()
    assert b'repo/deliverable' not in raw
    assert query(ledger, scope='activity', from_date='2026-10-01', to_date='2026-10-09')['stages']['assessments'] == []
    assert query(ledger, work_items=('wi_20261008_551',))['stages']['assessments'] == []
    assert query(ledger, runtime='pi')['stages']['assessments'] == []


@pytest.mark.parametrize('change', ['missing_times', 'duplicate_time', 'future', 'wrong_event',
    'candidate_digest', 'unknown_candidate', 'wrong_wi', 'artifact_digest', 'control_source', 'write_response'])
def test_reject_inconsistent_native_observations_without_any_save(native, tmp_path, change):
    candidate = finish(native)
    response = deepcopy(measurement(native))
    target = response['result']['current_observation']['candidates']['items'][0]
    if change == 'missing_times':
        response['result'].pop('outcome_record_times', None)
    elif change == 'duplicate_time':
        response['result']['outcome_record_times'] *= 2
    elif change == 'future':
        response['result']['outcome_record_times'][0]['recorded_at'] = '2030-01-01T00:00:00Z'
    elif change == 'wrong_event':
        response['result']['outcome_record_times'][0]['candidate_id'] = 'candidate_other'
    elif change == 'candidate_digest':
        target['content_digest'] = '0' * 64
    elif change == 'unknown_candidate':
        candidate = 'candidate_absent'
    elif change == 'wrong_wi':
        response['work_item']['id'] = 'wi_20261008_551'
    elif change == 'artifact_digest':
        target['content']['artifacts'][0]['sha256'] = '0' * 64
    elif change == 'control_source':
        target['review']['source_digest'] = 'bad'
    else:
        response['status'] = 'local_committed'
    ledger = store(tmp_path)
    with pytest.raises(ReceiptError):
        consume(ledger, response, candidate)
    assert ledger.receipt_snapshot()['watermark'] == 0


def test_pending_and_stage_declarations_cannot_mint_review(native, tmp_path):
    candidate = produce(native)
    ledger = store(tmp_path)
    assert consume(ledger, measurement(native), candidate)['inserted'] == 0
    assert query(ledger)['stages']['assessments'] == []


@pytest.mark.parametrize('score', [None, 0, 0.75])
def test_native_quality_keeps_zero_unknown_and_original_rubric(native, tmp_path, score):
    from harness_rewrite.model import SourceInput, digest
    from tests_rewrite.test_review import PROOF
    from pulse_compute_cost.native_output_assessment import artifact_version_ref
    candidate = finish(native)
    ledger = store(tmp_path)
    output = contribution(version=artifact_version_ref(ARTIFACT.reference()))
    ledger.ingest_receipts([output, link([output])])
    consume(ledger, measurement(native), candidate)
    data = {'candidate_id': candidate, 'rubric': 'synthetic-rubric', 'version': 'v1',
            'score': score, 'evidence': [PROOF.reference()]}
    native.contexts['quality'] = replace(VERIFIER, allowed_intents=INTENTS, artifacts=(PROOF,),
        source=SourceInput('quality-source', 'independent_result', b'Synthetic quality observation',
            'record_quality', digest({'intent': 'record_quality', 'work_item_id': WI, 'input': data})))
    original(native.send('record_quality', data, handle='quality'))
    assert consume(ledger, measurement(native), candidate)['inserted'] == 1
    assessments = query(ledger)['stages']['assessments']
    quality, = [a for a in assessments if a['assessment'] == 'quality']
    assert quality['score'] == (None if score is None else str(score))
    assert quality['result'] == ('unavailable' if score is None else 'observed')
    assert quality['rubric_ref'] is not None
    assert all(a['score'] is None for a in assessments if a['assessment'] != 'quality')


def test_native_waiver_is_not_independent_verification_and_revoke_remains_visible(native, tmp_path):
    from pulse_compute_cost.native_output_assessment import artifact_version_ref
    candidate = produce(native)
    data = {'candidate_id': candidate, 'action': 'waive', 'reason': 'Synthetic explicit waiver',
            'risk': 'No independent result', 'observed_verdict': None}
    original(native.approve('waive_verification', data, actor=native.contexts['owner']))
    ledger = store(tmp_path)
    output = contribution(version=artifact_version_ref(ARTIFACT.reference()))
    ledger.ingest_receipts([output, link([output])])
    consume(ledger, measurement(native), candidate)
    before = query(ledger, watermark=ledger.receipt_snapshot()['watermark'])
    original(native.approve('waive_verification', {**data, 'action': 'revoke'}, actor=native.contexts['owner']))
    consume(ledger, measurement(native), candidate)
    assessments = query(ledger)['stages']['assessments']
    assert {a['result'] for a in assessments} == {'waive', 'revoke'}
    assert {a['assessment'] for a in assessments} == {'waiver'}
    assert query(ledger, watermark=before['watermark']) == before


@pytest.mark.parametrize('mode', ['read', 'audit', 'verifier', 'destination_changed'])
def test_host_only_uses_bound_native_query(native, tmp_path, mode):
    candidate = finish(native)
    ledger = store(tmp_path)
    calls = []
    def inspect(wi, view):
        calls.append((wi, view))
        return measurement(native)
    host = NativeStageHost(SimpleNamespace(profile={'role': 'verifier' if mode == 'verifier' else 'producer'},
        execution={'mode': 'audit' if mode == 'audit' else 'automatic'}), None, inspect=inspect)
    host._guard = lambda: None
    destinations = iter([ledger, UsageLedger(tmp_path / 'changed')] if mode == 'destination_changed' else [ledger, ledger])
    host.ledger = lambda: next(destinations)
    if mode == 'read':
        assert host.import_assessments(WI, candidate)['inserted'] == 2
    else:
        with pytest.raises(ReceiptError):
            host.import_assessments(WI, candidate)
        assert ledger.receipt_snapshot()['watermark'] == 0
    assert calls == ([(WI, 'measurements')] if mode in {'read', 'destination_changed'} else [])


def test_host_checks_destination_before_read_and_cli_has_no_overrides(tmp_path):
    from pulse_compute_cost.cli import _parser
    calls = []
    host = NativeStageHost(SimpleNamespace(profile={'role': 'producer'}, execution={'mode': 'automatic'}),
                           None, inspect=lambda *args: calls.append(args))
    def missing():
        raise ReceiptError('destination missing')
    host.ledger = missing
    with pytest.raises(ReceiptError, match='destination missing'):
        host.import_assessments(WI, 'candidate_test')
    assert not calls
    parser = _parser()
    args = parser.parse_args(['import-scl-assessments', '--work-item', WI, '--candidate-id', 'candidate_test'])
    assert args.candidate_id == 'candidate_test'
    with pytest.raises(SystemExit):
        parser.parse_args(['import-scl-assessments', '--work-item', WI, '--candidate-id', 'candidate_test',
                          '--ledger-root', str(tmp_path)])
