"""Pure lifecycle decisions and replay. No filesystem, environment, network or old core."""

from __future__ import annotations

from . import affiliation

from copy import deepcopy
from typing import Any

from .model import (
    Decision,
    HostContext,
    Json,
    Rejected,
    digest,
    encoded,
    fields,
    human_decision,
    deadline,
    identity,
)
from . import workflow, review, delivery, collaboration, control, rounds, workspaces
from . import publication, migration, measurements, notifications, legacy, access, quota
from . import pull_requests, narrative, children, guide, self_rollover, self_switch, merge_authority
from .workflow import nonempty as _text, resource_paths as _resources, overlaps, within

INTENTS = (
    review.INTENTS
    | migration.INTENTS
    | legacy.INTENTS
    | measurements.INTENTS
    | notifications.INTENTS
    | delivery.INTENTS
    | collaboration.INTENTS
    | control.INTENTS
    | workspaces.INTENTS
    | publication.INTENTS
    | access.INTENTS
    | quota.INTENTS
    | quota.WORKER_INTENTS
    | workspaces.WORKER_INTENTS
    | frozenset(
        {
            "create_draft",
            "create_lightweight",
            "start_work",
            "review_existing_execution",
            "checkpoint",
            "record_work",
            "grant_scope",
            "revoke_scope",
            "rollover",
            "prepare_rollover",
            self_rollover.INTENT,
            self_switch.INTENT,
            "accept_transfer",
            "stop",
            "adopt_plan",
            "revise_plan",
            "claim_task",
            "record_task_result",
            "abandon_task",
            "complete_lightweight",
            "promote_workflow",
            "amend_definition",
            "add_requirement",
            "rename_work",
            "set_main_only",
            "record_project_narrative",
        }
    )
)
WORK_INTENTS = frozenset({"checkpoint", "record_work"})
# What a child accepts while its parent is not active: the human's stop or
# termination, a revocation that only narrows authority, observations and notices,
# and settlement of effects already decided.
PAUSED_CHILD_INTENTS = (
    frozenset({"stop", "terminate_work", "revoke_merge_authority"})
    | measurements.INTENTS
    | notifications.INTENTS
    | delivery.WORKER_INTENTS
    | quota.WORKER_INTENTS
    | workspaces.WORKER_INTENTS
)
SCOPED_INTENTS = WORK_INTENTS | workflow.TASK_INTENTS | {"capture_source"}


INTENTS = (INTENTS | affiliation.INTENTS | pull_requests.INTENTS | children.INTENTS
           | merge_authority.INTENTS)


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def _is_holder(state: Json, context: HostContext) -> bool:
    return bool(
        state["holder"] == context.principal() and state["generation"] == context.generation
    )


def _same_agent(state: Json, context: HostContext) -> bool:
    """Another session of the holder's Agent: it may append records, never control."""
    holder = state["holder"]
    return bool(
        holder is not None and holder["actor"] == context.actor and not _is_holder(state, context)
    )


def authorized(
    state: Json,
    context: HostContext,
    intent: str,
    resources: list[str] | None = None,
    *,
    same_agent_records: bool = False,
) -> bool:
    """Current execution authority for one intent and resource scope.

    `same_agent_records` admits another session of the holder's Agent to the
    holder's own scope for WORK_INTENTS only; holder and generation are unchanged.
    """
    if (
        intent not in context.allowed_intents
        or state["status"] != "active"
        or children.paused(state)
        or context.generation != state["generation"]
        or rounds.active_blocks_work(state)
        or state["graph"] is not None
        and state["graph"]["definition_revision"] != state["definition_revision"]
    ):
        return False
    requested = resources or []
    if intent in SCOPED_INTENTS and any(
        not requested
        or any(
            overlaps(r, scope) for r in requested for scope in state["workspaces"][key]["resources"]
        )
        for key in migration.inherited_resources(state)
    ):
        return False
    # A checkpoint only records where the work stands and writes no workspace; a
    # workspace whose allocation, revision or retirement is unsettled must not hide
    # that record (nor block /wi switch, which leaves through one).
    if intent in SCOPED_INTENTS - {"checkpoint"} and any(
        not workspaces.retired(state, resource)
        and not resource.get("superseded_by")
        and (workspaces.retiring(state, resource) or not workspaces.ready(state, resource))
        and any(overlaps(r, other) for r in requested for other in resource["resources"])
        for resource in state.get("workspaces", {}).values()
    ):
        return False
    # A delegated resource is no longer concurrently writable by its holder.
    holder_scope = all(within(r, state["resources"]) for r in requested) and not any(
        overlaps(r, other)
        for r in requested
        for grant in state["grants"].values()
        if grant["active"]
        for other in grant["resources"]
    )
    if _is_holder(state, context):
        return holder_scope
    if (
        same_agent_records
        and intent in WORK_INTENTS
        and _same_agent(state, context)
        and holder_scope
    ):
        return True
    return any(
        grant["active"]
        and grant["principal"] == context.principal()
        and grant["generation"] == context.generation
        and intent in grant["intents"]
        and all(within(r, grant["resources"]) for r in requested)
        for grant in state["grants"].values()
    )


def denial(
    state: Json,
    context: HostContext,
    intent: str,
    resources: list[str] | None = None,
    *,
    wi: str,
    same_agent_records: bool = False,
) -> str:
    """Why `authorized` refused, with the step that restores the right.

    Read only after a refusal; the checks follow `authorized` in its order.
    """
    requested = resources or []
    if intent not in context.allowed_intents:
        return f"Host policy does not permit {intent} for this session"
    if state["status"] != "active":
        return f"{wi} is {state['status']}, not active; execution writes need an active WI"
    if children.paused(state):
        return (
            f"The parent {state.get('parent')} is not active, so its child {wi} is paused; "
            "continue the parent first"
        )
    if context.generation != state["generation"]:
        return (
            f"This session acts at generation {context.generation} but {wi} is at generation "
            f"{state['generation']}; read the WI again and act as its current holder"
        )
    if rounds.active_blocks_work(state):
        return "A frozen candidate is under verification or review; revise_work reopens work"
    if state["graph"] is not None and state["graph"]["definition_revision"] != state["definition_revision"]:
        return "The definition changed after the adopted plan; revise_plan first"
    outside = [r for r in requested if not within(r, state["resources"])]
    if outside:
        return (
            f"Resources {outside} lie outside the WI's resources {state['resources']}; "
            "name scopes inside them (for example the WI's own scope)"
        )
    for resource in state.get("workspaces", {}).values():
        if (
            not workspaces.retired(state, resource)
            and not resource.get("superseded_by")
            and (workspaces.retiring(state, resource) or not workspaces.ready(state, resource))
            and any(overlaps(r, other) for r in requested for other in resource["resources"])
        ):
            return (
                f"Workspace {resource.get('resource_id')} on these resources is still being "
                "allocated, revised or retired; resume the WI until its effect settles"
            )
    for grant in state["grants"].values():
        if (_is_holder(state, context) or same_agent_records and _same_agent(state, context)) and grant[
            "active"
        ] and any(overlaps(r, o) for r in requested for o in grant["resources"]):
            return (
                f"Grant {grant.get('grant_id', '')} has delegated these resources to "
                f"{grant['principal'].get('actor')}; wait for it to be returned or revoked"
            )
    holder = state["holder"]
    if _is_holder(state, context):
        return f"This session holds {wi}, but the requested resources are not in its scope"
    if holder is not None and holder["actor"] == context.actor:
        allowed = " (checkpoint and record_work stay open to this session)" if (
            not same_agent_records
        ) else ""
        return (
            f"Another session of {context.actor} holds {wi}{allowed}; to move execution here, "
            f"the human sends /wi continue {wi} in this session"
        )
    if holder is not None:
        return (
            f"{wi} is held by {holder['actor']}; ask it for a delegation through "
            "offer_collaboration, or work on your own WI"
        )
    return f"{wi} has no current holder; its owner starts or continues it"


def decide(
    state: Json | None,
    intent: str,
    data: Json,
    context: HostContext,
    operation: str,
    wi: str,
    at: str,
) -> Decision:
    decision = _decide(state, intent, data, context, operation, wi, at)
    decision = Decision(
        decision.events + measurements.close_intervals(state, decision.events, context),
        decision.result,
    )
    resulting = reduce(state, decision.events)
    controls = rounds.advance(resulting)
    resulting = reduce(resulting, controls)
    return Decision(
        decision.events + controls + review.auto_candidate(resulting, operation, context, wi),
        decision.result,
    )


def _decide(
    state: Json | None,
    intent: str,
    data: Json,
    context: HostContext,
    operation: str,
    wi: str,
    at: str,
) -> Decision:
    if intent not in INTENTS:
        raise Rejected("invalid_input", "Intent is not implemented by this kernel version")
    if intent not in context.allowed_intents:
        raise Rejected("authority_denied", "Host policy does not permit this intent")
    if intent in legacy.INTENTS:
        return legacy.decide(state, intent, data, context)
    if intent in {"create_draft", "create_lightweight", "create_child"}:
        if not isinstance(data, dict) or "summary" not in data:
            raise Rejected("invalid_input", SUMMARY_GUIDE)
        parent = parent_wi = None
        if intent == "create_child":
            fields(data, *guide.shape("create_child"))
            parent_wi = children.parent_id(data)
            related = context.related_parent or {}
            parent = children.admit_parent(
                related.get("state") if related.get("id") == parent_wi else None, context
            )
            # The child's owner is its parent's; the input names no owner.
            data = {key: value for key, value in data.items() if key != "parent"}
            data["owner"] = parent["definition"]["owner"]
        fields(data, *guide.shape("create_draft" if intent == "create_draft" else "create_lightweight"))
        if state is not None:
            raise Rejected("domain_conflict", "Work item already exists")
        for key in ("why", "what", "owner"):
            _text(data[key])
        _summary(data["summary"])
        if (
            not isinstance(data["acceptance"], list)
            or not data["acceptance"]
            or any(not isinstance(v, str) or not v.strip() for v in data["acceptance"])
        ):
            raise Rejected("invalid_input", "Acceptance criteria are required")
        _resources(data["resources"])
        if context.source is None:
            raise Rejected("source_unavailable", "Creation requires original source bytes")
        profile = "standard" if intent == "create_draft" else "lightweight"
        if parent is not None and parent_wi is not None:
            created = event(
                "WorkItemCreated", definition=data, profile=profile,
                source=context.source.sha256, created_at=at, main_only=children.label(parent),
            )
            return Decision(
                [created] + children.creation_events(parent, parent_wi, context, wi, at),
                {"created": True, "parent": parent_wi},
            )
        # The owner's main body owns every standard WorkItem and every one an
        # Agent creates from the owner's own input; headless work is automatic.
        main_only = profile == "standard" or context.source.kind == "native_human"
        created = event(
            "WorkItemCreated", definition=data, profile=profile,
            source=context.source.sha256, created_at=at, main_only=main_only,
        )
        initial = reduce(None, [created])
        assert initial is not None
        return Decision(
            [created] + affiliation.default_events(initial, context, wi, at),
            {"created": True},
        )
    if state is None:
        raise Rejected("domain_conflict", "Work item does not exist")
    migration.blocked(state, context, intent)
    legacy.blocked(state, intent)
    children.block_ending(state, intent)
    children.block_control(state, intent)
    children.block_paused(state, intent, PAUSED_CHILD_INTENTS)
    quota.blocked(state, intent, data)
    if intent == "revoke_merge_authority":
        # Revoking only narrows authority: the Agent the grant serves may do so
        # from any session, whatever its carrier WI's state or generation.
        return merge_authority.decide(
            state, intent, data, context, operation, wi, _is_holder(state, context), at)
    if intent in migration.INTENTS:
        return migration.decide(state, intent, data, context, operation, wi)
    if intent == "set_main_only":
        # Only the owner relabels a WorkItem between main body and automatic.
        fields(data, {"main_only", "reason"})
        if type(data["main_only"]) is not bool:
            raise Rejected("invalid_input", "Main-body label must be true or false")
        workflow.nonempty(data["reason"])
        source = human_decision(context, intent, data, wi)
        return Decision(
            [event("MainBodyLabelSet", main_only=data["main_only"], reason=data["reason"], source=source)],
            {"main_only": data["main_only"]},
        )
    if intent in affiliation.INTENTS:
        return affiliation.decide(state, data, context, wi, at, intent)
    if intent == "review_existing_execution":
        from .existing_execution import decide as continue_existing
        return continue_existing(state, data, context)
    if intent in {"start_work", "adopt_plan", "assume_control", "reopen_work", "grant_scope"}:
        admission = affiliation.observation(state, context)
        if admission is not None and admission["state"] != "admitted":
            raise Rejected(
                admission["reason"],
                (admission["detail"]["message"] + ". " if "detail" in admission else "")
                + "Review existing business decisions; ask the human only for an undecided affiliation",
            )
    if intent in measurements.INTENTS:
        return measurements.decide(state, intent, data, context, operation, wi, at)
    if intent in notifications.INTENTS:
        return notifications.decide(state, intent, data, context, operation, wi, at)
    if intent == "record_task_result":
        return task_result(state, data, context, operation)
    if intent in quota.INTENTS | quota.WORKER_INTENTS:
        return quota.decide(state, intent, data, context, wi, at)
    if intent in workspaces.WORKER_INTENTS:
        return workspaces.reclaim_terminal(state, data, context, operation)
    if intent in delivery.WORKER_INTENTS:
        return delivery.decide(state, intent, data, context, operation, wi, at)
    if intent in control.INTENTS:
        return control.decide(state, intent, data, context, operation, wi)
    if context.generation != state["generation"]:
        raise Rejected("authority_changed", "Host generation is no longer current")
    if intent == "review_child":
        return children.review(state, data, _is_holder(state, context), context, operation, at)
    if intent in access.INTENTS:
        return access.decide(state, intent, data, context, operation, wi, _is_holder(state, context))
    if intent in merge_authority.INTENTS:
        return merge_authority.decide(
            state, intent, data, context, operation, wi, _is_holder(state, context), at)
    if intent in workspaces.INTENTS:
        return workspaces.decide(state, intent, data, context, operation, wi)
    if intent in publication.INTENTS:
        return publication.decide(state, intent, data, context, operation, wi)
    if intent in collaboration.INTENTS:
        return collaboration.decide(state, intent, data, context, operation, wi)
    if intent in pull_requests.INTENTS:
        return pull_requests.decide(state, intent, data, context, operation, wi, at)
    if intent in review.INTENTS:
        return review.decide(state, intent, data, context, operation, wi)
    if intent in delivery.INTENTS:
        return delivery.decide(state, intent, data, context, operation, wi, at)
    if intent in {"amend_definition", "add_requirement", "rename_work"}:
        fields(
            data,
            {"previous_revision", "reason", "why", "what", "acceptance"}
            if intent == "amend_definition"
            else {"previous_revision", "reason", "what", "acceptance", "material_refs"}
            if intent == "add_requirement"
            else {"reason"},
            {"title", "summary"} if intent == "rename_work" else None,
        )
        if (
            state["status"] not in {"draft", "active"}
            or (state["status"] == "draft" and context.actor != state["definition"]["owner"])
            or (state["status"] == "active" and not _is_holder(state, context))
        ):
            raise Rejected(
                "authority_denied",
                "Definition revision requires the current owner/holder",
            )
        _text(data["reason"])
        source = human_decision(context, intent, data, wi)
        if intent == "rename_work":
            if not {"title", "summary"} & data.keys():
                raise Rejected("invalid_input", "Rename requires a title, a summary or both")
            if "title" in data:
                _text(data["title"])
            if "summary" in data:
                _summary(data["summary"])
            return Decision(
                [
                    event(
                        "WorkRenamed",
                        **{key: data[key] for key in ("title", "summary") if key in data},
                        reason=data["reason"],
                        source=source,
                    )
                ]
            )
        if (
            type(data["previous_revision"]) is not int
            or data["previous_revision"] != state["definition_revision"]
        ):
            raise Rejected("domain_conflict", "Definition revision is no longer current")
        _text(data["what"])
        if not isinstance(data["acceptance"], list) or not data["acceptance"]:
            raise Rejected("invalid_input", "Acceptance criteria are required")
        for criterion in data["acceptance"]:
            _text(criterion)
        if intent == "add_requirement":
            # The original definition stays byte-identical; only the new
            # requirement and its criteria are appended (AH-081).
            if not isinstance(data["material_refs"], list):
                raise Rejected("invalid_input", "Material references must be a list")
            for ref in data["material_refs"]:
                _text(ref)
            current = state["definition"]
            addition = {
                "what": data["what"],
                "material_refs": data["material_refs"],
                "reason": data["reason"],
                "acceptance_from": len(current["acceptance"]),
            }
            definition = {
                **current,
                "acceptance": [*current["acceptance"], *data["acceptance"]],
                "additions": [*current.get("additions", []), addition],
            }
            return Decision(
                [event("RequirementAdded", definition=definition, source=source)],
                {"definition_revision": state["definition_revision"] + 1},
            )
        _text(data["why"])
        # A whole rewrite supersedes earlier additions; they stay in history.
        definition = {
            **{key: value for key, value in state["definition"].items() if key != "additions"},
            **{key: data[key] for key in ("why", "what", "acceptance")},
        }
        return Decision(
            [
                event(
                    "DefinitionRevised",
                    definition=definition,
                    reason=data["reason"],
                    source=source,
                )
            ],
            {"definition_revision": state["definition_revision"] + 1},
        )
    if intent == "complete_lightweight":
        fields(data, *guide.shape(intent))
        if (
            state["profile"] != "lightweight"
            or not _is_holder(state, context)
            or state["status"] != "active"
        ):
            raise Rejected(
                "domain_conflict",
                "Only active lightweight work can use lightweight completion",
            )
        if state.get("parent"):
            raise Rejected(
                "domain_conflict",
                "A child work item completes by its parent holder's review_child "
                "{decision: approved, reason}",
            )
        human_source = human_decision(context, intent, data, wi)
        if collaboration.pending(state):
            raise Rejected("domain_conflict", "Required collaboration has not been settled")
        if delivery.pending(state, None):
            raise Rejected("domain_conflict", "Required delivery has not been observed")
        ids = data["record_ids"]
        refs = data["acceptance_refs"]
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(v, str) for v in ids)
            or not isinstance(refs, list)
            or any(not isinstance(v, str) for v in refs)
        ):
            raise Rejected(
                "invalid_input",
                "Completion must identify work records and accepted criteria",
            )
        records = {r.get("record_id"): r for r in state["records"] if r["kind"] == "WorkRecorded"}
        expected = {f"AC-{i + 1}" for i in range(len(state["definition"]["acceptance"]))}
        bare = sorted(k for k, record in records.items() if not record["record"]["artifacts"])
        if set(ids) != set(records) or not records or set(refs) != expected or bare:
            raise Rejected(
                "domain_conflict",
                "Fixed work records and all criteria are required: record_ids must be exactly "
                + (", ".join(sorted(records)) or "(no WorkRecorded yet; record_work with artifacts first)")
                + "; acceptance_refs must be exactly "
                + ", ".join(sorted(expected, key=lambda r: int(r[3:])))
                + ("; records without artifacts: " + ", ".join(bare) if bare else ""),
            )
        return Decision(
            [
                event(
                    "WorkCompleted",
                    record_ids=ids,
                    acceptance_refs=refs,
                    source_digest=human_source,
                    profile="lightweight",
                )
            ]
        )
    if intent == "promote_workflow":
        fields(data, {"graph", "plan_text", "reason"})
        _text(data["reason"])
        _text(data["plan_text"])
        if (
            state["profile"] != "lightweight"
            or state["status"] != "active"
            or not _is_holder(state, context)
        ):
            raise Rejected(
                "domain_conflict",
                "Only the active lightweight holder may promote its work",
            )
        human_source = human_decision(context, intent, data, wi)
        workflow.validate_graph(data["graph"], state, wi, 1)
        assert context.source is not None
        if data["graph"]["confirmation_ref"] != context.source.reference:
            raise Rejected("source_invalid", "Promotion plan must bind its real confirmation")
        return Decision(
            [
                event("WorkflowPromoted", reason=data["reason"], source=human_source),
                event(
                    "PlanAdopted",
                    graph=data["graph"],
                    plan_text=data["plan_text"],
                    plan_digest=digest(data["plan_text"]),
                    source=human_source,
                    adopted_by=context.actor,
                    adopted_at=at,
                    reuse={},
                ),
            ],
            {"workflow_profile": "standard", "graph_revision": 1},
        )
    if intent == "start_work":
        fields(data, *guide.shape(intent))
        if state["status"] != "draft" or context.actor != state["definition"]["owner"]:
            raise Rejected("authority_denied", "Only the designated owner may start a draft")
        if state["profile"] != "lightweight" and (
            state["graph"] is None
            or type(data.get("graph_revision")) is not int
            or data.get("graph_revision") != state["graph"]["revision"]
        ):
            raise Rejected("domain_conflict", "Standard work requires the current adopted plan")
        return Decision(
            affiliation.default_events(state, context, wi, at)
            + [event("WorkStarted", principal=context.principal())]
        )
    if intent in {"adopt_plan", "revise_plan"}:
        fields(data, *guide.shape(intent))
        _text(data["plan_text"])
        if state["profile"] != "standard" or state["status"] not in {"draft", "active"}:
            raise Rejected("domain_conflict", "Only draft/active standard work may adopt a plan")
        if (state["status"] == "draft" and context.actor != state["definition"]["owner"]) or (
            state["status"] == "active" and not _is_holder(state, context)
        ):
            raise Rejected("authority_denied", "Plan adoption requires the designated owner/holder")
        current = state["graph"]
        if intent == "adopt_plan" and current is not None:
            raise Rejected(
                "domain_conflict",
                "Use an explicit plan revision to replace an adopted plan",
            )
        if intent == "revise_plan":
            _text(data["reason"])
            if (
                current is None
                or type(data["previous_revision"]) is not int
                or data["previous_revision"] != current["revision"]
            ):
                raise Rejected("domain_conflict", "Previous plan revision is no longer current")
        source = human_decision(context, intent, data, wi)
        graph = data["graph"]
        declared_resources = data.get("resources")
        if declared_resources is not None:
            _resources(declared_resources)
            if not state.get("production") or state["resources"] or current is not None:
                raise Rejected(
                    "domain_conflict",
                    "Only an original historical WI without declared scope may bind it with its confirmed first plan",
                )
            state = {**state, "resources": declared_resources}
        workflow.validate_graph(graph, state, wi, 1 if current is None else current["revision"] + 1)
        assert context.source is not None
        if graph["confirmation_ref"] != context.source.reference:
            raise Rejected(
                "source_invalid",
                "Plan confirmation must refer to the admitted human input",
            )
        if current is not None and graph["graph_id"] != current["graph_id"]:
            raise Rejected("domain_conflict", "Plan revision must preserve graph identity")
        reuse = (
            {}
            if current is None
            else workflow.reusable_results(state, graph, data["reuse_results"])
        )
        return Decision(
            (
                [event("HistoricalScopeDeclared", resources=declared_resources, source=source)]
                if declared_resources is not None
                else []
            )
            + [
                event(
                    "PlanAdopted" if current is None else "PlanRevised",
                    graph=graph,
                    plan_text=data["plan_text"],
                    plan_digest=digest(data["plan_text"]),
                    source=source,
                    adopted_by=context.actor,
                    adopted_at=at,
                    reuse=reuse,
                )
            ],
            {"graph_revision": graph["revision"], "reused_nodes": sorted(reuse)},
        )
    if intent == "claim_task":
        fields(data, {"graph_revision", "node_id"})
        node = workflow.graph_node(state, data["graph_revision"], data["node_id"])
        if node["kind"] not in workflow.TASK_KINDS:
            raise Rejected(
                "domain_conflict",
                "This node requires its verification/review/delivery intent",
            )
        if (
            not authorized(state, context, intent, node["resource_scope"])
            or (node["executor_role"] == "producer" and not _is_holder(state, context))
            or (node["executor_role"] == "collaborator" and _is_holder(state, context))
        ):
            raise Rejected(
                "authority_denied",
                "No task execution grant for the declared role and resources",
            )
        if workflow.stages(state)[node["node_id"]] != "ready":
            raise Rejected(
                "domain_conflict",
                "Task dependencies are not ready or the task is already claimed",
            )
        if any(ref not in [a.reference() for a in context.artifacts] for ref in node["input_refs"]):
            raise Rejected(
                "source_invalid",
                "Task inputs require matching captured bytes before execution",
            )
        if any(
            a["status"] == "running"
            and any(overlaps(r, s) for r in node["resource_scope"] for s in a["resources"])
            for a in state["attempts"].values()
        ):
            raise Rejected("domain_conflict", "A live task attempt owns an overlapping resource")
        grant_id = (
            None
            if _is_holder(state, context)
            else next(
                key
                for key, grant in state["grants"].items()
                if grant["active"]
                and grant["principal"] == context.principal()
                and intent in grant["intents"]
                and all(within(r, grant["resources"]) for r in node["resource_scope"])
            )
        )
        attempt = {
            "attempt_id": operation,
            "graph_revision": data["graph_revision"],
            "node_id": node["node_id"],
            "input_digest": workflow.binding(state, node),
            "principal": context.principal(),
            "generation": context.generation,
            "grant_id": grant_id,
            "resources": node["resource_scope"],
            "node": node,
        }
        return Decision(
            [event("TaskClaimed", **attempt)],
            {"attempt_id": operation, "input_digest": attempt["input_digest"]},
        )
    if intent == "abandon_task":
        fields(data, {"attempt_id", "reason"})
        identity(data["attempt_id"])
        _text(data["reason"])
        attempt = state["attempts"].get(data["attempt_id"])
        if attempt is None or attempt["status"] != "running":
            raise Rejected("domain_conflict", "Attempt is not currently running")
        if not _is_holder(state, context) and (
            attempt["principal"] != context.principal()
            or not authorized(state, context, intent, attempt["resources"])
        ):
            raise Rejected(
                "authority_denied",
                "Only its active executor or holder may abandon an attempt",
            )
        return Decision(
            [
                event(
                    "TaskAbandoned",
                    attempt_id=data["attempt_id"],
                    reason=data["reason"],
                )
            ]
        )
    if intent == "accept_transfer":
        fields(data, {"transfer_id"})
        transfer = state["transfer"]
        if (
            state["status"] != "transferring"
            or transfer is None
            or data["transfer_id"] != transfer["id"]
            or not _transfer_successor(transfer, context)
        ):
            raise Rejected("authority_denied", "Caller is not the reserved successor")
        return Decision(
            [
                event(
                    "TransferAccepted",
                    principal=context.principal(),
                    generation=state["generation"] + 1,
                )
            ],
            {"transfer_id": transfer["id"]},
        )
    if intent == narrative.INTENT:
        clean = narrative.validate(data)
        if not authorized(state, context, "checkpoint", state["resources"], same_agent_records=True):
            raise Rejected("authority_denied", "No current execution grant for this scope: " + denial(
                state, context, "checkpoint", state["resources"], wi=wi, same_agent_records=True))
        return Decision(
            [
                event(
                    "NarrativeRecorded",
                    narrative=clean,
                    author=context.actor,
                    recorded_at=at,
                    after_records=len(state["records"]),
                    record_id=operation,
                )
            ],
            {"record_id": operation},
        )
    if intent in WORK_INTENTS:
        fields(data, *guide.shape(intent))
        _text(data["summary"])
        resources = _resources(data["resources"])
        if len(encoded(data)) > 32768:
            raise Rejected("invalid_input", "Use versioned references for large progress inputs")
        if not authorized(state, context, intent, resources, same_agent_records=True):
            raise Rejected("authority_denied", "No current execution grant for this scope: " + denial(
                state, context, intent, resources, wi=wi, same_agent_records=True))
        if intent == "record_work":
            if not isinstance(data["artifacts"], list) or not data["artifacts"]:
                raise Rejected(
                    "invalid_input",
                    "Work results require versioned artifact references",
                )
            for ref in data["artifacts"]:
                workflow.artifact_ref(ref)
                if ref not in [a.reference() for a in context.artifacts]:
                    raise Rejected(
                        "source_invalid",
                        "Work record requires matching captured artifact bytes",
                    )
        kind = "CheckpointRecorded" if intent == "checkpoint" else "WorkRecorded"
        return Decision(
            [
                event(
                    kind,
                    record=data,
                    author=context.actor,
                    author_session=context.session,
                    record_id=operation,
                )
            ],
            {"record_id": operation},
        )
    if not _is_holder(state, context) or state["status"] != "active":
        raise Rejected("authority_denied", "Current execution holder is required")
    if intent == "grant_scope":
        fields(data, {"principal", "resources", "intents"})
        fields(data["principal"], {"actor", "session", "surface"})
        for value in data["principal"].values():
            identity(value)
        resources = _resources(data["resources"])
        if (
            not isinstance(data["intents"], list)
            or not data["intents"]
            or any(not isinstance(v, str) for v in data["intents"])
            or not set(data["intents"]) <= SCOPED_INTENTS
            or data["principal"] == state["holder"]
        ):
            raise Rejected(
                "invalid_input",
                "Grant requires a collaborator and bounded work intents",
            )
        if not authorized(state, context, intent, resources):
            raise Rejected(
                "domain_conflict",
                "Resource scope overlaps or exceeds the current grant",
            )
        if any(
            a["status"] == "running"
            and any(overlaps(r, s) for r in resources for s in a["resources"])
            for a in state["attempts"].values()
        ):
            raise Rejected("domain_conflict", "Cannot delegate resources held by an active task")
        return Decision(
            [
                event(
                    "ScopeGranted",
                    grant_id=operation,
                    principal=data["principal"],
                    resources=resources,
                    intents=data["intents"],
                    generation=state["generation"],
                )
            ],
            {"grant_id": operation},
        )
    if intent == "revoke_scope":
        fields(data, {"grant_id", "reason"})
        identity(data["grant_id"])
        _text(data["reason"])
        if data["grant_id"] not in state["grants"]:
            raise Rejected("domain_conflict", "Unknown scope grant")
        if not state["grants"][data["grant_id"]]["active"]:
            return Decision([], {"grant_id": data["grant_id"], "already_revoked": True})
        return Decision([event("ScopeRevoked", grant_id=data["grant_id"], reason=data["reason"])])
    if intent == self_rollover.INTENT:
        return self_rollover.decide(state, data, context)
    if intent == self_switch.INTENT:
        return self_switch.decide(state, data, context, wi)
    if intent == "prepare_rollover":
        fields(data, {"summary"})
        _text(data["summary"])
        proof = context.session_handover
        # The Host attests the source and, for an Agent's own request, names it.
        asked = (proof or {}).get("agent_request")
        if (
            proof is None
            or {k: v for k, v in proof.items() if k != "agent_request"}
            != {"source": context.principal()}
            or "agent_request" in proof and not isinstance(asked, str)
            or len(encoded(data)) > 32768
        ):
            raise Rejected("authority_denied", "Rollover requires the admitted Host session controller")
        if asked is not None:
            reason = self_rollover.blocker(state, context)
            if reason:
                raise Rejected("domain_conflict", reason)
        return Decision(
            [event("TransferReserved", transfer_id=operation, target=None,
                   source=context.principal(), summary=data["summary"],
                   **({"agent_request": asked} if asked is not None else {}))],
            {"transfer_id": operation},
        )
    if intent == "rollover":
        fields(data, {"target", "summary"}, {"deadline"})
        if "deadline" in data:
            deadline(data["deadline"])
        fields(data["target"], {"actor", "session", "surface"})
        for value in data["target"].values():
            identity(value)
        _text(data["summary"])
        if len(encoded(data)) > 32768 or data["target"] == state["holder"]:
            raise Rejected(
                "invalid_input",
                "A bounded checkpoint and different successor are required",
            )
        if data["target"]["surface"] != context.surface:
            raise Rejected(
                "domain_conflict",
                "Cross-surface transfer requires the migration protocol",
            )
        return Decision(
            [
                event(
                    "TransferReserved",
                    transfer_id=operation,
                    target=data["target"],
                    summary=data["summary"],
                    **({"deadline": data["deadline"]} if "deadline" in data else {}),
                )
            ],
            {"transfer_id": operation},
        )
    raise AssertionError("Exhaustive intent handling failed")


def _transfer_successor(transfer: Json, context: HostContext) -> bool:
    if transfer["target"] is not None:
        return bool(transfer["target"] == context.principal())
    source = transfer.get("source")
    return bool(
        source
        and context.session_handover == {"source": source, "transfer_id": transfer["id"]}
        and source["actor"] == context.actor
        and source["surface"] == context.surface
        and source["session"] != context.session
    )


def task_result(state: Json, data: Json, context: HostContext, operation: str) -> Decision:
    fields(data, {"attempt_id", "outcome", "outputs", "observation_ref"})
    identity(data["attempt_id"])
    _text(data["outcome"])
    identity(data["observation_ref"])
    attempt = state["attempts"].get(data["attempt_id"])
    if attempt is None:
        raise Rejected("domain_conflict", "Unknown task attempt")
    if context.principal() != attempt["principal"] or context.generation != attempt["generation"]:
        raise Rejected("authority_denied", "A result must come from the original attempt executor")
    if context.source is None or context.source.reference != data["observation_ref"]:
        raise Rejected("source_invalid", "Task result requires its original observation bytes")
    node = attempt["node"]
    if data["outcome"] not in node["output_contract"]["outcomes"] or not isinstance(
        data["outputs"], dict
    ):
        raise Rejected(
            "invalid_input",
            "Task outcome or outputs do not satisfy the declared contract",
        )
    expected = set(node["output_contract"]["artifacts"])
    supplied = set(data["outputs"])
    success = data["outcome"] in node["completion_rule"]["success_outcomes"]
    if not supplied <= expected or (success and supplied != expected):
        raise Rejected("invalid_input", "Successful result requires every declared artifact")
    admitted_refs = [a.reference() for a in context.artifacts]
    for ref in data["outputs"].values():
        workflow.artifact_ref(ref)
        if ref not in admitted_refs:
            raise Rejected("source_invalid", "Artifact reference has no matching captured bytes")
    content = {**data, "source_digest": context.source.sha256}
    if attempt["result"] is not None:
        if attempt["result"]["content_digest"] != digest(content):
            raise Rejected("domain_conflict", "Attempt already has a different immutable result")
        return Decision(
            [],
            {
                "result_id": attempt["result"]["result_id"],
                "current": attempt["result"]["current"],
            },
        )
    current = (
        attempt["status"] == "running"
        and state["graph"] is not None
        and attempt["graph_revision"] == state["graph"]["revision"]
        and context.generation == state["generation"]
        and authorized(state, context, "record_task_result", attempt["resources"])
    )
    result = {
        **content,
        "result_id": operation,
        "content_digest": digest(content),
        "current": current,
    }
    return Decision(
        [
            event(
                "TaskResultRecorded",
                attempt_id=data["attempt_id"],
                result=result,
                current=current,
            )
        ],
        {"result_id": operation, "current": current},
    )


def initial_state(definition: Json, profile: str) -> Json:
    return {
        "definition": definition,
        "definition_revision": 1,
        "profile": profile,
        "startup_profile": profile,
        "status": "draft",
        "generation": 1,
        "holder": None,
        "resources": definition["resources"],
        "grants": {},
        "records": [],
        "transfer": None,
        "effects": {},
        "graph": None,
        "plans": [],
        "node_results": {},
        "attempts": {},
        "active_candidate": None,
        "candidates": {},
        "execution_round": 1,
        "terminal": None,
    }


def reduce(state: Json | None, events: list[Json]) -> Json | None:
    """The state after `events`; `state` is not changed."""
    return advance(deepcopy(state), events)


def advance(result: Json | None, events: list[Json]) -> Json | None:
    """Apply `events` to a state the caller owns, in place.

    The returned state may hold the events' payloads, so a caller that applies
    more events later passes events it does not otherwise keep. A replay that
    owns its state applies each decision once instead of copying the whole
    state per decision (`reduce`), whose cost grows quadratically with history.
    """
    for item in events:
        kind, payload = item["kind"], item["payload"]
        if kind == "ProductionHistoryObserved":
            if result is not None:
                raise ValueError("Duplicate production baseline")
            from .production_history import restore

            result = restore(payload)
            continue
        if kind == "WorkItemCreated":
            if result is not None:
                raise ValueError("Duplicate creation")
            result = initial_state(payload["definition"], payload["profile"])
            result["created_at"] = payload.get("created_at")
            result["main_only"] = payload.get("main_only")
            continue
        if result is None:
            raise ValueError("Event precedes creation")
        before = result["holder"]
        if kind in {
            "WorkStarted",
            "WorkStopped",
            "WorkTerminated",
            "HumanControlAssumed",
            "WorkReopened",
            "TransferAccepted",
        }:
            result.pop("historical_progress", None)
            result.pop("historical_execution", None)
        if kind == "ExistingExecutionContinued":
            from .existing_execution import apply as continue_existing
            continue_existing(result, payload)
        elif kind == "HistoricalScopeDeclared":
            result["resources"] = payload["resources"]
            result["definition"] = {**result["definition"], "resources": payload["resources"]}
        elif kind in {"AffiliationRecorded", "AffiliationRevised"}:
            result["affiliation"] = payload["record"]
        elif kind == "WorkStarted":
            result.update(status="active", holder=payload["principal"])
        elif kind == "WorkflowPromoted":
            result["profile"] = "standard"
            result["main_only"] = True
        elif kind == "MainBodyLabelSet":
            result["main_only"] = payload["main_only"]
        elif kind in {"CheckpointRecorded", "WorkRecorded"}:
            result["records"].append({"kind": kind, **payload})
        elif kind == "NarrativeRecorded":
            result["narrative"] = payload  # Only the latest judgement is shown.
        elif kind in {"AccessGranted", "AccessRevoked"}:
            access.apply(result, kind, payload)
        elif kind in {"MergeAuthorityGranted", "MergeAuthorityRevoked"}:
            merge_authority.apply(result, kind, payload)
        elif kind == "ScopeGranted":
            result["grants"][payload["grant_id"]] = {**payload, "active": True}
        elif kind == "ScopeRevoked":
            result["grants"][payload["grant_id"]]["active"] = False
            workflow.invalidate_attempts(result, grant_id=payload["grant_id"])
        elif kind == "TransferReserved":
            workflow.invalidate_attempts(result)
            result.update(
                status="transferring",
                holder=None,
                transfer={
                    "id": payload["transfer_id"],
                    "target": payload["target"],
                    **({"source": payload["source"]} if "source" in payload else {}),
                    "summary": payload["summary"],
                    **({"deadline": payload["deadline"]} if "deadline" in payload else {}),
                },
            )
            for grant in result["grants"].values():
                grant["active"] = False
        elif kind == "TransferAccepted":
            result.update(
                status="active",
                holder=payload["principal"],
                generation=payload["generation"],
                transfer=None,
            )
            result.setdefault("successions", {})[str(payload["generation"])] = payload["principal"]["actor"]
        elif kind == "WorkStopped":
            workflow.invalidate_attempts(result)
            result.update(status="stopped", holder=None)
            for grant in result["grants"].values():
                grant["active"] = False
        elif kind in {"DefinitionRevised", "RequirementAdded"}:
            workflow.invalidate_attempts(result)
            result.update(
                definition=payload["definition"],
                definition_revision=result["definition_revision"] + 1,
                active_candidate=None,
                node_results={},
                control_results={},
                execution_round=result["execution_round"] + 1,
            )
        elif kind == "WorkRenamed":
            if "title" in payload:
                result["title"] = payload["title"]
            if "summary" in payload:
                result["summary"] = payload["summary"]
        elif (
            not migration.apply_event(result, kind, payload)
            and not quota.apply_event(result, kind, payload)
            and not legacy.apply_event(result, kind, payload)
            and not notifications.apply_event(result, kind, payload)
            and not measurements.apply_event(result, kind, payload)
            and not workspaces.apply_event(result, kind, payload)
            and not publication.apply_event(result, kind, payload)
            and not rounds.apply_event(result, kind, payload)
            and not control.apply_event(result, kind, payload)
            and not collaboration.apply_event(result, kind, payload)
            and not delivery.apply_event(result, kind, payload)
            and not workflow.apply_event(result, kind, payload)
            and not review.apply_event(result, kind, payload)
            and not pull_requests.apply_event(result, kind, payload)
            and not children.apply_event(result, kind, payload)
        ):
            raise Rejected("journal_reader_upgrade_required", "Installed reader does not support this event contract")
        if before is not None and result["holder"] != before and before not in result.get(
                "former_holders", []):
            # Every principal that once held this WI (rollover, continuation,
            # takeover): a successor may register its predecessor's head commit.
            result.setdefault("former_holders", []).append(before)
    return result


SUMMARY_LIMIT = 120
SUMMARY_GUIDE = (
    'A WI needs "summary": one plain line (at most 120 characters) telling a person '
    "what this WI does and why, without IDs, dates or process notes."
)


def _summary(value: object) -> None:
    if (not isinstance(value, str) or not value.strip() or "\n" in value.strip()
            or len(value.strip()) > SUMMARY_LIMIT):
        raise Rejected("invalid_input", SUMMARY_GUIDE)


def public_state(state: Json) -> Json:
    # Private Host/session/source inputs never appear in machine/public observations.
    return {
        "definition": state["definition"],
        # The refs complete_lightweight and plans name criteria by, in definition order.
        "acceptance": [
            {"ref": f"AC-{i + 1}", "text": text}
            for i, text in enumerate(state["definition"]["acceptance"])
        ],
        "production_history": state.get("production"),
        "existing_execution": state.get("existing_execution"),
        # An explicit title wins; otherwise the one-line summary, never the full `what`.
        "title": state.get("title")
        or state.get("summary", state["definition"].get("summary"))
        or state["definition"]["what"],
        "summary": state.get("summary", state["definition"].get("summary")),
        "definition_revision": state["definition_revision"],
        "profile": state["profile"],
        "startup_profile": state["startup_profile"],
        "business_progress": state["historical_progress"]
        if "historical_progress" in state
        else review.progress(state)
        if state["status"] == "active" and state["active_candidate"] is not None
        else state["status"],
        "generation": state["generation"],
        "records": [
            {key: value for key, value in record.items() if key != "author_session"}
            for record in state["records"]
        ],
        "narrative": state.get("narrative"),
        "source": "journal",
        "workflow": workflow.public_workflow(state),
        "candidates": review.public_candidates(state),
        "collaborations": collaboration.public_contracts(state),
        "control_history": control.public_history(state),
        "terminal": {
            **state["terminal"],
            "resources_retired": workspaces.settlement(state)["resources_retired"],
        }
        if state["terminal"] is not None and state.get("workspaces")
        else state["terminal"],
        "workspace_settlement": workspaces.settlement(state),
        "access_grants": access.public(state),
        "source_publication": publication.public(state),
        "delivery_obligations": delivery.public_obligations(state),
        "migration": migration.public(state),
        "legacy_import": legacy.public(state),
        "measurements": measurements.public(state),
        "notices": notifications.public(state),
        "disk_quota": quota.public(state),
        "pull_requests": pull_requests.public(state),
        "merge_authority": merge_authority.public(state),
        # The parent's identity (or null); a parent's children as the kernel read
        # them in this transaction: [{id, status, business_progress}].
        "parent": state.get("parent"),
        "children": state.get("children", []),
        "child_reviews": state.get("child_reviews", []),
    }


def observation(state: Json | None, context: HostContext, revision: int) -> Json:
    work = state is not None and any(authorized(state, context, i) for i in SCOPED_INTENTS)
    work_reason = "current_grant" if work else "no_current_execution_grant"
    if (
        not work
        and state is not None
        and state["status"] == "active"
        and _is_holder(state, context)
        and "record_work" in context.allowed_intents
        and rounds.active_blocks_work(state)
    ):
        work_reason = "frozen_candidate_requires_rework"
    elif (
        not work
        and state is not None
        and state["status"] == "active"
        and state["holder"] is not None
        and state["holder"]["actor"] == context.actor
        and not _is_holder(state, context)
    ):
        # The caller's own Agent holds this work from another session (AH-085).
        work_reason = "held_by_same_agent_in_another_session"

    def cap(name: str, value: str, reason: str) -> Json:
        return {
            "name": name,
            "state": value,
            "reason": reason,
            "source_revision": revision,
        }

    capabilities = [
        cap("local_read", "available", "authenticated_journal_read"),
        cap(
            "local_work",
            "available" if work else "unavailable",
            work_reason,
        ),
        cap(
            "external_publish",
            "available" if context.publication_targets else "unavailable",
            "admitted_targets_require_current_approved_source"
            if context.publication_targets
            else "adapter_not_connected",
        ),
        cap(
            "terminal_settlement",
            "available"
            if state is not None and workspaces.settlement(state)["resources_retired"]
            else "available"
            if state is not None
            and state["status"] in {"completed", "terminated"}
            and state.get("workspaces")
            else "unavailable",
            "physical_retirement_observed"
            if state is not None and workspaces.settlement(state)["resources_retired"]
            else "inventory_approval_and_safe_physical_readback_required",
        ),
    ]
    if state is None:
        next_action = None
    else:
        actions = {
            "draft": "adopt_plan"
            if state["profile"] == "standard" and state["graph"] is None
            else "start_work",
            "active": "record_work",
            "transferring": "accept_transfer",
            "migration_sealed": "inspect",
            "migration_prepared": "activate_migration",
            "legacy_prepared": "activate_legacy_draft",
            "stopped": "inspect",
            "terminated": "inspect",
            "completed": "retire_resources",
        }
        next_action = {"intent": actions[state["status"]], "reason": state["status"]}
        if state["status"] in {"completed", "terminated"}:
            inventory = workspaces.public(state)
            next_action = {
                "intent": "inspect"
                if not inventory or all(r["status"] == "retired" for r in inventory)
                else "resume"
                if any(r["retirement"] for r in inventory)
                else "retire_resources",
                "reason": "managed_resources_retired"
                if inventory and all(r["status"] == "retired" for r in inventory)
                else "no_managed_resources_declared"
                if not inventory
                else "physical_retirement_pending",
            }
            # A retirement read back as never having acted (e.g. the worktree was
            # removed by hand) runs again only on its owner's retry_effect.
            absent = [r for r in inventory if r.get("retirement") and r["status"] == "observed_absence"]
            if absent:
                next_action = {
                    "intent": "retry_effect",
                    "reason": "physical_retirement_absent",
                    "effect_id": absent[0]["retirement"],
                }
        if state["status"] == "completed" and delivery.public_obligations(state)["unsettled"]:
            next_action = {
                "intent": "resume",
                "reason": "completed_work_delivery_pending",
            }
        if state["status"] == "active" and state["graph"] is not None:
            progress = workflow.execution_progress(state)
            node_stages = workflow.stages(state)
            intent = (
                "revise_plan"
                if progress == "needs_revision"
                else "submit_candidate"
                if progress == "execution_finished"
                else "claim_task"
                if "ready" in node_stages.values()
                else "record_task_result"
                if "running" in node_stages.values()
                else "wait"
            )
            next_action = {"intent": intent, "reason": progress}
        if state["status"] == "active" and state["active_candidate"] is not None:
            item = state["candidates"][state["active_candidate"]]
            stage = review.progress(state)
            intent = (
                "observe_delivery"
                if stage == "awaiting_delivery"
                else "complete"
                if stage == "approved"
                else "record_review"
                if stage == "awaiting_review"
                else "revise_work"
                if stage == "needs_revision"
                else "assign_verification"
                if item["assignment"] is None
                else "acknowledge_verification"
                if not item["assignment"]["acknowledged"]
                else "record_verification"
            )
            next_action = {
                "intent": intent,
                "reason": stage,
                "candidate_id": state["active_candidate"],
            }
    if state is not None and (next_action or {}).get("intent") in {"record_work", "wait"}:
        # Registered pull requests wait on this Agent's review, revision or merge.
        next_action = pull_requests.next_action(state, context) or next_action
    if state is not None and not _is_holder(state, context):
        # A recipient Agent's sessions see what this WI waits on from them (AH-088).
        owed = collaboration.awaiting(state, context.actor, context.principal())
        if owed:
            next_action = {
                "intent": "respond_collaboration",
                "reason": "collaboration_" + owed[0]["status"],
                "contract_id": owed[0]["contract_id"],
                **(
                    {"main_only": True, "agent_action": collaboration.MAIN_ONLY_ACTION}
                    if owed[0]["main_only"]
                    else {}
                ),
            }
    if state is not None and not (next_action or {}).get("main_only"):
        # The requesting Agent's sessions see answers they still have to settle
        # first; only a main-only contract owed to this Agent comes before them.
        settle = collaboration.awaiting_settlement(state, context.actor)
        if settle:
            next_action = {
                "intent": "settle_collaboration",
                "reason": "collaboration_" + settle[0]["status"],
                "contract_id": settle[0]["contract_id"],
                "options": settle[0]["options"],
                "agent_action": collaboration.SETTLE_ACTION,
            }
    if state is not None and state["status"] in {
        "migration_sealed",
        "migration_prepared",
        "legacy_prepared",
    }:
        for capability in capabilities:
            if capability["name"] != "local_read":
                capability.update(state="unavailable", reason=state["status"])
    if state is not None and state["status"] in {"draft", "active"} and quota.level(state) == "exceeded":
        next_action = {
            "intent": "inspect",
            "reason": "disk_quota_exceeded",
            "agent_action": quota.CLEANUP + ", or ask the human to raise_disk_quota; new work stays refused until re-measured below the quota",
        }
    if context.read_only_reason is not None:
        for capability in capabilities:
            if capability["name"] != "local_read":
                capability.update(state="unavailable", reason=context.read_only_reason)
        next_action = {"intent": "inspect", "reason": context.read_only_reason}
    view = public_state(state) if state is not None else None
    if state is not None and view is not None and view["migration"] is not None:
        view["migration"]["physical_effects_available"] = (
            context.effect_binding is not None
            and context.effect_binding == state["migration"]["effect_binding"]
        )
    if state is not None and view is not None:
        admission = affiliation.observation(state, context)
        if admission is not None:
            view["affiliation"] = admission
            if state["status"] in {"draft", "active"} and admission["state"] != "admitted":
                next_action = {
                    "intent": "resolve_business_affiliation",
                    "reason": admission["reason"],
                    **({"detail": admission["detail"]} if "detail" in admission else {}),
                    "agent_action": "Read existing decisions; ask the human only for an undecided choice; preserve this WI and request",
                    "background_retry": False,
                    "execution_authority": False,
                }
        if state.get("historical_execution") and (
            admission is None or admission["state"] == "admitted"
        ):
            next_action = {
                "intent": "assume_control",
                "reason": "Original execution is preserved as history; the old-track continuation is retired, so a human assumes control to continue",
                "execution_authority": False,
            }
    return {
        "state": view,
        "capabilities": capabilities,
        "next_action": next_action,
        "effects": [
            {**{
                key: value
                for key, value in effect.items()
                if (key != "native_session_id" or "verification_dispatch" in context.effect_kinds)
                and (
                    key != "snapshot"
                    or effect["effect_kind"]
                    not in workspaces.EFFECT_KINDS
                    | publication.EFFECT_KINDS
                    | {notifications.EFFECT_KIND}
                    or effect["effect_kind"] in context.effect_kinds
                )
            }, 'retry': delivery.retry_policy(effect),
               # A resource intention that can never send or resume again.
               **({'obsolete': workspaces.obsolete(state, effect),
                   # The host that holds the workspace runs it (`workspaces.on_host`).
                   'host': workspaces.host_of(state, effect["snapshot"]["resource_id"])}
                  if effect["effect_kind"] in workspaces.EFFECT_KINDS else {}),
               # Whether the kernel would admit a new merge attempt now.
               **({'startable': pull_requests.startable(state, effect)}
                  if effect["effect_kind"] == pull_requests.MERGE_KIND else {})}
            for effect in state["effects"].values()
        ]
        if state
        else [],
    }
