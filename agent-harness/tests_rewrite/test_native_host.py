import json
import os
import hashlib

import pytest

from harness_rewrite.model import Rejected, digest
from harness_rewrite.native_host import NativeHost
from harness_rewrite.runtime_presence import process_stamp


@pytest.fixture
def native(tmp_path):
    # Explicit synthetic /proc and native transcript, never production identity.
    proc = tmp_path / "proc"
    process = proc / "101"
    process.mkdir(parents=True)
    binary = tmp_path / "pi"
    binary.write_text("fixture executable marker")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session = sessions / "2026_test-session.jsonl"
    session.write_text(json.dumps({"type": "session", "id": "test-session"}) + "\n")
    process.joinpath("status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t1\n")
    process.joinpath("cmdline").write_bytes(
        b"\0".join(s.encode() for s in [str(binary), "--session", str(session)])
    )
    process.joinpath("environ").write_bytes(
        b"PULSE_PI_TENANT_ID=telegram:fixture\0IGNORED_SECRET=not-public\0"
    )
    inputs = tmp_path / "human-inputs"
    inputs.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "namespace": "fixture",
                "surface": "local-fixture",
                "pi_binary": str(binary),
                "human_inputs": str(inputs),
                "profiles": [
                    {
                        "session_root": str(sessions),
                        "actor": "producer",
                        "role": "producer",
                        "artifact_roots": [str(sessions)],
                    }
                ],
            }
        )
    )
    return policy, proc, session, inputs


def test_native_identity_requires_real_ancestor_and_matching_policy_scope(native):
    policy, proc, session, _ = native
    host = NativeHost(policy, proc=proc, pid=101)
    assert host.base.actor == "producer"
    assert host.base.session.startswith("session.")
    assert "test-session" not in host.base.session
    assert host.environment == {"PULSE_PI_TENANT_ID": "telegram:fixture"}
    assert (
        host.artifact(str(session), "repo/input", "v1").content == session.read_bytes()
    )
    with pytest.raises(Rejected):
        host.artifact(str(policy), "repo/policy", "v1")
    with pytest.raises(Rejected):
        NativeHost(policy, proc=proc, pid=1)


@pytest.mark.parametrize("mode", ["manual", "automatic", "audit"])
@pytest.mark.parametrize("enabled", [False, True])
def test_tcloud_deployment_gate_maps_only_admitted_repositories_and_effect(native, monkeypatch, mode, enabled):
    import runpy
    from pathlib import Path

    from harness_rewrite.session_compute import observe

    policy, proc, session, _ = native
    data = json.loads(policy.read_text())
    data.update(surface="tcloud", execution={"mode": mode, "batch_budget": 8})
    if enabled:
        source = Path(__file__).resolve().parents[2]
        compiler = runpy.run_path(str(source / "Scripts/pulse_native_core_deployment.py"))
        gh = policy.parent / "gh"
        gh.write_text("#!/bin/sh\nexit 0\n")
        gh.chmod(0o700)
        monkeypatch.setattr(compiler["shutil"], "which", lambda name: str(gh))
        data["pull_request_gate"] = compiler["pull_request_gate"](
            "tcloud", source, policy.parent / "release", policy.parent
        )
    policy.write_text(json.dumps(data))
    host = NativeHost(policy, proc=proc, pid=101)
    allowed = enabled and mode != "audit"
    assert host.base.merge_repositories == (frozenset({
        "bigbird-online/PULSE", "bigbird-online/CollaborateWithAI", "bigbird-online/Oikos",
    }) if allowed else frozenset())
    assert host.base.surface == "tcloud"  # Installing a gate does not borrow Mac provenance.
    assert host.base.actor == "producer"
    assert observe("pi", session)["commits"] == []  # Gate enablement does not invent a Pi author.
    handle = host.worker(())
    assert ("merge_pull_request" in host.authenticate(handle).effect_kinds) == allowed


@pytest.mark.parametrize("runtime", ["cc", "codex"])
def test_tcloud_compiled_standalone_sources_keep_exact_workspace_and_native_ancestry(native, runtime):
    import runpy
    from pathlib import Path

    policy, proc, _, _ = native
    account = policy.parent
    workspace = account / "registered-agent"
    workspace.mkdir()
    other = account / "other-agent"
    other.mkdir()
    compiler = runpy.run_path(str(Path(__file__).resolve().parents[2] / "Scripts/pulse_native_consumers.py"))
    registry = {"agents": {
        "producer": {"workspace": "registered-agent", "channels": [
            {"host": "tcloud-node2", "relay_agent_id": "transport-alias"}]},
        "other": {"workspace": "other-agent"},
    }}
    config = compiler["canonical_consumers"]({"users": [{"agents": [
        {"id": "transport-alias", "workspace": str(workspace)},
        {"id": "other", "workspace": str(other)},
    ]}]}, registry, account, "tcloud")
    binary = account / runtime
    binary.write_text("synthetic native executable")
    binary.chmod(0o700)
    programs = {"cc": [], "codex": [], runtime: [str(binary)]}
    profiles = compiler["standalone_profiles"](config, account, account / "proofs", programs)
    data = json.loads(policy.read_text())
    data.update(profiles=profiles, producer_node=data["pi_binary"], surface="tcloud")
    policy.write_text(json.dumps(data))
    root = account / (".codex/sessions" if runtime == "codex" else ".claude/projects")
    session = root / "fixture-session.jsonl"

    def original(cwd):
        row = ({"type": "session_meta", "payload": {"id": "fixture-session", "cwd": str(cwd)}}
               if runtime == "codex" else {"type": "user", "sessionId": "fixture-session", "cwd": str(cwd),
                                          "message": {"role": "user", "content": "Original task"}})
        session.write_text(json.dumps(row) + "\n")

    original(workspace)
    argv = [str(binary), "--session-id", "fixture-session"] if runtime == "cc" else [str(binary), "exec"]
    (proc / "101/cmdline").write_bytes(b"\0".join(a.encode() for a in argv))
    (proc / "101/environ").write_bytes(b"CODEX_THREAD_ID=fixture-session\0")
    host = NativeHost(policy, proc=proc, pid=101)
    assert (host.runtime, host.base.actor, host.base.surface, host.entry) == (runtime, "producer", "tcloud", "cli")
    assert host.session_file == session
    assert host.profile["artifact_roots"] == [str(workspace)]
    assert host.profile["workspace_roots"] == [str(workspace)]
    assert "binding_root" not in host.profile  # No invented CC/Codex proof.
    original(other)
    assert NativeHost(policy, proc=proc, pid=101).base.actor == "other"
    managed = account / "managed/wi_fixture/workspace"
    managed.mkdir(parents=True)
    original(managed)
    with pytest.raises(Rejected, match="No admitted native process"):
        NativeHost(policy, proc=proc, pid=101)
    original(workspace)
    with pytest.raises(Rejected):
        NativeHost(policy, proc=proc, pid=1)  # A shell cannot borrow the profile.
    (proc / "101/cmdline").write_bytes(b"/unregistered/provider\0")
    with pytest.raises(Rejected, match="No admitted native process"):
        NativeHost(policy, proc=proc, pid=101)


def test_human_source_requires_original_relay_event_in_same_native_session(native):
    policy, proc, session, inputs = native
    source = {
        "channel": "telegram",
        "eventId": "event1",
        "humanRef": "human1",
        "message": "Approve this candidate",
    }
    record = {"source": source, "poolKey": "telegram:fixture", "observedAtMs": 1}
    ref = digest(["telegram", "event1", "human1", "telegram:fixture"])
    (inputs / (ref + ".json")).write_text(json.dumps(record))
    host = NativeHost(policy, proc=proc, pid=101)
    with pytest.raises(Rejected):
        host.human_source(ref, "record_review", {}, "wi_fixture")

    with session.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": f"Host human input reference: {ref}.",
                    },
                }
            )
            + "\n"
        )
    with pytest.raises(Rejected):
        host.human_source(ref, "record_review", {}, "wi_fixture")
    with session.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "user",
                        "content": f"Approve this candidate [Host human input reference: {ref}.]",
                    },
                }
            )
            + "\n"
        )
    value = host.human_source(
        ref, "record_review", {"candidate_id": "candidate1"}, "wi_fixture"
    )
    assert value.approved_input_digest == digest(
        {
            "intent": "record_review",
            "work_item_id": "wi_fixture",
            "input": {"candidate_id": "candidate1"},
        }
    )
    record["poolKey"] = "telegram:other"
    (inputs / (ref + ".json")).write_text(json.dumps(record))
    with pytest.raises(Rejected):
        host.human_source(ref, "record_review", {}, "wi_fixture")


@pytest.mark.parametrize("fault", ["pi_node", "relay_node", "relay_entry"])
def test_pi_process_title_does_not_erase_native_relay_admission(native, fault):
    policy, proc, session, _ = native
    data = json.loads(policy.read_text())
    binary = policy.parent / "pi"
    relay = policy.parent / "relay.js"
    relay.write_text("fixture Relay entry")
    # Toolchain Pi and platform Relay have distinct, independently pinned Nodes.
    pi_node = policy.parent / "toolchain-node"
    relay_node = policy.parent / "platform-node"
    pi_node.write_text("fixture canonical Node")
    relay_node.write_text("fixture platform Node")
    data.update(node=str(pi_node), producer_node=str(relay_node), relay_entry=str(relay))
    policy.write_text(json.dumps(data))
    child = proc / "101"
    child.joinpath("status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t201\n")
    child.joinpath("cmdline").write_bytes(b"pi\0")
    child.joinpath("comm").write_text("pi\n")
    child.joinpath("exe").symlink_to(pi_node)
    parent = proc / "201"
    parent.mkdir()
    parent.joinpath("exe").symlink_to(relay_node)
    parent.joinpath("status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t1\n")
    parent.joinpath("cmdline").write_bytes(
        str(binary).encode() + b"\0" + str(relay).encode() + b"\0"
    )
    # Parent node executable itself is not the Pi script. Keep its argv[0]
    # relative so the old explicit-script branch cannot accept this fixture.
    parent.joinpath("cmdline").write_bytes(b"node\0" + str(relay).encode() + b"\0")
    ref = "session_v1_" + hashlib.sha256(b"pulse-session-v1\0test-session").hexdigest()
    context = {
        "schema_version": "scl_relay_host_context/v1",
        "pid": 201,
        "session_ref": ref,
    }
    child.joinpath("environ").write_bytes(
        b"PULSE_PI_TENANT_ID=telegram:fixture\0SCL_SESSION_REF="
        + ref.encode()
        + b"\0SCL_RELAY_HOST_CONTEXT="
        + json.dumps(context).encode()
        + b"\0"
    )
    host = NativeHost(policy, proc=proc, pid=101)
    assert host.session_file == session
    assert host.base.actor == "producer"
    # Neither executable may borrow the other's admission, and CLI remains pinned.
    if fault == "pi_node":
        child.joinpath("exe").unlink()
        child.joinpath("exe").symlink_to(relay_node)
    elif fault == "relay_node":
        parent.joinpath("exe").unlink()
        parent.joinpath("exe").symlink_to(pi_node)
    else:
        parent.joinpath("cmdline").write_bytes(b"node\0/unrelated/relay.js\0")
    with pytest.raises(Rejected, match="Relay ancestor"):
        NativeHost(policy, proc=proc, pid=101)


def test_renamed_verifier_requires_exact_launch_process_and_boot(native):
    policy, proc, _, _ = native
    data = json.loads(policy.read_text())
    binary = policy.parent / "pi"
    dispatch = policy.parent / "dispatch"
    run = dispatch / "effect.fixture"
    sessions = run / "session"
    sessions.mkdir(parents=True)
    session = sessions / "2026_independent-session.jsonl"
    session.write_text(
        json.dumps({"type": "session", "id": "independent-session"}) + "\n"
    )
    data.update(node=str(binary), dispatch_root=str(dispatch))
    data["profiles"].append(
        {
            "role": "verifier",
            "actor": "independent",
            "session_root": str(dispatch),
            "artifact_roots": [str(dispatch)],
        }
    )
    policy.write_text(json.dumps(data))
    child = proc / "101"
    child.joinpath("cmdline").write_bytes(b"pi\0")
    child.joinpath("comm").write_text("pi\n")
    child.joinpath("exe").symlink_to(binary)
    child.joinpath("environ").write_bytes(b"HARNESS_REWRITE_DISPATCH=effect.fixture\0")
    child.joinpath("stat").write_text("101 (pi) S " + "0 " * 18 + "1234\n")
    boot = proc / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("boot-fixture\n")
    (run / "spawn.json").write_text(
        json.dumps(
            {
                "effect_id": "effect.fixture",
                "native_session_id": "independent-session",
                "process": process_stamp(101, proc),
            }
        )
    )
    host = NativeHost(policy, proc=proc, pid=101)
    assert host.base.actor == "independent"
    assert host.base.allowed_intents == {
        "acknowledge_verification",
        "record_verification",
        "record_quality",
    }
    boot.write_text("different-boot\n")
    with pytest.raises(Rejected, match="original launch"):
        NativeHost(policy, proc=proc, pid=101)


def test_a_human_reference_on_child_creation_names_the_fix(native):
    """A misplaced human_input_ref is refused with what to do, not a profile verdict."""
    policy, proc, _, _ = native
    host = NativeHost(policy, proc=proc, pid=101)
    with pytest.raises(Rejected) as refused:
        host.prepare("create_child", {"parent": "wi_fixture"}, None, 1, (), human_ref="a" * 64)
    assert refused.value.code == "authority_denied"
    assert "takes no human_input_ref; omit it" in refused.value.reason
    assert "inherits its parent" in refused.value.reason
