"""Continued original execution recorded in the production journal.

Continuing an old-kernel execution is retired with the old track: the intent is
rejected. Historical ExistingExecutionContinued events still replay, and their
retained repository bindings stay admissible for the continued allocations.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import subprocess
from typing import cast
from urllib.parse import urlsplit

from .model import Decision, HostContext, Json, Rejected

INTENT = "review_existing_execution"


def require(ok: object, reason: str) -> None:
    if not ok:
        raise Rejected("existing_execution_unavailable", reason)


def repository_name(remote: str) -> str:
    # Account SSH aliases may differ; identity is additionally checked against
    # the original digest-bound registration and the actual Git common directory.
    if "://" in remote:
        return urlsplit(remote).path.lstrip("/").removesuffix(".git")
    if ":" in remote and "@" in remote.split(":", 1)[0]:
        return remote.split(":", 1)[1].removesuffix(".git")
    return remote.removesuffix(".git")


def repository_identity(remote: str) -> str:
    """Canonical host/path while respecting the account's registered SSH aliases."""
    if "://" in remote:
        value = urlsplit(remote)
        host, name, ssh = (
            value.hostname or "",
            value.path.lstrip("/").removesuffix(".git"),
            value.scheme == "ssh",
        )
    elif ":" in remote and "@" in remote.split(":", 1)[0]:
        host, name = remote.split("@", 1)[1].split(":", 1)
        name, ssh = name.removesuffix(".git"), True
    else:
        return str(Path(remote).resolve()).removesuffix(".git")
    if ssh:
        require(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host), "Original SSH identity is invalid"
        )
        try:
            result = subprocess.run(
                ["ssh", "-G", host], capture_output=True, text=True, timeout=5, check=True
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise Rejected(
                "existing_execution_unavailable", "Registered SSH repository cannot be resolved"
            ) from exc
        names = [
            line.split()[1] for line in result.stdout.splitlines() if line.startswith("hostname ")
        ]
        require(len(names) == 1, "Registered SSH repository is ambiguous")
        host = names[0]
    return ("github.com" if host == "ssh.github.com" else host.lower()) + "/" + name


def retained_binding(resource: Json, current: Json) -> Json:
    original = resource.get("existing_binding")
    if original is None:
        return current
    require(
        repository_identity(original["remote_url"]) == repository_identity(current["remote_url"]),
        "Original repository is no longer admitted by this Host",
    )
    return cast(Json, original)


def decide(state: Json, data: Json, context: HostContext) -> Decision:
    raise Rejected(
        "track_retired",
        "Original execution continuation is retired with the old track; "
        "a human may assume control of the WI",
    )


def apply(state: Json, payload: Json) -> None:
    state.pop("historical_execution", None)
    state.pop("historical_progress", None)
    state["holder"] = payload["principal"]
    state["resources"] = [r for w in payload["workspaces"] for r in w["resources"]]
    state["definition"] = {**state["definition"], "resources": state["resources"]}
    state["workspaces"] = {w["resource_id"]: deepcopy(w) for w in payload["workspaces"]}
    state["existing_execution"] = {
        k: deepcopy(v)
        for k, v in payload.items()
        if k not in {"principal", "workspaces", "original_session_ref", "audit"}
    }
    if "business" in payload:
        from .existing_business import apply as apply_business

        apply_business(state, payload["business"])
    if payload["checkpoint"] is not None:
        state["records"].append(
            {"kind": "OriginalCheckpointObserved", "checkpoint": deepcopy(payload["checkpoint"])}
        )
