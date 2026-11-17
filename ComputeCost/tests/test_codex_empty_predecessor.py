"""Native total==last proves the current origin after an empty prior task."""
from pathlib import Path
import tempfile
import unittest
from pulse_compute_cost.receipts import canonical
from pulse_compute_cost.receipt_sources import read_codex
from test_collaboration_receipts import AT

THREAD='00000000-0000-4000-8000-000000000003'


class EmptyPredecessorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.file=self.root/f'rollout-2026-09-13T01-00-00-{THREAD}.jsonl'

    def rows(self, equal):
        total={'input_tokens':100,'output_tokens':2,'cached_input_tokens':20,'cache_write_input_tokens':0,'reasoning_output_tokens':0}
        last=total if equal else {**total,'input_tokens':90}
        data=[{'type':'session_meta','timestamp':AT,'payload':{'id':THREAD,'cwd':str(self.root)}},
              {'type':'event_msg','timestamp':AT,'payload':{'type':'task_started','turn_id':'inherited-empty'}},
              {'type':'event_msg','timestamp':'2026-09-13T01:00:01Z','payload':{'type':'task_started','turn_id':'actual'}},
              {'type':'turn_context','timestamp':'2026-09-13T01:00:01Z','payload':{'model':'openai/test'}},
              {'type':'event_msg','timestamp':'2026-09-13T01:00:03Z','payload':{'type':'token_count','info':{'total_token_usage':total,'last_token_usage':last}}},
              {'type':'event_msg','timestamp':'2026-09-13T01:00:04Z','payload':{'type':'task_complete','turn_id':'actual'}}]
        self.file.write_bytes(b''.join(canonical(r)+b'\n' for r in data))
        return [r for r in read_codex(self.root,'2026-09-13T02:00:00Z') if r.kind=='usage']

    def test_native_origin_recovers_interval_without_adding_or_splitting_tokens(self):
        rows=self.rows(True)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].payload['interval_start'],'2026-09-13T01:00:01Z')
        self.assertEqual(rows[0].payload['interval_end'],'2026-09-13T01:00:03Z')
        self.assertEqual(rows[0].payload['reported_total_tokens'],102)

    def test_unproved_empty_predecessor_still_has_unknown_interval(self):
        rows=self.rows(False)
        self.assertIsNone(rows[0].payload['interval_start'])
        self.assertEqual(rows[0].payload['reported_total_tokens'],102)


if __name__=='__main__':unittest.main()
