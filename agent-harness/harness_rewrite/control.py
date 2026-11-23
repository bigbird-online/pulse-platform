"""Explicit human control preserves original facts and never derives authority from views."""

from __future__ import annotations
from typing import Any
from . import self_switch, workflow
from .model import (
    Decision,
    HostContext,
    Json,
    Rejected,
    digest,
    fields,
    human_decision,
    identity,
)

INTENTS = frozenset(
    {"terminate_work", "assume_control", "continue_here", "reopen_work", "release_scope", "stop"}
)


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def decide(
    state: Json, intent: str, data: Json, context: HostContext, operation: str, wi: str
) -> Decision:
    if intent == "release_scope":
        fields(data, {"grant_id", "reason"})
        workflow.nonempty(data["reason"])
        grant = state["grants"].get(data["grant_id"])
        if grant is None or grant["principal"] != context.principal():
            raise Rejected("authority_denied", "Only the original scope recipient may return it")
        if not grant["active"]:
            return Decision([], {"grant_id": data["grant_id"], "already_released": True})
        return Decision([event("ScopeRevoked", grant_id=data["grant_id"], reason=data["reason"])])
    required = (
        {"reason", "kind"}
        if intent == "terminate_work"
        else {"reason", "nodes"}
        if intent == "reopen_work"
        else {"reason"}
    )
    fields(data, required, {"successor"} if intent == "terminate_work" else None)
    workflow.nonempty(data["reason"])
    # The same Agent's own committed request_switch, attested by the Host, stands in
    # for the human's /wi continue; authority still never leaves the Agent.
    switched = self_switch.accepted_proof(context, wi) if intent == "continue_here" else None
    source = digest(switched) if switched is not None else human_decision(context, intent, data, wi)
    if intent in {"stop", "terminate_work"}:
        if state["status"] in {"completed", "terminated"}:
            raise Rejected("domain_conflict", "Terminal work requires an explicit reopen decision")
        if intent == "stop":
            return Decision([event("WorkStopped", reason=data["reason"], source=source)])
        if data["kind"] not in {"cancelled", "failed", "superseded"}:
            raise Rejected(
                "invalid_input",
                "Unknown non-delivery termination; migration requires proven destination adoption",
            )
        successor = data.get("successor")
        if data["kind"] == "superseded":
            if not isinstance(successor, str):
                raise Rejected("invalid_input", "Superseded work requires its successor identity")
            identity(successor)
            if successor == wi:
                raise Rejected("invalid_input", "A successor cannot replace itself")
        elif successor is not None:
            raise Rejected("invalid_input", "Successor is only valid for a superseded outcome")
        return Decision(
            [
                event(
                    "WorkTerminated",
                    termination_id=operation,
                    termination=data["kind"],
                    business_epoch=(context.admission_policy or {}).get("current_epoch"),
                    reason=data["reason"],
                    successor=successor,
                    source=source,
                    decided_by=context.actor,
                )
            ]
        )
    if intent == "continue_here":
        # The same Agent continues its own work from another entry (AH-085).
        # A different Agent hands over through rollover or assume_control.
        holder = state["holder"]
        if state["status"] != "active" or holder is None:
            raise Rejected("domain_conflict", "Only active work held by a session can continue here")
        if holder == context.principal():
            return Decision([], {"already_holder": True})
        if holder["actor"] != context.actor:
            raise Rejected(
                "authority_denied",
                "Another Agent holds this work; use rollover or /wi reinit",
            )
        return Decision(
            [
                event(
                    "SessionContinued",
                    principal=context.principal(),
                    previous=holder,
                    generation=state["generation"] + 1,
                    reason=data["reason"],
                    source=source,
                    decided_by=context.actor,
                )
            ]
        )
    if intent == "assume_control":
        if state["status"] in {"completed", "terminated", "stopped"}:
            raise Rejected("domain_conflict", "Reopen must explicitly authorize renewed execution")
        # The successor is the authenticated human-authorized caller, never a
        # private identity supplied in the command or inferred from a cache.
        return Decision(
            [
                event(
                    "HumanControlAssumed",
                    principal=context.principal(),
                    generation=state["generation"] + 1,
                    reason=data["reason"],
                    source=source,
                    decided_by=context.actor,
                )
            ]
        )
    from .workspaces import SETTLED_STATUSES

    # A failed retirement is settled too: it acted for the last time, kept the
    # workspace, and the holder may request the retirement again.
    if any(
        e["effect_kind"] == "workspace_retire"
        and e["status"] not in SETTLED_STATUSES
        for e in state["effects"].values()
    ):
        raise Rejected(
            "domain_conflict",
            "Original retirement must settle before renewed execution",
        )
    if state["status"] not in {"completed", "terminated", "stopped"}:
        raise Rejected("domain_conflict", "Only ended or stopped work can be reopened")
    nodes = data["nodes"]
    known = {
        n["node_id"]
        for n in (state["graph"] or {}).get("nodes", [])
        if n["kind"] in workflow.TASK_KINDS
    }
    if (
        not isinstance(nodes, list)
        or any(not isinstance(n, str) for n in nodes)
        or not set(nodes) <= known
        or (state["graph"] is not None and not nodes)
    ):
        raise Rejected(
            "invalid_input",
            "Reopen must name affected execution nodes, or an empty list for work without a graph",
        )
    affected = set(nodes)
    if state["graph"]:
        for node in workflow.topological(state["graph"]):
            if any(e["to"] == node and e["from"] in affected for e in state["graph"]["edges"]):
                affected.add(node)
    return Decision(
        [
            event(
                "WorkReopened",
                principal=context.principal(),
                generation=state["generation"] + 1,
                affected=sorted(affected),
                reason=data["reason"],
                source=source,
                decided_by=context.actor,
            )
        ]
    )


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "SessionContinued":
        # Work in flight under the previous session cannot settle later; its
        # delegated scopes stay with the WI and its own scopes follow it.
        workflow.invalidate_attempts(state)
        for grant in state["grants"].values():
            if grant["active"] and grant["principal"] == data["previous"]:
                grant["principal"] = data["principal"]
        state.update(holder=data["principal"], generation=data["generation"])
        state.setdefault("successions", {})[str(data["generation"])] = data["principal"]["actor"]
        state.setdefault("control_history", []).append({"kind": kind, **data})
        return True
    if kind not in {"WorkTerminated", "HumanControlAssumed", "WorkReopened"}:
        return False
    workflow.invalidate_attempts(state)
    for grant in state["grants"].values():
        grant["active"] = False
    state["transfer"] = None
    if kind == "WorkTerminated":
        state.update(
            status="terminated",
            holder=None,
            terminal={**data, "resources_retired": False},
        )
    else:
        state["definition"] = {
            **state["definition"],
            "owner": data["principal"]["actor"],
        }
        state["generation"] = data["generation"]
        active = state["profile"] == "lightweight" or state["graph"] is not None
        state["status"] = "active" if active else "draft"
        state["holder"] = data["principal"] if active else None
        if kind == "WorkReopened":
            state["terminal"] = None
            state["active_candidate"] = None
            state["execution_round"] += 1
            for node in data["affected"]:
                state["node_results"].pop(node, None)
                state.get("control_results", {}).pop(node, None)
    state.setdefault("control_history", []).append({"kind": kind, **data})
    return True


def public_history(state: Json) -> list[Json]:
    return [
        {
            key: value
            for key, value in item.items()
            if key not in {"principal", "previous", "source"}
        }
        for item in state.get("control_history", [])
    ]
