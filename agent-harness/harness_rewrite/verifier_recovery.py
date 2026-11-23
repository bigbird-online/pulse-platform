"""Continue an interrupted assigned process without issuing another assignment.

Business responsibility stays in the journal. These append-only launch proofs
record physical attempts; neither a live PID nor a session file is a verdict.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pi_dispatch import PiDispatch

from .model import Json, Query, encoded
from .native_compute import (LAUNCH_FENCE, RUNNER, checksum, runner_bytes, launch_config,
                             session_proof, unspawned, write_exclusive)
from .runtime_presence import boot_identity, process_stamp, process_state


def presence(receipt: Json, proc: Path = Path("/proc")) -> str:
    if not receipt or receipt.get('schema') == LAUNCH_FENCE:
        # A fence won exclusive creation before a native launch proof; an
        # empty proof was left by a wrapper that failed before exec.
        return 'absent'
    expected = receipt["process"]
    if boot_identity(proc) != expected["boot"]:
        return "absent"
    try:
        current = process_stamp(expected["pid"], proc)
        state = process_state(expected["pid"], proc)
        return "running" if current == expected and state not in {"Z", "X", "x"} else "absent"
    except FileNotFoundError:
        return "absent"
    except (OSError, ValueError, IndexError):
        return "unknown"


def recover(adapter: PiDispatch, *, proc: Path = Path("/proc")) -> list[Json]:
    # The caller supplies the same admitted worker as dispatch; no public PID,
    # Session, model, scope or approval overrides are accepted here.
    current = adapter.kernel.inspect(Query(adapter.wi), adapter.host(()))
    from .effects import admission_fenced
    from .migration import observation_blocks_effect
    if admission_fenced(current):
        return []
    view = current["result"]["current_observation"]
    candidates = view.get("candidates", {}).get("items", [])
    results = []
    for effect in current["effects"]:
        if effect["effect_kind"] != "verification_dispatch" or effect["status"] == "cancelled":
            continue
        candidate = next((c for c in candidates if c["id"] == effect["candidate_id"]), None)
        if (candidate is None or view["candidates"]["active"] != effect["candidate_id"] or candidate["verification"] is not None
                or observation_blocks_effect(view, effect["effect_id"])):
            continue
        root = adapter.directory(effect)
        if not (root / "launch.json").exists():
            continue  # The existing effect dispatcher owns first launch.
        detail: Json = {"candidate_id": effect["candidate_id"], "assignment_id": effect["assignment_id"],
                        "native_session_id": effect["native_session_id"], "verdict": None}
        try:
            fd = os.open(root / ".continuation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                detail.update(continue_locked(adapter, effect, root, proc))
        except (OSError, ValueError, KeyError, TypeError):
            detail.update(state="unknown", reason="original_continuation_proof_unavailable")
        results.append(detail)
    return results


def continue_locked(adapter: PiDispatch, effect: Json, root: Path, proc: Path) -> Json:
    config = launch_config(root)
    if (config["effect_id"] != effect["effect_id"] or config["candidate_id"] != effect["candidate_id"]
            or config["native_session_id"] != effect["native_session_id"]):
        raise ValueError("Assignment differs")
    attempts = root / "attempts"
    if attempts.is_symlink():
        raise ValueError("Attempt directory redirects")
    attempts.mkdir(mode=0o700, exist_ok=True)
    prior = sorted(attempts.iterdir())
    if any(p.name != f"{i:06d}" or p.resolve() != p for i, p in enumerate(prior, 1)):
        raise ValueError("Attempt history differs")
    last = prior[-1] if prior else root
    spawn = last / "spawn.json"
    if spawn.is_symlink():
        raise ValueError("Process proof redirects")
    if not spawn.exists():
        # Give a normally scheduled wrapper time to write its claim. This wait
        # is not absence proof; only winning O_EXCL below proves non-execution.
        marker_path = last / 'launch.json'
        if marker_path.exists():
            marker = json.loads(marker_path.read_bytes())
            at = (datetime.fromisoformat(marker['at']).timestamp() if marker.get('at')
                  else marker_path.stat().st_mtime)
            if datetime.now(timezone.utc).timestamp() - at < 60:
                return {'state': 'unknown', 'reason': 'original_launch_result_unconfirmed',
                        'retry_after_seconds': 60}
        # The native wrapper MUST exclusively create and fsync spawn.json before
        # exec. Winning that same O_EXCL boundary proves no native execution and
        # prevents a delayed old wrapper from starting later (also old bundles).
        # This is an explicit no-spawn fence, not an invented PID or verdict.
        fence = {'schema': LAUNCH_FENCE,
                 'effect_id': effect['effect_id'], 'native_session_id': config['native_session_id'],
                 'reason': 'continuation_retired_unspawned_attempt'}
        try:
            write_exclusive(spawn, encoded(fence))
        except FileExistsError:
            pass  # Concurrent child won: observe its proof, never duplicate it.
    raw = spawn.read_bytes()
    if not raw and not unspawned(spawn, proc):
        # With procfs an empty proof may belong to a paused older wrapper that
        # can still write and exec (see empty_proof_unspawned): never continue.
        return {'state': 'unknown', 'reason': 'original_launch_result_unconfirmed'}
    # Here an empty proof means the wrapper failed before exec (see unspawned).
    receipt: Json = json.loads(raw) if raw else {}
    if raw and (receipt["effect_id"] != effect["effect_id"]
                or receipt["native_session_id"] != config["native_session_id"]):
        raise ValueError("Process assignment differs")
    state = presence(receipt, proc)
    if state != "absent":
        return {"state": state, "reason": "original_process_" + state}
    files = list((root / "session").glob("*_" + config["native_session_id"] + ".jsonl"))
    never_started = not files and all((p / 'spawn.json').exists() and unspawned(p / 'spawn.json', proc)
        for p in [root, *prior])
    if len(files) != 1 and not never_started:
        return {"state": "unknown", "reason": "original_session_unavailable"}
    if files:
        session_proof(root, files[0], config)
    # Cumulative retries are bounded per installed adapter version. A changed
    # deployment can repair a deterministic failure without resetting history.
    resource = runner_bytes()
    same_version = [p for p in prior if not (p / 'launch-config.json').exists()
                    or json.loads((p / "launch-config.json").read_bytes())["runner_sha256"] == checksum(resource)]
    if len(same_version) >= 4:
        return {"state": "interrupted", "reason": "repair_verifier_runtime_retry_budget_exhausted", "attempts": len(prior)}
    now = datetime.now(timezone.utc)
    if prior and (last / 'launch.json').exists():
        marker = json.loads((last / "launch.json").read_bytes())
        elapsed = (now - datetime.fromisoformat(marker["at"])).total_seconds()
        if elapsed < min(1800, 30 * 2 ** (len(same_version))):
            return {"state": "interrupted", "reason": "continuation_backoff", "attempts": len(prior)}
    # Recheck facts and original process immediately before making an intention.
    fresh = adapter.kernel.inspect(Query(adapter.wi), adapter.host(()))
    from .effects import admission_fenced
    from .migration import observation_blocks_effect
    if admission_fenced(fresh):
        return {"state": "unknown", "reason": "continuation_admission_changed"}
    view = fresh["result"]["current_observation"]
    candidate = next((c for c in view["candidates"]["items"] if c["id"] == effect["candidate_id"]), None)
    if candidate is None or view["candidates"]["active"] != effect["candidate_id"] or candidate["verification"] is not None or observation_blocks_effect(view, effect["effect_id"]):
        return {"state": "settled", "reason": "original_responsibility_no_longer_pending"}
    if presence(receipt, proc) != "absent":
        return {"state": "unknown", "reason": "original_process_presence_changed"}
    attempt = f"{len(prior) + 1:06d}"
    target = attempts / attempt
    target.mkdir(mode=0o700)
    adapter.fixed_file(target / RUNNER, resource)
    adapter.fixed_file(target / "launch-config.json", encoded({**config,
        "argv": [config["argv"][0], str(target / RUNNER)], "runner_sha256": checksum(resource),
        "original_config_sha256": checksum((root / "launch-config.json").read_bytes()),
        "session_file": str(files[0]) if files else None, "attempt": attempt}))
    adapter.fixed_file(target / "launch.json", encoded({"effect_id": effect["effect_id"],
        "candidate_id": effect["candidate_id"], "phase": "launch_requested", "at": now.isoformat(),
        "previous_process": receipt.get("process"),
        "previous_launch_fenced": receipt.get('schema') == LAUNCH_FENCE,
        "previous_launch_failed_before_exec": not raw,
        "boot": boot_identity(proc)}))
    from .pi_dispatch import dispatch_environment

    environment = {**dispatch_environment(), "PI_CODING_AGENT_DIR": config["agent_dir"],
        "HARNESS_REWRITE_DISPATCH": effect["effect_id"], "HARNESS_REWRITE_ATTEMPT": attempt}
    with (target / "native-output.jsonl").open("xb") as out:
        subprocess.Popen([adapter.policy["python"], "-I", "-B", "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); from harness_rewrite.native_launch import main; main()",
            str(adapter.bundle), str(root), attempt], cwd=root, env=environment,
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    return {"state": "continuation_requested", "reason": "same_assignment_same_session", "attempt": attempt}
