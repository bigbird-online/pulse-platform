"""Versioned incident projection state with a single-writer lock and atomic 0600 writes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Dict, Iterator, List, Literal, Mapping, Optional, Tuple

from .reducer import IncidentProjection


STATE_SCHEMA_VERSION = 1
MAX_EVIDENCE = 32
MAX_SEEN_EVENT_IDS = 128
MAX_SOURCE_POSITIONS = 64
_VALID_STATES = {"open", "resolved"}
_VALID_SEVERITIES = {"info", "warning", "critical"}


class StateError(RuntimeError):
    pass


class StateLockBusy(StateError):
    pass


@dataclass(frozen=True)
class StateLoadResult:
    status: Literal["ok", "missing", "corrupt"]
    incidents: Tuple[IncidentProjection, ...]
    source_watermarks: Mapping[str, str]
    source_cursors: Mapping[str, str]
    reason: Optional[str] = None


class IncidentStateStore:
    def __init__(self, path: Path, lock_path: Optional[Path] = None):
        self.path = Path(path)
        self.lock_path = Path(lock_path) if lock_path else self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def single_writer(self) -> Iterator[None]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(self.lock_path, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StateLockBusy(f"incident state writer already active: {self.lock_path}") from error
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def load(self) -> StateLoadResult:
        if not self.path.exists():
            return StateLoadResult("missing", (), {}, {})
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
                raise ValueError("unsupported incident state schema")
            projections = raw.get("incidents")
            if not isinstance(projections, list):
                raise ValueError("incidents must be a list")
            watermarks = self._decode_positions(raw.get("source_watermarks", {}), "source_watermarks")
            cursors = self._decode_positions(raw.get("source_cursors", {}), "source_cursors")
            return StateLoadResult(
                "ok",
                tuple(self._decode(item) for item in projections),
                watermarks,
                cursors,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            return StateLoadResult("corrupt", (), {}, {}, str(error))

    def save(
        self,
        incidents: List[IncidentProjection],
        *,
        source_watermarks: Optional[Mapping[str, str]] = None,
        source_cursors: Optional[Mapping[str, str]] = None,
    ) -> None:
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "incidents": [self._encode(item) for item in sorted(incidents, key=lambda value: value.incident_key)],
            "source_watermarks": self._bounded_positions(source_watermarks or {}),
            "source_cursors": self._bounded_positions(source_cursors or {}),
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        temporary_path = Path(temporary)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
            directory_descriptor = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def backup_corrupt(self, timestamp: datetime) -> Path:
        """Preserve the unreadable state before a complete rebuild replaces it."""
        suffix = timestamp.strftime("%Y%m%dT%H%M%SZ")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{suffix}.bak")
        payload = self.path.read_bytes()
        descriptor = os.open(str(backup), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            if backup.exists():
                backup.unlink()
            raise
        return backup

    @staticmethod
    def _encode(item: IncidentProjection) -> dict:
        return {
            **item.as_snapshot_dict(),
            "seen_event_ids": sorted(item.seen_event_ids)[-MAX_SEEN_EVENT_IDS:],
            "last_breach_at": item.last_breach_at.isoformat(),
            "evidence": list(item.evidence[-MAX_EVIDENCE:]),
            "recovery_evidence": list(item.recovery_evidence[-MAX_EVIDENCE:]),
        }

    @staticmethod
    def _decode(raw: object) -> IncidentProjection:
        if not isinstance(raw, dict):
            raise ValueError("incident entry must be an object")
        seen = raw["seen_event_ids"]
        evidence = raw["evidence"]
        recovery = raw["recovery_evidence"]
        if not all(isinstance(value, list) for value in (seen, evidence, recovery)):
            raise ValueError("incident evidence fields must be lists")
        if len(seen) > MAX_SEEN_EVENT_IDS or len(evidence) > MAX_EVIDENCE or len(recovery) > MAX_EVIDENCE:
            raise ValueError("incident idempotency fields exceed their bounds")
        state = raw["state"]
        severity = raw["severity"]
        if state not in _VALID_STATES:
            raise ValueError("incident state is invalid")
        if severity not in _VALID_SEVERITIES:
            raise ValueError("incident severity is invalid")
        opened_at = IncidentStateStore._aware_datetime(raw["opened_at"], "opened_at")
        updated_at = IncidentStateStore._aware_datetime(raw["updated_at"], "updated_at")
        resolved_at = (
            IncidentStateStore._aware_datetime(raw["resolved_at"], "resolved_at")
            if raw.get("resolved_at")
            else None
        )
        last_breach_at = IncidentStateStore._aware_datetime(raw["last_breach_at"], "last_breach_at")
        return IncidentProjection(
            incident_key=str(raw["incident_key"]),
            episode_id=str(raw["episode_id"]),
            state=state,
            severity=severity,
            capability_id=str(raw["capability_id"]),
            scope=str(raw["scope"]),
            title=str(raw["title"]),
            what_happened=str(raw["what_happened"]),
            impact=str(raw["impact"]),
            suggested_action=str(raw["suggested_action"]),
            opened_at=opened_at,
            updated_at=updated_at,
            resolved_at=resolved_at,
            occurrence_count=int(raw["occurrence_count"]),
            reopen_count=int(raw["reopen_count"]),
            evidence=tuple(str(value) for value in evidence[-MAX_EVIDENCE:]),
            recovery_evidence=tuple(str(value) for value in recovery[-MAX_EVIDENCE:]),
            seen_event_ids=frozenset(str(value) for value in seen[-MAX_SEEN_EVENT_IDS:]),
            last_breach_at=last_breach_at,
        )

    @staticmethod
    def _aware_datetime(value: object, field: str) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return parsed

    @staticmethod
    def _bounded_positions(values: Mapping[str, str]) -> Dict[str, str]:
        normalized = {str(key): str(value) for key, value in values.items()}
        return dict(sorted(normalized.items())[-MAX_SOURCE_POSITIONS:])

    @staticmethod
    def _decode_positions(raw: object, field: str) -> Dict[str, str]:
        if not isinstance(raw, dict) or len(raw) > MAX_SOURCE_POSITIONS:
            raise ValueError(f"{field} must be a bounded object")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw.items()):
            raise ValueError(f"{field} entries must be strings")
        return dict(raw)
