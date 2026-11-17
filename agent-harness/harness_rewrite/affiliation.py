"""Business affiliation decisions shared by every replacement-core entry.

This module interprets recorded facts; it neither reads a repository nor invokes
the old startup controller. Registry candidates never constitute human consent.
"""

from __future__ import annotations

from datetime import datetime
import re

from .model import Decision, HostContext, Json, Rejected, digest, fields, human_decision

INTENTS = frozenset({"record_affiliation", "revise_affiliation"})


class Invalid(ValueError):
    """Why the recorded affiliation cannot be read; carried to the caller as detail."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def evaluate(
    record: Json, policy: Json | None, admission: Json | None, *, default_eligible: bool = False
) -> Json:
    result: Json = {
        "state": "admitted",
        "execution_authority": False,
        "reason": "no_affiliation_policy",
        "candidates": [],
    }
    if policy is None:
        return result
    try:
        required = policy.get("required_result_refs", [])
        areas = policy.get("registered_operating_area_refs", [])
        classes = policy.get("registered_operating_area_work_classes", [])
        candidates = [{"type": "current_result", "ref": r} for r in required]
        candidates += [{"type": "operating_area", "ref": r} for r in areas]
        result.update(candidates=candidates, work_classes=classes)
        wi = record["work_item_id"]
        if wi not in policy.get("allow_work_items", []):
            created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
            cutoff = datetime.fromisoformat(str(policy["legacy_cutoff"]).replace("Z", "+00:00"))
            if created.tzinfo is None or cutoff.tzinfo is None:
                raise Invalid("timestamp_without_timezone", "WI creation time or the policy legacy_cutoff has no timezone")
            if created < cutoff:
                return {
                    **result,
                    "state": "decision_required",
                    "reason": "legacy_epoch_quarantined",
                }
        default = policy.get("default_portfolio_ref")
        if not required and not areas and not default and admission is None:
            return result
        matches = set()
        source = record.get("source") or ""
        corpus = record.get("title", "") + "\n" + source
        result_override = policy.get("work_item_result_overrides", {}).get(wi)
        if result_override:
            matches.add(("current_result", result_override))
        else:
            matches.update(
                ("current_result", ref)
                for ref in required
                if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(ref) + r"(?![A-Za-z0-9_-])", corpus)
            )
        area_override = policy.get("work_item_operating_area_overrides", {}).get(wi)
        if area_override:
            matches.add(("operating_area", area_override))

        def values(name: str) -> list[str]:
            return re.findall(r"(?<![A-Za-z0-9_])" + name + r"\s*[:=]\s*([A-Za-z0-9_-]+)", source)

        area_values, class_values = (
            values("operating_area_ref"),
            values("operating_area_work_class"),
        )
        if len(area_values) > 1 or len(class_values) > 1:
            raise Invalid("source_names_several_areas", "The WI source names more than one operating_area_ref or operating_area_work_class")
        work_class = class_values[0].lower() if class_values else None
        if area_values:
            if area_values[0] not in areas or work_class not in classes:
                raise Invalid("source_area_unregistered", f"The WI source names {area_values[0]} with work class {work_class}, which the platform policy does not register together")
            matches.add(("operating_area", area_values[0]))
        elif work_class and not area_override:
            raise Invalid("source_work_class_without_area", "The WI source names an operating_area_work_class without an operating_area_ref")
        if admission is not None:
            if admission["work_item_id"] != wi:
                raise Invalid("decision_for_another_wi", "The recorded affiliation names another WI")
            kind, ref = admission["admission_ref_type"], admission["admission_ref"]
            if not registered(policy, kind, ref, admission.get("operating_area_work_class")):
                raise Invalid("decision_unregistered", f"The recorded affiliation {kind} {ref} (work class {admission.get('operating_area_work_class')}) is not registered in the platform policy")
            if "revised_from" in admission or "inherited_from" in admission:
                # An audited revision, or a child's copy of its parent's decision,
                # outranks title, source and registry overrides; what it outranks
                # stays visible instead of silently disappearing.
                superseded = [{"type": t, "ref": r} for t, r in sorted(matches - {(kind, ref)})]
                if work_class and work_class != admission.get("operating_area_work_class"):
                    superseded.append({"type": "operating_area_work_class", "ref": work_class})
                for key in ("revised_from", "inherited_from"):
                    if key in admission:
                        result[key] = admission[key]
                if superseded:
                    result["superseded"] = superseded
                matches = set()
            else:
                if work_class and work_class != admission.get("operating_area_work_class"):
                    raise Invalid("decision_conflicts_with_source_work_class", f"The recorded work class {admission.get('operating_area_work_class')} differs from {work_class} named in the WI source; use revise_affiliation to choose one")
                if matches - {(kind, ref)}:
                    raise Invalid("decision_conflicts_with_source", "The WI title or source names " + ", ".join(sorted(r for _, r in matches - {(kind, ref)})) + f", which conflicts with the recorded {ref}; use revise_affiliation to make the chosen affiliation explicit")
            matches.add((kind, ref))
        if not matches:
            if default_eligible and default is not None:
                if not isinstance(default, str) or not re.fullmatch(
                    r"PORTFOLIO-[A-Z0-9][A-Z0-9-]{1,31}", default
                ) or not isinstance(policy.get("current_epoch"), str) or not policy["current_epoch"]:
                    raise Invalid("default_portfolio_policy_invalid", "The platform policy default portfolio or current_epoch is malformed")
                return {
                    **result,
                    "reason": "policy_default",
                    "reference": {"type": "portfolio_default", "ref": default},
                }
            return {
                **result,
                "state": "decision_required",
                "reason": "business_affiliation_missing",
                "next_action": "review_existing_decisions_then_ask_only_undecided_affiliation",
            }
        if len(matches) != 1:
            return {
                **result,
                "state": "decision_required",
                "reason": "business_affiliation_ambiguous",
            }
        kind, ref = next(iter(matches))
        # The work class in force: a recorded decision's, else the source field's.
        # A sole OA match came from that source field or from a registry override.
        if admission is not None:
            effective = admission.get("operating_area_work_class")
        else:
            effective = work_class if kind == "operating_area" else None
        return {**result, "reason": "existing_affiliation", "reference": {"type": kind, "ref": ref},
                "work_class": effective}
    except Invalid as error:
        return {**result, "state": "unavailable", "reason": "affiliation_source_invalid",
                "detail": {"code": error.code, "message": str(error)}}
    except (KeyError, TypeError, ValueError):
        return {**result, "state": "unavailable", "reason": "affiliation_source_invalid",
                "detail": {"code": "malformed_record_or_policy",
                           "message": "The WI record or the platform affiliation policy is malformed"}}


def registered(policy: Json, kind: str, ref: str, work_class: str | None) -> bool:
    if kind == "current_result":
        return ref in policy.get("required_result_refs", []) and work_class is None
    if kind == "operating_area":
        return ref in policy.get("registered_operating_area_refs", []) and work_class in policy.get(
            "registered_operating_area_work_classes", []
        )
    return (
        kind == "portfolio_default"
        and ref == policy.get("default_portfolio_ref")
        and work_class is None
    )


def observation(state: Json, context: HostContext) -> Json | None:
    if context.admission_policy is None:
        return None
    record = state.get("production", {}).get("record", {})
    if not record:
        # New production items also use the same registry and creation provenance.
        record = {
            "work_item_id": context.observed_work_item,
            "title": state["definition"]["what"],
            "source": state["definition"]["why"],
            "created_at": state.get("created_at", ""),
        }
    return evaluate(
        record, context.admission_policy, state.get("affiliation"),
        default_eligible=(
            state["profile"] == "lightweight" and state["status"] == "draft"
            and state.get("holder") is None and not state.get("production")
        ),
    )


def default_events(state: Json, context: HostContext, wi: str, at: str) -> list[Json]:
    """Record a policy decision atomically with native creation or first start.

    Reads only derive eligibility. The original draft can resume after an upgrade;
    no synthetic human approval, historical adoption, or execution grant is made.
    """
    admission = observation(state, context)
    if admission is None or admission["reason"] != "policy_default":
        return []
    policy = context.admission_policy
    assert policy is not None
    return [{"kind": "AffiliationRecorded", "payload": {
        "record": {
            "schema_version": "work_item_admission/v1",
            "work_item_id": wi,
            "admission_ref_type": "portfolio_default",
            "admission_ref": admission["reference"]["ref"],
            "operating_area_work_class": None,
            "decided_by": "policy:" + policy["current_epoch"],
            "reason": "Native lightweight startup uses the configured default portfolio.",
            "decided_at": at,
        },
        "source": {"kind": "production_policy", "sha256": digest(policy)},
    }}]


def decide(state: Json, data: Json, context: HostContext, wi: str, at: str,
           intent: str = "record_affiliation") -> Decision:
    fields(data, {"type", "ref", "reason"}, {"work_class"})
    if context.admission_policy is None:
        raise Rejected("policy_unavailable", "The production registry is unavailable")
    if not registered(context.admission_policy, data["type"], data["ref"], data.get("work_class")):
        raise Rejected("invalid_input", "Choose a registered business affiliation and work class")
    if context.actor != state["definition"]["owner"]:
        raise Rejected("authority_denied", "Only the responsible agent may record affiliation")
    if not isinstance(data["reason"], str) or not data["reason"].strip():
        raise Rejected("invalid_input", "The actual decision rationale is required")
    source = human_decision(context, intent, data, wi)
    record = {
        "schema_version": "work_item_admission/v1",
        "work_item_id": wi,
        "admission_ref_type": data["type"],
        "admission_ref": data["ref"],
        "operating_area_work_class": data.get("work_class"),
        "decided_by": context.actor,
        "reason": data["reason"],
        "decided_at": at,
    }
    old = state.get("affiliation")
    same = old is not None and all(old.get(k) == record[k] for k in TARGET)
    if intent == "revise_affiliation":
        return revise(state, context, record, source)
    if old:
        if not same:
            raise Rejected("domain_conflict", "An existing decision cannot be silently replaced")
        return Decision([], {"already_recorded": True})
    return Decision(
        [{"kind": "AffiliationRecorded", "payload": {"record": record, "source": source}}],
        {"affiliation_recorded": True, "execution_authority": False},
    )


TARGET = ("admission_ref_type", "admission_ref", "operating_area_work_class")


def revise(state: Json, context: HostContext, record: Json, source: str) -> Decision:
    """Replace the effective affiliation with an audited human decision.

    The earlier decision and the effective observation it replaces are kept in
    the event; the journal remains the audit trail. A revision that would not
    change the admitted affiliation, including its work class, writes nothing.
    """
    before = observation(state, context)
    assert before is not None
    reference = {"type": record["admission_ref_type"], "ref": record["admission_ref"]}
    if before["state"] == "admitted" and before.get("reference") == reference and (
        before.get("work_class") == record["operating_area_work_class"]
    ):
        return Decision([], {"already_recorded": True})
    previous = state.get("affiliation")
    if previous is not None:
        record["revised_from"] = {
            "type": previous["admission_ref_type"], "ref": previous["admission_ref"],
            "work_class": previous.get("operating_area_work_class"),
        }
    else:
        observed = before.get("reference")
        record["revised_from"] = None if observed is None else {
            **observed, "work_class": before.get("work_class")}
    prior = {k: before[k] for k in ("state", "reason", "reference", "work_class", "superseded")
             if k in before}
    return Decision(
        [{"kind": "AffiliationRevised", "payload": {
            "record": record, "previous": previous, "previous_observation": prior,
            "source": source,
        }}],
        {"affiliation_revised": True, "execution_authority": False},
    )
