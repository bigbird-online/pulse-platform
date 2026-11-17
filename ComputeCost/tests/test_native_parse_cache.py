"""Parser reuse must preserve late evidence, source failures and valid prefixes."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from pulse_compute_cost.ledger import LedgerError, UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.native_parse_cache import NativeParseCache, input_signature, _bundle_lines
from pulse_compute_cost.calculation_cache import CalculationCache, engine_digest
from pulse_compute_cost.receipts import Receipt, canonical, current_receipts, digest, reference

AT = "2026-09-16T01:00:00Z"


class NativeParseCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "native"; self.source.mkdir(mode=0o700)
        self.ledger = UsageLedger(self.root/"ledger"); self.ledger.root.mkdir(mode=0o700)
        self.registry = {"schema":"computecost_source_registry/v1","ledger_root":str(self.ledger.root),
                         "sources":[{"id":"pi","kind":"pi","path":str(self.source)}]}
        self.write("one",100)

    def write(self, identity, n):
        rows = [{"type":"session","id":identity,"timestamp":AT},
            {"type":"message","id":"request","timestamp":AT,"parentId":None,
             "message":{"role":"assistant","model":"openai/test","provider":"openai",
                        "usage":{"input":n,"output":10,"cacheRead":20,"cacheWrite":0}}}]
        path = self.source/(identity+".jsonl")
        path.write_text("".join(json.dumps(r)+"\n" for r in rows)); path.chmod(0o600)
        return path

    def collect(self, force=False):
        return collect_registered_sources(self.registry, self.ledger, force_recompute=force)

    def test_same_native_bytes_not_reparsed_across_collections(self):
        first = self.collect(); before = self.ledger.receipt_snapshot()
        warm = self.collect()
        self.assertEqual(first["parser_cache"],{"parsed_files":1,"reused_files":0})
        self.assertEqual(warm["parser_cache"],{"parsed_files":0,"reused_files":1})
        self.assertEqual(warm["inserted"],0)
        self.assertEqual(self.ledger.receipt_snapshot()["sha256"], before["sha256"])
        forced = self.collect(True)
        self.assertEqual(forced["parser_cache"]["parsed_files"],1)
        self.assertEqual(forced["inserted"],0)

    def test_indexed_revisions_match_the_linear_rescan_with_foreign_appends(self):
        from pulse_compute_cost.receipt_sources import _revision, read_pi
        first = read_pi(self.root/"native/one.jsonl", AT)
        refined = read_pi(self.write("one", 150), AT)
        foreign = read_pi(self.write("one", 120), AT)
        cache, indexed, simple = NativeParseCache(self.root/"cache"), [], []
        cache._revise(indexed, first)
        for row in first:
            _revision(simple, row)
        # Another reader appends to the same list between parsed files.
        indexed.extend(foreign); simple.extend(foreign)
        cache._revise(indexed, refined + first)
        for row in refined + first:
            _revision(simple, row)
        self.assertEqual([r.to_dict() for r in indexed], [r.to_dict() for r in simple])
        # The repeated first file is recognised through the index, not appended again.
        self.assertLess(len(indexed), len(first) * 2 + len(foreign) + len(refined))

    def test_new_file_only_parses_new_file(self):
        self.collect(); self.write("two",201)
        next_run = self.collect()
        self.assertEqual(next_run["parser_cache"],{"parsed_files":1,"reused_files":1})
        before = self.ledger.receipt_snapshot()
        self.assertEqual(self.collect(True)["inserted"],0)
        self.assertEqual(self.ledger.receipt_snapshot()["sha256"],before["sha256"])

    def test_same_size_rewrite_with_restored_mtime_is_not_a_hit(self):
        self.collect(); path = self.source/"one.jsonl"; st = path.stat()
        path.write_bytes(path.read_bytes().replace(b'100',b'999'))
        os.utime(path,ns=(st.st_atime_ns,st.st_mtime_ns))
        changed = self.collect()
        self.assertEqual(changed["parser_cache"]["parsed_files"],1)
        _, conflicts = current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertTrue(conflicts)

    def test_torn_file_keeps_verified_prefix_without_poisoning_cache(self):
        path=self.source/"one.jsonl"
        with path.open('ab') as stream: stream.write(b'{')
        bad=self.collect()
        active,_=current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertEqual(len([r for r in active if r.kind=='usage']),1)
        self.assertEqual(bad['sources'][0]['status'],'partial')
        self.write('one',100)
        self.assertNotEqual(self.collect()['sources'][0]['status'],'partial')

    def test_missing_registered_source_is_not_a_cached_success(self):
        self.registry['sources'][0]['path']=str(self.source/'one.jsonl')
        self.collect(); (self.source/'one.jsonl').unlink()
        result=self.collect()
        self.assertEqual(result['sources'][0]['status'],'unreachable')
        self.assertEqual(result['parser_cache']['reused_files'],0)

    def test_changed_input_dependencies_and_path_are_part_of_key(self):
        folder=self.root/'inputs';folder.mkdir(mode=0o700)
        source={'human_input_directory':str(folder)}
        initial=input_signature(source)
        record=folder/('a'*64+'.json');record.write_text('{}');record.chmod(0o600)
        changed=input_signature(source)
        self.assertNotEqual(initial,changed)
        record.write_text('{"changed":true}')
        self.assertNotEqual(changed,input_signature(source))
        record.chmod(0o644)
        with self.assertRaises(ValueError):input_signature(source)

    def test_bad_cache_falls_back_to_source(self):
        self.collect()
        for path in (self.ledger.root/'native-cache').glob('*.json'):path.write_text('{broken')
        self.assertEqual(self.collect()['parser_cache']['parsed_files'],1)

    def receipt_row(self):
        return Receipt.make("coverage", "transport-fixture", {
            "domain":"usage", "wi":None, "source_ref":reference("source","peer"),
            "status":"unknown", "expected":None, "observed":None, "watermark":None,
            "reason":"execution_inventory_not_observed", "source_detail":None,
        }, occurred_at=None, observed_at=AT, producer="fixture")

    def test_receipt_bundle_cursor_commits_only_after_ledger_ingest(self):
        peer=self.root/"peer.jsonl"; row=self.receipt_row()
        peer.write_bytes(canonical(row.to_dict())+b"\n"); peer.chmod(0o600)
        registry={"schema":"computecost_source_registry/v1","ledger_root":str(self.ledger.root),
                  "sources":[{"id":"peer","kind":"receipts","path":str(peer)}]}
        with patch.object(self.ledger,"ingest_receipts",side_effect=LedgerError("fixture failure")):
            with self.assertRaises(LedgerError):
                collect_registered_sources(registry,self.ledger)
        self.assertFalse(list((self.ledger.root/"native-cache").glob("receipt-bundle-*.json")),
                         "failed ingest must not advance an omission cursor")
        recovered=collect_registered_sources(registry,self.ledger)
        self.assertEqual(recovered["inserted"],1)
        warm=collect_registered_sources(registry,self.ledger)
        self.assertEqual(warm["inserted"],0)
        self.assertEqual(warm["parser_cache"],{"parsed_files":0,"reused_files":1})

    def test_v2_cursor_is_untrusted_and_v3_requires_committed_watermark(self):
        peer=self.root/"peer.jsonl"; row=self.receipt_row()
        data=canonical(row.to_dict())+b"\n"; peer.write_bytes(data); peer.chmod(0o600)
        root=self.ledger.root/"native-cache"; root.mkdir(mode=0o700)
        slot="receipt-bundle-"+digest(str(peer))
        CalculationCache(root).put(slot,[engine_digest(),"receipt-bundle-prefix/v2"],{
            "bytes":len(data),"sha256":sha256(data).hexdigest(),"count":1,
            "latest_usage_at":None,"ledger_watermark":99})
        cache=NativeParseCache(root)
        rows,_,_=cache.receipt_bundle(peer,data,ledger_watermark=0)
        self.assertEqual([r.fact_ref for r in rows],[row.fact_ref])
        self.assertEqual(cache.summary(),{"parsed_files":1,"reused_files":0})
        cache.commit_receipt_bundles(3)
        rolled=NativeParseCache(root)
        rows,_,_=rolled.receipt_bundle(peer,data,ledger_watermark=2)
        self.assertEqual([r.fact_ref for r in rows],[row.fact_ref])
        committed=NativeParseCache(root)
        rows,_,_=committed.receipt_bundle(peer,data,ledger_watermark=3)
        self.assertEqual(rows,[])
        self.assertEqual(committed.summary(),{"parsed_files":0,"reused_files":1})

    def test_receipt_bundle_replays_prefix_when_tail_names_rebased_source_parent(self):
        peer=self.root/"peer.jsonl"
        source_ref=reference("source","remote-native")
        def coverage(status, reason, *, supersedes=None):
            return Receipt.make("coverage", "remote-coverage", {
                "domain":"usage", "wi":None, "source_ref":source_ref,
                "status":status, "expected":None, "observed":None, "watermark":status,
                "reason":reason, "source_detail":None,
            }, occurred_at=None, observed_at=AT, producer="registered_collector.v1",
               supersedes=supersedes)
        base=coverage("unknown","base")
        # Local evidence refinement creates a durable successor of the source
        # base without changing the source fact_ref.
        self.ledger.ingest_receipts([base])
        richer=Receipt.from_dict({**base.to_dict(),
            "evidence":[reference("evidence","local-refinement")]})
        self.ledger.ingest_receipts([richer])
        parent=coverage("partial","parent",supersedes=base.fact_ref)
        peer.write_bytes(canonical(base.to_dict())+b"\n"+canonical(parent.to_dict())+b"\n")
        peer.chmod(0o600)
        registry={"schema":"computecost_source_registry/v1","ledger_root":str(self.ledger.root),
                  "sources":[{"id":"peer","kind":"receipts","path":str(peer)}]}
        first=collect_registered_sources(registry,self.ledger)
        self.assertGreater(first["inserted"],0)
        facts=self.ledger.receipt_snapshot()["facts"]
        self.assertNotIn(parent.fact_ref,{r.fact_ref for r in facts})
        rebased=[r for r in facts if (r.kind,r.entity_ref,r.occurred_at,r.producer,r._payload_json)==
                 (parent.kind,parent.entity_ref,parent.occurred_at,parent.producer,parent._payload_json)]
        self.assertEqual(len(rebased),1)
        self.assertNotEqual(rebased[0].fact_ref,parent.fact_ref)

        child=coverage("unreachable","child",supersedes=parent.fact_ref)
        with peer.open("ab") as stream:
            stream.write(canonical(child.to_dict())+b"\n")
        recovered=collect_registered_sources(registry,self.ledger)
        self.assertGreater(recovered["inserted"],0)
        self.assertEqual(recovered["parser_cache"],{"parsed_files":1,"reused_files":0})
        facts=self.ledger.receipt_snapshot()["facts"]
        child_assertions=[r for r in facts if (r.kind,r.entity_ref,r.occurred_at,r.producer,r._payload_json)==
                          (child.kind,child.entity_ref,child.occurred_at,child.producer,child._payload_json)]
        self.assertEqual(len(child_assertions),1)
        self.assertEqual(collect_registered_sources(registry,self.ledger)["inserted"],0)

    def test_engine_change_reparses(self):
        self.collect()
        with patch('pulse_compute_cost.native_parse_cache.engine_digest',return_value='next-engine'):
            self.assertEqual(self.collect()['parser_cache']['parsed_files'],1)


class ReceiptBufferMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = NativeParseCache(self.root / "cache")
        self.peer = self.root / "peer.jsonl"
        self.row = Receipt.make("coverage", "buffer-fixture", {
            "domain":"usage", "wi":None, "source_ref":reference("source","buffer"),
            "status":"unknown", "expected":None, "observed":None, "watermark":None,
            "reason":"execution_inventory_not_observed", "source_detail":None,
        }, occurred_at=None, observed_at=AT, producer="fixture")
        self.line = canonical(self.row.to_dict())

    def test_lazy_lines_preserve_all_bytes_splitlines_boundaries(self):
        # bytes.splitlines treats only CR, LF and CRLF as delimiters. Other
        # bytes, including Unicode line-separator encodings, stay in the line.
        data = bytes(range(256)) + b"\r\nend\r\n\n"
        for start in range(len(data) + 1):
            self.assertEqual(list(_bundle_lines(data, start)), data[start:].splitlines())

    def test_cold_and_committed_tail_do_not_bulk_slice_or_split(self):
        class NoBulkBytes(bytes):
            def splitlines(self, *args, **kwargs):
                raise AssertionError("whole-input line list")
            def __getitem__(self, index):
                if isinstance(index, slice) and len(range(*index.indices(len(self)))) > 2048:
                    raise AssertionError("whole-input or prefix copy")
                return super().__getitem__(index)
        prefix = NoBulkBytes((self.line + b"\n") * 100)
        rows, count, latest = self.cache.receipt_bundle(self.peer, prefix, ledger_watermark=0)
        self.assertEqual((len(rows), count, latest), (100, 100, None))
        self.cache.commit_receipt_bundles(1)
        result = self.cache.receipt_bundle(self.peer, NoBulkBytes(prefix + self.line + b"\n"),
                                          ledger_watermark=1)
        self.assertEqual(result, ([self.row], 101, None))

    def test_warm_large_prefix_peak_does_not_scale_with_prefix_copy(self):
        prefix = (b" " * 1023 + b"\n") * (32 * 1024)
        self.cache.receipt_bundle(self.peer, prefix, ledger_watermark=0)
        self.cache.commit_receipt_bundles(1)
        data = prefix + self.line + b"\n"
        # Isolate bundle-buffer work: CalculationCache.get independently uses
        # a fixed 32MiB bounded read buffer, unrelated to input-prefix size.
        slot = "receipt-bundle-" + digest(str(self.peer))
        key = [engine_digest(), "receipt-bundle-prefix/v3"]
        cursor = self.cache.cache.get(slot, key)
        self.assertIsNotNone(cursor)
        with patch.object(self.cache.cache, "get", return_value=cursor):
            tracemalloc.start()
            try:
                rows, count, latest = self.cache.receipt_bundle(self.peer, data, ledger_watermark=1)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual((rows, count, latest), ([self.row], 1, None))
        self.assertLess(peak, 1024 * 1024, "32MiB prefix must be hashed without a second buffer")

    def test_mixed_line_endings_and_no_final_newline_preserve_rows(self):
        data = b"\r\n" + self.line + b"\r" + self.line + b"\r\n  \n" + self.line
        rows, count, latest = self.cache.receipt_bundle(self.peer, data, ledger_watermark=0)
        self.assertEqual((rows, count, latest), ([self.row] * 3, 3, None))
        self.cache.commit_receipt_bundles(1)
        # A cursor without a final LF remains untrusted on the next read.
        warm = NativeParseCache(self.root / "cache")
        self.assertEqual(warm.receipt_bundle(self.peer, data, ledger_watermark=1)[0], rows)
        self.assertEqual(warm.summary(), {"parsed_files":1, "reused_files":0})

    def test_bad_tail_does_not_commit_cursor_and_retry_reads_same_rows(self):
        prefix = self.line + b"\n"
        self.cache.receipt_bundle(self.peer, prefix, ledger_watermark=0)
        self.cache.commit_receipt_bundles(1)
        with self.assertRaises(json.JSONDecodeError):
            self.cache.receipt_bundle(self.peer, prefix + b"{", ledger_watermark=1)
        self.assertFalse(self.cache._pending_receipt_bundles)
        rows, count, _ = self.cache.receipt_bundle(self.peer, prefix + self.line + b"\n",
                                                  ledger_watermark=1)
        self.assertEqual((rows, count), ([self.row], 2))
