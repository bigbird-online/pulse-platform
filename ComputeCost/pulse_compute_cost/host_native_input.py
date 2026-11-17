"""Join independently verified input provenance, never infer from time or bot names.

The Host proves the original input's exact dispatch; Pi proves that same input
body/tenant and each response's parent ancestry. Times only reject impossible
joins. All quantities remain the original native facts, not recovered totals.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import replace
from .events import parse_timestamp
from .receipts import Receipt, current_receipts


def authenticated_input_links(facts: list[Receipt]) -> tuple[dict[str, set[str]], set[str]]:
    active, conflicts = current_receipts(facts)
    by_ref = {r.entity_ref: r for r in active if r.entity_ref not in conflicts}
    bindings = defaultdict(set)
    declared_scopes = defaultdict(set)
    actors = defaultdict(set)
    origins = defaultdict(set)
    finals = set()
    # Index once. A growing native ledger must not trigger one full-ledger
    # scan for every historical input on every Stats refresh.
    # Receipt payloads are immutable encoded values. Keep only the specific
    # join indexes below; decoding every active assertion into another retained
    # object graph made production Stats exceed memory before querying a view.
    for row in active:
        p = row.payload
        if row.kind == 'usage' and row.producer == 'relay_pi.v1' and p.get('native_scope_ref'):
            declared_scopes[p.get('execution_ref')].add(p['native_scope_ref'])
        if row.kind == 'attribution' and p.get('scope') == 'actor':
            if p.get('executor'):
                actors[p['target_ref']].add(p['executor'])
            c = p['context']
            for name in ('node', 'entrypoint'):
                if c.get(name):
                    origins[(p['target_ref'], name)].add(c[name])
    for row in active:
        p = row.payload
        if (row.kind != 'attribution' or row.producer != 'registered_runtime_actor.v1'
                or p.get('scope') != 'actor' or not row.evidence):
            continue
        c = p['context']; target = by_ref.get(p['target_ref'])
        if (target is None or not c.get('input_ref') or not c.get('node')
                or c.get('entrypoint') not in {'feishu', 'telegram'}):
            continue
        # Explicit input provenance has no relation to a workspace basename.
        if any(len(origins[(target.entity_ref, name)]) != 1 for name in ('node', 'entrypoint')):
            continue
        key = (c['node'], c['entrypoint'], c['input_ref'])
        bindings[key].add(target.entity_ref)
        if target.kind == 'usage' and c.get('turn_final') is True:
            finals.add(target.entity_ref)
    links = defaultdict(set)
    for refs in bindings.values():
        # Reusing one authenticated input in multiple Host attempts is ambiguous,
        # even when timestamps could select a seemingly convenient winner.
        hosts = [by_ref[r] for r in refs if by_ref[r].kind == 'execution'
                 and by_ref[r].producer == 'relay_host.v1']
        if len(hosts) != 1:
            continue
        host = hosts[0]; h = host.payload
        if (h['runtime'] != 'pi' or h['state'] not in {'completed', 'failed', 'cancelled'}
                or not h.get('executor')
                or not host.occurred_at):
            continue
        low = parse_timestamp(host.occurred_at, 'Host start')
        high = parse_timestamp(host.observed_at, 'Host terminal')
        native = [by_ref[r] for r in refs if by_ref[r].kind == 'usage'
                  and by_ref[r].producer == 'pi_native.v1'
                  and by_ref[r].payload['granularity'] == 'request'
                  and by_ref[r].occurred_at
                  and low <= parse_timestamp(by_ref[r].occurred_at, 'native') <= high]
        scopes = {r.payload.get('native_scope_ref') for r in native}
        if not native or None in scopes or len(scopes) != 1:
            continue
        declared = declared_scopes[host.entity_ref]
        if declared and declared != scopes:
            continue
        for row in native:
            p = row.payload
            if (actors[row.entity_ref] - {h['executor']}
                    or actors[host.entity_ref] - {h['executor']}
                    or p['runtime'] != 'pi' or p.get('execution_ref') not in {None, host.entity_ref}
                    or p.get('assignment_ref') not in {None, h['assignment_ref']}
                    or any(p.get(k) is not None and p[k] != h.get(k)
                           for k in ('wi', 'executor', 'responsible', 'delegator'))):
                continue
            links[row.entity_ref].add(host.entity_ref)
    # One response must never become evidence for two attempts or nodes.
    return {ref: values for ref, values in links.items() if len(values) == 1}, finals


def reconcile_host_coverage(batch: list[Receipt], summaries: list[dict], observed: str,
                            current_sources: dict[str, list[Receipt]], prior: list[Receipt]) -> None:
    """Supplement the same Host denominator using this collection's valid inputs.

    No source mutation, synthetic usage, retired expectation, or stale snapshot
    is used. Independent missing executions and actual capture gaps stay visible.
    """
    from .receipt_sources import host_usage_coverage
    if any(s['kind'] == 'pi' and s['status'] in {'partial', 'unreachable'} for s in summaries):
        return
    active, _ = current_receipts(batch)
    fresh = [r for rows in current_sources.values() for r in rows]
    present, conflicts = current_receipts(fresh)
    previous = {r.entity_ref: r for r in prior}
    links, _ = authenticated_input_links(fresh)
    by_ref = {r.entity_ref: r for r in present}
    observed_executions = {next(iter(targets)) for ref, targets in links.items()
                          if all(v is not None for v in by_ref[ref].payload['tokens'].values())}
    for coverage in tuple(active):
        p = coverage.payload; detail = p.get('source_detail') or {}
        if (coverage.kind != 'coverage' or coverage.producer != 'registered_collector.v1'
                or detail.get('kind') != 'relay_host' or p['status'] != 'partial'
                or p['reason'] not in {None, 'host_usage_pending'}
                or detail.get('files_invalid') or detail.get('errors') or not p['watermark']):
            continue
        rows = [r for r in current_sources.get(p['source_ref'], []) if r.entity_ref not in conflicts]
        if any(r.kind == 'coverage' and r.payload['status'] in {'partial', 'unreachable'} for r in rows):
            continue
        expected, original, _, _ = host_usage_coverage(rows)
        executions = {r.entity_ref for r in rows if r.kind == 'execution' and r.payload['expected_usage']}
        reported = {r.payload.get('execution_ref') for r in rows if r.kind == 'usage'}
        count = len(executions & (reported | observed_executions))
        if expected != p['expected'] or original != p['observed'] or count <= original:
            continue
        status = 'complete' if count == expected else 'partial'
        reason = 'host_usage_reconciled_native' if status == 'complete' else p['reason']
        payload = {**p, 'observed': count, 'status': status, 'reason': reason}
        before = previous.get(coverage.entity_ref)
        proof = tuple(sorted(ref for ref, targets in links.items() if targets & executions))
        if before is not None and before.payload == payload:
            revised = replace(before, evidence=tuple(sorted(set(before.evidence) | set(proof))))
        else:
            revised = Receipt.make('coverage', None, payload,
                entity_ref=coverage.entity_ref, supersedes=before.fact_ref if before else None,
                occurred_at=None, observed_at=observed, producer=coverage.producer, evidence=proof)
        # The per-source value was provisional within this collection. Persist
        # one reconciled head, not an artificial partial/complete flip per tick.
        batch[:] = [r for r in batch if r.entity_ref != coverage.entity_ref]
        batch.append(revised)
        for summary in summaries:
            if summary['source_ref'] == p['source_ref'] and summary['kind'] == 'relay_host':
                summary.update(status=status, reason=reason)
