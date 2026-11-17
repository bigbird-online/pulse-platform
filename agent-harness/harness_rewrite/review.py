"""Candidate, independent verification and human review rules inside the domain."""

from __future__ import annotations

from typing import Any, cast

from . import workflow, delivery, collaboration, rounds, publication
from .model import (
    Decision,
    HostContext,
    Json,
    Rejected,
    digest,
    fields,
    human_decision,
    identity,
    native_session_ref,
)

INTENTS = frozenset(
    {
        "assign_verification",
        "acknowledge_verification",
        "record_verification",
        "record_review",
        "waive_verification",
        "revise_work",
        "complete",
    }
)


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def current(state: Json, candidate_id: str) -> bool:
    return bool(
        state["status"] == "active" and state["active_candidate"] == candidate_id
    )


def candidate(state: Json, candidate_id: Any) -> Json:
    identity(candidate_id)
    item = state["candidates"].get(candidate_id)
    if item is None:
        raise Rejected("domain_conflict", "Unknown fixed candidate")
    return cast(Json, item)


def owner(state: Json, context: HostContext) -> None:
    if (
        state["holder"] != context.principal()
        or state["generation"] != context.generation
    ):
        raise Rejected("authority_denied", "Current execution holder is required")


def auto_candidate(
    state: Json | None, operation: str, context: HostContext, wi: str
) -> list[Json]:
    if (
        state is None
        or state["status"] != "active"
        or state["profile"] != "standard"
        or state["active_candidate"] is not None
        or state["graph"] is None
        or state["graph"]["definition_revision"] != state["definition_revision"]
    ):
        return []
    scope = rounds.select(state)
    if scope is None:
        return []
    selected = [state["attempts"][a] for a in state["node_results"].values()]
    artifacts = [
        ref for attempt in selected for ref in attempt["result"]["outputs"].values()
    ]
    artifacts = sorted(
        {digest(ref): ref for ref in artifacts}.values(),
        key=lambda ref: (ref["object"], ref["version"]),
    )
    automatic_verifier = context.verification_actor or next(
        (
            c["content"].get("automatic_verifier")
            for c in reversed(list(state["candidates"].values()))
            if c["content"].get("automatic_verifier")
        ),
        None,
    )
    content = {
        **scope,
        "automatic_verifier": automatic_verifier,
        "definition_revision": state["definition_revision"],
        "definition": state["definition"],
        "graph_revision": state["graph"]["revision"],
        "graph": state["graph"],
        "artifacts": artifacts,
        "result_ids": [a["result"]["result_id"] for a in selected],
        "round": state["execution_round"],
        "producers": sorted(
            {state["definition"]["owner"]} | {a["principal"]["actor"] for a in selected}
        ),
    }
    candidate_id = "candidate_" + operation.removeprefix("op_")
    sources = publication.frozen(state, artifacts)
    if sources:
        content["sources"] = sources
    events = [
        event(
            "CandidateSubmitted",
            candidate_id=candidate_id,
            content=content,
            content_digest=digest(content),
        )
    ]
    if automatic_verifier is not None:
        if automatic_verifier in content["producers"]:
            raise Rejected(
                "authority_denied", "Configured verifier is a candidate producer"
            )
        raw = digest(["independent-verification", wi, candidate_id])[:32]
        native_id = "-".join((raw[:8], raw[8:12], raw[12:16], raw[16:20], raw[20:]))
        principal = {
            "actor": automatic_verifier,
            "session": native_session_ref(
                context.namespace,
                automatic_verifier,
                native_id,
                context.surface,
            ),
            "surface": context.surface,
        }
        assignment = operation + ".verify"
        events.extend(
            [
                event(
                    "VerificationAssigned",
                    candidate_id=candidate_id,
                    assignment_id=assignment,
                    verifier=principal,
                ),
                event(
                    "EffectRequested",
                    effect_id=operation + ".dispatch",
                    effect_kind="verification_dispatch",
                    candidate_id=candidate_id,
                    generation=state["generation"],
                    artifact=None,
                    target="verification/" + candidate_id,
                    required=False,
                    node_id=None,
                    snapshot=content,
                    assignment_id=assignment,
                    recipient=principal,
                    native_session_id=native_id,
                ),
            ]
        )
    return events


def validated_evidence(refs: Any, context: HostContext) -> None:
    if not isinstance(refs, list) or not refs:
        raise Rejected(
            "source_invalid",
            "Independent checks require captured evidence: put the file in the envelope "
            "files {name: {path, logical, version}} and list it in input as evidence: "
            "[{\"file\": name}]",
        )
    known = [a.reference() for a in context.artifacts]
    for ref in refs:
        workflow.artifact_ref(ref)
        if ref not in known:
            raise Rejected(
                "source_invalid",
                "Check evidence has no matching original bytes: reference only files captured "
                "in this same envelope's files, as {\"file\": name}; never write a hash",
            )


def decide(
    state: Json, intent: str, data: Json, context: HostContext, operation: str, wi: str
) -> Decision:
    if intent == "assign_verification":
        fields(data, {"candidate_id", "verifier"})
        item = candidate(state, data["candidate_id"])
        owner(state, context)
        fields(data["verifier"], {"actor", "session", "surface"})
        for value in data["verifier"].values():
            identity(value)
        if not current(state, data["candidate_id"]) or item["assignment"] is not None:
            raise Rejected(
                "domain_conflict",
                "Candidate already has an assignment or is no longer current",
            )
        if data["verifier"]["actor"] in item["content"]["producers"]:
            raise Rejected(
                "authority_denied",
                "A candidate producer cannot independently verify it",
            )
        workflow.control_ready(
            state,
            data["candidate_id"],
            "verification",
            [a.reference() for a in context.artifacts],
        )
        return Decision(
            [
                event(
                    "VerificationAssigned",
                    candidate_id=data["candidate_id"],
                    assignment_id=operation,
                    verifier=data["verifier"],
                )
            ],
            {"assignment_id": operation},
        )
    if intent in {"acknowledge_verification", "record_verification"}:
        required = {"candidate_id", "assignment_id"}
        if intent == "record_verification":
            required |= {"verdict", "checks", "reason"}
        fields(data, required, {"outputs"} if intent == "record_verification" else None)
        item = candidate(state, data["candidate_id"])
        assignment = item["assignment"]
        if (
            assignment is None
            or data["assignment_id"] != assignment["id"]
            or assignment["verifier"] != context.principal()
        ):
            raise Rejected(
                "authority_denied",
                "Only the exact assigned verifier may acknowledge/report",
            )
        if intent == "acknowledge_verification":
            if assignment["acknowledged"]:
                return Decision(
                    [], {"assignment_id": assignment["id"], "acknowledged": True}
                )
            return Decision(
                [event("VerificationAcknowledged", candidate_id=data["candidate_id"])]
            )
        if not assignment["acknowledged"]:
            raise Rejected(
                "domain_conflict", "Verifier must acknowledge the original assignment"
            )
        workflow.control_ready(
            state,
            data["candidate_id"],
            "verification",
            [a.reference() for a in context.artifacts],
        )
        source = context.source
        if (
            source is None
            or source.kind != "independent_result"
            or source.approved_intent != intent
            or source.approved_input_digest
            != digest({"intent": intent, "work_item_id": wi, "input": data})
        ):
            raise Rejected(
                "source_invalid",
                "Receipt must bind the original independent bytes to this result",
            )
        if not isinstance(data["verdict"], str) or data["verdict"] not in {
            "accepted",
            "partial",
            "blocked",
            "rejected",
        }:
            raise Rejected("invalid_input", "Unknown verification verdict")
        workflow.nonempty(data["reason"])
        if not isinstance(data["checks"], list):
            raise Rejected(
                "invalid_input", "Expected per-criterion verification checks"
            )
        expected = set(
            item["content"].get(
                "acceptance_refs",
                [
                    f"AC-{i + 1}"
                    for i in range(len(item["content"]["definition"]["acceptance"]))
                ],
            )
        )
        rounds.outputs(state, data["candidate_id"], "verification", data, context)
        seen = set()
        for check in data["checks"]:
            fields(check, {"criterion", "verdict", "evidence"})
            identity(check["criterion"])
            if (
                check["criterion"] not in expected
                or check["criterion"] in seen
                or check["verdict"] not in ("passed", "failed", "unknown")
            ):
                raise Rejected("invalid_input", "Invalid or repeated criterion check")
            seen.add(check["criterion"])
            validated_evidence(check["evidence"], context)
        if data["verdict"] == "accepted" and (
            seen != expected or any(c["verdict"] != "passed" for c in data["checks"])
        ):
            raise Rejected(
                "domain_conflict",
                "Accepted verification requires every criterion with evidence",
            )
        value = {**data, "source_digest": source.sha256}
        if item["verification"] is not None:
            if item["verification"]["digest"] != digest(value):
                raise Rejected(
                    "domain_conflict", "An independent result cannot be overwritten"
                )
            return Decision([], {"receipt_id": item["verification"]["id"]})
        return Decision(
            [
                event(
                    "VerificationRecorded",
                    candidate_id=data["candidate_id"],
                    verification={"id": operation, "digest": digest(value), **value},
                )
            ],
            {"receipt_id": operation, "current": current(state, data["candidate_id"])},
        )
    if intent == "record_review":
        fields(data, {"candidate_id", "decision", "reason"}, {"outputs"})
        item = candidate(state, data["candidate_id"])
        human_source = human_decision(context, intent, data, wi)
        if data["decision"] not in ("approve", "changes_requested"):
            raise Rejected("invalid_input", "Unknown review decision")
        workflow.nonempty(data["reason"])
        rounds.outputs(state, data["candidate_id"], "review", data, context)
        if data["decision"] == "approve" and not verified(item):
            raise Rejected(
                "domain_conflict",
                "Approval requires accepted verification or an explicit waiver",
            )
        if data["decision"] == "approve":
            workflow.control_ready(
                state,
                data["candidate_id"],
                "review",
                [a.reference() for a in context.artifacts],
            )
        if item["review"] is not None:
            raise Rejected(
                "domain_conflict",
                "Candidate review is immutable; revise the work for a new decision",
            )
        return Decision(
            [
                event(
                    "ReviewRecorded",
                    candidate_id=data["candidate_id"],
                    review={**data, "source_digest": human_source, "id": operation},
                )
            ]
        )
    if intent == "waive_verification":
        fields(data, {"candidate_id", "action", "reason", "risk", "observed_verdict"})
        item = candidate(state, data["candidate_id"])
        human_source = human_decision(context, intent, data, wi)
        workflow.nonempty(data["reason"])
        workflow.nonempty(data["risk"])
        if data["action"] not in ("waive", "revoke"):
            raise Rejected("invalid_input", "Unknown waiver action")
        actual = (
            None if item["verification"] is None else item["verification"]["verdict"]
        )
        if data["observed_verdict"] != actual:
            raise Rejected(
                "domain_conflict",
                "Waiver must acknowledge the actual independent result",
            )
        return Decision(
            [
                event(
                    "VerificationWaived",
                    candidate_id=data["candidate_id"],
                    waiver={**data, "source_digest": human_source, "id": operation},
                )
            ]
        )
    if intent == "revise_work":
        fields(data, {"candidate_id", "nodes", "reason"})
        item = candidate(state, data["candidate_id"])
        owner(state, context)
        workflow.nonempty(data["reason"])
        if not current(state, data["candidate_id"]):
            raise Rejected(
                "domain_conflict", "Rework must refer to the current candidate"
            )
        negative = (
            item["verification"] is not None
            and item["verification"]["verdict"] != "accepted"
            or item["review"] is not None
            and item["review"]["decision"] == "changes_requested"
        )
        if not negative:
            human_decision(context, intent, data, wi)
        nodes = data["nodes"]
        known = {
            n["node_id"]
            for n in (state["graph"] or {"nodes": []})["nodes"]
            if n["kind"] in workflow.TASK_KINDS
        }
        if (
            not isinstance(nodes, list)
            or (not nodes and not item["content"].get("historical"))
            or any(not isinstance(n, str) for n in nodes)
            or not set(nodes) <= known
        ):
            raise Rejected(
                "invalid_input", "Rework must identify existing execution nodes"
            )
        affected = set(nodes)
        for node_id in workflow.topological(state["graph"]) if state["graph"] else []:
            if any(
                e["to"] == node_id and e["from"] in affected
                for e in state["graph"]["edges"]
            ):
                affected.add(node_id)
        return Decision(
            [
                event(
                    "WorkRevised",
                    candidate_id=data["candidate_id"],
                    affected=sorted(affected),
                    reason=data["reason"],
                )
            ]
        )
    if intent == "complete":
        fields(data, {"candidate_id"})
        item = candidate(state, data["candidate_id"])
        owner(state, context)
        if (
            state["profile"] != "standard"
            or not item["content"].get("terminal", True)
            or not current(state, data["candidate_id"])
            or not verified(item)
            or item["review"] is None
            or item["review"]["decision"] != "approve"
        ):
            raise Rejected(
                "domain_conflict",
                "Current candidate verification and approval are required",
            )
        # Real delivery is implemented by S3. It must never be silently waived.
        status = workflow.stages(state)
        if collaboration.pending(state):
            raise Rejected(
                "domain_conflict", "Required collaboration has not been settled"
            )
        if delivery_pending(state):
            raise Rejected("domain_conflict", "Required delivery has not been observed")
        for node in (state["graph"] or {"nodes": []})["nodes"]:
            if node["required"] and node["kind"] in {"verification", "review"}:
                if status[node["node_id"]] == "skipped":
                    continue
                value = workflow.node_result(state, node["node_id"])
                if (
                    status[node["node_id"]] != "finished"
                    or value is None
                    or value["outcome"]
                    not in node["completion_rule"]["success_outcomes"]
                ):
                    raise Rejected(
                        "domain_conflict",
                        "A required review node has not met its declared contract",
                    )
        if not item["content"]["artifacts"]:
            raise Rejected("domain_conflict", "No fixed work artifact was submitted")
        return Decision(
            [
                event(
                    "WorkCompleted",
                    candidate_id=data["candidate_id"],
                    review_id=item["review"]["id"],
                    verification_id=None
                    if item["verification"] is None
                    else item["verification"]["id"],
                    waiver_id=None if item["waiver"] is None else item["waiver"]["id"],
                )
            ]
        )
    raise AssertionError("Unknown review intent")


def verified(item: Json) -> bool:
    return bool(
        item["verification"] is not None
        and item["verification"]["verdict"] == "accepted"
        or item["waiver"] is not None
        and item["waiver"]["action"] == "waive"
    )


def stage(item: Json) -> str:
    if item["review"] is not None and item["review"]["decision"] == "changes_requested":
        return "needs_revision"
    if verified(item):
        return "approved" if item["review"] is not None else "awaiting_review"
    if item["verification"] is not None:
        return "needs_revision"
    return "awaiting_verification"


def delivery_pending(state: Json) -> bool:
    from .existing_business import pending as original_pending
    if original_pending(state):
        return True
    if delivery.pending(state, state["active_candidate"]):
        return True
    status = workflow.stages(state)
    return bool(
        state["graph"] is not None
        and any(
            n["required"]
            and n["kind"] == "delivery"
            and status[n["node_id"]] not in {"skipped", "finished"}
            for n in state["graph"]["nodes"]
        )
    )


def progress(state: Json) -> str:
    value = stage(state["candidates"][state["active_candidate"]])
    return (
        "awaiting_delivery"
        if value == "approved" and delivery_pending(state)
        else value
    )


def public_candidates(state: Json) -> Json:
    return {
        "active": state["active_candidate"],
        "items": [
            {
                "id": key,
                "content": item["content"],
                "content_digest": item["content_digest"],
                "stage": stage(item),
                "assignment": None
                if item["assignment"] is None
                else {
                    "id": item["assignment"]["id"],
                    "verifier": item["assignment"]["verifier"]["actor"],
                    "acknowledged": item["assignment"]["acknowledged"],
                },
                "verification": item["verification"],
                "review": item["review"],
                "waiver": item["waiver"],
            }
            for key, item in state["candidates"].items()
        ],
    }


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "CandidateSubmitted":
        state["active_candidate"] = data["candidate_id"]
        state["candidates"][data["candidate_id"]] = {
            **data,
            "assignment": None,
            "verification": None,
            "review": None,
            "waiver": None,
        }
    elif kind == "VerificationAssigned":
        state["candidates"][data["candidate_id"]]["assignment"] = {
            "id": data["assignment_id"],
            "verifier": data["verifier"],
            "acknowledged": False,
        }
    elif kind == "VerificationAcknowledged":
        state["candidates"][data["candidate_id"]]["assignment"]["acknowledged"] = True
    elif kind == "VerificationRecorded":
        state["candidates"][data["candidate_id"]]["verification"] = data["verification"]
    elif kind == "ReviewRecorded":
        state["candidates"][data["candidate_id"]]["review"] = data["review"]
    elif kind == "VerificationWaived":
        state["candidates"][data["candidate_id"]]["waiver"] = data["waiver"]
    elif kind == "WorkRevised":
        state["active_candidate"] = None
        state["execution_round"] += 1
        for node_id in data["affected"]:
            state["node_results"].pop(node_id, None)
            state.get("control_results", {}).pop(node_id, None)
        workflow.invalidate_attempts(state)
    elif kind == "WorkCompleted":
        state.update(status="completed", holder=None, terminal=data)
        for grant in state["grants"].values():
            grant["active"] = False
        workflow.invalidate_attempts(state)
    else:
        return False
    return True
