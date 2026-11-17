"""Privacy-safe producer discovery and usage_event -> Work Item cohort joins."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .adapters import UsageCollection, _lineage_run_ref, collect_usage_events
from .cc_lineage import _root_run_ref, cc_transcript_lineage
from .cc_usage import CcUsageError, cc_authority_ref, validate_cc_session_snapshot
from .events import Lineage, TokenUsage, UNKNOWN, UsageEvent, parse_timestamp
from .retrospective_usage import (
    RetrospectiveUsageError,
    discover_relay_pi_retrospective,
)
from .scl_measurement import (
    load_scl_measurement_snapshot,
    validate_scl_measurement_snapshot,
)

WI_RE = re.compile(r"^wi_\d{8}_\d{3}$")
HANDOFF_RE = re.compile(r"(?<![a-z0-9_])ho_\d{8}_\d{3}(?![a-z0-9_])", re.IGNORECASE)
WI_SEED_REFERENCE_RE = re.compile(
    r"(?<![a-z0-9])wi(?:"
    r"_(?P<full_date>\d{8})_(?P<full_suffix>\d{3})|"
    r"(?P<compact_date>\d{8})(?P<compact_suffix>\d{3})|"
    r"[_-]?(?P<short_suffix>\d{3})"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
SOURCE_THREAD_RE = re.compile(r"<source_thread_id>([^<]+)</source_thread_id>")
STRUCTURED_LINEAGE_PREFIX_RE = re.compile(
    r"^<(?:codex_delegation|codex_rollover|codex_handoff|session_rollover)(?:>|\s)",
    re.IGNORECASE,
)
COVERAGE_STATES = {
    "observed",
    "not_applicable",
    "producer_missing",
    "producer_unreachable",
    "stale",
    "join_conflict",
    "partial",
    "retrospective_unsealed",
}


class WiDiscoveryError(ValueError):
    pass


@dataclass(frozen=True)
class WiWindow:
    work_item_id: str
    started_at: datetime
    ended_at: datetime
    source_ref: str


@dataclass(frozen=True)
class ProducerAttempt:
    producer: str
    status: str
    adapters: tuple[str, ...]
    reason: str
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in COVERAGE_STATES:
            raise WiDiscoveryError("invalid producer coverage status")

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer": self.producer,
            "status": self.status,
            "adapters": list(self.adapters),
            "reason": self.reason,
            "source_refs": list(self.source_refs),
        }


class _AssignmentInventory:
    def __init__(
        self,
        *,
        work_items: Mapping[str, str],
        recipients: Mapping[str, str],
        outcome_bound: frozenset[str],
        source_refs: Mapping[str, tuple[str, ...]],
        conflicts: frozenset[str],
    ) -> None:
        self.work_items = dict(work_items)
        self.recipients = dict(recipients)
        self.outcome_bound = outcome_bound
        self.source_refs = dict(source_refs)
        self.conflicts = conflicts

    def target_ids(self, work_item_id: str) -> frozenset[str]:
        return frozenset(
            assignment
            for assignment, assigned_wi in self.work_items.items()
            if assigned_wi == work_item_id
        )

    def summary(
        self,
        work_item_id: str,
        *,
        session_count: int | str,
        token_coverage: str,
    ) -> dict[str, Any]:
        assignments = self.target_ids(work_item_id)
        return {
            "status": (
                "join_conflict" if work_item_id in self.conflicts
                else "observed" if assignments
                else "not_applicable"
            ),
            "assignment_count": len(assignments),
            "assigned_verifier_count": len({
                self.recipients[assignment]
                for assignment in assignments
                if assignment in self.recipients
            }),
            "outcome_bound_assignment_count": len(assignments & self.outcome_bound),
            "assigned_session_count": session_count,
            "token_coverage": token_coverage,
            "assignment_refs": sorted(
                "assignment_v1_" + hashlib.sha256(
                    f"pulse-computecost-assignment-v1\0{assignment}".encode()
                ).hexdigest()
                for assignment in assignments
            ),
            "source_refs": list(self.source_refs.get(work_item_id, ())),
        }


@dataclass(frozen=True)
class WiUsageCohort:
    work_item_id: str
    window: WiWindow
    events: tuple[UsageEvent, ...]
    root_thread_refs: tuple[str, ...]
    attempts: tuple[ProducerAttempt, ...]
    diagnostics: Mapping[str, Any]
    retrospective_candidates: tuple[Mapping[str, Any], ...] = ()

    @property
    def status(self) -> str:
        # Retrospective candidates are deliberately not producer events and do
        # not alter canonical cohort coverage.
        statuses = {
            attempt.status for attempt in self.attempts
            if attempt.status != "retrospective_unsealed"
        }
        observed = bool(self.events) or "observed" in statuses
        gaps = any(status not in {"observed", "not_applicable"} for status in statuses)
        if observed and gaps:
            return "partial"
        if observed:
            return "observed"
        # Structured assignment/session facts can be observed while their
        # archived rollout Token source is unavailable.  That is partial
        # coverage, not producer absence and not a zero-valued usage cohort.
        for status in (
            "join_conflict",
            "partial",
            "stale",
            "producer_unreachable",
            "producer_missing",
            "not_applicable",
        ):
            if status in statuses:
                return status
        return "join_conflict"

    def token_totals(self) -> dict[str, int]:
        totals = Counter()
        for event in self.events:
            totals["input"] += event.tokens.input
            totals["output"] += event.tokens.output
            totals["cache_read"] += event.tokens.cache_read
            totals["cache_write"] += event.tokens.cache_write
            if event.tokens.reasoning is not None:
                totals["reasoning"] += event.tokens.reasoning
        totals["net_new_input"] = (
            totals["input"] - totals["cache_read"] - totals["cache_write"]
        )
        totals["total_resource_tokens"] = totals["input"] + totals["output"]
        return {
            key: totals[key]
            for key in (
                "input",
                "cache_read",
                "cache_write",
                "net_new_input",
                "output",
                "reasoning",
                "total_resource_tokens",
            )
        }

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        by_model: dict[str, Counter[str]] = defaultdict(Counter)
        quality_flags: Counter[str] = Counter()
        for event in self.events:
            row = by_model[event.model]
            row["event_count"] += 1
            row["input"] += event.tokens.input
            row["cache_read"] += event.tokens.cache_read
            row["cache_write"] += event.tokens.cache_write
            row["output"] += event.tokens.output
            if event.tokens.reasoning is not None:
                row["reasoning"] += event.tokens.reasoning
            quality_flags.update(set(event.quality_flags))
        payload: dict[str, Any] = {
            "schema": "computecost_wi_usage_cohort/v1",
            "work_item_id": self.work_item_id,
            "status": self.status,
            "window": {
                "started_at": self.window.started_at.isoformat().replace("+00:00", "Z"),
                "ended_at": self.window.ended_at.isoformat().replace("+00:00", "Z"),
                "source_ref": self.window.source_ref,
            },
            "event_count": len(self.events),
            "root_thread_count": len(self.root_thread_refs),
            "root_thread_refs": list(self.root_thread_refs),
            "tokens": self.token_totals() if self.events else "unavailable",
            "by_model": {
                model: dict(sorted(values.items()))
                for model, values in sorted(by_model.items())
            },
            "request_partitions": [
                {
                    "model": event.model,
                    "input": event.tokens.input,
                    "cache_read": event.tokens.cache_read,
                    "cache_write": event.tokens.cache_write,
                    "output": event.tokens.output,
                }
                for event in self.events
            ],
            "quality_flags": dict(sorted(quality_flags.items())),
            "producer_attempts": [attempt.to_dict() for attempt in self.attempts],
            "diagnostics": dict(self.diagnostics),
            "retrospective_candidates": [
                dict(candidate) for candidate in self.retrospective_candidates
            ],
        }
        if include_events:
            payload["events"] = [event.to_dict() for event in self.events]
        digest_input = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        payload["cohort_sha256"] = hashlib.sha256(digest_input).hexdigest()
        return payload


def _scalar(lines: Iterable[str], name: str) -> str | None:
    prefix = f"{name}:"
    for line in lines:
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value or None
    return None


def _source_digest(path: Path) -> str:
    digest = hashlib.sha256(b"pulse-computecost-structured-assignment-source-v1")
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        digest.update(b"\0unavailable")
    return "assignment_source_v1_" + digest.hexdigest()


def _yaml_documents(text: str) -> list[list[str]]:
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


def _load_assignment_inventory(
    harness_root: Path,
    work_item_ids: frozenset[str],
) -> _AssignmentInventory:
    """Join canonical lifecycle, Handoff record, acceptance, and outcomes."""

    created: dict[str, tuple[str, str]] = {}
    accepted: set[tuple[str, str, str]] = set()
    outcomes: set[tuple[str, str, str]] = set()
    refs: dict[str, set[str]] = defaultdict(set)
    conflicts: set[str] = set()
    for work_item_id in sorted(work_item_ids):
        event_path = harness_root / "events" / f"{work_item_id}.events.yaml"
        if not event_path.is_file() or event_path.is_symlink():
            continue
        try:
            documents = _yaml_documents(event_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        refs[work_item_id].add(_source_digest(event_path))
        for document in documents:
            if _document_scalar(document, "work_item_id", indent=0) != work_item_id:
                conflicts.add(work_item_id)
                continue
            event_type = _document_scalar(document, "event_type", indent=0)
            if event_type == "handoff.created":
                assignment = _document_scalar(document, "handoff_id", indent=2)
                purpose = _document_scalar(document, "purpose", indent=2)
                recipient = _document_scalar(document, "to_owner", indent=2)
                if (
                    assignment is None
                    or HANDOFF_RE.fullmatch(assignment) is None
                    or purpose != "outcome_verification"
                    or recipient is None
                ):
                    continue
                prior = created.get(assignment)
                if prior is not None and prior != (work_item_id, recipient):
                    conflicts.update((work_item_id, prior[0]))
                    continue
                created[assignment] = (work_item_id, recipient)
            elif event_type == "handoff.accepted":
                assignment = _document_scalar(document, "handoff_id", indent=2)
                operator = _document_scalar(document, "operator", indent=0)
                if (
                    assignment is not None
                    and HANDOFF_RE.fullmatch(assignment)
                    and operator is not None
                ):
                    accepted.add((assignment, work_item_id, operator))
            elif event_type in {
                "work_item.blocked",
                "work_item.outcome_accepted",
                "work_item.outcome_rejected",
            }:
                assignment = _document_scalar(
                    document, "verification_assignment", indent=2
                )
                verifier = _document_scalar(document, "verifier", indent=2)
                if (
                    assignment is not None
                    and HANDOFF_RE.fullmatch(assignment)
                    and verifier is not None
                ):
                    outcomes.add((assignment, work_item_id, verifier))

    valid_work_items: dict[str, str] = {}
    recipients: dict[str, str] = {}
    for assignment, (work_item_id, recipient) in sorted(created.items()):
        if (assignment, work_item_id, recipient) not in accepted:
            continue
        handoff_path = harness_root / "handoffs" / f"{assignment}.yaml"
        if not handoff_path.is_file() or handoff_path.is_symlink():
            conflicts.add(work_item_id)
            continue
        try:
            lines = handoff_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            conflicts.add(work_item_id)
            continue
        refs[work_item_id].add(_source_digest(handoff_path))
        if (
            _scalar(lines, "handoff_id") != assignment
            or _scalar(lines, "work_item_id") != work_item_id
            or _scalar(lines, "purpose") != "outcome_verification"
            or _scalar(lines, "to_owner") != recipient
            or _scalar(lines, "ack_state") not in {"accepted", "archived"}
        ):
            conflicts.add(work_item_id)
            continue
        valid_work_items[assignment] = work_item_id
        recipients[assignment] = recipient
    return _AssignmentInventory(
        work_items=valid_work_items,
        recipients=recipients,
        outcome_bound=frozenset(
            assignment
            for assignment, work_item_id, verifier in outcomes
            if valid_work_items.get(assignment) == work_item_id
            and recipients.get(assignment) == verifier
        ),
        source_refs={key: tuple(sorted(value)) for key, value in refs.items()},
        conflicts=frozenset(conflicts),
    )


def _structured_assignment_work_items(
    text: str,
    assignments: _AssignmentInventory,
) -> tuple[set[str], set[str]]:
    ids = {
        match.group(0).lower()
        for match in HANDOFF_RE.finditer(text)
        if match.group(0).lower() in assignments.work_items
    }
    return ({assignments.work_items[item] for item in ids}, ids)


def load_wi_window(harness_root: str | Path, work_item_id: str) -> WiWindow:
    if WI_RE.fullmatch(work_item_id) is None:
        raise WiDiscoveryError("work_item_id is invalid")
    root = Path(harness_root).expanduser().resolve()
    path = root / "work-items" / f"{work_item_id}.yaml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WiDiscoveryError("canonical Work Item is unavailable") from exc
    created = _scalar(lines, "created_at")
    updated = _scalar(lines, "updated_at")
    status = _scalar(lines, "status")
    if created is None or updated is None:
        raise WiDiscoveryError("canonical Work Item window is incomplete")
    if status not in {"completed", "failed", "cancelled", "superseded", "migrated"}:
        raise WiDiscoveryError("Work Item has no closed attribution window")
    started = parse_timestamp(created, "work_item.created_at")
    ended = parse_timestamp(updated, "work_item.updated_at")
    if ended < started:
        raise WiDiscoveryError("Work Item window is inverted")
    return WiWindow(
        work_item_id=work_item_id,
        started_at=started,
        ended_at=ended,
        source_ref=f"work-items/{work_item_id}.yaml",
    )


def load_work_item_inventory(harness_root: str | Path) -> frozenset[str]:
    work_items_dir = Path(harness_root).expanduser().resolve() / "work-items"
    try:
        candidates = tuple(work_items_dir.glob("wi_*.yaml"))
    except OSError as exc:
        raise WiDiscoveryError("canonical Work Item inventory is unavailable") from exc
    inventory = {
        path.stem.lower()
        for path in candidates
        if path.is_file() and WI_RE.fullmatch(path.stem.lower()) is not None
    }
    if not inventory:
        raise WiDiscoveryError("canonical Work Item inventory is empty")
    return frozenset(inventory)


def discover_harness_root(explicit: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    if os.environ.get("HARNESS_ROOT"):
        candidates.append(Path(os.environ["HARNESS_ROOT"]))
    if os.environ.get("PULSE_COEVOLUTION_ROOT"):
        candidates.append(Path(os.environ["PULSE_COEVOLUTION_ROOT"]) / ".harness")
    current = Path.cwd().resolve()
    for parent in (current, *current.parents):
        candidates.extend((parent / ".harness", parent.parent / ".harness"))
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "work-items").is_dir():
            return resolved
    return None


def _referenced_work_items(
    text: str, work_item_ids: frozenset[str]
) -> set[str]:
    """Canonicalize full/compact seeds and resolve short aliases by inventory."""

    referenced: set[str] = set()
    short_suffixes: list[str] = []
    for match in WI_SEED_REFERENCE_RE.finditer(text):
        full_date = match.group("full_date")
        compact_date = match.group("compact_date")
        if full_date is not None:
            referenced.add(f"wi_{full_date}_{match.group('full_suffix')}".lower())
        elif compact_date is not None:
            referenced.add(
                f"wi_{compact_date}_{match.group('compact_suffix')}".lower()
            )
        else:
            short_suffixes.append(str(match.group("short_suffix")))
    for suffix in short_suffixes:
        candidates = {
            work_item_id
            for work_item_id in work_item_ids
            if work_item_id.rsplit("_", 1)[-1] == suffix
        }
        explicit_candidates = referenced & candidates
        if len(candidates) == 1:
            referenced.update(candidates)
        elif len(explicit_candidates) == 1:
            # A full/compact identity in the same text disambiguates its short form.
            continue
        elif candidates:
            # Keep every candidate so each affected WI query sees the conflict.
            referenced.update(candidates)
        else:
            referenced.add(f"wi_short_unresolved_{suffix}")
    return referenced


def _session_index(
    path: Path,
    work_item_ids: frozenset[str],
    assignments: _AssignmentInventory,
) -> tuple[dict[str, str], dict[str, set[str]], dict[str, set[str]]]:
    titles: dict[str, str] = {}
    references: dict[str, set[str]] = defaultdict(set)
    assignment_refs: dict[str, set[str]] = defaultdict(set)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise WiDiscoveryError("Codex session index is unreachable") from exc
    for line in lines:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(row, Mapping):
            continue
        session_id = row.get("id")
        title = row.get("thread_name")
        if not isinstance(session_id, str) or not session_id:
            continue
        titles[session_id] = str(title) if isinstance(title, str) else "unindexed"
        if isinstance(title, str):
            references[session_id].update(_referenced_work_items(title, work_item_ids))
            assigned_wis, ids = _structured_assignment_work_items(title, assignments)
            references[session_id].update(assigned_wis)
            assignment_refs[session_id].update(ids)
    return titles, dict(references), dict(assignment_refs)


def _rollout_content_references(
    paths: Iterable[Path],
    work_item_ids: frozenset[str],
    assignments: _AssignmentInventory,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Find WI references only in user-authored Codex messages.

    Tool results and injected workspace instructions are intentionally excluded:
    a WI appearing in repository output is not execution attribution.
    """

    references: dict[str, set[str]] = defaultdict(set)
    assignment_refs: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        session_id: str | None = None
        path_references: set[str] = set()
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if (
                    '"session_meta"' not in line
                    and '"role":"user"' not in line
                    and '"role": "user"' not in line
                ):
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(row, Mapping):
                    continue
                payload = row.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                if row.get("type") == "session_meta":
                    raw_id = payload.get("id") or payload.get("session_id")
                    if isinstance(raw_id, str) and raw_id:
                        session_id = raw_id
                    continue
                if not (
                    row.get("type") == "response_item"
                    and payload.get("type") == "message"
                    and payload.get("role") == "user"
                ):
                    continue
                content = payload.get("content")
                if not isinstance(content, list):
                    continue
                text = " ".join(
                    str(item.get("text") or item.get("input_text") or "")
                    for item in content
                    if isinstance(item, Mapping)
                ).strip()
                if not text or text.startswith(
                    ("# AGENTS.md instructions", "<environment_context>")
                ):
                    continue
                path_references.update(_referenced_work_items(text, work_item_ids))
                assigned_wis, ids = _structured_assignment_work_items(text, assignments)
                path_references.update(assigned_wis)
                if session_id is not None:
                    assignment_refs[session_id].update(ids)
        if session_id is not None and path_references:
            references[session_id].update(path_references)
    return dict(references), dict(assignment_refs)


def _cc_content_seed_refs(
    paths: Iterable[Path],
    work_item_id: str,
    work_item_ids: frozenset[str],
) -> tuple[set[str], set[str]]:
    target = work_item_id.lower()
    matched_refs: set[str] = set()
    work_items_by_root: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"type":"user"' not in line and '"type": "user"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(row, Mapping) or row.get("type") != "user":
                    continue
                message = row.get("message")
                if not isinstance(message, Mapping) or message.get("role") != "user":
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        str(item.get("text") or "")
                        for item in content
                        if isinstance(item, Mapping)
                    )
                else:
                    continue
                lineage = cc_transcript_lineage(path, row)
                if lineage.root_run_ref == UNKNOWN:
                    continue
                root_ref = lineage.root_run_ref
                references = _referenced_work_items(text, work_item_ids)
                work_items_by_root[root_ref].update(references)
                if target in references:
                    matched_refs.add(root_ref)
    ambiguous = {
        root_ref
        for root_ref in matched_refs
        if work_items_by_root[root_ref] - {target}
    }
    return matched_refs - ambiguous, ambiguous


def _delegation_graph(paths: Iterable[Path]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        session_id: str | None = None
        sources: set[str] = set()
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"session_meta"' not in line and "<source_thread_id>" not in line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(row, Mapping):
                    continue
                payload = row.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                if row.get("type") == "session_meta":
                    raw_id = payload.get("id") or payload.get("session_id")
                    if isinstance(raw_id, str) and raw_id:
                        session_id = raw_id
                    direct_parent = payload.get("parent_thread_id")
                    if isinstance(direct_parent, str) and direct_parent:
                        sources.add(direct_parent)
                    source = payload.get("source")
                    if isinstance(source, Mapping):
                        subagent = source.get("subagent")
                        if isinstance(subagent, Mapping):
                            spawn = subagent.get("thread_spawn")
                            if isinstance(spawn, Mapping):
                                nested_parent = spawn.get("parent_thread_id")
                                if isinstance(nested_parent, str) and nested_parent:
                                    sources.add(nested_parent)
                if (
                    row.get("type") == "response_item"
                    and payload.get("type") == "message"
                    and payload.get("role") == "user"
                ):
                    content = payload.get("content")
                    if not isinstance(content, list):
                        continue
                    for item in content:
                        if not isinstance(item, Mapping):
                            continue
                        text = item.get("text")
                        if (
                            isinstance(text, str)
                            and STRUCTURED_LINEAGE_PREFIX_RE.match(text.lstrip())
                        ):
                            sources.update(SOURCE_THREAD_RE.findall(text))
        if session_id is None:
            continue
        graph.setdefault(session_id, set())
        for source in sources:
            graph[session_id].add(source)
            graph[source].add(session_id)
    return graph


def _closure(seeds: Iterable[str], graph: Mapping[str, set[str]]) -> set[str]:
    result = set(seeds)
    pending = deque(result)
    while pending:
        current = pending.popleft()
        for related in graph.get(current, set()):
            if related not in result:
                result.add(related)
                pending.append(related)
    return result


def _codex_seed_components(
    work_item_id: str,
    references_by_session: Mapping[str, set[str]],
    graph: Mapping[str, set[str]],
) -> tuple[set[str], set[str], set[str], int]:
    """Partition target seeds into clean or tainted root/delegation components."""

    target = work_item_id.lower()
    target_seeds = {
        session_id
        for session_id, references in references_by_session.items()
        if target in references
    }
    clean_seeds: set[str] = set()
    clean_session_ids: set[str] = set()
    ambiguous_seeds: set[str] = set()
    evaluated_seeds: set[str] = set()
    ambiguous_component_count = 0
    for seed in sorted(target_seeds):
        if seed in evaluated_seeds:
            continue
        component = _closure((seed,), graph)
        component_target_seeds = target_seeds & component
        evaluated_seeds.update(component_target_seeds)
        component_references: set[str] = set()
        for session_id in component:
            component_references.update(references_by_session.get(session_id, set()))
        if component_references - {target}:
            ambiguous_seeds.update(component_target_seeds)
            ambiguous_component_count += 1
        else:
            clean_seeds.update(component_target_seeds)
            clean_session_ids.update(component)
    return (
        clean_seeds,
        clean_session_ids,
        ambiguous_seeds,
        ambiguous_component_count,
    )


def _request_limits(paths: Iterable[Path], session_ids: set[str]) -> dict[str, int | None]:
    max_request_input = 0
    max_context_window = 0
    observed_request = False
    observed_window = False
    for path in paths:
        selected = False
        rows: list[Mapping[str, Any]] = []
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"session_meta"' not in line and '"token_count"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(row, Mapping):
                    continue
                rows.append(row)
                payload = row.get("payload")
                if row.get("type") == "session_meta" and isinstance(payload, Mapping):
                    raw_id = payload.get("id") or payload.get("session_id")
                    if raw_id in session_ids:
                        selected = True
        if not selected:
            continue
        for row in rows:
            payload = row.get("payload")
            if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, Mapping):
                continue
            last = info.get("last_token_usage")
            if isinstance(last, Mapping):
                value = last.get("input_tokens")
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    max_request_input = max(max_request_input, value)
                    observed_request = True
            value = info.get("model_context_window")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                max_context_window = max(max_context_window, value)
                observed_window = True
    return {
        "max_request_input_tokens": max_request_input if observed_request else None,
        "max_model_context_window": max_context_window if observed_window else None,
    }


def discover_codex_wi_usage(
    *,
    work_item_id: str,
    window: WiWindow,
    machine: str,
    observed_at: str,
    codex_home: str | Path | None = None,
    model_catalog_path: str | Path | None = None,
    work_item_ids: frozenset[str],
    assignments: _AssignmentInventory,
) -> WiUsageCohort:
    home = Path(codex_home or os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    index_path = home / "session_index.jsonl"
    producer_dirs = tuple(
        path for path in (home / "sessions", home / "archived_sessions")
        if path.is_dir() and not path.is_symlink()
    )
    safe_home_ref = "codex_home_v1_" + hashlib.sha256(str(home.resolve()).encode()).hexdigest()
    titles: dict[str, str] = {}
    indexed_references: dict[str, set[str]] = {}
    indexed_assignments: dict[str, set[str]] = {}
    if index_path.is_file() and not index_path.is_symlink():
        titles, indexed_references, indexed_assignments = _session_index(
            index_path, work_item_ids, assignments
        )
    paths = sorted({path for root in producer_dirs for path in root.rglob("*.jsonl")})
    content_references, content_assignments = _rollout_content_references(
        paths, work_item_ids, assignments
    )
    references_by_session: dict[str, set[str]] = defaultdict(set)
    for evidence in (indexed_references, content_references):
        for session_id, references in evidence.items():
            references_by_session[session_id].update(references)
    target_assignments = assignments.target_ids(work_item_id)
    structured_sessions = {
        session_id
        for evidence in (indexed_assignments, content_assignments)
        for session_id, assignment_ids in evidence.items()
        if assignment_ids & target_assignments
    }
    target = work_item_id.lower()
    indexed_seeds = {
        session_id
        for session_id, references in indexed_references.items()
        if target in references
    }
    content_seeds = {
        session_id
        for session_id, references in content_references.items()
        if target in references
    }
    graph = _delegation_graph(paths)
    missing_structured_rollouts = structured_sessions - set(graph)
    (
        seeds,
        session_ids,
        ambiguous_seeds,
        ambiguous_component_count,
    ) = _codex_seed_components(work_item_id, references_by_session, graph)
    if not seeds:
        conflict = bool(ambiguous_seeds) or work_item_id in assignments.conflicts
        assignment_observed = bool(target_assignments)
        status = "join_conflict" if conflict else "partial" if assignment_observed else "producer_missing"
        attempt = ProducerAttempt(
            producer="codex_desktop",
            status=status,
            adapters=(
                "harness_wi_lifecycle", "harness_verifier_handoff",
                "harness_verifier_outcome", "codex_session_index",
                "codex_structured_assignment", "codex_user_prompt_seed",
                "codex_rollout", "codex_archived_rollout",
            ),
            reason=(
                "Codex root/delegation component contains multiple WorkItem assignments; no mixed component is attributed"
                if conflict
                else "canonical verifier assignments were observed, but no exact assigned Codex session Token producer remains"
                if assignment_observed
                else "Codex producer exists but no normalized WI seed or structured assignment was discovered"
            ),
            source_refs=tuple(sorted({safe_home_ref, *assignments.source_refs.get(work_item_id, ())})),
        )
        assignment_summary = assignments.summary(
            work_item_id,
            session_count=(len(structured_sessions) if structured_sessions else "unavailable"),
            token_coverage="unavailable",
        )
        return WiUsageCohort(work_item_id, window, (), (), (attempt,), {
            "seed_thread_count": 0,
            "indexed_seed_count": len(indexed_seeds),
            "content_seed_count": len(content_seeds),
            "structured_assignment_seed_count": len(structured_sessions),
            "ambiguous_seed_count": len(ambiguous_seeds),
            "ambiguous_component_count": ambiguous_component_count,
            "candidate_evidence": {
                "status": (
                    "present_but_unpartitioned" if conflict
                    else "assigned_tokens_unavailable" if assignment_observed
                    else "absent"
                ),
                "seed_count": len(ambiguous_seeds),
                "component_count": ambiguous_component_count,
                "assigned_event_count": 0,
            },
            "canonical_verifier_assignment": assignment_summary,
            "indexed_title_count": len(titles),
            "session_index_available": index_path.is_file(),
            "active_rollout_file_count": sum("sessions" in path.parts and "archived_sessions" not in path.parts for path in paths),
            "archived_rollout_file_count": sum("archived_sessions" in path.parts for path in paths),
            "partition_evidence_consumed": assignment_observed,
        })
    collection: UsageCollection = collect_usage_events(
        machine=machine,
        observed_at=observed_at,
        codex_sessions_dir=producer_dirs,
        model_catalog_path=model_catalog_path,
        since=window.started_at.isoformat(),
        codex_required_session_ids=frozenset(session_ids),
    )
    root_refs = {_lineage_run_ref(session_id) for session_id in session_ids}
    events = tuple(
        replace(event, work_item_ref=work_item_id)
        for event in collection.events
        if event.lineage.root_run_ref in root_refs
        and window.started_at <= event.occurred_utc <= window.ended_at
    )
    observed_roots = tuple(sorted({event.lineage.root_run_ref for event in events}))
    if not events and structured_sessions:
        status = "partial"
        reason = "assigned Codex sessions were observed, but their rollout Token evidence is unavailable"
    elif not events:
        status = "join_conflict"
        reason = "WI seeds were discovered but no usage events joined the canonical window"
    elif ambiguous_seeds or missing_structured_rollouts:
        status = "partial"
        reason = (
            "assigned usage joined, but one or more assigned session Token rollouts are unavailable"
            if missing_structured_rollouts
            else "uniquely assigned usage joined; mixed WorkItem components were excluded"
        )
    else:
        status = "observed"
        reason = "canonical assignment and WI seeds joined delegation/rollover lineage and rollout usage"
    attempt = ProducerAttempt(
        producer="codex_desktop",
        status=status,
        adapters=(
            "harness_wi_lifecycle", "harness_verifier_handoff",
            "harness_verifier_outcome", "codex_session_index",
            "codex_structured_assignment", "codex_user_prompt_seed",
            "codex_rollout", "codex_archived_rollout",
        ),
        reason=reason,
        source_refs=tuple(sorted({safe_home_ref, *assignments.source_refs.get(work_item_id, ())})),
    )
    diagnostics = collection.diagnostics.to_dict()
    diagnostics.update(
        {
            "seed_thread_count": len(seeds),
            "indexed_seed_count": len(indexed_seeds),
            "content_seed_count": len(content_seeds),
            "structured_assignment_seed_count": len(structured_sessions),
            "missing_assigned_rollout_count": len(missing_structured_rollouts),
            "ambiguous_seed_count": len(ambiguous_seeds),
            "ambiguous_component_count": ambiguous_component_count,
            "lineage_closure_count": len(session_ids),
            "indexed_title_count": len(titles),
            "session_index_available": index_path.is_file(),
            "active_rollout_file_count": sum("sessions" in path.parts and "archived_sessions" not in path.parts for path in paths),
            "archived_rollout_file_count": sum("archived_sessions" in path.parts for path in paths),
            "canonical_verifier_assignment": assignments.summary(
                work_item_id,
                session_count=(len(structured_sessions) if structured_sessions else "unavailable"),
                token_coverage=(
                    "partial" if events and missing_structured_rollouts
                    else "observed" if events
                    else "unavailable"
                ),
            ),
            "candidate_evidence": {
                "status": (
                    "present_but_unpartitioned" if ambiguous_seeds
                    else "assigned_tokens_unavailable"
                    if structured_sessions and not events
                    else "observed"
                ),
                "seed_count": len(seeds),
                "component_count": ambiguous_component_count,
                "assigned_event_count": len(events),
            },
            "partition_evidence_consumed": bool(target_assignments),
            **_request_limits(paths, session_ids),
        }
    )
    return WiUsageCohort(
        work_item_id=work_item_id,
        window=window,
        events=events,
        root_thread_refs=observed_roots,
        attempts=(attempt,),
        diagnostics=diagnostics,
    )


def _scl_cc_epoch(
    epochs: Iterable[object], baseline: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """The Harness-recorded CC epoch that accepted this baseline under Host Session authority."""
    for epoch in epochs:
        if not isinstance(epoch, Mapping) or epoch.get("runtime") not in {"cc", "cc_sdk"}:
            continue
        if (epoch.get("agent_id"), epoch.get("execution_epoch"), epoch.get("phase")) != (
            baseline["agent_id"], baseline["epoch"], baseline["phase"]
        ):
            continue
        sealed = epoch.get("cc_usage")
        if isinstance(sealed, Mapping) and (
            sealed.get("runtime_binding_sha256") != baseline["runtime_binding_sha256"]
            or sealed.get("baseline_snapshot_sha256") != baseline["snapshot_sha256"]
        ):
            continue
        return epoch
    return None


def _cc_binding_intervals(
    root: Path,
    work_item_id: str,
    *,
    scl_epochs: Iterable[object],
    window_end: datetime,
) -> dict[str, list[tuple[datetime, datetime]]]:
    """Relay 为 WI 绑定的 CC 会话写下的有界快照：root lineage -> [baseline, end]。

    只认可通过 schema/摘要验证、文件名与绑定一致、transcript 文件名哈希等于
    快照 authority、且 SCL 已在 Host Session 授权下接受该 baseline 的会话；
    不以 prompt 文本推断归属。未封存区间截止于同一会话的下一个绑定 baseline
    （任何 WI），否则截止于 WI 窗口终点，从不无界。
    """
    epochs = list(scl_epochs)
    baselines: list[tuple[Path, Mapping[str, Any], datetime]] = []
    for baseline_path in sorted(root.glob("*/compute-cost/wi_*-baseline.json")):
        try:
            if baseline_path.is_symlink():
                continue
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            validate_cc_session_snapshot(baseline)
            if baseline_path.name != (
                f"{baseline['work_item_id']}-{baseline['runtime_binding_sha256']}-baseline.json"
            ):
                continue
            baselines.append((baseline_path, baseline, parse_timestamp(baseline["generated_at"], "baseline")))
        except (OSError, ValueError, KeyError, CcUsageError):
            continue
    intervals: dict[str, list[tuple[datetime, datetime]]] = {}
    for baseline_path, baseline, start in baselines:
        if baseline["work_item_id"] != work_item_id:
            continue
        epoch = _scl_cc_epoch(epochs, baseline)
        if epoch is None:
            continue  # 没有 SCL/Host 接受的同一授权，不凭本地文件归属。
        later = [
            other_start for path, other, other_start in baselines
            if path.parent == baseline_path.parent and other["authority_ref"] == baseline["authority_ref"]
            and other_start > start
        ]
        end = min([window_end, *later])
        sealed_path = baseline_path.with_name(baseline_path.name.replace("-baseline.json", "-sealed.json"))
        try:
            if sealed_path.is_file() and not sealed_path.is_symlink():
                sealed = json.loads(sealed_path.read_text(encoding="utf-8"))
                validate_cc_session_snapshot(sealed)
                cc_usage = epoch.get("cc_usage")
                if sealed["attribution_boundary"]["baseline_snapshot_sha256"] != baseline["snapshot_sha256"] or (
                    isinstance(cc_usage, Mapping) and cc_usage.get("final_snapshot_sha256") != sealed["snapshot_sha256"]
                ):
                    continue
                end = min(end, parse_timestamp(sealed["generated_at"], "sealed.generated_at"))
        except (OSError, ValueError, KeyError, CcUsageError):
            continue
        for transcript in baseline_path.parent.parent.glob("*.jsonl"):
            if cc_authority_ref(transcript.stem) == baseline["authority_ref"]:
                intervals.setdefault(_root_run_ref(transcript.stem), []).append((start, end))
    return intervals


def _cc_bound_cohort(
    *,
    work_item_id: str,
    window: WiWindow,
    machine: str,
    observed_at: str,
    root: Path,
    safe_root_ref: str,
    model_catalog_path: str | Path | None,
    bound: Mapping[str, list[tuple[datetime, datetime]]],
    source_file_count: int,
) -> WiUsageCohort | None:
    collection = collect_usage_events(
        machine=machine,
        observed_at=observed_at,
        cc_projects_dir=root,
        model_catalog_path=model_catalog_path,
        since=window.started_at.isoformat(),
    )
    events = tuple(
        replace(event, work_item_ref=work_item_id)
        for event in collection.events
        if event.lineage.root_run_ref in bound
        and window.started_at <= event.occurred_utc <= window.ended_at
        and any(start <= event.occurred_utc <= end for start, end in bound[event.lineage.root_run_ref])
    )
    if not events:
        return None
    attempt = ProducerAttempt(
        producer="claude_code",
        status="observed",
        adapters=("cc_usage_binding", "cc_transcript"),
        reason="Relay-bound Claude Code sessions joined their bounded usage snapshots",
        source_refs=(safe_root_ref,),
    )
    diagnostics = collection.diagnostics.to_dict()
    diagnostics.update({
        "source_file_count": source_file_count,
        "bound_session_count": len(bound),
        "partition_evidence_consumed": True,
    })
    return WiUsageCohort(
        work_item_id=work_item_id,
        window=window,
        events=events,
        root_thread_refs=tuple(sorted({event.lineage.root_run_ref for event in events})),
        attempts=(attempt,),
        diagnostics=diagnostics,
    )


def discover_cc_wi_usage(
    *,
    work_item_id: str,
    window: WiWindow,
    machine: str,
    observed_at: str,
    cc_projects_dir: str | Path | None = None,
    model_catalog_path: str | Path | None = None,
    work_item_ids: frozenset[str],
    scl_measurement_snapshot: Mapping[str, Any] | None = None,
) -> WiUsageCohort:
    root = Path(
        cc_projects_dir
        or os.environ.get("CLAUDE_PROJECTS_DIR")
        or (Path.home() / ".claude" / "projects")
    )
    safe_root_ref = "cc_projects_v1_" + hashlib.sha256(
        str(root.expanduser().resolve()).encode()
    ).hexdigest()
    if not root.is_dir():
        attempt = ProducerAttempt(
            producer="claude_code",
            status="producer_missing",
            adapters=("cc_user_prompt_seed", "cc_transcript"),
            reason="registered local Claude Code producer evidence was not found",
            source_refs=(safe_root_ref,),
        )
        return WiUsageCohort(work_item_id, window, (), (), (attempt,), {})
    paths = sorted(root.rglob("*.jsonl"))
    scl_epochs = scl_measurement_snapshot.get("epochs") if scl_measurement_snapshot else None
    bound = _cc_binding_intervals(
        root, work_item_id,
        scl_epochs=scl_epochs if isinstance(scl_epochs, list) else (),
        window_end=window.ended_at,
    )
    if bound:
        bound_cohort = _cc_bound_cohort(
            work_item_id=work_item_id, window=window, machine=machine, observed_at=observed_at,
            root=root, safe_root_ref=safe_root_ref, model_catalog_path=model_catalog_path,
            bound=bound, source_file_count=len(paths),
        )
        if bound_cohort is not None:
            return bound_cohort
    seed_refs, ambiguous_refs = _cc_content_seed_refs(
        paths, work_item_id, work_item_ids
    )
    if not seed_refs:
        conflict = bool(ambiguous_refs)
        attempt = ProducerAttempt(
            producer="claude_code",
            status="join_conflict" if conflict else "producer_missing",
            adapters=("cc_user_prompt_seed", "cc_transcript"),
            reason=(
                "Claude Code root contains multiple WorkItem seeds; no partition evidence adapter is implemented"
                if conflict
                else "Claude Code producer exists but no normalized WI seed was discovered"
            ),
            source_refs=(safe_root_ref,),
        )
        return WiUsageCohort(work_item_id, window, (), (), (attempt,), {
            "source_file_count": len(paths),
            "content_seed_count": 0,
            "ambiguous_seed_count": len(ambiguous_refs),
            "partition_evidence_consumed": False,
        })
    collection = collect_usage_events(
        machine=machine,
        observed_at=observed_at,
        cc_projects_dir=root,
        model_catalog_path=model_catalog_path,
        since=window.started_at.isoformat(),
    )
    events = tuple(
        replace(event, work_item_ref=work_item_id)
        for event in collection.events
        if event.lineage.root_run_ref in seed_refs
        and window.started_at <= event.occurred_utc <= window.ended_at
    )
    observed_roots = tuple(sorted({event.lineage.root_run_ref for event in events}))
    status = "partial" if events and ambiguous_refs else ("observed" if events else "join_conflict")
    reason = (
        "uniquely seeded Claude Code usage joined; multi-WorkItem sessions were excluded"
        if events and ambiguous_refs
        else (
            "normalized WI user prompts joined Claude Code transcript usage"
            if events
            else "WI user prompt seeds were discovered but no Claude Code usage joined the canonical window"
        )
    )
    attempt = ProducerAttempt(
        producer="claude_code",
        status=status,
        adapters=("cc_user_prompt_seed", "cc_transcript"),
        reason=reason,
        source_refs=(safe_root_ref,),
    )
    diagnostics = collection.diagnostics.to_dict()
    diagnostics.update({
        "source_file_count": len(paths),
        "content_seed_count": len(seed_refs),
        "ambiguous_seed_count": len(ambiguous_refs),
        "partition_evidence_consumed": False,
    })
    return WiUsageCohort(
        work_item_id=work_item_id,
        window=window,
        events=events,
        root_thread_refs=observed_roots,
        attempts=(attempt,),
        diagnostics=diagnostics,
    )


def _not_applicable_cohort(
    work_item_id: str,
    window: WiWindow,
    *,
    producer: str,
    adapters: tuple[str, ...],
    reason: str,
) -> WiUsageCohort:
    return WiUsageCohort(
        work_item_id,
        window,
        (),
        (),
        (ProducerAttempt(producer, "not_applicable", adapters, reason),),
        {},
    )


def _pi_source(procurement_source: object) -> str:
    return {
        "chatgpt_subscription": "chatgpt_pro",
        "claude_subscription": "claude_subscription",
        "openrouter_api": "openrouter_api",
    }.get(str(procurement_source), UNKNOWN)


def discover_pi_scl_wi_usage(
    *,
    work_item_id: str,
    window: WiWindow,
    machine: str,
    observed_at: str,
    scl_measurement_snapshot: Mapping[str, Any],
) -> WiUsageCohort:
    """Join only Host-bound, baseline/final-paired Pi SCL measurement epochs."""

    epochs = scl_measurement_snapshot.get("epochs")
    if not isinstance(epochs, list):
        raise WiDiscoveryError("canonical SCL measurement epochs are unavailable")
    expected = [
        epoch for epoch in epochs
        if isinstance(epoch, Mapping) and epoch.get("runtime") == "pi"
    ]
    adapters = (
        "scl_cost_attribution",
        "scl_registered_surface",
        "scl_pi_baseline_final",
        "scl_final_token_seal",
    )
    if not expected:
        return _not_applicable_cohort(
            work_item_id,
            window,
            producer="pi_managed_scl",
            adapters=adapters,
            reason="no canonical SCL epoch expects managed Pi measurement; unmanaged Pi is not measured",
        )

    events: list[UsageEvent] = []
    roots: set[str] = set()
    observed_epochs = 0
    missing_epochs = 0
    surfaces: set[str] = set()
    baseline_digests: list[str] = []
    final_digests: list[str] = []
    excluded_pre_baseline = 0
    deduplicated_entries = 0
    for epoch in expected:
        pi_usage = epoch.get("pi_usage")
        if not isinstance(pi_usage, Mapping):
            missing_epochs += 1
            continue
        if pi_usage.get("status") != "exact_bounded_sealed":
            raise WiDiscoveryError("canonical Pi SCL measurement is not finally sealed")
        summary = pi_usage.get("summary")
        requests = summary.get("request_partitions") if isinstance(summary, Mapping) else None
        if not isinstance(requests, list):
            raise WiDiscoveryError("canonical Pi SCL request partitions are unavailable")
        observed_epochs += 1
        surface = str(pi_usage.get("execution_surface"))
        surfaces.add(surface)
        baseline_digests.append(str(pi_usage.get("baseline_snapshot_sha256")))
        final_digests.append(str(pi_usage.get("final_snapshot_sha256")))
        boundary = pi_usage.get("attribution_boundary")
        if isinstance(boundary, Mapping):
            raw_excluded = boundary.get("excluded_pre_baseline_usage_events")
            raw_deduplicated = boundary.get("deduplicated_entries")
            if isinstance(raw_excluded, int) and not isinstance(raw_excluded, bool):
                excluded_pre_baseline += raw_excluded
            if isinstance(raw_deduplicated, int) and not isinstance(raw_deduplicated, bool):
                deduplicated_entries += raw_deduplicated
        root_ref = str(epoch.get("lineage_root_ref"))
        roots.add(root_ref)
        for index, request in enumerate(requests):
            if not isinstance(request, Mapping):
                raise WiDiscoveryError("canonical Pi SCL request partition is malformed")
            fact_ref = request.get("fact_ref")
            if not isinstance(fact_ref, str) or not fact_ref.startswith("pi_usage_v1_"):
                raise WiDiscoveryError("canonical Pi SCL usage fact identity is invalid")
            event_id = "uev1_" + hashlib.sha256(
                f"computecost-pi-scl-event-v1\0{pi_usage.get('runtime_binding_sha256')}\0{fact_ref}".encode()
            ).hexdigest()
            occurred_at = str(request.get("occurred_at"))
            reasoning = request.get("reasoning")
            events.append(UsageEvent(
                event_id=event_id,
                occurred_at=occurred_at,
                observed_at=observed_at,
                machine=machine,
                agent=str(epoch.get("agent_id")),
                consumer_path="managed_pi_scl",
                source=_pi_source(
                    epoch.get("usage", {}).get("procurement_source")
                    if isinstance(epoch.get("usage"), Mapping)
                    else UNKNOWN
                ),
                runtime="pi",
                provider=str(request.get("provider")),
                model=str(request.get("model")),
                effort=str(request.get("reasoning_effort")),
                tokens=TokenUsage(
                    # Pi summary request.input is already normalized to native
                    # uncached + cache-read + cache-write exactly once.
                    input=int(request.get("input")),
                    output=int(request.get("output")),
                    cache_read=int(request.get("cache_read")),
                    cache_write=int(request.get("cache_write")),
                    reasoning=None if reasoning is None else int(reasoning),
                ),
                work_item_ref=work_item_id,
                adapter="pi_scl_bounded",
                source_partition=surface,
                source_position=index,
                lineage=Lineage(
                    root_agent=UNKNOWN,
                    execution_mode=UNKNOWN,
                    session_role=UNKNOWN,
                    root_run_ref=root_ref,
                ),
            ))
    if not observed_epochs:
        status = "producer_missing"
        reason = "managed Pi is expected by SCL attribution, but no paired baseline/final Token seal was produced"
    elif missing_epochs:
        status = "partial"
        reason = "some SCL Pi epochs have bounded final usage while other expected epochs lack a final pair"
    else:
        status = "observed"
        reason = "paired Pi baseline/final positions joined exact SCL attribution and registered surface evidence"
    attempt = ProducerAttempt(
        producer="pi_managed_scl",
        status=status,
        adapters=adapters,
        reason=reason,
        source_refs=tuple(
            f"pi_measurement_v1_{digest}" for digest in sorted(final_digests)
        ),
    )
    return WiUsageCohort(
        work_item_id=work_item_id,
        window=window,
        events=tuple(events),
        root_thread_refs=tuple(sorted(roots if events else set())),
        attempts=(attempt,),
        diagnostics={
            "expected_epoch_count": len(expected),
            "observed_epoch_count": observed_epochs,
            "missing_final_epoch_count": missing_epochs,
            "registered_surfaces": sorted(surfaces),
            "baseline_snapshot_sha256": sorted(baseline_digests),
            "final_snapshot_sha256": sorted(final_digests),
            "excluded_pre_baseline_usage_events": excluded_pre_baseline,
            "deduplicated_entries": deduplicated_entries,
            "post_seal_entries_included": 0,
            "partition_evidence_consumed": True,
        },
    )


def _combine_cohorts(cohorts: Iterable[WiUsageCohort]) -> WiUsageCohort:
    rows = tuple(cohorts)
    if not rows:
        raise WiDiscoveryError("at least one producer cohort is required")
    if len(rows) == 1:
        return rows[0]
    first = rows[0]
    if any(row.work_item_id != first.work_item_id or row.window != first.window for row in rows):
        raise WiDiscoveryError("producer cohorts belong to different Work Items")
    events_by_id = {
        event.event_id: event
        for row in rows
        for event in row.events
    }
    diagnostics: dict[str, Any] = {
        "by_producer": {
            attempt.producer: dict(row.diagnostics)
            for row in rows
            for attempt in row.attempts
        }
    }
    # Preserve the established single-producer diagnostic read model while
    # adding typed not-applicable attempts for other runtimes.
    nonempty_diagnostics = [row.diagnostics for row in rows if row.diagnostics]
    for row_diagnostics in nonempty_diagnostics:
        for key, value in row_diagnostics.items():
            if sum(key in item for item in nonempty_diagnostics) == 1:
                diagnostics[key] = value
    request_limits = [
        row.diagnostics.get("max_request_input_tokens")
        for row in rows
        if isinstance(row.diagnostics.get("max_request_input_tokens"), int)
    ]
    context_windows = [
        row.diagnostics.get("max_model_context_window")
        for row in rows
        if isinstance(row.diagnostics.get("max_model_context_window"), int)
    ]
    diagnostics["max_request_input_tokens"] = (
        max(request_limits) if request_limits else None
    )
    diagnostics["max_model_context_window"] = (
        max(context_windows) if context_windows else None
    )
    return WiUsageCohort(
        work_item_id=first.work_item_id,
        window=first.window,
        events=tuple(events_by_id[key] for key in sorted(events_by_id)),
        root_thread_refs=tuple(sorted({ref for row in rows for ref in row.root_thread_refs})),
        attempts=tuple(attempt for row in rows for attempt in row.attempts),
        diagnostics=diagnostics,
        retrospective_candidates=tuple(
            candidate
            for row in rows
            for candidate in row.retrospective_candidates
        ),
    )


def auto_discover_wi_usage(
    work_item_id: str,
    *,
    machine: str,
    observed_at: str | None = None,
    harness_root: str | Path | None = None,
    codex_home: str | Path | None = None,
    cc_projects_dir: str | Path | None = None,
    model_catalog_path: str | Path | None = None,
    scl_measurement_snapshot: Mapping[str, Any] | None = None,
    surface_registry_path: str | Path | None = None,
    relay_config_path: str | Path | None = None,
    relay_state_path: str | Path | None = None,
    relay_pi_manifest_path: str | Path | None = None,
    relay_metering_dir: str | Path | None = None,
) -> WiUsageCohort:
    root = discover_harness_root(harness_root)
    if root is None:
        raise WiDiscoveryError("canonical Harness root could not be discovered")
    window = load_wi_window(root, work_item_id)
    work_item_ids = load_work_item_inventory(root)
    assignments = _load_assignment_inventory(root, work_item_ids)
    observed = observed_at or datetime.now(timezone.utc).isoformat()
    if scl_measurement_snapshot is None:
        scl_measurement_snapshot = load_scl_measurement_snapshot(
            root,
            work_item_id,
            surface_registry_path=(
                Path(surface_registry_path) if surface_registry_path is not None else None
            ),
        )
    else:
        scl_measurement_snapshot = validate_scl_measurement_snapshot(
            scl_measurement_snapshot,
            expected_work_item_id=work_item_id,
        )
    expected_runtimes = {
        str(epoch.get("runtime"))
        for epoch in scl_measurement_snapshot.get("epochs", [])
        if isinstance(epoch, Mapping)
    }
    if (
        expected_runtimes
        and "codex" not in expected_runtimes
        and not assignments.target_ids(work_item_id)
    ):
        cohorts = [_not_applicable_cohort(
            work_item_id,
            window,
            producer="codex_desktop",
            adapters=("codex_session_index", "codex_user_prompt_seed", "codex_rollout"),
            reason="canonical SCL attribution assigns this WI to another runtime",
        )]
    else:
        cohorts = [discover_codex_wi_usage(
            work_item_id=work_item_id,
            window=window,
            machine=machine,
            observed_at=observed,
            codex_home=codex_home,
            model_catalog_path=model_catalog_path,
            work_item_ids=work_item_ids,
            assignments=assignments,
        )]
    pi_cohort = discover_pi_scl_wi_usage(
        work_item_id=work_item_id,
        window=window,
        machine=machine,
        observed_at=observed,
        scl_measurement_snapshot=scl_measurement_snapshot,
    )
    sealed_pi_exists = any(
        isinstance(epoch, Mapping) and isinstance(epoch.get("pi_usage"), Mapping)
        for epoch in scl_measurement_snapshot.get("epochs", [])
    )
    lifecycle_events_available = (
        root / "events" / f"{work_item_id}.events.yaml"
    ).is_file()
    if not sealed_pi_exists and lifecycle_events_available:
        try:
            retrospective = discover_relay_pi_retrospective(
                work_item_id=work_item_id,
                harness_root=root,
                relay_config_path=relay_config_path,
                relay_state_path=relay_state_path,
                relay_pi_manifest_path=relay_pi_manifest_path,
                relay_metering_dir=relay_metering_dir,
            )
        except RetrospectiveUsageError as exc:
            raise WiDiscoveryError(str(exc)) from exc
        retrospective_available = retrospective["status"] in {"available", "partial"}
        pi_cohort = replace(
            pi_cohort,
            attempts=(*pi_cohort.attempts, ProducerAttempt(
                producer="relay_tg_pi_retrospective",
                status=(
                    "retrospective_unsealed" if retrospective_available
                    else "join_conflict" if retrospective["status"] in {"ambiguous_mapping", "source_unreadable"}
                    else "producer_missing"
                ),
                adapters=(
                    "relay_registration", "relay_state", "harness_lifecycle_windows",
                    "pi_jsonl_retrospective", "relay_metering_corroboration",
                ),
                reason=(
                    "registered Relay/TG Pi evidence is available only as an unsealed retrospective window estimate"
                    if retrospective_available
                    else "registered Relay/TG Pi retrospective evidence is incomplete or unavailable"
                ),
                source_refs=tuple(retrospective["source_refs"]),
            )),
            retrospective_candidates=(retrospective,),
        )
    cohorts.append(pi_cohort)
    if cc_projects_dir is not None or os.environ.get("CLAUDE_PROJECTS_DIR"):
        cohorts.append(discover_cc_wi_usage(
            work_item_id=work_item_id,
            window=window,
            machine=machine,
            observed_at=observed,
            cc_projects_dir=cc_projects_dir,
            model_catalog_path=model_catalog_path,
            work_item_ids=work_item_ids,
            scl_measurement_snapshot=scl_measurement_snapshot,
        ))
    return _combine_cohorts(cohorts)
