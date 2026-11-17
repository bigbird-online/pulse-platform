"""Presentation of existing WI delivery/Outcome facts, not a new outcome ledger.

Review activity, model calls and artifact revisions are never output counts.
All references here are the original opaque receipt references; absent prose is
not reconstructed from private prompts, file names, or the current WorkItem.
"""
from __future__ import annotations
from datetime import datetime
from typing import Any
from .events import parse_timestamp
from .receipts import Receipt


def outcomes_for_period(deliveries: list[Receipt], reviews: list[Receipt], *,
                        window: tuple[datetime, datetime] | None,
                        coverage: list[Receipt], wis: set[str],
                        conflicts: list[Receipt] = ()) -> dict[str, Any]:
    # Callers already selected current revisions and WI membership. Deduplicate
    # by stable delivery identity, not candidate/artifact or review identity.
    selected: dict[str, Receipt] = {}
    undated = []
    for row in deliveries:
        timestamp = row.payload.get("accepted_at") or row.occurred_at
        if window is not None:
            if timestamp is None:
                undated.append(row.fact_ref)
                continue
            if not window[0] <= parse_timestamp(timestamp, "outcome time") < window[1]:
                continue
        selected[row.entity_ref] = row
    def affects_selected_outcome(row: Receipt) -> bool:
        p = row.payload
        if p.get("wi") not in wis:
            return False
        if row.kind == "delivery":
            timestamp = p.get("accepted_at") or row.occurred_at
            return window is None or timestamp is None or window[0] <= parse_timestamp(timestamp, "outcome time") < window[1]
        if row.kind == "review":
            delivery = selected.get(p["delivery_ref"])
            return delivery is not None and p["candidate_ref"] == delivery.payload["candidate_ref"]
        return False
    relevant_conflicts = [r for r in conflicts if affects_selected_outcome(r)]
    covered = {r.payload["wi"] for r in coverage if r.payload["status"] == "complete"}
    complete = bool(wis) and wis <= covered and all(r.payload["status"] == "complete" for r in coverage)
    items = []
    for row in sorted(selected.values(), key=lambda r: (r.payload.get("accepted_at") or r.occurred_at or "", r.entity_ref)):
        p = row.payload
        matched = [r for r in reviews if r.payload["delivery_ref"] == row.entity_ref
                   and r.payload["candidate_ref"] == p["candidate_ref"]]
        review_conflicts = [r for r in relevant_conflicts
                            if r.kind == "review" and r.payload["delivery_ref"] == row.entity_ref]
        decisions = sorted({r.payload["decision"] for r in matched + review_conflicts})
        # A review-only import deliberately has status=unknown: its existence
        # does not prove a delivered Outcome. Preserve it as uncounted context.
        items.append({"outcome_ref": row.entity_ref, "fact_ref": row.fact_ref,
            "wi": p["wi"], "responsible": p.get("responsible"),
            "status": p["status"], "counted": p["status"] != "unknown",
            "occurred_at": p.get("accepted_at") or row.occurred_at,
            "candidate_ref": p["candidate_ref"], "artifact_ref": p["artifact_ref"],
            "acceptance_ref": p["acceptance_ref"],
            "review_decisions": decisions,
            "review_refs": sorted({r.fact_ref for r in matched + review_conflicts}),
            "evidence_refs": sorted(set(row.evidence)),
            "title": p.get("outcome_context", {}).get("title"), "summary": p.get("outcome_context", {}).get("summary"),
            # Who accepted it: a child WI closed by its parent holder's review is
            # `self_with_controls`, never shown as a human acceptance.
            "verification_level": p.get("outcome_context", {}).get("verification_level")})
    known = sum(item["counted"] for item in items)
    unknown = sum(not item["counted"] for item in items)
    if relevant_conflicts:
        status, count = "conflict", None
    elif undated or unknown:
        status, count = ("partial", known) if known else ("unavailable", None)
    elif complete:
        status, count = "complete", known
    elif known:
        status, count = "observed", known
    else:
        status, count = "unavailable", None
    return {"status": status, "count": count, "known_count": known,
            "metric_scope": "wi_outcomes_in_period", "items": items,
            "unallocated_refs": sorted(set(undated)),
            "conflict_refs": sorted({r.fact_ref for r in relevant_conflicts}),
            "coverage": "complete" if complete else "observed_subset" if known else "unknown"}


def outcome_references(result: dict[str, Any]) -> dict[str, Any]:
    """Rows reference the summary's objects; never duplicate their full bodies."""
    return {key: result[key] for key in ("status", "count", "known_count")} | {
        "refs": [item["outcome_ref"] for item in result["items"]],
        "metric_scope": "wi_reference_not_runtime_allocation"}
