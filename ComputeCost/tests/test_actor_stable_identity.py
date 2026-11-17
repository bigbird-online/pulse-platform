"""An actor claim keeps one entity per target and is revised only on real change."""
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pulse_compute_cost.actor_identity import ActorHeads
from pulse_compute_cost.collaboration_query import _actor_projection
from pulse_compute_cost import receipt_sources
from pulse_compute_cost.receipt_sources import actor_receipt, collect_registered_sources, usage_receipt
from pulse_compute_cost.receipts import Receipt, current_receipts, reference
from pulse_compute_cost.source_registration import actor_candidate_registry
from pulse_compute_cost.tenant_context import FEATURE, SCHEMA, collect_tenant_context

from test_cc_actor_attribution import setup_source

AT = '2026-10-02T10:00:00Z'
LATER = '2026-10-02T10:00:01Z'


def actors(ledger):
    return [r for r in ledger.receipt_snapshot()['facts']
            if r.kind == 'attribution' and r.producer == 'registered_runtime_actor.v1']


def usage(identity='one', at=AT, interval=None):
    return usage_receipt('codex', identity, {'input_tokens': 10, 'output_tokens': 2}, at=at, observed=at,
                         model='openai/test', mapping='codex_cli.v1', interval=interval,
                         native_scope_ref=reference('native_session', 'scope-one'))


def old_identity(row):
    """The same claim under the replaced identity formula (9091e1e38d)."""
    p = row.payload
    identity = [p['target_ref'], p['executor'], p['context'], p['effective_at'], sorted(row.evidence)]
    if p.get('usage_fact_ref') is not None:
        identity.append(p['usage_fact_ref'])
    return Receipt.make('attribution', ['runtime_actor', identity], p, occurred_at=row.occurred_at,
                        observed_at=row.observed_at, producer=row.producer, evidence=row.evidence)


def test_identity_excludes_executor_effective_and_evidence():
    first = usage()
    later = usage(at=LATER)
    base = actor_receipt(first, node='mac', executor='pulse', observed=AT, evidence=(reference('e', 1),))
    for other in (actor_receipt(first, node='mac', executor='duke', observed=AT),
                  actor_receipt(later, node='mac', executor='pulse', observed=AT, evidence=(reference('e', 2),))):
        assert other.entity_ref == base.entity_ref
    for other in (actor_receipt(first, node='vultr', executor='pulse', observed=AT),
                  actor_receipt(first, node='mac', executor='pulse', entrypoint='cli', observed=AT),
                  actor_receipt(first, node='mac', executor='pulse', observed=AT, path='other')):
        assert other.entity_ref != base.entity_ref


def test_settle_skips_equal_content_and_revises_only_a_real_change():
    target = usage()
    head = actor_receipt(target, node='mac', executor='pulse', observed=AT, evidence=(reference('e', 1),))
    heads = {target.entity_ref: target, head.entity_ref: head}
    settler = ActorHeads(heads, [head])
    # Different evidence, re-parsed candidate time: anchored on the current version.
    candidate = usage(at=LATER)
    again = actor_receipt(candidate, node='mac', executor='pulse', observed=LATER, evidence=(reference('e', 2),))
    assert settler.settle_all([again], heads) == []
    changed = actor_receipt(candidate, node='mac', executor='duke', observed=LATER, evidence=(reference('e', 2),))
    [revision] = settler.settle_all([changed], heads)
    assert revision.entity_ref == head.entity_ref and revision.supersedes == head.fact_ref
    assert revision.payload['executor'] == 'duke' and revision.payload['effective_at'] == AT
    assert set(head.evidence) <= set(revision.evidence)
    assert settler.settle_all([changed], heads) == []


def test_settle_keeps_the_last_claim_of_an_entity_in_one_list():
    target = usage()
    rows = [actor_receipt(target, node='mac', executor=agent, observed=AT) for agent in ('a', 'b')]
    settled = ActorHeads({}, []).settle_all(rows)
    assert [r.payload['executor'] for r in settled] == ['b'] and settled[0].supersedes is None


def test_old_identity_head_of_the_same_claim_blocks_a_new_entity():
    target = usage()
    head = old_identity(actor_receipt(target, node='mac', executor='pulse', observed=AT,
                                      evidence=(reference('e', 3),)))
    claim = actor_receipt(target, node='mac', executor='pulse', observed=LATER, evidence=(reference('e', 3),))
    assert head.entity_ref != claim.entity_ref and head._payload_json == claim._payload_json
    assert ActorHeads({target.entity_ref: target, head.entity_ref: head}, [head]).settle_all([claim]) == []


def executor(target, heads):
    [row], _, _, _ = _actor_projection([target], [target, *heads], {})
    return row.payload['executor']


def source_claim(target, source, agent, observed=AT):
    return actor_receipt(target, node='mac', executor=agent, observed=observed,
                         evidence=(reference('source', [source, agent]),), path=['source_executor', source])


def test_two_sources_that_disagree_converge_when_one_corrects():
    target = usage()
    x, y = source_claim(target, 'x', 'pulse'), source_claim(target, 'y', 'duke')
    assert x.entity_ref != y.entity_ref and executor(target, [x, y]) == 'ambiguous'
    heads = {target.entity_ref: target, x.entity_ref: x, y.entity_ref: y}
    settler = ActorHeads(heads, [x, y])
    # Source x now agrees with y: equal content to y's head, yet x's own head differs.
    fixed = source_claim(target, 'x', 'duke', observed=LATER)
    assert fixed._payload_json == y._payload_json
    [revision] = settler.settle_all([fixed], heads)
    assert revision.entity_ref == x.entity_ref and revision.supersedes == x.fact_ref
    assert executor(target, [revision, y]) == 'duke'
    # The next collection reads the same input and appends nothing.
    assert settler.settle_all([fixed, source_claim(target, 'y', 'duke', observed=LATER)], heads) == []


def test_both_sources_settled_in_one_list_revise_only_the_contrary_head():
    target = usage()
    x, y = source_claim(target, 'x', 'pulse'), source_claim(target, 'y', 'duke')
    heads = {target.entity_ref: target, x.entity_ref: x, y.entity_ref: y}
    claims = [source_claim(target, 'y', 'duke', observed=LATER), source_claim(target, 'x', 'duke', observed=LATER)]
    [revision] = ActorHeads(heads, [x, y]).settle_all(claims, heads)
    assert revision.supersedes == x.fact_ref and executor(target, [revision, y]) == 'duke'


def test_old_identity_head_with_equal_content_cannot_hide_a_revision():
    target = usage()
    current = source_claim(target, 'x', 'pulse')
    legacy = old_identity(source_claim(target, 'x', 'duke'))
    assert executor(target, [current, legacy]) == 'ambiguous'
    heads = {target.entity_ref: target, current.entity_ref: current, legacy.entity_ref: legacy}
    settler = ActorHeads(heads, [current, legacy])
    fixed = source_claim(target, 'x', 'duke', observed=LATER)
    assert fixed._payload_json == legacy._payload_json and fixed.entity_ref != legacy.entity_ref
    [revision] = settler.settle_all([fixed], heads)
    assert revision.entity_ref == current.entity_ref and revision.supersedes == current.fact_ref
    assert executor(target, [revision, legacy]) == 'duke'
    assert settler.settle_all([fixed], heads) == []


def ledger_view(target, facts):
    active, conflicts = current_receipts([target, *facts])
    [row], _, _, _ = _actor_projection([target], active, conflicts)
    return row.payload['executor']


def collection(facts, claims):
    """One collection: settle claims against the current heads of `facts`."""
    active, conflicts = current_receipts(facts)
    heads = {row.entity_ref: row for row in active}
    return ActorHeads(heads, [*active, *(r for rows in conflicts.values() for r in rows)]).settle_all(claims, heads)


@pytest.mark.parametrize('order', [('x', 'y'), ('y', 'x')])
def test_sources_that_first_agree_keep_independent_heads(order):
    target = usage()
    claims = {source: source_claim(target, source, 'pulse') for source in order}
    first = collection([target], list(claims.values()))
    assert {r.entity_ref for r in first} == {claims['x'].entity_ref, claims['y'].entity_ref}
    facts = [target, *first]
    assert ledger_view(target, facts[1:]) == 'pulse'
    # Only x is read next time and now names duke; y's claim was never withdrawn.
    [revision] = collection(facts, [source_claim(target, 'x', 'duke', observed=LATER)])
    assert revision.supersedes == claims['x'].fact_ref
    facts.append(revision)
    assert ledger_view(target, facts[1:]) == 'ambiguous'
    assert collection(facts, [source_claim(target, 'x', 'duke', observed=LATER)]) == []


def test_old_identity_head_of_another_source_does_not_block():
    target = usage()
    legacy = old_identity(source_claim(target, 'y', 'pulse'))
    claim = source_claim(target, 'x', 'pulse', observed=LATER)
    assert legacy._payload_json == claim._payload_json
    [row] = collection([target, legacy], [claim])
    assert row.entity_ref == claim.entity_ref and row.supersedes is None
    [revision] = collection([target, legacy, row], [source_claim(target, 'x', 'duke', observed=LATER)])
    assert ledger_view(target, [legacy, row, revision]) == 'ambiguous'


def test_old_identity_history_of_unchanged_sources_adds_nothing():
    target = usage()
    claims = [source_claim(target, source, 'pulse') for source in ('x', 'y')]
    # The previous producer appended one head per evidence set on every reread.
    history = [old_identity(claim) for claim in claims]
    history.append(old_identity(source_claim(target, 'x', 'pulse', observed=LATER)))
    history.append(old_identity(actor_receipt(target, node='mac', executor='pulse', observed=AT,
                                              evidence=(reference('stale', 1),), path=['source_executor', 'x'])))
    assert len({r.entity_ref for r in history}) == 3
    assert collection([target, *history], claims) == []
    assert collection([target, *history], claims[::-1]) == []


def test_cc_reparse_with_a_new_message_line_adds_no_actor(tmp_path):
    ledger, registry, surfaces, path, row = setup_source(tmp_path)
    enrolled = actor_candidate_registry(registry, surfaces, site='ownermac')
    collect_registered_sources(enrolled, ledger)
    before = actors(ledger)
    assert len(before) == 2 and len({r.entity_ref for r in before}) == 2
    # The same message written again later: new evidence, a later candidate time.
    second = deepcopy(row); second['uuid'] = 'native-message-2'; second['timestamp'] = LATER
    path.write_text(json.dumps(row) + '\n' + json.dumps(second) + '\n')
    collect_registered_sources(enrolled, ledger)
    assert actors(ledger) == before
    assert collect_registered_sources(enrolled, ledger)['inserted'] == 0


def test_cc_history_under_the_old_identity_gets_no_new_fact(tmp_path, monkeypatch):
    ledger, registry, surfaces, _, _ = setup_source(tmp_path)
    enrolled = actor_candidate_registry(registry, surfaces, site='ownermac')
    # The previous producer named each claim by its emitted evidence; the
    # collector then adds source evidence to the stored head in place.
    stable = receipt_sources.actor_receipt
    monkeypatch.setattr(receipt_sources, 'actor_receipt', lambda *a, **k: old_identity(stable(*a, **k)))
    collect_registered_sources(enrolled, ledger)
    history = {r.fact_ref for r in actors(ledger)}
    assert len(history) == 2 and any(old_identity(r).entity_ref != r.entity_ref for r in actors(ledger))
    monkeypatch.undo()
    collect_registered_sources(enrolled, ledger)
    assert {r.fact_ref for r in actors(ledger)} == history
    assert collect_registered_sources(enrolled, ledger)['inserted'] == 0


def test_tenant_membership_names_only_a_ledger_usage_version(tmp_path):
    target = usage(interval=(AT, LATER), at=LATER)
    actor = actor_receipt(target, node='ownermac', executor='shared', entrypoint='codex_app', observed=LATER,
                          evidence=(reference('workspace_registry', 'fixture'),))
    path = tmp_path / 'tenant-registry.json'
    path.write_text(json.dumps({'schema': SCHEMA, 'bindings': [{
        'tenant_id': 'team-a', 'tenant_name': 'Team A', 'agent': 'shared', 'node': 'ownermac',
        'runtime': 'codex', 'native_scope_ref': target.payload['native_scope_ref'],
        'effective_from': AT, 'effective_to': '2026-10-03T00:00:00Z'}]}))
    path.chmod(0o600)
    registry = {'required_receipt_features': [FEATURE], 'tenant_registry_path': str(path)}
    facts = [target, actor]
    assert collect_tenant_context(facts, registry, LATER, ledger_refs={actor.fact_ref}) == []
    [row] = collect_tenant_context(facts, registry, LATER, ledger_refs={target.fact_ref, actor.fact_ref})
    assert row.payload['context']['tenant']['usage_fact_ref'] == target.fact_ref
    # A later usage version revises the same membership entity.
    revised = Receipt.make('usage', None, {**target.payload, 'final': True}, occurred_at=target.occurred_at,
                           observed_at=LATER, producer=target.producer, entity_ref=target.entity_ref,
                           supersedes=target.fact_ref)
    [again] = collect_tenant_context([target, revised, actor], registry, LATER,
                                     ledger_refs={target.fact_ref, revised.fact_ref, actor.fact_ref})
    assert again.entity_ref == row.entity_ref
    assert again.payload['context']['tenant']['usage_fact_ref'] == revised.fact_ref
    settler = ActorHeads({row.entity_ref: row}, [row])
    [revision] = settler.settle_all([again])
    assert revision.supersedes == row.fact_ref
