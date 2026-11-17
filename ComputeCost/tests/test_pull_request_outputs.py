"""WI output is the PRs the core merged; anomalies stay unverified until read."""
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.pull_request_audit import GitHubUnavailable, collect_repository, derive
from pulse_compute_cost.pull_request_outputs import merged_keys, project, read_merged
from pulse_compute_cost.receipt_sources import actor_receipt
from pulse_compute_cost.stats_dashboard import build_dashboard
from test_collaboration_receipts import usage, AT, CATALOG, WI
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'Stats/exporter'))
from pulse_stats.cost_dashboard import _validate

NOW = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
OTHER = 'wi_20260913_102'
REPO = 'bigbird-online/PULSE'


def merge(number, merged_at, score=92, decision='review', repo=REPO):
    return {'repository': repo, 'number': number, 'head_sha': 'a' * 40, 'merge_commit': 'f' * 40,
            'merged_at': merged_at, 'observed_at': merged_at, 'score': score, 'reviewed_rounds': 1,
            'decision': decision}


def facts(items, unavailable=()):
    return {'schema': 'harness.stats_facts/v1', 'read_only': True, 'source': 'journal',
            'namespace': 'ns', 'coverage': 'partial' if unavailable else 'complete',
            'unavailable_items': [{'wi': w, 'code': 'journal_invalid'} for w in unavailable],
            'items': [{'id': wi, 'owner': owner, 'merged_pull_requests': rows} for wi, owner, rows in items]}


class MergedPullRequestTests(unittest.TestCase):
    def test_an_old_core_export_is_unavailable_not_zero(self):
        document = facts([(WI, 'pulse', [])])
        del document['items'][0]['merged_pull_requests']
        merged = read_merged(document)
        self.assertEqual((merged['status'], merged['reason']), ('unavailable', 'core_pull_requests_not_exported'))
        view = project(merged, [], date(2026, 9, 17), date(2026, 9, 18))
        self.assertIsNone(view['count'])
        self.assertEqual(view['work_items'], [])
        self.assertEqual(read_merged(None)['status'], 'unavailable')

    def test_prs_count_by_wi_in_the_shanghai_period_of_their_merge(self):
        merged = read_merged(facts([
            (WI, 'pulse', [merge(1, '2026-09-16T17:00:00Z'), merge(2, '2026-09-16T15:59:00Z'),
                           merge(3, '2026-09-17T02:00:00Z', score=None, decision='human')]),
            (OTHER, 'aki', [])]))
        self.assertEqual(merged['status'], 'complete')
        self.assertEqual(merged_keys(merged), {(REPO, 1), (REPO, 2), (REPO, 3)})
        groups = [{'key': {'wi': WI}, 'cost': {'machine_cash_usd': None, 'partial_machine_cash_usd': '3'}},
                  {'key': {'wi': WI}, 'cost': {'machine_cash_usd': None, 'partial_machine_cash_usd': None}},
                  {'key': {'wi': OTHER}, 'cost': {'machine_cash_usd': '1', 'partial_machine_cash_usd': None}}]
        # 01:00 Beijing on 09-17 is today; 23:59 Beijing on 09-16 is not.
        today = project(merged, groups, date(2026, 9, 17), date(2026, 9, 18))
        self.assertEqual((today['status'], today['count'], today['known_count']), ('complete', 2, 2))
        [row] = today['work_items']
        self.assertEqual((row['wi'], row['count'], row['cost_usd'], row['cost_complete']), (WI, 2, '3', False))
        self.assertEqual(row['cost_per_pull_request_usd'], '1.500000')
        self.assertEqual([p['number'] for p in row['pull_requests']], [1, 3])
        self.assertEqual(row['pull_requests'][1]['decision'], 'human')
        # The other WI used money but merged nothing: listed nowhere, so 0 when complete.
        self.assertNotIn(OTHER, [r['wi'] for r in today['work_items']])

    def test_unreadable_work_items_make_the_count_partial(self):
        merged = read_merged(facts([(WI, 'pulse', [merge(1, '2026-09-17T02:00:00Z')])], unavailable=[OTHER]))
        view = project(merged, [], date(2026, 9, 17), date(2026, 9, 18))
        self.assertEqual((view['status'], view['count'], view['known_count']), ('partial', None, 1))
        self.assertEqual(view['unreadable_work_items'], [OTHER])
        broken = read_merged(facts([(WI, 'pulse', [{'repository': REPO}])]))
        self.assertEqual((broken['status'], broken['unreadable']), ('partial', [WI]))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.ledger = UsageLedger(Path(self.temp.name) / 'ledger', clock=lambda: NOW)

    def build(self, merged, audit=None):
        result = build_dashboard(self.ledger, subscriptions={'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []},
            source_revision='a' * 40, now=NOW, catalog_snapshot={'retrieved_at': AT, 'catalog': CATALOG},
            identity_dimensions=True, activity_table=True, pull_requests=merged,
            pull_request_audit=audit, audit_pull_requests=True,
            work_items={WI: {'title': 'a', 'summary': None, 'status': 'active'},
                        OTHER: {'title': 'b', 'summary': None, 'status': 'active'}})
        _validate(result)
        return result

    def test_merged_prs_join_the_activity_view_beside_cost(self):
        u = usage(at='2026-09-17T02:00:00Z')
        self.ledger.ingest_receipts([u, actor_receipt(u, node='tcloud', executor='pulse', entrypoint='feishu',
                                                       observed=NOW.isoformat())])
        merged = read_merged(facts([(WI, 'pulse', [merge(1, '2026-09-17T03:00:00Z')]),
                                    (OTHER, 'aki', [merge(2, '2026-09-17T04:00:00Z')])]))
        document = self.build(merged)
        today = next(v for v in document['views'] if v['period'] == 'today' and v['dimension'] == 'activity')
        outputs = today['pull_request_outputs']
        self.assertEqual((outputs['status'], outputs['count']), ('complete', 2))
        by_wi = {r['wi']: r for r in outputs['work_items']}
        self.assertEqual(by_wi[WI]['cost_usd'], by_wi[WI]['cost_per_pull_request_usd'].rstrip('0').rstrip('.'))
        self.assertIsNone(by_wi[OTHER]['cost_usd'])
        # The WI without usage today gets its own row under its owner; no consumption invented.
        [only] = today['pull_request_only_groups']
        self.assertEqual((only['key']['wi'], only['key']['agent'], only['usage']['event_count']), (OTHER, 'aki', 0))
        self.assertIn('aki', today['active_agents'])
        self.assertIn(OTHER, [w['id'] for w in document['work_items']])
        # The Outcome extension stays beside it, unchanged.
        self.assertIn('outcomes', today)
        self.assertEqual(document['pull_request_anomalies']['status'], 'unverified')
        self.assertIsNone(document['pull_request_anomalies']['unrecorded_merges']['count'])

    def test_published_anomalies_split_reference_merges_from_enforced_ones(self):
        self.ledger.ingest_receipts([usage(at='2026-09-17T02:00:00Z')])
        merged = {'number': 9, 'merged_at': '2026-09-16T00:00:00Z', 'merge_commit': 'e' * 40, 'title': 'web merge'}
        audit = {'observed_at': '2026-09-17T07:00:00Z', 'since': '2026-09-10T08:00:00Z', 'window_days': 7,
                 'repositories': [{'repository': REPO, 'merges': 'read', 'commits': 'read', 'reason': None,
                                   'merged_pull_requests': [merged], 'pr_commits': [], 'direct_commits': []}]}
        kernel = read_merged(facts([(WI, 'pulse', [])]))
        unset = self.build(kernel, audit)['pull_request_anomalies']
        self.assertEqual((unset['unrecorded_merges']['count'], unset['unenforced_merges']['count']), (0, 1))
        enforced = build_dashboard(None, subscriptions={'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []},
            source_revision='a' * 40, now=NOW, pull_requests=kernel, pull_request_audit=audit,
            audit_pull_requests=True, kernel_merge_enforced_since='2026-09-16')['pull_request_anomalies']
        self.assertEqual((enforced['unrecorded_merges']['count'], enforced['unenforced_merges']['count']), (1, 0))

    def test_control_characters_in_github_titles_never_invalidate_cost(self):
        # Review repro: a direct commit titled "fix\tstats" made the whole cost document unusable.
        from pulse_compute_cost.pull_request_audit import _title
        self.assertEqual(_title('fix\tstats\nbody'), 'fix stats')
        self.ledger.ingest_receipts([usage(at='2026-09-17T02:00:00Z')])
        commit = {'sha': '5' * 40, 'committed_at': '2026-09-16T00:00:00Z', 'title': 'fix stats', 'author': 'x'}
        audit = {'observed_at': '2026-09-17T07:00:00Z', 'since': '2026-09-10T08:00:00Z', 'window_days': 7,
                 'repositories': [{'repository': REPO, 'merges': 'read', 'commits': 'read', 'reason': None,
                                   'merged_pull_requests': [], 'pr_commits': [], 'direct_commits': [commit]}]}
        document = self.build(read_merged(facts([(WI, 'pulse', [])])), audit)
        _validate(document)  # A normalized title is valid as published.
        import hashlib
        import json as _json
        from pulse_stats.cost_dashboard import _canonical
        document['pull_request_anomalies']['direct_commits']['items'][0]['title'] = 'fix\tstats'
        document['sha256'] = hashlib.sha256(_canonical({k: v for k, v in document.items() if k != 'sha256'})).hexdigest()
        before = _json.dumps(document, sort_keys=True)
        _validate(document)  # An invalid extension never fails cost, and the carrier is unchanged.
        self.assertEqual(_json.dumps(document, sort_keys=True), before)
        _validate(document)  # Its digest still verifies downstream (App snapshot, Agent query).
        from pulse_stats.cost_dashboard import published_anomalies
        shown = published_anomalies(document)
        self.assertEqual((shown['status'], shown['reason']), ('unverified', 'anomaly_extension_invalid'))
        self.assertIsNone(shown['direct_commits']['count'])

    def test_a_stale_or_future_core_export_never_counts_as_current(self):
        from datetime import timedelta
        document = facts([(WI, 'pulse', [])])
        for age in (timedelta(hours=24), timedelta(minutes=-30)):
            document['generated_at'] = (NOW - age).isoformat()
            merged = read_merged(document, NOW)
            self.assertEqual((merged['status'], merged['reason']), ('unavailable', 'core_facts_stale'))
        document['generated_at'] = (NOW - timedelta(minutes=10)).isoformat()
        self.assertNotEqual(read_merged(document, NOW)['reason'], 'core_facts_stale')

    def test_unavailable_kernel_records_publish_no_counts(self):
        self.ledger.ingest_receipts([usage(at='2026-09-17T02:00:00Z')])
        document = self.build(read_merged(None))
        today = next(v for v in document['views'] if v['period'] == 'today' and v['dimension'] == 'activity')
        self.assertEqual((today['pull_request_outputs']['status'], today['pull_request_outputs']['count']),
                         ('unavailable', None))
        self.assertEqual(today['pull_request_only_groups'], [])


class FamilyTests(unittest.TestCase):
    """A child WI keeps its own row; its parent's row gains a family total."""
    CHILD = 'wi_20260913_103'

    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.ledger = UsageLedger(Path(self.temp.name) / 'ledger', clock=lambda: NOW)

    def build(self, merged):
        return build_dashboard(self.ledger, subscriptions={'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []},
            source_revision='a' * 40, now=NOW, catalog_snapshot={'retrieved_at': AT, 'catalog': CATALOG},
            activity_table=True, activity_only=True, pull_requests=merged,
            work_items={WI: {'title': 'parent', 'summary': None, 'status': 'active'},
                        self.CHILD: {'title': 'child', 'summary': None, 'status': 'completed', 'parent': WI},
                        OTHER: {'title': 'b', 'summary': None, 'status': 'active'}})

    def test_family_adds_children_without_changing_rows_or_totals(self):
        self.ledger.ingest_receipts([usage(wi=wi, identity=identity, at='2026-09-17T02:00:00Z') for wi, identity in
                                     ((WI, 'parent-use'), (self.CHILD, 'child-use'), (OTHER, 'other-use'))])
        merged = read_merged(facts([(WI, 'pulse', [merge(1, '2026-09-17T03:00:00Z')]),
                                    (self.CHILD, 'pulse', [merge(2, '2026-09-17T04:00:00Z'),
                                                           merge(3, '2026-09-17T05:00:00Z')]),
                                    (OTHER, 'aki', [])]))
        document = self.build(merged)
        _validate(document)
        self.assertEqual([w.get('parent') for w in document['work_items']], [None, None, WI])
        today = next(v for v in document['views'] if v['period'] == 'today')
        [family] = today['work_item_families']
        own = {r['wi']: r for r in today['pull_request_outputs']['work_items']}
        self.assertEqual((family['wi'], family['children'], family['pull_request_count']), (WI, [self.CHILD], 3))
        from decimal import Decimal
        self.assertEqual(Decimal(family['cost_usd']), Decimal(own[WI]['cost_usd']) + Decimal(own[self.CHILD]['cost_usd']))
        self.assertEqual(family['cost_complete'], own[WI]['cost_complete'] and own[self.CHILD]['cost_complete'])
        # Rows and the summary are the same as without the family: nothing is counted twice.
        plain = build_dashboard(self.ledger, subscriptions={'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []},
            source_revision='a' * 40, now=NOW, catalog_snapshot={'retrieved_at': AT, 'catalog': CATALOG},
            activity_table=True, activity_only=True, pull_requests=merged)
        before = next(v for v in plain['views'] if v['period'] == 'today')
        self.assertEqual({k: v for k, v in today.items() if k != 'work_item_families'}, before)
        # The Agent query names the parent on the child's row and the family beside it.
        from argparse import Namespace
        from pulse_stats import query
        source = Namespace(cost=lambda: document, app=lambda: None)
        ask = Namespace(period='today', group=None, agent=None, work_item_id=None, runtime=None)
        answer = query.q_cost(source, ask)
        self.assertEqual({r['key']['wi']: r.get('wi_parent') for r in answer['rows']},
                         {WI: None, self.CHILD: WI, OTHER: None})
        self.assertEqual(answer['work_item_families'], [family])
        child = query.q_cost(source, Namespace(**{**vars(ask), 'work_item_id': self.CHILD}))
        self.assertEqual(child['work_item_families'], [family])
        wi = query.q_wi(source, Namespace(**{**vars(ask), 'work_item_id': WI}))
        self.assertEqual((wi['children'], wi['cost_by_period']['today']['family_total']), ([self.CHILD], family))
        self.assertEqual(query.q_wi(source, Namespace(**{**vars(ask), 'work_item_id': self.CHILD}))['parent'], WI)

    def test_unknown_pr_records_leave_the_family_count_unknown_and_tampering_fails(self):
        self.ledger.ingest_receipts([usage(wi=self.CHILD, identity='child-use', at='2026-09-17T02:00:00Z')])
        document = self.build(read_merged(facts([(WI, 'pulse', [])], unavailable=[self.CHILD])))
        _validate(document)
        today = next(v for v in document['views'] if v['period'] == 'today')
        [family] = today['work_item_families']
        self.assertIsNone(family['pull_request_count'])
        import hashlib
        from pulse_stats.cost_dashboard import _canonical
        for change in ({'pull_request_count': 0}, {'children': [OTHER]}, {'cost_usd': '9'}):
            broken = __import__('copy').deepcopy(document)
            next(v for v in broken['views'] if v['period'] == 'today')['work_item_families'][0].update(change)
            broken['sha256'] = hashlib.sha256(_canonical({k: v for k, v in broken.items() if k != 'sha256'})).hexdigest()
            with self.assertRaises(ValueError):
                _validate(broken)

    def test_without_children_nothing_changes(self):
        self.ledger.ingest_receipts([usage(at='2026-09-17T02:00:00Z')])
        document = build_dashboard(self.ledger, subscriptions={'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []},
            source_revision='a' * 40, now=NOW, catalog_snapshot={'retrieved_at': AT, 'catalog': CATALOG},
            activity_table=True, activity_only=True, pull_requests=read_merged(facts([(WI, 'pulse', [])])),
            work_items={WI: {'title': 'a', 'summary': None, 'status': 'active'}})
        _validate(document)
        self.assertTrue(all('work_item_families' not in v for v in document['views']))
        self.assertNotIn('parent', document['work_items'][0])


class FakeSession:
    def __init__(self, answers):
        self.answers, self.paths = answers, []

    def close(self):
        pass

    def get_many(self, paths):
        self.paths += paths
        result = []
        for path in paths:
            answer = next((v for k, v in self.answers.items() if k in path), [])
            result.append(answer)
        return result


def commit(sha, parents, message, at='2026-09-16T00:00:00Z'):
    return {'sha': sha, 'parents': [{'sha': p} for p in parents], 'author': {'login': 'example-owner'},
            'commit': {'message': message, 'committer': {'date': at}}}


class AuditTests(unittest.TestCase):
    since = datetime(2026, 9, 10, tzinfo=timezone.utc)

    def session(self):
        sha = lambda c: c * 40
        commits = [commit(sha('1'), [sha('2')], 'SCL: record native production decision'),
                   commit(sha('2'), [sha('3'), sha('9')], 'Merge pull request #5 from x'),
                   commit(sha('3'), [sha('4')], 'Fix the thing (#6)'),
                   commit(sha('4'), [sha('5')], 'Rebased change'),
                   commit(sha('5'), [sha('6')], 'Pushed straight to main'),
                   commit(sha('9'), [sha('3')], 'branch work, not first parent')]
        pulls = [{'number': n, 'merged_at': '2026-09-16T00:00:00Z', 'updated_at': '2026-09-16T00:00:00Z',
                  'merge_commit_sha': 'e' * 40, 'title': f'PR {n}', 'base': {'ref': 'main'}} for n in (5, 6, 7)]
        pulls.append({'number': 8, 'merged_at': None, 'updated_at': '2026-09-16T00:00:00Z', 'base': {'ref': 'main'}})
        return FakeSession({'/pulls?': pulls, '/commits?': commits,
                            f'/commits/{sha("4")}/pulls': [{'number': 7, 'merged_at': '2026-09-16T00:00:00Z', 'base': {'ref': 'main'}}],
                            f'/commits/{sha("5")}/pulls': []})

    def test_squash_and_rebase_merges_are_prs_and_only_direct_pushes_remain(self):
        row = collect_repository(self.session(), REPO, self.since)
        self.assertEqual((row['merges'], row['commits']), ('read', 'read'))
        self.assertEqual([p['number'] for p in row['merged_pull_requests']], [5, 6, 7])
        self.assertEqual(sorted(c['number'] for c in row['pr_commits']), [6, 7])
        self.assertEqual([c['sha'] for c in row['direct_commits']], ['5' * 40])
        audit = {'schema': 'x', 'observed_at': '2026-09-17T07:00:00Z', 'since': '2026-09-10T08:00:00Z',
                 'window_days': 7, 'repositories': [row]}
        merged = read_merged(facts([(WI, 'pulse', [merge(6, '2026-09-16T00:00:00Z')])]))
        # Enforced from 09-16 Beijing: merges at 09-16 08:00 Beijing are anomalies.
        result = derive(audit, merged, NOW, '2026-09-16')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual([i['number'] for i in result['unrecorded_merges']['items']], [7, 5])
        self.assertEqual(result['unrecorded_merges']['count'], 2)
        self.assertEqual(result['unenforced_merges']['count'], 0)
        self.assertEqual(result['direct_commits']['count'], 1)

    def test_merges_before_enforcement_or_without_a_date_are_only_a_reference(self):
        row = collect_repository(self.session(), REPO, self.since)
        audit = {'observed_at': '2026-09-17T07:00:00Z', 'since': '2026-09-10T08:00:00Z',
                 'window_days': 7, 'repositories': [row]}
        merged = read_merged(facts([(WI, 'pulse', [merge(6, '2026-09-16T00:00:00Z')])]))
        for enforced in (None, '2026-09-17'):
            result = derive(audit, merged, NOW, enforced)
            self.assertEqual(result['enforced_since'], enforced)
            self.assertEqual((result['unrecorded_merges']['count'], result['unrecorded_merges']['items']), (0, []))
            self.assertEqual([i['number'] for i in result['unenforced_merges']['items']], [7, 5])
            # Direct pushes stay anomalies either way.
            self.assertEqual(result['direct_commits']['count'], 1)
        invalid = derive(audit, merged, NOW, '17/09/2026')
        self.assertEqual((invalid['status'], invalid['reason']), ('unverified', 'enforcement_date_invalid'))

    def test_unread_github_or_core_is_unverified_never_zero(self):
        class Down(FakeSession):
            def get_many(self, paths):
                raise GitHubUnavailable('down')
        row = collect_repository(Down({}), REPO, self.since)
        self.assertEqual((row['merges'], row['commits'], row['reason']), ('unverified', 'unverified', 'github_unavailable'))
        audit = {'observed_at': '2026-09-17T07:00:00Z', 'since': '2026-09-10T08:00:00Z',
                 'window_days': 7, 'repositories': [row]}
        merged = read_merged(facts([(WI, 'pulse', [])]))
        result = derive(audit, merged, NOW)
        self.assertEqual((result['status'], result['unrecorded_merges']['count'], result['direct_commits']['count']),
                         ('partial', None, None))
        good = collect_repository(self.session(), REPO, self.since)
        result = derive({**audit, 'repositories': [good]}, read_merged(None), NOW)
        # Without the core's records no GitHub merge is called unrecorded.
        self.assertEqual(result['unrecorded_merges'], {'count': None, 'items': []})
        self.assertEqual(result['direct_commits']['count'], 1)
        self.assertEqual(derive(None, merged, NOW)['reason'], 'audit_not_collected')
        stale = derive({**audit, 'observed_at': '2026-09-16T00:00:00Z'}, merged, NOW)
        self.assertEqual((stale['status'], stale['reason']), ('unverified', 'audit_stale'))

    def test_a_spent_read_budget_leaves_the_remaining_repositories_unverified(self):
        from unittest.mock import patch
        from pulse_compute_cost import pull_request_audit
        started = []

        def start(repository):
            started.append(repository)
            return self.session()
        with patch.object(pull_request_audit, 'BUDGET_SECONDS', 0):
            result = pull_request_audit.collect(start, NOW)
        self.assertEqual(started, [])
        self.assertEqual({(r['merges'], r['commits']) for r in result['repositories']}, {('unverified', 'unverified')})



class UnregisteredTests(unittest.TestCase):
    """Beside each period's PR count: merged PRs the kernel has no record of, hinted by branch."""
    WINDOWS = {'today': (date(2026, 9, 17), date(2026, 9, 18)), 'week': (date(2026, 9, 14), date(2026, 9, 18)),
               'month': (date(2026, 9, 1), date(2026, 9, 18))}

    def audit(self, **extra):
        def pr(number, at, branch):
            return {'number': number, 'merged_at': at, 'merge_commit': 'e' * 40, 'title': f'PR {number}',
                    'head_ref': branch}
        rows = [pr(5, '2026-09-16T00:00:00Z', 'hestia/kr1'), pr(6, '2026-09-16T01:00:00Z', 'pulse/wi1'),
                pr(7, '2026-09-17T02:00:00Z', 'claude/x'), pr(8, '2026-09-02T00:00:00Z', 'duke/weekly'),
                pr(9, '2026-09-15T00:00:00Z', 'fix-typo')]
        return {'observed_at': '2026-09-17T07:00:00Z', 'since': '2026-09-10T08:00:00Z',
                'pulls_since': '2026-08-31T16:00:00Z', 'window_days': 7,
                'repositories': [{'repository': REPO, 'merges': 'read', 'commits': 'read', 'reason': None,
                                  'merged_pull_requests': rows, 'pr_commits': [], 'direct_commits': []}], **extra}

    def test_each_period_counts_unrecorded_merges_with_agent_hints(self):
        from pulse_compute_cost.pull_request_audit import unregistered
        merged = read_merged(facts([(WI, 'pulse', [merge(6, '2026-09-16T01:00:00Z')])]))
        result = unregistered(self.audit(), merged, NOW, self.WINDOWS, {'hestia', 'duke', 'pulse'})
        self.assertEqual({p: r['count'] for p, r in result.items()}, {'today': 1, 'week': 3, 'month': 4})
        week = result['week']
        # "claude/" names no registered Agent: no hint, like a branch without a prefix.
        self.assertEqual((week['agents'], week['unhinted_count']), ([{'agent': 'hestia', 'count': 1}], 2))
        self.assertEqual([i['number'] for i in week['items']], [7, 5, 9])
        self.assertEqual(result['month']['agents'], [{'agent': 'duke', 'count': 1}, {'agent': 'hestia', 'count': 1}])
        # The 7-day anomalies keep their window: the 09-02 merge is not among them.
        anomalies = derive(self.audit(), merged, NOW)
        self.assertEqual([i['number'] for i in anomalies['unenforced_merges']['items']], [7, 5, 9])
        self.assertNotIn('head_ref', anomalies['unenforced_merges']['items'][0])

    def test_unknown_is_never_zero(self):
        from pulse_compute_cost.pull_request_audit import unregistered
        merged = read_merged(facts([(WI, 'pulse', [])]))
        old = self.audit(); del old['pulls_since']  # A cache from before the wider PR read.
        result = unregistered(old, merged, NOW, self.WINDOWS, set())
        self.assertEqual((result['today']['count'], result['month']['count'], result['month']['reason']),
                         (1, None, 'audit_window_short'))
        for audit, kernel, reason in ((None, merged, 'audit_not_collected'),
                                      (self.audit(observed_at='2026-09-16T00:00:00Z'), merged, 'audit_stale'),
                                      (self.audit(), read_merged(facts([], unavailable=[OTHER])), 'core_pull_requests_unverified')):
            result = unregistered(audit, kernel, NOW, self.WINDOWS, set())
            self.assertEqual({(r['count'], r['reason']) for r in result.values()}, {(None, reason)})

    def test_the_published_extension_validates_and_tampering_falls_back(self):
        import copy
        from pulse_stats.pull_request_view import validate_anomalies
        document = build_dashboard(None, subscriptions={'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []},
            source_revision='a' * 40, now=NOW, pull_requests=read_merged(facts([(WI, 'pulse', [])])),
            pull_request_audit=self.audit(), audit_pull_requests=True, agent_tenants={'hestia': {}, 'duke': {}})
        anomalies = document['pull_request_anomalies']
        validate_anomalies(anomalies)
        self.assertEqual(anomalies['unregistered_pull_requests']['week']['count'], 4)
        for change in ({'count': 0}, {'unhinted_count': 9}, {'status': 'unverified'}):
            broken = copy.deepcopy(anomalies)
            broken['unregistered_pull_requests']['week'].update(change)
            with self.assertRaises(ValueError):
                validate_anomalies(broken)

    def test_collection_keeps_the_head_branch_back_to_the_period_start(self):
        pulls = [{'number': n, 'merged_at': at, 'updated_at': at, 'merge_commit_sha': 'e' * 40, 'title': 't',
                  'base': {'ref': 'main'}, 'head': {'ref': ref}}
                 for n, at, ref in ((5, '2026-09-16T00:00:00Z', 'hestia/kr1'), (4, '2026-09-02T00:00:00Z', 'bad ref'),
                                    (3, '2026-08-20T00:00:00Z', 'old/x'))]
        row = collect_repository(FakeSession({'/pulls?': pulls, '/commits?': []}), REPO,
                                 datetime(2026, 9, 10, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual([(p['number'], p['head_ref']) for p in row['merged_pull_requests']], [(4, None), (5, 'hestia/kr1')])
        from pulse_compute_cost.pull_request_audit import pulls_start
        # 2026-12-02 (Wednesday): the week starts in November, before the month.
        at = datetime(2026, 12, 2, 4, tzinfo=timezone.utc)
        self.assertEqual(pulls_start(at, at).isoformat(), '2026-11-30T00:00:00+08:00')
        self.assertEqual(pulls_start(NOW, NOW).isoformat(), '2026-09-01T00:00:00+08:00')


if __name__ == '__main__':
    unittest.main()
