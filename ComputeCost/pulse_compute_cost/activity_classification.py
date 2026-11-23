"""Classify reviewed purpose and actor context or exact Host/native proof.

This is not a session registry or a default for null WI. Original observations
and their effective boundaries are retained in the existing receipt graph.
"""
from __future__ import annotations
from collections import defaultdict
import json
from pathlib import Path
from typing import Any, Mapping
from .receipts import Receipt, ReceiptError, digest, non_wi_attribution, reference

FEATURE = "non_wi_activity.v1"
CONFIRMATIONS = Path(__file__).resolve().parents[1] / "reviewed-activity.json"


def _reviewed_cc_family_ingress(facts: list[Receipt], anchor: Receipt,
                                item: Mapping[str, Any], observed: str) -> list[Receipt]:
    """One confirmed family origin, not another WI or Host-authenticated input.

    CC's native scope already includes its child transcripts. A confirmation
    for that exact scope does not apply to another session sharing the WI.
    Quantity forks and independent contrary route assertions remain intact.
    """
    from collections import defaultdict
    from .events import parse_timestamp
    scope = item['native_scope_ref']
    p = anchor.payload
    if (anchor.kind != 'usage' or p.get('runtime') != 'cc'
            or p.get('mapping') != 'cc_message.v1' or p.get('granularity') != 'request' or not scope
            or p.get('native_scope_ref') != scope or item['agent'] is not None
            or not item['node'] or item['entrypoint'] not in {'telegram', 'feishu', 'cli'}):
        raise ReceiptError('invalid reviewed CC session ingress')
    effective = parse_timestamp(item['effective_at'], 'confirmed family boundary')
    start = p.get('interval_start') or anchor.occurred_at
    if start is None or effective > parse_timestamp(start, 'anchor start'):
        raise ReceiptError('reviewed family boundary follows its anchor')
    groups = defaultdict(list)
    for row in facts:
        if row.kind == 'usage':
            groups[row.entity_ref].append(row)
    evidence = tuple(sorted(set(item['evidence_refs']) | {anchor.fact_ref,
        reference('reviewed_activity', digest(item))}))
    result = []
    for target, versions in groups.items():
        if any(r.payload.get('runtime') != 'cc' or r.payload.get('mapping') != 'cc_message.v1'
               or r.payload.get('native_scope_ref') != scope or r.payload.get('granularity') != 'request'
               for r in versions):
            continue
        boundaries = [r.payload.get('interval_start') or r.occurred_at for r in versions]
        if any(b is None or parse_timestamp(b, 'native start') < effective for b in boundaries):
            continue
        result.append(Receipt.make('attribution', ['reviewed_cc_family_ingress', scope, target, digest(item)], {
            'target_ref': target, 'wi': None, 'executor': None, 'responsible': None, 'delegator': None,
            'state': 'unassigned', 'policy': 'registered_runtime_actor.v1', 'effective_at': item['effective_at'],
            'scope': 'actor', 'context': {'node': item['node'], 'entrypoint': item['entrypoint'], 'workspace_ref': None}},
            occurred_at=item['effective_at'], observed_at=observed, producer='reviewed_activity.v1',
            evidence=evidence + (reference('native_session_usage', [scope, target]),)))
    return result



def _native_request_prior(current, prior, item):
    """An explicitly addressed unknown request, never a general null-WI rule."""
    import re
    from .mcp_request_purpose import POLICY as LEGACY_POLICY
    fields = {'target_ref','wi','executor','responsible','delegator','state','policy','effective_at'}
    if prior is None or prior.kind != 'attribution' or set(prior.payload) != fields:
        return False
    p, usage = prior.payload, current.payload
    request, record = item.get('native_request_ref'), item.get('native_request_record_ref')
    if not all(isinstance(r,str) and re.fullmatch(r'ccf1_[a-f0-9]{64}',r) for r in (request,record)):
        return False
    scope = usage.get('native_scope_ref')
    return (current.kind == 'usage' and usage.get('runtime') == 'cc'
        and usage.get('mapping') == 'cc_message.v1' and usage.get('granularity') == 'request'
        and isinstance(scope,str) and re.fullmatch(r'ccf1_[a-f0-9]{64}',scope)
        and prior.entity_ref == reference('attribution',[LEGACY_POLICY,current.entity_ref])
        and prior.producer == p['policy'] == 'cc_native_request_purpose.v1'
        and p['target_ref'] == current.entity_ref and p['state'] == 'unassigned'
        and all(p[k] is None for k in ('wi','executor','responsible','delegator'))
        and p['effective_at'] == item['usage_interval'][0]
        and item.get('source_attribution_fact_ref') == prior.fact_ref
        and item['review_basis'] in {'native_user_request','recorded_continuation'}
        and current.fact_ref in item['evidence_refs'] and record in item['evidence_refs']
        and record in prior.evidence
        and reference('cc_native_request_record',[request,record]) in prior.evidence
        and reference('native_session_usage',[scope,current.entity_ref]) in prior.evidence
        and reference('cc_native_request_usage',[scope,current.entity_ref,request]) in prior.evidence)


def _native_session_prior(current, prior, item):
    """A human-reviewed unknown request whose boundary cannot be proven.

    Only the collector's own unassigned claim for this exact usage, witnessed
    as part of the named native session, may be replaced; never a WI, a
    reviewed decision or another producer's claim.
    """
    import re
    from .mcp_request_purpose import POLICY as LEGACY_POLICY
    fields = {'target_ref','wi','executor','responsible','delegator','state','policy','effective_at'}
    if prior is None or prior.kind != 'attribution' or set(prior.payload) != fields:
        return False
    p, usage = prior.payload, current.payload
    scope = item.get('native_scope_ref')
    return (current.kind == 'usage' and usage.get('runtime') == 'cc'
        and usage.get('mapping') == 'cc_message.v1' and usage.get('granularity') == 'request'
        and isinstance(scope, str) and re.fullmatch(r'ccf1_[a-f0-9]{64}', scope)
        and usage.get('native_scope_ref') == scope
        and prior.entity_ref == reference('attribution',[LEGACY_POLICY,current.entity_ref])
        and prior.producer == p['policy'] == 'cc_native_request_purpose.v1'
        and p['target_ref'] == current.entity_ref and p['state'] == 'unassigned'
        and all(p[k] is None for k in ('wi','executor','responsible','delegator'))
        and p['effective_at'] == item['usage_interval'][0]
        and item.get('source_attribution_fact_ref') == prior.fact_ref
        and item['review_basis'] == 'human_reviewed_session'
        and current.fact_ref in item['evidence_refs']
        and reference('native_session_usage',[scope,current.entity_ref]) in prior.evidence)


def _restates(revision, prior):
    """The same producer's assertion again, its boundary at the same instant."""
    from .events import parse_timestamp
    a, b = revision.payload, prior.payload
    if revision.producer != prior.producer or set(a) != set(b):
        return False
    if any(a[k] != b[k] for k in a if k != 'effective_at'):
        return False
    try:
        return parse_timestamp(a['effective_at'], 'boundary') == parse_timestamp(b['effective_at'], 'boundary')
    except (KeyError, TypeError, ValueError):
        return False


def _reviewed_native_request_receipts(old, prior, item, observed, by_fact, heads, valid=None):
    """Supersede only this source assertion; reuse a proven agreeing review."""
    if not (valid or _native_request_prior)(old, prior, item):
        raise ReceiptError('reviewed native request source proof is invalid')
    corrected = reviewed_purpose_receipt(old, prior, item, observed)
    head = heads.get(prior.entity_ref)
    if head is None:
        return []  # Concurrent heads are retained, never arbitrarily selected.
    if head.fact_ref == prior.fact_ref:
        return [corrected]
    cursor, seen, restated = head, set(), True
    while cursor.supersedes and cursor.fact_ref not in seen:
        seen.add(cursor.fact_ref)
        restated = restated and _restates(cursor, prior)
        if cursor.supersedes == prior.fact_ref:
            break
        cursor = by_fact.get(cursor.supersedes)
        if cursor is None:
            return []
    else:
        return []
    if restated:
        # The source only re-serialized the reviewed assertion; the review
        # still addresses it and now supersedes its current version.
        return [Receipt.make('attribution', None, corrected.payload, entity_ref=head.entity_ref,
            supersedes=head.fact_ref, occurred_at=corrected.occurred_at, observed_at=observed,
            producer=corrected.producer, evidence=tuple(sorted(set(corrected.evidence) | {head.fact_ref})))]
    if (head.producer != 'reviewed_activity.v1' or head.payload != corrected.payload
            or head.occurred_at != corrected.occurred_at):
        return []  # Other purpose/reviewer decisions remain authoritative.
    evidence = tuple(sorted(set(head.evidence) | set(corrected.evidence)))
    if set(evidence) == set(head.evidence):
        return [head]
    return [Receipt.make('attribution',None,head.payload,entity_ref=head.entity_ref,
        supersedes=head.fact_ref,occurred_at=head.occurred_at,observed_at=observed,
        producer=head.producer,evidence=evidence)]


def reviewed_activity_receipts(facts: list[Receipt], document: Mapping[str, Any], observed: str) -> list[Receipt]:
    if document.get("schema") != "computecost_reviewed_activity/v1" or not isinstance(document.get("items"), list):
        raise ReceiptError("invalid reviewed activity document")
    by_fact = {row.fact_ref: row for row in facts}
    from .receipts import current_receipts
    heads = {row.entity_ref: row for row in current_receipts(facts)[0]}
    result = []
    for item in document["items"]:
        common = {"target_fact_ref", "agent", "node", "entrypoint", "evidence_refs"}
        purpose_fields = common | {'work_item_id', 'source_attribution_fact_ref', 'usage_interval', 'review_basis'}
        request_fields = purpose_fields - {'source_attribution_fact_ref'}
        native_fields = purpose_fields | {'native_request_ref','native_request_record_ref'}
        session_fields = purpose_fields | {'native_scope_ref'}
        family_fields = common | {'scope', 'native_scope_ref', 'effective_at'}
        if set(item) not in (common | {"work_kind"}, common | {"work_item_id"}, common | {"scope"}, purpose_fields, request_fields, family_fields, native_fields, session_fields):
            raise ReceiptError("invalid reviewed activity fields")
        if "scope" in item and item["scope"] != ("cc_session_ingress" if set(item) == family_fields else "actor"):
            raise ReceiptError("unsupported reviewed identity scope")
        if not isinstance(item["evidence_refs"], list) or not item["evidence_refs"]:
            raise ReceiptError("reviewed activity requires retained evidence")
        old = by_fact.get(item["target_fact_ref"])
        if old is None:
            continue  # This ledger has never observed this exact historical fact.
        if set(item) == family_fields:
            result.extend(_reviewed_cc_family_ingress(facts, old, item, observed))
            continue
        if set(item) == session_fields:
            if any(item[k] is not None for k in ('agent','node','entrypoint')):
                raise ReceiptError('whole-request purpose cannot overwrite independent actor facts')
            prior = by_fact.get(item['source_attribution_fact_ref'])
            if prior is None:
                continue
            result.extend(_reviewed_native_request_receipts(old,prior,item,observed,by_fact,heads,_native_session_prior))
            continue
        if set(item) == native_fields:
            if any(item[k] is not None for k in ('agent','node','entrypoint')):
                raise ReceiptError('whole-request purpose cannot overwrite independent actor facts')
            prior = by_fact.get(item['source_attribution_fact_ref'])
            if prior is None:
                continue
            result.extend(_reviewed_native_request_receipts(old,prior,item,observed,by_fact,heads))
            continue
        if set(item) in (purpose_fields, request_fields):
            if any(item[k] is not None for k in ('agent', 'node', 'entrypoint')):
                raise ReceiptError('whole-request purpose cannot overwrite independent actor facts')
            prior = by_fact.get(item.get('source_attribution_fact_ref'))
            if set(item) == purpose_fields and prior is None:
                continue
            if set(item) == request_fields:
                # A detail projection may omit the canonical weak inference.
                # Resolve its exact identity from the COMPLETE receipt graph,
                # not from a session, time window or another WI's current owner.
                inference_id = reference('attribution', ['scl_host_session_binding.v1', old.entity_ref])
                candidate = heads.get(inference_id)
                if candidate is not None and candidate.producer == 'reviewed_activity.v1':
                    proposed = reviewed_purpose_receipt(old, None, item, observed)
                    if (candidate.kind == 'attribution' and candidate.payload == proposed.payload
                            and candidate.occurred_at == proposed.occurred_at):
                        # The ledger may merge equivalent reviews of a growing
                        # observation. Reuse that semantic head; add only genuinely
                        # new proof instead of creating a second purpose identity.
                        evidence = tuple(sorted(set(candidate.evidence) | set(proposed.evidence)))
                        if set(evidence) == set(candidate.evidence):
                            result.append(candidate)
                        else:
                            result.append(Receipt.make('attribution', None, candidate.payload,
                                entity_ref=candidate.entity_ref, supersedes=candidate.fact_ref,
                                occurred_at=candidate.occurred_at, observed_at=observed,
                                producer=candidate.producer, evidence=evidence))
                        continue
                    candidate = None  # A different decision remains independent.
                if (candidate is not None and candidate.entity_ref == inference_id
                        and candidate.kind == 'attribution'
                        and candidate.producer == 'scl_host_session_binding.v1'):
                    payload = candidate.payload
                    if (payload.get('policy') == 'scl_host_session_binding.v1'
                            and payload.get('target_ref') == old.entity_ref
                            and payload.get('state') == 'ambiguous'
                            and payload.get('effective_at') == item['usage_interval'][0]
                            and all(payload.get(k) is None for k in
                                    ('wi', 'executor', 'responsible', 'delegator',
                                     'assignment_ref', 'execution_ref', 'scope', 'work_kind', 'context'))):
                        prior = candidate
            corrected = reviewed_purpose_receipt(old, prior, item, observed)
            head = heads.get(prior.entity_ref) if prior is not None else None
            if prior is None or (head is not None and head.fact_ref in {prior.fact_ref, corrected.fact_ref}):
                result.append(corrected)
            continue
        # This recorded review addresses one immutable usage observation.
        # Later quantity corrections do not invalidate its business purpose.
        # Conflicting task/quantity evidence remains visible in the query; it
        # must not halt unrelated collection or be repaired by this label.
        current = old
        evidence = tuple(item["evidence_refs"]) + (reference("reviewed_activity", digest(item)),)
        if "work_item_id" in item:
            # Finite reviewed historical fact, not an execution grant or a rule
            # for subsequent requests from the same native session/workspace.
            # The review addresses this entire immutable usage observation,
            # not a new task starting after its last measured Token.
            effective = current.payload.get("interval_start") or current.occurred_at
            reviewed = Receipt.make("attribution", ["reviewed_work_item", current.entity_ref, digest(item)], {
                "target_ref": current.entity_ref, "wi": item["work_item_id"], "executor": item["agent"],
                "responsible": None, "delegator": None, "state": "assigned",
                "policy": "reviewed_activity.v1", "effective_at": effective,
            }, occurred_at=effective, observed_at=observed, producer="reviewed_activity.v1",
                evidence=evidence + (old.fact_ref,))
            prior = heads.get(reviewed.entity_ref)
            if prior is not None and prior.producer == reviewed.producer:
                if prior.payload == reviewed.payload and prior.occurred_at == effective:
                    # Reuse the correction head, never fork its original shape.
                    reviewed = prior
                elif (prior.occurred_at == current.occurred_at
                      and prior.payload.get("effective_at") == current.occurred_at
                      and old.fact_ref in prior.evidence
                      and all(prior.payload.get(k) == reviewed.payload.get(k)
                              for k in set(prior.payload) | set(reviewed.payload) if k != "effective_at")):
                    # Correct only our exact legacy end-boundary assertion.
                    # Different WI, principal or evidence stays a real conflict.
                    reviewed = Receipt.make("attribution", None, reviewed.payload,
                        occurred_at=effective, observed_at=observed, producer=reviewed.producer,
                        entity_ref=reviewed.entity_ref, supersedes=prior.fact_ref,
                        evidence=tuple(sorted(set(reviewed.evidence) | set(prior.evidence))))
            result.append(reviewed)
        elif "work_kind" in item:
            result.append(non_wi_attribution(current, work_kind=item["work_kind"], observed_at=observed, evidence=evidence))
        # Identity and entrypoint remain separate facts; they cannot create a WI.
        from .receipt_sources import actor_receipt
        result.append(actor_receipt(current, node=item["node"], executor=item["agent"],
            entrypoint=item["entrypoint"], observed=observed, evidence=evidence + (old.fact_ref,),
            path="reviewed_activity"))
    return result


def host_non_wi_receipts(facts: list[Receipt], observed: str) -> list[Receipt]:
    """A reconciled Host dispatch explicitly without a WI is ordinary work.

    Reuse the exact existing native-prefix/conservation proof. Merely lacking
    a native WI, a Host, an inventory, or an execution is never sufficient.
    No prompt text, directory guess, current WI or user-specific default is used.

    A Relay dispatch never names the WI of a conversation turn, so this is only
    the fallback: each classification carries HOST_FALLBACK and the query
    applies it after a written, created or explored WI of the same request.
    An earlier classification of this producer without the marker is revised
    once to carry it.
    """
    from .receipts import HOST_FALLBACK, current_receipts
    from .events import parse_timestamp
    from .native_inventory import reconcile_host_inventory
    active, conflicts = current_receipts(facts)
    usages = [r for r in active if r.kind == 'usage']
    executions = {r.entity_ref: r for r in active if r.kind == 'execution'}
    inventories = [r for r in active if r.kind == 'coverage' and r.producer == 'pi_native_inventory.v1']
    by_entity = {r.entity_ref: r for r in active}
    links = {}
    accepted = reconcile_host_inventory(usages, inventories, list(executions.values()), execution_links=links)
    claims = defaultdict(list)
    for r in active + [r for group in conflicts.values() for r in group]:
        if r.kind == 'attribution' and r.payload.get('scope') != 'actor':
            claims[r.payload['target_ref']].append(r)
    contrary = {r.entity_ref for r in facts if r.kind == 'execution'
                and any(r.payload.get(k) is not None for k in ('wi', 'assignment_ref', 'responsible', 'delegator'))}
    host_witnesses = {}
    for row in usages:
        p = row.payload
        if (row.entity_ref in accepted and p.get('execution_ref')
                and all(p.get(k) is None for k in ('wi', 'assignment_ref', 'responsible', 'delegator'))):
            host_witnesses.setdefault(p['execution_ref'], []).append(row)
    result = []
    for target, linked in links.items():
        if len(linked) != 1:
            continue
        execution_ref = next(iter(linked)); execution = executions.get(execution_ref); row = by_entity.get(target)
        if execution is None or row is None or execution_ref in claims or execution_ref in contrary:
            continue
        p, dispatch = row.payload, execution.payload
        if (p.get('wi') is not None or p.get('assignment_ref') is not None
                or execution.producer != 'relay_host.v1' or not execution.evidence
                or dispatch['state'] == 'unknown' or not dispatch['expected_usage']
                or not dispatch.get('executor') or dispatch['runtime'] != p['runtime']
                or not execution.occurred_at or not row.occurred_at
                or parse_timestamp(execution.occurred_at, 'dispatch') > parse_timestamp(row.occurred_at, 'native request')):
            continue
        hosts = [host for host in host_witnesses.get(execution_ref, [])
                 if host.payload.get('executor') == dispatch['executor']]
        if not hosts:
            continue
        evidence = {execution.fact_ref}
        for host in hosts:
            evidence.update((host.fact_ref, by_entity[accepted[host.entity_ref]].fact_ref))
        existing = claims.get(target, [])
        own = [r for r in existing if r.producer == 'non_wi_activity.v1' and r.payload.get('work_kind') == 'none'
               and set(r.evidence) - {HOST_FALLBACK} == evidence | {row.fact_ref}]
        if len(existing) != len(own) or len(own) > 1:
            continue  # Another claim decides, or this producer's history forked.
        if own and HOST_FALLBACK in own[0].evidence:
            continue
        result.append(non_wi_attribution(row, work_kind='none', observed_at=observed,
                                         evidence=tuple(sorted(evidence | {HOST_FALLBACK})),
                                         supersedes=own[0].fact_ref if own else None))
    return result


def load_confirmations() -> Any:
    """The reviewed activity document, only from a regular file that is not a symlink."""
    if not CONFIRMATIONS.is_file() or CONFIRMATIONS.is_symlink():
        raise ReceiptError("reviewed activity source is unavailable")
    return json.loads(CONFIRMATIONS.read_bytes())


def collect_reviewed_activity(facts: list[Receipt], registry: Mapping[str, Any], observed: str) -> list[Receipt]:
    if FEATURE not in registry.get("required_receipt_features", []):
        return []  # All installed readers must support the new classification.
    reviewed = reviewed_activity_receipts(facts, load_confirmations(), observed)
    if "runtime_subcall.v1" in registry.get("required_receipt_features", []):
        from .runtime_subcalls import reviewed_subcalls
        reviewed.extend(reviewed_subcalls(facts, load_confirmations(), observed))
    return reviewed + host_non_wi_receipts(facts + reviewed, observed)


def reviewed_purpose_receipt(current: Receipt, prior: Receipt | None, item: Mapping[str, Any], observed: str) -> Receipt:
    """Record one reviewed request purpose, never execution authority.

    A native request or accepted continuation can establish a whole turn's
    purpose even when permission is acquired or retired inside that turn.
    The exact original usage remains available. A replaced weak inference,
    when present, remains in the version graph as well.
    """
    valid_prior = prior is None
    if prior is not None:
        p = prior.payload
        same_target = (prior.kind == 'attribution' and p.get('target_ref') == current.entity_ref
            and all(p.get(k) is None for k in ('wi','executor','responsible','delegator')))
        weak = (prior.producer == 'scl_host_session_binding.v1'
            and p.get('policy') == 'scl_host_session_binding.v1' and p.get('state') == 'ambiguous')
        # A finite, explicit later review may correct an erroneous ordinary-work
        # label. It must identify the exact previous assertion and original fact;
        # a new native task binding by itself cannot erase an independent review.
        ordinary = (prior.producer == 'non_wi_activity.v1'
            and p.get('policy') == 'non_wi_activity.v1' and p.get('state') == 'assigned'
            and p.get('work_kind') == 'none' and p.get('scope') is None
            and item.get('source_attribution_fact_ref') == prior.fact_ref
            and current.fact_ref in prior.evidence
            and item.get('review_basis') == 'native_lifecycle_request')
        valid_prior = same_target and (weak or ordinary or _native_request_prior(current,prior,item)
                                       or _native_session_prior(current,prior,item))
    if (current.kind != 'usage' or current.payload.get('wi') is not None or not valid_prior
            or item['review_basis'] not in {'native_user_request','recorded_continuation','native_lifecycle_request',
                                            'human_reviewed_session'}):
        raise ReceiptError('reviewed request purpose or prior inference is invalid')
    interval = [current.payload.get('interval_start') or current.occurred_at,
                current.payload.get('interval_end') or current.occurred_at]
    if not all(interval) or item['usage_interval'] != interval:
        raise ReceiptError('reviewed purpose usage interval differs')
    evidence = tuple(sorted(set(item['evidence_refs']) | ({prior.fact_ref} if prior else set()) | {current.fact_ref,
                         reference('reviewed_request_purpose', digest(item))}))
    identity = None if prior else ['reviewed_request_purpose', current.entity_ref, digest(item)]
    return Receipt.make('attribution', identity, {
        'target_ref':current.entity_ref,'wi':item['work_item_id'],'executor':None,
        'responsible':None,'delegator':None,'state':'assigned',
        'policy':'reviewed_activity.v1','effective_at':interval[0],
    }, entity_ref=prior.entity_ref if prior else None, supersedes=prior.fact_ref if prior else None, occurred_at=interval[0],
       observed_at=observed, producer='reviewed_activity.v1', evidence=evidence)
