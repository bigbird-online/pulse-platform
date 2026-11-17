"""Accounting evidence from an existing Pi canonical-baseline adoption receipt.

This reader never changes a grant, binding, baseline or usage quantity. The
native receipt must name a snapshot already accepted in the registered SCL
measurement. Merely sharing a WI/Agent is not binding equivalence.
"""
from __future__ import annotations
from collections import defaultdict
import hashlib
import io
import json
import re
from typing import Mapping
from .receipts import Receipt, ReceiptError, reference
from .scl_session_attribution import pi_host_ref

POLICY = 'pi_canonical_binding_alias.v1'
SHA = re.compile(r'[0-9a-f]{64}')
FIELDS = {'schema', 'work_item_id', 'epoch', 'lifecycle_state', 'binding_sha256',
          'active_runtime_binding_sha256', 'snapshot_sha256', 'artifact_ref'}


def binding_alias_receipts(data: bytes, measurements: Mapping[tuple, dict], observed: str, *, path=None, verified_boundaries=()) -> list[Receipt]:
    stream = io.BytesIO(data)
    header = json.loads(stream.readline())
    entries = (json.loads(line) for line in stream if b'pulse-computecost-usage' in line)
    if header.get('type') != 'session':
        raise ReceiptError('native binding alias has no session header')
    host = pi_host_ref(header['id'])
    result = []
    for entry in entries:
        if entry.get('type') != 'custom' or entry.get('customType') != 'pulse-computecost-usage':
            continue
        claim = entry.get('data')
        if not isinstance(claim, dict) or 'active_runtime_binding_sha256' not in claim:
            continue
        if (set(claim) != FIELDS or claim.get('schema') != 'pi_host_usage_capture_receipt/v1'
                or claim.get('lifecycle_state') not in {'open', 'sealed'}
                or re.fullmatch(r'wi_\d{8}_\d{3}', str(claim.get('work_item_id'))) is None
                or type(claim.get('epoch')) is not int or claim['epoch'] < 1
                or any(SHA.fullmatch(str(claim.get(k))) is None for k in
                       ('binding_sha256', 'active_runtime_binding_sha256', 'snapshot_sha256'))):
            raise ReceiptError('invalid canonical binding adoption receipt')
        expected = f"{claim['work_item_id']}-{claim['binding_sha256']}-{claim['lifecycle_state']}.json"
        if claim['artifact_ref'] != expected:
            raise ReceiptError('canonical binding adoption artifact differs')
        matches = [(key, value) for key, value in measurements.items()
                   if key[0] == claim['work_item_id'] and key[2] == claim['epoch']
                   and key[4] == 'pi' and key[5] == host
                   and value.get('native_binding_snapshots', {}).get(claim['snapshot_sha256'])
                   == claim['binding_sha256']]
        if path is not None:
            retained = retained_binding_witness(path, data, claim, verified_boundaries)
            if retained is None:
                continue  # Even an accepted old snapshot cannot invent a new baseline.
            if any(key != retained[0] for key, _ in matches):
                raise ReceiptError('live and retained binding adoption scopes differ')
            if len(matches) <= 1:
                matches = [retained]
        if len(matches) != 1:
            continue  # Missing or contradictory evidence is not an alias proof.
        key, witness = matches[0]
        if not witness.get('not_before') or not witness.get('evidence_ref'):
            continue
        original = reference('assignment', claim['binding_sha256'])
        active = reference('assignment', claim['active_runtime_binding_sha256'])
        if original == active:
            continue
        evidence = (witness['evidence_ref'], reference('native_snapshot', claim['snapshot_sha256']),
                    reference('native_binding_adoption', hashlib.sha256(
                        json.dumps(entry, sort_keys=True, separators=(',', ':')).encode()).hexdigest()))
        result.append(Receipt.make('attribution', [POLICY, host, active, original], {
            'target_ref': active, 'assignment_ref': original, 'wi': key[0], 'executor': key[1],
            'responsible': None, 'delegator': None, 'state': 'assigned',
            'policy': POLICY, 'effective_at': None,
        }, occurred_at=None, observed_at=observed, producer=POLICY,
            evidence=tuple(sorted(evidence))))
    return result


def assignment_equivalences(facts: list[Receipt]) -> dict[tuple[str, str, str], str]:
    """Only one directly witnessed canonical target may resolve a binding ref."""
    candidates: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in facts:
        if row.kind != 'attribution' or row.producer != POLICY or not row.evidence:
            continue
        p = row.payload
        if (p.get('policy') != POLICY or p.get('state') != 'assigned'
                or not p.get('wi') or not p.get('executor') or not p.get('assignment_ref')):
            continue
        candidates[(p['target_ref'], p['wi'], p['executor'])].add(p['assignment_ref'])
    return {key: next(iter(values)) for key, values in candidates.items() if len(values) == 1}


def retained_binding_witness(path, data, claim, verified_boundaries):
    """An already verified native boundary survives loss of the live epoch row."""
    from pathlib import Path
    from .native_attribution import _private_snapshot
    folder = Path(path).parent / 'compute-cost'
    names = [claim['artifact_ref'], f"{claim['work_item_id']}-{claim['binding_sha256']}-baseline.json",
             f"{claim['work_item_id']}-{claim['active_runtime_binding_sha256']}-baseline.json"]
    if any(not (folder / name).exists() for name in names):
        return None  # Historical evidence can be unavailable; never invent it.
    snapshot = _private_snapshot(folder / claim['artifact_ref'])
    if (snapshot.get('schema') != 'pi_session_usage_snapshot/v2'
            or snapshot['snapshot_sha256'] != claim['snapshot_sha256']
            or snapshot['work_item_id'] != claim['work_item_id']
            or snapshot['epoch'] != claim['epoch']
            or snapshot['runtime_binding_sha256'] != claim['binding_sha256']):
        return None
    original = _private_snapshot(folder / f"{claim['work_item_id']}-{claim['binding_sha256']}-baseline.json")
    active = _private_snapshot(folder / f"{claim['work_item_id']}-{claim['active_runtime_binding_sha256']}-baseline.json")
    fields = ('work_item_id', 'agent_id', 'epoch', 'phase', 'authority_ref', 'execution_surface', 'runtime')
    if (any(original[k] != snapshot[k] or active[k] != snapshot[k] for k in fields)
            or active['runtime_binding_sha256'] != claim['active_runtime_binding_sha256']
            or snapshot['attribution_boundary']['baseline_snapshot_sha256'] != original['snapshot_sha256']
            or snapshot['attribution_boundary']['baseline'] != original['attribution_boundary']['baseline']):
        raise ReceiptError('retained binding adoption scope differs')
    host = pi_host_ref(json.loads(data.splitlines()[0])['id'])
    if snapshot['authority_ref'] != host:
        raise ReceiptError('retained binding adoption belongs to another native session')
    for document in (original, active, snapshot):
        for edge in (document['attribution_boundary']['baseline'], document['attribution_boundary']['final']):
            length = edge['source_bytes']
            if length > len(data) or hashlib.sha256(data[:length]).hexdigest() != edge['source_prefix_sha256']:
                raise ReceiptError('retained binding adoption prefix differs')
    baseline_ref = reference('pi_baseline', original['snapshot_sha256'])
    assignment = reference('assignment', claim['binding_sha256'])
    matches = [r for r in verified_boundaries if r.kind == 'attribution'
               and r.producer == 'scl_native_binding.v1' and baseline_ref in r.evidence
               and r.payload.get('assignment_ref') == assignment
               and r.payload.get('wi') == snapshot['work_item_id']
               and r.payload.get('executor') == snapshot['agent_id']]
    if not matches:
        return None
    key = (snapshot['work_item_id'], snapshot['agent_id'], snapshot['epoch'], snapshot['phase'], 'pi', host)
    return key, {'not_before':active['generated_at'], 'evidence_ref':reference('retained_binding_adoption',
        [original['snapshot_sha256'], active['snapshot_sha256'], sorted(r.fact_ref for r in matches)])}
