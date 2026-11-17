from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "pulse-runtime-priority.py"




def test_default_paths_keep_outer_checkout_and_flat_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("runtime_priority_paths", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    pulse_root = Path("/Users/owner/Coevolution/Bigbird/PULSE")
    monkeypatch.setattr(module, "__file__", str(pulse_root / "ComputeCost/bin/pulse-runtime-priority.py"))
    monkeypatch.delenv("PULSE_RUNTIME_PULSE_ROOT", raising=False)
    monkeypatch.delenv("PULSE_RUNTIME_HARNESS_ROOT", raising=False)

    paths = module._paths()

    assert paths["pulse_root"] == pulse_root
    assert paths["policies"] == Path("/Users/owner/Coevolution/.harness/policies")
    assert paths["compute_profiles"] == pulse_root / "Relay/config/compute-profiles.json"






@pytest.fixture
def managed_tree(tmp_path: Path) -> dict[str, Path | dict[str, str]]:
    pulse = tmp_path / "PULSE"
    data = pulse / "data"
    policies = tmp_path / ".harness" / "policies"
    compute_profiles = pulse / "Relay" / "config"
    backups = tmp_path / "backups"
    codex = tmp_path / "config.toml"
    relay_state = tmp_path / "state.json"
    relay_pid = tmp_path / "relay.pid"
    legacy_relay = tmp_path / ".relay" / "relay.json"
    cost_presets = tmp_path / "runtime-cost-presets.json"
    data.mkdir(parents=True)
    policies.mkdir(parents=True)
    compute_profiles.mkdir(parents=True)
    legacy_relay.parent.mkdir(parents=True)
    relay = {
        "users": [{
            "userId": "owner",
            "defaultAgent": "pulse",
            "agents": [{
                "id": "pulse",
                "name": "Pulse",
                "emoji": "P",
                "workspace": str(tmp_path),
                "botToken": "secret",
                "runtime": "cc",
            }],
        }],
        "bindings": [],
    }
    cron = {
        "version": 2,
        "jobs": [
            {
                "id": "direct",
                "agent": {"workspace": str(tmp_path), "prompt": "run"},
                "execution": {"mode": "runtime"},
            },
            {
                "id": "dispatch",
                "agent": {"workspace": str(tmp_path), "prompt": "scan"},
                "execution": {"mode": "harness_dispatch"},
            },
            {
                "id": "hestia-vultr-weekly-review-20260717",
                "schedule": {"expr": "0 22 * * 0", "tz": "Asia/Shanghai"},
                "agent": {"workspace": str(tmp_path), "prompt": "weekly"},
                "execution": None,
            },
            {
                "id": "hestia-19f9254e40c",
                "agent": {
                    "workspace": str(tmp_path),
                    "prompt": "gate",
                    "computeProfile": "media",
                    "gate": {"command": "/fake/gate", "skipExitCode": 3},
                },
                "execution": None,
            },
        ],
    }
    policy = """schema_version: \"1.0\"
agent_id: pulse
workspace: /tmp
scan_items:
  - type: pending_handoffs
    compute_profile: economy
    default_autonomy: autonomous_llm
  - type: unresolved_violations
    default_autonomy: mechanical
  - type: stale_work_items
    default_autonomy: autonomous_llm
autonomous_llm_config:
  runtime: claude
  fallback_runtimes: [legacy_runtime]
  compute_profile: old_profile
  codex_model: old-codex
  codex_reasoning_effort: low
  codex_yolo: false
  claude_model: old-cc
  allowed_tools: [Read]
  path_constraints:
    read: []
    write: []
escalation:
  push_immediately: []
  aggregate_daily: []
"""
    (data / "relay.json").write_text(json.dumps(relay) + "\n")
    (data / "cron-jobs.json").write_text(json.dumps(cron) + "\n")
    (compute_profiles / "compute-profiles.json").write_text(json.dumps({
        "schema_version": 2,
        "profiles": {
            "platform_default": {
                "cc": {"model": "old-cc"},
                "codex": {"model_alias": "codex-luna-latest", "reasoning_effort": "low"},
            },
            "media": {
                "cc": {"model_alias": "claude-opus-latest", "reasoning_effort": "xhigh"},
                "codex": {"model_alias": "codex-sol-latest", "reasoning_effort": "xhigh"},
            },
            "frontier": {
                "cc": {"model_alias": "claude-haiku-latest", "reasoning_effort": "low"},
                "codex": {"model_alias": "codex-luna-latest", "reasoning_effort": "low"},
            },
        },
    }) + "\n")
    cost_presets.write_text(
        (SCRIPT.parent.parent / "config/runtime-cost-presets.json").read_text()
    )
    (policies / "pulse.yaml").write_text(policy)
    relay_state.write_text(json.dumps({
        "version": 1,
        "updatedAt": "2026-08-15T00:00:00Z",
        "sessions": {
            "owner:pulse": {
                "poolKey": "owner:pulse",
                "agentId": "pulse",
                "runtime": "codex",
                "sessionId": "session",
                "turnCount": 0,
                "maxTurns": 100,
                "lastActivity": 0,
                "status": "active",
            },
        },
    }) + "\n")
    legacy_relay.write_text("legacy relay config\n")
    codex.write_text(
        'model = "old"\nmodel_reasoning_effort = "medium"\n\n'
        '[agents]\ndefault_subagent_reasoning_effort = "high"\n'
        'default_subagent_model = "gpt-old"\n\n[features]\napps = false\n'
    )
    env = os.environ.copy()
    env.update({
        "PULSE_RUNTIME_PULSE_ROOT": str(pulse),
        "PULSE_RUNTIME_HARNESS_ROOT": str(policies.parent),
        "PULSE_RUNTIME_CODEX_CONFIG": str(codex),
        "PULSE_RUNTIME_BACKUP_ROOT": str(backups),
        "PULSE_RUNTIME_RELAY_STATE": str(relay_state),
        # Reload tests must never inherit a live Relay PID from the test runner.
        "PULSE_RUNTIME_RELAY_PID_PATH": str(relay_pid),
        "PULSE_RUNTIME_LEGACY_RELAY_CONFIG": str(legacy_relay),
        "PULSE_RUNTIME_COST_PRESETS": str(cost_presets),
    })
    return {
        "pulse": pulse,
        "policies": policies,
        "backups": backups,
        "codex": codex,
        "relay_state": relay_state,
        "legacy_relay": legacy_relay,
        "cost_presets": cost_presets,
        "env": env,
    }


def run_manager(tree: dict[str, Path | dict[str, str]], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=tree["env"],  # type: ignore[arg-type]
        text=True,
        capture_output=True,
        check=False,
    )


def structured_plan(
    tree: dict[str, Path | dict[str, str]], *profile_args: str
) -> dict[str, object]:
    result = run_manager(tree, "plan", *profile_args, "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def guarded_preset_apply(
    tree: dict[str, Path | dict[str, str]], preset: str
) -> subprocess.CompletedProcess[str]:
    plan = structured_plan(tree, "--preset", preset)
    return run_manager(
        tree,
        "apply",
        "--preset",
        preset,
        "--expected-active-route-digest",
        str(plan["active_route_digest"]),
        "--expected-plan-digest",
        str(plan["plan_digest"]),
    )


def load_manager_module():
    spec = importlib.util.spec_from_file_location("pulse_runtime_priority_test_module", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def backend_media_managed_bytes(
    tree: dict[str, Path | dict[str, str]],
) -> dict[Path, bytes]:
    pulse = tree["pulse"]
    paths = [pulse / "data/cron-jobs.json", *sorted(tree["policies"].glob("*.yaml"))]  # type: ignore[operator,union-attr]
    return {path: path.read_bytes() for path in paths}


def test_missing_profile_uses_deepseek_cc_default_and_fable_desired_profile_fails_closed(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    pulse = Path(managed_tree["pulse"])
    profile_path = pulse / "data/runtime-priority.json"

    plan = run_manager(managed_tree, "plan", "--codex-yolo", "on")
    assert plan.returncode == 0, plan.stderr
    assert "claude=deepseek/deepseek-v4-flash-0731" in plan.stdout
    assert "fable" not in plan.stdout.lower()

    blocked = run_manager(
        managed_tree,
        "plan",
        "--claude-model",
        "claude-fable-5",
    )
    assert blocked.returncode == 2
    assert "Fable is disabled" in blocked.stderr
    assert not profile_path.exists()


def test_plan_is_read_only_and_apply_converges(managed_tree: dict[str, Path | dict[str, str]]) -> None:
    pulse = managed_tree["pulse"]
    profile_path = pulse / "data/runtime-priority.json"  # type: ignore[operator]
    before = (pulse / "data/relay.json").read_bytes()  # type: ignore[operator]

    plan = run_manager(managed_tree, "plan", "--codex-yolo", "on")
    assert plan.returncode == 0, plan.stderr
    assert "PLAN ONLY" in plan.stdout
    assert not profile_path.exists()
    assert (pulse / "data/relay.json").read_bytes() == before  # type: ignore[operator]

    apply = run_manager(managed_tree, "apply", "--codex-yolo", "on")
    assert apply.returncode == 0, apply.stderr
    assert "APPLY OK" in apply.stdout
    verify = run_manager(managed_tree, "verify")
    assert verify.returncode == 0, verify.stderr
    assert "VERIFY OK" in verify.stdout
    assert "mismatched=0" in verify.stdout

    relay = json.loads((pulse / "data/relay.json").read_text())  # type: ignore[operator]
    agent = relay["users"][0]["agents"][0]
    assert agent["runtime"] == "codex"
    assert agent["conversationRuntimePreference"] == ["codex"]
    assert agent["codexYoloDefault"] is True

    cron = json.loads((pulse / "data/cron-jobs.json").read_text())  # type: ignore[operator]
    assert cron["jobs"][0]["agent"]["codexYolo"] is True
    assert cron["jobs"][0]["agent"]["computeProfile"] == "platform_default"
    assert "codexModel" not in cron["jobs"][0]["agent"]
    assert "codexReasoningEffort" not in cron["jobs"][0]["agent"]
    assert "ccModel" not in cron["jobs"][0]["agent"]
    assert "codexYolo" not in cron["jobs"][1]["agent"]

    profiles = json.loads(
        (pulse / "Relay/config/compute-profiles.json").read_text()  # type: ignore[operator]
    )
    assert profiles["profiles"]["platform_default"]["codex"] == {
        "model_alias": "codex-sol-latest",
        "reasoning_effort": "xhigh",
    }
    assert profiles["profiles"]["frontier"] == {
        "cc": {"model_alias": "claude-opus-latest", "reasoning_effort": "high"},
        "codex": {"model_alias": "codex-sol-latest", "reasoning_effort": "high"},
    }
    assert profiles["profiles"]["media"] == {
        "cc": {"model_alias": "claude-sonnet-latest", "reasoning_effort": "medium"},
        "codex": {"model_alias": "codex-terra-latest", "reasoning_effort": "medium"},
    }
    assert profiles["profiles"]["economy"] == {
        "cc": {"model_alias": "claude-haiku-latest", "reasoning_effort": "low"},
        "codex": {"model_alias": "codex-luna-latest", "reasoning_effort": "low"},
    }

    cfg = yaml.safe_load((managed_tree["policies"] / "pulse.yaml").read_text())  # type: ignore[operator]
    llm_config = cfg["autonomous_llm_config"]
    assert llm_config["runtime"] == "codex"
    assert llm_config["fallback_runtimes"] == []
    assert llm_config["compute_profile"] == "platform_default"
    assert llm_config["codex_yolo"] is True
    assert not {
        "codex_model",
        "codex_reasoning_effort",
        "claude_model",
    }.intersection(llm_config)
    per_type = {item["type"]: item for item in cfg["scan_items"]}
    assert per_type["pending_handoffs"]["compute_profile"] == "frontier"
    assert per_type["stale_work_items"]["compute_profile"] == "frontier"
    assert "compute_profile" not in per_type["unresolved_violations"]
    codex_text = managed_tree["codex"].read_text()  # type: ignore[union-attr]
    assert 'sandbox_mode = "danger-full-access"' in codex_text
    assert 'approval_policy = "never"' in codex_text
    assert "default_subagent_reasoning_effort" not in codex_text
    assert "default_subagent_model" not in codex_text
    route = json.loads(profile_path.with_name("runtime-cost-route.json").read_text())
    assert route["preset"] == "current_safe"
    assert route["routes"]["stats_quota_probe"]["effective_route"] == {
        "profile": "platform_default"
    }
    weekly = next(job for job in cron["jobs"] if job["id"] == "hestia-vultr-weekly-review-20260717")
    assert weekly["schedule"] == {"expr": "0 16 * * 0", "tz": "Asia/Shanghai"}
    assert weekly["agent"]["computeProfile"] == "platform_default"
    assert weekly["agent"]["expectArtifact"]["path"] == "memo/review-${ISO_WEEK}-draft.md"
    assert weekly["agent"]["expectArtifact"]["minBytes"] == 2000
    legacy_relay = managed_tree["legacy_relay"]
    assert legacy_relay.is_symlink()  # type: ignore[union-attr]
    assert legacy_relay.resolve() == (pulse / "data/relay.json").resolve()  # type: ignore[union-attr,operator]


def test_rollback_restores_exact_originals(managed_tree: dict[str, Path | dict[str, str]]) -> None:
    pulse = managed_tree["pulse"]
    relay_path = pulse / "data/relay.json"  # type: ignore[operator]
    original = relay_path.read_bytes()
    legacy_relay = managed_tree["legacy_relay"]
    legacy_original = legacy_relay.read_bytes()  # type: ignore[union-attr]
    apply = run_manager(managed_tree, "apply")
    assert apply.returncode == 0, apply.stderr
    backup_id = apply.stdout.split("backup=", 1)[1].splitlines()[0]

    rollback = run_manager(managed_tree, "rollback", backup_id)
    assert rollback.returncode == 0, rollback.stderr
    assert relay_path.read_bytes() == original
    assert not (pulse / "data/runtime-priority.json").exists()  # type: ignore[operator]
    assert not legacy_relay.is_symlink()  # type: ignore[union-attr]
    assert legacy_relay.read_bytes() == legacy_original  # type: ignore[union-attr]


def test_backend_media_uat_is_narrow_and_rolls_back_exactly(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    """@anchor test backend-media-uat-narrow-overlay-and-exact-rollback"""
    pulse = managed_tree["pulse"]
    cron_path = pulse / "data/cron-jobs.json"  # type: ignore[operator]
    policies = managed_tree["policies"]
    policy_path = policies / "pulse.yaml"  # type: ignore[operator]
    original_cron = cron_path.read_bytes()
    original_policy = policy_path.read_bytes()

    plan = run_manager(managed_tree, "backend-media-uat-plan")
    assert plan.returncode == 0, plan.stderr
    assert cron_path.read_bytes() == original_cron
    assert policy_path.read_bytes() == original_policy

    apply = run_manager(managed_tree, "backend-media-uat-apply")
    assert apply.returncode == 0, apply.stderr
    backup_id = apply.stdout.split("backup=", 1)[1].splitlines()[0]
    cron = json.loads(cron_path.read_text())
    for job in cron["jobs"]:
        if (job.get("execution") or {}).get("mode") != "harness_dispatch":
            assert job["agent"]["computeProfile"] == "media"
    policy = yaml.safe_load(policy_path.read_text())
    assert policy["autonomous_llm_config"]["compute_profile"] == "media"
    per_type = {item["type"]: item for item in policy["scan_items"]}
    assert per_type["pending_handoffs"]["compute_profile"] == "media"
    assert per_type["stale_work_items"]["compute_profile"] == "media"
    assert run_manager(managed_tree, "backend-media-uat-verify").returncode == 0

    rollback = run_manager(managed_tree, "backend-media-uat-rollback", backup_id)
    assert rollback.returncode == 0, rollback.stderr
    assert cron_path.read_bytes() == original_cron
    assert policy_path.read_bytes() == original_policy


@pytest.mark.parametrize(
    "foreign_prefix",
    ["apply", "ordinary-runtime", "other-special"],
)
def test_backend_media_uat_rollback_rejects_foreign_snapshot_types_without_writes(
    managed_tree: dict[str, Path | dict[str, str]],
    foreign_prefix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_manager_module()
    for key, value in managed_tree["env"].items():  # type: ignore[union-attr]
        monkeypatch.setenv(key, value)
    paths = module._paths()
    foreign = module._backup(
        paths,
        module._backend_media_uat_managed_paths(paths),
        prefix=foreign_prefix,
    )
    before = backend_media_managed_bytes(managed_tree)

    rollback = run_manager(
        managed_tree,
        "backend-media-uat-rollback",
        foreign.name,
    )

    assert rollback.returncode == 2
    assert backend_media_managed_bytes(managed_tree) == before
    assert not list(
        managed_tree["backups"].glob("pre-backend-media-uat-rollback-*")  # type: ignore[union-attr]
    )


@pytest.mark.parametrize(
    ("tamper", "use_latest"),
    [
        ("snapshot_type", True),
        ("backup_id", False),
        ("missing_entry", False),
        ("extra_entry", False),
        ("duplicate_entry", False),
        ("target_escape", False),
        ("backup_escape", False),
        ("non_file_entry", False),
        ("bad_checksum", False),
    ],
)
def test_backend_media_uat_rollback_validates_complete_snapshot_before_writes(
    managed_tree: dict[str, Path | dict[str, str]],
    tamper: str,
    use_latest: bool,
) -> None:
    apply = run_manager(managed_tree, "backend-media-uat-apply")
    assert apply.returncode == 0, apply.stderr
    backup_id = apply.stdout.split("backup=", 1)[1].splitlines()[0]
    backup = managed_tree["backups"] / backup_id  # type: ignore[operator]
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    if tamper == "snapshot_type":
        manifest["snapshot_type"] = "ordinary-runtime"
    elif tamper == "backup_id":
        manifest["backup_id"] = f"{backup_id}-forged"
    elif tamper == "missing_entry":
        manifest["entries"].pop()
    elif tamper == "extra_entry":
        extra = deepcopy(manifest["entries"][0])
        extra["path"] = str(managed_tree["pulse"] / "data/not-managed.json")  # type: ignore[operator]
        extra["backup"] = "999-not-managed.json"
        (backup / extra["backup"]).write_bytes((backup / manifest["entries"][0]["backup"]).read_bytes())
        manifest["entries"].append(extra)
    elif tamper == "duplicate_entry":
        manifest["entries"].append(deepcopy(manifest["entries"][0]))
    elif tamper == "target_escape":
        manifest["entries"][0]["path"] = str(
            managed_tree["pulse"] / "data/../outside.json"  # type: ignore[operator]
        )
    elif tamper == "backup_escape":
        manifest["entries"][0]["backup"] = "../manifest.json"
    elif tamper == "non_file_entry":
        manifest["entries"][0]["existed"] = False
    elif tamper == "bad_checksum":
        manifest["entries"][0]["sha256"] = hashlib.sha256(b"forged").hexdigest()
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(tamper)
    manifest_path.write_text(json.dumps(manifest) + "\n")
    before = backend_media_managed_bytes(managed_tree)

    args = ["backend-media-uat-rollback"]
    if not use_latest:
        args.append(backup_id)
    rollback = run_manager(managed_tree, *args)

    assert rollback.returncode == 2
    assert backend_media_managed_bytes(managed_tree) == before
    assert not list(
        managed_tree["backups"].glob("pre-backend-media-uat-rollback-*")  # type: ignore[union-attr]
    )


def test_backend_media_uat_verify_fails_closed_for_invalid_media_catalog(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    """@anchor test backend-media-uat-catalog-schema-gate"""
    pulse = managed_tree["pulse"]
    catalog_path = pulse / "Relay/config/compute-profiles.json"  # type: ignore[operator]
    catalog = json.loads(catalog_path.read_text())
    catalog["profiles"]["media"]["codex"]["reasoning_effort"] = ""
    catalog_path.write_text(json.dumps(catalog))
    verify = run_manager(managed_tree, "backend-media-uat-verify")
    assert verify.returncode == 2
    assert "media.codex needs model_alias and reasoning_effort" in verify.stderr


@pytest.mark.parametrize(
    ("policy_text", "item_indent"),
    [
        (
            """scan_items:
  - type: pending_handoffs
    per_action:
      - match: nested
        compute_profile: economy
  - type: stale_work_items
    default_autonomy: autonomous_llm
autonomous_llm_config:
  compute_profile: platform_default
""",
            2,
        ),
        (
            """scan_items:
- type: pending_handoffs
  per_action:
  - match: nested
    compute_profile: economy
- type: stale_work_items
  default_autonomy: autonomous_llm
autonomous_llm_config:
  compute_profile: platform_default
""",
            0,
        ),
    ],
)
def test_hit_profile_rewrite_is_scan_item_indent_aware(
    policy_text: str,
    item_indent: int,
    tmp_path: Path,
) -> None:
    """@anchor test backend-media-uat-scan-item-indent-and-per-action-regression"""
    module = load_manager_module()
    rendered = module._set_hit_profiles(policy_text, tmp_path / "policy.yaml", "media")
    parsed = yaml.safe_load(rendered)
    items = {item["type"]: item for item in parsed["scan_items"]}
    assert items["pending_handoffs"]["compute_profile"] == "media"
    assert items["stale_work_items"]["compute_profile"] == "media"
    assert items["pending_handoffs"]["per_action"][0]["compute_profile"] == "economy"
    assert f"{' ' * (item_indent + 2)}compute_profile: media" in rendered


def test_backend_media_uat_reload_preflight_is_before_writes(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    pulse = managed_tree["pulse"]
    cron_path = pulse / "data/cron-jobs.json"  # type: ignore[operator]
    original = cron_path.read_bytes()
    result = run_manager(managed_tree, "backend-media-uat-apply", "--reload-relay")
    assert result.returncode == 2
    assert "relay reload preflight failed" in result.stderr
    assert cron_path.read_bytes() == original


def test_backend_media_uat_reload_signals_after_apply(
    managed_tree: dict[str, Path | dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """@anchor test backend-media-uat-reload-preflight-and-sighup"""
    pid_path = tmp_path / "relay.pid"
    pid_path.write_text("4242\n")
    for key, value in managed_tree["env"].items():  # type: ignore[union-attr]
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PULSE_RUNTIME_RELAY_PID_PATH", str(pid_path))
    module = load_manager_module()
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: calls.append((pid, sig)))

    assert module._backend_media_uat_plan_or_apply(apply=True, reload_relay=True) == 0
    assert calls == [(4242, 0), (4242, module.signal.SIGHUP)]

    # An already-rendered overlay still needs an activation retry: a prior
    # apply may have happened without reload, or its SIGHUP may not have loaded.
    assert module._backend_media_uat_plan_or_apply(apply=True, reload_relay=True) == 0
    assert calls == [
        (4242, 0),
        (4242, module.signal.SIGHUP),
        (4242, 0),
        (4242, module.signal.SIGHUP),
    ]


def test_manager_refuses_a_codex_model_without_a_latest_family_alias(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    plan = run_manager(managed_tree, "plan", "--codex-model", "gpt-5.5")

    assert plan.returncode == 2
    assert "must name a Sol/Terra/Luna family model" in plan.stderr


def test_verify_detects_drift_and_invalid_priority_is_rejected(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    assert run_manager(managed_tree, "apply").returncode == 0
    pulse = managed_tree["pulse"]
    relay_path = pulse / "data/relay.json"  # type: ignore[operator]
    relay = json.loads(relay_path.read_text())
    relay["users"][0]["agents"][0]["runtime"] = "cc"
    relay_path.write_text(json.dumps(relay) + "\n")
    verify = run_manager(managed_tree, "verify")
    assert verify.returncode == 1
    assert str(relay_path) in verify.stderr

    invalid = run_manager(managed_tree, "plan", "--priority", "codex,unknown")
    assert invalid.returncode == 2
    assert "runtime_priority" in invalid.stderr


def test_profile_schema_and_field_types_fail_closed(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    assert run_manager(managed_tree, "apply").returncode == 0
    pulse = managed_tree["pulse"]
    profile_path = pulse / "data/runtime-priority.json"  # type: ignore[operator]
    valid_profile = json.loads(profile_path.read_text())
    malformed_profiles = {}

    boolean_schema = deepcopy(valid_profile)
    boolean_schema["schema_version"] = True
    malformed_profiles["boolean schema"] = boolean_schema

    invalid_priority = deepcopy(valid_profile)
    invalid_priority["runtime_priority"] = ["codex", "unknown"]
    malformed_profiles["invalid priority"] = invalid_priority

    for field, value in (
        ("model", 7),
        ("model", "  "),
        ("reasoning_effort", None),
        ("reasoning_effort", "  "),
        ("yolo", "yes"),
    ):
        malformed = deepcopy(valid_profile)
        malformed["codex"][field] = value
        malformed_profiles[f"invalid codex {field}: {value!r}"] = malformed

    invalid_claude_model = deepcopy(valid_profile)
    invalid_claude_model["claude"]["model"] = []
    malformed_profiles["invalid claude model"] = invalid_claude_model

    for label, malformed in malformed_profiles.items():
        for command in ("show", "verify"):
            profile_path.write_text(json.dumps(malformed) + "\n")
            result = run_manager(managed_tree, command)
            assert result.returncode == 2, (
                f"{label} was accepted by {command}: "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
            assert "ERROR:" in result.stderr


def test_verify_detects_persisted_runtime_without_mutating_state(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    assert run_manager(managed_tree, "apply").returncode == 0
    state_path = managed_tree["relay_state"]
    state = json.loads(state_path.read_text())  # type: ignore[union-attr]
    state["sessions"]["owner:pulse"]["runtime"] = "cc"
    state_path.write_text(json.dumps(state) + "\n")  # type: ignore[union-attr]
    before = state_path.read_bytes()  # type: ignore[union-attr]

    verify = run_manager(managed_tree, "verify")

    assert verify.returncode == 1
    assert "owner:pulse: runtime=cc expected=codex" in verify.stderr
    assert "mismatched=1" in verify.stdout
    assert state_path.read_bytes() == before  # type: ignore[union-attr]


def test_apply_repairs_legacy_alias_and_rollback_restores_symlink(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    legacy_relay = managed_tree["legacy_relay"]
    custom_target = Path(legacy_relay).parent / "custom-relay.json"
    custom_target.write_text("custom\n")
    Path(legacy_relay).unlink()
    Path(legacy_relay).symlink_to(custom_target)

    apply = run_manager(managed_tree, "apply")
    assert apply.returncode == 0, apply.stderr
    backup_id = apply.stdout.split("backup=", 1)[1].splitlines()[0]
    pulse = managed_tree["pulse"]
    assert Path(legacy_relay).resolve() == (Path(pulse) / "data/relay.json").resolve()

    rollback = run_manager(managed_tree, "rollback", backup_id)
    assert rollback.returncode == 0, rollback.stderr
    assert Path(legacy_relay).is_symlink()
    assert os.readlink(legacy_relay) == str(custom_target)


def test_structured_cost_plan_is_read_only_and_digest_bound(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    pulse = Path(managed_tree["pulse"])
    route_path = pulse / "data/runtime-cost-route.json"
    before_relay = (pulse / "data/relay.json").read_bytes()

    plan = structured_plan(managed_tree, "--preset", "current_safe")

    assert plan["schema"] == "runtime_priority_plan.v1"
    assert plan["mode"] == "plan_only"
    assert plan["current_preset"] is None
    assert plan["target_preset"] == "current_safe"
    assert str(plan["active_route_digest"]).startswith("routecfg1_")
    assert str(plan["target_route_digest"]).startswith("routecfg1_")
    assert str(plan["plan_digest"]).startswith("plan1_")
    assert plan["global_compute_profile_changed"] is False
    assert plan["single_consumer_candidate"] is False
    assert "phase23_canary" in plan["approved_presets"]
    assert ["phase2_stats_canary", "phase23_canary"] in plan["allowed_transitions"]
    assert not route_path.exists()
    assert (pulse / "data/relay.json").read_bytes() == before_relay


def test_cost_preset_apply_requires_fresh_plan_preconditions_before_write(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    pulse = Path(managed_tree["pulse"])
    route_path = pulse / "data/runtime-cost-route.json"
    missing = run_manager(managed_tree, "apply", "--preset", "current_safe")
    assert missing.returncode == 2
    assert "requires --expected-active-route-digest" in missing.stderr
    assert not route_path.exists()

    first = guarded_preset_apply(managed_tree, "current_safe")
    assert first.returncode == 0, first.stderr
    original = route_path.read_bytes()
    plan = structured_plan(managed_tree, "--preset", "phase2_stats_canary")

    stale = run_manager(
        managed_tree,
        "apply",
        "--preset",
        "phase2_stats_canary",
        "--expected-active-route-digest",
        "routecfg1_" + "0" * 64,
        "--expected-plan-digest",
        str(plan["plan_digest"]),
    )
    assert stale.returncode == 2
    assert "active route digest changed" in stale.stderr
    assert route_path.read_bytes() == original

    bad_plan = run_manager(
        managed_tree,
        "apply",
        "--preset",
        "phase2_stats_canary",
        "--expected-active-route-digest",
        str(plan["active_route_digest"]),
        "--expected-plan-digest",
        "plan1_" + "0" * 64,
    )
    assert bad_plan.returncode == 2
    assert "plan digest changed" in bad_plan.stderr
    assert route_path.read_bytes() == original


def test_structured_plan_identifies_single_effective_route_change(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    assert guarded_preset_apply(managed_tree, "current_safe").returncode == 0
    phase2 = structured_plan(managed_tree, "--preset", "phase2_stats_canary")
    assert phase2["expected_changed_routes"] == ["stats_quota_probe"]
    assert phase2["single_consumer_candidate"] is True
    assert phase2["route_diff"] == [
        {
            "consumer_path": "stats.quota_probe",
            "effective_profile_changed": True,
            "from_effective_profile": "platform_default",
            "route_name": "stats_quota_probe",
            "to_effective_profile": "economy",
        }
    ]


def test_staged_cost_presets_plan_apply_verify_and_rollback(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    pulse = Path(managed_tree["pulse"])
    route_path = pulse / "data/runtime-cost-route.json"
    cron_path = pulse / "data/cron-jobs.json"

    skipped_from_absent = run_manager(managed_tree, "plan", "--preset", "phase23_canary")
    assert skipped_from_absent.returncode == 2
    assert "cost preset transition rejected: absent -> phase23_canary" in skipped_from_absent.stderr
    assert not route_path.exists()

    current_safe = guarded_preset_apply(managed_tree, "current_safe")
    assert current_safe.returncode == 0, current_safe.stderr
    assert json.loads(route_path.read_text())["preset"] == "current_safe"

    skipped_from_safe = run_manager(managed_tree, "apply", "--preset", "phase23_canary")
    assert skipped_from_safe.returncode == 2
    assert (
        "cost preset transition rejected: current_safe -> phase23_canary"
        in skipped_from_safe.stderr
    )
    assert json.loads(route_path.read_text())["preset"] == "current_safe"

    phase2_plan = run_manager(managed_tree, "plan", "--preset", "phase2_stats_canary")
    assert phase2_plan.returncode == 0, phase2_plan.stderr
    assert "route=stats_quota_probe" in phase2_plan.stdout
    assert "effective=economy" in phase2_plan.stdout
    assert json.loads(route_path.read_text())["preset"] == "current_safe"

    phase2 = guarded_preset_apply(managed_tree, "phase2_stats_canary")
    assert phase2.returncode == 0, phase2.stderr
    route = json.loads(route_path.read_text())
    assert route["preset"] == "phase2_stats_canary"
    assert route["routes"]["stats_quota_probe"]["effective_route"]["profile"] == "economy"
    assert route["routes"]["hestia_weekly_review"]["effective_route"]["profile"] == "platform_default"
    assert run_manager(managed_tree, "verify").returncode == 0

    phase23 = guarded_preset_apply(managed_tree, "phase23_canary")
    assert phase23.returncode == 0, phase23.stderr
    phase23_backup = phase23.stdout.split("backup=", 1)[1].splitlines()[0]
    route = json.loads(route_path.read_text())
    assert route["preset"] == "phase23_canary"
    weekly = next(
        job for job in json.loads(cron_path.read_text())["jobs"]
        if job["id"] == "hestia-vultr-weekly-review-20260717"
    )
    assert weekly["agent"]["computeProfile"] == "media"
    direct_jobs = [
        job for job in json.loads(cron_path.read_text())["jobs"]
        if not (
            isinstance(job.get("execution"), dict)
            and job["execution"].get("mode") == "harness_dispatch"
        )
    ]
    assert {
        job["id"] for job in direct_jobs if job["agent"].get("computeProfile") == "media"
    } == {"hestia-vultr-weekly-review-20260717", "hestia-19f9254e40c"}
    assert run_manager(managed_tree, "verify").returncode == 0

    downgrade_phase2 = guarded_preset_apply(managed_tree, "phase2_stats_canary")
    assert downgrade_phase2.returncode == 0, downgrade_phase2.stderr
    assert json.loads(route_path.read_text())["preset"] == "phase2_stats_canary"
    downgrade_safe = guarded_preset_apply(managed_tree, "current_safe")
    assert downgrade_safe.returncode == 0, downgrade_safe.stderr
    assert json.loads(route_path.read_text())["preset"] == "current_safe"

    rollback = run_manager(managed_tree, "rollback", phase23_backup)
    assert rollback.returncode == 0, rollback.stderr
    assert json.loads(route_path.read_text())["preset"] == "phase2_stats_canary"
    assert run_manager(managed_tree, "verify").returncode == 0


def test_cost_routes_fail_closed_for_unknown_preset_bad_schema_and_unsafe_floor(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    unknown = run_manager(managed_tree, "plan", "--preset", "unknown")
    assert unknown.returncode == 2
    assert "unknown cost preset" in unknown.stderr

    cost_presets = Path(managed_tree["cost_presets"])
    valid = json.loads(cost_presets.read_text())
    malformed = deepcopy(valid)
    malformed["schema_version"] = True
    cost_presets.write_text(json.dumps(malformed))
    result = run_manager(managed_tree, "plan", "--preset", "current_safe")
    assert result.returncode == 2
    assert "schema_version" in result.stderr

    unsafe = deepcopy(valid)
    route = unsafe["presets"]["phase23_canary"]["routes"]["harness_pending_handoffs"]
    route["base_profile"] = "economy"
    cost_presets.write_text(json.dumps(unsafe))
    assert guarded_preset_apply(managed_tree, "current_safe").returncode == 0
    assert guarded_preset_apply(managed_tree, "phase2_stats_canary").returncode == 0
    result = run_manager(managed_tree, "plan", "--preset", "phase23_canary")
    assert result.returncode == 0, result.stderr
    assert "route=harness_pending_handoffs" in result.stdout
    assert "base=economy floor=frontier effective=frontier" in result.stdout

    active = Path(managed_tree["pulse"]) / "data/runtime-cost-route.json"
    active.write_text('{"schema_version": true}\n')
    result = run_manager(managed_tree, "plan", "--preset", "current_safe")
    assert result.returncode == 2
    assert "active cost route schema_version" in result.stderr


def test_deterministic_critical_route_applies_floor_only_to_escalation(
    managed_tree: dict[str, Path | dict[str, str]],
) -> None:
    cost_presets = Path(managed_tree["cost_presets"])
    presets = json.loads(cost_presets.read_text())
    route = presets["presets"]["phase23_canary"]["routes"]["hestia_30min"]
    route["risk_class"] = "critical_ambiguous"
    route["escalation_profile"] = "economy"
    cost_presets.write_text(json.dumps(presets))
    assert guarded_preset_apply(managed_tree, "current_safe").returncode == 0
    assert guarded_preset_apply(managed_tree, "phase2_stats_canary").returncode == 0

    unsafe = run_manager(managed_tree, "plan", "--preset", "phase23_canary")
    assert unsafe.returncode == 2
    assert "critical deterministic route hestia_30min escalation is below frontier" in unsafe.stderr

    route["escalation_profile"] = "frontier"
    cost_presets.write_text(json.dumps(presets))
    safe = run_manager(managed_tree, "plan", "--preset", "phase23_canary")
    assert safe.returncode == 0, safe.stderr
    assert "route=hestia_30min" in safe.stdout
    assert "base=none floor=frontier effective=none" in safe.stdout
