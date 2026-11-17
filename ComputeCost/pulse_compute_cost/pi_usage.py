"""Privacy-safe native Pi session usage and bounded SCL measurement artifacts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping


PI_USAGE_SCHEMA = "pi_native_usage_fact/v1"
PI_SNAPSHOT_SCHEMA_V1 = "pi_session_usage_snapshot/v1"
PI_SNAPSHOT_SCHEMA = "pi_session_usage_snapshot/v2"
_BOUNDARY_POLICY = "baseline_exclusive_final_inclusive"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_REF_RE = re.compile(r"^[a-z_]+_v1_[0-9a-f]{64}$")
_WI_RE = re.compile(r"^wi_\d{8}_\d{3}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SURFACE_RE = re.compile(r"^surface_v1_[a-z0-9_]{1,96}$")
_FORBIDDEN_KEYS = {
    "session_id",
    "sessionid",
    "sessionfile",
    "session_file",
    "responseid",
    "response_id",
    "cwd",
    "content",
}


class PiUsageError(ValueError):
    """Raised when a Pi session cannot provide trustworthy usage facts."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PiUsageError(f"{field} must be a non-empty string")
    return value.strip()


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PiUsageError(f"{field} must be a non-negative integer")
    return value


def _optional_count(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _count(value, field)


def _timestamp(value: object, field: str) -> str:
    text = _nonempty(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PiUsageError(f"{field} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PiUsageError(f"{field} must be a timezone-aware timestamp")
    return text


def _opaque(kind: str, raw: str) -> str:
    digest = sha256(f"pulse-computecost-v2:pi:{kind}\0{raw}".encode()).hexdigest()
    return f"{kind}_v1_{digest}"


def _assert_privacy(value: object) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if str(key).lower() in _FORBIDDEN_KEYS:
                    raise PiUsageError(
                        f"Pi usage snapshot contains forbidden field: {key}"
                    )
                pending.append(item)
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, str) and (
            current.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", current)
        ):
            raise PiUsageError("Pi usage snapshot contains an absolute path")


@dataclass(frozen=True)
class PiTokenUsage:
    """Pi native counters; cache counters are additive to native input/output."""

    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int
    reasoning: int | None

    @classmethod
    def from_usage(cls, value: object, field: str) -> "PiTokenUsage":
        if not isinstance(value, Mapping):
            raise PiUsageError(f"{field} must be an object")
        parsed = cls(
            input=_count(value.get("input"), f"{field}.input"),
            output=_count(value.get("output"), f"{field}.output"),
            cache_read=_count(value.get("cacheRead"), f"{field}.cacheRead"),
            cache_write=_count(value.get("cacheWrite"), f"{field}.cacheWrite"),
            total_tokens=_count(value.get("totalTokens"), f"{field}.totalTokens"),
            reasoning=_optional_count(value.get("reasoning"), f"{field}.reasoning"),
        )
        expected = parsed.input + parsed.output + parsed.cache_read + parsed.cache_write
        if parsed.total_tokens != expected:
            raise PiUsageError(f"{field}.totalTokens does not conserve native counters")
        if parsed.reasoning is not None and parsed.reasoning > parsed.output:
            raise PiUsageError(f"{field}.reasoning cannot exceed output")
        return parsed

    def to_dict(self) -> dict[str, object]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reasoning": self.reasoning,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class PiUsageBinding:
    work_item_id: str
    phase: str
    epoch: int
    procurement_source: str
    runtime_binding_sha256: str | None = None
    agent_id: str | None = None
    execution_surface: str | None = None
    authority_ref: str | None = None

    def __post_init__(self) -> None:
        for field in ("work_item_id", "phase", "procurement_source"):
            object.__setattr__(self, field, _nonempty(getattr(self, field), field))
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch < 1:
            raise PiUsageError("epoch must be a positive integer")
        if self.procurement_source not in {
            "chatgpt_subscription",
            "openrouter_api",
            "claude_subscription",
            "cursor_subscription",
            "unknown",
        }:
            raise PiUsageError("unsupported procurement_source")
        if self.runtime_binding_sha256 is not None and (
            not isinstance(self.runtime_binding_sha256, str)
            or _SHA_RE.fullmatch(self.runtime_binding_sha256) is None
        ):
            raise PiUsageError("runtime_binding_sha256 is invalid")
        managed_fields = (
            self.runtime_binding_sha256,
            self.agent_id,
            self.execution_surface,
            self.authority_ref,
        )
        if any(item is not None for item in managed_fields) and not all(
            item is not None for item in managed_fields
        ):
            raise PiUsageError("managed Pi binding is incomplete")
        if self.agent_id is not None:
            if _TOKEN_RE.fullmatch(_nonempty(self.agent_id, "agent_id")) is None:
                raise PiUsageError("agent_id is invalid")
            if _WI_RE.fullmatch(self.work_item_id) is None or _TOKEN_RE.fullmatch(self.phase) is None:
                raise PiUsageError("managed Pi WI/phase binding is invalid")
            if not isinstance(self.execution_surface, str) or _SURFACE_RE.fullmatch(self.execution_surface) is None:
                raise PiUsageError("execution_surface is invalid")
            if not isinstance(self.authority_ref, str) or re.fullmatch(
                r"session_v1_[0-9a-f]{64}", self.authority_ref
            ) is None:
                raise PiUsageError("authority_ref is invalid")

    @property
    def managed(self) -> bool:
        return self.runtime_binding_sha256 is not None


@dataclass(frozen=True)
class PiUsageFact:
    fact_id: str
    session_ref: str
    entry_ref: str
    occurred_at: str
    work_item_id: str
    phase: str
    epoch: int
    provider: str
    model: str
    reasoning_effort: str
    procurement_source: str
    activity: str
    usage: PiTokenUsage

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PI_USAGE_SCHEMA,
            "fact_id": self.fact_id,
            "session_ref": self.session_ref,
            "entry_ref": self.entry_ref,
            "occurred_at": self.occurred_at,
            "work_item_id": self.work_item_id,
            "phase": self.phase,
            "epoch": self.epoch,
            "runtime": "pi",
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "procurement_source": self.procurement_source,
            "activity": self.activity,
            "usage": self.usage.to_dict(),
            "coverage": "exact_pi_session_entry",
            "quality": "validated",
        }


@dataclass(frozen=True)
class PiUsageCollection:
    facts: tuple[PiUsageFact, ...]
    last_entry_ref: str | None
    source_prefix_sha256: str
    source_bytes: int
    source_entry_count: int = 0
    excluded_usage_event_count: int = 0
    deduplicated_entry_count: int = 0

    def summary(self) -> dict[str, Any]:
        totals: dict[str, int] = {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 0,
            "total_tokens": 0,
        }
        by_model: dict[str, dict[str, object]] = {}
        request_partitions: list[dict[str, object]] = []
        for fact in self.facts:
            usage = fact.usage
            for field in ("input", "output", "cache_read", "cache_write", "total_tokens"):
                totals[field] += getattr(usage, field)
            totals["reasoning"] += usage.reasoning or 0
            key = f"{fact.provider}/{fact.model}/{fact.reasoning_effort}"
            row = by_model.setdefault(
                key,
                {
                    "event_count": 0,
                    "input": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "output": 0,
                    "reasoning": 0,
                    "total_tokens": 0,
                },
            )
            row["event_count"] = int(row["event_count"]) + 1
            for field in ("input", "cache_read", "cache_write", "output", "total_tokens"):
                row[field] = int(row[field]) + getattr(usage, field)
            row["reasoning"] = int(row["reasoning"]) + (usage.reasoning or 0)
            request_partitions.append(
                {
                    "fact_ref": fact.fact_id,
                    "occurred_at": fact.occurred_at,
                    "provider": fact.provider,
                    "model": fact.model,
                    "reasoning_effort": fact.reasoning_effort,
                    "activity": fact.activity,
                    "native_input": usage.input,
                    "input": usage.input + usage.cache_read + usage.cache_write,
                    "cache_read": usage.cache_read,
                    "cache_write": usage.cache_write,
                    "output": usage.output,
                    "reasoning": usage.reasoning,
                    "total_tokens": usage.total_tokens,
                }
            )
        return {
            "schema": "pi_native_usage_summary/v1",
            "event_count": len(self.facts),
            "totals": totals,
            "by_provider_model_effort": {
                key: by_model[key] for key in sorted(by_model)
            },
            "request_partitions": request_partitions,
            "pricing_contract": "tokens_only_openrouter_authority_required",
            "last_entry_ref": self.last_entry_ref,
            "source_prefix_sha256": self.source_prefix_sha256,
            "source_bytes": self.source_bytes,
            "source_entry_count": self.source_entry_count,
            "excluded_usage_event_count": self.excluded_usage_event_count,
            "deduplicated_entry_count": self.deduplicated_entry_count,
            "privacy": "raw_session_entry_response_content_and_path_omitted",
        }


def _read_source(path: str | Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise PiUsageError("cannot read Pi session file") from exc


def _collect_pi_source(
    raw: bytes,
    binding: PiUsageBinding,
    *,
    start_bytes: int = 0,
    end_bytes: int | None = None,
    not_after: datetime | None = None,
) -> PiUsageCollection:
    final_bytes = len(raw) if end_bytes is None else end_bytes
    if isinstance(start_bytes, bool) or not isinstance(start_bytes, int) or start_bytes < 0:
        raise PiUsageError("Pi baseline source position is invalid")
    if isinstance(final_bytes, bool) or not isinstance(final_bytes, int) or final_bytes < start_bytes or final_bytes > len(raw):
        raise PiUsageError("Pi final source position is invalid")
    prefix = raw[:final_bytes]
    if prefix and not prefix.endswith(b"\n"):
        raise PiUsageError("Pi measured source prefix has an unterminated final record")
    if start_bytes and not raw[:start_bytes].endswith(b"\n"):
        raise PiUsageError("Pi baseline does not end at a record boundary")
    if not_after is not None and (
        not isinstance(not_after, datetime)
        or not_after.tzinfo is None
        or not_after.utcoffset() is None
    ):
        raise PiUsageError("Pi retrospective terminal boundary is invalid")

    header_id: str | None = None
    session_ref: str | None = None
    provider = model = reasoning_effort = "unknown"
    seen_entries: dict[str, bytes] = {}
    facts: list[PiUsageFact] = []
    last_entry_ref: str | None = None
    source_entry_count = 0
    excluded_usage = 0
    deduplicated = 0
    cursor = 0

    for position, raw_line in enumerate(prefix.splitlines(keepends=True), start=1):
        line_start = cursor
        cursor += len(raw_line)
        payload = raw_line[:-1] if raw_line.endswith(b"\n") else raw_line
        if payload.endswith(b"\r"):
            payload = payload[:-1]
        if not payload.strip():
            continue
        try:
            entry = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PiUsageError(f"malformed Pi session JSON at line {position}") from exc
        if not isinstance(entry, Mapping):
            raise PiUsageError(f"Pi session line {position} must be an object")
        entry_type = entry.get("type")
        if position == 1:
            if entry_type != "session":
                raise PiUsageError("Pi session header must be the first record")
            header_id = _nonempty(entry.get("id"), "session.id")
            session_ref = _opaque("pi_session", header_id)
            continue
        if header_id is None or session_ref is None:
            raise PiUsageError("Pi session header is missing")
        if not_after is not None:
            occurred_at = datetime.fromisoformat(
                _timestamp(entry.get("timestamp"), "entry.timestamp").replace(
                    "Z", "+00:00"
                )
            )
            if occurred_at > not_after:
                continue
        entry_id = _nonempty(entry.get("id"), f"line {position} id")
        entry_digest = sha256(_canonical(entry)).digest()
        prior = seen_entries.get(entry_id)
        if prior is not None:
            if prior != entry_digest:
                raise PiUsageError("duplicate Pi session entry id changed immutable content")
            deduplicated += 1
            continue
        seen_entries[entry_id] = entry_digest
        source_entry_count += 1
        entry_ref = _opaque("pi_entry", f"{header_id}\0{entry_id}")
        last_entry_ref = entry_ref

        if entry_type == "model_change":
            provider = _nonempty(entry.get("provider"), "model_change.provider")
            model = _nonempty(entry.get("modelId"), "model_change.modelId")
            continue
        if entry_type == "thinking_level_change":
            reasoning_effort = _nonempty(
                entry.get("thinkingLevel"), "thinking_level_change.thinkingLevel"
            )
            continue

        usage_value: object | None = None
        activity = "unknown"
        if entry_type == "message":
            message = entry.get("message")
            if not isinstance(message, Mapping):
                raise PiUsageError(f"line {position} message must be an object")
            role = message.get("role")
            if role == "assistant":
                provider = _nonempty(message.get("provider"), "message.provider")
                model = _nonempty(message.get("model"), "message.model")
                usage_value = message.get("usage")
                activity = "assistant_turn"
            elif role == "toolResult" and message.get("usage") is not None:
                usage_value = message.get("usage")
                activity = "nested_tool_llm"
        elif entry_type in {"compaction", "branch_summary"} and entry.get("usage") is not None:
            usage_value = entry.get("usage")
            activity = "compaction" if entry_type == "compaction" else "branch_summary"
        if usage_value is None:
            continue

        if line_start < start_bytes:
            # Historical counters are outside this attribution interval. Their
            # immutable source identity is still checked above and by the prefix digest.
            excluded_usage += 1
            continue
        usage = PiTokenUsage.from_usage(usage_value, f"line {position} usage")
        fact_id = _opaque("pi_usage", f"{header_id}\0{entry_id}\0{activity}")
        facts.append(
            PiUsageFact(
                fact_id=fact_id,
                session_ref=session_ref,
                entry_ref=entry_ref,
                occurred_at=_timestamp(entry.get("timestamp"), "entry.timestamp"),
                work_item_id=binding.work_item_id,
                phase=binding.phase,
                epoch=binding.epoch,
                provider=provider,
                model=model,
                reasoning_effort=reasoning_effort,
                procurement_source=binding.procurement_source,
                activity=activity,
                usage=usage,
            )
        )
    if header_id is None:
        raise PiUsageError("Pi session file is empty")
    return PiUsageCollection(
        tuple(facts),
        last_entry_ref,
        sha256(prefix).hexdigest(),
        final_bytes,
        source_entry_count,
        excluded_usage,
        deduplicated,
    )


def collect_pi_session_usage(
    path: str | Path,
    binding: PiUsageBinding,
) -> PiUsageCollection:
    """Read a complete Pi JSONL without retaining message content or raw ids."""

    raw = _read_source(path)
    if raw and not raw.endswith(b"\n"):
        raise PiUsageError("Pi session file has an unterminated final record")
    return _collect_pi_source(raw, binding)


def collect_pi_session_usage_through(
    path: str | Path,
    binding: PiUsageBinding,
    not_after: datetime,
) -> PiUsageCollection:
    """Read only records inside a terminal retrospective lifecycle boundary.

    Canonical collection remains strict over its complete sealed source.  This
    bounded reader prevents later records in a reused live Session from
    invalidating an already terminal historical window.
    """

    raw = _read_source(path)
    if raw and not raw.endswith(b"\n"):
        raise PiUsageError("Pi session file has an unterminated final record")
    return _collect_pi_source(raw, binding, not_after=not_after)


def _position(collection: PiUsageCollection) -> dict[str, object]:
    return {
        "source_bytes": collection.source_bytes,
        "source_prefix_sha256": collection.source_prefix_sha256,
        "last_entry_ref": collection.last_entry_ref,
    }


def _snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    return sha256(_canonical({
        key: item for key, item in snapshot.items() if key != "snapshot_sha256"
    })).hexdigest()


def source_manifest_digest(sources: list[Mapping[str, Any]]) -> str:
    """Aggregate prefix digest of a multi-file source manifest (CC main + subagents)."""
    return sha256(_canonical(list(sources))).hexdigest()


def _validate_sources(value: object, field: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise PiUsageError(f"{field}.sources is invalid")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"source_ref", "source_bytes", "source_prefix_sha256"}:
            raise PiUsageError(f"{field}.sources entry is invalid")
        ref, digest = item.get("source_ref"), item.get("source_prefix_sha256")
        if not isinstance(ref, str) or _OPAQUE_REF_RE.fullmatch(ref) is None:
            raise PiUsageError(f"{field}.sources ref is invalid")
        if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
            raise PiUsageError(f"{field}.sources digest is invalid")
        result.append({"source_ref": ref, "source_bytes": _count(item.get("source_bytes"), f"{field}.sources.bytes"),
                       "source_prefix_sha256": digest})
    refs = [item["source_ref"] for item in result]
    if refs != sorted(set(refs)):
        raise PiUsageError(f"{field}.sources must be sorted and unique")
    return result


def _validate_position(value: object, field: str, *, source_manifest: bool = False) -> dict[str, object]:
    keys = {"source_bytes", "source_prefix_sha256", "last_entry_ref"}
    if source_manifest:
        keys.add("sources")
    if not isinstance(value, Mapping) or set(value) != keys:
        raise PiUsageError(f"{field} is invalid")
    source_bytes = _count(value.get("source_bytes"), f"{field}.source_bytes")
    digest = value.get("source_prefix_sha256")
    if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
        raise PiUsageError(f"{field}.source_prefix_sha256 is invalid")
    last = value.get("last_entry_ref")
    if last is not None and (
        not isinstance(last, str) or _OPAQUE_REF_RE.fullmatch(last) is None
    ):
        raise PiUsageError(f"{field}.last_entry_ref is invalid")
    result: dict[str, object] = {
        "source_bytes": source_bytes,
        "source_prefix_sha256": digest,
        "last_entry_ref": last,
    }
    if source_manifest:
        sources = _validate_sources(value.get("sources"), field)
        if (sum(int(item["source_bytes"]) for item in sources) != source_bytes  # type: ignore[call-overload]
                or source_manifest_digest(sources) != digest):
            raise PiUsageError(f"{field} aggregate does not match its source manifest")
        result["sources"] = sources
    return result


def validate_pi_usage_summary(value: object, *, family: str = "pi") -> dict[str, Any]:
    # CC 复用同一有界汇总形状，仅 schema 与 fact 前缀按运行时族区分。
    fields = {
        "schema", "event_count", "totals", "by_provider_model_effort",
        "request_partitions", "pricing_contract", "last_entry_ref",
        "source_prefix_sha256", "source_bytes", "source_entry_count",
        "excluded_usage_event_count", "deduplicated_entry_count", "privacy",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise PiUsageError("Pi usage snapshot summary fields are invalid")
    if value.get("schema") != f"{family}_native_usage_summary/v1":
        raise PiUsageError("Pi usage snapshot summary schema is invalid")
    event_count = _count(value.get("event_count"), "summary.event_count")
    totals = value.get("totals")
    token_fields = {"input", "output", "cache_read", "cache_write", "reasoning", "total_tokens"}
    if not isinstance(totals, Mapping) or set(totals) != token_fields:
        raise PiUsageError("Pi usage snapshot totals are invalid")
    parsed_totals = {field: _count(totals.get(field), f"summary.totals.{field}") for field in token_fields}
    if parsed_totals["total_tokens"] != (
        parsed_totals["input"] + parsed_totals["output"]
        + parsed_totals["cache_read"] + parsed_totals["cache_write"]
    ):
        raise PiUsageError("Pi usage snapshot totals do not conserve native counters")
    if parsed_totals["reasoning"] > parsed_totals["output"]:
        raise PiUsageError("Pi usage snapshot reasoning exceeds output")
    requests = value.get("request_partitions")
    if not isinstance(requests, list) or len(requests) != event_count:
        raise PiUsageError("Pi usage request partitions do not match event count")
    request_fields = {
        "fact_ref", "occurred_at", "provider", "model", "reasoning_effort",
        "activity", "native_input", "input", "cache_read", "cache_write",
        "output", "reasoning", "total_tokens",
    }
    request_totals = {field: 0 for field in token_fields}
    seen_facts: set[str] = set()
    for index, request in enumerate(requests):
        if not isinstance(request, Mapping) or set(request) != request_fields:
            raise PiUsageError(f"Pi usage request partition {index} is invalid")
        fact_ref = request.get("fact_ref")
        if (
            not isinstance(fact_ref, str)
            or _OPAQUE_REF_RE.fullmatch(fact_ref) is None
            or not fact_ref.startswith(f"{family}_usage_v1_")
            or fact_ref in seen_facts
        ):
            raise PiUsageError("Pi usage request fact references are invalid")
        seen_facts.add(fact_ref)
        _timestamp(request.get("occurred_at"), f"request_partitions[{index}].occurred_at")
        for field in ("provider", "model", "reasoning_effort", "activity"):
            _nonempty(request.get(field), f"request_partitions[{index}].{field}")
        native_input = _count(request.get("native_input"), f"request_partitions[{index}].native_input")
        cache_read = _count(request.get("cache_read"), f"request_partitions[{index}].cache_read")
        cache_write = _count(request.get("cache_write"), f"request_partitions[{index}].cache_write")
        normalized_input = _count(request.get("input"), f"request_partitions[{index}].input")
        output = _count(request.get("output"), f"request_partitions[{index}].output")
        reasoning = request.get("reasoning")
        parsed_reasoning = 0 if reasoning is None else _count(reasoning, f"request_partitions[{index}].reasoning")
        total_tokens = _count(request.get("total_tokens"), f"request_partitions[{index}].total_tokens")
        if normalized_input != native_input + cache_read + cache_write:
            raise PiUsageError("Pi usage request input/cache counters do not conserve")
        if total_tokens != normalized_input + output or parsed_reasoning > output:
            raise PiUsageError("Pi usage request total/reasoning counters do not conserve")
        for field, amount in {
            "input": native_input, "cache_read": cache_read, "cache_write": cache_write,
            "output": output, "reasoning": parsed_reasoning, "total_tokens": total_tokens,
        }.items():
            request_totals[field] += amount
    if request_totals != parsed_totals:
        raise PiUsageError("Pi usage request partitions do not conserve totals")
    by_model = value.get("by_provider_model_effort")
    if not isinstance(by_model, Mapping):
        raise PiUsageError("Pi usage model partitions are invalid")
    model_totals = {field: 0 for field in token_fields}
    model_events = 0
    model_fields = token_fields | {"event_count"}
    for key, row in by_model.items():
        if not isinstance(key, str) or not key or not isinstance(row, Mapping) or set(row) != model_fields:
            raise PiUsageError("Pi usage model partition is invalid")
        model_events += _count(row.get("event_count"), "model.event_count")
        for field in token_fields:
            model_totals[field] += _count(row.get(field), f"model.{field}")
    if model_events != event_count or model_totals != parsed_totals:
        raise PiUsageError("Pi usage model partitions do not conserve")
    if value.get("pricing_contract") != "tokens_only_openrouter_authority_required" or value.get("privacy") != "raw_session_entry_response_content_and_path_omitted":
        raise PiUsageError("Pi usage summary authority labels are invalid")
    _validate_position({
        "source_bytes": value.get("source_bytes"),
        "source_prefix_sha256": value.get("source_prefix_sha256"),
        "last_entry_ref": value.get("last_entry_ref"),
    }, "summary source position")
    _count(value.get("source_entry_count"), "summary.source_entry_count")
    _count(value.get("excluded_usage_event_count"), "summary.excluded_usage_event_count")
    _count(value.get("deduplicated_entry_count"), "summary.deduplicated_entry_count")
    _assert_privacy(value)
    return dict(value)


def validate_pi_attribution_boundary(
    value: object,
    *,
    summary: Mapping[str, Any],
    measurement_role: str,
    source_manifest: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "policy", "baseline_snapshot_sha256", "baseline", "final",
        "excluded_pre_baseline_usage_events", "deduplicated_entries",
    }:
        raise PiUsageError("bounded Pi attribution boundary is invalid")
    if value.get("policy") != _BOUNDARY_POLICY:
        raise PiUsageError("bounded Pi attribution policy is invalid")
    baseline = _validate_position(value.get("baseline"), "baseline position", source_manifest=source_manifest)
    final = _validate_position(value.get("final"), "final position", source_manifest=source_manifest)
    if int(final["source_bytes"]) < int(baseline["source_bytes"]):
        raise PiUsageError("bounded Pi final position precedes baseline")
    if source_manifest:
        # 每个 baseline 文件都必须在 final 中延续，且只增不减；final 中的新文件从 0 起算。
        final_sources = {item["source_ref"]: item for item in final["sources"]}  # type: ignore[union-attr]
        for item in baseline["sources"]:  # type: ignore[union-attr]
            later = final_sources.get(item["source_ref"])
            if later is None or int(later["source_bytes"]) < int(item["source_bytes"]):
                raise PiUsageError("bounded final source manifest does not extend its baseline")
    excluded = _count(
        value.get("excluded_pre_baseline_usage_events"),
        "excluded_pre_baseline_usage_events",
    )
    deduplicated = _count(value.get("deduplicated_entries"), "deduplicated_entries")
    if (
        summary["source_bytes"] != final["source_bytes"]
        or summary["source_prefix_sha256"] != final["source_prefix_sha256"]
        or summary["last_entry_ref"] != final["last_entry_ref"]
    ):
        raise PiUsageError("bounded Pi summary does not match final source position")
    if (
        summary["excluded_usage_event_count"] != excluded
        or summary["deduplicated_entry_count"] != deduplicated
    ):
        raise PiUsageError("bounded Pi summary exclusions do not match boundary")
    baseline_digest = value.get("baseline_snapshot_sha256")
    if measurement_role == "baseline":
        if (
            baseline_digest is not None
            or baseline != final
            or summary["event_count"] != 0
            or any(summary["totals"].values())
        ):
            raise PiUsageError("bounded Pi baseline is not an exact zero boundary")
    elif (
        measurement_role not in {"progress", "final"}
        or not isinstance(baseline_digest, str)
        or _SHA_RE.fullmatch(baseline_digest) is None
    ):
        raise PiUsageError("bounded Pi measurement lacks its immutable baseline digest")
    return dict(value)


def validate_pi_session_snapshot(value: object) -> None:
    if not isinstance(value, Mapping):
        raise PiUsageError("unsupported Pi session usage snapshot")
    schema = value.get("schema")
    if schema not in {PI_SNAPSHOT_SCHEMA_V1, PI_SNAPSHOT_SCHEMA}:
        raise PiUsageError("unsupported Pi session usage snapshot")
    generated_at = _timestamp(value.get("generated_at"), "generated_at")
    del generated_at
    for field in ("work_item_id", "phase", "procurement_source"):
        _nonempty(value.get(field), field)
    epoch = value.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise PiUsageError("Pi usage snapshot epoch must be positive")
    if value.get("lifecycle_state") not in {"open", "sealed"}:
        raise PiUsageError("Pi usage snapshot lifecycle_state is invalid")
    digest = value.get("snapshot_sha256")
    if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
        raise PiUsageError("Pi usage snapshot digest is invalid")
    if _snapshot_digest(value) != digest:
        raise PiUsageError("Pi usage snapshot digest does not match content")
    if schema == PI_SNAPSHOT_SCHEMA_V1:
        if not isinstance(value.get("summary"), Mapping):
            raise PiUsageError("Pi usage snapshot summary must be an object")
        binding_digest = value.get("runtime_binding_sha256")
        if binding_digest is not None and (
            not isinstance(binding_digest, str) or _SHA_RE.fullmatch(binding_digest) is None
        ):
            raise PiUsageError("Pi usage snapshot runtime binding digest is invalid")
        _assert_privacy(value)
        return

    expected_fields = {
        "schema", "generated_at", "lifecycle_state", "measurement_role",
        "work_item_id", "agent_id", "execution_surface", "phase", "epoch",
        "runtime", "procurement_source", "authority_ref",
        "runtime_binding_sha256", "attribution_boundary", "summary",
        "coverage", "snapshot_sha256",
    }
    if set(value) != expected_fields:
        raise PiUsageError("bounded Pi usage snapshot fields are invalid")
    role = value.get("measurement_role")
    lifecycle = value.get("lifecycle_state")
    if role not in {"baseline", "progress", "final"} or (lifecycle == "sealed") != (role == "final"):
        raise PiUsageError("bounded Pi measurement role is invalid")
    if value.get("runtime") != "pi":
        raise PiUsageError("bounded Pi runtime is invalid")
    if not isinstance(value.get("work_item_id"), str) or _WI_RE.fullmatch(value["work_item_id"]) is None:
        raise PiUsageError("bounded Pi work item is invalid")
    if not isinstance(value.get("phase"), str) or _TOKEN_RE.fullmatch(value["phase"]) is None:
        raise PiUsageError("bounded Pi phase is invalid")
    if not isinstance(value.get("agent_id"), str) or _TOKEN_RE.fullmatch(value["agent_id"]) is None:
        raise PiUsageError("bounded Pi agent is invalid")
    if value.get("procurement_source") not in {
        "chatgpt_subscription", "openrouter_api", "claude_subscription",
        "cursor_subscription", "unknown",
    }:
        raise PiUsageError("bounded Pi procurement source is invalid")
    surface = value.get("execution_surface")
    if not isinstance(surface, str) or _SURFACE_RE.fullmatch(surface) is None:
        raise PiUsageError("bounded Pi execution surface is invalid")
    authority = value.get("authority_ref")
    if not isinstance(authority, str) or re.fullmatch(r"session_v1_[0-9a-f]{64}", authority) is None:
        raise PiUsageError("bounded Pi authority ref is invalid")
    binding_digest = value.get("runtime_binding_sha256")
    if not isinstance(binding_digest, str) or _SHA_RE.fullmatch(binding_digest) is None:
        raise PiUsageError("bounded Pi runtime binding digest is invalid")
    summary = validate_pi_usage_summary(value.get("summary"))
    validate_pi_attribution_boundary(
        value.get("attribution_boundary"),
        summary=summary,
        measurement_role=str(role),
    )
    expected_coverage = {
        "baseline": "exact_zero_baseline",
        "progress": "exact_bounded_open",
        "final": "exact_bounded_sealed",
    }[str(role)]
    if value.get("coverage") != expected_coverage:
        raise PiUsageError("bounded Pi coverage is invalid")
    _assert_privacy(value)


def _validate_baseline_for_binding(
    baseline_snapshot: object,
    binding: PiUsageBinding,
) -> Mapping[str, Any]:
    validate_pi_session_snapshot(baseline_snapshot)
    if not isinstance(baseline_snapshot, Mapping) or baseline_snapshot.get("schema") != PI_SNAPSHOT_SCHEMA or baseline_snapshot.get("measurement_role") != "baseline":
        raise PiUsageError("managed Pi measurement requires a bounded baseline")
    expected = {
        "work_item_id": binding.work_item_id,
        "agent_id": binding.agent_id,
        "execution_surface": binding.execution_surface,
        "phase": binding.phase,
        "epoch": binding.epoch,
        "procurement_source": binding.procurement_source,
        "authority_ref": binding.authority_ref,
        "runtime_binding_sha256": binding.runtime_binding_sha256,
    }
    if any(baseline_snapshot.get(key) != item for key, item in expected.items()):
        raise PiUsageError("managed Pi baseline does not match runtime authority")
    return baseline_snapshot


def _build_bounded_snapshot(
    path: str | Path,
    binding: PiUsageBinding,
    *,
    generated_at: str,
    lifecycle_state: str,
    measurement_role: str,
    baseline_snapshot: Mapping[str, Any] | None,
    unpersisted_baseline: bool = False,
) -> dict[str, Any]:
    raw = _read_source(path)
    if raw and not raw.endswith(b"\n"):
        raise PiUsageError("Pi session file has an unterminated final record")
    if unpersisted_baseline and measurement_role != "baseline":
        raise PiUsageError("unpersisted origin is only valid for a baseline")
    if measurement_role == "baseline":
        if lifecycle_state != "open" or baseline_snapshot is not None:
            raise PiUsageError("managed Pi baseline lifecycle is invalid")
        entire = _collect_pi_source(raw, binding, start_bytes=len(raw))
        if unpersisted_baseline:
            # A native pre-turn header/configuration is volatile until Pi's first
            # assistant write. The prospective interval starts at byte zero, so
            # a restart cannot invalidate it or exclude any future usage.
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
            if any(record.get("type") not in {"model_change", "thinking_level_change"}
                   for record in records[1:]):
                raise PiUsageError("unpersisted baseline contains non-configuration entries")
            entire = replace(entire, last_entry_ref=None,
                             source_prefix_sha256=sha256(b"").hexdigest(),
                             source_bytes=0, source_entry_count=0)
        collection = entire
        baseline_position = final_position = _position(entire)
        baseline_digest: str | None = None
        excluded = entire.excluded_usage_event_count
    else:
        if baseline_snapshot is None:
            raise PiUsageError("managed Pi measurement is missing its immutable baseline")
        baseline = _validate_baseline_for_binding(baseline_snapshot, binding)
        raw_boundary = baseline["attribution_boundary"]
        assert isinstance(raw_boundary, Mapping)
        baseline_position = _validate_position(raw_boundary["baseline"], "baseline position")
        baseline_bytes = int(baseline_position["source_bytes"])
        if baseline_bytes > len(raw) or sha256(raw[:baseline_bytes]).hexdigest() != baseline_position["source_prefix_sha256"]:
            raise PiUsageError("Pi session changed before the immutable baseline")
        collection = _collect_pi_source(raw, binding, start_bytes=baseline_bytes)
        final_position = _position(collection)
        baseline_digest = str(baseline["snapshot_sha256"])
        excluded = int(raw_boundary["excluded_pre_baseline_usage_events"])
        if collection.excluded_usage_event_count != excluded:
            raise PiUsageError("Pi pre-baseline usage changed after baseline capture")
    snapshot: dict[str, Any] = {
        "schema": PI_SNAPSHOT_SCHEMA,
        "generated_at": generated_at,
        "lifecycle_state": lifecycle_state,
        "measurement_role": measurement_role,
        "work_item_id": binding.work_item_id,
        "agent_id": binding.agent_id,
        "execution_surface": binding.execution_surface,
        "phase": binding.phase,
        "epoch": binding.epoch,
        "runtime": "pi",
        "procurement_source": binding.procurement_source,
        "authority_ref": binding.authority_ref,
        "runtime_binding_sha256": binding.runtime_binding_sha256,
        "attribution_boundary": {
            "policy": _BOUNDARY_POLICY,
            "baseline_snapshot_sha256": baseline_digest,
            "baseline": baseline_position,
            "final": final_position,
            "excluded_pre_baseline_usage_events": excluded,
            "deduplicated_entries": collection.deduplicated_entry_count,
        },
        "summary": collection.summary(),
        "coverage": {
            "baseline": "exact_zero_baseline",
            "progress": "exact_bounded_open",
            "final": "exact_bounded_sealed",
        }[measurement_role],
    }
    snapshot["snapshot_sha256"] = _snapshot_digest(snapshot)
    validate_pi_session_snapshot(snapshot)
    return snapshot


def build_pi_session_snapshot(
    path: str | Path,
    binding: PiUsageBinding,
    *,
    generated_at: str | None = None,
    lifecycle_state: str = "open",
    measurement_role: str | None = None,
    baseline_snapshot: Mapping[str, Any] | None = None,
    unpersisted_baseline: bool = False,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _timestamp(generated, "generated_at")
    role = measurement_role or ("final" if lifecycle_state == "sealed" else "baseline")
    if binding.managed:
        return _build_bounded_snapshot(
            path,
            binding,
            generated_at=generated,
            lifecycle_state=lifecycle_state,
            measurement_role=role,
            baseline_snapshot=baseline_snapshot,
            unpersisted_baseline=unpersisted_baseline,
        )
    if unpersisted_baseline:
        raise PiUsageError("unpersisted baseline requires a managed binding")
    if role not in {"baseline", "final"}:
        raise PiUsageError("legacy Pi snapshots do not support progress measurement")
    collection = collect_pi_session_usage(path, binding)
    snapshot: dict[str, Any] = {
        "schema": PI_SNAPSHOT_SCHEMA_V1,
        "generated_at": generated,
        "lifecycle_state": lifecycle_state,
        "work_item_id": binding.work_item_id,
        "phase": binding.phase,
        "epoch": binding.epoch,
        "runtime": "pi",
        "procurement_source": binding.procurement_source,
        "runtime_binding_sha256": binding.runtime_binding_sha256,
        "summary": collection.summary(),
    }
    snapshot["snapshot_sha256"] = _snapshot_digest(snapshot)
    validate_pi_session_snapshot(snapshot)
    return snapshot


def verify_pi_snapshot_source_prefix(
    path: str | Path,
    snapshot: Mapping[str, Any],
    *,
    position: str,
) -> dict[str, int]:
    """Verify an immutable managed prefix while explicitly ignoring later suffixes."""

    validate_pi_session_snapshot(snapshot)
    if snapshot.get("schema") != PI_SNAPSHOT_SCHEMA or position not in {"baseline", "final"}:
        raise PiUsageError("Pi source prefix verification requires a bounded snapshot")
    boundary = snapshot["attribution_boundary"]
    assert isinstance(boundary, Mapping)
    source_position = _validate_position(boundary[position], f"{position} position")
    raw = _read_source(path)
    source_bytes = int(source_position["source_bytes"])
    if source_bytes > len(raw) or sha256(raw[:source_bytes]).hexdigest() != source_position["source_prefix_sha256"]:
        raise PiUsageError(f"Pi session changed before the sealed {position} position")
    suffix = raw[source_bytes:]
    return {
        "verified_prefix_bytes": source_bytes,
        "excluded_suffix_bytes": len(suffix),
        "excluded_suffix_records": suffix.count(b"\n"),
    }
