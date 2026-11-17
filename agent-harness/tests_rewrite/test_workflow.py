"""A-06/A-07 and the task-specific A-08/A-15 assertions through the public kernel."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3

import pytest

from harness_rewrite import Command, HostContext, Kernel, Query, SourceInput
from harness_rewrite.model import ArtifactInput, digest
from harness_rewrite.review import INTENTS as REVIEW_INTENTS

ALL = REVIEW_INTENTS | frozenset(
    {
        "create_draft",
        "create_lightweight",
        "start_work",
        "adopt_plan",
        "revise_plan",
        "claim_task",
        "record_task_result",
        "abandon_task",
        "checkpoint",
        "record_work",
        "grant_scope",
        "revoke_scope",
        "rollover",
        "accept_transfer",
        "stop",
        "complete_lightweight",
        "promote_workflow",
        "record_project_narrative",
    }
)
BASE = HostContext(
    "team", "producer", "private-producer-session", "local", 1, frozenset({"*"}), ALL
)
OTHER = replace(BASE, actor="helper", session="private-helper-session")
DEFINITION = {
    "summary": "Fixture work for tests.",
    "why": "Keep the same collaboration facts",
    "what": "Execute a confirmed plan",
    "acceptance": ["A fixed observable artifact"],
    "owner": "producer",
    "resources": ["repo"],
}


class Rig:
    def __init__(self, tmp_path):
        self.contexts = {
            "owner": BASE,
            "helper": OTHER,
            "create": replace(
                BASE,
                generation=0,
                source=SourceInput("initial", "native_agent", b"Create draft"),
            ),
        }
        self.kernel = Kernel(
            tmp_path / "journal.sqlite",
            self.contexts.__getitem__,
            clock=lambda: "2026-10-07T00:00:00Z",
        )
        result = self.kernel.execute(
            Command("create", "create_draft", DEFINITION), "create"
        )
        self.wi = result["work_item"]["id"]
        self.counter = 0

    def send(self, intent, data, *, handle="owner", request=None):
        self.counter += 1
        return self.kernel.execute(
            Command(request or f"r-{self.counter}", intent, data, self.wi), handle
        )

    def approve(self, intent, data, *, request=None, actor=BASE):
        ref = data.get("graph", {}).get("confirmation_ref", "human-confirmation")
        self.contexts["approval"] = replace(
            actor,
            source=SourceInput(
                ref,
                "native_human",
                b"Approve this exact plan",
                intent,
                digest({"intent": intent, "work_item_id": self.wi, "input": data}),
            ),
        )
        return self.send(intent, data, handle="approval", request=request)

    def adopt(self, graph, *, text="Original agreed plan"):
        result = self.approve("adopt_plan", {"graph": graph, "plan_text": text})
        assert result["status"] == "local_committed", result
        started = self.send("start_work", {"graph_revision": 1})
        assert started["status"] == "local_committed", started

    def claim(self, node, *, revision=1, handle="owner", request=None):
        return self.send(
            "claim_task",
            {"graph_revision": revision, "node_id": node},
            handle=handle,
            request=request,
        )

    def result(
        self,
        attempt_id,
        outcome="success",
        *,
        artifacts=None,
        handle="owner",
        request=None,
        observation="observed",
    ):
        artifacts = artifacts or {}
        context = self.contexts[handle]
        result_handle = f"result-{handle}"
        self.contexts[result_handle] = replace(
            context,
            source=SourceInput(
                observation, "native_agent", b"Executed and read back artifacts"
            ),
            artifacts=tuple(artifacts.values()),
        )
        return self.send(
            "record_task_result",
            {
                "attempt_id": attempt_id,
                "outcome": outcome,
                "outputs": {k: v.reference() for k, v in artifacts.items()},
                "observation_ref": observation,
            },
            handle=result_handle,
            request=request,
        )

    def state(self, *, query=None, handle="owner"):
        return self.kernel.inspect(query or Query(self.wi), handle)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def node(
    name,
    *,
    kind="work",
    role="producer",
    outcomes=None,
    success=None,
    outputs=None,
    join="all",
    resource=None,
):
    return {
        "node_id": name,
        "kind": kind,
        "purpose": f"Perform {name}",
        "executor_role": role,
        "input_refs": [],
        "output_contract": {
            "artifacts": outputs or [],
            "outcomes": outcomes or ["success", "failure"],
        },
        "resource_scope": [resource or f"repo/{name}"],
        "required": True,
        "completion_rule": {"join": join, "success_outcomes": success or ["success"]},
    }


def graph(rig, nodes=None, edges=None, *, revision=1):
    return {
        "work_item_id": rig.wi,
        "graph_id": "graph",
        "revision": revision,
        "definition_revision": 1,
        "objective": "Complete the declared collaboration",
        "acceptance_refs": ["AC-1"],
        "confirmation_ref": f"approval-{revision}",
        "nodes": nodes or [node("work")],
        "edges": edges or [],
    }


def edge(a, b, condition="success"):
    return {"from": a, "to": b, "condition": condition}


def original(response):
    assert response["status"] == "local_committed", response
    return response["result"]["original_request_result"]


def workflow(response):
    return response["result"]["current_observation"]["workflow"]


def error(response):
    return response["error"]["code"] if response["error"] else None


def test_plan_confirmation_and_original_text_are_fixed(rig):
    proposed = graph(rig)
    data = {"graph": proposed, "plan_text": "Agreed original text"}
    assert error(rig.send("adopt_plan", data)) == "source_invalid"
    assert error(rig.send("start_work", {"graph_revision": 1})) == "domain_conflict"
    first = rig.approve("adopt_plan", data, request="adopt")
    assert original(first)["graph_revision"] == 1
    assert original(rig.approve("adopt_plan", data, request="adopt")) == original(first)
    proposed["objective"] = "Changed mutable draft"
    assert workflow(rig.state())["graph"]["objective"] != proposed["objective"]
    adoption = workflow(rig.state())["adoption"]
    assert adoption["plan_text"] == "Agreed original text"
    assert adoption["adopted_at"] == "2026-10-07T00:00:00Z"
    before = rig.kernel._database.read_bytes()
    assert (
        rig.state(query=Query(rig.wi, view="plan", compare_plan_text="changed"))[
            "result"
        ]["plan_matches"]
        is False
    )
    assert (
        rig.state(
            query=Query(rig.wi, view="plan", compare_plan_text="Agreed original text")
        )["result"]["plan_matches"]
        is True
    )
    assert rig.kernel._database.read_bytes() == before
    assert error(rig.approve("adopt_plan", data)) == "domain_conflict"


@pytest.mark.parametrize(
    "case",
    [
        "cycle",
        "unknown",
        "duplicate",
        "condition",
        "role",
        "scope",
        "definition",
        "confirmation",
    ],
)
def test_invalid_plan_cannot_be_adopted(rig, case):
    g = graph(rig, [node("a"), node("b")], [edge("a", "b")])
    if case == "cycle":
        g["edges"].append(edge("b", "a"))
    elif case == "unknown":
        g["edges"].append(edge("missing", "a"))
    elif case == "duplicate":
        g["nodes"].append(deepcopy(g["nodes"][0]))
    elif case == "condition":
        g["edges"][0]["condition"] = "run arbitrary shell"
    elif case == "role":
        g["nodes"][0]["executor_role"] = "human"
    elif case == "scope":
        g["nodes"][0]["resource_scope"] = ["other-repo"]
    elif case == "definition":
        g["definition_revision"] = 2
    else:
        g["confirmation_ref"] = "forged"
    data = {"graph": g, "plan_text": "Plan"}
    if case == "confirmation":
        rig.contexts["approval"] = replace(
            BASE,
            source=SourceInput(
                "real",
                "native_human",
                b"approval",
                "adopt_plan",
                digest({"intent": "adopt_plan", "work_item_id": rig.wi, "input": data}),
            ),
        )
        response = rig.send("adopt_plan", data, handle="approval")
    else:
        response = rig.approve("adopt_plan", data)
    assert response["status"] == "rejected", response
    assert workflow(rig.state())["graph"] is None


def test_conditions_selected_join_and_unrelated_waiting(rig):
    nodes = [
        node(
            "choice",
            kind="decision",
            outcomes=["left", "right"],
            success=["left", "right"],
        ),
        node("left"),
        node("right"),
        node("join", join="selected"),
        node("external", kind="wait"),
    ]
    edges = [
        edge("choice", "left", "left"),
        edge("choice", "right", "right"),
        edge("left", "join"),
        edge("right", "join"),
    ]
    rig.adopt(graph(rig, nodes, edges))
    assert error(rig.claim("join")) == "domain_conflict"
    choose = original(rig.claim("choice"))["attempt_id"]
    external = original(rig.claim("external"))["attempt_id"]
    original(rig.result(choose, "left"))
    stage = workflow(rig.state())["node_stages"]
    assert stage["right"] == "skipped" and stage["join"] == "waiting"
    assert error(rig.claim("right")) == "domain_conflict"
    left = original(rig.claim("left"))["attempt_id"]
    original(rig.result(left))
    assert workflow(rig.state())["node_stages"]["join"] == "ready"
    joined = original(rig.claim("join"))["attempt_id"]
    original(rig.result(joined))
    assert workflow(rig.state())["execution_progress"] == "executing"
    original(rig.result(external))
    assert workflow(rig.state())["execution_progress"] == "execution_finished"


def test_all_join_waits_for_every_parallel_input(rig):
    rig.adopt(
        graph(
            rig,
            [node("a"), node("b"), node("join")],
            [edge("a", "join"), edge("b", "join")],
        )
    )
    a, b = (original(rig.claim(n))["attempt_id"] for n in ["a", "b"])
    original(rig.result(a))
    assert error(rig.claim("join")) == "domain_conflict"
    original(rig.result(b))
    assert original(rig.claim("join"))["attempt_id"]


def test_parallel_task_and_resource_competition_are_transactional(rig):
    rig.adopt(
        graph(
            rig,
            [
                node("a", resource="repo/shared"),
                node("b", resource="repo/shared"),
                node("free"),
            ],
        )
    )
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda n: rig.claim(n, request=f"claim-{n}"), ["a", "b"])
        )
    assert sorted(r["status"] for r in results) == ["local_committed", "rejected"]
    original(rig.claim("free"))
    winner = next(r for r in results if r["status"] == "local_committed")
    original(rig.result(original(winner)["attempt_id"]))
    loser = "b" if results[0]["status"] == "local_committed" else "a"
    original(rig.claim(loser))


def test_same_task_concurrent_claims_and_repeated_requests(rig):
    rig.adopt(graph(rig))
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda n: rig.claim("work", request=n), ["claim-one", "claim-two"])
        )
    assert sorted(r["status"] for r in results) == ["local_committed", "rejected"]
    winner = next(r for r in results if r["status"] == "local_committed")
    retry = rig.claim("work", request=winner["request_id"])
    assert original(retry) == original(winner)
    result = rig.result(original(winner)["attempt_id"], request="result")
    assert original(
        rig.result(original(winner)["attempt_id"], request="result")
    ) == original(result)
    assert (
        error(rig.result(original(winner)["attempt_id"], outcome="failure"))
        == "domain_conflict"
    )


def test_artifact_is_captured_and_missing_or_changed_bytes_rejected(rig):
    rig.adopt(graph(rig, [node("work", outputs=["deliverable"])]))
    attempt = original(rig.claim("work"))["attempt_id"]
    assert error(rig.result(attempt)) == "invalid_input"
    captured = ArtifactInput("repo/result.txt", "commit-1", b"immutable artifact")
    context = replace(
        BASE,
        source=SourceInput("observed", "native_agent", b"readback"),
        artifacts=(captured,),
    )
    rig.contexts["bytes"] = context
    data = {
        "attempt_id": attempt,
        "outcome": "success",
        "outputs": {"deliverable": captured.reference()},
        "observation_ref": "observed",
    }
    forged = deepcopy(data)
    forged["outputs"]["deliverable"]["size"] += 1
    assert (
        error(rig.send("record_task_result", forged, handle="bytes"))
        == "source_invalid"
    )
    original(rig.send("record_task_result", data, handle="bytes"))
    with sqlite3.connect(rig.kernel._database) as db:
        body = db.execute(
            "SELECT content FROM originals WHERE digest=?",
            (captured.reference()["sha256"],),
        ).fetchone()[0]
    assert body == captured.content


def test_abandoned_attempt_result_is_history_not_new_attempt_success(rig):
    rig.adopt(graph(rig))
    old = original(rig.claim("work"))["attempt_id"]
    original(
        rig.send("abandon_task", {"attempt_id": old, "reason": "executor interrupted"})
    )
    current = original(rig.claim("work"))["attempt_id"]
    assert original(rig.result(old))["current"] is False
    assert workflow(rig.state())["node_stages"]["work"] == "running"
    assert original(rig.result(current))["current"] is True
    assert len(workflow(rig.state())["attempts"]) == 2


def test_revision_reuses_only_explicit_contract_compatible_results(rig):
    g = graph(rig, [node("a"), node("b")], [edge("a", "b")])
    rig.adopt(g)
    a = original(rig.claim("a"))["attempt_id"]
    original(rig.result(a))
    old_b = original(rig.claim("b"))["attempt_id"]
    revised = deepcopy(g)
    revised.update(revision=2, confirmation_ref="approval-2")
    revised["nodes"][1]["purpose"] = "Different work on B"
    data = {
        "graph": revised,
        "plan_text": "Revised plan",
        "previous_revision": 1,
        "reuse_results": ["a"],
        "reason": "Correct B",
    }
    original(rig.approve("revise_plan", data))
    assert workflow(rig.state())["node_stages"] == {"a": "finished", "b": "ready"}
    assert original(rig.result(old_b))["current"] is False
    new_b = original(rig.claim("b", revision=2))["attempt_id"]
    original(rig.result(new_b))
    assert error(rig.claim("b", revision=1)) == "domain_conflict"
    old_observation = rig.state(query=Query(rig.wi, revision=2))
    assert workflow(old_observation)["adoption"]["plan_text"] == "Original agreed plan"


@pytest.mark.parametrize(
    "change", ["input", "output", "dependency", "missing-upstream"]
)
def test_invalid_result_reuse_cannot_skip_new_work(rig, change):
    g = graph(rig, [node("a"), node("b")], [edge("a", "b")])
    rig.adopt(g)
    for n in ["a", "b"]:
        original(rig.result(original(rig.claim(n))["attempt_id"]))
    revised = deepcopy(g)
    revised.update(revision=2, confirmation_ref="approval-2")
    if change == "input":
        revised["nodes"][1]["input_refs"] = [
            ArtifactInput("repo/in", "v2", b"changed").reference()
        ]
    elif change == "output":
        revised["nodes"][1]["output_contract"]["artifacts"] = ["new-proof"]
    elif change == "dependency":
        revised["edges"][0]["condition"] = "*"
    reuse = ["b"] if change == "missing-upstream" else ["a", "b"]
    response = rig.approve(
        "revise_plan",
        {
            "graph": revised,
            "plan_text": "new",
            "previous_revision": 1,
            "reuse_results": reuse,
            "reason": "change",
        },
    )
    assert error(response) == "domain_conflict"
    assert workflow(rig.state())["graph"]["revision"] == 1


def test_revoked_collaborator_can_only_record_original_late_observation(rig):
    rig.adopt(graph(rig, [node("a", role="collaborator")]))
    grant = original(
        rig.send(
            "grant_scope",
            {
                "principal": OTHER.principal(),
                "resources": ["repo/a"],
                "intents": ["claim_task", "record_task_result", "abandon_task"],
            },
        )
    )["grant_id"]
    old = original(rig.claim("a", handle="helper"))["attempt_id"]
    assert error(rig.result(old, handle="owner")) == "authority_denied"
    original(rig.send("revoke_scope", {"grant_id": grant, "reason": "reassign"}))
    assert original(rig.result(old, handle="helper"))["current"] is False
    assert workflow(rig.state())["node_stages"]["a"] == "ready"
    assert error(rig.claim("a", handle="helper")) == "authority_denied"


def test_rollover_preserves_plan_and_invalidates_running_attempt(rig):
    rig.adopt(graph(rig))
    old = original(rig.claim("work"))["attempt_id"]
    transfer = original(
        rig.send("rollover", {"target": OTHER.principal(), "summary": "Resume plan"})
    )["transfer_id"]
    original(rig.send("accept_transfer", {"transfer_id": transfer}, handle="helper"))
    rig.contexts["successor"] = replace(OTHER, generation=2)
    new = original(rig.claim("work", handle="successor"))["attempt_id"]
    assert original(rig.result(old))["current"] is False
    assert original(rig.result(new, handle="successor"))["current"] is True
    result = rig.state()
    assert "private-" not in json.dumps(result)
    assert workflow(result)["graph"]["revision"] == 1


def test_running_task_prevents_delegating_its_resource(rig):
    rig.adopt(graph(rig))
    original(rig.claim("work"))
    assert (
        error(
            rig.send(
                "grant_scope",
                {
                    "principal": OTHER.principal(),
                    "resources": ["repo/work"],
                    "intents": ["record_work"],
                },
            )
        )
        == "domain_conflict"
    )


def test_task_input_requires_captured_bytes_and_persists_them_atomically(rig):
    artifact = ArtifactInput("repo/input", "v1", b"fixed task input")
    work = node("work")
    work["input_refs"] = [artifact.reference()]
    rig.adopt(graph(rig, [work]))
    assert error(rig.claim("work")) == "source_invalid"
    assert workflow(rig.state())["node_stages"]["work"] == "ready"
    rig.contexts["with-input"] = replace(BASE, artifacts=(artifact,))
    original(rig.claim("work", handle="with-input"))
    with sqlite3.connect(rig.kernel._database) as db:
        assert (
            db.execute(
                "SELECT content FROM originals WHERE digest=?",
                (artifact.reference()["sha256"],),
            ).fetchone()[0]
            == artifact.content
        )
