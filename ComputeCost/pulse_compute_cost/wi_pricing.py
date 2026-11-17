"""WI machine-cash valuation of a discovered WI usage cohort.

Standing WI policy: the machine cash cost used in ROI is the cohort Token
classes priced exactly once at top-level flat standard OpenRouter rates from a
timestamped, digest-bound ``GET /api/v1/models`` catalog. This calculated
amount is the WI machine cash, independent of procurement route or
subscription. Producer-native cost and subscription settlement are excluded.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from .token_semantics import InputTokenClasses, TokenSemanticsError


class WiPricingError(ValueError):
    pass


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise WiPricingError(f"{field} must be decimal")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise WiPricingError(f"{field} must be decimal") from exc
    if not result.is_finite() or result < 0:
        raise WiPricingError(f"{field} must be finite and non-negative")
    return result


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"unsupported OpenRouter catalog value: {type(value).__name__}")


def _text(value: Decimal) -> str:
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def usage_evidence_sha256(cohort: Mapping[str, Any]) -> str:
    """Bind valuation to complete evidence, not merely WI identity or totals."""
    unsigned = {key: value for key, value in cohort.items() if key != "cohort_sha256"}
    digest = sha256(json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"),
                               sort_keys=True, default=_json_default).encode()).hexdigest()
    if "cohort_sha256" in cohort and cohort["cohort_sha256"] != digest:
        raise WiPricingError("usage cohort digest is invalid")
    return digest


_CLAUDE_SUFFIX_RE = re.compile(r"(?:-\d{8})?(?:\[[0-9a-z]+\])?$")
_CLAUDE_VERSION_RE = re.compile(r"(?<=-)(\d{1,2})-(\d{1,2})(?=-|$)")


def _or_model_id(model: str) -> str:
    if "/" in model:
        return model
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return f"openai/{model}"
    if model.startswith("claude-"):
        # Anthropic API ids (Claude Code transcripts, CC SDK results) spell
        # versions with hyphens and may carry a snapshot date or a context
        # tag such as [1m]; OpenRouter ids use a dotted version without either.
        # WI cash uses flat standard rates, so the context tag selects nothing.
        # This is a spelling map only: the catalog must still list exactly one
        # matching model.
        base = _CLAUDE_SUFFIX_RE.sub("", model, count=1)
        return "anthropic/" + _CLAUDE_VERSION_RE.sub(r"\1.\2", base)
    return model


def _model(payload: Mapping[str, Any], model_id: str) -> Mapping[str, Any]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise WiPricingError("OpenRouter model payload is malformed")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("id") == model_id]
    if len(matches) != 1:
        raise WiPricingError(f"OpenRouter model is not uniquely priced: {model_id}")
    return matches[0]


_NATIVE_COST_KEYS = {
    "cost", "usage_cost", "usage.cost", "cost_usd", "total_cost_usd",
    "provider_reported_cost_usd", "provider_estimated_cost_usd", "native_cost",
}


def _reject_native_cost(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in _NATIVE_COST_KEYS or normalized.endswith("native_cost_usd"):
                raise WiPricingError("producer-native cost is forbidden in WI valuation input")
            _reject_native_cost(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_native_cost(nested)


def price_wi_usage_cohort(
    cohort: Mapping[str, Any],
    openrouter_models: Mapping[str, Any],
    *,
    retrieved_at: str,
) -> dict[str, Any]:
    _reject_native_cost(cohort)
    source_digest = usage_evidence_sha256(cohort)
    if cohort.get("schema") != "computecost_wi_usage_cohort/v1":
        raise WiPricingError("usage cohort schema is unsupported")
    if cohort.get("status") not in {"observed", "partial"}:
        raise WiPricingError("usage cohort has no observed usage to price")
    by_model = cohort.get("by_model")
    requests = cohort.get("request_partitions")
    if not isinstance(by_model, Mapping) or not by_model:
        raise WiPricingError("usage cohort has no model partitions")
    if not isinstance(requests, list) or not requests:
        raise WiPricingError("per-request token evidence is required for OpenRouter pricing")

    expected: dict[str, dict[str, int]] = {}
    for raw_model, raw_usage in by_model.items():
        if not isinstance(raw_model, str) or not isinstance(raw_usage, Mapping):
            raise WiPricingError("usage model partition is malformed")
        values = {key: raw_usage.get(key) for key in ("input", "cache_read", "cache_write", "output")}
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values.values()
        ):
            raise WiPricingError("usage model token partition is invalid")
        expected[raw_model] = {key: int(value) for key, value in values.items()}

    observed = {
        model: {key: 0 for key in ("input", "cache_read", "cache_write", "output")}
        for model in expected
    }
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    total = Decimal(0)
    for index, request in enumerate(requests):
        if not isinstance(request, Mapping):
            raise WiPricingError("request token partition is malformed")
        raw_model = request.get("model")
        if not isinstance(raw_model, str) or raw_model not in expected:
            raise WiPricingError("request token partition has an unknown model")
        raw_counts = {key: request.get(key) for key in ("input", "cache_read", "cache_write", "output")}
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in raw_counts.values()
        ):
            raise WiPricingError(f"request token partition {index} is invalid")
        input_tokens = int(raw_counts["input"])
        cache_read = int(raw_counts["cache_read"])
        cache_write = int(raw_counts["cache_write"])
        output = int(raw_counts["output"])
        try:
            uncached = InputTokenClasses.from_inclusive(
                input_tokens, cache_read, cache_write).require_uncached()
        except TokenSemanticsError as exc:
            raise WiPricingError("request cache tokens exceed input") from exc
        for key, value in raw_counts.items():
            observed[raw_model][key] += int(value)

        model_id = _or_model_id(raw_model)
        model = _model(openrouter_models, model_id)
        pricing = model.get("pricing")
        if not isinstance(pricing, Mapping):
            raise WiPricingError(f"OpenRouter pricing is missing: {model_id}")
        # Standing WI policy is the model's flat standard OpenRouter rate.
        # Catalog overrides (including long-context tiers) are retained in the
        # catalog digest but are never selected for WI valuation.
        raw_cache_write_rate = pricing.get("input_cache_write")
        if raw_cache_write_rate is None and cache_write:
            raise WiPricingError(f"OpenRouter cache-write pricing is missing: {model_id}")
        rates = {
            "uncached_input": _decimal(pricing.get("prompt"), f"{model_id}.prompt"),
            "cache_read": _decimal(pricing.get("input_cache_read"), f"{model_id}.input_cache_read"),
            "cache_write": (
                _decimal(raw_cache_write_rate, f"{model_id}.input_cache_write")
                if raw_cache_write_rate is not None
                else Decimal(0)
            ),
            "output": _decimal(pricing.get("completion"), f"{model_id}.completion"),
        }
        counts = {
            "uncached_input": uncached,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "output": output,
        }
        components = {key: Decimal(counts[key]) * rates[key] for key in counts}
        subtotal = sum(components.values(), Decimal(0))
        total += subtotal
        group = grouped.setdefault(
            (raw_model, "standard"),
            {
                "source_model": raw_model,
                "openrouter_model": model_id,
                "canonical_slug": model.get("canonical_slug"),
                "tier": "standard",
                "request_count": 0,
                "max_request_input_tokens": 0,
                "tokens": {key: 0 for key in counts},
                "rates": rates,
                "components": {key: Decimal(0) for key in counts},
                "total": Decimal(0),
            },
        )
        if group["rates"] != rates:
            raise WiPricingError("OpenRouter rate override is inconsistent within a tier")
        group["request_count"] += 1
        group["max_request_input_tokens"] = max(group["max_request_input_tokens"], input_tokens)
        for key in counts:
            group["tokens"][key] += counts[key]
            group["components"][key] += components[key]
        group["total"] += subtotal

    if observed != expected:
        raise WiPricingError("per-request token evidence does not conserve model totals")

    partitions = []
    for key in sorted(grouped):
        group = grouped[key]
        partitions.append(
            {
                "source_model": group["source_model"],
                "openrouter_model": group["openrouter_model"],
                "canonical_slug": group["canonical_slug"],
                "tier": group["tier"],
                "request_count": group["request_count"],
                "max_request_input_tokens": group["max_request_input_tokens"],
                "tokens": group["tokens"],
                "price_usd_per_token": {
                    name: _text(value) for name, value in group["rates"].items()
                },
                "components_usd": {
                    name: _text(value) for name, value in group["components"].items()
                },
                "total_usd": _text(group["total"]),
            }
        )
    catalog_sha256 = sha256(
        json.dumps(
            openrouter_models,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=_json_default,
        ).encode()
    ).hexdigest()
    result: dict[str, Any] = {
        "schema": "computecost_openrouter_cash_valuation/v2",
        "usage_evidence_sha256": source_digest,
        "work_item_id": cohort.get("work_item_id"),
        "status": "calculated",
        "basis": "openrouter_standard_token_cash",
        "pricing_policy": "openrouter_standard_rates_no_overrides_no_subscription_settlement",
        "pricing_authority": "openrouter.models",
        "pricing_source_url": "https://openrouter.ai/api/v1/models",
        "catalog_sha256": catalog_sha256,
        "retrieved_at": retrieved_at,
        "partitions": partitions,
        "total_usd": _text(total),
    }
    result["valuation_sha256"] = sha256(
        json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return result


def retrospective_pricing_cohort(
    work_item_id: str,
    usage_cohort: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any] | None, str | None]:
    if usage_cohort is None:
        return None, None
    candidates = usage_cohort.get("retrospective_candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        return None, None
    candidate = candidates[0]
    if not isinstance(candidate, Mapping):
        return None, None
    windows = candidate.get("windows")
    broad = windows.get("created_to_terminal") if isinstance(windows, Mapping) else None
    pi = broad.get("pi_jsonl") if isinstance(broad, Mapping) else None
    requests = pi.get("pricing_request_partitions") if isinstance(pi, Mapping) else None
    if not isinstance(requests, list) or not requests:
        digest = candidate.get("candidate_sha256")
        return None, str(digest) if isinstance(digest, str) else None
    by_model: dict[str, dict[str, int]] = {}
    normalized: list[dict[str, object]] = []
    for request in requests:
        if not isinstance(request, Mapping) or not isinstance(request.get("model"), str):
            raise WiPricingError("retrospective Pi pricing request is malformed")
        model = str(request["model"])
        row = by_model.setdefault(
            model,
            {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
        )
        normalized_request: dict[str, object] = {"model": model}
        for key in row:
            value = request.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise WiPricingError("retrospective Pi pricing Token is invalid")
            row[key] += value
            normalized_request[key] = value
        normalized.append(normalized_request)
    return {
        "schema": "computecost_wi_usage_cohort/v1",
        "work_item_id": work_item_id,
        "status": "observed",
        "source_candidate_sha256": candidate.get("candidate_sha256"),
        "by_model": by_model,
        "request_partitions": normalized,
    }, str(candidate.get("candidate_sha256"))
