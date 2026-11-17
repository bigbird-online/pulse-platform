"""Stats WI panel from native core WorkItem facts only."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'Stats/exporter'))
from pulse_stats import wi_facts

# Thursday 2026-10-15 20:00 CST.
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def at(days=0, hours=0):
    return (NOW - timedelta(days=days, hours=hours)).isoformat()


def item(wi, status, created, updated, closed=None, holder=None, profile='lightweight'):
    return {'id': wi, 'title': wi + ' title', 'owner': 'pulse', 'status': status,
            'business_progress': status, 'profile': profile, 'holder': holder,
            'created_at': created, 'updated_at': updated, 'closed_at': closed, 'local_revision': 1}


def facts(items, unavailable=()):
    return {'schema': wi_facts.SCHEMA, 'generated_at': at(hours=0.1), 'namespace': 'n',
            'items': items, 'unavailable_items': list(unavailable),
            'coverage': 'partial' if unavailable else 'complete', 'source': 'journal', 'read_only': True}


ITEMS = [
    item('wi_today_done', 'completed', at(hours=1), at(hours=0.5), closed=at(hours=0.6)),
    item('wi_monday', 'active', at(days=3), at(hours=2), holder='hestia', profile='standard'),
    item('wi_old_active', 'active', at(days=30), at(days=9)),
    item('wi_draft', 'draft', at(days=2), at(days=2)),
    item('wi_stopped', 'stopped', at(days=12), at(days=4)),
    item('wi_last_week_done', 'completed', at(days=20), at(days=1), closed=at(days=8)),
    item('wi_terminated', 'terminated', at(days=1), at(days=1), closed=at(days=1)),
    {**item('wi_main_body', 'active', at(days=5), at(days=1)), 'main_only': True},
    {**item('wi_main_done', 'completed', at(days=6), at(days=2), closed=at(days=2)), 'main_only': True},
]


class WiFactsTests(unittest.TestCase):
    def write(self, document, mode=0o600):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'wi-facts-v1.json'
        path.write_bytes(json.dumps(document).encode())
        os.chmod(path, mode)
        return path

    def test_counters_use_display_day_week_and_closing_time(self):
        document, error = wi_facts.load(self.write(facts(ITEMS)), NOW)
        self.assertIsNone(error)
        counters = wi_facts.counters(document, NOW)
        self.assertEqual((counters['day_created'], counters['day_completed']), (1, 1))
        # Week of Monday 2026-10-12: today, Monday-ish, draft, terminated.
        self.assertEqual(counters['iso_week_created'], 4)
        self.assertEqual(counters['iso_week_completed'], 2)
        self.assertEqual(counters['week_created'], 6)
        self.assertEqual([row['work_item_id'] for row in counters['stalled']], ['wi_old_active'])
        self.assertIsNone(counters['handoff_pending'])

    def test_period_counts_cover_created_and_every_terminal_closing(self):
        migrated = item('wi_migrated_done', 'completed', at(days=1), at(days=1))
        document, _ = wi_facts.load(self.write(facts(ITEMS + [migrated])), NOW)
        counters = wi_facts.counters(document, NOW)
        # October 1-15: everything except the two September creations.
        self.assertEqual((counters['day_created'], counters['iso_week_created'], counters['month_created']), (1, 5, 8))
        # Completed and terminated by closed_at; no closed_at means no closing day.
        self.assertEqual((counters['day_closed'], counters['iso_week_closed'], counters['month_closed']), (1, 3, 4))

    def test_only_main_body_work_items_need_the_owner_and_the_rest_are_automatic(self):
        document, _ = wi_facts.load(self.write(facts(ITEMS)), NOW)
        rows = {row['work_item_id']: row for row in wi_facts.in_flight(document, NOW)}
        self.assertEqual(set(rows), {'wi_monday', 'wi_old_active', 'wi_draft', 'wi_stopped', 'wi_main_body'})
        self.assertEqual({k for k, r in rows.items() if r['needs_owner']}, {'wi_main_body'})
        self.assertEqual(rows['wi_main_body']['disposition'], 'manual_required')
        self.assertEqual((rows['wi_monday']['status'], rows['wi_monday']['blocked_by'],
                          rows['wi_monday']['disposition'], rows['wi_monday']['workflow_profile']),
                         ('in_progress', 'hestia', 'waiting_peer', 'standard'))
        self.assertEqual((rows['wi_draft']['needs_owner'], rows['wi_draft']['disposition']), (False, 'sweep_watching'))
        self.assertEqual(rows['wi_stopped']['disposition'], 'blocked_needs_read')
        self.assertEqual(rows['wi_old_active']['disposition'], 'sweep_overdue')
        requests = wi_facts.human_requests(document, NOW)
        self.assertEqual([(r['id'], r['asks']) for r in requests], [('wi_main_body', '/wi wi_main_body')])

    def test_authority_is_validated_only_for_fresh_complete_facts(self):
        document, error = wi_facts.load(self.write(facts(ITEMS)), NOW)
        authority = wi_facts.authority(document, error)
        self.assertEqual((authority['status'], authority['scope'], authority['freshness']), ('validated', 'global', 'fresh'))
        self.assertTrue(authority['authority_revision'].startswith('sha256:'))
        partial, error = wi_facts.load(self.write(facts(ITEMS, [{'wi': 'wi_x', 'code': 'journal_invalid'}])), NOW)
        self.assertEqual(wi_facts.authority(partial, error)['status'], 'partial')
        self.assertEqual(wi_facts.counters(partial, NOW)['unreadable_work_items'], 1)
        stale = facts(ITEMS)
        stale['generated_at'] = at(hours=2)
        document, error = wi_facts.load(self.write(stale), NOW)
        self.assertEqual((document, error), (None, 'wi_facts_stale'))
        self.assertEqual(wi_facts.authority(None, error)['freshness'], 'stale')

    def test_unsafe_or_invalid_sources_are_unavailable_not_empty(self):
        self.assertEqual(wi_facts.load(self.write(facts(ITEMS), mode=0o644), NOW), (None, 'invalid_wi_facts'))
        broken = facts([{**ITEMS[0], 'status': 'in_progress'}])
        self.assertEqual(wi_facts.load(self.write(broken), NOW), (None, 'invalid_wi_facts'))
        writable = facts(ITEMS)
        writable['read_only'] = False
        self.assertEqual(wi_facts.load(self.write(writable), NOW), (None, 'invalid_wi_facts'))
        self.assertEqual(wi_facts.load(None, NOW), (None, 'source_not_configured'))


if __name__ == '__main__':
    unittest.main()
