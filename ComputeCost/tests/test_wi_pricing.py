from __future__ import annotations

from hashlib import sha256
import json
import unittest

from pulse_compute_cost.cli import _pricing_cohort
from pulse_compute_cost.collaboration_query import _value_cost
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.wi_pricing import WiPricingError, _or_model_id, price_wi_usage_cohort
from pulse_compute_cost.wi_report import WiReportError, build_wi_report


class WiPricingTests(unittest.TestCase):
    def requests(self, *, mixed: bool = False) -> list[dict[str, int | str]]:
        rows: list[dict[str, int | str]] = [
            {
                "model": "gpt-5.6-sol",
                "input": 250_000,
                "cache_read": 230_000,
                "cache_write": 0,
                "output": 1_000,
            },
            {
                "model": "gpt-5.6-terra",
                "input": 100_000,
                "cache_read": 90_000,
                "cache_write": 0,
                "output": 500,
            },
        ]
        if mixed:
            rows.append(
                {
                    "model": "gpt-5.6-sol",
                    "input": 300_000,
                    "cache_read": 280_000,
                    "cache_write": 0,
                    "output": 2_000,
                }
            )
        return rows

    def cohort(self, *, mixed: bool = False):
        requests = self.requests(mixed=mixed)
        by_model: dict[str, dict[str, int]] = {}
        for request in requests:
            model = str(request["model"])
            row = by_model.setdefault(
                model,
                {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
            )
            for key in row:
                row[key] += int(request[key])
        return {
            "schema": "computecost_wi_usage_cohort/v1",
            "work_item_id": "wi_20260822_938",
            "status": "observed",
            "by_model": by_model,
            "request_partitions": requests,
        }

    def report_cohort(self):
        cohort = self.cohort()
        cohort.update({
            "window": {
                "started_at": "2026-08-22T13:52:39Z",
                "ended_at": "2026-08-25T14:32:34Z",
                "source_ref": "work-items/wi_20260822_938.yaml",
            },
            "event_count": 2,
            "root_thread_count": 1,
            "root_thread_refs": ["lineage_v1_" + "a" * 64],
            "tokens": {
                "input": 350_000,
                "cache_read": 320_000,
                "cache_write": 0,
                "net_new_input": 30_000,
                "output": 1_500,
                "reasoning": 0,
                "total_resource_tokens": 351_500,
            },
            "producer_attempts": [{
                "producer": "codex_desktop",
                "status": "observed",
                "adapters": ["codex_rollout"],
                "reason": "fixture",
                "source_refs": ["codex_home_v1_" + "b" * 64],
            }],
            "diagnostics": {},
        })
        cohort["cohort_sha256"] = sha256(json.dumps(
            cohort, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()).hexdigest()
        return cohort

    def models(self):
        common = {
            "prompt": "0.000002",
            "input_cache_read": "0.0000002",
            "input_cache_write": "0.0000025",
        }
        return {"data": [
            {
                "id": "openai/gpt-5.6-sol",
                "canonical_slug": "openai/gpt-5.6-sol-20260709",
                "pricing": {
                    **common,
                    "completion": "0.00001",
                    "overrides": [{
                        "min_prompt_tokens": 272_000,
                        "prompt": "0.000004",
                        "input_cache_read": "0.0000004",
                        "input_cache_write": "0.000005",
                        "completion": "0.000015",
                    }],
                },
            },
            {
                "id": "openai/gpt-5.6-terra",
                "canonical_slug": "openai/gpt-5.6-terra-20260709",
                "pricing": {
                    **common,
                    "completion": "0.000012",
                    "overrides": [{
                        "min_prompt_tokens": 272_000,
                        "prompt": "0.000004",
                        "input_cache_read": "0.0000004",
                        "input_cache_write": "0.000005",
                        "completion": "0.000018",
                    }],
                },
            },
        ]}

    def test_openrouter_standard_prices_each_model_and_cache(self) -> None:
        result = price_wi_usage_cohort(
            self.cohort(), self.models(), retrieved_at="2026-08-26T04:00:00Z"
        )
        self.assertEqual(result["total_usd"], "0.14")
        self.assertEqual(result["pricing_authority"], "openrouter.models")
        self.assertEqual(result["pricing_source_url"], "https://openrouter.ai/api/v1/models")
        self.assertEqual(len(result["catalog_sha256"]), 64)
        sol, terra = result["partitions"]
        self.assertEqual(sol["tier"], "standard")
        self.assertEqual(sol["components_usd"]["cache_read"], "0.046")
        self.assertEqual(terra["components_usd"]["output"], "0.006")

    def claude_models(self):
        return {"data": [
            {
                "id": "anthropic/claude-opus-5.5",
                "canonical_slug": "anthropic/claude-opus-5.5",
                "pricing": {
                    "prompt": "0.000005",
                    "completion": "0.000025",
                    "input_cache_read": "0.0000005",
                    "input_cache_write": "0.00000625",
                },
            },
            {
                "id": "anthropic/claude-haiku-4.5",
                "canonical_slug": "anthropic/claude-haiku-4.5",
                "pricing": {
                    "prompt": "0.000001",
                    "completion": "0.000005",
                    "input_cache_read": "0.0000001",
                    "input_cache_write": "0.00000125",
                },
            },
        ]}

    def test_claude_code_model_ids_map_to_openrouter_spelling(self) -> None:
        self.assertEqual(_or_model_id("claude-opus-5-5"), "anthropic/claude-opus-5.5")
        self.assertEqual(_or_model_id("claude-sonnet-5-5"), "anthropic/claude-sonnet-5.5")
        self.assertEqual(_or_model_id("claude-fable-5-1"), "anthropic/claude-fable-5.1")
        self.assertEqual(
            _or_model_id("claude-haiku-4-5-20251001"), "anthropic/claude-haiku-4.5"
        )
        self.assertEqual(
            _or_model_id("claude-3-5-sonnet-20241022"), "anthropic/claude-3.5-sonnet"
        )
        self.assertEqual(_or_model_id("claude-sonnet-4-20250514"), "anthropic/claude-sonnet-4")
        self.assertEqual(_or_model_id("claude-opus-5-5[1m]"), "anthropic/claude-opus-5.5")
        self.assertEqual(
            _or_model_id("anthropic/claude-opus-5.5"), "anthropic/claude-opus-5.5"
        )
        self.assertEqual(_or_model_id("<synthetic>"), "<synthetic>")

    def test_claude_code_cohort_prices_all_four_token_classes(self) -> None:
        # CC usage events carry inclusive input (uncached + cache read + write).
        requests = [
            {"model": "claude-opus-5-5", "input": 1_600, "cache_read": 1_000,
             "cache_write": 500, "output": 200},
            {"model": "claude-haiku-4-5-20251001", "input": 300, "cache_read": 0,
             "cache_write": 100, "output": 50},
        ]
        cohort = {
            "schema": "computecost_wi_usage_cohort/v1",
            "work_item_id": "wi_20260822_938",
            "status": "observed",
            "by_model": {
                row["model"]: {key: row[key] for key in ("input", "cache_read", "cache_write", "output")}
                for row in requests
            },
            "request_partitions": requests,
        }
        result = price_wi_usage_cohort(
            cohort, self.claude_models(), retrieved_at="2026-10-01T00:00:00Z"
        )
        haiku, opus = result["partitions"]
        self.assertEqual(opus["openrouter_model"], "anthropic/claude-opus-5.5")
        self.assertEqual(
            opus["components_usd"],
            {"uncached_input": "0.0005", "cache_read": "0.0005",
             "cache_write": "0.003125", "output": "0.005"},
        )
        self.assertEqual(haiku["openrouter_model"], "anthropic/claude-haiku-4.5")
        self.assertEqual(haiku["components_usd"]["cache_write"], "0.000125")
        self.assertEqual(result["total_usd"], "0.0097")

    def test_claude_model_absent_from_catalog_stays_unpriced(self) -> None:
        cohort = {
            "schema": "computecost_wi_usage_cohort/v1",
            "work_item_id": "wi_20260822_938",
            "status": "observed",
            "by_model": {"claude-sonnet-5-5": {"input": 10, "cache_read": 0, "cache_write": 0, "output": 1}},
            "request_partitions": [
                {"model": "claude-sonnet-5-5", "input": 10, "cache_read": 0, "cache_write": 0, "output": 1}
            ],
        }
        with self.assertRaisesRegex(WiPricingError, "anthropic/claude-sonnet-5.5"):
            price_wi_usage_cohort(cohort, self.claude_models(), retrieved_at="2026-10-01T00:00:00Z")

    def test_cc_receipt_machine_cash_prices_cache_classes(self) -> None:
        usage = {"input_tokens": 100, "cache_read_input_tokens": 1_000,
                 "cache_creation_input_tokens": 500, "output_tokens": 200}
        for mapping in ("cc_message.v1", "cc_result.v1", "cc_sdk_result.v1"):
            runtime = "cc_sdk" if mapping == "cc_sdk_result.v1" else "cc"
            row = usage_receipt(runtime, ["s", mapping], usage, at="2026-10-01T00:00:00Z",
                                observed="2026-10-01T00:00:00Z", model="claude-opus-5-5",
                                mapping=mapping)
            self.assertEqual(
                row.payload["tokens"],
                {"uncached_input": 100, "cache_read": 1_000, "cache_write": 500, "output": 200},
            )
            cost, reason = _value_cost(row, self.claude_models())
            self.assertIsNone(reason)
            self.assertEqual(str(cost.normalize()), "0.009125")
        unknown = usage_receipt("cc", ["s", "sonnet"], usage, at="2026-10-01T00:00:00Z",
                                observed="2026-10-01T00:00:00Z", model="claude-sonnet-5-5",
                                mapping="cc_message.v1")
        self.assertEqual(_value_cost(unknown, self.claude_models()), (None, "missing_or_invalid_price"))

    def test_long_context_requests_keep_flat_standard_rates(self) -> None:
        result = price_wi_usage_cohort(
            self.cohort(mixed=True), self.models(), retrieved_at="2026-08-26T04:00:00Z"
        )
        self.assertEqual(result["total_usd"], "0.256")
        sol = [row for row in result["partitions"] if row["source_model"] == "gpt-5.6-sol"]
        self.assertEqual({row["tier"] for row in sol}, {"standard"})
        self.assertEqual(sum(row["request_count"] for row in sol), 2)
        self.assertEqual(
            sol[0]["price_usd_per_token"],
            {
                "uncached_input": "0.000002",
                "cache_read": "0.0000002",
                "cache_write": "0.0000025",
                "output": "0.00001",
            },
        )

    def test_historical_pi_flat_standard_subtotal_is_exact(self) -> None:
        uncached_input = 2_338_224
        cache_read = 95_818_112
        output = 188_783
        cohort = {
            "schema": "computecost_wi_usage_cohort/v1",
            "work_item_id": "wi_20260906_342",
            "status": "observed",
            "by_model": {
                "gpt-5.6-sol": {
                    "input": uncached_input + cache_read,
                    "cache_read": cache_read,
                    "cache_write": 0,
                    "output": output,
                }
            },
            "request_partitions": [{
                "model": "gpt-5.6-sol",
                "input": uncached_input + cache_read,
                "cache_read": cache_read,
                "cache_write": 0,
                "output": output,
            }],
        }
        models = {"data": [{
            "id": "openai/gpt-5.6-sol",
            "canonical_slug": "openai/gpt-5.6-sol-standard-snapshot",
            "pricing": {
                "prompt": "0.000002",
                "input_cache_read": "0.0000002",
                "input_cache_write": "0.0000025",
                "completion": "0.00001",
                "overrides": [{
                    "min_prompt_tokens": 272_000,
                    "prompt": "99",
                    "input_cache_read": "99",
                    "completion": "99",
                }],
            },
        }]}

        result = price_wi_usage_cohort(
            cohort, models, retrieved_at="2026-09-09T00:00:00Z"
        )

        self.assertEqual(result["total_usd"], "25.7279004")
        self.assertEqual(result["basis"], "openrouter_standard_token_cash")
        self.assertEqual(result["schema"], "computecost_openrouter_cash_valuation/v2")
        self.assertEqual(result["partitions"][0]["tier"], "standard")
        self.assertNotIn("settlement", result["total_usd"])

    def test_unbounded_explicit_pi_snapshot_is_excluded_before_pricing(self) -> None:
        requests = [row for row in self.requests(mixed=True) if row["model"] == "gpt-5.6-sol"]
        snapshot = {
            "schema": "pi_session_usage_snapshot/v1",
            "generated_at": "2026-08-26T04:00:00Z",
            "lifecycle_state": "sealed",
            "work_item_id": "wi_20260822_938",
            "phase": "work_item",
            "epoch": 1,
            "runtime": "pi",
            "procurement_source": "chatgpt_subscription",
            "runtime_binding_sha256": None,
            "summary": {
                "request_partitions": requests,
                "provider_estimated_cost_usd": "999999",
            },
        }
        snapshot["snapshot_sha256"] = sha256(json.dumps(
            snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()).hexdigest()
        cohort = _pricing_cohort("wi_20260822_938", None, snapshot)
        self.assertIsNone(cohort)

    def test_report_requires_digest_validated_openrouter_authority(self) -> None:
        cohort = self.report_cohort()
        valuation = price_wi_usage_cohort(
            cohort, self.models(), retrieved_at="2026-08-26T04:00:00Z"
        )
        report = build_wi_report(
            None,
            "wi_20260822_938",
            usage_cohort=cohort,
            openrouter_reference_cost=valuation,
        )
        self.assertEqual(report["cost"]["machine_pricing_authority"], "openrouter.models")
        self.assertEqual(
            report["cost"]["openrouter_cash_valuation"]["total_usd"], "0.14"
        )
        self.assertEqual(report["cost"]["machine_cash_usd"], "0.14")
        self.assertEqual(
            report["cost"]["cash_coverage"],
            "calculated_from_digest_bound_openrouter_catalog",
        )
        self.assertEqual(report["cost"]["cash_basis"], "openrouter_standard_token_cash")
        self.assertEqual(
            report["cost"]["subscription_settlement"],
            "excluded_from_wi_report_and_roi",
        )

        valuation["pricing_authority"] = "pi_native"
        valuation["valuation_sha256"] = sha256(json.dumps(
            {key: value for key, value in valuation.items() if key != "valuation_sha256"},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()).hexdigest()
        with self.assertRaisesRegex(WiReportError, "OpenRouter standard token cash"):
            build_wi_report(
                None,
                "wi_20260822_938",
                usage_cohort=cohort,
                openrouter_reference_cost=valuation,
            )

    def test_native_producer_cost_is_rejected_from_wi_valuation(self) -> None:
        cohort = self.cohort()
        cohort["request_partitions"][0]["provider_reported_cost_usd"] = "999"
        with self.assertRaisesRegex(WiPricingError, "producer-native cost"):
            price_wi_usage_cohort(
                cohort, self.models(), retrieved_at="2026-08-26T04:00:00Z"
            )

    def test_excess_cache_preserves_public_pricing_error(self) -> None:
        cohort = self.cohort()
        request = cohort["request_partitions"][0]
        request["cache_read"] = request["input"]
        request["cache_write"] = 1
        with self.assertRaisesRegex(WiPricingError, "request cache tokens exceed input"):
            price_wi_usage_cohort(
                cohort, self.models(), retrieved_at="2026-08-26T04:00:00Z"
            )

    def test_missing_or_nonconserving_request_evidence_fails_closed(self) -> None:
        cohort = self.cohort()
        cohort.pop("request_partitions")
        with self.assertRaisesRegex(WiPricingError, "per-request token evidence"):
            price_wi_usage_cohort(
                cohort, self.models(), retrieved_at="2026-08-26T04:00:00Z"
            )
        cohort = self.cohort()
        cohort["request_partitions"][0]["input"] += 1
        with self.assertRaisesRegex(WiPricingError, "does not conserve"):
            price_wi_usage_cohort(
                cohort, self.models(), retrieved_at="2026-08-26T04:00:00Z"
            )


if __name__ == "__main__":
    unittest.main()
