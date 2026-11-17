"""Read the Grok account weekly quota from Cursor's dashboard service.

The service (``GetSandUsageStatus``) is an internal app API without a stable
contract, so the response is validated strictly and anything unexpected is
unavailable with a stable reason, never a simulated value. The Cursor access
token stays in memory: it is read from Cursor's own state database in SQLite
read-only mode, sent only in the request header, never logged, refreshed or
written. Renewal belongs to the original login provider; 401/403 is reported
as an invalid login.

The account is checked against Grok Bot's ``active`` fingerprint before and
after the request; a mismatch or a switch during the read refuses publication.

Only a desensitized fact is published: percentage, period start, next reset,
read time, plan label, status and reason. The quota is the account's weekly
pool, not per-bot cost or tokens, and never joins a cost total.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse, request

SCHEMA = "pulse.stats.grok_quota/v1"
SOURCE = "cursor_sand_usage_status"
ENDPOINT = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus"
TOKEN_KEY = "cursorAuth/accessToken"
ACCOUNTS_KEY = "cursor-accounts"
GROK_BILLING_BRAND = "SAND_BILLING_BRAND_GROK"
MAX_BYTES = 65536
MAX_TOKEN_BYTES = 16384
MAX_PLAN_LABEL = 64
# A weekly pool; a longer period means the service changed meaning.
MAX_PERIOD_SECONDS = 8 * 24 * 3600
FUTURE_TOLERANCE_SECONDS = 60
TIMEOUT_SECONDS = 15

# Fields this reader uses; others in the response are ignored.
KNOWN_FIELDS = frozenset({
    "usagePercent", "currentPeriodStart", "nextResetTimestampUtc",
    "hasNonZeroIncludedLimit", "hasAvailableUsage", "grokPlanLabel", "isUnified",
    "billingBrand", "usesPooledEnterpriseAllowance",
})
BOOLEAN_FIELDS = ("hasNonZeroIncludedLimit", "hasAvailableUsage", "isUnified",
                  "usesPooledEnterpriseAllowance")

# The account has no personal weekly quota; none is fabricated.
NOT_APPLICABLE = frozenset({"pooled_enterprise_allowance", "no_personal_allowance"})
# Failures that make any earlier reading misleading: the fact file is replaced
# by an unavailable fact carrying the reason. Other failures (network, server)
# keep the previous reading, which then expires with its own window.
DEFINITIVE = frozenset({
    "cursor_auth_db_unavailable", "cursor_token_missing", "grok_account_unavailable",
    "grok_login_invalid", "account_mismatch", "account_changed_during_read",
    "quota_schema_changed", "quota_api_changed", "unexpected_billing_brand",
})
TRANSIENT = frozenset({"quota_request_failed", "quota_rate_limited", "quota_server_error",
                       "quota_response_too_large"})
REASONS = NOT_APPLICABLE | DEFINITIVE | TRANSIENT

Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, str, bytes]]


class QuotaError(ValueError):
    """Stable error code only: never server text, token or header values."""

    def __init__(self, code: str, *, fields: tuple[str, ...] = ()):
        super().__init__(code)
        # Unknown response field *names* only, for the manual check's diagnosis.
        self.fields = fields


def read_access_token(state_db: Path) -> str:
    """Cursor's access token from its state database, opened read-only."""
    if not state_db.is_absolute() or not state_db.is_file():
        raise QuotaError("cursor_auth_db_unavailable")
    uri = "file:" + parse.quote(str(state_db)) + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = connection.execute("SELECT value FROM ItemTable WHERE key = ?", (TOKEN_KEY,)).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        raise QuotaError("cursor_auth_db_unavailable") from None
    value = row[0] if row else None
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            raise QuotaError("cursor_token_missing") from None
    if (not isinstance(value, str) or not value or len(value) > MAX_TOKEN_BYTES
            or not value.isascii() or any(c.isspace() or not c.isprintable() for c in value)):
        raise QuotaError("cursor_token_missing")
    return value


def fingerprint(token: str) -> str:
    """SHA-256 hex of the JWT ``sub`` (signature not verified), else of the token."""
    parts = token.split(".")
    if len(parts) == 3:
        try:
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        except (ValueError, TypeError):
            payload = None
        sub = payload.get("sub") if isinstance(payload, dict) else None
        if isinstance(sub, str) and sub:
            return hashlib.sha256(sub.encode()).hexdigest()
    return hashlib.sha256(token.encode()).hexdigest()


def read_active_account(accounts: Path) -> str:
    """Grok Bot's active account fingerprint; ``cursor-accounts`` is a JSON string."""
    try:
        if not accounts.is_absolute() or not accounts.is_file() or accounts.stat().st_size > 1024 * 1024:
            raise QuotaError("grok_account_unavailable")
        outer = json.loads(accounts.read_bytes())
        inner = outer.get(ACCOUNTS_KEY) if isinstance(outer, dict) else None
        active = json.loads(inner).get("active") if isinstance(inner, str) else None
    except (OSError, ValueError, AttributeError):
        raise QuotaError("grok_account_unavailable") from None
    if not isinstance(active, str) or not active.strip():
        raise QuotaError("grok_account_unavailable")
    return active.strip().lower()


def account_ref(account_fingerprint: str) -> str:
    """Opaque published reference; not the fingerprint Grok Bot stores."""
    return hashlib.sha256(("cursor\0" + account_fingerprint).encode()).hexdigest()


def _instant(value: Any) -> int:
    """Seconds since the epoch from ISO-8601 with zone or epoch (s / ms)."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if type(value) is int:
        seconds = value // 1000 if value > 10**11 else value
        if seconds <= 0:
            raise QuotaError("quota_schema_changed")
        return seconds
    if not isinstance(value, str):
        raise QuotaError("quota_schema_changed")
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise QuotaError("quota_schema_changed") from None
    if at.tzinfo is None:
        raise QuotaError("quota_schema_changed")
    return int(at.timestamp())


def projection(response: Any, ref: str, observed: datetime) -> dict[str, Any]:
    """Validate a service response and project the published fact."""
    if not isinstance(response, dict):
        raise QuotaError("quota_schema_changed")
    # The live response also carries upsell and billing-link fields (seen
    # 2026-10-26); only the fields read below decide the reading.
    for name in BOOLEAN_FIELDS:
        if name in response and type(response[name]) is not bool:
            raise QuotaError("quota_schema_changed")
    if response.get("billingBrand") != GROK_BILLING_BRAND:
        raise QuotaError("unexpected_billing_brand")
    label = response.get("grokPlanLabel")
    if label is not None and (not isinstance(label, str) or not label.strip()
                              or len(label) > MAX_PLAN_LABEL or not label.isprintable()):
        raise QuotaError("quota_schema_changed")
    base = {"schema": SCHEMA, "source": SOURCE, "observed_at": observed.isoformat(),
            "account_ref": ref, "plan_label": label.strip() if label else None}
    # Proto3 JSON omits false booleans, so absence means "no included limit".
    reason = ("pooled_enterprise_allowance" if response.get("usesPooledEnterpriseAllowance") is True
              else "no_personal_allowance" if response.get("hasNonZeroIncludedLimit") is not True
              else None)
    if reason is not None:
        return {**base, "status": "not_applicable", "reason": reason}
    percent = response.get("usagePercent")
    # A missing percentage is unavailable, never 0%.
    if (type(percent) not in (int, float) or not math.isfinite(percent)
            or not 0 <= percent <= 100):
        raise QuotaError("quota_schema_changed")
    if "currentPeriodStart" not in response or "nextResetTimestampUtc" not in response:
        raise QuotaError("quota_schema_changed")
    start = _instant(response["currentPeriodStart"])
    resets = _instant(response["nextResetTimestampUtc"])
    if not 0 < resets - start <= MAX_PERIOD_SECONDS:
        raise QuotaError("quota_schema_changed")
    if observed.timestamp() + FUTURE_TOLERANCE_SECONDS < start or observed.timestamp() >= resets:
        raise QuotaError("quota_schema_changed")
    return {**base, "status": "available",
            "weekly": {"used_percent": percent, "period_start": start, "resets_at": resets}}


def failure_document(code: str, observed: datetime) -> dict[str, Any]:
    """The published fact for a definitive failure; it carries no account data."""
    if code not in DEFINITIVE:
        raise QuotaError("quota_schema_changed")
    return {"schema": SCHEMA, "source": SOURCE, "status": "unavailable",
            "observed_at": observed.isoformat(), "reason": code}


class _NoRedirect(request.HTTPRedirectHandler):
    """A redirect would forward the bearer token to another location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(url: str, headers: dict[str, str], body: bytes,
                     timeout: float) -> tuple[int, str, bytes]:
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.build_opener(_NoRedirect).open(req, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read(MAX_BYTES + 1)
    except urlerror.HTTPError as exc:
        status = exc.code
        exc.close()
        return status, "", b""


def read_account_quota(*, state_db: Path, accounts: Path, transport: Transport | None = None,
                       now: Callable[[], datetime] | None = None,
                       timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """One quota read; raises ``QuotaError`` with a code from ``REASONS``."""
    clock = now or (lambda: datetime.now(timezone.utc))
    send = transport or urllib_transport
    token = read_access_token(state_db)
    before = fingerprint(token)
    active = read_active_account(accounts)
    if before != active:
        raise QuotaError("account_mismatch")
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
               "Accept": "application/json", "Connect-Protocol-Version": "1"}
    try:
        status, content_type, data = send(ENDPOINT, headers, b"{}", timeout)
    except (OSError, ValueError):
        # URLError, timeouts and TLS failures; their text is never kept.
        raise QuotaError("quota_request_failed") from None
    finally:
        headers.clear()
    observed = clock()
    # Re-read both sides: a switch during the request refuses publication.
    after = fingerprint(read_access_token(state_db))
    if after != before or read_active_account(accounts) != active:
        raise QuotaError("account_changed_during_read")
    if status in (401, 403):
        raise QuotaError("grok_login_invalid")
    if status == 429:
        raise QuotaError("quota_rate_limited")
    if status >= 500:
        raise QuotaError("quota_server_error")
    if status != 200:
        raise QuotaError("quota_api_changed")
    if len(data) > MAX_BYTES:
        raise QuotaError("quota_response_too_large")
    if content_type.split(";")[0].strip().lower() != "application/json":
        raise QuotaError("quota_api_changed")
    try:
        response = json.loads(data)
    except ValueError:
        raise QuotaError("quota_api_changed") from None
    return projection(response, account_ref(before), observed)


def observed_at(document: dict[str, Any]) -> datetime:
    try:
        at = datetime.fromisoformat(document["observed_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise QuotaError("invalid_quota_time") from None
    if at.tzinfo is None:
        raise QuotaError("invalid_quota_time")
    return at


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate(document: Any, now: datetime) -> dict[str, Any]:
    """Validate a published fact; an available reading must be current."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise QuotaError("invalid_quota_document")
    if document.get("source") != SOURCE:
        raise QuotaError("invalid_quota_source")
    at = observed_at(document)
    if (at - now).total_seconds() > FUTURE_TOLERANCE_SECONDS:
        raise QuotaError("quota_from_future")
    status = document.get("status")
    common = {"schema", "source", "status", "observed_at"}
    if status == "unavailable":
        if set(document) != common | {"reason"} or document["reason"] not in DEFINITIVE:
            raise QuotaError("invalid_quota_document")
        return document
    label = document.get("plan_label")
    if (not _hex64(document.get("account_ref"))
            or not (label is None or (isinstance(label, str) and 0 < len(label) <= MAX_PLAN_LABEL))):
        raise QuotaError("invalid_quota_document")
    common |= {"account_ref", "plan_label"}
    if status == "not_applicable":
        if set(document) != common | {"reason"} or document["reason"] not in NOT_APPLICABLE:
            raise QuotaError("invalid_quota_document")
        return document
    if status != "available" or set(document) != common | {"weekly"}:
        raise QuotaError("invalid_quota_document")
    weekly = document["weekly"]
    if not isinstance(weekly, dict) or set(weekly) != {"used_percent", "period_start", "resets_at"}:
        raise QuotaError("invalid_quota_window")
    percent, start, resets = weekly["used_percent"], weekly["period_start"], weekly["resets_at"]
    if (type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100
            or type(start) is not int or type(resets) is not int
            or not 0 < resets - start <= MAX_PERIOD_SECONDS):
        raise QuotaError("invalid_quota_window")
    # Same rule as Codex and CC: the latest reading stays current until its
    # period resets, keeping its original read time.
    if resets <= now.timestamp():
        raise QuotaError("quota_window_elapsed")
    if at.timestamp() + FUTURE_TOLERANCE_SECONDS < start:
        raise QuotaError("quota_outside_current_window")
    return document


def load_quota(path: Path, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    """(document, reason). A not-applicable or unavailable fact returns its reason."""
    try:
        if (path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077
                or path.stat().st_size > MAX_BYTES):
            raise QuotaError("quota_source_unavailable")
        document = validate(json.loads(path.read_bytes()), now)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return None, str(exc) if isinstance(exc, QuotaError) else "quota_source_unavailable"
    return document, None if document["status"] == "available" else document["reason"]
