"""Privacy-safe CC, CC-SDK, and OpenRouter v2 usage samples."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Mapping, TYPE_CHECKING

from .runtime_binding import RuntimeUsageBinding
from .token_semantics import InputTokenClasses

if TYPE_CHECKING:
    from .cc_usage import CcUsageBinding


RUNTIME_USAGE_SAMPLE_SCHEMA = "runtime_usage_sample/v2"
# Reserved by CC for local notices; it is not a provider model identifier.
CC_SYNTHETIC_MODEL = "<synthetic>"


class RuntimeSampleError(ValueError):
    """Raised when host usage cannot be normalized without guessing attribution."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeSampleError(f"{field} must be an object")
    return value


def _text(value: object, field: str, *, optional: bool = False) -> str:
    if optional and value is None:
        return "unknown"
    if not isinstance(value, str) or not value.strip():
        raise RuntimeSampleError(f"{field} must be a non-empty string")
    return value.strip()


def _count(value: object, field: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeSampleError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: object, field: str) -> str:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeSampleError(f"{field} must be a timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeSampleError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_ref(producer: str, raw_event_id: str) -> str:
    digest = sha256(
        f"pulse-computecost-runtime-sample:v1:{producer}\0{raw_event_id}".encode()
    ).hexdigest()
    return f"usage_sample_v1_{digest}"


def _base(
    *,
    binding: RuntimeUsageBinding | CcUsageBinding,
    producer: str,
    raw_event_id: str,
    occurred_at: str,
    model: str,
    effort: str,
    tokens: Mapping[str, object],
    coverage: str,
) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "schema": RUNTIME_USAGE_SAMPLE_SCHEMA,
        "work_item_id": binding.work_item_id,
        "agent_id": binding.agent_id,
        "phase": binding.phase,
        "epoch": binding.epoch,
        "runtime": binding.runtime,
        "procurement_source": binding.procurement_source,
        "runtime_binding_sha256": binding.binding_sha256,
        "producer": producer,
        "event_ref": _event_ref(producer, raw_event_id),
        "occurred_at": _timestamp(occurred_at, "occurred_at"),
        "model": _text(model, "model"),
        "effort": _text(effort, "effort", optional=True),
        "tokens": dict(tokens),
        "valuation_policy": "tokens_only_openrouter_standard_rates_downstream",
        "coverage": coverage,
        "privacy": "raw_provider_identity_content_and_path_omitted",
    }
    sample["sample_sha256"] = sha256(_canonical(sample)).hexdigest()
    validate_runtime_usage_sample(sample)
    return sample


def build_cc_usage_sample(
    payload: object,
    *,
    binding: RuntimeUsageBinding | CcUsageBinding,
    producer: str = "cc",
) -> dict[str, Any]:
    """Normalize one Claude Code transcript or SDK assistant usage event."""

    expected_runtime = {"cc": "cc", "cc_sdk": "cc_sdk"}.get(producer)
    if expected_runtime is None or binding.runtime != expected_runtime:
        raise RuntimeSampleError("CC producer does not match runtime binding")
    root = _object(payload, producer)
    if producer == "cc":
        if root.get("type") != "assistant":
            raise RuntimeSampleError("CC sample must be an assistant event")
        message = _object(root.get("message"), "cc.message")
        raw_event_id = _text(message.get("id"), "cc.message.id")
        occurred_at = _text(root.get("timestamp"), "cc.timestamp")
        model = _text(message.get("model"), "cc.message.model")
        effort = root.get("effort")
        usage = _object(message.get("usage"), "cc.message.usage")
    else:
        raw_event_id = _text(root.get("event_id"), "cc_sdk.event_id")
        occurred_at = _text(root.get("occurred_at"), "cc_sdk.occurred_at")
        model = _text(root.get("model"), "cc_sdk.model")
        effort = root.get("effort")
        usage = _object(root.get("usage"), "cc_sdk.usage")
    uncached = _count(usage.get("input_tokens"), "usage.input_tokens")
    cache_read = _count(
        usage.get("cache_read_input_tokens", 0), "usage.cache_read_input_tokens"
    )
    cache_write = _count(
        usage.get("cache_creation_input_tokens", 0),
        "usage.cache_creation_input_tokens",
    )
    output = _count(usage.get("output_tokens"), "usage.output_tokens")
    assert isinstance(uncached, int) and isinstance(cache_read, int)
    assert isinstance(cache_write, int) and isinstance(output, int)
    total_input = InputTokenClasses.from_disjoint(uncached, cache_read, cache_write).require_total()
    tokens = {
        "input": total_input,
        "output": output,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "reasoning": None,
        "total": total_input + output,
    }
    return _base(
        binding=binding,
        producer=producer,
        raw_event_id=raw_event_id,
        occurred_at=occurred_at,
        model=model,
        effort=_text(effort, "effort", optional=True),
        tokens=tokens,
        coverage="exact_host_bound_cc_usage",
    )


def build_openrouter_usage_sample(
    payload: object,
    *,
    binding: RuntimeUsageBinding | CcUsageBinding,
    occurred_at: str,
) -> dict[str, Any]:
    """Normalize one request response; management activity remains cash authority."""

    if binding.procurement_source != "openrouter_api":
        raise RuntimeSampleError("OpenRouter sample requires openrouter_api procurement")
    root = _object(payload, "OpenRouter response")
    raw_event_id = _text(root.get("id"), "response.id")
    model = _text(root.get("model"), "response.model")
    usage = _object(root.get("usage"), "response.usage")
    prompt = _count(usage.get("prompt_tokens"), "usage.prompt_tokens")
    completion = _count(usage.get("completion_tokens"), "usage.completion_tokens")
    total = _count(usage.get("total_tokens"), "usage.total_tokens")
    details = usage.get("prompt_tokens_details")
    cached: int | None = None
    if details is not None:
        cached = _count(
            _object(details, "usage.prompt_tokens_details").get("cached_tokens"),
            "usage.prompt_tokens_details.cached_tokens",
            optional=True,
        )
    reasoning_details = usage.get("completion_tokens_details")
    reasoning: int | None = None
    if reasoning_details is not None:
        reasoning = _count(
            _object(
                reasoning_details, "usage.completion_tokens_details"
            ).get("reasoning_tokens"),
            "usage.completion_tokens_details.reasoning_tokens",
            optional=True,
        )
    assert isinstance(prompt, int) and isinstance(completion, int)
    assert isinstance(total, int)
    if prompt + completion != total:
        raise RuntimeSampleError("OpenRouter total tokens do not reconcile")
    if cached is not None and cached > prompt:
        raise RuntimeSampleError("OpenRouter cached input exceeds prompt input")
    if reasoning is not None and reasoning > completion:
        raise RuntimeSampleError("OpenRouter reasoning exceeds completion output")
    # Producer-native usage.cost is intentionally discarded. WI valuation uses
    # only these Token quantities and a timestamped OpenRouter model catalog.
    tokens = {
        "input": prompt,
        "output": completion,
        "cache_read": cached,
        "cache_write": None,
        "reasoning": reasoning,
        "total": total,
    }
    return _base(
        binding=binding,
        producer="openrouter_api",
        raw_event_id=raw_event_id,
        occurred_at=occurred_at,
        model=model,
        effort="unknown",
        tokens=tokens,
        coverage="request_usage_tokens_only",
    )


def validate_runtime_usage_sample(value: object) -> None:
    root = _object(value, "runtime usage sample")
    expected = {
        "schema",
        "work_item_id",
        "agent_id",
        "phase",
        "epoch",
        "runtime",
        "procurement_source",
        "runtime_binding_sha256",
        "producer",
        "event_ref",
        "occurred_at",
        "model",
        "effort",
        "tokens",
        "valuation_policy",
        "coverage",
        "privacy",
        "sample_sha256",
    }
    if set(root) != expected or root.get("schema") != RUNTIME_USAGE_SAMPLE_SCHEMA:
        raise RuntimeSampleError("unsupported runtime usage sample")
    _timestamp(root.get("occurred_at"), "occurred_at")
    if root.get("producer") not in {"cc", "cc_sdk", "openrouter_api"}:
        raise RuntimeSampleError("sample producer is unsupported")
    if root.get("runtime") not in {"cc", "cc_sdk"}:
        raise RuntimeSampleError("sample runtime is unsupported")
    if root.get("producer") in {"cc", "cc_sdk"} and root.get(
        "producer"
    ) != root.get("runtime"):
        raise RuntimeSampleError("CC producer/runtime mismatch")
    tokens = _object(root.get("tokens"), "tokens")
    if set(tokens) != {
        "input",
        "output",
        "cache_read",
        "cache_write",
        "reasoning",
        "total",
    }:
        raise RuntimeSampleError("sample token fields are invalid")
    parsed = {
        field: _count(tokens.get(field), f"tokens.{field}", optional=field in {
            "cache_read", "cache_write", "reasoning"
        })
        for field in tokens
    }
    if parsed["input"] + parsed["output"] != parsed["total"]:  # type: ignore[operator]
        raise RuntimeSampleError("sample total tokens do not conserve")
    for subset, whole in (("cache_read", "input"), ("reasoning", "output")):
        if parsed[subset] is not None and parsed[subset] > parsed[whole]:  # type: ignore[operator]
            raise RuntimeSampleError(f"sample {subset} exceeds {whole}")
    if root.get("valuation_policy") != "tokens_only_openrouter_standard_rates_downstream":
        raise RuntimeSampleError("sample valuation policy is invalid")
    digest = root.get("sample_sha256")
    unsigned = {key: item for key, item in root.items() if key != "sample_sha256"}
    if not isinstance(digest, str) or sha256(_canonical(unsigned)).hexdigest() != digest:
        raise RuntimeSampleError("runtime usage sample digest does not match")
    serialized = _canonical(root).decode("utf-8").lower()
    for forbidden in ("sessionid", "messageid", "requestid", "credential", "prompt", "response"):
        if forbidden in serialized:
            raise RuntimeSampleError("runtime usage sample contains forbidden identity")
