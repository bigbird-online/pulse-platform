"""Read account quota from Codex App Server; never create a model turn.

The executable and CODEX_HOME are deployment inputs, not inferred identities.
Only an opaque account fingerprint and the service's matching quota windows are
published. Authentication remains entirely owned by the installed Codex runtime.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Any, Sequence

SCHEMA = "pulse.stats.codex_quota/v1"
MAX_BYTES = 1024 * 1024
WEEK_MINUTES = 7 * 24 * 60


class QuotaError(ValueError):
    """Stable error code only: do not copy server messages or credentials."""


def account_ref(result: Any) -> str:
    account = result.get("account") if isinstance(result, dict) else None
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise QuotaError("chatgpt_account_required")
    identity = account.get("id") or account.get("email")
    if not isinstance(identity, str) or not identity.strip():
        raise QuotaError("account_identity_unavailable")
    return hashlib.sha256(("chatgpt\0" + identity.strip()).encode()).hexdigest()


def projection(result: Any, identity: str, observed: datetime) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise QuotaError("invalid_rate_limits")
    buckets = result.get("rateLimitsByLimitId")
    if isinstance(buckets, dict):
        bucket = buckets.get("codex")
    else:
        bucket = result.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") != "codex":
        raise QuotaError("codex_bucket_unavailable")
    windows = []
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise QuotaError("invalid_quota_window")
        percent, minutes, resets = (window.get(k) for k in
                                   ("usedPercent", "windowDurationMins", "resetsAt"))
        if (type(percent) not in (int, float) or not math.isfinite(percent)
                or not 0 <= percent <= 100 or type(minutes) is not int or minutes <= 0
                or type(resets) is not int or resets <= observed.timestamp()):
            raise QuotaError("invalid_quota_window")
        windows.append({"used_percent": percent, "window_minutes": minutes, "resets_at": resets})
    weekly = [w for w in windows if w["window_minutes"] == WEEK_MINUTES]
    if len(weekly) != 1:
        raise QuotaError("weekly_window_unavailable")
    return {"schema": SCHEMA, "status": "available", "observed_at": observed.isoformat(),
            "account_ref": identity, "limit_id": "codex", "windows": windows,
            "plan_type": bucket.get("planType") if isinstance(bucket.get("planType"), str) else None,
            "weekly": weekly[0]}


def validate(document: Any, now: datetime) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise QuotaError("invalid_quota_document")
    if document.get("status") != "available":
        raise QuotaError("quota_unavailable")
    ref = document.get("account_ref")
    if not isinstance(ref, str) or len(ref) != 64 or any(c not in "0123456789abcdef" for c in ref):
        raise QuotaError("invalid_account_reference")
    try:
        at = datetime.fromisoformat(document["observed_at"].replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError()
        age = (now - at).total_seconds()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise QuotaError("invalid_quota_time") from None
    if age < -60:
        raise QuotaError("quota_from_future")
    # The latest account reading stays current until its weekly window resets,
    # the same rule as CC quota; its original observation time is kept.
    weekly = document.get("weekly")
    if not isinstance(weekly, dict) or type(weekly.get("resets_at")) is not int:
        raise QuotaError("invalid_quota_window")
    if weekly["resets_at"] <= now.timestamp():
        raise QuotaError("quota_window_elapsed")
    if at.timestamp() <= weekly["resets_at"] - WEEK_MINUTES * 60:
        raise QuotaError("quota_outside_current_window")
    if document.get("limit_id") != "codex":
        raise QuotaError("codex_bucket_unavailable")
    windows = document.get("windows")
    if not isinstance(windows, list) or not 1 <= len(windows) <= 2:
        raise QuotaError("invalid_quota_window")
    native = {"limitId": "codex", "planType": document.get("plan_type")}
    for name, window in zip(("primary", "secondary"), windows):
        if not isinstance(window, dict):
            raise QuotaError("invalid_quota_window")
        native[name] = {"usedPercent": window.get("used_percent"),
                        "windowDurationMins": window.get("window_minutes"),
                        "resetsAt": window.get("resets_at")}
    result = projection({"rateLimits": native}, ref, at)
    if result["weekly"] != weekly:
        raise QuotaError("quota_window_mismatch")
    return document


def read_account_quota(command: Sequence[str], *, codex_home: Path,
                       timeout: float = 25) -> dict[str, Any]:
    return _read_account(command, codex_home=codex_home, timeout=timeout, identity_only=False)


def read_account_identity(command: Sequence[str], *, codex_home: Path,
                          timeout: float = 25) -> str:
    """Recheck a pending publication's identity without another quota read."""
    return _read_account(command, codex_home=codex_home, timeout=timeout, identity_only=True)


def _read_account(command: Sequence[str], *, codex_home: Path,
                  timeout: float, identity_only: bool) -> dict[str, Any] | str:
    if (not command or not Path(command[0]).is_absolute() or not codex_home.is_absolute()
            or not codex_home.is_dir()):
        raise QuotaError("quota_runtime_not_configured")
    # No login, reset-credit consumption, thread, or turn methods are permitted.
    env = dict(os.environ, CODEX_HOME=str(codex_home))
    proc = subprocess.Popen([*command, "app-server", "--listen", "stdio://"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, cwd=codex_home,
                            env=env, start_new_session=True)
    deadline, data, read_bytes = time.monotonic() + timeout, b"", 0
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stdin is not None
    selector.register(proc.stdout, selectors.EVENT_READ)

    def send(value: dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(value).encode() + b"\n")
        proc.stdin.flush()

    def request(identifier: int, method: str, params: dict[str, Any] | None = None) -> Any:
        nonlocal data, read_bytes
        payload: dict[str, Any] = {"id": identifier, "method": method}
        if params is not None:
            payload["params"] = params
        send(payload)
        while time.monotonic() < deadline:
            if b"\n" not in data:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise QuotaError("quota_server_closed")
                read_bytes += len(chunk)
                if read_bytes > MAX_BYTES:
                    raise QuotaError("quota_response_too_large")
                data += chunk
                continue
            line, data = data.split(b"\n", 1)
            try:
                item = json.loads(line)
            except ValueError:
                raise QuotaError("invalid_quota_rpc") from None
            if not isinstance(item, dict):
                raise QuotaError("invalid_quota_rpc")
            if item.get("id") == identifier and "method" not in item:
                if "error" in item:
                    raise QuotaError("quota_rpc_failed")
                return item.get("result")
            if "id" in item and "method" in item:
                # Never grant interactive authentication or other server requests.
                send({"id": item["id"], "error": {"code": -32601, "message": "Unsupported by read-only Stats"}})
        raise QuotaError("quota_timeout")

    try:
        request(1, "initialize", {"clientInfo": {"name": "pulse_stats", "title": "PULSE Stats", "version": "0.2.1"}})
        send({"method": "initialized"})
        before = account_ref(request(2, "account/read", {"refreshToken": False}))
        if identity_only:
            return before
        limits = request(3, "account/rateLimits/read")
        after = account_ref(request(4, "account/read", {"refreshToken": False}))
        if before != after:
            raise QuotaError("account_changed_during_read")
        return projection(limits, before, datetime.now(timezone.utc))
    except (OSError, subprocess.SubprocessError):
        raise QuotaError("quota_runtime_failed") from None
    finally:
        selector.close()
        try:
            proc.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=3)
        proc.stdout.close()


def load_quota(path: Path, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 65536:
            raise QuotaError('quota_source_unavailable')
        return validate(json.loads(path.read_bytes()), now), None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return None, str(exc) if isinstance(exc, QuotaError) else 'quota_source_unavailable'
