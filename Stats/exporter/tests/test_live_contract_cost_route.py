from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pulse_stats.live_contract import CostRouteError, _resolve_codex_probe_route


NOW = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _route(path: Path, profile: str = "economy") -> Path:
    return _write(path, {
        "schema_version": 1,
        "preset": "phase2_stats_canary",
        "status": "canary",
        "routes": {
            "stats_quota_probe": {
                "consumer_path": "stats.quota_probe",
                "workload_shape": "repeatable",
                "risk_class": "standard",
                "verifier_id": "stats.token_count.window_reset.v1",
                "base_route": {"profile": profile},
                "risk_floor": {"profile": None},
                "effective_route": {"profile": profile},
                "rollback_profile": "platform_default",
            },
            "hestia_30min": {
                "consumer_path": "relay.cron.hestia_30min",
                "workload_shape": "deterministic",
                "risk_class": "standard",
                "verifier_id": "hestia.new_events.gate.v1",
                "base_route": {"profile": None},
                "risk_floor": {"profile": None},
                "effective_route": {"profile": None},
                "rollback_profile": "media",
                "escalation_profile": "media",
            },
            "hestia_weekly_review": {
                "consumer_path": "relay.cron.hestia_weekly_review",
                "workload_shape": "routine_judgment",
                "risk_class": "standard",
                "verifier_id": "hestia.weekly_review.draft.v1",
                "base_route": {"profile": "platform_default"},
                "risk_floor": {"profile": None},
                "effective_route": {"profile": "platform_default"},
                "rollback_profile": "platform_default",
            },
            "harness_pending_handoffs": {
                "consumer_path": "harness.hit.pending_handoffs",
                "workload_shape": "routine_judgment",
                "risk_class": "critical_ambiguous",
                "verifier_id": "harness.handoff.outcome.v1",
                "base_route": {"profile": "frontier"},
                "risk_floor": {"profile": "frontier"},
                "effective_route": {"profile": "frontier"},
                "rollback_profile": "frontier",
            },
            "harness_stale_work_items": {
                "consumer_path": "harness.hit.stale_work_items",
                "workload_shape": "routine_judgment",
                "risk_class": "critical_ambiguous",
                "verifier_id": "harness.work_item.outcome.v1",
                "base_route": {"profile": "frontier"},
                "risk_floor": {"profile": "frontier"},
                "effective_route": {"profile": "frontier"},
                "rollback_profile": "frontier",
            },
        },
    })


def _profiles(path: Path, effort: str = "low") -> Path:
    return _write(path, {
        "schema_version": 2,
        "profiles": {
            "economy": {
                "cc": {"model_alias": "claude-haiku-latest", "reasoning_effort": "low"},
                "codex": {"model_alias": "codex-luna-latest", "reasoning_effort": effort},
            }
        },
    })


def _cache(path: Path, *, fetched_at: datetime = NOW) -> Path:
    return _write(path, {
        "fetched_at": fetched_at.isoformat(),
        "models": [
            {
                "slug": "gpt-5.6-luna",
                "visibility": "list",
                "supported_reasoning_levels": [{"effort": "low"}],
            },
            {
                "slug": "gpt-6.1-luna",
                "visibility": "list",
                "supported_reasoning_levels": [{"effort": "low"}],
            },
            {
                "slug": "gpt-9.0-luna",
                "visibility": "hide",
                "supported_reasoning_levels": [{"effort": "low"}],
            },
        ],
    })


def test_stats_route_resolves_luna_low_from_visible_fresh_catalog(tmp_path: Path) -> None:
    resolved = _resolve_codex_probe_route(
        _route(tmp_path / "route.json"),
        _profiles(tmp_path / "profiles.json"),
        _cache(tmp_path / "cache.json"),
        now=NOW,
    )

    assert resolved == ("gpt-6.1-luna", "low")
    assert "gpt-" not in (tmp_path / "route.json").read_text()
    assert "gpt-" not in (tmp_path / "profiles.json").read_text()


@pytest.mark.parametrize(
    "failure",
    [
        "only_stats",
        "missing_root",
        "missing_base",
        "missing_floor",
        "missing_rollback",
        "extra_route",
        "bogus_preset",
        "status_mismatch",
        "profile_mismatch",
    ],
)
def test_active_manifest_schema_is_fully_fail_closed(tmp_path: Path, failure: str) -> None:
    route = _route(tmp_path / "route.json")
    manifest = json.loads(route.read_text())
    stats = manifest["routes"]["stats_quota_probe"]
    if failure == "only_stats":
        manifest["routes"] = {"stats_quota_probe": stats}
    elif failure == "missing_root":
        del manifest["status"]
    elif failure == "missing_base":
        del stats["base_route"]
    elif failure == "missing_floor":
        del stats["risk_floor"]
    elif failure == "missing_rollback":
        del stats["rollback_profile"]
    elif failure == "extra_route":
        manifest["routes"]["unexpected"] = dict(stats)
    elif failure == "bogus_preset":
        manifest["preset"] = "bogus"
    elif failure == "status_mismatch":
        manifest["status"] = "baseline"
    else:
        stats["base_route"]["profile"] = "platform_default"
        stats["effective_route"]["profile"] = "platform_default"
    _write(route, manifest)

    with pytest.raises(CostRouteError):
        _resolve_codex_probe_route(
            route,
            _profiles(tmp_path / "profiles.json"),
            _cache(tmp_path / "cache.json"),
            now=NOW,
        )


def test_only_stats_manifest_materializes_no_probe_argv(tmp_path: Path) -> None:
    route = _route(tmp_path / "route.json")
    manifest = json.loads(route.read_text())
    manifest["routes"] = {"stats_quota_probe": manifest["routes"]["stats_quota_probe"]}
    _write(route, manifest)
    exporter = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(exporter),
        "PULSE_RUNTIME_COST_ROUTE_PATH": str(route),
        "PULSE_COMPUTE_PROFILES_PATH": str(_profiles(tmp_path / "profiles.json")),
        "PULSE_CODEX_MODELS_CACHE_PATH": str(_cache(tmp_path / "cache.json")),
    })
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pulse_stats.live_contract import CODEX_PROBE_ARGV, CODEX_PROBE_ROUTE_ERROR; "
                "assert CODEX_PROBE_ARGV is None; assert CODEX_PROBE_ROUTE_ERROR; print('disabled')"
            ),
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "disabled"


@pytest.mark.parametrize("failure", ["route", "profiles", "cache", "effort"])
def test_stats_route_failures_never_fall_back_to_platform_default(
    tmp_path: Path,
    failure: str,
) -> None:
    route = _route(tmp_path / "route.json")
    profiles = _profiles(tmp_path / "profiles.json")
    cache = _cache(tmp_path / "cache.json")
    if failure == "route":
        route.write_text('{"schema_version": true}', encoding="utf-8")
    elif failure == "profiles":
        profiles.write_text('{"schema_version": 1}', encoding="utf-8")
    elif failure == "cache":
        _cache(cache, fetched_at=NOW - timedelta(days=8))
    else:
        _profiles(profiles, effort="maximal")

    with pytest.raises(CostRouteError):
        _resolve_codex_probe_route(route, profiles, cache, now=NOW)


def test_missing_route_materializes_no_probe_argv_and_starts_no_process(tmp_path: Path) -> None:
    missing = tmp_path / "missing-route.json"
    exporter = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(exporter),
        "PULSE_RUNTIME_COST_ROUTE_PATH": str(missing),
        "PULSE_COMPUTE_PROFILES_PATH": str(tmp_path / "missing-profiles.json"),
        "PULSE_CODEX_MODELS_CACHE_PATH": str(tmp_path / "missing-cache.json"),
    })
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from unittest import mock; "
                "from pulse_stats.live_contract import CODEX_PROBE_ARGV, CODEX_PROBE_ROUTE_ERROR; "
                "from pulse_stats.usage_sources import run_codex_probe; "
                "assert CODEX_PROBE_ARGV is None; assert CODEX_PROBE_ROUTE_ERROR; "
                "p=mock.patch('pulse_stats.usage_sources.subprocess.run'); run=p.start(); "
                "assert run_codex_probe(CODEX_PROBE_ARGV) is False; run.assert_not_called(); "
                "p.stop(); print('disabled')"
            ),
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "disabled"


def test_valid_economy_route_materializes_luna_low_argv_in_clean_process(tmp_path: Path) -> None:
    route = _route(tmp_path / "route.json")
    profiles = _profiles(tmp_path / "profiles.json")
    cache = _cache(tmp_path / "cache.json", fetched_at=datetime.now(timezone.utc))
    exporter = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(exporter),
        "PULSE_RUNTIME_COST_ROUTE_PATH": str(route),
        "PULSE_COMPUTE_PROFILES_PATH": str(profiles),
        "PULSE_CODEX_MODELS_CACHE_PATH": str(cache),
    })
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pulse_stats.live_contract import CODEX_PROBE_ARGV as a; "
                "assert a is not None; "
                "assert a[1:4] == ('--ask-for-approval', 'never', 'exec'); "
                "assert a[a.index('-m')+1] == 'gpt-6.1-luna'; "
                "assert 'model_reasoning_effort=\"low\"' in a; "
                "assert a[a.index('--sandbox')+1] == 'read-only'; "
                "assert a.index('--ask-for-approval') < a.index('exec'); print('enabled')"
            ),
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "enabled"
