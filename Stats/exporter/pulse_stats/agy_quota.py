"""Read the agy (Antigravity CLI) account quota from ``agy -p /quota``.

The command answers locally without a model turn; its JSON ``command.data``
lists quota groups, each with a weekly bucket and, except Gemini 4 Argon, a
5-hour bucket. The answer has no published contract, so it is validated
strictly: anything unexpected is an unavailable fact with a stable reason,
never a simulated value.

Every failed read (command missing, timeout, non-zero exit, unexpected
output) replaces the previous reading with an unavailable fact carrying the
reason and the time of the failed read, so an old figure is never shown as a
new one. An available reading is current for ``MAX_AGE_SECONDS`` and each
bucket only until its own reset.

The quota is the account's pool, not per-agent cost or tokens, and never
joins a cost total.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from typing import Any, Callable

SCHEMA = "pulse.stats.agy_quota/v1"
SOURCE = "agy_cli_quota"
ARGS = ("-p", "/quota", "--output-format", "json")
MAX_BYTES = 65536
MAX_GROUPS = 16
MAX_BUCKETS = 8
MAX_NAME = 64
WINDOWS = frozenset({"weekly", "5h"})
# A weekly bucket resets within 7 days; a longer horizon means the meaning changed.
MAX_RESET_SECONDS = 8 * 24 * 3600
FUTURE_TOLERANCE_SECONDS = 60
# The reader refreshes every publication cycle; an older reading is stale.
MAX_AGE_SECONDS = 3600
TIMEOUT_SECONDS = 45
# After a timeout kill, how long to wait for the killed process to be reaped.
KILL_WAIT_SECONDS = 5

# Every read failure is published as unavailable with one of these codes.
READ_FAILURES = frozenset({
    "agy_command_unavailable", "agy_command_timeout", "agy_command_failed",
    "quota_output_too_large", "quota_schema_changed",
})

# (argv, env, cwd, timeout) -> (exit code, stdout bytes)
Runner = Callable[[list[str], dict[str, str], str, float], tuple[int, bytes]]


class QuotaError(ValueError):
    """Stable error code only: never command output or environment values."""


def subprocess_runner(argv: list[str], env: dict[str, str], cwd: str,
                      timeout: float) -> tuple[int, bytes]:
    """Run once in its own process group; a timeout kills the whole group.

    Every wait is bounded, so a stuck command cannot hold the caller.
    """
    try:
        process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
    except OSError:
        raise QuotaError("agy_command_unavailable") from None
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        # A descendant that left the group may still hold the pipe open, so
        # close it instead of draining it, and bound the reap.
        try:
            process.stdout.close()
        except OSError:
            pass
        try:
            process.wait(timeout=KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        raise QuotaError("agy_command_timeout") from None
    return process.returncode, stdout


def command_env(home: Path) -> dict[str, str]:
    """The account HOME locates the agy launcher's state; nothing else is inherited."""
    env = {"HOME": str(home), "PATH": os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"}
    for name in ("LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _instant(value: Any) -> int:
    if not isinstance(value, str):
        raise QuotaError("quota_schema_changed")
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise QuotaError("quota_schema_changed") from None
    if at.tzinfo is None:
        raise QuotaError("quota_schema_changed")
    return int(at.timestamp())


def _name(value: Any) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > MAX_NAME
            or not value.isprintable()):
        raise QuotaError("quota_schema_changed")
    return value.strip()


def _fraction(value: Any) -> float:
    # Range before finiteness: a huge JSON integer overflows math.isfinite.
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        raise QuotaError("quota_schema_changed")
    return float(value)


def projection(response: Any, observed: datetime) -> dict[str, Any]:
    """Validate the ``/quota`` JSON answer and project the published fact."""
    if not isinstance(response, dict):
        raise QuotaError("quota_schema_changed")
    if response.get("status") != "SUCCESS":
        raise QuotaError("agy_command_failed")
    command = response.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list) or not 0 < len(groups) <= MAX_GROUPS:
        raise QuotaError("quota_schema_changed")
    now = observed.timestamp()
    published, names = [], set()
    for group in groups:
        if not isinstance(group, dict):
            raise QuotaError("quota_schema_changed")
        name = _name(group.get("name"))
        buckets = group.get("buckets")
        if name in names or not isinstance(buckets, list) or not 0 < len(buckets) <= MAX_BUCKETS:
            raise QuotaError("quota_schema_changed")
        names.add(name)
        windows, rows = set(), []
        for bucket in buckets:
            if not isinstance(bucket, dict) or not isinstance(bucket.get("window"), str) \
                    or bucket["window"] not in WINDOWS:
                raise QuotaError("quota_schema_changed")
            window = bucket["window"]
            if window in windows or "remaining_fraction" not in bucket or "reset_time" not in bucket:
                raise QuotaError("quota_schema_changed")
            windows.add(window)
            resets = _instant(bucket["reset_time"])
            if not now - FUTURE_TOLERANCE_SECONDS < resets <= now + MAX_RESET_SECONDS:
                raise QuotaError("quota_schema_changed")
            rows.append({"window": window, "remaining_fraction": _fraction(bucket["remaining_fraction"]),
                         "resets_at": resets})
        # Every group has a weekly bucket; only some (not Argon) have a 5-hour one.
        if "weekly" not in windows:
            raise QuotaError("quota_schema_changed")
        published.append({"name": name, "buckets": rows})
    return {"schema": SCHEMA, "source": SOURCE, "status": "available",
            "observed_at": observed.isoformat(), "groups": published}


def failure_document(code: str, observed: datetime) -> dict[str, Any]:
    """The published fact for a failed read; it replaces any earlier reading."""
    if code not in READ_FAILURES:
        raise QuotaError("quota_schema_changed")
    return {"schema": SCHEMA, "source": SOURCE, "status": "unavailable",
            "observed_at": observed.isoformat(), "reason": code}


def read_quota(*, command: Path, home: Path, runner: Runner | None = None,
               now: Callable[[], datetime] | None = None,
               timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """One ``agy -p /quota`` read; raises ``QuotaError`` with a ``READ_FAILURES`` code."""
    clock = now or (lambda: datetime.now(timezone.utc))
    run = runner or subprocess_runner
    if not command.is_absolute() or not home.is_absolute():
        raise QuotaError("agy_command_unavailable")
    # A scratch directory, so the CLI never sees or writes a real workspace.
    with tempfile.TemporaryDirectory(prefix="agy-quota-") as cwd:
        code, stdout = run([str(command), *ARGS], command_env(home), cwd, timeout)
    observed = clock()
    if code != 0:
        raise QuotaError("agy_command_failed")
    if len(stdout) > MAX_BYTES:
        raise QuotaError("quota_output_too_large")
    try:
        return projection(json.loads(stdout), observed)
    except QuotaError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError, AttributeError):
        # Any other malformed answer is a changed schema, never a crash.
        raise QuotaError("quota_schema_changed") from None


def observed_at(document: dict[str, Any]) -> datetime:
    try:
        at = datetime.fromisoformat(document["observed_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise QuotaError("invalid_quota_time") from None
    if at.tzinfo is None:
        raise QuotaError("invalid_quota_time")
    return at


def validate(document: Any, now: datetime) -> dict[str, Any]:
    """Validate a published fact; an available reading must be recent."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise QuotaError("invalid_quota_document")
    if document.get("source") != SOURCE:
        raise QuotaError("invalid_quota_source")
    at = observed_at(document)
    if (at - now).total_seconds() > FUTURE_TOLERANCE_SECONDS:
        raise QuotaError("quota_from_future")
    common = {"schema", "source", "status", "observed_at"}
    status = document.get("status")
    if status == "unavailable":
        if set(document) != common | {"reason"} or document["reason"] not in READ_FAILURES:
            raise QuotaError("invalid_quota_document")
        return document
    if status != "available" or set(document) != common | {"groups"}:
        raise QuotaError("invalid_quota_document")
    groups = document["groups"]
    if not isinstance(groups, list) or not 0 < len(groups) <= MAX_GROUPS:
        raise QuotaError("invalid_quota_document")
    for group in groups:
        if (not isinstance(group, dict) or set(group) != {"name", "buckets"}
                or not isinstance(group["name"], str) or not 0 < len(group["name"]) <= MAX_NAME
                or not isinstance(group["buckets"], list) or not 0 < len(group["buckets"]) <= MAX_BUCKETS):
            raise QuotaError("invalid_quota_document")
        for bucket in group["buckets"]:
            if (not isinstance(bucket, dict) or set(bucket) != {"window", "remaining_fraction", "resets_at"}
                    or not isinstance(bucket["window"], str) or bucket["window"] not in WINDOWS
                    or type(bucket["resets_at"]) is not int
                    or type(bucket["remaining_fraction"]) not in (int, float)
                    or not 0 <= bucket["remaining_fraction"] <= 1
                    or not math.isfinite(bucket["remaining_fraction"])):
                raise QuotaError("invalid_quota_window")
    if (now - at).total_seconds() >= MAX_AGE_SECONDS:
        raise QuotaError("quota_reading_stale")
    return document


def current_buckets(document: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Flat rows for buckets whose window has not reset yet, in published order."""
    return [{"group": group["name"], **bucket}
            for group in document["groups"] for bucket in group["buckets"]
            if bucket["resets_at"] > now.timestamp()]


def load_quota(path: Path, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    """(document, reason). An unavailable fact returns itself with its reason."""
    try:
        if (path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077
                or path.stat().st_size > MAX_BYTES):
            raise QuotaError("quota_source_unavailable")
        document = validate(json.loads(path.read_bytes()), now)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return None, str(exc) if isinstance(exc, QuotaError) else "quota_source_unavailable"
    return document, None if document["status"] == "available" else document["reason"]
