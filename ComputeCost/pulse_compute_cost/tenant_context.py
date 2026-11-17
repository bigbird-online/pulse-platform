"""Explicit product tenant membership; routes and paths cannot name a tenant.

Only a separately enrolled, private declaration provides a public tenant id
and display name. Matching preserves original usage, task and actor evidence.
The declaration is not a permission grant or an ownership inference.
"""
from __future__ import annotations
from pathlib import Path
from typing import Mapping, Any
import json, os, re

from .receipts import ReceiptError, reference, digest, current_receipts
from .events import parse_timestamp

FEATURE = 'tenant_context.v1'
SCHEMA = 'pulse_stats_tenant_registry/v1'
FIELDS = {'tenant_id', 'tenant_name', 'agent', 'node', 'runtime',
          'native_scope_ref', 'effective_from', 'effective_to'}
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
REF = re.compile(r'^ccf1_[a-f0-9]{64}$')
RESERVED = {'unassigned','ambiguous','unknown','not_applicable'}


def validate_tenant_context(value, *, usage_bound=True):
    fields = {'id','name','binding_ref','registry_ref'}
    if usage_bound:
        fields.add('usage_fact_ref')
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ReceiptError('invalid tenant display context')
    if not isinstance(value['id'], str) or not ID.fullmatch(value['id']) or value['id'] in RESERVED:
        raise ReceiptError('invalid registered tenant identity')
    name = value['name']
    if (not isinstance(name,str) or not 1 <= len(name) <= 64 or name != name.strip()
            or any(ord(c)<32 for c in name) or any(c in name for c in '/\\:@')):
        raise ReceiptError('tenant display name must be safe declared text')
    for key in ('binding_ref','registry_ref') + (('usage_fact_ref',) if usage_bound else ()):
        if not isinstance(value[key],str) or not REF.fullmatch(value[key]):
            raise ReceiptError('tenant display requires opaque declaration proof')


def load_tenant_document(path: Path):
    if not path.is_absolute():
        raise ReceiptError('tenant registry path must be explicitly absolute')
    st=path.lstat()
    if (path.is_symlink() or not path.is_file() or st.st_uid != os.getuid()
            or st.st_mode & 0o077 or st.st_size > 1024*1024):
        raise ReceiptError('tenant declaration must be a private owned regular file')
    from .relay_tenant_context import decode_document,validate_user_definitions,SCHEMA as RELAY_SCHEMA
    document=decode_document(path.read_bytes())
    allowed={'schema','bindings'}
    if isinstance(document,dict) and document.get('schema')==RELAY_SCHEMA:
        allowed|={'relay_users','definition_observed_at'}
    if (not isinstance(document,dict) or set(document)!=allowed
            or document['schema'] not in {SCHEMA,RELAY_SCHEMA}
            or not isinstance(document['bindings'],list) or len(document['bindings'])>256):
        raise ReceiptError('invalid explicit tenant registry')
    if document['schema']==RELAY_SCHEMA:validate_user_definitions(document)
    return document


def tenant_bindings(document):
    registry_ref=reference('tenant_registry',digest(document)); result=[]
    for entry in document['bindings']:
        if not isinstance(entry,dict) or set(entry) != FIELDS:
            raise ReceiptError('invalid tenant member declaration')
        binding_ref=reference('tenant_actor_binding',[registry_ref,entry])
        context={'id':entry['tenant_id'],'name':entry['tenant_name'],
                 'registry_ref':registry_ref,'binding_ref':binding_ref}
        validate_tenant_context(context, usage_bound=False)
        for field in ('agent','node'):
            if (not isinstance(entry[field],str) or not ID.fullmatch(entry[field])
                    or entry[field] in RESERVED):
                raise ReceiptError('tenant member must declare exact actor and node')
        if entry['runtime'] not in {'codex','pi','cc','cc_sdk'}:
            raise ReceiptError('tenant member must declare runtime')
        if not isinstance(entry['native_scope_ref'],str) or not REF.fullmatch(entry['native_scope_ref']):
            raise ReceiptError('tenant member requires exact native scope proof')
        start=parse_timestamp(entry['effective_from'],'tenant binding start')
        end=parse_timestamp(entry['effective_to'],'tenant binding end') if entry['effective_to'] else None
        if end is not None and start >= end:
            raise ReceiptError('tenant member window is empty or reversed')
        result.append((entry,context,start,end))
    if len({digest(v[0]) for v in result}) != len(result):
        raise ReceiptError('duplicate tenant member declaration')
    return result


def load_tenant_registry(path: Path):
    return tenant_bindings(load_tenant_document(path))


def collect_tenant_context(facts, registry: Mapping[str, Any], observed: str, *, ledger_refs=None):
    """Append membership for exact enrolled scopes and fully bounded intervals.

    Without a naming registry, identity remains unknown. Existing actor evidence
    chooses the actual executor/node before membership matches; a collector node,
    bare poolKey, cwd string or a WI owner is never a membership selector.

    With `ledger_refs`, membership names only a usage version already in the
    ledger; a version this collection is still appending is proved by the
    next collection, so no claim names a version ingest may rewrite.
    """
    path=registry.get('tenant_registry_path')
    if path is None:
        return []
    if FEATURE not in registry.get('required_receipt_features',[]):
        raise ReceiptError('tenant metadata requires reader-first capability enrollment')
    if not isinstance(path,str):
        raise ReceiptError('invalid enrolled tenant registry path')
    document=load_tenant_document(Path(path))
    bindings=tenant_bindings(document)
    from .collaboration_query import _actor_projection
    from .receipt_sources import actor_receipt
    active,conflicts=current_receipts(facts)
    heads=active+[r for versions in conflicts.values() for r in versions]
    scopes={entry['native_scope_ref'] for entry,_,_,_ in bindings}
    usages=[r for r in heads if r.kind=='usage' and r.payload.get('native_scope_ref') in scopes]
    targets={ref for r in usages for ref in (r.entity_ref,r.payload.get('execution_ref')) if ref}
    actors=[r for r in heads if r.kind=='attribution' and r.payload.get('scope')=='actor' and r.payload['target_ref'] in targets]
    by_scope={}
    for binding in bindings:by_scope.setdefault(binding[0]['native_scope_ref'],[]).append(binding)
    identified,contexts,_,_= _actor_projection(usages,actors,{})
    result={}
    for target in identified:
        if ledger_refs is not None and target.fact_ref not in ledger_refs:
            continue
        p=target.payload; actor=contexts.get(target.entity_ref,{})
        start=p.get('interval_start') or target.occurred_at
        end=p.get('interval_end') or target.occurred_at
        if (not start or not end or (not p.get('interval_start')
                and p['granularity'] in {'turn_aggregate','session_counter'})):
            continue
        low,high=parse_timestamp(start,'usage start'),parse_timestamp(end,'usage end')
        metering = p.get('metering')
        if metering is not None:
            from .native_metering import validate_metering
            validate_metering(p)
            points = [parse_timestamp(point['at'], 'tenant metered at') for point in metering['points']]
        else:
            points = None
        for entry,tenant,first,last in by_scope.get(p.get('native_scope_ref'),[]):
            # Native notification points are the complete recorded token timing.
            # A coarse end can include consumption at its timestamp; equality
            # with an exclusive declaration end is insufficient proof.
            contained = (all(first <= point and (last is None or point < last) for point in points)
                         if points is not None else
                         low >= first and (last is None or high < last))
            if (entry['native_scope_ref'] != p.get('native_scope_ref')
                    or entry['agent'] != p.get('executor') or entry['node'] != actor.get('node')
                    or entry['runtime'] != p['runtime'] or not contained):
                continue
            row=actor_receipt(target,node=actor['node'],executor=p['executor'],observed=observed,
                evidence=(tenant['registry_ref'],tenant['binding_ref']),
                tenant={**tenant, 'usage_fact_ref':target.fact_ref}, path='tenant')
            result[row.fact_ref]=row
    rows=list(result.values())
    from .relay_tenant_context import collect_relay_input_tenants,SCHEMA as RELAY_SCHEMA
    if document['schema']==RELAY_SCHEMA:
        rows.extend(collect_relay_input_tenants(facts,registry,document,observed,ledger_refs=ledger_refs))
    return rows
