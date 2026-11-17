from pathlib import Path
import tempfile
import unittest
import importlib.util

MODULE=Path(__file__).resolve().parents[1]/"uat/production_cycle_profile.py"
spec=importlib.util.spec_from_file_location("production_cycle_profile_tested",MODULE)
profile=importlib.util.module_from_spec(spec);spec.loader.exec_module(profile)

class ProductionCycleProfileSourceTests(unittest.TestCase):
    def test_file_and_directory_receipt_sources_copy_without_shape_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/"source.jsonl";source.write_text("{}\n");source.chmod(0o600)
            out=root/"file-copy.jsonl"
            profile.copy_registered_receipt_source(source,out)
            self.assertEqual(out.read_bytes(),source.read_bytes())
            folder=root/"coverage";folder.mkdir();(folder/"a.jsonl").write_text("{}\n")
            copied=root/"coverage-copy"
            profile.copy_registered_receipt_source(folder,copied)
            self.assertEqual((copied/"a.jsonl").read_text(),"{}\n")

    def test_symlink_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/"target";target.mkdir();link=root/"link";link.symlink_to(target,target_is_directory=True)
            with self.assertRaises(ValueError):
                profile.copy_registered_receipt_source(link,root/"copy")

    def test_performance_diagnosis_records_attribution_gap_but_rejects_hard_errors(self):
        usage={'event_count':2,'time_unallocated_event_count':0,'conflicted_event_count':0,
               'source_details':[]}
        cost={'unpriced':[]}
        groups=[{'key':{'agent':'pulse','wi':'unassigned','entrypoint':'telegram','node':'ownermac','runtime':'pi','model':'m'},
                 'usage':{'event_count':2}}]
        view={'summary':{'usage':usage,'cost':cost},'groups':groups}
        result=profile.performance_week_diagnosis(view)
        self.assertFalse(result['strict_week_accepted']);self.assertEqual(result['missing']['wi'],2)
        bad={'summary':{'usage':{**usage,'conflicted_event_count':1},'cost':cost},'groups':groups}
        with self.assertRaises(AssertionError): profile.performance_week_diagnosis(bad)

if __name__=="__main__": unittest.main()
