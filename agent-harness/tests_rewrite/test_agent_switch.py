"""/wi switch through relay-channel: an Agent's own request, a child target, bounded phases."""

import json
from pathlib import Path

import pytest
from harness_rewrite import Command, Kernel, Query, agent_switch, cli
from harness_rewrite.model import Rejected, writer_identity
from harness_rewrite.native_host import NativeHost
from harness_rewrite.relay_channel import command_host
from tests_rewrite.test_relay_channel import switch_request
from tests_rewrite.test_relay_session import process, rig as rig
from tests_rewrite.test_native_host import native as native

LIGHT = {"summary": "Second fixture work.", "why": "fixture", "what": "other work",
         "acceptance": ["entered"], "owner": "producer", "resources": ["repo"]}


def session(proc, pid, native_id):
    """Another native Pi session of the same Agent in the same Relay conversation."""
    policy = json.loads((proc.parent / "host-policy.json").read_bytes())
    file = Path(policy["profiles"][0]["session_root"]) / ("2026_" + native_id + ".jsonl")
    file.write_text(json.dumps({"type": "session", "id": native_id}) + "\n")
    process(proc, pid, 99, [Path(policy["pi_binary"]), "--session", file],
            b"PULSE_PI_TENANT_ID=telegram:fixture\0")
    return NativeHost(proc.parent / "host-policy.json", proc=proc, pid=pid)


def kernel_of(host):
    """A kernel on the rig's database that authenticates this native session."""
    import hashlib

    entry = Path(host.policy["database"]).parent / "harness.pyz"
    return Kernel(Path(host.policy["database"]), host.authenticate, writer_id=writer_identity(
        hashlib.sha256(entry.read_bytes()).hexdigest(), host.policy))


def run(entry, request, proc, capsys, monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])
    host = command_host(entry, request, proc=proc, caller=55)
    code = cli.main(["channel", "--human-input-ref", request["ref"]], _host=host)
    return code, json.loads(capsys.readouterr().out)


def frontier(kernel, host, wi):
    note = Command("frontier", "checkpoint", {"summary": "done; next", "resources": ["repo"]}, wi)
    assert kernel.execute(note, host.handle)["status"] == "local_committed"


def ask(host, wi, target, tmp, monkeypatch, capsys, request="ask"):
    """The Agent's request_switch through the native execute interface."""
    envelope = tmp / (request + ".json")
    envelope.write_text(json.dumps({"request_id": request, "intent": "request_switch",
                                    "work_item_id": wi, "input": {"target": target, "summary": "go"}}))
    monkeypatch.setattr(cli.sys, "argv", [str(tmp / "harness.pyz")])
    code = cli.main(["execute", "--input", str(envelope)], _host=host)
    return code, json.loads(capsys.readouterr().out)


def test_human_switch_to_a_child_answers_before_leaving(rig, monkeypatch, capsys):
    _, _, kernel, host, wi, proc = rig
    child = {k: v for k, v in LIGHT.items() if k != "owner"} | {"parent": wi}
    host.prepare("create_child", child, None, 1, (), None)
    created = kernel.execute(Command("child", "create_child", child), host.handle)
    target = created["result"]["original_request_result"]["work_item_id"]
    before = kernel.inspect(Query(wi), host.handle)["work_item"]["local_revision"]
    entry, ref, request = switch_request(rig, "/wi switch " + target, "switch-child", phase="leave")
    code, left = run(entry, request, proc, capsys, monkeypatch)
    assert code == 0 and left["left"] == wi and left["recorded"] is False
    assert left["no_switch"] == target + " 是当前 WI " + wi + " 的子 WI，执行权已在本会话，无需切换"
    assert kernel.inspect(Query(wi), host.handle)["work_item"]["local_revision"] == before


def test_human_leave_answers_with_the_bounded_facts_only(rig, monkeypatch, capsys):
    _, _, kernel, host, wi, proc = rig
    entry, _, request = switch_request(rig, "/wi switch wi_20990101_003", "switch-small", phase="leave")
    code, left = run(entry, request, proc, capsys, monkeypatch)
    assert code == 0, left
    assert set(left) <= set(cli.HANDOVER_FIELDS) | {"work_item"}
    assert left["work_item"]["id"] == wi and left["status"] == "local_committed"


def test_agent_request_leaves_its_own_session_and_enters_a_draft_it_owns(rig, tmp_path, monkeypatch, capsys):
    _, _, kernel, host, wi, proc = rig
    host.prepare("create_lightweight", LIGHT, None, 0, (), None)
    target = kernel.execute(Command("draft", "create_lightweight", LIGHT), host.handle)["work_item"]["id"]
    host.prepare("checkpoint", {}, wi, 1, (), None)
    frontier(kernel, host, wi)
    code, asked = ask(host, wi, target, tmp_path, monkeypatch, capsys)
    assert code == 0, asked
    ref = asked["session_switch_request"]["ref"]
    assert ref == agent_switch.notice_ref("fixture", wi, "ask")
    assert agent_switch.read(host.policy, ref)["target"] == target
    revision = kernel.inspect(Query(wi), host.handle)["work_item"]["local_revision"]
    entry = proc.parent / "harness.pyz"
    code, left = run(entry, {"ref": ref, "pid": 101, "phase": "leave"}, proc, capsys, monkeypatch)
    assert code == 0 and left == {"schema": "pulse.wi_switch/v1", "phase": "leave", "left": wi,
                                  "recorded": False, "agent_request": True}
    # The Agent's own checkpoint stays the latest record.
    assert kernel.inspect(Query(wi), host.handle)["work_item"]["local_revision"] == revision
    fresh = session(proc, 103, "fresh-session")
    # Before enter, any session of the conversation reads back that nothing was entered.
    code, before = run(entry, {"ref": ref, "pid": 101, "phase": "readback"}, proc, capsys, monkeypatch)
    assert code == 0 and before == {"schema": "pulse.wi_switch/v1", "phase": "readback",
                                    "wi": target, "committed": False}
    with pytest.raises(Rejected, match="Only the session that asked"):
        command_host(entry, {"ref": ref, "pid": 103, "phase": "leave"}, proc=proc, caller=55)
    code, entered = run(entry, {"ref": ref, "pid": 103, "phase": "enter"}, proc, capsys, monkeypatch)
    assert code == 0 and entered["status"] == "local_committed", entered
    assert entered["work_item"] == {**entered["work_item"], "id": target, "business_progress": "active"}
    assert cli.held_work_item(kernel_of(fresh), fresh.handle) == target
    # The committed enter reads back as committed, from the source or the target session.
    for pid in (101, 103):
        code, after = run(entry, {"ref": ref, "pid": pid, "phase": "readback"}, proc, capsys, monkeypatch)
        assert code == 0 and after["committed"] is True
    # A repeated enter finds the work already held there and moves nothing.
    code, again = run(entry, {"ref": ref, "pid": 103, "phase": "enter"}, proc, capsys, monkeypatch)
    assert code == 0 and again["status"] == "read_only" and again["work_item"]["id"] == target
    assert again["work_item"]["generation"] == entered["work_item"]["generation"]


def test_agent_request_enters_work_held_by_another_session_of_the_same_agent(
    rig, tmp_path, monkeypatch, capsys
):
    _, _, kernel, host, wi, proc = rig
    other_session = session(proc, 102, "other-session")
    theirs = kernel_of(other_session)
    other_session.prepare("create_lightweight", LIGHT, None, 0, (), None)
    other = theirs.execute(Command("other", "create_lightweight", LIGHT), other_session.handle)
    assert other["status"] == "local_committed", other["error"]
    target = other["work_item"]["id"]
    other_session.prepare("start_work", {}, target, 1, (), None)
    assert theirs.execute(Command("other-start", "start_work", {}, target), other_session.handle)[
        "status"] == "local_committed"
    code, refused = ask(host, wi, target, tmp_path, monkeypatch, capsys, "early")
    assert code == 2 and "latest record must be a checkpoint" in refused["error"]["reason"]
    host.prepare("checkpoint", {}, wi, 1, (), None)
    frontier(kernel, host, wi)
    code, asked = ask(host, wi, target, tmp_path, monkeypatch, capsys)
    assert code == 0, asked
    ref = asked["session_switch_request"]["ref"]
    entry = proc.parent / "harness.pyz"
    assert run(entry, {"ref": ref, "pid": 101, "phase": "leave"}, proc, capsys, monkeypatch)[0] == 0
    fresh = session(proc, 103, "fresh-session")
    code, entered = run(entry, {"ref": ref, "pid": 103, "phase": "enter"}, proc, capsys, monkeypatch)
    assert code == 0 and entered["status"] == "local_committed", entered
    assert cli.held_work_item(kernel_of(fresh), fresh.handle) == target
    assert kernel.inspect(Query(target), host.handle)["work_item"]["generation"] == 2
    # The left WI stays held by the session that asked.
    assert cli.held_work_item(kernel, host.handle) == wi


def test_agent_switch_notice_needs_a_committed_request_of_this_conversation(rig, monkeypatch, capsys):
    _, _, kernel, host, wi, proc = rig
    entry = proc.parent / "harness.pyz"
    ref = agent_switch.post(host, wi, "never-asked", "wi_20990101_005")["ref"]
    code, left = run(entry, {"ref": ref, "pid": 101, "phase": "leave"}, proc, capsys, monkeypatch)
    assert code == 2 and "not committed" in left["error"]["reason"]
    with pytest.raises(Rejected):
        command_host(entry, {"ref": ref, "pid": 101}, proc=proc, caller=55)  # A phase is required.
    notice = agent_switch.notice_path(host.policy, ref)
    value = json.loads(notice.read_bytes())
    notice.write_text(json.dumps({**value, "tenant": "telegram:another"}))
    with pytest.raises(Rejected, match="differs from this conversation"):
        command_host(entry, {"ref": ref, "pid": 101, "phase": "leave"}, proc=proc, caller=55)


def test_request_switch_outside_a_relay_conversation_is_refused(rig, tmp_path, monkeypatch, capsys):
    _, _, kernel, host, wi, _ = rig
    host.prepare("checkpoint", {}, wi, 1, (), None)
    frontier(kernel, host, wi)
    host.environment = {}
    code, refused = ask(host, wi, "wi_20990101_006", tmp_path, monkeypatch, capsys)
    assert code == 2 and refused["error"]["code"] == "host_session_controller_required"


def test_human_switch_enter_reads_back_by_its_original_reference(rig, monkeypatch, capsys):
    _, _, kernel, host, wi, proc = rig
    host.prepare("create_lightweight", LIGHT, None, 0, (), None)
    target = kernel.execute(Command("draft", "create_lightweight", LIGHT), host.handle)["work_item"]["id"]
    entry, ref, request = switch_request(rig, "/wi switch " + target, "switch-readback", phase="leave")
    assert run(entry, request, proc, capsys, monkeypatch)[0] == 0
    readback = {**request, "phase": "readback"}
    assert run(entry, readback, proc, capsys, monkeypatch)[1]["committed"] is False
    session(proc, 103, "fresh-session")
    code, entered = run(entry, {**request, "pid": 103, "phase": "enter"}, proc, capsys, monkeypatch)
    assert code == 0 and entered["status"] == "local_committed", entered
    assert run(entry, readback, proc, capsys, monkeypatch)[1]["committed"] is True


def test_recovery_reads_a_committed_enter_back_and_reenters_only_as_the_same_target(
    rig, tmp_path, monkeypatch, capsys
):
    """Relay's restart path against the real relay-channel: leave, enter in the first target
    session, answer lost; readback says committed, and only that same session enters again."""
    _, _, kernel, host, wi, proc = rig
    host.prepare("create_lightweight", LIGHT, None, 0, (), None)
    target = kernel.execute(Command("draft", "create_lightweight", LIGHT), host.handle)["work_item"]["id"]
    host.prepare("checkpoint", {}, wi, 1, (), None)
    frontier(kernel, host, wi)
    ref = ask(host, wi, target, tmp_path, monkeypatch, capsys)[1]["session_switch_request"]["ref"]
    entry = proc.parent / "harness.pyz"
    assert run(entry, {"ref": ref, "pid": 101, "phase": "leave"}, proc, capsys, monkeypatch)[0] == 0
    session(proc, 103, "first-target")
    code, entered = run(entry, {"ref": ref, "pid": 103, "phase": "enter"}, proc, capsys, monkeypatch)
    assert code == 0 and entered["status"] == "local_committed"  # Relay exits before saving this.
    # After the restart Relay reads back from the source session it is on.
    code, back = run(entry, {"ref": ref, "pid": 101, "phase": "readback"}, proc, capsys, monkeypatch)
    assert code == 0 and back["committed"] is True
    # A second, newly generated target identity cannot reuse the request: this is why the
    # recorded target session must be the one Relay settles on.
    session(proc, 104, "second-target")
    code, other = run(entry, {"ref": ref, "pid": 104, "phase": "enter"}, proc, capsys, monkeypatch)
    assert code == 2 and other["error"]["code"] == "request_conflict", other
    code, again = run(entry, {"ref": ref, "pid": 103, "phase": "enter"}, proc, capsys, monkeypatch)
    assert code == 0 and again["error"] is None and again["work_item"]["id"] == target


def test_recovery_reads_an_uncommitted_enter_back_as_absent(rig, tmp_path, monkeypatch, capsys):
    _, _, kernel, host, wi, proc = rig
    host.prepare("create_lightweight", LIGHT, None, 0, (), None)
    target = kernel.execute(Command("draft", "create_lightweight", LIGHT), host.handle)["work_item"]["id"]
    host.prepare("checkpoint", {}, wi, 1, (), None)
    frontier(kernel, host, wi)
    ref = ask(host, wi, target, tmp_path, monkeypatch, capsys)[1]["session_switch_request"]["ref"]
    entry = proc.parent / "harness.pyz"
    assert run(entry, {"ref": ref, "pid": 101, "phase": "leave"}, proc, capsys, monkeypatch)[0] == 0
    session(proc, 103, "target")  # Relay stopped after replacing the session, before enter.
    for pid in (101, 103):
        code, back = run(entry, {"ref": ref, "pid": pid, "phase": "readback"}, proc, capsys, monkeypatch)
        assert code == 0 and back["committed"] is False
    assert kernel.inspect(Query(target), host.handle)["work_item"]["business_progress"] == "draft"
