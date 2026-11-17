"""Repeated actor evidence is not a transfer; unknown and contrary claims remain."""
import pytest
from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.receipt_sources import actor_receipt as build_actor_receipt
from pulse_compute_cost.collaboration_query import _request_observation_projection, _actor_projection
from test_cc_stream_reconciliation import execution, usage


def actor_receipt(target, context, observed, evidence):
    return build_actor_receipt(target, **context, observed=observed,
                              evidence=tuple(reference('test_evidence', x) for x in evidence))


def row(at, *, host=False, start='2026-01-01T01:00:00Z'):
    original = usage('same-message', execution('host'), output=3, final=True)
    p = original.payload
    p['native_scope_ref'] = reference('native', 'session')
    if not host:
        p.update(execution_ref=None, coverage_ref=None, executor=None, interval_start=None, interval_end=None)
    else:
        p.update(interval_start=start, interval_end=at)
    return Receipt.make('usage', None, p, entity_ref=original.entity_ref,
                        occurred_at=at, observed_at=at, producer='cc_message.v1')


@pytest.mark.parametrize('reverse', [False, True])
def test_repeated_same_actor_inside_interval_is_not_a_change(reverse):
    first = row('2026-01-01T01:00:05Z'); last = row('2026-01-01T01:00:07Z')
    target = _request_observation_projection([], {first.entity_ref:[first,last]})[0][0]
    context = dict(executor='pulse',node='ownermac',entrypoint='telegram')
    early = actor_receipt(first, context, '2026-01-01T02:00:00Z', ('first',))
    middle = actor_receipt(row('2026-01-01T01:00:06Z'), context, '2026-01-01T02:00:00Z', ('middle',))
    projected, contexts, _, conflicts = _actor_projection([target], [middle,early] if reverse else [early,middle], {})
    assert not conflicts
    assert projected[0].payload['executor'] == 'pulse'
    assert contexts[target.entity_ref]['entrypoint'] == 'telegram'


@pytest.mark.parametrize('earlier', [True, False])
def test_late_actor_never_backfills_or_overwrites_another_actor(earlier):
    first = row('2026-01-01T01:00:05Z'); last = row('2026-01-01T01:00:07Z')
    target = _request_observation_projection([], {first.entity_ref:[first,last]})[0][0]
    middle = actor_receipt(row('2026-01-01T01:00:06Z'), dict(executor='duke',node='ownermac',entrypoint='telegram'), '2026-01-01T02:00:00Z', ('late',))
    early = actor_receipt(first, dict(executor='pulse',node='ownermac',entrypoint='telegram'), '2026-01-01T02:00:00Z', ('early',))
    projected, _, _, conflicts = _actor_projection([target], [early,middle] if earlier else [middle], {})
    assert target.entity_ref in conflicts
    assert projected[0].payload['executor'] == 'ambiguous'


@pytest.mark.parametrize('at', ['2026-01-01T01:00:06Z','2026-01-01T01:00:07Z'])
def test_direct_native_node_describes_the_message_not_earlier_dispatch(at):
    target = row('2026-01-01T01:00:07Z', host=True)
    location = actor_receipt(row(at), dict(node='ownermac'), '2026-01-01T02:00:00Z', ('native-source',))
    projected, contexts, evidence, conflicts = _actor_projection([target], [location], {})
    assert contexts[target.entity_ref]['node'] == 'ownermac'
    assert contexts[target.entity_ref]['entrypoint'] is None
    assert projected[0] == target
    assert conflicts == {}
    assert location.fact_ref in evidence[target.entity_ref]


@pytest.mark.parametrize('reverse', [False,True])
def test_direct_native_location_keeps_two_real_nodes_ambiguous(reverse):
    target = row('2026-01-01T01:00:07Z', host=True)
    left = actor_receipt(row('2026-01-01T01:00:05Z'), dict(node='ownermac'), '2026-01-01T02:00:00Z', ('left',))
    right = actor_receipt(row('2026-01-01T01:00:06Z'), dict(node='vultr'), '2026-01-01T02:00:00Z', ('right',))
    _, contexts, _, conflicts = _actor_projection([target], [right,left] if reverse else [left,right], {})
    assert contexts[target.entity_ref]['node'] == 'ambiguous'
    assert conflicts[target.entity_ref] == ['node']


@pytest.mark.parametrize('case', ['after_end','other_target','foreign_producer','wrong_policy','other_runtime','execution_target','aggregate'])
def test_native_location_never_uses_unproven_or_future_context(case):
    target = row('2026-01-01T01:00:07Z', host=True)
    context_target = row('2026-01-01T01:00:08Z' if case=='after_end' else '2026-01-01T01:00:06Z')
    if case == 'other_runtime':
        target = Receipt.make('usage',None,{**target.payload,'runtime':'pi','mapping':'pi_native.v1'},
            entity_ref=target.entity_ref,occurred_at=target.occurred_at,observed_at=target.observed_at,producer='pi_native.v1')
    if case == 'aggregate':
        target = Receipt.make('usage',None,{**target.payload,'granularity':'turn_aggregate'},
            entity_ref=target.entity_ref,occurred_at=target.occurred_at,observed_at=target.observed_at,producer=target.producer)
    location = actor_receipt(context_target, dict(node='ownermac'), '2026-01-01T02:00:00Z', ('context',))
    payload = location.payload
    if case == 'other_target': payload['target_ref'] = reference('other','message')
    if case == 'execution_target': payload['target_ref'] = target.payload['execution_ref']
    if case == 'wrong_policy':
        from pulse_compute_cost.receipts import ReceiptError
        with pytest.raises(ReceiptError, match='actor evidence cannot grant task authority'):
            Receipt.make('attribution',None,{**payload,'policy':'other.v1'},
                entity_ref=location.entity_ref,occurred_at=location.occurred_at,
                observed_at=location.observed_at,producer=location.producer,evidence=location.evidence)
        return
    location = Receipt.make('attribution',None,payload,entity_ref=location.entity_ref,
        occurred_at=location.occurred_at,observed_at=location.observed_at,
        producer='other.v1' if case=='foreign_producer' else location.producer,evidence=location.evidence)
    _, contexts, _, _ = _actor_projection([target],[location],{})
    assert contexts[target.entity_ref]['node'] != 'ownermac'


def test_native_node_does_not_backdate_late_executor_or_entrypoint():
    target = row('2026-01-01T01:00:07Z',host=True)
    location = actor_receipt(row('2026-01-01T01:00:06Z'),
        dict(node='ownermac',executor='duke',entrypoint='telegram'),'2026-01-01T02:00:00Z',('late',))
    projected, contexts, _, conflicts = _actor_projection([target],[location],{})
    assert contexts[target.entity_ref]['node']=='ownermac'
    assert projected[0].payload['executor']=='ambiguous'
    assert contexts[target.entity_ref]['entrypoint']=='ambiguous'
    assert set(conflicts[target.entity_ref])=={'executor','entrypoint'}


def test_late_native_node_query_keeps_fact_history_usage_and_price(tmp_path):
    from pulse_compute_cost.ledger import UsageLedger
    from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
    from test_cc_native_dedupe import execution,host_message,native,END
    from test_collaboration_receipts import CATALOG
    host=execution();stream=host_message('m1',host,'2026-09-13T01:01:02Z',8)
    settled=native('m1','2026-09-13T01:01:01Z',40)
    ledger=UsageLedger(tmp_path/'ledger');ledger.ingest_receipts([host,stream,settled])
    old=ledger.receipt_snapshot();q=lambda mark:CollaborationQuery(scope='activity',from_date='2026-09-13',to_date='2026-09-14',timezone='UTC',group_by=('agent','wi','node'),watermark=mark)
    before=query_collaboration(ledger,q(old['watermark']),catalog=CATALOG,catalog_observed_at=END)
    location=build_actor_receipt(settled,node='ownermac',observed=END,evidence=(reference('source','enrolled'),))
    ledger.ingest_receipts([location]);current=ledger.receipt_snapshot()
    after=query_collaboration(ledger,q(current['watermark']),catalog=CATALOG,catalog_observed_at=END)
    counted = [g for g in after['groups'] if g['usage']['event_count']]
    assert len(counted)==1 and counted[0]['key']['node']=='ownermac'
    assert {k:v for k,v in after['summary']['usage'].items() if k!='attribution_refs'} == {k:v for k,v in before['summary']['usage'].items() if k!='attribution_refs'}
    assert location.fact_ref in after['summary']['usage']['attribution_refs']
    assert before['summary']['usage']['attribution_refs']==[]
    assert after['summary']['cost']==before['summary']['cost']
    assert ledger.receipt_snapshot()['sha256']==current['sha256']
    assert ledger.receipt_snapshot(watermark=old['watermark'])['sha256']==old['sha256']
    assert query_collaboration(ledger,q(old['watermark']),catalog=CATALOG,catalog_observed_at=END)==before
