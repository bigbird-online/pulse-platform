"""Intermediate control rounds are original graph facts, never recycled approvals."""

from dataclasses import replace
import pytest
from harness_rewrite import SourceInput
from harness_rewrite.model import ArtifactInput, digest
from tests_rewrite.test_workflow import (
    Rig,
    graph,
    node,
    edge,
    original,
    workflow,
    error,
)
from tests_rewrite.test_review import (
    control,
    ARTIFACT,
    PROOF,
    VERIFIER,
    assign,
    verify,
    approve,
    candidate_id,
)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.contexts["verifier"] = VERIFIER
    return r


def staged(rig, *, with_review=True, parallel=False):
    v1 = control("v1", "verification", ["accepted", "blocked"])
    nodes = [
        node("first", outputs=["report"]),
        v1,
        node("second", outputs=["report"]),
        control("v2", "verification", ["accepted"]),
        control("r2", "review", ["approve"]),
    ]
    edges = [edge("first", "v1"), edge("second", "v2"), edge("v2", "r2", "accepted")]
    if with_review:
        nodes.append(control("r1", "review", ["approve", "changes_requested"]))
        edges += [edge("v1", "r1", "accepted"), edge("r1", "second", "approve")]
    else:
        edges.append(edge("v1", "second", "accepted"))
    if parallel:
        nodes.append(node("parallel", outputs=["report"]))
    rig.adopt(graph(rig, nodes, edges))
    original(
        rig.result(
            original(rig.claim("first"))["attempt_id"], artifacts={"report": ARTIFACT}
        )
    )
    return candidate_id(rig)


def test_two_rounds_keep_exact_control_results_and_final_acceptance(rig):
    first = staged(rig)
    assert error(rig.claim("second")) == "domain_conflict"
    original(verify(rig, first, assign(rig, first)))
    original(approve(rig, first))
    assert candidate_id(rig) is None
    assert workflow(rig.state())["node_stages"]["second"] == "ready"
    second_artifact = ArtifactInput(
        "repo/deliverable", "version-2", b"New actual output"
    )
    original(
        rig.result(
            original(rig.claim("second"))["attempt_id"],
            artifacts={"report": second_artifact},
        )
    )
    second = candidate_id(rig)
    assert second != first
    assert error(rig.send("complete", {"candidate_id": first})) == "domain_conflict"
    assert error(approve(rig, second)) == "domain_conflict"
    original(verify(rig, second, assign(rig, second)))
    original(approve(rig, second))
    original(rig.send("complete", {"candidate_id": second}))
    state = rig.state()["result"]["current_observation"]
    assert len(state["candidates"]["items"]) == 2
    assert {
        k: v["candidate_id"] for k, v in state["workflow"]["control_results"].items()
    } == {"v1": first, "r1": first, "v2": second, "r2": second}
    assert state["candidates"]["items"][0]["content"]["artifacts"] == [
        ARTIFACT.reference()
    ]


def test_pending_intermediate_gate_does_not_block_unrelated_ready_task(rig):
    first = staged(rig, with_review=False, parallel=True)
    attempt = original(rig.claim("parallel"))["attempt_id"]
    original(rig.result(attempt, artifacts={"report": PROOF}))
    assert candidate_id(rig) == first
    assert workflow(rig.state())["node_stages"]["parallel"] == "finished"
    original(verify(rig, first, assign(rig, first)))
    assert candidate_id(rig) is None
    assert workflow(rig.state())["node_stages"]["second"] == "ready"


def test_negative_intermediate_result_reworks_same_nodes_and_preserves_receipt(rig):
    first = staged(rig)
    original(verify(rig, first, assign(rig, first), "blocked"))
    assert candidate_id(rig) == first
    original(
        rig.send(
            "revise_work",
            {
                "candidate_id": first,
                "nodes": ["first"],
                "reason": "Fix the actual evidence gap",
            },
        )
    )
    assert workflow(rig.state())["control_results"] == {}
    original(
        rig.result(
            original(rig.claim("first"))["attempt_id"], artifacts={"report": PROOF}
        )
    )
    assert candidate_id(rig) != first
    assert (
        rig.state()["result"]["current_observation"]["candidates"]["items"][0][
            "verification"
        ]["verdict"]
        == "blocked"
    )


def test_control_named_outputs_require_real_bytes_and_feed_downstream_binding(rig):
    v = control("verify", "verification", ["accepted"])
    v["output_contract"]["artifacts"] = ["proof"]
    rig.adopt(
        graph(rig, [node("work", outputs=["report"]), v], [edge("work", "verify")])
    )
    original(
        rig.result(
            original(rig.claim("work"))["attempt_id"], artifacts={"report": ARTIFACT}
        )
    )
    candidate = candidate_id(rig)
    assignment = assign(rig, candidate)
    assert error(verify(rig, candidate, assignment)) == "invalid_input"
    data = {
        "candidate_id": candidate,
        "assignment_id": assignment,
        "verdict": "accepted",
        "checks": [
            {"criterion": "AC-1", "verdict": "passed", "evidence": [PROOF.reference()]}
        ],
        "reason": "Actual check",
        "outputs": {"proof": PROOF.reference()},
    }
    rig.contexts["receipt"] = replace(
        VERIFIER,
        artifacts=(PROOF,),
        source=SourceInput(
            "proof",
            "independent_result",
            b"Actual independent output",
            "record_verification",
            digest(
                {"intent": "record_verification", "work_item_id": rig.wi, "input": data}
            ),
        ),
    )
    original(rig.send("record_verification", data, handle="receipt"))
    assert workflow(rig.state())["control_results"]["verify"]["outputs"] == {
        "proof": PROOF.reference()
    }


def test_intermediate_real_delivery_unblocks_downstream_and_keeps_original_result(
    rig, tmp_path
):
    from harness_rewrite.domain import INTENTS
    from harness_rewrite.delivery import WORKER_INTENTS
    from harness_rewrite.effects import Effects, FileDelivery

    rig.contexts["owner"] = replace(rig.contexts["owner"], allowed_intents=INTENTS)
    v = control("v", "verification", ["accepted"])
    review = control("review", "review", ["approve"])
    ship = node("ship", kind="delivery", role="delivery", outputs=["report"])
    rig.adopt(
        graph(
            rig,
            [
                node("first", outputs=["report"]),
                v,
                review,
                ship,
                node("second", outputs=["report"]),
            ],
            [
                edge("first", "v"),
                edge("v", "review", "accepted"),
                edge("review", "ship", "approve"),
                edge("ship", "second"),
            ],
        )
    )
    original(
        rig.result(
            original(rig.claim("first"))["attempt_id"], artifacts={"report": ARTIFACT}
        )
    )
    first = candidate_id(rig)
    original(verify(rig, first, assign(rig, first)))
    original(approve(rig, first))
    assert candidate_id(rig) == first
    assert workflow(rig.state())["node_stages"]["second"] == "waiting"
    original(
        rig.send(
            "declare_delivery",
            {
                "candidate_id": first,
                "deliveries": [
                    {
                        "artifact": ARTIFACT.reference(),
                        "target": "first.md",
                        "required": True,
                        "node_id": "ship",
                        "output": "report",
                    }
                ],
            },
        )
    )
    dest = tmp_path / "delivered"
    dest.mkdir()

    def worker(artifacts):
        rig.contexts["worker"] = replace(
            rig.contexts["owner"],
            actor="worker",
            session="worker-session",
            allowed_intents=WORKER_INTENTS,
            effect_kinds=frozenset({"deliver_artifact"}),
            artifacts=artifacts,
        )
        return "worker"

    Effects(rig.kernel, FileDelivery(dest), worker).drain(rig.wi)
    assert (dest / "first.md").read_bytes() == ARTIFACT.content
    assert candidate_id(rig) is None
    assert workflow(rig.state())["node_stages"]["second"] == "ready"
    original(
        rig.result(
            original(rig.claim("second"))["attempt_id"], artifacts={"report": PROOF}
        )
    )
    assert candidate_id(rig) != first
    assert workflow(rig.state())["control_results"]["ship"]["outputs"] == {
        "report": ARTIFACT.reference()
    }


def test_intermediate_acceptance_scope_never_reduces_final_criteria(rig):
    from harness_rewrite import Command
    from tests_rewrite.test_workflow import DEFINITION

    rig.wi = original(
        rig.kernel.execute(
            Command(
                "two-criteria",
                "create_draft",
                {**DEFINITION, "acceptance": ["First criterion", "Second criterion"]},
            ),
            "create",
        )
    )["work_item_id"]
    v1 = control("v1", "verification", ["accepted"])
    v1["acceptance_refs"] = ["AC-1"]
    v2 = control("v2", "verification", ["accepted"])
    v2["acceptance_refs"] = ["AC-1"]
    rig.adopt(
        graph(
            rig,
            [
                node("first", outputs=["report"]),
                v1,
                node("second", outputs=["report"]),
                v2,
            ],
            [
                edge("first", "v1"),
                edge("v1", "second", "accepted"),
                edge("second", "v2"),
            ],
        )
    )
    original(
        rig.result(
            original(rig.claim("first"))["attempt_id"], artifacts={"report": ARTIFACT}
        )
    )
    first = candidate_id(rig)
    original(verify(rig, first, assign(rig, first)))
    original(
        rig.result(
            original(rig.claim("second"))["attempt_id"], artifacts={"report": PROOF}
        )
    )
    second = candidate_id(rig)
    assignment = assign(rig, second)
    assert error(verify(rig, second, assignment)) == "domain_conflict"
    item = rig.state()["result"]["current_observation"]["candidates"]["items"][-1]
    assert item["content"]["acceptance_refs"] == ["AC-1", "AC-2"]
    assert item["content"]["terminal"] is True
