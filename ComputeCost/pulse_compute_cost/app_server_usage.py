"""Privacy-safe App Server usage watermarks for ComputeCost v2.

This module is intended to run in the host/client integration layer that already
receives App Server notifications.  It never asks the model to report its own
usage and never persists provider thread or turn identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


APP_SERVER_FACT_SCHEMA = "app_server_epoch_usage_fact/v1"
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_TOKEN_WIRE_FIELDS = {
    "input_tokens": "inputTokens",
    "cached_input_tokens": "cachedInputTokens",
    "cache_write_input_tokens": "cacheWriteInputTokens",
    "output_tokens": "outputTokens",
    "reasoning_output_tokens": "reasoningOutputTokens",
    "total_tokens": "totalTokens",
}


class AppServerUsageError(ValueError):
    """Raised before persistence when App Server usage evidence is unsafe."""


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppServerUsageError(f"{field} must be a non-empty string")
    return value.strip()


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AppServerUsageError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: object, field: str = "observed_at") -> str:
    raw = _nonempty(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppServerUsageError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AppServerUsageError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _opaque_ref(kind: str, raw_value: str) -> str:
    digest = sha256(f"pulse-computecost-v2:{kind}\0{raw_value}".encode()).hexdigest()
    return f"{kind}_v1_{digest}"


@dataclass(frozen=True)
class UsageWatermark:
    """One provider-owned token watermark; cached/reasoning are subsets."""

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    cache_write_input_tokens: int = 0

    def __post_init__(self) -> None:
        for field in _TOKEN_FIELDS:
            _count(getattr(self, field), field)
        if self.cached_input_tokens > self.input_tokens:
            raise AppServerUsageError("cached_input_tokens cannot exceed input_tokens")
        if self.reasoning_output_tokens > self.output_tokens:
            raise AppServerUsageError(
                "reasoning_output_tokens cannot exceed output_tokens"
            )

    @classmethod
    def from_wire(cls, value: object, field: str) -> "UsageWatermark":
        if not isinstance(value, Mapping):
            raise AppServerUsageError(f"{field} must be an object")
        return cls(
            **{
                name: _count(
                    value.get(wire_name, 0)
                    if name == "cache_write_input_tokens"
                    else value.get(wire_name),
                    f"{field}.{wire_name}",
                )
                for name, wire_name in _TOKEN_WIRE_FIELDS.items()
            }
        )

    @classmethod
    def from_dict(cls, value: object, field: str) -> "UsageWatermark":
        if not isinstance(value, Mapping):
            raise AppServerUsageError(f"{field} must be an object")
        return cls(
            **{
                name: _count(value.get(name), f"{field}.{name}")
                for name in _TOKEN_FIELDS
            }
        )

    def to_dict(self) -> dict[str, int]:
        return {field: getattr(self, field) for field in _TOKEN_FIELDS}

    def delta_from(self, earlier: "UsageWatermark") -> "UsageWatermark":
        values = {
            field: getattr(self, field) - getattr(earlier, field)
            for field in _TOKEN_FIELDS
        }
        if any(value < 0 for value in values.values()):
            raise AppServerUsageError("cumulative token watermark moved backwards")
        return UsageWatermark(**values)

    def plus(self, other: "UsageWatermark") -> "UsageWatermark":
        return UsageWatermark(
            **{
                field: getattr(self, field) + getattr(other, field)
                for field in _TOKEN_FIELDS
            }
        )


@dataclass(frozen=True)
class AppServerTokenUpdate:
    thread_ref: str
    turn_ref: str
    last: UsageWatermark
    total: UsageWatermark
    model_context_window: int | None

    @classmethod
    def from_notification(cls, message: object) -> "AppServerTokenUpdate":
        if not isinstance(message, Mapping):
            raise AppServerUsageError("notification must be an object")
        if message.get("method") != "thread/tokenUsage/updated":
            raise AppServerUsageError(
                "notification method must be thread/tokenUsage/updated"
            )
        params = message.get("params")
        if not isinstance(params, Mapping):
            raise AppServerUsageError("notification params must be an object")
        usage = params.get("tokenUsage")
        if not isinstance(usage, Mapping):
            raise AppServerUsageError("params.tokenUsage must be an object")
        thread_id = _nonempty(params.get("threadId"), "params.threadId")
        turn_id = _nonempty(params.get("turnId"), "params.turnId")
        return cls(
            thread_ref=_opaque_ref("thread", thread_id),
            turn_ref=_opaque_ref("turn", turn_id),
            last=UsageWatermark.from_wire(usage.get("last"), "tokenUsage.last"),
            total=UsageWatermark.from_wire(usage.get("total"), "tokenUsage.total"),
            model_context_window=(
                None
                if usage.get("modelContextWindow") is None
                else _count(
                    usage.get("modelContextWindow"),
                    "tokenUsage.modelContextWindow",
                )
            ),
        )


def _completed_refs(message: object) -> tuple[str, str]:
    if not isinstance(message, Mapping) or message.get("method") != "turn/completed":
        raise AppServerUsageError("notification method must be turn/completed")
    params = message.get("params")
    if not isinstance(params, Mapping):
        raise AppServerUsageError("turn/completed params must be an object")
    thread_id = _nonempty(params.get("threadId"), "params.threadId")
    turn_value = params.get("turn")
    if isinstance(turn_value, Mapping):
        turn_id = _nonempty(turn_value.get("id"), "params.turn.id")
    else:
        turn_id = _nonempty(params.get("turnId"), "params.turnId")
    return _opaque_ref("thread", thread_id), _opaque_ref("turn", turn_id)


@dataclass(frozen=True)
class EpochUsageContext:
    work_item_id: str
    phase: str
    epoch: int
    runtime: str
    procurement_source: str
    model: str
    reasoning_effort: str
    runtime_binding_sha256: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "work_item_id",
            "phase",
            "runtime",
            "procurement_source",
            "model",
            "reasoning_effort",
        ):
            object.__setattr__(self, field, _nonempty(getattr(self, field), field))
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch < 1:
            raise AppServerUsageError("epoch must be a positive integer")
        if self.procurement_source not in {
            "chatgpt_subscription",
            "openrouter_api",
            "claude_subscription",
            "cursor_subscription",
            "unknown",
        }:
            raise AppServerUsageError("unsupported procurement_source")
        if self.runtime_binding_sha256 is not None and (
            not isinstance(self.runtime_binding_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.runtime_binding_sha256) is None
        ):
            raise AppServerUsageError("runtime_binding_sha256 is invalid")

    @property
    def context_ref(self) -> str:
        return _opaque_ref(
            "epoch",
            "\0".join((self.work_item_id, self.phase, str(self.epoch), self.runtime)),
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "work_item_id": self.work_item_id,
            "phase": self.phase,
            "epoch": self.epoch,
            "runtime": self.runtime,
            "procurement_source": self.procurement_source,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "context_ref": self.context_ref,
        }
        if self.runtime_binding_sha256 is not None:
            value["runtime_binding_sha256"] = self.runtime_binding_sha256
        return value


@dataclass(frozen=True)
class LedgerAppendResult:
    inserted: bool
    duplicate: bool
    fact: dict[str, Any]


class AppServerUsageLedger:
    """Append-only facts with process locking, fsync, and immutable replay."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        facts: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AppServerUsageError("cannot read App Server usage ledger") from exc
        for position, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                fact = json.loads(line)
                _validate_fact(fact)
            except (json.JSONDecodeError, AppServerUsageError) as exc:
                raise AppServerUsageError(
                    f"malformed App Server usage ledger at line {position}"
                ) from exc
            fact_id = fact["fact_id"]
            if fact_id in seen:
                raise AppServerUsageError("duplicate fact_id already exists in ledger")
            seen.add(fact_id)
            facts.append(fact)
        return facts

    def append(self, fact: Mapping[str, Any]) -> LedgerAppendResult:
        candidate = dict(fact)
        _validate_fact(candidate)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            lock = lock_path.open("a+", encoding="utf-8")
        except OSError as exc:
            raise AppServerUsageError("cannot open App Server usage lock") from exc
        with lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            for prior in self.read():
                if prior["fact_id"] != candidate["fact_id"]:
                    continue
                if prior != candidate:
                    raise AppServerUsageError("fact_id conflicts with immutable evidence")
                return LedgerAppendResult(False, True, prior)
            try:
                fd = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(_canonical(candidate).decode("utf-8") + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise AppServerUsageError("cannot append App Server usage ledger") from exc
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return LedgerAppendResult(True, False, candidate)


def _validate_fact(value: object) -> None:
    if not isinstance(value, Mapping):
        raise AppServerUsageError("usage fact must be an object")
    if value.get("schema") != APP_SERVER_FACT_SCHEMA:
        raise AppServerUsageError("unsupported usage fact schema")
    for field in ("fact_id", "kind", "observed_at", "thread_ref", "turn_ref"):
        _nonempty(value.get(field), field)
    _timestamp(value.get("observed_at"))
    context = value.get("context")
    if not isinstance(context, Mapping):
        raise AppServerUsageError("context must be an object")
    parsed_context = EpochUsageContext(
        work_item_id=context.get("work_item_id"),
        phase=context.get("phase"),
        epoch=context.get("epoch"),
        runtime=context.get("runtime"),
        procurement_source=context.get("procurement_source"),
        model=context.get("model"),
        reasoning_effort=context.get("reasoning_effort"),
        runtime_binding_sha256=context.get("runtime_binding_sha256"),
    )
    if context.get("context_ref") != parsed_context.context_ref:
        raise AppServerUsageError("context_ref does not match context fields")
    for field in ("last", "cumulative", "step_delta", "epoch_delta"):
        UsageWatermark.from_dict(value.get(field), field)
    context_window = value.get("model_context_window")
    if context_window is not None and _count(
        context_window, "model_context_window"
    ) == 0:
        raise AppServerUsageError("model_context_window must be positive when available")
    digest = value.get("content_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise AppServerUsageError("content_sha256 must be a SHA-256 digest")
    without_digest = {key: item for key, item in value.items() if key != "content_sha256"}
    if sha256(_canonical(without_digest)).hexdigest() != digest:
        raise AppServerUsageError("content_sha256 does not match usage fact")
    raw = _canonical(value).decode("utf-8").lower()
    for forbidden in ("threadid", "turnid", "session_id", "prompt", "response"):
        if forbidden in raw:
            raise AppServerUsageError(f"usage fact contains forbidden field: {forbidden}")


def _fact_id(kind: str, context_ref: str, identity: object) -> str:
    digest = sha256(
        b"pulse-computecost-v2:fact\0"
        + kind.encode()
        + b"\0"
        + context_ref.encode()
        + b"\0"
        + _canonical(identity)
    ).hexdigest()
    return f"asuf1_{digest}"


def _with_content_digest(fact: Mapping[str, Any]) -> dict[str, Any]:
    value = {key: item for key, item in fact.items() if key != "content_sha256"}
    value["content_sha256"] = sha256(_canonical(value)).hexdigest()
    return value
