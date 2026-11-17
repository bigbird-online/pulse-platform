from dataclasses import replace

from harness_rewrite import Command
from tests_rewrite.test_workflow import (
    Rig,
    BASE,
    DEFINITION,
    node,
    graph,
    original,
    error,
    workflow,
)
from tests_rewrite.test_review import ARTIFACT


def light(tmp_path):
    rig = Rig(tmp_path)
    # A distinct isolated light WI; no production or other test database is imported.
    created = rig.kernel.execute(
        Command("light", "create_lightweight", DEFINITION), "create"
    )
    rig.wi = original(created)["work_item_id"]
    original(rig.send("start_work", {}))
    return rig


def record(rig):
    rig.contexts["work"] = replace(BASE, artifacts=(ARTIFACT,))
    return original(
        rig.send(
            "record_work",
            {
                "summary": "Completed actual bounded work",
                "resources": ["repo"],
                "artifacts": [ARTIFACT.reference()],
            },
            handle="work",
        )
    )["record_id"]


def test_lightweight_needs_fixed_work_and_acceptance_but_no_graph_or_verifier(tmp_path):
    rig = light(tmp_path)
    empty = {"record_ids": [], "acceptance_refs": ["AC-1"]}
    assert rig.approve("complete_lightweight", empty)["status"] == "rejected"
    work = record(rig)
    data = {"record_ids": [work], "acceptance_refs": ["AC-1"]}
    assert error(rig.send("complete_lightweight", data)) == "source_invalid"
    done = rig.approve("complete_lightweight", data, request="complete-light")
    assert done["work_item"]["business_progress"] == "completed"
    assert original(
        rig.approve("complete_lightweight", data, request="complete-light")
    ) == original(done)
    state = rig.state()["result"]["current_observation"]
    assert state["workflow"]["graph"] is None
    assert state["candidates"]["items"] == []
    assert state["terminal"]["record_ids"] == [work]


def test_promotion_preserves_identity_work_startup_profile_and_requires_standard_review(
    tmp_path,
):
    rig = light(tmp_path)
    wi = rig.wi
    work = record(rig)
    data = {
        "graph": graph(rig, [node("further")]),
        "plan_text": "Organize the remaining work",
        "reason": "Scope needs a graph",
    }
    promoted = rig.approve("promote_workflow", data, request="promote")
    assert original(
        rig.approve("promote_workflow", data, request="promote")
    ) == original(promoted)
    state = rig.state()["result"]["current_observation"]
    assert (
        rig.wi == wi
        and state["profile"] == "standard"
        and state["startup_profile"] == "lightweight"
    )
    assert state["records"][0]["record_id"] == work
    assert workflow(rig.state())["node_stages"]["further"] == "ready"
    assert (
        error(
            rig.approve(
                "complete_lightweight",
                {"record_ids": [work], "acceptance_refs": ["AC-1"]},
            )
        )
        == "domain_conflict"
    )
    original(rig.claim("further"))


def test_work_record_cannot_substitute_a_digest_for_missing_actual_bytes(tmp_path):
    rig = light(tmp_path)
    result = rig.send(
        "record_work",
        {
            "summary": "Only claimed",
            "resources": ["repo"],
            "artifacts": [ARTIFACT.reference()],
        },
    )
    assert error(result) == "source_invalid"
    assert rig.state()["result"]["current_observation"]["records"] == []


def test_closing_errors_name_the_expected_records_criteria_and_human_ref_place(tmp_path):
    rig = light(tmp_path)
    state = rig.state()["result"]["current_observation"]
    assert state["acceptance"] == [
        {"ref": f"AC-{i + 1}", "text": text}
        for i, text in enumerate(state["definition"]["acceptance"])
    ]
    work = record(rig)
    wrong = rig.approve(
        "complete_lightweight", {"record_ids": [work, "op_other"], "acceptance_refs": ["x"]}
    )
    assert wrong["error"]["code"] == "domain_conflict"
    assert work in wrong["error"]["reason"] and "AC-1" in wrong["error"]["reason"]
    misplaced = rig.send(
        "complete_lightweight",
        {"record_ids": [work], "acceptance_refs": ["AC-1"], "human_input_ref": "a" * 64},
    )
    assert misplaced["error"]["code"] == "invalid_input"
    assert "top level of execute" in misplaced["error"]["reason"]
    unapproved = rig.send("complete_lightweight", {"record_ids": [work], "acceptance_refs": ["AC-1"]})
    assert unapproved["error"]["code"] == "source_invalid"
    assert "top-level human_input_ref" in unapproved["error"]["reason"]
