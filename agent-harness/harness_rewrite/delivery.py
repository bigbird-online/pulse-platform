"""Durable external intentions and observations; no external I/O in decisions."""

from __future__ import annotations

from typing import Any, cast
from datetime import datetime, timedelta

from .model import (
    Decision,
    HostContext,
    Json,
    Rejected,
    fields,
    human_decision,
    identity,
)
from . import workflow

INTENTS = frozenset(
    {
        "declare_delivery",
        "begin_effect",
        "continue_effect",
        "observe_effect",
        "cancel_effect",
        "retry_effect",
    }
)
# Workers also report per-WI disk usage (quota.WORKER_INTENTS) and reclaim
# terminal WIs' workspaces (workspaces.WORKER_INTENTS).
WORKER_INTENTS = frozenset({
    "begin_effect", "continue_effect", "observe_effect", "observe_disk_usage",
    "retire_terminal_resources",
})


# Reason of an obsolete resource intention settled without any external action.
OBSOLETE = "obsolete_resource_intention"


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def owner(state: Json, context: HostContext) -> None:
    if (
        state["status"] != "active"
        or state["holder"] != context.principal()
        or state["generation"] != context.generation
    ):
        raise Rejected("authority_denied", "Current execution holder is required")


def fixed_artifacts(state: Json, candidate_id: Any) -> list[Json]:
    if state["profile"] == "standard":
        if candidate_id != state["active_candidate"] or candidate_id is None:
            raise Rejected(
                "domain_conflict", "Delivery must bind the current fixed candidate"
            )
        return cast(
            list[Json], state["candidates"][candidate_id]["content"]["artifacts"]
        )
    if candidate_id is not None:
        raise Rejected("invalid_input", "Lightweight work has no standard candidate")
    return [
        ref
        for record in state["records"]
        if record["kind"] == "WorkRecorded"
        for ref in record["record"]["artifacts"]
    ]


def retry_refusal(effect: Json) -> str:
    """retry_effect opens a new budget only for a send proven absent; say what to do instead."""
    status, kind = effect['status'], effect.get('effect_kind')
    head = f"Effect {effect.get('effect_id')} ({kind}) is {status}; retry_effect only restarts an effect read back as observed_absence. "
    if status in {'observed_success', 'cancelled'}:
        return head + 'It is settled; nothing is retried'
    if status == 'observed_failure':
        if kind == 'verification_dispatch':
            return head + ('The holder session resumes the WI (action resume): resume continues '
                           'the original verifier session without a new assignment')
        if kind == 'merge_pull_request':
            return head + ('A failed merge is final: fix and register a new head with '
                           'submit_pull_request, or once its checks pass register the same head '
                           'as a new request (a new request_id; the passed review is reused), '
                           'or ask the human for confirm_pull_request_merge')
        return head + 'A failed attempt settles that effect; read its observation with inspect view=effects'
    return head + ('Read it back first: the holder resumes the WI (action resume) or the effect '
                   'service drains it; retry only after it reads back as observed_absence')


def decide(
    state: Json, intent: str, data: Json, context: HostContext, operation: str, wi: str, at: str
) -> Decision:
    if intent == "retry_effect":
        fields(data, {"effect_id", "reason"})
        workflow.nonempty(data['reason'])
        effect = get(state, data['effect_id'])
        if (context.actor != state['definition']['owner'] or context.generation != state['generation']
                or (state['status'] == 'active' and state['holder'] != context.principal())):
            raise Rejected('authority_denied', 'Current responsible owner must request repair retry')
        if effect['status'] != 'observed_absence':
            raise Rejected('domain_conflict', retry_refusal(effect))
        return Decision([event('EffectRetryRequested', effect_id=data['effect_id'],
                               attempt_offset=len(effect['attempts']), reason=data['reason'], at=at)])
    if intent == "declare_delivery":
        fields(data, {"candidate_id", "deliveries"})
        if state["status"] == "completed":
            if (
                context.actor != state["definition"]["owner"]
                or context.generation != state["generation"]
            ):
                raise Rejected(
                    "authority_denied",
                    "Completed work requires its current responsible owner",
                )
        else:
            owner(state, context)
        known = fixed_artifacts(state, data["candidate_id"])
        rows = data["deliveries"]
        if not isinstance(rows, list) or not rows or len(rows) > 64:
            raise Rejected("invalid_input", "Expected bounded delivery requirements")
        events = []
        targets = set()
        for index, row in enumerate(rows):
            fields(row, {"artifact", "target", "required", "node_id"}, {"output"})
            workflow.artifact_ref(row["artifact"])
            workflow.resource_paths([row["target"]])
            if type(row["required"]) is not bool or row["artifact"] not in known:
                raise Rejected(
                    "source_invalid",
                    "Delivery requires an already captured work artifact",
                )
            if row["node_id"] is not None:
                if state["status"] == "completed":
                    raise Rejected(
                        "domain_conflict",
                        "Terminal delivery cannot rerun a completed graph node",
                    )
                node = workflow.graph_node(
                    state,
                    state["graph"]["revision"] if state["graph"] else None,
                    row["node_id"],
                )
                if (
                    node["kind"] != "delivery"
                    or not row["required"]
                    and node["required"]
                ):
                    raise Rejected(
                        "domain_conflict", "Delivery must honor its graph requirement"
                    )
                declared = node["output_contract"]["artifacts"]
                if (
                    declared
                    and row.get("output") not in declared
                    or not declared
                    and row.get("output") is not None
                ):
                    raise Rejected(
                        "invalid_input",
                        "Delivery output must match the node artifact contract",
                    )
            elif row.get("output") is not None:
                raise Rejected(
                    "invalid_input", "Named delivery output requires its graph node"
                )
            if row["target"] in targets or any(
                e["target"] == row["target"]
                and e["candidate_id"] == data["candidate_id"]
                for e in state["effects"].values()
            ):
                raise Rejected(
                    "domain_conflict",
                    "A delivery target already has an original obligation",
                )
            targets.add(row["target"])
            effect_id = f"{operation}.{index}"
            events.append(
                event(
                    "EffectRequested",
                    effect_id=effect_id,
                    effect_kind="deliver_artifact",
                    candidate_id=data["candidate_id"],
                    generation=state["generation"],
                    **row,
                )
            )
        return Decision(
            events, {"effect_ids": [e["payload"]["effect_id"] for e in events]}
        )
    if intent == "cancel_effect":
        fields(data, {"effect_id", "reason"})
        terminal = state["status"] in {"completed", "terminated"}
        if not terminal:
            owner(state, context)
        elif (
            context.actor != state["definition"]["owner"]
            or context.generation != state["generation"]
        ):
            raise Rejected(
                "authority_denied",
                "Ended work requires its current responsible owner",
            )
        workflow.nonempty(data["reason"])
        human_decision(context, intent, data, wi)
        effect = get(state, data["effect_id"])
        if effect["effect_kind"] == "workspace_retire":
            # A retirement keeps its original effect; cancelling it would leave
            # the resource permanently unretirable.
            raise Rejected(
                "domain_conflict",
                "An original retirement settles only by physical readback",
            )
        if effect["required"] or effect["status"] not in {
            "requested",
            "observed_absence",
        }:
            raise Rejected(
                "domain_conflict",
                "Cannot cancel required or possibly executed delivery",
            )
        from . import workspaces

        return Decision(
            [
                event(
                    "EffectCancelled",
                    effect_id=data["effect_id"],
                    reason=data["reason"],
                ),
                # Without attempts no writer can still act; otherwise the
                # resource keeps naming this effect.
                *([] if effect["attempts"] else workspaces.withdrawal(state, effect)),
            ]
        )
    fields(
        data,
        {"effect_id"}
        if intent in {"begin_effect", "continue_effect"}
        else {"effect_id", "attempt_id", "outcome", "evidence", "details"},
    )
    effect = get(state, data["effect_id"])
    from .migration import effect_admitted

    effect_admitted(state, data["effect_id"], context)
    if effect["effect_kind"] not in context.effect_kinds:
        raise Rejected(
            "authority_denied",
            "Only an admitted effect adapter may send or observe this action",
        )
    if intent == "continue_effect":
        from . import workspaces, publication, notifications

        if (
            effect["effect_kind"]
            not in workspaces.EFFECT_KINDS
            | publication.EFFECT_KINDS
            | {notifications.EFFECT_KIND}
            or not effect["attempts"]
            or effect["status"]
            in {"requested", "observed_absence", "cancelled", "observed_success"}
            # A failed retirement is settled: never read back into a new round.
            or effect["status"] == "observed_failure" and effect["effect_kind"] == "workspace_retire"
            or effect["attempts"][-1]["executor"] != context.actor
        ):
            raise Rejected(
                "authority_denied", "Original resumable resource attempt required"
            )
        if effect["effect_kind"] == notifications.EFFECT_KIND:
            if not notifications.can_begin(state, effect, context):
                raise Rejected(
                    "authority_changed",
                    "Notice resolved or authority changed before send",
                )
        elif effect["effect_kind"] in publication.EFFECT_KINDS:
            publication.can_begin(state, effect)
        elif workspaces.obsolete(state, effect):
            # The original attempt may have acted: the worker reads it back
            # without resuming (observe_effect), and an absence settles it.
            raise Rejected(OBSOLETE, "Obsolete resource intention is read back, never resumed")
        return Decision([], {"attempt_id": effect["attempts"][-1]["id"]})
    if intent == "begin_effect":
        if effect["status"] not in {"requested", "observed_absence"}:
            raise Rejected(
                "domain_conflict",
                "Original uncertain attempt must be read back before another send",
            )
        from . import workspaces

        if workspaces.settles_obsolete(state, effect):
            if effect["attempts"]:
                # An earlier attempt's writer may still act: only the fenced
                # probe (observe_effect) may settle it.
                raise Rejected(OBSOLETE, "Obsolete resource intention is read back, never resumed")
            # Never begun: no writer exists that could act on it.
            return Decision(
                [event("EffectCancelled", effect_id=data["effect_id"], reason=OBSOLETE),
                 *workspaces.withdrawal(state, effect)],
                {"suppressed": True},
            )
        retry = retry_policy(effect)
        if retry['attempts_remaining'] == 0:
            raise Rejected('effect_retry_exhausted', 'Repair the original delivery, then request retry_effect with its reason')
        if retry['not_before'] and datetime.fromisoformat(at) < datetime.fromisoformat(retry['not_before']):
            raise Rejected('effect_retry_backoff', 'Retry the original effect after ' + retry['not_before'])
        from . import workspaces, publication

        from . import notifications

        notice_effect = effect["effect_kind"] == notifications.EFFECT_KIND
        if notice_effect and not notifications.can_begin(state, effect, context):
            return Decision(
                [
                    event(
                        "EffectCancelled",
                        effect_id=data["effect_id"],
                        reason="notice_resolved_or_stale",
                    )
                ],
                {"suppressed": True},
            )
        from . import pull_requests

        resource_effect = effect["effect_kind"] in workspaces.EFFECT_KINDS
        source_effect = effect["effect_kind"] in publication.EFFECT_KINDS
        merge_effect = effect["effect_kind"] in {
            pull_requests.MERGE_KIND, pull_requests.DISPATCH_KIND
        }
        if merge_effect:
            pull_requests.can_begin(state, effect)
        if resource_effect:
            workspaces.can_begin(state, effect)
        if source_effect:
            publication.can_begin(state, effect)
        terminal_delivery = (
            state["status"] == "completed"
            and effect["effect_kind"] == "deliver_artifact"
            and effect["node_id"] is None
        )
        if (
            not resource_effect
            and not source_effect
            and not notice_effect
            and not merge_effect
            and (
                state["status"] != "active"
                and not terminal_delivery
                or effect["candidate_id"] != state["active_candidate"]
                or effect["generation"] != state["generation"]
            )
        ):
            raise Rejected(
                "domain_conflict", "Obsolete/terminal work cannot send a new effect"
            )
        if (
            state["profile"] == "standard"
            and effect["effect_kind"] == "deliver_artifact"
        ):
            from .review import verified

            item = state["candidates"][effect["candidate_id"]]
            if (
                not verified(item)
                or item["review"] is None
                or item["review"]["decision"] != "approve"
            ):
                raise Rejected(
                    "domain_conflict", "Delivery cannot precede human approval"
                )
        if (
            effect["node_id"] is not None
            and workflow.stages(state)[effect["node_id"]] != "ready"
        ):
            raise Rejected(
                "domain_conflict", "Delivery node dependencies are not ready"
            )
        return Decision(
            [
                event(
                    "EffectAttemptStarted",
                    effect_id=data["effect_id"],
                    attempt_id=operation,
                    executor=context.actor,
                    at=at,
                )
            ],
            {"attempt_id": operation},
        )
    if intent != "observe_effect":
        raise AssertionError("Unknown effect intent")
    if (
        not effect["attempts"]
        or effect["attempts"][-1]["id"] != data["attempt_id"]
        or effect["attempts"][-1]["executor"] != context.actor
    ):
        raise Rejected(
            "authority_denied", "Observation must bind the original adapter attempt"
        )
    if data["outcome"] not in (
        "success",
        "absence",
        "failure",
        "unknown",
    ) or not isinstance(data["details"], dict):
        raise Rejected("invalid_input", "Invalid external observation")
    if not isinstance(data["evidence"], list) or not data["evidence"]:
        raise Rejected(
            "source_invalid", "External observations require captured original evidence"
        )
    refs = [a.reference() for a in context.artifacts]
    for ref in data["evidence"]:
        workflow.artifact_ref(ref)
        if ref not in refs:
            raise Rejected(
                "source_invalid", "Observation evidence bytes are unavailable"
            )
    from . import notifications

    if effect["effect_kind"] == notifications.EFFECT_KIND:
        notifications.validate_observation(effect, data["outcome"], data["details"])
    from . import workspaces

    if effect["status"] == "cancelled" and effect["effect_kind"] in workspaces.EFFECT_KINDS:
        # A late result never revives a settled intention; it is kept as
        # evidence and, unless absent, marks the effect for human attention.
        return Decision([event("EffectLateObserved", effect_id=data["effect_id"],
                               observation={**data, "id": operation})],
                        {"effect_id": data["effect_id"], "status": "cancelled"})
    if effect["status"] == "observed_failure" and effect["effect_kind"] == "workspace_retire":
        # A failed retirement is settled: a late readback is kept as evidence only.
        return Decision([event("EffectLateObserved", effect_id=data["effect_id"],
                               observation={**data, "id": operation})],
                        {"effect_id": data["effect_id"], "status": "observed_failure"})
    if effect["status"] == "observed_success":
        if data["outcome"] != "success":
            raise Rejected(
                "domain_conflict",
                "Observed success cannot be erased by later ambiguity",
            )
        return Decision(
            [], {"effect_id": data["effect_id"], "status": "observed_success"}
        )
    observed = event(
        "EffectObserved",
        effect_id=data["effect_id"],
        observation={**data, "id": operation},
    )
    if data["outcome"] == "absence" and workspaces.settles_obsolete(state, effect):
        # Readback proved the obsolete attempt did nothing; it can never resume.
        return Decision([observed, event("EffectCancelled", effect_id=data["effect_id"],
                                         reason=OBSOLETE), *workspaces.withdrawal(state, effect)])
    return Decision([observed])


def get(state: Json, value: Any) -> Json:
    identity(value)
    if value not in state["effects"]:
        raise Rejected("domain_conflict", "Unknown original effect")
    return cast(Json, state["effects"][value])


def pending(state: Json, candidate_id: str | None) -> bool:
    return any(
        e["required"]
        and e["candidate_id"] == candidate_id
        and e["status"] != "observed_success"
        for e in state["effects"].values()
    )


def public_obligations(state: Json) -> Json:
    effects = [
        e
        for e in state["effects"].values()
        if e["effect_kind"] == "deliver_artifact"
        and e["candidate_id"] == state["active_candidate"]
    ]
    return {
        "source": "journal",
        "required_pending": sum(
            e["required"] and e["status"] != "observed_success" for e in effects
        ),
        "unsettled": sum(
            e["status"] not in {"observed_success", "cancelled"} for e in effects
        ),
        "effect_ids": [e["effect_id"] for e in effects],
    }


def node_result(state: Json, node_id: str) -> Json | None:
    effects = [
        e
        for e in state["effects"].values()
        if e["node_id"] == node_id and e["candidate_id"] == state["active_candidate"]
    ]
    if not effects:
        return cast(Json | None, state.get("control_results", {}).get(node_id))
    if any(e["status"] != "observed_success" for e in effects):
        return None
    outputs = {e.get("output", e["target"]): e["artifact"] for e in effects}
    node = next(n for n in state["graph"]["nodes"] if n["node_id"] == node_id)
    declared = node["output_contract"]["artifacts"]
    if declared and (set(outputs) != set(declared) or len(outputs) != len(effects)):
        return None
    return {"outcome": "success", "outputs": outputs}


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "EffectRequested":
        state["effects"][data["effect_id"]] = {
            **data,
            "status": "requested",
            "attempts": [],
            "observations": [],
        }
    elif kind == "EffectAttemptStarted":
        item = state["effects"][data["effect_id"]]
        item["status"] = "attempt_started"
        item["attempts"].append(
            {"id": data["attempt_id"], "executor": data["executor"],
             **({'at': data['at']} if 'at' in data else {})}
        )
    elif kind == "EffectObserved":
        item = state["effects"][data["effect_id"]]
        item["status"] = (
            "unknown"
            if data["observation"]["outcome"] == "unknown"
            else "observed_" + data["observation"]["outcome"]
        )
        item["observations"].append(data["observation"])
    elif kind == "EffectRetryRequested":
        state['effects'][data['effect_id']]['retry_request'] = dict(data)
    elif kind == "EffectCancelled":
        state["effects"][data["effect_id"]]["status"] = "cancelled"
    elif kind == "EffectLateObserved":
        item = state["effects"][data["effect_id"]]
        item.setdefault("late_observations", []).append(data["observation"])
        if data["observation"]["outcome"] != "absence" and item["status"] == "cancelled":
            item["attention"] = "late_result_after_cancel"
    else:
        return False
    return True


def retry_policy(effect: Json) -> Json:
    """Derived scheduling only; neither disappearance nor retry implies success."""
    offset = effect.get('retry_request', {}).get('attempt_offset', 0)
    count = max(0, len(effect['attempts']) - offset)
    last = effect['attempts'][-1].get('at') if effect['attempts'] else None
    not_before = None
    if count >= 2 and last is not None:
        not_before = (datetime.fromisoformat(last) + timedelta(seconds=min(300, 5 * 2 ** min(count-2, 6)))).isoformat()
    return {'attempts_remaining': max(0, 8-count), 'not_before': not_before,
            'next_action': 'repair_then_retry_effect' if count >= 8 else 'readback_original_then_resume'}
