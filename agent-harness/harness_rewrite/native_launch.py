"""Persist a child-owned process identity before replacing it with native Pi.

The dispatcher may disappear immediately after Popen. This child therefore owns
the durable spawn proof; exec keeps its PID/start time and cannot race ahead of
the proof. A failed or partial proof never admits an unrecorded native process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from .model import encoded
from .native_compute import write_exclusive
from .runtime_presence import boot_identity, process_stamp


def launch(root: Path, attempt: str | None = None) -> None:
    if root.resolve(strict=True) != root or Path.cwd() != root:
        raise OSError("Dispatch launch directory differs")
    from .native_compute import attempt_root, continuation_config
    target = attempt_root(root, attempt)
    for name in ("launch.json", "launch-config.json", "candidate.json", "spawn.json"):
        if (target / name).is_symlink():
            raise OSError("Dispatch launch proof redirected")
    marker = json.loads((target / "launch.json").read_bytes())
    config = json.loads((target / "launch-config.json").read_bytes())
    candidate = json.loads((root / "candidate.json").read_bytes())
    if (
        config["effect_id"] != marker["effect_id"]
        or config["candidate_id"] != marker["candidate_id"]
        or config["candidate_id"] != candidate["candidate_id"]
        or config["effect_id"] != os.environ.get("HARNESS_REWRITE_DISPATCH")
        or marker["phase"] != "launch_requested"
    ):
        raise OSError("Dispatch launch intention differs")
    if config.get("runner") == "pi-sdk/v1":
        from .native_compute import launch_config

        config = continuation_config(root, attempt) if attempt else launch_config(root)
    if os.environ.get("HARNESS_REWRITE_ATTEMPT") != attempt:
        raise OSError("Native attempt differs from its launch")
    if attempt and marker.get("boot") != boot_identity():
        raise OSError("Continuation was prepared on another boot")
    argv = config["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(arg, str) for arg in argv)
        or not Path(argv[0]).is_absolute()
        or (
            config.get("runner") != "pi-sdk/v1"
            and (
                argv.count("--session-id") != 1
                or argv[argv.index("--session-id") + 1] != config["native_session_id"]
            )
        )
    ):
        raise OSError("Dispatch native command differs")
    # Read the identity before creating the proof: a host that cannot name
    # this process leaves no proof file at all, never an empty one.
    proof = encoded(
        {
            "effect_id": config["effect_id"],
            "native_session_id": config["native_session_id"],
            "process": process_stamp(os.getpid()),
        }
    )
    # Exclusive creation also refuses an accidental second wrapper. Never
    # replace a prior process proof, including one from a failed exec. The
    # proof appears whole or not at all (write_exclusive), and exec happens
    # only after it is fsynced, so this wrapper never leaves an empty one.
    write_exclusive(target / "spawn.json", proof)
    os.execve(argv[0], argv, dict(os.environ))


def main() -> None:
    launch(Path(sys.argv[2]), sys.argv[3] if len(sys.argv) == 4 else None)


if __name__ == "__main__":
    main()
