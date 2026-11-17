"""TCloud must not run full-ledger queries; ownermac behavior is unchanged."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pulse_compute_cost import cli

WI = "wi_20261015_422"
OVERRIDE = "PULSE_COMPUTE_COST_ALLOW_HEAVY_ON_PEER"
NODE_NOT_SERVED = 69


class PeerHeavyQueryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # No real account registry participates unless a test writes one.
        account = mock.patch.object(cli, "_account_registry_path", return_value=self.root / "account-sources.json",
                                    create=True)
        account.start()
        self.addCleanup(account.stop)
        environment = mock.patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("PULSE_COMPUTECOST_SOURCES", "PULSE_WORK_ITEM_CORE_HOST", OVERRIDE):
            os.environ.pop(name, None)
        self.ledger = mock.patch.object(cli, "_receipt_ledger", return_value=object())
        self.collect = mock.patch.object(cli, "collect_registered_sources", return_value={"imported": 0})
        self.query = mock.patch.object(cli, "query_collaboration", return_value={"report": "ok"})
        self.legacy = mock.patch.object(cli, "build_wi_report", return_value={"report": "legacy"})
        self.ledger_mock = self.ledger.start()
        self.collect_mock = self.collect.start()
        self.query_mock = self.query.start()
        self.legacy_mock = self.legacy.start()
        for patcher in (self.ledger, self.collect, self.query, self.legacy):
            self.addCleanup(patcher.stop)

    def registry(self, node: str, path: Path | None = None) -> Path:
        path = path or self.root / "sources.json"
        path.write_text(json.dumps({
            "schema": "computecost_source_registry/v1", "ledger_root": str(self.root / "ledger"),
            "sources": [{"kind": "cc", "node": node}, {"kind": "pi", "node": node},
                        {"kind": "relay_host", "node": node}, {"kind": "receipts"}],
        }))
        path.chmod(0o600)
        return path

    def run_cli(self, *argv: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stderr.getvalue()

    def assert_refused(self, code: int, stderr: str) -> None:
        self.assertEqual(NODE_NOT_SERVED, code)
        self.assertIn("ownermac", stderr)
        self.assertIn("全量计量账本", stderr)
        self.assertIn("pulse_stats", stderr)
        self.ledger_mock.assert_not_called()
        self.collect_mock.assert_not_called()
        self.query_mock.assert_not_called()
        self.legacy_mock.assert_not_called()

    def test_tcloud_refuses_collaboration_query_without_touching_ledger(self):
        registry = self.registry("tcloud")
        self.assert_refused(*self.run_cli("query", "--collaboration", "--source-registry", str(registry),
                                          "--work-item", WI))

    def test_tcloud_refuses_wi_report_from_environment_registry(self):
        os.environ["PULSE_COMPUTECOST_SOURCES"] = str(self.registry("tcloud"))
        self.assert_refused(*self.run_cli("wi-report", WI))

    def test_tcloud_refuses_unregistered_wi_report_by_account_registry(self):
        self.registry("tcloud", self.root / "account-sources.json")
        self.assert_refused(*self.run_cli(WI))

    def test_override_lets_human_run_heavy_query_on_tcloud(self):
        os.environ[OVERRIDE] = "1"
        registry = self.registry("tcloud")
        code, _ = self.run_cli("query", "--collaboration", "--source-registry", str(registry), "--work-item", WI)
        self.assertEqual(0, code)
        self.ledger_mock.assert_called_once()
        self.query_mock.assert_called_once()

    def test_override_requires_exact_value(self):
        os.environ[OVERRIDE] = "yes"
        os.environ["PULSE_COMPUTECOST_SOURCES"] = str(self.registry("tcloud"))
        self.assert_refused(*self.run_cli("wi-report", WI))

    def test_ownermac_collaboration_query_and_wi_report_unchanged(self):
        os.environ["PULSE_COMPUTECOST_SOURCES"] = str(self.registry("ownermac"))
        self.assertEqual(0, self.run_cli("wi-report", WI)[0])
        self.assertEqual(0, self.run_cli("query", "--collaboration", "--work-item", WI)[0])
        self.assertEqual(2, self.ledger_mock.call_count)
        self.assertEqual(2, self.collect_mock.call_count)
        self.assertEqual(2, self.query_mock.call_count)

    def test_unknown_site_keeps_legacy_wi_report(self):
        self.assertEqual(0, self.run_cli("wi-report", WI, "--no-auto-discover", "--no-auto-price")[0])
        self.legacy_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
