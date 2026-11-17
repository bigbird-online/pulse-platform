"""Native process launch and source proof; no WorkItem authority or meter state.

The launcher retains its original process while Pi may erase its argv. The
installed extension observes the original SessionManager. Neither side accepts
an actor, WI, human decision or execution grant from the caller.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import time
import subprocess
from typing import cast

from .model import Json, Rejected


def uses_default_pi_route(arguments: list[str]) -> bool:
    commands = {"auth", "config", "install", "list", "remove", "uninstall", "update"}
    preserve = {
        "--help", "-h", "--version", "-v", "--list-models", "--export",
        "--provider", "--model", "--models", "--continue", "-c", "--resume", "-r",
        "--session", "--session-id", "--fork",
    }
    if arguments and arguments[0] in commands:
        return False
    for argument in arguments:
        if argument == "--":
            break
        if argument.split("=", 1)[0] in preserve:
            return False
    return True


def run(entry: Path, argv: list[str]) -> int:
    if not argv or argv[0] != "pi":
        raise Rejected("invalid_input", "native-run requires pi and its native CLI arguments")
    policy_path = entry.parent / "host-policy.json"
    if (
        policy_path.is_symlink()
        or policy_path.stat().st_uid != os.getuid()
        or policy_path.stat().st_mode & 0o022
    ):
        raise Rejected("authority_denied", "Installed native policy is not protected")
    policy = json.loads(policy_path.read_bytes())
    if "production_ledger" in policy:
        from .production_install import require_active_program

        require_active_program(entry, policy)
    profiles = [
        p for p in policy["profiles"] if p.get("standalone") and p.get("runtime", "pi") == "pi"
    ]
    roots = {p["binding_root"] for p in profiles}
    if not profiles or len(roots) != 1:
        raise Rejected("authority_denied", "No installed standalone Pi source binding")
    extension = Path(policy["relay_entry"]).parent.parent / "runtime/pi-work-item-core.mjs"
    if not extension.is_file():
        raise Rejected("source_unavailable", "Installed standalone Pi extension is unavailable")
    arguments = argv[1:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    extensions = ["--extension", str(extension)]
    shared_codex = "pi_codex_oauth" in policy
    if shared_codex:
        oauth_extension = extension.with_name("pi-codex-oauth.mjs")
        if not oauth_extension.is_file():
            raise Rejected("source_unavailable", "Installed Codex account extension is unavailable")
        extensions += ["--extension", str(oauth_extension)]
        if uses_default_pi_route(arguments):
            from .policy import verification_compute

            route = verification_compute(policy)
            arguments = ["--provider", route["provider"], "--model", route["model"], *arguments]
    environment = dict(os.environ)
    # A standalone process cannot inherit another transport's human identity.
    for key in (
        "PULSE_PI_TENANT_ID",
        "PULSE_PI_CODEX_OAUTH_HOST",
        "PULSE_NATIVE_HANDOVER_REF",
        "SCL_RELAY_HOST_CONTEXT",
        "SCL_SESSION_REF",
    ):
        environment.pop(key, None)
    if shared_codex:
        environment["PULSE_PI_CODEX_OAUTH_HOST"] = json.dumps({"entrypoint": str(entry)})
    environment["PULSE_WORK_ITEM_CORE_HOST"] = json.dumps(
        {
            "python": policy["python"],
            "entrypoint": str(entry),
            "standaloneBindingRoot": next(iter(roots)),
            "launcherPid": os.getpid(),
        }
    )
    process = subprocess.Popen(
        [policy["producer_node"], policy["pi_binary"], *extensions, *arguments],
        env=environment,
    )
    previous = {}
    terminate_at: float | None = None

    def forward(number: int, _frame: object) -> None:
        nonlocal terminate_at
        if process.poll() is None:
            terminate_at = terminate_at or time.monotonic() + 10
            process.send_signal(number)

    try:
        for number in (signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, forward)
        while True:
            try:
                return process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if terminate_at is not None and time.monotonic() >= terminate_at:
                    process.kill()
            except KeyboardInterrupt:
                # Foreground terminal SIGINT reaches Pi too; preserve the
                # original child until it exits instead of orphaning its work.
                continue
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def identity(
    policy: Json, profile: Json, native: Json, chain: list[Json], proc: Path, entry: Path
) -> Json | None:
    from .session_sources import process_identity

    path = Path(profile["binding_root"]) / (str(native["pid"]) + ".json")
    if not path.exists():
        return None
    if (
        path.resolve() != path
        or path.stat().st_uid != os.getuid()
        or path.stat().st_mode & 0o077
        or path.stat().st_size > 4096
    ):
        raise Rejected("authority_denied", "Standalone source proof is not protected")
    record = json.loads(path.read_bytes())
    parent = next((p for p in chain if p["pid"] == record.get("launcher_pid")), None)
    if parent is None:
        raise Rejected("authority_denied", "Standalone source has no original launcher ancestor")
    args = parent["argv"]
    programs = [
        i
        for i, value in enumerate(args[:8])
        if value.startswith("/") and Path(value).resolve() == entry
    ]
    valid_command = bool(
        programs and args[programs[0] + 1 : programs[0] + 3] == ["native-run", "pi"]
    )
    python = Path(policy["python"]).resolve()
    valid_python = parent.get("executable") == str(python) or bool(
        args and Path(args[0]).resolve() == python
    )
    if (
        record.get("schema") != "pulse.standalone-session-source/v1"
        or record.get("runtime") != "pi"
        or record.get("process") != process_identity(native["pid"], proc)
        or record.get("launcher_process") != process_identity(parent["pid"], proc)
        or parent["uid"] != native["uid"]
        or not valid_command
        or not valid_python
    ):
        raise Rejected(
            "authority_denied", "Standalone source proof differs from its installed launcher"
        )
    return cast(Json, record)
