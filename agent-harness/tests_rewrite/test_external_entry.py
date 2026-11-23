"""External entries act as an owner-confirmed Agent; synthetic identities only (A-37)."""

import json
import os

import pytest

from harness_rewrite import external_entry
from harness_rewrite.model import Rejected, digest
from harness_rewrite.native_host import NativeHost
from tests_rewrite.test_native_host import native as native

OWNER = "1000000001"


def confirm(inputs, pool, message, event, human=OWNER):
    """What the Relay platform bot records when the owner presses confirm."""
    source = {"channel": "telegram", "eventId": event, "humanRef": human,
              "message": json.dumps(message, sort_keys=True)}
    ref = digest([source["channel"], event, human, pool])
    path = inputs / (ref + ".json")
    path.write_text(json.dumps({"source": source, "poolKey": pool, "observedAtMs": 0}))
    path.chmod(0o600)
    return ref


def bind(policy_data, inputs, actor="producer", entry="chatgpt_web", human=OWNER):
    request_id = external_entry.request_binding(policy_data, entry, actor)
    message = {"schema": external_entry.CONFIRMATION, "kind": "binding",
               "request_id": request_id, "entry": entry, "actor": actor}
    ref = confirm(inputs, "entry-binding:" + request_id, message, "cb-" + request_id, human)
    binding_id = request_id[::-1]
    record = {"schema": external_entry.BINDING, "binding_id": binding_id, "entry": entry,
              "actor": actor, "request_id": request_id, "human_input_ref": ref,
              "confirmed_at": "2026-10-16T00:00:00Z"}
    path = external_entry.directory(policy_data, "bindings") / (binding_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    log = external_entry.session_log(policy_data, binding_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"type": "session", "id": binding_id, "entry": entry}) + "\n")
    return binding_id, path


@pytest.fixture
def external(native, tmp_path):
    policy, proc, _, inputs = native
    python = tmp_path / "python"
    python.write_text("fixture interpreter")
    entry = tmp_path / "harness.pyz"
    entry.write_text("fixture bundle")
    data = json.loads(policy.read_text())
    data["python"] = str(python)
    data["external_entries"] = {"root": str(tmp_path / "entries"), "owner": OWNER,
                                "entries": ["chatgpt_web"]}
    policy.write_text(json.dumps(data))
    return policy, proc, inputs, data, python, entry


def launcher(proc, python, entry, binding_id):
    """Child 101 runs under the entry launcher 202."""
    parent = proc / "202"
    parent.mkdir(exist_ok=True)
    parent.joinpath("status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t1\n")
    parent.joinpath("cmdline").write_bytes(b"\0".join(
        a.encode() for a in [str(python), "-I", "-B", str(entry), "entry", binding_id, "--", "execute"]))
    child = proc / "101"
    child.joinpath("status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t202\n")
    child.joinpath("cmdline").write_bytes(b"\0".join(
        a.encode() for a in [str(python), "-I", "-B", str(entry), "execute"]))
    child.joinpath("environ").write_bytes(b"")


@pytest.mark.skipif(os.getuid() == 0, reason="root reads every environment")
def test_an_unreadable_older_pi_above_the_launcher_does_not_refuse_it(external):
    """wi_20261120_666: the scan for a dispatched verifier reads only processes titled
    harness-pi-v1. An older Pi (title pi) above the launcher, whose environment this
    account cannot read, is never read, and the entry nearer than it still works."""
    policy, proc, inputs, data, python, entry = external
    data["node"] = str(policy.parent / "pi")
    policy.write_text(json.dumps(data))
    binding_id, _ = bind(data, inputs)
    launcher(proc, python, entry, binding_id)
    proc.joinpath("202/status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t203\n")
    older = proc / "203"
    older.mkdir()
    older.joinpath("status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t1\n")
    older.joinpath("cmdline").write_bytes(b"pi\0")
    older.joinpath("comm").write_text("pi\n")
    older.joinpath("environ").write_bytes(b"HARNESS_REWRITE_DISPATCH=effect.fixture\0")
    older.joinpath("environ").chmod(0)
    try:
        with pytest.raises(PermissionError):
            older.joinpath("environ").read_bytes()
        host = NativeHost(policy, proc=proc, pid=101)
        assert (host.runtime, host.entry, host.base.actor) == ("external", "chatgpt_web", "producer")
    finally:
        older.joinpath("environ").chmod(0o600)


def test_confirmed_binding_works_as_its_agent_with_exact_confirmations(external):
    policy, proc, inputs, data, python, entry = external
    binding_id, _ = bind(data, inputs)
    launcher(proc, python, entry, binding_id)
    host = NativeHost(policy, proc=proc, pid=101)
    assert (host.runtime, host.entry, host.base.actor) == ("external", "chatgpt_web", "producer")
    assert host.base.session == "session." + digest(
        [data["namespace"], "producer", binding_id, data["surface"]])
    assert {"checkpoint", "claim_task", "add_requirement"} <= host.base.allowed_intents

    command = {"summary": "Owner adopted the draft", "resources": []}
    request_id = external_entry.request_decision(
        data, binding_id, "producer", "chatgpt_web", "checkpoint", "wi_fixture", command)
    request = json.loads(
        (external_entry.directory(data, "requests") / (request_id + ".json")).read_text())
    assert request["kind"] == "decision" and "Owner adopted" in request["summary"]
    message = {"schema": external_entry.CONFIRMATION, "kind": "decision",
               "request_id": request_id, "binding_id": binding_id, "intent": "checkpoint",
               "work_item_id": "wi_fixture", "input_digest": request["input_digest"]}
    ref = confirm(inputs, "entry:" + binding_id, message, "cb-decision")
    host.prepare("checkpoint", command, "wi_fixture", 1, (), human_ref=ref)
    assert host.current.source.kind == "native_human"
    # One changed word needs a new confirmation.
    with pytest.raises(Rejected, match="different content"):
        host.prepare("checkpoint", {**command, "summary": "Changed"}, "wi_fixture", 1, (),
                     human_ref=ref)
    # A binding confirmation is not a decision for this session.
    record = json.loads(external_entry.directory(data, "bindings").joinpath(
        binding_id + ".json").read_text())
    with pytest.raises(Rejected):
        host.prepare("checkpoint", command, "wi_fixture", 1, (),
                     human_ref=record["human_input_ref"])


def test_a_plan_binds_its_confirmation_after_the_owner_confirms(external):
    policy, proc, inputs, data, python, entry = external
    binding_id, _ = bind(data, inputs)
    launcher(proc, python, entry, binding_id)
    host = NativeHost(policy, proc=proc, pid=101)
    graph = {"graph_id": "plan", "confirmation_ref": "to-be-confirmed", "nodes": [{"id": "work"}]}
    plan = {"graph": graph, "plan_text": "Owner agreed plan"}
    request_id = external_entry.request_decision(
        data, binding_id, "producer", "chatgpt_web", "adopt_plan", "wi_fixture", plan)
    request = json.loads(
        (external_entry.directory(data, "requests") / (request_id + ".json")).read_text())
    message = {"schema": external_entry.CONFIRMATION, "kind": "decision",
               "request_id": request_id, "binding_id": binding_id, "intent": "adopt_plan",
               "work_item_id": "wi_fixture", "input_digest": request["input_digest"]}
    ref = confirm(inputs, "entry:" + binding_id, message, "cb-plan")
    bound = {**plan, "graph": {**graph, "confirmation_ref": ref}}
    host.prepare("adopt_plan", bound, "wi_fixture", 1, (), human_ref=ref)
    assert host.current.source.approved_input_digest == digest(
        {"intent": "adopt_plan", "work_item_id": "wi_fixture", "input": bound})
    # Any other confirmation reference, or any other plan content, is refused.
    for changed in ({**plan, "graph": {**graph, "confirmation_ref": "other"}},
                    {**bound, "graph": {**bound["graph"], "nodes": []}},
                    {**bound, "plan_text": "Changed plan"}):
        with pytest.raises(Rejected, match="different content"):
            host.prepare("adopt_plan", changed, "wi_fixture", 1, (), human_ref=ref)


def test_unconfirmed_forged_or_revoked_bindings_are_refused(external):
    policy, proc, inputs, data, python, entry = external
    # Someone other than the owner pressed confirm.
    stranger, _ = bind(data, inputs, human="1234")
    launcher(proc, python, entry, stranger)
    with pytest.raises(Rejected, match="owner"):
        NativeHost(policy, proc=proc, pid=101)
    # A hand-written binding without the owner's confirmation.
    binding_id, path = bind(data, inputs)
    forged = json.loads(path.read_text())
    forged["actor"] = "verifier-like"
    path.write_text(json.dumps(forged))
    launcher(proc, python, entry, binding_id)
    with pytest.raises(Rejected):
        NativeHost(policy, proc=proc, pid=101)
    # Revocation ends the binding.
    binding_id, path = bind(data, inputs)
    revoked = json.loads(path.read_text())
    revoked["revoked_at"] = "2026-10-16T01:00:00Z"
    path.write_text(json.dumps(revoked))
    launcher(proc, python, entry, binding_id)
    with pytest.raises(Rejected, match="revoked"):
        NativeHost(policy, proc=proc, pid=101)
    # Unknown entries and unregistered Agents cannot even be requested.
    with pytest.raises(Rejected):
        external_entry.request_binding(data, "unknown_entry", "producer")
    with pytest.raises(Rejected):
        external_entry.request_binding(data, "chatgpt_web", "nobody")


def test_policy_shape_of_external_entries(external):
    *_, data, _, _ = external
    for broken in (
        {**data["external_entries"], "owner": "not-an-account"},
        {**data["external_entries"], "root": "relative/path"},
        {**data["external_entries"], "entries": []},
        {"root": data["external_entries"]["root"], "owner": OWNER},
    ):
        with pytest.raises(ValueError):
            external_entry.shape(broken)


def test_wait_reports_pending_then_the_platform_result(external):
    *_, data, _, _ = external
    request_id = external_entry.request_binding(data, "chatgpt_web", "producer")
    assert external_entry.wait(data, request_id, 0) == {"status": "pending", "request_id": request_id}
    result = external_entry.directory(data, "requests") / (request_id + ".result.json")
    result.write_text(json.dumps({"status": "confirmed", "binding_id": "b" * 32}))
    result.chmod(0o600)
    assert external_entry.wait(data, request_id, 0)["status"] == "confirmed"


def test_launcher_logs_each_command_before_running_it(external, tmp_path):
    *_, data, _, _ = external
    inputs = tmp_path / "human-inputs"
    binding_id, _ = bind(data, inputs)
    log = external_entry.session_log(data, binding_id)
    log.unlink()
    program = tmp_path / "program.py"
    program.write_text("import sys; raise SystemExit(len(sys.argv))")
    assert external_entry.launch(program, data, [binding_id, "--", "identity"]) == 2
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[0] == {"type": "session", "id": binding_id, "entry": "chatgpt_web"}
    assert rows[1]["type"] == "agent_command" and rows[1]["argv"] == ["identity"]
    assert log.stat().st_mode & 0o077 == 0
    with pytest.raises(Rejected):
        external_entry.launch(program, data, [binding_id, "identity"])
    with pytest.raises(Rejected):
        external_entry.launch(program, data, ["0" * 32, "--", "identity"])
