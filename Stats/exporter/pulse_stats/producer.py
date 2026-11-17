"""One-shot snapshot assembly from already-normalized, caller-supplied data."""

from __future__ import annotations

from datetime import datetime
import hashlib
import re
from typing import Iterable, Mapping, Optional, Sequence

from .global_authority import PUBLICATION_ENDPOINTS
from .reducer import (
    TIER_ACT,
    TIER_DONE,
    TIER_WATCH,
    AlertProjection,
    IncidentProjection,
)


GLOBAL_AUTHORITY_EVIDENCE_SCHEMA = "pulse_stats_global_authority_evidence/v1"
GLOBAL_READ_MODEL_SCHEMA = "pulse_stats_global_read_model/v1"
GLOBAL_AUTHORITY_CAPABILITY_IDS = frozenset({"s2.ledger_invariants"})
GLOBAL_AUTHORITY_LOCAL_METRIC_IDS = frozenset({"harness.unreadable_records"})
GLOBAL_AUTHORITY_REPOSITORY = "bigbird-online/PULSE"
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def authority_revision_for_git(source_revision: object) -> str | None:
    """Return the one authority identity used by SCL producer and Stats.

    A carrier may be fresh and internally self-consistent while the service
    checkout supplying the actual metrics has already moved (or never
    converged). Binding both sides to ``repository@full_sha`` prevents that
    split from being labelled global/validated.
    """

    if not isinstance(source_revision, str) or _GIT_SHA_RE.fullmatch(source_revision) is None:
        return None
    payload = f"{GLOBAL_AUTHORITY_REPOSITORY}@{source_revision}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _evidence_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def project_global_authority(
    evidence: Mapping[str, object] | None,
    *,
    generated_at: datetime,
    source_revision: object = None,
) -> dict[str, object]:
    """Validate the four proofs required before Stats may claim global truth.

    A local checkout being readable is deliberately not one of those proofs.
    The projection is privacy-safe: it exposes counts and content digests, not
    hostnames or filesystem paths.
    """
    proof_names = (
        "authority_revision",
        "source_revision",
        "required_endpoint_coverage",
        "materialization",
        "freshness",
    )
    if not isinstance(evidence, Mapping):
        return {
            "schema_version": GLOBAL_READ_MODEL_SCHEMA,
            "status": "unknown",
            "scope": "unknown",
            "authority_revision": None,
            "required_endpoint_count": None,
            "materialized_endpoint_count": None,
            "materialization_digest": None,
            "freshness": "unknown",
            "degraded_reasons": list(proof_names),
        }

    degraded: list[str] = []
    if evidence.get("schema_version") != GLOBAL_AUTHORITY_EVIDENCE_SCHEMA:
        degraded.extend(proof_names)

    revision = evidence.get("authority_revision")
    valid_revision = isinstance(revision, str) and _SHA256_REF_RE.fullmatch(revision) is not None
    if not valid_revision:
        degraded.append("authority_revision")
    expected_revision = authority_revision_for_git(source_revision)
    if expected_revision is None or revision != expected_revision:
        degraded.append("source_revision")

    raw_required = evidence.get("required_endpoints")
    required = (
        [item for item in raw_required if isinstance(item, str)]
        if isinstance(raw_required, list)
        else []
    )
    required_unique = set(required)
    if raw_required != list(PUBLICATION_ENDPOINTS):
        degraded.append("required_endpoint_coverage")

    raw_materializations = evidence.get("materializations")
    materializations = (
        [item for item in raw_materializations if isinstance(item, Mapping)]
        if isinstance(raw_materializations, list)
        else []
    )
    by_endpoint = {
        str(item.get("endpoint_id")): item
        for item in materializations
        if isinstance(item.get("endpoint_id"), str) and str(item.get("endpoint_id")).strip()
    }
    if required_unique and not required_unique.issubset(by_endpoint):
        degraded.append("required_endpoint_coverage")
    valid_materialization_ids: set[str] = set()
    materialization_digests: set[str] = set()
    for endpoint_id in required_unique:
        row = by_endpoint.get(endpoint_id)
        if row is None:
            continue
        digest = row.get("digest")
        if (
            not valid_revision
            or row.get("authority_revision") != revision
            or not isinstance(digest, str)
            or _SHA256_REF_RE.fullmatch(digest) is None
        ):
            continue
        valid_materialization_ids.add(endpoint_id)
        materialization_digests.add(digest)
    materialization_complete = (
        raw_required == list(PUBLICATION_ENDPOINTS)
        and isinstance(raw_materializations, list)
        and len(raw_materializations) == len(PUBLICATION_ENDPOINTS)
        and all(set(row) == {"endpoint_id", "authority_revision", "digest"}
                for row in materializations)
        and [row.get("endpoint_id") for row in materializations]
            == list(PUBLICATION_ENDPOINTS)
        and valid_materialization_ids == required_unique
        and len(materialization_digests) == 1
    )
    if not materialization_complete:
        degraded.append("materialization")

    observed_at = _evidence_timestamp(evidence.get("observed_at"))
    expires_at = _evidence_timestamp(evidence.get("expires_at"))
    freshness = "unknown"
    if observed_at is not None and expires_at is not None:
        freshness = "fresh" if observed_at <= generated_at < expires_at else "stale"
    if freshness != "fresh":
        degraded.append("freshness")

    degraded = list(dict.fromkeys(degraded))
    status = "validated" if not degraded else "partial"
    return {
        "schema_version": GLOBAL_READ_MODEL_SCHEMA,
        "status": status,
        "scope": "global" if status == "validated" else "partial",
        "authority_revision": revision if valid_revision else None,
        "required_endpoint_count": len(required_unique) if raw_required is not None else None,
        "materialized_endpoint_count": len(valid_materialization_ids),
        "materialization_digest": (
            next(iter(materialization_digests)) if materialization_complete else None
        ),
        "freshness": freshness,
        "degraded_reasons": degraded,
    }


def _degrade_global_metrics(
    metrics: Sequence[Mapping[str, object]],
    authority: Mapping[str, object],
) -> list[dict[str, object]]:
    status = str(authority["status"])
    if status == "validated":
        return [dict(item) for item in metrics]
    reason = "Missing global authority proof: " + ", ".join(
        str(item) for item in authority.get("degraded_reasons", [])
    )
    scope = "partial authority coverage" if status == "partial" else "authority scope unknown"
    projected: list[dict[str, object]] = []
    for source in metrics:
        item = dict(source)
        metric_id = str(item.get("id") or "")
        capability_id = str(item.get("capability_id") or "")
        is_global = (
            metric_id not in GLOBAL_AUTHORITY_LOCAL_METRIC_IDS
            and (
                metric_id.startswith("harness.")
                or capability_id in GLOBAL_AUTHORITY_CAPABILITY_IDS
            )
        )
        if is_global:
            if item.get("quality") == "validated":
                item["quality"] = (
                    "insufficient" if item.get("status") == "available" else "unavailable"
                )
            item["scope"] = scope
            existing = item.get("reason")
            item["reason"] = f"{existing}; {reason}" if existing else reason
        projected.append(item)
    return projected


def _degrade_global_capabilities(
    capabilities: Sequence[Mapping[str, object]],
    authority: Mapping[str, object],
) -> list[dict[str, object]]:
    status = str(authority["status"])
    if status == "validated":
        return [dict(item) for item in capabilities]
    scope = "partial authority coverage" if status == "partial" else "authority scope unknown"
    projected: list[dict[str, object]] = []
    for source in capabilities:
        item = dict(source)
        if str(item.get("id") or "") in GLOBAL_AUTHORITY_CAPABILITY_IDS:
            if item.get("implementation_status") == "active_validated":
                item["implementation_status"] = "active_partial"
            item["scope"] = scope
            item["reason"] = "Global authority evidence is incomplete"
        projected.append(item)
    return projected


def reduce_runtime_health(
    capabilities: Sequence[Mapping[str, object]],
    incidents: Iterable[IncidentProjection],
) -> str:
    open_severities = {item.severity for item in incidents if item.state == "open"}
    if "critical" in open_severities:
        return "critical"
    if "warning" in open_severities:
        return "warning"

    runtime = [item for item in capabilities if item.get("aggregation_role") == "runtime"]
    if not runtime:
        return "unknown"
    freshness = {str(item.get("freshness", {}).get("state", "unknown")) for item in runtime}
    if "stale" in freshness:
        return "stale"
    if freshness & {"unknown", "error"}:
        return "unknown"
    return "ok"


def reduce_coverage(
    capabilities: Sequence[Mapping[str, object]],
    required_capability_ids: Iterable[str],
) -> str:
    required = set(required_capability_ids)
    if not required:
        return "unknown"
    by_id = {str(item.get("id")): item for item in capabilities}
    present = [by_id[item] for item in required if item in by_id]
    if not present:
        return "unknown"
    for item in present:
        freshness = item.get("freshness", {})
        if isinstance(freshness, Mapping) and freshness.get("state") in {"unknown", "error"}:
            return "unknown"
    if len(present) != len(required):
        return "partial"
    for item in present:
        if item.get("implementation_status") != "active_validated":
            return "partial"
        freshness = item.get("freshness", {})
        if not isinstance(freshness, Mapping) or freshness.get("state") != "fresh":
            return "partial"
    return "full"


def _alert_dict(alert: AlertProjection) -> dict[str, object]:
    return {
        "event_id": alert.event_id,
        "projection_status": alert.projection_status,
        "severity": alert.severity,
        "capability_id": alert.capability_id,
        "title": alert.title,
        "occurred_at": alert.occurred_at.isoformat(),
        "reason": alert.reason,
        # `done` entries stay in this list -- nothing is dropped -- but the tier
        # lets the App filter them out of the main view without a second array.
        "tier": alert.tier,
        **({"work_item_id": alert.work_item_id} if alert.work_item_id is not None else {}),
    }


def build_snapshot(
    *,
    generated_at: datetime,
    expires_at: datetime,
    capabilities: Sequence[Mapping[str, object]],
    required_capability_ids: Iterable[str],
    incidents: Sequence[IncidentProjection],
    alerts: Sequence[AlertProjection] = (),
    metrics: Sequence[Mapping[str, object]] = (),
    provenance: Mapping[str, object],
    headline: str,
    baseline_version: str = "0.2.1",
    runtime_health_override: Optional[str] = None,
    global_authority_evidence: Optional[Mapping[str, object]] = None,
    global_authority: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    source_revisions = provenance.get("source_revisions")
    harness_revision = (
        source_revisions.get("harness")
        if isinstance(source_revisions, Mapping)
        else None
    )
    # A native-core projection is already validated against its own facts.
    authority = dict(global_authority) if global_authority is not None else project_global_authority(
        global_authority_evidence,
        generated_at=generated_at,
        source_revision=harness_revision,
    )
    has_global_metrics = any(
        str(item.get("id") or "") not in GLOBAL_AUTHORITY_LOCAL_METRIC_IDS
        and (
            str(item.get("id") or "").startswith("harness.")
            or str(item.get("capability_id") or "") in GLOBAL_AUTHORITY_CAPABILITY_IDS
        )
        for item in metrics
    )
    projected_capabilities = (
        _degrade_global_capabilities(capabilities, authority)
        if has_global_metrics
        else [dict(item) for item in capabilities]
    )
    projected_metrics = (
        _degrade_global_metrics(metrics, authority)
        if has_global_metrics
        else [dict(item) for item in metrics]
    )
    runtime_health = runtime_health_override or reduce_runtime_health(
        projected_capabilities, incidents
    )
    coverage = reduce_coverage(projected_capabilities, required_capability_ids)
    if has_global_metrics and authority["status"] == "unknown":
        coverage = "unknown"
    elif has_global_metrics and authority["status"] == "partial" and coverage == "full":
        coverage = "partial"
    alert_dicts = [_alert_dict(item) for item in alerts]
    return {
        "schema_version": "pulse.monitoring.snapshot.v2",
        "generated_at": generated_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "baseline_version": baseline_version,
        "overall": {
            "runtime_health": runtime_health,
            "coverage": coverage,
            "open_incident_count": sum(item.state == "open" for item in incidents),
            "headline": headline,
        },
        "capabilities": projected_capabilities,
        "incidents": [item.as_snapshot_dict() for item in incidents],
        "metrics": projected_metrics,
        "alerts": alert_dicts,
        # Pre-counted so the menu bar can render "N 件要你出手" without walking
        # the alert list itself.
        "alert_tiers": {
            TIER_ACT: sum(item["tier"] == TIER_ACT for item in alert_dicts),
            TIER_WATCH: sum(item["tier"] == TIER_WATCH for item in alert_dicts),
            TIER_DONE: sum(item["tier"] == TIER_DONE for item in alert_dicts),
        },
        "provenance": dict(provenance),
        "global_authority": authority,
    }
