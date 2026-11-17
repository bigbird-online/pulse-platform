"""Physical Pi prefix inventory for reconciling Host stats, never another charge.

A complete parsed source prefix identifies the native requests. A Host summary
can be accounted for only in its own explicit native session and dispatch
interval, with exact four-class conservation and no competing dispatch claim.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from hashlib import sha256
import json
from .events import parse_timestamp
from .receipts import Receipt, reference, TOKEN_FIELDS


def pi_prefix_inventory(data: bytes, rows: list[Receipt], source_ref: str, observed: str, *, native_file_ref: str | None = None) -> Receipt:
    records=[json.loads(line) for line in data.splitlines() if line.strip()]
    if not records:
        raise ValueError("native prefix is empty")
    header=records[0]
    if header.get("type")!="session" or not header.get("id") or not data.endswith(b"\n"):
        raise ValueError("native prefix is incomplete")
    session=header["id"]
    # Native request inventory includes separately observed compaction and
    # branch-summary invocations. A tool-returned child aggregate is not a
    # disjoint request. Host membership is proved by exact counter conservation
    # below, never by inheriting a configured model for compaction.
    request_refs=set()
    for entry in records:
        activity=entry.get("type")
        if activity=="message":
            activity="assistant" if entry.get("message",{}).get("role")=="assistant" else None
        if activity in {"assistant","compaction","branch_summary"} and entry.get("id"):
            request_refs.add(reference("usage",[session,entry["id"],activity]))
    selected=[r for r in rows if r.kind=="usage" and r.entity_ref in request_refs]
    refs=sorted({r.entity_ref for r in selected})
    # Two enrolled files can retain one session id without sharing a prefix.
    # Keep their witnesses independent instead of reverting one mutable head
    # from the complete file to its earlier copy on every collection.
    identity = ["pi_native_prefix", source_ref, session]
    if native_file_ref is not None:
        identity.append(native_file_ref)
    return Receipt.make("coverage",identity,{
        "domain":"usage","wi":None,"source_ref":reference("native_session",["pi",session]),"status":"unknown",
        "expected":None,"observed":len(refs),"watermark":sha256(data).hexdigest(),
        "reason":"pi_native_prefix_inventory"},
        occurred_at=None,observed_at=observed,producer="pi_native_inventory.v1", evidence=tuple(refs))


def reconcile_host_inventory(rows: list[Receipt], inventories: list[Receipt], executions: list[Receipt], *, execution_links: dict[str, set[str]] | None = None, assignment_aliases: dict | None = None, input_links: dict[str, set[str]] | None = None, final_native_refs: set[str] | None = None) -> dict[str, str]:
    aliases = assignment_aliases or {}
    def canonical_assignment(payload, assignment):
        return aliases.get((assignment, payload.get("wi"), payload.get("executor")), assignment)
    by_ref={r.entity_ref:r for r in rows}
    payloads={r.entity_ref:r.payload for r in rows}
    by_execution={r.entity_ref:r.payload for r in executions}
    times={r.entity_ref:parse_timestamp(r.occurred_at,"native time") if r.occurred_at else None for r in rows}
    hosts_by_scope=defaultdict(list)
    for row in rows:
        value=payloads[row.entity_ref]
        if value["mapping"]=="relay_pi.v1" and value["granularity"]!="request":
            hosts_by_scope[value.get("native_scope_ref")].append(row)
    proposed: dict[str,tuple[str,set[str]]] = {}
    for inventory in inventories:
        if inventory.producer!="pi_native_inventory.v1":
            continue
        p=inventory.payload
        if p["reason"]!="pi_native_prefix_inventory" or p["observed"]!=len(inventory.evidence):
            continue
        scope=p["source_ref"]
        targets=[by_ref[ref] for ref in inventory.evidence if ref in by_ref]
        if len(targets)!=len(inventory.evidence):
            continue
        if any(payloads[r.entity_ref]["runtime"]!="pi" or payloads[r.entity_ref]["mapping"]!="pi_native.v1"
               or payloads[r.entity_ref]["granularity"]!="request" or payloads[r.entity_ref].get("native_scope_ref")!=scope for r in targets):
            continue
        for host in hosts_by_scope.get(scope, []):
            p=payloads[host.entity_ref]
            first,last=p["interval_start"],p["interval_end"]
            if not first or not last:
                continue
            missing_summary = (all(p['tokens'][k] is None for k in TOKEN_FIELDS)
                and not p['native_tokens'] and not p['covers'] and p.get('reported_total_tokens') is None)
            low,high=parse_timestamp(first,"Host start"),parse_timestamp(last,"Host end")
            selected=[r for r in targets if times[r.entity_ref] is not None and low<=times[r.entity_ref]<=high
                      and (p["model"] is None or p["model"]==payloads[r.entity_ref]["model"])
                      and (p["provider"] is None or p["provider"]==payloads[r.entity_ref]["provider"])]
            if not selected:
                continue
            ex=by_execution.get(p["execution_ref"])
            if missing_summary:
                # A complete native turn can replace only an empty Host placeholder,
                # never a contradictory count, model, session or assignment.
                latest = max(selected, key=lambda r: times[r.entity_ref])
                if (not ex or ex['state'] != 'completed'
                        or latest.entity_ref not in (final_native_refs or set())
                        or any((input_links or {}).get(r.entity_ref) != {p['execution_ref']} for r in selected)
                        or any(not payloads[r.entity_ref]['model'] or not payloads[r.entity_ref]['provider']
                               or any(payloads[r.entity_ref]['tokens'][k] is None for k in TOKEN_FIELDS) for r in selected)):
                    continue
            elif not all(all(payloads[r.entity_ref]['tokens'][k] is not None for r in selected)
                         and sum(payloads[r.entity_ref]['tokens'][k] for r in selected) == p['tokens'][k]
                         for k in TOKEN_FIELDS):
                continue
            if ex and (ex["runtime"] != p["runtime"] or any(
                    ex.get(k) is not None and p.get(k) is not None and ex[k] != p[k]
                    for k in ("wi", "executor", "responsible", "delegator"))):
                continue  # A contradictory Host/execution pair is not a witness.
            assignment=ex["assignment_ref"] if ex else None
            if any((payloads[r.entity_ref].get("wi") is not None and p.get("wi") is not None and payloads[r.entity_ref]["wi"]!=p["wi"])
                or (payloads[r.entity_ref].get("executor") is not None and p.get("executor") is not None and payloads[r.entity_ref]["executor"]!=p["executor"])
                or (payloads[r.entity_ref].get("assignment_ref") is not None and assignment is not None
                    and canonical_assignment(payloads[r.entity_ref], payloads[r.entity_ref]["assignment_ref"])
                        != canonical_assignment(p, assignment))
                for r in selected):
                continue
            refs={r.entity_ref for r in selected}
            prior=proposed.get(host.entity_ref)
            if prior and prior[1]!=refs:
                # Multiple distinct native witnesses must not select a winner.
                proposed[host.entity_ref]=(inventory.entity_ref,set())
            elif prior is None:
                proposed[host.entity_ref]=(inventory.entity_ref,refs)
    multiplicity=Counter(ref for _,refs in proposed.values() for ref in refs)
    accepted = {host:proof for host,(proof,refs) in proposed.items()
            if refs and all(multiplicity[ref]==1 for ref in refs)}
    if execution_links is not None:
        for host in accepted:
            execution_ref=payloads[host]["execution_ref"]
            if execution_ref in by_execution:
                for request_ref in proposed[host][1]:
                    execution_links.setdefault(request_ref,set()).add(execution_ref)
    return accepted


def resolved_session_inferences(facts: list[Receipt]) -> set[str]:
    """An exact Host dispatch resolves only a weak session-interval ambiguity.

    Establish physical membership before applying inferred task labels. A
    pending interval is not a contrary task assertion and must not invalidate
    the complete native prefix/counter witness. Explicit assignments, reviewed
    disputes and independent contradictory receipts remain untouched.
    """
    usages = [r for r in facts if r.kind == 'usage']
    executions = [r for r in facts if r.kind == 'execution']
    inventories = [r for r in facts if r.kind == 'coverage']
    from .host_native_input import authenticated_input_links
    links, finals = authenticated_input_links(facts)
    input_links = {key: set(value) for key, value in links.items()}
    from .pi_binding_alias import assignment_equivalences
    reconcile_host_inventory(usages, inventories, executions, execution_links=links,
                             assignment_aliases=assignment_equivalences(facts),
                             input_links=input_links, final_native_refs=finals)
    by_usage = {r.entity_ref: r for r in usages}
    by_execution = {r.entity_ref: r for r in executions}
    eligible = set()
    for target, candidates in links.items():
        if len(candidates) != 1:
            continue
        execution = by_execution[next(iter(candidates))]
        p = execution.payload
        usage = by_usage[target]
        boundary = usage.payload.get('interval_start') or usage.occurred_at
        if (execution.producer == 'relay_host.v1' and (p.get('assignment_ref') or execution.evidence)
                and p.get('wi') and p.get('executor') and p['state'] != 'unknown'
                and execution.occurred_at and boundary
                and parse_timestamp(execution.occurred_at, 'Host purpose') <= parse_timestamp(boundary, 'usage')):
            eligible.add(target)
    # A collector-validated session declaration proves purpose independently
    # of the time execution permission was acquired. Retain the weak interval
    # receipt; ignore it only while this exact positive evidence is current.
    for claim in facts:
        if claim.kind != 'attribution' or claim.producer != 'session_wi_declaration.v1':
            continue
        p = claim.payload
        if (claim.kind != 'attribution' or claim.producer != 'session_wi_declaration.v1'
                or p.get('policy') != 'session_wi_declaration.v1'
                or p.get('state') != 'assigned' or not p.get('wi')
                or p.get('scope') is not None):
            continue
        usage = by_usage.get(p.get('target_ref'))
        if usage is None or usage.payload.get('runtime') not in ('codex', 'cc'):
            continue
        scope = usage.payload.get('native_scope_ref')
        boundary = usage.payload.get('interval_start') or usage.occurred_at
        proof = reference('native_session_usage', [scope, usage.entity_ref])
        if scope and boundary and p.get('effective_at') == boundary and proof in claim.evidence:
            eligible.add(usage.entity_ref)
    return {r.fact_ref for r in facts if r.kind == 'attribution'
            and r.producer == 'scl_host_session_binding.v1'
            and r.payload.get('policy') == 'scl_host_session_binding.v1'
            and r.payload.get('state') == 'ambiguous'
            and r.payload.get('scope') != 'actor'
            and r.payload.get('wi') is None and r.payload.get('executor') is None
            and r.payload.get('target_ref') in eligible}
