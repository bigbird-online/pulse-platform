"""CC context compactions per native session, as Relay recorded them.

Relay counts each ``compact_boundary`` of a CC session and keeps the count in
``<RELAY_DATA_DIR>/context/context-compactions-v1.json``
(``pulse.stats.context_compactions/v1``; Relay/src/context-rollover-hint.ts).
The refresh cycle validates it, publishes it to the Stats state directory and
forwards it to the remote publisher, like the CC quota. A session is named by
the hashed scope ``reference("native_session", ["cc", <session id>])``; the
native ID itself never leaves Relay.

A row's ``wi`` is the session's WI now; ``by_wi`` splits its ``compactions``
by the WI each happened under (``wi: null`` while it had none). A WI's total
adds only its own entries, so a session relabelled by a later declaration does
not carry its earlier compactions to the new WI.

Relay counts only CC print mode, whose stream carries the boundaries; an
interactive-mode session is never in the record. The record holds sessions
compacted at least once since ``recording_started_at``, whose last compaction
lies within KEEP_DAYS of the last write, at most MAX_SESSIONS of them;
``dropped_through`` is the latest last compaction of a session it dropped. The
refresh cycle stamps ``observed_at`` when it reads Relay's file: the counts
are as of then, and a publication older than MAX_AGE is stale. A session or WI
without a row is unknown, never zero: it may not have compacted, or it fell
outside that coverage. A WI's total is complete only for a current
publication whose coverage spans the WI's whole life.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .wi_facts import MAX_AGE

SCHEMA = "pulse.stats.context_compactions/v1"
SOURCE = "relay_cc_compact_boundary"
MAX_BYTES = 2 * 1024 * 1024
MAX_SESSIONS = 2000
# Relay drops a session whose last compaction is older than this (KEEP_MS).
KEEP_DAYS = 30
FUTURE_TOLERANCE = timedelta(minutes=5)
_REF = re.compile(r"ccf1_[a-f0-9]{64}")
_WI = re.compile(r"wi_\d{8}_\d{3}")
_SESSION_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


class CompactionsError(ValueError):
    """Stable error code only."""


def _instant(value: Any) -> datetime:
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise CompactionsError("invalid_compaction_time") from None
    if at.tzinfo is None:
        raise CompactionsError("invalid_compaction_time")
    return at


def _wi(value: Any) -> bool:
    return value is None or isinstance(value, str) and _WI.fullmatch(value) is not None


def _valid_split(entries: Any, total: int) -> bool:
    """Distinct WIs (or null), each counted at least once, summing to the session's count."""
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and set(e) == {"wi", "compactions"} and _wi(e["wi"])
            and type(e["compactions"]) is int and e["compactions"] >= 1 for e in entries):
        return False
    return len({e["wi"] for e in entries}) == len(entries) and sum(e["compactions"] for e in entries) == total


def validate(document: Any, now: datetime) -> dict[str, Any]:
    if (not isinstance(document, dict) or document.get("schema") != SCHEMA
            or document.get("source") != SOURCE or not isinstance(document.get("sessions"), list)):
        raise CompactionsError("invalid_compactions_document")
    updated = _instant(document.get("updated_at"))
    if updated > now + FUTURE_TOLERANCE:
        raise CompactionsError("compactions_from_future")
    if "dropped_through" not in document or _instant(document.get("recording_started_at")) > updated or not (
            document["dropped_through"] is None or _instant(document["dropped_through"]) <= updated):
        raise CompactionsError("invalid_compactions_coverage")
    if "observed_at" in document and not updated <= _instant(document["observed_at"]) <= now + FUTURE_TOLERANCE:
        raise CompactionsError("invalid_compactions_observation")
    sessions = document["sessions"]
    if len(sessions) > MAX_SESSIONS:
        raise CompactionsError("compactions_unbounded")
    seen = set()
    for row in sessions:
        if (not isinstance(row, dict)
                or set(row) != {"session_ref", "runtime", "wi", "compactions", "by_wi", "context_ratio",
                                "first_compaction_at", "last_compaction_at"}
                or not isinstance(row["session_ref"], str) or not _REF.fullmatch(row["session_ref"])
                or row["session_ref"] in seen or row["runtime"] != "cc"
                or not _wi(row["wi"])
                or type(row["compactions"]) is not int or row["compactions"] < 1
                or not (row["context_ratio"] is None or type(row["context_ratio"]) in (int, float)
                        and 0 <= row["context_ratio"] <= 10)
                or not _valid_split(row["by_wi"], row["compactions"])):
            raise CompactionsError("invalid_compacted_session")
        if _instant(row["first_compaction_at"]) > _instant(row["last_compaction_at"]):
            raise CompactionsError("invalid_compacted_session")
        seen.add(row["session_ref"])
    return document


def load(path: Path | None, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, "compactions_source_not_configured"
    try:
        if (path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077
                or path.stat().st_size > MAX_BYTES):
            raise CompactionsError("compactions_source_unavailable")
        return validate(json.loads(path.read_bytes()), now), None
    except (OSError, ValueError, TypeError) as exc:
        return None, str(exc) if isinstance(exc, CompactionsError) else "compactions_source_unavailable"


def session_ref(session_id: str) -> str:
    """The hashed scope of a CC native session ID, as Relay and ComputeCost compute it."""
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid_session_id")
    canonical = json.dumps(["native_session", ["cc", session_id]], separators=(",", ":"), ensure_ascii=False)
    return "ccf1_" + hashlib.sha256(canonical.encode()).hexdigest()


def _of_wi(row: dict[str, Any], wi: str) -> int:
    return sum(e["compactions"] for e in row["by_wi"] if e["wi"] == wi)


def sessions(document: dict[str, Any] | None, *, wi: str | None = None, ref: str | None = None) -> list[dict[str, Any]]:
    """Recorded sessions, newest compaction first, optionally of one session or with compactions under one WI.

    Of one WI, each row also states ``wi_compactions``, its compactions under that WI.
    """
    rows = [{**r, "wi_compactions": _of_wi(r, wi)} if wi is not None else dict(r)
            for r in (document or {}).get("sessions", []) if ref is None or r["session_ref"] == ref]
    rows = [r for r in rows if wi is None or r["wi_compactions"]]
    return sorted(rows, key=lambda r: (r["last_compaction_at"], r["session_ref"]), reverse=True)


def observed(document: dict[str, Any], now: datetime) -> dict[str, Any]:
    """When the publisher read Relay's record, until when that reading is current, and the gap since."""
    at = document.get("observed_at")
    if at is None:
        return {"observed_at": None, "expires_at": None, "stale": None, "uncovered_from": None, "uncovered_to": None}
    expires = _instant(at) + MAX_AGE
    stale = now >= expires
    return {"observed_at": at, "expires_at": expires.isoformat(), "stale": stale,
            "uncovered_from": at if stale else None, "uncovered_to": now.isoformat() if stale else None}


def coverage(document: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The span the record answers for: sessions whose last compaction lies in (from, to]."""
    rows = document["sessions"]
    reading = observed(document, now)
    return {"last_compaction_from": document["dropped_through"] or document["recording_started_at"],
            "last_compaction_to": reading["observed_at"] or document["updated_at"],
            "recording_started_at": document["recording_started_at"],
            "dropped_through": document["dropped_through"], **reading,
            "retention_days": KEEP_DAYS, "max_sessions": MAX_SESSIONS, "sessions_recorded": len(rows),
            "at_capacity": len(rows) >= MAX_SESSIONS, "compacted_sessions_only": True, "cc_mode": "print"}


def _status(document: dict[str, Any], cover: dict[str, Any], rows: list, *, wi: str | None, ref: str | None,
            wi_created_at: str | None) -> tuple[str, str | None]:
    if not rows:
        return "unknown", "not_in_published_record"
    if cover["observed_at"] is None:
        return "partial", "observation_time_unknown"
    if cover["stale"]:
        return "partial", "record_stale"
    if ref is not None:
        return "complete", None
    if wi is not None:
        if wi_created_at is None:
            return "partial", "work_item_start_unknown"
        created = _instant(wi_created_at)
        if created < _instant(document["recording_started_at"]):
            return "partial", "work_item_predates_record"
        if document["dropped_through"] is not None and _instant(document["dropped_through"]) >= created:
            return "partial", "record_dropped_sessions_since_work_item_start"
        return "complete", None
    return ("partial", "record_dropped_sessions") if document["dropped_through"] is not None else ("complete", None)


def summary(document: dict[str, Any], now: datetime, *, wi: str | None = None, ref: str | None = None,
            wi_created_at: str | None = None) -> dict[str, Any]:
    """Matching sessions, their total as of the reading, and how complete that total is.

    Of one WI the total adds only its compactions under that WI.
    """
    rows = sessions(document, wi=wi, ref=ref)
    count = "wi_compactions" if wi is not None else "compactions"
    cover = coverage(document, now)
    status, reason = _status(document, cover, rows, wi=wi, ref=ref, wi_created_at=wi_created_at)
    return {"updated_at": document["updated_at"], "observed_at": cover["observed_at"], "coverage": cover,
            "sessions": rows, "total_compactions": sum(r[count] for r in rows) if rows else None,
            "status": status, "reason": reason}
