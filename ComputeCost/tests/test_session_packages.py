"""Session-package link keys: each kind's nodes; source and assignment refs never link."""
from pulse_compute_cost.receipt_sources import actor_receipt, usage_receipt
from pulse_compute_cost.receipts import FIELDS, Receipt, reference
from pulse_compute_cost.session_packages import LINKED_KINDS, PUBLIC_KINDS, input_node, is_shared, link_nodes, node
from pulse_compute_cost.stage_facts import STAGE_FIELDS

from support.synthetic_ledger import COUNTS, OBSERVED, claim, pi_turn

AT = '2026-11-02T01:00:00Z'
SCOPE, PARENT_SCOPE = reference('native_session', 'child'), reference('native_session', 'parent')
SOURCE = reference('source', 'shared')


def execution(name='exec', parent=None):
    return Receipt.make('execution', name, {
        'wi': 'wi_20261102_001', 'executor': 'pulse', 'responsible': 'pulse', 'delegator': None, 'runtime': 'cc',
        'assignment_ref': reference('assignment', 'wi_20261102_001'), 'parent_ref': parent, 'state': 'completed',
        'expected_usage': True}, occurred_at=AT, observed_at=OBSERVED, producer='relay_host.v1')


def usage(name='request', **kwargs):
    return usage_receipt('cc', name, COUNTS, at=AT, observed=AT, model='opus', mapping='cc_message.v1',
                         coverage_ref=SOURCE, **kwargs)


def nodes(*refs):
    return tuple(sorted({node(ref) for ref in refs}))


def test_usage_links_itself_its_session_execution_and_covered_requests():
    e = execution()
    item = usage(native_scope_ref=SCOPE, execution_ref=e.entity_ref)
    aggregate = usage('aggregate', granularity='turn_aggregate', covers=[item.entity_ref])
    assert link_nodes(item) == nodes(item.entity_ref, SCOPE, e.entity_ref)
    assert link_nodes(aggregate) == nodes(aggregate.entity_ref, item.entity_ref)


def test_execution_human_and_claims_link_their_targets():
    parent = execution('parent')
    child = execution('child', parent=parent.entity_ref)
    assert link_nodes(parent) == nodes(parent.entity_ref)
    assert link_nodes(child) == nodes(child.entity_ref, parent.entity_ref)
    human = Receipt.make('human', 'h', {'wi': None, 'executor': None, 'responsible': None, 'delegator': None,
        'execution_ref': child.entity_ref, 'origin': 'human', 'category': None, 'band': None,
        'focused_seconds': None, 'method': None}, occurred_at=AT, observed_at=OBSERVED, producer='x.v1')
    assert link_nodes(human) == nodes(human.entity_ref, child.entity_ref)
    request = usage(native_scope_ref=SCOPE)
    task = claim(request, 'wi_20261102_001', 'cc_native_request_purpose.v1')
    assert link_nodes(task) == nodes(task.entity_ref, request.entity_ref)
    subcall = actor_receipt(request, node='ownermac', executor='pulse', observed=OBSERVED, evidence=(SOURCE,),
        subcall={'parent_scope_ref': PARENT_SCOPE, 'child_scope_ref': SCOPE, 'call_ref': reference('call', 1),
                 'parent_runtime': 'cc', 'child_runtime': 'cc', 'basis': 'native_tool_result'}, path='subcall')
    assert link_nodes(subcall) == nodes(subcall.entity_ref, request.entity_ref, PARENT_SCOPE)


def test_stage_link_and_pi_inventory_link_their_listed_refs():
    targets = [reference('usage', 'a'), reference('usage', 'b')]
    link = Receipt.make('stage_link', 'l', {'wi': 'wi_20261102_001', 'context_ref': reference('context', 1),
        'target_refs': targets, 'state': 'assigned', 'basis': 'declared', 'epistemic': 'agent_judgment'},
        occurred_at=AT, observed_at=OBSERVED, producer='stage.v1', evidence=(SOURCE,))
    assert link_nodes(link) == nodes(link.entity_ref, *targets)

    def coverage(producer, evidence):
        return Receipt.make('coverage', [producer, evidence], {'domain': 'usage', 'wi': None, 'source_ref': SCOPE,
            'status': 'unknown', 'expected': None, 'observed': len(evidence), 'watermark': 'w',
            'reason': 'pi_native_prefix_inventory'}, occurred_at=None, observed_at=OBSERVED, producer=producer,
            evidence=tuple(evidence))
    inventory = coverage('pi_native_inventory.v1', targets)
    assert link_nodes(inventory) == nodes(inventory.entity_ref, *targets)
    history = coverage('pi_parent_history.v1', targets[:1])
    assert link_nodes(history) == nodes(history.entity_ref, targets[0])
    assert link_nodes(coverage('registered_collector.v1', targets)) == ()
    assert link_nodes(coverage('pi_native_inventory.v1', [])) == ()


def test_source_and_assignment_refs_never_link():
    first, second = execution('one'), execution('two')
    assert first.payload['assignment_ref'] == second.payload['assignment_ref']
    assert not set(link_nodes(first)) & set(link_nodes(second))
    a, b = usage('a'), usage('b')
    assert a.payload['coverage_ref'] == b.payload['coverage_ref'] == SOURCE
    assert not set(link_nodes(a)) & set(link_nodes(b))
    assert node(SOURCE) not in link_nodes(a)


def test_actor_claims_of_one_authenticated_input_link_through_it():
    execution, host_actor, request, request_actor = pi_turn('one', 0)[:4]
    assert not set(link_nodes(execution)) & set(link_nodes(request))
    shared = set(link_nodes(host_actor)) & set(link_nodes(request_actor))
    assert shared == {input_node(host_actor.payload['context'])}
    context = host_actor.payload['context']
    for change in ({'node': 'vultr'}, {'entrypoint': 'telegram'}, {'input_ref': reference('input_metadata', 'two')}):
        assert input_node({**context, **change}) not in {None, *shared}
    # Without a named input the claim links its target only; a task claim never reads its context.
    for missing in ('node', 'entrypoint', 'input_ref'):
        assert input_node({**context, missing: None}) is None
    plain = actor_receipt(request, node='tcloud', executor='pulse', entrypoint='feishu', observed=OBSERVED,
                          evidence=(SOURCE,))
    assert link_nodes(plain) == nodes(plain.entity_ref, request.entity_ref)


def test_a_binding_alias_is_a_shared_public_fact():
    turn = pi_turn('one', 0, alias=True)
    alias = turn[-1]
    assert alias.producer == 'pi_canonical_binding_alias.v1' and is_shared(alias) and link_nodes(alias) == ()
    assert not any(is_shared(row) for row in turn[:-1])
    # Its two assignments stay off the graph: the execution and requests naming them do not meet there.
    assert node(alias.payload['target_ref']) not in {n for row in turn for n in link_nodes(row)}
    assert node(alias.payload['assignment_ref']) not in {n for row in turn for n in link_nodes(row)}


def test_every_kind_is_either_linked_or_public():
    assert LINKED_KINDS | PUBLIC_KINDS == set(FIELDS) and not LINKED_KINDS & PUBLIC_KINDS
    assert {'coverage', 'delivery', 'review', 'value', 'human_summary', 'native_input'} <= PUBLIC_KINDS
    assert set(STAGE_FIELDS) - {'stage_link'} <= PUBLIC_KINDS
