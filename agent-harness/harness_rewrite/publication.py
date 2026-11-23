"""Frozen Git source, exact publication and separately observed consumer adoption."""

from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any

from .model import Decision, HostContext, Json, Rejected, digest, fields
from .workspaces import ready, retiring

INTENTS = frozenset({"capture_source", "declare_source_publication"})
EFFECT_KINDS = frozenset({"publish_source", "adopt_source"})


def commit(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", value
    ):
        raise Rejected(
            "invalid_input", "Exact Git commit identity required: the full 40-hex SHA (git rev-parse HEAD)"
        )
    return value


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def decide(
    state: Json, intent: str, data: Json, context: HostContext, operation: str, wi: str
) -> Decision:
    from .domain import authorized
    from .review import candidate, current, owner, verified

    if intent == "capture_source":
        fields(data, {"resource_id", "commit"})
        commit(data["commit"])
        resource = state.get("workspaces", {}).get(data["resource_id"])
        if resource is None or not authorized(
            state, context, intent, resource["resources"]
        ):
            raise Rejected("authority_denied", "Current owned Git workspace required")
        if (
            resource.get("superseded_by")
            or retiring(state, resource)
            or not ready(state, resource)
        ):
            raise Rejected("domain_conflict", "Workspace must be physically ready")
        source = context.git_source
        if (
            source is None
            or source["resource_id"] != data["resource_id"]
            or source["commit"] != data["commit"]
        ):
            raise Rejected("source_invalid", "Native Git capture is required")
        if (
            source["repo_id"] != resource["repo_id"]
            or source["base_commit"] != resource["base_commit"]
            or source["artifact"] not in [a.reference() for a in context.artifacts]
        ):
            raise Rejected(
                "source_invalid",
                "Capture must bind original workspace and exact bundle bytes",
            )
        snapshot = {**source, "snapshot_id": "source_" + digest(source)}
        known = state.get("sources", {}).get(snapshot["snapshot_id"])
        return Decision(
            [] if known else [event("SourceCaptured", snapshot=snapshot)],
            {"snapshot": snapshot},
        )

    fields(
        data, {"candidate_id", "snapshot_id", "target_id", "expected_head", "consumers"}
    )
    owner(state, context)
    item = candidate(state, data["candidate_id"])
    if (
        not current(state, data["candidate_id"])
        or not verified(item)
        or item["review"] is None
        or item["review"]["decision"] != "approve"
    ):
        raise Rejected(
            "domain_conflict",
            "Publication requires the current independently verified and approved candidate",
        )
    source = next(
        (
            s
            for s in item["content"].get("sources", [])
            if s["snapshot_id"] == data["snapshot_id"]
        ),
        None,
    )
    if source is None:
        raise Rejected("source_invalid", "Git source is not frozen in this candidate")
    target = next(
        (
            asdict(t)
            for t in context.publication_targets
            if t.target_id == data["target_id"] and t.repo_id == source["repo_id"]
        ),
        None,
    )
    if target is None:
        raise Rejected("authority_denied", "Publication target is not admitted")
    commit(data["expected_head"])
    rows = data["consumers"]
    if not isinstance(rows, list) or not rows or len(rows) > 32:
        raise Rejected(
            "invalid_input", "At least one bounded actual consumer is required"
        )
    consumers = []
    ids = set()
    for row in rows:
        fields(row, {"consumer_id", "expected_head"})
        commit(row["expected_head"])
        binding = next(
            (
                asdict(c)
                for c in context.source_consumers
                if c.consumer_id == row["consumer_id"]
                and c.repo_id == source["repo_id"]
            ),
            None,
        )
        if (
            binding is None
            or binding["remote_url"] != target["remote_url"]
            or "refs/heads/" + binding["branch"] != target["ref"]
        ):
            raise Rejected(
                "authority_denied", "Consumer must bind this admitted source target"
            )
        if row["consumer_id"] in ids:
            raise Rejected("invalid_input", "Duplicate source consumer")
        ids.add(row["consumer_id"])
        consumers.append({"binding": binding, "expected_head": row["expected_head"]})
    # Separate requests cannot race an unresolved write to the same physical ref.
    for effect in state["effects"].values():
        if (
            effect["effect_kind"] in EFFECT_KINDS
            and effect["snapshot"]["target"] == target
            and effect["status"] != "observed_success"
        ):
            raise Rejected(
                "domain_conflict",
                "An original publication to this target remains unsettled",
            )
    snapshot = {
        "source": source,
        "target": target,
        "expected_head": data["expected_head"],
        "consumers": consumers,
    }
    effects = []
    for index, consumer in enumerate([None, *consumers]):
        effects.append(
            event(
                "EffectRequested",
                effect_id=f"{operation}.{index}",
                effect_kind="publish_source" if consumer is None else "adopt_source",
                candidate_id=data["candidate_id"],
                generation=state["generation"],
                node_id=None,
                required=True,
                target=target["target_id"],
                artifact=source["artifact"],
                snapshot={
                    **snapshot,
                    "consumer": consumer,
                    "publication_effect": operation + ".0",
                },
            )
        )
    return Decision(
        effects, {"effect_ids": [e["payload"]["effect_id"] for e in effects]}
    )


def can_begin(state: Json, effect: Json) -> None:
    from .review import verified

    item = state["candidates"].get(effect["candidate_id"])
    if (
        state["status"] not in {"active", "completed"}
        or effect["generation"] != state["generation"]
        or item is None
        or effect["candidate_id"] != state["active_candidate"]
        or not verified(item)
        or item["review"] is None
        or item["review"]["decision"] != "approve"
    ):
        raise Rejected(
            "domain_conflict", "Original approved publication is no longer current"
        )
    if (
        effect["effect_kind"] == "adopt_source"
        and state["effects"][effect["snapshot"]["publication_effect"]]["status"]
        != "observed_success"
    ):
        raise Rejected(
            "domain_conflict", "Actual source publication must precede adoption"
        )


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind != "SourceCaptured":
        return False
    state.setdefault("sources", {})[data["snapshot"]["snapshot_id"]] = data["snapshot"]
    return True


def frozen(state: Json, artifacts: list[Json]) -> list[Json]:
    return [s for s in state.get("sources", {}).values() if s["artifact"] in artifacts]


def safe_retirement(state: Json, resource: Json) -> Json:
    for effect in reversed(list(state["effects"].values())):
        if (
            effect["effect_kind"] != "publish_source"
            or effect["status"] != "observed_success"
        ):
            continue
        source = effect["snapshot"]["source"]
        if source["resource_id"] != resource["resource_id"]:
            continue
        adopted = [
            e
            for e in state["effects"].values()
            if e["effect_kind"] == "adopt_source"
            and e["snapshot"]["publication_effect"] == effect["effect_id"]
        ]
        if adopted and all(e["status"] == "observed_success" for e in adopted):
            return {
                **resource,
                "retire_commit": source["commit"],
                "preserved_source": source,
                "publication_effect": effect["effect_id"],
                "adoption_effects": [e["effect_id"] for e in adopted],
            }
    return resource


def public(state: Json) -> Json:
    return {
        "snapshots": list(state.get("sources", {}).values()),
        "operations": [
            {
                "effect_id": e["effect_id"],
                "kind": e["effect_kind"],
                "candidate_id": e["candidate_id"],
                "snapshot_id": e["snapshot"]["source"]["snapshot_id"],
                "commit": e["snapshot"]["source"]["commit"],
                "target_id": e["snapshot"]["target"]["target_id"],
                "consumer_id": e["snapshot"]["consumer"]["binding"]["consumer_id"]
                if e["snapshot"]["consumer"]
                else None,
                "status": e["status"],
            }
            for e in state["effects"].values()
            if e["effect_kind"] in EFFECT_KINDS
        ],
    }
