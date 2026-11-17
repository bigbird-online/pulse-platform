"""Per-gate fixed candidates and historical graph control results."""

from __future__ import annotations
from copy import deepcopy
from typing import Any, cast
from . import workflow
from .model import Json, HostContext, Rejected, fields


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def select(state: Json) -> Json | None:
    status = workflow.stages(state)
    nodes = {n["node_id"]: n for n in state["graph"]["nodes"]}
    ready = [
        nodes[key]
        for key in workflow.topological(state["graph"])
        if nodes[key]["kind"] in {"verification", "review", "delivery"}
        and status[key] == "ready"
        and (nodes[key]["kind"] != "delivery" or bool(state["node_results"]))
    ]
    gates: Json = {}
    if ready:
        selected = ready[0]
        gates[selected["kind"]] = selected["node_id"]
        if selected["kind"] == "verification":
            # A directly dependent review judges this exact frozen candidate.
            reviews = [
                nodes[e["to"]]
                for e in state["graph"]["edges"]
                if e["from"] == selected["node_id"]
                and nodes[e["to"]]["kind"] == "review"
            ]
            if len(reviews) == 1:
                review = reviews[0]
                other = [
                    e
                    for e in state["graph"]["edges"]
                    if e["to"] == review["node_id"] and e["from"] != selected["node_id"]
                ]
                if all(status[e["from"]] == "finished" for e in other):
                    gates["review"] = review["node_id"]
    if gates and "delivery" not in gates:
        deliveries = [
            n
            for n in nodes.values()
            if n["kind"] == "delivery"
            and any(
                e["to"] == n["node_id"] and e["from"] in gates.values()
                for e in state["graph"]["edges"]
            )
            and all(
                e["from"] in gates.values() or status[e["from"]] == "finished"
                for e in state["graph"]["edges"]
                if e["to"] == n["node_id"]
            )
        ]
        if len(deliveries) == 1:
            gates["delivery"] = deliveries[0]["node_id"]
    finished = workflow.execution_progress(state) == "execution_finished"
    if not gates and not finished:
        return None
    terminal = finished and all(
        n["node_id"] in gates.values()
        or status[n["node_id"]] in {"finished", "skipped"}
        for n in nodes.values()
        if n["required"] and n["kind"] in {"verification", "review"}
    )
    if not gates and not terminal:
        return None
    if terminal and any(a["status"] == "running" for a in state["attempts"].values()):
        return None
    refs = (
        nodes[gates["verification"]].get(
            "acceptance_refs", state["graph"]["acceptance_refs"]
        )
        if "verification" in gates
        else state["graph"]["acceptance_refs"]
    )
    # Final completion always checks every acceptance criterion.
    if terminal:
        refs = [f"AC-{i + 1}" for i in range(len(state["definition"]["acceptance"]))]
    return {
        "gates": gates,
        "terminal": terminal,
        "acceptance_refs": refs,
        "control_results": deepcopy(state.get("control_results", {})),
    }


def active_blocks_work(state: Json) -> bool:
    key = state["active_candidate"]
    return bool(
        key is not None and state["candidates"][key]["content"].get("terminal", True)
    )


def node_value(state: Json, node: Json) -> Json | None:
    active = state["active_candidate"]
    if active is not None:
        item = state["candidates"][active]
        gates = item["content"].get("gates")
        if gates is None or gates.get(node["kind"]) == node["node_id"]:
            receipt = None
            if node["kind"] == "verification":
                if item["waiver"] is not None and item["waiver"]["action"] == "waive":
                    return {"outcome": "waived", "outputs": {}}
                receipt = item["verification"]
            elif node["kind"] == "review":
                receipt = item["review"]
            if receipt is not None:
                return {
                    "outcome": receipt[
                        "verdict" if node["kind"] == "verification" else "decision"
                    ],
                    "outputs": receipt.get("outputs", {}),
                }
    return cast(Json | None, state.get("control_results", {}).get(node["node_id"]))


def outputs(
    state: Json, candidate_id: str, kind: str, data: Json, context: HostContext
) -> None:
    item = state["candidates"][candidate_id]
    key = item["content"].get("gates", {}).get(kind)
    nodes = [n for n in (item["content"]["graph"] or {"nodes": []})["nodes"] if n["node_id"] == key]
    expected = set(nodes[0]["output_contract"]["artifacts"]) if nodes else set()
    values = data.get("outputs", {})
    fields(values, set(), expected)
    if set(values) != expected:
        raise Rejected("invalid_input", "Control result requires every declared output")
    captured = [a.reference() for a in context.artifacts]
    for ref in values.values():
        workflow.artifact_ref(ref)
        if ref not in captured:
            raise Rejected(
                "source_invalid", "Control outputs require actual captured bytes"
            )


def advance(state: Json | None) -> list[Json]:
    if (
        state is None
        or state["status"] != "active"
        or state["active_candidate"] is None
    ):
        return []
    key = state["active_candidate"]
    item = state["candidates"][key]
    content = item["content"]
    if "gates" not in content:
        return []
    nodes = {n["node_id"]: n for n in state["graph"]["nodes"]}
    events = []
    settled = True
    for kind, node_id in content["gates"].items():
        if kind == "delivery":
            from .delivery import node_result

            value = node_result(state, node_id)
        else:
            value = node_value(state, nodes[node_id])
        if (
            value is None
            or value["outcome"]
            not in nodes[node_id]["completion_rule"]["success_outcomes"]
        ):
            settled = False
        if value is not None and state.get("control_results", {}).get(node_id) != {
            **value,
            "candidate_id": key,
        }:
            events.append(
                event(
                    "ControlNodeRecorded",
                    node_id=node_id,
                    value={**value, "candidate_id": key},
                )
            )
    if settled and content["gates"] and not content["terminal"]:
        events.append(event("ControlRoundAdvanced", candidate_id=key))
    return events


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "ControlNodeRecorded":
        state.setdefault("control_results", {})[data["node_id"]] = data["value"]
    elif kind == "ControlRoundAdvanced":
        state["active_candidate"] = None
    else:
        return False
    return True
