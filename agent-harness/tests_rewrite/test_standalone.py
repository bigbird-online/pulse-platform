import json
import os
from types import SimpleNamespace

import pytest
from harness_rewrite.model import Rejected
from harness_rewrite.native_host import NativeHost
from harness_rewrite.policy import shape
from harness_rewrite.session_sources import original_user
from harness_rewrite import standalone
from harness_rewrite.standalone import uses_default_pi_route
from tests_rewrite.test_native_host import native as native


@pytest.mark.parametrize("arguments", [
    ["-c"], ["--resume"], ["--session=existing"], ["--fork", "existing"],
    ["--model", "openai-codex/example"], ["--provider=openai-codex"], ["--models", "*"],
    ["auth", "check"], ["list"], ["--export", "session.jsonl"], ["-h"],
])
def test_oracle_default_model_preserves_explicit_or_existing_selection(arguments):
    assert not uses_default_pi_route(arguments)


def test_oracle_default_model_does_not_parse_arguments_after_separator():
    assert uses_default_pi_route(["-p", "--", "--model is a prompt"])


def test_shared_codex_account_policy_requires_the_production_installation():
    dispatch = "/fixture/dispatch"
    account = {"credential_file": "/fixture/codex/auth.json", "codex": "/fixture/codex.js"}
    policy = {
        "namespace": "fixture", "surface": "fixture", "views_root": "/fixture/views",
        "delivery_root": "/fixture/delivery", "dispatch_root": dispatch, "verification_actor": "verifier",
        "python": "/fixture/python", "node": "/fixture/node", "pi_binary": "/fixture/pi",
        "profiles": [{"role": "verifier", "actor": "verifier", "session_root": dispatch,
                      "artifact_roots": [dispatch]}],
        "pi_codex_oauth": account,
    }
    with pytest.raises(ValueError, match="registered production installation"):
        shape({**policy, "database": "/fixture/facts.sqlite"})
    policy["production_ledger"] = {"repository": "/fixture/repo", "remote": "origin",
                                   "ref": "refs/heads/main", "identity": "fixture"}
    shape(policy)
    for override in ({"credential_file": "/fixture/other.json"}, {"codex": "relative"}):
        with pytest.raises(ValueError, match="Codex OAuth binding"):
            shape({**policy, "pi_codex_oauth": {**account, **override}})


@pytest.mark.parametrize("shared_codex", [False, True])
def test_standalone_launches_only_the_selected_extensions(tmp_path, monkeypatch, shared_codex):
    entry = tmp_path / "harness.pyz"
    relay = tmp_path / "Relay"
    runtime = relay / "runtime"
    runtime.mkdir(parents=True)
    work_item = runtime / "pi-work-item-core.mjs"
    oauth = runtime / "pi-codex-oauth.mjs"
    work_item.touch()
    policy = {
        "python": "/canonical/python", "producer_node": "/canonical/node", "pi_binary": "/canonical/pi",
        "relay_entry": str(relay / "dist/cli.js"), "profiles": [{"standalone": True, "binding_root": "/proofs"}],
        "verification_compute": {"provider": "openai-codex", "model": "gpt-6.1-sol", "thinking": "medium",
                                 "tools": ["read", "bash"]},
    }
    if shared_codex:
        oauth.touch()
        policy["pi_codex_oauth"] = {"credential_file": "/account/auth.json", "codex": "/canonical/codex"}
    (tmp_path / "host-policy.json").write_text(json.dumps(policy))
    launched = []
    def start(arguments, *, env):
        launched.append((arguments, env))
        return SimpleNamespace(wait=lambda **kwargs: 0)
    monkeypatch.setattr(standalone.subprocess, "Popen", start)
    monkeypatch.setenv("PULSE_PI_CODEX_OAUTH_HOST", "untrusted")
    assert standalone.run(entry, ["pi", "--", "--print", "hello"]) == 0
    expected = ["/canonical/node", "/canonical/pi", "--extension", str(work_item)]
    if shared_codex:
        expected += ["--extension", str(oauth), "--provider", "openai-codex", "--model", "gpt-6.1-sol"]
        assert json.loads(launched[0][1]["PULSE_PI_CODEX_OAUTH_HOST"]) == {"entrypoint": str(entry)}
    else:
        assert "PULSE_PI_CODEX_OAUTH_HOST" not in launched[0][1]
    assert launched[0][0] == expected + ["--print", "hello"]


def standalone_source(native, monkeypatch):
    from harness_rewrite import session_sources

    policy, proc, session, inputs = native
    value = json.loads(policy.read_text())
    node = policy.parent / "node"
    python = policy.parent / "python"
    entry = policy.parent / "harness.pyz"
    for binary in (node, python, entry):
        binary.touch()
    root = policy.parent / "binding"
    root.mkdir()
    value.update(python=str(python), producer_node=str(node))
    value["profiles"][0].update(
        standalone=True, binding_root=str(root), workspace_roots=[str(session.parent)]
    )
    policy.write_text(json.dumps(value))
    session.write_text(
        json.dumps({"type": "session", "id": "test-session", "cwd": str(session.parent)}) + "\n"
    )
    with session.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "message": {"role": "user", "content": "Original standalone approval"},
                }
            )
            + "\n"
        )
    parent = proc / "202"
    parent.mkdir()
    parent.joinpath("status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t1\n")
    parent.joinpath("cmdline").write_bytes(
        b"\0".join(s.encode() for s in [str(python), "-I", "-B", str(entry), "native-run", "pi"])
    )
    parent.joinpath("environ").write_bytes(b"")
    (proc / "101/status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t202\n")
    (proc / "101/cmdline").write_bytes(b"pi\0")
    (proc / "101/exe").symlink_to(node)
    monkeypatch.setattr(
        session_sources, "process_identity", lambda pid, _: {"pid": pid, "birth": "original"}
    )
    record = {
        "schema": "pulse.standalone-session-source/v1",
        "runtime": "pi",
        "session_id": "test-session",
        "launcher_pid": 202,
        "launcher_process": {"pid": 202, "birth": "original"},
        "process": {"pid": 101, "birth": "original"},
    }
    proof = root / "101.json"
    proof.write_text(json.dumps(record))
    proof.chmod(0o600)
    return policy, proc, session, proof


def test_original_standalone_identity_ignores_caller_tenant_and_preserves_human_source(
    native, monkeypatch
):
    policy, proc, session, proof = standalone_source(native, monkeypatch)
    host = NativeHost(policy, proc=proc, pid=101)
    assert host.base.actor == "producer" and host.environment == {}
    ref, raw = original_user(session, "pi", "test-session", None)
    assert host.human_input(ref, latest=True) == raw
    assert "checkpoint" in host.base.allowed_intents


@pytest.mark.parametrize(
    "fault", ["child_birth", "parent_birth", "command", "missing", "workspace", "permissions"]
)
def test_standalone_source_cannot_borrow_process_or_workspace(native, monkeypatch, fault):
    policy, proc, session, proof = standalone_source(native, monkeypatch)
    record = json.loads(proof.read_bytes())
    if fault == "child_birth":
        record["process"]["birth"] = "other"
        proof.write_text(json.dumps(record))
    elif fault == "parent_birth":
        record["launcher_process"]["birth"] = "other"
        proof.write_text(json.dumps(record))
    elif fault == "command":
        path = proc / "202/cmdline"
        path.write_bytes(path.read_bytes().replace(b"native-run", b"execute"))
    elif fault == "missing":
        proof.unlink()
    elif fault == "permissions":
        proof.chmod(0o644)
    else:
        value = json.loads(policy.read_bytes())
        value["profiles"][0]["workspace_roots"] = [str(policy.parent / "other")]
        policy.write_text(json.dumps(value))
    with pytest.raises(Rejected):
        NativeHost(policy, proc=proc, pid=101)
