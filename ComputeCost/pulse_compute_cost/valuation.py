"""Exact reference valuation plus deprecated monthly Stats settlement helpers.

Subscription allocation is retained only for the pre-policy managed snapshot.
It is not a WI-report or ROI valuation path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Mapping

from .openrouter import OpenRouterPriceSnapshot


class ValuationError(ValueError):
    """Raised when usage cannot be valued without inventing evidence."""


def _token_count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValuationError(f"{field} must be a non-negative integer")
    return value


def _weight(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValuationError(f"{field} must be a non-negative decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValuationError(f"{field} must be a non-negative decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValuationError(f"{field} must be a non-negative decimal")
    return parsed


def estimate_reference_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    price: OpenRouterPriceSnapshot,
) -> Decimal:
    """Price input/output/cache tokens exactly using one reference snapshot."""

    input_count = _token_count(input_tokens, "input_tokens")
    output_count = _token_count(output_tokens, "output_tokens")
    cache_count = _token_count(cache_read_tokens, "cache_read_tokens")
    if cache_count > input_count:
        raise ValuationError("cache_read_tokens cannot exceed input_tokens")
    if cache_count and price.input_cache_read_per_token is None:
        raise ValuationError("cache usage requires an input_cache_read price")
    cache_price = price.input_cache_read_per_token or Decimal("0")
    uncached_input = input_count - cache_count
    return (
        Decimal(uncached_input) * price.prompt_per_token
        + Decimal(cache_count) * cache_price
        + Decimal(output_count) * price.completion_per_token
    )


@dataclass(frozen=True)
class SubscriptionAllocation:
    """A cents-conserving monthly subscription allocation."""

    allocations_cents: dict[str, int]
    unallocated_cents: int
    reference_total_usd: Decimal
    settlement_factor: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocations_cents": dict(sorted(self.allocations_cents.items())),
            "unallocated_cents": self.unallocated_cents,
            "reference_total_usd": str(self.reference_total_usd),
            "settlement_factor": (
                str(self.settlement_factor) if self.settlement_factor is not None else None
            ),
        }


@dataclass(frozen=True)
class ManagedCostSettlement:
    """One monthly management-cost result with cash reconciliation kept separate."""

    gross_reference_cost_usd: Decimal
    api_reference_cost_usd: Decimal
    pro_reference_cost_usd: Decimal
    pro_settled_cost_usd: Decimal
    pro_settlement_adjustment_usd: Decimal
    managed_cost_usd: Decimal
    actual_openrouter_usd: Decimal | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "gross_reference_cost_usd": str(self.gross_reference_cost_usd),
            "api_reference_cost_usd": str(self.api_reference_cost_usd),
            "pro_reference_cost_usd": str(self.pro_reference_cost_usd),
            "pro_settled_cost_usd": str(self.pro_settled_cost_usd),
            "pro_settlement_adjustment_usd": str(self.pro_settlement_adjustment_usd),
            "managed_cost_usd": str(self.managed_cost_usd),
            "actual_openrouter_usd": (
                str(self.actual_openrouter_usd)
                if self.actual_openrouter_usd is not None
                else None
            ),
        }


def allocate_subscription_cents(
    weights: Mapping[str, Decimal | int | str],
    *,
    total_cents: int = 20_000,
) -> SubscriptionAllocation:
    """Deprecated: allocate legacy monthly Stats shadow cents, never WI/ROI cost."""

    cents = _token_count(total_cents, "total_cents")
    parsed: dict[str, Decimal] = {}
    for key, value in weights.items():
        if not isinstance(key, str) or not key:
            raise ValuationError("allocation keys must be non-empty strings")
        if key in parsed:
            raise ValuationError(f"duplicate allocation key: {key}")
        parsed[key] = _weight(value, f"weights[{key!r}]")
    reference_total = sum(parsed.values(), Decimal("0"))
    if reference_total == 0:
        return SubscriptionAllocation(
            allocations_cents={key: 0 for key in sorted(parsed)},
            unallocated_cents=cents,
            reference_total_usd=reference_total,
            settlement_factor=None,
        )

    exact: dict[str, Decimal] = {
        key: (value / reference_total) * Decimal(cents)
        for key, value in parsed.items()
    }
    allocated = {
        key: int(value.to_integral_value(rounding=ROUND_FLOOR))
        for key, value in exact.items()
    }
    remaining = cents - sum(allocated.values())
    remainder_order = sorted(
        exact,
        key=lambda key: (-(exact[key] - Decimal(allocated[key])), key),
    )
    for key in remainder_order[:remaining]:
        allocated[key] += 1

    if sum(allocated.values()) != cents:
        raise AssertionError("subscription allocation must conserve total cents")
    return SubscriptionAllocation(
        allocations_cents=dict(sorted(allocated.items())),
        unallocated_cents=0,
        reference_total_usd=reference_total,
        settlement_factor=Decimal(cents) / Decimal(100) / reference_total,
    )


def settle_managed_cost(
    *,
    api_reference_cost_usd: Decimal | int | str,
    pro_reference_cost_usd: Decimal | int | str,
    pro_allocation: SubscriptionAllocation,
    actual_openrouter_usd: Decimal | int | str | None = None,
) -> ManagedCostSettlement:
    """Deprecated: settle the legacy monthly Stats shadow, never WI/ROI cost."""

    api_reference = _weight(api_reference_cost_usd, "api_reference_cost_usd")
    pro_reference = _weight(pro_reference_cost_usd, "pro_reference_cost_usd")
    actual_openrouter = (
        _weight(actual_openrouter_usd, "actual_openrouter_usd")
        if actual_openrouter_usd is not None
        else None
    )
    if pro_allocation.reference_total_usd != pro_reference:
        raise ValuationError("Pro allocation weights must equal pro_reference_cost_usd")
    allocated_cents = sum(pro_allocation.allocations_cents.values())
    settled_cents = allocated_cents + pro_allocation.unallocated_cents
    if settled_cents != 20_000:
        raise ValuationError("Pro allocation must conserve exactly 20000 cents")

    pro_settled = Decimal(settled_cents) / Decimal(100)
    gross_reference = api_reference + pro_reference
    adjustment = pro_settled - pro_reference
    managed_cost = gross_reference + adjustment
    if managed_cost != api_reference + pro_settled:
        raise AssertionError("managed cost formulas must be equivalent")
    return ManagedCostSettlement(
        gross_reference_cost_usd=gross_reference,
        api_reference_cost_usd=api_reference,
        pro_reference_cost_usd=pro_reference,
        pro_settled_cost_usd=pro_settled,
        pro_settlement_adjustment_usd=adjustment,
        managed_cost_usd=managed_cost,
        actual_openrouter_usd=actual_openrouter,
    )
