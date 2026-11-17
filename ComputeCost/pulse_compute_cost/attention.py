"""Unknown-preserving human-attention vectors for WI reports.

Elapsed lifecycle exposure, exact feedback events, HAU effort, and measured focus
are deliberately separate signals.  In particular, wall clock and Relay outer
turns never become focused time or human feedback.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from typing import Any, Mapping


ATTENTION_VECTOR_SCHEMA = "computecost_human_attention_vector/v1"
UNAVAILABLE = "unavailable"
CATEGORIES = (
    "human_touch",
    "review_approval",
    "correction",
    "re_explanation",
    "manual_retry",
    "rework",
)
EFFORT_BANDS = ("micro", "bounded", "judgment", "framing")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _density(value: object, elapsed_seconds: object) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, str, Decimal))
        or isinstance(elapsed_seconds, bool)
        or not isinstance(elapsed_seconds, int)
        or elapsed_seconds <= 0
    ):
        return UNAVAILABLE
    try:
        amount = Decimal(str(value))
    except Exception:
        return UNAVAILABLE
    return format(
        (amount * Decimal(86_400) / Decimal(elapsed_seconds)).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        ),
        "f",
    )


def _opaque_ref(domain: str, value: object) -> str:
    digest = sha256(domain.encode() + b"\0" + _canonical(value)).hexdigest()
    return f"attention_source_v1_{digest}"


def _unknown_counts(names: tuple[str, ...]) -> dict[str, str]:
    return {name: UNAVAILABLE for name in names}


def build_attention_vector(
    *,
    prospective_scl: Mapping[str, Any] | None = None,
    prospective_harness: Mapping[str, Any] | None = None,
    retrospective: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge typed attention evidence without summing overlapping projections.

    A native prospective SCL event set has precedence over the older Harness G0
    projection.  Retrospective lifecycle/review facts remain visible, but are
    not added to a prospective event set because their event identities are not
    guaranteed to share a deduplication namespace.
    """

    elapsed: int | str = UNAVAILABLE
    retrospective_status = UNAVAILABLE
    retrospective_review_count: int | str = UNAVAILABLE
    relay_turn_binding = UNAVAILABLE
    sources: list[str] = []
    if retrospective is not None:
        retrospective_status = str(retrospective.get("status", UNAVAILABLE))
        raw_elapsed = retrospective.get("cycle_elapsed_seconds")
        if isinstance(raw_elapsed, int) and not isinstance(raw_elapsed, bool) and raw_elapsed >= 0:
            elapsed = raw_elapsed
        raw_categories = retrospective.get("category_counts")
        if isinstance(raw_categories, Mapping):
            review_count = raw_categories.get("review_approval")
            if isinstance(review_count, int) and not isinstance(review_count, bool) and review_count >= 0:
                retrospective_review_count = review_count
        raw_binding = retrospective.get("relay_turn_binding")
        if isinstance(raw_binding, str):
            relay_turn_binding = raw_binding
        sources.append(_opaque_ref("retrospective", retrospective))

    selected: Mapping[str, Any] | None = None
    selected_kind: str | None = None
    suppressed: list[str] = []
    if prospective_scl is not None:
        selected = prospective_scl
        selected_kind = "prospective_scl_events"
        if prospective_harness is not None:
            suppressed.append("prospective_harness_projection")
    elif prospective_harness is not None:
        selected = prospective_harness
        selected_kind = "prospective_harness_projection"

    focused: int | str = UNAVAILABLE
    focused_coverage = UNAVAILABLE
    focused_unknown_count: int | str = UNAVAILABLE
    feedback_count: int | str = UNAVAILABLE
    categories: dict[str, int | str] = _unknown_counts(CATEGORIES)
    bands: dict[str, int | str] = _unknown_counts(EFFORT_BANDS)
    hau: str = UNAVAILABLE
    contract_version = "g0_hau_v1"
    if selected is not None:
        raw_count = selected.get("raw_turn_count")
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
            feedback_count = raw_count
        raw_categories = selected.get("category_counts")
        if isinstance(raw_categories, Mapping) and set(raw_categories) == set(CATEGORIES):
            if all(
                isinstance(raw_categories[name], int)
                and not isinstance(raw_categories[name], bool)
                and raw_categories[name] >= 0
                for name in CATEGORIES
            ):
                categories = {name: int(raw_categories[name]) for name in CATEGORIES}
        elif isinstance(retrospective_review_count, int):
            categories["review_approval"] = retrospective_review_count
        raw_bands = selected.get("effort_band_counts")
        if isinstance(raw_bands, Mapping) and set(raw_bands) == set(EFFORT_BANDS):
            if all(
                isinstance(raw_bands[name], int)
                and not isinstance(raw_bands[name], bool)
                and raw_bands[name] >= 0
                for name in EFFORT_BANDS
            ):
                bands = {name: int(raw_bands[name]) for name in EFFORT_BANDS}
        raw_hau = selected.get("human_attention_units")
        if isinstance(raw_hau, (str, int, Decimal)) and not isinstance(raw_hau, bool):
            hau = format(Decimal(str(raw_hau)), "f")
            if "." in hau:
                hau = hau.rstrip("0").rstrip(".")
            hau = hau or "0"
        raw_focused = selected.get("focused_seconds")
        if isinstance(raw_focused, int) and not isinstance(raw_focused, bool) and raw_focused >= 0:
            focused = raw_focused
        raw_focused_coverage = selected.get("focused_seconds_coverage")
        if raw_focused_coverage in {"observed", "partial", UNAVAILABLE}:
            focused_coverage = str(raw_focused_coverage)
        raw_unknown = selected.get("focused_seconds_unknown_event_count")
        if isinstance(raw_unknown, int) and not isinstance(raw_unknown, bool) and raw_unknown >= 0:
            focused_unknown_count = raw_unknown
        sources.append(_opaque_ref(selected_kind or "prospective", selected))
    elif isinstance(retrospective_review_count, int):
        categories["review_approval"] = retrospective_review_count

    precedence: list[str] = []
    if selected_kind is not None:
        precedence.append(selected_kind)
    if retrospective is not None:
        precedence.append("retrospective_lifecycle_review_subset")
    known = (
        isinstance(elapsed, int)
        or isinstance(feedback_count, int)
        or any(isinstance(value, int) for value in categories.values())
        or isinstance(focused, int)
        or hau != UNAVAILABLE
    )
    status = (
        "observed"
        if selected is not None and isinstance(feedback_count, int)
        else "partial"
        if known
        else UNAVAILABLE
    )
    elapsed_days = (
        format(
            (Decimal(elapsed) / Decimal(86_400)).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            ),
            "f",
        )
        if isinstance(elapsed, int)
        else UNAVAILABLE
    )
    vector: dict[str, Any] = {
        "schema": ATTENTION_VECTOR_SCHEMA,
        "status": status,
        "focused_seconds": focused,
        "focused_seconds_coverage": focused_coverage,
        "focused_seconds_unknown_event_count": focused_unknown_count,
        "cycle_elapsed_seconds": elapsed,
        "cycle_elapsed_classification": "attention_exposure_not_focused_time",
        "elapsed_days": elapsed_days,
        "exact_human_feedback_count": feedback_count,
        # Backward-readable name; it has the same exactness as the feedback count.
        "raw_turn_count": feedback_count,
        "category_counts": categories,
        "effort_band_counts": bands,
        "human_attention_units": hau,
        "contract_version": contract_version,
        "density_per_elapsed_day": {
            "feedback_count": _density(feedback_count, elapsed),
            "human_attention_units": _density(hau, elapsed),
        },
        "source_precedence": precedence,
        "suppressed_overlapping_sources": suppressed,
        "deduplication": "prospective_event_set_precedes_retrospective_overlapping_facts",
        "evidence": {
            "retrospective_status": retrospective_status,
            "retrospective_review_approval_count": retrospective_review_count,
            "relay_turn_binding": relay_turn_binding,
            "source_refs": sorted(set(sources)),
        },
    }
    vector["vector_sha256"] = sha256(_canonical(vector)).hexdigest()
    return vector
