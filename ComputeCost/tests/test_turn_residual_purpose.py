"""A CC model counter without its own requests inherits its turns' unanimous WI."""
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.turn_residual_purpose import POLICY, turn_residual_receipts

SCOPE = reference('native_session', ['cc', 'session'])
COUNTS = {'input_tokens': 3, 'output_tokens': 1, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
OBSERVED = '2026-10-23T05:00:00Z'


def counter(start='2026-10-23T03:57:00Z', end='2026-10-23T04:01:00Z', scope=SCOPE):
    return usage_receipt('cc', ['result', start, end], COUNTS, at=end, observed=end, model='haiku',
                         granularity='turn_aggregate', interval=(start, end), mapping='cc_sdk_result.v1',
                         native_scope_ref=scope)


def request(at, scope=SCOPE):
    return usage_receipt('cc', ['message', at], COUNTS, at=at, observed=at, model='opus',
                         mapping='cc_message.v1', native_scope_ref=scope)


def claim(row, wi, policy='cc_native_request_purpose.v1', state='assigned'):
    return Receipt.make('attribution', ['claim', row.entity_ref, policy], {
        'target_ref': row.entity_ref, 'wi': wi if state == 'assigned' else None, 'executor': None,
        'responsible': None, 'delegator': None, 'state': state, 'policy': policy,
        'effective_at': row.occurred_at}, occurred_at=row.occurred_at, observed_at=OBSERVED, producer=policy,
        evidence=(reference('proof', [row.entity_ref]),))


def test_counter_takes_the_wi_all_its_requests_agree_on():
    c, r1, r2 = counter(), request('2026-10-23T03:58:00Z'), request('2026-10-23T04:00:00Z')
    facts = [c, r1, r2, claim(r1, 'wi_20261023_408'), claim(r2, 'wi_20261023_408', policy='reviewed_activity.v1')]
    made = turn_residual_receipts(facts, OBSERVED)
    assert [(r.payload['target_ref'], r.payload['wi'], r.payload['policy']) for r in made] == [
        (c.entity_ref, 'wi_20261023_408', POLICY)]
    assert c.fact_ref in made[0].evidence
    assert turn_residual_receipts(facts + made, OBSERVED) == []


def test_disagreeing_unassigned_or_absent_requests_leave_the_counter_alone():
    c, r1, r2 = counter(), request('2026-10-23T03:58:00Z'), request('2026-10-23T04:00:00Z')
    assert turn_residual_receipts([c, r1, r2, claim(r1, 'wi_20261023_001'), claim(r2, 'wi_20261023_002')], OBSERVED) == []
    assert turn_residual_receipts([c, r1, r2, claim(r1, 'wi_20261023_001'), claim(r2, None, state='unassigned')], OBSERVED) == []
    assert turn_residual_receipts([c, r1, claim(r1, 'wi_20261023_001'), r2], OBSERVED) == []
    outside = request('2026-10-23T04:30:00Z')
    assert turn_residual_receipts([c, outside, claim(outside, 'wi_20261023_001')], OBSERVED) == []
    other = request('2026-10-23T03:58:00Z', scope=reference('native_session', ['cc', 'other']))
    assert turn_residual_receipts([c, other, claim(other, 'wi_20261023_001')], OBSERVED) == []


def test_another_primary_claim_wins_and_a_supplement_does_not():
    c, r1 = counter(), request('2026-10-23T03:58:00Z')
    base = [c, r1, claim(r1, 'wi_20261023_001')]
    assert turn_residual_receipts(base + [claim(c, 'wi_20261023_002', policy='reviewed_activity.v1')], OBSERVED) == []
    supplement = claim(c, 'wi_20261023_002', policy='holder_supplement.v1')
    assert [r.payload['wi'] for r in turn_residual_receipts(base + [supplement], OBSERVED)] == ['wi_20261023_001']
