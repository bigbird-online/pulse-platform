"""An Agent moves its Relay conversation from the WI it holds to another of its own WIs.

The holder session asks with `request_switch {target, summary}` on the WI it
holds. The decision writes no event: nothing moves until the Host session
controller (Relay, through `relay-channel` with the request's private notice)
leaves this session and enters the target's original or a new session, exactly
as for `/wi switch`. The left WI stays held by this session, so a later switch
back resumes it here.

Readiness is the same as `request_rollover`: the latest record is this
session's checkpoint and no effect is in doubt. The target is read in the same
transaction (`context.switch_target`): a root WI of the same Agent that it can
continue (held by another of its sessions) or start (its own draft). A target
that is this WI or one of its children needs no switch and gets that answer
instead of a request.

At enter the Host attests `session_switch {source, source_wi, request_id,
target}` after reading the committed request back; `continue_here` accepts it
in place of a human decision, still only for the same Agent.
"""

from __future__ import annotations

from .model import Decision, HostContext, Json, Rejected, encoded, fields, identity
from .self_rollover import blocker
from .workflow import nonempty

INTENT = "request_switch"
PROOF_FIELDS = frozenset({"source", "source_wi", "request_id", "target"})


def target_id(data: object) -> str:
    """The target a request_switch input names, read before the decision."""
    value = data.get("target") if isinstance(data, dict) else None
    if not isinstance(value, str):
        raise Rejected("invalid_input", "request_switch requires input.target, the WI to switch to")
    return identity(value)


def no_switch(state: Json, wi: str, target: str, context: HostContext) -> str | None:
    if target == wi:
        return f"{target} 就是本会话当前持有的 WI，无需切换"
    if any(child["id"] == target for child in state.get("children") or []):
        return f"{target} 是当前 WI {wi} 的子 WI，执行权已在本会话，无需切换"
    found = (context.switch_target or {}).get("state")
    if found is not None and found.get("holder") == context.principal():
        return f"{target} 的执行权已在本会话，无需切换"
    return None


def refusal(found: Json | None, target: str, context: HostContext) -> Rejected | None:
    """Why the target cannot take this conversation, or None when it can."""
    if found is None:
        return Rejected("domain_conflict", f"{target} does not exist")
    if found.get("parent") is not None:
        return Rejected(
            "domain_conflict",
            f"{target} is a child of {found['parent']}; switch to its parent, whose holder holds it",
        )
    status, holder = found["status"], found.get("holder")
    if status == "draft":
        if found["definition"]["owner"] != context.actor:
            return Rejected("authority_denied", f"{target} is another Agent's draft")
        return None
    if status != "active":
        return Rejected("domain_conflict", f"{target} is {status}; only active work or your draft can be entered")
    if holder is None:
        return Rejected(
            "domain_conflict",
            f"No session holds {target}; the human moves it with /wi continue {target}",
        )
    if holder["actor"] != context.actor:
        return Rejected("authority_denied", f"Another Agent holds {target}")
    return None


def decide(state: Json, data: Json, context: HostContext, wi: str) -> Decision:
    """The caller is already the current holder of an active WI."""
    fields(data, {"target", "summary"})
    target = target_id(data)
    nonempty(data["summary"])
    if len(encoded(data)) > 32768:
        raise Rejected("invalid_input", "Use a bounded switch summary; the checkpoint holds the frontier")
    reason = no_switch(state, wi, target, context)
    if reason:
        return Decision([], {"no_switch": reason})
    reason = blocker(state, context, INTENT)
    if reason:
        raise Rejected("domain_conflict", reason)
    found = (context.switch_target or {}) if (context.switch_target or {}).get("id") == target else {}
    refused = refusal(found.get("state"), target, context)
    if refused is not None:
        raise refused
    return Decision(
        [],
        {
            "switch_request": {
                "source": context.principal(),
                "generation": state["generation"],
                "target": target,
                "summary": data["summary"],
                "next": "End this turn now and record nothing more: after it the Host moves this "
                f"conversation to {target} and hands it to you there; {wi} stays held here",
            }
        },
    )


def accepted_proof(context: HostContext, wi: str) -> Json | None:
    """The Host-attested Agent switch into `wi`, or None when there is none."""
    proof = context.session_switch
    if proof is None:
        return None
    if (
        not isinstance(proof, dict)
        or set(proof) != PROOF_FIELDS
        or proof["target"] != wi
        or not isinstance(proof["source"], dict)
        or proof["source"].get("actor") != context.actor
    ):
        raise Rejected("authority_denied", "Agent switch requires the admitted Host session controller")
    return proof
