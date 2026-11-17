from __future__ import annotations

from decimal import Decimal
import io
import json
import tempfile
from pathlib import Path
import unittest

from pulse_compute_cost.openrouter import (
    OpenRouterContractError,
    fetch_openrouter_activity,
    fetch_openrouter_models,
    load_dotenv_value,
    parse_openrouter_prices,
    summarize_openrouter_activity,
)


class OpenRouterContractTests(unittest.TestCase):
    def test_parse_openrouter_prices_preserves_exact_decimal_rates(self) -> None:
        snapshot = parse_openrouter_prices(
            {
                "data": [
                    {
                        "id": "deepseek/deepseek-v4-flash-0731",
                        "canonical_slug": "deepseek/deepseek-v4-flash-20260731",
                        "pricing": {
                            "prompt": "0.00000014",
                            "completion": "0.00000028",
                            "input_cache_read": "0.000000028",
                        },
                    }
                ]
            },
            retrieved_at="2026-08-17T00:00:00Z",
        )
        self.assertEqual(snapshot.prompt_per_token, Decimal("0.00000014"))
        self.assertEqual(snapshot.completion_per_token, Decimal("0.00000028"))
        self.assertEqual(snapshot.input_cache_read_per_token, Decimal("0.000000028"))
        self.assertEqual(
            snapshot.to_dict()["canonical_slug"],
            "deepseek/deepseek-v4-flash-20260731",
        )
        self.assertEqual(
            snapshot.to_dict()["pricing"],
            {
                "prompt": "0.00000014",
                "completion": "0.00000028",
                "input_cache_read": "0.000000028",
            },
        )

    def test_missing_required_price_fails_closed(self) -> None:
        with self.assertRaisesRegex(OpenRouterContractError, "prompt and completion"):
            parse_openrouter_prices(
                {
                    "data": [
                        {
                            "id": "deepseek/deepseek-v4-flash-0731",
                            "canonical_slug": "deepseek/deepseek-v4-flash-20260731",
                            "pricing": {"prompt": "0.00000014"},
                        }
                    ]
                }
            )

    def test_activity_summary_drops_endpoint_identity_and_conserves_totals(self) -> None:
        summary = summarize_openrouter_activity(
            {
                "data": [
                    {
                        "date": "2026-08-15 00:00:00",
                        "endpoint_id": "must-not-leave-source",
                        "api_key_hash": "must-not-leave-source",
                        "workspace_id": "must-not-leave-source",
                        "user_id": "must-not-leave-source",
                        "model": "deepseek/deepseek-v4-flash-0731",
                        "model_permaslug": "deepseek/deepseek-v4-flash-20260731",
                        "provider_name": "baidu/fp8",
                        "requests": 2,
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "reasoning_tokens": 5,
                        "usage": 0.25,
                        "byok_usage_inference": 0,
                    },
                    {
                        "date": "2026-08-15 00:00:00",
                        "endpoint_id": "also-private",
                        "model": "deepseek/deepseek-v4-flash-0731",
                        "model_permaslug": "deepseek/deepseek-v4-flash-20260731",
                        "provider_name": "baidu/fp8",
                        "requests": 3,
                        "prompt_tokens": 200,
                        "completion_tokens": 40,
                        "reasoning_tokens": 10,
                        "usage": "0.5",
                        "byok_usage_inference": "0",
                    },
                ]
            }
        )
        self.assertEqual(
            summary["totals"],
            {
                "requests": 5,
                "prompt_tokens": 300,
                "completion_tokens": 60,
                "reasoning_tokens": 15,
                "usage_usd": "0.75",
                "byok_usage_usd": "0",
            },
        )
        self.assertEqual(len(summary["by_model"]), 1)
        rendered = repr(summary)
        for forbidden in (
            "must-not-leave-source",
            "also-private",
            "api_key_hash",
            "workspace_id",
            "user_id",
            "endpoint_id",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_dotenv_loader_returns_only_requested_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text(
                "IGNORED=not-this\nexport "
                + "OPENROUTER_M_KEY"
                + "='fixture-management-value'\n",
                encoding="utf-8",
            )
            self.assertEqual(load_dotenv_value(path), "fixture-management-value")

    def test_management_key_is_sent_only_to_activity_endpoint(self) -> None:
        calls = []

        def opener(request, *, timeout):
            calls.append((request, timeout))
            return io.BytesIO(json.dumps({"data": []}).encode("utf-8"))

        fetch_openrouter_activity("fixture-management-key", opener=opener)
        fetch_openrouter_models(opener=opener)

        activity_request = calls[0][0]
        models_request = calls[1][0]
        self.assertEqual(
            activity_request.get_header("Authorization"),
            "Bearer " + "fixture-management-key",
        )
        self.assertIsNone(models_request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
