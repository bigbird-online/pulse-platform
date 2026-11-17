from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from pulse_compute_cost.app_server_account import (
    AppServerAccountError,
    _sanitize_rate_limits,
    _sanitize_usage,
    probe_app_server_account,
)
from pulse_compute_cost.cli import main as cli_main


class AppServerAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex_home = self.root / "private-codex-home"
        self.codex_home.mkdir()

    def usage_result(self) -> dict[str, object]:
        return {
            "summary": {
                "lifetimeTokens": 123456,
                "peakDailyTokens": 40000,
                "currentStreakDays": 3,
                "longestStreakDays": 8,
                "longestRunningTurnSec": 987,
            },
            "dailyUsageBuckets": [
                {"startDate": "2026-08-24", "tokens": 12000},
                {"startDate": "2026-08-25", "tokens": 13000},
            ],
            "threadUsage": None,
            "privateAccountId": "must-not-be-copied",
        }

    def rate_result(self) -> dict[str, object]:
        return {
            "rateLimits": {
                "planType": "pro",
                "limitId": "codex",
                "limitName": "Codex",
                "primary": {
                    "usedPercent": 35,
                    "resetsAt": 1787600000,
                    "windowDurationMins": 300,
                },
                "secondary": {
                    "usedPercent": 65,
                    "resetsAt": 1788000000,
                    "windowDurationMins": 10080,
                },
                "credits": {
                    "hasCredits": True,
                    "unlimited": False,
                    "balance": "12.50",
                },
                "individualLimit": {
                    "limit": "50.00",
                    "used": "4.25",
                    "remainingPercent": 91,
                    "resetsAt": 1789000000,
                },
                "spendControlReached": False,
                "rateLimitReachedType": None,
                "privateUserId": "must-not-be-copied",
            },
            "rateLimitResetCredits": {
                "availableCount": 2,
                "credits": [
                    {"id": "private-reset-id", "status": "available"}
                ],
            },
            "rateLimitsByLimitId": {
                "private-account-bucket": {"planType": "pro"}
            },
        }

    def fake_binary(self) -> Path:
        path = self.root / "fake-codex"
        path.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json, sys
                usage = {self.usage_result()!r}
                rate = {self.rate_result()!r}
                for line in sys.stdin:
                    request = json.loads(line)
                    if request.get("id") == 1:
                        result = {{
                            "codexHome": "/private/codex/home",
                            "platformFamily": "unix",
                            "platformOs": "macos",
                            "userAgent": "private-user-agent",
                        }}
                        print(json.dumps({{"id": 1, "result": result}}), flush=True)
                    elif request.get("id") == 2:
                        print(json.dumps({{"id": 2, "result": usage}}), flush=True)
                    elif request.get("id") == 3:
                        print(json.dumps({{"id": 3, "result": rate}}), flush=True)
                """
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def test_sanitizers_keep_usage_quota_and_drop_identity(self) -> None:
        usage = _sanitize_usage(self.usage_result())
        rate = _sanitize_rate_limits(self.rate_result())
        self.assertEqual(usage["summary"]["lifetime_tokens"], 123456)
        self.assertEqual(
            usage["daily_usage"],
            {
                "bucket_count": 2,
                "start_date": "2026-08-24",
                "end_date": "2026-08-25",
                "tokens_total": 25000,
            },
        )
        self.assertEqual(rate["primary"]["used_percent"], 35)
        self.assertEqual(rate["secondary"]["window_duration_mins"], 10080)
        self.assertEqual(rate["credits"]["balance"], "12.50")
        self.assertEqual(rate["reset_credits"], {"available_count": 2})
        serialized = json.dumps({"usage": usage, "rate": rate})
        for forbidden in (
            "must-not-be-copied",
            "private-reset-id",
            "private-account-bucket",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_real_transport_contract_emits_only_sanitized_snapshot(self) -> None:
        snapshot = probe_app_server_account(
            binary=self.fake_binary(),
            codex_home=self.codex_home,
            timeout_seconds=5,
        )
        self.assertEqual(snapshot["schema"], "app_server_account_snapshot/v1")
        self.assertEqual(snapshot["usage"]["summary"]["lifetime_tokens"], 123456)
        self.assertEqual(snapshot["rate_limits"]["primary"]["used_percent"], 35)
        self.assertEqual(len(snapshot["snapshot_sha256"]), 64)
        serialized = json.dumps(snapshot)
        for forbidden in (
            str(self.codex_home),
            "private-user-agent",
            "must-not-be-copied",
            "private-reset-id",
            "private-account-bucket",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_thread_usage_in_account_probe_fails_closed(self) -> None:
        value = self.usage_result()
        value["threadUsage"] = {
            "threadId": "private-thread",
            "estimatedUsageCreditsMicros": 12,
            "groups": [],
        }
        with self.assertRaisesRegex(AppServerAccountError, "thread identity"):
            _sanitize_usage(value)

    def test_malformed_quota_and_usage_values_fail_closed(self) -> None:
        usage = self.usage_result()
        usage["summary"]["lifetimeTokens"] = -1  # type: ignore[index]
        with self.assertRaisesRegex(AppServerAccountError, "non-negative"):
            _sanitize_usage(usage)

        rate = self.rate_result()
        rate["rateLimits"]["primary"]["usedPercent"] = "35"  # type: ignore[index]
        with self.assertRaisesRegex(AppServerAccountError, "non-negative"):
            _sanitize_rate_limits(rate)

    def test_binary_and_codex_home_must_be_explicit_absolute_paths(self) -> None:
        with self.assertRaisesRegex(AppServerAccountError, "absolute file"):
            probe_app_server_account(
                binary=Path("codex"),
                codex_home=self.codex_home,
            )
        with self.assertRaisesRegex(AppServerAccountError, "absolute directory"):
            probe_app_server_account(
                binary=self.fake_binary(),
                codex_home=Path(".codex"),
            )

    def test_cli_requires_host_bindings_and_does_not_echo_them(self) -> None:
        output = io.StringIO()
        env = {
            "PULSE_CODEX_APP_SERVER_BIN": str(self.fake_binary()),
            "PULSE_CODEX_HOME": str(self.codex_home),
        }
        with patch.dict(os.environ, env), redirect_stdout(output):
            rc = cli_main(["app-server-account-probe", "--timeout-seconds", "5"])
        self.assertEqual(rc, 0)
        snapshot = json.loads(output.getvalue())
        self.assertEqual(snapshot["rate_limits"]["plan_type"], "pro")
        self.assertNotIn(str(self.codex_home), output.getvalue())
        self.assertNotIn(env["PULSE_CODEX_APP_SERVER_BIN"], output.getvalue())


if __name__ == "__main__":
    unittest.main()
