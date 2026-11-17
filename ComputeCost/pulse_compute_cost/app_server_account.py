"""Read-only, identity-free App Server account usage and quota probe."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import select
import subprocess
import time
from typing import Any, Mapping


ACCOUNT_SNAPSHOT_SCHEMA = "app_server_account_snapshot/v1"


class AppServerAccountError(RuntimeError):
    """Raised when account facts are unavailable or cannot be sanitized."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AppServerAccountError(f"{field} must be a non-negative integer")
    return value


def _optional_count(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _count(value, field)


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AppServerAccountError(f"{field} must be a non-empty string or null")
    return value.strip()


def _optional_bool(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise AppServerAccountError(f"{field} must be boolean or null")
    return value


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AppServerAccountError(f"{field} must be an object")
    return value


def _window(value: object, field: str) -> dict[str, int | None] | None:
    if value is None:
        return None
    raw = _object(value, field)
    return {
        "used_percent": _count(raw.get("usedPercent"), f"{field}.usedPercent"),
        "resets_at": _optional_count(raw.get("resetsAt"), f"{field}.resetsAt"),
        "window_duration_mins": _optional_count(
            raw.get("windowDurationMins"), f"{field}.windowDurationMins"
        ),
    }


def _credits(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    raw = _object(value, "rateLimits.credits")
    return {
        "has_credits": bool(raw.get("hasCredits"))
        if isinstance(raw.get("hasCredits"), bool)
        else _raise("rateLimits.credits.hasCredits must be boolean"),
        "unlimited": bool(raw.get("unlimited"))
        if isinstance(raw.get("unlimited"), bool)
        else _raise("rateLimits.credits.unlimited must be boolean"),
        "balance": _optional_string(raw.get("balance"), "rateLimits.credits.balance"),
    }


def _raise(message: str) -> Any:
    raise AppServerAccountError(message)


def _individual_limit(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    raw = _object(value, "rateLimits.individualLimit")
    return {
        "limit": _optional_string(raw.get("limit"), "individualLimit.limit"),
        "used": _optional_string(raw.get("used"), "individualLimit.used"),
        "remaining_percent": _count(
            raw.get("remainingPercent"), "individualLimit.remainingPercent"
        ),
        "resets_at": _count(raw.get("resetsAt"), "individualLimit.resetsAt"),
    }


def _sanitize_usage(result: object) -> dict[str, object]:
    raw = _object(result, "account/usage/read result")
    if raw.get("threadUsage") is not None:
        raise AppServerAccountError(
            "account-wide usage response unexpectedly contains thread identity"
        )
    summary = _object(raw.get("summary"), "usage.summary")
    buckets_value = raw.get("dailyUsageBuckets")
    if buckets_value is not None and not isinstance(buckets_value, list):
        raise AppServerAccountError("usage.dailyUsageBuckets must be an array or null")
    daily_usage: dict[str, object] | None = None
    if isinstance(buckets_value, list):
        dates: list[str] = []
        tokens_total = 0
        for index, item in enumerate(buckets_value):
            bucket = _object(item, f"dailyUsageBuckets[{index}]")
            date = _optional_string(
                bucket.get("startDate"),
                f"dailyUsageBuckets[{index}].startDate",
            )
            if date is None:
                raise AppServerAccountError("daily usage startDate cannot be null")
            dates.append(date)
            tokens_total += _count(
                bucket.get("tokens"), f"dailyUsageBuckets[{index}].tokens"
            )
        daily_usage = {
            "bucket_count": len(dates),
            "start_date": min(dates) if dates else None,
            "end_date": max(dates) if dates else None,
            "tokens_total": tokens_total,
        }
    return {
        "summary": {
            "lifetime_tokens": _optional_count(
                summary.get("lifetimeTokens"), "usage.summary.lifetimeTokens"
            ),
            "peak_daily_tokens": _optional_count(
                summary.get("peakDailyTokens"), "usage.summary.peakDailyTokens"
            ),
            "current_streak_days": _optional_count(
                summary.get("currentStreakDays"), "usage.summary.currentStreakDays"
            ),
            "longest_streak_days": _optional_count(
                summary.get("longestStreakDays"), "usage.summary.longestStreakDays"
            ),
            "longest_running_turn_sec": _optional_count(
                summary.get("longestRunningTurnSec"),
                "usage.summary.longestRunningTurnSec",
            ),
        },
        "daily_usage": daily_usage,
        "coverage": "account_activity_not_wi_attribution",
    }


def _sanitize_rate_limits(result: object) -> dict[str, object]:
    raw = _object(result, "account/rateLimits/read result")
    snapshot = _object(raw.get("rateLimits"), "rateLimits")
    reset_value = raw.get("rateLimitResetCredits")
    reset_summary = None
    if reset_value is not None:
        reset = _object(reset_value, "rateLimitResetCredits")
        reset_summary = {
            "available_count": _count(
                reset.get("availableCount"), "rateLimitResetCredits.availableCount"
            )
        }
    return {
        "plan_type": _optional_string(snapshot.get("planType"), "rateLimits.planType"),
        "limit_id": _optional_string(snapshot.get("limitId"), "rateLimits.limitId"),
        "limit_name": _optional_string(
            snapshot.get("limitName"), "rateLimits.limitName"
        ),
        "primary": _window(snapshot.get("primary"), "rateLimits.primary"),
        "secondary": _window(snapshot.get("secondary"), "rateLimits.secondary"),
        "credits": _credits(snapshot.get("credits")),
        "individual_limit": _individual_limit(snapshot.get("individualLimit")),
        "spend_control_reached": _optional_bool(
            snapshot.get("spendControlReached"), "rateLimits.spendControlReached"
        ),
        "rate_limit_reached_type": _optional_string(
            snapshot.get("rateLimitReachedType"),
            "rateLimits.rateLimitReachedType",
        ),
        "reset_credits": reset_summary,
        "coverage": "account_quota_not_wi_attribution",
    }


class _JsonRpcProcess:
    def __init__(
        self,
        binary: Path,
        codex_home: Path,
        timeout_seconds: float,
    ):
        if not binary.is_absolute() or not binary.is_file():
            raise AppServerAccountError("App Server binary must be an absolute file")
        if not codex_home.is_absolute() or not codex_home.is_dir():
            raise AppServerAccountError("CODEX_HOME must be an absolute directory")
        if timeout_seconds <= 0:
            raise AppServerAccountError("timeout_seconds must be positive")
        env = os.environ.copy()
        env["HOME"] = str(codex_home.parent)
        env["CODEX_HOME"] = str(codex_home)
        try:
            self.process = subprocess.Popen(
                [str(binary), "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise AppServerAccountError("cannot start App Server") from exc
        self.timeout_seconds = timeout_seconds

    def send(self, value: Mapping[str, object]) -> None:
        if self.process.stdin is None:
            raise AppServerAccountError("App Server stdin is unavailable")
        try:
            self.process.stdin.write(_canonical(value).decode("utf-8") + "\n")
            self.process.stdin.flush()
        except OSError as exc:
            raise AppServerAccountError("cannot write App Server request") from exc

    def response(self, request_id: int) -> Mapping[str, Any]:
        if self.process.stdout is None:
            raise AppServerAccountError("App Server stdout is unavailable")
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [self.process.stdout], [], [], max(0, deadline - time.monotonic())
            )
            if not ready:
                break
            line = self.process.stdout.readline()
            if not line:
                break
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AppServerAccountError("App Server returned malformed JSON") from exc
            if isinstance(value, Mapping) and value.get("id") == request_id:
                if value.get("error") is not None:
                    raise AppServerAccountError(
                        f"App Server request {request_id} failed"
                    )
                return _object(value.get("result"), "App Server result")
        raise AppServerAccountError(f"App Server request {request_id} timed out")

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            if stream is not None:
                stream.close()


def validate_app_server_account_snapshot(value: object) -> None:
    if not isinstance(value, Mapping) or value.get("schema") != ACCOUNT_SNAPSHOT_SCHEMA:
        raise AppServerAccountError("unsupported App Server account snapshot")
    generated_at = value.get("generated_at")
    if not isinstance(generated_at, str):
        raise AppServerAccountError("account snapshot generated_at is required")
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppServerAccountError("account snapshot generated_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AppServerAccountError("account snapshot generated_at must be timezone-aware")
    digest = value.get("snapshot_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise AppServerAccountError("account snapshot digest is invalid")
    unsigned = {key: item for key, item in value.items() if key != "snapshot_sha256"}
    if sha256(_canonical(unsigned)).hexdigest() != digest:
        raise AppServerAccountError("account snapshot digest does not match content")
    serialized = _canonical(value).decode("utf-8").lower()
    for forbidden in (
        "codexhome",
        "useragent",
        "threadid",
        "accountid",
        "userid",
    ):
        if forbidden in serialized:
            raise AppServerAccountError(
                f"account snapshot contains forbidden identity field: {forbidden}"
            )


def probe_app_server_account(
    *,
    binary: str | Path,
    codex_home: str | Path,
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    """Read account usage/quota without emitting codex home or account identity."""

    rpc = _JsonRpcProcess(Path(binary), Path(codex_home), timeout_seconds)
    try:
        rpc.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "pulse-computecost",
                        "version": "v2",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        rpc.response(1)
        rpc.send({"method": "initialized"})
        rpc.send({"id": 2, "method": "account/usage/read", "params": None})
        usage = _sanitize_usage(rpc.response(2))
        rpc.send({"id": 3, "method": "account/rateLimits/read", "params": None})
        rate_limits = _sanitize_rate_limits(rpc.response(3))
    finally:
        rpc.close()

    snapshot: dict[str, Any] = {
        "schema": ACCOUNT_SNAPSHOT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "producer": "openai_app_server",
        "usage": usage,
        "rate_limits": rate_limits,
        "privacy": "account_user_thread_identity_and_codex_home_omitted",
    }
    snapshot["snapshot_sha256"] = sha256(_canonical(snapshot)).hexdigest()
    validate_app_server_account_snapshot(snapshot)
    return snapshot
