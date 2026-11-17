"""Adapters from Relay, Claude Code, and Codex evidence into usage_event.v1."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .cc_lineage import cc_transcript_lineage
from .events import (
    UNKNOWN,
    Lineage,
    TokenUsage,
    UsageEvent,
    UsageEventError,
    parse_timestamp,
    utc_text,
)
from .token_semantics import InputTokenClasses, TokenSemanticsError


class AdapterError(ValueError):
    """Raised when source evidence is internally inconsistent."""


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    source: str
    runtime: str = UNKNOWN


@dataclass
class CollectionDiagnostics:
    source_files: Counter[str] = field(default_factory=Counter)
    source_rows: Counter[str] = field(default_factory=Counter)
    malformed_rows: Counter[str] = field(default_factory=Counter)
    emitted_events: Counter[str] = field(default_factory=Counter)
    reconciled_relay_rows: Counter[str] = field(default_factory=Counter)
    deduplicated_events: Counter[str] = field(default_factory=Counter)
    unmatched_relay_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_files": dict(sorted(self.source_files.items())),
            "source_rows": dict(sorted(self.source_rows.items())),
            "malformed_rows": dict(sorted(self.malformed_rows.items())),
            "emitted_events": dict(sorted(self.emitted_events.items())),
            "reconciled_relay_rows": dict(sorted(self.reconciled_relay_rows.items())),
            "deduplicated_events": dict(sorted(self.deduplicated_events.items())),
            "unmatched_relay_rows": self.unmatched_relay_rows,
        }


@dataclass(frozen=True)
class CodexNativeObservation:
    """Canonical rollout counter metadata for receipt materialization only."""
    session_id: str
    source_path: Path
    source_position: int
    counter: dict[str, int]
    counter_fields: frozenset[str]
    previous_counter: dict[str, int]
    previous_fields: frozenset[str]
    interval_start: str | None = None
    interval_end: str | None = None
    # Native startup metadata is kept only in this in-memory side channel.
    startup_cwd: str | None = None
    originator: str | None = None
    native_source: str | None = None
    startup_ref: str | None = None
    parent_session_id: str | None = None


@dataclass
class UsageCollection:
    events: list[UsageEvent]
    diagnostics: CollectionDiagnostics

    def summary(self) -> dict[str, Any]:
        by_adapter: Counter[str] = Counter()
        by_source: Counter[str] = Counter()
        by_runtime: Counter[str] = Counter()
        by_month: Counter[str] = Counter()
        tokens = Counter()
        partial = 0
        for event in self.events:
            by_adapter[event.adapter] += 1
            by_source[event.source] += 1
            by_runtime[event.runtime] += 1
            by_month[event.product_month] += 1
            tokens["input"] += event.tokens.input
            tokens["output"] += event.tokens.output
            tokens["cache_read"] += event.tokens.cache_read
            tokens["cache_write"] += event.tokens.cache_write
            tokens["reasoning"] += event.tokens.reasoning or 0
            partial += bool(event.missing_fields or event.quality_flags)
        return {
            "schema_version": 1,
            "event_count": len(self.events),
            "partial_event_count": partial,
            "by_adapter": dict(sorted(by_adapter.items())),
            "by_source": dict(sorted(by_source.items())),
            "by_runtime": dict(sorted(by_runtime.items())),
            "by_product_month": dict(sorted(by_month.items())),
            "tokens": dict(sorted(tokens.items())),
            "diagnostics": self.diagnostics.to_dict(),
        }


@dataclass
class _RelayRecord:
    record_id: str
    partition: str
    position: int
    occurred: datetime
    tenant: str
    session_id: str
    model: str
    raw_input: int
    raw_output: int
    raw_cache_read: int
    raw_cache_write: int
    lineage: Lineage
    lineage_quality_flags: tuple[str, ...]
    consumed_by: str | None = None

    @property
    def upstream_id(self) -> str:
        """Stable identity for the upstream fact, independent of file placement."""

        material = json.dumps(
            [
                utc_text(self.occurred),
                self.tenant,
                self.session_id,
                self.model,
                self.raw_input,
                self.raw_output,
                self.raw_cache_read,
                self.raw_cache_write,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def agent(self) -> str:
        parts = self.tenant.split(":")
        return parts[1].strip() if len(parts) >= 2 and parts[1].strip() else UNKNOWN

    @property
    def consumer_path(self) -> str:
        parts = self.tenant.split(":")
        scope = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else "default"
        agent = self.agent
        return f"relay/{agent}/{_safe_segment(scope)}" if agent != UNKNOWN else "relay/unknown"


@dataclass(frozen=True)
class _CcRecord:
    upstream_id: str
    position: int
    occurred: datetime
    session_id: str
    lineage: Lineage
    cwd: str
    effort: str
    model: str
    raw_input: int
    output: int
    cache_read: int
    cache_write: int


@dataclass(frozen=True)
class _CodexRollout:
    path: Path
    session_id: str
    parent_session_id: str | None
    parent_evidence_valid: bool
    stream_after_meta: tuple[tuple[int, bytes], ...]
    stream_from_meta: tuple[tuple[int, bytes], ...]


def _safe_segment(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")
    return normalized[:80] or UNKNOWN


def _stable_event_id(adapter: str, machine: str, upstream_id: str) -> str:
    digest = hashlib.sha256(
        f"usage_event.v1\0{adapter}\0{machine}\0{upstream_id}".encode("utf-8")
    ).hexdigest()
    return f"uev1_{adapter}_{digest}"


def _lineage_run_ref(raw_run_id: str) -> str:
    digest = hashlib.sha256(
        f"pulse.compute_cost.lineage.v1\0codex_run\0{raw_run_id}".encode("utf-8")
    ).hexdigest()
    return f"lineage_v1_{digest}"


def _count(value: object, field_name: str, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterError(f"{field_name} must be a non-negative integer")
    return value


def _paths(
    root: str | Path | Sequence[str | Path] | None,
    *,
    recursive: bool,
) -> list[Path]:
    if root is None:
        return []
    roots = [root] if isinstance(root, (str, Path)) else list(root)
    result: set[Path] = set()
    for item in roots:
        path = Path(item)
        # A source path is evidence, not a convenience traversal: reject links
        # before any parser can open an alias outside the registered tree.
        if path.is_symlink():
            raise AdapterError("source symlink is not accepted")
        if not path.is_dir():
            continue
        entries = path.rglob("*") if recursive else path.glob("*")
        for candidate in entries:
            if candidate.is_symlink():
                raise AdapterError("source symlink is not accepted")
            if candidate.suffix == ".jsonl" and candidate.is_file():
                result.add(candidate)
    return sorted(result)


def _modified_since(path: Path, since: datetime | None) -> bool:
    if since is None:
        return True
    try:
        return path.stat().st_mtime >= since.timestamp()
    except OSError:
        return True


def _jsonl(
    path: Path,
    adapter: str,
    diagnostics: CollectionDiagnostics,
    *,
    needles: tuple[str, ...] | None = None,
) -> Iterable[tuple[int, Mapping[str, Any]]]:
    diagnostics.source_files[adapter] += 1
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AdapterError(f"cannot read {adapter} source") from exc
    try:
        with handle:
            for position, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                diagnostics.source_rows[adapter] += 1
                if needles is not None and not any(needle in raw_line for needle in needles):
                    continue
                try:
                    value = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    diagnostics.malformed_rows[adapter] += 1
                    continue
                if not isinstance(value, Mapping):
                    diagnostics.malformed_rows[adapter] += 1
                    continue
                yield position, value
    except OSError as exc:
        raise AdapterError(f"cannot stream {adapter} source") from exc


def load_model_routes(path: str | Path | None) -> dict[str, ModelRoute]:
    """Load explicit model→provider→cost-source evidence from Relay's catalog."""

    if path is None:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise AdapterError("model catalog is unreadable or invalid") from exc
    if not isinstance(value, Mapping) or not isinstance(value.get("models"), list):
        raise AdapterError("model catalog must contain a models list")
    routes: dict[str, ModelRoute] = {}
    source_by_provider = {
        "openai-chatgpt": "chatgpt_pro",
        "openrouter": "openrouter_api",
    }
    providers = value.get("providers")
    provider_runtime: dict[str, str] = {}
    if isinstance(providers, Mapping):
        for provider_id, provider_row in providers.items():
            if not isinstance(provider_id, str) or not isinstance(provider_row, Mapping):
                continue
            protocol = provider_row.get("protocol")
            if protocol == "openai":
                provider_runtime[provider_id] = "codex"
            elif protocol == "anthropic":
                provider_runtime[provider_id] = "cc"
    for row in value["models"]:
        if not isinstance(row, Mapping):
            continue
        model = row.get("id")
        provider = row.get("provider")
        if not isinstance(model, str) or not model.strip():
            continue
        provider_text = provider.strip() if isinstance(provider, str) and provider.strip() else UNKNOWN
        routes[model.strip()] = ModelRoute(
            provider=provider_text,
            source=source_by_provider.get(provider_text, UNKNOWN),
            runtime=provider_runtime.get(provider_text, UNKNOWN),
        )
    return routes


def _route(model: str, routes: Mapping[str, ModelRoute]) -> ModelRoute:
    return routes.get(
        model,
        ModelRoute(provider=UNKNOWN, source=UNKNOWN, runtime=UNKNOWN),
    )


def _observed_after(observed_at: str, occurred: datetime) -> str:
    observed = parse_timestamp(observed_at, "observed_at")
    return utc_text(max(observed, occurred))


def _read_relay(
    root: str | Path | None,
    *,
    machine: str,
    diagnostics: CollectionDiagnostics,
    since: datetime | None,
) -> list[_RelayRecord]:
    records: list[_RelayRecord] = []
    for path in _paths(root, recursive=False):
        for position, row in _jsonl(path, "relay_metering", diagnostics):
            try:
                occurred = parse_timestamp(row.get("ts"), "relay.ts")
                if since is not None and occurred < since:
                    continue
                tenant = row.get("tenant")
                session_id = row.get("session_id")
                if not isinstance(tenant, str) or not tenant:
                    raise AdapterError("relay.tenant is required")
                if not isinstance(session_id, str) or not session_id:
                    raise AdapterError("relay.session_id is required")
                default_count = None if row.get("schema") == "relay_usage_observation/v2" else 0
                raw_input = _count(row.get("input_tokens"), "relay.input_tokens", default=default_count)
                raw_output = _count(row.get("output_tokens"), "relay.output_tokens", default=default_count)
                cache_read = _count(row.get("cache_read_input_tokens"), "relay.cache_read", default=default_count)
                cache_write = _count(row.get("cache_creation_input_tokens"), "relay.cache_write", default=default_count)
                if row.get("schema") == "relay_usage_observation/v2" and row.get("input_semantics") == "inclusive":
                    raw_input -= cache_read + cache_write
                    if raw_input < 0:
                        raise AdapterError("relay cache classes exceed inclusive input")
                model = row.get("model") if isinstance(row.get("model"), str) and row.get("model") else UNKNOWN
            except (AdapterError, UsageEventError):
                diagnostics.malformed_rows["relay_metering"] += 1
                continue
            lineage = Lineage()
            lineage_quality_flags: tuple[str, ...] = ()
            if row.get("lineage") is not None:
                try:
                    lineage = Lineage.from_dict(row.get("lineage"))
                except UsageEventError:
                    diagnostics.malformed_rows["relay_lineage"] += 1
                    lineage_quality_flags = ("relay_lineage_invalid",)
            records.append(_RelayRecord(
                record_id=f"{path.name}:{position}",
                partition=path.name,
                position=position,
                occurred=occurred,
                tenant=tenant,
                session_id=session_id,
                model=model,
                raw_input=raw_input,
                raw_output=raw_output,
                raw_cache_read=cache_read,
                raw_cache_write=cache_write,
                lineage=lineage,
                lineage_quality_flags=lineage_quality_flags,
            ))
    return records


def _deduplicate_relay_records(
    records: Iterable[_RelayRecord],
    diagnostics: CollectionDiagnostics,
) -> list[_RelayRecord]:
    """Fold recovery/rollover copies before reconciliation or cumulative deltas."""

    unique: dict[str, _RelayRecord] = {}
    ordered = sorted(records, key=lambda item: (item.occurred, item.record_id))
    for record in ordered:
        if record.upstream_id in unique:
            diagnostics.deduplicated_events["relay_metering"] += 1
            continue
        unique[record.upstream_id] = record
    return list(unique.values())


def _infer_agent(path_text: object) -> str:
    if not isinstance(path_text, str) or not path_text:
        return UNKNOWN
    lowered = path_text.lower()
    mapping = (
        ("duke-space", "duke"), ("pulse-space", "pulse"),
        ("bigbird/pulse", "pulse"), ("aki-space", "aki"),
        ("hestia-space", "hestia"), ("amanda-space", "amanda"),
        ("ken-space", "ken"), ("emi-space", "emi"),
        ("asobi-space", "asobi"), ("reeve-space", "reeve"),
        ("hume-space", "hume"), ("vigil-space", "vigil"),
        ("steward-space", "steward"), ("marsh-space", "marsh"),
    )
    for needle, agent in mapping:
        if needle in lowered:
            return agent
    return UNKNOWN


def _nearest_relay(
    records: Sequence[_RelayRecord],
    *,
    occurred: datetime,
    predicate: Any,
    tolerance: timedelta = timedelta(seconds=30),
) -> _RelayRecord | None:
    candidates = [
        record for record in records
        if record.consumed_by is None
        and abs(record.occurred - occurred) <= tolerance
        and predicate(record)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda record: (abs(record.occurred - occurred), record.record_id))


def _merge_lineage(primary: Lineage, enrichment: Lineage) -> tuple[Lineage, tuple[str, ...]]:
    fields = (
        "root_agent", "execution_mode", "session_role", "root_run_ref",
        "parent_run_ref", "cron_job_ref", "scan_ref",
    )
    values: dict[str, str] = {}
    conflict = False
    for field_name in fields:
        left = getattr(primary, field_name)
        right = getattr(enrichment, field_name)
        if left == UNKNOWN:
            values[field_name] = right
        elif right == UNKNOWN or left == right:
            values[field_name] = left
        else:
            values[field_name] = UNKNOWN
            conflict = True
    return Lineage(**values), (("lineage_conflict",) if conflict else ())


def _cc_events(
    root: str | Path | None,
    *,
    machine: str,
    observed_at: str,
    routes: Mapping[str, ModelRoute],
    relay_records: list[_RelayRecord],
    diagnostics: CollectionDiagnostics,
    since: datetime | None,
) -> list[UsageEvent]:
    latest: dict[str, _CcRecord] = {}
    for path in _paths(root, recursive=True):
        if not _modified_since(path, since):
            continue
        for position, row in _jsonl(
            path,
            "cc_transcript",
            diagnostics,
            needles=('"usage"',),
        ):
            message = row.get("message")
            if not isinstance(message, Mapping) or not isinstance(message.get("usage"), Mapping):
                continue
            upstream = message.get("id") or row.get("uuid")
            if not isinstance(upstream, str) or not upstream:
                diagnostics.malformed_rows["cc_transcript"] += 1
                continue
            usage = message["usage"]
            try:
                occurred = parse_timestamp(row.get("timestamp"), "cc.timestamp")
                if since is not None and occurred < since:
                    continue
                latest[upstream] = _CcRecord(
                    upstream_id=upstream,
                    position=position,
                    occurred=occurred,
                    session_id=(
                        row["sessionId"]
                        if isinstance(row.get("sessionId"), str) else ""
                    ),
                    lineage=cc_transcript_lineage(path, row),
                    cwd=row["cwd"] if isinstance(row.get("cwd"), str) else "",
                    effort=(
                        row["effort"]
                        if isinstance(row.get("effort"), str) and row.get("effort")
                        else UNKNOWN
                    ),
                    model=(
                        message["model"]
                        if isinstance(message.get("model"), str) and message.get("model")
                        else UNKNOWN
                    ),
                    raw_input=_count(usage.get("input_tokens"), "cc.input_tokens", default=0),
                    output=_count(usage.get("output_tokens"), "cc.output_tokens", default=0),
                    cache_read=_count(usage.get("cache_read_input_tokens"), "cc.cache_read", default=0),
                    cache_write=_count(usage.get("cache_creation_input_tokens"), "cc.cache_write", default=0),
                )
            except (AdapterError, UsageEventError):
                diagnostics.malformed_rows["cc_transcript"] += 1
                continue

    events: list[UsageEvent] = []
    for upstream, record in sorted(latest.items()):
        occurred = record.occurred
        raw_input = record.raw_input
        output = record.output
        cache_read = record.cache_read
        cache_write = record.cache_write
        model = record.model
        session_id = record.session_id

        relay = _nearest_relay(
            relay_records,
            occurred=occurred,
            predicate=lambda record: (
                bool(session_id) and record.session_id == session_id
                and record.raw_input == raw_input
                and record.raw_output == output
                and record.raw_cache_read == cache_read
                and record.raw_cache_write == cache_write
            ),
        )
        if relay is not None:
            relay.consumed_by = "cc_transcript"
            diagnostics.reconciled_relay_rows["cc_transcript"] += 1
            agent = relay.agent
            consumer_path = relay.consumer_path
            route = _route(relay.model if relay.model != UNKNOWN else model, routes)
            event_lineage, conflict_flags = _merge_lineage(record.lineage, relay.lineage)
            quality_flags = relay.lineage_quality_flags + conflict_flags
        else:
            agent = _infer_agent(record.cwd)
            consumer_path = "direct/cc"
            route = _route(model, routes)
            event_lineage = record.lineage
            quality_flags = ("relay_attribution_unavailable",)

        # Anthropic usage reports disjoint input classes.
        tokens = TokenUsage(
            input=InputTokenClasses.from_disjoint(raw_input, cache_read, cache_write).require_total(),
            output=output,
            cache_read=cache_read,
            cache_write=cache_write,
            reasoning=None,
        )
        event = UsageEvent(
            event_id=_stable_event_id("cc", machine, upstream),
            occurred_at=utc_text(occurred),
            observed_at=_observed_after(observed_at, occurred),
            machine=machine,
            agent=agent,
            consumer_path=consumer_path,
            source=route.source,
            runtime=route.runtime,
            provider=route.provider,
            model=model,
            effort=record.effort,
            tokens=tokens,
            adapter="cc_transcript",
            source_partition=occurred.date().isoformat(),
            source_position=record.position,
            quality_flags=quality_flags,
            lineage=event_lineage,
        )
        events.append(event)
        diagnostics.emitted_events["cc_transcript"] += 1
    return events


def _usage_counter(value: object) -> dict[str, int] | None:
    """Legacy integer projection; callers retain the raw present-field set."""
    if not isinstance(value, Mapping):
        return None
    keys = (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens",
    )
    try:
        # Validate only actual native fields before making the old zero-filled
        # projection used by UsageEvent.
        for key in keys:
            if key in value and value[key] is not None:
                _count(value[key], f"codex.{key}")
        return {key: _count(value.get(key), f"codex.{key}", default=0) for key in keys}
    except AdapterError:
        return None


def _rollout_id_matches_path(path: Path, session_id: str) -> bool:
    stem = path.stem
    return stem == session_id or stem.endswith(f"-{session_id}")


def _parent_evidence(payload: Mapping[str, Any]) -> tuple[str | None, bool]:
    direct = payload.get("parent_thread_id")
    source = payload.get("source")
    nested: object = None
    if isinstance(source, Mapping):
        subagent = source.get("subagent")
        if isinstance(subagent, Mapping):
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, Mapping):
                nested = spawn.get("parent_thread_id")
    direct_present = isinstance(direct, str) and bool(direct)
    nested_present = isinstance(nested, str) and bool(nested)
    if not direct_present and not nested_present:
        return None, True
    if direct_present and nested_present:
        if direct != nested:
            return None, False
        return str(direct), True
    if direct_present:
        return str(direct), True
    return str(nested), True


def _index_codex_rollout(path: Path) -> _CodexRollout | None:
    lines: list[tuple[int, bytes]] = []
    local_meta: tuple[int, str, str | None, bool] | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for position, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                encoded = raw_line.rstrip("\r\n").encode("utf-8")
                lines.append((position, hashlib.sha256(encoded).digest()))
                if '"session_meta"' not in raw_line:
                    continue
                try:
                    row = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    continue
                payload = row.get("payload") if isinstance(row, Mapping) else None
                if row.get("type") != "session_meta" or not isinstance(payload, Mapping):
                    continue
                raw_id = payload.get("id") or payload.get("session_id")
                if (
                    isinstance(raw_id, str)
                    and raw_id
                    and _rollout_id_matches_path(path, raw_id)
                ):
                    parent_id, parent_valid = _parent_evidence(payload)
                    if local_meta is not None and (local_meta[1] != raw_id or local_meta[2] != parent_id or not local_meta[3]):
                        parent_valid = False
                    local_meta = (position, raw_id, parent_id, parent_valid)
    except OSError as exc:
        raise AdapterError("cannot read codex_rollout source") from exc
    if local_meta is None:
        return None
    meta_position, session_id, parent_id, parent_valid = local_meta
    return _CodexRollout(
        path=path,
        session_id=session_id,
        parent_session_id=parent_id,
        parent_evidence_valid=parent_valid,
        stream_after_meta=tuple(item for item in lines if item[0] > meta_position),
        stream_from_meta=tuple(item for item in lines if item[0] >= meta_position),
    )


def _codex_rollout_index(
    paths: Sequence[Path],
    seeds: Iterable[Path],
) -> dict[str, _CodexRollout]:
    rollouts: list[_CodexRollout] = []
    pending = list(seeds)
    scanned: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in scanned:
            continue
        scanned.add(path)
        rollout = _index_codex_rollout(path)
        if rollout is None:
            continue
        rollouts.append(rollout)
        relevant_ids = {rollout.session_id}
        if rollout.parent_session_id is not None:
            relevant_ids.add(rollout.parent_session_id)
        pending.extend(
            candidate
            for candidate in paths
            if candidate not in scanned
            and any(
                _rollout_id_matches_path(candidate, session_id)
                for session_id in relevant_ids
            )
        )

    by_session: dict[str, list[_CodexRollout]] = defaultdict(list)
    for rollout in rollouts:
        by_session[rollout.session_id].append(rollout)
    indexed: dict[str, _CodexRollout] = {}
    for session_id, versions in by_session.items():
        versions.sort(key=lambda item: (len(item.stream_from_meta), str(item.path)))
        selected = versions[-1]
        selected_hashes = tuple(item[1] for item in selected.stream_from_meta)
        compatible = all(
            version.parent_session_id == selected.parent_session_id
            and version.parent_evidence_valid == selected.parent_evidence_valid
            and selected_hashes[: len(version.stream_from_meta)]
            == tuple(item[1] for item in version.stream_from_meta)
            for version in versions
        )
        if compatible:
            indexed[session_id] = selected
    return indexed


def _root_codex_session(
    rollout: _CodexRollout,
    indexed: Mapping[str, _CodexRollout],
) -> str | None:
    seen: set[str] = set()
    current = rollout
    while True:
        if current.session_id in seen or not current.parent_evidence_valid:
            return None
        seen.add(current.session_id)
        if current.parent_session_id is None:
            return current.session_id
        parent = indexed.get(current.parent_session_id)
        if parent is None:
            return None
        current = parent


def _codex_lineage(
    rollout: _CodexRollout | None,
    indexed: Mapping[str, _CodexRollout],
) -> Lineage:
    if rollout is None or not rollout.parent_evidence_valid:
        return Lineage()
    if rollout.parent_session_id is None:
        return Lineage(
            session_role="main",
            root_run_ref=_lineage_run_ref(rollout.session_id),
        )
    root_session = _root_codex_session(rollout, indexed)
    return Lineage(
        session_role="sub_agent",
        root_run_ref=(
            _lineage_run_ref(root_session) if root_session is not None else UNKNOWN
        ),
        parent_run_ref=_lineage_run_ref(rollout.parent_session_id),
    )


def _copied_parent_positions(
    rollout: _CodexRollout | None,
    indexed: Mapping[str, _CodexRollout],
) -> frozenset[int]:
    if (
        rollout is None
        or not rollout.parent_evidence_valid
        or rollout.parent_session_id is None
    ):
        return frozenset()
    parent = indexed.get(rollout.parent_session_id)
    if parent is None:
        return frozenset()
    matched = 0
    for child_item, parent_item in zip(
        rollout.stream_after_meta,
        parent.stream_from_meta,
    ):
        if child_item[1] != parent_item[1]:
            break
        matched += 1
    return frozenset(
        position for position, _ in rollout.stream_after_meta[:matched]
    )


def _codex_events(
    root: str | Path | Sequence[str | Path] | None,
    *,
    machine: str,
    observed_at: str,
    routes: Mapping[str, ModelRoute],
    relay_records: list[_RelayRecord],
    diagnostics: CollectionDiagnostics,
    since: datetime | None,
    required_session_ids: frozenset[str] = frozenset(),
    native_observations: dict[str, CodexNativeObservation] | None = None,
) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    paths = _paths(root, recursive=True)
    active_paths = [
        path for path in paths
        if _modified_since(path, since)
        or any(_rollout_id_matches_path(path, session_id) for session_id in required_session_ids)
    ]
    indexed = _codex_rollout_index(paths, active_paths)
    indexed_by_path = {rollout.path: rollout for rollout in indexed.values()}
    for path in active_paths:
        rollout = indexed_by_path.get(path)
        candidate_rollout = rollout or _index_codex_rollout(path)
        # Exact active/archive copies are represented by the one canonical
        # indexed rollout. Conflicting duplicate session producers remain
        # excluded rather than being reparsed as unrelated path-stem sessions.
        if rollout is None and candidate_rollout is not None:
            continue
        lineage = _codex_lineage(rollout, indexed)
        copied_positions = _copied_parent_positions(rollout, indexed)
        session_id = rollout.session_id if rollout is not None else path.stem
        previous_total = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }
        # session_meta establishes the required cumulative input baseline at
        # zero for every cumulative class. This establishes an origin, not a
        # fabricated observation: a field missing from the CURRENT counter is
        # still unknown. Copied fork history replaces this origin below.
        previous_fields: frozenset[str] = frozenset(previous_total)
        task_ordinal = 0
        current_turn: str | None = None
        turn_started_at: datetime | None = None
        previous_total_at: datetime | None = None
        previous_complete = True
        current_model = UNKNOWN
        turn_models: set[str] = set()
        current_effort = UNKNOWN
        current_cwd: object = None
        startup_cwd = originator = native_source = startup_ref = None
        startup_identity = None
        startup_conflict = False
        final_total: dict[str, int] | None = None
        final_total_at: datetime | None = None
        final_position: int | None = None
        final_fields: frozenset[str] = frozenset()
        counter_scope_unknown = False
        counter_origin_restarted = False

        def emit_final(*, incomplete: bool) -> None:
            nonlocal previous_total, previous_fields, current_turn, final_total, final_total_at, final_position, final_fields
            nonlocal previous_total_at, previous_complete
            if current_turn is None or final_total is None or final_total_at is None or final_position is None:
                # An interrupted task without a counter still leaves an
                # unbounded cumulative interval. The next counter must retain
                # the prior observed boundary (or unknown), not its own start.
                if incomplete:
                    previous_complete = False
                current_turn = None
                final_total = None
                final_total_at = None
                final_position = None
                final_fields = frozenset()
                return
            if since is not None and final_total_at < since:
                previous_total = final_total
                previous_fields = final_fields
                previous_total_at, previous_complete = final_total_at, not incomplete
                current_turn = None
                final_total = None
                final_total_at = None
                final_position = None
                final_fields = frozenset()
                return

            # Never let the legacy zero-filled projection erase a raw field.
            # A decrease in a field actually observed on both sides is a
            # contradiction; a field absent on either side is simply unknown.
            delta = {key: (final_total[key] - previous_total[key]
                           if key in final_fields and key in previous_fields else None)
                     for key in final_total}
            if any(value is not None and value < 0 for value in delta.values()):
                diagnostics.malformed_rows["codex_rollout"] += 1
                previous_total = final_total
                previous_fields = final_fields
                previous_total_at, previous_complete = final_total_at, not incomplete
                current_turn = None
                final_total = None
                final_total_at = None
                final_position = None
                final_fields = frozenset()
                return
            # Keep the legacy event projection usable for existing consumers:
            # its implicit-zero classes may contribute normally, but an absent
            # field may never create a negative event or reject this raw native
            # observation. Receipt materialization uses `delta` above instead.
            projected_delta = {key: max(0, final_total[key] - previous_total[key])
                               for key in final_total}
            event_model = current_model if len(turn_models) <= 1 else UNKNOWN
            route = _route(event_model, routes)
            relay = _nearest_relay(
                relay_records,
                occurred=final_total_at,
                predicate=lambda record: (
                    record.model == event_model
                    and record.raw_input == final_total["input_tokens"]
                    and record.raw_output == final_total["output_tokens"]
                ),
            )
            if relay is not None:
                relay.consumed_by = "codex_rollout"
                diagnostics.reconciled_relay_rows["codex_rollout"] += 1
                agent = relay.agent
                consumer_path = relay.consumer_path
                event_lineage, conflict_flags = _merge_lineage(lineage, relay.lineage)
                quality_flags = relay.lineage_quality_flags + conflict_flags
            else:
                agent = _infer_agent(current_cwd or str(path.parent))
                consumer_path = "direct/codex"
                event_lineage = lineage
                quality_flags = ("relay_attribution_unavailable",)
            if len(turn_models) > 1:
                quality_flags += ("model_scope_unknown",)
            if incomplete:
                quality_flags += ("incomplete_task",)
            if counter_scope_unknown:
                quality_flags += ("counter_scope_unknown",)
            if counter_origin_restarted:
                quality_flags += ("counter_origin_restarted_from_native_last",)
            event = UsageEvent(
                event_id=_stable_event_id("codex", machine, f"{session_id}:{current_turn}"),
                occurred_at=utc_text(final_total_at),
                observed_at=_observed_after(observed_at, final_total_at),
                machine=machine,
                agent=agent,
                consumer_path=consumer_path,
                source=route.source,
                runtime=route.runtime,
                provider=route.provider,
                model=event_model,
                effort=current_effort,
                tokens=TokenUsage(
                    input=projected_delta["input_tokens"],
                    output=projected_delta["output_tokens"],
                    cache_read=projected_delta["cached_input_tokens"],
                    cache_write=projected_delta["cache_write_input_tokens"],
                    reasoning=projected_delta["reasoning_output_tokens"],
                ),
                lineage=event_lineage,
                adapter="codex_rollout",
                source_partition=final_total_at.date().isoformat(),
                source_position=final_position,
                quality_flags=quality_flags,
            )
            events.append(event)
            if native_observations is not None:
                # A normal completed predecessor seals the counter at a turn
                # boundary. After an interrupted predecessor, include the wider
                # measured counter interval rather than assigning its tail to
                # the next dispatch. Invalid/missing boundaries remain unknown.
                start = turn_started_at if previous_complete or counter_origin_restarted else previous_total_at
                interval_known = (start is not None and start <= final_total_at
                                  and not counter_scope_unknown)
                native_observations[event.event_id] = CodexNativeObservation(
                    session_id=session_id, source_path=path, source_position=final_position,
                    counter=dict(final_total), counter_fields=final_fields,
                    previous_counter=dict(previous_total), previous_fields=previous_fields,
                    interval_start=utc_text(start) if interval_known else None,
                    interval_end=utc_text(final_total_at) if interval_known else None,
                    startup_cwd=startup_cwd if not startup_conflict else None,
                    originator=originator if not startup_conflict else None,
                    native_source=native_source if not startup_conflict else None,
                    startup_ref=startup_ref if not startup_conflict else None,
                    parent_session_id=(rollout.parent_session_id if rollout is not None
                                       and rollout.parent_evidence_valid and not startup_conflict else None),
                )
            diagnostics.emitted_events["codex_rollout"] += 1
            previous_total = final_total
            previous_fields = final_fields
            previous_total_at, previous_complete = final_total_at, not incomplete
            current_turn = None
            final_total = None
            final_total_at = None
            final_position = None
            final_fields = frozenset()

        for position, row in _jsonl(
            path,
            "codex_rollout",
            diagnostics,
            needles=(
                '"session_meta"',
                '"turn_context"',
                '"task_started"',
                '"token_count"',
                '"task_complete"',
            ),
        ):
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                continue
            if row.get("type") == "session_meta":
                raw_id = payload.get("id") or payload.get("session_id")
                if rollout is None and isinstance(raw_id, str) and raw_id:
                    session_id = raw_id
                if rollout is not None and raw_id == rollout.session_id:
                    identity = tuple(payload.get(k) if isinstance(payload.get(k), str) else None
                                     for k in ("cwd", "originator", "source"))
                    if startup_identity is not None and startup_identity != identity:
                        startup_conflict = True
                    startup_identity = identity
                    startup_cwd, originator, native_source = identity
                    from .receipts import reference
                    startup_ref = reference("codex_startup", [session_id, *identity])
                continue
            if payload.get("type") == "task_started":
                if current_turn is not None:
                    emit_final(incomplete=True)
                task_ordinal += 1
                raw_turn = payload.get("turn_id")
                current_turn = raw_turn if isinstance(raw_turn, str) and raw_turn else f"ordinal-{task_ordinal}"
                try:
                    turn_started_at = parse_timestamp(row.get("timestamp"), "codex.task_started")
                except UsageEventError:
                    turn_started_at = None
                current_model = UNKNOWN
                turn_models.clear()
                current_effort = UNKNOWN
                current_cwd = None
                final_total = None
                final_total_at = None
                final_position = None
                final_fields = frozenset()
                counter_scope_unknown = False
                counter_origin_restarted = False
                continue
            if row.get("type") == "turn_context" and current_turn is not None:
                if isinstance(payload.get("model"), str) and payload.get("model"):
                    current_model = str(payload["model"])
                    turn_models.add(current_model)
                if isinstance(payload.get("effort"), str) and payload.get("effort"):
                    current_effort = str(payload["effort"])
                current_cwd = payload.get("cwd")
                continue
            if payload.get("type") == "token_count" and current_turn is not None:
                info = payload.get("info")
                raw_counter = info.get("total_token_usage") if isinstance(info, Mapping) else None
                counter = _usage_counter(raw_counter)
                if counter is not None:
                    final_fields = frozenset(key for key in counter if isinstance(raw_counter, Mapping) and raw_counter.get(key) is not None)
                    if position not in copied_positions:
                        baseline = final_total if final_total is not None else previous_total
                        baseline_fields = (frozenset(baseline) if final_total is not None else previous_fields)
                        decreasing = any(counter[key] < baseline[key] for key in final_fields & baseline_fields)
                        empty_predecessor = (not previous_complete and previous_total_at is None
                            and final_total is None and not any(previous_total.values()))
                        if decreasing or empty_predecessor:
                            last = info.get("last_token_usage") if isinstance(info, Mapping) else None
                            # Only a first update with native total == native last
                            # establishes a fresh counter origin. A drop alone
                            # never authorizes a guessed zero baseline.
                            proven = (final_total is None and isinstance(last, Mapping)
                                and {"input_tokens", "output_tokens"} <= final_fields
                                and all(isinstance(last.get(key), int) and not isinstance(last.get(key), bool)
                                    and last[key] == raw_counter[key] for key in final_fields))
                            if proven:
                                previous_total = dict.fromkeys(previous_total, 0)
                                previous_fields = final_fields
                                counter_origin_restarted = True
                            elif decreasing:
                                previous_fields = frozenset()
                                counter_scope_unknown = True
                    if position in copied_positions:
                        previous_total = counter
                        previous_fields = final_fields
                        try:
                            previous_total_at = parse_timestamp(row.get("timestamp"), "codex.timestamp")
                        except UsageEventError:
                            previous_total_at = None
                        final_total = None
                        final_total_at = None
                        final_position = None
                        final_fields = frozenset()
                        continue
                    try:
                        final_total_at = parse_timestamp(row.get("timestamp"), "codex.timestamp")
                    except UsageEventError:
                        diagnostics.malformed_rows["codex_rollout"] += 1
                        continue
                    final_total = counter
                    final_position = position
                continue
            if payload.get("type") != "task_complete" or current_turn is None:
                continue
            if position in copied_positions:
                current_turn = None
                final_total = None
                final_total_at = None
                final_position = None
                final_fields = frozenset()
                continue
            emit_final(incomplete=False)
        if current_turn is not None:
            emit_final(incomplete=True)
    return events


def _relay_fallback_events(
    records: Iterable[_RelayRecord],
    *,
    machine: str,
    observed_at: str,
    routes: Mapping[str, ModelRoute],
    diagnostics: CollectionDiagnostics,
) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    previous_cumulative: dict[tuple[str, str], tuple[int, int, int, int]] = {}
    unique_records = _deduplicate_relay_records(records, diagnostics)
    ordered = sorted(
        unique_records,
        key=lambda item: (item.session_id, item.occurred, item.upstream_id),
    )
    for record in ordered:
        route = _route(record.model, routes)
        raw_input = record.raw_input
        raw_output = record.raw_output
        raw_cache_read = record.raw_cache_read
        raw_cache_write = record.raw_cache_write
        fallback_flags = ["relay_fallback_usage_semantics"]
        if route.runtime == "codex":
            counter_key = (record.session_id, record.model)
            previous = previous_cumulative.get(counter_key, (0, 0, 0, 0))
            current = (raw_input, raw_output, raw_cache_read, raw_cache_write)
            deltas: list[int] = []
            counter_reset = False
            for value, prior in zip(current, previous):
                if value >= prior:
                    deltas.append(value - prior)
                else:
                    deltas.append(value)
                    counter_reset = True
            if counter_reset:
                fallback_flags.append("relay_cumulative_counter_reset")
            raw_input, raw_output, raw_cache_read, raw_cache_write = deltas
            fallback_flags.append("relay_cumulative_delta")
        if record.consumed_by is not None:
            # Native events already account for this interval. Even a malformed
            # relay cache split must advance the cumulative deduplication boundary.
            if route.runtime == "codex":
                previous_cumulative[counter_key] = current
            continue
        try:
            inputs = (InputTokenClasses.from_inclusive(raw_input, raw_cache_read, raw_cache_write)
                      if route.runtime == "codex" else
                      InputTokenClasses.from_disjoint(raw_input, raw_cache_read, raw_cache_write))
            normalized_input = inputs.require_total()
        except TokenSemanticsError:
            diagnostics.malformed_rows["relay_metering"] += 1
            continue
        if route.runtime == "codex":
            previous_cumulative[counter_key] = current
        event = UsageEvent(
            event_id=_stable_event_id("relay", machine, record.upstream_id),
            occurred_at=utc_text(record.occurred),
            observed_at=_observed_after(observed_at, record.occurred),
            machine=machine,
            agent=record.agent,
            consumer_path=record.consumer_path,
            source=route.source,
            runtime=route.runtime,
            provider=route.provider,
            model=record.model,
            effort=UNKNOWN,
            # Relay's CC rows use disjoint input/cache counters. Relay's Codex
            # input already includes cached input, and all four counters are
            # cumulative, so the Codex branch deltas each counter independently
            # and must not add cache to canonical input a second time.
            tokens=TokenUsage(
                input=normalized_input,
                output=raw_output,
                cache_read=raw_cache_read,
                cache_write=raw_cache_write,
                reasoning=None,
            ),
            adapter="relay_metering",
            source_partition=record.occurred.date().isoformat(),
            source_position=record.position,
            quality_flags=tuple(
                (["runtime_unresolved"] if route.runtime == UNKNOWN else [])
                + list(record.lineage_quality_flags)
                + fallback_flags
            ),
            lineage=record.lineage,
        )
        events.append(event)
        diagnostics.emitted_events["relay_metering"] += 1
        diagnostics.unmatched_relay_rows += 1
    return events


def collect_usage_events(
    *,
    machine: str,
    observed_at: str,
    relay_metering_dir: str | Path | None = None,
    cc_projects_dir: str | Path | None = None,
    codex_sessions_dir: str | Path | Sequence[str | Path] | None = None,
    model_catalog_path: str | Path | None = None,
    since: str | None = None,
    codex_required_session_ids: frozenset[str] = frozenset(),
    _codex_native_observations: dict[str, CodexNativeObservation] | None = None,
) -> UsageCollection:
    """Collect once, reconcile overlapping evidence, and emit canonical events."""

    if not isinstance(machine, str) or not machine.strip():
        raise AdapterError("machine is required")
    observed = parse_timestamp(observed_at, "observed_at")
    since_dt = parse_timestamp(since, "since") if since is not None else None
    if since_dt is not None and since_dt > observed:
        raise AdapterError("since cannot be after observed_at")
    diagnostics = CollectionDiagnostics()
    routes = load_model_routes(model_catalog_path)
    relay_records = _read_relay(
        relay_metering_dir,
        machine=machine,
        diagnostics=diagnostics,
        since=since_dt,
    )
    relay_records = _deduplicate_relay_records(relay_records, diagnostics)
    observed_text = utc_text(observed)
    events = _cc_events(
        cc_projects_dir,
        machine=machine,
        observed_at=observed_text,
        routes=routes,
        relay_records=relay_records,
        diagnostics=diagnostics,
        since=since_dt,
    )
    events.extend(_codex_events(
        codex_sessions_dir,
        machine=machine,
        observed_at=observed_text,
        routes=routes,
        relay_records=relay_records,
        diagnostics=diagnostics,
        since=since_dt,
        required_session_ids=codex_required_session_ids,
        native_observations=_codex_native_observations,
    ))
    events.extend(_relay_fallback_events(
        relay_records,
        machine=machine,
        observed_at=observed_text,
        routes=routes,
        diagnostics=diagnostics,
    ))
    events.sort(key=lambda event: (event.occurred_at, event.event_id))
    seen: dict[str, UsageEvent] = {}
    for event in events:
        prior = seen.get(event.event_id)
        if prior is not None and prior.semantic_dict() != event.semantic_dict():
            prior_facts = prior.semantic_dict()
            event_facts = event.semantic_dict()
            prior_flags = set(prior.quality_flags)
            event_flags = set(event.quality_flags)
            attribution_flag = "relay_attribution_unavailable"

            same_except_time = dict(prior_facts)
            other_except_time = dict(event_facts)
            for facts in (same_except_time, other_except_time):
                facts.pop("occurred_at", None)
                facts.pop("quality", None)
            if same_except_time == other_except_time:
                diagnostics.deduplicated_events[event.adapter] += 1
                continue

            one_is_relay_enriched = (
                (attribution_flag in prior_flags) != (attribution_flag in event_flags)
                and (prior_flags ^ event_flags) == {attribution_flag}
            )
            if one_is_relay_enriched:
                for facts in (same_except_time, other_except_time):
                    facts.pop("agent", None)
                    facts.pop("consumer_path", None)
                if same_except_time == other_except_time:
                    diagnostics.deduplicated_events[event.adapter] += 1
                    if attribution_flag in prior_flags:
                        seen[event.event_id] = event
                    continue

            changed = sorted(
                key for key in set(prior_facts) | set(event_facts)
                if prior_facts.get(key) != event_facts.get(key)
            )
            raise AdapterError(
                f"conflicting canonical event id: {event.event_id}; fields={','.join(changed)}"
            )
        seen[event.event_id] = event
    deduplicated = sorted(seen.values(), key=lambda event: (event.occurred_at, event.event_id))
    return UsageCollection(events=deduplicated, diagnostics=diagnostics)
