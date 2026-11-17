"""A collector without the native files judges recorded request inputs the same way.

Each case runs the real collector over real native fixtures (ledger A), records
what its request readers returned as native inputs, then gives a second
collector with no sources ledger A without those claims plus the inputs
(ledger B). B must reach the same claims.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pulse_compute_cost import cc_request_purpose, mcp_request_purpose, native_request_purpose, pi_request_purpose
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources, read_pi
from pulse_compute_cost.receipts import Receipt, ReceiptError, current_receipts, reference
from pulse_compute_cost.request_inputs import (CC_REQUEST, CODEX_REQUEST, PI_REQUEST, SESSION_DECLARATION,
    cc_values, codex_values, load_read_marks, pi_bindings, pi_values, recorded, save_read_marks, snapshot_inputs,
    version_inputs)

READ_AT = '2026-11-15T00:00:00Z'
WI_A = 'wi_20261113_754'
SOURCE = reference('source', 'fixture')
PRODUCERS = {cc_request_purpose.POLICY, mcp_request_purpose.POLICY, pi_request_purpose.POLICY,
             native_request_purpose.POLICY}


def recorded_inputs(calls):
    """Native inputs from exactly what each request reader handed its stage."""
    cc, sessions, pi, codex = calls
    rows = []
    for call in cc.call_args_list:
        rows += snapshot_inputs(CC_REQUEST, cc_values(call.args[2]), source_ref=SOURCE, read_at=READ_AT)
        rows += snapshot_inputs(SESSION_DECLARATION, call.args[3], source_ref=SOURCE, read_at=READ_AT)
    for call in sessions.call_args_list:
        rows += snapshot_inputs(SESSION_DECLARATION, call.args[2], source_ref=SOURCE, read_at=READ_AT)
    for call in pi.call_args_list:
        rows += snapshot_inputs(PI_REQUEST, pi_values(call.args[2]), source_ref=SOURCE, read_at=READ_AT)
    for call in codex.call_args_list:
        rows += snapshot_inputs(CODEX_REQUEST, codex_values(call.args[1]), source_ref=SOURCE, read_at=READ_AT)
    return rows


def claims(ledger):
    active, conflicts = current_receipts(ledger.receipt_snapshot()['facts'])
    assert not conflicts
    return {r.entity_ref: (r.payload, r.evidence) for r in active if r.producer in PRODUCERS}


def relay(test, registry, ledger, path):
    """Collect A with recording, then B from inputs; return both claim sets."""
    with patch.object(cc_request_purpose, 'request_attributions', wraps=cc_request_purpose.request_attributions) as cc, \
         patch.object(mcp_request_purpose, 'session_attributions', wraps=mcp_request_purpose.session_attributions) as sessions, \
         patch.object(pi_request_purpose, 'request_attributions', wraps=pi_request_purpose.request_attributions) as pi, \
         patch.object(native_request_purpose, 'purpose_attributions', wraps=native_request_purpose.purpose_attributions) as codex:
        collect_registered_sources(registry, ledger, retirements={}, recoveries={})
    inputs = recorded_inputs((cc, sessions, pi, codex))
    test.assertTrue(inputs)
    kept = [r for r in ledger.receipt_snapshot()['facts'] if r.producer not in PRODUCERS]
    other = UsageLedger(path)
    other.ingest_receipts(kept + inputs)
    collect_registered_sources({**registry, 'sources': []}, other, retirements={}, recoveries={})
    expected = claims(ledger)
    test.assertTrue(expected)
    test.assertEqual(claims(other), expected)
    # A repeated push of the same inputs is only a duplicate.
    test.assertEqual(other.ingest_receipts(inputs)['inserted'], 0)
    return expected


def fixture(case_class, name):
    case = case_class(name)
    case.setUp()
    return case


class RecordedRequestInputTests(unittest.TestCase):
    def test_cc_request_bindings_and_session_declarations(self):
        from test_cc_request_purpose import RequestPurposeTests, WI, OTHER_WI
        case = fixture(RequestPurposeTests, 'test_reused_session_has_each_request_and_no_ambient_wi')
        self.addCleanup(case.doCleanups)
        case.request('a', 0, WI); case.request('b', 10, OTHER_WI); case.request('c', 20)
        case.write()
        relay(self, case.registry, case.ledger, case.root / 'relay')

    def test_codex_session_declaration(self):
        from test_mcp_session_wi import SessionWiTests
        case = fixture(SessionWiTests, 'test_late_one_call_covers_prior_and_future_turns')
        self.addCleanup(case.doCleanups)
        case.declare()
        found = relay(self, case.registry, case.ledger, case.root / 'relay')
        self.assertTrue(any(payload['state'] == 'assigned' for payload, _ in found.values()))

    def test_codex_request_inputs(self):
        from test_native_request_purpose import NativePurposeTests
        import test_scl_session_attribution as f
        case = fixture(NativePurposeTests, 'test_two_open_wis_use_each_request_not_latest_session_binding')
        self.addCleanup(case.doCleanups)
        case.submit(f.WI); case.more_native(f.OTHER_WI)
        found = relay(self, case.registry, case.ledger, case.root / 'relay')
        self.assertEqual({payload['wi'] for payload, _ in found.values()}, {f.WI, f.OTHER_WI})

    def test_pi_request_bindings(self):
        from test_pi_request_purpose import PiRequestPurposeTests, WI, SINCE
        case = fixture(PiRequestPurposeTests, 'test_registered_source_reads_declarations_only_when_enabled')
        self.addCleanup(case.doCleanups)
        case.user('a'); case.assistant('a1', ('declare', {'wi': WI}))
        case.result('a1r', 'declare', json.dumps(pi_request_purpose.transport(WI))); case.assistant('a2')
        case.path.write_text(''.join(json.dumps(r) + '\n' for r in case.rows))
        registry = {'schema': REGISTRY_SCHEMA, 'sources': [
            {'id': 'pi', 'kind': 'pi', 'path': str(case.path.parent), 'session_wi_mcp_since': SINCE}]}
        found = relay(self, registry, UsageLedger(case.path.parent / 'ledger'), case.path.parent / 'relay')
        self.assertEqual({payload['wi'] for payload, _ in found.values()}, {WI})


class SnapshotRuleTests(unittest.TestCase):
    KEY = reference('native_session', ['cc', 'fixture'])

    def declarations(self, wi, at):
        return snapshot_inputs(SESSION_DECLARATION, {self.KEY: [{'schema': 'cc_session_wi/v1', 'wi': wi,
            'origin': 'agent_declaration', 'native_scope_ref': self.KEY, 'declared_at': '2026-11-14T00:00:00Z',
            'native_evidence_ref': reference('fixture', wi)}]}, source_ref=SOURCE, read_at=at)

    def push(self, ledger, rows):
        """One stream writer batch; its read marks persist beside the ledger between batches."""
        path = Path(ledger.root) / 'read-marks.json'
        marks = load_read_marks(path)
        versioned = version_inputs(rows, ledger.receipt_snapshot()['facts'], marks)
        if versioned:
            ledger.ingest_receipts(versioned)
        save_read_marks(path, marks)
        return versioned

    def test_a_late_older_value_never_replaces_a_newer_equal_reread(self):
        ledger = UsageLedger(Path(self.tmp()) / 'ledger')
        self.push(ledger, self.declarations(WI_A, '2026-11-15T00:00:00Z'))
        self.assertEqual(self.push(ledger, self.declarations(WI_A, '2026-11-15T02:00:00Z')), [])
        self.assertEqual(self.push(ledger, self.declarations('none', '2026-11-15T01:00:00Z')), [])
        facts = ledger.receipt_snapshot()['facts']
        self.assertEqual(len([r for r in facts if r.kind == 'native_input']), 1)
        self.assertEqual(recorded(facts, SESSION_DECLARATION)[self.KEY][0]['wi'], WI_A)
        # A different value at the re-read's own instant is still a conflict; a later one still wins.
        self.push(ledger, self.declarations('none', '2026-11-15T03:00:00Z'))
        self.assertEqual(recorded(ledger.receipt_snapshot()['facts'], SESSION_DECLARATION)[self.KEY][0]['wi'], 'none')
        other = UsageLedger(Path(self.tmp()) / 'other')
        self.push(other, self.declarations(WI_A, '2026-11-15T00:00:00Z'))
        self.push(other, self.declarations(WI_A, '2026-11-15T02:00:00Z'))
        self.push(other, self.declarations('none', '2026-11-15T02:00:00Z'))
        self.assertEqual(recorded(other.receipt_snapshot()['facts'], SESSION_DECLARATION), {})

    def test_equal_reread_adds_nothing_and_a_b_a_ends_at_a(self):
        ledger = UsageLedger(Path(self.tmp()) / 'ledger')
        self.assertEqual(len(self.push(ledger, self.declarations(WI_A, READ_AT))), 1)
        self.assertEqual(self.push(ledger, self.declarations(WI_A, '2026-11-15T01:00:00Z')), [])
        self.push(ledger, self.declarations('none', '2026-11-15T02:00:00Z'))
        self.push(ledger, self.declarations(WI_A, '2026-11-15T03:00:00Z'))
        facts = ledger.receipt_snapshot()['facts']
        self.assertEqual(len([r for r in facts if r.kind == 'native_input']), 3)
        self.assertFalse(current_receipts(facts)[1])
        self.assertEqual(recorded(facts, SESSION_DECLARATION)[self.KEY][0]['wi'], WI_A)
        self.assertEqual(recorded(facts, SESSION_DECLARATION, {self.KEY}), {})

    def test_a_retried_or_reordered_old_read_never_replaces_a_newer_head(self):
        ledger = UsageLedger(Path(self.tmp()) / 'ledger')
        first = self.declarations(WI_A, READ_AT)
        self.push(ledger, first)
        self.push(ledger, self.declarations('none', '2026-11-15T02:00:00Z'))
        self.assertEqual(self.push(ledger, first), [])
        self.assertEqual(self.push(ledger, self.declarations(WI_A, '2026-11-15T01:00:00Z')), [])
        facts = ledger.receipt_snapshot()['facts']
        self.assertEqual(recorded(facts, SESSION_DECLARATION)[self.KEY][0]['wi'], 'none')
        # A different value read at the head's own instant is a visible conflict.
        self.push(ledger, self.declarations(WI_A, '2026-11-15T02:00:00Z'))
        self.assertEqual(recorded(ledger.receipt_snapshot()['facts'], SESSION_DECLARATION), {})

    def test_concurrent_heads_prove_nothing(self):
        clash = self.declarations(WI_A, READ_AT) + self.declarations('none', READ_AT)
        self.assertEqual(recorded(clash, SESSION_DECLARATION), {})

    def test_values_are_references_wis_and_times_only(self):
        good = self.declarations('none', READ_AT)[0].to_dict()
        for change in (lambda d: d.update(wi='private text'), lambda d: d.update(path='x'),
                       lambda d: d.update(declared_at='/Users/raw')):
            value = json.loads(json.dumps(good)); change(value['payload']['value']['data'][0])
            with self.assertRaises(ReceiptError):
                Receipt.make('native_input', None, value['payload'], entity_ref=value['entity_ref'],
                             occurred_at=value['occurred_at'], observed_at=READ_AT, producer=value['producer'],
                             evidence=tuple(value['evidence']))

    def test_each_class_checks_its_whole_shape(self):
        key = reference('usage', 'fixture')
        for input_class, data in ((PI_REQUEST, {}), (PI_REQUEST, None), (CC_REQUEST, {}), (SESSION_DECLARATION, {}),
                                  (SESSION_DECLARATION, [{'wi': 'none'}]), (CODEX_REQUEST, [[None]]),
                                  (CODEX_REQUEST, [[{'schema': 'codex_request_input/v1'}, None]]),
                                  (PI_REQUEST, {'request_ref': key, 'wis': ['none', 'none'], 'none_until': None,
                                                'evidence': []})):
            with self.subTest(input_class=input_class, data=data), self.assertRaises(ReceiptError):
                snapshot_inputs(input_class, {key: data}, source_ref=SOURCE, read_at=READ_AT)

    def tmp(self):
        import tempfile
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        return directory.name

    def test_codex_declaration_must_join_its_captured_input(self):
        from test_native_request_purpose import NativePurposeTests
        import test_scl_session_attribution as f
        case = fixture(NativePurposeTests, 'test_two_open_wis_use_each_request_not_latest_session_binding')
        self.addCleanup(case.doCleanups)
        case.submit(f.WI)
        with patch.object(native_request_purpose, 'purpose_attributions',
                          wraps=native_request_purpose.purpose_attributions) as codex:
            collect_registered_sources(case.registry, case.ledger, retirements={}, recoveries={})
        values = codex_values(codex.call_args.args[1])
        (key, items), = values.items()
        self.assertIsNotNone(items[0][1])
        snapshot_inputs(CODEX_REQUEST, values, source_ref=SOURCE, read_at=READ_AT)
        for field, wrong in (('request_ref', reference('fixture', 'other request')),
                             ('native_scope_ref', reference('fixture', 'other scope')), ('input_sha256', '0' * 64),
                             ('declared_at', '2000-01-01T00:00:00Z'), ('origin', 'human')):
            changed = json.loads(json.dumps(values)); changed[key][0][1][field] = wrong
            with self.subTest(field=field), self.assertRaises(ReceiptError):
                snapshot_inputs(CODEX_REQUEST, changed, source_ref=SOURCE, read_at=READ_AT)

    def test_an_undeclared_pi_request_round_trips(self):
        from test_pi_request_purpose import PiRequestPurposeTests, SINCE
        case = fixture(PiRequestPurposeTests, 'test_two_wis_in_one_request_are_ambiguous_and_a_wrong_ack_declares_nothing')
        self.addCleanup(case.doCleanups)
        case.user('a'); case.assistant('a1')
        case.path.write_text(''.join(json.dumps(r) + '\n' for r in case.rows))
        raw = pi_request_purpose.request_bindings([case.path], since=SINCE)
        self.assertEqual([b['wis'] for b in raw.values()], [set()])
        rows = snapshot_inputs(PI_REQUEST, pi_values(raw), source_ref=SOURCE, read_at=READ_AT)
        self.assertEqual(pi_bindings(recorded(rows, PI_REQUEST)), pi_bindings(pi_values(raw)))

    def test_pi_projection_is_exact(self):
        from test_pi_request_purpose import PiRequestPurposeTests, WI, SINCE
        case = fixture(PiRequestPurposeTests, 'test_two_wis_in_one_request_are_ambiguous_and_a_wrong_ack_declares_nothing')
        self.addCleanup(case.doCleanups)
        case.user('a'); case.assistant('a1', ('declare', {'wi': 'none'}))
        case.result('a1r', 'declare', json.dumps(pi_request_purpose.transport('none')))
        case.assistant('a2', ('declare2', {'wi': WI}))
        case.result('a2r', 'declare2', json.dumps(pi_request_purpose.transport(WI))); case.assistant('a3')
        case.path.write_text(''.join(json.dumps(r) + '\n' for r in case.rows))
        usages = read_pi(case.path, READ_AT)
        raw = pi_request_purpose.request_bindings([case.path], since=SINCE)
        self.assertTrue(any(b['none_until'] for b in raw.values()))
        direct = pi_request_purpose.request_attributions([], usages, raw, READ_AT)
        projected = pi_request_purpose.request_attributions([], usages, pi_bindings(pi_values(raw)), READ_AT)
        self.assertEqual([r.to_dict() for r in projected], [r.to_dict() for r in direct])


if __name__ == '__main__':
    unittest.main()
