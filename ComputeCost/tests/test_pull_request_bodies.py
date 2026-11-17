"""PR text for agent_day: listed by update time, merged into the cache, coverage never overstated."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost import pull_request_bodies as bodies
from pulse_compute_cost.pull_request_audit import GitHubUnavailable

NOW = datetime(2026, 11, 10, 2, tzinfo=timezone.utc)
REPO = 'bigbird-online/PULSE'


def pull(number, updated, merged=None, closed=None, body='text', state=None):
    return {'number': number, 'state': state or ('closed' if closed or merged else 'open'), 'draft': False,
            'created_at': '2026-11-01T00:00:00Z', 'updated_at': updated, 'closed_at': closed or merged,
            'merged_at': merged, 'merge_commit_sha': 'a' * 40, 'title': f'PR {number}', 'body': body,
            'user': {'login': 'example-owner'}, 'head': {'ref': f'hestia/pr-{number}'}, 'base': {'ref': 'main'}}


class Pages:
    """A session answering the listing from a newest-first list of PRs, page by page."""

    def __init__(self, pulls, fail=False):
        self.pulls, self.fail, self.paths, self.deadline = pulls, fail, [], None

    def get_many(self, paths):
        self.paths += paths
        if self.fail:
            return [GitHubUnavailable('down') for _ in paths]
        out = []
        for path in paths:
            page = int(path.rsplit('page=', 1)[1])
            out.append(self.pulls[(page - 1) * bodies.PAGE:page * bodies.PAGE])
        return out

    def close(self):
        pass


class PullRequestBodyTests(unittest.TestCase):
    def test_first_read_backfills_and_keeps_bounded_text(self):
        long = 'x' * (bodies.BODY_LIMIT + 5)
        session = Pages([pull(3, '2026-11-09T00:00:00Z', merged='2026-11-09T00:00:00Z', body=long),
                         pull(2, '2026-11-08T00:00:00Z', closed='2026-11-08T00:00:00Z', body=None),
                         pull(1, '2026-06-01T00:00:00Z')])
        document = bodies.collect(lambda repo: session, NOW, None, repositories=(REPO,))
        [row] = document['repositories']
        self.assertEqual((row['status'], row['reason'], row['observed_at']), ('read', None, '2026-11-10T02:00:00Z'))
        self.assertEqual(row['covered_since'], '2026-07-13T02:00:00Z')  # BACKFILL_DAYS before now
        self.assertEqual([p['number'] for p in row['pull_requests']], [2, 3])
        merged, closed = row['pull_requests'][1], row['pull_requests'][0]
        self.assertEqual((len(merged['body']), merged['body_truncated']), (bodies.BODY_LIMIT, True))
        self.assertEqual((closed['body'], closed['merged_at'], closed['closed_at']),
                         (None, None, '2026-11-08T00:00:00Z'))
        self.assertIn('state=all&base=main&sort=updated&direction=desc', session.paths[0])

    def test_next_read_starts_at_the_previous_one_and_keeps_its_coverage(self):
        first = bodies.collect(lambda repo: Pages([pull(1, '2026-11-01T00:00:00Z', body='old')]), NOW, None,
                               repositories=(REPO,))
        later = NOW + timedelta(hours=4)
        session = Pages([pull(1, '2026-11-10T05:00:00Z', body='edited'), pull(2, '2026-11-10T04:00:00Z')])
        second = bodies.collect(lambda repo: session, later, first, repositories=(REPO,))
        [row] = second['repositories']
        self.assertEqual(row['covered_since'], first['repositories'][0]['covered_since'])
        self.assertEqual({p['number']: p['body'] for p in row['pull_requests']}, {1: 'edited', 2: 'text'})
        self.assertEqual(row['observed_at'], '2026-11-10T06:00:00Z')

    def test_unreadable_github_keeps_the_previous_rows_and_coverage(self):
        first = bodies.collect(lambda repo: Pages([pull(1, '2026-11-01T00:00:00Z')]), NOW, None, repositories=(REPO,))
        second = bodies.collect(lambda repo: Pages([], fail=True), NOW + timedelta(hours=4), first,
                                repositories=(REPO,))
        [old], [row] = first['repositories'], second['repositories']
        self.assertEqual((row['status'], row['reason']), ('unverified', 'github_unavailable'))
        self.assertEqual((row['observed_at'], row['covered_since'], row['pull_requests']),
                         (old['observed_at'], old['covered_since'], old['pull_requests']))
        never = bodies.collect(lambda repo: Pages([], fail=True), NOW, None, repositories=(REPO,))
        self.assertEqual((never['repositories'][0]['observed_at'], never['repositories'][0]['covered_since']),
                         (None, None))

    def test_page_limit_covers_only_back_to_the_oldest_listed_update(self):
        newest = datetime(2026, 11, 9, tzinfo=timezone.utc)
        pulls = [pull(n, (newest - timedelta(minutes=n)).strftime('%Y-%m-%dT%H:%M:%SZ'))
                 for n in range(bodies.PAGE * bodies.PAGES + 1)]
        document = bodies.collect(lambda repo: Pages(pulls), NOW, None, repositories=(REPO,))
        [row] = document['repositories']
        oldest = newest - timedelta(minutes=bodies.PAGE * bodies.PAGES - 1)
        self.assertEqual((row['status'], row['reason']), ('unverified', 'pull_request_pages_exceeded'))
        self.assertEqual(row['covered_since'], oldest.strftime('%Y-%m-%dT%H:%M:%SZ'))
        self.assertEqual(row['observed_at'], '2026-11-10T02:00:00Z')

    def test_a_gap_since_the_previous_read_restarts_coverage(self):
        first = bodies.collect(lambda repo: Pages([]), NOW, None, repositories=(REPO,))
        much_later = NOW + timedelta(days=bodies.BACKFILL_DAYS + 10)
        second = bodies.collect(lambda repo: Pages([]), much_later, first, repositories=(REPO,))
        self.assertEqual(second['repositories'][0]['covered_since'],
                         (much_later - timedelta(days=bodies.BACKFILL_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ'))

    def test_due_after_the_interval_or_the_first_time_past_utc8_midnight(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cache.json'
            self.assertTrue(bodies.due(path, NOW))
            read_at = datetime(2026, 11, 10, 14, tzinfo=timezone.utc)  # 22:00 UTC+8
            path.write_text(json.dumps({'observed_at': read_at.strftime('%Y-%m-%dT%H:%M:%SZ')}))
            self.assertFalse(bodies.due(path, read_at + timedelta(hours=1)))
            self.assertTrue(bodies.due(path, read_at + timedelta(hours=2, minutes=5)))  # 00:05 UTC+8
            self.assertTrue(bodies.due(path, read_at + timedelta(seconds=bodies.REFRESH_SECONDS)))


if __name__ == '__main__':
    unittest.main()
