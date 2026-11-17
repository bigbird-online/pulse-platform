"""Conflict evidence must follow the same cohort as the displayed amount."""
import unittest
from pulse_compute_cost.receipts import Receipt, digest, non_wi_attribution, reference
from pulse_compute_cost.receipt_sources import actor_receipt
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from test_collaboration_receipts import usage, execution, CATALOG, AT, WI, OTHER

DIMS=('wi','runtime','agent','model','node','entrypoint')


def change(row, **updates):
    payload={**row.payload, **updates}
    return Receipt.make(row.kind, None, payload, entity_ref=row.entity_ref,
                        occurred_at=row.occurred_at, observed_at=AT, producer=row.producer)


def example(name, *, wi=WI, runtime='pi', agent='pulse', node='ownermac', entrypoint='telegram', model='openai/test', at=AT):
    row=change(usage(wi=wi,identity=name,n=5,at=at,agent=agent),runtime=runtime,model=model)
    actor=actor_receipt(row,node=node,executor=agent,entrypoint=entrypoint,observed=AT,evidence=(row.fact_ref,))
    return [row,actor]


class Memory:
    def __init__(self, facts): self.facts=facts
    def receipt_snapshot(self, **kwargs):
        return dict(facts=self.facts,watermark=1,recorded_at=AT,sha256=digest([r.to_dict() for r in self.facts]))


def query(facts, **args):
    return query_collaboration(Memory(facts),CollaborationQuery(scope='activity',from_date='2026-09-13',
        to_date='2026-09-14',timezone='UTC',group_by=DIMS,**args),catalog=CATALOG,catalog_observed_at=AT)


def fork(rows):
    return [*rows,change(rows[0],tokens={**rows[0].payload['tokens'],'output':7})]


class ConflictScopeTests(unittest.TestCase):
    def test_other_wi_fork_does_not_change_ordinary_or_unknown_bucket(self):
        for ordinary in (True,False):
            with self.subTest(ordinary=ordinary):
                good=example('good',wi=None)
                if ordinary: good.append(non_wi_attribution(good[0],work_kind='none',observed_at=AT,evidence=(reference('human','ordinary'),)))
                before=query(good)['groups'][0]
                after=query(good+fork(example('bad',wi=OTHER,runtime='codex')))
                own=next(g for g in after['groups'] if g['key']==before['key'])
                self.assertEqual(own['cost'],before['cost'])
                self.assertEqual(own['usage']['conflicted_event_count'],0)
                self.assertIn('join_conflict',after['summary']['cost']['reasons'])
                self.assertEqual(after['summary']['usage']['conflicted_event_count'],1)

    def test_priced_total_keeps_every_priced_row_when_conflict_withholds_amounts(self):
        good=example('good',wi=None)
        before=query(good)['summary']
        after=query(good+fork(example('bad',wi=OTHER,runtime='codex')))['summary']
        self.assertIn('join_conflict',after['cost']['reasons'])
        self.assertIsNone(after['cost']['machine_cash_usd'])
        self.assertIsNone(after['cost']['partial_machine_cash_usd'])
        self.assertEqual(after['cost']['priced_machine_cash_usd'],before['cost']['partial_machine_cash_usd'])
        self.assertEqual(after['cost']['priced_event_count'],after['usage']['event_count'])
        self.assertEqual(before['cost']['priced_machine_cash_usd'],before['cost']['partial_machine_cash_usd'])
        empty=query([])['summary']
        self.assertEqual((empty['cost']['priced_event_count'],empty['cost']['priced_machine_cash_usd']),(0,None))

    def test_each_known_dimension_limits_usage_conflict(self):
        for delta in ({'runtime':'codex'},{'agent':'other'},{'node':'tcloud'},
                      {'entrypoint':'feishu'},{'model':'other/model'},{'wi':OTHER},
                      {'at':'2026-01-13T01:00:00Z'}):
            with self.subTest(delta=delta):
                good=example('good');before=query(good)['groups'][0]
                after=query(good+fork(example('bad',**delta)))
                own=next(g for g in after['groups'] if g['key']==before['key'])
                self.assertEqual(own['cost'],before['cost'])
                self.assertEqual(own['usage']['conflicted_event_count'],0)

    def test_relevant_usage_fork_still_withholds_its_group(self):
        good=example('good');after=query(good+fork(example('bad')))
        own=next(g for g in after['groups'] if g['usage']['event_count'])
        self.assertIn('join_conflict',own['cost']['reasons'])
        self.assertIsNone(own['cost']['partial_machine_cash_usd'])
        self.assertEqual(own['usage']['conflicted_event_count'],1)

    def test_direct_runtime_filter_does_not_import_other_runtime_forks(self):
        good=example('good');before=query(good,runtime='pi')['summary']
        after=query(good+fork(example('bad',runtime='codex')),runtime='pi')['summary']
        self.assertEqual(after['cost'],before['cost'])
        self.assertEqual(after['usage']['conflicted_event_count'],0)

    def test_old_unassigned_execution_fork_does_not_contaminate_current_wi(self):
        good=example('good');before=query(good)['groups'][0]
        e=execution(wi=None,identity='old',state='accepted')
        e=Receipt.make('execution','old',e.payload,occurred_at='2026-01-13T00:00:00Z',observed_at=AT,producer=e.producer)
        after=query(good+[e,change(e,state='failed')])
        own=next(g for g in after['groups'] if g['key']==before['key'])
        self.assertEqual(own['cost'],before['cost'])

    def test_linked_execution_conflict_remains_relevant_across_start_date(self):
        e=execution(identity='linked',state='accepted')
        e=Receipt.make('execution','linked',e.payload,occurred_at='2026-09-12T23:00:00Z',observed_at=AT,producer=e.producer)
        good=example('good',runtime='cc');good[0]=change(good[0],execution_ref=e.entity_ref)
        after=query(good+[e,change(e,state='failed')])
        own=next(g for g in after['groups'] if g['usage']['event_count'])
        self.assertIn('execution_evidence_conflict',own['cost']['reasons'])

    def test_coverage_fork_counts_only_where_a_head_is_an_applicable_denominator(self):
        def forked(domain, statuses, wi=None):
            source = reference('source', 'forked-' + domain)
            heads = [Receipt.make('coverage', [source, domain, wi or 'global'], dict(domain=domain, wi=wi,
                source_ref=source, status=status, expected=0 if status == 'idle' else None,
                observed=0 if status == 'idle' else None, watermark=str(i), reason=None, source_detail=None),
                occurred_at=None, observed_at=AT, producer='registered_collector.v1')
                for i, status in enumerate(statuses)]
            return heads
        good = example('good')
        for heads, relevant in ((forked('usage', ('partial', 'unknown')), True),
                                (forked('execution', ('idle', 'unknown')), True),
                                (forked('usage', ('partial', 'unknown'), wi=WI), True),
                                (forked('usage', ('partial', 'unknown'), wi=OTHER), False),
                                (forked('usage', ('idle', 'excluded')), False),
                                (forked('quality', ('partial', 'unknown')), False),
                                (forked('human', ('partial', 'unknown')), False)):
            with self.subTest(domain=heads[0].payload['domain'], wi=heads[0].payload['wi'],
                              statuses=[h.payload['status'] for h in heads]):
                own = next(g for g in query(good + heads)['groups'] if g['usage']['event_count'])
                self.assertEqual('execution_evidence_conflict' in own['cost']['reasons'], relevant)

    def test_foreign_undated_usage_does_not_add_time_warning(self):
        good=example('good',wi=None);good.append(non_wi_attribution(good[0],work_kind='none',observed_at=AT,evidence=(reference('human','ordinary'),)))
        before=query(good)['groups'][0];bad=example('undated',wi=OTHER,runtime='codex',at=None)
        after=query(good+bad);own=next(g for g in after['groups'] if g['key']==before['key'])
        self.assertNotIn('time_unallocated',own['cost']['reasons'])
        self.assertEqual(own['usage']['time_unallocated_event_count'],0)
        self.assertIn('time_unallocated',after['summary']['cost']['reasons'])

    def test_declared_fork_belongs_to_its_wi_not_the_unknown_bucket(self):
        good=example('good',wi=None);bad=example('bad',wi=None)
        claim=Receipt.make('attribution','purpose',dict(target_ref=bad[0].entity_ref,wi=OTHER,
            executor=None,responsible=None,delegator=None,state='assigned',policy='session_wi_declaration.v1',effective_at=AT),
            occurred_at=AT,observed_at=AT,producer='session_wi_declaration.v1')
        before=query(good)['groups'][0];result=query(good+fork(bad)+[claim])
        own=next(g for g in result['groups'] if g['key']==before['key'])
        self.assertEqual(own['cost'],before['cost'])
        self.assertEqual(own['usage']['conflicted_event_count'],0)
        selected=query(good+fork(bad)+[claim],work_items=(OTHER,))
        self.assertIn('join_conflict',selected['summary']['cost']['reasons'])

    def test_ambiguous_bucket_keeps_its_own_quantity_conflict(self):
        good=example('good',wi=None);bad=example('bad',wi=None)
        claims=[Receipt.make('attribution',['ambiguous',r.entity_ref],dict(target_ref=r.entity_ref,wi=None,
            executor=None,responsible=None,delegator=None,state='ambiguous',policy='session_wi_declaration.v1',effective_at=AT),
            occurred_at=AT,observed_at=AT,producer='session_wi_declaration.v1') for r in (good[0],bad[0])]
        result=query(good+fork(bad)+claims)
        own=next(g for g in result['groups'] if g['usage']['event_count'])
        self.assertEqual(own['key']['wi'],'ambiguous')
        self.assertIn('join_conflict',own['cost']['reasons'])
        self.assertEqual(own['usage']['conflicted_event_count'],1)
