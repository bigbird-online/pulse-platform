from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import json
import tempfile
import unittest

from pulse_compute_cost.app_server_usage import AppServerUsageLedger
from pulse_compute_cost.cli import _retrospective_price_result
from pulse_compute_cost.wi_pricing import price_wi_usage_cohort
from pulse_compute_cost.wi_report import WiReportError, build_wi_report

# Fixture helpers are shared with the existing Harness measurement contract
# tests so the cash policy regression suite exercises the same strict closure.
try:  # unittest discover -s mode
    from test_harness_measurement import measurement_snapshot  # noqa: E402
except ImportError:  # dotted package path invocation from the repository root
    from ComputeCost.tests.test_harness_measurement import measurement_snapshot  # type: ignore[no-redef]  # noqa: E402


WI = "wi_20260822_938"
CASH_BASIS = "openrouter_standard_token_cash"
CASH_COVERAGE_CALCULATED = "calculated_from_digest_bound_openrouter_catalog"
DEPRECATED_BASIS = "flat_standard_openrouter_equivalent_not_provider_cash"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def cohort(*, work_item: str = WI, above_272k: bool = False) -> dict[str, object]:
    requests: list[dict[str, object]] = [
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
    if above_272k:
        requests.append({
            "model": "gpt-5.6-sol",
            "input": 300_000,
            "cache_read": 280_000,
            "cache_write": 0,
            "output": 2_000,
        })
    by_model: dict[str, dict[str, int]] = {}
    for request in requests:
        model = str(request["model"])
        row = by_model.setdefault(
            model,
            {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
        )
        for key in row:
            row[key] += int(request[key])
    value: dict[str, object] = {
        "schema": "computecost_wi_usage_cohort/v1",
        "work_item_id": work_item,
        "status": "observed",
        "window": {
            "started_at": "2026-08-22T13:52:39Z",
            "ended_at": "2026-08-25T14:32:34Z",
            "source_ref": "work-items/wi_20260822_938.yaml",
        },
        "event_count": len(requests),
        "root_thread_count": 1,
        "root_thread_refs": ["lineage_v1_" + "a" * 64],
        "tokens": {
            "input": sum(int(row["input"]) for row in by_model.values()),
            "cache_read": sum(int(row["cache_read"]) for row in by_model.values()),
            "cache_write": sum(int(row["cache_write"]) for row in by_model.values()),
            "net_new_input": sum(
                int(row["input"]) - int(row["cache_read"]) - int(row["cache_write"])
                for row in by_model.values()
            ),
            "output": sum(int(row["output"]) for row in by_model.values()),
            "reasoning": 0,
            "total_resource_tokens": sum(
                int(row["input"]) + int(row["output"]) for row in by_model.values()
            ),
        },
        "by_model": by_model,
        "request_partitions": requests,
        "quality_flags": {},
        "producer_attempts": [{
            "producer": "codex_desktop",
            "status": "observed",
            "adapters": ["codex_rollout"],
            "reason": "fixture",
            "source_refs": ["codex_home_v1_" + "b" * 64],
        }],
        "diagnostics": {},
    }
    value["cohort_sha256"] = sha256(_canonical(value)).hexdigest()
    return value


def models() -> dict[str, object]:
    return {"data": [
        {
            "id": "openai/gpt-5.6-sol",
            "canonical_slug": "openai/gpt-5.6-sol-20260709",
            "pricing": {
                "prompt": "0.000002",
                "input_cache_read": "0.0000002",
                "input_cache_write": "0.0000025",
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
                "prompt": "0.000002",
                "input_cache_read": "0.0000002",
                "input_cache_write": "0.0000025",
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


def valuation(*, above_272k: bool = False) -> dict[str, object]:
    return price_wi_usage_cohort(
        cohort(above_272k=above_272k),
        models(),
        retrieved_at="2026-08-26T04:00:00Z",
    )


class CashPolicyTests(unittest.TestCase):
    def test_canonical_priced_usage_populates_machine_cash_and_coverage(self) -> None:
        report = build_wi_report(
            None,
            WI,
            usage_cohort=cohort(),
            openrouter_reference_cost=valuation(),
        )
        self.assertEqual(report["cost"]["openrouter_cash_valuation"]["total_usd"], "0.14")
        self.assertEqual(report["cost"]["machine_cash_usd"], "0.14")
        self.assertEqual(report["cost"]["cash_basis"], CASH_BASIS)
        self.assertEqual(report["cost"]["cash_coverage"], CASH_COVERAGE_CALCULATED)
        self.assertEqual(report["coverage"]["cash"], CASH_COVERAGE_CALCULATED)
        self.assertEqual(report["coverage"]["rate_card"], CASH_COVERAGE_CALCULATED)
        self.assertRegex(
            report["evidence"]["openrouter_valuation_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertEqual(
            report["cost"]["subscription_settlement"],
            "excluded_from_wi_report_and_roi",
        )
        self.assertEqual(report["usage"]["tokens"]["total_tokens"], 351_500)

    def test_harness_native_cash_cannot_override_policy_machine_cash(self) -> None:
        snapshot = measurement_snapshot(work_item=WI)
        snapshot["cost"]["token_hard_cost"]["actual_cash_usd"] = "999"  # type: ignore[index]
        report = build_wi_report(
            None,
            WI,
            usage_cohort=cohort(),
            openrouter_reference_cost=valuation(),
            measurement_snapshot=snapshot,
        )
        # The digest-bound OpenRouter standard token total remains authority.
        self.assertEqual(report["cost"]["machine_cash_usd"], "0.14")
        self.assertEqual(report["cost"]["cash_basis"], CASH_BASIS)
        self.assertEqual(report["cost"]["cash_coverage"], CASH_COVERAGE_CALCULATED)
        self.assertEqual(report["coverage"]["cash"], CASH_COVERAGE_CALCULATED)
        # Harness/provider native cash is visible but explicitly non-authoritative.
        self.assertEqual(report["cost"]["provider_native_cash_usd"], "999")
        self.assertIn("provider_native_cash_not_wi_cash_authority", report["warnings"])
        self.assertEqual(
            report["cost"]["subscription_settlement"],
            "excluded_from_wi_report_and_roi",
        )

    def test_native_cash_alone_never_creates_machine_cash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = build_wi_report(
                AppServerUsageLedger(Path(directory) / "usage.jsonl"),
                WI,
                measurement_snapshot=measurement_snapshot(work_item=WI),
            )
        self.assertEqual(report["cost"]["provider_native_cash_usd"], "0")
        self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
        self.assertEqual(report["cost"]["cash_basis"], CASH_BASIS)
        self.assertEqual(report["cost"]["cash_coverage"], "unavailable")
        self.assertEqual(report["coverage"]["cash"], "unavailable")

    def test_above_272k_request_keeps_flat_standard_tier_and_machine_cash(self) -> None:
        value = valuation(above_272k=True)
        self.assertEqual(value["total_usd"], "0.256")
        report = build_wi_report(
            None,
            WI,
            usage_cohort=cohort(above_272k=True),
            openrouter_reference_cost=value,
        )
        sol = [
            row for row in value["partitions"]
            if row["source_model"] == "gpt-5.6-sol"
        ]
        self.assertEqual({row["tier"] for row in sol}, {"standard"})
        self.assertEqual(
            sol[0]["price_usd_per_token"]["uncached_input"], "0.000002"
        )
        self.assertEqual(report["cost"]["machine_cash_usd"], "0.256")
        self.assertEqual(report["cost"]["cash_basis"], CASH_BASIS)

    def test_unknown_usage_is_not_zero_and_roi_eligibility_stays_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = build_wi_report(
                AppServerUsageLedger(Path(directory) / "usage.jsonl"),
                "wi_missing",
            )
        self.assertEqual(report["usage"]["status"], "unavailable")
        self.assertEqual(report["usage"]["tokens"], "unavailable")
        self.assertEqual(report["usage"]["conservation"], "unavailable")
        self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
        self.assertEqual(report["cost"]["cash_coverage"], "unavailable")

        # ROI-eligible terminal WI with no attribution stays incomplete.
        report = build_wi_report(
            AppServerUsageLedger(Path(directory) / "usage.jsonl"),
            "wi_missing",
            measurement_snapshot=measurement_snapshot(work_item="wi_missing"),
        )
        self.assertEqual(report["coverage"]["terminal_measurement"], "incomplete")
        self.assertEqual(
            report["outcome"]["measurement_readiness"]["final_token_seal"],
            "unavailable",
        )
        self.assertIn("roi_eligible_measurement_incomplete", report["warnings"])

    def test_deprecated_not_provider_cash_basis_is_rejected_not_aliased(self) -> None:
        value = valuation()
        value["basis"] = DEPRECATED_BASIS
        value["valuation_sha256"] = sha256(_canonical({
            key: item for key, item in value.items() if key != "valuation_sha256"
        })).hexdigest()
        with self.assertRaisesRegex(WiReportError, "OpenRouter standard token cash"):
            build_wi_report(
                None,
                WI,
                usage_cohort=cohort(),
                openrouter_reference_cost=value,
            )

    def test_wi342_historical_retrospective_cash_estimate_is_flagged_noncanonical(self) -> None:
        # WI342 installed terminal recall: Codex Token attribution is incomplete,
        # so the canonical total stays unavailable. The user-supplied Pi-only
        # created-to-terminal quantities price to exactly $25.7279004 under the
        # single flat standard OpenRouter policy and are exposed as an explicitly
        # retrospective Pi-only machine-cash estimate with both flags set.
        uncached_input = 2_338_224
        cache_read = 95_818_112
        output = 188_783
        retro_cohort: dict[str, object] = {
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
        catalog: dict[str, object] = {"data": [{
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
        valuation = price_wi_usage_cohort(
            retro_cohort, catalog, retrieved_at="2026-09-09T00:00:00Z"
        )
        self.assertEqual(valuation["total_usd"], "25.7279004")
        estimate = _retrospective_price_result(
            work_item_id="wi_20260906_342",
            candidate_sha256="candidate_sha256_" + "a" * 58,
            status="calculated",
            valuation=valuation,
        )
        self.assertEqual(estimate["valuation"]["total_usd"], "25.7279004")
        self.assertFalse(estimate["canonical_attribution"])
        self.assertFalse(estimate["canonical_total_eligible"])
        self.assertTrue(estimate["may_include_unrelated_turns"])
        self.assertTrue(estimate["noncanonical_total"])
        self.assertEqual(
            estimate["classification"],
            "pi_only_estimate_may_include_unrelated_turns",
        )
        self.assertNotIn("settlement", estimate["valuation"]["total_usd"])

    def test_subscription_and_native_cash_never_drive_wi_machine_cash(self) -> None:
        snapshot = measurement_snapshot(work_item=WI)
        snapshot["cost"]["token_hard_cost"]["actual_cash_usd"] = "0"  # type: ignore[index]
        report = build_wi_report(
            None,
            WI,
            usage_cohort=cohort(),
            measurement_snapshot=snapshot,
        )
        self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
        self.assertEqual(report["cost"]["cash_basis"], CASH_BASIS)
        self.assertEqual(report["cost"]["cash_coverage"], "unavailable")
        self.assertEqual(report["cost"]["provider_native_cash_usd"], "0")
        self.assertEqual(
            report["cost"]["subscription_settlement"],
            "excluded_from_wi_report_and_roi",
        )


if __name__ == "__main__":
    unittest.main()