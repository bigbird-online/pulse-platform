"""Stage semantics in the existing receipt ledger, not a second lifecycle.

Only Agent declarations are admitted here. Host authentication and native
verification/acceptance remain producer responsibilities, never payload flags.
This module neither stores facts nor prices usage; query_collaboration calls its
read projections at the same receipt watermark as all other measurements.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Mapping
import re

from .events import parse_timestamp

_COMMON = {"wi", "context_ref", "activity_refs", "epistemic"}
STAGE_FIELDS = {
    "stage_goal": _COMMON | {"name", "desired_change", "expected", "input_scope", "unknowns"},
    "stage_output": _COMMON | {"object_ref", "version_ref", "name", "category", "unit",
        "comparison_basis", "contribution", "extent", "quantity"},
    "stage_feedback": _COMMON | {"object_ref", "version_ref", "reason", "change", "category"},
    "stage_participation": _COMMON | {"purpose", "secondary_purposes", "basis", "active_seconds",
        "estimator", "configuration_ref", "unobserved_scope"},
    "stage_link": {"wi", "context_ref", "target_refs", "state", "basis", "epistemic"},
}
SEMANTIC_KINDS = frozenset(STAGE_FIELDS) - {"stage_link"}
LINKABLE_KINDS = SEMANTIC_KINDS | {"usage", "human"}
PURPOSES = {"co_creation", "reading", "acceptance", "coordination", "correction", "recovery"}
# Prevent accidental secrets in short semantic summaries. This is not a claim
# of general DLP; raw transcripts/paths are never permitted as stage inputs.
_PRIVATE = re.compile(r"(?:https?://[^\s/@]+:[^\s/@]+@|github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]{16,})")


def _text(value, field, maximum=1024):
    from .receipts import ReceiptError
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 for c in value) or _PRIVATE.search(value)):
        raise ReceiptError(f"invalid bounded stage {field}")


def _refs(values, field):
    from .receipts import ReceiptError, _ref
    if not isinstance(values, list) or not 1 <= len(values) <= 256:
        raise ReceiptError(f"stage {field} needs a bounded nonempty reference list")
    for value in values:
        _ref(value, field, False)
    if len(set(values)) != len(values):
        raise ReceiptError(f"stage {field} contains duplicates")


def _texts(values, field):
    from .receipts import ReceiptError
    if not isinstance(values, list) or len(values) > 32:
        raise ReceiptError(f"invalid stage {field}")
    for value in values:
        _text(value, field)


def validate_stage_payload(kind, p):
    from .receipts import ReceiptError, _ref, decimal
    _ref(p["context_ref"], "context_ref", False)
    if p["epistemic"] != "agent_judgment":
        raise ReceiptError("stage declaration cannot assert observation or human confirmation")
    if kind == "stage_link":
        _refs(p["target_refs"], "target_refs")
        _text(p["basis"], "basis")
        if p["state"] not in ("assigned", "shared", "unknown", "conflict"):
            raise ReceiptError("invalid stage membership state")
        if (p["state"] == "assigned") != (p["wi"] is not None):
            raise ReceiptError("only an assigned stage link identifies a WI")
        return
    if p["wi"] is not None:
        raise ReceiptError("stage WI membership must use bounded links")
    _refs(p["activity_refs"], "activity_refs")
    if kind == "stage_goal":
        for field in ("name", "desired_change", "input_scope"):
            _text(p[field], field, 256 if field == "name" else 1024)
        _texts(p["unknowns"], "unknowns")
        expected = p["expected"]
        if not isinstance(expected, list) or not 1 <= len(expected) <= 32:
            raise ReceiptError("stage goal needs bounded expectations")
        for row in expected:
            if not isinstance(row, Mapping) or set(row) != {
                    "category", "unit", "comparison_basis", "quantity", "baseline", "criterion"}:
                raise ReceiptError("invalid stage expectation")
            for field in ("category", "unit", "comparison_basis", "criterion"):
                _text(row[field], field, 512)
            for field in ("quantity", "baseline"):
                if row[field] is not None:
                    decimal(row[field], field, nonnegative=True)
    if kind in {"stage_output", "stage_feedback"}:
        _ref(p["object_ref"], "object_ref", False)
        _ref(p["version_ref"], "version_ref", False)
    if kind == "stage_output":
        for field in ("name", "category", "unit", "comparison_basis", "extent"):
            _text(p[field], field, 1024 if field == "extent" else 256)
        if p["contribution"] not in ("new", "improved", "reused"):
            raise ReceiptError("invalid stage output contribution")
        if p["quantity"] is not None:
            decimal(p["quantity"], "quantity", nonnegative=True)
    if kind == "stage_feedback":
        _text(p["reason"], "reason")
        _text(p["change"], "change")
        if p["category"] not in ("correction", "goal_change", "assessment", "stop"):
            raise ReceiptError("stage feedback cannot mint a verification or acceptance")
    if kind == "stage_participation":
        if not isinstance(p["purpose"], str) or p["purpose"] not in PURPOSES:
            raise ReceiptError("invalid participation purpose")
        secondary = p["secondary_purposes"]
        if (not isinstance(secondary, list) or any(not isinstance(s, str) or s not in PURPOSES for s in secondary)
                or len(set(secondary)) != len(secondary) or p["purpose"] in secondary):
            raise ReceiptError("invalid secondary purposes")
        _text(p["basis"], "basis")
        _texts(p["unobserved_scope"], "unobserved_scope")
        times = p["active_seconds"]
        if not isinstance(times, Mapping) or set(times) != {"measured", "self_reported", "estimated"}:
            raise ReceiptError("participation time evidence must stay separate")
        # The semantic producer can estimate, not fabricate observed human time.
        if times["measured"] is not None or times["self_reported"] is not None:
            raise ReceiptError("human time observations require native source evidence")
        bounds = times["estimated"]
        if bounds is not None:
            if (not isinstance(bounds, list) or len(bounds) != 2
                    or any(type(n) is not int or n < 0 for n in bounds) or bounds[0] > bounds[1]):
                raise ReceiptError("invalid active-time estimate bounds")
            _text(p["estimator"], "estimator", 128)
            _ref(p["configuration_ref"], "configuration_ref", False)
        elif p["estimator"] is not None or p["configuration_ref"] is not None:
            raise ReceiptError("abstention does not carry an estimator result")


def project_membership(rows, conflicts):
    """Attach bounded memberships in memory, never rewriting input identities.

    Disagreement with an existing runtime assignment is a conflict, not an
    implicit reassign. Missing targets remain diagnostic references until the
    original collector saves them. Links cannot select whole sessions/epochs.
    """
    from .receipts import Receipt, canonical
    claims = defaultdict(list)
    link_conflicts = set()
    for row in rows:
        if row.kind == "stage_link":
            for target in row.payload["target_refs"]:
                claims[target].append(row)
    for versions in conflicts.values():
        for row in versions:
            if row.kind == "stage_link":
                for target in row.payload["target_refs"]:
                    claims[target].append(row)
                    link_conflicts.add(target)
    diagnostics = {"conflicts": {}, "shared_refs": [], "unknown_refs": [], "unresolved_refs": [],
                   "candidates": {ref: sorted({r.payload["wi"] for r in group if r.payload["wi"]})
                                  for ref, group in sorted(claims.items())},
                   "contexts": {ref: sorted({r.payload["context_ref"] for r in group})
                                for ref, group in sorted(claims.items())},
                   "link_refs": sorted({r.fact_ref for group in claims.values() for r in group})}
    projected = []
    found = set()
    for row in rows:
        links = claims.get(row.entity_ref, [])
        if not links:
            projected.append(row)
            continue
        found.add(row.entity_ref)
        p = row.payload
        choices = {(r.payload["context_ref"], r.payload["wi"], r.payload["state"]) for r in links}
        candidate_wis = {r.payload["wi"] for r in links if r.payload["wi"]}
        if p.get("wi"):
            candidate_wis.add(p["wi"])
        bad = (row.entity_ref in link_conflicts or row.kind not in LINKABLE_KINDS or len(choices) != 1
               or (row.kind == "usage" and p.get("granularity") != "request"))
        if not bad:
            context, wi, state = next(iter(choices))
            bad = (state == "conflict" or (p.get("context_ref") is not None and p["context_ref"] != context)
                   or (p.get("wi") is not None and p["wi"] != wi))
        if bad:
            diagnostics["conflicts"][row.entity_ref] = sorted(candidate_wis)
            # A bad link must not rewrite lifecycle/control objects at all.
            if row.kind in LINKABLE_KINDS:
                p = {**p, "wi": "ambiguous"}
        elif state == "assigned":
            p = {**p, "wi": wi}
        else:
            diagnostics["shared_refs" if state == "shared" else "unknown_refs"].append(row.entity_ref)
        projected.append(row if p == row.payload else Receipt(
            row.kind, row.entity_ref, row.fact_ref, row.occurred_at, row.observed_at,
            row.producer, row.evidence, row.supersedes, canonical(p)))
    diagnostics["unresolved_refs"] = sorted(set(claims) - found)
    for field in ("shared_refs", "unknown_refs"):
        diagnostics[field].sort()
    return projected, diagnostics


def _item(row):
    return {**row.payload, "entity_ref": row.entity_ref, "fact_ref": row.fact_ref,
            "occurred_at": row.occurred_at, "observed_at": row.observed_at,
            "producer": row.producer, "evidence": list(row.evidence)}


def _output_groups(rows):
    from .receipts import decimal, decimal_text
    buckets = defaultdict(lambda: defaultdict(list))
    for row in rows:
        p = row.payload
        key = (p["category"], p["unit"], p["comparison_basis"], p["contribution"])
        buckets[key][p["object_ref"]].append(row)
    result = []
    for key, objects in sorted(buckets.items()):
        amounts, unresolved = [], []
        for obj, contributions in sorted(objects.items()):
            values = {None if row.payload["quantity"] is None else decimal(row.payload["quantity"], "quantity")
                      for row in contributions}
            if len(values) == 1 and None not in values:
                amounts.append(next(iter(values)))
            else:
                # No evidence of disjoint extent: never sum per-version numbers.
                unresolved.append(obj)
        result.append(dict(zip(("category", "unit", "comparison_basis", "contribution"), key)) | {
            "object_count": len(objects), "object_refs": sorted(objects),
            "quantity": decimal_text(sum(amounts, Decimal(0))) if not unresolved else None,
            "known_quantity": decimal_text(sum(amounts, Decimal(0))) if amounts else None,
            "unresolved_object_refs": unresolved, "epistemic": "calculated_from_agent_judgments",
            "fact_refs": sorted(r.fact_ref for group in objects.values() for r in group)})
    return result


def context_selection(rows, conflicts, membership, context_ref):
    """Exact context membership only; no WI/session/execution expansion.

    Reconciliation must run BEFORE this selection. A suppressed request is not
    permission to substitute its entire aggregate. Shared/disputed quantities
    remain in the global inventory, not an invented allocation to this context.
    """
    related = {ref for ref, contexts in membership["contexts"].items() if context_ref in contexts}
    related.update(r.entity_ref for r in [*rows, *(r for rs in conflicts.values() for r in rs)]
                   if r.kind in STAGE_FIELDS and r.payload["context_ref"] == context_ref)
    selected = set()
    for row in rows:
        p = row.payload
        if row.kind in STAGE_FIELDS:
            if p["context_ref"] == context_ref:
                selected.add(row.entity_ref)
        elif (row.kind in {"usage", "human"} and row.entity_ref in related
              and membership["contexts"].get(row.entity_ref) == [context_ref]
              and row.entity_ref not in membership["conflicts"]
              and row.entity_ref not in membership["shared_refs"]
              and (row.kind != "usage" or p["granularity"] == "request")):
            selected.add(row.entity_ref)
    return selected, related


def stage_report(rows, conflicts, membership, *, work_items=(), window=None, unsupported_filters=(),
                 context_ref=None, history=()):
    """A bounded stage section of the SAME query, not a parallel cost report.

    Cumulative means observed through the selected window's end and receipt
    watermark; period formations are a separate set, never added to cumulative.
    Semantic declarations alone never prove complete output/activity inventory.
    """
    def relevant(row):
        return (row.kind in SEMANTIC_KINDS
                and (context_ref is None or row.payload["context_ref"] == context_ref)
                and (not work_items or row.payload.get("wi") in work_items)
                and (window is None or row.occurred_at is None
                     or parse_timestamp(row.occurred_at, "stage time") < window[1]))

    def in_period(row):
        return window is None or (row.occurred_at is not None
            and window[0] <= parse_timestamp(row.occurred_at, "stage time") < window[1])

    selected = [] if unsupported_filters else sorted((r for r in rows if relevant(r)),
        key=lambda r: (r.occurred_at or "", r.entity_ref, r.fact_ref))
    outputs = [r for r in selected if r.kind == "stage_output"]
    participations = [r for r in selected if r.kind == "stage_participation" and in_period(r)]
    feedback = [r for r in selected if r.kind == "stage_feedback"]
    related = None if context_ref is None else context_selection(rows, conflicts, membership, context_ref)[1]
    def related_ref(ref):
        return related is None or ref in related
    relevant_conflicts = {ref: wis for ref, wis in membership["conflicts"].items()
                          if related_ref(ref) and (not work_items or set(wis) & set(work_items))}
    revision_conflicts = sorted(ref for ref, versions in conflicts.items() if related_ref(ref) and any(
        r.kind in SEMANTIC_KINDS and (relevant(r) or
            (set(membership["candidates"].get(ref, [])) & set(work_items))) for r in versions))
    # History is evidence, never an additional current contribution. Select by
    # exact entity so a pre-WI predecessor remains available after a late link.
    entities = {r.entity_ref for r in selected} | set(revision_conflicts)
    revisions = defaultdict(list)
    for row in history:
        if (row.kind in SEMANTIC_KINDS and row.entity_ref in entities
                and (context_ref is None or row.payload["context_ref"] == context_ref)
                and (window is None or row.occurred_at is None
                     or parse_timestamp(row.occurred_at, "stage time") < window[1])):
            revisions[row.entity_ref].append(row)
    current = {r.fact_ref for r in selected}
    revision_history = sorted(
        ({**_item(r), "kind": r.kind, "supersedes": r.supersedes, "current": r.fact_ref in current}
         for group in revisions.values() if len(group) > 1 for r in group),
        key=lambda item: (item["observed_at"], item["entity_ref"], item["fact_ref"]))
    from .native_output_assessment import project_assessments, accepted_versions
    assessments = project_assessments(rows, outputs, window=window)
    return {
        "assessments": assessments,
        "status": "unsupported_filter" if unsupported_filters else "conflict" if relevant_conflicts or revision_conflicts else
                  "recorded_subset" if selected else "unavailable",
        "coverage": "recorded_declarations_only", "unsupported_filters": list(unsupported_filters),
        "execution_authority": False,
        "goals": [_item(r) for r in selected if r.kind == "stage_goal"],
        "outputs": {"items": [_item(r) for r in outputs],
                    "formed": _output_groups([r for r in outputs if in_period(r)]),
                    "cumulative": {"object_count": len({r.payload["object_ref"] for r in outputs}),
                                   "groups": _output_groups(outputs)},
                    "accepted": accepted_versions(assessments, outputs)},
        "feedback": [_item(r) for r in feedback],
        "participation": {"items": [_item(r) for r in participations],
                          "activity_count": len({a for r in participations for a in r.payload["activity_refs"]}),
                          "total_active_seconds": None,
                          "reason": "estimates_not_additive_without_overlap_and_coverage_evidence"},
        "membership_conflicts": sorted(relevant_conflicts),
        "shared_refs": [r for r in membership["shared_refs"] if related_ref(r)] if not work_items else [],
        "unknown_refs": [r for r in membership["unknown_refs"] if related_ref(r)] if not work_items else [],
        "unresolved_link_targets": [r for r in membership["unresolved_refs"] if related_ref(r)
            and (not work_items or set(membership["candidates"].get(r, [])) & set(work_items))],
        "revision_conflicts": revision_conflicts,
        "revision_history": revision_history if not unsupported_filters else [],
        "time_unallocated_refs": sorted(r.fact_ref for r in selected if r.occurred_at is None),
    }
