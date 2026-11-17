"""Observe exact native candidate decisions; never issue an acceptance.

The production caller is NativeStageHost's installed read boundary. JSON shape
validation alone is not authentication. Only private references cross into the
existing receipt ledger; no original prose, paths or native identities are saved.
"""
from __future__ import annotations

import re

from .events import parse_timestamp
from .harness_query_report import report, checksum, identifier, integer, object_fields, require, sequence
from .receipts import Receipt, ReceiptError, WI, digest, reference

POLICY = 'scl_output_assessment.v1'
RESULTS = {'verification': {'accepted', 'partial', 'blocked', 'rejected'},
           'human_review': {'approve', 'changes_requested'},
           'quality': {'observed', 'unavailable'}, 'waiver': {'waive', 'revoke'}}


def validate_selection(wi, candidate):
    if (not isinstance(wi, str) or not WI.fullmatch(wi) or not isinstance(candidate, str)
            or not re.fullmatch(r'candidate_[A-Za-z0-9_]+', candidate)):
        raise ReceiptError('assessment import requires exact WI and candidate identities')


def artifact_version_ref(value):
    ref = object_fields(value, {'object', 'version', 'sha256', 'size', 'access'})
    require(all(isinstance(ref[k], str) and ref[k] for k in ('object', 'version')), 'Invalid fixed artifact')
    require(ref['access'] in {'private', 'team', 'public'}, 'Invalid fixed artifact access')
    checksum(ref['sha256'])
    integer(ref['size'])
    return reference('scl_artifact.v1', ref)


def artifact_set_version_ref(values):
    refs = sorted(artifact_version_ref(v) for v in sequence(values))
    require(bool(refs) and len(refs) == len(set(refs)), 'Invalid fixed artifact set')
    return reference('scl_artifact_set.v1', refs)


def validate_payload(p):
    from .receipts import _ref, decimal
    if (p['wi'] is None or p['scope'] not in {'final', 'intermediate', 'unknown'}
            or type(p['native_revision']) is not int or p['native_revision'] < 1):
        raise ReceiptError('invalid native assessment scope')
    for name in ('candidate_ref', 'source_ref'):
        _ref(p[name], name, False)
    for name in ('version_refs', 'gate_refs', 'criterion_refs'):
        values = p[name]
        if not isinstance(values, list) or len(values) > 4096 or len(set(values)) != len(values):
            raise ReceiptError('invalid native assessment references')
        for value in values:
            _ref(value, name, False)
    if not p['version_refs'] or not p['criterion_refs']:
        raise ReceiptError('native assessment requires its frozen target')
    kind = p['assessment']
    if not isinstance(kind, str) or kind not in RESULTS or p['result'] not in RESULTS[kind]:
        raise ReceiptError('invalid native assessment result')
    if kind == 'quality':
        _ref(p['rubric_ref'], 'rubric_ref', False)
        if (p['score'] is None) != (p['result'] == 'unavailable'):
            raise ReceiptError('native quality coverage differs')
        if p['score'] is not None and decimal(p['score'], 'score', nonnegative=True) > 1:
            raise ReceiptError('native quality score exceeds one')
    elif p['rubric_ref'] is not None or p['score'] is not None:
        raise ReceiptError('decision cannot invent a quality score')
    if not isinstance(p['checks'], list) or (kind != 'verification' and p['checks']):
        raise ReceiptError('invalid native assessment checks')
    seen = set()
    for row in p['checks']:
        if (not isinstance(row, dict) or set(row) != {'criterion_ref', 'result', 'evidence_refs'}
                or row['criterion_ref'] not in p['criterion_refs'] or row['criterion_ref'] in seen
                or row['result'] not in {'passed', 'failed', 'unknown'}):
            raise ReceiptError('invalid native criterion check')
        seen.add(row['criterion_ref'])
        if not isinstance(row['evidence_refs'], list) or not row['evidence_refs']:
            raise ReceiptError('native criterion lacks evidence')
        for ref in row['evidence_refs']:
            _ref(ref, 'evidence_refs', False)
    if kind == 'verification' and p['result'] == 'accepted' and (
            seen != set(p['criterion_refs']) or any(c['result'] != 'passed' for c in p['checks'])):
        raise ReceiptError('accepted native verification lacks complete checks')


def import_assessments(ledger, query, wi, candidate_id, *, observed_at):
    """All-or-nothing append of existing native observations, not declarations."""
    validate_selection(wi, candidate_id)
    try:
        report(query, wi, digest(query))
        result = query['result']
        data = result['items']
        candidate, = [c for c in result['current_observation']['candidates']['items'] if c['id'] == candidate_id]
        outcome, = [c for c in data['outcomes'] if c['candidate_id'] == candidate_id]
        content = candidate['content']
        require(checksum(candidate['content_digest']) == digest(content), 'Candidate content digest differs')
        for name in ('verification', 'review', 'waiver'):
            require(candidate[name] == outcome[name], 'Candidate control observations disagree')
        for name in ('result_ids', 'definition_revision', 'graph_revision'):
            require(content[name] == outcome[name], 'Candidate frozen scope differs')
        versions = sorted({artifact_version_ref(v) for v in sequence(content['artifacts'])}
                          | {artifact_set_version_ref(content['artifacts'])})
        terminal = content.get('terminal')
        require(terminal is None or type(terminal) is bool, 'Invalid native terminal scope')
        scope = 'unknown' if terminal is None else 'final' if terminal else 'intermediate'
        gates = content.get('gates', {})
        require(isinstance(gates, dict) and set(gates) <= {'verification', 'review', 'delivery'}, 'Invalid candidate gates')
        gate_refs = sorted(reference('scl_gate', [wi, content['graph_revision'], k, identifier(v)])
                           for k, v in gates.items())
        criteria = content.get('acceptance_refs')
        require(isinstance(criteria, list) and bool(criteria) and len(set(criteria)) == len(criteria), 'Missing candidate criteria')
        criterion = {identifier(c): reference('scl_criterion', [wi, content['definition_revision'], c]) for c in criteria}
        times = {}
        for raw in sequence(result['outcome_record_times']):
            row = object_fields(raw, {'candidate_id', 'kind', 'record_id', 'recorded_at', 'revision'})
            key = (identifier(row['candidate_id']), row['kind'], identifier(row['record_id']))
            require(key not in times, 'Duplicate native decision time')
            require(parse_timestamp(row['recorded_at'], 'decision time') <= parse_timestamp(observed_at, 'observation time'),
                    'Native decision is from the future')
            require(integer(row['revision'], 1) <= query['work_item']['local_revision'], 'Future native revision')
            times[key] = row
        observations = [(k, candidate[k]) for k in ('verification', 'review', 'waiver') if candidate[k] is not None]
        observations += [('quality', q) for q in data['quality'] if q['candidate_id'] == candidate_id]
        pending = []
        for name, raw in observations:
            record_id = identifier(raw['record_id'] if name == 'quality' else raw['id'])
            require(raw['candidate_id'] == candidate_id, 'Decision belongs to another candidate')
            timing = times[(candidate_id, name, record_id)]
            at = timing['recorded_at']
            source = checksum(raw['source_sha256'] if name == 'quality' else raw['source_digest'])
            checks = []
            if name == 'verification':
                require(checksum(raw['digest']) == digest({k: v for k, v in raw.items() if k not in {'id', 'digest'}}),
                        'Verification digest differs')
                assignment = candidate['assignment']
                require(assignment is not None and assignment['acknowledged'] is True
                        and assignment['id'] == raw['assignment_id'], 'Verifier assignment differs')
                checks = [{'criterion_ref': criterion[c['criterion']], 'result': c['verdict'],
                           'evidence_refs': [artifact_version_ref(e) for e in sequence(c['evidence'])]}
                          for c in sequence(raw['checks'])]
            if name == 'quality':
                assignment = candidate['assignment']
                require(assignment is not None and assignment['acknowledged'] is True
                        and assignment['id'] == raw['assignment_id'] and assignment['verifier'] == raw['verifier']
                        and raw['recorded_at'] == at, 'Quality observation provenance differs')
            assessment = 'human_review' if name == 'review' else name
            verdict = raw[{'verification': 'verdict', 'review': 'decision', 'waiver': 'action', 'quality': 'coverage'}[name]]
            source_ref = reference('scl_decision_source', source)
            payload = {'wi': wi, 'candidate_ref': reference('scl_candidate', [wi, candidate_id, candidate['content_digest']]),
                'version_refs': versions, 'scope': scope, 'gate_refs': gate_refs,
                'criterion_refs': sorted(criterion.values()), 'assessment': assessment, 'result': verdict,
                'rubric_ref': reference('scl_rubric', [raw['rubric'], raw['version']]) if name == 'quality' else None,
                'score': str(raw['score']) if name == 'quality' and raw['score'] is not None else None,
                'checks': checks, 'source_ref': source_ref, 'native_revision': timing['revision']}
            evidence = [source_ref, reference('scl_candidate_content', candidate['content_digest'])]
            if name == 'quality':
                evidence.extend(artifact_version_ref(e) for e in raw['evidence'])
            pending.append(Receipt.make('output_assessment', [POLICY, wi, record_id], payload,
                occurred_at=at, observed_at=observed_at, producer=POLICY, evidence=tuple(evidence)))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ReceiptError('native candidate assessment evidence is unavailable or inconsistent') from exc
    snapshot = ledger.receipt_snapshot()
    # Native decisions are immutable. A repeat has exactly the same fact identity;
    # disagreement is rejected rather than manufacturing a correction/acceptance.
    for row in pending:
        prior = [r for r in snapshot['facts'] if r.entity_ref == row.entity_ref]
        if any(r.fact_ref != row.fact_ref for r in prior):
            raise ReceiptError('native decision differs from its saved observation')
    saved = ledger.ingest_receipts(pending, expected_watermark=snapshot['watermark'])
    return {'schema': 'computecost_native_assessment_import/v1', **saved,
            'work_item_id': wi, 'candidate_ref': reference('scl_candidate', [wi, candidate_id, candidate['content_digest']]),
            'artifact_version_refs': sorted(artifact_version_ref(v) for v in content['artifacts']),
            'artifact_set_version_ref': artifact_set_version_ref(content['artifacts']),
            'source_revision': query['work_item']['local_revision'], 'execution_authority': False,
            'consumer_state': 'saved_not_display_verified'}


def project_assessments(rows, outputs, *, window=None):
    """Exact versions within the selected stage scope; no same-WI expansion."""
    items = []
    for row in rows:
        if row.kind != 'output_assessment' or (window and (row.occurred_at is None
                or parse_timestamp(row.occurred_at, 'assessment time') >= window[1])):
            continue
        p = row.payload
        matched = sorted(r.fact_ref for r in outputs if r.payload['version_ref'] in p['version_refs']
                         and r.payload.get('wi') == p['wi'])
        if matched:
            items.append({**p, 'fact_ref': row.fact_ref, 'occurred_at': row.occurred_at,
                'observed_at': row.observed_at, 'evidence': list(row.evidence),
                'output_fact_refs': matched, 'in_period': window is None or (
                    row.occurred_at is not None and window[0] <= parse_timestamp(row.occurred_at, 'assessment time'))})
    return sorted(items, key=lambda x: (x['occurred_at'] or '', x['wi'], x['native_revision'], x['fact_ref']))


def accepted_versions(assessments, outputs):
    """Historical exact-version final acceptance, not present execution authority.

    Intermediate checks and Agent declarations cannot satisfy this join. Keep
    each candidate's independent verification/waiver and human review together;
    never combine two different frozen candidates into an invented approval.
    """
    from .stage_facts import _output_groups
    groups = {}
    conflicts = False
    for row in assessments:
        if row['scope'] != 'final' or row['assessment'] == 'quality':
            continue
        key = row['candidate_ref'], row['assessment']
        prior = groups.get(key)
        if prior is None or row['native_revision'] > prior['native_revision']:
            groups[key] = row
        elif row['native_revision'] == prior['native_revision'] and row['fact_ref'] != prior['fact_ref']:
            conflicts = True
    accepted = {}
    for (candidate, kind), human in groups.items():
        if kind != 'human_review' or human['result'] != 'approve':
            continue
        verified = groups.get((candidate, 'verification'))
        waived = groups.get((candidate, 'waiver'))
        verification = verified if verified and verified['result'] == 'accepted' else None
        waiver = waived if waived and waived['result'] == 'waive' else None
        if not verification and not waiver:
            continue
        proof = verification or waiver
        refs = set(human['output_fact_refs']) & set(proof['output_fact_refs'])
        for output in outputs:
            if output.fact_ref not in refs:
                continue
            # One object/version can be approved in multiple candidates. Keep
            # its earliest complete witness, never add it as another output.
            at = max(human['occurred_at'], proof['occurred_at'], key=lambda value: parse_timestamp(value, 'acceptance time'))
            item = {'output_fact_ref': output.fact_ref, 'object_ref': output.payload['object_ref'],
                    'version_ref': output.payload['version_ref'], 'candidate_ref': candidate,
                    'verification_ref': verification['fact_ref'] if verification else None,
                    'waiver_ref': waiver['fact_ref'] if not verification and waiver else None,
                    'human_review_ref': human['fact_ref'], 'accepted_at': at, 'scope': 'final'}
            if output.fact_ref not in accepted or parse_timestamp(at, 'acceptance time') < parse_timestamp(accepted[output.fact_ref]['accepted_at'], 'acceptance time'):
                accepted[output.fact_ref] = item
    if conflicts:
        return {'status': 'unavailable', 'reason': 'native_assessment_conflict'}
    if not accepted:
        return {'status': 'unavailable', 'reason': 'native_exact_version_acceptance_not_joined'}
    selected = [r for r in outputs if r.fact_ref in accepted]
    return {'status': 'recorded_subset', 'reason': None,
            'object_count': len({r.payload['object_ref'] for r in selected}),
            'groups': _output_groups(selected),
            'items': sorted(accepted.values(), key=lambda row: row['output_fact_ref'])}
