"""Automatic assignment tests; the process stub is not a model or UAT receipt."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_rewrite.delivery import WORKER_INTENTS
from harness_rewrite.effects import Effects
from harness_rewrite.model import native_session_ref
from harness_rewrite.pi_dispatch import PiDispatch
from harness_rewrite.native_compute import checksum
from tests_rewrite.test_review import produce, ARTIFACT
from tests_rewrite.test_workflow import Rig, BASE, original, error


def configured(tmp_path):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(BASE, verification_actor="native-verifier")
    candidate = produce(rig)

    def worker(artifacts):
        rig.contexts["worker"] = replace(
            BASE,
            actor="worker",
            session="worker",
            allowed_intents=WORKER_INTENTS,
            effect_kinds=frozenset({"verification_dispatch"}),
            artifacts=artifacts,
        )
        return "worker"

    worker(())
    return rig, candidate, worker


def test_fixed_candidate_assignment_and_intention_precede_dispatch(tmp_path):
    rig, candidate, worker = configured(tmp_path)
    view = rig.state()
    item = view["result"]["current_observation"]["candidates"]["items"][0]
    assert item["assignment"]["acknowledged"] is False
    assert item["verification"] is None
    assert view["effects"][0]["status"] == "requested"
    assert "native_session_id" not in view["effects"][0]
    effect = rig.state(handle=worker(()))["effects"][0]
    principal = effect["recipient"]
    assert principal["session"] == native_session_ref(
        BASE.namespace, "native-verifier", effect["native_session_id"], BASE.surface
    )
    data = {"candidate_id": candidate, "assignment_id": item["assignment"]["id"]}
    assert error(rig.send("acknowledge_verification", data)) == "authority_denied"
    rig.contexts["native"] = replace(
        BASE, actor=principal["actor"], session=principal["session"]
    )
    original(rig.send("acknowledge_verification", data, handle="native"))
    assert (
        rig.state()["result"]["current_observation"]["candidates"]["items"][0][
            "verification"
        ]
        is None
    )


def write_session(directory, effect):
    """The assigned native session file an SDK runner would create."""
    config = json.loads((directory / "launch-config.json").read_text())
    selection = config["selection"]
    (
        directory / "session" / ("2026-10-08_" + effect["native_session_id"] + ".jsonl")
    ).write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"type": "session", "id": effect["native_session_id"]},
                {
                    "type": "model_change",
                    "provider": selection["provider"],
                    "modelId": selection["model"],
                },
                {
                    "type": "thinking_level_change",
                    "thinkingLevel": selection["thinking"],
                },
                {
                    "type": "custom",
                    "customType": "harness.verification-compute/v1",
                    "data": {
                        "selection": selection,
                        "runner_sha256": config["runner_sha256"],
                        "config_sha256": checksum(
                            (directory / "launch-config.json").read_bytes()
                        ),
                    },
                },
            ]
        )
        + "\n"
    )


@pytest.mark.parametrize("parent_lost_result", [False, True])
def test_uncertain_dispatch_reads_native_session_without_second_launch(
    tmp_path, monkeypatch, parent_lost_result
):
    rig, _, worker = configured(tmp_path)
    root = tmp_path / "dispatch"
    root.mkdir()
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    policy = {
        "dispatch_root": str(root),
        "delivery_root": str(delivery),
        "python": "/canonical/python",
        "node": "/canonical/node",
        "pi_binary": "/existing/pi",
        "agent_config_dir": "/existing/auth",
    }
    launches = []

    def launch(argv, **kwargs):
        launches.append(argv)
        if parent_lost_result:
            raise OSError("parent lost launch result after creating child")
        return SimpleNamespace(pid=123)

    monkeypatch.setattr("harness_rewrite.pi_dispatch.subprocess.Popen", launch)
    adapter = PiDispatch(rig.kernel, policy, worker, rig.wi, tmp_path / "harness.pyz")
    runner = Effects(rig.kernel, adapter, worker)
    runner.drain(rig.wi)
    runner.drain(rig.wi)
    assert len(launches) == 1
    effect = rig.state(handle=worker(()))["effects"][0]
    assert effect["status"] == "unknown"
    directory = root / effect["effect_id"]
    assert (directory / "artifact-0").read_bytes() == ARTIFACT.content
    write_session(directory, effect)
    runner.drain(rig.wi)
    assert len(launches) == 1
    assert rig.state()["effects"][0]["status"] == "observed_success"
    candidate = rig.state()["result"]["current_observation"]["candidates"]["items"][0]
    assert candidate["assignment"]["acknowledged"] is False
    assert candidate["verification"] is None
    assert launches[0][0] == policy["python"]
    assert "native_launch" in launches[0][4]
    config = json.loads((directory / "launch-config.json").read_text())
    assert config["runner"] == "pi-sdk/v1"
    assert config["selection"]["model"] == "gpt-6.1-sol"
    assert len(config["argv"]) == 2
    assert not (directory / "spawn.json").exists()  # Only the real child writes it.
    assert Path(directory / "candidate.json").exists()


def test_empty_spawn_proof_is_a_pre_exec_failure_only_without_procfs(tmp_path, monkeypatch):
    rig, _, worker = configured(tmp_path)
    root = tmp_path / "dispatch"
    root.mkdir()
    policy = {
        "dispatch_root": str(root),
        "delivery_root": str(tmp_path),
        "python": "/canonical/python",
        "node": "/canonical/node",
        "pi_binary": "/existing/pi",
        "agent_config_dir": "/existing/auth",
    }
    monkeypatch.setattr(
        "harness_rewrite.pi_dispatch.subprocess.Popen", lambda *a, **k: None
    )
    adapter = PiDispatch(rig.kernel, policy, worker, rig.wi, tmp_path / "harness.pyz")
    Effects(rig.kernel, adapter, worker).drain(rig.wi)
    effect = rig.state(handle=worker(()))["effects"][0]
    directory = root / effect["effect_id"]
    # Wrapper created spawn.json, then failed before writing it (no exec).
    (directory / "spawn.json").write_bytes(b"")
    # With procfs a paused older wrapper may still write and exec: no proof.
    monkeypatch.setattr("harness_rewrite.pi_dispatch.empty_proof_unspawned", lambda: False)
    assert adapter.readback(effect) == (
        "unknown", {"reason": "native_dispatch_not_yet_observed"}
    )
    # Without procfs (macOS) an older wrapper's stamp always failed before its write.
    monkeypatch.setattr("harness_rewrite.pi_dispatch.empty_proof_unspawned", lambda: True)
    assert adapter.readback(effect) == (
        "failure",
        {"reason": "native_launcher_failed_before_exec", "model_invoked": False},
    )
    write_session(directory, effect)
    assert adapter.readback(effect)[0] == "success"
