"""Trusted runtime snapshots; observations never grant or revoke authority."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess

from .model import Json, RuntimeObservation, digest


PROC = Path("/proc")


def procfs_absent(proc: Path) -> bool:
    """Only the real host without procfs (macOS) uses ps/sysctl; fixtures keep a fake /proc."""
    return proc == PROC and not proc.is_dir()


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except subprocess.SubprocessError as error:
        raise OSError("Host process metadata is unavailable") from error


def _ps(pid: int, field: str) -> str:
    # Like a missing /proc/<pid>/stat: ps prints nothing and exits 1 for an
    # ended PID, so callers keep one FileNotFoundError meaning "not running".
    result = _run(["ps", "-p", str(pid), "-o", field + "="])
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise FileNotFoundError("Process is not running: " + str(pid))
    return value


def process_started(pid: int) -> str:
    """macOS process birth: `ps lstart`, the same text Relay records for its proofs."""
    return _ps(pid, "lstart")


def boot_identity(proc: Path = PROC) -> str:
    """One value per host boot: Linux boot_id, or macOS kern.bootsessionuuid.

    kern.boottime is not used: XNU shifts it when the wall clock is stepped, so
    one boot could look like two and a live process like an absent one.
    """
    if procfs_absent(proc):
        boot = _run(["sysctl", "-n", "kern.bootsessionuuid"]).stdout.strip()
    else:
        boot = (proc / "sys/kernel/random/boot_id").read_text().strip()
    if not boot:
        raise ValueError("Boot identity is unavailable")
    return boot


def process_state(pid: int, proc: Path = PROC) -> str:
    """Scheduler state letter; macOS `ps state` maps U (uninterruptible) to D."""
    if procfs_absent(proc):
        letter = _ps(pid, "state")[0]
        return "D" if letter == "U" else letter
    return (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]


def process_stamp(pid: int, proc: Path = PROC) -> Json:
    """Comparable identity of one process on this host.

    Linux keeps {pid, start_ticks, boot} so existing proofs still compare.
    macOS has no procfs and returns {pid, started, boot}; a record written on
    one platform never equals a live stamp from the other.
    """
    if procfs_absent(proc):
        return {"pid": pid, "started": process_started(pid), "boot": boot_identity(proc)}
    fields = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    ticks = fields[19]
    boot = boot_identity(proc)
    if not ticks.isdecimal():
        raise ValueError("Process identity is unavailable")
    return {"pid": pid, "start_ticks": ticks, "boot": boot}


def capture_native(
    pid: int, expected: Json | None, principal: Json, proc: Path
) -> RuntimeObservation:
    presence, reason, proof = "unknown", "admission_stamp_unavailable", None
    if expected is not None:
        try:
            before = process_stamp(pid, proc)
            state = process_state(pid, proc)
            after = process_stamp(pid, proc)
            if before != expected or after != before:
                reason = "runtime_identity_changed"
            elif state in {"Z", "X", "x"}:
                reason = "runtime_not_observed_running"
            elif state not in {"R", "S", "D", "T", "t", "I", "W", "P"}:
                reason = "runtime_metadata_invalid"
            else:
                presence, reason = (
                    "observed_present",
                    "admitted_native_process_observed",
                )
                proof = digest({"principal": principal, "process": before})
        except (OSError, ValueError, IndexError, UnicodeError):
            reason = "runtime_metadata_unavailable"
    return RuntimeObservation(
        digest(principal),
        datetime.now(timezone.utc).isoformat(),
        presence,
        reason,
        "linux_native_process",
        proof,
    )


def public(
    principal: Json, observations: tuple[RuntimeObservation, ...], now: datetime | None
) -> Json:
    result: Json = {
        "presence": "unknown",
        "reason": "journal_interaction_is_not_runtime_liveness",
        "runtime_observation": None,
    }
    matching = [v for v in observations if v.principal_digest == digest(principal)]
    if not matching:
        return result
    if len(matching) != 1:
        result["reason"] = "ambiguous_runtime_observation"
        return result
    value = matching[0]
    try:
        at = datetime.fromisoformat(value.observed_at)
        if at.tzinfo is None:
            raise ValueError("Missing observation timezone")
    except (ValueError, TypeError):
        result["reason"] = "runtime_observation_time_unavailable"
        return result
    if value.source != "linux_native_process" or value.presence not in {
        "observed_present",
        "unknown",
    }:
        result["reason"] = "unsupported_runtime_observation"
        return result
    allowed_reasons = {
        "admitted_native_process_observed",
        "admission_stamp_unavailable",
        "runtime_identity_changed",
        "runtime_not_observed_running",
        "runtime_metadata_invalid",
        "runtime_metadata_unavailable",
    }
    if value.reason not in allowed_reasons:
        result["reason"] = "unsupported_runtime_observation"
        return result
    result["runtime_observation"] = {
        "observed_at": value.observed_at,
        "source": value.source,
        "temporal_scope": "current_observation",
        "proof_digest": None,
    }
    if now is None:
        result["reason"] = "query_clock_unavailable"
        return result
    if now < at:
        result["reason"] = "query_clock_before_runtime_observation"
        return result
    if value.presence == "observed_present" and (
        value.reason != "admitted_native_process_observed"
        or not isinstance(value.proof_digest, str)
        or re.fullmatch("[a-f0-9]{64}", value.proof_digest) is None
    ):
        result["reason"] = "runtime_observation_proof_unavailable"
        return result
    result["presence"], result["reason"] = value.presence, value.reason
    if value.presence == "observed_present":
        result["runtime_observation"]["proof_digest"] = value.proof_digest
    return result
