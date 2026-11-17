"""WI300 mixed Harness evidence; real historical note plus isolated bad documents."""
from pathlib import Path
import json
import tempfile
import unittest
import shutil
import yaml
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import read_harness_root, collect_registered_sources
from pulse_compute_cost.receipts import ReceiptError, current_receipts
from pulse_compute_cost.source_layout import source_detail

ROOT = Path(__file__).resolve().parents[2]
AT = "2026-09-15T05:00:00Z"
WI = "wi_20260820_272"

class HarnessDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "harness"; self.root.mkdir()
        self.ledger = UsageLedger(Path(self.tmp.name) / "ledger")
        self.source = {"id":"harness", "kind":"harness_root", "path":str(self.root), "label":"ownermac.harness_root"}
        self.registry = {"schema":"computecost_source_registry/v1", "sources":[self.source]}
    def put(self, path, value):
        p = self.root / path; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value)); return p
    def review(self):
        value = yaml.safe_load((ROOT / ".harness/reviews" / (WI + ".yaml")).read_text())
        return value
    def reference(self):
        original = ROOT / ".harness/evidence/measurements" / WI / "terminal-authority-reconciliation.yaml"
        p = self.root / original.relative_to(ROOT / ".harness")
        p.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(original,p)
        return p
    def coverage(self):
        facts,_ = current_receipts(self.ledger.receipt_snapshot()["facts"])
        return [r for r in facts if r.kind=="coverage"]
    def test_real_reconciliation_is_in_inventory_not_a_new_approval_or_usage(self):
        p=self.reference(); before=p.read_bytes()
        rows=[]; files=read_harness_root(self.root,AT,rows)
        self.assertEqual(rows,[]); self.assertIn(p,files); self.assertEqual(p.read_bytes(),before)
    def test_recognized_note_does_not_stop_later_valid_document(self):
        self.reference()
        later=self.put("evidence/measurements/wi_20260822_730/valid-review.json",self.review())
        rows=[]; detail=source_detail(self.source)
        files=read_harness_root(self.root,AT,rows,detail=detail)
        self.assertIn(later,files);self.assertEqual(len(rows),2)
        self.assertEqual(detail["files_seen"],2);self.assertEqual(detail["files_read"],2)
        self.assertEqual(detail["files_invalid"],0)
    def test_reference_never_fabricates_complete_coverage(self):
        self.reference();collect_registered_sources(self.registry,self.ledger)
        for row in self.coverage():
            self.assertNotIn(row.payload["status"],{"complete","partial","unreachable"})
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)["inserted"],0)
    def test_unknown_schema_stays_a_gap_but_does_not_drop_later_valid_facts(self):
        self.put("evidence/measurements/a/unknown.json",{"schema":"unexpected/v9"})
        self.put("evidence/measurements/z/review.json",self.review())
        collect_registered_sources(self.registry,self.ledger)
        current,_=current_receipts(self.ledger.receipt_snapshot()["facts"])
        self.assertTrue(any(r.kind=="review" for r in current))
        for row in self.coverage():
            self.assertEqual(row.payload["status"],"partial")
            self.assertEqual(row.payload["source_detail"]["errors"],{"invalid_native_record":1})
            self.assertEqual(row.payload["source_detail"]["files_seen"],2)
        with self.assertRaisesRegex(ReceiptError,"unsupported Harness"):
            read_harness_root(self.root,AT,[])
    def test_bad_document_is_local_error_not_false_empty_root(self):
        p=self.put("evidence/measurements/a/broken.yaml",{});p.write_text("x: [invalid")
        self.put("evidence/measurements/z/review.json",self.review())
        collect_registered_sources(self.registry,self.ledger)
        for row in self.coverage():
            self.assertEqual(row.payload["status"],"partial")
            self.assertEqual(row.payload["source_detail"]["errors"],{"invalid_native_record":1})
    def test_misplaced_or_mismatched_reference_is_not_ignored(self):
        p=self.reference();v=yaml.safe_load(p.read_text());v["work_item_id"]="wi_20260915_999";p.write_text(json.dumps(v))
        with self.assertRaisesRegex(ReceiptError,"reference document identity"):
            read_harness_root(self.root,AT,[])
    def test_recovered_source_revises_gap_without_changing_old_watermark(self):
        p=self.put("evidence/measurements/a/unknown.json",{"schema":"unexpected/v9"})
        collect_registered_sources(self.registry,self.ledger)
        before=self.ledger.receipt_snapshot();p.unlink();self.reference()
        collect_registered_sources(self.registry,self.ledger)
        self.assertEqual(self.ledger.receipt_snapshot(watermark=before["watermark"])["sha256"],before["sha256"])
        self.assertTrue(all(r.payload["status"]=="unknown" for r in self.coverage()))
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)["inserted"],0)

if __name__=="__main__": unittest.main()
