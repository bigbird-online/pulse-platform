"""A declared `none` is the absence of a WI, not counter-evidence to a WI claim.

A request declared before its WI existed carries `none`; the session's own
WI write (holder supplement) or a stage membership naming the WI attributes it
to that WI. Reviewed classifications, cron labels and two different WIs keep
their strict behavior.
"""
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, reference
from test_mcp_cc_session_wi import CATALOG
from test_scheduled_job_attribution import AT, codex_usage, scheduled_stream

WI = 'wi_20261030_991'
OTHER_WI = 'wi_20261030_992'
CONTEXT = reference('context', 'declared-none-pre-wi')
ACTIVITY = reference('activity', 'declared-none-pre-wi')


def attribution(row, producer, *, wi=None, state='assigned', policy=None, **extra):
    start = row.payload.get('interval_start') or row.occurred_at
    payload = {'target_ref': row.entity_ref, 'wi': wi, 'executor': None, 'responsible': None,
               'delegator': None, 'state': state, 'policy': policy or producer, 'effective_at': start, **extra}
    return Receipt.make('attribution', [producer, row.entity_ref], payload, occurred_at=start,
                        observed_at=AT, producer=producer, evidence=(row.fact_ref,))


def declared_none(row):
    """What pi_request_purpose writes for a request declared `none`."""
    return attribution(row, 'pi_native_request_purpose.v1', policy='non_wi_activity.v1', work_kind='none')


def reviewed(row, work_kind):
    """A reviewed non-WI classification (`non_wi_attribution`)."""
    return attribution(row, 'non_wi_activity.v1', work_kind=work_kind)


def held(row, wi=WI, state='assigned'):
    return attribution(row, 'holder_supplement.v1', wi=wi, state=state)


def stage_link(rows, wi=WI, identity='link'):
    return Receipt.make('stage_link', identity, {'wi': wi, 'context_ref': CONTEXT,
        'target_refs': [r.entity_ref for r in rows], 'state': 'assigned', 'basis': '合成阶段成员',
        'epistemic': 'agent_judgment'}, occurred_at=AT, observed_at=AT, producer='synthetic_stage.v1',
        evidence=(ACTIVITY,))


class DeclaredNoneTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.rows = [codex_usage('one', 1), codex_usage('two', 2)]

    def query(self, *facts):
        ledger = UsageLedger(self.root/f'ledger-{len(list(self.root.iterdir()))}')
        ledger.ingest_receipts([*self.rows, *facts])
        result = query_collaboration(ledger, CollaborationQuery(scope='activity', from_date='2026-01-01',
            to_date='2026-01-02', timezone='UTC', group_by=('wi',)), catalog=CATALOG)
        return {g['key']['wi']: g for g in result['groups']}, result['summary']['usage']

    def test_holder_wi_outweighs_a_declared_none(self):
        groups, usage = self.query(*map(declared_none, self.rows), *map(held, self.rows))
        self.assertEqual(set(groups), {WI})
        self.assertEqual(set(groups[WI]['attribution']['sources']), {'holder_supplement'})
        self.assertEqual((usage['wi_unassigned_event_count'], usage['no_wi_event_count']), (0, 0))

    def test_stage_membership_outweighs_a_declared_none(self):
        groups, usage = self.query(*map(declared_none, self.rows), *map(held, self.rows), stage_link(self.rows))
        self.assertEqual(set(groups), {WI})
        self.assertEqual(groups[WI]['usage']['event_count'], 2)
        self.assertEqual((usage['wi_unassigned_event_count'], usage['no_wi_event_count']), (0, 0))

    def test_declared_none_alone_stays_non_wi_work(self):
        groups, usage = self.query(*map(declared_none, self.rows))
        self.assertEqual(set(groups), {'none'})
        self.assertEqual((usage['wi_unassigned_event_count'], usage['no_wi_event_count']), (0, 2))

    def test_non_wi_supplement_does_not_replace_a_declared_none(self):
        from pulse_compute_cost.receipts import EXPLORATION_POLICY
        explored = [attribution(r, 'holder_supplement.v1', wi=WI, policy=EXPLORATION_POLICY) for r in self.rows]
        groups, _ = self.query(*map(declared_none, self.rows), *explored)
        self.assertEqual(set(groups), {'none'})

    def test_cron_label_and_reviewed_none_keep_their_behavior(self):
        for kind in ('cron', 'none'):
            with self.subTest(kind=kind):
                labels = [reviewed(r, kind) for r in self.rows]
                groups, _ = self.query(*labels, *map(held, self.rows))
                self.assertEqual(set(groups), {kind})      # a held WI never replaces a reviewed label
                groups, usage = self.query(*labels, stage_link(self.rows))
                self.assertEqual(set(groups), {'ambiguous'})
                self.assertEqual(usage['wi_unassigned_event_count'], 2)

    def test_two_different_wis_stay_ambiguous(self):
        nones = list(map(declared_none, self.rows))
        groups, usage = self.query(*nones, *(held(r, wi=None, state='ambiguous') for r in self.rows))
        self.assertEqual(set(groups), {'ambiguous'})
        self.assertEqual(usage['wi_unassigned_event_count'], 2)
        groups, usage = self.query(*nones, stage_link(self.rows), stage_link(self.rows, OTHER_WI, 'other'))
        self.assertEqual(set(groups), {'ambiguous'})
        self.assertEqual(usage['wi_unassigned_event_count'], 2)
        declared = [attribution(r, 'pi_native_request_purpose.v1', wi=OTHER_WI) for r in self.rows]
        groups, _ = self.query(*declared, stage_link(self.rows))
        self.assertEqual(set(groups), {'ambiguous'})

    def test_stage_membership_never_hides_a_conflicting_holder_claim(self):
        nones = list(map(declared_none, self.rows))
        for name, holders in (('other WI', [held(r, wi=OTHER_WI) for r in self.rows]),
                              ('ambiguous', [held(r, wi=None, state='ambiguous') for r in self.rows])):
            with self.subTest(holder=name):
                groups, usage = self.query(*nones, *holders, stage_link(self.rows))
                self.assertEqual(set(groups), {'ambiguous'})
                self.assertEqual(usage['wi_unassigned_event_count'], 2)

    def test_every_applicable_wi_write_is_checked_not_only_the_winning_supplement(self):
        nones = list(map(declared_none, self.rows))
        job = scheduled_stream(self.root/'host.jsonl', OTHER_WI)
        cases = (('job outranks holder, stage agrees with job', [*map(held, self.rows), *job, stage_link(self.rows, OTHER_WI)]),
                 ('job outranks holder, no stage', [*map(held, self.rows), *job]),
                 ('two holders disagree, stage names one', [*map(held, self.rows),
                     *(held(r, wi=OTHER_WI) for r in self.rows), stage_link(self.rows)]),
                 ('two holders disagree, no stage', [*map(held, self.rows), *(held(r, wi=OTHER_WI) for r in self.rows)]))
        for name, facts in cases:
            with self.subTest(case=name):
                groups, usage = self.query(*nones, *facts)
                self.assertEqual(set(groups), {'ambiguous'})
                self.assertEqual(usage['wi_unassigned_event_count'], 2)

    def test_an_agreeing_job_write_does_not_hide_the_holder_wi(self):
        groups, usage = self.query(*map(declared_none, self.rows), *map(held, self.rows),
                                   *scheduled_stream(self.root/'host.jsonl', WI))
        self.assertEqual(set(groups), {WI})
        self.assertEqual(usage['wi_unassigned_event_count'], 0)


if __name__ == '__main__':
    unittest.main()
