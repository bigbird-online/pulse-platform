"""The isolated producer -> independent result -> human review -> rework/complete cycle."""

from dataclasses import replace
import sqlite3

import pytest

from harness_rewrite import Kernel, SourceInput
from harness_rewrite.model import ArtifactInput, digest
from tests_rewrite.test_workflow import (
    Rig,
    BASE,
    graph,
    node,
    edge,
    original,
    workflow,
    error,
)

VERIFIER = replace(BASE, actor="independent-verifier", session="private-verifier")
ARTIFACT = ArtifactInput("repo/deliverable", "version-1", b"actual fixed output")
PROOF = ArtifactInput(
    "proof/check.txt", "v1", b"independently inspected exact candidate bytes"
)


@pytest.fixture
def rig(tmp_path):
    value = Rig(tmp_path)
    value.contexts["verifier"] = VERIFIER
    return value


def candidate_id(rig):
    return rig.state()["result"]["current_observation"]["candidates"]["active"]


def produce(rig, *, extra=None):
    nodes = [node("work", outputs=["result"])] + (extra or [])
    rig.adopt(graph(rig, nodes))
    attempt = original(rig.claim("work"))["attempt_id"]
    result = rig.result(attempt, artifacts={"result": ARTIFACT}, request="finish")
    original(result)
    return candidate_id(rig)


def assign(rig, candidate):
    assigned = rig.send(
        "assign_verification",
        {"candidate_id": candidate, "verifier": VERIFIER.principal()},
    )
    assignment = original(assigned)["assignment_id"]
    original(
        rig.send(
            "acknowledge_verification",
            {"candidate_id": candidate, "assignment_id": assignment},
            handle="verifier",
        )
    )
    return assignment


def verify(rig, candidate, assignment, verdict="accepted", *, request=None):
    checks = [
        {
            "criterion": "AC-1",
            "verdict": "passed" if verdict == "accepted" else "failed",
            "evidence": [PROOF.reference()],
        }
    ]
    data = {
        "candidate_id": candidate,
        "assignment_id": assignment,
        "verdict": verdict,
        "checks": checks,
        "reason": "Checked the exact candidate",
    }
    rig.contexts["receipt"] = replace(
        VERIFIER,
        artifacts=(PROOF,),
        source=SourceInput(
            "verifier-result",
            "independent_result",
            b"Original independent verifier receipt",
            "record_verification",
            digest(
                {"intent": "record_verification", "work_item_id": rig.wi, "input": data}
            ),
        ),
    )
    return rig.send("record_verification", data, handle="receipt", request=request)


def approve(rig, candidate, decision="approve"):
    return rig.approve(
        "record_review",
        {
            "candidate_id": candidate,
            "decision": decision,
            "reason": "Review exact candidate",
        },
    )


def test_last_task_atomically_freezes_candidate_and_requires_independent_ack(rig):
    candidate = produce(rig)
    assert candidate is not None
    response = rig.state()
    assert response["work_item"]["business_progress"] == "awaiting_verification"
    assert response["next_action"]["intent"] == "assign_verification"
    assert (
        next(c["state"] for c in response["capabilities"] if c["name"] == "local_work")
        == "unavailable"
    )
    items = response["result"]["current_observation"]["candidates"]["items"]
    assert items[0]["content"]["artifacts"] == [ARTIFACT.reference()]
    assert (
        error(
            rig.send(
                "assign_verification",
                {"candidate_id": candidate, "verifier": BASE.principal()},
            )
        )
        == "authority_denied"
    )
    assignment = original(
        rig.send(
            "assign_verification",
            {"candidate_id": candidate, "verifier": VERIFIER.principal()},
        )
    )["assignment_id"]
    assert error(verify(rig, candidate, assignment)) == "domain_conflict"
    assert (
        error(
            rig.send(
                "acknowledge_verification",
                {"candidate_id": candidate, "assignment_id": assignment},
            )
        )
        == "authority_denied"
    )
    original(
        rig.send(
            "acknowledge_verification",
            {"candidate_id": candidate, "assignment_id": assignment},
            handle="verifier",
        )
    )
    receipt = verify(rig, candidate, assignment, request="receipt")
    assert original(verify(rig, candidate, assignment, request="receipt")) == original(
        receipt
    )
    assert rig.state()["next_action"]["intent"] == "record_review"
    original(approve(rig, candidate))
    completed = rig.send("complete", {"candidate_id": candidate}, request="complete")
    assert completed["work_item"]["business_progress"] == "completed"
    assert original(
        rig.send("complete", {"candidate_id": candidate}, request="complete")
    ) == original(completed)
    assert rig.state()["next_action"] == {
        "intent": "inspect",
        "reason": "no_managed_resources_declared",
    }
    assert not rig.state()["result"]["current_observation"]["workspace_settlement"][
        "resources_retired"
    ]
    assert (
        rig.state()["result"]["current_observation"]["terminal"]["candidate_id"]
        == candidate
    )


@pytest.mark.parametrize("verdict", ["partial", "blocked", "rejected"])
def test_negative_verification_reworks_same_wi_without_reusing_old_receipt(
    rig, verdict
):
    old = produce(rig)
    assignment = assign(rig, old)
    original(verify(rig, old, assignment, verdict))
    assert error(approve(rig, old)) == "domain_conflict"
    assert rig.state()["next_action"]["intent"] == "revise_work"
    original(
        rig.send(
            "revise_work",
            {"candidate_id": old, "nodes": ["work"], "reason": "Repair observed issue"},
        )
    )
    assert candidate_id(rig) is None
    assert workflow(rig.state())["node_stages"]["work"] == "ready"
    attempt = original(rig.claim("work"))["attempt_id"]
    changed = ArtifactInput("repo/deliverable", "version-2", b"corrected output")
    original(rig.result(attempt, artifacts={"result": changed}))
    new = candidate_id(rig)
    assert new != old
    assert error(verify(rig, new, assignment)) == "authority_denied"
    items = rig.state()["result"]["current_observation"]["candidates"]["items"]
    assert len(items) == 2 and items[0]["verification"]["verdict"] == verdict
    assert items[1]["verification"] is None
    assert items[1]["content"]["round"] == 2


def test_human_feedback_retains_unaffected_upstream_result(rig):
    rig.adopt(
        graph(
            rig, [node("a", outputs=["a"]), node("b", outputs=["b"])], [edge("a", "b")]
        )
    )
    for name in ["a", "b"]:
        original(
            rig.result(
                original(rig.claim(name))["attempt_id"], artifacts={name: ARTIFACT}
            )
        )
    old = candidate_id(rig)
    original(verify(rig, old, assign(rig, old)))
    original(approve(rig, old, decision="changes_requested"))
    original(
        rig.send(
            "revise_work", {"candidate_id": old, "nodes": ["b"], "reason": "Fix only B"}
        )
    )
    assert workflow(rig.state())["node_stages"] == {"a": "finished", "b": "ready"}
    assert len(workflow(rig.state())["attempts"]) == 2


def test_waiver_records_risk_without_fabricating_independent_acceptance(rig):
    candidate = produce(rig)
    original(verify(rig, candidate, assign(rig, candidate), "blocked"))
    data = {
        "candidate_id": candidate,
        "action": "waive",
        "reason": "Explicit bounded exception",
        "risk": "Independent blocker remains",
        "observed_verdict": "blocked",
    }
    assert error(rig.send("waive_verification", data)) == "source_invalid"
    original(rig.approve("waive_verification", data))
    original(approve(rig, candidate))
    original(rig.approve("waive_verification", {**data, "action": "revoke"}))
    assert error(rig.send("complete", {"candidate_id": candidate})) == "domain_conflict"
    item = rig.state()["result"]["current_observation"]["candidates"]["items"][0]
    assert item["verification"]["verdict"] == "blocked"
    assert item["waiver"]["action"] == "revoke"


def control(name, kind, outcomes):
    value = node(name)
    value.update(
        kind=kind, executor_role="verifier" if kind == "verification" else "human"
    )
    value["output_contract"]["outcomes"] = outcomes
    value["completion_rule"]["success_outcomes"] = [outcomes[0]]
    return value


def test_explicit_control_nodes_follow_dependencies_and_actual_receipts(rig):
    v = control("verify", "verification", ["accepted", "blocked", "waived"])
    r = control("review", "review", ["approve", "changes_requested"])
    rig.adopt(
        graph(
            rig,
            [node("work", outputs=["result"]), v, r],
            [edge("work", "verify"), edge("verify", "review", "accepted")],
        )
    )
    assert workflow(rig.state())["node_stages"]["review"] == "waiting"
    original(
        rig.result(
            original(rig.claim("work"))["attempt_id"], artifacts={"result": ARTIFACT}
        )
    )
    candidate = candidate_id(rig)
    original(verify(rig, candidate, assign(rig, candidate)))
    assert workflow(rig.state())["node_stages"]["review"] == "ready"
    original(approve(rig, candidate))
    assert workflow(rig.state())["node_stages"]["review"] == "finished"
    original(rig.send("complete", {"candidate_id": candidate}))


def test_waiver_does_not_fabricate_selected_verification_edge(rig):
    v = control("verify", "verification", ["accepted", "blocked", "waived"])
    r = control("review", "review", ["approve", "changes_requested"])
    rig.adopt(
        graph(
            rig,
            [node("work", outputs=["result"]), v, r],
            [edge("work", "verify"), edge("verify", "review", "accepted")],
        )
    )
    original(
        rig.result(
            original(rig.claim("work"))["attempt_id"], artifacts={"result": ARTIFACT}
        )
    )
    candidate = candidate_id(rig)
    original(verify(rig, candidate, assign(rig, candidate), "blocked"))
    original(
        rig.approve(
            "waive_verification",
            {
                "candidate_id": candidate,
                "action": "waive",
                "reason": "Explicit exception",
                "risk": "Blocked verification",
                "observed_verdict": "blocked",
            },
        )
    )
    assert workflow(rig.state())["node_stages"]["review"] == "skipped"
    assert error(approve(rig, candidate)) == "domain_conflict"
    assert error(rig.send("complete", {"candidate_id": candidate})) == "domain_conflict"


@pytest.mark.parametrize("multiple", [False, True])
def test_intermediate_control_rounds_are_admitted_without_implicit_execution(
    rig, multiple
):
    v = control("verify", "verification", ["accepted"])
    nodes = [node("work"), v]
    edges = [edge("verify", "work", "accepted")]
    if multiple:
        nodes.append(control("verify_again", "verification", ["accepted"]))
        edges = []
    response = rig.approve(
        "adopt_plan",
        {
            "graph": graph(rig, nodes, edges),
            "plan_text": "Explicit intermediate review",
        },
    )
    original(response)
    assert workflow(rig.state())["graph"] is not None
    assert rig.state()["work_item"]["business_progress"] == "draft"


def test_receipt_cannot_claim_acceptance_without_checks_or_source_binding(rig):
    candidate = produce(rig)
    assignment = assign(rig, candidate)
    data = {
        "candidate_id": candidate,
        "assignment_id": assignment,
        "verdict": "accepted",
        "checks": [],
        "reason": "claim",
    }
    assert (
        error(rig.send("record_verification", data, handle="verifier"))
        == "source_invalid"
    )
    rig.contexts["fake-complete"] = replace(
        VERIFIER,
        source=SourceInput(
            "receipt",
            "independent_result",
            b"empty claim",
            "record_verification",
            digest(
                {"intent": "record_verification", "work_item_id": rig.wi, "input": data}
            ),
        ),
    )
    assert (
        error(rig.send("record_verification", data, handle="fake-complete"))
        == "domain_conflict"
    )


def test_required_delivery_cannot_be_skipped_at_completion(rig):
    candidate = produce(rig, extra=[node("ship", kind="delivery", role="delivery")])
    original(verify(rig, candidate, assign(rig, candidate)))
    original(approve(rig, candidate))
    assert error(rig.send("complete", {"candidate_id": candidate})) == "domain_conflict"
    response = rig.state()
    assert response["work_item"]["business_progress"] == "awaiting_delivery"
    assert response["next_action"]["intent"] == "observe_delivery"


def test_no_half_candidate_after_journal_failure(rig):
    rig.adopt(graph(rig, [node("work", outputs=["result"])]))
    attempt = original(rig.claim("work"))["attempt_id"]

    def fail(stage):
        if stage == "after_journal":
            raise OSError(28, "disk full before commit")

    normal = rig.kernel
    rig.kernel = Kernel(normal._database, rig.contexts.__getitem__, fault=fail)
    assert (
        error(rig.result(attempt, artifacts={"result": ARTIFACT}, request="atomic"))
        == "storage_unavailable"
    )
    rig.kernel = normal
    assert candidate_id(rig) is None
    assert workflow(rig.state())["node_stages"]["work"] == "running"
    original(rig.result(attempt, artifacts={"result": ARTIFACT}, request="atomic"))
    assert len(rig.state()["result"]["current_observation"]["candidates"]["items"]) == 1
    with sqlite3.connect(rig.kernel._database) as db:
        import json

        entry = json.loads(
            db.execute(
                "SELECT content FROM journal WHERE request_id='atomic'"
            ).fetchone()[0]
        )
    assert [e["kind"] for e in entry["events"]] == [
        "TaskResultRecorded",
        "CandidateSubmitted",
    ]
