"""Same query/store for pre-WI and WI continuation; synthetic evidence, not UAT.

High-risk query slice: exact context, immutable cutoff, retained correction
history and no session/cohort expansion. No source collection or authority grant.
"""
from dataclasses import replace

import pytest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import ReceiptError, reference
from test_stage_facts import (ledger, fact, goal, contribution, link, query, usage,
                             CONTEXT, WI, OTHER, AT, LATER, OBJECT, VERSION, ACTIVITY)

OTHER_CONTEXT = reference('context', 'other-subject-same-session')


def other(row):
    return fact(row.kind, ['other', row.entity_ref], {**row.payload, 'context_ref': OTHER_CONTEXT})


def unknown_link(rows, **kwargs):
    return link(rows, wi=None, state='unknown', **kwargs)


def test_context_reads_only_exact_facts_and_linked_usage_before_wi(ledger):
    g, o = goal(), contribution()
    u = usage(wi=None, identity='one', at=AT)
    unrelated = usage(wi=None, identity='two', at=AT)
    ledger.ingest_receipts([g, o, u, unrelated, other(g), other(o), unknown_link([u])])
    result = query(ledger, context_ref=CONTEXT)
    assert result['query']['context_ref'] == CONTEXT
    assert [r['entity_ref'] for r in result['stages']['goals']] == [g.entity_ref]
    assert [r['entity_ref'] for r in result['stages']['outputs']['items']] == [o.entity_ref]
    assert result['summary']['usage']['known_total_tokens'] == 100
    assert result['summary']['usage']['event_count'] == 1
    assert result['summary']['work_items'] == []
    assert result['stages']['execution_authority'] is False
    assert result['summary']['cost']['machine_cash_usd'] is None
    assert query(ledger)['summary']['usage']['known_total_tokens'] == 200


def test_context_does_not_infer_usage_from_same_session_or_same_wi(ledger):
    g = goal()
    u, another = usage(wi=WI, identity='linked', at=AT), usage(wi=WI, identity='same-wi', at=AT)
    ledger.ingest_receipts([g, u, another, link([g, u])])
    result = query(ledger, context_ref=CONTEXT, work_items=(WI,))
    assert result['summary']['usage']['known_total_tokens'] == 100
    assert query(ledger, work_items=(WI,))['summary']['usage']['known_total_tokens'] == 200
    assert query(ledger, context_ref=CONTEXT, work_items=(OTHER,))['stages']['goals'] == []


def test_context_late_wi_link_and_reopen_preserve_old_watermark(ledger):
    g, u = goal(), usage(wi=None, identity='pre-wi', at=AT)
    initial = unknown_link([g, u])
    ledger.ingest_receipts([g, u, initial])
    before = query(ledger, context_ref=CONTEXT, watermark=1)
    ledger.ingest_receipts([link([g, u], supersedes=initial.fact_ref)])
    reopened = UsageLedger(ledger.root)
    after = query(reopened, context_ref=CONTEXT)
    assert after['summary']['usage']['known_total_tokens'] == 100
    assert after['stages']['goals'][0]['wi'] == WI
    assert query(reopened, context_ref=CONTEXT, watermark=1) == before
    assert query(reopened, context_ref=OTHER_CONTEXT)['stages']['status'] == 'unavailable'


def test_context_conflict_never_allocates_usage_to_either_subject(ledger):
    u = usage(wi=None, identity='disputed', at=AT)
    a = unknown_link([u])
    b = other(unknown_link([u], identity='other-claim'))
    ledger.ingest_receipts([goal(), other(goal()), u, a, b])
    for context in (CONTEXT, OTHER_CONTEXT):
        result = query(ledger, context_ref=context)
        assert result['summary']['usage']['event_count'] == 0
        assert result['stages']['membership_conflicts'] == [u.entity_ref]
        assert result['stages']['status'] == 'conflict'
    assert query(ledger)['summary']['usage']['known_total_tokens'] == 100


def test_global_quantity_conflict_is_not_hidden_by_context_selection(ledger):
    u = usage(wi=None, identity='request', at=AT)
    aggregate = usage(wi=None, identity='aggregate', n=200, at=AT,
                      granularity='turn_aggregate', covers=[u.entity_ref])
    ledger.ingest_receipts([goal(), u, aggregate, unknown_link([u])])
    result = query(ledger, context_ref=CONTEXT)
    assert result['summary']['usage']['event_count'] == 0
    assert result['reconciliation']['context_selection']['unavailable_usage_refs'] == [u.entity_ref]
    assert 'context_linked_usage_unavailable' in result['summary']['cost']['reasons']
    assert result['summary']['cost']['machine_cash_usd'] is None


def test_unrelated_revision_history_is_not_inherited(ledger):
    a, b = contribution(), other(contribution())
    revised = fact(b.kind, ['other', a.entity_ref], {**b.payload, 'extent': 'unrelated correction'},
                   supersedes=b.fact_ref)
    ledger.ingest_receipts([a, b, revised])
    assert query(ledger, context_ref=CONTEXT)['stages']['revision_history'] == []
    assert len(query(ledger, context_ref=OTHER_CONTEXT)['stages']['revision_history']) == 2


def test_shared_context_does_not_imply_an_allocation(ledger):
    u = usage(wi=None, identity='shared', at=AT)
    ledger.ingest_receipts([goal(), u, link([u], wi=None, state='shared')])
    result = query(ledger, context_ref=CONTEXT)
    assert result['summary']['usage']['event_count'] == 0
    assert result['stages']['shared_refs'] == [u.entity_ref]
    assert query(ledger)['summary']['usage']['known_total_tokens'] == 100


def test_foreign_context_link_reports_conflict_without_inheriting_output(ledger):
    o = contribution()
    ledger.ingest_receipts([o, other(link([o]))])
    result = query(ledger, context_ref=OTHER_CONTEXT)
    assert result['stages']['outputs']['items'] == []
    assert result['stages']['membership_conflicts'] == [o.entity_ref]


def test_context_diagnostics_do_not_include_unrelated_pending_links(ledger):
    a, b = usage(wi=None, identity='missing-a'), usage(wi=None, identity='missing-b')
    ledger.ingest_receipts([goal(), unknown_link([a]), other(unknown_link([b], identity='other'))])
    result = query(ledger, context_ref=CONTEXT)
    assert result['stages']['unresolved_link_targets'] == [a.entity_ref]
    ledger.ingest_receipts([a])
    assert query(ledger, context_ref=CONTEXT)['stages']['unresolved_link_targets'] == []
    assert query(ledger, context_ref=CONTEXT)['summary']['usage']['event_count'] == 1


def test_correction_history_is_readable_but_not_a_second_output(ledger):
    g, o = goal(), contribution()
    ledger.ingest_receipts([g, o, link([g, o])])
    initial = query(ledger, work_items=(WI,), watermark=1)
    revised = fact('stage_goal', 'goal', {**g.payload, 'desired_change': '保留原目标并解释变化'},
                   supersedes=g.fact_ref, at=LATER)
    revised_output = contribution(version=reference('version', 'corrected'), supersedes=o.fact_ref)
    feedback = fact('stage_feedback', 'reason', dict(wi=None, context_ref=CONTEXT,
        activity_refs=[ACTIVITY], object_ref=OBJECT, version_ref=VERSION,
        category='correction', reason='原版本漏了范围', change='补充原对象的适用范围', epistemic='agent_judgment'))
    ledger.ingest_receipts([revised, revised_output, feedback, link([feedback], identity='feedback-link')])
    result = query(ledger, work_items=(WI,))['stages']
    assert result['goals'][0]['desired_change'] == '保留原目标并解释变化'
    assert result['feedback'][0]['reason'] == '原版本漏了范围'
    assert result['outputs']['cumulative']['object_count'] == 1
    history = {r['fact_ref']: r for r in result['revision_history']}
    assert set(history) == {g.fact_ref, o.fact_ref, revised.fact_ref, revised_output.fact_ref}
    assert history[g.fact_ref]['current'] is False
    assert history[revised.fact_ref]['current'] is True
    assert history[revised.fact_ref]['supersedes'] == g.fact_ref
    assert history[revised.fact_ref]['producer'] == revised.producer
    assert history[g.fact_ref]['wi'] is None  # Original declaration, not rewritten membership.
    assert query(ledger, work_items=(WI,), watermark=1) == initial


def test_branching_corrections_remain_visible_as_conflict(ledger):
    original = contribution()
    first = contribution(version=reference('version', 'fork-a'), supersedes=original.fact_ref)
    second = contribution(version=reference('version', 'fork-b'), supersedes=original.fact_ref)
    ledger.ingest_receipts([original, first, second, link([original])])
    result = query(ledger, work_items=(WI,))['stages']
    assert result['outputs']['items'] == []
    assert result['revision_conflicts'] == [original.entity_ref]
    assert {r['fact_ref'] for r in result['revision_history']} == {r.fact_ref for r in (original, first, second)}
    assert not any(r['current'] for r in result['revision_history'])


@pytest.mark.parametrize('context', ['', 'private/path', 123, [], 'ccf1_bad'])
def test_invalid_context_rejected_before_any_read(context):
    with pytest.raises(ReceiptError):
        CollaborationQuery(context_ref=context)


def test_context_not_a_legacy_accepted_wi_cohort():
    with pytest.raises(ReceiptError, match='context'):
        CollaborationQuery(context_ref=CONTEXT, scope='accepted_wi', from_date='2026-10-01', to_date='2026-10-02')


def test_context_selection_retains_calendar_and_fixed_session_boundaries(ledger):
    from pulse_compute_cost.collaboration_query import CollaborationQuerySession
    a, b = usage(wi=None, identity='today', at=AT), usage(wi=None, identity='later', at=LATER)
    ledger.ingest_receipts([goal(), a, b, unknown_link([a, b])])
    session = CollaborationQuerySession(ledger.receipt_snapshot())
    q = CollaborationQuery(context_ref=CONTEXT, from_date='2026-10-01', to_date='2026-10-02',
        timezone='UTC', scope='activity', watermark=1, group_by=('day',))
    result = session.query(q)
    assert result['summary']['usage']['known_total_tokens'] == 100
    assert sum(g['usage']['known_total_tokens'] for g in result['groups']) == 100
    assert session.query(replace(q, context_ref=OTHER_CONTEXT))['stages']['goals'] == []
    assert session.query(q) == result
