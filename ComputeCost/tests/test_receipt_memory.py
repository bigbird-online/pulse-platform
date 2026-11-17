"""Bound peak copies while preserving the existing receipt/ledger wire contract."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import tempfile
import tracemalloc
import unittest

from pulse_compute_cost.ledger import UsageLedger, LedgerError
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost import receipts


class StreamingReceiptTests(unittest.TestCase):
    def test_sequence_digest_matches_existing_canonical_bytes(self):
        samples = [[], [None, True, 3, -0.0], [{'z':'雪😀', 'a':['quote"', '\\', '\n']}], [[], {}]]
        for rows in samples:
            with self.subTest(rows=rows):
                self.assertEqual(receipts.digest_sequence(iter(rows)), receipts.digest(rows))
        with self.assertRaises(receipts.ReceiptError):
            receipts.digest_sequence(iter([1, float('nan')]))

    def test_hex_digest_matches_legacy_including_chunk_boundaries(self):
        for data in [b'', bytes(range(256)), b'\x00\xff' * (512 * 1024 + 1)]:
            self.assertEqual(receipts.digest_hex(iter([data[:19], data[19:]])), receipts.digest(data.hex()))

    def test_hex_digest_does_not_allocate_whole_hex_string(self):
        data = b'abc' * (4 * 1024 * 1024)
        expected = receipts.digest(data.hex())
        tracemalloc.start()
        try:
            actual = receipts.digest_hex(memoryview(data)[i:i+65536] for i in range(0,len(data),65536))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(actual, expected)
        self.assertLess(peak, 1024 * 1024)

    def test_snapshot_is_identical_without_full_serialization_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            ticks=iter(datetime(2026,9,26,tzinfo=timezone.utc)+timedelta(seconds=i) for i in range(10))
            ledger=UsageLedger(directory,clock=lambda:next(ticks))
            facts=[usage_receipt('cc', ['memory',i], {'input_tokens':1,'output_tokens':0,
                'cache_read_input_tokens':0,'cache_creation_input_tokens':0},
                at='2026-09-26T00:00:00Z',observed='2026-09-26T00:00:00Z',model='test-'+('x'*16384)) for i in range(200)]
            ledger.ingest_receipts(facts)
            expected=receipts.digest([r.to_dict() for r in facts])
            tracemalloc.start()
            try:
                snapshot=ledger.receipt_snapshot()
                _,peak=tracemalloc.get_traced_memory()
            finally:tracemalloc.stop()
            self.assertEqual(snapshot['sha256'],expected)
            self.assertEqual(snapshot['facts'],facts)
            self.assertEqual(snapshot['watermark'],1)
            self.assertLess(peak, 2*1024*1024, 'snapshot copies the full receipt payload')
            reopened=UsageLedger(directory)
            self.assertEqual(reopened.receipt_snapshot(),snapshot)
            self.assertEqual(reopened.ingest_receipts(facts),{'inserted':0,'duplicates':200,'watermark':1})
            self.assertEqual(reopened.receipt_snapshot(watermark=0)['sha256'],receipts.digest([]))
            # Persisted transaction bytes and chain retain the original wire format.
            transaction=json.loads((Path(directory)/'receipts/00000000000000000001.json').read_text())
            self.assertEqual(transaction['sha256'], receipts.digest({k:v for k,v in transaction.items() if k!='sha256'}))
            self.assertEqual(len(transaction['facts']),200)

    def test_invalid_chain_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=UsageLedger(directory)
            row=usage_receipt('cc','tamper',{'input_tokens':1,'output_tokens':0,
                'cache_read_input_tokens':0,'cache_creation_input_tokens':0},
                at='2026-09-26T00:00:00Z',observed='2026-09-26T00:00:00Z',model='test')
            ledger.ingest_receipts([row])
            p=Path(directory)/'receipts/00000000000000000001.json';value=json.loads(p.read_text());value['facts'][0]['producer']='modified';p.write_text(json.dumps(value))
            with self.assertRaises(LedgerError):UsageLedger(directory).receipt_snapshot()

    def test_decoded_payload_cache_has_a_memory_budget(self):
        from pulse_compute_cost.collaboration_query import _PayloadReads
        rows=[usage_receipt('cc',['decoded-memory',i],{'input_tokens':1,'output_tokens':0,
            'cache_read_input_tokens':0,'cache_creation_input_tokens':0},
            at='2026-09-26T00:00:00Z',observed='2026-09-26T00:00:00Z',
            model='test-'+('x'*16384)) for i in range(1200)]
        cache=_PayloadReads()
        tracemalloc.start()
        try:
            for row in rows:
                self.assertEqual(cache.read(row),row.payload)
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertLess(peak,12*1024*1024)
        # Eviction only discards a decoded view; the original fact is unchanged.
        self.assertEqual(cache.read(rows[0]),rows[0].payload)

if __name__=='__main__':unittest.main()
