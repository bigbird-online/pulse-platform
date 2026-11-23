"""agy rollover: no process between turns and no tools, so Relay names both conversations."""

import json
from pathlib import Path

import pytest
from harness_rewrite.model import Rejected, digest
from harness_rewrite import relay_session as handover
from harness_rewrite.session_sources import AGY_TRANSCRIPT
from tests_rewrite.test_native_host import native as native
from tests_rewrite.test_relay_session import rig as rig

TENANT = "telegram:fixture"
SUCCESSOR = "8f0067ec-0000-4000-8000-000000000001"


def conversation(brain: Path, native_id: str, text: str) -> None:
    path = brain / native_id / AGY_TRANSCRIPT
    path.parent.mkdir(parents=True)
    row = {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
           "content": "<USER_REQUEST>\n" + text + "\n</USER_REQUEST>"}
    path.write_text(json.dumps(row) + "\n")


def retained(proofs: Path, native_id: str, allocation: str) -> None:
    path = proofs / ("retained-" + digest([native_id])[:16] + ".json")
    path.write_text(json.dumps({"schema": "pulse.native-session-retained/v1", "runtime": "agy",
                                "session_id": native_id, "allocation_id": allocation,
                                "tenant": TENANT, "entry": "telegram"}))
    path.chmod(0o600)


@pytest.fixture
def agy(rig, tmp_path):
    _, _, kernel, host, wi, proc = rig
    policy_path = tmp_path / "host-policy.json"
    policy = json.loads(policy_path.read_text())
    brain, proofs = tmp_path / "brain", tmp_path / "proofs"
    brain.mkdir()
    proofs.mkdir(mode=0o700)
    policy["profiles"][0].update(runtime="agy", session_root=str(brain.resolve()), binding_root=str(proofs),
                                 relay_tenants=[TENANT])
    policy_path.write_text(json.dumps(policy))
    source = host.native_header["id"]
    conversation(brain.resolve(), source, "earlier work")
    retained(proofs, source, "relay-source-session")
    ref = digest(["telegram", "original", "human", TENANT])

    def run(stage, native_id=None):
        return handover.run(tmp_path / "harness.pyz", {"stage": stage, "ref": ref,
                            **({"native_id": native_id, "tenant": TENANT} if native_id else {})},
                            proc=proc, caller=55)

    return run, brain.resolve(), proofs, source, ref, kernel, wi


def test_relay_hands_an_idle_agy_conversation_to_its_successor(agy):
    run, brain, proofs, source, ref, _, wi = agy
    prepared = run("prepare", source)
    assert prepared["status"] == "local_committed", prepared
    physical = prepared["session_handover"]
    assert physical["prepared"] and not physical["accepted"] and physical["runtime"] == "agy"
    assert not physical["target_session_exists"]
    # The successor's first turn carries the continuation; agy names its own conversation.
    conversation(brain, SUCCESSOR, "[Host continuation operation: " + ref + "]\ncontinue " + wi)
    retained(proofs, SUCCESSOR, physical["target_session_id"])
    observed = run("inspect")["session_handover"]
    assert observed["target_session_exists"] and observed["target_native_id"] == SUCCESSOR
    assert observed["target_bootstrap_observed"]
    accepted = run("accept", SUCCESSOR)
    assert accepted["status"] == "local_committed", accepted
    assert accepted["session_handover"]["accepted"]
    assert run("accept", SUCCESSOR)["work_item"]["local_revision"] == accepted["work_item"]["local_revision"]


def test_an_agy_successor_of_another_allocation_is_refused(agy):
    run, brain, proofs, source, _, _, _ = agy
    run("prepare", source)
    conversation(brain, SUCCESSOR, "unrelated")
    retained(proofs, SUCCESSOR, "another-relay-session")
    with pytest.raises(Rejected, match="one Host allocation"):
        run("accept", SUCCESSOR)
    assert not run("inspect")["session_handover"]["accepted"]


def test_only_agy_is_accepted_without_its_process(agy, tmp_path):
    run, brain, proofs, source, _, _, _ = agy
    prepared = run("prepare", source)
    policy_path = tmp_path / "host-policy.json"
    policy = json.loads(policy_path.read_text())
    policy["profiles"][0]["runtime"] = "codex"
    policy_path.write_text(json.dumps(policy))
    # A retained record names agy only; a codex profile reads no such record.
    retained(proofs, SUCCESSOR, prepared["session_handover"]["target_session_id"])
    with pytest.raises(Rejected):
        run("accept", SUCCESSOR)


def test_relay_reads_an_idle_agy_conversation_s_access_by_its_retained_record(
        agy, rig, tmp_path, monkeypatch, capsys):
    """agy fixes its permissions at launch and has no process between turns: Relay
    reads the next turn's grants by the conversation's retained record."""
    from harness_rewrite import cli
    from harness_rewrite.relay_channel import access_host
    from tests_rewrite.test_session_access import execute, human

    _, _, kernel, original, wi, proc = rig
    _, _, proofs, source, _, _, _ = agy
    entry = tmp_path / "harness.pyz"
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])

    def read():
        host = access_host(entry, {"native_id": source, "tenant": TENANT}, proc=proc, caller=55)
        assert cli.main(["access"], _host=host) == 0
        return json.loads(capsys.readouterr().out)["grants"]

    assert read() == []
    data = {"paths": ["/Users/fixture/granted"], "mode": "write", "reason": "agy turn"}
    approve = human(original, "agy-approve", "同意")
    assert execute(kernel, original, "grant", "grant_access", data, wi, approve)["status"] == (
        "local_committed")
    (row,) = read()
    assert (row["paths"], row["mode"]) == (data["paths"], "write")
    revoke = human(original, "agy-revoke", "撤销")
    assert execute(kernel, original, "revoke", "revoke_access",
                   {"grant_id": row["grant_id"], "reason": "done"}, wi, revoke)["status"] == (
        "local_committed")
    assert read() == []
    # Only a retained agy record is read without its process, and only one source at a time.
    with pytest.raises(Rejected):
        access_host(entry, {"native_id": source, "tenant": TENANT, "pid": 101}, proc=proc, caller=55)
    with pytest.raises(Rejected):
        access_host(entry, {"native_id": SUCCESSOR, "tenant": TENANT}, proc=proc, caller=55)
