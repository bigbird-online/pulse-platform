"""Actual protocol against synthetic procfs and native files; no live conversation."""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from harness_rewrite import Command, Kernel, Query
from harness_rewrite.model import Rejected, digest, writer_identity
from harness_rewrite.native_host import NativeHost
from harness_rewrite import relay_session as handover
from tests_rewrite.test_native_host import native as native


def process(proc, pid, parent, argv, env=b""):
    root = proc / str(pid)
    root.mkdir(exist_ok=True)
    (root / "status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t{parent}\n")
    (root / "cmdline").write_bytes(b"\0".join(str(a).encode() for a in argv))
    (root / "environ").write_bytes(env)
    (root / "exe").symlink_to(argv[0])


@pytest.fixture
def rig(native, tmp_path):
    original, proc, session, inputs = native
    entry = tmp_path / "harness.pyz"
    entry.write_bytes(b"explicit isolated fixture")
    node, relay = tmp_path / "node", tmp_path / "relay.js"
    node.touch()
    relay.touch()
    policy = json.loads(original.read_bytes())
    policy.update(
        producer_node=str(node), relay_entry=str(relay), database=str(tmp_path / "facts.sqlite")
    )
    path = tmp_path / "host-policy.json"
    path.write_text(json.dumps(policy))
    process(proc, 99, 1, [node, relay])
    process(proc, 55, 99, [node])
    (proc / "101/status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t99\n")
    host = NativeHost(path, proc=proc, pid=101)
    import hashlib

    kernel = Kernel(
        Path(policy["database"]),
        host.authenticate,
        writer_id=writer_identity(hashlib.sha256(entry.read_bytes()).hexdigest(), policy),
    )
    payload = {
        "summary": "Fixture work for tests.",
        "why": "fixture",
        "what": "one original work",
        "acceptance": ["continued"],
        "owner": "producer",
        "resources": ["repo"],
    }
    host.prepare("create_lightweight", payload, None, 0, (), None)
    created = kernel.execute(Command("create", "create_lightweight", payload), host.handle)
    wi = created["work_item"]["id"]
    host.current = replace(host.base, generation=1)
    assert (
        kernel.execute(Command("start", "start_work", {}, wi), host.handle)["status"]
        == "local_committed"
    )
    source = {
        "channel": "telegram",
        "eventId": "original",
        "humanRef": "human",
        "message": "/wi rollover " + wi,
    }
    ref = digest(["telegram", "original", "human", "telegram:fixture"])
    human = inputs / (ref + ".json")
    human.write_text(json.dumps({"source": source, "poolKey": "telegram:fixture"}))
    human.chmod(0o600)

    def run(stage, pid=None, caller=55):
        return handover.run(
            entry,
            {"stage": stage, "ref": ref, **({"pid": pid} if pid else {})},
            proc=proc,
            caller=caller,
        )

    def target(native_id):
        target = session.parent / ("2026_" + native_id + ".jsonl")
        target.write_text(json.dumps({"type": "session", "id": native_id}) + "\n")
        process(
            proc,
            102,
            99,
            [Path(policy["pi_binary"]), "--session", target],
            b"PULSE_PI_TENANT_ID=telegram:fixture\0",
        )
        process(proc, 56, 102, [node])

    return run, target, kernel, host, wi, proc


def test_prepare_real_successor_accept_and_original_retry_after_source_disappears(rig):
    run, target, kernel, host, wi, proc = rig
    prepared = run("prepare", 101)
    assert prepared["status"] == "local_committed", prepared
    physical = prepared["session_handover"]
    assert physical["prepared"] and not physical["accepted"]
    assert run("prepare", 101)["session_handover"] == physical
    late = kernel.execute(
        Command("late", "checkpoint", {"summary": "old source", "resources": ["repo"]}, wi),
        host.handle,
    )
    assert late["status"] == "rejected"
    target(physical["target_session_id"])
    (proc / "101/cmdline").unlink()  # physical source vanished; no old Host context is revived
    accepted = run("accept", caller=56)
    assert accepted["status"] == "local_committed", accepted
    assert accepted["session_handover"]["accepted"]
    assert run("inspect")["session_handover"]["accepted"]
    original_revision = accepted["work_item"]["local_revision"]
    assert run("accept", caller=56)["work_item"]["local_revision"] == original_revision
    assert kernel.inspect(Query(wi), host.handle)["work_item"]["generation"] == 2


def test_a_native_caller_cannot_select_somebody_elses_process_or_prepare_host_command(rig):
    run, target, _, _, _, _ = rig
    prepared = run("prepare", 101)
    target(prepared["session_handover"]["target_session_id"])
    with pytest.raises(Rejected, match="another process"):
        run("accept", pid=101, caller=56)
    with pytest.raises(Rejected, match="installed Relay"):
        run("prepare", pid=101, caller=56)


def test_wrong_successor_keeps_original_transfer_and_never_replaces_allocated_identity(rig):
    run, target, _, _, _, _ = rig
    prepared = run("prepare", 101)
    target("wrong-native-session")
    with pytest.raises(Rejected, match="one Host allocation"):
        run("accept", caller=56)
    assert run("inspect")["session_handover"] == prepared["session_handover"]


def test_implicit_wi_prepare_retry_reads_its_original_binding_after_source_fencing(rig):
    run, _, _, host, _, _ = rig
    policy = host.policy
    inputs = Path(policy["human_inputs"])
    source = next(inputs.glob("*.json"))
    value = json.loads(source.read_bytes())
    value["source"]["message"] = "/wi rollover"
    source.write_text(json.dumps(value))
    first = run("prepare", 101)
    assert first["session_handover"]["prepared"]
    assert run("prepare", 101)["session_handover"] == first["session_handover"]


def test_cli_answers_relay_with_json_when_the_handover_action_fails(monkeypatch, capsys):
    import io
    import sys

    from harness_rewrite import cli

    def broken(entry, request):
        raise AttributeError("synthetic")

    monkeypatch.setattr(handover, "run", broken)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"stage": "inspect", "ref": "a" * 64})))
    assert cli.main(["relay-session", "--input", "-"]) == 2
    out, err = capsys.readouterr()
    answer = json.loads(out)
    assert answer["status"] == "rejected"
    assert answer["error"]["code"] == "source_unavailable"
    assert answer["error"]["reason"] == "Relay session action failed: AttributeError"
    assert answer["error"]["exception_type"] == "AttributeError"
    assert err == ""


def test_cli_answers_relay_with_the_handover_only_for_a_large_work_item(monkeypatch, capsys):
    import io
    import sys

    from harness_rewrite import cli

    handover_facts = {"ref": "a" * 64, "wi": "wi_large", "prepared": True, "accepted": False}

    def large(entry, request):
        return {
            "schema": "harness.response/v1",
            "status": "local_committed",
            "work_item": {"id": "wi_large", "generation": 7, "local_revision": 1712, "records": ["x" * 4096] * 1024},
            "effects": [{"detail": "y" * 4096}] * 1024,
            "result": {"current_observation": "z" * 4096},
            "session_handover": handover_facts,
        }

    monkeypatch.setattr(handover, "run", large)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"stage": "inspect", "ref": "a" * 64})))
    assert cli.main(["relay-session", "--input", "-"]) == 0
    out = capsys.readouterr().out
    assert len(out) < 2048
    assert json.loads(out) == {
        "schema": "harness.response/v1",
        "status": "local_committed",
        "work_item": {"id": "wi_large", "generation": 7, "local_revision": 1712},
        "session_handover": handover_facts,
    }


def test_agent_request_is_fenced_and_bound_to_a_successor_of_the_same_agent(rig):
    run, target, kernel, host, wi, proc = rig
    for name in Path(host.policy["human_inputs"]).glob("*.json"):
        name.unlink()  # No human /wi rollover: only the Agent's own committed request.
    note = Command("frontier", "checkpoint", {"summary": "done; next", "resources": ["repo"]}, wi)
    assert kernel.execute(note, host.handle)["status"] == "local_committed"
    asked = kernel.execute(
        Command("ask", "request_rollover", {"summary": "continue"}, wi), host.handle
    )
    assert asked["status"] == "local_committed", asked
    ref = handover.post_agent_request(host, wi, "ask")["ref"]

    def agent(stage, pid=None, caller=55):
        return handover.run(
            entry_of(host), {"stage": stage, "ref": ref, **({"pid": pid} if pid else {})},
            proc=proc, caller=caller,
        )

    prepared = agent("prepare", 101)
    assert prepared["status"] == "local_committed", prepared
    physical = prepared["session_handover"]
    assert physical["prepared"] and physical["wi"] == wi
    target(physical["target_session_id"])
    accepted = agent("accept", caller=56)
    assert accepted["session_handover"]["accepted"], accepted
    assert kernel.inspect(Query(wi), host.handle)["work_item"]["generation"] == 2


def test_agent_request_notice_must_name_the_attested_source_and_a_committed_request(rig):
    run, _, kernel, host, wi, proc = rig
    for name in Path(host.policy["human_inputs"]).glob("*.json"):
        name.unlink()
    note = Command("frontier", "checkpoint", {"summary": "done; next", "resources": ["repo"]}, wi)
    assert kernel.execute(note, host.handle)["status"] == "local_committed"
    # A notice for a request that was never committed is refused before any fence.
    ref = handover.post_agent_request(host, wi, "never-asked")["ref"]
    with pytest.raises(Rejected, match="not committed"):
        handover.run(entry_of(host), {"stage": "prepare", "ref": ref, "pid": 101}, proc=proc, caller=55)
    notice = handover.handover_root(host.policy) / "agent-requests" / (ref + ".json")
    value = json.loads(notice.read_bytes())
    value["source_native_id"] = "another-native-session"
    notice.write_text(json.dumps(value))
    with pytest.raises(Rejected, match="differs from this source"):
        handover.run(entry_of(host), {"stage": "prepare", "ref": ref, "pid": 101}, proc=proc, caller=55)
    assert kernel.inspect(Query(wi), host.handle)["work_item"]["generation"] == 1  # nothing fenced


def entry_of(host):
    return Path(host.policy["database"]).parent / "harness.pyz"
