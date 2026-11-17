"""An operator's standing no-WI decision classifies only its own unclaimed usage."""
from pulse_compute_cost.receipt_sources import actor_receipt, usage_receipt
from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.standing_non_wi import load_rules, standing_non_wi_receipts, RULES

AT = '2026-10-23T01:00:00Z'
DECISION = reference('human', 'decision')
RULE = {'agent': 'pulse', 'node': 'mac', 'runtime': 'codex', 'entrypoint': 'codex_app', 'decision_ref': DECISION}
COUNTS = {'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}


def usage(identity, runtime='codex'):
    return usage_receipt(runtime, [identity], COUNTS, at=AT, observed=AT, model='test', final=True,
                         native_scope_ref=reference('native_session', [runtime, 'session']))


def facts(agent='pulse', entrypoint='codex_app'):
    row = usage('turn')
    return row, [row, actor_receipt(row, node='mac', executor=agent, entrypoint=entrypoint, observed=AT,
                                    evidence=(reference('native', 'origin'),))]


def test_unclaimed_matching_usage_is_non_wi_work_once():
    row, rows = facts()
    made = standing_non_wi_receipts(rows, AT, [RULE])
    assert [(r.payload['target_ref'], r.payload['work_kind'], r.payload['wi']) for r in made] == [(row.entity_ref, 'none', None)]
    assert DECISION in made[0].evidence
    assert standing_non_wi_receipts(rows + made, AT, [RULE]) == []  # Already classified.


def test_a_declaration_other_agent_or_entrypoint_is_left_alone():
    row, rows = facts()
    declared = Receipt.make('attribution', 'declared', {'target_ref': row.entity_ref, 'wi': 'wi_20261023_001',
        'executor': None, 'responsible': None, 'delegator': None, 'state': 'assigned',
        'policy': 'codex_request_purpose.v1', 'effective_at': AT},
        occurred_at=AT, observed_at=AT, producer='codex_request_purpose.v1', evidence=(reference('proof', 'x'),))
    assert standing_non_wi_receipts(rows + [declared], AT, [RULE]) == []
    assert standing_non_wi_receipts(facts(agent='aki')[1], AT, [RULE]) == []
    assert standing_non_wi_receipts(facts(entrypoint='cli')[1], AT, [RULE]) == []
    assert standing_non_wi_receipts(facts()[1], AT, [dict(RULE, node='other')]) == []


def test_the_shipped_rules_are_valid():
    assert {(r['agent'], r['node'], r['runtime'], r['entrypoint']) for r in load_rules(RULES)} == {('pulse', 'ownermac', 'codex', 'codex_app')}
