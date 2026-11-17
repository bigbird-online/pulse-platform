from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost.app_server_thread_usage import (
    AppServerThreadUsageError,
    build_app_server_thread_usage_snapshot,
    validate_app_server_thread_usage_snapshot,
)
from pulse_compute_cost.app_server_usage import AppServerUsageLedger
from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.runtime_binding import build_runtime_usage_binding
from pulse_compute_cost.wi_report import build_wi_report


RAW_THREAD = "provider-private-thread-730"


def binding(*, runtime: str = "chatgpt_work"):
    return build_runtime_usage_binding(
        work_item_id="wi_20260822_730",
        agent_id="pulse",
        phase="work_item",
        epoch=1,
        runtime=runtime,
        procurement_source="chatgpt_subscription",
        issued_at="2026-08-25T02:00:00Z",
    )


def thread_response(*, include_usd: bool = True) -> dict[str, object]:
    usage: dict[str, object] = {
        "threadId": RAW_THREAD,
        "estimatedUsageCreditsMicros": 12,
        "groups": [
            {
                "model": "gpt-5.6-sol",
                "reasoningEffort": "high",
                "speed": "standard",
                "inputTokens": 100,
                "cachedInputTokens": 40,
                "netNewInputTokens": 60,
                "outputTokens": 20,
                "totalTokens": 120,
                "estimatedUsageCreditsMicros": 7,
            },
            {
                "model": "gpt-5.6-codex",
                "reasoningEffort": None,
                "speed": None,
                "inputTokens": 50,
                "cachedInputTokens": 10,
                "netNewInputTokens": 40,
                "outputTokens": 5,
                "totalTokens": 55,
                "estimatedUsageCreditsMicros": 5,
            },
        ],
    }
    if include_usd:
        usage["estimatedUsageUsdMicros"] = 34_000
    return {
        "summary": {"lifetimeTokens": 999_999},
        "dailyUsageBuckets": [],
        "threadUsage": usage,
        "privateAccountId": "must-not-be-copied",
    }


def snapshot(*, include_usd: bool = True) -> dict[str, object]:
    return build_app_server_thread_usage_snapshot(
        thread_response(include_usd=include_usd),
        raw_thread_id=RAW_THREAD,
        binding=binding(),
        generated_at="2026-08-25T02:01:00Z",
    )


class AppServerThreadUsageTests(unittest.TestCase):
    def test_host_bound_snapshot_preserves_estimates_and_omits_raw_identity(self) -> None:
        value = snapshot()
        validate_app_server_thread_usage_snapshot(value)
        self.assertEqual(value["estimated_usage_credits_micros"], 12)
        self.assertEqual(value["estimated_usage_usd_micros"], 34_000)
        self.assertEqual(
            sum(group["estimated_usage_credits_micros"] for group in value["groups"]),
            12,
        )
        serialized = json.dumps(value, sort_keys=True)
        self.assertNotIn(RAW_THREAD, serialized)
        self.assertNotIn("must-not-be-copied", serialized)
        self.assertNotIn("privateAccountId", serialized)

    def test_optional_usd_remains_unavailable_instead_of_zero(self) -> None:
        value = snapshot(include_usd=False)
        self.assertIsNone(value["estimated_usage_usd_micros"])

    def test_thread_binding_and_runtime_mismatch_fail_closed(self) -> None:
        with self.assertRaisesRegex(AppServerThreadUsageError, "another thread"):
            build_app_server_thread_usage_snapshot(
                thread_response(),
                raw_thread_id="different-thread",
                binding=binding(),
            )
        with self.assertRaisesRegex(AppServerThreadUsageError, "App Server host"):
            build_app_server_thread_usage_snapshot(
                thread_response(),
                raw_thread_id=RAW_THREAD,
                binding=binding(runtime="pi"),
            )

    def test_group_credit_and_token_conservation_fail_closed(self) -> None:
        response = thread_response()
        response["threadUsage"]["groups"][0][  # type: ignore[index]
            "estimatedUsageCreditsMicros"
        ] = 8
        with self.assertRaisesRegex(AppServerThreadUsageError, "credits do not conserve"):
            build_app_server_thread_usage_snapshot(
                response, raw_thread_id=RAW_THREAD, binding=binding()
            )
        response = thread_response()
        response["threadUsage"]["groups"][0]["netNewInputTokens"] = 61  # type: ignore[index]
        with self.assertRaisesRegex(AppServerThreadUsageError, "net-new"):
            build_app_server_thread_usage_snapshot(
                response, raw_thread_id=RAW_THREAD, binding=binding()
            )

    def test_snapshot_digest_tamper_is_rejected(self) -> None:
        value = snapshot()
        value["estimated_usage_usd_micros"] = 1
        with self.assertRaisesRegex(AppServerThreadUsageError, "digest"):
            validate_app_server_thread_usage_snapshot(value)

    def test_wi_report_joins_rate_card_without_claiming_policy_cash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = build_wi_report(
                AppServerUsageLedger(Path(directory) / "usage.jsonl"),
                "wi_20260822_730",
                thread_usage_snapshot=snapshot(),
            )
        self.assertEqual(report["cost"]["reference_credits"]["status"], "estimated")
        self.assertEqual(
            report["cost"]["reference_credits"][
                "estimated_usage_credits_micros"
            ],
            12,
        )
        self.assertNotIn("provider_estimated_cost_usd", report["cost"])
        self.assertEqual(report["cost"]["machine_pricing_authority"], "openrouter.models")
        self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
        self.assertEqual(report["cost"]["cash_basis"], "openrouter_standard_token_cash")
        self.assertEqual(report["coverage"]["rate_card"], "unavailable")
        self.assertIn("app_server_thread_price_excluded_non_openrouter", report["warnings"])

    def test_cli_reads_raw_thread_only_from_host_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            response_path = Path(directory) / "response.json"
            response_path.write_text(json.dumps(thread_response()), encoding="utf-8")
            output = io.StringIO()
            env = {
                "PULSE_APP_SERVER_THREAD_ID": RAW_THREAD,
                "PULSE_RUNTIME_USAGE_BINDING": json.dumps(binding().to_dict()),
            }
            with patch.dict(os.environ, env, clear=False), redirect_stdout(output):
                rc = cli_main(
                    ["app-server-thread-usage", "--response", str(response_path)]
                )
        self.assertEqual(rc, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["estimated_usage_credits_micros"], 12)
        self.assertNotIn(RAW_THREAD, output.getvalue())


if __name__ == "__main__":
    unittest.main()
