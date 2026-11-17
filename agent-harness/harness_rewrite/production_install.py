"""Activate an exact program against the original production ledger, without WI writes."""

from __future__ import annotations

import fcntl
import os
from typing import Callable
from pathlib import Path
from .model import Json

import json
import subprocess
import inspect
import time
from .storage_contract import inspect_contract


def require_active_program(entry: Path, policy: Json) -> None:
    """A retired release cannot keep writing through its old absolute entry."""
    import hashlib
    import json
    from .model import Rejected, encoded

    release = entry.parent
    root = release.parent.parent
    try:
        receipt = json.loads((release / "installation.json").read_bytes())
        if (
            release.parent.name != "releases"
            or (root / "active" / "harness.pyz").resolve(strict=True) != entry
            or receipt["bundle_sha256"] != hashlib.sha256(entry.read_bytes()).hexdigest()
            or receipt["policy_sha256"] != hashlib.sha256(encoded(policy)).hexdigest()
        ):
            raise ValueError("Inactive program")
    except (OSError, ValueError, KeyError) as exc:
        raise Rejected(
            "program_changed", "Use the currently installed native production entry"
        ) from exc


PROBE_ATTEMPTS = 3
PROBE_RETRY_SECONDS = 3


def run_probe(command: list[str],
              runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
              sleep: Callable[[float], None] = time.sleep) -> subprocess.CompletedProcess[bytes]:
    """Read the storage contract with the exact program. The read goes to the
    production remote; a transient failure is retried, an incompatible program
    fails every attempt."""
    for attempt in range(PROBE_ATTEMPTS):
        if attempt:
            sleep(PROBE_RETRY_SECONDS)
        result = runner(command, capture_output=True, timeout=60)
        if not result.returncode:
            return result
    lines = [line for line in result.stderr.decode(errors="replace").splitlines() if line.strip()]
    raise ValueError(
        "The exact proposed program cannot read the production storage contract; active entry preserved"
        + (f" ({lines[-1].strip()[:300]})" if lines else "")
    )


def activate(
    root: Path, release: Path, policy: Json, receipt: Json, fault: Callable[[str], None],
    *, relay_release: Path | None = None, prepare_only: bool = False,
    _already_locked: bool = False, _rollback: bool = False
) -> Json:
    # Serializes local program installation only. Business concurrency is the
    # production ref transaction, not this host lock.
    fd = os.open(root / ".production-install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as stream:
        if not _already_locked:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        # The probe imports protocol constants and the Git reader from the exact
        # target bundle. Its orchestration comes from this installer so a sealed
        # compatible predecessor need not contain a newly added probe module.
        probe = """from __future__ import annotations
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from harness_rewrite.model import Rejected
from harness_rewrite.production_ledger import ProductionLedger, FORMAT, SCHEMA
from harness_rewrite.production_segments import SCHEMA as SEGMENT_SCHEMA
from harness_rewrite.observations import HEAD, SCHEMA as OBSERVATION_SCHEMA
""" + inspect.getsource(inspect_contract) + """
policy=json.loads(Path(sys.argv[2]).read_bytes())
ledger=ProductionLedger(policy['production_ledger'],policy['namespace'])
print(json.dumps(inspect_contract(ledger)))
"""
        result = run_probe([
            policy["python"], "-I", "-B", "-c", probe,
            str(release / "harness.pyz"), str(release / "host-policy.json"),
        ])
        evidence = json.loads(result.stdout)
        count = evidence["count"]
        fault("after_fact_gate")
        active = root / "active"
        if active.is_symlink() and not _rollback:
            from .policy import compatible, load

            previous = active.resolve(strict=True)
            if previous.parent != root / "releases":
                raise ValueError("Production installation pointer escaped its owned root")
            compatible(load(previous / "host-policy.json"), policy, relay_release=relay_release)
        if prepare_only:
            return {
                **receipt, "status": "prepared", "fact_storage": "production_git",
                "independent_database": False, "historical_items_read": count,
                "compatibility_scope": evidence['compatibility_scope'],
                "historical_replay": evidence['historical_replay'],
                "observed_revision": evidence["revision"], "business_mutation": False,
            }
        fault("before_pointer")
        pointer = root / (".activate-" + release.name)
        if pointer.is_symlink():
            if os.readlink(pointer) != "releases/" + release.name:
                raise ValueError("Activation target differs")
        else:
            pointer.symlink_to("releases/" + release.name)
        os.replace(pointer, active)
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fault("after_pointer")
    return {
        **receipt,
        "active": str(active / "harness.pyz"),
        "fact_storage": "production_git",
        "independent_database": False,
        "historical_items_read": count,
        "compatibility_scope": evidence['compatibility_scope'],
        "historical_replay": evidence['historical_replay'],
        "observed_revision": evidence["revision"],
        "ledger": policy["production_ledger"],
        "business_mutation": False,
    }


def restore(root: Path, expected: str, previous: str | None) -> None:
    """Undo this operator deployment only; never restore or rewrite WI facts."""
    import json
    from .policy import load
    from .release import sha, verify

    fd = os.open(root / ".production-install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        active = root / "active"
        current = os.readlink(active) if active.is_symlink() else None
        if current == previous:
            return
        if current != "releases/" + expected:
            raise ValueError("Native core changed after this deployment; rollback refused")
        if previous is None:
            raise ValueError("First production adoption has no compatible predecessor; retain the native reader and keep writers stopped")
        prior = (root / previous).resolve(strict=True)
        if prior.parent != root / "releases" or previous != "releases/" + prior.name:
            raise ValueError("Rollback predecessor is outside managed releases")
        receipt = json.loads((prior / "installation.json").read_bytes())
        bundle = prior / "harness.pyz"
        verify(bundle)
        if sha(bundle.read_bytes()) != receipt["bundle_sha256"]:
            raise ValueError("Rollback predecessor bytes differ")
        policy = load(prior / "host-policy.json")
        current_policy = load(active / "host-policy.json")
        if any(policy.get(k) != current_policy.get(k) for k in ("production_ledger", "namespace")):
            raise ValueError("Rollback would change the production authority")
        # Restore checks the predecessor's protocol, never all business records.
        activate(root, prior, policy, receipt, lambda stage: None, prepare_only=True,
                 _already_locked=True, _rollback=True)
        pointer = root / ".restore-production"
        if pointer.is_symlink():
            pointer.unlink()
        pointer.symlink_to(previous)
        os.replace(pointer, active)
