"""The visible request list must not turn unreadable input into no work."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pulse_stats.usage_sources import collect_main_only_pending
from Stats.exporter.tests.test_live import LiveFixture, FakeRunner, ack_result, systemd_result
import json

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)

class RequestCoverageTests(unittest.TestCase):
    def test_missing_input_marks_unknown_without_suppressing_other_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(Path(directory))
            source = fixture.paths.handoffs
            if source.is_dir():
                for path in source.glob("*"): path.unlink()
                source.rmdir()
            service = fixture.service(FakeRunner(ack=ack_result(), systemd=systemd_result()), now=NOW)
            data = json.loads(service.render_json())
            capability = next(c for c in data['capabilities'] if c['id'] == 'notify.action_package')
            self.assertEqual(capability['health'], 'unknown')
            self.assertNotEqual(capability['freshness']['state'], 'fresh')
            self.assertTrue(data['metrics'])

    def test_bad_record_keeps_known_requests_but_exposes_incomplete_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); handoffs = root/'handoffs'; handoffs.mkdir()
            work = root/'work-items'; work.mkdir()
            (work/'wi_good.yaml').write_text('work_item_id: wi_good\nmain_only: true\nstatus: in_progress\nowner: pulse\ntitle: A real request\n')
            (work/'wi_bad.yaml').write_text('invalid: [')
            errors = []
            rows = collect_main_only_pending(handoffs, NOW, work, coverage_errors=errors)
            self.assertEqual([row['id'] for row in rows], ['wi_good'])
            self.assertIn('work_item_unreadable', errors)

    def test_unreadable_directory_is_not_an_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); errors = []
            with patch.object(Path, 'iterdir', side_effect=PermissionError()):
                self.assertIsNone(collect_main_only_pending(root, NOW, coverage_errors=errors))
            self.assertEqual(errors, ['handoffs_unreadable'])

class AuthorityIdentityTests(unittest.TestCase):
    def test_retired_repository_cannot_validate_the_same_commit_bytes(self):
        import hashlib
        from pulse_stats.producer import authority_revision_for_git
        sha = "a" * 40
        current = "sha256:" + hashlib.sha256(("bigbird-online/PULSE@" + sha).encode()).hexdigest()
        retired = "sha256:" + hashlib.sha256(("bigbird-online/CollaborateWithAI@" + sha).encode()).hexdigest()
        self.assertEqual(authority_revision_for_git(sha), current)
        self.assertNotEqual(authority_revision_for_git(sha), retired)

if __name__ == '__main__': unittest.main()
