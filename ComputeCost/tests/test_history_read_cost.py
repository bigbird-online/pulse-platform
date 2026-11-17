"""No disk cache or relaxed validation: share immutable history and its digest work."""
import json, os, tempfile, unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from pulse_compute_cost.ledger import UsageLedger, LedgerError
from pulse_compute_cost.receipts import Receipt, canonical, digest_sequence
from pulse_compute_cost.receipt_sources import usage_receipt

AT='2026-10-01T00:00:00Z'
def fact(i=0):
    return usage_receipt('cc',str(i),{'input_tokens':2,'output_tokens':3},
        at=AT,observed=AT,model='test',final=True,mapping='cc_message.v1')

class HistoryReadCostTests(unittest.TestCase):
    def test_snapshot_and_append_do_not_decode_old_payloads_again(self):
        with tempfile.TemporaryDirectory() as d:
            writer=UsageLedger(d);writer.ingest_receipts([fact(0),fact(1)])
            reader=UsageLedger(d); first=reader.receipt_snapshot()
            def no_old(row):
                if row.fact_ref in {r.fact_ref for r in first['facts']}:
                    raise AssertionError('old payload decoded again for a snapshot')
                return json.loads(row._payload_json)
            with patch.object(Receipt,'payload',property(no_old)):
                historical=reader.receipt_snapshot(watermark=1)
                reader.ingest_receipts([fact(2)])
                latest=reader.receipt_snapshot()
            self.assertEqual(historical,first)
            self.assertEqual(latest['sha256'],digest_sequence(r.to_dict() for r in latest['facts']))
            self.assertEqual(reader.receipt_snapshot(watermark=0)['sha256'],digest_sequence([]))
            self.assertEqual(reader.receipt_snapshot(as_of=first['recorded_at']),first)

    def test_cold_history_shares_only_immutable_bytes_and_strings(self):
        with tempfile.TemporaryDirectory() as d:
            rows=[fact(i) for i in range(12)];writer=UsageLedger(d);writer.ingest_receipts(rows)
            got=UsageLedger(d).receipt_snapshot()['facts']
            self.assertEqual([r.to_dict() for r in rows],[r.to_dict() for r in got])
            self.assertTrue(all(r._payload_json is got[0]._payload_json for r in got))
            self.assertTrue(all(r.producer is got[0].producer for r in got))
            self.assertFalse(hasattr(got[0],'__dict__'))
            changed=got[0].payload;changed['tokens']['output']=999
            self.assertEqual(got[0].payload['tokens']['output'],3)
            self.assertEqual(got[-1].payload['tokens']['output'],3)

    def test_changed_permissions_and_bytes_are_never_hidden_by_cache(self):
        with tempfile.TemporaryDirectory() as d:
            writer=UsageLedger(d);writer.ingest_receipts([fact()]);reader=UsageLedger(d)
            first=reader.receipt_snapshot();p=next((Path(d)/'receipts').glob('*.json'))
            os.chmod(p,0o644)
            with self.assertRaises(LedgerError):reader.receipt_snapshot()
            os.chmod(p,0o600);self.assertEqual(reader.receipt_snapshot(),first)
            data=json.loads(p.read_text());data['facts'][0]['payload']['tokens']['output']=42
            p.write_bytes(canonical(data))
            with self.assertRaises(LedgerError):reader.receipt_snapshot()

    def test_foreign_append_revalidates_and_preserves_old_digest(self):
        with tempfile.TemporaryDirectory() as d:
            writer=UsageLedger(d);writer.ingest_receipts([fact()]);reader=UsageLedger(d)
            first=reader.receipt_snapshot();writer.ingest_receipts([fact(1)])
            latest=reader.receipt_snapshot()
            self.assertEqual(latest,UsageLedger(d).receipt_snapshot())
            self.assertEqual(reader.receipt_snapshot(watermark=1),first)

    def test_permission_change_invalidates_warm_cache_even_with_unchanged_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            writer=UsageLedger(d);writer.ingest_receipts([fact()]);reader=UsageLedger(d)
            p=next((Path(d)/'receipts').glob('*.json'))
            original_stat=Path.stat;original_lstat=Path.lstat
            before=p.stat()
            def fixed_times(method, path, *args, **kwargs):
                info=method(path,*args,**kwargs)
                if path!=p:return info
                fields=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink',
                        'st_size','st_mtime_ns','st_ctime_ns')
                value={key:getattr(info,key) for key in fields}
                value.update(st_mtime_ns=before.st_mtime_ns,st_ctime_ns=before.st_ctime_ns)
                return SimpleNamespace(**value)
            with patch.object(Path,'stat',lambda path,*a,**kw:fixed_times(original_stat,path,*a,**kw)), \
                 patch.object(Path,'lstat',lambda path,*a,**kw:fixed_times(original_lstat,path,*a,**kw)):
                first=reader.receipt_snapshot()
                os.chmod(p,0o644)
                with self.assertRaises(LedgerError):reader.receipt_snapshot()
                os.chmod(p,0o600)
                self.assertEqual(reader.receipt_snapshot(),first)

    def test_intern_pool_is_bounded_and_does_not_change_values(self):
        from pulse_compute_cost.receipts import _ReceiptReadPool
        pool=_ReceiptReadPool(max_entries=3,max_bytes=200)
        for i in range(30):
            value=('value-'+str(i)).encode();self.assertEqual(pool.share(value),value)
            self.assertLessEqual(len(pool.values),3);self.assertLessEqual(pool.size,200)
        self.assertEqual(pool.share(b'x'*500),b'x'*500)
        self.assertLessEqual(pool.size,200)

    def test_same_size_rewrite_with_same_stat_still_revalidates_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            writer=UsageLedger(d);writer.ingest_receipts([fact()]);reader=UsageLedger(d)
            p=next((Path(d)/'receipts').glob('*.json'))
            original_lstat=Path.lstat;before=p.lstat()
            with patch.object(Path,'lstat',lambda path,*a,**kw:
                              before if path==p else original_lstat(path,*a,**kw)):
                reader.receipt_snapshot()
                original=p.read_bytes()
                changed=original.replace(b'"output":3',b'"output":4')
                self.assertNotEqual(original,changed)
                self.assertEqual(len(original),len(changed))
                p.write_bytes(changed)
                with self.assertRaises(LedgerError):reader.receipt_snapshot()
