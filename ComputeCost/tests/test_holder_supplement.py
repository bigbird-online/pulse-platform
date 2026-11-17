"""Undeclared work takes the one WI its session wrote; declarations always win."""
from pathlib import Path
import json
import os
import tempfile
import unittest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.holder_supplement import (POLICY, read_session_writes, session_key,
                                                  supplement_attributions)
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import ReceiptError, current_receipts, reference
import test_cc_request_purpose as cc_requests
from test_scheduled_job_attribution import AT, codex_usage, scheduled_stream
from test_mcp_cc_session_wi import SESSION, WI, OTHER_WI, CATALOG

NAMESPACE, SURFACE, ACTOR = 'pulse-production', 'surface_v1_test', 'pulse'
THIRD_WI = 'wi_20260101_103'


def facts_document(writes, *, exported=True, created=None):
    """A stats-facts export; ``writes`` maps WI -> [(native id, ISO time)]."""
    items = []
    for wi, rows in writes.items():
        item = {'id': wi, **({'created_at': created[wi]} if created and wi in created else {})}
        if exported:
            item['session_writes'] = [{'session': session_key(NAMESPACE, ACTOR, native, SURFACE),
                                       'actor': ACTOR, 'at': at} for native, at in rows]
        items.append(item)
    return {'schema': 'harness.stats_facts/v1', 'read_only': True, 'source': 'journal',
            'namespace': NAMESPACE, 'items': items}


class CcRequestSupplementTests(unittest.TestCase):
    request, write, collect, target = (cc_requests.RequestPurposeTests.request, cc_requests.RequestPurposeTests.write,
                                       cc_requests.RequestPurposeTests.collect, cc_requests.RequestPurposeTests.target)

    def setUp(self):
        cc_requests.RequestPurposeTests.setUp(self)
        self.facts = self.root/'wi-facts-v1.json'
        self.registry.update(core_session_writes={'path': str(self.facts), 'surface': SURFACE},
                             required_receipt_features=['holder_supplement.v1'])
        self.writes = {}

    def held(self, wi, minute, second=40):
        self.writes.setdefault(wi, []).append((SESSION, f'2026-01-01T01:{minute:02d}:{second:02d}Z'))

    def export(self, **kwargs):
        kwargs.setdefault('created', getattr(self, 'created', None))
        self.facts.write_text(json.dumps(facts_document(self.writes, **kwargs)))
        os.chmod(self.facts, 0o600)

    def groups(self):
        result = query_collaboration(self.ledger, CollaborationQuery(scope='activity', from_date='2026-01-01',
            to_date='2026-01-02', timezone='UTC', group_by=('wi',)), catalog=CATALOG)
        return {g['key']['wi']: g for g in result['groups']}

    def supplements(self):
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()['facts'])
        self.assertFalse(conflicts)
        return {r.payload['target_ref']: (r.payload['state'], r.payload['wi'])
                for r in active if r.producer == POLICY}

    def test_declaration_first_then_single_holder_then_ambiguous(self):
        self.request('a', 0, WI); self.held(OTHER_WI, 2)      # declared: the write does not override
        self.request('b', 10); self.held(OTHER_WI, 12)        # one WI written: supplement
        self.request('c', 20); self.held(WI, 22); self.held(THIRD_WI, 23)  # two WIs: ambiguous
        self.request('d', 30)                                 # nothing written: stays unknown
        self.export(); self.collect()
        states = self.supplements()
        self.assertNotIn(self.target('a-before'), states)
        self.assertEqual(states[self.target('b-before')], ('assigned', OTHER_WI))
        self.assertEqual(states[self.target('c-after')], ('ambiguous', None))
        self.assertNotIn(self.target('d-before'), states)
        groups = self.groups()
        self.assertEqual(groups[WI]['attribution']['sources'], {'declared': {'event_count': 3, 'priced_machine_cash_usd': '220'}})
        self.assertEqual(set(groups[OTHER_WI]['attribution']['sources']), {'holder_supplement'})
        self.assertEqual(groups[OTHER_WI]['usage']['event_count'], 2)
        self.assertEqual(groups['ambiguous']['usage']['event_count'], 2)
        self.assertEqual(groups['ambiguous']['attribution']['sources']['holder_supplement']['event_count'], 2)
        self.assertEqual(groups['unassigned']['attribution']['sources']['unattributed']['event_count'], 2)
        self.assertIn('attribution_unknown', groups['unassigned']['cost']['reasons'])
        self.assertEqual(self.collect()['inserted'], 0)

    def test_exploration_before_a_created_wi_belongs_to_it_otherwise_none(self):
        self.request('a', 0)                                  # explores, declares nothing
        self.request('b', 10, WI); self.held(WI, 12)          # creates WI in this request
        self.request('c', 20)                                 # undeclared, after declared work
        self.request('d', 30, OTHER_WI); self.held(OTHER_WI, 32)  # continues an older WI
        self.request('e', 40)                                 # not decided yet
        self.created = {WI: '2026-01-01T01:12:40Z', OTHER_WI: '2025-12-01T00:00:00Z'}
        self.export(); self.collect()
        groups = self.groups()
        self.assertEqual(groups[WI]['attribution']['sources']['exploration']['event_count'], 2)
        # A later undeclared request continues the session's work: it stays a
        # visible gap, not non-WI work.
        self.assertNotIn('none', groups)
        self.assertEqual(groups['unassigned']['usage']['event_count'], 4)
        self.assertEqual(self.collect()['inserted'], 0)

    def test_opening_exploration_that_creates_no_wi_is_non_wi_work(self):
        self.request('a', 0)                                  # opens by exploring
        self.request('b', 10, OTHER_WI); self.held(OTHER_WI, 12)  # then continues an older WI
        self.created = {OTHER_WI: '2025-12-01T00:00:00Z'}
        self.export(); self.collect()
        groups = self.groups()
        self.assertEqual(groups['none']['attribution']['sources']['exploration']['event_count'], 2)
        self.assertNotIn('unassigned', groups)

    def test_a_later_write_withdraws_an_earlier_exploration_conclusion(self):
        from pulse_compute_cost.receipts import EXPLORATION_POLICY
        self.request('a', 0)
        self.request('b', 10, OTHER_WI); self.held(OTHER_WI, 12)
        self.created = {OTHER_WI: '2025-12-01T00:00:00Z'}
        self.export(); self.collect()
        self.assertIn('none', self.groups())
        self.held(WI, 2); self.export(); self.collect()   # request a now wrote its own WI
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()['facts'])
        self.assertFalse(conflicts)
        self.assertEqual({r.payload['state'] for r in active if r.payload.get('policy') == EXPLORATION_POLICY},
                         {'unassigned'})
        self.assertEqual(set(self.groups()), {WI, OTHER_WI})

    def test_a_later_write_revises_the_supplement_and_a_late_declaration_wins(self):
        self.request('a', 0); self.held(WI, 2); self.export(); self.collect()
        self.assertEqual(self.supplements()[self.target('a-after')], ('assigned', WI))
        self.held(OTHER_WI, 6); self.export(); self.collect()
        self.assertEqual(self.supplements()[self.target('a-after')], ('ambiguous', None))
        # A declaration that arrives later replaces the supplement in the query.
        self.main = []; self.request('a', 0, OTHER_WI); self.collect()
        self.assertEqual(set(self.groups()[OTHER_WI]['attribution']['sources']), {'declared'})

    def test_without_feature_or_with_an_old_export_nothing_is_supplemented(self):
        self.request('a', 0); self.held(WI, 2)
        self.export(exported=False); self.collect()
        self.assertEqual(self.supplements(), {})
        self.registry['required_receipt_features'] = []; self.export(); self.collect()
        self.assertEqual(self.supplements(), {})
        self.assertEqual(set(self.groups()), {'unassigned'})

    def test_unreadable_export_is_a_diagnostic_not_a_failed_collection(self):
        self.request('a', 0); self.held(WI, 2); self.export(); os.chmod(self.facts, 0o644)
        result = self.collect()
        self.assertEqual(self.supplements(), {})
        self.assertIn('holder_supplement_unavailable', result['sources'][0]['diagnostics'])


class SessionAndQueryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.ledger = UsageLedger(self.root/'ledger')

    def query(self):
        result = query_collaboration(self.ledger, CollaborationQuery(scope='activity', from_date='2026-01-01',
            to_date='2026-01-02', timezone='UTC', group_by=('wi',)), catalog=CATALOG)
        return {g['key']['wi']: g for g in result['groups']}

    def session(self, writes, thread='thread-1'):
        key = session_key(NAMESPACE, ACTOR, thread, SURFACE)
        return ([{'scope': reference('native_session', ['codex', thread]), 'key': key, 'mode': 'session',
                  'starts': []}], {key: writes})

    def test_codex_session_that_wrote_one_wi_is_wholly_that_wi(self):
        from datetime import datetime, timezone
        rows = [codex_usage('one', 1), codex_usage('two', 50)]
        sessions, writes = self.session([(datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc), WI)])
        self.ledger.ingest_receipts([*rows, *supplement_attributions(rows, sessions, writes, AT)])
        groups = self.query()
        self.assertEqual(set(groups), {WI})
        self.assertEqual(groups[WI]['attribution']['sources']['holder_supplement']['event_count'], 2)

    def test_codex_declaration_is_never_replaced(self):
        from datetime import datetime, timezone
        rows = [codex_usage('one', 1), codex_usage('two', 50, wi=OTHER_WI)]
        sessions, writes = self.session([(datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc), WI)])
        self.ledger.ingest_receipts([*rows, *supplement_attributions(rows, sessions, writes, AT)])
        groups = self.query()
        self.assertEqual(groups[OTHER_WI]['attribution']['sources'], {'declared': {'event_count': 1, 'priced_machine_cash_usd': '102'}})
        self.assertEqual(set(groups[WI]['attribution']['sources']), {'holder_supplement'})

    def scheduled(self, wi=None):
        return scheduled_stream(self.root/'host.jsonl', wi)

    def test_configured_job_wi_then_held_wi_then_job_name(self):
        from datetime import datetime, timezone
        rows = [codex_usage('one', 1)]
        sessions, writes = self.session([(datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc), OTHER_WI)])
        held = supplement_attributions(rows, sessions, writes, AT)
        self.ledger.ingest_receipts([*rows, *held, *self.scheduled()])
        self.assertEqual(set(self.query()), {OTHER_WI})      # the run's own write beats the job name
        configured = UsageLedger(self.root/'configured')
        configured.ingest_receipts([*rows, *held, *self.scheduled(WI)])
        self.ledger = configured
        self.assertEqual(set(self.query()), {WI})            # an explicit job WI beats both
        self.assertEqual(set(self.query()[WI]['attribution']['sources']), {'scheduled_job'})

    def test_export_reader_rejects_unsafe_or_malformed_writes(self):
        path = self.root/'facts.json'
        path.write_text(json.dumps(facts_document({WI: [('n', AT)]}))); os.chmod(path, 0o600)
        namespace, writes, actors = read_session_writes(path)
        self.assertEqual((namespace, actors), (NAMESPACE, {ACTOR}))
        self.assertEqual(list(writes), [session_key(NAMESPACE, ACTOR, 'n', SURFACE)])
        os.chmod(path, 0o644)
        with self.assertRaises(ReceiptError):
            read_session_writes(path)
        bad = facts_document({WI: [('n', AT)]}); bad['items'][0]['session_writes'][0]['session'] = 'session.raw'
        path.write_text(json.dumps(bad)); os.chmod(path, 0o600)
        with self.assertRaises(ReceiptError):
            read_session_writes(path)


if __name__ == '__main__':
    unittest.main()
