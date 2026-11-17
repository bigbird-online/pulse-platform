"""Strict read-side join for durable Harness ComputeCost evidence.

This adapter consumes repository-local SCL artifacts without importing Harness.
It never reads provider transcripts or persists raw runtime identities.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from .cc_usage import CC_RUNTIMES, CC_SNAPSHOT_SCHEMA, validate_cc_session_snapshot
from .token_semantics import InputTokenClasses
from .pi_usage import (
    PI_SNAPSHOT_SCHEMA,
    PiUsageError,
    validate_pi_attribution_boundary,
    validate_pi_session_snapshot,
    validate_pi_usage_summary,
)


# v3：每个 epoch 增加 `cc_usage`，与 `pi_usage` 同形状，承载 SCL 封存的 CC 有界用量。
SCL_MEASUREMENT_SCHEMA = "computecost_scl_measurement_join/v3"


@dataclass(frozen=True)
class _NativeFamily:
    """One Harness-sealed native usage producer (Pi extension or Relay-observed CC)."""

    name: str
    label: str
    runtimes: frozenset[str]
    snapshot_schema: str
    measurement_schema: str
    epoch_key: str
    missing_producer: str
    validate_snapshot: Callable[[object], None]
    source_manifest: bool  # CC 位置携带逐文件清单（主 transcript + 子代理）

    @property
    def directory(self) -> str:
        return self.measurement_schema.replace("/", ".")


_NATIVE_FAMILIES = (
    _NativeFamily("pi", "Pi", frozenset({"pi"}), PI_SNAPSHOT_SCHEMA, "scl_pi_usage_measurement/v1",
                  "pi_usage", "pi_bounded_usage", validate_pi_session_snapshot, False),
    _NativeFamily("cc", "CC", CC_RUNTIMES, CC_SNAPSHOT_SCHEMA, "scl_cc_usage_measurement/v1",
                  "cc_usage", "cc_bounded_usage", validate_cc_session_snapshot, True),
)


def _family_for_runtime(runtime: object) -> _NativeFamily | None:
    return next((family for family in _NATIVE_FAMILIES if runtime in family.runtimes), None)
_WI_RE = re.compile(r"^wi_\d{8}_\d{3}$")
_HEX_REF = {
    "work_item_ref": re.compile(r"^work_item_v1_[0-9a-f]{64}$"),
    "measurement_epoch_ref": re.compile(r"^measurement_epoch_v1_[0-9a-f]{64}$"),
    "lineage_root_ref": re.compile(r"^lineage_v1_[0-9a-f]{64}$"),
    "seal_id": re.compile(r"^epoch_token_seal_v1_[0-9a-f]{64}$"),
    "task_ref": re.compile(r"^task_v1_[0-9a-f]{64}$"),
    "outcome_ref": re.compile(r"^outcome_v1_[0-9a-f]{64}$"),
    "event_ref": re.compile(r"^human_attention_v1_[0-9a-f]{64}$"),
}
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_REF = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[^\x00]+$")
_TOKEN_FIELDS = {
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
}
_FORBIDDEN_KEYS = {
    "session_id", "sessionid", "thread_id", "threadid", "turn_id", "turnid",
    "request_id", "requestid", "message_id", "messageid", "prompt", "response",
    "credential", "api_key", "cwd", "path",
}


class SclMeasurementError(ValueError):
    """Raised when durable SCL evidence is unsafe, malformed, or ambiguous."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _domain_ref(prefix: str, domain: str, *parts: str) -> str:
    digest = sha256(domain.encode("utf-8"))
    for part in parts:
        digest.update(b"\0")
        digest.update(part.strip().encode("utf-8"))
    return prefix + digest.hexdigest()


def _walk_privacy(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise SclMeasurementError("SCL evidence contains a forbidden raw identity")
            _walk_privacy(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk_privacy(nested)


def _read(path: Path, *, require_mode_0600: bool = False) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SclMeasurementError("SCL evidence must be a regular non-symlink file")
    if require_mode_0600 and (path.stat().st_mode & 0o777) != 0o600:
        raise SclMeasurementError("native SCL measurement must be a mode-0600 regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SclMeasurementError("SCL evidence is unreadable") from exc
    if not isinstance(value, dict):
        raise SclMeasurementError("SCL evidence must be an object")
    _walk_privacy(value)
    return value


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise SclMeasurementError(f"{label} fields do not match the contract")


def _tokens(value: object, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _TOKEN_FIELDS:
        raise SclMeasurementError(f"{label} Token shape is invalid")
    result: dict[str, int] = {}
    for field in _TOKEN_FIELDS:
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise SclMeasurementError(f"{label}.{field} is invalid")
        result[field] = item
    if result["cached_input_tokens"] > result["input_tokens"]:
        raise SclMeasurementError(f"{label} cached input exceeds input")
    if result["reasoning_output_tokens"] > result["output_tokens"]:
        raise SclMeasurementError(f"{label} reasoning output exceeds output")
    if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        raise SclMeasurementError(f"{label} Token total does not conserve")
    return result


def _validate_attribution(value: Mapping[str, Any], work_item_id: str) -> dict[str, Any]:
    _exact(value, {
        "schema", "schema_version", "work_item_id", "work_item_ref",
        "measurement_epoch_ref", "measurement_epoch_label", "execution_epoch",
        "execution_phase", "execution_runtime", "agent_id", "lineage_root_ref",
        "operation_ref", "started_at", "ended_at", "boundary_policy",
    }, "cost attribution")
    if value.get("schema") != "work_item_cost_attribution/v1" or value.get("schema_version") != 1:
        raise SclMeasurementError("cost attribution schema is unsupported")
    if value.get("work_item_id") != work_item_id:
        raise SclMeasurementError("cost attribution belongs to another WI")
    expected_wi_ref = _domain_ref("work_item_v1_", "pulse-work-item-v1", work_item_id)
    if value.get("work_item_ref") != expected_wi_ref:
        raise SclMeasurementError("cost attribution WI ref is invalid")
    for field in ("measurement_epoch_ref", "lineage_root_ref"):
        item = value.get(field)
        if not isinstance(item, str) or not _HEX_REF[field].fullmatch(item):
            raise SclMeasurementError(f"cost attribution {field} is invalid")
    epoch = value.get("execution_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise SclMeasurementError("cost attribution execution epoch is invalid")
    for field in ("measurement_epoch_label", "execution_phase", "execution_runtime", "agent_id", "started_at"):
        if not isinstance(value.get(field), str) or not str(value[field]).strip():
            raise SclMeasurementError(f"cost attribution {field} is invalid")
    if value.get("boundary_policy") != "prospective_exact_lineage_no_backfill":
        raise SclMeasurementError("cost attribution boundary policy is invalid")
    return {
        "measurement_epoch_ref": value["measurement_epoch_ref"],
        "execution_epoch": epoch,
        "phase": value["execution_phase"],
        "runtime": value["execution_runtime"],
        "agent_id": value["agent_id"],
        "lineage_root_ref": value["lineage_root_ref"],
        "started_at": value["started_at"],
        "ended_at": value["ended_at"],
        "content_sha256": sha256(_canonical(value)).hexdigest(),
    }


def _validate_seal(value: Mapping[str, Any], work_item_ref: str) -> dict[str, Any]:
    expected = {
        "schema", "schema_version", "work_item_ref", "measurement_epoch_ref", "phase",
        "execution_epoch", "execution_surface", "runtime", "procurement_source",
        "usage_producer", "provider", "model", "reasoning_effort", "thread_ref",
        "lineage_root_ref", "seal_id", "baseline_observation_id", "final_observation_id",
        "final_completion_id", "final_turn_ref", "sealed_after_sequence", "baseline_total",
        "final_total", "delta", "final_turn_completed", "accounting", "coverage",
    }
    _exact(value, expected, "SCL Token seal")
    if value.get("schema") != "app_server_epoch_token_seal.v1" or value.get("schema_version") != 1:
        raise SclMeasurementError("SCL Token seal schema is unsupported")
    if value.get("work_item_ref") != work_item_ref:
        raise SclMeasurementError("SCL Token seal belongs to another WI")
    for field in ("measurement_epoch_ref", "lineage_root_ref", "seal_id"):
        item = value.get(field)
        if not isinstance(item, str) or not _HEX_REF[field].fullmatch(item):
            raise SclMeasurementError(f"SCL Token seal {field} is invalid")
    baseline = _tokens(value.get("baseline_total"), "baseline_total")
    final = _tokens(value.get("final_total"), "final_total")
    delta = _tokens(value.get("delta"), "delta")
    if any(final[field] - baseline[field] != delta[field] for field in _TOKEN_FIELDS):
        raise SclMeasurementError("SCL Token seal delta does not conserve")
    if value.get("final_turn_completed") is not True:
        raise SclMeasurementError("SCL Token seal lacks final completion")
    return {
        "seal_id": value["seal_id"],
        "measurement_epoch_ref": value["measurement_epoch_ref"],
        "execution_epoch": value["execution_epoch"],
        "phase": value["phase"],
        "runtime": value["runtime"],
        "execution_surface": value["execution_surface"],
        "usage_producer": value["usage_producer"],
        "procurement_source": value["procurement_source"],
        "model": value["model"],
        "reasoning_effort": value["reasoning_effort"],
        "tokens": {**delta, "cache_write_input_tokens": "unavailable"},
        "coverage": value["coverage"],
        "content_sha256": sha256(_canonical(value)).hexdigest(),
    }


def _validate_outcome(value: Mapping[str, Any], work_item_id: str) -> dict[str, Any]:
    _exact(value, {
        "schema_version", "work_item_id", "producer", "measurement_epoch_ref",
        "task_ref", "outcome_ref", "status", "verifier", "evidence",
        "human_correction", "recorded_at", "receipt_sha256",
    }, "task outcome")
    if value.get("schema_version") != "work_item_task_outcome_receipt/v1" or value.get("work_item_id") != work_item_id:
        raise SclMeasurementError("task outcome schema or WI is invalid")
    for field in ("measurement_epoch_ref", "task_ref", "outcome_ref"):
        item = value.get(field)
        if not isinstance(item, str) or not _HEX_REF[field].fullmatch(item):
            raise SclMeasurementError(f"task outcome {field} is invalid")
    expected_outcome = _domain_ref(
        "outcome_v1_", "agent-harness.task-outcome.outcome.v1", str(value["task_ref"])
    )
    if value.get("outcome_ref") != expected_outcome or value.get("status") not in {"accepted", "rejected"}:
        raise SclMeasurementError("task outcome identity or status is invalid")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise SclMeasurementError("task outcome evidence is missing")
    refs: list[str] = []
    for item in evidence:
        if not isinstance(item, Mapping) or set(item) != {"ref", "sha256"}:
            raise SclMeasurementError("task outcome evidence shape is invalid")
        ref, digest = item.get("ref"), item.get("sha256")
        if not isinstance(ref, str) or not _SAFE_REF.fullmatch(ref) or not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise SclMeasurementError("task outcome evidence ref or digest is invalid")
        refs.append(ref)
    if refs != sorted(refs) or len(refs) != len(set(refs)):
        raise SclMeasurementError("task outcome evidence must be sorted and unique")
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if value.get("receipt_sha256") != "sha256:" + sha256(_canonical(unsigned)).hexdigest():
        raise SclMeasurementError("task outcome receipt digest is invalid")
    return {
        "measurement_epoch_ref": value["measurement_epoch_ref"],
        "task_ref": value["task_ref"],
        "outcome_ref": value["outcome_ref"],
        "status": value["status"],
        "human_correction": value["human_correction"],
        "recorded_at": value["recorded_at"],
        "evidence_refs": refs,
        "receipt_sha256": value["receipt_sha256"],
    }


def _validate_attention(value: Mapping[str, Any], work_item_id: str) -> dict[str, Any]:
    _exact(value, {"schema_version", "work_item_id", "events"}, "human attention")
    if value.get("schema_version") != "s1_human_attention_evidence/v1" or value.get("work_item_id") != work_item_id:
        raise SclMeasurementError("human attention schema or WI is invalid")
    events = value.get("events")
    if not isinstance(events, list) or not events:
        raise SclMeasurementError("human attention events are missing")
    bands: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    measured = 0
    measured_event_count = 0
    measured_complete = True
    refs: list[str] = []
    source_refs: set[str] = set()
    for event in events:
        if not isinstance(event, Mapping):
            raise SclMeasurementError("human attention event is invalid")
        _exact(event, {
            "event_ref", "category", "effort_band", "occurred_at", "source_ref",
            "source_sha256", "timing_basis", "measured_seconds", "event_sha256",
        }, "human attention event")
        ref = event.get("event_ref")
        if not isinstance(ref, str) or not _HEX_REF["event_ref"].fullmatch(ref):
            raise SclMeasurementError("human attention event ref is invalid")
        band = event.get("effort_band")
        category = event.get("category")
        if band not in {"micro", "bounded", "judgment", "framing"} or category not in {
            "human_touch", "review_approval", "correction", "re_explanation", "manual_retry", "rework",
        }:
            raise SclMeasurementError("human attention classification is invalid")
        timing, seconds = event.get("timing_basis"), event.get("measured_seconds")
        if timing == "unavailable":
            if seconds is not None:
                raise SclMeasurementError("unavailable attention timing carries seconds")
            measured_complete = False
        elif timing in {"explicit_timer", "trusted_telemetry"}:
            if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
                raise SclMeasurementError("measured attention seconds are invalid")
            measured += seconds
            measured_event_count += 1
        else:
            raise SclMeasurementError("attention timing basis is invalid")
        unsigned = {key: item for key, item in event.items() if key != "event_sha256"}
        if event.get("event_sha256") != "sha256:" + sha256(_canonical(unsigned)).hexdigest():
            raise SclMeasurementError("human attention event digest is invalid")
        source_ref = event.get("source_ref")
        if not isinstance(source_ref, str) or not _SAFE_REF.fullmatch(source_ref):
            raise SclMeasurementError("human attention source ref is invalid")
        refs.append(ref)
        source_refs.add(source_ref)
        bands[str(band)] += 1
        categories[str(category)] += 1
    if refs != sorted(refs) or len(refs) != len(set(refs)):
        raise SclMeasurementError("human attention events must be sorted and unique")
    units = sum(
        count * weight
        for count, weight in (
            (bands["micro"], 0.25),
            (bands["bounded"], 1),
            (bands["judgment"], 2),
            (bands["framing"], 4),
        )
    )
    units_text = format(units, ".2f").rstrip("0").rstrip(".")
    unknown_timing_count = len(events) - measured_event_count
    return {
        "status": "observed",
        "raw_turn_count": len(events),
        "human_attention_units": units_text,
        "effort_band_counts": {name: bands[name] for name in ("micro", "bounded", "judgment", "framing")},
        "category_counts": {name: categories[name] for name in (
            "human_touch", "review_approval", "correction", "re_explanation", "manual_retry", "rework"
        )},
        "focused_seconds": (
            measured if measured_complete or measured_event_count else "unavailable"
        ),
        "focused_seconds_coverage": (
            "observed" if measured_complete
            else "partial" if measured_event_count
            else "unavailable"
        ),
        "focused_seconds_unknown_event_count": unknown_timing_count,
        "measured_seconds": measured if measured_complete else "unavailable",
        "event_refs": refs,
        "source_refs": sorted(source_refs),
        "contract_version": "wi028-g0-v1",
        "content_sha256": sha256(_canonical(value)).hexdigest(),
    }


def _registered_surface_ids(path: Path | None = None) -> frozenset[str]:
    registry_path = path or Path(__file__).resolve().parents[2] / "config/scl-surfaces.yaml"
    try:
        value = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SclMeasurementError("registered SCL surface evidence is unavailable") from exc
    if not isinstance(value, Mapping) or value.get("schema_version") != "pulse_scl_surface_registry/v3":
        raise SclMeasurementError("registered SCL surface evidence is invalid")
    surfaces = value.get("surfaces")
    if not isinstance(surfaces, list):
        raise SclMeasurementError("registered SCL surfaces are invalid")
    result: set[str] = set()
    for surface in surfaces:
        if not isinstance(surface, Mapping) or not isinstance(surface.get("surface_id"), str):
            raise SclMeasurementError("registered SCL surface is malformed")
        result.add(str(surface["surface_id"]))
    # A retired surface no longer runs or deploys; its recorded measurements
    # still need the registration they were produced under.
    for surface in value.get("retired_surfaces", []):
        if not isinstance(surface, Mapping) or not isinstance(surface.get("surface_id"), str):
            raise SclMeasurementError("retired SCL surface is malformed")
        result.add(str(surface["surface_id"]))
    if not result:
        raise SclMeasurementError("registered SCL surface inventory is empty")
    return frozenset(result)


def _validate_native_measurement(
    value: Mapping[str, Any],
    *,
    family: _NativeFamily,
    work_item_id: str,
    work_item_ref: str,
    attribution: Mapping[str, Any],
    seal: Mapping[str, Any],
    registered_surfaces: frozenset[str],
) -> dict[str, Any]:
    label = family.label
    _exact(value, {
        "schema", "schema_version", "work_item_ref", "measurement_epoch_ref",
        "execution_epoch", "phase", "runtime", "agent_id", "execution_surface",
        "runtime_binding_sha256", "baseline_snapshot", "final_snapshot",
        "accounting", "coverage", "artifact_sha256",
    }, f"bounded {label} SCL measurement")
    if value.get("schema") != family.measurement_schema or value.get("schema_version") != 1:
        raise SclMeasurementError(f"bounded {label} SCL measurement schema is unsupported")
    unsigned = {key: item for key, item in value.items() if key != "artifact_sha256"}
    digest = value.get("artifact_sha256")
    if not isinstance(digest, str) or digest != sha256(_canonical(unsigned)).hexdigest():
        raise SclMeasurementError(f"bounded {label} SCL measurement digest is invalid")
    if value.get("work_item_ref") != work_item_ref:
        raise SclMeasurementError(f"bounded {label} SCL measurement belongs to another WI")
    surface = value.get("execution_surface")
    if not isinstance(surface, str) or surface not in registered_surfaces:
        raise SclMeasurementError(f"bounded {label} SCL measurement surface is not registered")
    runtime = value.get("runtime")
    if (
        value.get("measurement_epoch_ref") != attribution["measurement_epoch_ref"]
        or value.get("execution_epoch") != attribution["execution_epoch"]
        or value.get("phase") != attribution["phase"]
        or runtime not in family.runtimes
        or attribution.get("runtime") != runtime
        or value.get("agent_id") != attribution.get("agent_id")
        or seal.get("runtime") != runtime
        or seal.get("execution_surface") != surface
        or seal.get("usage_producer") != f"{family.name}_native"
        or value.get("accounting") != "baseline_exclusive_final_inclusive"
        or value.get("coverage") != "exact_bounded_sealed"
    ):
        raise SclMeasurementError(f"bounded {label} SCL measurement has no exact attribution/surface/seal join")
    binding_digest = value.get("runtime_binding_sha256")
    if not isinstance(binding_digest, str) or re.fullmatch(r"[0-9a-f]{64}", binding_digest) is None:
        raise SclMeasurementError(f"bounded {label} runtime binding digest is invalid")
    baseline = value.get("baseline_snapshot")
    final = value.get("final_snapshot")
    try:
        family.validate_snapshot(baseline)
        family.validate_snapshot(final)
    except PiUsageError as exc:
        raise SclMeasurementError(str(exc)) from exc
    if not isinstance(baseline, Mapping) or not isinstance(final, Mapping):
        raise SclMeasurementError(f"bounded {label} snapshots are invalid")
    identity_fields = (
        "work_item_id", "agent_id", "execution_surface", "phase", "epoch",
        "runtime", "procurement_source", "authority_ref", "runtime_binding_sha256",
    )
    if (
        baseline.get("schema") != family.snapshot_schema
        or final.get("schema") != family.snapshot_schema
        or baseline.get("measurement_role") != "baseline"
        or final.get("measurement_role") != "final"
        or any(baseline.get(field) != final.get(field) for field in identity_fields)
        or final.get("work_item_id") != work_item_id
        or final.get("runtime") != runtime
        or final.get("runtime_binding_sha256") != binding_digest
        or final.get("procurement_source") != seal.get("procurement_source")
        or final.get("epoch") != value.get("execution_epoch")
        or final.get("phase") != value.get("phase")
        or final.get("agent_id") != value.get("agent_id")
        or final.get("execution_surface") != surface
    ):
        raise SclMeasurementError(f"bounded {label} baseline/final authority does not match")
    baseline_boundary = baseline.get("attribution_boundary")
    final_boundary = final.get("attribution_boundary")
    if not isinstance(baseline_boundary, Mapping) or not isinstance(final_boundary, Mapping) or (
        final_boundary.get("baseline_snapshot_sha256") != baseline.get("snapshot_sha256")
        or final_boundary.get("baseline") != baseline_boundary.get("baseline")
    ):
        raise SclMeasurementError(f"bounded {label} final does not descend from the immutable baseline")
    summary = final.get("summary")
    if not isinstance(summary, Mapping) or not isinstance(summary.get("totals"), Mapping):
        raise SclMeasurementError(f"bounded {label} final summary is invalid")
    seal_tokens = seal.get("tokens")
    if not isinstance(seal_tokens, Mapping) or any(
        seal_tokens.get(field) != amount for field, amount in _normalized_native(summary["totals"]).items()
    ):
        raise SclMeasurementError(f"bounded {label} usage does not conserve with the canonical Token seal")
    return {
        "status": "exact_bounded_sealed",
        "execution_surface": surface,
        "runtime_binding_sha256": binding_digest,
        "baseline_snapshot_sha256": baseline["snapshot_sha256"],
        "final_snapshot_sha256": final["snapshot_sha256"],
        "summary": dict(summary),
        "attribution_boundary": dict(final_boundary),
        "content_sha256": digest,
    }


def _normalized_native(totals: Mapping[str, Any]) -> dict[str, int]:
    inputs = InputTokenClasses.from_disjoint(
        int(totals["input"]), int(totals["cache_read"]), int(totals["cache_write"]))
    return {
        "input_tokens": inputs.require_total(),
        "cached_input_tokens": int(totals["cache_read"]),
        "output_tokens": int(totals["output"]),
        "reasoning_output_tokens": int(totals["reasoning"]),
        "total_tokens": int(totals["total_tokens"]),
    }


def load_scl_measurement_snapshot(
    harness_root: Path,
    work_item_id: str,
    *,
    surface_registry_path: Path | None = None,
) -> dict[str, Any]:
    """Load and join one WI's durable SCL measurement artifacts."""

    if not _WI_RE.fullmatch(work_item_id):
        raise SclMeasurementError("work_item_id is invalid")
    root = harness_root.resolve(strict=True)
    if not root.is_dir():
        raise SclMeasurementError("Harness root must be a directory")
    work_item_ref = _domain_ref("work_item_v1_", "pulse-work-item-v1", work_item_id)

    attribution_paths = sorted((root / "evidence/compute-cost/attributions" / work_item_id).glob("*.json"))
    attributions: dict[str, dict[str, Any]] = {}
    for path in attribution_paths:
        raw = _read(path)
        attribution = _validate_attribution(raw, work_item_id)
        operation = raw.get("operation_ref")
        if not isinstance(operation, str) or path.stem != sha256(operation.encode("utf-8")).hexdigest():
            raise SclMeasurementError("cost attribution filename does not match its operation")
        epoch_ref = attribution["measurement_epoch_ref"]
        if epoch_ref in attributions:
            raise SclMeasurementError("measurement epoch attribution is duplicated")
        attributions[epoch_ref] = attribution

    seal_paths = sorted((root / "evidence/compute-cost/observations" / work_item_id / "app_server_epoch_token_seal.v1").glob("*.json"))
    seals: dict[str, dict[str, Any]] = {}
    for path in seal_paths:
        raw_seal = _read(path)
        seal = _validate_seal(raw_seal, work_item_ref)
        if path.stem != seal["seal_id"]:
            raise SclMeasurementError("SCL Token seal filename does not match its identity")
        epoch_ref = seal["measurement_epoch_ref"]
        if epoch_ref in seals:
            raise SclMeasurementError("measurement epoch has multiple Token seals")
        attribution = attributions.get(epoch_ref)
        if attribution is None or (
            attribution["execution_epoch"] != seal["execution_epoch"]
            or attribution["phase"] != seal["phase"]
            or attribution["lineage_root_ref"] != raw_seal["lineage_root_ref"]
        ):
            raise SclMeasurementError("SCL Token seal has no exact attribution join")
        seals[epoch_ref] = seal

    native_paths = [
        (family, path)
        for family in _NATIVE_FAMILIES
        for path in sorted((
            root / "evidence/compute-cost/observations" / work_item_id / family.directory
        ).glob("*.json"))
    ]
    registered_surfaces = (
        _registered_surface_ids(surface_registry_path) if native_paths else frozenset()
    )
    native_measurements: dict[str, tuple[_NativeFamily, dict[str, Any]]] = {}
    for family, path in native_paths:
        label = family.label
        raw_native = _read(path, require_mode_0600=True)
        epoch_ref = raw_native.get("measurement_epoch_ref")
        if not isinstance(epoch_ref, str) or path.stem != epoch_ref:
            raise SclMeasurementError(f"bounded {label} SCL measurement filename is invalid")
        if epoch_ref in native_measurements:
            raise SclMeasurementError("measurement epoch has multiple bounded native artifacts")
        attribution = attributions.get(epoch_ref)
        seal = seals.get(epoch_ref)
        if attribution is None or seal is None:
            raise SclMeasurementError(f"bounded {label} SCL measurement lacks attribution or final Token seal")
        native_measurements[epoch_ref] = (family, _validate_native_measurement(
            raw_native,
            family=family,
            work_item_id=work_item_id,
            work_item_ref=work_item_ref,
            attribution=attribution,
            seal=seal,
            registered_surfaces=registered_surfaces,
        ))

    outcome_paths = sorted((root / "evidence/task-outcomes" / work_item_id).glob("*.json"))
    outcomes: dict[str, list[dict[str, Any]]] = {}
    unlinked_outcomes = 0
    for path in outcome_paths:
        outcome = _validate_outcome(_read(path), work_item_id)
        if path.stem != outcome["outcome_ref"]:
            raise SclMeasurementError("task outcome filename does not match its identity")
        epoch_ref = outcome["measurement_epoch_ref"]
        if epoch_ref not in attributions:
            unlinked_outcomes += 1
            continue
        outcomes.setdefault(epoch_ref, []).append(outcome)

    attention_path = root / "evidence/measurements" / work_item_id / "human-attention.json"
    attention: object = "unavailable"
    if attention_path.exists() or attention_path.is_symlink():
        attention = _validate_attention(_read(attention_path), work_item_id)

    epochs: list[dict[str, Any]] = []
    for epoch_ref, attribution in sorted(attributions.items(), key=lambda item: (item[1]["execution_epoch"], item[0])):
        seal = seals.get(epoch_ref)
        native = native_measurements.get(epoch_ref)
        epoch_outcomes = sorted(outcomes.get(epoch_ref, []), key=lambda item: item["outcome_ref"])
        required = _family_for_runtime(attribution["runtime"])
        epochs.append({
            **attribution,
            "usage": seal if seal is not None else "unavailable",
            **{
                family.epoch_key: native[1] if native is not None and native[0] is family else "unavailable"
                for family in _NATIVE_FAMILIES
            },
            "outcomes": epoch_outcomes,
            "measurement_eligible": bool(
                seal
                and (required is None or (native is not None and native[0] is required))
                and any(item["status"] == "accepted" for item in epoch_outcomes)
            ),
        })

    missing: list[str] = []
    if not attributions:
        missing.append("cost_attribution")
    if not seals:
        missing.append("token_seal")
    for family in _NATIVE_FAMILIES:
        if any(
            item["runtime"] in family.runtimes and epoch_ref not in native_measurements
            for epoch_ref, item in attributions.items()
        ):
            missing.append(family.missing_producer)
    if not any(outcomes.values()):
        missing.append("linked_task_outcome")
    if attention == "unavailable":
        missing.append("human_attention")
    warnings = ["unlinked_outcome_excluded"] if unlinked_outcomes else []
    eligible = sum(1 for epoch in epochs if epoch["measurement_eligible"])
    snapshot: dict[str, Any] = {
        "schema": SCL_MEASUREMENT_SCHEMA,
        "work_item_id": work_item_id,
        "status": "complete" if epochs and eligible == len(epochs) and not missing else "partial" if epochs else "unavailable",
        "eligible_epoch_count": eligible,
        "epochs": epochs,
        "human_attention": attention,
        "missing_producers": sorted(missing),
        "warnings": warnings,
    }
    snapshot["snapshot_sha256"] = sha256(_canonical(snapshot)).hexdigest()
    return snapshot


def _validate_projected_native(
    value: Mapping[str, Any], family: _NativeFamily, usage: object
) -> bool:
    label = family.label
    native = value.get(family.epoch_key)
    if native == "unavailable":
        return False
    if not isinstance(native, Mapping):
        raise SclMeasurementError(f"SCL projected {label} usage availability is invalid")
    _exact(native, {
        "status", "execution_surface", "runtime_binding_sha256",
        "baseline_snapshot_sha256", "final_snapshot_sha256", "summary",
        "attribution_boundary", "content_sha256",
    }, f"SCL projected bounded {label} usage")
    if (
        value.get("runtime") not in family.runtimes
        or native.get("status") != "exact_bounded_sealed"
        or not isinstance(usage, Mapping)
        or native.get("execution_surface") != usage.get("execution_surface")
    ):
        raise SclMeasurementError(f"SCL projected bounded {label} usage is invalid")
    for field in (
        "runtime_binding_sha256", "baseline_snapshot_sha256",
        "final_snapshot_sha256", "content_sha256",
    ):
        if not isinstance(native.get(field), str) or re.fullmatch(r"[0-9a-f]{64}", native[field]) is None:
            raise SclMeasurementError(f"SCL projected bounded {label} {field} is invalid")
    try:
        summary = validate_pi_usage_summary(native.get("summary"), family=family.name)
        boundary = validate_pi_attribution_boundary(
            native.get("attribution_boundary"),
            summary=summary,
            measurement_role="final",
            source_manifest=family.source_manifest,
        )
    except PiUsageError as exc:
        raise SclMeasurementError(str(exc)) from exc
    if boundary["baseline_snapshot_sha256"] != native["baseline_snapshot_sha256"]:
        raise SclMeasurementError(f"SCL projected {label} baseline identity is inconsistent")
    if any(usage["tokens"].get(field) != amount for field, amount in _normalized_native(summary["totals"]).items()):
        raise SclMeasurementError(f"SCL projected {label} usage does not conserve with its Token seal")
    return True


def _validate_projected_epoch(value: object) -> bool:
    if not isinstance(value, Mapping):
        raise SclMeasurementError("SCL measurement epoch must be an object")
    _exact(value, {
        "measurement_epoch_ref", "execution_epoch", "phase", "runtime", "agent_id",
        "lineage_root_ref", "started_at", "ended_at", "content_sha256",
        "usage", "pi_usage", "cc_usage", "outcomes", "measurement_eligible",
    }, "SCL measurement epoch")
    for field in ("measurement_epoch_ref", "lineage_root_ref"):
        item = value.get(field)
        if not isinstance(item, str) or not _HEX_REF[field].fullmatch(item):
            raise SclMeasurementError(f"SCL measurement epoch {field} is invalid")
    if not isinstance(value.get("execution_epoch"), int) or isinstance(value.get("execution_epoch"), bool) or value["execution_epoch"] < 1:
        raise SclMeasurementError("SCL measurement execution epoch is invalid")
    if not isinstance(value.get("content_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["content_sha256"]):
        raise SclMeasurementError("SCL measurement attribution digest is invalid")
    if not isinstance(value.get("agent_id"), str) or not value["agent_id"]:
        raise SclMeasurementError("SCL measurement agent is invalid")
    outcomes = value.get("outcomes")
    if not isinstance(outcomes, list):
        raise SclMeasurementError("SCL measurement outcomes must be a list")
    accepted = False
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            raise SclMeasurementError("SCL projected outcome is invalid")
        _exact(outcome, {
            "measurement_epoch_ref", "task_ref", "outcome_ref", "status",
            "human_correction", "recorded_at", "evidence_refs", "receipt_sha256",
        }, "SCL projected outcome")
        if outcome.get("measurement_epoch_ref") != value["measurement_epoch_ref"]:
            raise SclMeasurementError("SCL projected outcome epoch join is invalid")
        if outcome.get("status") not in {"accepted", "rejected"}:
            raise SclMeasurementError("SCL projected outcome status is invalid")
        accepted = accepted or outcome.get("status") == "accepted"
    usage = value.get("usage")
    sealed = isinstance(usage, Mapping)
    if usage != "unavailable" and not sealed:
        raise SclMeasurementError("SCL projected usage availability is invalid")
    if sealed:
        _exact(usage, {
            "seal_id", "measurement_epoch_ref", "execution_epoch", "phase", "runtime",
            "execution_surface", "usage_producer", "procurement_source", "model",
            "reasoning_effort", "tokens", "coverage", "content_sha256",
        }, "SCL projected usage")
        if usage.get("measurement_epoch_ref") != value["measurement_epoch_ref"] or usage.get("execution_epoch") != value["execution_epoch"]:
            raise SclMeasurementError("SCL projected usage epoch join is invalid")
        raw_tokens = usage.get("tokens")
        if not isinstance(raw_tokens, Mapping) or raw_tokens.get("cache_write_input_tokens") != "unavailable":
            raise SclMeasurementError("SCL projected cache-write coverage is invalid")
        _tokens(
            {key: item for key, item in raw_tokens.items() if key != "cache_write_input_tokens"},
            "SCL projected usage",
        )
    native_sealed = {
        family.name: _validate_projected_native(value, family, usage) for family in _NATIVE_FAMILIES
    }
    required = _family_for_runtime(value.get("runtime"))
    eligible = value.get("measurement_eligible")
    expected_eligible = sealed and accepted and (required is None or native_sealed[required.name])
    if not isinstance(eligible, bool) or eligible != expected_eligible:
        raise SclMeasurementError("SCL measurement eligibility is inconsistent")
    return eligible


def validate_scl_measurement_snapshot(value: object, *, expected_work_item_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SclMeasurementError("SCL measurement snapshot must be an object")
    snapshot = dict(value)
    _exact(snapshot, {
        "schema", "work_item_id", "status", "eligible_epoch_count", "epochs",
        "human_attention", "missing_producers", "warnings", "snapshot_sha256",
    }, "SCL measurement snapshot")
    if snapshot.get("schema") != SCL_MEASUREMENT_SCHEMA or snapshot.get("work_item_id") != expected_work_item_id:
        raise SclMeasurementError("SCL measurement snapshot schema or WI is invalid")
    if snapshot.get("status") not in {"complete", "partial", "unavailable"}:
        raise SclMeasurementError("SCL measurement status is invalid")
    epochs = snapshot.get("epochs")
    if not isinstance(epochs, list):
        raise SclMeasurementError("SCL measurement epochs must be a list")
    eligible = sum(1 for epoch in epochs if _validate_projected_epoch(epoch))
    if snapshot.get("eligible_epoch_count") != eligible:
        raise SclMeasurementError("SCL eligible epoch count is inconsistent")
    for field in ("missing_producers", "warnings"):
        items = snapshot.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise SclMeasurementError(f"SCL measurement {field} is invalid")
    for family in _NATIVE_FAMILIES:
        family_missing = any(
            isinstance(epoch, Mapping)
            and epoch.get("runtime") in family.runtimes
            and not isinstance(epoch.get(family.epoch_key), Mapping)
            for epoch in epochs
        )
        if (family.missing_producer in snapshot["missing_producers"]) != family_missing:
            raise SclMeasurementError(f"SCL bounded {family.label} producer coverage is inconsistent")
    expected_status = (
        "complete" if epochs and eligible == len(epochs) and not snapshot["missing_producers"]
        else "partial" if epochs
        else "unavailable"
    )
    if snapshot["status"] != expected_status:
        raise SclMeasurementError("SCL measurement status is inconsistent")
    attention = snapshot.get("human_attention")
    if attention != "unavailable":
        if not isinstance(attention, Mapping):
            raise SclMeasurementError("SCL projected human attention is invalid")
        _exact(attention, {
            "status", "raw_turn_count", "human_attention_units",
            "effort_band_counts", "category_counts", "focused_seconds",
            "focused_seconds_coverage", "focused_seconds_unknown_event_count",
            "measured_seconds", "event_refs", "source_refs", "contract_version",
            "content_sha256",
        }, "SCL projected human attention")
        if attention.get("status") != "observed" or not isinstance(attention.get("raw_turn_count"), int) or attention["raw_turn_count"] < 1:
            raise SclMeasurementError("SCL projected human attention count is invalid")
        bands = attention.get("effort_band_counts")
        if not isinstance(bands, Mapping) or set(bands) != {"micro", "bounded", "judgment", "framing"} or any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in bands.values()
        ) or sum(bands.values()) != attention["raw_turn_count"]:
            raise SclMeasurementError("SCL projected human attention bands do not conserve")
        expected_units = sum(
            count * weight
            for count, weight in (
                (bands["micro"], 0.25),
                (bands["bounded"], 1),
                (bands["judgment"], 2),
                (bands["framing"], 4),
            )
        )
        expected_units_text = format(expected_units, ".2f").rstrip("0").rstrip(".")
        if attention.get("human_attention_units") != expected_units_text or attention.get("contract_version") != "wi028-g0-v1":
            raise SclMeasurementError("SCL projected human attention units are invalid")
        seconds = attention.get("measured_seconds")
        focused = attention.get("focused_seconds")
        focused_coverage = attention.get("focused_seconds_coverage")
        focused_unknown = attention.get("focused_seconds_unknown_event_count")
        if seconds != "unavailable" and (isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0):
            raise SclMeasurementError("SCL projected human attention timing is invalid")
        if focused != "unavailable" and (isinstance(focused, bool) or not isinstance(focused, int) or focused < 0):
            raise SclMeasurementError("SCL projected focused attention is invalid")
        if focused_coverage not in {"observed", "partial", "unavailable"} or isinstance(focused_unknown, bool) or not isinstance(focused_unknown, int) or focused_unknown < 0:
            raise SclMeasurementError("SCL projected focused attention coverage is invalid")
        if focused_coverage == "observed" and (focused != seconds or focused_unknown != 0):
            raise SclMeasurementError("SCL projected focused attention does not conserve")
        if focused_coverage == "unavailable" and focused != "unavailable":
            raise SclMeasurementError("unavailable focused attention carries seconds")
        for refs_field in ("event_refs", "source_refs"):
            refs = attention.get(refs_field)
            if not isinstance(refs, list) or refs != sorted(set(refs)) or any(not isinstance(ref, str) for ref in refs):
                raise SclMeasurementError(f"SCL projected human attention {refs_field} is invalid")
            if refs_field == "event_refs" and any(
                _HEX_REF["event_ref"].fullmatch(ref) is None for ref in refs
            ):
                raise SclMeasurementError("SCL projected human attention event ref is invalid")
            if refs_field == "source_refs" and any(
                _SAFE_REF.fullmatch(ref) is None for ref in refs
            ):
                raise SclMeasurementError("SCL projected human attention source ref is invalid")
    digest = snapshot.get("snapshot_sha256")
    unsigned = {key: item for key, item in snapshot.items() if key != "snapshot_sha256"}
    if not isinstance(digest, str) or digest != sha256(_canonical(unsigned)).hexdigest():
        raise SclMeasurementError("SCL measurement snapshot digest is invalid")
    _walk_privacy(snapshot)
    return snapshot
