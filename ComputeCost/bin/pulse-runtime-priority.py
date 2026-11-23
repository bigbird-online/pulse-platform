#!/usr/bin/env python3
"""Manage PULSE runtime priority across Relay, harness, Codex, and Stats."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import tomllib
from typing import Any, Iterator

import yaml


SUPPORTED_RUNTIMES = ("codex", "cc")
HARNESS_RUNTIME = {"codex": "codex", "cc": "claude"}
MANAGED_POLICY_KEYS = (
    "runtime",
    "fallback_runtimes",
    "compute_profile",
    "codex_model",
    "codex_reasoning_effort",
    "codex_yolo",
    "claude_model",
)
HARNESS_DEFAULT_COMPUTE_PROFILE = "platform_default"
FABLE_CC_ALIAS = "claude-fable-latest"
PLATFORM_CC_ALIAS_FIELD = "platform_default_cc_alias"
BACKEND_MEDIA_UAT_PROFILE = "media"
BACKEND_MEDIA_UAT_BACKUP_PREFIX = "backend-media-uat"
CURRENT_SAFE_PRESET = "current_safe"
ALLOWED_COST_PRESET_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"current_safe"}),
    "current_safe": frozenset({"current_safe", "phase2_stats_canary"}),
    "phase2_stats_canary": frozenset({
        "current_safe",
        "phase2_stats_canary",
        "phase23_canary",
    }),
    "phase23_canary": frozenset({
        "current_safe",
        "phase2_stats_canary",
        "phase23_canary",
    }),
}
WEEKLY_REVIEW_JOB_ID = "hestia-vultr-weekly-review-20260717"
HESTIA_30MIN_JOB_ID = "hestia-19f9254e40c"
REQUIRED_COST_ROUTES = frozenset({
    "stats_quota_probe",
    "hestia_30min",
    "hestia_weekly_review",
    "harness_pending_handoffs",
    "harness_stale_work_items",
})
WORKLOAD_SHAPES = frozenset({"deterministic", "repeatable", "routine_judgment"})
RISK_CLASSES = frozenset({"standard", "critical_ambiguous"})
PROFILE_ROUTE_RANK = {
    ("luna", "low"): 1,
    ("luna", "medium"): 2,
    ("terra", "medium"): 3,
    ("terra", "high"): 4,
    ("sol", "high"): 5,
    ("sol", "xhigh"): 6,
}
DEFAULT_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "runtime_priority": ["codex"],
    "codex": {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "yolo": True,
    },
    "claude": {"model": "deepseek/deepseek-v4-flash-0731"},
}


class ManagementError(RuntimeError):
    pass


def _paths() -> dict[str, Path]:
    module_root = Path(__file__).resolve().parents[1]
    pulse_root = Path(os.environ.get("PULSE_RUNTIME_PULSE_ROOT", module_root.parent)).resolve()
    # PULSE remains physically nested at Coevolution/Bigbird/PULSE; only its
    # modules are flat at the checkout root.
    coevolution_root = pulse_root.parents[1]
    harness_root = Path(
        os.environ.get("PULSE_RUNTIME_HARNESS_ROOT", coevolution_root / ".harness")
    ).resolve()
    codex_config = Path(
        os.environ.get("PULSE_RUNTIME_CODEX_CONFIG", "/home/pulse/.codex/config.toml")
    ).resolve()
    backup_root = Path(
        os.environ.get(
            "PULSE_RUNTIME_BACKUP_ROOT",
            "/home/pulse/.relay/runtime-priority-backups",
        )
    ).resolve()
    relay_state = Path(
        os.environ.get("PULSE_RUNTIME_RELAY_STATE", "/home/pulse/.relay/state.json")
    ).resolve()
    legacy_relay_alias = Path(
        os.environ.get(
            "PULSE_RUNTIME_LEGACY_RELAY_CONFIG",
            "/home/pulse/.relay/relay.json",
        )
    )
    return {
        "pulse_root": pulse_root,
        "profile": pulse_root / "data/runtime-priority.json",
        "relay": pulse_root / "data/relay.json",
        "cron": pulse_root / "data/cron-jobs.json",
        "compute_profiles": pulse_root / "Relay/config/compute-profiles.json",
        "cost_presets": Path(
            os.environ.get(
                "PULSE_RUNTIME_COST_PRESETS",
                str(module_root / "config/runtime-cost-presets.json"),
            )
        ).resolve(),
        "cost_route": pulse_root / "data/runtime-cost-route.json",
        "policies": harness_root / "policies",
        "codex_config": codex_config,
        "relay_state": relay_state,
        "relay_pid": Path(
            os.environ.get("PULSE_RUNTIME_RELAY_PID_PATH", "/home/pulse/.relay/relay.pid")
        ).resolve(),
        "legacy_relay_alias": legacy_relay_alias,
        "backup_root": backup_root,
        "lock": backup_root.parent / ".runtime-priority.lock",
    }


def _validate_cost_presets(value: dict[str, Any]) -> dict[str, Any]:
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ManagementError("cost preset schema_version must be integer 1")
    presets = value.get("presets")
    if not isinstance(presets, dict) or not presets:
        raise ManagementError("cost presets must contain presets object")
    for preset_name, preset in presets.items():
        if not isinstance(preset_name, str) or not preset_name.strip() or not isinstance(preset, dict):
            raise ManagementError("cost preset names and values must be objects")
        if set(preset) != {"status", "routes"}:
            raise ManagementError(f"cost preset {preset_name} has unexpected fields")
        if preset.get("status") not in {"baseline", "canary", "proposed"}:
            raise ManagementError(f"cost preset {preset_name} has invalid status")
        routes = preset.get("routes")
        if not isinstance(routes, dict) or set(routes) != REQUIRED_COST_ROUTES:
            raise ManagementError(
                f"cost preset {preset_name} must define exactly the required routes"
            )
        for route_name, route in routes.items():
            _validate_cost_route(route_name, route)
    return value


def _validate_cost_route(name: str, route: object) -> dict[str, Any]:
    if not isinstance(route, dict):
        raise ManagementError(f"cost route {name} must be an object")
    required = {
        "consumer_path",
        "workload_shape",
        "risk_class",
        "verifier_id",
        "base_profile",
        "rollback_profile",
    }
    optional = {"escalation_profile"}
    if not required.issubset(route) or not set(route).issubset(required | optional):
        raise ManagementError(f"cost route {name} has invalid fields")
    for field in ("consumer_path", "verifier_id", "rollback_profile"):
        if not isinstance(route.get(field), str) or not route[field].strip():
            raise ManagementError(f"cost route {name}.{field} must be a non-empty string")
    if route.get("workload_shape") not in WORKLOAD_SHAPES:
        raise ManagementError(f"cost route {name} has unsupported workload_shape")
    if route.get("risk_class") not in RISK_CLASSES:
        raise ManagementError(f"cost route {name} has unsupported risk_class")
    for field in ("base_profile", "escalation_profile"):
        value = route.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ManagementError(f"cost route {name}.{field} must be null or a profile name")
    if route["workload_shape"] == "deterministic" and route["base_profile"] is not None:
        raise ManagementError(f"deterministic cost route {name} must not enable an LLM")
    if route["workload_shape"] != "deterministic" and route["base_profile"] is None:
        raise ManagementError(f"LLM cost route {name} must name a base_profile")
    return route


def _validate_active_cost_route(value: dict[str, Any]) -> dict[str, Any]:
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ManagementError("active cost route schema_version must be integer 1")
    if set(value) != {"schema_version", "preset", "status", "routes"}:
        raise ManagementError("active cost route has unexpected fields")
    if not isinstance(value.get("preset"), str) or not value["preset"].strip():
        raise ManagementError("active cost route preset must be a non-empty string")
    if value.get("status") not in {"baseline", "canary", "proposed"}:
        raise ManagementError("active cost route status is invalid")
    routes = value.get("routes")
    if not isinstance(routes, dict) or set(routes) != REQUIRED_COST_ROUTES:
        raise ManagementError("active cost route must contain exactly the required routes")
    required = {
        "consumer_path",
        "workload_shape",
        "risk_class",
        "verifier_id",
        "base_route",
        "risk_floor",
        "effective_route",
        "rollback_profile",
    }
    for name, route in routes.items():
        if not isinstance(route, dict) or not required.issubset(route):
            raise ManagementError(f"active cost route {name} has invalid fields")
        if not set(route).issubset(required | {"escalation_profile"}):
            raise ManagementError(f"active cost route {name} has unexpected fields")
        for field in ("consumer_path", "verifier_id", "rollback_profile"):
            if not isinstance(route.get(field), str) or not route[field].strip():
                raise ManagementError(f"active cost route {name}.{field} is invalid")
        if route.get("workload_shape") not in WORKLOAD_SHAPES:
            raise ManagementError(f"active cost route {name} workload_shape is invalid")
        if route.get("risk_class") not in RISK_CLASSES:
            raise ManagementError(f"active cost route {name} risk_class is invalid")
        for field in ("base_route", "risk_floor", "effective_route"):
            container = route.get(field)
            if not isinstance(container, dict) or set(container) != {"profile"}:
                raise ManagementError(f"active cost route {name}.{field} is invalid")
            profile_name = container["profile"]
            if profile_name is not None and (
                not isinstance(profile_name, str) or not profile_name.strip()
            ):
                raise ManagementError(f"active cost route {name}.{field}.profile is invalid")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManagementError(f"missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManagementError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManagementError(f"expected JSON object: {path}")
    return value


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _profile_from_disk(path: Path) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_PROFILE))
    return _validate_profile(_read_json(path))


def _validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    schema_version = profile.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise ManagementError("runtime profile schema_version must be integer 1")
    priority = profile.get("runtime_priority")
    if (
        not isinstance(priority, list)
        or not priority
        or any(item not in SUPPORTED_RUNTIMES for item in priority)
        or len(set(priority)) != len(priority)
    ):
        raise ManagementError("runtime_priority must be a unique non-empty list of codex/cc")
    codex = profile.get("codex")
    claude = profile.get("claude")
    if not isinstance(codex, dict) or not isinstance(claude, dict):
        raise ManagementError("runtime profile needs codex and claude objects")
    for field in ("model", "reasoning_effort"):
        if not isinstance(codex.get(field), str) or not codex[field].strip():
            raise ManagementError(f"codex.{field} must be a non-empty string")
    if not isinstance(codex.get("yolo"), bool):
        raise ManagementError("codex.yolo must be boolean")
    if not isinstance(claude.get("model"), str) or not claude["model"].strip():
        raise ManagementError("claude.model must be a non-empty string")
    if profile.get(PLATFORM_CC_ALIAS_FIELD) not in (None, FABLE_CC_ALIAS):
        raise ManagementError(f"{PLATFORM_CC_ALIAS_FIELD} must be {FABLE_CC_ALIAS} or null")
    return profile


def _compute_profile_catalog(path: Path) -> dict[str, Any]:
    catalog = _read_json(path)
    if catalog.get("schema_version") != 2 or not isinstance(catalog.get("profiles"), dict):
        raise ManagementError(f"invalid compute profile catalog: {path}")
    return catalog


def _uses_fable_default(catalog: dict[str, Any]) -> bool:
    default = catalog["profiles"].get(HARNESS_DEFAULT_COMPUTE_PROFILE)
    cc = default.get("cc") if isinstance(default, dict) else None
    return isinstance(cc, dict) and cc.get("model_alias") == FABLE_CC_ALIAS


def _desired_profile(
    args: argparse.Namespace, current: dict[str, Any], compute_profiles_path: Path
) -> dict[str, Any]:
    desired = json.loads(json.dumps(current))
    if args.priority:
        desired["runtime_priority"] = [part.strip() for part in args.priority.split(",") if part.strip()]
    if args.codex_model:
        desired["codex"]["model"] = args.codex_model.strip()
    if args.codex_reasoning_effort:
        desired["codex"]["reasoning_effort"] = args.codex_reasoning_effort.strip()
    if args.codex_yolo:
        desired["codex"]["yolo"] = args.codex_yolo == "on"
    if args.claude_model:
        desired["claude"]["model"] = args.claude_model.strip()
    if args.platform_default_cc is not None:
        desired[PLATFORM_CC_ALIAS_FIELD] = (
            FABLE_CC_ALIAS if args.platform_default_cc == FABLE_CC_ALIAS else None
        )
    elif (
        PLATFORM_CC_ALIAS_FIELD not in desired
        and _uses_fable_default(_compute_profile_catalog(compute_profiles_path))
    ):
        desired[PLATFORM_CC_ALIAS_FIELD] = FABLE_CC_ALIAS
    desired = _validate_profile(desired)
    if "fable" in desired["claude"]["model"].lower():
        raise ManagementError("claude.model Fable is disabled; choose an approved non-Fable model")
    return desired


def _profile_rank(name: str, catalog: dict[str, Any]) -> int:
    raw = catalog["profiles"].get(name)
    if not isinstance(raw, dict) or not isinstance(raw.get("codex"), dict):
        raise ManagementError(f"cost route references unknown compute profile: {name}")
    codex = raw["codex"]
    alias = codex.get("model_alias")
    effort = codex.get("reasoning_effort")
    if not isinstance(alias, str) or not isinstance(effort, str):
        raise ManagementError(f"cost route compute profile {name} lacks Codex alias/effort")
    match = re.fullmatch(r"codex-(luna|terra|sol)-latest", alias)
    if match is None or (match.group(1), effort) not in PROFILE_ROUTE_RANK:
        raise ManagementError(f"cost route compute profile {name} has unsupported rank")
    return PROFILE_ROUTE_RANK[(match.group(1), effort)]


def _materialize_cost_route(
    preset_name: str,
    preset: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    routes: dict[str, Any] = {}
    for route_name, source in preset["routes"].items():
        route = _validate_cost_route(route_name, source)
        base_profile = route["base_profile"]
        rollback_profile = route["rollback_profile"]
        escalation_profile = route.get("escalation_profile")
        for referenced in (base_profile, rollback_profile, escalation_profile):
            if referenced is not None:
                _profile_rank(referenced, catalog)

        floor_profile = "frontier" if route["risk_class"] == "critical_ambiguous" else None
        if floor_profile is not None:
            _profile_rank(floor_profile, catalog)
        effective_profile = base_profile
        if route["workload_shape"] == "deterministic":
            effective_profile = None
            if floor_profile is not None:
                if escalation_profile is None:
                    raise ManagementError(
                        f"critical deterministic route {route_name} needs an escalation_profile"
                    )
                if _profile_rank(escalation_profile, catalog) < _profile_rank(floor_profile, catalog):
                    raise ManagementError(
                        f"critical deterministic route {route_name} escalation is below frontier"
                    )
        else:
            if floor_profile is not None and (
                effective_profile is None
                or _profile_rank(effective_profile, catalog) < _profile_rank(floor_profile, catalog)
            ):
                effective_profile = floor_profile
            if route["risk_class"] == "critical_ambiguous" and (
                effective_profile is None
                or _profile_rank(effective_profile, catalog) < _profile_rank("frontier", catalog)
            ):
                raise ManagementError(f"critical_ambiguous route {route_name} is below frontier")

        materialized = {
            "consumer_path": route["consumer_path"],
            "workload_shape": route["workload_shape"],
            "risk_class": route["risk_class"],
            "verifier_id": route["verifier_id"],
            "base_route": {"profile": base_profile},
            "risk_floor": {"profile": floor_profile},
            "effective_route": {"profile": effective_profile},
            "rollback_profile": rollback_profile,
        }
        if escalation_profile is not None:
            materialized["escalation_profile"] = escalation_profile
        routes[route_name] = materialized
    return {
        "schema_version": 1,
        "preset": preset_name,
        "status": preset["status"],
        "routes": routes,
    }


def _desired_cost_route(
    preset_name: str | None,
    paths: dict[str, Path],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    preset_catalog = _validate_cost_presets(_read_json(paths["cost_presets"]))
    active = None
    if paths["cost_route"].exists():
        active = _validate_active_cost_route(_read_json(paths["cost_route"]))
    current_preset = active["preset"] if active is not None else None
    if preset_name:
        preset_name = preset_name.strip()
    elif active is not None:
        preset_name = active.get("preset")
        if not isinstance(preset_name, str) or not preset_name:
            raise ManagementError("active cost route has no valid preset")
    else:
        preset_name = CURRENT_SAFE_PRESET
    preset = preset_catalog["presets"].get(preset_name)
    if not isinstance(preset, dict):
        raise ManagementError(f"unknown cost preset: {preset_name}")
    allowed = ALLOWED_COST_PRESET_TRANSITIONS.get(current_preset, frozenset())
    if preset_name not in allowed:
        current_label = current_preset if current_preset is not None else "absent"
        raise ManagementError(
            f"cost preset transition rejected: {current_label} -> {preset_name}"
        )
    return _materialize_cost_route(preset_name, preset, catalog)


def _relay_agents(relay: dict[str, Any]) -> list[dict[str, Any]]:
    users = relay.get("users")
    if not isinstance(users, list):
        raise ManagementError("relay.json users must be an array")
    agents: list[dict[str, Any]] = []
    for user in users:
        if not isinstance(user, dict) or not isinstance(user.get("agents"), list):
            raise ManagementError("relay.json each user must contain agents[]")
        for agent in user["agents"]:
            if not isinstance(agent, dict):
                raise ManagementError("relay.json agent must be an object")
            agents.append(agent)
    if not agents:
        raise ManagementError("relay.json contains no managed agents")
    return agents


def _transform_relay(path: Path, profile: dict[str, Any]) -> tuple[bytes, int]:
    relay = _read_json(path)
    priority = profile["runtime_priority"]
    active_model = (
        profile["codex"]["model"]
        if priority[0] == "codex"
        else profile["claude"]["model"]
    )
    agents = _relay_agents(relay)
    for agent in agents:
        agent["runtime"] = priority[0]
        agent["conversationRuntimePreference"] = priority.copy()
        agent["ccModel"] = active_model
        agent["codexYoloDefault"] = profile["codex"]["yolo"]
    return _json_bytes(relay), len(agents)


def _cron_jobs(cron: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = cron.get("jobs")
    if not isinstance(jobs, list):
        raise ManagementError("cron-jobs.json jobs must be an array")
    if any(not isinstance(job, dict) for job in jobs):
        raise ManagementError("cron-jobs.json job must be an object")
    return jobs


def _is_harness_dispatch(job: dict[str, Any]) -> bool:
    execution = job.get("execution")
    return isinstance(execution, dict) and execution.get("mode") == "harness_dispatch"


def _cost_profile(cost_route: dict[str, Any], route_name: str) -> str | None:
    try:
        profile = cost_route["routes"][route_name]["effective_route"]["profile"]
    except (KeyError, TypeError) as exc:
        raise ManagementError(f"active cost route lacks {route_name} effective profile") from exc
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise ManagementError(f"active cost route {route_name} profile is invalid")
    return profile


def _transform_cron(
    path: Path,
    profile: dict[str, Any],
    cost_route: dict[str, Any],
) -> tuple[bytes, int, int]:
    cron = _read_json(path)
    direct = 0
    harness = 0
    weekly_matches = 0
    hestia_gate_matches = 0
    for job in _cron_jobs(cron):
        if _is_harness_dispatch(job):
            harness += 1
            continue
        agent = job.get("agent")
        if not isinstance(agent, dict):
            raise ManagementError(f"direct cron job {job.get('id', '?')} has no agent object")
        agent["runtimePreference"] = profile["runtime_priority"].copy()
        agent["codexYolo"] = profile["codex"]["yolo"]
        for literal in ("ccModel", "ccReasoningEffort", "codexModel", "codexReasoningEffort"):
            agent.pop(literal, None)
        job_id = job.get("id")
        if job_id == WEEKLY_REVIEW_JOB_ID:
            weekly_matches += 1
            weekly_profile = _cost_profile(cost_route, "hestia_weekly_review")
            if weekly_profile is None:
                raise ManagementError("weekly review route must enable an LLM")
            agent["computeProfile"] = weekly_profile
            schedule = job.get("schedule")
            if not isinstance(schedule, dict):
                raise ManagementError("weekly review job has no schedule object")
            schedule["expr"] = "0 16 * * 0"
            schedule["tz"] = "Asia/Shanghai"
            agent["expectArtifact"] = {
                "path": "memo/review-${ISO_WEEK}-draft.md",
                "minBytes": 2000,
                "maxAgeSeconds": 5400,
                "contains": [
                    "status:",
                    "draft",
                    "## ①",
                    "## ②",
                    "## ③",
                    "## ④",
                    "## ⑤",
                    "[待确认]",
                ],
            }
        elif job_id == HESTIA_30MIN_JOB_ID:
            hestia_gate_matches += 1
            route = cost_route["routes"]["hestia_30min"]
            if route["effective_route"]["profile"] is not None:
                raise ManagementError("Hestia 30min normal route must remain deterministic")
            escalation_profile = route.get("escalation_profile")
            if not isinstance(escalation_profile, str) or not escalation_profile:
                raise ManagementError("Hestia 30min route lacks escalation_profile")
            gate = agent.get("gate")
            if not isinstance(gate, dict) or not gate.get("command"):
                raise ManagementError("Hestia 30min job lacks deterministic gate")
            agent["computeProfile"] = escalation_profile
        else:
            agent["computeProfile"] = "platform_default"
        direct += 1
    if weekly_matches != 1:
        raise ManagementError(f"expected exactly one weekly review job, found {weekly_matches}")
    if hestia_gate_matches != 1:
        raise ManagementError(f"expected exactly one Hestia 30min job, found {hestia_gate_matches}")
    return _json_bytes(cron), direct, harness


def _transform_backend_media_uat_cron(path: Path) -> tuple[bytes, int, int]:
    """Render the deliberately narrow backend-only UAT cron overlay.

    This must not touch runtime preference, yolo, gates, schedules, or cost
    routes.  ``harness_dispatch`` jobs do not themselves spawn an LLM and are
    intentionally left byte-for-byte alone; their policy is handled below.
    """
    cron = _read_json(path)
    direct = 0
    harness = 0
    for job in _cron_jobs(cron):
        if _is_harness_dispatch(job):
            harness += 1
            continue
        agent = job.get("agent")
        if not isinstance(agent, dict):
            raise ManagementError(f"direct cron job {job.get('id', '?')} has no agent object")
        agent["computeProfile"] = BACKEND_MEDIA_UAT_PROFILE
        direct += 1
    return _json_bytes(cron), direct, harness


def _transform_compute_profiles(path: Path, profile: dict[str, Any]) -> bytes:
    catalog = _compute_profile_catalog(path)
    model_match = re.fullmatch(
        r"gpt-\d+(?:\.\d+)*-(sol|terra|luna)",
        profile["codex"]["model"],
    )
    if model_match is None:
        raise ManagementError(
            "codex.model must name a Sol/Terra/Luna family model so cron can use a latest alias"
        )
    cc_alias = profile.get(PLATFORM_CC_ALIAS_FIELD)
    cc_profile = {"model_alias": cc_alias} if cc_alias else {"model": profile["claude"]["model"]}
    platform_default = {
        "cc": cc_profile,
        "codex": {
            "model_alias": f"codex-{model_match.group(1)}-latest",
            "reasoning_effort": profile["codex"]["reasoning_effort"],
        },
    }
    catalog["profiles"]["platform_default"] = platform_default
    catalog["profiles"]["frontier"] = {
        "cc": {
            "model_alias": "claude-opus-latest",
            "reasoning_effort": "high",
        },
        "codex": {
            "model_alias": "codex-sol-latest",
            "reasoning_effort": "high",
        },
    }
    catalog["profiles"]["media"] = {
        "cc": {
            "model_alias": "claude-sonnet-latest",
            "reasoning_effort": "medium",
        },
        "codex": {
            "model_alias": "codex-terra-latest",
            "reasoning_effort": "medium",
        },
    }
    catalog["profiles"]["economy"] = {
        "cc": {
            "model_alias": "claude-haiku-latest",
            "reasoning_effort": "low",
        },
        "codex": {
            "model_alias": "codex-luna-latest",
            "reasoning_effort": "low",
        },
    }
    return _json_bytes(catalog)


def _policy_block(text: str, path: Path) -> tuple[int, int, list[str]]:
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "autonomous_llm_config:"), -1)
    if start < 0:
        raise ManagementError(f"policy lacks autonomous_llm_config: {path}")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t", "#")):
            end = index
            break
    return start, end, lines


def _enforce_hit_profiles(text: str, path: Path) -> str:
    return _set_hit_profiles(text, path, "frontier")


def _set_hit_profiles(text: str, path: Path, profile: str) -> str:
    lines = text.splitlines(keepends=True)
    found: set[str] = set()
    scan_start = next(
        (index for index, line in enumerate(lines) if re.match(r"^(\s*)scan_items:\s*(?:#.*)?$", line.rstrip("\n"))),
        -1,
    )
    if scan_start < 0:
        # Some valid policies have no scan-driven LLM surface (for example a
        # dedicated Pulse policy). Their top-level profile remains managed by
        # the caller, while there are no hit profiles to rewrite.
        return text
    scan_indent = len(lines[scan_start]) - len(lines[scan_start].lstrip(" "))
    index = scan_start + 1
    while index < len(lines):
        raw = lines[index].rstrip("\n")
        if raw.strip() and not raw.lstrip().startswith("#"):
            indent = len(raw) - len(raw.lstrip(" "))
            # A same-indent sequence is legal YAML under ``scan_items``;
            # a same-indent mapping key starts the next top-level section.
            if indent <= scan_indent and not raw.lstrip().startswith("- "):
                break
        match = re.match(
            r"^(\s*)-\s+type:\s*(pending_handoffs|stale_work_items)\s*(?:#.*)?$",
            raw,
        )
        if match is None:
            index += 1
            continue
        item_indent = len(match.group(1))
        hit_type = match.group(2)
        found.add(hit_type)
        block_end = index + 1
        while block_end < len(lines):
            candidate = lines[block_end].rstrip("\n")
            if candidate.strip() and not candidate.lstrip().startswith("#"):
                candidate_indent = len(candidate) - len(candidate.lstrip(" "))
                if candidate_indent <= item_indent:
                    break
            block_end += 1
        profile_index = next(
            (
                position
                for position in range(index + 1, block_end)
                if re.match(
                    rf"^ {{{item_indent + 2}}}compute_profile:\s*",
                    lines[position],
                )
            ),
            None,
        )
        if profile_index is None:
            lines.insert(index + 1, f"{' ' * (item_indent + 2)}compute_profile: {profile}\n")
            block_end += 1
        else:
            lines[profile_index] = f"{' ' * (item_indent + 2)}compute_profile: {profile}\n"
        index = block_end
    try:
        parsed = yaml.safe_load("".join(lines))
    except yaml.YAMLError as exc:
        raise ManagementError(f"generated invalid YAML {path}: {exc}") from exc
    for item in parsed.get("scan_items", []):
        if isinstance(item, dict) and item.get("type") in found:
            if item.get("compute_profile") != profile:
                raise ManagementError(f"generated policy lost {profile} hit profile: {path}")
    return "".join(lines)


def _transform_policy(path: Path, profile: dict[str, Any]) -> bytes:
    text = _enforce_hit_profiles(path.read_text(encoding="utf-8"), path)
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManagementError(f"invalid YAML {path}: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("autonomous_llm_config"), dict):
        raise ManagementError(f"invalid autonomous_llm_config in {path}")
    start, end, lines = _policy_block(text, path)
    key_pattern = re.compile(r"^  (" + "|".join(MANAGED_POLICY_KEYS) + r"):($|\s)")
    retained = [line for line in lines[start + 1:end] if not key_pattern.match(line)]
    priority = [HARNESS_RUNTIME[item] for item in profile["runtime_priority"]]
    managed = [
        f"  runtime: {priority[0]}\n",
        f"  fallback_runtimes: {json.dumps(priority[1:])}\n",
        f"  compute_profile: {HARNESS_DEFAULT_COMPUTE_PROFILE}\n",
        f"  codex_yolo: {'true' if profile['codex']['yolo'] else 'false'}\n",
    ]
    output = "".join(lines[: start + 1] + managed + retained + lines[end:])
    try:
        checked = yaml.safe_load(output)
    except yaml.YAMLError as exc:
        raise ManagementError(f"generated invalid YAML {path}: {exc}") from exc
    cfg = checked.get("autonomous_llm_config", {})
    expected = {
        "runtime": priority[0],
        "fallback_runtimes": priority[1:],
        "compute_profile": HARNESS_DEFAULT_COMPUTE_PROFILE,
        "codex_yolo": profile["codex"]["yolo"],
    }
    for key, value in expected.items():
        if cfg.get(key) != value:
            raise ManagementError(f"generated policy mismatch {path}: {key}")
    return output.encode("utf-8")


def _transform_backend_media_uat_policy(path: Path) -> bytes:
    """Set only backend LLM profile fields for the media UAT overlay."""
    text = _set_hit_profiles(path.read_text(encoding="utf-8"), path, BACKEND_MEDIA_UAT_PROFILE)
    start, end, lines = _policy_block(text, path)
    retained = [
        line for line in lines[start + 1:end]
        if not re.match(r"^  compute_profile:($|\s)", line)
    ]
    output = "".join(
        lines[: start + 1]
        + [f"  compute_profile: {BACKEND_MEDIA_UAT_PROFILE}\n"]
        + retained
        + lines[end:]
    )
    try:
        checked = yaml.safe_load(output)
    except yaml.YAMLError as exc:
        raise ManagementError(f"generated invalid YAML {path}: {exc}") from exc
    cfg = checked.get("autonomous_llm_config", {})
    if cfg.get("compute_profile") != BACKEND_MEDIA_UAT_PROFILE:
        raise ManagementError(f"generated UAT policy mismatch {path}: compute_profile")
    for item in checked.get("scan_items", []):
        if isinstance(item, dict) and item.get("type") in {"pending_handoffs", "stale_work_items"}:
            if item.get("compute_profile") != BACKEND_MEDIA_UAT_PROFILE:
                raise ManagementError(f"generated UAT policy mismatch {path}: {item.get('type')}")
    return output.encode("utf-8")


def _replace_top_level_toml(text: str, key: str, value: str) -> str:
    replacement = f"{key} = {json.dumps(value)}"
    table_at = next((match.start() for match in re.finditer(r"(?m)^\[", text)), len(text))
    header = text[:table_at]
    tail = text[table_at:]
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
    if pattern.search(header):
        header = pattern.sub(replacement, header, count=1)
    else:
        header = replacement + "\n" + header
    return header + tail


def _remove_toml_table_key(text: str, table: str, key: str) -> str:
    table_match = re.search(rf"(?m)^\[{re.escape(table)}\]\s*$", text)
    if table_match is None:
        return text
    start = table_match.end()
    next_table = re.search(r"(?m)^\[", text[start:])
    end = start + next_table.start() if next_table else len(text)
    body = text[start:end]
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$\n?")
    body = pattern.sub("", body, count=1)
    return text[:start] + body + text[end:]


def _transform_codex_config(path: Path, profile: dict[str, Any]) -> bytes:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ManagementError(f"missing Codex config: {path}") from exc
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManagementError(f"invalid TOML {path}: {exc}") from exc
    output = _replace_top_level_toml(text, "model", profile["codex"]["model"])
    output = _replace_top_level_toml(
        output,
        "model_reasoning_effort",
        profile["codex"]["reasoning_effort"],
    )
    output = _replace_top_level_toml(output, "approval_policy", "never")
    output = _replace_top_level_toml(
        output,
        "sandbox_mode",
        "danger-full-access" if profile["codex"]["yolo"] else "workspace-write",
    )
    # Current Codex parses every key below [agents] as an AgentRoleToml object.
    # Scalar "defaults" make the whole config unreadable. Spawn callers must
    # resolve a central profile and pass model/effort explicitly.
    output = _remove_toml_table_key(output, "agents", "default_subagent_reasoning_effort")
    output = _remove_toml_table_key(output, "agents", "default_subagent_model")
    try:
        checked = tomllib.loads(output)
    except tomllib.TOMLDecodeError as exc:
        raise ManagementError(f"generated invalid TOML {path}: {exc}") from exc
    if checked.get("model") != profile["codex"]["model"]:
        raise ManagementError("generated Codex config model mismatch")
    if checked.get("model_reasoning_effort") != profile["codex"]["reasoning_effort"]:
        raise ManagementError("generated Codex config reasoning mismatch")
    if checked.get("approval_policy") != "never":
        raise ManagementError("generated Codex config approval policy mismatch")
    expected_sandbox = "danger-full-access" if profile["codex"]["yolo"] else "workspace-write"
    if checked.get("sandbox_mode") != expected_sandbox:
        raise ManagementError("generated Codex config sandbox mode mismatch")
    agents = checked.get("agents")
    if isinstance(agents, dict) and {
        "default_subagent_reasoning_effort",
        "default_subagent_model",
    }.intersection(agents):
        raise ManagementError("generated Codex config retained unsupported subagent defaults")
    return output.encode("utf-8")


def _render(
    paths: dict[str, Path],
    profile: dict[str, Any],
    preset_name: str | None = None,
) -> tuple[dict[Path, bytes], dict[str, int], dict[str, Any]]:
    profile = _validate_profile(profile)
    compute_profiles_bytes = _transform_compute_profiles(paths["compute_profiles"], profile)
    compute_catalog = json.loads(compute_profiles_bytes)
    cost_route = _desired_cost_route(preset_name, paths, compute_catalog)
    relay_bytes, relay_count = _transform_relay(paths["relay"], profile)
    cron_bytes, direct_count, harness_count = _transform_cron(paths["cron"], profile, cost_route)
    policy_paths = sorted(paths["policies"].glob("*.yaml"))
    if not policy_paths:
        raise ManagementError(f"no harness policies found in {paths['policies']}")
    rendered = {
        paths["profile"]: _json_bytes(profile),
        paths["cost_route"]: _json_bytes(cost_route),
        paths["relay"]: relay_bytes,
        paths["cron"]: cron_bytes,
        paths["compute_profiles"]: compute_profiles_bytes,
        paths["codex_config"]: _transform_codex_config(paths["codex_config"], profile),
    }
    for path in policy_paths:
        rendered[path] = _transform_policy(path, profile)
    return rendered, {
        "relay_agents": relay_count,
        "direct_jobs": direct_count,
        "harness_dispatch_jobs": harness_count,
        "harness_policies": len(policy_paths),
    }, cost_route


def _current_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _changes(rendered: dict[Path, bytes]) -> dict[Path, bytes]:
    return {path: content for path, content in rendered.items() if _current_bytes(path) != content}


def _legacy_alias_matches(paths: dict[str, Path]) -> bool:
    alias = paths["legacy_relay_alias"]
    return alias.is_symlink() and alias.resolve(strict=False) == paths["relay"].resolve(strict=False)


def _changed_paths(paths: dict[str, Path], rendered: dict[Path, bytes]) -> list[Path]:
    changed = list(_changes(rendered))
    if not _legacy_alias_matches(paths):
        changed.append(paths["legacy_relay_alias"])
    return changed


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _atomic_symlink(path: Path, target: str | Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    os.unlink(tmp_name)
    try:
        os.symlink(str(target), tmp_name)
        os.replace(tmp_name, path)
    finally:
        if os.path.lexists(tmp_name):
            os.unlink(tmp_name)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _backup(paths: dict[str, Path], targets: list[Path], prefix: str = "apply") -> Path:
    root = paths["backup_root"]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    backup_id = f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}"
    destination = root / backup_id
    destination.mkdir(mode=0o700)
    entries = []
    for index, path in enumerate(targets):
        existed = os.path.lexists(path)
        name = f"{index:03d}-{path.name}"
        entry: dict[str, Any] = {"path": str(path), "existed": existed, "backup": name}
        if path.is_symlink():
            entry["kind"] = "symlink"
            entry["link_target"] = os.readlink(path)
        elif existed:
            entry["kind"] = "file"
            content = path.read_bytes()
            backup_path = destination / name
            backup_path.write_bytes(content)
            os.chmod(backup_path, 0o600)
            entry["sha256"] = _sha256(content)
            entry["mode"] = path.stat().st_mode & 0o777
        entries.append(entry)
    manifest = {
        "schema_version": 1,
        "snapshot_type": prefix,
        "backup_id": backup_id,
        "entries": entries,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    os.chmod(manifest_path, 0o600)
    return destination


def _session_observation(
    paths: dict[str, Path],
    profile: dict[str, Any],
) -> tuple[dict[str, int], list[tuple[str, str]]]:
    state_path = paths["relay_state"]
    counts = {"total": 0, "managed": 0, "unmanaged": 0, "matching": 0, "mismatched": 0}
    if not state_path.exists():
        return counts, []
    state = _read_json(state_path)
    if type(state.get("version")) is not int or state.get("version") != 1:
        raise ManagementError("Relay state.json version must be integer 1")
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        raise ManagementError("Relay state.json sessions must be an object")

    relay = _read_json(paths["relay"])
    managed_agents = {agent.get("id") for agent in _relay_agents(relay)}
    if None in managed_agents or any(not isinstance(agent_id, str) for agent_id in managed_agents):
        raise ManagementError("relay.json managed agents must have string ids")

    primary = profile["runtime_priority"][0]
    mismatches: list[tuple[str, str]] = []
    counts["total"] = len(sessions)
    for pool_key, raw_session in sessions.items():
        if not isinstance(pool_key, str) or not isinstance(raw_session, dict):
            raise ManagementError("Relay state.json sessions must map pool keys to objects")
        agent_id = raw_session.get("agentId")
        if agent_id not in managed_agents:
            counts["unmanaged"] += 1
            continue
        counts["managed"] += 1
        runtime = raw_session.get("runtime", "cc")
        if not isinstance(runtime, str) or not runtime:
            raise ManagementError(f"Relay state session runtime must be a string: {pool_key}")
        if runtime == primary:
            counts["matching"] += 1
        else:
            counts["mismatched"] += 1
            mismatches.append((pool_key, runtime))
    return counts, mismatches


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _json_digest(prefix: str, value: object) -> str:
    return prefix + hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _active_cost_route(paths: dict[str, Path]) -> dict[str, Any] | None:
    if not paths["cost_route"].exists():
        return None
    return _validate_active_cost_route(_read_json(paths["cost_route"]))


def _cost_route_digest(route: dict[str, Any] | None) -> str:
    payload: object = route if route is not None else {"state": "absent"}
    return _json_digest("routecfg1_", payload)


def _runtime_profile_digest(profile: dict[str, Any]) -> str:
    return _json_digest("runtimecfg1_", _validate_profile(profile))


def _structured_plan(
    paths: dict[str, Path],
    current_profile: dict[str, Any],
    desired_profile: dict[str, Any],
    target_route: dict[str, Any],
) -> dict[str, Any]:
    active = _active_cost_route(paths)
    route_diff: list[dict[str, Any]] = []
    expected_changed_routes: list[str] = []
    active_routes = active["routes"] if active is not None else {}
    for route_name in sorted(target_route["routes"]):
        before = active_routes.get(route_name)
        after = target_route["routes"][route_name]
        before_effective = (
            before.get("effective_route", {}).get("profile")
            if isinstance(before, dict)
            else None
        )
        after_effective = after["effective_route"]["profile"]
        if before != after:
            route_diff.append(
                {
                    "route_name": route_name,
                    "consumer_path": after["consumer_path"],
                    "from_effective_profile": before_effective,
                    "to_effective_profile": after_effective,
                    "effective_profile_changed": before_effective != after_effective,
                }
            )
        if before_effective != after_effective:
            expected_changed_routes.append(route_name)

    preset_catalog = _validate_cost_presets(_read_json(paths["cost_presets"]))
    allowed_transitions = sorted(
        [source, target]
        for source, targets in ALLOWED_COST_PRESET_TRANSITIONS.items()
        if source is not None
        for target in targets
    )
    current_preset = active["preset"] if active is not None else None
    payload: dict[str, Any] = {
        "schema": "runtime_priority_plan.v1",
        "schema_version": 1,
        "mode": "plan_only",
        "current_preset": current_preset,
        "target_preset": target_route["preset"],
        "active_route_digest": _cost_route_digest(active),
        "target_route_digest": _cost_route_digest(target_route),
        "current_runtime_profile_digest": _runtime_profile_digest(current_profile),
        "target_runtime_profile_digest": _runtime_profile_digest(desired_profile),
        "global_compute_profile_changed": current_profile != desired_profile,
        "route_diff": route_diff,
        "expected_changed_routes": expected_changed_routes,
        "single_consumer_candidate": (
            active is not None
            and current_profile == desired_profile
            and len(expected_changed_routes) == 1
            and len(route_diff) == 1
        ),
        "approved_presets": sorted(preset_catalog["presets"]),
        "allowed_transitions": allowed_transitions,
        "precondition": {
            "active_route_digest": _cost_route_digest(active),
            "current_preset": current_preset,
        },
    }
    payload["plan_digest"] = _json_digest("plan1_", payload)
    return payload


def _print_structured_plan_summary(plan: dict[str, Any]) -> None:
    changed = ",".join(plan["expected_changed_routes"]) or "none"
    print(
        f"active_route_digest={plan['active_route_digest']} "
        f"target_route_digest={plan['target_route_digest']}"
    )
    print(
        f"plan_digest={plan['plan_digest']} expected_changed_routes={changed} "
        f"single_consumer_candidate={'yes' if plan['single_consumer_candidate'] else 'no'} "
        f"global_compute_profile_changed={'yes' if plan['global_compute_profile_changed'] else 'no'}"
    )


def _require_apply_preconditions(args: argparse.Namespace, plan: dict[str, Any]) -> None:
    if args.preset is None:
        return
    expected_active = getattr(args, "expected_active_route_digest", None)
    expected_plan = getattr(args, "expected_plan_digest", None)
    if not expected_active or not expected_plan:
        raise ManagementError(
            "cost preset apply requires --expected-active-route-digest and --expected-plan-digest from plan"
        )
    if expected_active != plan["active_route_digest"]:
        raise ManagementError("apply precondition failed: active route digest changed")
    if expected_plan != plan["plan_digest"]:
        raise ManagementError("apply precondition failed: plan digest changed")


def _print_session_observation(counts: dict[str, int]) -> None:
    print(
        f"observed persisted_sessions={counts['total']} managed={counts['managed']} "
        f"matching={counts['matching']} mismatched={counts['mismatched']} "
        f"unmanaged={counts['unmanaged']}"
    )


def _print_summary(
    profile: dict[str, Any],
    counts: dict[str, int],
    cost_route: dict[str, Any],
) -> None:
    print(
        "priority=" + ",".join(profile["runtime_priority"]),
        f"codex={profile['codex']['model']}/{profile['codex']['reasoning_effort']}",
        f"codex_yolo={'on' if profile['codex']['yolo'] else 'off'}",
        f"claude={profile['claude']['model']}",
    )
    if PLATFORM_CC_ALIAS_FIELD in profile:
        print(f"platform_default_cc={profile[PLATFORM_CC_ALIAS_FIELD] or 'shared'}")
    print(
        f"managed relay_agents={counts['relay_agents']} direct_jobs={counts['direct_jobs']} "
        f"harness_policies={counts['harness_policies']} "
        f"excluded_harness_dispatch_jobs={counts['harness_dispatch_jobs']}"
    )
    print(f"cost_preset={cost_route['preset']} status={cost_route['status']}")
    for route_name, route in cost_route["routes"].items():
        print(
            f"route={route_name} consumer={route['consumer_path']} "
            f"workload={route['workload_shape']} risk={route['risk_class']} "
            f"base={route['base_route']['profile'] or 'none'} "
            f"floor={route['risk_floor']['profile'] or 'none'} "
            f"effective={route['effective_route']['profile'] or 'none'} "
            f"verifier={route['verifier_id']} rollback={route['rollback_profile']}"
        )


def _plan_or_apply(args: argparse.Namespace, apply: bool) -> int:
    paths = _paths()
    current = _profile_from_disk(paths["profile"])
    desired = _desired_profile(args, current, paths["compute_profiles"])
    rendered, counts, cost_route = _render(paths, desired, args.preset)
    plan = _structured_plan(paths, current, desired, cost_route)
    changed = _changed_paths(paths, rendered)
    if not apply and getattr(args, "json", False):
        print(json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    _print_summary(desired, counts, cost_route)
    _print_structured_plan_summary(plan)
    session_counts, _ = _session_observation(paths, desired)
    _print_session_observation(session_counts)
    print(f"changed_files={len(changed)}")
    for path in changed:
        print(f"  {path}")
    if not apply:
        print("PLAN ONLY: no files written")
        return 0

    _require_apply_preconditions(args, plan)
    if not changed:
        print("APPLY: already converged")
        return 0
    with _lock(paths["lock"]):
        # Re-read every precondition-bearing input under the cooperative lock.
        # A plan that was valid before lock acquisition must not authorize a
        # write after another manager invocation changed active state.
        locked_current = _profile_from_disk(paths["profile"])
        locked_desired = _desired_profile(args, locked_current, paths["compute_profiles"])
        rendered, counts, cost_route = _render(paths, locked_desired, args.preset)
        locked_plan = _structured_plan(paths, locked_current, locked_desired, cost_route)
        _require_apply_preconditions(args, locked_plan)
        changed = _changed_paths(paths, rendered)
        if not changed:
            print("APPLY: already converged after lock")
            return 0
        backup = _backup(paths, changed)
        rendered_changes = _changes(rendered)
        for path, content in rendered_changes.items():
            _atomic_write(path, content)
        if paths["legacy_relay_alias"] in changed:
            _atomic_symlink(paths["legacy_relay_alias"], paths["relay"])
        verify_rendered, _, _ = _render(paths, locked_desired, cost_route["preset"])
        residual = _changed_paths(paths, verify_rendered)
        if residual:
            raise ManagementError("post-apply verification failed: " + ", ".join(map(str, residual)))
    print(f"APPLY OK backup={backup.name}")
    if rendered_changes:
        print("Relay conversation defaults require one safe Relay restart; cron reloads on SIGHUP/restart.")
    else:
        print("Legacy Relay config alias converged; no Relay restart required.")
    return 0


def _verify() -> int:
    paths = _paths()
    profile = _validate_profile(_read_json(paths["profile"]))
    rendered, counts, cost_route = _render(paths, profile)
    residual = _changed_paths(paths, rendered)
    session_counts, session_mismatches = _session_observation(paths, profile)
    _print_summary(profile, counts, cost_route)
    _print_session_observation(session_counts)
    if residual or session_mismatches:
        if residual:
            print("VERIFY FAILED drifted files:", file=sys.stderr)
            for path in residual:
                print(f"  {path}", file=sys.stderr)
        if session_mismatches:
            print("VERIFY FAILED persisted session runtime drift:", file=sys.stderr)
            for pool_key, runtime in session_mismatches:
                print(
                    f"  {pool_key}: runtime={runtime} expected={profile['runtime_priority'][0]}",
                    file=sys.stderr,
                )
        return 1
    print("VERIFY OK: all managed surfaces match the runtime profile")
    return 0


def _show() -> int:
    paths = _paths()
    profile = _profile_from_disk(paths["profile"])
    _, counts, cost_route = _render(paths, profile)
    print(json.dumps(profile, indent=2, ensure_ascii=False))
    print(json.dumps(cost_route, indent=2, ensure_ascii=False))
    _print_summary(profile, counts, cost_route)
    session_counts, _ = _session_observation(paths, profile)
    _print_session_observation(session_counts)
    return 0


def _list_backups() -> int:
    root = _paths()["backup_root"]
    if not root.exists():
        print("no backups")
        return 0
    for path in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True):
        print(path.name)
    return 0


def _rollback(backup_id: str | None) -> int:
    paths = _paths()
    root = paths["backup_root"]
    candidates = sorted(
        (item for item in root.glob("apply-*") if item.is_dir()),
        reverse=True,
    ) if root.exists() else []
    target = root / backup_id if backup_id else (candidates[0] if candidates else None)
    if target is None or not target.is_dir() or target.parent != root:
        raise ManagementError(f"backup not found: {backup_id or 'latest'}")
    manifest = _read_json(target / "manifest.json")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManagementError(f"invalid backup manifest: {target}")
    restore_paths = [Path(entry["path"]) for entry in entries]
    with _lock(paths["lock"]):
        safety = _backup(paths, restore_paths, prefix="pre-rollback")
        for entry, path in zip(entries, restore_paths, strict=True):
            if entry.get("existed"):
                kind = entry.get("kind", "file")
                if kind == "symlink":
                    link_target = entry.get("link_target")
                    if not isinstance(link_target, str) or not link_target:
                        raise ManagementError(f"invalid symlink backup entry: {path}")
                    _atomic_symlink(path, link_target)
                elif kind == "file":
                    source = target / entry["backup"]
                    content = source.read_bytes()
                    if _sha256(content) != entry.get("sha256"):
                        raise ManagementError(f"backup checksum mismatch: {source}")
                    _atomic_write(path, content)
                    os.chmod(path, int(entry.get("mode", 0o600)))
                else:
                    raise ManagementError(f"unsupported backup entry kind: {kind}")
            elif os.path.lexists(path):
                path.unlink()
    print(f"ROLLBACK OK restored={target.name} safety_backup={safety.name}")
    print("Run verify against the restored profile, then safely restart Relay if required.")
    return 0


def _backend_media_uat_render(paths: dict[str, Path]) -> tuple[dict[Path, bytes], dict[str, int]]:
    cron_bytes, direct, harness = _transform_backend_media_uat_cron(paths["cron"])
    policy_paths = sorted(paths["policies"].glob("*.yaml"))
    if not policy_paths:
        raise ManagementError(f"no harness policies found in {paths['policies']}")
    rendered = {paths["cron"]: cron_bytes}
    for path in policy_paths:
        rendered[path] = _transform_backend_media_uat_policy(path)
    return rendered, {
        "direct_jobs": direct,
        "harness_dispatch_jobs": harness,
        "harness_policies": len(policy_paths),
    }


def _backend_media_uat_managed_paths(paths: dict[str, Path]) -> list[Path]:
    policy_paths = sorted(paths["policies"].glob("*.yaml"))
    if not policy_paths:
        raise ManagementError(f"no harness policies found in {paths['policies']}")
    return [paths["cron"], *policy_paths]


def _backend_media_uat_snapshot(
    paths: dict[str, Path],
    target: Path,
) -> list[tuple[Path, bytes, int]]:
    """Validate a complete UAT snapshot before rollback writes any file."""
    root = paths["backup_root"]
    if (
        target.parent != root
        or target.is_symlink()
        or not target.is_dir()
        or not target.name.startswith(f"{BACKEND_MEDIA_UAT_BACKUP_PREFIX}-")
    ):
        raise ManagementError(f"invalid backend media UAT snapshot provenance: {target}")

    manifest_path = target / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ManagementError(f"invalid backend media UAT backup manifest: {target}")
    manifest = _read_json(manifest_path)
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("snapshot_type") != BACKEND_MEDIA_UAT_BACKUP_PREFIX
        or manifest.get("backup_id") != target.name
    ):
        raise ManagementError(f"invalid backend media UAT snapshot identity: {target}")

    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManagementError(f"invalid backend media UAT backup manifest: {target}")

    managed_paths = _backend_media_uat_managed_paths(paths)
    expected = {path.resolve(strict=False) for path in managed_paths}
    if len(expected) != len(managed_paths):
        raise ManagementError("backend media UAT managed target set contains duplicates")

    seen_paths: set[Path] = set()
    seen_backups: set[str] = set()
    validated: list[tuple[Path, bytes, int]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ManagementError(f"invalid backend media UAT backup entry: {target}")
        raw_path = entry.get("path")
        backup_name = entry.get("backup")
        mode = entry.get("mode")
        checksum = entry.get("sha256")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or entry.get("existed") is not True
            or entry.get("kind") != "file"
            or not isinstance(backup_name, str)
            or not backup_name
            or Path(backup_name).name != backup_name
            or Path(backup_name).is_absolute()
            or backup_name in seen_backups
            or type(mode) is not int
            or not 0 <= mode <= 0o7777
            or not isinstance(checksum, str)
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        ):
            raise ManagementError(f"invalid backend media UAT backup entry: {target}")

        path = Path(raw_path)
        resolved_path = path.resolve(strict=False)
        if (
            not path.is_absolute()
            or path != resolved_path
            or resolved_path not in expected
            or resolved_path in seen_paths
            or path.is_symlink()
            or not path.is_file()
        ):
            raise ManagementError(f"invalid backend media UAT backup target: {raw_path}")

        source = target / backup_name
        if source.parent != target or source.is_symlink() or not source.is_file():
            raise ManagementError(f"unsafe backend media UAT backup path: {source}")
        content = source.read_bytes()
        if _sha256(content) != checksum:
            raise ManagementError(f"backup checksum mismatch: {source}")

        seen_paths.add(resolved_path)
        seen_backups.add(backup_name)
        validated.append((path, content, mode))

    if seen_paths != expected or len(validated) != len(managed_paths):
        raise ManagementError("backend media UAT backup target set mismatch")
    return validated


def _backend_media_uat_catalog(paths: dict[str, Path]) -> dict[str, dict[str, str]]:
    """Validate the named UAT profile without resolving versioned model IDs."""
    catalog = _read_json(paths["compute_profiles"])
    if catalog.get("schema_version") != 2 or not isinstance(catalog.get("profiles"), dict):
        raise ManagementError("backend media UAT compute profile catalog is invalid")
    media = catalog["profiles"].get(BACKEND_MEDIA_UAT_PROFILE)
    if not isinstance(media, dict):
        raise ManagementError("backend media UAT media profile is missing")
    result: dict[str, dict[str, str]] = {}
    for provider in ("codex", "cc"):
        entry = media.get(provider)
        if not isinstance(entry, dict):
            raise ManagementError(f"backend media UAT media.{provider} is missing")
        alias = entry.get("model_alias")
        effort = entry.get("reasoning_effort")
        if not isinstance(alias, str) or not alias.strip() or not isinstance(effort, str) or not effort.strip():
            raise ManagementError(f"backend media UAT media.{provider} needs model_alias and reasoning_effort")
        result[provider] = {"model_alias": alias, "reasoning_effort": effort}
    return result


def _print_backend_media_uat_summary(counts: dict[str, int], catalog: dict[str, dict[str, str]]) -> None:
    print(
        f"backend_media_uat_profile={BACKEND_MEDIA_UAT_PROFILE} "
        f"direct_jobs={counts['direct_jobs']} "
        f"harness_policies={counts['harness_policies']} "
        f"excluded_harness_dispatch_jobs={counts['harness_dispatch_jobs']}"
    )
    for provider in ("codex", "cc"):
        profile = catalog[provider]
        print(
            f"backend_media_uat_{provider}="
            f"{profile['model_alias']}/{profile['reasoning_effort']}"
        )


def _relay_pid_preflight(paths: dict[str, Path]) -> int:
    pid_path = paths["relay_pid"]
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError) as exc:
        raise ManagementError(f"relay reload preflight failed: invalid PID file {pid_path}") from exc
    if pid <= 1:
        raise ManagementError(f"relay reload preflight failed: unsafe PID {pid}")
    try:
        os.kill(pid, 0)
    except OSError as exc:
        raise ManagementError(f"relay reload preflight failed: PID {pid} is not alive") from exc
    return pid


def _reload_relay(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGHUP)
    except OSError as exc:
        raise ManagementError(f"relay reload failed: SIGHUP PID {pid}") from exc


def _backend_media_uat_plan_or_apply(apply: bool, reload_relay: bool = False) -> int:
    paths = _paths()
    rendered, counts = _backend_media_uat_render(paths)
    catalog = _backend_media_uat_catalog(paths)
    changed = _changes(rendered)
    _print_backend_media_uat_summary(counts, catalog)
    print(f"changed_files={len(changed)}")
    for path in changed:
        print(f"  {path}")
    if not apply:
        print("PLAN ONLY: no files written")
        return 0
    pid = _relay_pid_preflight(paths) if reload_relay else None
    if not changed:
        if pid is not None:
            _reload_relay(pid)
            print("BACKEND MEDIA UAT APPLY: already converged; relay reloaded")
            print(f"RELAY RELOAD OK pid={pid} signal=SIGHUP")
        else:
            print("BACKEND MEDIA UAT APPLY: already converged")
        return 0
    with _lock(paths["lock"]):
        rendered, counts = _backend_media_uat_render(paths)
        _backend_media_uat_catalog(paths)
        changed = _changes(rendered)
        if not changed:
            if pid is not None:
                _reload_relay(pid)
                print("BACKEND MEDIA UAT APPLY: already converged after lock; relay reloaded")
                print(f"RELAY RELOAD OK pid={pid} signal=SIGHUP")
            else:
                print("BACKEND MEDIA UAT APPLY: already converged after lock")
            return 0
        backup = _backup(
            paths,
            _backend_media_uat_managed_paths(paths),
            prefix=BACKEND_MEDIA_UAT_BACKUP_PREFIX,
        )
        for path, content in _changes(rendered).items():
            _atomic_write(path, content)
        residual = _changes(_backend_media_uat_render(paths)[0])
        if residual:
            raise ManagementError("backend media UAT post-apply verification failed: " + ", ".join(map(str, residual)))
        if pid is not None:
            _reload_relay(pid)
    print(f"BACKEND MEDIA UAT APPLY OK backup={backup.name}")
    print(f"RELAY RELOAD OK pid={pid} signal=SIGHUP" if pid is not None else "Relay reload/restart is intentionally not performed by this command.")
    return 0


def _backend_media_uat_verify() -> int:
    paths = _paths()
    rendered, counts = _backend_media_uat_render(paths)
    catalog = _backend_media_uat_catalog(paths)
    residual = _changes(rendered)
    _print_backend_media_uat_summary(counts, catalog)
    if residual:
        print("VERIFY FAILED backend media UAT drift:", file=sys.stderr)
        for path in residual:
            print(f"  {path}", file=sys.stderr)
        return 1
    print("VERIFY OK: backend direct cron and harness policy profiles are media")
    return 0


def _backend_media_uat_rollback(backup_id: str | None, reload_relay: bool = False) -> int:
    paths = _paths()
    root = paths["backup_root"]
    candidates = sorted(
        (item for item in root.glob(f"{BACKEND_MEDIA_UAT_BACKUP_PREFIX}-*") if item.is_dir()),
        reverse=True,
    ) if root.exists() else []
    if backup_id is not None and (
        Path(backup_id).name != backup_id
        or not backup_id.startswith(f"{BACKEND_MEDIA_UAT_BACKUP_PREFIX}-")
    ):
        raise ManagementError(f"backend media UAT backup not found: {backup_id}")
    target = root / backup_id if backup_id else (candidates[0] if candidates else None)
    if target is None or target.parent != root:
        raise ManagementError(f"backend media UAT backup not found: {backup_id or 'latest'}")
    pid = _relay_pid_preflight(paths) if reload_relay else None
    _backend_media_uat_snapshot(paths, target)
    with _lock(paths["lock"]):
        # Revalidate under the cooperative manager lock so no stale payload is
        # restored if another manager changed the snapshot after preflight.
        validated = _backend_media_uat_snapshot(paths, target)
        restore_paths = [path for path, _, _ in validated]
        safety = _backup(paths, restore_paths, prefix="pre-backend-media-uat-rollback")
        for path, content, mode in validated:
            _atomic_write(path, content)
            os.chmod(path, mode)
        if pid is not None:
            _reload_relay(pid)
    print(f"BACKEND MEDIA UAT ROLLBACK OK restored={target.name} safety_backup={safety.name}")
    print(f"RELAY RELOAD OK pid={pid} signal=SIGHUP" if pid is not None else "Run backend-media-uat-verify only to check an active overlay; do not expect it to pass after rollback.")
    return 0


def _add_profile_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preset", help="tracked cost route preset")
    parser.add_argument("--priority", help="comma-separated priority, e.g. codex or codex,cc")
    parser.add_argument("--codex-model")
    parser.add_argument("--codex-reasoning-effort")
    parser.add_argument("--codex-yolo", choices=("on", "off"))
    parser.add_argument("--claude-model")
    parser.add_argument(
        "--platform-default-cc",
        choices=(FABLE_CC_ALIAS, "shared"),
        help="select Fable latest or the shared Claude model for the Relay platform default",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="preview convergent changes")
    _add_profile_options(plan)
    plan.add_argument("--json", action="store_true", help="emit only runtime_priority_plan.v1 JSON")
    apply = subparsers.add_parser("apply", help="backup and apply changes")
    _add_profile_options(apply)
    apply.add_argument("--expected-active-route-digest")
    apply.add_argument("--expected-plan-digest")
    subparsers.add_parser("verify", help="check all managed surfaces for drift")
    subparsers.add_parser("show", help="show the active profile and managed counts")
    subparsers.add_parser("backups", help="list rollback snapshots")
    rollback = subparsers.add_parser("rollback", help="restore an apply snapshot")
    rollback.add_argument("backup_id", nargs="?")
    subparsers.add_parser("backend-media-uat-plan", help="preview backend-only media UAT overlay")
    uat_apply = subparsers.add_parser("backend-media-uat-apply", help="backup and apply backend-only media UAT overlay")
    uat_apply.add_argument("--reload-relay", action="store_true", help="preflight relay PID then SIGHUP after apply")
    subparsers.add_parser("backend-media-uat-verify", help="verify backend-only media UAT overlay")
    uat_rollback = subparsers.add_parser("backend-media-uat-rollback", help="restore a backend-only media UAT snapshot")
    uat_rollback.add_argument("backup_id", nargs="?")
    uat_rollback.add_argument("--reload-relay", action="store_true", help="preflight relay PID then SIGHUP after rollback")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            return _plan_or_apply(args, apply=False)
        if args.command == "apply":
            return _plan_or_apply(args, apply=True)
        if args.command == "verify":
            return _verify()
        if args.command == "show":
            return _show()
        if args.command == "backups":
            return _list_backups()
        if args.command == "rollback":
            return _rollback(args.backup_id)
        if args.command == "backend-media-uat-plan":
            return _backend_media_uat_plan_or_apply(apply=False)
        if args.command == "backend-media-uat-apply":
            return _backend_media_uat_plan_or_apply(apply=True, reload_relay=args.reload_relay)
        if args.command == "backend-media-uat-verify":
            return _backend_media_uat_verify()
        if args.command == "backend-media-uat-rollback":
            return _backend_media_uat_rollback(args.backup_id, reload_relay=args.reload_relay)
        raise ManagementError(f"unsupported command: {args.command}")
    except (ManagementError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
