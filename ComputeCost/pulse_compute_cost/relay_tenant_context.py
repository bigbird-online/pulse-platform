"""Named product users from exact authenticated Relay input metadata.

Current configuration declares public names only. Historical membership is
proved by that event's authenticated input and existing native/dispatch actor
proof, never by current Agent ownership, poolKey naming or a session default.
No input body is interpreted, retained or exported here.
"""
from collections import defaultdict
from pathlib import Path
from datetime import datetime,timezone
from hashlib import sha256
import json,os,re

from .events import parse_timestamp
from .receipts import ReceiptError,reference,digest,current_receipts
from .tenant_context import validate_tenant_context

FEATURE='tenant_relay_input.v1'
SCHEMA='pulse_stats_tenant_registry/v2'
FIELDS={'user_id','tenant_id','tenant_name','source_ref','definition_ref'}

def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ReceiptError('duplicate tenant metadata field')
        result[key]=value
    return result

def decode_document(raw):
    return json.loads(raw,object_pairs_hook=_unique)


def validate_user_definitions(document):
    definitions=document.get('relay_users')
    if not isinstance(definitions,list) or not definitions or len(definitions)>256:
        raise ReceiptError('invalid Relay product tenant declaration')
    parse_timestamp(document['definition_observed_at'],'tenant naming observed')
    registry_ref=reference('tenant_registry',digest(document));result=[]
    for entry in definitions:
        if not isinstance(entry,dict) or set(entry)!=FIELDS:
            raise ReceiptError('invalid registered Relay user definition')
        user=entry['user_id']
        if not isinstance(user,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',user):
            raise ReceiptError('Relay tenant must name an explicit product user id')
        binding_ref=reference('relay_tenant_identity',[registry_ref,entry])
        context={'id':entry['tenant_id'],'name':entry['tenant_name'],'registry_ref':registry_ref,'binding_ref':binding_ref}
        validate_tenant_context(context,usage_bound=False)
        if any(not isinstance(entry[k],str) or not re.fullmatch(r'ccf1_[a-f0-9]{64}',entry[k]) for k in ('source_ref','definition_ref')):
            raise ReceiptError('Relay tenant definition lacks source proof')
        result.append((entry,context))
    if len({(e['source_ref'],e['user_id']) for e,_ in result})!=len(result):
        raise ReceiptError('duplicate Relay tenant definition')
    return result


def relay_user_declaration(config_path, registry, *, names, observed_at):
    """Read real UserConfig IDs; prepare explicit names, not historical scopes."""
    config_path=Path(config_path);st=config_path.lstat()
    if (not config_path.is_absolute() or config_path.is_symlink() or not config_path.is_file()
            or st.st_uid!=os.getuid() or st.st_mode & 0o077 or st.st_size>16*1024*1024):
        raise ReceiptError('Relay tenant naming source must be a private owned config')
    raw=config_path.read_bytes();config=decode_document(raw)
    users=config.get('users')
    if not isinstance(users,list) or not users or not all(isinstance(u,dict) for u in users):
        raise ReceiptError('explicit Relay user registry missing')
    ids=[u.get('userId') for u in users]
    if len(set(ids))!=len(ids) or set(names)!=set(ids):
        raise ReceiptError('public tenant names must cover the exact registered users')
    hosts=[s for s in registry['sources'] if s['kind']=='relay_host' and s.get('actor_attribution') is True
           and s.get('host_native_input') is True and s.get('human_input_directory')]
    if not hosts:
        raise ReceiptError('authenticated Relay input source must already be enrolled')
    definitions=[{'user_id':uid,'tenant_id':uid,'tenant_name':names[uid],
                  'source_ref':reference('source',host['id']),
                  'definition_ref':reference('relay_user_config',[sha256(raw).hexdigest(),uid,host['id'],names[uid]])}
                 for host in hosts for uid in sorted(ids)]
    document={'schema':SCHEMA,'bindings':[],'relay_users':definitions,'definition_observed_at':observed_at}
    validate_user_definitions(document)
    return document


def _input_metadata(registry, wanted, definitions):
    """Hash private original bytes; extract identity/time fields only."""
    from .pi_context import input_directories
    declared={entry['source_ref'] for entry,_ in definitions};result=defaultdict(list)
    for source in registry['sources']:
        source_ref=reference('source',source['id'])
        if (source_ref not in declared or source.get('kind')!='relay_host'
                or source.get('actor_attribution') is not True or source.get('host_native_input') is not True):
            continue
        paths=[]
        for directory in input_directories(source):
            info=directory.lstat()
            if (directory.is_symlink() or not directory.is_dir() or info.st_uid!=os.getuid() or info.st_mode & 0o077):
                raise ReceiptError('unsafe enrolled tenant input directory')
            paths.extend(directory.glob('*.json'))
        if len(paths)>10000:
            raise ReceiptError('enrolled tenant input inventory exceeds bound')
        for path in sorted(set(paths)):
            if not re.fullmatch(r'[a-f0-9]{64}',path.stem):continue
            info=path.lstat()
            if (path.is_symlink() or not path.is_file() or info.st_uid!=os.getuid()
                    or info.st_mode & 0o077 or info.st_size>1024*1024):
                raise ReceiptError('unsafe enrolled tenant input metadata')
            raw=path.read_bytes();input_ref=reference('input_metadata',sha256(raw).hexdigest())
            if input_ref not in wanted:continue
            value=decode_document(raw)
            if not isinstance(value,dict) or not isinstance(value.get('source'),dict):
                raise ReceiptError('invalid authenticated tenant input metadata')
            origin=value['source']
            # humanRef is captured from authenticated UserConfig.userId by the
            # registered Relay channel, not a guessed route/owner or chat id.
            identity=[origin.get('channel'),origin.get('eventId'),origin.get('humanRef'),value.get('poolKey')]
            milliseconds=value.get('observedAtMs')
            if (identity[0] not in {'telegram','feishu'} or any(not isinstance(v,str) or not v for v in identity)
                    or type(milliseconds) is not int or milliseconds<=0
                    or sha256(json.dumps(identity,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()!=path.stem):
                raise ReceiptError('tenant input metadata identity differs')
            result[input_ref].append({'user_id':identity[2],'channel':identity[0],'node':source.get('node'),
                'source_ref':source_ref,'at':datetime.fromtimestamp(milliseconds/1000,timezone.utc)})
    return result


def collect_relay_input_tenants(facts,registry,document,observed,*,ledger_refs=None):
    definitions=validate_user_definitions(document)
    if FEATURE not in registry.get('required_receipt_features',[]):
        raise ReceiptError('Relay tenant input requires reader-first feature enrollment')
    active,conflicts=current_receipts(facts)
    from .collaboration_query import _request_observation_projection
    # Use the existing exact quantity join to refine an encompassing Host
    # interval to its evidenced native request point. Its original fact ref
    # stays the anchor; no projected quantity is ever persisted. Without that
    # compatible witness, a coarse interval cannot gain early membership.
    active,conflicts,groups,_=_request_observation_projection(active,conflicts)
    heads=[*active,*(r for values in conflicts.values() for r in values)]
    # Decode each identity fact once; the producer field rules most out
    # without decoding.
    def actor_payload(row):
        if row.kind!='attribution' or row.producer!='registered_runtime_actor.v1':return None
        p=row.payload
        return p if p.get('scope')=='actor' and p.get('policy')=='registered_runtime_actor.v1' else None
    head_actors=[(r,p) for r in heads if (p:=actor_payload(r)) is not None]
    # Route evidence retains the exact immutable original dispatch version.
    # Its superseded actor head cannot erase agreeing historical ingress proof.
    input_actors={r.fact_ref:r for r in facts if (p:=actor_payload(r)) is not None and p['context'].get('input_ref')}
    wanted={r.payload['context']['input_ref'] for r in input_actors.values()}
    metadata=_input_metadata(registry,wanted,definitions)
    names=defaultdict(list)
    for entry,context in definitions:names[(entry['source_ref'],entry['user_id'])].append(context)
    # Membership already recorded under this same registry stays true for
    # that exact usage version; only versions without it need proving. A new
    # registry document has a new ref and is proven afresh for every usage.
    registry_refs={context['registry_ref'] for _,context in definitions}
    members={(p['context'].get('tenant') or {}).get('usage_fact_ref') for _,p in head_actors
             if (p['context'].get('tenant') or {}).get('registry_ref') in registry_refs}
    usages=[r for r in heads if r.kind=='usage' and r.fact_ref not in members]
    targets={ref for r in usages for ref in (r.entity_ref,r.payload.get('execution_ref')) if ref}
    actors=[r for r,p in head_actors if p['target_ref'] in targets]
    del head_actors  # Release decoded payloads before the projection.
    by_target=defaultdict(list)
    for row in actors:by_target[row.payload['target_ref']].append(row)
    from .collaboration_query import _actor_projection
    from .receipt_sources import actor_receipt
    executions=[r for r in heads if r.kind=='execution' and r.entity_ref in targets]
    # Executions carry the dispatch time that bounds Host-declared identity.
    identified,contexts,_,_=_actor_projection(usages,actors+executions,{},observation_groups=groups)
    dispatched={r.entity_ref:r.occurred_at for r in executions if r.occurred_at}
    def effective_start(actor,target,usage,start):
        # Identity the Host declared at its dispatch covers every request of
        # that execution, as in _actor_projection; a native twin recorded a
        # few milliseconds earlier does not make it late.
        when=parse_timestamp(actor.payload['effective_at'],'tenant actor start')
        end=usage.payload.get('interval_end') or usage.occurred_at
        if (target==usage.payload.get('execution_ref') and target in dispatched and end
                and when==parse_timestamp(dispatched[target],'dispatch')
                and when<=parse_timestamp(end,'usage end')):
            return start
        return when
    result={}
    for usage in identified:
        # Name only a usage version the ledger already holds (see tenant_context).
        if ledger_refs is not None and usage.fact_ref not in ledger_refs:continue
        p=usage.payload;executor=p.get('executor');context=contexts.get(usage.entity_ref,{})
        if executor in {None,'ambiguous'} or context.get('node') in {None,'ambiguous'}:continue
        boundary=p.get('interval_start') or usage.occurred_at
        if not boundary or (not p.get('interval_start') and p['granularity'] in {'session_counter','turn_aggregate'}):continue
        start=parse_timestamp(boundary,'tenant usage start')
        proofs=[]
        for target in {usage.entity_ref,p.get('execution_ref')}-{None}:
            for actor in by_target.get(target,[]):
                a=actor.payload;ctx=a['context']
                if a.get('usage_fact_ref') not in {None,usage.fact_ref}:continue
                if not a.get('effective_at') or effective_start(actor,target,usage,start)>start:continue
                if ctx.get('input_ref'):
                    proofs.append((ctx['input_ref'],actor,target))
                elif p['runtime']=='cc' and target==usage.entity_ref:
                    # Existing CC route evidence binds this message to the exact
                    # immutable input and its original dispatch actor fact.
                    for ref in actor.evidence:
                        origin=input_actors.get(ref)
                        if origin is None:continue
                        input_ref=origin.payload['context']['input_ref']
                        expected=reference('attribution',['cc_native_input_route',usage.entity_ref,input_ref])
                        if actor.entity_ref==expected and input_ref in actor.evidence:
                            proofs.append((input_ref,origin,None))
        for input_ref,actor,target in proofs:
            a=actor.payload;ctx=a['context']
            if (a['executor']!=executor or ctx['node']!=context['node']
                    or ctx.get('entrypoint') not in {'telegram','feishu'}):continue
            effective=a.get('effective_at')
            if not effective or effective_start(actor,target,usage,start)>start:continue
            for original in metadata.get(input_ref,[]):
                if (original['node']!=context['node'] or original['channel']!=ctx['entrypoint']
                        or original['at']>parse_timestamp(a['effective_at'],'authenticated dispatch')
                        or original['at']>start):continue
                for tenant in names.get((original['source_ref'],original['user_id']),[]):
                    binding_ref=reference('authenticated_relay_tenant',[tenant['binding_ref'],input_ref,actor.fact_ref,usage.fact_ref])
                    receipt=actor_receipt(usage,node=context['node'],executor=executor,observed=observed,
                        evidence=(tenant['registry_ref'],tenant['binding_ref'],input_ref,actor.fact_ref),
                        tenant={**tenant,'binding_ref':binding_ref,'usage_fact_ref':usage.fact_ref},path='relay_tenant')
                    result[receipt.fact_ref]=receipt
    return list(result.values())
