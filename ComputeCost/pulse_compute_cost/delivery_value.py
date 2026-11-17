"""Exact Harness review and consumer-verified business-value evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Any, Mapping


HARNESS_REVIEW_SCHEMA = "work_item_review/v1"
HARNESS_REVIEW_SCHEMAS = (HARNESS_REVIEW_SCHEMA, "work_item_review/v2")
BUSINESS_VALUE_SCHEMA = "computecost_business_value_receipt/v1"
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_WI = re.compile(r"^wi_\d{8}_\d{3}$")
_OUTCOME = re.compile(r"^outcome_v1_[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SAFE_REF = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[^\x00]+$")
_NUMERIC_KINDS = {
    "time_saved",
    "cost_avoided",
    "throughput_change",
    "error_reduction",
    "risk_reduction",
}
_VALUE_DIRECTIONS = {"higher_is_value", "lower_is_value"}


class DeliveryValueError(ValueError):
    """Raised when delivery/value evidence is stale, unsafe, or ambiguous."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeliveryValueError(f"{field} must be an object")
    return value


def _exact(value: Mapping[str, Any], fields: set[str], name: str) -> None:
    if set(value) != fields:
        raise DeliveryValueError(f"{name} fields do not match the contract")


def _text(value: object, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryValueError(f"{field} must be a non-empty string")
    result = value.strip()
    if pattern is not None and pattern.fullmatch(result) is None:
        raise DeliveryValueError(f"{field} is invalid")
    return result


def _timestamp(value: object, field: str, *, canonical_only: bool = True) -> str:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryValueError(f"{field} must be a timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeliveryValueError(f"{field} must be timezone-aware")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical_only and raw != canonical:
        raise DeliveryValueError(f"{field} must be canonical UTC")
    return canonical


def _ref(value: object, field: str) -> str:
    raw = _text(value, field)
    if _SAFE_REF.fullmatch(raw) is None:
        raise DeliveryValueError(f"{field} must be repository-relative")
    return raw


def _decimal(value: object, field: str) -> tuple[Decimal, str]:
    if isinstance(value, bool) or not isinstance(value, str):
        raise DeliveryValueError(f"{field} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise DeliveryValueError(f"{field} must be a decimal") from exc
    if not parsed.is_finite():
        raise DeliveryValueError(f"{field} must be finite")
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    text = text or "0"
    if text != value or value == "-0":
        raise DeliveryValueError(f"{field} must be canonical")
    return parsed, text


def review_candidate_fingerprint(
    *,
    work_item_id: str,
    acceptance_fingerprint: str,
    artifact_set_sha256: str,
    repo_heads: list[Mapping[str, str]],
) -> str:
    material = {
        "work_item_id": work_item_id,
        "acceptance_fingerprint": acceptance_fingerprint,
        "artifact_set_sha256": artifact_set_sha256,
        "repo_heads": sorted(
            [dict(item) for item in repo_heads], key=lambda item: item["repo"]
        ),
    }
    return "sha256:" + sha256(_canonical(material)).hexdigest()


def _review_waiver(value: object, candidate: str, acceptance: str) -> dict[str, Any]:
    """Mirror the recorded Harness v2 envelope; never authorize a new waiver."""
    waiver = _object(value, "outcome_waiver")
    _exact(waiver, {"human_confirmed", "expected_candidate_fingerprint", "decision_source_ref",
        "decision_source_sha256", "reason", "accepted_risks", "frozen_claim", "frozen_producer",
        "frozen_change_type", "frozen_acceptance_fingerprint", "artifact_refs",
        "submission_events_sha256", "outcome_status", "outcome_receipt_sha256",
        "outcome_evidence_sha256"}, "outcome_waiver")
    if waiver["human_confirmed"] is not True:
        raise DeliveryValueError("waiver needs explicit human confirmation")
    if (waiver["expected_candidate_fingerprint"] != candidate or
            waiver["frozen_acceptance_fingerprint"] != acceptance):
        raise DeliveryValueError("waiver differs from the exact reviewed candidate or acceptance")
    def path_ref(value: object) -> str:
        ref = _ref(value, "waiver repository reference")
        if ref.startswith("~") or "\\" in ref or re.match(r"^[A-Za-z]:/", ref):
            raise DeliveryValueError("waiver references must be repository relative")
        return ref
    result = dict(waiver)
    for key in ("decision_source_sha256", "submission_events_sha256"):
        v = waiver[key]
        if not isinstance(v, str) or re.fullmatch(r"[a-f0-9]{64}", v) is None:
            raise DeliveryValueError("invalid waiver evidence digest")
    result["decision_source_ref"] = path_ref(waiver["decision_source_ref"])
    result["reason"] = _text(waiver["reason"], "waiver reason")
    risks = waiver["accepted_risks"]
    if (len(waiver["reason"]) > 2000 or not isinstance(risks, list) or not risks or
            any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in risks)):
        raise DeliveryValueError("waiver reason and accepted risks must be nonblank and bounded")
    result["accepted_risks"] = [v.strip() for v in risks]
    for key in ("frozen_claim", "frozen_producer", "frozen_change_type"):
        if not isinstance(waiver[key], str):
            raise DeliveryValueError("invalid frozen waiver field")
    artifacts = waiver["artifact_refs"]
    if not isinstance(artifacts, list):
        raise DeliveryValueError("invalid waiver artifact references")
    result["artifact_refs"] = [path_ref(v) for v in artifacts]
    status, receipt = waiver["outcome_status"], waiver["outcome_receipt_sha256"]
    if status not in ("missing", "blocked", "partial", "rejected", "accepted"):
        raise DeliveryValueError("unsupported original Outcome status")
    if ((status == "missing") != (receipt is None) or receipt is not None and
            (not isinstance(receipt, str) or re.fullmatch(r"[a-f0-9]{64}", receipt) is None)):
        raise DeliveryValueError("independent receipt absence/status mismatch")
    evidence = _object(waiver["outcome_evidence_sha256"], "captured Outcome evidence")
    if (status == "missing") != (not evidence):
        raise DeliveryValueError("original Outcome status and evidence disagree")
    result["outcome_evidence_sha256"] = {}
    for ref, fingerprint in evidence.items():
        if not isinstance(fingerprint, str) or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
            raise DeliveryValueError("invalid captured Outcome digest")
        result["outcome_evidence_sha256"][path_ref(ref)] = fingerprint
    return result


def validate_harness_review_snapshot(
    value: object, *, expected_work_item_id: str
) -> dict[str, Any]:
    """Validate the exact-candidate Harness human-review projection."""

    root = _object(value, "Harness review")
    schema = root.get("schema_version")
    if schema not in HARNESS_REVIEW_SCHEMAS:
        raise DeliveryValueError("unsupported Harness review schema")
    _exact(
        root,
        {
            "schema_version",
            "work_item_id",
            "reviewer",
            "decision",
            "acceptance_fingerprint",
            "artifact_set_sha256",
            "repo_heads",
            "candidate_fingerprint",
            "evidence_refs",
            "reviewed_at",
        } | ({"outcome_waiver"} if schema == "work_item_review/v2" else set()),
        "Harness review",
    )
    work_item_id = _text(root.get("work_item_id"), "work_item_id", _WI)
    if work_item_id != expected_work_item_id:
        raise DeliveryValueError("Harness review belongs to another work item")
    reviewer = _text(root.get("reviewer"), "reviewer")
    decision = root.get("decision")
    if decision not in {"approved", "changes_requested"}:
        raise DeliveryValueError("Harness review decision is invalid")
    acceptance = _text(
        root.get("acceptance_fingerprint"), "acceptance_fingerprint", _SHA
    )
    artifact_set = _text(
        root.get("artifact_set_sha256"), "artifact_set_sha256", _SHA
    )
    heads_raw = root.get("repo_heads")
    if not isinstance(heads_raw, list) or not heads_raw:
        raise DeliveryValueError("Harness review requires repo heads")
    heads: list[dict[str, str]] = []
    for index, item in enumerate(heads_raw):
        head = _object(item, f"repo_heads[{index}]")
        _exact(head, {"repo", "revision"}, f"repo_heads[{index}]")
        repo = _text(head.get("repo"), f"repo_heads[{index}].repo")
        # Harness ReviewRepoHead uses an opaque logical name, not necessarily
        # an org/repo slug. Preserve it verbatim for the candidate fingerprint;
        # never infer an organization or rewrite an already reviewed identity.
        if repo.startswith(("/", "~")) or ".." in repo:
            raise DeliveryValueError("review repository identity is invalid")
        revision = _text(
            head.get("revision"), f"repo_heads[{index}].revision", _GIT_SHA
        )
        heads.append({"repo": repo, "revision": revision})
    if heads != sorted(heads, key=lambda item: item["repo"]) or len(
        {head["repo"] for head in heads}
    ) != len(heads):
        raise DeliveryValueError("review repo heads must be sorted and unique")
    candidate = _text(
        root.get("candidate_fingerprint"), "candidate_fingerprint", _SHA
    )
    expected_candidate = review_candidate_fingerprint(
        work_item_id=work_item_id,
        acceptance_fingerprint=acceptance,
        artifact_set_sha256=artifact_set,
        repo_heads=heads,
    )
    if candidate != expected_candidate:
        raise DeliveryValueError("review candidate fingerprint does not match")
    refs_raw = root.get("evidence_refs")
    if not isinstance(refs_raw, list) or not refs_raw:
        raise DeliveryValueError("Harness review requires evidence refs")
    refs = [_ref(item, f"evidence_refs[{index}]") for index, item in enumerate(refs_raw)]
    if len(refs) != len(set(refs)):
        raise DeliveryValueError("review evidence refs must be unique")
    refs = sorted(refs)
    # The native Harness datetime serializer may emit +00:00 (or another
    # explicit offset). Normalize the instant only in this consumer projection.
    # Business-value receipts retain their stricter canonical-UTC wire contract.
    reviewed_at = _timestamp(root.get("reviewed_at"), "reviewed_at", canonical_only=False)
    normalized = {
        "schema_version": schema,
        "work_item_id": work_item_id,
        "reviewer": reviewer,
        "decision": decision,
        "acceptance_fingerprint": acceptance,
        "artifact_set_sha256": artifact_set,
        "repo_heads": heads,
        "candidate_fingerprint": candidate,
        "evidence_refs": refs,
        "reviewed_at": reviewed_at,
    }
    if schema == "work_item_review/v2":
        if decision != "approved":
            raise DeliveryValueError("review v2 requires an approved explicit waiver")
        normalized["outcome_waiver"] = _review_waiver(root["outcome_waiver"], candidate, acceptance)
    normalized["snapshot_sha256"] = sha256(_canonical(normalized)).hexdigest()
    return normalized


def _validate_measurement(value: object) -> dict[str, object]:
    measurement = _object(value, "measurement")
    _exact(
        measurement,
        {
            "kind",
            "metric_name",
            "unit",
            "baseline",
            "current",
            "delta",
            "value_direction",
            "statement",
        },
        "measurement",
    )
    kind = _text(measurement.get("kind"), "measurement.kind", _TOKEN)
    metric_name = _text(
        measurement.get("metric_name"), "measurement.metric_name", _TOKEN
    )
    statement = _text(measurement.get("statement"), "measurement.statement")
    if len(statement) > 500:
        raise DeliveryValueError("business value statement is too long")
    if kind == "qualitative":
        if any(measurement.get(field) is not None for field in ("baseline", "current", "delta")):
            raise DeliveryValueError("qualitative value cannot carry numeric fields")
        if measurement.get("unit") != "qualitative" or measurement.get(
            "value_direction"
        ) != "qualitative":
            raise DeliveryValueError("qualitative value markers are invalid")
        return {
            "kind": kind,
            "metric_name": metric_name,
            "unit": "qualitative",
            "baseline": None,
            "current": None,
            "delta": None,
            "value_direction": "qualitative",
            "statement": statement,
        }
    if kind not in _NUMERIC_KINDS:
        raise DeliveryValueError("business value kind is unsupported")
    unit = _text(measurement.get("unit"), "measurement.unit", _TOKEN)
    direction = measurement.get("value_direction")
    if direction not in _VALUE_DIRECTIONS:
        raise DeliveryValueError("business value direction is invalid")
    baseline, baseline_text = _decimal(measurement.get("baseline"), "measurement.baseline")
    current, current_text = _decimal(measurement.get("current"), "measurement.current")
    delta, delta_text = _decimal(measurement.get("delta"), "measurement.delta")
    if current - baseline != delta:
        raise DeliveryValueError("business value delta does not reconcile")
    return {
        "kind": kind,
        "metric_name": metric_name,
        "unit": unit,
        "baseline": baseline_text,
        "current": current_text,
        "delta": delta_text,
        "value_direction": direction,
        "statement": statement,
    }


def build_business_value_receipt(
    *,
    work_item_id: str,
    outcome_ref: str,
    artifact_set_sha256: str,
    producer: str,
    consumer: str,
    verifier: str,
    measurement: Mapping[str, Any],
    evidence: list[Mapping[str, str]],
    observed_at: str,
) -> dict[str, Any]:
    """Build a consumer value receipt; callers still own evidence authority."""

    unsigned: dict[str, Any] = {
        "schema": BUSINESS_VALUE_SCHEMA,
        "work_item_id": work_item_id,
        "outcome_ref": outcome_ref,
        "artifact_set_sha256": artifact_set_sha256,
        "producer": producer,
        "producer_role": "downstream_consumer",
        "consumer": consumer,
        "verifier": verifier,
        "measurement": dict(measurement),
        "evidence": sorted(
            [dict(item) for item in evidence], key=lambda item: item.get("ref", "")
        ),
        "observed_at": observed_at,
    }
    normalized_measurement = _validate_measurement(unsigned["measurement"])
    unsigned["measurement"] = normalized_measurement
    unsigned["receipt_sha256"] = "sha256:" + sha256(_canonical(unsigned)).hexdigest()
    return validate_business_value_receipt(
        unsigned,
        expected_work_item_id=work_item_id,
        expected_artifact_set_sha256=artifact_set_sha256,
    )


def validate_business_value_receipt(
    value: object, *, expected_work_item_id: str, expected_artifact_set_sha256: str
) -> dict[str, Any]:
    """Validate one downstream-consumer value receipt bound to reviewed artifacts."""

    root = _object(value, "business value receipt")
    _exact(
        root,
        {
            "schema",
            "work_item_id",
            "outcome_ref",
            "artifact_set_sha256",
            "producer",
            "producer_role",
            "consumer",
            "verifier",
            "measurement",
            "evidence",
            "observed_at",
            "receipt_sha256",
        },
        "business value receipt",
    )
    if root.get("schema") != BUSINESS_VALUE_SCHEMA:
        raise DeliveryValueError("unsupported business value receipt schema")
    work_item_id = _text(root.get("work_item_id"), "work_item_id", _WI)
    if work_item_id != expected_work_item_id:
        raise DeliveryValueError("business value belongs to another work item")
    outcome_ref = _text(root.get("outcome_ref"), "outcome_ref", _OUTCOME)
    artifact_set = _text(
        root.get("artifact_set_sha256"), "artifact_set_sha256", _SHA
    )
    if artifact_set != expected_artifact_set_sha256:
        raise DeliveryValueError("business value does not bind the reviewed artifacts")
    producer = _text(root.get("producer"), "producer", _TOKEN)
    consumer = _text(root.get("consumer"), "consumer", _TOKEN)
    verifier = _text(root.get("verifier"), "verifier", _TOKEN)
    if root.get("producer_role") != "downstream_consumer":
        raise DeliveryValueError("business value producer is not a downstream consumer")
    if verifier in {producer, consumer}:
        raise DeliveryValueError("business value requires an independent verifier")
    measurement = _validate_measurement(root.get("measurement"))
    evidence_raw = root.get("evidence")
    if not isinstance(evidence_raw, list) or not evidence_raw:
        raise DeliveryValueError("business value requires evidence")
    evidence: list[dict[str, str]] = []
    for index, item in enumerate(evidence_raw):
        artifact = _object(item, f"evidence[{index}]")
        _exact(artifact, {"ref", "sha256"}, f"evidence[{index}]")
        evidence.append(
            {
                "ref": _ref(artifact.get("ref"), f"evidence[{index}].ref"),
                "sha256": _text(
                    artifact.get("sha256"), f"evidence[{index}].sha256", _SHA
                ),
            }
        )
    if evidence != sorted(evidence, key=lambda item: item["ref"]) or len(
        {item["ref"] for item in evidence}
    ) != len(evidence):
        raise DeliveryValueError("business value evidence must be sorted and unique")
    observed_at = _timestamp(root.get("observed_at"), "observed_at")
    unsigned = {
        "schema": BUSINESS_VALUE_SCHEMA,
        "work_item_id": work_item_id,
        "outcome_ref": outcome_ref,
        "artifact_set_sha256": artifact_set,
        "producer": producer,
        "producer_role": "downstream_consumer",
        "consumer": consumer,
        "verifier": verifier,
        "measurement": measurement,
        "evidence": evidence,
        "observed_at": observed_at,
    }
    digest = _text(root.get("receipt_sha256"), "receipt_sha256", _SHA)
    if digest != "sha256:" + sha256(_canonical(unsigned)).hexdigest():
        raise DeliveryValueError("business value receipt digest does not match")
    return {**unsigned, "receipt_sha256": digest}
