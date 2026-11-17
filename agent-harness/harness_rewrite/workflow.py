"""Pure versioned plan and attempt semantics, internal to the lifecycle domain."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from . import guide
from .model import Json, Rejected, digest, fields, identity, deadline

TASK_KINDS = frozenset({"work", "decision", "wait"})
KINDS = TASK_KINDS | {"verification", "review", "delivery"}
TASK_INTENTS = frozenset({"claim_task", "record_task_result", "abandon_task"})


def nonempty(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Rejected("invalid_input", "Expected nonempty text")
    return value


def resource_paths(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(r, str)
            or not r
            or r.startswith("/")
            or any(p in {"", ".", ".."} for p in r.split("/"))
            for r in value
        )
        or len(value) != len(set(value))
    ):
        raise Rejected(
            "invalid_input", "Resources must be unique logical resource paths"
        )
    return cast(list[str], value)


def overlaps(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def within(resource: str, scope: list[str]) -> bool:
    return any(resource == root or resource.startswith(root + "/") for root in scope)


def artifact_ref(value: Json) -> None:
    fields(value, {"object", "version", "sha256", "access", "size"})
    resource_paths([value["object"]])
    nonempty(value["version"])
    if not isinstance(value["access"], str) or value["access"] not in {
        "private",
        "team",
    }:
        raise Rejected("invalid_input", "Unknown artifact access scope")
    sha = value["sha256"]
    if (
        not isinstance(sha, str)
        or len(sha) != 64
        or any(c not in "0123456789abcdef" for c in sha)
    ):
        raise Rejected("invalid_input", "Invalid artifact digest")
    if type(value["size"]) is not int or value["size"] < 0:
        raise Rejected("invalid_input", "Invalid artifact length")


def validate_graph(graph: Json, state: Json, wi: str, expected_revision: int) -> None:
    fields(graph, *guide.nested("graph"))
    identity(graph["graph_id"])
    identity(graph["confirmation_ref"])
    nonempty(graph["objective"])
    if (
        graph["work_item_id"] != wi
        or type(graph["revision"]) is not int
        or graph["revision"] != expected_revision
        or type(graph["definition_revision"]) is not int
        or graph["definition_revision"] != state["definition_revision"]
    ):
        raise Rejected(
            "domain_conflict", "Plan refers to a different WI, definition or revision"
        )
    refs = graph["acceptance_refs"]
    valid_refs = {f"AC-{i + 1}" for i in range(len(state["definition"]["acceptance"]))}
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(v, str) for v in refs)
        or not set(refs) <= valid_refs
    ):
        raise Rejected(
            "invalid_input", "Plan must reference existing acceptance criteria"
        )
    if (
        not isinstance(graph["nodes"], list)
        or not graph["nodes"]
        or len(graph["nodes"]) > 512
        or not isinstance(graph["edges"], list)
        or len(graph["edges"]) > 4096
    ):
        raise Rejected("invalid_input", "Expected a bounded, nonempty graph")
    seen = set()
    for node in graph["nodes"]:
        fields(node, *guide.nested("node"))
        if "deadline" in node:
            deadline(node["deadline"])
        identity(node["node_id"])
        nonempty(node["purpose"])
        if node["node_id"] in seen:
            raise Rejected("invalid_input", "Duplicate graph node")
        seen.add(node["node_id"])
        if not isinstance(node["kind"], str) or node["kind"] not in KINDS:
            raise Rejected("invalid_input", "Unknown node kind")
        roles = {
            "work": {"producer", "collaborator"},
            "decision": {"producer", "collaborator"},
            "wait": {"producer", "collaborator"},
            "verification": {"verifier"},
            "review": {"human"},
            "delivery": {"delivery"},
        }
        if (
            not isinstance(node["executor_role"], str)
            or node["executor_role"] not in roles[node["kind"]]
        ):
            raise Rejected(
                "invalid_input", "Node role does not match its business responsibility"
            )
        resources = resource_paths(node["resource_scope"])
        if any(not within(r, state["resources"]) for r in resources):
            raise Rejected(
                "authority_denied", "Plan exceeds the declared resource scope"
            )
        if type(node["required"]) is not bool or not isinstance(
            node["input_refs"], list
        ):
            raise Rejected("invalid_input", "Invalid node input/requirement")
        for ref in node["input_refs"]:
            artifact_ref(ref)
        contract = node["output_contract"]
        fields(contract, {"artifacts", "outcomes"})
        for values in contract.values():
            if not isinstance(values, list) or any(
                not isinstance(v, str) for v in values
            ):
                raise Rejected(
                    "invalid_input", "Output contract lists must contain identifiers"
                )
            for value in values:
                identity(value)
            if len(values) != len(set(values)):
                raise Rejected("invalid_input", "Duplicate output contract member")
        if not contract["outcomes"]:
            raise Rejected("invalid_input", "Node must declare observable outcomes")
        rule = node["completion_rule"]
        fields(rule, {"join", "success_outcomes"})
        success = rule["success_outcomes"]
        if (
            rule["join"] not in ("all", "selected")
            or not isinstance(success, list)
            or not success
            or any(not isinstance(v, str) for v in success)
            or not set(success) <= set(contract["outcomes"])
        ):
            raise Rejected("invalid_input", "Invalid completion rule")
    nodes = {n["node_id"]: n for n in graph["nodes"]}
    edge_ids = set()
    for edge in graph["edges"]:
        fields(edge, *guide.nested("edge"))
        identity(edge["from"])
        identity(edge["to"])
        nonempty(edge["condition"])
        if edge["from"] not in seen or edge["to"] not in seen:
            raise Rejected("invalid_input", "Edge references an unknown node")
        if (edge["from"], edge["to"]) in edge_ids:
            raise Rejected(
                "invalid_input", "Duplicate dependency; encode alternatives as outcomes"
            )
        edge_ids.add((edge["from"], edge["to"]))
        if (
            edge["condition"] != "*"
            and edge["condition"]
            not in nodes[edge["from"]]["output_contract"]["outcomes"]
        ):
            raise Rejected(
                "invalid_input", "Dependency outcome is not declared by its source"
            )
    topological(graph)
    for control in graph["nodes"]:
        if "acceptance_refs" in control:
            selected = control["acceptance_refs"]
            if (
                control["kind"] != "verification"
                or not isinstance(selected, list)
                or not selected
                or any(not isinstance(v, str) for v in selected)
                or not set(selected) <= valid_refs
            ):
                raise Rejected(
                    "invalid_input",
                    "Verification scope must reference actual acceptance criteria",
                )
        outcomes = {
            "verification": {"accepted", "partial", "blocked", "rejected", "waived"},
            "review": {"approve", "changes_requested"},
        }.get(control["kind"])
        if (
            outcomes is not None
            and not set(control["output_contract"]["outcomes"]) <= outcomes
        ):
            raise Rejected("invalid_input", "Unknown control outcome")


def topological(graph: Json) -> list[str]:
    incoming: dict[str, set[str]] = {n["node_id"]: set() for n in graph["nodes"]}
    for edge in graph["edges"]:
        incoming[edge["to"]].add(edge["from"])
    result = []
    while incoming:
        ready = sorted(n for n, parents in incoming.items() if not parents)
        if not ready:
            raise Rejected("invalid_input", "Execution plan must be acyclic")
        result.extend(ready)
        for node in ready:
            del incoming[node]
        for parents in incoming.values():
            parents.difference_update(ready)
    return result


def node_result(state: Json, node_id: str) -> Json | None:
    pointer = state["node_results"].get(node_id)
    if pointer is None:
        if state["graph"] is None:
            return None
        node = next(n for n in state["graph"]["nodes"] if n["node_id"] == node_id)
        if node["kind"] in {"verification", "review"}:
            from .rounds import node_value

            return node_value(state, node)
        if node["kind"] == "delivery":
            from .delivery import node_result as delivered

            return delivered(state, node_id)
        return None
    return cast(Json, state["attempts"][pointer]["result"])


def stages(state: Json) -> dict[str, str]:
    graph = state["graph"]
    if graph is None:
        return {}
    nodes = {n["node_id"]: n for n in graph["nodes"]}
    result: dict[str, str] = {}
    for node_id in topological(graph):
        edges = [edge for edge in graph["edges"] if edge["to"] == node_id]
        checks: list[bool | None] = []
        for edge in edges:
            parent = node_result(state, edge["from"])
            if result[edge["from"]] == "skipped":
                checks.append(False)
            elif result[edge["from"]] != "finished" or parent is None:
                checks.append(None)
            else:
                checks.append(
                    edge["condition"] == "*" or parent["outcome"] == edge["condition"]
                )
        if not checks:
            result[node_id] = "ready"
        elif nodes[node_id]["completion_rule"]["join"] == "all":
            result[node_id] = (
                "skipped"
                if False in checks
                else "waiting"
                if None in checks
                else "ready"
            )
        else:
            # Wait for every predecessor to settle, then join every selected branch.
            result[node_id] = (
                "waiting" if None in checks else "ready" if any(checks) else "skipped"
            )
        if result[node_id] == "ready":
            if node_result(state, node_id) is not None:
                result[node_id] = "finished"
            elif any(
                a["node_id"] == node_id
                and a["status"] == "running"
                and a["graph_revision"] == graph["revision"]
                for a in state["attempts"].values()
            ):
                result[node_id] = "running"
    return result


def control_ready(
    state: Json, candidate_id: str, kind: str, captured: list[Json]
) -> None:
    """Validate against the fixed candidate even when a historical receipt arrives."""
    snapshot = deepcopy(state)
    item = snapshot["candidates"][candidate_id]
    snapshot["graph"] = item["content"]["graph"]
    snapshot["active_candidate"] = candidate_id
    snapshot["control_results"] = item["content"].get("control_results", {})
    result_ids = set(item["content"]["result_ids"])
    snapshot["node_results"] = {
        a["node_id"]: key
        for key, a in snapshot["attempts"].items()
        if a["result"] is not None and a["result"]["result_id"] in result_ids
    }
    status = stages(snapshot)
    for node in (snapshot["graph"] or {"nodes": []})["nodes"]:
        gates = item["content"].get("gates")
        if (
            node["kind"] != kind
            or gates is not None
            and gates.get(kind) != node["node_id"]
        ):
            continue
        if status[node["node_id"]] not in {"ready", "finished"}:
            raise Rejected(
                "domain_conflict",
                "Control node dependencies do not permit this receipt",
            )
        if any(ref not in captured for ref in node["input_refs"]):
            raise Rejected(
                "source_invalid", "Control node requires captured input bytes"
            )


def binding(state: Json, node: Json) -> str:
    graph = state["graph"]
    incoming = []
    for edge in sorted(
        (e for e in graph["edges"] if e["to"] == node["node_id"]),
        key=lambda e: e["from"],
    ):
        parent = node_result(state, edge["from"])
        incoming.append(
            {
                "edge": edge,
                "result": None
                if parent is None
                else {"outcome": parent["outcome"], "outputs": parent["outputs"]},
            }
        )
    return digest(
        {
            "definition_revision": graph["definition_revision"],
            "node": node,
            "incoming": incoming,
        }
    )


def graph_node(state: Json, revision: Any, node_id: Any) -> Json:
    identity(node_id)
    graph = state["graph"]
    if graph is None or type(revision) is not int or revision != graph["revision"]:
        raise Rejected("domain_conflict", "Plan revision is no longer current")
    node = next((n for n in graph["nodes"] if n["node_id"] == node_id), None)
    if node is None:
        raise Rejected("invalid_input", "Unknown task node")
    return cast(Json, node)


def reusable_results(state: Json, graph: Json, reuse: Any) -> Json:
    if (
        not isinstance(reuse, list)
        or any(not isinstance(n, str) for n in reuse)
        or len(reuse) != len(set(reuse))
    ):
        raise Rejected("invalid_input", "Explicit unique result reuse list is required")
    candidate = deepcopy(state)
    candidate.update(
        graph=graph, node_results={}, active_candidate=None, control_results={}
    )
    for attempt in candidate["attempts"].values():
        if attempt["status"] == "running":
            attempt["status"] = "invalidated"
    known = {n["node_id"]: n for n in graph["nodes"]}
    if not set(reuse) <= known.keys():
        raise Rejected("invalid_input", "Reuse references an unknown node")
    for node_id in topological(graph):
        if node_id not in reuse:
            continue
        previous_id = state["node_results"].get(node_id)
        old = state["attempts"].get(previous_id)
        if (
            old is None
            or old["result"] is None
            or stages(candidate)[node_id] != "ready"
            or old["input_digest"] != binding(candidate, known[node_id])
            or old["result"]["outcome"]
            not in known[node_id]["completion_rule"]["success_outcomes"]
        ):
            raise Rejected(
                "domain_conflict",
                "Requested result no longer satisfies its input/output contract",
            )
        candidate["node_results"][node_id] = previous_id
    return cast(Json, candidate["node_results"])


def execution_progress(state: Json) -> str:
    if state["graph"] is None:
        return "no_plan"
    status = stages(state)
    required = [
        n for n in state["graph"]["nodes"] if n["required"] and n["kind"] in TASK_KINDS
    ]
    for node in required:
        result = node_result(state, node["node_id"])
        if (
            result is not None
            and result["outcome"] not in node["completion_rule"]["success_outcomes"]
        ):
            return "needs_revision"
    if required and all(
        status[n["node_id"]] in {"finished", "skipped"} for n in required
    ):
        return "execution_finished"
    return "executing"


def public_workflow(state: Json) -> Json:
    return {
        "graph": state["graph"],
        "node_stages": stages(state),
        "control_results": state.get("control_results", {}),
        "adoption": None
        if not state["plans"]
        else {
            k: state["plans"][-1][k]
            for k in ("plan_text", "plan_digest", "adopted_by", "adopted_at")
        },
        "execution_progress": execution_progress(state),
        "attempts": [
            {k: v for k, v in a.items() if k not in {"principal", "source"}}
            for a in state["attempts"].values()
        ],
    }


def invalidate_attempts(state: Json, *, grant_id: str | None = None) -> None:
    for attempt in state["attempts"].values():
        if attempt["status"] == "running" and (
            grant_id is None or attempt["grant_id"] == grant_id
        ):
            attempt["status"] = "invalidated"


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind in {"PlanAdopted", "PlanRevised"}:
        invalidate_attempts(state)
        state["active_candidate"] = None
        state.pop("control_results", None)
        if kind == "PlanRevised":
            state["execution_round"] += 1
        state.update(graph=data["graph"], node_results=data["reuse"])
        state["plans"].append(data)
    elif kind == "TaskClaimed":
        state["attempts"][data["attempt_id"]] = {
            **data,
            "status": "running",
            "result": None,
        }
    elif kind == "TaskResultRecorded":
        attempt = state["attempts"][data["attempt_id"]]
        attempt["result"] = data["result"]
        if data["current"]:
            attempt["status"] = "finished"
            state["node_results"][attempt["node_id"]] = data["attempt_id"]
    elif kind == "TaskAbandoned":
        state["attempts"][data["attempt_id"]]["status"] = "abandoned"
    else:
        return False
    return True
