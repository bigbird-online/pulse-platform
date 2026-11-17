"""WI-level ComputeCost v4 report over exact App Server epoch seals.

The single WI machine cash policy is flat standard OpenRouter token rates: the
calculated total is the WI machine cash used in ROI, independent of procurement
route or subscription. Producer-native cash is never a competing authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from .wi_pricing import WiPricingError, usage_evidence_sha256, retrospective_pricing_cohort
from .attention import build_attention_vector
from .app_server_account import (
    AppServerAccountError,
    validate_app_server_account_snapshot,
)
from .app_server_usage import AppServerUsageError, AppServerUsageLedger, UsageWatermark
from .app_server_thread_usage import (
    AppServerThreadUsageError,
    validate_app_server_thread_usage_snapshot,
)
from .delivery_value import (
    DeliveryValueError,
    validate_business_value_receipt,
    validate_harness_review_snapshot,
)
from .harness_measurement import (
    HarnessMeasurementError,
    measurement_join_coverage,
    validate_harness_measurement_snapshot,
)
from .pi_usage import PiUsageError, validate_pi_session_snapshot
from .retrospective_usage import (
    RetrospectiveUsageError,
    validate_retrospective_candidate,
)
from .scl_measurement import SclMeasurementError, validate_scl_measurement_snapshot
from .token_semantics import InputTokenClasses, TokenSemanticsError


WI_REPORT_SCHEMA = "computecost_wi_report/v4"
# Single WI machine-cash policy: flat standard OpenRouter Token rates. This is
# the WI machine cash cost used in ROI, independent of procurement route or
# subscription. Producer-native cash never overrides it.
CASH_BASIS = "openrouter_standard_token_cash"
CASH_COVERAGE_CALCULATED = "calculated_from_digest_bound_openrouter_catalog"
# Deprecated historical basis strings ("..._not_provider_cash") are deliberately
# rejected, not aliased: no compatibility alias is consumed by the report or ROI.
_RETROSPECTIVE_CLASSIFICATION = "pi_only_estimate_may_include_unrelated_turns"
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class WiReportError(ValueError):
    """Raised when a WI report would overstate or double-count evidence."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _zero() -> dict[str, int]:
    return {field: 0 for field in _TOKEN_FIELDS}


def _add(target: dict[str, int], usage: Mapping[str, object]) -> None:
    parsed = UsageWatermark.from_dict(usage, "epoch_delta")
    for field in _TOKEN_FIELDS:
        target[field] += getattr(parsed, field)


def _partition_key(context: Mapping[str, object]) -> str:
    return "|".join(
        str(context[field])
        for field in ("runtime", "model", "procurement_source")
    )


def _sum_partitions(partitions: Iterable[Mapping[str, object]]) -> dict[str, int]:
    result = _zero()
    for partition in partitions:
        tokens = partition.get("tokens")
        if not isinstance(tokens, Mapping):
            raise WiReportError("partition tokens must be an object")
        _add(result, tokens)
    return result


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WiReportError(f"{label} must be a Decimal string")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise WiReportError(f"{label} must be a Decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise WiReportError(f"{label} must be finite and non-negative")
    return parsed


def _validate_flat_openrouter_valuation(
    value: Mapping[str, Any],
    *,
    work_item_id: str,
) -> None:
    expected_fields = {
        "schema", "work_item_id", "status", "basis", "pricing_policy",
        "pricing_authority", "pricing_source_url", "catalog_sha256",
        "retrieved_at", "partitions", "total_usd", "valuation_sha256",
        "usage_evidence_sha256",
    }
    if set(value) != expected_fields or (
        value.get("schema") != "computecost_openrouter_cash_valuation/v2"
        or value.get("work_item_id") != work_item_id
        or value.get("status") != "calculated"
        or value.get("basis") != CASH_BASIS
        or value.get("pricing_policy")
        != "openrouter_standard_rates_no_overrides_no_subscription_settlement"
        or value.get("pricing_authority") != "openrouter.models"
        or value.get("pricing_source_url")
        != "https://openrouter.ai/api/v1/models"
    ):
        raise WiReportError("WI valuation does not use the OpenRouter standard token cash policy")
    source_digest = value.get("usage_evidence_sha256")
    if (not isinstance(source_digest, str) or len(source_digest) != 64
            or any(character not in "0123456789abcdef" for character in source_digest)):
        raise WiReportError("WI valuation usage evidence digest is invalid")
    retrieved_at = value.get("retrieved_at")
    catalog_digest = value.get("catalog_sha256")
    try:
        parsed_retrieved_at = datetime.fromisoformat(
            str(retrieved_at).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise WiReportError("OpenRouter price timestamp is invalid") from exc
    if (
        not isinstance(retrieved_at, str)
        or parsed_retrieved_at.tzinfo is None
        or parsed_retrieved_at.utcoffset() is None
        or parsed_retrieved_at.astimezone(timezone.utc) != parsed_retrieved_at
        or not retrieved_at.endswith("Z")
        or not isinstance(catalog_digest, str)
        or len(catalog_digest) != 64
        or any(character not in "0123456789abcdef" for character in catalog_digest)
    ):
        raise WiReportError("OpenRouter price timestamp or catalog digest is invalid")
    partitions = value.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise WiReportError("WI valuation partitions are unavailable")
    total = Decimal(0)
    for index, partition in enumerate(partitions):
        partition_fields = {
            "source_model", "openrouter_model", "canonical_slug", "tier",
            "request_count", "max_request_input_tokens", "tokens",
            "price_usd_per_token", "components_usd", "total_usd",
        }
        if (
            not isinstance(partition, Mapping)
            or set(partition) != partition_fields
            or partition.get("tier") != "standard"
        ):
            raise WiReportError("WI valuation selected a non-standard price tier")
        if any(
            not isinstance(partition.get(field), str) or not partition.get(field)
            for field in ("source_model", "openrouter_model", "canonical_slug")
        ):
            raise WiReportError("WI valuation model identity is invalid")
        for count_field in ("request_count", "max_request_input_tokens"):
            count = partition.get(count_field)
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < (1 if count_field == "request_count" else 0)
            ):
                raise WiReportError("WI valuation request evidence is invalid")
        tokens = partition.get("tokens")
        rates = partition.get("price_usd_per_token")
        components = partition.get("components_usd")
        names = {"uncached_input", "cache_read", "cache_write", "output"}
        if (
            not isinstance(tokens, Mapping) or set(tokens) != names
            or not isinstance(rates, Mapping) or set(rates) != names
            or not isinstance(components, Mapping) or set(components) != names
        ):
            raise WiReportError("WI valuation component shape is invalid")
        subtotal = Decimal(0)
        for name in names:
            count = tokens[name]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise WiReportError("WI valuation Token count is invalid")
            rate = _decimal(rates[name], f"valuation[{index}].{name}.rate")
            component = _decimal(
                components[name], f"valuation[{index}].{name}.component"
            )
            if Decimal(count) * rate != component:
                raise WiReportError("WI valuation component arithmetic does not conserve")
            subtotal += component
        if subtotal != _decimal(partition.get("total_usd"), "partition total"):
            raise WiReportError("WI valuation partition total does not conserve")
        total += subtotal
    if total != _decimal(value.get("total_usd"), "valuation total"):
        raise WiReportError("WI valuation total does not conserve")
    digest = value.get("valuation_sha256")
    expected = sha256(_canonical({
        key: item for key, item in value.items() if key != "valuation_sha256"
    })).hexdigest()
    if not isinstance(digest, str) or digest != expected:
        raise WiReportError("OpenRouter machine-cash valuation digest is invalid")


def _model_tokens(rows: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    keys = ("input", "cache_read", "cache_write", "output")
    result = {}
    for model, raw in rows.items():
        if not isinstance(model, str) or not isinstance(raw, Mapping):
            raise WiReportError("usage model partition is invalid")
        counts = {key: raw.get(key) for key in keys}
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts.values()):
            raise WiReportError("usage model Token counts are invalid")
        try:
            InputTokenClasses.from_inclusive(counts["input"], counts["cache_read"], counts["cache_write"])
        except TokenSemanticsError as exc:
            raise WiReportError("usage model cache exceeds input") from exc
        result[model] = counts
    return result


def _validate_valuation_usage(value: Mapping[str, Any], cohort: Mapping[str, Any]) -> None:
    try:
        source_digest = usage_evidence_sha256(cohort)
    except WiPricingError as exc:
        raise WiReportError(str(exc)) from exc
    if value["usage_evidence_sha256"] != source_digest:
        raise WiReportError("WI valuation does not bind the current usage evidence")
    raw_models = cohort.get("by_model")
    if not isinstance(raw_models, Mapping):
        raise WiReportError("WI valuation has no source model partitions")
    expected = _model_tokens(raw_models)
    actual = {}
    for partition in value["partitions"]:
        model = partition["source_model"]
        if model in actual:
            raise WiReportError("WI valuation repeats a model partition")
        counts = partition["tokens"]
        inputs = InputTokenClasses.from_disjoint(
            counts["uncached_input"], counts["cache_read"], counts["cache_write"])
        actual[model] = {
            "input": inputs.require_total(),
            "cache_read": counts["cache_read"], "cache_write": counts["cache_write"],
            "output": counts["output"],
        }
    if actual != expected:
        raise WiReportError("WI valuation model Token quantities do not match usage")


def _validate_retrospective_price(
    value: Mapping[str, Any],
    *,
    work_item_id: str,
    candidates: list[dict[str, Any]],
) -> None:
    expected_fields = {
        "schema", "work_item_id", "status", "canonical_attribution",
        "canonical_total_eligible", "may_include_unrelated_turns",
        "noncanonical_total", "source_candidate_sha256", "window",
        "classification", "valuation", "reason", "estimate_sha256",
    }
    if set(value) != expected_fields or (
        value.get("schema") != "computecost_retrospective_openrouter_estimate/v1"
        or value.get("work_item_id") != work_item_id
        or value.get("status") not in {
            "calculated", "token_quantity_unavailable", "producer_unreachable",
            "join_conflict",
        }
        or value.get("canonical_attribution") is not False
        or value.get("canonical_total_eligible") is not False
        or value.get("may_include_unrelated_turns") is not True
        or value.get("noncanonical_total") is not True
        or value.get("window") != "created_to_terminal"
        or value.get("classification")
        != "pi_only_estimate_may_include_unrelated_turns"
    ):
        raise WiReportError("retrospective OpenRouter estimate contract is invalid")
    candidate_digests = {item["candidate_sha256"] for item in candidates}
    source_digest = value.get("source_candidate_sha256")
    if source_digest not in candidate_digests and not (
        value.get("status") == "join_conflict" and source_digest is None
    ):
        raise WiReportError("retrospective estimate does not bind its source candidate")
    if value.get("status") == "calculated":
        valuation = value.get("valuation")
        if not isinstance(valuation, Mapping) or value.get("reason") is not None:
            raise WiReportError("calculated retrospective estimate is incomplete")
        _validate_flat_openrouter_valuation(valuation, work_item_id=work_item_id)
        candidate = next(item for item in candidates if item["candidate_sha256"] == source_digest)
        try:
            source, _ = retrospective_pricing_cohort(work_item_id, {"retrospective_candidates": [candidate]})
        except WiPricingError as exc:
            raise WiReportError(str(exc)) from exc
        if source is None:
            raise WiReportError("retrospective valuation source quantities are unavailable")
        _validate_valuation_usage(valuation, source)
    elif value.get("valuation") != "unavailable" or not isinstance(value.get("reason"), str):
        raise WiReportError("unavailable retrospective estimate lacks a typed reason")
    digest = value.get("estimate_sha256")
    expected = sha256(_canonical({
        key: item for key, item in value.items() if key != "estimate_sha256"
    })).hexdigest()
    if not isinstance(digest, str) or digest != expected:
        raise WiReportError("retrospective OpenRouter estimate digest is invalid")



def _native_bounded_present(epoch: Mapping[str, Any]) -> bool:
    """Pi/CC epochs need their Harness-sealed bounded native usage; others need none."""
    runtime = epoch.get("runtime")
    if runtime == "pi":
        return isinstance(epoch.get("pi_usage"), Mapping)
    if runtime in {"cc", "cc_sdk"}:
        return isinstance(epoch.get("cc_usage"), Mapping)
    return True

def build_wi_report(
    ledger: AppServerUsageLedger | None,
    work_item_id: str,
    *,
    usage_cohort: Mapping[str, Any] | None = None,
    openrouter_reference_cost: Mapping[str, Any] | None = None,
    retrospective_openrouter_estimate: Mapping[str, Any] | None = None,
    account_snapshot: Mapping[str, Any] | None = None,
    pi_snapshot: Mapping[str, Any] | None = None,
    measurement_snapshot: Mapping[str, Any] | None = None,
    scl_measurement_snapshot: Mapping[str, Any] | None = None,
    thread_usage_snapshot: Mapping[str, Any] | None = None,
    review_snapshot: Mapping[str, Any] | None = None,
    business_value_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an unknown-preserving report; only source seals count as usage."""

    if not isinstance(work_item_id, str) or not work_item_id.strip():
        raise WiReportError("work_item_id must be a non-empty string")
    work_item_id = work_item_id.strip()
    try:
        facts = [] if ledger is None else [
            fact
            for fact in ledger.read()
            if fact["context"]["work_item_id"] == work_item_id
        ]
    except AppServerUsageError as exc:
        raise WiReportError(str(exc)) from exc

    by_context: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        context_ref = fact["context"]["context_ref"]
        by_context.setdefault(context_ref, []).append(fact)

    total = _zero()
    partitions: dict[str, dict[str, Any]] = {}
    sealed_epochs: list[dict[str, Any]] = []
    open_epochs: list[dict[str, Any]] = []
    evidence_digests: list[str] = []

    for context_ref, context_facts in sorted(by_context.items()):
        baselines = [fact for fact in context_facts if fact["kind"] == "bind_baseline"]
        seals = [fact for fact in context_facts if fact["kind"] == "source_seal"]
        if len(baselines) != 1:
            raise WiReportError(
                f"epoch context {context_ref} must have exactly one baseline"
            )
        context = baselines[0]["context"]
        summary = {
            "context_ref": context_ref,
            "phase": context["phase"],
            "epoch": context["epoch"],
            "runtime": context["runtime"],
            "model": context["model"],
            "reasoning_effort": context["reasoning_effort"],
            "procurement_source": context["procurement_source"],
        }
        if not seals:
            open_epochs.append(summary)
            continue
        if len(seals) != 1:
            raise WiReportError(
                f"epoch context {context_ref} has multiple source seals"
            )
        seal = seals[0]
        epoch_tokens = UsageWatermark.from_dict(
            seal["epoch_delta"], "source_seal.epoch_delta"
        ).to_dict()
        _add(total, epoch_tokens)
        key = _partition_key(context)
        partition = partitions.setdefault(
            key,
            {
                "runtime": context["runtime"],
                "model": context["model"],
                "procurement_source": context["procurement_source"],
                "tokens": _zero(),
                "sealed_epoch_count": 0,
            },
        )
        _add(partition["tokens"], epoch_tokens)
        partition["sealed_epoch_count"] += 1
        sealed_epochs.append(
            {
                **summary,
                "tokens": epoch_tokens,
                "source_seal_fact_id": seal["fact_id"],
            }
        )
        evidence_digests.append(seal["content_sha256"])

    partition_list = [partitions[key] for key in sorted(partitions)]
    if _sum_partitions(partition_list) != total:
        raise WiReportError("runtime/model/procurement partitions do not conserve")

    discovered_usage: object = "not_applicable"
    discovery_status = "not_applicable"
    discovery_digest: str | None = None
    discovered_tokens: dict[str, int] | None = None
    retrospective_candidates: list[dict[str, Any]] = []
    retrospective_status = "unavailable"
    if usage_cohort is not None:
        if usage_cohort.get("schema") != "computecost_wi_usage_cohort/v1":
            raise WiReportError("usage cohort schema is unsupported")
        if usage_cohort.get("work_item_id") != work_item_id:
            raise WiReportError("usage cohort belongs to another work item")
        raw_status = usage_cohort.get("status")
        if raw_status not in {
            "observed", "not_applicable", "producer_missing",
            "producer_unreachable", "stale", "join_conflict", "partial",
            "retrospective_unsealed",
        }:
            raise WiReportError("usage cohort coverage status is invalid")
        raw_tokens = usage_cohort.get("tokens")
        raw_event_count = usage_cohort.get("event_count")
        if (
            isinstance(raw_event_count, bool)
            or not isinstance(raw_event_count, int)
            or raw_event_count < 0
        ):
            raise WiReportError("usage cohort event count is invalid")
        has_observed_tokens = raw_status == "observed" or (
            raw_status == "partial" and raw_event_count > 0
        )
        if raw_status == "observed" and raw_event_count == 0:
            raise WiReportError("observed usage cohort has no events")
        if has_observed_tokens:
            if not isinstance(raw_tokens, Mapping):
                raise WiReportError("observed usage cohort requires token totals")
            required = {
                "input", "cache_read", "cache_write", "net_new_input",
                "output", "reasoning", "total_resource_tokens",
            }
            if set(raw_tokens) != required or any(
                isinstance(raw_tokens[key], bool)
                or not isinstance(raw_tokens[key], int)
                or raw_tokens[key] < 0
                for key in required
            ):
                raise WiReportError("usage cohort token totals are invalid")
            try:
                uncached = InputTokenClasses.from_inclusive(
                    raw_tokens["input"], raw_tokens["cache_read"], raw_tokens["cache_write"]).require_uncached()
            except TokenSemanticsError as exc:
                raise WiReportError("usage cohort input/cache totals do not conserve") from exc
            if uncached != raw_tokens["net_new_input"]:
                raise WiReportError("usage cohort input/cache totals do not conserve")
            if raw_tokens["input"] + raw_tokens["output"] != raw_tokens["total_resource_tokens"]:
                raise WiReportError("usage cohort resource totals do not conserve")
            discovered_tokens = {
                "input_tokens": raw_tokens["input"],
                "cached_input_tokens": raw_tokens["cache_read"],
                "cache_write_input_tokens": raw_tokens["cache_write"],
                "output_tokens": raw_tokens["output"],
                "reasoning_output_tokens": raw_tokens["reasoning"],
                "total_tokens": raw_tokens["total_resource_tokens"],
            }
        elif raw_tokens != "unavailable":
            raise WiReportError("unobserved usage cohort must preserve Token unavailability")
        raw_retrospective = usage_cohort.get("retrospective_candidates", [])
        if not isinstance(raw_retrospective, list) or len(raw_retrospective) > 1:
            raise WiReportError("usage cohort retrospective candidates are invalid")
        try:
            retrospective_candidates = [
                validate_retrospective_candidate(
                    candidate, expected_work_item_id=work_item_id
                )
                for candidate in raw_retrospective
            ]
        except RetrospectiveUsageError as exc:
            raise WiReportError(str(exc)) from exc
        if retrospective_candidates:
            retrospective_status = str(retrospective_candidates[0]["status"])
        discovered_usage = {
            "status": raw_status,
            "event_count": usage_cohort.get("event_count"),
            "root_thread_count": usage_cohort.get("root_thread_count"),
            "tokens": raw_tokens,
            "quality_flags": usage_cohort.get("quality_flags", {}),
            "producer_attempts": usage_cohort.get("producer_attempts"),
            "diagnostics": usage_cohort.get("diagnostics"),
            "retrospective_candidate_count": len(retrospective_candidates),
        }
        discovery_status = str(raw_status)
        digest = usage_cohort.get("cohort_sha256")
        expected_digest = sha256(_canonical({
            key: value for key, value in usage_cohort.items() if key != "cohort_sha256"
        })).hexdigest()
        if not isinstance(digest, str) or digest != expected_digest:
            raise WiReportError("usage cohort digest is invalid")
        discovery_digest = digest

    producer_conflict = False
    model_partitions_available = True
    if discovered_tokens is not None:
        raw_models = usage_cohort.get("by_model") if usage_cohort is not None else None
        if not isinstance(raw_models, Mapping):
            raise WiReportError("usage cohort model partitions are unavailable")
        discovery_models = _model_tokens(raw_models)
        model_partitions_available = bool(discovery_models)
        mapped_fields = {"input": "input_tokens", "cache_read": "cached_input_tokens",
                         "cache_write": "cache_write_input_tokens", "output": "output_tokens"}
        if model_partitions_available and any(sum(row[key] for row in discovery_models.values()) != discovered_tokens[field]
               for key, field in mapped_fields.items()):
            raise WiReportError("usage model partitions do not conserve cohort totals")
        if sealed_epochs:
            ledger_models: dict[str, dict[str, int]] = {}
            for partition in partition_list:
                row = ledger_models.setdefault(str(partition["model"]), {key: 0 for key in mapped_fields})
                for key, field in mapped_fields.items():
                    row[key] += partition["tokens"][field]
            producer_conflict = total != discovered_tokens or (model_partitions_available and ledger_models != discovery_models)
    discovery_incomplete = (discovery_status not in {"observed", "not_applicable"}
                            or not model_partitions_available)
    if sealed_epochs and discovered_tokens is not None:
        usage_status = "partial" if (producer_conflict or open_epochs or discovery_incomplete) else "exact"
    elif sealed_epochs:
        usage_status = "partial" if open_epochs or discovery_incomplete else "exact"
    elif discovered_tokens is not None:
        total = discovered_tokens
        usage_status = "partial" if open_epochs or discovery_incomplete else "exact"
    else:
        usage_status = "unavailable"
    warnings: list[str] = []
    if open_epochs:
        warnings.append("unsealed_epoch_excluded")
    if producer_conflict:
        warnings.append("producer_join_conflict")
    if discovery_incomplete:
        warnings.append("usage_producer_coverage_incomplete")
    if not model_partitions_available:
        warnings.append("usage_model_partitions_unavailable")
    if ledger is not None and not facts:
        warnings.append("no_app_server_usage_evidence")
    retrospective_available = retrospective_status in {"available", "partial"}
    if not facts and discovered_tokens is None:
        warnings.append(
            "no_canonical_usage_producer"
            if retrospective_available else "no_joinable_usage_producer"
        )
    if retrospective_available:
        warnings.extend((
            "retrospective_unsealed_candidate_excluded_from_canonical_usage",
            "created_to_terminal_estimate_may_include_unrelated_turns",
            "lifecycle_active_estimate_may_omit_verification_work",
        ))
        candidate_diagnostics = retrospective_candidates[0].get("diagnostics", [])
        if "historical_source_permissions_privacy_defect" in candidate_diagnostics:
            warnings.append("historical_source_permissions_privacy_defect")
        if "pi_relay_metering_divergence" in candidate_diagnostics:
            warnings.append("retrospective_cross_source_divergence")
    if usage_cohort is not None:
        quality_flags = usage_cohort.get("quality_flags", {})
        if isinstance(quality_flags, Mapping) and quality_flags.get("incomplete_task"):
            warnings.append("incomplete_task_usage_included")
        discovery_diagnostics = usage_cohort.get("diagnostics")
        candidate_evidence = (
            discovery_diagnostics.get("candidate_evidence")
            if isinstance(discovery_diagnostics, Mapping) else None
        )
        if (
            isinstance(candidate_evidence, Mapping)
            and candidate_evidence.get("status") == "present_but_unpartitioned"
        ):
            warnings.append("unpartitioned_candidate_evidence_excluded")

    pi_usage: object = "unavailable"
    pi_coverage = "unavailable"
    pi_digest: str | None = None
    if usage_cohort is not None:
        attempts = usage_cohort.get("producer_attempts")
        if isinstance(attempts, list):
            pi_attempts = [
                attempt for attempt in attempts
                if isinstance(attempt, Mapping) and attempt.get("producer") == "pi_managed_scl"
            ]
            if len(pi_attempts) == 1:
                pi_coverage = str(pi_attempts[0].get("status"))
                pi_usage = {
                    "status": pi_coverage,
                    "management": (
                        "managed_measured" if pi_coverage in {"observed", "partial"}
                        else "unmanaged_or_not_expected" if pi_coverage == "not_applicable"
                        else "managed_measurement_unavailable"
                    ),
                    "producer_attempt": pi_attempts[0],
                }
    if pi_snapshot is not None:
        try:
            validate_pi_session_snapshot(pi_snapshot)
        except PiUsageError as exc:
            raise WiReportError(str(exc)) from exc
        if pi_snapshot.get("work_item_id") != work_item_id:
            raise WiReportError("Pi usage snapshot belongs to another work item")
        summary = pi_snapshot.get("summary")
        if not isinstance(summary, Mapping):
            raise WiReportError("Pi usage snapshot summary must be an object")
        lifecycle = str(pi_snapshot["lifecycle_state"])
        bounded = pi_snapshot.get("schema") == "pi_session_usage_snapshot/v2"
        pi_usage = {
            "status": "bounded_uncommitted_diagnostic" if bounded else "unbounded_session_diagnostic",
            "lifecycle_state": lifecycle,
            "phase": pi_snapshot.get("phase"),
            "epoch": pi_snapshot.get("epoch"),
            "procurement_source": pi_snapshot.get("procurement_source"),
            "totals": summary.get("totals"),
            "event_count": summary.get("event_count"),
            "by_provider_model_effort": summary.get("by_provider_model_effort"),
            "source_prefix_sha256": summary.get("source_prefix_sha256"),
            "cost_eligible": False,
        }
        pi_coverage = "diagnostic_bounded_uncommitted" if bounded else "diagnostic_unbounded"
        pi_digest = str(pi_snapshot["snapshot_sha256"])
        if "provider_estimated_cost_usd" in summary:
            warnings.append("pi_native_price_discarded_non_authoritative")
        warnings.append(
            "explicit_pi_snapshot_not_canonical_wi_cost"
            if bounded else "unbounded_pi_snapshot_not_wi_cost"
        )
        if lifecycle == "open":
            warnings.append("open_pi_epoch_not_final_cost")

    quota_pressure: object = "unavailable"
    quota_coverage = "unavailable"
    account_digest: str | None = None
    if account_snapshot is not None:
        try:
            validate_app_server_account_snapshot(account_snapshot)
        except AppServerAccountError as exc:
            raise WiReportError(str(exc)) from exc
        rate_limits = account_snapshot.get("rate_limits")
        if not isinstance(rate_limits, Mapping):
            raise WiReportError("account snapshot rate_limits must be an object")
        quota_pressure = {
            "scope": "account_not_work_item",
            "plan_type": rate_limits.get("plan_type"),
            "primary": rate_limits.get("primary"),
            "secondary": rate_limits.get("secondary"),
            "credits": rate_limits.get("credits"),
            "individual_limit": rate_limits.get("individual_limit"),
            "spend_control_reached": rate_limits.get("spend_control_reached"),
        }
        quota_coverage = "observed_account_context"
        account_digest = str(account_snapshot["snapshot_sha256"])

    outcome_quality: object = "unavailable"
    downstream_effect: object = "unavailable"
    efficiency: object = "unavailable"
    measurement_cost: object = "unavailable"
    prospective_harness_attention: Mapping[str, Any] | None = None
    prospective_scl_attention: Mapping[str, Any] | None = None
    measurement_coverage = "unavailable"
    measurement_digest: str | None = None
    measurement_source_refs: list[str] = []
    machine_cash_usd: object = "unavailable"
    partial_machine_cash_usd: object = "unavailable"
    cash_basis = CASH_BASIS
    cash_coverage: object = "unavailable"
    provider_native_cash_usd: object = "unavailable"
    measurement: Mapping[str, Any] | None = None
    measurement_eligibility: object = "unavailable"
    if measurement_snapshot is not None:
        try:
            measurement = validate_harness_measurement_snapshot(
                measurement_snapshot, expected_work_item_id=work_item_id
            )
        except HarnessMeasurementError as exc:
            raise WiReportError(str(exc)) from exc
        measurement_eligibility = measurement["measurement_eligibility"]
        quality = measurement["quality"]
        outcome_quality = {
            "delivery_status": quality["delivery_status"],
            "human_review": {
                "final_status": quality["final_review_status"],
                "first_pass": quality["first_pass_review"],
                "rework_rounds": quality["review_rework_rounds"],
            },
            "post_approval_corrections": quality["post_approval_corrections"],
        }
        if quality["effect_status"] != "unknown":
            downstream_effect = quality["effect_status"]
        efficiency = measurement["efficiency"]
        measurement_cost = measurement["cost"]["token_hard_cost"]
        measured_attention = measurement["cost"]["human_attention"]
        if measured_attention != "unknown":
            prospective_harness_attention = {
                "raw_turn_count": measured_attention["raw_turn_count"],
                "human_attention_units": measured_attention[
                    "human_attention_units"
                ],
                "effort_band_counts": measured_attention["effort_band_counts"],
                "focused_seconds": "unavailable",
                "focused_seconds_coverage": "unavailable",
                "focused_seconds_unknown_event_count": measured_attention[
                    "raw_turn_count"
                ],
                "contract_version": measured_attention["contract_version"],
            }
        measurement_coverage = measurement_join_coverage(measurement)
        measurement_digest = measurement["snapshot_sha256"]
        measurement_source_refs = measurement["safe_source_refs"]
        if measurement["normalized_actual_cash_usd"] is not None:
            # Harness/provider native cash is carried as non-authoritative
            # context. It never overrides the WI machine-cash policy: it does
            # not populate machine_cash_usd, cash coverage, or cash basis.
            provider_native_cash_usd = measurement["normalized_actual_cash_usd"]
            warnings.append("provider_native_cash_not_wi_cash_authority")
        if measurement_coverage != "exact":
            warnings.append("harness_measurement_partial")
        if measurement["evidence"]["authority_gaps"]:
            warnings.append("harness_measurement_authority_gap")

    scl_measurement: object = "unavailable"
    scl_measurement_coverage = "unavailable"
    scl_measurement_digest: str | None = None
    scl_has_sealed_usage = False
    if scl_measurement_snapshot is not None:
        try:
            joined = validate_scl_measurement_snapshot(
                scl_measurement_snapshot, expected_work_item_id=work_item_id
            )
        except SclMeasurementError as exc:
            raise WiReportError(str(exc)) from exc
        scl_measurement = {
            "status": joined["status"],
            "eligible_epoch_count": joined["eligible_epoch_count"],
            "epochs": joined["epochs"],
            "human_attention": joined["human_attention"],
            "missing_producers": joined["missing_producers"],
        }
        scl_measurement_coverage = str(joined["status"])
        scl_measurement_digest = str(joined["snapshot_sha256"])
        joined_epochs = [
            epoch for epoch in joined.get("epochs", []) if isinstance(epoch, Mapping)
        ]
        scl_has_sealed_usage = any(
            isinstance(epoch.get("usage"), Mapping) for epoch in joined_epochs
        )
        if usage_status == "exact" and any(
            not isinstance(epoch.get("usage"), Mapping)
            or not _native_bounded_present(epoch)
            for epoch in joined_epochs
        ):
            usage_status = "partial"
            warnings.append("expected_scl_usage_incomplete")
        scl_pi_epochs = [
            epoch for epoch in joined_epochs if isinstance(epoch.get("pi_usage"), Mapping)
        ]
        if scl_pi_epochs:
            native_totals = {
                name: sum(
                    int(epoch["pi_usage"]["summary"]["totals"][name])
                    for epoch in scl_pi_epochs
                )
                for name in (
                    "input", "cache_read", "cache_write", "output",
                    "reasoning", "total_tokens",
                )
            }
            pi_usage = {
                "status": "exact_bounded_sealed",
                "management": "managed_measured",
                "cost_eligible": True,
                "epoch_count": len(scl_pi_epochs),
                "event_count": sum(
                    int(epoch["pi_usage"]["summary"]["event_count"])
                    for epoch in scl_pi_epochs
                ),
                "totals": native_totals,
                "epochs": [epoch["pi_usage"] for epoch in scl_pi_epochs],
            }
            pi_coverage = "exact_bounded_sealed"
        warnings.extend(str(item) for item in joined.get("warnings", []))
        if joined["status"] != "complete":
            warnings.append("scl_measurement_partial")
        if isinstance(joined["human_attention"], Mapping):
            prospective_scl_attention = joined["human_attention"]

    if retrospective_available:
        retrospective = retrospective_candidates[0]
        canonical_pi = pi_usage
        pi_usage = {
            "status": "retrospective_unsealed_candidate",
            "management": "historical_registered_unsealed",
            "canonical_measurement": canonical_pi,
            "candidate_sha256": retrospective["candidate_sha256"],
            "candidate_status": retrospective["status"],
            "canonical_usage_eligible": False,
            "token_seal_eligible": False,
            "cost_eligible": False,
            "roi_eligible": False,
        }
        pi_coverage = "retrospective_unsealed"

    retrospective_attention = (
        retrospective_candidates[0].get("human_attention")
        if retrospective_candidates
        and isinstance(retrospective_candidates[0].get("human_attention"), Mapping)
        else None
    )
    human_attention = build_attention_vector(
        prospective_scl=prospective_scl_attention,
        prospective_harness=prospective_harness_attention,
        retrospective=retrospective_attention,
    )
    human_attention_coverage = str(human_attention["status"])

    terminal_measurement = {
        "status": "unavailable",
        "eligibility": measurement_eligibility,
        "attribution": "unavailable",
        "final_token_seal": "unavailable",
    }
    if isinstance(measurement_eligibility, Mapping):
        eligible_value = measurement_eligibility.get("eligible")
        joined_epochs = [
            epoch for epoch in (joined.get("epochs", []) if scl_measurement_snapshot is not None else [])
            if isinstance(epoch, Mapping)
        ]
        has_attribution = bool(joined_epochs)
        final_complete = bool(joined_epochs) and all(
            isinstance(epoch.get("usage"), Mapping)
            and _native_bounded_present(epoch)
            for epoch in joined_epochs
        )
        terminal_measurement = {
            "status": (
                "complete" if eligible_value is True and has_attribution and final_complete
                else "incomplete" if eligible_value is True
                else "ineligible" if eligible_value is False
                else "unavailable"
            ),
            "eligibility": measurement_eligibility,
            "attribution": "observed" if has_attribution else "unavailable",
            "final_token_seal": "observed" if final_complete else "unavailable",
        }
        if eligible_value is True and not (has_attribution and final_complete):
            warnings.append("roi_eligible_measurement_incomplete")
        elif eligible_value is False:
            warnings.append("measurement_ineligible")

    artifacts: object = "unavailable"
    artifact_coverage = "unavailable"
    review_digest: str | None = None
    review_evidence_refs: list[str] = []
    review: Mapping[str, Any] | None = None
    if review_snapshot is not None:
        try:
            review = validate_harness_review_snapshot(
                review_snapshot, expected_work_item_id=work_item_id
            )
        except DeliveryValueError as exc:
            raise WiReportError(str(exc)) from exc
        if measurement is not None and measurement["quality"][
            "final_review_status"
        ] != review["decision"]:
            raise WiReportError("Harness measurement and exact review disagree")
        artifacts = {
            "status": (
                "exact_approved_set"
                if review["decision"] == "approved"
                else "exact_changes_requested_set"
            ),
            "artifact_set_sha256": review["artifact_set_sha256"],
            "candidate_fingerprint": review["candidate_fingerprint"],
            "acceptance_fingerprint": review["acceptance_fingerprint"],
            "repo_heads": review["repo_heads"],
            "reviewed_at": review["reviewed_at"],
        }
        if review.get("outcome_waiver") is not None:
            artifacts.update(outcome_decision="human_waiver",
                captured_outcome_status=review["outcome_waiver"]["outcome_status"])
        artifact_coverage = (
            "exact_approved_set"
            if review["decision"] == "approved"
            else "exact_changes_requested_set"
        )
        review_digest = str(review["snapshot_sha256"])
        review_evidence_refs = list(review["evidence_refs"])

    business_value: object = "unavailable"
    business_value_coverage = "unavailable"
    business_value_digest: str | None = None
    business_value_evidence_refs: list[str] = []
    if business_value_receipt is not None:
        if review is None or review["decision"] != "approved":
            raise WiReportError(
                "business value requires an exact approved artifact review"
            )
        if measurement is not None and measurement["quality"][
            "delivery_status"
        ] != "accepted":
            raise WiReportError(
                "business value requires accepted Harness delivery status"
            )
        try:
            value_receipt = validate_business_value_receipt(
                business_value_receipt,
                expected_work_item_id=work_item_id,
                expected_artifact_set_sha256=str(review["artifact_set_sha256"]),
            )
        except DeliveryValueError as exc:
            raise WiReportError(str(exc)) from exc
        business_value = {
            "status": "consumer_verified",
            "outcome_ref": value_receipt["outcome_ref"],
            "consumer": value_receipt["consumer"],
            "measurement": value_receipt["measurement"],
            "observed_at": value_receipt["observed_at"],
        }
        business_value_coverage = "consumer_verified"
        business_value_digest = str(value_receipt["receipt_sha256"])
        business_value_evidence_refs = [
            item["ref"] for item in value_receipt["evidence"]
        ]

    reference_credits: object = "unavailable"
    rate_card_coverage = "unavailable"
    thread_usage_digest: str | None = None
    openrouter_cost: object = "not_applicable"
    openrouter_valuation_digest: str | None = None
    retrospective_openrouter_cost: object = "not_applicable"
    retrospective_valuation_digest: str | None = None
    retrospective_rate_coverage = "not_applicable"
    retrospective_machine_cash_usd: object = "unavailable"
    if openrouter_reference_cost is not None:
        if openrouter_reference_cost.get("schema") != "computecost_openrouter_cash_valuation/v2":
            raise WiReportError("WI machine-cash valuation schema is unsupported")
        if openrouter_reference_cost.get("work_item_id") != work_item_id:
            raise WiReportError("WI machine-cash valuation belongs to another work item")
        price_status = openrouter_reference_cost.get("status")
        if price_status not in {"calculated", "producer_unreachable", "join_conflict"}:
            raise WiReportError("WI machine-cash valuation status is invalid")
        openrouter_cost = dict(openrouter_reference_cost)
        digest = openrouter_reference_cost.get("valuation_sha256")
        if price_status == "calculated":
            _validate_flat_openrouter_valuation(
                openrouter_reference_cost, work_item_id=work_item_id
            )
            if usage_cohort is None or discovered_tokens is None:
                raise WiReportError("WI valuation requires its current attributed usage cohort")
            _validate_valuation_usage(openrouter_reference_cost, usage_cohort)
            openrouter_valuation_digest = str(digest)
            rate_card_coverage = CASH_COVERAGE_CALCULATED
            if producer_conflict:
                cash_coverage = "join_conflict"
            elif usage_status != "exact":
                # A known subtotal is useful, but is not the canonical WI total.
                partial_machine_cash_usd = str(openrouter_reference_cost["total_usd"])
                cash_coverage = "partial_usage"
            else:
                machine_cash_usd = str(openrouter_reference_cost["total_usd"])
                cash_coverage = CASH_COVERAGE_CALCULATED
        else:
            rate_card_coverage = str(price_status)
            cash_coverage = str(price_status)
    if retrospective_openrouter_estimate is not None:
        _validate_retrospective_price(
            retrospective_openrouter_estimate,
            work_item_id=work_item_id,
            candidates=retrospective_candidates,
        )
        retrospective_openrouter_cost = dict(retrospective_openrouter_estimate)
        retrospective_valuation_digest = str(
            retrospective_openrouter_estimate["estimate_sha256"]
        )
        retrospective_rate_coverage = str(
            retrospective_openrouter_estimate["status"]
        )
        if retrospective_rate_coverage == "calculated":
            retrospective_machine_cash_usd = str(
                retrospective_openrouter_estimate["valuation"]["total_usd"]
            )
            warnings.append(
                "retrospective_pi_price_estimate_excluded_from_canonical_total"
            )
    if thread_usage_snapshot is not None:
        try:
            validate_app_server_thread_usage_snapshot(thread_usage_snapshot)
        except AppServerThreadUsageError as exc:
            raise WiReportError(str(exc)) from exc
        if thread_usage_snapshot.get("work_item_id") != work_item_id:
            raise WiReportError("App Server thread usage belongs to another work item")
        reference_credits = {
            "status": "estimated",
            "basis": "app_server_thread_billing_route",
            "estimated_usage_credits_micros": thread_usage_snapshot[
                "estimated_usage_credits_micros"
            ],
            "groups": thread_usage_snapshot["groups"],
        }
        thread_usage_digest = str(thread_usage_snapshot["snapshot_sha256"])
        warnings.append("app_server_thread_price_excluded_non_openrouter")

    report: dict[str, Any] = {
        "schema": WI_REPORT_SCHEMA,
        "work_item_id": work_item_id,
        "usage": {
            "status": "partial" if usage_status == "unavailable" and scl_has_sealed_usage else usage_status,
            "producer": (
                "reconciled_app_server_and_usage_event"
                if sealed_epochs and discovered_tokens is not None and total == discovered_tokens
                else "openai_app_server" if sealed_epochs
                else "usage_event_cohort" if discovered_tokens is not None
                else "none_joined"
            ),
            "tokens": total if (sealed_epochs or discovered_tokens is not None) else "unavailable",
            "sealed_epochs": sealed_epochs,
            "open_epochs": open_epochs,
            "by_runtime_model_procurement": partition_list,
            "usage_event_cohort": discovered_usage,
            "pi_native": pi_usage,
            "retrospective_candidates": retrospective_candidates,
            "harness_scl": scl_measurement,
            "conservation": "partial" if producer_conflict else "pass" if (sealed_epochs or discovered_tokens is not None) else "partial" if scl_has_sealed_usage else "unavailable",
        },
        "cost": {
            "token_resource": "observed" if (sealed_epochs or discovered_tokens is not None) else "partial" if scl_has_sealed_usage else "unavailable",
            "reference_credits": reference_credits,
            "machine_pricing_authority": "openrouter.models",
            "openrouter_cash_valuation": openrouter_cost,
            "machine_cash_usd": machine_cash_usd,
            "partial_machine_cash_usd": partial_machine_cash_usd,
            "cash_basis": cash_basis,
            "cash_coverage": cash_coverage,
            "provider_native_cash_usd": provider_native_cash_usd,
            "retrospective_cash_estimate": retrospective_openrouter_cost,
            "retrospective_machine_cash_usd": retrospective_machine_cash_usd,
            "retrospective_cash_coverage": retrospective_rate_coverage,
            "subscription_settlement": "excluded_from_wi_report_and_roi",
            "harness_measurement": measurement_cost,
            "human_attention": human_attention,
            "quota_pressure": quota_pressure,
        },
        "lifecycle": (
            retrospective_candidates[0]["lifecycle"]
            if retrospective_candidates else "unavailable"
        ),
        "outcome": {
            "artifacts": artifacts,
            "quality": outcome_quality,
            "downstream_effect": downstream_effect,
            "business_value": business_value,
            "efficiency": efficiency,
            "measurement_readiness": terminal_measurement,
        },
        "coverage": {
            "app_server_usage": (
                "exact" if sealed_epochs and not open_epochs
                else "partial" if sealed_epochs or open_epochs
                else "not_applicable" if usage_cohort is not None
                else "producer_missing"
            ),
            "producer_discovery": discovery_status,
            "retrospective_usage": retrospective_status,
            "pi_usage": pi_coverage,
            "outcome_join": measurement_coverage,
            "terminal_measurement": terminal_measurement["status"],
            "scl_measurement": scl_measurement_coverage,
            "human_attention": human_attention_coverage,
            "artifacts": artifact_coverage,
            "business_value": business_value_coverage,
            "rate_card": rate_card_coverage,
            "retrospective_rate_card": retrospective_rate_coverage,
            "cash": cash_coverage,
            "quota": quota_coverage,
        },
        "evidence": {
            "source_seal_content_sha256": sorted(evidence_digests),
            "usage_cohort_sha256": discovery_digest,
            "retrospective_candidate_sha256": [
                candidate["candidate_sha256"] for candidate in retrospective_candidates
            ],
            "account_snapshot_sha256": account_digest,
            "pi_snapshot_sha256": pi_digest,
            "harness_measurement_snapshot_sha256": measurement_digest,
            "scl_measurement_snapshot_sha256": scl_measurement_digest,
            "harness_measurement_source_refs": measurement_source_refs,
            "app_server_thread_usage_snapshot_sha256": thread_usage_digest,
            "openrouter_valuation_sha256": openrouter_valuation_digest,
            "retrospective_openrouter_estimate_sha256": retrospective_valuation_digest,
            "harness_review_snapshot_sha256": review_digest,
            "harness_review_evidence_refs": review_evidence_refs,
            "business_value_receipt_sha256": business_value_digest,
            "business_value_evidence_refs": business_value_evidence_refs,
        },
        "warnings": sorted(warnings),
    }
    digest_input = {key: value for key, value in report.items() if key != "report_sha256"}
    report["report_sha256"] = sha256(_canonical(digest_input)).hexdigest()
    return report
