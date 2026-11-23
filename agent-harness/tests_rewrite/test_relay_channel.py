"""Real channel/Host/Kernel decisions with isolated original runtime fixtures."""

import json
from pathlib import Path

import pytest
from harness_rewrite import cli, Command, Query
from harness_rewrite.model import Rejected, digest
from harness_rewrite.relay_channel import command_host
from tests_rewrite.test_relay_session import rig as rig, process
from tests_rewrite.test_native_host import native as native
from harness_rewrite.session_sources import AGY_TRANSCRIPT


@pytest.mark.parametrize("runtime", ["pi", "cc", "codex", "agy"])
@pytest.mark.parametrize("command", ["cancel", "checkpoint"])
def test_original_channel_command_commits_without_model_dispatch(
    rig, monkeypatch, capsys, runtime, command
):
    _, _, kernel, original, wi, proc = rig
    entry = proc.parent / "harness.pyz"
    policy_path = entry.parent / "host-policy.json"
    policy = json.loads(policy_path.read_bytes())
    source = {
        "channel": "feishu",
        "eventId": "cancel-input",
        "humanRef": "human",
        "message": "/wi " + command + " " + wi + (" saved work" if command == "checkpoint" else ""),
    }
    tenant = "telegram:fixture"
    ref = digest(["feishu", "cancel-input", "human", tenant])
    record = Path(policy["human_inputs"]) / (ref + ".json")
    record.write_text(json.dumps({"source": source, "poolKey": tenant}))
    record.chmod(0o600)
    request = {"ref": ref, "pid": 101}
    if runtime != "pi":
        sessions = proc.parent / runtime
        sessions.mkdir()
        native_id = original.native_header["id"]
        row = (
            {
                "type": "session_meta",
                "payload": {"id": native_id, "cwd": str(sessions), "source": "cli"},
            }
            if runtime == "codex"
            else {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "previous input"}
            if runtime == "agy"
            else {
                "type": "user",
                "sessionId": native_id,
                "cwd": str(sessions),
                "message": {"role": "user", "content": "previous input"},
            }
        )
        if runtime == "agy":
            # agy keeps one directory per conversation under its brain root.
            transcript = sessions / native_id / AGY_TRANSCRIPT
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps(row) + "\n")
        else:
            (sessions / (native_id + ".jsonl")).write_text(json.dumps(row) + "\n")
        proofs = proc.parent / "proofs"
        proofs.mkdir(mode=0o700)
        # agy has no process between turns; Relay retains its conversation record instead.
        proof = proofs / ("retained-0123.json" if runtime == "agy" else "123.json")
        proof.write_text(
            json.dumps(
                {
                    "schema": "pulse.native-session-retained/v1"
                    if runtime == "agy"
                    else "pulse.native-session-source/v1",
                    "runtime": runtime,
                    "session_id": native_id,
                    "tenant": tenant,
                }
            )
        )
        proof.chmod(0o600)
        profile = {
            **policy["profiles"][0],
            "runtime": runtime,
            "session_root": str(sessions),
            "binding_root": str(proofs),
            "workspace_roots": [str(sessions)],
            "relay_tenants": [tenant],
        }
        policy["profiles"] = [profile]
        policy_path.write_text(json.dumps(policy))
        request = {"ref": ref, "native_id": native_id, "tenant": tenant}
    host = command_host(entry, request, proc=proc, caller=55)
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])
    code = cli.main(["channel", "--human-input-ref", ref], _host=host)
    response = json.loads(capsys.readouterr().out)
    assert code == 0, response
    assert response["request_id"] == "channel." + ref
    revision = response["work_item"]["local_revision"]
    assert cli.main(["channel", "--human-input-ref", ref], _host=host) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["work_item"]["local_revision"] == revision
    assert kernel.inspect(Query(wi), original.handle)["work_item"]["business_progress"] == (
        "terminated" if command == "cancel" else "active"
    )
    if command == "cancel":
        assert response["result"]["current_observation"]["terminal"]["termination"] == "cancelled"
    process(proc, 56, 101, [proc.parent / "node"])
    with pytest.raises(Rejected):
        command_host(entry, request, proc=proc, caller=56)
    with pytest.raises(Rejected):
        host.human_input("0" * 64)


def test_original_input_missing_is_retryable_not_a_replacement_decision(rig):
    from harness_rewrite.kernel import Kernel

    _, _, _, host, _, _ = rig
    with pytest.raises(Rejected) as error:
        host.human_input("0" * 64)
    assert Kernel._error(error.value)["error"]["retryable"] is True


@pytest.mark.parametrize("runtime", ["pi", "cc"])
def test_first_slash_input_in_new_relay_session_needs_no_native_log(rig, monkeypatch, capsys, runtime):
    from harness_rewrite.native_host import NativeHost
    from harness_rewrite.session_sources import process_identity, tenant_directory

    _, _, kernel, original, wi, proc = rig
    entry = proc.parent / "harness.pyz"
    policy_path = entry.parent / "host-policy.json"
    policy = json.loads(policy_path.read_bytes())
    tenant = "telegram:fixture"
    native_id = original.native_header["id"]
    executable = Path(policy["producer_node"]) if runtime == "pi" else proc.parent / "claude"
    executable.touch()
    process(proc, 102, 99, [executable, "--session-id", native_id])
    (proc / "102/stat").write_text("102 (native) " + " ".join(["S"] + ["0"] * 18 + ["4242"]))
    (proc / "sys/kernel/random").mkdir(parents=True, exist_ok=True)
    (proc / "sys/kernel/random/boot_id").write_text("fixture-boot\n")
    # The runtime has not written its first log row: the tenant root stays empty.
    sessions = proc.parent / ("relay-" + runtime) / (tenant_directory(tenant) if runtime == "pi" else "projects")
    sessions.mkdir(parents=True)
    proofs = proc.parent / "proofs"
    proofs.mkdir(mode=0o700)
    proof = proofs / "102.json"
    proof.write_text(json.dumps({
        "schema": "pulse.native-session-source/v1", "runtime": runtime,
        "process": process_identity(102, proc), "relay_pid": 99,
        "session_id": native_id, "allocation_id": native_id, "tenant": tenant,
    }))
    proof.chmod(0o600)
    profile = {
        **policy["profiles"][0],
        "runtime": runtime,
        "session_root": str(sessions),
        "binding_root": str(proofs),
        "workspace_roots": [str(sessions)],
        **({"executable": str(executable), "relay_tenants": [tenant]} if runtime == "cc" else {}),
    }
    policy["profiles"] = [profile]
    policy_path.write_text(json.dumps(policy))
    source = {"channel": "feishu", "eventId": "first-input", "humanRef": "human",
              "message": "/wi checkpoint " + wi + " first input"}
    ref = digest(["feishu", "first-input", "human", tenant])
    record = Path(policy["human_inputs"]) / (ref + ".json")
    record.write_text(json.dumps({"source": source, "poolKey": tenant}))
    record.chmod(0o600)

    # Ordinary native tools still require the original log.
    with pytest.raises(Rejected):
        NativeHost(policy_path, proc=proc, pid=102)
    host = command_host(entry, {"ref": ref, "pid": 102}, proc=proc, caller=55)
    assert host.session_file is None and host.native_header["id"] == native_id
    with pytest.raises(Rejected) as usage:
        host.prepare("open_usage_interval", {"task_attempt": None}, wi, 1, (), None)
    assert usage.value.code == "measurement_source_unavailable"
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])
    code = cli.main(["channel", "--human-input-ref", ref], _host=host)
    response = json.loads(capsys.readouterr().out)
    assert code == 0, response
    assert response["request_id"] == "channel." + ref
    assert kernel.inspect(Query(wi), original.handle)["work_item"]["business_progress"] == "active"


@pytest.mark.parametrize("intent", ["create_lightweight", "create_draft"])
def test_bare_slash_wi_starts_a_startable_draft_and_keeps_planless_standard_read_only(
    rig, monkeypatch, capsys, intent
):
    from harness_rewrite import Command

    _, _, kernel, original, active_wi, proc = rig
    entry = proc.parent / "harness.pyz"
    policy_path = entry.parent / "host-policy.json"
    policy = json.loads(policy_path.read_bytes())
    # Starting work drains its effects, which needs the Host delivery root.
    (proc.parent / "delivery").mkdir(mode=0o700)
    policy["delivery_root"] = str(proc.parent / "delivery")
    policy_path.write_text(json.dumps(policy))
    payload = {"summary": "Fixture work for tests.", "why": "fixture", "what": "draft work", "acceptance": ["started"],
               "owner": "producer", "resources": ["repo"]}
    original.prepare(intent, payload, None, 0, (), None)
    wi = kernel.execute(Command("draft", intent, payload), original.handle)["work_item"]["id"]
    tenant = "telegram:fixture"
    for event, target, expected in (("take-draft", wi, "active" if intent == "create_lightweight" else "draft"),
                                    ("take-active", active_wi, "active")):
        source = {"channel": "feishu", "eventId": event, "humanRef": "human", "message": "/wi " + target}
        ref = digest(["feishu", event, "human", tenant])
        record = Path(policy["human_inputs"]) / (ref + ".json")
        record.write_text(json.dumps({"source": source, "poolKey": tenant}))
        record.chmod(0o600)
        host = command_host(entry, {"ref": ref, "pid": 101}, proc=proc, caller=55)
        monkeypatch.setattr(cli.sys, "argv", [str(entry)])
        assert cli.main(["channel", "--human-input-ref", ref], _host=host) == 0
        response = json.loads(capsys.readouterr().out)
        assert response["status"] == ("local_committed" if target == wi and expected == "active"
                                      else "read_only"), response
        assert kernel.inspect(Query(target), original.handle)["work_item"]["business_progress"] == expected


def test_relay_tenant_profile_takes_precedence_over_direct_profile_of_same_cli(rig):
    from harness_rewrite.native_host import process_chain
    from harness_rewrite.session_sources import locate, process_identity

    _, _, _, original, _, proc = rig
    policy = json.loads((proc.parent / "host-policy.json").read_bytes())
    claude, sessions, proofs = proc.parent / "claude", proc.parent / "projects", proc.parent / "proofs"
    claude.touch()
    sessions.mkdir()
    proofs.mkdir(mode=0o700)
    native_id = original.native_header["id"]
    process(proc, 103, 99, [claude, "--session-id", native_id])
    (proc / "103/stat").write_text("103 (claude) " + " ".join(["S"] + ["0"] * 18 + ["77"]))
    (proc / "sys/kernel/random").mkdir(parents=True, exist_ok=True)
    (proc / "sys/kernel/random/boot_id").write_text("fixture-boot\n")
    (sessions / (native_id + ".jsonl")).write_text(json.dumps(
        {"type": "user", "sessionId": native_id, "cwd": str(sessions),
         "message": {"role": "user", "content": "first"}}) + "\n")
    base = {**policy["profiles"][0], "runtime": "cc", "executable": str(claude),
            "session_root": str(sessions), "workspace_roots": [str(sessions)]}
    relay = {**base, "binding_root": str(proofs), "relay_tenants": ["telegram:fixture"]}
    policy["profiles"] = [relay, base]
    chain = process_chain(103, proc)
    # Without a Relay proof this is a directly launched CLI: the direct profile owns it.
    assert locate(policy, chain, proc)["profile"] == base
    proof = proofs / "103.json"
    proof.write_text(json.dumps({
        "schema": "pulse.native-session-source/v1", "runtime": "cc",
        "process": process_identity(103, proc), "relay_pid": 99,
        "session_id": native_id, "allocation_id": native_id, "tenant": "telegram:fixture"}))
    proof.chmod(0o600)
    located = locate(policy, chain, proc)
    assert located["profile"] == relay
    assert located["environment"]["PULSE_PI_TENANT_ID"] == "telegram:fixture"


def switch_request(rig, message, event, **extra):
    _, _, _, _, _, proc = rig
    entry = proc.parent / "harness.pyz"
    policy_path = entry.parent / "host-policy.json"
    policy = json.loads(policy_path.read_bytes())
    policy["delivery_root"] = str(proc.parent / "delivery")
    policy_path.write_text(json.dumps(policy))
    tenant = "telegram:fixture"
    source = {"channel": "telegram", "eventId": event, "humanRef": "human", "message": message}
    ref = digest(["telegram", event, "human", tenant])
    record = Path(policy["human_inputs"]) / (ref + ".json")
    record.write_text(json.dumps({"source": source, "poolKey": tenant}))
    record.chmod(0o600)
    return entry, ref, {"ref": ref, "pid": 101, **extra}


def test_switch_leave_records_the_held_work_and_enter_moves_authority(rig, monkeypatch, capsys):
    _, _, kernel, original, wi, proc = rig
    target = "wi_20990101_001"
    entry, ref, request = switch_request(rig, "/wi switch " + target, "switch-1", phase="leave")
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])
    host = command_host(entry, request, proc=proc, caller=55)
    assert host.relay_switch_phase == "leave"
    assert cli.main(["channel", "--human-input-ref", ref], _host=host) == 0
    left = json.loads(capsys.readouterr().out)
    assert left["request_id"] == "channel." + ref + ".leave"
    assert left["work_item"]["id"] == wi
    records = kernel.inspect(Query(wi), original.handle)["result"]["current_observation"]["records"]
    assert "切换到 " + target in records[-1]["record"]["summary"]
    # Entering the work this session already holds changes nothing.
    entry, ref, request = switch_request(rig, "/wi switch " + wi, "switch-2", phase="leave")
    host = command_host(entry, request, proc=proc, caller=55)
    assert cli.main(["channel", "--human-input-ref", ref], _host=host) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": "pulse.wi_switch/v1", "phase": "leave", "left": wi, "recorded": False}
    host = command_host(entry, {**request, "phase": "enter"}, proc=proc, caller=55)
    assert cli.main(["channel", "--human-input-ref", ref], _host=host) == 0
    entered = json.loads(capsys.readouterr().out)
    assert entered["work_item"]["id"] == wi and entered["status"] == "read_only"


def test_switch_leave_ignores_a_draft_the_session_created(rig, monkeypatch, capsys):
    _, _, kernel, original, wi, proc = rig
    draft = {"summary": "Planned next work.", "why": "fixture", "what": "a planned work",
             "acceptance": ["planned"], "owner": "producer", "resources": ["repo"]}
    original.prepare("create_draft", draft, None, 0, (), None)
    target = kernel.execute(Command("draft", "create_draft", draft), original.handle)["work_item"]["id"]
    entry, ref, request = switch_request(rig, "/wi switch " + target, "switch-draft", phase="leave")
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])
    host = command_host(entry, request, proc=proc, caller=55)
    assert cli.main(["channel", "--human-input-ref", ref], _host=host) == 0
    assert json.loads(capsys.readouterr().out)["work_item"]["id"] == wi
    records = kernel.inspect(Query(wi), original.handle)["result"]["current_observation"]["records"]
    assert "切换到 " + target in records[-1]["record"]["summary"]


def test_switch_phase_belongs_to_switch_only(rig):
    _, _, _, _, wi, proc = rig
    entry, _, request = switch_request(rig, "/wi switch " + wi, "switch-3")
    with pytest.raises(Rejected):
        command_host(entry, request, proc=proc, caller=55)  # /wi switch needs a phase.
    entry, _, request = switch_request(rig, "/wi status " + wi, "switch-4", phase="enter")
    with pytest.raises(Rejected):
        command_host(entry, request, proc=proc, caller=55)
    entry, _, request = switch_request(rig, "/wi switch " + wi, "switch-5", phase="sideways")
    with pytest.raises(Rejected):
        command_host(entry, request, proc=proc, caller=55)


def test_switch_leave_stops_when_bindings_cannot_be_read(rig, monkeypatch, capsys):
    _, _, kernel, _, _, proc = rig
    entry, ref, request = switch_request(rig, "/wi switch wi_20990101_002", "switch-6", phase="leave")
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])
    host = command_host(entry, request, proc=proc, caller=55)
    real = cli.Kernel.inspect

    def partial(self, query, handle):
        response = real(self, query, handle)
        if query.view == "bindings":
            response["result"]["coverage"] = "partial"
        return response

    monkeypatch.setattr(cli.Kernel, "inspect", partial)
    assert cli.main(["channel", "--human-input-ref", ref], _host=host) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "binding_coverage_incomplete"
