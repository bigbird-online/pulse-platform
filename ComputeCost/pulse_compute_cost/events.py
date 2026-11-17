"""Canonical, identity-safe usage events for ComputeCost."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .token_semantics import InputTokenClasses, TokenSemanticsError


BEIJING = ZoneInfo("Asia/Shanghai")
UNKNOWN = "unknown"
KNOWN_SOURCES = frozenset({"chatgpt_pro", "claude_subscription", "openrouter_api", UNKNOWN})
KNOWN_RUNTIMES = frozenset({"cc", "codex", "pi", UNKNOWN})
KNOWN_EXECUTION_MODES = frozenset({"human", "automatic", UNKNOWN})
KNOWN_SESSION_ROLES = frozenset({"main", "sub_agent", UNKNOWN})
PRIVATE_REF_PATTERNS = {
    "root_run_ref": re.compile(r"^lineage_v1_[0-9a-f]{64}$"),
    "parent_run_ref": re.compile(r"^lineage_v1_[0-9a-f]{64}$"),
    "cron_job_ref": re.compile(r"^cron_v1_[0-9a-f]{64}$"),
    "scan_ref": re.compile(r"^scan_v1_[0-9a-f]{64}$"),
}
LINEAGE_FIELDS = (
    "root_agent",
    "execution_mode",
    "session_role",
    "root_run_ref",
    "parent_run_ref",
    "cron_job_ref",
    "scan_ref",
)


class UsageEventError(ValueError):
    """Raised when an event would require invented or inconsistent evidence."""


def parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise UsageEventError(f"{field_name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageEventError(f"{field_name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UsageEventError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise UsageEventError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def beijing_month(value: datetime) -> str:
    return value.astimezone(BEIJING).strftime("%Y-%m")


def _unknown_string(value: object, field_name: str) -> str:
    if value is None:
        return UNKNOWN
    if not isinstance(value, str) or not value.strip():
        raise UsageEventError(f"{field_name} must be a non-empty string or null")
    return value.strip()


def _optional_count(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageEventError(f"{field_name} must be a non-negative integer or null")
    return value


@dataclass(frozen=True)
class TokenUsage:
    """Provider-neutral token fields; input is total input including cache."""

    input: int
    output: int
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int | None = None

    def __post_init__(self) -> None:
        for name in ("input", "output", "cache_read", "cache_write"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise UsageEventError(f"tokens.{name} must be a non-negative integer")
        _optional_count(self.reasoning, "tokens.reasoning")
        try:
            InputTokenClasses.from_inclusive(self.input, self.cache_read, self.cache_write)
        except TokenSemanticsError as exc:
            raise UsageEventError(f"tokens.{exc}") from exc

    def to_dict(self) -> dict[str, int | None]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reasoning": self.reasoning,
        }

    @classmethod
    def from_dict(cls, value: object) -> "TokenUsage":
        if not isinstance(value, Mapping):
            raise UsageEventError("tokens must be an object")
        return cls(
            input=_required_count(value.get("input"), "tokens.input"),
            output=_required_count(value.get("output"), "tokens.output"),
            cache_read=_required_count(value.get("cache_read", 0), "tokens.cache_read"),
            cache_write=_required_count(value.get("cache_write", 0), "tokens.cache_write"),
            reasoning=_optional_count(value.get("reasoning"), "tokens.reasoning"),
        )


def _required_count(value: object, field_name: str) -> int:
    parsed = _optional_count(value, field_name)
    if parsed is None:
        raise UsageEventError(f"{field_name} is required")
    return parsed


def _private_ref(value: object, name: str) -> str:
    field_name = f"lineage.{name}"
    parsed = _unknown_string(value, field_name)
    pattern = PRIVATE_REF_PATTERNS[name]
    if parsed != UNKNOWN and pattern.fullmatch(parsed) is None:
        prefix = {
            "root_run_ref": "lineage_v1_",
            "parent_run_ref": "lineage_v1_",
            "cron_job_ref": "cron_v1_",
            "scan_ref": "scan_v1_",
        }[name]
        raise UsageEventError(
            f"{field_name} must be unknown or a {prefix} SHA-256 reference"
        )
    return parsed


@dataclass(frozen=True)
class Lineage:
    """Privacy-safe execution ancestry; raw producer identifiers are forbidden."""

    root_agent: str = UNKNOWN
    execution_mode: str = UNKNOWN
    session_role: str = UNKNOWN
    root_run_ref: str = UNKNOWN
    parent_run_ref: str = UNKNOWN
    cron_job_ref: str = UNKNOWN
    scan_ref: str = UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "root_agent",
            _unknown_string(self.root_agent, "lineage.root_agent"),
        )
        if self.execution_mode not in KNOWN_EXECUTION_MODES:
            raise UsageEventError(f"unsupported lineage.execution_mode: {self.execution_mode}")
        if self.session_role not in KNOWN_SESSION_ROLES:
            raise UsageEventError(f"unsupported lineage.session_role: {self.session_role}")
        for name in ("root_run_ref", "parent_run_ref", "cron_job_ref", "scan_ref"):
            object.__setattr__(
                self,
                name,
                _private_ref(getattr(self, name), name),
            )

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return tuple(
            f"lineage.{name}"
            for name in LINEAGE_FIELDS
            if getattr(self, name) == UNKNOWN
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "root_agent": self.root_agent,
            "execution_mode": self.execution_mode,
            "session_role": self.session_role,
            "root_run_ref": self.root_run_ref,
            "parent_run_ref": self.parent_run_ref,
            "cron_job_ref": self.cron_job_ref,
            "scan_ref": self.scan_ref,
        }

    @classmethod
    def from_dict(cls, value: object) -> "Lineage":
        if not isinstance(value, Mapping):
            raise UsageEventError("lineage must be an object")
        if set(value) != set(LINEAGE_FIELDS):
            raise UsageEventError("lineage must contain exactly the seven known fields")
        return cls(
            root_agent=_unknown_string(value.get("root_agent"), "lineage.root_agent"),
            execution_mode=_unknown_string(
                value.get("execution_mode"), "lineage.execution_mode"
            ),
            session_role=_unknown_string(
                value.get("session_role"), "lineage.session_role"
            ),
            root_run_ref=_private_ref(value.get("root_run_ref"), "root_run_ref"),
            parent_run_ref=_private_ref(value.get("parent_run_ref"), "parent_run_ref"),
            cron_job_ref=_private_ref(value.get("cron_job_ref"), "cron_job_ref"),
            scan_ref=_private_ref(value.get("scan_ref"), "scan_ref"),
        )


@dataclass(frozen=True)
class UsageEvent:
    """`usage_event.v1`; missing attribution stays explicit instead of inferred."""

    event_id: str
    occurred_at: str
    observed_at: str
    machine: str
    agent: str
    consumer_path: str
    source: str
    runtime: str
    provider: str
    model: str
    effort: str
    tokens: TokenUsage
    task_family: str = UNKNOWN
    risk_class: str = UNKNOWN
    task_ref: str = UNKNOWN
    work_item_ref: str = UNKNOWN
    outcome_ref: str = UNKNOWN
    retry_count: int | None = None
    escalation_count: int | None = None
    base_route: str = UNKNOWN
    risk_floor: str = UNKNOWN
    effective_route: str = UNKNOWN
    verification_result: str = UNKNOWN
    outcome_status: str = UNKNOWN
    human_correction: bool | None = None
    high_risk_error: bool | None = None
    adapter: str = UNKNOWN
    source_partition: str = UNKNOWN
    source_position: int | None = None
    quality_flags: tuple[str, ...] = field(default_factory=tuple)
    lineage: Lineage = field(default_factory=Lineage)
    _occurred_utc: datetime = field(init=False, repr=False, compare=False)
    _observed_utc: datetime = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.startswith("uev1_"):
            raise UsageEventError("event_id must be a stable uev1_ identifier")
        occurred = parse_timestamp(self.occurred_at, "occurred_at")
        observed = parse_timestamp(self.observed_at, "observed_at")
        object.__setattr__(self, "_occurred_utc", occurred)
        object.__setattr__(self, "_observed_utc", observed)
        if observed < occurred:
            raise UsageEventError("observed_at cannot precede occurred_at")
        if self.source not in KNOWN_SOURCES:
            raise UsageEventError(f"unsupported source: {self.source}")
        if self.runtime not in KNOWN_RUNTIMES:
            raise UsageEventError(f"unsupported runtime: {self.runtime}")
        if not isinstance(self.lineage, Lineage):
            raise UsageEventError("lineage must be a Lineage object")
        for name in (
            "machine", "agent", "consumer_path", "provider", "model", "effort",
            "task_family", "risk_class", "task_ref", "work_item_ref", "outcome_ref", "base_route", "risk_floor",
            "effective_route", "verification_result", "outcome_status", "adapter",
            "source_partition",
        ):
            object.__setattr__(self, name, _unknown_string(getattr(self, name), name))
        _optional_count(self.retry_count, "retry_count")
        _optional_count(self.escalation_count, "escalation_count")
        _optional_count(self.source_position, "source_position")
        for name in ("human_correction", "high_risk_error"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise UsageEventError(f"{name} must be boolean or null")
        if any(not isinstance(flag, str) or not flag for flag in self.quality_flags):
            raise UsageEventError("quality_flags must contain non-empty strings")

    @property
    def product_month(self) -> str:
        return beijing_month(self._occurred_utc)

    @property
    def observed_month(self) -> str:
        return beijing_month(self._observed_utc)

    @property
    def occurred_utc(self) -> datetime:
        return self._occurred_utc

    @property
    def observed_utc(self) -> datetime:
        return self._observed_utc

    @property
    def late_event(self) -> bool:
        return self.observed_month > self.product_month

    @property
    def missing_fields(self) -> tuple[str, ...]:
        values = {
            "agent": self.agent,
            "consumer_path": self.consumer_path,
            "task_family": self.task_family,
            "risk_class": self.risk_class,
            "task_ref": self.task_ref,
            "work_item_ref": self.work_item_ref,
            "outcome_ref": self.outcome_ref,
            "source": self.source,
            "runtime": self.runtime,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "retry_count": self.retry_count,
            "escalation_count": self.escalation_count,
            "base_route": self.base_route,
            "risk_floor": self.risk_floor,
            "effective_route": self.effective_route,
            "verification_result": self.verification_result,
            "outcome_status": self.outcome_status,
        }
        missing = tuple(
            name for name, value in values.items()
            if value is None or value == UNKNOWN
        )
        return missing + self.lineage.missing_fields

    @property
    def has_missing_fields(self) -> bool:
        """Fast completeness predicate for aggregation hot paths."""

        return (
            UNKNOWN in (
                self.agent,
                self.consumer_path,
                self.task_family,
                self.risk_class,
                self.task_ref,
                self.work_item_ref,
                self.outcome_ref,
                self.source,
                self.runtime,
                self.provider,
                self.model,
                self.effort,
                self.base_route,
                self.risk_floor,
                self.effective_route,
                self.verification_result,
                self.outcome_status,
                self.lineage.root_agent,
                self.lineage.execution_mode,
                self.lineage.session_role,
                self.lineage.root_run_ref,
                self.lineage.parent_run_ref,
                self.lineage.cron_job_ref,
                self.lineage.scan_ref,
            )
            or self.retry_count is None
            or self.escalation_count is None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "event_id": self.event_id,
            "occurred_at": utc_text(self._occurred_utc),
            "observed_at": utc_text(self._observed_utc),
            "product_month": self.product_month,
            "observed_month": self.observed_month,
            "late_event": self.late_event,
            "machine": self.machine,
            "agent": self.agent,
            "consumer_path": self.consumer_path,
            "task_family": self.task_family,
            "risk_class": self.risk_class,
            "task_ref": self.task_ref,
            "work_item_ref": self.work_item_ref,
            "outcome_ref": self.outcome_ref,
            "source": self.source,
            "runtime": self.runtime,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "tokens": self.tokens.to_dict(),
            "lineage": self.lineage.to_dict(),
            "execution": {
                "retry_count": self.retry_count,
                "escalation_count": self.escalation_count,
            },
            "route": {
                "base": self.base_route,
                "risk_floor": self.risk_floor,
                "effective": self.effective_route,
            },
            "feedback": {
                "verification_result": self.verification_result,
                "outcome_status": self.outcome_status,
                "human_correction": self.human_correction,
                "high_risk_error": self.high_risk_error,
            },
            "provenance": {
                "adapter": self.adapter,
                "source_partition": self.source_partition,
                "source_position": self.source_position,
            },
            "quality": {
                "status": "complete" if not self.missing_fields and not self.quality_flags else "partial",
                "missing_fields": list(self.missing_fields),
                "flags": sorted(set(self.quality_flags)),
            },
        }

    def semantic_dict(self) -> dict[str, Any]:
        """Immutable event facts, excluding first-observation placement metadata."""

        # Keep this shape exactly equal to ``to_dict()`` after removing
        # observation/provenance fields, but avoid computing those discarded
        # fields (and repeatedly reparsing their timestamps) for large ledgers.
        missing = self.missing_fields
        return {
            "schema_version": 1,
            "event_id": self.event_id,
            "occurred_at": utc_text(self._occurred_utc),
            "product_month": beijing_month(self._occurred_utc),
            "machine": self.machine,
            "agent": self.agent,
            "consumer_path": self.consumer_path,
            "task_family": self.task_family,
            "risk_class": self.risk_class,
            "task_ref": self.task_ref,
            "work_item_ref": self.work_item_ref,
            "outcome_ref": self.outcome_ref,
            "source": self.source,
            "runtime": self.runtime,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "tokens": self.tokens.to_dict(),
            "lineage": self.lineage.to_dict(),
            "execution": {
                "retry_count": self.retry_count,
                "escalation_count": self.escalation_count,
            },
            "route": {
                "base": self.base_route,
                "risk_floor": self.risk_floor,
                "effective": self.effective_route,
            },
            "feedback": {
                "verification_result": self.verification_result,
                "outcome_status": self.outcome_status,
                "human_correction": self.human_correction,
                "high_risk_error": self.high_risk_error,
            },
            "quality": {
                "status": "complete" if not missing and not self.quality_flags else "partial",
                "missing_fields": list(missing),
                "flags": sorted(set(self.quality_flags)),
            },
        }

    @classmethod
    def from_dict(cls, value: object) -> "UsageEvent":
        if not isinstance(value, Mapping) or value.get("schema_version") != 1:
            raise UsageEventError("usage event must be a schema_version 1 object")
        execution = value.get("execution")
        route = value.get("route")
        feedback = value.get("feedback")
        provenance = value.get("provenance")
        quality = value.get("quality")
        lineage_missing = "lineage" not in value
        legacy_missing_cohort_fields = {
            name for name in ("task_family", "risk_class") if name not in value
        }
        for name, section in (
            ("execution", execution), ("route", route), ("feedback", feedback),
            ("provenance", provenance), ("quality", quality),
        ):
            if not isinstance(section, Mapping):
                raise UsageEventError(f"{name} must be an object")
        raw_flags = quality.get("flags")
        if (
            not isinstance(raw_flags, list)
            or any(not isinstance(item, str) or not item for item in raw_flags)
        ):
            raise UsageEventError("quality.flags must be a list of non-empty strings")
        event = cls(
            event_id=_unknown_string(value.get("event_id"), "event_id"),
            occurred_at=_unknown_string(value.get("occurred_at"), "occurred_at"),
            observed_at=_unknown_string(value.get("observed_at"), "observed_at"),
            machine=_unknown_string(value.get("machine"), "machine"),
            agent=_unknown_string(value.get("agent"), "agent"),
            consumer_path=_unknown_string(value.get("consumer_path"), "consumer_path"),
            task_family=_unknown_string(value.get("task_family"), "task_family"),
            risk_class=_unknown_string(value.get("risk_class"), "risk_class"),
            task_ref=_unknown_string(value.get("task_ref"), "task_ref"),
            work_item_ref=_unknown_string(value.get("work_item_ref"), "work_item_ref"),
            outcome_ref=_unknown_string(value.get("outcome_ref"), "outcome_ref"),
            source=_unknown_string(value.get("source"), "source"),
            runtime=_unknown_string(value.get("runtime"), "runtime"),
            provider=_unknown_string(value.get("provider"), "provider"),
            model=_unknown_string(value.get("model"), "model"),
            effort=_unknown_string(value.get("effort"), "effort"),
            tokens=TokenUsage.from_dict(value.get("tokens")),
            lineage=(
                Lineage() if lineage_missing else Lineage.from_dict(value.get("lineage"))
            ),
            retry_count=_optional_count(execution.get("retry_count"), "retry_count"),
            escalation_count=_optional_count(execution.get("escalation_count"), "escalation_count"),
            base_route=_unknown_string(route.get("base"), "route.base"),
            risk_floor=_unknown_string(route.get("risk_floor"), "route.risk_floor"),
            effective_route=_unknown_string(route.get("effective"), "route.effective"),
            verification_result=_unknown_string(feedback.get("verification_result"), "verification_result"),
            outcome_status=_unknown_string(feedback.get("outcome_status"), "outcome_status"),
            human_correction=feedback.get("human_correction"),
            high_risk_error=feedback.get("high_risk_error"),
            adapter=_unknown_string(provenance.get("adapter"), "provenance.adapter"),
            source_partition=_unknown_string(provenance.get("source_partition"), "source_partition"),
            source_position=_optional_count(provenance.get("source_position"), "source_position"),
            quality_flags=tuple(raw_flags),
        )
        expected_derived = {
            "product_month": event.product_month,
            "observed_month": event.observed_month,
            "late_event": event.late_event,
        }
        for name, expected in expected_derived.items():
            if value.get(name) != expected:
                raise UsageEventError(f"{name} does not match event timestamps")
        expected_missing = event.missing_fields
        if lineage_missing or legacy_missing_cohort_fields:
            expected_missing = tuple(
                name
                for name in expected_missing
                if not (lineage_missing and name.startswith("lineage."))
                and name not in legacy_missing_cohort_fields
            )
        expected_status = (
            "complete" if not expected_missing and not event.quality_flags else "partial"
        )
        if quality.get("status") != expected_status:
            raise UsageEventError("quality.status does not match event completeness")
        if quality.get("missing_fields") != list(expected_missing):
            raise UsageEventError("quality.missing_fields does not match event fields")
        if raw_flags != sorted(set(raw_flags)):
            raise UsageEventError("quality.flags must be sorted and unique")
        return event

    def with_observation(self, observed_at: str) -> "UsageEvent":
        return replace(self, observed_at=observed_at)
