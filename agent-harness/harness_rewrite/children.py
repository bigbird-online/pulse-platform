"""Child WorkItems: work the parent's holder session spawns and accepts itself.

`create_child` runs on no WorkItem; its input is `create_lightweight`'s plus
`parent`. The parent must be active, held by the calling session at its current
generation, and not itself a child (one level only). The child copies the
parent's owner, main-body label and effective business affiliation, and starts
active under that same session: no execution authority moves. Children are
lightweight.

The parent's holder session reviews the child (`review_child` on the child,
{decision: approved|returned, reason}). `returned` records the reason and the
child stays active. `approved` completes the child in the same decision, with
lightweight completion's requirements (every work record has artifacts, all
criteria, no pending collaboration or delivery) and no human decision; the
child does not use `complete_lightweight`. The human confirms once, when the
parent ends; a parent cannot complete or terminate while a child is open.

Execution authority over a child follows the parent: every write on a child is
judged against the parent's current holder and generation, which the kernel
reads in the same transaction (`follow`). A takeover, rollover or continuation
of the parent therefore moves its children too; control intents on a child
itself are refused.

The parent's `children` are not stored in its own journal: the kernel reads
each child's journal (found through its `ChildCreated` claim) inside the same
transaction and passes [{id, status, business_progress}] in as `children`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from . import affiliation, guide
from .model import Decision, HostContext, Json, Rejected, fields, identity
from .workflow import nonempty

INTENTS = frozenset({"create_child", "review_child"})
# Intents that end a WorkItem; open children block them on the parent.
ENDING_INTENTS = frozenset({"complete", "complete_lightweight", "terminate_work"})
ENDED = frozenset({"completed", "terminated"})
# A child's holder is its parent's; control moves only on the parent.
CONTROL_INTENTS = frozenset(
    {
        "rollover",
        "request_rollover",
        "request_switch",
        "prepare_rollover",
        "accept_transfer",
        "assume_control",
        "continue_here",
    }
)
DECISIONS = frozenset({"approved", "returned"})


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def parent_id(data: Any) -> str:
    """The parent a create_child input names."""
    value = data.get("parent") if isinstance(data, dict) else None
    if not isinstance(value, str):
        raise Rejected("invalid_input", "create_child requires the parent WorkItem identity")
    return identity(value)


def open_children(state: Json) -> list[Json]:
    return [c for c in state.get("children") or [] if c["status"] not in ENDED]


def block_control(state: Json, intent: str) -> None:
    if intent in CONTROL_INTENTS and state.get("parent") is not None:
        raise Rejected(
            "domain_conflict",
            "A child follows its parent's holder; hand over or take over the parent instead",
        )


def follow(state: Json, parent: Json | None) -> Json:
    """A child's execution authority is its parent's current holder and generation.

    The child's own recorded holder is history only. An ended or stopped child
    keeps its own (empty) holder; while the parent is not active (stopped,
    transferring, ended or unreadable) nobody holds or executes the child:
    `parent_active` is false and every execution write is refused, delegated
    scopes and the holder Agent's other sessions included.
    """
    if parent is None:
        grants = {key: {**grant, "active": False} for key, grant in state["grants"].items()}
        return {**state, "holder": None, "grants": grants, "parent_active": False}
    active = parent["status"] == "active"
    holder = parent["holder"] if active else None
    # A delegation on the child lives only within the parent generation that
    # granted it, as the parent's own delegations end with its control.
    grants = {
        key: {**grant, "active": grant["active"] and active
              and grant["generation"] == parent["generation"]}
        for key, grant in state["grants"].items()
    }
    return {
        **state,
        "holder": holder if state["status"] == "active" else state["holder"],
        "generation": parent["generation"],
        # The generation is the parent's, so is the record of whose own handover
        # (rollover, continue_here) made each one: an offer on the child stays
        # answerable exactly as long as one on the parent would.
        "successions": parent.get("successions", {}),
        "grants": grants,
        "parent_active": active,
    }


def paused(state: Json) -> bool:
    """A child whose parent is not active (or unread): nobody executes it."""
    return state.get("parent") is not None and state.get("parent_active") is not True


def block_paused(state: Json, intent: str, open_intents: frozenset[str]) -> None:
    if paused(state) and intent not in open_intents:
        raise Rejected(
            "authority_denied",
            "The parent work item is not active; its child accepts no execution until "
            "the parent is active again",
        )


def followed(state: Json, replay: Any) -> Json:
    """`follow` inside a collection read; an unreadable parent holds nothing."""
    if state.get("parent") is None:
        return state
    try:
        parent = replay(state["parent"])
    except Rejected:
        parent = None
    return follow(state, parent)


def block_ending(state: Json, intent: str) -> None:
    if intent not in ENDING_INTENTS:
        return
    remaining = open_children(state)
    if remaining:
        raise Rejected(
            "domain_conflict",
            "Open child work items must end first: "
            + ", ".join(f"{c['id']} ({c['business_progress']})" for c in remaining),
        )


def label(parent: Json) -> bool:
    label = parent.get("main_only")
    if label is None:
        record = (parent.get("production") or {}).get("record") or {}
        original = (parent.get("legacy_import") or {}).get("original_metadata") or {}
        label = record.get("main_only", original.get("main_only"))
    if label is None:
        label = parent["profile"] == "standard"
    return label is True


def admit_parent(parent: Json | None, context: HostContext) -> Json:
    """The parent a child may be created under, or the rejection naming why not."""
    if parent is None:
        raise Rejected("domain_conflict", "Parent work item does not exist")
    if parent.get("parent") is not None:
        raise Rejected("domain_conflict", "A child work item cannot have children (one level only)")
    if parent["status"] != "active":
        raise Rejected("domain_conflict", "Children are created only under active work")
    if parent["holder"] != context.principal() or parent["generation"] != context.generation:
        raise Rejected("authority_denied", "Only the session holding the parent creates its children")
    return parent


def creation_events(
    parent: Json, parent_wi: str, context: HostContext, wi: str, at: str
) -> list[Json]:
    """Events after the child's WorkItemCreated: link, inherited affiliation, start."""
    events = [event("ChildCreated", parent=parent_wi, holder=context.principal())]
    admission = affiliation.observation(parent, replace(context, observed_work_item=parent_wi))
    reference = (admission or {}).get("reference")
    if admission is not None and admission["state"] == "admitted" and reference:
        events.append(event("AffiliationRecorded", record={
            "schema_version": "work_item_admission/v1",
            "work_item_id": wi,
            "admission_ref_type": reference["type"],
            "admission_ref": reference["ref"],
            "operating_area_work_class": admission.get("work_class"),
            "decided_by": "parent:" + parent_wi,
            "reason": "A child work item keeps its parent's business affiliation.",
            "decided_at": at,
            "inherited_from": parent_wi,
        }, source={"kind": "parent_work_item", "work_item_id": parent_wi}))
    events.append(event("WorkStarted", principal=context.principal()))
    return events


def review(
    state: Json, data: Json, holder: bool, context: HostContext, operation: str, at: str
) -> Decision:
    """Record the parent holder's review; an approval completes the child in the same
    decision, under lightweight completion's own requirements."""
    from . import collaboration, delivery

    fields(data, *guide.shape("review_child"))
    if state.get("parent") is None:
        raise Rejected("domain_conflict", "Only a child work item is reviewed by its parent's holder")
    if state["status"] != "active" or not holder:
        raise Rejected("authority_denied", "The session currently holding the parent reviews its child")
    if data["decision"] not in DECISIONS:
        raise Rejected("invalid_input", "Review decision must be approved or returned")
    nonempty(data["reason"])
    records = [r for r in state["records"] if r["kind"] == "WorkRecorded"]
    if not records:
        raise Rejected("domain_conflict", "There is no recorded work to review yet")
    item = {
        "review_id": operation,
        "decision": data["decision"],
        "reason": data["reason"],
        "record_ids": [r["record_id"] for r in records],
        "reviewer": context.actor,
        "at": at,
    }
    events = [event("ChildReviewed", review=item)]
    if data["decision"] == "approved":
        bare = sorted(r["record_id"] for r in records if not r["record"]["artifacts"])
        if state["profile"] != "lightweight":
            raise Rejected("domain_conflict", "A standard child completes through its own review")
        if bare:
            raise Rejected("domain_conflict", "Records without artifacts: " + ", ".join(bare))
        if collaboration.pending(state):
            stuck = sorted(
                key for key, item in state.get("collaborations", {}).items()
                if item["required"] and not item["withdrawn"]
                and collaboration.child_transfer(state, item["kind"]))
            raise Rejected("domain_conflict", "Required collaboration has not been settled" + (
                "; a child's full responsibility transfer can only be withdrawn: "
                "withdraw_collaboration " + ", ".join(stuck) if stuck else ""))
        if delivery.pending(state, None):
            raise Rejected("domain_conflict", "Required delivery has not been observed")
        events.append(event(
            "WorkCompleted",
            record_ids=item["record_ids"],
            acceptance_refs=[f"AC-{i + 1}" for i in range(len(state["definition"]["acceptance"]))],
            source_digest=None,
            profile="lightweight",
            review_id=operation,
        ))
    return Decision(events, {"review_id": operation, "completed": data["decision"] == "approved"})


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "ChildCreated":
        state["parent"] = data["parent"]
        return True
    if kind == "ChildReviewed":
        state.setdefault("child_reviews", []).append(data["review"])
        return True
    return False
