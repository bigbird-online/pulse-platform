"""Nonterminal dispatch coverage stays partial, without becoming an IO failure."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from pulse_compute_cost.receipt_sources import host_usage_coverage, collect_registered_sources, HOST_SCHEMA
from pulse_compute_cost.receipts import reference, current_receipts
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.source_layout import source_has_read_failure, source_has_pending_usage
from test_collaboration_receipts import execution, usage, coverage, AT


class PendingHostCoverageTests(unittest.TestCase):
    def test_pending_keeps_expected_observed_and_partial_status(self):
        for state in ('accepted', 'running'):
            with self.subTest(state=state):
                self.assertEqual(host_usage_coverage([execution(state=state)]),
                                 (1, 0, 'partial', 'host_usage_pending'))

    def test_terminal_and_unknown_missing_usage_are_not_pending(self):
        for state in ('completed', 'failed', 'cancelled', 'unknown'):
            with self.subTest(state=state):
                self.assertEqual(host_usage_coverage([execution(state=state)]), (1, 0, 'partial', None))
        self.assertEqual(host_usage_coverage([execution(state='accepted'), execution(state='completed')]),
                         (1, 0, 'partial', None))

    def test_capture_gap_never_uses_pending_exception(self):
        self.assertEqual(host_usage_coverage([execution(state='accepted'), coverage(status='partial')]),
                         (1, 0, 'partial', None))

    def test_only_proven_older_gaps_are_separate_from_new_dispatch(self):
        from pulse_compute_cost.receipts import Receipt
        ex = execution(state='accepted'); template = coverage(status='partial')
        for at, pending in (('2026-09-12T01:00:00Z', True), (AT, False),
                            ('2026-09-14T01:00:00Z', False), (None, False)):
            gap = Receipt.make('coverage', 'gap-'+str(at), template.payload,
                occurred_at=at, observed_at=AT, producer='test')
            with self.subTest(at=at):
                self.assertEqual(host_usage_coverage([ex,gap])[3], 'host_usage_pending' if pending else None)
                self.assertEqual(gap.payload['status'], 'partial')
        undated = Receipt.make('execution', 'undated', ex.payload, occurred_at=None,
                               observed_at=AT, producer='test')
        self.assertIsNone(host_usage_coverage([undated])[3])

    def test_completed_usage_reconciles_normally(self):
        ex = execution(state='completed')
        self.assertEqual(host_usage_coverage([ex, usage(ex=ex)]), (1, 1, 'complete', None))
        self.assertEqual(host_usage_coverage([]), (0, 0, 'idle', None))

    def test_wrong_kind_unknown_reason_and_read_errors_still_fail(self):
        pending = dict(kind='relay_host', status='partial', reason='host_usage_pending', files_invalid=0, errors={})
        self.assertTrue(source_has_pending_usage(pending))
        self.assertFalse(source_has_read_failure(pending))
        for change in (dict(kind='pi'), dict(reason=None), dict(status='unreachable'),
                       dict(files_invalid=1), dict(errors={'source_unreadable':1})):
            with self.subTest(change=change):
                self.assertFalse(source_has_pending_usage({**pending, **change}))
                self.assertTrue(source_has_read_failure({**pending, **change}))

    def test_registered_host_collection_preserves_gap_and_recovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); source = root/'host.jsonl'; ledger = UsageLedger(root/'ledger')
            registry = {'schema':'computecost_source_registry/v1','ledger_root':str(ledger.root),
                        'sources':[{'id':'fixture','kind':'relay_host','path':str(source)}]}
            event = dict(schema=HOST_SCHEMA, event_ref=reference('event','start'), kind='execution',
                         at=AT, attempt_ref=reference('execution','attempt'), runtime='pi', state='accepted')
            def collect(events):
                source.write_text(''.join(json.dumps(row)+'\n' for row in events))
                result = collect_registered_sources(registry, ledger)
                return result['sources'][0]
            first = collect([event]); old = ledger.receipt_snapshot()
            self.assertEqual(first['status'], 'partial')
            self.assertEqual(first['reason'], 'host_usage_pending')
            active, _ = current_receipts(old['facts'])
            envelope = next(r.payload for r in active if r.kind=='coverage' and r.producer=='registered_collector.v1')
            self.assertEqual((envelope['expected'],envelope['observed']), (1,0))
            self.assertEqual(sum(r.kind=='usage' for r in active), 0)
            terminal = {**event, 'event_ref':reference('event','end'), 'state':'completed'}
            missing = collect([event, terminal])
            self.assertEqual(missing['status'], 'partial')
            self.assertIsNone(missing['reason'])
            consumed = dict(schema=HOST_SCHEMA, event_ref=reference('event','usage'), kind='usage', at=AT,
                attempt_ref=event['attempt_ref'], runtime='pi', granularity='turn_aggregate',
                model='openai/test', mapping='relay_pi.v1', final=True,
                usage={'input_tokens':10,'output_tokens':2,'cache_read_input_tokens':0,'cache_creation_input_tokens':0})
            recovered = collect([event, terminal, consumed])
            self.assertEqual(recovered['status'], 'complete')
            self.assertIsNone(recovered['reason'])
            self.assertEqual(ledger.receipt_snapshot(watermark=old['watermark'])['sha256'], old['sha256'])
