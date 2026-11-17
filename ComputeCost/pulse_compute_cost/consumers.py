"""Strict, zero-recalculation adapters for S3 weekly patrol and Duke consumers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .drivers import validate_cost_driver_bridge
from .query import QUERY_SCHEMA
from .recommendations import validate_recommendation_artifact


class ConsumerError(ValueError):
    """Raised when a downstream consumer artifact is unsafe or ambiguous."""


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConsumerError(f"{field} must be an object")
    return value


def _rows(value: object, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise ConsumerError(f"{field} must be an array of objects")
    return [dict(row) for row in value]


def consume_weekly_query(value: object) -> dict[str, Any]:
    """Project a validated ISO-week query into the weekly-review input contract.

    No token/cost arithmetic happens here.  The patrol receives exactly the
    query engine's totals and three required partitions, so it cannot silently
    drift back to awk/filter/manual recomputation.
    """

    document = _mapping(value, "weekly query")
    if document.get("schema") != QUERY_SCHEMA or document.get("schema_version") != 1:
        raise ConsumerError("weekly consumer requires cost_query.v1")
    if document.get("mode") != "shadow":
        raise ConsumerError("weekly consumer requires shadow query mode")

    window = _mapping(document.get("window"), "window")
    if window.get("duration_days") != 7 or not isinstance(window.get("iso_week"), str):
        raise ConsumerError("weekly consumer requires one ISO-week window")
    if window.get("semantics") != "half_open" or window.get("timezone") != "Asia/Shanghai":
        raise ConsumerError("weekly consumer requires Beijing half-open semantics")

    conservation = _mapping(document.get("conservation"), "conservation")
    if conservation.get("status") != "pass":
        raise ConsumerError("weekly consumer refuses non-conserving query")

    driver_delta = _mapping(document.get("driver_delta"), "driver_delta")
    try:
        validate_cost_driver_bridge(driver_delta)
    except ValueError as exc:
        raise ConsumerError(f"invalid S4 driver bridge: {exc}") from exc

    groups = _mapping(document.get("groups"), "groups")
    by_agent = _rows(document.get("by_agent"), "by_agent")
    by_consumer = _rows(document.get("by_consumer"), "by_consumer")
    by_outcome = _rows(document.get("by_outcome"), "by_outcome")
    if by_agent != _rows(groups.get("agent"), "groups.agent"):
        raise ConsumerError("by_agent must be the query engine agent partition")
    if by_consumer != _rows(groups.get("consumer"), "groups.consumer"):
        raise ConsumerError("by_consumer must be the query engine consumer partition")
    if by_outcome != _rows(groups.get("outcome"), "groups.outcome"):
        raise ConsumerError("by_outcome must be the query engine outcome partition")

    totals = dict(_mapping(document.get("totals"), "totals"))
    baseline = dict(_mapping(document.get("baseline"), "baseline"))
    quality = dict(_mapping(document.get("quality"), "quality"))

    return {
        "schema": "weekly_cost_consumption.v1",
        "schema_version": 1,
        "mode": "shadow_read_only",
        "source_schema": QUERY_SCHEMA,
        "generated_at": document.get("generated_at"),
        "window": dict(window),
        "totals": totals,
        "baseline": baseline,
        "by_agent": by_agent,
        "by_consumer": by_consumer,
        "by_outcome": by_outcome,
        "driver_delta": dict(driver_delta),
        "weekly_capacity": deepcopy(document.get("weekly_capacity")),
        "managed_cost": deepcopy(document.get("managed_cost")),
        "quality": quality,
        "conservation_status": "pass",
        "manual_recalculation_required": False,
    }


def consume_duke_recommendation(value: object) -> dict[str, Any]:
    """Project one validated recommendation into Duke's review-only context."""

    try:
        validate_recommendation_artifact(value)
    except ValueError as exc:
        raise ConsumerError(str(exc)) from exc
    document = _mapping(value, "recommendation artifact")
    authorization = _mapping(document.get("authorization"), "authorization")
    if authorization.get("production_apply_authorized") is not False:
        raise ConsumerError("Duke consumer cannot receive production apply authorization")

    return {
        "schema": "duke_recommendation_consumption.v1",
        "schema_version": 1,
        "mode": "recommend_only_review",
        "source_schema": document.get("schema"),
        "status": document.get("status"),
        "target_consumer": document.get("target_consumer"),
        "route_name": document.get("route_name"),
        "current_preset": document.get("current_preset"),
        "target_preset": document.get("target_preset"),
        "rollback_preset": document.get("rollback_preset"),
        "evidence_window": deepcopy(document.get("evidence_window")),
        "coverage": deepcopy(document.get("coverage")),
        "expected_reference_cost_change_usd": document.get(
            "expected_reference_cost_change_usd"
        ),
        "accepted_outcome_change": document.get("accepted_outcome_change"),
        "risk_floor": document.get("risk_floor"),
        "quality_constraints": deepcopy(document.get("quality_constraints")),
        "expected_changed_routes": deepcopy(document.get("expected_changed_routes")),
        "active_route_digest": document.get("active_route_digest"),
        "plan_digest": document.get("plan_digest"),
        "validity": deepcopy(document.get("validity")),
        "reasons": deepcopy(document.get("reasons")),
        "duke_decision": authorization.get("duke_decision"),
        "production_apply_authorized": False,
        "manual_recalculation_required": False,
    }
