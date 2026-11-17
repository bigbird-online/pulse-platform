"""Join one authenticated SCL original to Pi receipts in the existing ledger.

Only the installed Host supplies query/original/session evidence. No aggregate
usage, transcript, grant or acceptance is written. A different current session
needs the core's proof from the original interval-opening Host source.
"""
from hashlib import sha256
from pathlib import Path
import json
import re

from .harness_query_report import report, HarnessQueryError
from .receipt_sources import read_pi, reconcile_native_usage
from .receipts import Receipt, ReceiptError, canonical, digest, reference
from .stage_record import same_session, session_identity, validate_range

POLICY = 'scl_saved_native_usage.v1'


def validate_selection(wi, record_id):
    if (not isinstance(wi, str) or not re.fullmatch(r'wi_[A-Za-z0-9_]+', wi)
            or not isinstance(record_id, str) or not re.fullmatch(r'op_[A-Za-z0-9_]+', record_id)):
        raise ReceiptError('saved native usage requires exact WI and record identities')


def saved_record(query, wi, record_id, principal=None):
    validate_selection(wi, record_id)
    try:
        # Reuse the existing complete measurement-query contract, not a second
        # shape/parser which could accept rejected or contradictory observations.
        report(query, wi, digest(query))
        matches = [r for r in query['result']['items']['usage'] if r['record_id'] == record_id]
        if len(matches) != 1:
            raise ReceiptError('saved native usage record is unavailable')
        row = matches[0]
        if principal is not None:
            _same_principal(row, principal)
        return row
    except (HarnessQueryError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ReceiptError):
            raise
        raise ReceiptError('saved native usage query failed validation') from exc


def _same_principal(row, principal):
    if row['session'] != 'measurement-session.' + digest(principal):
        raise ReceiptError('saved native usage needs its original authenticated session')
    if row['actor'] != principal['actor']:
        raise ReceiptError('saved native usage actor differs from its authenticated session')


def _source_header(row, principal, header, proof):
    if proof is None or proof.get('status') == 'unavailable':
        # Older installed readers can still prove their own current session.
        # No cross-session inference from a WI or matching actor is permitted.
        _same_principal(row, principal)
        return header
    if (proof.get('schema') != 'harness.saved-usage-source/v1' or proof.get('status') != 'verified'
            or any(proof.get(k) != row[k] for k in
                   ('record_id', 'interval_id', 'session', 'actor', 'source_sha256'))
            or not isinstance(proof.get('interval_source_sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', proof['interval_source_sha256'])
            or not isinstance(proof.get('native_header'), dict)):
        raise ReceiptError('saved native usage source proof differs from its record')
    return proof['native_header']


def import_saved_usage(ledger, query, wi, record_id, raw, *, path: Path, header,
                       principal, observed_at, source_proof=None):
    row = saved_record(query, wi, record_id)
    header = _source_header(row, principal, header, source_proof)
    start, end = validate_range({k: row[k] for k in ('start_byte', 'end_byte')})
    if (not isinstance(raw, bytes) or len(raw) != end - start or not raw.endswith(b'\n')
            or sha256(raw).hexdigest() != row['source_sha256']):
        raise ReceiptError('saved native usage original differs from its recorded range')
    try:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if session_identity(header) is None:
            raise ValueError('header')
        if start == 0:
            # Identity is (type, id); the original header's other fields
            # (version, timestamp, cwd) need not be registered by the Host.
            if not rows or not same_session(header, rows[0]):
                raise ValueError('header')
            rows = rows[1:]
        if any(not isinstance(r, dict) or r.get('type') == 'session' for r in rows):
            raise ValueError('range')
        # No prefix read to guess an inherited model. Both original collectors
        # and record-stage use these same native-record and invocation IDs.
        source = canonical(header) + b'\n' + b''.join(canonical(r) + b'\n' for r in rows)
        usage = read_pi(path, observed_at, data=source)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReceiptError('saved native usage original is not a bounded Pi source') from exc
    snapshot = ledger.receipt_snapshot()
    pending = reconcile_native_usage(snapshot['facts'], usage)
    evidence = (reference('scl_usage_record', [wi, record_id, row['source_sha256']]),
                reference('scl_usage_interval', [wi, row['interval_id'], row['session']]))
    attributed = []
    for call in usage:
        # A child tool summary is overlapping evidence, not a proven invocation
        # inside this interval. Keep it for the existing global reconciliation.
        if call.payload['granularity'] != 'request' or call.occurred_at is None:
            continue
        pending.append(Receipt.make('attribution', [POLICY, wi, record_id, call.entity_ref], {
            'target_ref': call.entity_ref, 'wi': wi, 'executor': row['actor'],
            'responsible': None, 'delegator': None, 'state': 'assigned',
            'policy': POLICY, 'effective_at': call.occurred_at,
        }, occurred_at=call.occurred_at, observed_at=observed_at, producer=POLICY,
            evidence=tuple(sorted(set(evidence) | set(call.evidence)))))
        attributed.append(call.entity_ref)
    saved = ledger.ingest_receipts(pending, expected_watermark=snapshot['watermark'])
    return {'schema': 'computecost_scl_usage_import/v1', 'status': 'saved', **saved,
            'work_item_id': wi, 'record_id': record_id,
            'source_revision': query['work_item']['local_revision'],
            'source_sha256': row['source_sha256'], 'request_count': len(attributed),
            'usage_refs': attributed,
            'unattributed_usage_refs': sorted(r.entity_ref for r in usage if r.entity_ref not in attributed),
            'coverage': 'exact_saved_range_only',
            'scl_assistant_messages': sum(g['messages'] for g in row['groups']),
            'aggregate_usage_added': False, 'execution_authority': False,
            'consumer_state': 'saved_not_display_verified'}
