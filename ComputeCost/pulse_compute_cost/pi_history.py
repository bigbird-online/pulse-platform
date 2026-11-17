"""An explicit parentSession plus identical native entries proves copied history.

No new bill, directory-name inference, source mutation or deletion. The existing
coverage receipt carries a bounded witness; queries retain both observations.
"""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Any
import json
import os

from .receipts import Receipt, ReceiptError, digest, reference, TOKEN_FIELDS

PRODUCER = 'pi_parent_history.v1'
REASON = 'identical_native_parent_entry'


def history_index(path: Path, data: bytes) -> dict[str, Any]:
    entries = {}; header = None
    for raw in data.splitlines(keepends=True):
        if not raw.strip():
            continue
        if not raw.endswith(b'\n'):
            raise ReceiptError('incomplete Pi history prefix')
        row = json.loads(raw)
        if header is None:
            if row.get('type') != 'session' or not isinstance(row.get('id'), str):
                raise ReceiptError('invalid Pi history header')
            header = row
            continue
        activity = row.get('type')
        if activity == 'message':
            message = row.get('message', {})
            activity = 'assistant' if message.get('role') == 'assistant' and isinstance(message.get('usage'), dict) else None
        elif activity not in {'compaction','branch_summary'} or not isinstance(row.get('usage'), dict):
            activity = None
        if activity and isinstance(row.get('id'), str):
            key = (row['id'], activity)
            native = reference('native_record', digest(row))
            if key in entries and entries[key] != native:
                raise ReceiptError('conflicting Pi native history entry')
            entries[key] = native
    if header is None:
        raise ReceiptError('empty Pi history prefix')
    parent = header.get('parentSession')
    if parent is not None and (not isinstance(parent, str) or not Path(parent).is_absolute()):
        raise ReceiptError('Pi parent must name an enrolled absolute file')
    return {'session':header['id'], 'header':reference('pi_history_header',digest(header)),
            'parent':os.path.normpath(parent) if parent else None, 'entries':entries}


def copy_receipts(index: Mapping[str, dict[str, Any]], observed: str) -> list[Receipt]:
    """Only files present in this enrolled source prefix can be compared."""
    result = []
    for path, child in index.items():
        parent_path = child['parent']
        if parent_path not in index:
            continue
        chain = set(); current = path
        while current in index:
            if current in chain:
                raise ReceiptError('cyclic Pi parent history')
            chain.add(current); current = index[current]['parent']
        parent = index[parent_path]
        if child['session'] == parent['session']:
            continue  # ordinary archive copies already share exact usage identity
        for key, native in child['entries'].items():
            if key not in parent['entries']:
                continue  # a child may make genuinely new calls after its fork
            if native != parent['entries'][key]:
                raise ReceiptError('changed Pi parent history entry')
            original = reference('usage',[parent['session'], *key])
            copied = reference('usage',[child['session'], *key])
            proof = [original,copied,native,parent['header'],child['header']]
            result.append(Receipt.make('coverage',[PRODUCER,original,copied],{
                'domain':'usage','wi':None,'source_ref':reference('native_session',['pi',child['session']]),
                'status':'excluded','expected':1,'observed':1,'watermark':digest(sorted(proof)),
                'reason':REASON}, occurred_at=None,observed_at=observed,producer=PRODUCER,
                evidence=tuple(sorted(proof))))
    return result


def reconcile_copies(rows: list[Receipt], witnesses: list[Receipt]) -> dict[str,str]:
    """Return copied->original only with a unique, quantity-conserving witness."""
    by_ref = {r.entity_ref:r for r in rows}
    proposals = defaultdict(set)
    for witness in witnesses:
        p = witness.payload
        if (witness.producer != PRODUCER or p.get('reason') != REASON
                or p.get('expected') != 1 or p.get('observed') != 1 or len(witness.evidence) != 5
                or p.get('watermark') != digest(list(witness.evidence))):
            continue
        targets = [by_ref[r] for r in witness.evidence if r in by_ref]
        if len(targets) != 2:
            continue  # never erase a copy when the original was not collected
        children = [r for r in targets if r.payload.get('native_scope_ref') == p.get('source_ref')]
        if len(children) != 1:
            continue
        child = children[0]; parent = next(r for r in targets if r != child)
        cp, pp = child.payload, parent.payload
        if (cp.get('native_scope_ref') == pp.get('native_scope_ref')
                or child.producer != parent.producer or child.producer != 'pi_native.v1'
                or cp.get('granularity') != 'request' or pp.get('granularity') != 'request'
                or child.occurred_at != parent.occurred_at
                or any(cp.get(k) != pp.get(k) for k in ('runtime','model','provider','tokens','native_tokens'))
                or any(cp['tokens'].get(k) is None for k in TOKEN_FIELDS)):
            continue
        shared = set(child.evidence) & set(parent.evidence) & set(witness.evidence)
        # The typed witness also includes the exact shared native-record hash.
        if not shared:
            continue
        proposals[child.entity_ref].add(parent.entity_ref)
    accepted = {child:next(iter(parents)) for child,parents in proposals.items() if len(parents)==1}
    for child in list(accepted):
        seen = set(); current = child
        while current in accepted:
            if current in seen:
                for member in seen:accepted.pop(member,None)
                break
            seen.add(current);current=accepted[current]
    return accepted
