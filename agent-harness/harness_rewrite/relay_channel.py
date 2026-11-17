"""Deterministic slash input from the installed Relay, for every native runtime.

This adapter supplies only an authenticated original input and actual child.
The ordinary channel parser, Host and Kernel still decide and execute the work.
"""

import json
import os
import re
from pathlib import Path
from typing import NoReturn

from . import channel
from .model import Json, fields, Rejected
from .native_host import NativeHost
from .relay_session import native_child, private_json, relay_parent, require, retired_source


def installed_relay(entry: Path, proc: Path, caller: int | None) -> tuple[Path, Json, int]:
    """The operator policy and the installed Relay that directly invoked this core."""
    policy_path = entry.parent / "host-policy.json"
    info = policy_path.stat()
    require(
        not policy_path.is_symlink() and info.st_uid == os.getuid() and not info.st_mode & 0o022,
        "Host policy is not operator owned",
    )
    policy = json.loads(policy_path.read_bytes())
    parent, direct = relay_parent(policy, proc, caller or os.getpid())
    require(direct, "Only the installed Relay can submit a channel command")
    if "production_ledger" in policy:
        from .production_install import require_active_program

        require_active_program(entry, policy)
    return policy_path, policy, parent


def access_host(
    entry: Path, request: Json, *, proc: Path = Path("/proc"), caller: int | None = None
) -> NativeHost:
    """Relay reads the access granted to its actual native child, before any model turn."""
    fields(request, {"pid"})
    policy_path, _, parent = installed_relay(entry, proc, caller)
    return native_child(policy_path, request["pid"], parent, proc, pre_model=True)


def standing_access(
    entry: Path, request: Json, *, proc: Path = Path("/proc"), caller: int | None = None
) -> Json:
    """Relay reads the standing grants of every Agent, to apply at each session start.

    One read serves all Agents, since it replays every WI journal; the kernel
    rechecks family-rule grants against the installed workspace layout. It
    decides and changes nothing.
    """
    from .access import agent_workspaces
    from .kernel import Kernel

    fields(request, set())
    _, policy, _ = installed_relay(entry, proc, caller)
    storage = None
    if "production_ledger" in policy:
        from .production_ledger import ProductionLedger

        storage = ProductionLedger(policy["production_ledger"], policy["namespace"])

    def no_session(_: object) -> NoReturn:
        raise PermissionError("Standing access is not a session read")

    kernel = Kernel(
        Path(policy["database"]) if storage is None else None, no_session, storage=storage
    )
    return kernel.standing_access(policy["namespace"], agent_workspaces(policy))


def command_host(
    entry: Path, request: Json, *, proc: Path = Path("/proc"), caller: int | None = None
) -> NativeHost:
    fields(request, {"ref"}, {"pid", "native_id", "tenant", "phase"})
    phase = request.get("phase")
    require(
        phase in (None, "leave", "enter", "readback"), "Expected switch phase leave, enter or readback"
    )
    require(
        set(request) - {"phase"} in ({"ref", "pid"}, {"ref", "native_id", "tenant"}),
        "Expected exactly one actual or retained native source",
    )
    policy_path, policy, parent = installed_relay(entry, proc, caller)
    if "pid" in request:
        # Slash input precedes model dispatch, so a new session may have no log yet.
        host = native_child(policy_path, request["pid"], parent, proc, pre_model=True)
    else:
        # Codex CLI has no process between turns. Its original Relay observation
        # and native log identify the same conversation; no live process or
        # execution grant is invented. Ordinary tools cannot use this entry.
        original_source = retired_source(policy, request["native_id"], request["tenant"])
        from .policy import execution, verification_compute

        verification_compute(policy)
        host = NativeHost.__new__(NativeHost)
        host.policy, host.execution, host.proc = policy, execution(policy), proc
        host.profile, host.runtime = original_source.profile, original_source.runtime
        host.entry = original_source.entry
        host.session_file = original_source.session_file
        host.native_header = {"type": "session", **original_source.native_header}
        host.environment = original_source.environment
        host.native_pid, host.native_stamp, host.relay_command_input = None, None, None
        host.relay_agent_switch = None
        host.bind_context(original_source.session_file.stat().st_size)
    ref = request["ref"]
    require(
        isinstance(ref, str) and re.fullmatch(r"[a-f0-9]{64}", ref) is not None,
        "Expected original channel input reference",
    )
    # Read before granting the narrow pre-model exception. No caller-supplied
    # text, principal, WI, action, generation or approval is accepted here.
    try:
        original = private_json(Path(policy["human_inputs"]) / (ref + ".json"))
    except FileNotFoundError as exc:
        # No human input: the ref may name an Agent's own committed request_switch.
        from . import agent_switch

        notice = agent_switch.read(policy, ref)
        if notice is None:
            raise Rejected(
                "human_input_unavailable",
                "Original channel input custody is unavailable; retain the same request",
            ) from exc
        agent_switch.check(notice, policy, host, phase)
        host.relay_agent_switch = notice
        host.relay_switch_phase = phase
        return host
    parsed = channel.parse(original["source"]["message"])
    require(parsed["action"] != "rollover", "Use the native session controller for rollover")
    require((parsed["action"] == "switch") == (phase is not None),
            "A switch phase belongs to /wi switch only, and /wi switch needs one")
    host.relay_command_input = ref
    host.relay_switch_phase = phase
    host.human_input(ref, latest=True)  # same digest and tenant checks as native tools
    return host
