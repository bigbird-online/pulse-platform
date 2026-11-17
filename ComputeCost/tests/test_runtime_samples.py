from __future__ import annotations

import json
import unittest

from pulse_compute_cost.runtime_binding import build_runtime_usage_binding
from pulse_compute_cost.runtime_samples import (
    RuntimeSampleError,
    build_cc_usage_sample,
    build_openrouter_usage_sample,
    validate_runtime_usage_sample,
)


def binding(*, runtime: str, procurement: str):
    return build_runtime_usage_binding(
        work_item_id="wi_20260822_730",
        agent_id="pulse",
        phase="work_item",
        epoch=1,
        runtime=runtime,
        procurement_source=procurement,
        issued_at="2026-08-25T03:00:00Z",
    )


def cc_payload() -> dict[str, object]:
    return {
        "type": "assistant",
        "timestamp": "2026-08-25T03:01:00Z",
        "sessionId": "raw-session-must-not-persist",
        "cwd": "/private/workspace",
        "effort": "high",
        "message": {
            "id": "raw-message-must-not-persist",
            "model": "claude-opus-4-1",
            "content": "must-not-persist",
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 700,
                "cache_creation_input_tokens": 50,
                "output_tokens": 80,
            },
        },
    }


def sdk_payload() -> dict[str, object]:
    return {
        "event_id": "raw-sdk-event",
        "occurred_at": "2026-08-25T03:02:00Z",
        "model": "claude-sonnet-4",
        "effort": None,
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 5,
            "output_tokens": 7,
        },
    }


def openrouter_payload() -> dict[str, object]:
    return {
        "id": "raw-openrouter-request",
        "model": "deepseek/deepseek-v4-flash-0731",
        "choices": [{"message": {"content": "must-not-persist"}}],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 120,
            "total_tokens": 1120,
            "cost": "0.00123",
            "prompt_tokens_details": {"cached_tokens": 800},
            "completion_tokens_details": {"reasoning_tokens": 40},
        },
    }


class RuntimeSampleTests(unittest.TestCase):
    def test_cc_sample_normalizes_disjoint_cache_counters_and_privacy(self) -> None:
        sample = build_cc_usage_sample(
            cc_payload(),
            binding=binding(runtime="cc", procurement="claude_subscription"),
        )
        validate_runtime_usage_sample(sample)
        self.assertEqual(sample["tokens"]["input"], 850)
        self.assertEqual(sample["tokens"]["total"], 930)
        self.assertEqual(sample["procurement_source"], "claude_subscription")
        rendered = json.dumps(sample)
        for private in (
            "raw-session-must-not-persist",
            "raw-message-must-not-persist",
            "/private/workspace",
            "must-not-persist",
        ):
            self.assertNotIn(private, rendered)

    def test_cc_sdk_sample_uses_explicit_openrouter_procurement(self) -> None:
        sample = build_cc_usage_sample(
            sdk_payload(),
            binding=binding(runtime="cc_sdk", procurement="openrouter_api"),
            producer="cc_sdk",
        )
        self.assertEqual(sample["tokens"]["input"], 35)
        self.assertEqual(sample["procurement_source"], "openrouter_api")
        self.assertEqual(sample["effort"], "unknown")

    def test_openrouter_request_sample_discards_native_cost_and_keeps_tokens(self) -> None:
        sample = build_openrouter_usage_sample(
            openrouter_payload(),
            binding=binding(runtime="cc_sdk", procurement="openrouter_api"),
            occurred_at="2026-08-25T03:03:00Z",
        )
        self.assertEqual(sample["tokens"]["cache_read"], 800)
        self.assertEqual(sample["tokens"]["reasoning"], 40)
        self.assertNotIn("provider_reported_cost_usd", sample)
        self.assertNotIn("cash_semantics", sample)
        self.assertEqual(
            sample["valuation_policy"],
            "tokens_only_openrouter_standard_rates_downstream",
        )
        self.assertNotIn("raw-openrouter-request", json.dumps(sample))

    def test_binding_and_counter_mismatch_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeSampleError, "runtime binding"):
            build_cc_usage_sample(
                cc_payload(),
                binding=binding(runtime="cc_sdk", procurement="claude_subscription"),
            )
        with self.assertRaisesRegex(RuntimeSampleError, "openrouter_api procurement"):
            build_openrouter_usage_sample(
                openrouter_payload(),
                binding=binding(runtime="cc", procurement="claude_subscription"),
                occurred_at="2026-08-25T03:03:00Z",
            )
        payload = openrouter_payload()
        payload["usage"]["total_tokens"] = 1  # type: ignore[index]
        with self.assertRaisesRegex(RuntimeSampleError, "do not reconcile"):
            build_openrouter_usage_sample(
                payload,
                binding=binding(runtime="cc", procurement="openrouter_api"),
                occurred_at="2026-08-25T03:03:00Z",
            )

    def test_sample_digest_tamper_is_rejected(self) -> None:
        sample = build_cc_usage_sample(
            cc_payload(),
            binding=binding(runtime="cc", procurement="claude_subscription"),
        )
        sample["tokens"]["output"] = 81
        with self.assertRaisesRegex(RuntimeSampleError, "conserve|digest"):
            validate_runtime_usage_sample(sample)


if __name__ == "__main__":
    unittest.main()
