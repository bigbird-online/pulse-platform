"""Privacy-safe retrospective Relay/TG Pi usage candidates.

This adapter is deliberately diagnostic.  It uses Relay registration/state and
Harness lifecycle timestamps to form two historical windows, but never turns a
window match into canonical WI attribution, a Token seal, cost, or ROI input.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Mapping

from .attention import CATEGORIES, EFFORT_BANDS, UNAVAILABLE
from .events import parse_timestamp
from .pi_usage import (
    PiUsageBinding,
    PiUsageError,
    collect_pi_session_usage_through,
)


RETROSPECTIVE_CANDIDATE_SCHEMA = "computecost_retrospective_usage_candidate/v1"
_WI_RE = re.compile(r"^wi_\d{8}_\d{3}$")
_SOURCE_REF_RE = re.compile(r"^source_v1_[0-9a-f]{64}$")
_TERMINAL_STATES = {"completed", "failed", "cancelled", "superseded", "migrated"}
_CANDIDATE_STATES = {
    "available", "partial", "ambiguous_mapping", "source_missing", "source_unreadable",
}
_FORBIDDEN_KEYS = {
    "tenant", "tenant_id", "tenantid", "session_id", "sessionid", "chat_id", "chatid",
    "path", "credential", "credentials", "bot_token", "bottoken", "message", "text",
    "prompt", "response", "content",
}
_TOKEN_FIELDS = ("input", "cache_read", "cache_write", "output")
_UNAVAILABLE = "unavailable"
_ATTENTION_CANDIDATE_SCHEMA = "computecost_retrospective_human_attention_candidate/v1"

_DIAGNOSTICS = {
    "retrospective_unsealed_not_canonical",
    "created_to_terminal_may_include_unrelated_turns",
    "lifecycle_active_may_omit_verification_work",
    "multiple_lifecycle_active_intervals",
    "relay_pi_mapping_ambiguous",
    "relay_pi_mapping_unavailable",
    "relay_registration_source_unreadable",
    "relay_pi_source_missing",
    "relay_pi_source_unreadable",
    "relay_metering_source_missing",
    "relay_metering_source_unreadable",
    "historical_source_permissions_privacy_defect",
    "pi_relay_metering_divergence",
    "pi_non_outer_turn_usage_excluded_from_cross_check",
    "pi_reasoning_partial",
    "relay_metering_resource_fields_unavailable",
}


class RetrospectiveUsageError(ValueError):
    """Raised when retrospective evidence cannot be represented safely."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _scalar(lines: Iterable[str], name: str) -> str | None:
    prefix = f"{name}:"
    for line in lines:
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if value in {"", "null", "~"}:
            return None
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value or None
    return None


def _source_ref(path: Path) -> str:
    """Bind source location and current bytes without disclosing either."""

    digest = sha256(b"pulse-computecost-retrospective-source-v1")
    try:
        resolved = path.expanduser().resolve(strict=True)
        digest.update(b"\0")
        digest.update(str(resolved).encode("utf-8"))
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        # A missing attempted source still gets a stable opaque reference.  The
        # candidate status, never this digest, states whether bytes were read.
        digest.update(b"\0missing\0")
        digest.update(str(path.expanduser().absolute()).encode("utf-8"))
    return f"source_v1_{digest.hexdigest()}"


def _read_json_object(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RetrospectiveUsageError(f"{label}_missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RetrospectiveUsageError(f"{label}_unreadable") from exc
    if not isinstance(value, Mapping):
        raise RetrospectiveUsageError(f"{label}_unreadable")
    return value


def _mode(path: Path) -> str | None:
    try:
        return f"{stat.S_IMODE(path.stat().st_mode):04o}"
    except OSError:
        return None


def _permissions(paths: Iterable[Path]) -> dict[str, Any]:
    rows = list(paths)
    modes = [_mode(path) for path in rows]
    observed = sorted({mode for mode in modes if mode is not None})
    unreadable = sum(mode is None for mode in modes)
    unsafe = sum(mode is not None and mode != "0600" for mode in modes)
    return {
        "file_count": len(rows),
        "observed_modes": observed,
        "unsafe_file_count": unsafe,
        "unreadable_file_count": unreadable,
        "status": (
            _UNAVAILABLE if not rows
            else "privacy_defect" if unsafe
            else "unavailable" if unreadable
            else "ok"
        ),
    }


def _documents(text: str) -> list[list[str]]:
    documents: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "---":
            if current:
                documents.append(current)
                current = []
            continue
        current.append(line)
    if current:
        documents.append(current)
    return documents


def _document_scalar(lines: Iterable[str], name: str, *, indent: int) -> str | None:
    prefix = " " * indent + f"{name}:"
    for line in lines:
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if value in {"", "null", "~"}:
            return None
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value or None
    return None


def _artifact_refs(lines: list[str]) -> list[str]:
    refs: list[str] = []
    in_refs = False
    for line in lines:
        if line == "artifact_refs:":
            in_refs = True
            continue
        if not in_refs:
            continue
        if line.startswith("- "):
            value = line[2:].strip()
            if value:
                refs.append(value)
            continue
        if line and not line.startswith(" "):
            break
    return refs


def _artifact_availability(harness_root: Path, refs: list[str]) -> dict[str, Any]:
    local = 0
    authority_root = harness_root.parent.resolve()
    for ref in refs:
        if not ref or ref.startswith(("/", "~")) or ".." in Path(ref).parts:
            continue
        candidate = (authority_root / ref).resolve()
        try:
            candidate.relative_to(authority_root)
        except ValueError:
            continue
        if candidate.is_file() and not candidate.is_symlink():
            local += 1
    if refs and local == len(refs):
        status = "available"
    elif local:
        status = "partial"
    elif refs:
        status = "registered_refs_only"
    else:
        status = _UNAVAILABLE
    return {
        "status": status,
        "registered_artifact_ref_count": len(refs),
        "locally_available_artifact_count": local,
    }


@dataclass(frozen=True)
class _Lifecycle:
    status: str
    owner: str
    created_at: datetime
    terminal_at: datetime
    intervals: tuple[tuple[datetime, datetime], ...]
    artifact_availability: Mapping[str, Any]
    review_approval_count: int
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "terminal": True,
            "created_at": _utc(self.created_at),
            "terminal_at": _utc(self.terminal_at),
            "active_interval_count": len(self.intervals),
            "active_intervals": [
                {"started_at": _utc(start), "ended_at": _utc(end)}
                for start, end in self.intervals
            ],
            "evidence_availability": dict(self.artifact_availability),
            "source_refs": list(self.source_refs),
        }


def _utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _timestamp(value: object, label: str) -> datetime:
    try:
        return parse_timestamp(value, label)
    except ValueError as exc:
        raise RetrospectiveUsageError(f"{label}_invalid") from exc


def _load_lifecycle(harness_root: Path, work_item_id: str) -> _Lifecycle:
    item_path = harness_root / "work-items" / f"{work_item_id}.yaml"
    event_path = harness_root / "events" / f"{work_item_id}.events.yaml"
    try:
        item_lines = item_path.read_text(encoding="utf-8").splitlines()
        event_text = event_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RetrospectiveUsageError("harness_lifecycle_source_missing") from exc
    status = _scalar(item_lines, "status")
    owner = _scalar(item_lines, "owner")
    created_text = _scalar(item_lines, "created_at")
    if status not in _TERMINAL_STATES or owner is None or created_text is None:
        raise RetrospectiveUsageError("harness_lifecycle_incomplete")
    created = _timestamp(created_text, "work_item.created_at")

    active_start: datetime | None = None
    intervals: list[tuple[datetime, datetime]] = []
    terminal_at: datetime | None = None
    created_event_at: datetime | None = None
    prior_event_at: datetime | None = None
    review_event_ids: set[str] = set()
    for document in _documents(event_text):
        event_wi = _document_scalar(document, "work_item_id", indent=0)
        event_type = _document_scalar(document, "event_type", indent=0)
        timestamp_text = _document_scalar(document, "timestamp", indent=0)
        if event_wi != work_item_id or event_type is None or timestamp_text is None:
            raise RetrospectiveUsageError("harness_lifecycle_event_malformed")
        timestamp = _timestamp(timestamp_text, "work_item.event.timestamp")
        if prior_event_at is not None and timestamp < prior_event_at:
            raise RetrospectiveUsageError("harness_lifecycle_events_out_of_order")
        prior_event_at = timestamp
        from_status = _document_scalar(document, "from_status", indent=2)
        to_status = _document_scalar(document, "to_status", indent=2)
        if event_type == "work_item.created":
            created_event_at = timestamp
        if event_type == "work_item.review_approved":
            event_id = _document_scalar(document, "event_id", indent=0)
            operator = _document_scalar(document, "operator", indent=0)
            if event_id is None or operator is None or event_id in review_event_ids:
                raise RetrospectiveUsageError("harness_review_event_malformed")
            review_event_ids.add(event_id)
        if event_type == "work_item.in_progress" and to_status == "in_progress":
            if active_start is not None:
                raise RetrospectiveUsageError("harness_lifecycle_intervals_ambiguous")
            active_start = timestamp
        if from_status == "in_progress" and to_status not in {None, "in_progress"}:
            if active_start is None or timestamp < active_start:
                raise RetrospectiveUsageError("harness_lifecycle_intervals_ambiguous")
            intervals.append((active_start, timestamp))
            active_start = None
        if event_type == f"work_item.{status}" and to_status == status:
            terminal_at = timestamp
    if created_event_at != created or terminal_at is None or active_start is not None:
        raise RetrospectiveUsageError("harness_lifecycle_incomplete")
    if terminal_at < created or any(
        start < created or end > terminal_at for start, end in intervals
    ):
        raise RetrospectiveUsageError("harness_lifecycle_intervals_ambiguous")
    refs = _artifact_refs(item_lines)
    return _Lifecycle(
        status=status,
        owner=owner,
        created_at=created,
        terminal_at=terminal_at,
        intervals=tuple(intervals),
        artifact_availability=_artifact_availability(harness_root, refs),
        review_approval_count=len(review_event_ids),
        source_refs=(_source_ref(item_path), _source_ref(event_path)),
    )


@dataclass(frozen=True)
class _RelayMapping:
    pool_key: str
    session_id: str
    session_dir: Path
    metering_dir: Path
    source_refs: tuple[str, ...]


def _default_path(explicit: str | Path | None, env_name: str, fallback: Path) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    configured = os.environ.get(env_name)
    return Path(configured).expanduser() if configured else fallback


def _effective_pi_route(config: Mapping[str, Any], agent: Mapping[str, Any]) -> tuple[str | None, str | None]:
    route = agent.get("conversationRuntime")
    defaults = config.get("conversationRuntimeDefaults")
    default_pi = defaults.get("pi") if isinstance(defaults, Mapping) else None
    route_map = route if isinstance(route, Mapping) else {}
    runtime = route_map.get("runtime") or agent.get("runtime")
    profile = route_map.get("isolationProfile")
    if profile is None and isinstance(default_pi, Mapping):
        profile = default_pi.get("isolationProfile")
    return (
        runtime if isinstance(runtime, str) else None,
        profile if isinstance(profile, str) else None,
    )


def _telegram_registered(
    config: Mapping[str, Any], user: Mapping[str, Any], agent: Mapping[str, Any]
) -> bool:
    if not isinstance(agent.get("botToken"), str) or not agent.get("botToken"):
        return False
    if isinstance(user.get("chatId"), str) and user.get("chatId"):
        return True
    bindings = config.get("bindings")
    if not isinstance(bindings, list):
        return False
    user_id = user.get("userId")
    agent_id = agent.get("id")
    for raw in bindings:
        if not isinstance(raw, Mapping) or raw.get("status") != "active":
            continue
        address = raw.get("address")
        if not isinstance(address, Mapping):
            continue
        if (
            raw.get("principalId") == user_id
            and address.get("channel") == "telegram"
            and address.get("kind") == "telegram_chat"
            and (
                address.get("botId") == agent_id
                or raw.get("defaultAgentId") == agent_id
                or agent_id in (raw.get("allowedAgentIds") or [])
            )
        ):
            return True
    return False


def _safe_tenant_directory(pool_key: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", pool_key)[:48] or "tenant"
    digest = sha256(pool_key.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _load_relay_mapping(
    lifecycle: _Lifecycle,
    *,
    relay_config_path: str | Path | None,
    relay_state_path: str | Path | None,
    relay_pi_manifest_path: str | Path | None,
    relay_metering_dir: str | Path | None,
) -> tuple[str, _RelayMapping | None, tuple[str, ...]]:
    relay_home = Path.home() / ".relay"
    config_path = _default_path(
        relay_config_path, "RELAY_CONFIG_PATH", relay_home / "relay.json"
    )
    state_path = Path(relay_state_path).expanduser() if relay_state_path is not None else config_path.parent / "state.json"
    manifest_path = _default_path(
        relay_pi_manifest_path,
        "RELAY_PI_RUNTIME_MANIFEST_PATH",
        config_path.parent / "local-pulse" / "pi-runtime-manifest.json",
    )
    metering_path = _default_path(
        relay_metering_dir, "METERING_DIR", config_path.parent / "metering"
    )
    attempted_refs = tuple(
        _source_ref(path) for path in (config_path, state_path, manifest_path)
    )
    try:
        config = _read_json_object(config_path, "relay_config")
        state = _read_json_object(state_path, "relay_state")
        manifest = _read_json_object(manifest_path, "relay_pi_manifest")
    except RetrospectiveUsageError as exc:
        reason = str(exc)
        return (
            "source_missing" if reason.endswith("_missing") else "source_unreadable",
            None,
            attempted_refs,
        )
    if (
        manifest.get("schemaVersion") != "relay_pi_runtime_manifest/v1"
        or state.get("version") != 1
        or not isinstance(config.get("users"), list)
        or not isinstance(state.get("sessions"), Mapping)
        or not isinstance(manifest.get("profiles"), Mapping)
    ):
        return "source_unreadable", None, attempted_refs

    candidates: list[_RelayMapping] = []
    mapping_conflict = False
    for user in config["users"]:
        if not isinstance(user, Mapping) or not isinstance(user.get("agents"), list):
            continue
        user_id = user.get("userId")
        if not isinstance(user_id, str) or not user_id:
            continue
        for agent in user["agents"]:
            if not isinstance(agent, Mapping):
                continue
            agent_id = agent.get("id")
            principal = agent.get("harnessAgentId", agent_id)
            runtime, profile_id = _effective_pi_route(config, agent)
            if (
                principal != lifecycle.owner
                or not isinstance(agent_id, str)
                or not agent_id
                or runtime != "pi"
                or profile_id is None
                or not _telegram_registered(config, user, agent)
            ):
                continue
            pool_key = f"{user_id}:{agent_id}"
            persisted = state["sessions"].get(pool_key)
            configured_session = agent.get("sessionId")
            if (
                not isinstance(persisted, Mapping)
                or persisted.get("poolKey") != pool_key
                or persisted.get("agentId") != agent_id
                or persisted.get("runtime") != "pi"
                or not isinstance(configured_session, str)
                or not configured_session
                or persisted.get("sessionId") != configured_session
            ):
                mapping_conflict = True
                continue
            profile = manifest["profiles"].get(profile_id)
            session_root = profile.get("sessionRoot") if isinstance(profile, Mapping) else None
            if not isinstance(session_root, str) or not Path(session_root).is_absolute():
                mapping_conflict = True
                continue
            candidates.append(_RelayMapping(
                pool_key=pool_key,
                session_id=configured_session,
                session_dir=Path(session_root) / _safe_tenant_directory(pool_key),
                metering_dir=metering_path,
                source_refs=attempted_refs,
            ))
    if len(candidates) > 1 or mapping_conflict:
        return "ambiguous_mapping", None, attempted_refs
    if not candidates:
        return "source_missing", None, attempted_refs
    return "unique", candidates[0], attempted_refs


def _in_intervals(value: datetime, intervals: tuple[tuple[datetime, datetime], ...]) -> bool:
    return any(start <= value <= end for start, end in intervals)


def _reasoning(values: list[int | None]) -> dict[str, Any]:
    known = [value for value in values if value is not None]
    unknown_count = len(values) - len(known)
    return {
        "status": (
            "observed" if not unknown_count
            else "partial" if known
            else _UNAVAILABLE
        ),
        "known_tokens": sum(known) if known else (0 if not values else _UNAVAILABLE),
        "unknown_event_count": unknown_count,
    }


def _unavailable_tokens() -> dict[str, Any]:
    return {
        **{field: _UNAVAILABLE for field in _TOKEN_FIELDS},
        "reasoning": {
            "status": _UNAVAILABLE,
            "known_tokens": _UNAVAILABLE,
            "unknown_event_count": _UNAVAILABLE,
        },
        "total_tokens": _UNAVAILABLE,
    }


def _pi_window(facts: Iterable[Any], predicate: Any) -> dict[str, Any]:
    window_facts = [
        fact for fact in facts
        if predicate(_timestamp(fact.occurred_at, "pi.occurred_at"))
    ]
    # Relay metering records settled outer turns.  Use Pi assistant-turn records
    # for that cross-check and disclose internal compaction/tool usage separately
    # rather than silently adding incomparable work to one side.
    selected = [fact for fact in window_facts if fact.activity == "assistant_turn"]
    excluded = [fact for fact in window_facts if fact.activity != "assistant_turn"]

    def totals_for(rows: Iterable[Any]) -> tuple[dict[str, int], list[int | None], int]:
        totals = {field: 0 for field in _TOKEN_FIELDS}
        reasoning: list[int | None] = []
        total_tokens = 0
        for fact in rows:
            totals["input"] += fact.usage.input
            totals["cache_read"] += fact.usage.cache_read
            totals["cache_write"] += fact.usage.cache_write
            totals["output"] += fact.usage.output
            reasoning.append(fact.usage.reasoning)
            total_tokens += fact.usage.total_tokens
        return totals, reasoning, total_tokens

    totals, reasoning, total_tokens = totals_for(selected)
    excluded_totals, _excluded_reasoning, excluded_total_tokens = totals_for(excluded)
    pricing_requests = [
        {
            "model": fact.model,
            # WI pricing consumes normalized total input and derives uncached
            # input once. Pi's native input/cache counters are disjoint.
            "input": (
                fact.usage.input
                + fact.usage.cache_read
                + fact.usage.cache_write
            ),
            "cache_read": fact.usage.cache_read,
            "cache_write": fact.usage.cache_write,
            "output": fact.usage.output,
        }
        for fact in selected
    ]
    return {
        "status": "available",
        "scope": "assistant_turn_usage_for_outer_turn_cross_check",
        "event_count": len(selected),
        "tokens": {**totals, "reasoning": _reasoning(reasoning), "total_tokens": total_tokens},
        "pricing_request_partitions": pricing_requests,
        "excluded_non_outer_turn_usage": {
            "event_count": len(excluded),
            "tokens": {**excluded_totals, "total_tokens": excluded_total_tokens},
        },
    }


def _collect_pi(
    mapping: _RelayMapping,
    *,
    terminal_at: datetime,
) -> tuple[str, list[Any], list[Path], list[str]]:
    try:
        if not mapping.session_dir.is_dir() or mapping.session_dir.is_symlink():
            return "source_missing", [], [], ["relay_pi_source_missing"]
        files = sorted(
            path for path in mapping.session_dir.glob("*.jsonl")
            if path.is_file() and not path.is_symlink()
            and path.name.endswith(f"_{mapping.session_id}.jsonl")
        )
    except OSError:
        return "source_unreadable", [], [], ["relay_pi_source_unreadable"]
    if not files:
        return "source_missing", [], [], ["relay_pi_source_missing"]
    facts_by_id: dict[str, Any] = {}
    try:
        for path in files:
            with path.open("r", encoding="utf-8", errors="strict") as handle:
                first = handle.readline()
            header = json.loads(first)
            if not isinstance(header, Mapping) or header.get("type") != "session" or header.get("id") != mapping.session_id:
                raise RetrospectiveUsageError("relay_pi_session_header_mismatch")
            collection = collect_pi_session_usage_through(
                path,
                # The native parser requires its legacy envelope fields, but
                # this sentinel is explicitly non-WI and is discarded before
                # candidate aggregation.  No fact is rebound to the target WI.
                PiUsageBinding(
                    work_item_id="retrospective_unattributed",
                    phase="retrospective",
                    epoch=1,
                    procurement_source="unknown",
                ),
                terminal_at,
            )
            for fact in collection.facts:
                prior = facts_by_id.get(fact.fact_id)
                if prior is not None and prior.to_dict() != fact.to_dict():
                    raise RetrospectiveUsageError("relay_pi_duplicate_fact_conflict")
                facts_by_id[fact.fact_id] = fact
    except (OSError, json.JSONDecodeError, PiUsageError, RetrospectiveUsageError):
        return "source_unreadable", [], files, ["relay_pi_source_unreadable"]
    return "available", list(facts_by_id.values()), files, []


def _metering_row_identity(row: Mapping[str, Any]) -> str:
    return sha256(_canonical([
        row.get("ts"), row.get("tenant"), row.get("session_id"), row.get("model"),
        *[row.get(field) for field in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens",
        )],
    ])).hexdigest()


def _collect_metering(mapping: _RelayMapping) -> tuple[str, list[tuple[datetime, Mapping[str, Any]]], list[Path], list[str]]:
    try:
        files = sorted(
            path for path in mapping.metering_dir.glob("*.jsonl")
            if path.is_file() and not path.is_symlink()
        ) if mapping.metering_dir.is_dir() and not mapping.metering_dir.is_symlink() else []
    except OSError:
        return "source_unreadable", [], [], ["relay_metering_source_unreadable"]
    if not files:
        return "source_missing", [], [], ["relay_metering_source_missing"]
    records: dict[str, tuple[datetime, Mapping[str, Any], Path]] = {}
    malformed = False
    for path in files:
        try:
            handle = path.open("r", encoding="utf-8", errors="strict")
        except OSError:
            malformed = True
            continue
        with handle:
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                try:
                    row = json.loads(raw_line)
                    if not isinstance(row, Mapping):
                        raise ValueError("not an object")
                except (json.JSONDecodeError, ValueError):
                    malformed = True
                    continue
                if row.get("tenant") != mapping.pool_key or row.get("session_id") != mapping.session_id:
                    continue
                try:
                    occurred = _timestamp(row.get("ts"), "relay_metering.ts")
                except RetrospectiveUsageError:
                    malformed = True
                    continue
                records.setdefault(_metering_row_identity(row), (occurred, row, path))
    if not records:
        return (
            "source_unreadable" if malformed else "source_missing",
            [],
            [],
            ["relay_metering_source_unreadable" if malformed else "relay_metering_source_missing"],
        )
    used_files = sorted({record[2] for record in records.values()})
    if malformed:
        return "source_unreadable", [], used_files, ["relay_metering_source_unreadable"]
    rows = [(occurred, row) for occurred, row, _path in records.values()]
    return "available", rows, used_files, []


def _metering_window(
    rows: Iterable[tuple[datetime, Mapping[str, Any]]], predicate: Any
) -> dict[str, Any]:
    selected = [row for occurred, row in rows if predicate(occurred)]
    totals: dict[str, int | str] = {}
    raw_fields = {
        "input": "input_tokens",
        "cache_read": "cache_read_input_tokens",
        "cache_write": "cache_creation_input_tokens",
        "output": "output_tokens",
    }
    for target, source in raw_fields.items():
        values = [row.get(source) for row in selected]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            totals[target] = _UNAVAILABLE
        else:
            totals[target] = sum(int(value) for value in values)
    total_tokens: int | str
    if all(isinstance(totals[field], int) for field in _TOKEN_FIELDS):
        total_tokens = sum(int(totals[field]) for field in _TOKEN_FIELDS)
    else:
        total_tokens = _UNAVAILABLE
    return {
        "status": "available" if isinstance(total_tokens, int) else "partial",
        "event_count": len(selected),
        "tokens": {
            **totals,
            "reasoning": {
                "status": _UNAVAILABLE,
                "known_tokens": _UNAVAILABLE,
                "unknown_event_count": len(selected) if selected else 0,
            },
            "total_tokens": total_tokens,
        },
    }


def _unavailable_source(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "event_count": _UNAVAILABLE,
        "tokens": _unavailable_tokens(),
    }


def _cross_check(pi: Mapping[str, Any], metering: Mapping[str, Any]) -> dict[str, Any]:
    pi_tokens = pi.get("tokens")
    relay_tokens = metering.get("tokens")
    pi_total = pi_tokens.get("total_tokens") if isinstance(pi_tokens, Mapping) else None
    relay_total = relay_tokens.get("total_tokens") if isinstance(relay_tokens, Mapping) else None
    if not isinstance(pi_total, int) or not isinstance(relay_total, int):
        return {
            "status": _UNAVAILABLE,
            "difference_tokens": _UNAVAILABLE,
            "difference_ratio": _UNAVAILABLE,
            "event_count_comparison": "not_comparable_native_events_vs_outer_turns",
        }
    difference = abs(pi_total - relay_total)
    denominator = max(pi_total, relay_total)
    ratio = Decimal(0) if denominator == 0 else Decimal(difference) / Decimal(denominator)
    return {
        "status": "corroborated_exact" if difference == 0 else "corroborated_with_divergence",
        "difference_tokens": difference,
        "difference_ratio": format(ratio.quantize(Decimal("0.000001")), "f"),
        "event_count_comparison": "not_comparable_native_events_vs_outer_turns",
    }


def _window(
    *,
    classification: str,
    bounds: Mapping[str, Any],
    pi: Mapping[str, Any],
    metering: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "classification": classification,
        "bounds": dict(bounds),
        "pi_jsonl": dict(pi),
        "relay_metering": dict(metering),
        "cross_check": _cross_check(pi, metering),
    }


def _candidate_digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical({
        key: item for key, item in value.items() if key != "candidate_sha256"
    })).hexdigest()


def _retrospective_attention(
    lifecycle: _Lifecycle,
    *,
    relay_turn_binding: str,
) -> dict[str, Any]:
    """Build a safe terminal-WI subset; never time-window Relay turns."""

    elapsed = int((lifecycle.terminal_at - lifecycle.created_at).total_seconds())
    categories: dict[str, int | str] = {
        name: UNAVAILABLE for name in CATEGORIES
    }
    categories["review_approval"] = lifecycle.review_approval_count
    candidate: dict[str, Any] = {
        "schema": _ATTENTION_CANDIDATE_SCHEMA,
        "status": "partial",
        "canonical_backfill": False,
        "cycle_elapsed_seconds": elapsed,
        "cycle_elapsed_classification": "attention_exposure_not_focused_time",
        "focused_seconds": UNAVAILABLE,
        "focused_seconds_basis": "timer_or_trusted_telemetry_only",
        "exact_human_feedback_count": UNAVAILABLE,
        "category_counts": categories,
        "effort_band_counts": {
            name: UNAVAILABLE for name in EFFORT_BANDS
        },
        "human_attention_units": UNAVAILABLE,
        "contract_version": "g0_hau_v1",
        "density_per_elapsed_day": {
            "feedback_count": UNAVAILABLE,
            "human_attention_units": UNAVAILABLE,
        },
        "relay_turn_binding": relay_turn_binding,
        "time_window_turn_inference_used": False,
        "source_refs": list(lifecycle.source_refs),
    }
    candidate["candidate_sha256"] = sha256(_canonical(candidate)).hexdigest()
    return candidate


def _base_candidate(
    work_item_id: str,
    lifecycle: _Lifecycle,
    *,
    status: str,
    mapping: str,
    diagnostics: Iterable[str],
    source_refs: Iterable[str],
    pi_status: str = "source_missing",
    metering_status: str = "source_missing",
) -> dict[str, Any]:
    pi = _unavailable_source(pi_status)
    metering = _unavailable_source(metering_status)
    candidate: dict[str, Any] = {
        "schema": RETROSPECTIVE_CANDIDATE_SCHEMA,
        "work_item_id": work_item_id,
        "type": "relay_tg_pi_unsealed_window_estimate",
        "status": status,
        "canonical_attribution": False,
        "canonical_usage_eligible": False,
        "token_seal_eligible": False,
        "cost_eligible": False,
        "roi_eligible": False,
        "attribution": {
            "method": "relay_registration_state_and_harness_lifecycle_window",
            "channel": "telegram",
            "mapping": mapping,
            "model_text_searched": False,
        },
        "lifecycle": lifecycle.to_dict(),
        "human_attention": _retrospective_attention(
            lifecycle,
            relay_turn_binding=(
                "ambiguous"
                if mapping == "ambiguous"
                else "unavailable_no_exact_wi_bound_relay_turn_schema"
            ),
        ),
        "windows": {
            "created_to_terminal": _window(
                classification="estimate_may_include_unrelated_turns",
                bounds={"started_at": _utc(lifecycle.created_at), "ended_at": _utc(lifecycle.terminal_at)},
                pi=pi,
                metering=metering,
            ),
            "lifecycle_active": _window(
                classification="estimate_may_omit_verification_work",
                bounds={"interval_count": len(lifecycle.intervals)},
                pi=pi,
                metering=metering,
            ),
        },
        "source_permissions": {
            "status": _UNAVAILABLE,
            "expected_mode": "0600",
            "by_source": {
                "pi_jsonl": _permissions(()),
                "relay_metering": _permissions(()),
            },
        },
        "diagnostics": sorted(set(diagnostics)),
        "source_refs": sorted(set((*lifecycle.source_refs, *source_refs))),
    }
    candidate["candidate_sha256"] = _candidate_digest(candidate)
    validate_retrospective_candidate(candidate, expected_work_item_id=work_item_id)
    return candidate


def discover_relay_pi_retrospective(
    *,
    work_item_id: str,
    harness_root: str | Path,
    relay_config_path: str | Path | None = None,
    relay_state_path: str | Path | None = None,
    relay_pi_manifest_path: str | Path | None = None,
    relay_metering_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Discover an unsealed estimate; never emit canonical usage events."""

    if _WI_RE.fullmatch(work_item_id) is None:
        raise RetrospectiveUsageError("work_item_id_invalid")
    root = Path(harness_root).expanduser().resolve()
    lifecycle = _load_lifecycle(root, work_item_id)
    mapping_status, mapping, registration_refs = _load_relay_mapping(
        lifecycle,
        relay_config_path=relay_config_path,
        relay_state_path=relay_state_path,
        relay_pi_manifest_path=relay_pi_manifest_path,
        relay_metering_dir=relay_metering_dir,
    )
    base_diagnostics = [
        "retrospective_unsealed_not_canonical",
        "created_to_terminal_may_include_unrelated_turns",
        "lifecycle_active_may_omit_verification_work",
    ]
    if len(lifecycle.intervals) > 1:
        base_diagnostics.append("multiple_lifecycle_active_intervals")
    if mapping_status != "unique" or mapping is None:
        diagnostic = {
            "ambiguous_mapping": "relay_pi_mapping_ambiguous",
            "source_unreadable": "relay_registration_source_unreadable",
        }.get(mapping_status, "relay_pi_mapping_unavailable")
        return _base_candidate(
            work_item_id,
            lifecycle,
            status=mapping_status,
            mapping="ambiguous" if mapping_status == "ambiguous_mapping" else _UNAVAILABLE,
            diagnostics=(*base_diagnostics, diagnostic),
            source_refs=registration_refs,
            pi_status=mapping_status,
            metering_status=mapping_status,
        )

    pi_status, pi_facts, pi_files, pi_diagnostics = _collect_pi(
        mapping,
        terminal_at=lifecycle.terminal_at,
    )
    metering_status, metering_rows, metering_files, metering_diagnostics = _collect_metering(mapping)
    pi_broad = (
        _pi_window(
            pi_facts,
            lambda occurred: lifecycle.created_at <= occurred <= lifecycle.terminal_at,
        )
        if pi_status == "available" else _unavailable_source(pi_status)
    )
    pi_active = (
        _pi_window(pi_facts, lambda occurred: _in_intervals(occurred, lifecycle.intervals))
        if pi_status == "available" else _unavailable_source(pi_status)
    )
    metering_broad = (
        _metering_window(
            metering_rows,
            lambda occurred: lifecycle.created_at <= occurred <= lifecycle.terminal_at,
        )
        if metering_status == "available" else _unavailable_source(metering_status)
    )
    metering_active = (
        _metering_window(
            metering_rows,
            lambda occurred: _in_intervals(occurred, lifecycle.intervals),
        )
        if metering_status == "available" else _unavailable_source(metering_status)
    )
    pi_permissions = _permissions(pi_files)
    metering_permissions = _permissions(metering_files)
    permission_status = (
        "privacy_defect"
        if "privacy_defect" in {pi_permissions["status"], metering_permissions["status"]}
        else "ok"
        if {pi_permissions["status"], metering_permissions["status"]} == {"ok"}
        else _UNAVAILABLE
    )
    diagnostics = [*base_diagnostics, *pi_diagnostics, *metering_diagnostics]
    if (
        isinstance(pi_broad.get("excluded_non_outer_turn_usage"), Mapping)
        and pi_broad["excluded_non_outer_turn_usage"].get("event_count", 0) > 0
    ):
        diagnostics.append("pi_non_outer_turn_usage_excluded_from_cross_check")
    pi_reasoning = pi_broad.get("tokens", {}).get("reasoning")
    if isinstance(pi_reasoning, Mapping) and pi_reasoning.get("status") == "partial":
        diagnostics.append("pi_reasoning_partial")
    if any(source.get("status") == "partial" for source in (metering_broad, metering_active)):
        diagnostics.append("relay_metering_resource_fields_unavailable")
    if permission_status == "privacy_defect":
        diagnostics.append("historical_source_permissions_privacy_defect")
    cross_checks = (
        _cross_check(pi_broad, metering_broad),
        _cross_check(pi_active, metering_active),
    )
    if any(check["status"] == "corroborated_with_divergence" for check in cross_checks):
        diagnostics.append("pi_relay_metering_divergence")
    available_count = sum(status == "available" for status in (pi_status, metering_status))
    window_partial = any(
        source.get("status") == "partial"
        for source in (pi_broad, pi_active, metering_broad, metering_active)
    )
    status = (
        "available" if available_count == 2 and not window_partial
        else "partial" if available_count or window_partial
        else "source_unreadable" if "source_unreadable" in {pi_status, metering_status}
        else "source_missing"
    )
    source_paths = [*pi_files, *metering_files]
    candidate: dict[str, Any] = {
        "schema": RETROSPECTIVE_CANDIDATE_SCHEMA,
        "work_item_id": work_item_id,
        "type": "relay_tg_pi_unsealed_window_estimate",
        "status": status,
        "canonical_attribution": False,
        "canonical_usage_eligible": False,
        "token_seal_eligible": False,
        "cost_eligible": False,
        "roi_eligible": False,
        "attribution": {
            "method": "relay_registration_state_and_harness_lifecycle_window",
            "channel": "telegram",
            "mapping": "unique",
            "model_text_searched": False,
        },
        "lifecycle": lifecycle.to_dict(),
        "human_attention": _retrospective_attention(
            lifecycle,
            relay_turn_binding="unavailable_no_exact_wi_bound_relay_turn_schema",
        ),
        "windows": {
            "created_to_terminal": _window(
                classification="estimate_may_include_unrelated_turns",
                bounds={"started_at": _utc(lifecycle.created_at), "ended_at": _utc(lifecycle.terminal_at)},
                pi=pi_broad,
                metering=metering_broad,
            ),
            "lifecycle_active": _window(
                classification="estimate_may_omit_verification_work",
                bounds={"interval_count": len(lifecycle.intervals)},
                pi=pi_active,
                metering=metering_active,
            ),
        },
        "source_permissions": {
            "status": permission_status,
            "expected_mode": "0600",
            "by_source": {
                "pi_jsonl": pi_permissions,
                "relay_metering": metering_permissions,
            },
        },
        "diagnostics": sorted(set(diagnostics)),
        "source_refs": sorted(set((
            *lifecycle.source_refs,
            *mapping.source_refs,
            *(_source_ref(path) for path in source_paths),
        ))),
    }
    candidate["candidate_sha256"] = _candidate_digest(candidate)
    validate_retrospective_candidate(candidate, expected_work_item_id=work_item_id)
    return candidate


def _walk_privacy(value: object) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if str(key).lower() in _FORBIDDEN_KEYS:
                    raise RetrospectiveUsageError("retrospective_candidate_contains_raw_identity")
                pending.append(nested)
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, str) and (
            current.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", current)
        ):
            raise RetrospectiveUsageError("retrospective_candidate_contains_raw_path")


def _exact_fields(value: object, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise RetrospectiveUsageError(f"{label}_fields_invalid")
    return value


def _count_or_unavailable(value: object, label: str) -> None:
    if value == _UNAVAILABLE:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RetrospectiveUsageError(f"{label}_invalid")


def _validate_reasoning(value: object, *, output: object, label: str) -> None:
    row = _exact_fields(
        value, {"status", "known_tokens", "unknown_event_count"}, label
    )
    if row.get("status") not in {"observed", "partial", _UNAVAILABLE}:
        raise RetrospectiveUsageError(f"{label}_status_invalid")
    _count_or_unavailable(row.get("known_tokens"), f"{label}_known_tokens")
    _count_or_unavailable(row.get("unknown_event_count"), f"{label}_unknown_events")
    known = row.get("known_tokens")
    if isinstance(known, int) and isinstance(output, int) and known > output:
        raise RetrospectiveUsageError(f"{label}_exceeds_output")


def _validate_tokens(value: object, label: str) -> None:
    row = _exact_fields(
        value, {*_TOKEN_FIELDS, "reasoning", "total_tokens"}, label
    )
    for field in _TOKEN_FIELDS:
        _count_or_unavailable(row.get(field), f"{label}_{field}")
    _count_or_unavailable(row.get("total_tokens"), f"{label}_total")
    _validate_reasoning(row.get("reasoning"), output=row.get("output"), label=f"{label}_reasoning")
    values = [row.get(field) for field in _TOKEN_FIELDS]
    if all(isinstance(item, int) and not isinstance(item, bool) for item in values):
        if row.get("total_tokens") != sum(int(item) for item in values):
            raise RetrospectiveUsageError(f"{label}_does_not_conserve")


def _validate_source(value: object, *, pi: bool, label: str) -> None:
    row = value if isinstance(value, Mapping) else {}
    status = row.get("status")
    source_states = {
        "available", "partial", "source_missing", "source_unreadable",
        "ambiguous_mapping",
    }
    if status not in source_states:
        raise RetrospectiveUsageError(f"{label}_status_invalid")
    unavailable = status not in {"available", "partial"}
    if unavailable:
        _exact_fields(row, {"status", "event_count", "tokens"}, label)
    elif pi:
        _exact_fields(
            row,
            {"status", "scope", "event_count", "tokens", "pricing_request_partitions", "excluded_non_outer_turn_usage"},
            label,
        )
        if row.get("scope") != "assistant_turn_usage_for_outer_turn_cross_check":
            raise RetrospectiveUsageError(f"{label}_scope_invalid")
        requests = row.get("pricing_request_partitions")
        if not isinstance(requests, list) or len(requests) != row.get("event_count"):
            raise RetrospectiveUsageError(f"{label}_pricing_requests_invalid")
        normalized = {field: 0 for field in _TOKEN_FIELDS}
        for request in requests:
            request_row = _exact_fields(
                request, {"model", "input", "cache_read", "cache_write", "output"},
                f"{label}_pricing_request",
            )
            if not isinstance(request_row.get("model"), str) or not request_row["model"]:
                raise RetrospectiveUsageError(f"{label}_pricing_model_invalid")
            for field in _TOKEN_FIELDS:
                _count_or_unavailable(request_row.get(field), f"{label}_pricing_{field}")
                normalized[field] += int(request_row[field])
            if request_row["cache_read"] + request_row["cache_write"] > request_row["input"]:
                raise RetrospectiveUsageError(f"{label}_pricing_cache_invalid")
        source_tokens = row.get("tokens")
        if isinstance(source_tokens, Mapping) and all(
            isinstance(source_tokens.get(field), int) for field in _TOKEN_FIELDS
        ):
            expected_normalized_input = (
                int(source_tokens["input"])
                + int(source_tokens["cache_read"])
                + int(source_tokens["cache_write"])
            )
            if (
                normalized["input"] != expected_normalized_input
                or normalized["cache_read"] != source_tokens["cache_read"]
                or normalized["cache_write"] != source_tokens["cache_write"]
                or normalized["output"] != source_tokens["output"]
            ):
                raise RetrospectiveUsageError(f"{label}_pricing_requests_do_not_conserve")
        excluded = _exact_fields(
            row.get("excluded_non_outer_turn_usage"), {"event_count", "tokens"},
            f"{label}_excluded",
        )
        _count_or_unavailable(excluded.get("event_count"), f"{label}_excluded_events")
        excluded_tokens = _exact_fields(
            excluded.get("tokens"), {*_TOKEN_FIELDS, "total_tokens"},
            f"{label}_excluded_tokens",
        )
        for field in (*_TOKEN_FIELDS, "total_tokens"):
            _count_or_unavailable(excluded_tokens.get(field), f"{label}_excluded_{field}")
        values = [excluded_tokens.get(field) for field in _TOKEN_FIELDS]
        if all(isinstance(item, int) and not isinstance(item, bool) for item in values):
            if excluded_tokens.get("total_tokens") != sum(int(item) for item in values):
                raise RetrospectiveUsageError(f"{label}_excluded_does_not_conserve")
    else:
        _exact_fields(row, {"status", "event_count", "tokens"}, label)
    _count_or_unavailable(row.get("event_count"), f"{label}_events")
    _validate_tokens(row.get("tokens"), f"{label}_tokens")
    if unavailable and (
        row.get("event_count") != _UNAVAILABLE
        or any(row["tokens"].get(field) != _UNAVAILABLE for field in (*_TOKEN_FIELDS, "total_tokens"))
    ):
        raise RetrospectiveUsageError(f"{label}_unavailable_claims_zero")


def _validate_cross_check(value: object, label: str) -> None:
    row = _exact_fields(
        value,
        {"status", "difference_tokens", "difference_ratio", "event_count_comparison"},
        label,
    )
    if row.get("status") not in {
        "corroborated_exact", "corroborated_with_divergence", _UNAVAILABLE,
    } or row.get("event_count_comparison") != "not_comparable_native_events_vs_outer_turns":
        raise RetrospectiveUsageError(f"{label}_invalid")
    _count_or_unavailable(row.get("difference_tokens"), f"{label}_difference")
    ratio = row.get("difference_ratio")
    if ratio != _UNAVAILABLE and (
        not isinstance(ratio, str) or re.fullmatch(r"\d+\.\d{6}", ratio) is None
    ):
        raise RetrospectiveUsageError(f"{label}_ratio_invalid")


def _validate_permission_row(value: object, label: str) -> None:
    row = _exact_fields(
        value,
        {"file_count", "observed_modes", "unsafe_file_count", "unreadable_file_count", "status"},
        label,
    )
    for field in ("file_count", "unsafe_file_count", "unreadable_file_count"):
        _count_or_unavailable(row.get(field), f"{label}_{field}")
    modes = row.get("observed_modes")
    if not isinstance(modes, list) or any(
        not isinstance(mode, str) or re.fullmatch(r"0[0-7]{3}", mode) is None
        for mode in modes
    ) or row.get("status") not in {"ok", "privacy_defect", _UNAVAILABLE}:
        raise RetrospectiveUsageError(f"{label}_invalid")


def _validate_retrospective_attention(value: object) -> None:
    row = _exact_fields(
        value,
        {
            "schema", "status", "canonical_backfill", "cycle_elapsed_seconds",
            "cycle_elapsed_classification", "focused_seconds",
            "focused_seconds_basis", "exact_human_feedback_count",
            "category_counts", "effort_band_counts", "human_attention_units",
            "contract_version", "density_per_elapsed_day", "relay_turn_binding",
            "time_window_turn_inference_used", "source_refs", "candidate_sha256",
        },
        "retrospective_attention",
    )
    if (
        row.get("schema") != _ATTENTION_CANDIDATE_SCHEMA
        or row.get("status") != "partial"
        or row.get("canonical_backfill") is not False
        or row.get("cycle_elapsed_classification")
        != "attention_exposure_not_focused_time"
        or row.get("focused_seconds") != UNAVAILABLE
        or row.get("focused_seconds_basis") != "timer_or_trusted_telemetry_only"
        or row.get("exact_human_feedback_count") != UNAVAILABLE
        or row.get("human_attention_units") != UNAVAILABLE
        or row.get("contract_version") != "g0_hau_v1"
        or row.get("relay_turn_binding") not in {
            "ambiguous", "unavailable_no_exact_wi_bound_relay_turn_schema"
        }
        or row.get("time_window_turn_inference_used") is not False
    ):
        raise RetrospectiveUsageError("retrospective_attention_contract_invalid")
    elapsed = row.get("cycle_elapsed_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0:
        raise RetrospectiveUsageError("retrospective_attention_elapsed_invalid")
    categories = _exact_fields(
        row.get("category_counts"), set(CATEGORIES),
        "retrospective_attention_categories",
    )
    for name, count in categories.items():
        if name == "review_approval":
            _count_or_unavailable(count, f"retrospective_attention_{name}")
        elif count != UNAVAILABLE:
            raise RetrospectiveUsageError("retrospective_attention_infers_category_zero")
    bands = _exact_fields(
        row.get("effort_band_counts"), set(EFFORT_BANDS),
        "retrospective_attention_bands",
    )
    if any(count != UNAVAILABLE for count in bands.values()):
        raise RetrospectiveUsageError("retrospective_attention_infers_effort")
    density = _exact_fields(
        row.get("density_per_elapsed_day"),
        {"feedback_count", "human_attention_units"},
        "retrospective_attention_density",
    )
    if any(item != UNAVAILABLE for item in density.values()):
        raise RetrospectiveUsageError("retrospective_attention_infers_density")
    refs = row.get("source_refs")
    if not isinstance(refs, list) or any(
        not isinstance(ref, str) or _SOURCE_REF_RE.fullmatch(ref) is None
        for ref in refs
    ):
        raise RetrospectiveUsageError("retrospective_attention_source_refs_invalid")
    digest = row.get("candidate_sha256")
    unsigned = {key: item for key, item in row.items() if key != "candidate_sha256"}
    if not isinstance(digest, str) or digest != sha256(_canonical(unsigned)).hexdigest():
        raise RetrospectiveUsageError("retrospective_attention_digest_invalid")


def validate_retrospective_candidate(
    value: object, *, expected_work_item_id: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RetrospectiveUsageError("retrospective_candidate_invalid")
    expected_fields = {
        "schema", "work_item_id", "type", "status", "canonical_attribution",
        "canonical_usage_eligible", "token_seal_eligible", "cost_eligible",
        "roi_eligible", "attribution", "lifecycle", "human_attention", "windows", "source_permissions",
        "diagnostics", "source_refs", "candidate_sha256",
    }
    if set(value) != expected_fields:
        raise RetrospectiveUsageError("retrospective_candidate_fields_invalid")
    if (
        value.get("schema") != RETROSPECTIVE_CANDIDATE_SCHEMA
        or value.get("work_item_id") != expected_work_item_id
        or value.get("type") != "relay_tg_pi_unsealed_window_estimate"
        or value.get("status") not in _CANDIDATE_STATES
    ):
        raise RetrospectiveUsageError("retrospective_candidate_identity_invalid")
    for field in (
        "canonical_attribution", "canonical_usage_eligible", "token_seal_eligible",
        "cost_eligible", "roi_eligible",
    ):
        if value.get(field) is not False:
            raise RetrospectiveUsageError("retrospective_candidate_claims_canonical_eligibility")
    attribution = _exact_fields(
        value.get("attribution"),
        {"method", "channel", "mapping", "model_text_searched"},
        "retrospective_candidate_attribution",
    )
    if (
        attribution.get("method") != "relay_registration_state_and_harness_lifecycle_window"
        or attribution.get("channel") != "telegram"
        or attribution.get("mapping") not in {"unique", "ambiguous", _UNAVAILABLE}
        or attribution.get("model_text_searched") is not False
    ):
        raise RetrospectiveUsageError("retrospective_candidate_attribution_invalid")

    _validate_retrospective_attention(value.get("human_attention"))

    lifecycle = _exact_fields(
        value.get("lifecycle"),
        {"status", "terminal", "created_at", "terminal_at", "active_interval_count", "active_intervals", "evidence_availability", "source_refs"},
        "retrospective_candidate_lifecycle",
    )
    if lifecycle.get("status") not in _TERMINAL_STATES or lifecycle.get("terminal") is not True:
        raise RetrospectiveUsageError("retrospective_candidate_lifecycle_invalid")
    created = _timestamp(lifecycle.get("created_at"), "retrospective.created_at")
    terminal = _timestamp(lifecycle.get("terminal_at"), "retrospective.terminal_at")
    intervals = lifecycle.get("active_intervals")
    _count_or_unavailable(lifecycle.get("active_interval_count"), "active_interval_count")
    if not isinstance(intervals, list) or len(intervals) != lifecycle.get("active_interval_count"):
        raise RetrospectiveUsageError("retrospective_candidate_intervals_invalid")
    for interval in intervals:
        row = _exact_fields(interval, {"started_at", "ended_at"}, "active_interval")
        start = _timestamp(row.get("started_at"), "active_interval.started_at")
        end = _timestamp(row.get("ended_at"), "active_interval.ended_at")
        if start < created or end < start or end > terminal:
            raise RetrospectiveUsageError("retrospective_candidate_intervals_invalid")
    evidence = _exact_fields(
        lifecycle.get("evidence_availability"),
        {"status", "registered_artifact_ref_count", "locally_available_artifact_count"},
        "retrospective_candidate_evidence_availability",
    )
    if evidence.get("status") not in {"available", "partial", "registered_refs_only", _UNAVAILABLE}:
        raise RetrospectiveUsageError("retrospective_candidate_evidence_availability_invalid")
    for field in ("registered_artifact_ref_count", "locally_available_artifact_count"):
        _count_or_unavailable(evidence.get(field), field)

    windows = _exact_fields(
        value.get("windows"), {"created_to_terminal", "lifecycle_active"},
        "retrospective_candidate_windows",
    )
    for name, classification, bound_fields in (
        ("created_to_terminal", "estimate_may_include_unrelated_turns", {"started_at", "ended_at"}),
        ("lifecycle_active", "estimate_may_omit_verification_work", {"interval_count"}),
    ):
        window = _exact_fields(
            windows.get(name), {"classification", "bounds", "pi_jsonl", "relay_metering", "cross_check"},
            f"retrospective_candidate_{name}",
        )
        if window.get("classification") != classification:
            raise RetrospectiveUsageError("retrospective_candidate_window_classification_invalid")
        bounds = _exact_fields(window.get("bounds"), bound_fields, f"{name}_bounds")
        if name == "created_to_terminal":
            if _timestamp(bounds.get("started_at"), "window.started_at") != created or _timestamp(bounds.get("ended_at"), "window.ended_at") != terminal:
                raise RetrospectiveUsageError("retrospective_candidate_window_bounds_invalid")
        elif bounds.get("interval_count") != len(intervals):
            raise RetrospectiveUsageError("retrospective_candidate_window_bounds_invalid")
        _validate_source(window.get("pi_jsonl"), pi=True, label=f"{name}_pi")
        _validate_source(window.get("relay_metering"), pi=False, label=f"{name}_metering")
        _validate_cross_check(window.get("cross_check"), f"{name}_cross_check")

    permissions = _exact_fields(
        value.get("source_permissions"), {"status", "expected_mode", "by_source"},
        "retrospective_candidate_source_permissions",
    )
    if permissions.get("status") not in {"ok", "privacy_defect", _UNAVAILABLE} or permissions.get("expected_mode") != "0600":
        raise RetrospectiveUsageError("retrospective_candidate_source_permissions_invalid")
    by_source = _exact_fields(
        permissions.get("by_source"), {"pi_jsonl", "relay_metering"},
        "retrospective_candidate_permissions_by_source",
    )
    _validate_permission_row(by_source.get("pi_jsonl"), "pi_jsonl_permissions")
    _validate_permission_row(by_source.get("relay_metering"), "relay_metering_permissions")

    diagnostics = value.get("diagnostics")
    if not isinstance(diagnostics, list) or any(item not in _DIAGNOSTICS for item in diagnostics):
        raise RetrospectiveUsageError("retrospective_candidate_diagnostics_invalid")
    refs = value.get("source_refs")
    lifecycle_refs = lifecycle.get("source_refs")
    if not isinstance(refs, list) or not isinstance(lifecycle_refs, list) or any(
        not isinstance(ref, str) or _SOURCE_REF_RE.fullmatch(ref) is None
        for ref in (*refs, *lifecycle_refs)
    ):
        raise RetrospectiveUsageError("retrospective_candidate_source_refs_invalid")
    digest = value.get("candidate_sha256")
    if not isinstance(digest, str) or digest != _candidate_digest(value):
        raise RetrospectiveUsageError("retrospective_candidate_digest_invalid")
    _walk_privacy(value)
    return dict(value)
