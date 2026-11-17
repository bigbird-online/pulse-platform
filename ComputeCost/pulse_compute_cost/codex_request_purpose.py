"""Exact native delegated request purpose; no session-wide or execution grant inheritance."""
from collections import defaultdict
import json
import re
from pathlib import Path
from .adapters import _codex_rollout_index, _root_codex_session, _copied_parent_positions, _stable_event_id
from .events import parse_timestamp
from .receipts import Receipt, current_receipts, reference, digest

UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$',re.I)
POLICY = 'codex_native_root_request.v1'


def native_root_requests(paths):
    """Join a delegated request to its verified root, not its copied context.

    Legacy byte-identical prefixes keep the usage adapter's exclusion. Native
    context replay can also carry an ancestor's SessionMeta without reproducing
    that prefix. Such a stream needs a native item_completed thread/turn witness
    for every accepted local request. The ancestor header is never local identity.
    """
    paths = sorted({Path(p) for p in paths if not Path(p).is_symlink() and Path(p).is_file()})
    index = _codex_rollout_index(paths, paths)
    choices, proofs = defaultdict(set), defaultdict(set)
    for item in index.values():
        root_session = _root_codex_session(item, index)
        if root_session is None or root_session == item.session_id:
            continue
        ancestry, ancestor = [], item
        while True:
            ancestry.append([ancestor.session_id, ancestor.parent_session_id,
                             ancestor.stream_from_meta[0][1].hex()])
            if ancestor.parent_session_id is None:
                break
            ancestor = index[ancestor.parent_session_id]
        ancestor_ids = {entry[0] for entry in ancestry[1:]}
        ancestry_ref = reference('native_root_ancestry', ancestry)
        copied = _copied_parent_positions(item, index)
        startup, valid, found = None, True, []
        inherited, owners = set(), defaultdict(dict)
        with item.path.open('rb') as handle:
            for position, raw in enumerate(handle, 1):
                if not raw.endswith(b'\n'):
                    break
                if position in copied or not any(tag in raw for tag in (
                        b'"session_meta"', b'"task_started"', b'"item_completed"')):
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    valid = False
                    break
                if not isinstance(row, dict) or not isinstance(row.get('payload'), dict):
                    valid = False
                    break
                p = row['payload']
                if row.get('type') == 'session_meta':
                    if p.get('id') in ancestor_ids:
                        inherited.add(reference('native_inherited_startup', digest(row)))
                        continue
                    if p.get('id') != item.session_id or p.get('session_id', root_session) != root_session:
                        valid = False
                        break
                    startup = reference('native_delegation_startup', digest(row))
                    continue
                if row.get('type') != 'event_msg':
                    continue
                if p.get('type') == 'item_completed':
                    turn, thread = p.get('turn_id'), p.get('thread_id')
                    if not all(isinstance(value, str) and UUID.fullmatch(value)
                               for value in (turn, thread)):
                        continue
                    try:
                        at = parse_timestamp(row.get('timestamp'), 'native task ownership')
                    except (ValueError, TypeError):
                        continue
                    prior = owners[turn].get(thread)
                    # One bounded witness per native owner, not every tool body.
                    if prior is None or at > prior[0]:
                        owners[turn][thread] = (at, reference('native_task_owner', digest(row)))
                    continue
                if p.get('type') != 'task_started' or startup is None:
                    continue
                turn, root = p.get('turn_id'), p.get('root_turn_id')
                if (not isinstance(turn, str) or not UUID.fullmatch(turn)
                        or not isinstance(root, str) or not UUID.fullmatch(root) or turn == root):
                    continue
                child = reference('usage', _stable_event_id('codex', 'registered-source', item.session_id+':'+turn))
                parent = reference('usage', _stable_event_id('codex', 'registered-source', root_session+':'+root))
                scopes = (reference('native_session', ['codex', item.session_id]),
                          reference('native_session', ['codex', root_session]))
                found.append((turn, row.get('timestamp'), child, (parent, *scopes),
                              (startup, ancestry_ref, reference('native_root_task_started', digest(row)))))
        if not valid:
            continue
        for turn, started, child, selected, evidence in found:
            witnesses = owners.get(turn, {})
            # An explicit contrary owner is never selected away, even on an old layout.
            if witnesses and set(witnesses) != {item.session_id}:
                continue
            if inherited:
                owner = witnesses.get(item.session_id)
                if owner is None:
                    continue
                try:
                    if owner[0] < parse_timestamp(started, 'native task start'):
                        continue
                except (ValueError, TypeError):
                    continue
                evidence = (*evidence, owner[1], *sorted(inherited))
            choices[child].add(selected)
            proofs[child].update(evidence)
    return {child: {'parent': next(iter(values))[0], 'child_scope': next(iter(values))[1],
                    'parent_scope': next(iter(values))[2], 'evidence': tuple(sorted(proofs[child]))}
            for child, values in choices.items() if len(values) == 1 and len(proofs[child]) >= 3}


def _root_request_candidates(facts, links, observed, *, prepared=None):
    """The exact parent request needs an already proven native task interval.

    Neither today's parent owner, cwd, a prompt mention, nor an unmatched
    parent thread establishes a cost purpose. Child Agent/quantities/rights
    are unchanged. Strong contrary child claims remain visible in the query.
    """
    active,conflicts=current_receipts(facts) if prepared is None else prepared
    usages={r.entity_ref:r for r in active if r.kind=='usage'}
    tasks=defaultdict(list)
    assertions=defaultdict(list)
    for row in active:
        if row.kind=='attribution' and row.payload.get('scope')!='actor':
            assertions[row.payload['target_ref']].append(row)
        if row.kind=='attribution' and row.producer in {'scl_host_session_binding.v1','codex_request_purpose.v1'}:
            p=row.payload
            valid = (p.get('policy')=='scl_host_session_binding.v1' and row.producer=='scl_host_session_binding.v1' and p.get('wi'))
            explicit = (row.producer=='codex_request_purpose.v1' and (
                (p.get('policy')=='codex_request_purpose.v1' and p.get('wi')) or
                (p.get('policy')=='non_wi_activity.v1' and p.get('work_kind')=='none' and p.get('wi') is None)))
            if ((valid or explicit) and p.get('scope') is None
                    and p.get('state')=='assigned' and row.evidence):
                tasks[p['target_ref']].append(row)
    uncertain={r.payload.get('target_ref') for values in conflicts.values() for r in values
               if r.kind=='attribution' and r.payload.get('scope')!='actor'}
    result=[]
    for entity,link in links.items():
        child,parent=usages.get(entity),usages.get(link['parent'])
        if not child or not parent or parent.entity_ref in uncertain or not link['evidence']:continue
        cp,pp=child.payload,parent.payload
        if (cp.get('runtime')!='codex' or pp.get('runtime')!='codex'
                or cp.get('native_scope_ref')!=link['child_scope']
                or pp.get('native_scope_ref')!=link['parent_scope']
                or cp.get('wi') is not None):continue
        starts=[pp.get('interval_start'),cp.get('interval_start')]
        ends=[cp.get('interval_end'),pp.get('interval_end')]
        if not all(starts+ends):continue
        if not (parse_timestamp(starts[0],'root start')<=parse_timestamp(starts[1],'child start')
                <=parse_timestamp(ends[0],'child end')<=parse_timestamp(ends[1],'root end')):continue
        claims=tasks[parent.entity_ref]
        purposes={(r.payload.get('wi'),r.payload.get('work_kind')) for r in claims}
        if len(purposes)!=1:continue
        wi,kind=next(iter(purposes))
        if pp.get('wi') not in (None,wi):continue
        if any(r.payload.get('state')!='assigned' or (r.payload.get('wi'),r.payload.get('work_kind'))!=(wi,kind)
               for r in assertions[parent.entity_ref]):continue
        evidence=set(link['evidence'])|{child.fact_ref,parent.fact_ref}
        evidence.update(r.fact_ref for r in claims)
        result.append(Receipt.make('attribution',[POLICY,entity,parent.entity_ref],{
            'target_ref':entity,'wi':wi,'executor':None,'responsible':None,'delegator':None,
            'state':'assigned','policy':'non_wi_activity.v1' if kind=='none' else POLICY,
            'effective_at':cp['interval_start'],**({'work_kind':'none'} if kind=='none' else {})},
            occurred_at=cp['interval_start'],observed_at=observed,producer=POLICY,
            evidence=tuple(sorted(evidence))))
    return result


def root_request_attributions(facts, links, observed):
    """Revalidate dependent claims, including already-published child purposes.

    A previously correct parent can gain an unclassified steering input or
    contrary evidence. Merely skipping that parent leaves its old child claim
    active forever. Version only this producer's exact child/parent identity;
    preserve independent assignments, conflicts, native quantities and history.
    """
    from dataclasses import replace

    active, conflicts = current_receipts(facts)
    heads = {row.entity_ref: row for row in active}
    usages = {row.entity_ref: row for row in active if row.kind == 'usage'}
    parent_claims = defaultdict(list)
    for row in active + [r for versions in conflicts.values() for r in versions]:
        if row.kind == 'attribution' and row.payload.get('scope') != 'actor':
            parent_claims[row.payload['target_ref']].append(row)
    candidates = {row.entity_ref: row for row in _root_request_candidates(facts, links, observed, prepared=(active, conflicts))}
    # Retraction is bounded to a fresh same-source native link and two observed
    # usages, not a missing file, a session-wide guess or a global sweep.
    for child_ref, link in links.items():
        entity = reference('attribution', [POLICY, child_ref, link['parent']])
        prior = heads.get(entity)
        child, parent = usages.get(child_ref), usages.get(link['parent'])
        if (entity in candidates or entity in conflicts or prior is None
                or prior.kind != 'attribution' or prior.producer != POLICY
                or prior.payload.get('target_ref') != child_ref
                or prior.payload.get('state') != 'assigned' or child is None or parent is None
                or child.payload.get('runtime') != 'codex' or parent.payload.get('runtime') != 'codex'
                or child.payload.get('native_scope_ref') != link['child_scope']
                or parent.payload.get('native_scope_ref') != link['parent_scope']):
            continue
        start = prior.payload['effective_at']
        evidence = set(link['evidence']) | {prior.fact_ref, child.fact_ref, parent.fact_ref}
        evidence.update(row.fact_ref for row in parent_claims[parent.entity_ref])
        candidates[entity] = Receipt.make('attribution', None, {
            'target_ref': child_ref, 'wi': None, 'executor': None,
            'responsible': None, 'delegator': None, 'state': 'unassigned',
            'policy': POLICY, 'effective_at': start}, entity_ref=entity,
            occurred_at=start, observed_at=observed, producer=POLICY,
            evidence=tuple(sorted(evidence)))
    result = []
    for entity, candidate in candidates.items():
        prior = heads.get(entity)
        if entity in conflicts or (prior is not None and prior.producer != POLICY):
            continue
        if prior is None:
            result.append(candidate)
            continue
        evidence = tuple(sorted(set(prior.evidence) | set(candidate.evidence)))
        if prior.payload == candidate.payload and prior.occurred_at == candidate.occurred_at:
            result.append(replace(prior, evidence=evidence))
        else:
            result.append(Receipt.make('attribution', None, candidate.payload,
                entity_ref=entity, supersedes=prior.fact_ref,
                occurred_at=candidate.occurred_at, observed_at=observed,
                producer=POLICY, evidence=tuple(sorted(set(evidence) | {prior.fact_ref}))))
    return result
