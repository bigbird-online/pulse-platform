"""Validate the Claude Code subscription quota that Relay observed on a real turn.

Relay writes the latest ``rate_limit_event`` of an ordinary CC turn; Stats never
starts a turn and never reads credentials. The weekly figure is the latest
observation inside the current provider window. It is valid only while
``resets_at`` is in the future and ``observed_at`` lies after
``resets_at - 7 days`` (inside that same window); otherwise it is unknown,
never 0. The five-hour figure follows the same rule with its own window and,
when outside it, is withheld without invalidating the weekly figure.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any

SCHEMA = "pulse.stats.cc_quota/v1"
SOURCE = "relay_cc_rate_limit_event"
WEEK_MINUTES = 7 * 24 * 60
FIVE_HOUR_MINUTES = 5 * 60
MAX_BYTES = 65536
FUTURE_TOLERANCE_SECONDS = 60


class QuotaError(ValueError):
    """Stable error code only: do not copy provider messages or credentials."""


def observed_at(document: dict[str, Any]) -> datetime:
    try:
        at = datetime.fromisoformat(document["observed_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise QuotaError("invalid_quota_time") from None
    if at.tzinfo is None:
        raise QuotaError("invalid_quota_time")
    return at


def _window(value: Any, minutes: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QuotaError("invalid_quota_window")
    percent, length, resets = (value.get(k) for k in ("used_percent", "window_minutes", "resets_at"))
    if (type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100
            or type(length) is not int or length != minutes or type(resets) is not int or resets <= 0):
        raise QuotaError("invalid_quota_window")
    return value


def _current(window: dict[str, Any], at: datetime, now: datetime) -> str | None:
    """Reason the observation is not current for its window, or None."""
    resets = window["resets_at"]
    if resets <= now.timestamp():
        return "quota_window_elapsed"
    if at.timestamp() <= resets - window["window_minutes"] * 60:
        return "quota_outside_current_window"
    return None


def validate(document: Any, now: datetime) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise QuotaError("invalid_quota_document")
    if document.get("status") != "available":
        raise QuotaError("quota_unavailable")
    if document.get("source") != SOURCE:
        raise QuotaError("invalid_quota_source")
    at = observed_at(document)
    if (at - now).total_seconds() > FUTURE_TOLERANCE_SECONDS:
        raise QuotaError("quota_from_future")
    weekly = _window(document.get("weekly"), WEEK_MINUTES)
    if document.get("five_hour") is not None:
        _window(document["five_hour"], FIVE_HOUR_MINUTES)
    status = document.get("rate_limit_status")
    if status is not None and not isinstance(status, str):
        raise QuotaError("invalid_rate_limit_status")
    reason = _current(weekly, at, now)
    if reason is not None:
        raise QuotaError(reason)
    return document


def five_hour(document: dict[str, Any], now: datetime) -> tuple[float | None, str | None]:
    """Current five-hour percentage of an already validated document."""
    window = document.get("five_hour")
    if window is None:
        return None, "five_hour_window_unavailable"
    reason = _current(window, observed_at(document), now)
    return (None, reason) if reason else (window["used_percent"], None)


def load_quota(path: Path, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if (path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077
                or path.stat().st_size > MAX_BYTES):
            raise QuotaError("quota_source_unavailable")
        return validate(json.loads(path.read_bytes()), now), None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return None, str(exc) if isinstance(exc, QuotaError) else "quota_source_unavailable"
