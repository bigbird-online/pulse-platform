"""A Pi request carries the WI its own declare_session_wi call declared."""
from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.pi_request_purpose import POLICY, request_attributions, request_bindings, transport
from pulse_compute_cost.receipt_sources import read_pi

OBSERVED = '2026-10-17T12:00:00Z'
SINCE = '2026-10-01T00:00:00Z'
WI = 'wi_20261017_150'


class PiRequestPurposeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'session.jsonl'
        self.rows = [{'type': 'session', 'version': 3, 'id': 'pi-session', 'timestamp': '2026-10-17T05:00:00Z',
                      'cwd': '/synthetic'}]
        self.last = None
        self.minute = 0

    def add(self, message, ident):
        self.minute += 1
        row = {'type': 'message', 'id': ident, 'parentId': self.last,
               'timestamp': f'2026-10-17T05:{self.minute:02d}:00Z', 'message': message}
        self.rows.append(row); self.last = ident
        return row

    def user(self, ident):
        self.add({'role': 'user', 'content': [{'type': 'text', 'text': 'private request ' + ident}]}, ident)

    def assistant(self, ident, *calls):
        self.add({'role': 'assistant', 'model': 'gpt-test', 'provider': 'openai',
                  'content': [{'type': 'toolCall', 'id': c[0], 'name': 'declare_session_wi', 'arguments': c[1]}
                              for c in calls] or [{'type': 'text', 'text': 'work'}],
                  'usage': {'input': 1, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0, 'totalTokens': 2}}, ident)

    def result(self, ident, call, text, *, error=False):
        self.add({'role': 'toolResult', 'toolCallId': call, 'toolName': 'declare_session_wi',
                  'content': [{'type': 'text', 'text': text}], 'isError': error}, ident)

    def claims(self):
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in self.rows))
        usages = read_pi(self.path, OBSERVED)
        diagnostics = []
        bindings = request_bindings([self.path], since=SINCE, diagnostics=diagnostics)
        rows = request_attributions([], usages, bindings, OBSERVED)
        by_target = {r.payload['target_ref']: (r.payload['state'], r.payload['wi']) for r in rows}
        return {u.entity_ref: by_target.get(u.entity_ref) for u in usages if u.kind == 'usage'}, diagnostics, rows

    def test_declared_request_assigns_its_usage_and_next_request_needs_its_own(self):
        self.user('a')
        self.assistant('a1', ('refused', {'work_item_id': WI}))
        self.result('a1r', 'refused', 'wi_only_arguments_required', error=True)
        self.assistant('a2', ('declare', {'wi': WI}))
        self.result('a2r', 'declare', json.dumps(transport(WI)))
        self.assistant('a3')
        self.user('b')
        self.assistant('b1')
        claims, diagnostics, rows = self.claims()
        self.assertEqual(sorted(v for v in claims.values() if v), [('assigned', WI)] * 3)
        self.assertEqual(sum(v is None for v in claims.values()), 1)
        self.assertIn('pi_declaration_refused', diagnostics)
        self.assertTrue(all(r.producer == POLICY for r in rows))

    def test_a_request_declared_none_is_non_wi_work(self):
        self.user('a')
        self.assistant('a1', ('declare', {'wi': 'none'}))
        self.result('a1r', 'declare', json.dumps(transport('none')))
        self.assistant('a2')
        _, _, rows = self.claims()
        self.assertEqual({(r.payload['state'], r.payload['wi'], r.payload['policy'], r.payload['work_kind'])
                          for r in rows}, {('assigned', None, 'non_wi_activity.v1', 'none')})

    def attribute(self, facts=()):
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in self.rows))
        usages = read_pi(self.path, OBSERVED)
        bindings = request_bindings([self.path], since=SINCE, diagnostics=[])
        rows = request_attributions(list(facts), usages, bindings, OBSERVED)
        return {r.payload['target_ref'].split(':')[-1]: (r.payload['state'], r.payload['wi'], r.payload.get('work_kind'))
                for r in rows}, rows

    def test_none_then_wi_in_one_request_switches_from_the_wi_declaration_on(self):
        self.user('a')
        self.assistant('a1', ('chat', {'wi': 'none'}))
        self.result('a1r', 'chat', json.dumps(transport('none')))
        self.assistant('a2', ('work', {'wi': WI}))
        self.result('a2r', 'work', json.dumps(transport(WI)))
        self.assistant('a3')
        _, rows = self.attribute()
        by = {r.payload['target_ref']: r.payload for r in rows}
        states = Counter((p['state'], p['wi'], p.get('work_kind')) for p in by.values())
        # a1 declares none, a2 calls the WI declaration before it returns, a3 works under the WI.
        self.assertEqual(states, Counter({('assigned', None, 'none'): 2, ('assigned', WI, None): 1}))

    def test_incremental_collection_keeps_earlier_none_and_assigns_later_work(self):
        self.user('a')
        self.assistant('a1', ('chat', {'wi': 'none'}))
        self.result('a1r', 'chat', json.dumps(transport('none')))
        self.assistant('a2')
        _, first = self.attribute()
        self.assertEqual({(r.payload['wi'], r.payload.get('work_kind')) for r in first}, {(None, 'none')})
        self.assistant('a3', ('work', {'wi': WI}))
        self.result('a3r', 'work', json.dumps(transport(WI)))
        self.assistant('a4')
        _, second = self.attribute(first)
        # Earlier usage keeps none (a re-issue only adds evidence); later work takes the WI.
        earlier = {r.entity_ref for r in first}
        self.assertTrue(all(r.payload.get('work_kind') == 'none' and r.payload['wi'] is None
                            for r in second if r.entity_ref in earlier))
        self.assertEqual(Counter(r.payload['wi'] for r in second if r.entity_ref not in earlier), Counter({None: 1, WI: 1}))

    def test_two_real_wis_after_none_stay_ambiguous(self):
        self.user('a')
        self.assistant('a1', ('chat', {'wi': 'none'}))
        self.result('a1r', 'chat', json.dumps(transport('none')))
        self.assistant('a2', ('one', {'wi': WI}), ('two', {'wi': 'wi_20261017_151'}))
        self.result('a2r', 'one', json.dumps(transport(WI)))
        self.result('a2s', 'two', json.dumps(transport('wi_20261017_151')))
        self.assistant('a3')
        _, rows = self.attribute()
        self.assertIn(('ambiguous', None), {(r.payload['state'], r.payload['wi']) for r in rows})
        self.assertIn('none', {r.payload.get('work_kind') for r in rows})

    def test_compaction_inside_a_request_keeps_the_request_and_is_its_usage(self):
        self.user('a')
        self.assistant('a1', ('declare', {'wi': WI}))
        self.result('a1r', 'declare', json.dumps(transport(WI)))
        self.assistant('a2')
        self.minute += 1
        # Pi compacts a long running request; later entries continue from it.
        self.rows.append({'type': 'compaction', 'id': 'c1', 'parentId': self.last,
                          'timestamp': f'2026-10-17T05:{self.minute:02d}:00Z', 'summary': 'private summary',
                          'firstKeptEntryId': 'a2', 'tokensBefore': 1000,
                          'usage': {'input': 5, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0, 'totalTokens': 6}})
        self.last = 'c1'
        self.assistant('a3')
        self.user('b')
        self.assistant('b1')
        claims, _, _ = self.claims()
        self.assertEqual(sorted(v for v in claims.values() if v), [('assigned', WI)] * 4)
        self.assertEqual(sum(v is None for v in claims.values()), 1)

    def test_two_wis_in_one_request_are_ambiguous_and_a_wrong_ack_declares_nothing(self):
        self.user('a')
        self.assistant('a1', ('one', {'wi': WI}), ('two', {'wi': 'wi_20261017_151'}))
        self.result('a1r', 'one', json.dumps(transport(WI)))
        self.result('a1s', 'two', json.dumps(transport('wi_20261017_151')))
        self.user('b')
        self.assistant('b1', ('forged', {'wi': WI}))
        self.result('b1r', 'forged', json.dumps(transport('wi_20261017_999')))
        claims, diagnostics, _ = self.claims()
        self.assertIn(('ambiguous', None), claims.values())
        self.assertNotIn(('assigned', WI), claims.values())
        self.assertIn('pi_declaration_result_mismatch', diagnostics)

    def test_registered_source_reads_declarations_only_when_enabled(self):
        from pulse_compute_cost.ledger import UsageLedger
        from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources
        self.user('a')
        self.assistant('a1', ('declare', {'wi': WI}))
        self.result('a1r', 'declare', json.dumps(transport(WI)))
        self.assistant('a2')
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in self.rows))
        source = {'id': 'pi', 'kind': 'pi', 'path': str(self.path.parent)}
        for enabled in (False, True):
            ledger = UsageLedger(self.path.parent / f'ledger-{enabled}')
            if enabled:
                source['session_wi_mcp_since'] = SINCE
            collect_registered_sources({'schema': REGISTRY_SCHEMA, 'sources': [source]}, ledger)
            claims = [r for r in ledger.receipt_snapshot()['facts'] if r.producer == POLICY]
            self.assertEqual(len(claims), 2 if enabled else 0)
            self.assertTrue(all(r.payload['wi'] == WI for r in claims))


if __name__ == '__main__':
    unittest.main()
