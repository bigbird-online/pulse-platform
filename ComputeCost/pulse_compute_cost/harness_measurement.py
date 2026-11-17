"""Strict, privacy-safe adapter for Harness S1 measurement projections."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Any, Mapping


HARNESS_MEASUREMENT_SCHEMA = "s1_measurement_pilot.v1"
_UNKNOWN = "unknown"
_METRICS = {
    "quality.first_pass_review",
    "quality.review_rework_rounds",
    "quality.final_review_status",
    "quality.delivery_status",
    "quality.effect_status",
    "quality.post_approval_corrections",
    "efficiency.execution_started_at",
    "efficiency.delivery_accepted_at",
    "efficiency.result_cycle_seconds",
    "efficiency.session_segment_count",
    "efficiency.planned_rollover_count",
    "efficiency.runtime_recovery_count",
    "efficiency.human_reexplanation_count",
    "cost.token_hard_cost",
    "cost.human_attention",
}
_SAFE_REF = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[^\x00]+$")
_FORBIDDEN_KEYS = {
    "thread_id",
    "threadid",
    "turn_id",
    "turnid",
    "session_id",
    "sessionid",
    "message_id",
    "messageid",
    "response_id",
    "responseid",
    "credential",
    "codex_home",
    "user_agent",
}


class HarnessMeasurementError(ValueError):
    """Raised when a Harness measurement projection is unsafe or ambiguous."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HarnessMeasurementError(f"{field} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise HarnessMeasurementError(f"{field} fields do not match the contract")


def _non_negative_int_or_unknown(value: object, field: str) -> None:
    if value == _UNKNOWN:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HarnessMeasurementError(f"{field} must be non-negative or unknown")


def _safe_ref(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or not _SAFE_REF.fullmatch(value):
        raise HarnessMeasurementError(f"{field} must be a repository-relative ref")
    return value


def _walk_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise HarnessMeasurementError("measurement keys must be strings")
            if key.lower() in _FORBIDDEN_KEYS:
                raise HarnessMeasurementError(
                    "Harness measurement contains forbidden raw identity"
                )
            _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk_keys(nested)


def _decimal_text(value: object, field: str) -> str:
    if isinstance(value, bool):
        raise HarnessMeasurementError(f"{field} must be a non-negative decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HarnessMeasurementError(
            f"{field} must be a non-negative decimal"
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise HarnessMeasurementError(f"{field} must be a non-negative decimal")
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def validate_harness_measurement_snapshot(
    value: object, *, expected_work_item_id: str
) -> dict[str, Any]:
    """Validate and normalize one read-only Harness measurement projection."""

    root = _mapping(value, "Harness measurement")
    _exact_keys(
        root,
        {
            "schema",
            "work_item_id",
            "measurement_eligibility",
            "quality",
            "efficiency",
            "cost",
            "evidence",
        },
        "Harness measurement",
    )
    if root.get("schema") != HARNESS_MEASUREMENT_SCHEMA:
        raise HarnessMeasurementError("unsupported Harness measurement schema")
    if root.get("work_item_id") != expected_work_item_id:
        raise HarnessMeasurementError("Harness measurement belongs to another work item")
    _walk_keys(root)

    eligibility = _mapping(root.get("measurement_eligibility"), "measurement_eligibility")
    _exact_keys(eligibility, {"eligible", "result_class"}, "measurement_eligibility")
    if eligibility.get("eligible") not in {True, False, _UNKNOWN}:
        raise HarnessMeasurementError("measurement eligibility is invalid")
    if eligibility.get("result_class") not in {
        "behavior_change",
        "artifact_delivery",
        "recurring_delivery",
        "restoration",
        "decision_support",
        "other",
    }:
        raise HarnessMeasurementError("measurement result_class is invalid")

    quality = _mapping(root.get("quality"), "quality")
    _exact_keys(
        quality,
        {
            "first_pass_review",
            "review_rework_rounds",
            "final_review_status",
            "delivery_status",
            "effect_status",
            "post_approval_corrections",
        },
        "quality",
    )
    if quality.get("first_pass_review") not in {True, False, _UNKNOWN}:
        raise HarnessMeasurementError("quality.first_pass_review is invalid")
    _non_negative_int_or_unknown(
        quality.get("review_rework_rounds"), "quality.review_rework_rounds"
    )
    _non_negative_int_or_unknown(
        quality.get("post_approval_corrections"),
        "quality.post_approval_corrections",
    )
    if quality.get("final_review_status") not in {
        "approved",
        "changes_requested",
        "none",
        _UNKNOWN,
    }:
        raise HarnessMeasurementError("quality.final_review_status is invalid")
    if quality.get("delivery_status") not in {
        "accepted",
        "partial",
        "rejected",
        "blocked",
        "none",
        _UNKNOWN,
    }:
        raise HarnessMeasurementError("quality.delivery_status is invalid")
    if not isinstance(quality.get("effect_status"), str):
        raise HarnessMeasurementError("quality.effect_status must be a string")

    efficiency = _mapping(root.get("efficiency"), "efficiency")
    _exact_keys(
        efficiency,
        {
            "execution_started_at",
            "delivery_accepted_at",
            "result_cycle_seconds",
            "session_segment_count",
            "planned_rollover_count",
            "runtime_recovery_count",
            "human_reexplanation_count",
        },
        "efficiency",
    )
    for field in ("execution_started_at", "delivery_accepted_at"):
        if not isinstance(efficiency.get(field), str):
            raise HarnessMeasurementError(f"efficiency.{field} must be a string")
    for field in (
        "result_cycle_seconds",
        "session_segment_count",
        "planned_rollover_count",
        "runtime_recovery_count",
        "human_reexplanation_count",
    ):
        _non_negative_int_or_unknown(efficiency.get(field), f"efficiency.{field}")

    cost = _mapping(root.get("cost"), "cost")
    _exact_keys(cost, {"token_hard_cost", "human_attention"}, "cost")
    token_cost = cost.get("token_hard_cost")
    normalized_cash: str | None = None
    cost_source_refs: set[str] = set()
    if token_cost != _UNKNOWN:
        token = _mapping(token_cost, "cost.token_hard_cost")
        _exact_keys(
            token,
            {
                "total_tokens",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "actual_cash_usd",
                "source_ref",
            },
            "cost.token_hard_cost",
        )
        for field in (
            "total_tokens",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
        ):
            item = token.get(field)
            if item is not None:
                _non_negative_int_or_unknown(item, f"cost.token_hard_cost.{field}")
        cost_source_refs.add(
            _safe_ref(token.get("source_ref"), "cost.token_hard_cost.source_ref")
        )
        if token.get("actual_cash_usd") is not None:
            normalized_cash = _decimal_text(
                token.get("actual_cash_usd"), "cost.token_hard_cost.actual_cash_usd"
            )
    human_attention = cost.get("human_attention")
    if human_attention != _UNKNOWN:
        attention = _mapping(human_attention, "cost.human_attention")
        _exact_keys(
            attention,
            {
                "raw_turn_count",
                "human_attention_units",
                "effort_band_counts",
                "contract_version",
                "source_ref",
            },
            "cost.human_attention",
        )
        _non_negative_int_or_unknown(
            attention.get("raw_turn_count"), "cost.human_attention.raw_turn_count"
        )
        units = _decimal_text(
            attention.get("human_attention_units"),
            "cost.human_attention.human_attention_units",
        )
        bands = _mapping(
            attention.get("effort_band_counts"),
            "cost.human_attention.effort_band_counts",
        )
        _exact_keys(bands, {"micro", "bounded", "judgment", "framing"}, "effort_band_counts")
        for band, count in bands.items():
            _non_negative_int_or_unknown(count, f"effort_band_counts.{band}")
        if any(isinstance(count, bool) or not isinstance(count, int) for count in bands.values()):
            raise HarnessMeasurementError("human attention effort counts must be integers")
        if sum(bands.values()) != attention.get("raw_turn_count"):
            raise HarnessMeasurementError("human attention turn counts do not conserve")
        expected_units = sum(
            Decimal(weight) * bands[band]
            for band, weight in {
                "micro": "0.25",
                "bounded": "1",
                "judgment": "2",
                "framing": "4",
            }.items()
        )
        if Decimal(units) != expected_units:
            raise HarnessMeasurementError("human attention units do not conserve")
        if attention.get("contract_version") != "wi028-g0-v1":
            raise HarnessMeasurementError("human attention contract is invalid")
        cost_source_refs.add(
            _safe_ref(attention.get("source_ref"), "cost.human_attention.source_ref")
        )
    else:
        units = None

    evidence = _mapping(root.get("evidence"), "evidence")
    _exact_keys(
        evidence,
        {
            "as_of",
            "readouts",
            "missing_producers",
            "authority_gaps",
            "sensor_health",
        },
        "evidence",
    )
    if evidence.get("sensor_health") not in {"healthy", "degraded"}:
        raise HarnessMeasurementError("evidence.sensor_health is invalid")
    readouts = _mapping(evidence.get("readouts"), "evidence.readouts")
    if set(readouts) != _METRICS:
        raise HarnessMeasurementError("evidence.readouts metric set is incomplete")
    unavailable: list[str] = []
    refs: set[str] = set()
    for metric, raw_readout in readouts.items():
        readout = _mapping(raw_readout, f"evidence.readouts.{metric}")
        _exact_keys(
            readout,
            {
                "window",
                "as_of",
                "eligible_denominator",
                "observed_denominator",
                "coverage",
                "status",
                "value",
                "unknown_count",
                "source_refs",
            },
            f"evidence.readouts.{metric}",
        )
        status = readout.get("status")
        if status not in {"computable", "partial", "unavailable"}:
            raise HarnessMeasurementError(f"readout status is invalid: {metric}")
        if status == "unavailable":
            unavailable.append(metric)
            if readout.get("value") is not None:
                raise HarnessMeasurementError(
                    f"unavailable readout carries a value: {metric}"
                )
        source_refs = readout.get("source_refs")
        if not isinstance(source_refs, list):
            raise HarnessMeasurementError(f"readout source_refs is invalid: {metric}")
        for index, ref in enumerate(source_refs):
            refs.add(_safe_ref(ref, f"{metric}.source_refs[{index}]"))

    projected = {
        **{f"quality.{name}": value for name, value in quality.items()},
        **{f"efficiency.{name}": value for name, value in efficiency.items()},
    }
    for metric, projected_value in projected.items():
        readout = readouts[metric]
        if projected_value == _UNKNOWN:
            if readout["status"] != "unavailable" or readout["value"] is not None:
                raise HarnessMeasurementError(f"{metric} does not match its readout")
        elif readout["status"] == "unavailable" or readout["value"] != projected_value:
            raise HarnessMeasurementError(f"{metric} does not match its readout")
    token_readout = readouts["cost.token_hard_cost"]
    if token_cost == _UNKNOWN:
        if (
            token_readout["status"] != "unavailable"
            or token_readout["value"] is not None
        ):
            raise HarnessMeasurementError(
                "cost.token_hard_cost does not match its readout"
            )
    elif (
        token_readout["status"] == "unavailable"
        or token_readout["value"] != token["total_tokens"]
    ):
        raise HarnessMeasurementError(
            "cost.token_hard_cost does not match its readout"
        )
    attention_readout = readouts["cost.human_attention"]
    if human_attention == _UNKNOWN:
        if (
            attention_readout["status"] != "unavailable"
            or attention_readout["value"] is not None
        ):
            raise HarnessMeasurementError(
                "cost.human_attention does not match its readout"
            )
    elif attention_readout["status"] == "unavailable" or _decimal_text(
        attention_readout["value"], "cost.human_attention.readout"
    ) != units:
        raise HarnessMeasurementError("cost.human_attention does not match its readout")

    missing = evidence.get("missing_producers")
    if missing != sorted(unavailable):
        raise HarnessMeasurementError(
            "missing_producers does not match unavailable readouts"
        )
    gaps = evidence.get("authority_gaps")
    if not isinstance(gaps, list) or gaps != sorted(set(gaps)) or any(
        not isinstance(gap, str) or not gap for gap in gaps
    ):
        raise HarnessMeasurementError("evidence.authority_gaps is invalid")
    expected_health = "degraded" if unavailable or gaps else "healthy"
    if evidence.get("sensor_health") != expected_health:
        raise HarnessMeasurementError("sensor_health does not match evidence gaps")

    canonical_value = json.loads(_canonical(root))
    canonical_value["snapshot_sha256"] = sha256(_canonical(root)).hexdigest()
    canonical_value["normalized_actual_cash_usd"] = normalized_cash
    canonical_value["safe_source_refs"] = sorted(refs | cost_source_refs)
    return canonical_value


def measurement_join_coverage(snapshot: Mapping[str, Any]) -> str:
    """Return exact/partial/unavailable for quality and efficiency leaves."""

    readouts = snapshot["evidence"]["readouts"]
    statuses = [
        readouts[metric]["status"]
        for metric in sorted(_METRICS)
        if metric.startswith(("quality.", "efficiency."))
    ]
    if all(status == "computable" for status in statuses):
        return "exact"
    if any(status != "unavailable" for status in statuses):
        return "partial"
    return "unavailable"
