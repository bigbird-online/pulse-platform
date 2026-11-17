"""Duke-facing recommendation artifacts that never authorize production apply."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .events import parse_timestamp, utc_text
from .query import QueryError, QueryWindow


RECOMMENDATION_SCHEMA = "recommendation_artifact.v1"
RECOMMENDATION_STATES = frozenset(
    {"hold", "promote", "rollback", "insufficient_evidence"}
)
_DIGEST_RE = re.compile(r"^(?:[A-Za-z0-9_.:-]+)?[0-9a-f]{64}$")


class RecommendationError(ValueError):
    """Raised when recommendation facts are malformed or unsafe."""


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RecommendationError(f"{field} must be a trimmed non-empty string")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise RecommendationError(f"{field} must be boolean")
    return value


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecommendationError(f"{field} must be a non-negative integer")
    return value


def _delta_count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecommendationError(f"{field} must be an integer")
    return value


def _decimal_text(value: object, field: str) -> str:
    if isinstance(value, bool):
        raise RecommendationError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RecommendationError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise RecommendationError(f"{field} must be a finite decimal")
    if parsed == 0:
        return "0"
    rendered = format(parsed, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _digest(value: object, field: str) -> str:
    parsed = _nonempty(value, field)
    if _DIGEST_RE.fullmatch(parsed) is None:
        raise RecommendationError(f"{field} must end in a SHA-256 hex digest")
    return parsed


def _string_set(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RecommendationError(f"{field} must be an array of preset names")
    result: set[str] = set()
    for item in value:
        result.add(_nonempty(item, field))
    return frozenset(result)


def _transition_set(value: object) -> frozenset[tuple[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RecommendationError("allowed_transitions must be an array")
    result: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes))
            or len(item) != 2
        ):
            raise RecommendationError(
                f"allowed_transitions[{index}] must be [from_preset, to_preset]"
            )
        result.add(
            (
                _nonempty(item[0], f"allowed_transitions[{index}][0]"),
                _nonempty(item[1], f"allowed_transitions[{index}][1]"),
            )
        )
    return frozenset(result)


def _coverage(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RecommendationError("coverage must be an object")
    required = {
        "event_count",
        "priced_event_count",
        "outcome_linked_event_count",
        "conservation_status",
    }
    if set(value) != required:
        raise RecommendationError("coverage has invalid fields")
    event_count = _count(value.get("event_count"), "coverage.event_count")
    priced = _count(value.get("priced_event_count"), "coverage.priced_event_count")
    outcomes = _count(
        value.get("outcome_linked_event_count"),
        "coverage.outcome_linked_event_count",
    )
    if priced > event_count or outcomes > event_count:
        raise RecommendationError("coverage counts cannot exceed event_count")
    conservation = value.get("conservation_status")
    if conservation not in {"pass", "failed"}:
        raise RecommendationError("coverage.conservation_status must be pass or failed")
    return {
        "event_count": event_count,
        "priced_event_count": priced,
        "outcome_linked_event_count": outcomes,
        "conservation_status": conservation,
    }


def _window(value: object) -> QueryWindow:
    if not isinstance(value, Mapping) or set(value) != {"from", "to"}:
        raise RecommendationError("evidence_window must contain exactly from and to")
    try:
        return QueryWindow.from_text(str(value.get("from")), str(value.get("to")))
    except QueryError as exc:
        raise RecommendationError(str(exc)) from exc


def _manager_plan_digest(value: Mapping[str, Any]) -> str:
    payload = dict(value)
    payload.pop("plan_digest", None)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "plan1_" + hashlib.sha256(encoded).hexdigest()


def _manager_plan_binding(
    value: object,
    *,
    current_preset: str,
    candidate_target: str,
    route_name: str,
    approved_presets: frozenset[str],
    allowed_transitions: frozenset[tuple[str, str]],
    active_route_digest: str,
    plan_digest: str,
) -> tuple[bool, bool, list[str]]:
    if value is None:
        return False, False, ["manager_plan_unbound"]
    if not isinstance(value, Mapping):
        return False, False, ["manager_plan_invalid"]
    expected_fields = {
        "schema",
        "schema_version",
        "mode",
        "current_preset",
        "target_preset",
        "active_route_digest",
        "target_route_digest",
        "current_runtime_profile_digest",
        "target_runtime_profile_digest",
        "global_compute_profile_changed",
        "route_diff",
        "expected_changed_routes",
        "single_consumer_candidate",
        "approved_presets",
        "allowed_transitions",
        "precondition",
        "plan_digest",
    }
    if set(value) != expected_fields:
        return False, False, ["manager_plan_invalid"]
    if (
        value.get("schema") != "runtime_priority_plan.v1"
        or value.get("schema_version") != 1
        or value.get("mode") != "plan_only"
    ):
        return False, False, ["manager_plan_invalid"]

    try:
        plan_active_digest = _digest(
            value.get("active_route_digest"), "manager_plan.active_route_digest"
        )
        _digest(value.get("target_route_digest"), "manager_plan.target_route_digest")
        _digest(
            value.get("current_runtime_profile_digest"),
            "manager_plan.current_runtime_profile_digest",
        )
        _digest(
            value.get("target_runtime_profile_digest"),
            "manager_plan.target_runtime_profile_digest",
        )
        claimed_plan_digest = _digest(
            value.get("plan_digest"), "manager_plan.plan_digest"
        )
        plan_approved = _string_set(
            value.get("approved_presets"), "manager_plan.approved_presets"
        )
        plan_transitions = _transition_set(value.get("allowed_transitions"))
        global_changed = _boolean(
            value.get("global_compute_profile_changed"),
            "manager_plan.global_compute_profile_changed",
        )
        single = _boolean(
            value.get("single_consumer_candidate"),
            "manager_plan.single_consumer_candidate",
        )
    except RecommendationError:
        return False, False, ["manager_plan_invalid"]

    route_diff = value.get("route_diff")
    expected = value.get("expected_changed_routes")
    precondition = value.get("precondition")
    if (
        not isinstance(route_diff, list)
        or any(not isinstance(row, Mapping) for row in route_diff)
        or not isinstance(expected, list)
        or any(not isinstance(item, str) or not item for item in expected)
        or not isinstance(precondition, Mapping)
        or set(precondition) != {"active_route_digest", "current_preset"}
    ):
        return False, False, ["manager_plan_invalid"]
    try:
        precondition_digest = _digest(
            precondition.get("active_route_digest"),
            "manager_plan.precondition.active_route_digest",
        )
    except RecommendationError:
        return False, False, ["manager_plan_invalid"]

    reasons: list[str] = []
    if value.get("current_preset") != current_preset:
        reasons.append("manager_plan_current_mismatch")
    if value.get("target_preset") != candidate_target:
        reasons.append("manager_plan_target_mismatch")
    if plan_active_digest != active_route_digest:
        reasons.append("manager_plan_active_digest_mismatch")
    if precondition_digest != plan_active_digest or precondition.get("current_preset") != value.get("current_preset"):
        reasons.append("manager_plan_precondition_mismatch")
    recomputed_plan_digest = _manager_plan_digest(value)
    if claimed_plan_digest != recomputed_plan_digest:
        reasons.append("manager_plan_digest_invalid")
    if claimed_plan_digest != plan_digest:
        reasons.append("manager_plan_digest_mismatch")
    if plan_approved != approved_presets:
        reasons.append("manager_plan_catalog_mismatch")
    if plan_transitions != allowed_transitions:
        reasons.append("manager_plan_transition_map_mismatch")

    expected_routes = [route_name] if candidate_target != current_preset else []
    if expected != expected_routes:
        reasons.append("manager_plan_route_diff_mismatch")
    if candidate_target != current_preset:
        if len(route_diff) != 1:
            reasons.append("manager_plan_route_diff_mismatch")
        else:
            row = route_diff[0]
            if row.get("route_name") != route_name or row.get("effective_profile_changed") is not True:
                reasons.append("manager_plan_route_diff_mismatch")
    single_consumer = (
        candidate_target == current_preset
        or (
            single is True
            and global_changed is False
            and expected == [route_name]
            and len(route_diff) == 1
        )
    )
    if candidate_target != current_preset and not single_consumer:
        reasons.append("manager_plan_not_single_consumer")
    return not reasons, single_consumer, sorted(set(reasons))


def build_recommendation_artifact(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Build one fail-closed artifact from read-only runtime-manager facts.

    ``approved_presets`` and ``allowed_transitions`` must come from the existing
    runtime manager. This module validates those facts; it never invents a
    profile, calls manager apply, or treats Duke approval as apply authority.
    """

    if not isinstance(facts, Mapping):
        raise RecommendationError("recommendation facts must be an object")
    required = {
        "requested_status",
        "target_consumer",
        "route_name",
        "current_preset",
        "target_preset",
        "rollback_preset",
        "approved_presets",
        "allowed_transitions",
        "evidence_window",
        "coverage",
        "expected_reference_cost_change_usd",
        "accepted_outcome_change",
        "risk_floor",
        "critical_route",
        "frontier_satisfied",
        "price_fresh",
        "active_route_digest",
        "plan_digest",
        "generated_at",
        "expires_at",
    }
    allowed_fields = required | {"manager_plan"}
    if not required.issubset(facts) or not set(facts).issubset(allowed_fields):
        missing = sorted(required - set(facts))
        extra = sorted(set(facts) - allowed_fields)
        raise RecommendationError(
            f"recommendation facts have invalid fields; missing={missing}, extra={extra}"
        )

    requested = facts.get("requested_status")
    if requested not in RECOMMENDATION_STATES:
        raise RecommendationError("requested_status is invalid")
    consumer = _nonempty(facts.get("target_consumer"), "target_consumer")
    route_name = _nonempty(facts.get("route_name"), "route_name")
    current = _nonempty(facts.get("current_preset"), "current_preset")
    requested_target = _nonempty(facts.get("target_preset"), "target_preset")
    requested_rollback = _nonempty(facts.get("rollback_preset"), "rollback_preset")
    approved = _string_set(facts.get("approved_presets"), "approved_presets")
    transitions = _transition_set(facts.get("allowed_transitions"))
    if current not in approved:
        raise RecommendationError(
            "current_preset is not present in the read-only manager catalog"
        )

    window = _window(facts.get("evidence_window"))
    coverage = _coverage(facts.get("coverage"))
    expected_cost_delta = _decimal_text(
        facts.get("expected_reference_cost_change_usd"),
        "expected_reference_cost_change_usd",
    )
    accepted_outcome_change = _delta_count(
        facts.get("accepted_outcome_change"), "accepted_outcome_change"
    )
    risk_floor = _nonempty(facts.get("risk_floor"), "risk_floor")
    critical_route = _boolean(facts.get("critical_route"), "critical_route")
    frontier_satisfied = _boolean(
        facts.get("frontier_satisfied"), "frontier_satisfied"
    )
    price_fresh = _boolean(facts.get("price_fresh"), "price_fresh")
    active_route_digest = _digest(
        facts.get("active_route_digest"), "active_route_digest"
    )
    plan_digest = _digest(facts.get("plan_digest"), "plan_digest")
    candidate_target_for_plan = (
        requested_rollback if requested == "rollback" else requested_target
    )
    manager_plan_bound, single_consumer_plan, manager_plan_reasons = _manager_plan_binding(
        facts.get("manager_plan"),
        current_preset=current,
        candidate_target=candidate_target_for_plan,
        route_name=route_name,
        approved_presets=approved,
        allowed_transitions=transitions,
        active_route_digest=active_route_digest,
        plan_digest=plan_digest,
    )

    try:
        generated_dt = parse_timestamp(facts.get("generated_at"), "generated_at")
        expires_dt = parse_timestamp(facts.get("expires_at"), "expires_at")
    except ValueError as exc:
        raise RecommendationError(str(exc)) from exc
    if expires_dt <= generated_dt:
        raise RecommendationError("expires_at must be after generated_at")
    ttl_seconds = int((expires_dt - generated_dt).total_seconds())

    reasons: list[str] = []
    if requested in {"promote", "rollback"}:
        reasons.extend(manager_plan_reasons)
    rollback = requested_rollback
    candidate_target = requested_rollback if requested == "rollback" else requested_target
    target = candidate_target

    preset_approved = target in approved and rollback in approved
    if not preset_approved:
        reasons.append("no_approved_preset")
        target = current
        if rollback not in approved:
            rollback = current

    transition_allowed = target == current or (current, target) in transitions
    if not transition_allowed:
        # Design §6.1 names the contract reason no_approved_preset for either a
        # missing target preset or a transition outside the manager allow-list.
        reasons.extend(("no_approved_preset", "transition_not_allowed"))
        target = current

    if not price_fresh:
        reasons.append("price_stale")
    price_coverage_complete = coverage["priced_event_count"] == coverage["event_count"]
    if not price_coverage_complete:
        reasons.append("price_coverage_incomplete")
    outcome_complete = (
        coverage["outcome_linked_event_count"] == coverage["event_count"]
    )
    if not outcome_complete:
        reasons.append("outcome_coverage_incomplete")
    conservation_ok = coverage["conservation_status"] == "pass"
    if not conservation_ok:
        reasons.append("conservation_failed")
    if critical_route and not frontier_satisfied:
        reasons.append("critical_route_below_frontier")

    control_plane_failure = any(
        reason in {"no_approved_preset", "transition_not_allowed"}
        or reason.startswith("manager_plan_")
        for reason in reasons
    )
    status = str(requested)
    if control_plane_failure:
        status = "insufficient_evidence"
        target = current
    elif reasons and status != "rollback":
        status = "insufficient_evidence"
        target = current

    if status in {"hold", "insufficient_evidence"}:
        target = current
    if status == "promote" and target == current:
        status = "insufficient_evidence"
        reasons.append("no_effective_transition")
    if status == "rollback" and target == current:
        status = "insufficient_evidence"
        reasons.append("no_effective_transition")

    changed_routes = [route_name] if target != current else []
    quality_constraints = {
        "price_fresh": price_fresh,
        "price_coverage_complete": price_coverage_complete,
        "outcome_complete": outcome_complete,
        "conservation_ok": conservation_ok,
        "preset_approved": preset_approved,
        "transition_allowed": transition_allowed,
        "manager_plan_bound": manager_plan_bound,
        "single_consumer_plan": single_consumer_plan,
        "frontier_satisfied": frontier_satisfied,
    }

    return {
        "schema": RECOMMENDATION_SCHEMA,
        "schema_version": 1,
        "mode": "recommend_only",
        "status": status,
        "target_consumer": consumer,
        "route_name": route_name,
        "current_profile": current,
        "suggested_profile": target,
        "rollback_profile": rollback,
        "current_preset": current,
        "target_preset": target,
        "rollback_preset": rollback,
        "evidence_window": window.to_dict(),
        "coverage": coverage,
        "expected_reference_cost_change_usd": expected_cost_delta,
        "accepted_outcome_change": accepted_outcome_change,
        "risk_floor": risk_floor,
        "quality_constraints": quality_constraints,
        "critical_route": critical_route,
        "expected_changed_routes": changed_routes,
        "active_route_digest": active_route_digest,
        "plan_digest": plan_digest,
        "validity": {
            "generated_at": utc_text(generated_dt),
            "expires_at": utc_text(expires_dt),
            "ttl_seconds": ttl_seconds,
        },
        "reasons": sorted(set(reasons)),
        "authorization": {
            "duke_decision": "pending",
            "production_apply_authorized": False,
        },
    }


def validate_recommendation_artifact(value: object) -> None:
    """Validate the Duke-facing safety invariants of an emitted artifact."""

    if not isinstance(value, Mapping):
        raise RecommendationError("recommendation artifact must be an object")
    if value.get("schema") != RECOMMENDATION_SCHEMA or value.get("schema_version") != 1:
        raise RecommendationError("unsupported recommendation artifact schema")
    if value.get("mode") != "recommend_only":
        raise RecommendationError("recommendation artifact must stay recommend_only")
    status = value.get("status")
    if status not in RECOMMENDATION_STATES:
        raise RecommendationError("recommendation status is invalid")
    authorization = value.get("authorization")
    if not isinstance(authorization, Mapping):
        raise RecommendationError("authorization must be an object")
    if authorization.get("production_apply_authorized") is not False:
        raise RecommendationError("recommendation artifact cannot authorize production apply")
    changed = value.get("expected_changed_routes")
    if not isinstance(changed, list) or len(changed) > 1:
        raise RecommendationError("recommendation may change at most one route")
    route_name = _nonempty(value.get("route_name"), "route_name")
    if changed and changed != [route_name]:
        raise RecommendationError("expected_changed_routes must contain only route_name")
    if status in {"hold", "insufficient_evidence"} and changed:
        raise RecommendationError(f"{status} cannot carry an effective route change")
    if status in {"promote", "rollback"} and changed != [route_name]:
        raise RecommendationError(f"{status} must identify exactly one changed route")

    constraints = value.get("quality_constraints")
    if not isinstance(constraints, Mapping):
        raise RecommendationError("quality_constraints must be an object")
    hard_evidence_failure = any(
        constraints.get(field) is not True
        for field in (
            "price_fresh",
            "price_coverage_complete",
            "outcome_complete",
            "conservation_ok",
        )
    ) or (
        value.get("critical_route") is True
        and constraints.get("frontier_satisfied") is not True
    )
    control_plane_failure = any(
        constraints.get(field) is not True
        for field in ("preset_approved", "transition_allowed")
    ) or (
        status in {"promote", "rollback"}
        and any(
            constraints.get(field) is not True
            for field in ("manager_plan_bound", "single_consumer_plan")
        )
    )
    if control_plane_failure and status != "insufficient_evidence":
        raise RecommendationError("invalid manager catalog/transition must fail closed")
    if hard_evidence_failure and status not in {"insufficient_evidence", "rollback"}:
        raise RecommendationError("unsafe evidence cannot produce hold/promote")
