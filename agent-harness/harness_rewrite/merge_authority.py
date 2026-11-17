"""Standing merge authority: a human's lasting decision that an Agent merges its
own pull request once it has fixed the findings of the third reviewed round.

The human approves `grant_merge_authority {reason}` once, in a conversation of
the Agent's session holding an active WI; that WI only carries the audit record.
The approval must be the human's latest input there (the Host checks it, as for
`grant_access`), and one approval registers one grant across the fact store,
revoked or not (the kernel checks it), so an old approval never restores a
revoked authority.
The grant serves the Agent on every WI until revoked: the current holder session
of a WI (its exact principal at the current generation) may then `confirm_pull_request_merge` without a human input for a head
registered after three reviewed rounds (`review_block: three_rounds`). It covers
nothing else: not the rejected third-round head itself, not authorship blocks,
not blocked checks and never `override_checks`; the merge still waits for every
check of that head to pass.

`revoke_merge_authority {grant_id, reason}` ends the grant. Revoking only
narrows authority: any session of the Agent it serves may do so, at any
generation, whatever state its carrier WI is in, a child's paused or ended
parent included.
"""

from __future__ import annotations

from typing import Any

from .model import Decision, HostContext, Json, Rejected, fields, human_decision
from .workflow import nonempty

INTENTS = frozenset({"grant_merge_authority", "revoke_merge_authority"})
SCOPE = "merge_after_fixed_third_round"


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def decide(
    state: Json, intent: str, data: Json, context: HostContext, operation: str, wi: str,
    holder: bool, at: str,
) -> Decision:
    if intent == "grant_merge_authority":
        fields(data, {"reason"})
        nonempty(data["reason"])
        if state["status"] != "active" or not holder:
            raise Rejected("authority_denied", "Only the holder of an active WI carries the grant")
        source = human_decision(context, intent, data, wi)
        return Decision(
            [event("MergeAuthorityGranted", grant_id=operation, principal=context.principal(),
                   scope=SCOPE, reason=data["reason"], source=source, at=at)],
            {"grant_id": operation, "scope": SCOPE},
        )
    fields(data, {"grant_id", "reason"})
    nonempty(data["reason"])
    grant = state.get("merge_authority", {}).get(data["grant_id"])
    if grant is None or not grant["active"]:
        raise Rejected("domain_conflict", "Merge authority is not active on this WI")
    if grant["principal"]["actor"] != context.actor:
        raise Rejected("authority_denied", "Only the Agent the grant serves revokes it")
    try:
        source = human_decision(context, intent, data, wi)
    except Rejected:
        source = "grant_actor"
    return Decision(
        [event("MergeAuthorityRevoked", grant_id=data["grant_id"], reason=data["reason"],
               source=source, by=context.principal(), at=at)],
        {"grant_id": data["grant_id"], "revoked": True},
    )


def apply(result: Json, kind: str, payload: Json) -> None:
    grants = result.setdefault("merge_authority", {})
    if kind == "MergeAuthorityGranted":
        grants[payload["grant_id"]] = {**payload, "active": True}
    else:
        grants[payload["grant_id"]].update(active=False, revocation={
            key: payload[key] for key in ("reason", "source", "at")})


def standing(state: Json, actor: str | None = None) -> list[Json]:
    """Unrevoked grants of `actor`, whatever the carrier WI's state."""
    return [
        grant for grant in state.get("merge_authority", {}).values()
        if grant["active"] and (actor is None or grant["principal"]["actor"] == actor)
    ]


def public(state: Json) -> list[Json]:
    return [
        {key: grant[key] for key in ("grant_id", "scope", "reason", "active", "at")}
        | {"actor": grant["principal"]["actor"],
           **({"revocation": grant["revocation"]} if "revocation" in grant else {})}
        for grant in state.get("merge_authority", {}).values()
    ]
