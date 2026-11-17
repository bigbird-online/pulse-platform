"""Host-bound App Server thread rate-card evidence for ComputeCost v2."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from .app_server_usage import _opaque_ref
from .runtime_binding import RuntimeUsageBinding


APP_SERVER_THREAD_USAGE_SCHEMA = "app_server_thread_usage_snapshot/v1"
_GROUP_FIELDS = {
    "model",
    "reasoningEffort",
    "speed",
    "inputTokens",
    "cachedInputTokens",
    "netNewInputTokens",
    "outputTokens",
    "totalTokens",
    "estimatedUsageCreditsMicros",
}


class AppServerThreadUsageError(ValueError):
    """Raised when thread-scoped rate-card evidence is unsafe or inconsistent."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AppServerThreadUsageError(f"{field} must be an object")
    return value


def _count(value: object, field: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AppServerThreadUsageError(f"{field} must be a non-negative integer")
    return value


def _text(value: object, field: str, *, optional: bool = False) -> str:
    if optional and value is None:
        return "unknown"
    if not isinstance(value, str) or not value.strip():
        raise AppServerThreadUsageError(f"{field} must be a non-empty string")
    return value.strip()


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise AppServerThreadUsageError("generated_at must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppServerThreadUsageError("generated_at must be a timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AppServerThreadUsageError("generated_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_group(value: object, index: int) -> dict[str, object]:
    raw = _object(value, f"threadUsage.groups[{index}]")
    if not set(raw).issubset(_GROUP_FIELDS) or "estimatedUsageCreditsMicros" not in raw:
        raise AppServerThreadUsageError("thread usage group fields are invalid")
    group: dict[str, object] = {
        "model": _text(raw.get("model"), f"groups[{index}].model", optional=True),
        "reasoning_effort": _text(
            raw.get("reasoningEffort"),
            f"groups[{index}].reasoningEffort",
            optional=True,
        ),
        "speed": _text(raw.get("speed"), f"groups[{index}].speed", optional=True),
        "input_tokens": _count(
            raw.get("inputTokens"), f"groups[{index}].inputTokens", optional=True
        ),
        "cached_input_tokens": _count(
            raw.get("cachedInputTokens"),
            f"groups[{index}].cachedInputTokens",
            optional=True,
        ),
        "net_new_input_tokens": _count(
            raw.get("netNewInputTokens"),
            f"groups[{index}].netNewInputTokens",
            optional=True,
        ),
        "output_tokens": _count(
            raw.get("outputTokens"),
            f"groups[{index}].outputTokens",
            optional=True,
        ),
        "total_tokens": _count(
            raw.get("totalTokens"), f"groups[{index}].totalTokens", optional=True
        ),
        "estimated_usage_credits_micros": _count(
            raw.get("estimatedUsageCreditsMicros"),
            f"groups[{index}].estimatedUsageCreditsMicros",
        ),
    }
    input_tokens = group["input_tokens"]
    cached = group["cached_input_tokens"]
    net_new = group["net_new_input_tokens"]
    output = group["output_tokens"]
    total = group["total_tokens"]
    if isinstance(input_tokens, int) and isinstance(cached, int):
        if cached > input_tokens:
            raise AppServerThreadUsageError("cached input exceeds input")
        if isinstance(net_new, int) and net_new != input_tokens - cached:
            raise AppServerThreadUsageError("net-new input does not reconcile")
    if isinstance(input_tokens, int) and isinstance(output, int) and isinstance(total, int):
        if total != input_tokens + output:
            raise AppServerThreadUsageError("group total tokens do not reconcile")
    return group


def build_app_server_thread_usage_snapshot(
    response: object,
    *,
    raw_thread_id: str,
    binding: RuntimeUsageBinding,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Sanitize one host response while raw thread identity remains transient."""

    if binding.runtime not in {"codex", "chatgpt_work", "chatgpt_web"}:
        raise AppServerThreadUsageError(
            "runtime usage binding is not for an App Server host"
        )
    expected_thread = _text(raw_thread_id, "raw_thread_id")
    root = _object(response, "account/usage/read response")
    usage = _object(root.get("threadUsage"), "threadUsage")
    allowed_usage_fields = {
        "threadId",
        "estimatedUsageCreditsMicros",
        "estimatedUsageUsdMicros",
        "groups",
    }
    required_usage_fields = {
        "threadId",
        "estimatedUsageCreditsMicros",
        "groups",
    }
    if not required_usage_fields.issubset(usage) or not set(usage).issubset(
        allowed_usage_fields
    ):
        raise AppServerThreadUsageError("threadUsage fields are invalid")
    if _text(usage.get("threadId"), "threadUsage.threadId") != expected_thread:
        raise AppServerThreadUsageError("thread usage belongs to another thread")
    groups_raw = usage.get("groups")
    if not isinstance(groups_raw, list):
        raise AppServerThreadUsageError("threadUsage.groups must be an array")
    groups = [_normalize_group(item, index) for index, item in enumerate(groups_raw)]
    groups.sort(key=lambda item: _canonical(item))
    estimated_credits = _count(
        usage.get("estimatedUsageCreditsMicros"),
        "threadUsage.estimatedUsageCreditsMicros",
    )
    group_credits = sum(
        int(group["estimated_usage_credits_micros"]) for group in groups
    )
    if groups and group_credits != estimated_credits:
        raise AppServerThreadUsageError("thread usage credits do not conserve")
    snapshot: dict[str, Any] = {
        "schema": APP_SERVER_THREAD_USAGE_SCHEMA,
        "generated_at": _timestamp(
            generated_at
            or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ),
        "work_item_id": binding.work_item_id,
        "agent_id": binding.agent_id,
        "phase": binding.phase,
        "epoch": binding.epoch,
        "runtime": binding.runtime,
        "procurement_source": binding.procurement_source,
        "runtime_binding_sha256": binding.binding_sha256,
        "thread_ref": _opaque_ref("thread", expected_thread),
        "estimated_usage_credits_micros": estimated_credits,
        "estimated_usage_usd_micros": _count(
            usage.get("estimatedUsageUsdMicros"),
            "threadUsage.estimatedUsageUsdMicros",
            optional=True,
        ),
        "groups": groups,
        "coverage": "estimated_thread_billing_route",
        "cash_semantics": "provider_estimate_not_actual_billed_cash",
        "privacy": "raw_provider_identity_and_content_omitted",
    }
    snapshot["snapshot_sha256"] = sha256(_canonical(snapshot)).hexdigest()
    validate_app_server_thread_usage_snapshot(snapshot)
    return snapshot


def validate_app_server_thread_usage_snapshot(value: object) -> None:
    root = _object(value, "App Server thread usage snapshot")
    expected = {
        "schema",
        "generated_at",
        "work_item_id",
        "agent_id",
        "phase",
        "epoch",
        "runtime",
        "procurement_source",
        "runtime_binding_sha256",
        "thread_ref",
        "estimated_usage_credits_micros",
        "estimated_usage_usd_micros",
        "groups",
        "coverage",
        "cash_semantics",
        "privacy",
        "snapshot_sha256",
    }
    if set(root) != expected or root.get("schema") != APP_SERVER_THREAD_USAGE_SCHEMA:
        raise AppServerThreadUsageError("unsupported App Server thread usage snapshot")
    _timestamp(root.get("generated_at"))
    epoch = root.get("epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise AppServerThreadUsageError("snapshot epoch is invalid")
    for field in ("agent_id", "phase"):
        _text(root.get(field), field)
    if not isinstance(root.get("work_item_id"), str) or re.fullmatch(
        r"wi_\d{8}_\d{3}", root["work_item_id"]
    ) is None:
        raise AppServerThreadUsageError("snapshot work item is invalid")
    if root.get("runtime") not in {"codex", "chatgpt_work", "chatgpt_web"}:
        raise AppServerThreadUsageError("snapshot runtime is invalid")
    if root.get("procurement_source") not in {
        "chatgpt_subscription",
        "openrouter_api",
        "claude_subscription",
        "cursor_subscription",
        "unknown",
    }:
        raise AppServerThreadUsageError("snapshot procurement source is invalid")
    if not isinstance(root.get("runtime_binding_sha256"), str) or re.fullmatch(
        r"[0-9a-f]{64}", root["runtime_binding_sha256"]
    ) is None:
        raise AppServerThreadUsageError("snapshot runtime binding digest is invalid")
    if not isinstance(root.get("thread_ref"), str) or re.fullmatch(
        r"thread_v1_[0-9a-f]{64}", root["thread_ref"]
    ) is None:
        raise AppServerThreadUsageError("snapshot thread ref is invalid")
    _count(root.get("estimated_usage_credits_micros"), "estimated credits")
    _count(root.get("estimated_usage_usd_micros"), "estimated USD", optional=True)
    if root.get("coverage") != "estimated_thread_billing_route":
        raise AppServerThreadUsageError("thread usage coverage is invalid")
    if root.get("cash_semantics") != "provider_estimate_not_actual_billed_cash":
        raise AppServerThreadUsageError("thread usage cash semantics are invalid")
    if root.get("privacy") != "raw_provider_identity_and_content_omitted":
        raise AppServerThreadUsageError("thread usage privacy marker is invalid")
    groups = root.get("groups")
    if not isinstance(groups, list):
        raise AppServerThreadUsageError("snapshot groups must be an array")
    if groups != sorted(groups, key=lambda item: _canonical(item)):
        raise AppServerThreadUsageError("snapshot groups are not canonical")
    for index, group in enumerate(groups):
        normalized = _normalize_group(
            {
                "model": group.get("model"),
                "reasoningEffort": group.get("reasoning_effort"),
                "speed": group.get("speed"),
                "inputTokens": group.get("input_tokens"),
                "cachedInputTokens": group.get("cached_input_tokens"),
                "netNewInputTokens": group.get("net_new_input_tokens"),
                "outputTokens": group.get("output_tokens"),
                "totalTokens": group.get("total_tokens"),
                "estimatedUsageCreditsMicros": group.get(
                    "estimated_usage_credits_micros"
                ),
            },
            index,
        )
        if normalized != group:
            raise AppServerThreadUsageError("snapshot group is not canonical")
    if groups and sum(
        int(group["estimated_usage_credits_micros"]) for group in groups
    ) != root["estimated_usage_credits_micros"]:
        raise AppServerThreadUsageError("snapshot credits do not conserve")
    digest = root.get("snapshot_sha256")
    unsigned = {key: item for key, item in root.items() if key != "snapshot_sha256"}
    if not isinstance(digest, str) or sha256(_canonical(unsigned)).hexdigest() != digest:
        raise AppServerThreadUsageError("thread usage snapshot digest does not match")
    serialized = _canonical(root).decode("utf-8").lower()
    for forbidden in ("threadid", "accountid", "userid", "credential", "codexhome"):
        if forbidden in serialized:
            raise AppServerThreadUsageError(
                "thread usage snapshot contains forbidden raw identity"
            )
