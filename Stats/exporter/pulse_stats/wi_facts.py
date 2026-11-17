"""WorkItem facts exported by the installed native core (harness.stats_facts/v1).

The publisher on the authority host runs ``harness.pyz stats-facts`` and ships
the bytes here. Stats derives its WI counters, in-flight rows, human requests
and global-authority lamp from these facts only; it does not read legacy
ledger files or infer lifecycle state.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .live_contract import (
    DISPLAY_TZ,
    IN_FLIGHT_ACTIVE_DAYS,
    IN_FLIGHT_SWEEP_DAYS,
    WI_STALLED_DAYS,
    WI_STALLED_MAX_ITEMS,
)
from .producer import GLOBAL_READ_MODEL_SCHEMA

SCHEMA = "harness.stats_facts/v1"
MAX_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 50000
# The publisher runs every 30 minutes; one missed cycle plus grace is stale.
MAX_AGE = timedelta(minutes=75)
STATUSES = {"draft", "active", "stopped", "transferring", "completed", "terminated"}
IN_FLIGHT = {"draft", "active", "stopped", "transferring"}
TERMINAL = {"completed", "terminated"}
# The App's in-flight vocabulary predates the native core.
DISPLAY_STATUS = {
    "draft": "draft",
    "active": "in_progress",
    "transferring": "in_progress",
    "stopped": "blocked",
}
_WI = re.compile(r"wi_[A-Za-z0-9_]{1,64}")


class WiFactsError(ValueError):
    pass


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise WiFactsError("timestamp required")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise WiFactsError("timezone-aware timestamp required")
    return moment


def validate(document: object, now: datetime) -> Mapping[str, object]:
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise WiFactsError("unknown schema")
    if document.get("read_only") is not True or document.get("source") != "journal":
        raise WiFactsError("not a read-only journal export")
    if _instant(document.get("generated_at")) > now + timedelta(minutes=5):
        raise WiFactsError("facts from the future")
    items = document.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise WiFactsError("items unbounded")
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not _WI.fullmatch(item["id"])
            or item.get("status") not in STATUSES
            or not isinstance(item.get("title"), str)
            or not isinstance(item.get("owner"), str)
        ):
            raise WiFactsError("invalid item")
        for key in ("created_at", "updated_at"):
            _instant(item.get(key))
        if not isinstance(item.get("main_only", False), bool):
            raise WiFactsError("invalid main-body label")
        summary = item.get("summary")
        if summary is not None and (not isinstance(summary, str) or len(summary) > 200):
            raise WiFactsError("invalid summary")
        if item.get("closed_at") is not None:
            _instant(item["closed_at"])
    if not isinstance(document.get("unavailable_items"), list):
        raise WiFactsError("coverage evidence required")
    return document


def load(path: Optional[Path], now: datetime,
         max_age: Optional[timedelta] = MAX_AGE) -> Tuple[Optional[Mapping[str, object]], Optional[str]]:
    """Return (facts, None) when fresh and valid, otherwise (None, reason).

    ``max_age=None`` accepts any age: a reader that bounds coverage by
    ``generated_at`` itself (the agent_day query) needs older facts too.
    """
    if path is None:
        return None, "source_not_configured"
    try:
        if path.is_symlink():
            raise WiFactsError("symlink source")
        with path.open("rb") as handle:
            mode = __import__("os").fstat(handle.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
                raise WiFactsError("source not private")
            data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise WiFactsError("source oversized")
        document = dict(validate(json.loads(data), now))
    except FileNotFoundError:
        return None, "source_not_configured"
    except (OSError, ValueError, TypeError):
        return None, "invalid_wi_facts"
    if max_age is not None and now - _instant(document["generated_at"]) > max_age:
        return None, "wi_facts_stale"
    document["digest"] = "sha256:" + hashlib.sha256(data).hexdigest()
    return document, None


def _days(now: datetime, moment: datetime) -> int:
    return (now - moment).days


def counters(facts: Mapping[str, object], now: datetime) -> Dict[str, object]:
    """Same windows and keys as the Stats WI counters.

    Calendar periods are Asia/Shanghai days: today, the ISO week from Monday
    and the month, each ending today. ``*_closed`` counts completed and
    terminated WorkItems by ``closed_at``; a terminal WorkItem without
    ``closed_at`` has no known closing day and is not counted.
    """
    today = now.astimezone(DISPLAY_TZ).date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    week_cutoff = now - timedelta(days=7)
    week = day = day_done = iso_week = iso_week_done = 0
    month = day_closed = iso_week_closed = month_closed = 0
    stalled: List[Dict[str, object]] = []
    for item in facts["items"]:  # type: ignore[union-attr]
        created = _instant(item["created_at"])
        local = created.astimezone(DISPLAY_TZ).date()
        week += created >= week_cutoff
        day += local == today
        iso_week += local >= week_start
        month += month_start <= local <= today
        if item["status"] in TERMINAL and item.get("closed_at"):
            closed = _instant(item["closed_at"]).astimezone(DISPLAY_TZ).date()
            if item["status"] == "completed":
                day_done += closed == today
                iso_week_done += closed >= week_start
            day_closed += closed == today
            iso_week_closed += week_start <= closed <= today
            month_closed += month_start <= closed <= today
        if item["status"] in IN_FLIGHT:
            age = _days(now, _instant(item["updated_at"]))
            if age > WI_STALLED_DAYS:
                stalled.append({"work_item_id": item["id"], "title": item["title"],
                                "days": age, "status": DISPLAY_STATUS[item["status"]]})
    stalled.sort(key=lambda row: (-int(row["days"]), str(row["work_item_id"])))
    return {
        "week_created": week,
        "day_created": day,
        "day_completed": day_done,
        "iso_week_created": iso_week,
        "iso_week_completed": iso_week_done,
        "iso_week_start": week_start.isoformat(),
        "month_created": month,
        "day_closed": day_closed,
        "iso_week_closed": iso_week_closed,
        "month_closed": month_closed,
        "stalled": stalled[:WI_STALLED_MAX_ITEMS],
        # The native core has no handoff queue; requests are WorkItem states.
        "handoff_pending": None,
        "unreadable_work_items": len(facts["unavailable_items"]),  # type: ignore[arg-type]
        "unreadable_handoffs": 0,
    }


def in_flight(facts: Mapping[str, object], now: datetime) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for item in facts["items"]:  # type: ignore[union-attr]
        if item["status"] not in IN_FLIGHT:
            continue
        created = _instant(item["created_at"])
        age = _days(now, _instant(item["updated_at"]))
        status = DISPLAY_STATUS[item["status"]]
        # Only the owner's main-body label makes a WorkItem the owner's to act on;
        # every other in-flight WorkItem is picked up by Agents automatically.
        needs_owner = item.get("main_only") is True
        if status == "blocked":
            disposition = "blocked_needs_read"
        elif needs_owner:
            disposition = "manual_required"
        elif age <= IN_FLIGHT_ACTIVE_DAYS:
            disposition = "waiting_peer"
        else:
            disposition = "sweep_overdue" if age >= IN_FLIGHT_SWEEP_DAYS else "sweep_watching"
        row: Dict[str, object] = {
            "work_item_id": item["id"],
            "title": item["title"].splitlines()[0][:64] if item["title"] else "",
            "blocked_by": item.get("holder") or item["owner"],
            "status": status,
            "days": age,
            "open_days": _days(now, created) if created <= now else None,
            "created_at": created.isoformat() if created <= now else None,
            "needs_owner": needs_owner,
            "disposition": disposition,
            "quiet_days": age,
            "circuit_broken": False,
            "blocked_reason": "stopped" if status == "blocked" else None,
            "owner": item["owner"],
        }
        if item.get("profile") in {"lightweight", "standard"}:
            row["workflow_profile"] = item["profile"]
        rows.append(row)
    rows.sort(key=lambda row: (row["open_days"] is None, -(row["open_days"] or 0),
                               row["created_at"] or "", str(row["work_item_id"])))
    return rows


def human_requests(facts: Mapping[str, object], now: datetime) -> List[Dict[str, object]]:
    """In-flight WorkItems that carry the owner's main-body label."""
    rows: List[Dict[str, object]] = []
    for item in facts["items"]:  # type: ignore[union-attr]
        if item["status"] not in IN_FLIGHT or item.get("main_only") is not True:
            continue
        rows.append({
            "id": item["id"],
            "business_subject": item["id"],
            "days": _days(now, _instant(item["updated_at"])),
            "rounds": 1,
            "gist": item["title"],
            "asks": "/wi " + item["id"],
            "ack_state": "no_handoff",
            "from_owner": item.get("holder") or item["owner"],
            "to_owner": item["owner"],
        })
    rows.sort(key=lambda row: (-int(row["days"]), str(row["id"])))
    return rows


def authority(facts: Optional[Mapping[str, object]], reason: Optional[str]) -> Dict[str, object]:
    """One production ledger is the only authority endpoint."""
    if facts is None:
        return {
            "schema_version": GLOBAL_READ_MODEL_SCHEMA,
            "status": "unknown",
            "scope": "unknown",
            "authority_revision": None,
            "required_endpoint_count": 1,
            "materialized_endpoint_count": 0,
            "materialization_digest": None,
            "freshness": "stale" if reason == "wi_facts_stale" else "unknown",
            "degraded_reasons": ["freshness" if reason == "wi_facts_stale" else "materialization"],
        }
    complete = facts.get("coverage") == "complete"
    return {
        "schema_version": GLOBAL_READ_MODEL_SCHEMA,
        "status": "validated" if complete else "partial",
        "scope": "global" if complete else "partial",
        "authority_revision": facts["digest"],
        "required_endpoint_count": 1,
        "materialized_endpoint_count": 1,
        "materialization_digest": facts["digest"],
        "freshness": "fresh",
        "degraded_reasons": [] if complete else ["materialization"],
    }
