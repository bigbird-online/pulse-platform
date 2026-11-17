"""Installation checks storage protocols; business replay belongs to queries.

A damaged WI or optional metering batch must not prevent installing its repair
or restoring a compatible program. This gate reads only the authority contract
and observation header, never WI history, private evidence or observation bodies.
Semantic compatibility is verified by the exact candidate's CI/replay evidence,
not by holding the runtime activation lock through an unbounded data audit.
"""
from __future__ import annotations

import json

from .model import Json, Rejected
from .production_ledger import FORMAT, SCHEMA, ProductionLedger
from .production_segments import SCHEMA as SEGMENT_SCHEMA
from .observations import HEAD, SCHEMA as OBSERVATION_SCHEMA


def inspect_contract(ledger: ProductionLedger) -> Json:
    revision = ledger.refresh()
    tree = ledger.tree(revision)
    for path in (FORMAT, HEAD):
        if path in tree:
            oid = tree[path].removeprefix('100755:blob:')
            import re
            if not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', oid):
                raise Rejected('ledger_invalid', 'Storage contract is not a regular Git blob')
            if int(ledger.git('cat-file', '-s', oid)) > 64 * 1024:
                raise Rejected('ledger_invalid', 'Storage contract exceeds its metadata bound')
    bodies = ledger.blobs({p: tree[p] for p in (FORMAT, HEAD) if p in tree})
    contract = json.loads(bodies[FORMAT]) if FORMAT in bodies else None
    if contract is not None:
        expected = {'schema': contract.get('schema'), 'namespace': ledger.namespace,
                    'authority': ledger.config['identity'], 'ref': ledger.config['ref']}
        if contract != expected or contract['schema'] not in {SCHEMA, SEGMENT_SCHEMA}:
            raise Rejected('ledger_identity_changed', 'Installed program does not support the production storage contract')
    elif any(p.startswith('.harness/core/work-items/') for p in tree):
        raise Rejected('ledger_invalid', 'Native journals require their authority binding')
    head = json.loads(bodies[HEAD]) if HEAD in bodies else None
    if head is not None and (set(head) != {'schema', 'namespace', 'sequence', 'sha256'}
            or head['schema'] != OBSERVATION_SCHEMA or head['namespace'] != ledger.namespace
            or type(head['sequence']) is not int or head['sequence'] < 1):
        raise Rejected('observation_invalid', 'Installed program does not support the observation contract')
    return {'count': 0, 'revision': revision,
            'compatibility_scope': 'storage_contract', 'historical_replay': 'not_performed',
            'observation_watermark': head['sequence'] if head else 0}
