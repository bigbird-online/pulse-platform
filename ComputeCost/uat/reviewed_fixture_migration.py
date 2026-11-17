"""Finite reviewed migration helpers. Not a collector rule or a new ledger."""
import hashlib
import json
import re
from pathlib import Path
from pulse_compute_cost.receipts import Receipt, reference, digest, current_receipts
from pulse_compute_cost.receipt_sources import read_host

# Alpha-rename random invocation IDs, preserving equalities and parent links.
# Keep stable startup dispatch, native session, business identity and all counters.
IDS = {'event_ref', 'writer_ref', 'attempt_ref', 'usage_ref', 'coverage_ref',
       'human_ref', 'target_ref', 'attribution_ref', 'dispatch_ref', 'parent_ref'}
CLOCKS = {'at', 'started_at', 'sequence'}
POLICY = 'reviewed_test_contamination.v1'


def trace_shape(data):
    rows = [json.loads(line) for line in data.splitlines()]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError('invalid reviewed trace')
    stable = rows[0].get('dispatch_ref')
    names = {}
    def value(key, item):
        if key in IDS and item is not None and item != stable:
            names.setdefault(item, 'symbol_' + str(len(names)))
            return names[item]
        return item
    return [{k: value(k, v) for k, v in sorted(row.items()) if k not in CLOCKS} for row in rows]


def assertion(row):
    return row.kind, row.entity_ref, row.occurred_at, row.producer, row._payload_json


def source_plan(manifest, directory, observed):
    if manifest.get('schema') != 'reviewed_non_observations/v1':
        raise ValueError('review manifest required')
    traces = {item['sha256']: item for item in manifest['reproductions']}
    originals, additions = {}, {}
    for item in manifest['records']:
        if not re.fullmatch(r'ccf1_[a-f0-9]{64}\.jsonl', item['file']):
            raise ValueError('invalid exact source filename')
        path = Path(directory) / item['file']
        if path.is_symlink() or not path.is_file():
            raise ValueError('original source unavailable')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('original source changed')
        shape = trace_shape(data)
        witness = traces[item['trace_sha256']]
        if digest(shape) != witness['sha256'] or shape != witness['trace']:
            raise ValueError('original does not match reproduced mock graph')
        active, conflicts = current_receipts(read_host(path, observed, data=data))
        targets = [r for r in active if r.kind in {'usage', 'execution'}]
        if conflicts or sorted(r.fact_ref for r in targets) != item['target_fact_refs']:
            raise ValueError('reviewed source facts changed')
        for row in targets:
            if row.payload.get('wi') is not None or row.payload.get('executor') is not None:
                raise ValueError('business assignment in mock target')
            originals[row.fact_ref] = row
            at = row.payload.get('interval_start') or row.occurred_at
            additions[row.entity_ref] = Receipt.make('attribution', ['reviewed-non-observation', row.entity_ref], {
                'target_ref': row.entity_ref, 'wi': None, 'executor': None, 'responsible': None,
                'delegator': None, 'state': 'excluded', 'policy': POLICY, 'effective_at': at},
                occurred_at=at, observed_at=observed, producer=POLICY,
                evidence=(row.fact_ref, reference('source_bytes', item['sha256']),
                          reference('reviewed_fixture', digest(manifest))))
    return list(originals.values()), list(additions.values())


def verify_ledger(facts, originals):
    wanted = {r.fact_ref: r for r in originals}
    entities = {r.entity_ref for r in originals}
    retained = {r.fact_ref: r for r in facts if r.fact_ref in wanted}
    if set(retained) != set(wanted) or any(assertion(retained[k]) != assertion(v) for k, v in wanted.items()):
        raise ValueError('reviewed originals not intact in this ledger')
    active, conflicts = current_receipts([r for r in facts if r.entity_ref in entities])
    by_entity = {r.entity_ref: r for r in originals}
    if conflicts or {r.entity_ref for r in active} != entities or any(
            assertion(r) != assertion(by_entity[r.entity_ref]) for r in active):
        raise ValueError('target current assertions differ')
    for row in facts:
        if row.kind == 'attribution':
            p = row.payload
            if p.get('target_ref') in entities and p.get('scope') != 'actor' and row.producer != POLICY:
                raise ValueError('independent business claim requires separate review')
    return retained
