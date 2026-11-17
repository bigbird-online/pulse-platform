"""Per-WI disk quota over the WI's own directories and Agent-made copies (AH-086).

The effect service measures each WI's unretired registered workspaces and
every other entry under `<managed root>/<wi>` (copies made without
allocate_workspaces). It also reports the copies Agents made elsewhere that
name this WI, and for an active WI the copies of its holding Agent that name
no WI (agent_copies.py), against a separate per-Agent limit. Agent copies
count toward the level only while `AGENT_COPIES_ENFORCED` is set; otherwise
they are recorded and shown with the level they would cause.

Each Host records its own measurement: a Host measures only its own disk, so
one Host's measurement replaces only that Host's record, and the WI level is
the most severe of the latest records. Only a level change or a material
change of a Host's record is journaled. A measurement without a Host (an older
program) is one shared legacy record, which the next Host-keyed measurement
replaces. Decisions compare recorded values: they never measure. Above the
quota, intents that open new work are refused until the Agent removes garbage
(the next measurement lifts the block) or a human raises this WI's quota.
Missing or failed measurement never blocks work.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any, Iterable

from .model import Decision, HostContext, Json, Rejected, fields, human_decision
from . import workflow

DEFAULT_LIMIT = 1 << 30
# Copies an Agent made that name no WI: room for a few sparse copies or one full clone.
AGENT_LIMIT = 2 << 30
# Switch: False records and shows Agent copies without blocking; True blocks on them.
AGENT_COPIES_ENFORCED = False
WARNING_RATIO = 0.8
MAX_LIMIT = 64 << 30
REPORTED_UNREGISTERED = 32
LEGACY_HOST = ""
INTENTS = frozenset({"raise_disk_quota"})
WORKER_INTENTS = frozenset({"observe_disk_usage"})
# Opening new work; closing, delivery, publication, retirement and reads stay open.
# Not here, so an exceeded WI can still get out: a checkpoint records where it stands,
# offer_collaboration asks for help, and a revise_workspace that only narrows a sparse
# checkout (see `narrows`) frees space. Its admitted effect may then run to the end.
PROGRESS_INTENTS = frozenset({
    "start_work", "adopt_plan", "revise_plan", "claim_task", "record_work",
    "grant_scope", "allocate_workspaces", "replace_workspace", "revise_workspace",
    "reopen_work", "assume_control",
})
PROGRESS_EFFECTS = frozenset({"workspace_allocate"})
ORDER = ("exceeded", "warning", "ok")


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def limit(state: Json) -> int:
    raised = state.get("disk_quota")
    return int(raised["limit_bytes"]) if raised else DEFAULT_LIMIT


def _grade(used: int, bound: int) -> str:
    if used > bound:
        return "exceeded"
    return "warning" if used >= WARNING_RATIO * bound else "ok"


def _worst(grades: Iterable[str]) -> str:
    present = set(grades)
    return next((g for g in ORDER if g in present), "unmeasured")


def host_level(state: Json, record: Json, enforced: bool) -> str:
    """One Host record's level; with `enforced`, Agent copies count."""
    named = sum((record.get("named") or {}).values())
    grades = [_grade(record["bytes"] + (named if enforced else 0), limit(state))]
    copies = record.get("agent_copies")
    if enforced and copies:
        grades.append(_grade(copies["bytes"], AGENT_LIMIT))
    return _worst(grades)


def records(state: Json) -> Json:
    return dict(state.get("disk_usage") or {})


def level(state: Json, enforced: bool | None = None) -> str:
    """The most severe latest Host record; "unmeasured" without any."""
    enforced = AGENT_COPIES_ENFORCED if enforced is None else enforced
    return _worst(host_level(state, r, enforced) for r in records(state).values())


CLEANUP = (
    "Delete the unregistered copies listed in disk_quota.unregistered (and the copies in "
    "disk_quota.named_copies and disk_quota.agent_copies.copies), or the garbage in this WI's "
    "workspaces (a revise_workspace that narrows its sparse paths is admitted for this); "
    "make new copies with allocate_workspaces (sparse). checkpoint and offer_collaboration "
    "stay open, so the WI can record where it stands and ask for help"
)


def public(state: Json) -> Json:
    hosts = records(state)
    # The Host record that sets the level, then the largest: what to clean first.
    worst = min(hosts.items(), key=lambda item: (
        ORDER.index(host_level(state, item[1], True)), -item[1]["bytes"], item[0]), default=None)
    usage = worst[1] if worst else {}
    unregistered = usage.get("unregistered") or {}
    named = usage.get("named") or {}
    copies = usage.get("agent_copies")
    holder = state.get("holder")
    view = {
        "limit_bytes": limit(state),
        "level": level(state),
        # The level once Agent copies block (equal to level when they already do).
        "level_with_agent_copies": level(state, True),
        "agent_copies_enforced": AGENT_COPIES_ENFORCED,
        "bytes": usage.get("bytes"),
        "measured_at": usage.get("measured_at"),
        "host": worst[0] if worst else None,
        "raised_by_human": state.get("disk_quota") is not None,
        "unregistered": unregistered,
        "named_copies": named,
        # The Agent whose unattributed copies this WI carries while it holds the WI.
        "agent": holder.get("actor") if state.get("status") == "active" and holder else None,
        "agent_copies": {**copies, "limit_bytes": AGENT_LIMIT,
                         "level": _grade(copies["bytes"], AGENT_LIMIT)} if copies else None,
        "hosts": {
            host: {"bytes": r["bytes"], "measured_at": r["measured_at"],
                   "named_bytes": sum((r.get("named") or {}).values()),
                   "agent_copies_bytes": (r.get("agent_copies") or {}).get("bytes"),
                   "level": host_level(state, r, AGENT_COPIES_ENFORCED)}
            for host, r in sorted(hosts.items())
        },
    }
    if unregistered or named or copies and copies["copies"]:
        view["agent_action"] = CLEANUP
    return view


def narrows(state: Json, data: Json) -> bool:
    """A workspace revision whose sparse paths all lie inside the current ones.

    A full checkout (`[]`) is narrowed by any explicit list; `[]` itself never narrows.
    """
    if not isinstance(data, dict) or not isinstance(data.get("sparse"), list) or not data["sparse"]:
        return False
    resource_id = data.get("resource_id")
    if not isinstance(resource_id, str):
        return False  # The workspace decision rejects a malformed id.
    resource = (state.get("workspaces") or {}).get(resource_id)
    if not isinstance(resource, dict):
        return False
    current = resource.get("sparse") or []
    if not current:
        return True
    def within(path: object) -> bool:
        return isinstance(path, str) and any(
            path == old or path.startswith(old.rstrip("/") + "/") for old in current
        )

    return all(within(path) for path in data["sparse"])


def blocked(state: Json, intent: str, data: Json) -> None:
    effect = (state.get("effects") or {}).get(data.get("effect_id")) if isinstance(data, dict) else None
    opening = intent in PROGRESS_INTENTS or (
        intent == "begin_effect" and effect is not None and effect["effect_kind"] in PROGRESS_EFFECTS
    )
    if opening and level(state) == "exceeded" and not (intent == "revise_workspace" and narrows(state, data)):
        raise Rejected(
            "disk_quota_exceeded",
            "This WI, or the copies its holding Agent made outside any WI, uses more disk "
            "than its quota. " + CLEANUP + ", then re-measure with "
            "`harness.pyz effect-service measure --wi <wi>` (the effect service also "
            "re-measures periodically), or ask the human to raise_disk_quota for this WI "
            "(the per-Agent limit on copies that name no WI is not raised).",
        )


def _byte_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and type(v) is int and v >= 0 for k, v in value.items()
    )


def decide(state: Json, intent: str, data: Json, context: HostContext, wi: str, at: str) -> Decision:
    if intent == "raise_disk_quota":
        fields(data, {"limit_bytes", "reason"})
        workflow.nonempty(data["reason"])
        value = data["limit_bytes"]
        if type(value) is not int or not limit(state) < value <= MAX_LIMIT:
            raise Rejected("invalid_input", "A raised quota must exceed the current one and stay bounded")
        holder = state.get("holder")
        if context.actor != state["definition"]["owner"] and (holder is None or holder.get("actor") != context.actor):
            raise Rejected("authority_denied", "The WI owner or holder requests a quota raise")
        source = human_decision(context, intent, data, wi)
        return Decision([event("DiskQuotaRaised", limit_bytes=value, reason=data["reason"], source=source)],
                        {"limit_bytes": value})
    fields(data, {"bytes", "resources", "evidence"}, {"unregistered", "named", "agent_copies"})
    copies = data.get("agent_copies")
    if copies is not None:
        if not isinstance(copies, dict):
            raise Rejected("invalid_input", "agent_copies must be an object")
        fields(copies, {"actor", "bytes", "copies"})
        workflow.nonempty(copies["actor"])
    if (
        type(data["bytes"]) is not int
        or data["bytes"] < 0
        or copies is not None and (type(copies["bytes"]) is not int or copies["bytes"] < 0)
        or not all(_byte_map(part) for part in (
            data["resources"], data.get("unregistered", {}), data.get("named", {}),
            *([copies["copies"]] if copies else [])))
    ):
        raise Rejected("invalid_input", "A disk measurement needs non-negative byte counts")
    if not isinstance(data["evidence"], list) or not data["evidence"]:
        raise Rejected("source_invalid", "A disk measurement requires captured evidence")
    refs = [a.reference() for a in context.artifacts]
    for ref in data["evidence"]:
        workflow.artifact_ref(ref)
        if ref not in refs:
            raise Rejected("source_invalid", "Measurement evidence bytes are unavailable")
    # The Host is the authenticated measuring surface, never a caller field.
    host = context.surface
    previous = records(state).get(host)
    record = {"bytes": data["bytes"], "measured_at": at, "named": data.get("named", {}),
              "agent_copies": copies}
    measured = {**state, "disk_usage": replace_record(records(state), host, record)}

    def changed(new: int, old: int) -> bool:
        return abs(new - old) >= max(64 << 20, old // 10)

    before = (previous or {}).get("agent_copies")
    material = (
        previous is None
        or changed(data["bytes"], previous["bytes"])
        or changed(sum(record["named"].values()), sum((previous.get("named") or {}).values()))
        or (before or {}).get("actor") != (copies or {}).get("actor")
        or before is not None and copies is not None and changed(copies["bytes"], before["bytes"])
    )
    # This Host's own transition counts even while another Host's record masks the WI level.
    unchanged = previous is not None and all(
        host_level(state, record, e) == host_level(state, previous, e) for e in (False, True))
    if unchanged and not material:
        return Decision([], {"recorded": False, "level": level(state)})
    extra = {k: data[k] for k in ("named", "agent_copies") if data.get(k) is not None}
    return Decision([event("DiskUsageObserved", host=host, bytes=data["bytes"],
                           resources=data["resources"], unregistered=data.get("unregistered", {}),
                           measured_at=at, evidence=data["evidence"], **extra)],
                    {"recorded": True, "level": level(measured)})


def replace_record(hosts: Json, host: str, record: Json) -> Json:
    """A Host's measurement replaces its own record and the legacy shared one."""
    kept = {h: r for h, r in hosts.items() if h not in {host, LEGACY_HOST}}
    return {**kept, host: record}


def apply_event(state: Json, kind: str, payload: Json) -> bool:
    if kind == "DiskUsageObserved":
        record = {"bytes": payload["bytes"], "measured_at": payload["measured_at"],
                  "resources": payload["resources"],
                  "unregistered": payload.get("unregistered", {}),
                  "named": payload.get("named", {}),
                  "agent_copies": payload.get("agent_copies")}
        host = payload.get("host")
        hosts = records(state)
        # An older program's measurement updates only the shared legacy record.
        state["disk_usage"] = ({**hosts, LEGACY_HOST: record} if host is None
                               else replace_record(hosts, host, record))
        return True
    if kind == "DiskQuotaRaised":
        state["disk_quota"] = {"limit_bytes": payload["limit_bytes"], "reason": payload["reason"],
                               "source": payload["source"]}
        return True
    return False


def allocated_bytes(
    path: Path,
    *,
    max_entries: int = 2_000_000,
    exclude: frozenset[str] = frozenset(),
    inodes: set[tuple[int, int]] | None = None,
) -> int | None:
    """Disk blocks under one directory; links are not followed, contents never read.

    A `.git` file is a worktree pointer: shared repository objects are platform
    storage, not this WI's result. A `.git` directory is an independent clone
    and counts. Paths in `exclude` are measured by their owner. `inodes` carries
    hard links already counted elsewhere in the same measurement. Unreadable or
    oversized trees return None (unmeasured), never zero.
    """
    try:
        root = os.lstat(path)
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    if not stat.S_ISDIR(root.st_mode):
        return None
    counted = set() if inodes is None else inodes
    total, seen, stack = 0, 0, [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > max_entries:
                        return None
                    if entry.path in exclude:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    directory = stat.S_ISDIR(info.st_mode)
                    if entry.name == ".git" and not directory:
                        continue
                    if not directory and info.st_nlink > 1:
                        if (info.st_dev, info.st_ino) in counted:
                            continue
                        counted.add((info.st_dev, info.st_ino))
                    total += info.st_blocks * 512
                    if directory:
                        stack.append(entry.path)
        except OSError:
            return None
    return total


def measure(wi: str, roots: Iterable[str], registered: dict[str, str]) -> Json | None:
    """Bytes of this WI's registered workspaces and of everything else in its WI directories.

    `registered` maps unretired resource ids to their paths; `roots` are the
    managed roots whose `<root>/<wi>` directory belongs to this WI. Each byte is
    counted once: registered paths by their resource, the remaining entries of
    a WI directory as unregistered (the largest `REPORTED_UNREGISTERED` are
    named). Any unreadable tree returns None, which never blocks work.
    """
    if not wi or wi in {".", ".."} or "/" in wi or "\0" in wi:
        return None
    inodes: set[tuple[int, int]] = set()
    paths = {rid: os.path.normpath(p) for rid, p in registered.items()}
    owned = set(paths.values())
    resources: dict[str, int] = {}
    measured: set[str] = set()
    for rid, path in sorted(paths.items()):
        if path in measured:
            resources[rid] = 0
            continue
        measured.add(path)
        nested = frozenset(o for o in owned if o.startswith(path + "/"))
        size = allocated_bytes(Path(path), exclude=nested, inodes=inodes)
        if size is None:
            return None
        resources[rid] = size
    unregistered: dict[str, int] = {}
    for root in sorted({os.path.normpath(r) for r in roots}):
        directory = os.path.join(root, wi)
        try:
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                continue
            with os.scandir(directory) as scanned:
                entries = sorted(scanned, key=lambda e: e.name)
            for entry in entries:
                if entry.path in owned:
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    nested = frozenset(o for o in owned if o.startswith(entry.path + "/"))
                    size = allocated_bytes(Path(entry.path), exclude=nested, inodes=inodes)
                elif info.st_nlink > 1 and (info.st_dev, info.st_ino) in inodes:
                    size = 0
                else:
                    inodes.add((info.st_dev, info.st_ino))
                    size = info.st_blocks * 512
                if size is None:
                    return None
                unregistered[entry.path] = size
        except FileNotFoundError:
            continue
        except OSError:
            return None
    total = sum(resources.values()) + sum(unregistered.values())
    largest = sorted(unregistered.items(), key=lambda item: (-item[1], item[0]))[:REPORTED_UNREGISTERED]
    return {"bytes": total, "resources": resources, "unregistered": dict(sorted(largest))}
