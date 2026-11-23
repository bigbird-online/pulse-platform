"""Bounded waiting observations from original facts, without liveness decisions."""

from __future__ import annotations

from datetime import datetime

from . import collaboration, workspaces, runtime_presence
from .model import Json, Query, RuntimeObservation, digest


def instant(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def age(record: Json | None, now: datetime | None, threshold: int | None) -> Json:
    result: Json = {
        "seconds": None,
        "classification": "unknown",
        "reason": "missing_record_time",
    }
    before = instant(record["at"]) if record else None
    if now is None:
        result["reason"] = "observation_clock_unavailable"
    elif before is not None:
        elapsed = (now - before).total_seconds()
        if elapsed < 0:
            result["reason"] = "observation_clock_before_record"
        else:
            result["seconds"] = elapsed
            result["reason"] = (
                "no_query_threshold"
                if threshold is None
                else "caller_selected_age_threshold"
            )
            if threshold is not None:
                result["classification"] = "stale" if elapsed >= threshold else "recent"
    return result


def overdue(deadline: Json | None, now: datetime | None, record: Json | None) -> Json:
    if deadline is None:
        return {"state": "unknown", "reason": "no_original_deadline_recorded"}
    due = instant(deadline["at"])
    recorded = instant(record["at"]) if record else None
    if now is None:
        return {"state": "unknown", "reason": "observation_clock_unavailable"}
    if recorded is not None and now < recorded:
        return {"state": "unknown", "reason": "observation_clock_before_record"}
    if due is None:
        return {"state": "unknown", "reason": "original_deadline_unavailable"}
    elapsed = (now - due).total_seconds()
    return {
        "state": "overdue" if elapsed >= 0 else "not_due",
        "reason": "original_recorded_deadline",
        "lateness_seconds": max(0, elapsed),
    }


def graph_deadline(graph: Json | None, node_id: str | None) -> Json | None:
    if graph is None or node_id is None:
        return None
    node = next((n for n in graph["nodes"] if n["node_id"] == node_id), None)
    if node is None or "deadline" not in node:
        return None
    return {
        "at": node["deadline"],
        "source": "confirmed_plan",
        "graph_revision": graph["revision"],
        "node_id": node_id,
    }


def summarize(items: list[Json]) -> Json:
    kinds: Json = {}
    overdue_counts = {"overdue": 0, "not_due": 0, "unknown": 0}
    age_counts = {"stale": 0, "recent": 0, "unknown": 0}
    executors: dict[str, str] = {}
    for item in items:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
        overdue_counts[item["overdue"]["state"]] += 1
        age_counts[item["record_age"]["classification"]] += 1
        if item["executor"]:
            executors[item["executor"]["reference"]] = item["executor"]["presence"]
    return {
        "waiting_items": len(items),
        "by_kind": kinds,
        "overdue": overdue_counts,
        "record_age": age_counts,
        "executors": {
            "total": len(executors),
            "presence_unknown": sum(v == "unknown" for v in executors.values()),
            "observed_present": sum(
                v == "observed_present" for v in executors.values()
            ),
        },
    }


def observe(
    state: Json,
    history: list[Json],
    revision: int,
    query: Query,
    now: str,
    runtime_observations: tuple[RuntimeObservation, ...] = (),
) -> Json:
    """No filesystem/process calls and no permission or state transitions."""
    clock = instant(now)
    related: dict[str, Json] = {}
    interactions: dict[str, Json] = {}
    rows = [row for row in history if row["revision"] <= revision]
    for row in rows:
        stamp = {"revision": row["revision"], "at": row["at"]}
        principal = row["semantics"]["principal"]
        interactions[digest(principal)] = stamp
        for event in row["events"]:
            for field in (
                "attempt_id",
                "candidate_id",
                "contract_id",
                "effect_id",
                "transfer_id",
            ):
                key = event["payload"].get(field)
                if isinstance(key, str):
                    related[key] = stamp
    items: list[Json] = []

    def add(
        kind: str,
        key: str,
        status: str,
        *,
        principal: Json | None = None,
        required: bool = False,
        detail: Json | None = None,
        deadline: Json | None = None,
    ) -> None:
        executor: Json | None = None
        if principal is not None:
            opaque = digest(principal)
            last = interactions.get(opaque)
            executor = {
                "reference": "watch-executor." + opaque,
                "last_authenticated_record": last,
                "last_record_age": age(last, clock, query.age_threshold_seconds),
                **runtime_presence.public(principal, runtime_observations, clock),
            }
        last_record = related.get(key)
        if kind == "execution" and executor:
            last_record = executor["last_authenticated_record"]
        items.append(
            {
                "kind": kind,
                "id": key,
                "recorded_status": status,
                "required": required,
                "last_related_record": last_record,
                "record_age": age(last_record, clock, query.age_threshold_seconds),
                "executor": executor,
                "overdue": overdue(deadline, clock, last_record),
                "deadline": deadline,
                "detail": detail or {},
            }
        )

    if state["status"] == "active" and state["holder"] is not None:
        add(
            "execution",
            str(query.work_item_id),
            "current_holder",
            principal=state["holder"],
        )
    for key, attempt in state["attempts"].items():
        if attempt["status"] == "running":
            add(
                "task",
                key,
                "running",
                principal=attempt["principal"],
                deadline=graph_deadline(state["graph"], attempt["node_id"]),
                detail={
                    "node_id": attempt["node_id"],
                    "graph_revision": attempt["graph_revision"],
                },
            )
    for key, contract in state.get("collaborations", {}).items():
        if contract["withdrawn"]:
            continue
        for recipient in contract["recipients"]:
            principal = recipient["principal"]
            response = contract["responses"][principal["actor"]]
            if (
                response["status"] == "completed"
                or contract["kind"] == "responsibility"
                and response["status"] == "accepted"
            ):
                continue
            if not contract["required"] and response["status"] in {
                "rejected",
                "returned",
            }:
                continue
            add(
                "collaboration",
                key,
                response["status"],
                principal=response.get("principal", principal),
                required=contract["required"],
                deadline={
                    "at": contract["deadline"],
                    "source": "original_contract",
                    "contract_id": key,
                }
                if "deadline" in contract
                else None,
                detail={
                    "responsibility": contract["kind"],
                    "owner_attention_needed": response["status"]
                    in {"rejected", "returned"}
                    # Only its requester can end it, by withdrawing.
                    or collaboration.child_transfer(state, contract["kind"]),
                },
            )
    if state["status"] == "active" and state["active_candidate"]:
        key = state["active_candidate"]
        candidate = state["candidates"][key]
        assignment = candidate["assignment"]
        verification = candidate["verification"]
        waiver = candidate["waiver"]
        content = candidate["content"]
        gates = content.get("gates", {})
        if verification is None and waiver is None:
            add(
                "verification",
                key,
                "unassigned"
                if assignment is None
                else "awaiting_result"
                if assignment["acknowledged"]
                else "awaiting_ack",
                principal=assignment["verifier"] if assignment else None,
                deadline=graph_deadline(content["graph"], gates.get("verification")),
                required=True,
                detail={"assignment_id": assignment["id"] if assignment else None},
            )
        if candidate["review"] is None and (
            verification is not None or waiver is not None
        ):
            add(
                "review",
                key,
                "awaiting_human_decision",
                required=True,
                deadline=graph_deadline(content["graph"], gates.get("review")),
            )
        if (
            candidate["review"] is not None
            and candidate["review"]["decision"] == "changes_requested"
        ):
            add(
                "revision",
                key,
                "changes_requested",
                principal=state["holder"],
                required=True,
            )
    for key, effect in state["effects"].items():
        if effect["status"] not in {"observed_success", "cancelled"}:
            candidate = state["candidates"].get(effect["candidate_id"])
            deadline = (
                graph_deadline(candidate["content"]["graph"], effect.get("node_id"))
                if candidate
                else None
            )
            add(
                "effect",
                key,
                effect["status"],
                required=effect["required"],
                deadline=deadline,
                detail={
                    "effect_kind": effect["effect_kind"],
                    "attempt_count": len(effect["attempts"]),
                },
            )
    if state["status"] == "transferring" and state["transfer"]:
        transfer = state["transfer"]
        add(
            "transfer",
            transfer["id"],
            "awaiting_acceptance",
            principal=transfer["target"],
            deadline={
                "at": transfer["deadline"],
                "source": "original_transfer",
                "transfer_id": transfer["id"],
            }
            if "deadline" in transfer
            else None,
            required=True,
        )
    items.sort(
        key=lambda item: (
            item["kind"],
            item["id"],
            item["executor"]["reference"] if item["executor"] else "",
        )
    )
    resources = workspaces.settlement(state)
    settled = all(
        e["status"] in {"observed_success", "cancelled"}
        for e in state["effects"].values()
    )
    terminal = state["status"] in {"completed", "terminated"}
    archive = "not_ready"
    if terminal and settled and not collaboration.pending(state):
        archive = (
            "candidate"
            if resources["resources_retired"]
            else "unknown"
            if not resources["resources"]
            else "not_ready"
        )
    return {
        "schema": "harness.watch/v1",
        "source": "journal",
        "source_revision": revision,
        "generation": state["generation"],
        "as_of": clock.isoformat() if clock else None,
        "age_threshold_seconds": query.age_threshold_seconds,
        "items": items[query.after : query.after + query.limit],
        "total_count": len(items),
        "summary": summarize(items),
        "next_offset": query.after + query.limit
        if len(items) > query.after + query.limit
        else None,
        "archive_scan": {
            "state": archive,
            "basis": "recorded_managed_inventory_and_obligations_only",
            "unretired_resources": resources["unretired_resource_count"],
            "required_originals_retained": True,
            "deletion_authority": False,
        },
        "lifecycle_mutation": False,
    }
