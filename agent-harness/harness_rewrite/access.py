"""Host file access for native sessions, recorded by the kernel, enforced by Relay.

A session grant: an Agent asks, the human approves in the same conversation,
and the kernel records the grant on the bound WorkItem. It opens the paths to
that one session and ends with the WorkItem or an approved revocation.

A standing grant (`standing: true`) is read-only and serves the Agent itself:
Relay opens its paths to every session of that Agent, conversation and
scheduled run alike, until it is revoked. The WorkItem it was requested on only
carries the audit record. A request that matches the family rule is admitted
without a further human approval; any other standing request needs one.

Family rule (the operator's decision of 2026-11-05, FAMILY_AUTHORITY): a base
Agent `<base>` and each of its avatars `<base>-<member>`, whose workspace is
`.../tenants/<member>/<base>-<member>-space`, may read each other's workspace.
The target must be exactly the counterpart's installed workspace root; the
workspaces come from the operator-installed host policy, never from the caller.
A family grant that the current policy no longer supports is not applied.

Grants confer file access, never lifecycle authority.
"""

from __future__ import annotations

import os
from pathlib import PurePosixPath
import re
from typing import Any

from . import guide
from .model import Decision, HostContext, Json, Rejected, fields, human_decision
from .workflow import nonempty

INTENTS = frozenset({"grant_access", "revoke_access"})
MODES = frozenset({"read", "write"})
FAMILY_AUTHORITY = "operator_decision.2026-11-05.family_read"
MEMBER = re.compile(r"[A-Za-z0-9_]+\Z")


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def agent_workspaces(policy: Json) -> Json:
    """Each producer actor's installed workspace roots."""
    rows: dict[str, set[str]] = {}
    for profile in policy.get("profiles", []):
        if profile.get("role") == "producer":
            rows.setdefault(profile["actor"], set()).update(profile.get("workspace_roots", []))
    return {actor: sorted(roots) for actor, roots in rows.items() if roots}


def _avatar_roots(avatar: str, base: str, workspaces: Json) -> list[str]:
    """The avatar's roots in the family layout, empty when it is not base's avatar."""
    member = avatar[len(base) + 1 :]
    if base not in workspaces or not avatar.startswith(base + "-") or not MEMBER.fullmatch(member):
        return []
    return [
        root
        for root in workspaces.get(avatar, [])
        if (path := PurePosixPath(root)).name == avatar + "-space"
        and path.parent.name == member
        and path.parent.parent.name == "tenants"
    ]


def family(actor: str, workspaces: Json | None) -> set[str]:
    """Workspace roots `actor` may read under the family rule."""
    if not workspaces or actor not in workspaces:
        return set()
    targets: set[str] = set()
    for other in workspaces:
        if other == actor:
            continue
        if _avatar_roots(actor, other, workspaces):
            targets.update(workspaces[other])
        targets.update(_avatar_roots(other, actor, workspaces))
    return targets - set(workspaces[actor])


def _paths(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise Rejected("invalid_input", "Expected one to 32 host paths")
    paths = []
    for path in value:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or len(path) > 1024
            or "\0" in path
            or os.path.normpath(path) != path
        ):
            raise Rejected("invalid_input", "Access paths must be normalized absolute host paths")
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise Rejected("invalid_input", "Access paths must be unique")
    return paths


def _grant(data: Json, context: HostContext, operation: str, wi: str) -> Decision:
    fields(data, *guide.shape("grant_access"))
    paths = _paths(data["paths"])
    if data["mode"] not in MODES:
        raise Rejected("invalid_input", "Access mode must be read or write")
    nonempty(data["reason"])
    standing = data.get("standing", False)
    if type(standing) is not bool:
        raise Rejected("invalid_input", "standing must be true or false")
    if standing and data["mode"] != "read":
        raise Rejected("invalid_input", "A standing grant is read-only; write access is a session grant")
    by_rule = standing and set(paths) <= family(context.actor, context.agent_workspaces)
    payload: Json = {
        "grant_id": operation,
        "principal": context.principal(),
        "paths": paths,
        "mode": data["mode"],
        "reason": data["reason"],
        "source": FAMILY_AUTHORITY if by_rule else human_decision(context, "grant_access", data, wi),
    }
    if standing:
        payload.update(scope="agent", admission="family_rule" if by_rule else "human")
    return Decision([event("AccessGranted", **payload)], {"grant_id": operation, **(
        {"scope": "agent", "admission": payload["admission"]} if standing else {})})


def decide(
    state: Json, intent: str, data: Json, context: HostContext, operation: str, wi: str, holder: bool
) -> Decision:
    if intent == "grant_access":
        if state["status"] != "active" or not holder:
            raise Rejected("authority_denied", "Only the current holder may change session access")
        return _grant(data, context, operation, wi)
    fields(data, {"grant_id", "reason"})
    nonempty(data["reason"])
    grant = state.get("access", {}).get(data["grant_id"])
    if grant is None or not grant["active"]:
        raise Rejected("domain_conflict", "Access grant is not active")
    if grant.get("scope") == "agent" and grant["principal"]["actor"] == context.actor:
        # Giving up a standing grant only narrows access: any session of the
        # Agent it serves may do so, whatever state its carrier WI is in.
        try:
            source = human_decision(context, intent, data, wi)
        except Rejected:
            source = "grant_actor"
    elif state["status"] != "active" or not holder:
        raise Rejected("authority_denied", "Only the current holder may change session access")
    else:
        source = human_decision(context, intent, data, wi)
    return Decision(
        [
            event(
                "AccessRevoked",
                grant_id=data["grant_id"],
                reason=data["reason"],
                source=source,
                by=context.principal(),
            )
        ]
    )


def apply(result: Json, kind: str, payload: Json) -> None:
    grants = result.setdefault("access", {})
    if kind == "AccessGranted":
        grants[payload["grant_id"]] = {**payload, "active": True}
    else:
        grants[payload["grant_id"]]["active"] = False


def active(state: Json, principal: Json | None = None) -> list[Json]:
    """Session grants Relay may enforce now; a WorkItem that is not active grants nothing."""
    if state["status"] != "active":
        return []
    return [
        grant
        for grant in state.get("access", {}).values()
        if grant["active"]
        and grant.get("scope") != "agent"
        and (principal is None or grant["principal"] == principal)
    ]


def standing(state: Json, actor: str | None = None, workspaces: Json | None = None) -> list[Json]:
    """Unrevoked standing grants of `actor`, whatever the carrier WI's state.

    With `workspaces` (the current installed layout), a family-rule grant the
    layout no longer supports is left out.
    """
    return [
        grant
        for grant in state.get("access", {}).values()
        if grant["active"]
        and grant.get("scope") == "agent"
        and (actor is None or grant["principal"]["actor"] == actor)
        and (
            workspaces is None
            or grant["admission"] != "family_rule"
            or set(grant["paths"]) <= family(grant["principal"]["actor"], workspaces)
        )
    ]


def public(state: Json) -> list[Json]:
    keys = ("grant_id", "paths", "mode", "reason")
    return [
        {key: grant[key] for key in keys} | {"session": grant["principal"]["session"]}
        for grant in active(state)
    ] + [
        {key: grant[key] for key in keys}
        | {"scope": "agent", "actor": grant["principal"]["actor"], "admission": grant["admission"]}
        for grant in standing(state)
    ]
