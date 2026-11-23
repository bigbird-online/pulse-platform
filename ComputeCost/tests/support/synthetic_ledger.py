"""A seeded synthetic receipt ledger for the incremental-equivalence tests (WI754).

Each session is a CC native session under one Host execution with its task
claim: itemized requests with purpose and actor claims, a turn counter, a human row and
sometimes a usage revision, concurrent heads, a reviewed claim, a late claim,
a subcall into another session, a cross-session parent execution or a Codex
app request a standing non-WI rule matches. Public facts are source coverage
and WI deliveries with reviews. Shared `assignment_ref` and `coverage_ref`
values span sessions but are not link keys. `pi_turn` adds a Pi turn whose Host
execution and native requests meet only through their authenticated input or
a binding alias. Facts are returned in a valid
append order; `split` cuts them into batches at random, `write` appends the
batches to a private local ledger.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import actor_receipt, usage_receipt
from pulse_compute_cost.receipts import Receipt, reference

T0 = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
COUNTS = {'input_tokens': 3, 'output_tokens': 1, 'cache_read_input_tokens': 2, 'cache_creation_input_tokens': 0}
CODEX = {'input_tokens': 5, 'cached_input_tokens': 2, 'output_tokens': 1}
OBSERVED = '2026-11-20T00:00:00Z'


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace('+00:00', 'Z')


def claim(target: Receipt, wi: str | None, policy: str, *, when: str | None = None, tag: object = None) -> Receipt:
    when = when or target.payload.get('interval_start') or target.occurred_at
    return Receipt.make('attribution', ['claim', target.entity_ref, policy, tag], {
        'target_ref': target.entity_ref, 'wi': wi, 'executor': None, 'responsible': None, 'delegator': None,
        'state': 'assigned' if wi else 'unassigned', 'policy': policy, 'effective_at': when},
        occurred_at=when, observed_at=OBSERVED, producer=policy, evidence=(reference('proof', [target.entity_ref, tag]),))


def _session(rnd: random.Random, index: int, wi: str, scope_of: list[str], executions: list[Receipt],
             coverage_ref: str, late: list[Receipt], spacing: float = 30.0) -> list[Receipt]:
    rows: list[Receipt] = []
    start = index * spacing
    scope = reference('native_session', ['cc', 'synthetic', index])
    parent = executions[rnd.randrange(len(executions))].entity_ref if executions and rnd.random() < 0.2 else None
    execution = Receipt.make('execution', ['synthetic', index], {
        'wi': wi, 'executor': 'pulse', 'responsible': 'pulse', 'delegator': None, 'runtime': 'cc',
        'assignment_ref': reference('assignment', wi), 'parent_ref': parent, 'state': 'completed',
        'expected_usage': True}, occurred_at=at(start), observed_at=OBSERVED, producer='relay_host.v1')
    rows += [execution, claim(execution, wi, 'synthetic_assignment.v1')]
    requests = []
    for n in range(rnd.randint(1, 4)):
        when = at(start + 1 + n)
        request = usage_receipt('cc', ['message', index, n], COUNTS, at=when, observed=when, model='opus',
                                mapping='cc_message.v1', native_scope_ref=scope, execution_ref=execution.entity_ref,
                                coverage_ref=coverage_ref)
        requests.append(request)
        rows.append(request)
        rows.append(actor_receipt(request, node='ownermac', executor='pulse', entrypoint='cli', observed=OBSERVED,
                                  evidence=(reference('actor_proof', [index, n]),)))
        purpose = claim(request, wi, 'cc_native_request_purpose.v1')
        (late if rnd.random() < 0.15 else rows).append(purpose)
    end = at(start + 2 + len(requests))
    rows.append(usage_receipt('cc', ['result', index], COUNTS, at=end, observed=end, model='haiku',
                              granularity='turn_aggregate', interval=(at(start + 0.5), end), mapping='cc_sdk_result.v1',
                              native_scope_ref=scope, execution_ref=execution.entity_ref, coverage_ref=coverage_ref))
    rows.append(Receipt.make('human', ['synthetic', index], {
        'wi': wi, 'executor': 'pulse', 'responsible': 'pulse', 'delegator': None,
        'execution_ref': execution.entity_ref, 'origin': 'human', 'category': None, 'band': 'bounded',
        'focused_seconds': 60, 'method': 'synthetic.v1'}, occurred_at=at(start), observed_at=OBSERVED,
        producer='synthetic.v1'))
    first = requests[0]
    if rnd.random() < 0.3:  # A usage revision.
        revised = dict(first.payload, tokens=dict(first.payload['tokens'], output=7))
        rows.append(Receipt.make('usage', None, revised, occurred_at=first.occurred_at, observed_at=OBSERVED,
                                 producer=first.producer, entity_ref=first.entity_ref, supersedes=first.fact_ref))
    if rnd.random() < 0.2:  # Concurrent heads of one request.
        fork = dict(first.payload, tokens=dict(first.payload['tokens'], uncached_input=9))
        rows.append(Receipt.make('usage', None, fork, occurred_at=first.occurred_at, observed_at=OBSERVED,
                                 producer='cc_fork.v1', entity_ref=first.entity_ref))
    if rnd.random() < 0.3:  # A reviewed claim.
        rows.append(claim(requests[-1], wi, 'reviewed_activity.v1', tag='reviewed'))
    if scope_of and rnd.random() < 0.3:  # A subcall launched from an earlier session.
        child = requests[-1]
        rows.append(actor_receipt(child, node='ownermac', executor='pulse', entrypoint='cli', observed=OBSERVED,
                                  evidence=(reference('subcall_proof', index),), subcall={
            'parent_scope_ref': scope_of[rnd.randrange(len(scope_of))], 'child_scope_ref': scope,
            'call_ref': reference('call', index), 'parent_runtime': 'cc', 'child_runtime': 'cc',
            'basis': 'native_tool_result'}, path='subcall'))
    if rnd.random() < 0.2:  # A Codex app request no WI claims.
        when = at(start + 10)
        codex = usage_receipt('codex', ['codex', index], CODEX, at=when, observed=when, model='gpt',
                              mapping='codex_cli.v1', native_scope_ref=reference('native_session', ['codex', index]))
        rows += [codex, actor_receipt(codex, node='ownermac', executor='pulse', entrypoint='codex_app',
                                      observed=OBSERVED, evidence=(reference('codex_actor', index),))]
    scope_of.append(scope)
    executions.append(execution)
    return rows


PI = {'input': 3, 'output': 2, 'cacheRead': 7, 'cacheWrite': 0, 'totalTokens': 12}
PI_TURN = {'input_tokens': 6, 'output_tokens': 4, 'cache_read_input_tokens': 14, 'cache_creation_input_tokens': 0}


def pi_turn(name: str, start: float, *, alias: bool = False) -> list[Receipt]:
    """One Pi turn: a Host execution and two native requests under one authenticated input.

    Plain, nothing but that input joins the execution to the requests. With
    `alias`, the Host also reports the turn total and a prefix inventory lists
    the requests; they name another assignment than the execution, and only
    the binding alias claim proves the two assignments are one.
    """
    scope = reference('native_session', ['pi', name])
    active, original = reference('assignment', [name, 'active']), reference('assignment', [name, 'original'])
    who = {'wi': 'wi_20261102_009', 'executor': 'pulse', 'responsible': None, 'delegator': None}
    execution = Receipt.make('execution', ['pi-host', name], {
        **who, 'runtime': 'pi', 'assignment_ref': active, 'parent_ref': None, 'state': 'completed',
        'expected_usage': True}, occurred_at=at(start), observed_at=at(start + 5), producer='relay_host.v1')
    message = reference('input_metadata', name)

    def actor(target: Receipt, **kwargs) -> Receipt:
        return actor_receipt(target, node='tcloud', executor='pulse', entrypoint='feishu', observed=OBSERVED,
                             evidence=(message,), input_ref=message, **kwargs)

    rows = [execution, actor(execution)]
    requests = []
    for n in (1, 2):
        request = usage_receipt('pi', ['pi-native', name, n], PI, at=at(start + n), observed=OBSERVED, model='gpt',
                                provider='openai', mapping='pi_native.v1', native_scope_ref=scope,
                                common={**who, 'assignment_ref': original} if alias else None)
        requests.append(request)
        rows += [request, actor(request, turn_final=n == 2)]
    if alias:
        refs = tuple(sorted(r.entity_ref for r in requests))
        rows += [
            usage_receipt('pi', ['pi-host-usage', name], PI_TURN, at=at(start + 5), observed=OBSERVED, model='gpt',
                          provider='openai', mapping='relay_pi.v1', granularity='turn_aggregate', common=who,
                          interval=(at(start), at(start + 5)), native_scope_ref=scope,
                          execution_ref=execution.entity_ref),
            Receipt.make('coverage', ['pi-inventory', name], {
                'domain': 'usage', 'wi': None, 'source_ref': scope, 'status': 'unknown', 'expected': None,
                'observed': len(refs), 'watermark': 'w', 'reason': 'pi_native_prefix_inventory'}, occurred_at=None,
                observed_at=OBSERVED, producer='pi_native_inventory.v1', evidence=refs),
            Receipt.make('attribution', ['binding-alias', name], {
                **who, 'target_ref': active, 'assignment_ref': original, 'state': 'assigned',
                'policy': 'pi_canonical_binding_alias.v1', 'effective_at': None}, occurred_at=None,
                observed_at=OBSERVED, producer='pi_canonical_binding_alias.v1',
                evidence=(reference('binding_witness', name),))]
    return rows


def generate(seed: int, sessions: int = 12, *, spacing: float = 30.0) -> list[Receipt]:
    """`spacing` minutes between session starts; a large one spreads sessions over days and weeks."""
    rnd = random.Random(seed)
    wis = [f'wi_20261102_{n:03d}' for n in range(1, 4)]
    coverage_ref = reference('source', 'synthetic')
    rows: list[Receipt] = []
    late: list[Receipt] = []
    scopes: list[str] = []
    executions: list[Receipt] = []
    for index in range(sessions):
        rows += _session(rnd, index, wis[index % len(wis)], scopes, executions, coverage_ref, late, spacing)
        if late and rnd.random() < 0.5:  # A late claim arrives some sessions later.
            rows.append(late.pop(0))
    rows += late
    for round_ in range(3):  # Source coverage revised every round.
        prior = next((r for r in reversed(rows) if r.kind == 'coverage'), None)
        rows.append(Receipt.make('coverage', ['synthetic', 'source'], {
            'domain': 'usage', 'wi': None, 'source_ref': coverage_ref, 'status': 'unknown', 'expected': None,
            'observed': round_, 'watermark': str(round_), 'reason': None}, occurred_at=None,
            observed_at=OBSERVED, producer='registered_collector.v1', entity_ref=prior.entity_ref if prior else None,
            supersedes=prior.fact_ref if prior else None))
    for wi in wis:
        delivery = Receipt.make('delivery', wi, {
            'wi': wi, 'responsible': 'pulse', 'candidate_ref': reference('candidate', wi),
            'artifact_ref': reference('artifact', wi), 'acceptance_ref': reference('acceptance', wi),
            'status': 'accepted', 'accepted_at': at(sessions * spacing)}, occurred_at=at(sessions * spacing),
            observed_at=OBSERVED, producer='synthetic.v1')
        rows += [delivery, Receipt.make('review', [wi, 'review'], {
            'wi': wi, 'delivery_ref': delivery.entity_ref, 'candidate_ref': delivery.payload['candidate_ref'],
            'reviewer': 'human', 'decision': 'approved', 'first_pass': True, 'rework_rounds': 0,
            'post_approval_corrections': 0, 'rubric': None, 'score': None, 'verification': 'human_review',
            'critical': []}, occurred_at=at(sessions * spacing), observed_at=OBSERVED, producer='synthetic.v1')]
    return rows


def split(rows: list[Receipt], rnd: random.Random) -> list[list[Receipt]]:
    """Cut the append order into non-empty batches at random positions."""
    cuts = sorted(rnd.sample(range(1, len(rows)), rnd.randint(0, min(len(rows) - 1, 12))))
    return [rows[a:b] for a, b in zip([0, *cuts], [*cuts, len(rows)])]


def write(root, batches: list[list[Receipt]]) -> UsageLedger:
    clock = [T0]

    def tick() -> datetime:
        clock[0] += timedelta(seconds=1)
        return clock[0]

    ledger = UsageLedger(root, clock=tick)
    for batch in batches:
        result = ledger.ingest_receipts(batch)
        assert result['inserted'] == len(batch), result
    return ledger
