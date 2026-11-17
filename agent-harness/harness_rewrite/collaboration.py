"""Original collaboration contracts, recipient decisions and bounded responsibility."""

from __future__ import annotations

from typing import Any

from .model import Decision, HostContext, Json, Rejected, fields, identity, deadline
from . import guide, workflow

INTENTS = frozenset(
    {
        "offer_collaboration",
        "respond_collaboration",
        "withdraw_collaboration",
        "reassign_collaboration",
    }
)
TASK_RIGHTS = [
    "claim_task",
    "record_task_result",
    "abandon_task",
    "checkpoint",
    "record_work",
]


def event(event_kind: str, **payload: Any) -> Json:
    return {"kind": event_kind, "payload": payload}


def holder(state: Json, context: HostContext) -> None:
    if state["status"] == "draft" and context.actor == state["definition"]["owner"]:
        return
    if state["status"] == "active" and context.principal() == state["holder"]:
        return
    raise Rejected("authority_denied", "Current responsible owner/holder is required")


def requesting(state: Json, item: Json, principal: Json) -> bool:
    """The session that offered the contract, or the same Agent's session that
    now holds the WI it was offered from, stands on the requesting side and never
    answers that contract, even when the recipient is its own Agent."""
    offered = item["offered_by"]
    return bool(
        offered == principal
        or offered["actor"] == principal["actor"]
        and state.get("holder") == principal
    )


def answers(item: Json, recipient: Json, context: HostContext) -> bool:
    """An exact-session recipient answers only from that session; an Agent
    recipient answers from any of its sessions on any surface (AH-088)."""
    principal = recipient["principal"]
    if principal["actor"] != context.actor:
        return False
    if item.get("historical") is not None:
        return bool(principal["surface"] == context.surface)
    return "session" not in principal or principal == context.principal()


MAIN_ONLY_REQUIRED = (
    "Declare main_only. true: the recipient's main session handles it; choose it when the work "
    "uses the recipient's ongoing work (unmerged changes, authorizations it holds, a WI it is "
    "advancing) or needs write access. For an Agent recipient, target_work_item_id names the WI "
    "it handles the contract on; omitted, the one active WI that Agent holds is used. "
    "false: an unattended fork handles it; choose it only when read-only work on what main "
    "already has completes the contract. "
    "请写 main_only：要用到对方正在进行的工作（未合并改动、手上的授权、在推进的 WI）或需要写权限，"
    "写 true 交主身；接收方只写 Agent 时，target_work_item_id 写对方处理它的 WI，不写则取对方"
    "唯一持有的进行中 WI。只读、只依赖 main 上已有内容就能办完，才写 false 交分身"
)
# Candidates named in a refusal, enough to choose from without a flood.
SHOWN = 10


def untargeted(actor: str, held: list[Json]) -> str:
    """The refusal for a main-only Agent recipient without a target WI when its
    Agent holds no active WI, or more than one."""
    if not held:
        return (
            f"A main_only contract to {actor} needs the WI it is handled on, and {actor} "
            "holds no active WI. Name that WI in target_work_item_id, or address one exact "
            f"session of {actor} (actor, session, surface). "
            f"主身合同发给 {actor}，需要对方处理它的 WI，而对方现在没有持有中的进行中 WI。"
            "请在该接收方写 target_work_item_id，或指定对方的具体会话（actor、session、surface）"
        )
    listed = "; ".join(
        f"{item['work_item_id']} ({str(item['title'])[:60]})" for item in held[:SHOWN]
    ) + ("; ..." if len(held) > SHOWN else "")
    return (
        f"A main_only contract to {actor} needs the WI it is handled on, and {actor} holds "
        f"{len(held)} active WIs. Name one in target_work_item_id: {listed}. "
        f"主身合同发给 {actor}，对方持有 {len(held)} 个进行中 WI，请在该接收方写 "
        f"target_work_item_id 选其一：{listed}"
    )


def untargeted_agents(intent: str, data: Any) -> frozenset[str]:
    """Agent recipients of a main-only offer or reassignment that name no target
    WI; the kernel reads which active WIs each holds before deciding."""
    if intent not in {"offer_collaboration", "reassign_collaboration"} or not isinstance(data, dict):
        return frozenset()
    if data.get("kind") == "responsibility" or data.get("main_only") is False:
        return frozenset()
    if intent == "offer_collaboration" and data.get("main_only") is not True:
        return frozenset()
    recipients = data.get("recipients")
    if not isinstance(recipients, list) or len(recipients) > 32:
        return frozenset()
    found = set()
    for recipient in map(recipient_input, recipients):
        principal = recipient.get("principal") if isinstance(recipient, dict) else None
        if (
            isinstance(principal, dict)
            and set(principal) == {"actor"}
            and isinstance(principal["actor"], str)
            and "target_work_item_id" not in recipient
        ):
            found.add(principal["actor"])
    return frozenset(found)


def held_by(state: Json | None) -> str | None:
    """The Agent holding an active WI that is not a child; a child is held by
    its parent's session, so the parent is the address."""
    if state is None or state["status"] != "active" or state.get("parent"):
        return None
    holder = state.get("holder")
    return holder["actor"] if isinstance(holder, dict) else None


def resolve_target(actor: str, context: HostContext) -> str:
    held = (context.recipient_work or {}).get(actor) or []
    if len(held) != 1:
        raise Rejected("invalid_input", untargeted(actor, held))
    return str(held[0]["work_item_id"])


MAIN_ONLY_ACTION = "主身合同：在与人类的会话中处理"
# Relay scheduler runs: unattended sessions that never answer a main-only contract.
UNATTENDED_ENTRY = "scheduler"


def same_lineage(state: Json, item: Json) -> bool:
    """A contract stays answerable while its offering Agent still bears the WI.

    Each generation since the offer must come from that Agent's own succession
    (Host rollover or continue_here); a takeover, reopening, responsibility
    transfer or a pass through another Agent ends the offer's authority for good."""
    if item["generation"] == state["generation"]:
        return True
    actor = item["offered_by"]["actor"]
    if (state.get("holder") or {}).get("actor") != actor:
        return False
    successions = state.get("successions", {})
    return all(
        successions.get(str(g)) == actor
        for g in range(item["generation"] + 1, state["generation"] + 1)
    )


def main_only(item: Json) -> bool:
    """Contracts offered before the field existed may be handled unattended."""
    return bool(item.get("main_only", False))


def target_of(item: Json, actor: str) -> Json:
    """The target WI an Agent recipient was given, as an optional field."""
    for recipient in item["recipients"]:
        if recipient["principal"]["actor"] == actor and "target_work_item_id" in recipient:
            return {"target_work_item_id": recipient["target_work_item_id"]}
    return {}


def awaiting(state: Json, actor: str, viewer: Json | None = None) -> list[Json]:
    """Open contracts that the Agent still has to accept, reject or settle,
    main-only contracts first.

    With a `viewer` session, contracts on whose requesting side it stands are
    left out: that session cannot answer them."""
    if state["status"] not in {"draft", "active"}:
        return []
    owed = [
        {
            "contract_id": key,
            "kind": item["kind"],
            "status": item["responses"][actor]["status"],
            "required": item["required"],
            "main_only": main_only(item),
            **({"deadline": item["deadline"]} if "deadline" in item else {}),
            **target_of(item, actor),
        }
        for key, item in state.get("collaborations", {}).items()
        if not item["withdrawn"]
        and actor in item["responses"]
        and (viewer is None or not requesting(state, item, viewer))
        and (
            item["responses"][actor]["status"] == "pending"
            or item["responses"][actor]["status"] == "accepted"
            and item["kind"] != "responsibility"
        )
    ]
    owed.sort(key=lambda entry: not entry["main_only"])
    return owed


# Answers that hand the contract back to its requesting Agent, most urgent first.
ANSWERED = ("returned", "rejected", "completed")
# Requester options per answer. resend and reassign are reassign_collaboration
# (same or new recipients); withdraw, adopt and adopt_partial are
# withdraw_collaboration whose reason names what was adopted.
SETTLEMENT = {
    "returned": ["resend", "reassign", "withdraw", "adopt_partial"],
    "rejected": ["reassign", "withdraw"],
    "completed": ["adopt"],
}
SETTLE_ACTION = (
    "Settle the answered contract as its requester: resend or reassign with "
    "reassign_collaboration (same or new recipients); withdraw, adopt or adopt_partial "
    "with withdraw_collaboration {contract_id, reason} whose reason names what was adopted "
    "(there is no settlement or decision field). "
    "Only the WorkItem's current holder writes either."
)


def awaiting_settlement(state: Json, actor: str) -> list[Json]:
    """Contracts the Agent offered that came back answered and that it has not
    yet settled. Settled means withdrawn: the requester's withdraw_collaboration
    or reassign_collaboration is the one structured fact naming the contract."""
    if state["status"] not in {"draft", "active"}:
        return []
    owed = []
    for key, item in state.get("collaborations", {}).items():
        if item["withdrawn"] or item["offered_by"]["actor"] != actor:
            continue
        answered = {
            name: response["status"]
            for name, response in item["responses"].items()
            if response["status"] in ANSWERED
        }
        if not answered:
            continue
        status = next(s for s in ANSWERED if s in answered.values())
        owed.append(
            {
                "contract_id": key,
                "kind": item["kind"],
                "status": status,
                "answered_by": sorted(answered),
                "options": SETTLEMENT[status],
            }
        )
    return owed


def contract(state: Json, key: str) -> Json:
    identity(key)
    item = state.get("collaborations", {}).get(key)
    if item is None:
        raise Rejected(
            "domain_conflict",
            "Unknown original collaboration contract on this WI: answer on the offering WI "
            "(list shows it under awaiting_response) with the full contract_id",
        )
    return dict(item)


def recipient_input(recipient: Any) -> Any:
    """Accept the shape observations show, {actor, resources}, for an Agent recipient."""
    if isinstance(recipient, dict) and "actor" in recipient and "principal" not in recipient:
        return {"principal": {"actor": recipient["actor"]},
                **{k: v for k, v in recipient.items() if k != "actor"}}
    return recipient


def target(recipient: Json, principal: Json, data: Json, wi: str) -> None:
    """The recipient's own WI on which it handles a main-only contract. Whether
    that WI exists and who holds it is read at delivery, since holders change."""
    value = recipient["target_work_item_id"]
    if not isinstance(value, str) or not value.startswith("wi_"):
        raise Rejected("invalid_input", "target_work_item_id names one WI ID")
    identity(value)
    if data["main_only"] is not True:
        raise Rejected("invalid_input", "A target WI is only for a main_only contract")
    if data["kind"] == "responsibility":
        raise Rejected("invalid_input", "Full responsibility moves this WI; it has no target")
    if len(principal) != 1:
        raise Rejected(
            "invalid_input", "An exact-session recipient answers from that session; omit the target WI"
        )
    if value == wi:
        raise Rejected("invalid_input", "The target WI is the recipient's own work, not this WI")


def offer(state: Json, data: Json, context: HostContext, operation: str, wi: str) -> Decision:
    if isinstance(data, dict) and type(data.get("main_only")) is not bool:
        raise Rejected("invalid_input", MAIN_ONLY_REQUIRED)
    fields(data, *guide.shape("offer_collaboration"))
    if "deadline" in data:
        deadline(data["deadline"])
    holder(state, context)
    workflow.nonempty(data["purpose"])
    if (
        data["kind"] not in {"request", "delegate", "responsibility"}
        or type(data["required"]) is not bool
    ):
        raise Rejected("invalid_input", "Unknown collaboration responsibility")
    recipients = data["recipients"]
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 32:
        raise Rejected("invalid_input", "Expected bounded recipients")
    recipients = [recipient_input(recipient) for recipient in recipients]
    if data["kind"] == "responsibility" and len(recipients) != 1:
        raise Rejected(
            "invalid_input", "Complete responsibility has exactly one successor"
        )
    if (
        data["kind"] == "responsibility"
        and context.actor != state["definition"]["owner"]
    ):
        raise Rejected(
            "authority_denied",
            "Only the responsible owner may transfer full responsibility",
        )
    actors = set()
    for index, recipient in enumerate(recipients):
        fields(recipient, {"principal", "resources"}, {"target_work_item_id"})
        principal = recipient["principal"]
        fields(principal, {"actor"}, {"session", "surface"})
        if len(principal) == 2:
            raise Rejected(
                "invalid_input", "A recipient is an Agent or one exact session"
            )
        for value in principal.values():
            identity(value)
        if principal["actor"] in actors or principal == context.principal():
            raise Rejected(
                "invalid_input", "Recipients must be distinct counterparties"
            )
        actors.add(principal["actor"])
        # Another session of the offering Agent may take a request or a
        # delegation; full responsibility within one Agent moves by rollover.
        if data["kind"] == "responsibility" and principal["actor"] == context.actor:
            raise Rejected(
                "invalid_input", "Responsibility within one Agent moves by rollover"
            )
        # Responsibility moves the execution holder; like rollover it stays on
        # this surface with one exact successor. Requests and delegations reach
        # any surface writing the same ledger (AH-088).
        if data["kind"] == "responsibility" and "session" not in principal:
            raise Rejected(
                "invalid_input", "Full responsibility names one exact successor session"
            )
        if data["kind"] == "responsibility" and principal["surface"] != context.surface:
            raise Rejected(
                "domain_conflict", "Cross-surface transfer requires migration"
            )
        resources = workflow.resource_paths(recipient["resources"])
        if any(not workflow.within(r, state["resources"]) for r in resources):
            raise Rejected(
                "authority_denied",
                f"Contract exceeds current resource scope: recipient resources {resources} "
                f"must lie within this WI's resources {state['resources']}",
            )
        if data["kind"] == "responsibility" and resources != state["resources"]:
            raise Rejected(
                "invalid_input", "Full responsibility must include the whole scope"
            )
        if "target_work_item_id" in recipient:
            target(recipient, principal, data, wi)
        elif data["main_only"] is True and data["kind"] != "responsibility" and len(principal) == 1:
            # Delivery reaches a main session only through the WI it holds; the
            # one active WI the recipient holds is recorded as its target.
            recipients[index] = {**recipient,
                                 "target_work_item_id": resolve_target(principal["actor"], context)}
    if context.source is None:
        raise Rejected(
            "source_unavailable", "Collaboration requires its original source"
        )
    return Decision(
        [
            event(
                "CollaborationOffered",
                contract_id=operation,
                kind=data["kind"],
                recipients=recipients,
                purpose=data["purpose"],
                required=data["required"],
                main_only=data["main_only"],
                **({"deadline": data["deadline"]} if "deadline" in data else {}),
                offered_by=context.principal(),
                generation=state["generation"],
                source=context.source.sha256,
            )
        ],
        {"contract_id": operation},
    )


def decide(
    state: Json, intent: str, data: Json, context: HostContext, operation: str, wi: str
) -> Decision:
    if intent == "offer_collaboration":
        return offer(state, data, context, operation, wi)
    if intent == "reassign_collaboration":
        # The successor keeps the original main_only unless the requester restates it.
        fields(data, {"contract_id", "recipients", "reason"}, {"main_only"})
        old = contract(state, data["contract_id"])
        withdrawn = decide(
            state,
            "withdraw_collaboration",
            {"contract_id": data["contract_id"], "reason": data["reason"]},
            context,
            operation,
            wi,
        )
        new = offer(
            state,
            {
                **{
                    k: (data["recipients"] if k == "recipients" else old[k])
                    for k in ("kind", "recipients", "purpose", "required", "deadline")
                    if k in old
                },
                "main_only": data.get("main_only", main_only(old)),
            },
            context,
            operation,
            wi,
        )
        return Decision(
            withdrawn.events + new.events,
            {"contract_id": operation, "previous_contract_id": data["contract_id"]},
        )
    if intent == "withdraw_collaboration":
        fields(data, {"contract_id", "reason"})
        holder(state, context)
        workflow.nonempty(data["reason"])
        item = contract(state, data["contract_id"])
        if item["withdrawn"]:
            return Decision([], {"contract_id": data["contract_id"], "withdrawn": True})
        if item["kind"] == "responsibility" and any(
            r["status"] == "accepted" for r in item["responses"].values()
        ):
            raise Rejected(
                "domain_conflict",
                "Accepted responsibility needs a new successor contract",
            )
        events = [
            event(
                "CollaborationWithdrawn",
                contract_id=data["contract_id"],
                reason=data["reason"],
                actor=context.actor,
            )
        ]
        for response in item["responses"].values():
            grant = response.get("grant_id")
            if grant and state["grants"][grant]["active"]:
                events.append(
                    event("ScopeRevoked", grant_id=grant, reason=data["reason"])
                )
        return Decision(events)
    fields(data, *guide.shape("respond_collaboration"))
    workflow.nonempty(data["reason"])
    item = contract(state, data["contract_id"])
    if state["status"] not in {"draft", "active"} or item["withdrawn"]:
        raise Rejected("domain_conflict", "Original collaboration is no longer open")
    recipient = next((r for r in item["recipients"] if answers(item, r, context)), None)
    if recipient is None:
        raise Rejected(
            "authority_denied", "Only the exact recipient may answer this contract"
        )
    if requesting(state, item, context.principal()):
        raise Rejected(
            "authority_denied", "The requesting session cannot answer its own contract"
        )
    if main_only(item) and context.entry == UNATTENDED_ENTRY:
        raise Rejected(
            "authority_denied",
            "A main_only contract is answered only from the main session with the "
            "human, never from an unattended scheduler run. " + MAIN_ONLY_ACTION,
        )
    previous = item["responses"][context.actor]
    decision = data["decision"]
    if context.source is None:
        raise Rejected(
            "source_unavailable", "Recipient decision requires its original source"
        )
    if decision == "return" and previous["status"] == "returned":
        return Decision(
            [],
            {
                "contract_id": data["contract_id"],
                "decision": decision,
                "grant_id": None,
            },
        )
    if decision == "accept" and not same_lineage(state, item):
        raise Rejected(
            "authority_changed",
            "Original collaboration belongs to an earlier authority generation",
        )
    if decision not in {"accept", "reject", "return", "complete"}:
        raise Rejected("invalid_input", "Unknown recipient decision")
    if previous["status"] != "pending" and decision in {"accept", "reject"}:
        raise Rejected(
            "domain_conflict", "Recipient already answered the original offer"
        )
    if decision in {"return", "complete"} and previous["status"] != "accepted":
        raise Rejected(
            "domain_conflict",
            "Only an accepted responsibility may be returned/completed: answer accept first, "
            "then return or complete",
        )
    if item["kind"] == "responsibility" and decision in {"return", "complete"}:
        raise Rejected(
            "domain_conflict",
            "Full responsibility is transferred through a new contract",
        )
    evidence = data.get("evidence", [])
    if decision == "complete":
        from .review import validated_evidence

        validated_evidence(evidence, context)
    elif evidence:
        raise Rejected(
            "invalid_input", "Evidence belongs to the actual completion decision"
        )
    events = []
    grant_id = None
    if decision == "accept" and item["kind"] == "delegate":
        if state["status"] != "active" or state["active_candidate"] is not None:
            raise Rejected("domain_conflict", "Delegation requires active execution")
        if any(
            g["active"]
            and any(
                workflow.overlaps(r, s)
                for r in recipient["resources"]
                for s in g["resources"]
            )
            for g in state["grants"].values()
        ) or any(
            a["status"] == "running"
            and any(
                workflow.overlaps(r, s)
                for r in recipient["resources"]
                for s in a["resources"]
            )
            for a in state["attempts"].values()
        ):
            raise Rejected(
                "domain_conflict",
                "Resource is already held by a live task or delegation",
            )
        grant_id = operation + ".scope"
        events.append(
            event(
                "ScopeGranted",
                grant_id=grant_id,
                principal=context.principal(),
                resources=recipient["resources"],
                intents=TASK_RIGHTS,
                generation=state["generation"],
            )
        )
    if decision == "accept" and item["kind"] == "responsibility":
        if item["offered_by"]["actor"] != state["definition"]["owner"]:
            raise Rejected(
                "authority_changed", "Offer no longer belongs to the responsible owner"
            )
        events.append(
            event(
                "ResponsibilityTransferred",
                contract_id=data["contract_id"],
                principal=context.principal(),
                generation=state["generation"] + 1,
            )
        )
    if decision in {"return", "complete"} and previous.get("grant_id"):
        events.append(
            event("ScopeRevoked", grant_id=previous["grant_id"], reason=data["reason"])
        )
    events.append(
        event(
            "CollaborationResponded",
            contract_id=data["contract_id"],
            actor=context.actor,
            decision=decision,
            reason=data["reason"],
            evidence=evidence,
            source=context.source.sha256,
            grant_id=grant_id or previous.get("grant_id"),
            principal=context.principal(),
        )
    )
    return Decision(
        events,
        {
            "contract_id": data["contract_id"],
            "decision": decision,
            "grant_id": grant_id,
        },
    )


def pending(state: Json) -> bool:
    return any(
        item["required"]
        and not item["withdrawn"]
        and any(
            r["status"] != "completed"
            and not (item["kind"] == "responsibility" and r["status"] == "accepted")
            for r in item["responses"].values()
        )
        for item in state.get("collaborations", {}).values()
    )


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "CollaborationOffered":
        state.setdefault("collaborations", {})[data["contract_id"]] = {
            **data,
            "withdrawn": False,
            "responses": {
                r["principal"]["actor"]: {"status": "pending", "history": []}
                for r in data["recipients"]
            },
        }
    elif kind == "CollaborationWithdrawn":
        state["collaborations"][data["contract_id"]]["withdrawn"] = True
    elif kind == "CollaborationResponded":
        response = state["collaborations"][data["contract_id"]]["responses"][
            data["actor"]
        ]
        if data["decision"] == "accept" and "principal" in data:
            # The first accepting session bears the contract.
            response["principal"] = data["principal"]
        response.update(
            status={
                "accept": "accepted",
                "reject": "rejected",
                "return": "returned",
                "complete": "completed",
            }[data["decision"]],
            grant_id=data["grant_id"],
        )
        response["history"].append(
            {k: v for k, v in data.items() if k != "principal"}
        )
    elif kind == "ResponsibilityTransferred":
        workflow.invalidate_attempts(state)
        state["definition"] = {
            **state["definition"],
            "owner": data["principal"]["actor"],
        }
        state["generation"] = data["generation"]
        if state["status"] == "active":
            state["holder"] = data["principal"]
        for grant in state["grants"].values():
            grant["active"] = False
    else:
        return False
    return True


def public_contracts(state: Json) -> list[Json]:
    return [
        {
            "contract_id": key,
            "kind": item["kind"],
            "purpose": item["purpose"],
            "required": item["required"],
            "main_only": main_only(item),
            **({"deadline": item["deadline"]} if "deadline" in item else {}),
            "withdrawn": item["withdrawn"],
            "offered_by": item["offered_by"]["actor"],
            "recipients": [
                {"actor": r["principal"]["actor"], "resources": r["resources"],
                 **({"target_work_item_id": r["target_work_item_id"]}
                    if "target_work_item_id" in r else {})}
                for r in item["recipients"]
            ],
            "responses": {
                actor: {k: v for k, v in response.items() if k != "principal"}
                for actor, response in item["responses"].items()
            },
        }
        for key, item in state.get("collaborations", {}).items()
    ]
