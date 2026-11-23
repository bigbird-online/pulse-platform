from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.events import TokenUsage, UsageEvent
from pulse_compute_cost.ledger import LedgerError, UsageLedger


def event(event_id: str, occurred_at: str, observed_at: str, *, output: int = 1) -> UsageEvent:
    return UsageEvent(
        event_id=event_id,
        occurred_at=occurred_at,
        observed_at=observed_at,
        machine="fixture",
        agent="pulse",
        consumer_path="direct/codex",
        source="chatgpt_pro",
        runtime="codex",
        provider="openai-chatgpt",
        model="gpt-fixture",
        effort="high",
        tokens=TokenUsage(input=10, output=output),
        adapter="fixture",
        source_partition="2026-08-01",
        source_position=1,
    )


class UsageLedgerTests(unittest.TestCase):
    def test_legacy_event_without_lineage_replays_as_current_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = UsageLedger(temporary)
            current = event(
                "uev1_fixture_legacy", "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z"
            )
            legacy = current.to_dict()
            legacy.pop("lineage")
            legacy["quality"]["missing_fields"] = [
                name for name in legacy["quality"]["missing_fields"]
                if not name.startswith("lineage.")
            ]
            usage_path = Path(temporary) / "usage" / "2026-08.jsonl"
            usage_path.parent.mkdir(parents=True)
            usage_path.write_text(
                json.dumps(legacy, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            result = ledger.ingest([current])
            self.assertEqual(result.inserted, 0)
            self.assertEqual(result.duplicates, 1)
            self.assertEqual(ledger.read_month("2026-08")[0].lineage, current.lineage)

    def test_replay_is_idempotent_and_keeps_first_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = UsageLedger(temporary)
            original = event(
                "uev1_fixture_same", "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z"
            )
            replay = original.with_observation("2026-08-31T23:59:00Z")
            first = ledger.ingest([original])
            second = ledger.ingest([replay])
            self.assertEqual(first.inserted, 1)
            self.assertEqual(second.inserted, 0)
            self.assertEqual(second.duplicates, 1)
            self.assertEqual(len(ledger.read_month("2026-08")), 1)
            self.assertEqual(ledger.read_month("2026-08")[0].observed_at, original.observed_at)

    def test_cross_month_late_event_goes_to_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = UsageLedger(temporary)
            late = event(
                "uev1_fixture_late", "2026-07-31T15:59:59Z", "2026-08-01T00:00:00Z"
            )
            result = ledger.ingest([late])
            self.assertEqual(result.corrections_inserted, 1)
            self.assertFalse((Path(temporary) / "usage" / "2026-07.jsonl").exists())
            self.assertTrue((Path(temporary) / "corrections" / "2026-07.jsonl").exists())
            self.assertEqual(ledger.read_month("2026-07")[0].event_id, late.event_id)

    def test_same_event_id_with_changed_facts_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = UsageLedger(temporary)
            original = event(
                "uev1_fixture_conflict", "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z"
            )
            changed = event(
                "uev1_fixture_conflict", "2026-08-01T00:00:00Z", "2026-08-01T02:00:00Z",
                output=2,
            )
            ledger.ingest([original])
            with self.assertRaisesRegex(LedgerError, "conflict"):
                ledger.ingest([changed])
            self.assertEqual(ledger.read_month("2026-08")[0].tokens.output, 1)

    def test_cross_month_conflict_is_preflighted_before_any_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = UsageLedger(temporary)
            august = event(
                "uev1_fixture_existing", "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z"
            )
            ledger.ingest([august])
            july_new = event(
                "uev1_fixture_july", "2026-07-01T00:00:00Z", "2026-07-01T01:00:00Z"
            )
            august_conflict = event(
                "uev1_fixture_existing", "2026-08-01T00:00:00Z", "2026-08-01T02:00:00Z",
                output=9,
            )
            with self.assertRaisesRegex(LedgerError, "conflict"):
                ledger.ingest([july_new, august_conflict])
            self.assertEqual(ledger.read_month("2026-07"), [])

    def test_event_id_is_global_across_month_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = UsageLedger(temporary)
            july = event(
                "uev1_fixture_global", "2026-07-01T00:00:00Z", "2026-07-01T01:00:00Z"
            )
            august_same_id = event(
                "uev1_fixture_global", "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z"
            )
            ledger.ingest([july])
            with self.assertRaisesRegex(LedgerError, "conflict"):
                ledger.ingest([august_same_id])
            self.assertEqual(ledger.read_month("2026-08"), [])


class LedgerAppendSectionTests(unittest.TestCase):
    def test_section_spans_exactly_the_transaction_publication(self):
        from pulse_compute_cost.receipts import Receipt
        events = []
        from contextlib import contextmanager
        @contextmanager
        def section():
            events.append('enter'); yield; events.append('exit')
        with tempfile.TemporaryDirectory() as root:
            ledger = UsageLedger(root)
            row = Receipt.make('coverage', ['fixture'], {'domain': 'usage', 'wi': None, 'source_ref': None,
                'status': 'unknown', 'expected': None, 'observed': None, 'watermark': None,
                'reason': None, 'source_detail': None}, occurred_at=None,
                observed_at='2026-10-20T00:00:00+00:00', producer='fixture')
            UsageLedger.append_section = section
            try:
                self.assertEqual(ledger.ingest_receipts([row])['inserted'], 1)
                self.assertEqual(ledger.ingest_receipts([row])['inserted'], 0)
            finally:
                UsageLedger.append_section = None
            self.assertEqual(events, ['enter', 'exit'])  # A duplicate publishes nothing.
            self.assertEqual(len(ledger.receipt_snapshot()['facts']), 1)


class VerifiedReadTests(unittest.TestCase):
    def test_reads_back_the_validated_facts_and_still_rejects_altered_bytes(self):
        from pulse_compute_cost.receipts import Receipt
        with tempfile.TemporaryDirectory() as root:
            ledger = UsageLedger(root)
            rows = [Receipt.make('coverage', ['fixture', n], {'domain': 'usage', 'wi': None, 'source_ref': None,
                'status': 'unknown', 'expected': None, 'observed': None, 'watermark': None,
                'reason': None, 'source_detail': None}, occurred_at=None,
                observed_at='2026-10-20T00:00:00+00:00', producer='fixture', evidence=(f'ccf1_{n:064x}',))
                for n in range(3)]
            ledger.ingest_receipts(rows)
            self.assertEqual(UsageLedger(root).receipt_snapshot()['facts'], rows)
            path = next((Path(root) / 'receipts').glob('*.json'))
            batch = json.loads(path.read_text())
            batch['facts'][0]['payload']['status'] = 'complete'
            path.write_text(json.dumps(batch))
            with self.assertRaises(LedgerError):
                UsageLedger(root).receipt_snapshot()



class IndexedIngest:
    """The same ingest cases, deciding by fact-index point reads (WI754)."""

    def setUp(self):
        from pulse_compute_cost.fact_index import ingest_index_provider
        UsageLedger.index_provider = ingest_index_provider()
        self.addCleanup(setattr, UsageLedger, "index_provider", None)


class IndexedLedgerAppendSectionTests(IndexedIngest, LedgerAppendSectionTests):
    pass


class IndexedVerifiedReadTests(IndexedIngest, VerifiedReadTests):
    pass


class IngestLookupChoiceTests(unittest.TestCase):
    def test_off_creates_no_index_and_on_indexes_every_append(self):
        from pulse_compute_cost.fact_index import ingest_index_provider
        from pulse_compute_cost.receipts import Receipt
        rows = [Receipt.make('coverage', ['fixture', n], {'domain': 'usage', 'wi': None, 'source_ref': None,
            'status': 'unknown', 'expected': None, 'observed': None, 'watermark': None,
            'reason': None, 'source_detail': None}, occurred_at=None,
            observed_at='2026-10-20T00:00:00+00:00', producer='fixture') for n in range(2)]
        with tempfile.TemporaryDirectory() as root:
            ledger = UsageLedger(root)
            ledger.ingest_receipts(rows[:1])
            self.assertEqual(ledger.last_ingest_lookup, "table")
            self.assertFalse((Path(root) / "derived").exists())
            UsageLedger.index_provider = ingest_index_provider()
            self.addCleanup(setattr, UsageLedger, "index_provider", None)
            indexed = UsageLedger(root)
            self.assertEqual(indexed.ingest_receipts(rows)["inserted"], 1)
            self.assertEqual(indexed.last_ingest_lookup, "index")
            self.assertEqual(indexed._index.binding().sequence, 2)
            self.assertEqual(oct((Path(root) / "derived" / "fact-index.sqlite").stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
