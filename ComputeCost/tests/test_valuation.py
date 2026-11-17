from __future__ import annotations

from decimal import Decimal
import random
import unittest

from pulse_compute_cost.openrouter import OpenRouterPriceSnapshot
from pulse_compute_cost.valuation import (
    ValuationError,
    allocate_subscription_cents,
    estimate_reference_cost,
    settle_managed_cost,
)


def reference_price() -> OpenRouterPriceSnapshot:
    return OpenRouterPriceSnapshot(
        model_id="deepseek/deepseek-v4-flash-0731",
        canonical_slug="deepseek/deepseek-v4-flash-20260731",
        retrieved_at="2026-08-17T00:00:00Z",
        prompt_per_token=Decimal("0.00000014"),
        completion_per_token=Decimal("0.00000028"),
        input_cache_read_per_token=Decimal("0.000000028"),
    )


class ValuationTests(unittest.TestCase):
    def test_reference_cost_prices_uncached_cache_and_output_exactly(self) -> None:
        cost = estimate_reference_cost(
            input_tokens=1_000,
            cache_read_tokens=200,
            output_tokens=100,
            price=reference_price(),
        )
        self.assertEqual(cost, Decimal("0.000145600"))

    def test_cache_tokens_cannot_exceed_input_tokens(self) -> None:
        with self.assertRaisesRegex(ValuationError, "cannot exceed"):
            estimate_reference_cost(
                input_tokens=9,
                cache_read_tokens=10,
                output_tokens=0,
                price=reference_price(),
            )

    def test_subscription_allocation_conserves_20000_cents(self) -> None:
        allocation = allocate_subscription_cents(
            {
                "agent-a": Decimal("2"),
                "agent-b": Decimal("1"),
            }
        )
        self.assertEqual(allocation.allocations_cents, {"agent-a": 13_333, "agent-b": 6_667})
        self.assertEqual(sum(allocation.allocations_cents.values()), 20_000)
        self.assertEqual(allocation.unallocated_cents, 0)

    def test_zero_usage_keeps_subscription_unallocated(self) -> None:
        allocation = allocate_subscription_cents(
            {"agent-a": Decimal("0"), "agent-b": Decimal("0")}
        )
        self.assertEqual(allocation.allocations_cents, {"agent-a": 0, "agent-b": 0})
        self.assertEqual(allocation.unallocated_cents, 20_000)
        self.assertIsNone(allocation.settlement_factor)

    def test_subscription_allocation_conserves_random_nonnegative_weights(self) -> None:
        generator = random.Random(20260817)
        for size in range(1, 30):
            weights = {
                f"agent-{index:02d}": Decimal(generator.randrange(0, 100_000)) / 1000
                for index in range(size)
            }
            allocation = allocate_subscription_cents(weights)
            self.assertEqual(
                sum(allocation.allocations_cents.values()) + allocation.unallocated_cents,
                20_000,
            )

    def test_managed_cost_settlement_applies_pro_adjustment_without_double_counting(self) -> None:
        allocation = allocate_subscription_cents({"pro-agent": Decimal("300")})
        settlement = settle_managed_cost(
            api_reference_cost_usd=Decimal("10"),
            pro_reference_cost_usd=Decimal("300"),
            pro_allocation=allocation,
            actual_openrouter_usd=Decimal("7.75"),
        )
        self.assertEqual(settlement.gross_reference_cost_usd, Decimal("310"))
        self.assertEqual(settlement.pro_settled_cost_usd, Decimal("200"))
        self.assertEqual(settlement.pro_settlement_adjustment_usd, Decimal("-100"))
        self.assertEqual(settlement.managed_cost_usd, Decimal("210"))
        self.assertEqual(settlement.actual_openrouter_usd, Decimal("7.75"))

    def test_managed_cost_rejects_allocation_from_a_different_pro_total(self) -> None:
        allocation = allocate_subscription_cents({"pro-agent": Decimal("299")})
        with self.assertRaisesRegex(ValuationError, "weights must equal"):
            settle_managed_cost(
                api_reference_cost_usd=Decimal("10"),
                pro_reference_cost_usd=Decimal("300"),
                pro_allocation=allocation,
            )


if __name__ == "__main__":
    unittest.main()
