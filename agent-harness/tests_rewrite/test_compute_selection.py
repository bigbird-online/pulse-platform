"""Source fixtures prove selection boundaries, never native ACK or UAT."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from harness_rewrite.native_compute import (
    RUNNER,
    checksum,
    runner_bytes,
    launch_config,
    session_proof,
    sdk_entries,
)
from harness_rewrite.policy import verification_compute
from harness_rewrite.native_host import NativeHost
from tests_rewrite.test_native_host import native  # noqa: F401
from tests_rewrite.test_dispatch import configured
from harness_rewrite.pi_dispatch import PiDispatch


@pytest.mark.parametrize(
    "patch",
    [
        {"provider": "openai*"},
        {"model": "model:high"},
        {"thinking": "ultra"},
        {"tools": []},
        {"tools": ["bash", "bash"]},
        {"tools": ["bash", "secret"]},
        {"tools": [True]},
        {"extra": True},
        {"provider": 3},
        {"tools": "bash"},
    ],
)
def test_closed_compute_configuration_rejects_silent_defaults(patch):
    selected = {**verification_compute({}), **patch}
    with pytest.raises(ValueError):
        verification_compute({"verification_compute": selected})


def test_selection_returns_independent_default_and_no_shell_overrides():
    first = verification_compute({})
    first["tools"].append("ls")
    assert verification_compute({})["tools"] == ["read", "bash", "edit", "write"]


def sdk_fixture(root, fault="none"):
    # Deliberate SDK double. Real node executes the actual bundled runner.
    root.mkdir(exist_ok=True)
    (root / "session").mkdir()
    (root / RUNNER).write_bytes(runner_bytes())
    transport = root / "fake-http.mjs"
    transport.write_text("""
export function parseHttpIdleTimeoutMs(value) {
  if (value === undefined) return undefined;
  return typeof value === "number" && value >= 0 ? value : undefined;
}
export function applyHttpProxySettings(value) {
  if (value?.trim()) {
    process.env.HTTP_PROXY ??= value.trim();
    process.env.HTTPS_PROXY ??= value.trim();
  }
}
export function configureHttpDispatcher() { globalThis.transportInitialized = true; }
""")
    sdk = root / "fake-sdk.mjs"
    sdk.write_text(
        """
import { appendFileSync, writeFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
const fault = FAULT;
export const SettingsManager = { inMemory: x => x };
export class DefaultResourceLoader {
  constructor(x) { if (!x.noExtensions || !x.noContextFiles) throw Error(); }
  async reload() {}
}
export const ModelRuntime = { create: async options => {
  if (options.allowModelNetwork || options.refreshOnCreate) throw Error();
  return {refresh: async ({allowNetwork,providers}) => {
    if (allowNetwork || providers.length !== 1) throw Error();
    return {aborted:false,errors:new Map()};
  }, getError: () => undefined, getModel: (provider,id) => fault === "missing" ? undefined :
    {provider, id: fault === "fallback" ? "other-model" : id}};
}};
export const SessionManager = { create: (root,dir,{id}) => {
  const file = join(dir,"fixture_"+id+".jsonl");
  writeFileSync(file, JSON.stringify({type:"session",id})+"\\n");
  const append = row => appendFileSync(file,JSON.stringify(row)+"\\n");
  return {append,appendCustomEntry: (customType,data) => append({type:"custom",customType,data})};
}, open: (file) => {
  const header = JSON.parse(readFileSync(file,"utf8").split("\\n")[0]);
  const append = row => appendFileSync(file,JSON.stringify(row)+"\\n");
  return {append, getSessionId:()=>header.id, getSessionFile:()=>file,
    appendCustomEntry:(customType,data)=>append({type:"custom",customType,data})};
}};
export async function createAgentSession(options) {
  const {model,thinkingLevel,tools,sessionManager} = options;
  sessionManager.append({type:"model_change", provider:model.provider, modelId:model.id});
  sessionManager.append({type:"thinking_level_change", thinkingLevel});
  return { session: {
    model,thinkingLevel: fault === "clamp" ? "off" : thinkingLevel,
    getActiveToolNames: () => fault === "extra-tool" ? [...tools,"ls"] : tools,
    subscribe(fn) { this.listener = fn; },dispose() {},
    async prompt() {
      if (!globalThis.transportInitialized) throw Error("transport must precede prompt");
      writeFileSync("model-invoked","yes");
      if (fault === "model-error") this.listener({
        type:"message_end",message:{role:"assistant",stopReason:"error",content:[]}
      });
    }
  }};
}
""".replace("FAULT", json.dumps(fault))
    )
    config = {
        "effect_id": "effect.fixture",
        "candidate_id": "candidate.fixture",
        "native_session_id": "assigned-session",
        "runner": "pi-sdk/v1",
        "runner_sha256": checksum(runner_bytes()),
        "argv": [shutil.which("node"), str(root / RUNNER)],
        "sdk_entry": str(sdk),
        "http_entry": str(transport),
        "agent_dir": str(root / "auth"),
        "selection": verification_compute({}),
    }
    (root / "launch-config.json").write_text(json.dumps(config))
    (root / "launch.json").write_text(
        json.dumps(
            {
                "effect_id": config["effect_id"],
                "candidate_id": config["candidate_id"],
                "phase": "launch_requested",
            }
        )
    )
    (root / "candidate.json").write_text(
        json.dumps({"candidate_id": config["candidate_id"]})
    )
    (root / "instructions.txt").write_text("This fixture must not access a model.")
    return config


@pytest.mark.parametrize(
    "fault", ["none", "missing", "fallback", "clamp", "extra-tool"]
)
def test_actual_runner_refuses_model_fallback_clamp_and_extra_tools(tmp_path, fault):
    if not shutil.which("node"):
        pytest.skip("Node is needed for the installed SDK adapter test")
    root = tmp_path.resolve()
    config = sdk_fixture(root, fault)
    result = subprocess.run(
        config["argv"],
        cwd=root,
        env={
            **os.environ,
            "HARNESS_REWRITE_DISPATCH": config["effect_id"],
        },
        capture_output=True,
        timeout=20,
    )
    receipt = json.loads((root / "runner-result.json").read_bytes())
    assert result.stderr == b""
    if fault == "none":
        assert result.returncode == 0 and receipt["model_invoked"]
        session_proof(
            root, root / "session/fixture_assigned-session.jsonl", launch_config(root)
        )
    else:
        assert result.returncode == 2 and receipt["phase"] == "pre_model_failure"
        assert receipt["model_invoked"] is False
        assert not (root / "model-invoked").exists()


def test_missing_submission_tool_does_not_start_process_or_create_intention(
    tmp_path, monkeypatch
):
    rig, _, worker = configured(tmp_path)
    selected = verification_compute({})
    selected["tools"] = ["read"]
    root = tmp_path / "dispatch"
    root.mkdir()
    policy = {
        "dispatch_root": str(root),
        "delivery_root": str(tmp_path),
        "verification_compute": selected,
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("No model should start")

    monkeypatch.setattr("harness_rewrite.pi_dispatch.subprocess.Popen", forbidden)
    adapter = PiDispatch(rig.kernel, policy, worker, rig.wi, tmp_path / "bundle")
    assert adapter.apply(rig.state(handle=worker(()))["effects"][0], b"") == (
        "failure",
        {"reason": "verifier_tools_cannot_submit_native_results"},
    )
    assert not list(root.iterdir())


@pytest.mark.parametrize(
    "tamper",
    [None, "runner", "selection", "session", "boot", "cli-title", "transport", "executable"],
)
@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_sdk_native_admission_requires_original_runner_process_and_selection(
    request, tamper, continuation, platform
):
    from harness_rewrite.model import Rejected
    from harness_rewrite.runtime_presence import process_stamp

    policy_path, proc, _, _ = request.getfixturevalue("native")
    policy = json.loads(policy_path.read_bytes())
    dispatch = policy_path.parent / "dispatch"
    root = dispatch / "effect.fixture"
    root.mkdir(parents=True)
    config = sdk_fixture(root)
    node = policy_path.parent / "pi"
    config["argv"][0] = str(node)
    config["sdk_entry"] = str(sdk_entries(node)[0])
    config["http_entry"] = str(sdk_entries(node)[1])
    policy.update(
        node=str(node),
        dispatch_root=str(dispatch),
        agent_config_dir=config["agent_dir"],
    )
    policy["profiles"].append(
        {
            "actor": "verifier",
            "role": "verifier",
            "session_root": str(dispatch),
            "artifact_roots": [str(dispatch)],
        }
    )
    policy_path.write_text(json.dumps(policy))
    (root / "launch-config.json").write_text(json.dumps(config))
    selected = config["selection"]
    rows = [
        {"type": "session", "id": config["native_session_id"]},
        {
            "type": "model_change",
            "provider": selected["provider"],
            "modelId": selected["model"],
        },
        {"type": "thinking_level_change", "thinkingLevel": selected["thinking"]},
        {
            "type": "custom",
            "customType": "harness.verification-compute/v1",
            "data": {
                "selection": selected,
                "runner_sha256": config["runner_sha256"],
                "config_sha256": checksum((root / "launch-config.json").read_bytes()),
            },
        },
    ]
    session = root / "session/fixture_assigned-session.jsonl"
    session.write_text("\n".join(map(json.dumps, rows)) + "\n")
    child = proc / "101"
    child.joinpath("cmdline").write_bytes(b"harness-pi-v1\0")
    child.joinpath("comm").write_text("harness-pi-v1\n")
    child.joinpath("exe").symlink_to(node)
    child.joinpath("environ").write_bytes(b"HARNESS_REWRITE_DISPATCH=effect.fixture\0")
    child.joinpath("stat").write_text("101 (harness-pi-v1) S " + "0 " * 18 + "1234\n")
    boot = proc / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("boot-original")
    (root / "spawn.json").write_text(
        json.dumps(
            {
                "effect_id": config["effect_id"],
                "native_session_id": config["native_session_id"],
                "process": process_stamp(101, proc),
            }
        )
    )
    if continuation:
        target = root / "attempts/000001"
        target.mkdir(parents=True)
        (target / RUNNER).write_bytes(runner_bytes())
        (target / "launch-config.json").write_text(json.dumps({**config,
            "argv": [config["argv"][0], str(target / RUNNER)], "attempt": "000001",
            "session_file": str(session),
            "original_config_sha256": checksum((root / "launch-config.json").read_bytes())}))
        (target / "launch.json").write_bytes((root / "launch.json").read_bytes())
        (target / "spawn.json").write_bytes((root / "spawn.json").read_bytes())
        child.joinpath("environ").write_bytes(b"HARNESS_REWRITE_DISPATCH=effect.fixture\0HARNESS_REWRITE_ATTEMPT=000001\0")
    if tamper == "transport":
        config["http_entry"] = "/unadmitted/http.mjs"
        (root / "launch-config.json").write_text(json.dumps(config))
    elif tamper == "cli-title":
        child.joinpath("comm").write_text("pi\n")
    elif tamper == "runner":
        (root / RUNNER).write_bytes(runner_bytes() + b"\n// substituted")
    elif tamper == "selection":
        config["selection"]["model"] = "other"
        (root / "launch-config.json").write_text(json.dumps(config))
    elif tamper == "session":
        rows[1]["modelId"] = "other"
        session.write_text("\n".join(map(json.dumps, rows)) + "\n")
    elif tamper == "boot":
        boot.write_text("reused-pid")
    elif tamper == "executable":
        other = policy_path.parent / "other-node"
        other.write_text("not the admitted node")
        child.joinpath("exe").unlink()
        child.joinpath("exe").symlink_to(other)
    if platform == "macos":
        macos_process(request.getfixturevalue("monkeypatch"), proc, child)
    if tamper:
        with pytest.raises(Rejected):
            NativeHost(policy_path, proc=proc, pid=101)
    else:
        host = NativeHost(policy_path, proc=proc, pid=101)
        assert host.base.actor == "verifier"
        assert host.base.allowed_intents == {
            "acknowledge_verification",
            "record_verification",
            "record_quality",
        }


def procargs_bytes(title: str, environ: bytes, argc: int = 4) -> bytes:
    """KERN_PROCARGS2 after Node's process.title blanked the rest of argv."""
    return (
        argc.to_bytes(4, sys.byteorder)
        + b"/opt/node/bin/node\0\0\0\0"
        + title.encode()
        + b"\0" * 40
        + environ
        + b"executable_path=/opt/node/bin/node\0\0"
    )


def macos_process(monkeypatch, proc, child):
    """Replace procfs with the macOS readers: no comm, environ or exe remains."""
    from harness_rewrite import native_host

    chain = native_host.process_chain(int(child.name), proc)
    raw = procargs_bytes(
        child.joinpath("comm").read_text().strip(), child.joinpath("environ").read_bytes()
    )
    executable = child.joinpath("exe").resolve(strict=True)
    for name in ("comm", "environ", "exe"):
        child.joinpath(name).unlink()

    def procargs(pid):
        if pid != int(child.name):
            raise OSError("no such process")
        return raw

    monkeypatch.setattr(native_host, "procfs_absent", lambda path: path == proc)
    monkeypatch.setattr(native_host, "process_chain", lambda pid, path: chain)
    monkeypatch.setattr(native_host, "procargs", procargs)
    monkeypatch.setattr(native_host, "pidpath", lambda pid: executable)


def test_macos_procargs_select_title_and_environment_only():
    from harness_rewrite.native_host import procargs_fields

    title, environment = procargs_fields(
        procargs_bytes(
            "harness-pi-v1",
            b"HOME=/x\0HARNESS_REWRITE_DISPATCH=op_a\0HARNESS_REWRITE_ATTEMPT=000001\0",
        )
    )
    assert title == "harness-pi-v1"
    assert environment == {
        "HARNESS_REWRITE_DISPATCH": "op_a",
        "HARNESS_REWRITE_ATTEMPT": "000001",
    }
    with pytest.raises(ValueError):
        procargs_fields(
            procargs_bytes(
                "harness-pi-v1",
                b"HARNESS_REWRITE_DISPATCH=op_a\0HARNESS_REWRITE_DISPATCH=op_b\0",
            )
        )
    for raw in (b"", (1).to_bytes(4, sys.byteorder) + b"/opt/node\0\0\0"):
        with pytest.raises(ValueError):
            procargs_fields(raw)


def test_sdk_child_stamp_is_durable_before_exact_exec(tmp_path, monkeypatch):
    from harness_rewrite import native_launch

    root = tmp_path.resolve()
    config = sdk_fixture(root)
    monkeypatch.chdir(root)
    monkeypatch.setenv("HARNESS_REWRITE_DISPATCH", config["effect_id"])
    stamp = {"pid": 123, "start_ticks": "1234", "boot": "fixture"}
    monkeypatch.setattr(native_launch, "process_stamp", lambda pid: stamp)

    def exec_fixture(exe, argv, environment):
        assert json.loads((root / "spawn.json").read_bytes())["process"] == stamp
        assert argv == config["argv"] and exe == argv[0]
        raise OSError("fixture exec boundary")

    monkeypatch.setattr(native_launch.os, "execve", exec_fixture)
    with pytest.raises(OSError, match="fixture exec"):
        native_launch.launch(root)
    with pytest.raises(FileExistsError):
        native_launch.launch(root)


@pytest.mark.parametrize("extra", [False, True])
def test_bundle_requires_exact_runner_and_rejects_other_javascript(tmp_path, extra):
    import zipfile
    from harness_rewrite.release import verify

    files = {"harness_rewrite/native_compute.py": b"# fixture"}
    if extra:
        files["harness_rewrite/verifier-runner.mjs"] = runner_bytes()
        files["harness_rewrite/unreviewed.mjs"] = b"throw Error()"
    bundle = tmp_path / "bad.pyz"
    with zipfile.ZipFile(bundle, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        archive.writestr(
            "build.json",
            json.dumps(
                {
                    "schema": "harness.rewrite_bundle/v1",
                    "files": {name: checksum(value) for name, value in files.items()},
                }
            ),
        )
    with pytest.raises(ValueError):
        verify(bundle)


def test_resolved_sdk_prompt_with_model_error_is_not_success(tmp_path):
    config = sdk_fixture(tmp_path, "model-error")
    result = subprocess.run(
        config["argv"],
        cwd=tmp_path,
        env={**os.environ, "HARNESS_REWRITE_DISPATCH": config["effect_id"]},
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert json.loads((tmp_path / "runner-result.json").read_bytes()) == {
        "phase": "model_interrupted",
        "reason": "native_model_error",
        "model_invoked": True,
    }
    assert (tmp_path / "model-invoked").exists()


@pytest.mark.parametrize(
    "fault", ["missing-module", "bad-proxy", "bad-timeout", "settings-symlink"]
)
def test_transport_failure_never_calls_model_or_edits_settings(tmp_path, fault):
    config = sdk_fixture(tmp_path)
    agent = tmp_path / "auth"
    agent.mkdir()
    settings = agent / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "httpProxy": 12 if fault == "bad-proxy" else "http://fixture.invalid",
                "httpIdleTimeoutMs": "invalid" if fault == "bad-timeout" else 5000,
            }
        )
    )
    if fault == "missing-module":
        Path(config["http_entry"]).unlink()
    if fault == "settings-symlink":
        original = agent / "original.json"
        settings.rename(original)
        settings.symlink_to(original)
    before = settings.read_bytes()
    result = subprocess.run(
        config["argv"],
        cwd=tmp_path,
        env={**os.environ, "HARNESS_REWRITE_DISPATCH": config["effect_id"]},
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert json.loads((tmp_path / "runner-result.json").read_bytes()) == {
        "phase": "pre_model_failure",
        "reason": "runtime_transport_unavailable",
        "model_invoked": False,
    }
    assert settings.read_bytes() == before
    assert not (tmp_path / "model-invoked").exists()


def test_existing_network_settings_are_selected_without_disclosing_or_writing(tmp_path):
    config = sdk_fixture(tmp_path)
    agent = tmp_path / "auth"
    agent.mkdir()
    settings = agent / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "httpProxy": "http://private-proxy.invalid:1234",
                "httpIdleTimeoutMs": 5000,
                "unrelated_private_setting": "must-not-appear",
            }
        )
    )
    before = settings.read_bytes()
    result = subprocess.run(
        config["argv"],
        cwd=tmp_path,
        env={
            **os.environ,
            "HTTPS_PROXY": "http://environment-first.invalid",
            "HARNESS_REWRITE_DISPATCH": config["effect_id"],
        },
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0 and settings.read_bytes() == before
    proof = json.loads((tmp_path / "transport.json").read_bytes())
    assert proof["initialized"] and proof["proxy_from_operator_settings"]
    assert proof["proxy_environment_already_present"]
    assert proof["settings_sha256"] == checksum(before)
    assert b"private-proxy" not in (tmp_path / "transport.json").read_bytes()
    assert b"must-not-appear" not in result.stdout + result.stderr


def test_exact_http_module_paths_for_bundled_and_unbundled_installed_pi(tmp_path):
    plain = tmp_path / "dist/cli.js"
    bundled = tmp_path / "dist/bundle/cli.js"
    assert sdk_entries(plain) == (
        plain.parent / "index.js",
        plain.parent / "core/http-dispatcher.js",
    )
    assert sdk_entries(bundled) == (
        bundled.parent / "index.js",
        plain.parent / "core/http-dispatcher.js",
    )
