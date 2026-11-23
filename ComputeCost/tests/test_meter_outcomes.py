"""合同第 5 节末：WI 产出从内核导出增量并入 wi_outcomes。"""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ComputeCost'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_meter import outcomes, publish  # noqa: E402
from test_meter_publish import NOW, WI, wi_facts  # noqa: E402
from test_meter_views import Fixture, session as _session  # noqa: E402


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.f = Fixture(self.root)
        self.facts = self.root / 'wi-facts.json'
        self.facts.write_text(json.dumps(wi_facts()))
        self.conn = publish.connect(self.f.path)
        self.addCleanup(self.conn.close)

    def test_three_kinds_from_export(self):
        found = sorted((r[0], r[1], r[2], r[4]) for r in outcomes.rows(wi_facts()))
        self.assertEqual(found, [(WI, 'pull_request_merged', 'org/PULSE#9', 'pulse'),
                                 (WI, 'wi_completed', 'rev:7', 'pulse'),
                                 ('wi_20261121_002', 'work_accepted', 'rev:3', 'emi')])

    def test_unreadable_entries_are_skipped_not_guessed(self):
        facts = wi_facts()
        facts['items'][0]['completions'][0]['acceptance'] = 'short'
        facts['items'][0]['merged_pull_requests'][0]['merged_at'] = 'yesterday'
        self.assertEqual([r[1] for r in outcomes.rows(facts)], ['work_accepted'])
        with self.assertRaises(ValueError):
            outcomes.rows({'schema': 'other'})

    def test_sync_is_incremental_and_idempotent(self):
        first = outcomes.sync(self.conn, self.facts, now=NOW)
        self.assertEqual((first['status'], first['changed']), ('read', 3))
        self.assertEqual(outcomes.sync(self.conn, self.facts, now=NOW), {'status': 'unchanged', 'changed': 0})
        self.facts.write_text(json.dumps(wi_facts(generated_at=(NOW + timedelta(minutes=2)).isoformat())))
        self.assertEqual(outcomes.sync(self.conn, self.facts, now=NOW)['changed'], 0)  # 只有导出时间变了
        facts = wi_facts()
        facts['items'][1]['completions'][0]['actor'] = 'amanda'
        self.facts.write_text(json.dumps(facts))
        self.assertEqual(outcomes.sync(self.conn, self.facts, now=NOW)['changed'], 1)

    def test_session_writes_are_kept_for_the_holder_layer(self):
        facts = wi_facts()
        key = outcomes.session_key('pulse-production', 'pulse', 'sess-1', 'surface_v1_local_macos')
        facts['items'][0]['session_writes'] = [{'actor': 'pulse', 'at': '2026-11-20T03:00:00+00:00', 'session': key}]
        facts['collaborations'] = [{'offered_by': {'actor': 'pulse', 'surface': 'surface_v1_local_macos'}}]
        self.facts.write_text(json.dumps(facts))
        outcomes.sync(self.conn, self.facts, now=NOW)
        self.f.usage('e1', sid='sess-1', turn_id='t1')
        self.assertEqual(outcomes.holder_claims(self.conn), [(_session('cc', 'sess-1'), '*', WI)])

    def test_owners_are_kept(self):
        outcomes.sync(self.conn, self.facts, now=NOW)
        self.assertEqual(sorted(self.conn.execute('SELECT * FROM wi_owners')),
                         [(WI, 'pulse'), ('wi_20261121_002', 'emi')])

    def test_coverage(self):
        self.assertEqual(outcomes.coverage(self.conn, now=NOW), 'unavailable')
        outcomes.sync(self.conn, self.facts, now=NOW)
        self.assertEqual(outcomes.coverage(self.conn, now=NOW), 'complete')
        self.assertEqual(outcomes.coverage(self.conn, now=NOW + timedelta(hours=3)), 'observed')

    def test_window_and_item_refs(self):
        outcomes.sync(self.conn, self.facts, now=NOW)
        rows = outcomes.in_window(self.conn, '2026-11-20', '2026-11-21')
        self.assertEqual([(r['kind'], r['ref']) for r in rows],
                         [('pull_request_merged', 'org/PULSE#9'), ('wi_completed', 'rev:7')])
        item = outcomes.item(rows[1])
        self.assertEqual((item['wi'], item['status'], item['counted'], item['verification_level']),
                         (WI, 'accepted', True, 'human'))
        self.assertTrue(item['outcome_ref'].startswith('ccf1_'))


if __name__ == '__main__':
    unittest.main()
