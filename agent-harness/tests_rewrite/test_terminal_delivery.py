"""Completed work may deliver fixed bytes without reviving production authority."""

from dataclasses import replace

from harness_rewrite.effects import Effects, FileDelivery
from tests_rewrite.test_delivery import ready as delivery_fixture
from tests_rewrite.test_review import ARTIFACT
from tests_rewrite.test_workflow import original, error

ready = delivery_fixture


def finish(ready):
    rig, candidate, _, destination, worker = ready
    Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi)
    original(rig.send("complete", {"candidate_id": candidate}))
    return rig, candidate, destination, worker


def data(candidate, target):
    return {
        "candidate_id": candidate,
        "deliveries": [
            {
                "artifact": ARTIFACT.reference(),
                "target": target,
                "node_id": None,
                "required": True,
            }
        ],
    }


def test_completed_owner_delivers_original_fixed_bytes_without_reopen_or_review(ready):
    rig, candidate, destination, worker = finish(ready)
    before = rig.state()["result"]["current_observation"]
    original(
        rig.send(
            "declare_delivery",
            data(candidate, "after-completion.txt"),
            request="terminal-delivery",
        )
    )
    pending = rig.state()
    assert pending["work_item"]["business_progress"] == "completed"
    assert (
        pending["result"]["current_observation"]["delivery_obligations"][
            "required_pending"
        ]
        == 1
    )
    assert pending["next_action"]["intent"] == "resume"
    assert error(rig.claim("work")) in {"authority_denied", "domain_conflict"}
    Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi)
    assert (destination / "after-completion.txt").read_bytes() == ARTIFACT.content
    after = rig.state()["result"]["current_observation"]
    assert after["generation"] == before["generation"]
    assert after["terminal"] == before["terminal"]
    assert after["candidates"] == before["candidates"]
    assert after["delivery_obligations"]["required_pending"] == 0
    original(
        rig.send(
            "declare_delivery",
            data(candidate, "after-completion.txt"),
            request="terminal-delivery",
        )
    )
    assert Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi) == []


def test_terminal_delivery_refuses_other_actor_new_bytes_and_graph_reexecution(ready):
    rig, candidate, _, _ = finish(ready)
    rig.contexts["other"] = replace(rig.contexts["owner"], actor="other")
    assert (
        error(
            rig.send("declare_delivery", data(candidate, "other.txt"), handle="other")
        )
        == "authority_denied"
    )
    changed = data(candidate, "different.txt")
    changed["deliveries"][0]["artifact"] = {**ARTIFACT.reference(), "sha256": "0" * 64}
    assert error(rig.send("declare_delivery", changed)) == "source_invalid"
    changed = data(candidate, "rerun.txt")
    changed["deliveries"][0]["node_id"] = "ship"
    assert error(rig.send("declare_delivery", changed)) == "domain_conflict"


def test_terminal_delivery_conflict_preserves_human_file_and_original_completion(ready):
    rig, candidate, destination, worker = finish(ready)
    (destination / "occupied.txt").write_text("human-owned destination")
    original(rig.send("declare_delivery", data(candidate, "occupied.txt")))
    Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi)
    assert (destination / "occupied.txt").read_text() == "human-owned destination"
    state = rig.state()
    assert state["work_item"]["business_progress"] == "completed"
    assert (
        state["result"]["current_observation"]["delivery_obligations"][
            "required_pending"
        ]
        == 1
    )
    assert all(
        c["state"] == "unavailable"
        for c in state["capabilities"]
        if c["name"] == "local_work"
    )


def test_lightweight_terminal_delivery_keeps_original_human_acceptance(tmp_path):
    from tests_rewrite.test_lightweight import light, record
    from harness_rewrite.domain import INTENTS
    from harness_rewrite.delivery import WORKER_INTENTS

    rig = light(tmp_path)
    work = record(rig)
    original(
        rig.approve(
            "complete_lightweight", {"record_ids": [work], "acceptance_refs": ["AC-1"]}
        )
    )
    rig.contexts["owner"] = replace(rig.contexts["owner"], allowed_intents=INTENTS)
    terminal = rig.state()["result"]["current_observation"]["terminal"]
    original(rig.send("declare_delivery", data(None, "lightweight.txt")))

    def worker(artifacts):
        rig.contexts["worker"] = replace(
            rig.contexts["owner"],
            actor="worker",
            session="service",
            allowed_intents=WORKER_INTENTS,
            effect_kinds=frozenset({"deliver_artifact"}),
            artifacts=artifacts,
        )
        return "worker"

    destination = tmp_path / "delivery"
    destination.mkdir()
    Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi)
    assert (destination / "lightweight.txt").read_bytes() == ARTIFACT.content
    state = rig.state()["result"]["current_observation"]
    assert state["business_progress"] == "completed" and state["terminal"] == terminal
    assert state["candidates"]["items"] == []
