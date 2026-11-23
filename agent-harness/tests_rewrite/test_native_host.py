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


def dispatched_under_producer(native, monkeypatch, source, forged=False, attempt=None,
                              legacy=False):
    """A verifier the kernel dispatched from inside a producer's command (pid 101),
    still the child of the producer's session process (pid 102): a Pi CLI, a
    Relay-proven session, or a source made by `as_source`.

    The verifier is the SDK runner with the launch configuration PiDispatch writes
    (`attempt` set: a continuation under attempts/<attempt>). `legacy` is an older
    Pi CLI dispatch: title pi and no launch configuration."""
    from harness_rewrite.native_compute import RUNNER, TITLE, checksum, runner_bytes, sdk_entries
    from tests_rewrite.test_compute_selection import sdk_fixture

    policy, proc, producer_session, inputs = native
    data = json.loads(policy.read_text())
    binary = policy.parent / "pi"
    dispatch = policy.parent / "dispatch"
    run = dispatch / "effect.fixture"
    dispatch.mkdir()
    data.update(node=str(binary), dispatch_root=str(dispatch))
    if legacy:
        (run / "session").mkdir(parents=True)
        (run / "session" / "2026_assigned-session.jsonl").write_text(
            json.dumps({"type": "session", "id": "assigned-session"}) + "\n")
    else:
        config = sdk_fixture(run)
        config.update(argv=[str(binary), config["argv"][1]],
                      sdk_entry=str(sdk_entries(binary)[0]), http_entry=str(sdk_entries(binary)[1]))
        (run / "launch-config.json").write_text(json.dumps(config))
        selected = config["selection"]
        rows = [
            {"type": "session", "id": config["native_session_id"]},
            {"type": "model_change", "provider": selected["provider"],
             "modelId": selected["model"]},
            {"type": "thinking_level_change", "thinkingLevel": selected["thinking"]},
            {"type": "custom", "customType": "harness.verification-compute/v1",
             "data": {"selection": selected, "runner_sha256": config["runner_sha256"],
                      "config_sha256": checksum((run / "launch-config.json").read_bytes())}},
        ]
        session = run / "session/fixture_assigned-session.jsonl"
        session.write_text("\n".join(map(json.dumps, rows)) + "\n")
        data["agent_config_dir"] = config["agent_dir"]
    data["profiles"].append({"role": "verifier", "actor": "independent",
                             "session_root": str(dispatch), "artifact_roots": [str(dispatch)]})
    producer = proc / "102"
    producer.mkdir()
    for name in ("status", "cmdline", "environ"):
        producer.joinpath(name).write_bytes((proc / "101" / name).read_bytes())
    if source not in {"pi_cli", "relay_session"}:
        as_source(data, policy.parent, producer, source, inputs)
    policy.write_text(json.dumps(data))
    title = "pi" if legacy else TITLE
    child = proc / "101"
    child.joinpath("status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t102\n")
    child.joinpath("cmdline").write_bytes(title.encode() + b"\0")
    child.joinpath("comm").write_text(title + "\n")
    child.joinpath("exe").symlink_to(binary)
    environment = b"HARNESS_REWRITE_DISPATCH=effect.fixture\0SCL_SESSION_REF=inherited\0"
    child.joinpath("stat").write_text(f"101 ({title}) S " + "0 " * 18 + "1234\n")
    boot = proc / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("boot-fixture\n")
    stamp = process_stamp(101, proc)
    receipt = json.dumps({
        "effect_id": "effect.fixture", "native_session_id": "assigned-session",
        "process": {**stamp, "started": "another process"} if forged else stamp})
    target = run
    if attempt:
        target = run / "attempts" / attempt
        target.mkdir(parents=True)
        (target / RUNNER).write_bytes(runner_bytes())
        (target / "launch-config.json").write_text(json.dumps({
            **config, "argv": [config["argv"][0], str(target / RUNNER)], "attempt": attempt,
            "session_file": str(session),
            "original_config_sha256": checksum((run / "launch-config.json").read_bytes())}))
        (target / "launch.json").write_bytes((run / "launch.json").read_bytes())
        (run / "spawn.json").write_text(json.dumps({
            "effect_id": "effect.fixture", "native_session_id": "assigned-session",
            "process": {"pid": 42, "start_ticks": "123", "boot": "old-boot"}}))
        environment += f"HARNESS_REWRITE_ATTEMPT={attempt}\0".encode()
    (target / "spawn.json").write_text(receipt)
    child.joinpath("environ").write_bytes(environment)
    if source == "relay_session":
        # A Relay-proven Agent session (telegram) located above the verifier.
        from harness_rewrite import session_sources

        def relay(policy_data, chain, proc_root, nearer_than=None, **kwargs):
            row = next((r for r in chain[:nearer_than] if r["pid"] == 102), None)
            return row and {"runtime": "pi", "entry": "telegram", "process": row,
                            "path": producer_session, "profile": policy_data["profiles"][0],
                            "header": {"type": "session", "id": "test-session"},
                            "environment": {}}
        monkeypatch.setattr(session_sources, "locate", relay)
    return policy, proc


def as_source(data, directory, process, kind, inputs):
    """Make `process` an Agent session source whose own check fails or passes:
    `external_entry` a confirmed entry launcher, `external_revoked` one whose binding
    the owner revoked after the dispatch, `cc_missing_root` a CC session whose log
    root is not available (resolve(strict=True) raises FileNotFoundError)."""
    if kind == "cc_missing_root":
        cc = directory / "claude"
        cc.write_text("fixture cc executable")
        data["profiles"].append({
            "runtime": "cc", "executable": str(cc), "role": "producer", "actor": "producer",
            "session_root": str(directory / "cc-sessions-unavailable"),
            "artifact_roots": [str(directory)]})
        process.joinpath("cmdline").write_bytes(
            b"\0".join(a.encode() for a in [str(cc), "--session-id", "cc-session"]))
        return
    from tests_rewrite.test_external_entry import OWNER, bind

    python, entry = directory / "python", directory / "harness.pyz"
    python.write_text("fixture interpreter")
    entry.write_text("fixture bundle")
    data.update(python=str(python), external_entries={
        "root": str(directory / "entries"), "owner": OWNER, "entries": ["chatgpt_web"]})
    binding_id, binding = bind(data, inputs)
    process.joinpath("cmdline").write_bytes(b"\0".join(a.encode() for a in [
        str(python), "-I", "-B", str(entry), "entry", binding_id, "--", "execute"]))
    process.joinpath("environ").write_bytes(b"")
    if kind == "external_revoked":
        binding.write_text(json.dumps(
            {**json.loads(binding.read_text()), "revoked_at": "2026-11-21T01:00:00Z"}))


LAUNCHES = [None, "000001"]  # First launch and continuation.


@pytest.mark.parametrize("attempt", LAUNCHES)
@pytest.mark.parametrize("source", ["pi_cli", "relay_session", "external_entry"])
def test_a_verifier_dispatched_inside_an_agent_session_is_the_verifier(
    native, monkeypatch, source, attempt
):
    """wi_20261120_666 (2026-11-21): the verifier ran `identity` while the producer's
    record_task_result was still running, so the producer's session source above it
    named the principal (actor pulse, entry telegram) and acknowledge was refused."""
    policy, proc = dispatched_under_producer(native, monkeypatch, source, attempt=attempt)
    host = NativeHost(policy, proc=proc, pid=101)
    assert host.base.actor == "independent" and host.entry == "cli"
    assert host.base.allowed_intents == {
        "acknowledge_verification", "record_verification", "record_quality"}
    # The producer's own invocation in the same tree stays the producer.
    assert NativeHost(policy, proc=proc, pid=102).base.actor == "producer"


@pytest.mark.parametrize("attempt", LAUNCHES)
@pytest.mark.parametrize(
    "source", ["pi_cli", "relay_session", "external_entry", "external_revoked"])
def test_a_producer_cannot_become_the_verifier_by_dispatch_environment(
    native, monkeypatch, source, attempt
):
    """A process below the producer naming a dispatch without its exact launch proof is
    refused, never admitted as the verifier."""
    policy, proc = dispatched_under_producer(
        native, monkeypatch, source, forged=True, attempt=attempt)
    with pytest.raises(Rejected, match="original launch"):
        NativeHost(policy, proc=proc, pid=101)


@pytest.mark.parametrize("source", ["pi_cli", "relay_session", "external_entry"])
def test_an_older_pi_dispatch_without_launch_configuration_does_not_replace_the_producer(
    native, monkeypatch, source
):
    """Only the SDK runner's full proof outranks the session source above it; an older
    Pi CLI process naming a dispatch, with no launch configuration, does not."""
    policy, proc = dispatched_under_producer(native, monkeypatch, source, legacy=True)
    assert NativeHost(policy, proc=proc, pid=101).base.actor == "producer"


OUTER_FAILURES = {"external_revoked": (Rejected, "revoked"),
                  "cc_missing_root": (FileNotFoundError, "cc-sessions-unavailable")}


@pytest.mark.parametrize("attempt", LAUNCHES)
@pytest.mark.parametrize("source", sorted(OUTER_FAILURES))
def test_a_source_failing_above_the_verifier_does_not_hide_its_dispatch(
    native, monkeypatch, source, attempt
):
    """Sources above the runner are never read: an entry binding revoked after the
    dispatch, or an Agent session whose log root is unavailable, still refuses its own
    process, never the dispatched verifier below it."""
    policy, proc = dispatched_under_producer(native, monkeypatch, source, attempt=attempt)
    host = NativeHost(policy, proc=proc, pid=101)
    assert host.base.actor == "independent" and host.entry == "cli"
    assert "acknowledge_verification" in host.base.allowed_intents
    error, match = OUTER_FAILURES[source]
    with pytest.raises(error, match=match):
        NativeHost(policy, proc=proc, pid=102)


@pytest.mark.parametrize("attempt", LAUNCHES)
@pytest.mark.parametrize("source", sorted(OUTER_FAILURES))
def test_a_source_failing_nearer_than_the_verifier_still_refuses(
    native, monkeypatch, source, attempt
):
    """A source nearer than the runner (pid 100, the verifier's own child) is read and
    proven; when its check fails, the invocation is refused, not handed to the runner."""
    policy, proc = dispatched_under_producer(native, monkeypatch, "pi_cli", attempt=attempt)
    data = json.loads(policy.read_text())
    nearer = proc / "100"
    nearer.mkdir()
    nearer.joinpath("status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t101\n")
    nearer.joinpath("environ").write_bytes(b"")
    as_source(data, policy.parent, nearer, source, native[3])
    policy.write_text(json.dumps(data))
    assert NativeHost(policy, proc=proc, pid=101).base.actor == "independent"
    error, match = OUTER_FAILURES[source]
    with pytest.raises(error, match=match):
        NativeHost(policy, proc=proc, pid=100)


@pytest.mark.parametrize("attempt", LAUNCHES)
@pytest.mark.parametrize("source", ["pi_cli", "relay_session", "external_entry"])
def test_a_missing_producer_log_root_does_not_refuse_the_dispatched_verifier(
    native, monkeypatch, source, attempt
):
    """A proven dispatch session matches only verifier profiles in the dispatch root:
    another Pi producer whose log directory is missing is not read for it, and a
    root that does not exist never refuses the producer's own session either."""
    policy, proc = dispatched_under_producer(native, monkeypatch, source, attempt=attempt)
    data = json.loads(policy.read_text())
    data["profiles"].append({
        "role": "producer", "actor": "elsewhere",
        "session_root": str(policy.parent / "pi-producer-sessions-missing"),
        "artifact_roots": [str(policy.parent)]})
    policy.write_text(json.dumps(data))
    host = NativeHost(policy, proc=proc, pid=101)
    assert host.base.actor == "independent" and host.profile["role"] == "verifier"
    assert NativeHost(policy, proc=proc, pid=102).base.actor == "producer"


def test_a_dispatch_session_needs_exactly_one_verifier_profile_holding_it(
    native, monkeypatch
):
    """The verifier profile still has to hold the dispatch session, and exactly one."""
    policy, proc = dispatched_under_producer(native, monkeypatch, "pi_cli")
    data = json.loads(policy.read_text())
    data["profiles"].append({**data["profiles"][-1], "actor": "second"})
    policy.write_text(json.dumps(data))
    with pytest.raises(Rejected, match="no unique admitted profile"):
        NativeHost(policy, proc=proc, pid=101)
    data["profiles"].pop()
    data["profiles"][-1]["session_root"] = str(policy.parent / "sessions")
    policy.write_text(json.dumps(data))
    with pytest.raises(Rejected, match="no unique admitted profile"):
        NativeHost(policy, proc=proc, pid=101)


def test_a_dispatched_verifier_does_not_inherit_the_dispatcher_session_proofs(monkeypatch):
    from harness_rewrite.pi_dispatch import dispatch_environment

    for key in ("PULSE_PI_TENANT_ID", "SCL_RELAY_HOST_CONTEXT", "SCL_SESSION_REF",
                "HARNESS_REWRITE_ATTEMPT"):
        monkeypatch.setenv(key, "dispatcher")
    monkeypatch.setenv("PATH", "/usr/bin")
    environment = dispatch_environment()
    assert environment["PATH"] == "/usr/bin"
    assert not {"PULSE_PI_TENANT_ID", "SCL_RELAY_HOST_CONTEXT", "SCL_SESSION_REF",
                "HARNESS_REWRITE_ATTEMPT"} & set(environment)
