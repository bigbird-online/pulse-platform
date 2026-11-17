from __future__ import annotations

import json
import importlib.util
import io
import pathlib
import subprocess
import sys
import unittest
from datetime import datetime, timezone, timedelta
from tempfile import TemporaryDirectory
from contextlib import redirect_stderr, redirect_stdout


ROOT = pathlib.Path(__file__).parents[2]
SCRIPT = ROOT / "exporter" / "pulse-stats-snapshot.py"
FIXTURE = ROOT / "exporter" / "tests" / "producer-input.json"

# The CLI is loaded by file path, which does not put its own directory on the
# import path, so its `from pulse_stats...` imports need the package root here.
sys.path.insert(0, str(ROOT / "exporter"))


class SnapshotCLITests(unittest.TestCase):
    def test_cost_only_reads_original_query_without_live_collection_or_health_claims(self):
        sys.path.insert(0, str(ROOT.parent / "ComputeCost"))
        from pulse_compute_cost.stats_dashboard import build_dashboard, publish_dashboard
        from pulse_compute_cost.ledger import UsageLedger
        spec = importlib.util.spec_from_file_location("pulse_stats_cli_cost_only", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "cost.json"
            document = build_dashboard(UsageLedger(pathlib.Path(directory) / "ledger"),
                subscriptions={"schema_version": "pulse_stats_procurement_facts/v1", "subscriptions": []},
                source_revision="c" * 40, now=datetime.now(timezone.utc),
                identity_dimensions=True, activity_table=True, activity_only=True)
            publish_dashboard(path, document)
            before = path.read_bytes()
            def forbidden_live_factory():
                self.fail("cost-only export must never enter live collection")
            output = io.StringIO()
            with redirect_stdout(output):
                code = module.main(["--json", "--cost-dashboard-only", "--cost-dashboard", str(path)],
                                   live_service_factory=forbidden_live_factory)
            actual = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(actual["cost_dashboard"], document)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(actual["overall"]["runtime_health"], "unknown")
            self.assertEqual(actual["overall"]["coverage"], "unknown")
            self.assertEqual(actual["global_authority"]["status"], "unknown")

    def test_cost_only_requires_explicit_absolute_source_and_rejects_mixed_modes(self):
        for args in [["--cost-dashboard-only"], ["--cost-dashboard-only", "--cost-dashboard", "relative"],
                     ["--cost-dashboard-only", "--cost-dashboard", "/private/cost.json", "--fixture", str(FIXTURE)],
                     ["--cost-dashboard-only", "--cost-dashboard", "/private/cost.json", "--compute-cost-shadow", "/shadow"]]:
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(SCRIPT), "--json", *args], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_cost_only_keeps_an_expired_publication_at_its_own_time(self):
        sys.path.insert(0, str(ROOT.parent / "ComputeCost"))
        from pulse_compute_cost.stats_dashboard import build_dashboard, publish_dashboard
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "cost.json"
            document = build_dashboard(None,
                subscriptions={"schema_version": "pulse_stats_procurement_facts/v1", "subscriptions": []},
                source_revision="c" * 40, now=datetime.now(timezone.utc) - timedelta(hours=2))
            publish_dashboard(path, document)
            result = subprocess.run([sys.executable, str(SCRIPT), "--json", "--cost-dashboard-only",
                                     "--cost-dashboard", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["cost_dashboard"], document)

    def test_fixture_json_writes_exactly_one_snapshot_to_stdout(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--fixture", str(FIXTURE), "--json"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        snapshot = json.loads(result.stdout)
        self.assertEqual(snapshot["schema_version"], "pulse.monitoring.snapshot.v2")
        self.assertEqual(snapshot["overall"]["runtime_health"], "warning")
        self.assertEqual(snapshot["overall"]["coverage"], "partial")
        self.assertEqual(result.stderr, "")

    def test_missing_json_flag_is_rejected_before_any_live_work(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("--json", result.stderr)

    def test_no_fixture_uses_injected_live_service(self) -> None:
        spec = importlib.util.spec_from_file_location("pulse_stats_cli", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        class FakeService:
            def render_json(self) -> str:
                return '{"schema_version":"pulse.monitoring.snapshot.v2"}'

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = module.main(["--json"], live_service_factory=FakeService)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["schema_version"], "pulse.monitoring.snapshot.v2")
        self.assertEqual(stderr.getvalue(), "")

    def test_explicit_compute_cost_shadow_is_passed_to_live_factory(self) -> None:
        spec = importlib.util.spec_from_file_location("pulse_stats_cli_shadow", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        expected = pathlib.Path(
            "/home/pulse/.local/state/pulse/compute-cost/managed-cost-current.json"
        )
        received = []

        class FakeService:
            def render_json(self) -> str:
                return '{"schema_version":"pulse.monitoring.snapshot.v2"}'

        def factory(*, compute_cost_shadow_path):
            received.append(compute_cost_shadow_path)
            return FakeService()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = module.main(
                ["--json", "--compute-cost-shadow", str(expected)],
                live_service_factory=factory,
            )
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertEqual(received, [expected])
        self.assertEqual(json.loads(stdout.getvalue())["schema_version"], "pulse.monitoring.snapshot.v2")

    def test_live_failure_returns_nonzero_with_empty_stdout(self) -> None:
        spec = importlib.util.spec_from_file_location("pulse_stats_cli_failure", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        class FailingService:
            def render_json(self) -> str:
                raise module.LiveSnapshotError("fixture failure")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = module.main(["--json"], live_service_factory=FailingService)
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("LiveSnapshotError", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
