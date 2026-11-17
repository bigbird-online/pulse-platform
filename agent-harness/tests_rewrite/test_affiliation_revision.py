"""An owner revises a recorded or derived affiliation only with an audited human decision."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from harness_rewrite import Command, Query, SourceInput
from harness_rewrite.model import digest
from tests_rewrite.test_default_affiliation import DEFAULT_POLICY, create, publish_policy, state
from tests_rewrite.test_production_ledger import Rig, git
from tests_rewrite.test_workflow import DEFINITION

POLICY = {**DEFAULT_POLICY, "required_result_refs": ["CR-ONE", "CR-TWO"],
          "registered_operating_area_work_classes": ["improvement", "operations"]}
TARGET = {"type": "current_result", "ref": "CR-TWO", "reason": "Human moved this WI to CR-TWO"}


def revise(rig, wi, data, request=None, human=True):
    rig.context = replace(rig.context, source=SourceInput(
        "native-confirmation", "native_human", b"Confirmed affiliation revision",
        "revise_affiliation",
        digest({"intent": "revise_affiliation", "work_item_id": wi, "input": data}),
    ) if human else SourceInput("native-request", "native_agent", b"Agent wants a new area"))
    return rig.kernel.execute(Command(request or "revise-" + wi, "revise_affiliation", data, wi), "native")


def make(rig, why, request):
    """A native lightweight WI per case, so cases share one ledger fixture."""
    rig.context = replace(rig.context, source=SourceInput(
        "native-request", "native_agent", b"Bounded native request"))
    result = rig.kernel.execute(
        Command(request, "create_lightweight", {**DEFINITION, "why": why}), "native")
    assert result["status"] == "local_committed", result
    return result["work_item"]["id"]


def observed(rig, wi):
    return rig.kernel.inspect(Query(wi), "native")["result"]["current_observation"]["affiliation"]


def revised_events(rig, wi):
    return [e["payload"] for h in state(rig, wi)[1] for e in h["events"]
            if e["kind"] == "AffiliationRevised"]


def test_revision_replaces_a_recorded_default_and_keeps_the_audit(tmp_path):
    rig = Rig(tmp_path)
    publish_policy(rig, POLICY)
    wi = create(rig)
    original = state(rig, wi)[0]["affiliation"]
    assert original["admission_ref"] == "PORTFOLIO-DEFAULT"
    record = dict(TARGET)
    rig.context = replace(rig.context, source=SourceInput(
        "native-confirmation", "native_human", b"Confirmed", "record_affiliation",
        digest({"intent": "record_affiliation", "work_item_id": wi, "input": record})))
    refused = rig.kernel.execute(Command("record", "record_affiliation", record, wi), "native")
    assert refused["error"]["code"] == "domain_conflict"
    result = revise(rig, wi, TARGET)
    assert result["status"] == "local_committed", result
    current = state(rig, wi)[0]["affiliation"]
    assert (current["admission_ref_type"], current["admission_ref"]) == ("current_result", "CR-TWO")
    assert current["revised_from"] == {
        "type": "portfolio_default", "ref": "PORTFOLIO-DEFAULT", "work_class": None}
    view = observed(rig, wi)
    assert view["state"] == "admitted" and view["reference"] == {"type": "current_result", "ref": "CR-TWO"}
    assert view["revised_from"]["ref"] == "PORTFOLIO-DEFAULT" and "superseded" not in view
    [event] = revised_events(rig, wi)
    assert event["previous"] == original
    assert event["previous_observation"]["reference"] == {
        "type": "portfolio_default", "ref": "PORTFOLIO-DEFAULT"}
    assert event["record"]["reason"] == TARGET["reason"]
    assert event["record"]["decided_by"] == rig.context.actor
    assert event["source"] == rig.context.source.sha256
    again = revise(rig, wi, TARGET, request="again")
    assert again["status"] == "local_committed" and len(revised_events(rig, wi)) == 1
    assert state(rig, wi)[1][-1]["events"] == []
    peer = rig.client(rig.backend(rig.b))
    assert peer.execute(Command("start", "start_work", {}, wi), "native")["status"] == "local_committed"
    # A revision must stay registered; deregistering its target fails closed.
    publish_policy(rig, {**POLICY, "required_result_refs": ["CR-ONE"]})
    assert observed(rig, wi)["state"] == "unavailable"


OUTRANKED = [
    ("Both CR-ONE and CR-TWO are named", [{"type": "current_result", "ref": "CR-ONE"}]),
    ("operating_area_ref=OA-PULSE; operating_area_work_class=improvement",
     [{"type": "operating_area", "ref": "OA-PULSE"},
      {"type": "operating_area_work_class", "ref": "improvement"}]),
]


def test_revision_outranks_source_and_records_what_it_superseded(tmp_path):
    rig = Rig(tmp_path)
    publish_policy(rig, POLICY)
    for n, (why, superseded) in enumerate(OUTRANKED):
        outranks(rig, make(rig, why, f"create-{n}"), superseded)


def outranks(rig, wi, superseded):
    before = observed(rig, wi)
    assert revise(rig, wi, TARGET)["status"] == "local_committed"
    view = observed(rig, wi)
    assert view["state"] == "admitted", view
    assert view["reference"] == {"type": "current_result", "ref": "CR-TWO"}
    assert view["superseded"] == superseded
    [event] = revised_events(rig, wi)
    assert event["previous"] is None
    assert event["previous_observation"]["state"] == before["state"]
    assert state(rig, wi)[0]["affiliation"]["revised_from"] == (
        None if "reference" not in before
        else {**before["reference"], "work_class": before.get("work_class")})


REFUSALS = [
    ({**TARGET, "ref": "CR-UNKNOWN"}, None, True, "invalid_input"),
    ({**TARGET, "type": "operating_area", "ref": "OA-PULSE"}, None, True, "invalid_input"),
    (TARGET, "outsider", True, "authority_denied"),
    (TARGET, None, False, "source_invalid"),
    ({**TARGET, "reason": " "}, None, True, "invalid_input"),
]


def test_revision_refusals_write_nothing(tmp_path):
    # Refusals write nothing, so one ledger serves every case.
    rig = Rig(tmp_path)
    publish_policy(rig, POLICY)
    wi = create(rig)
    owner = rig.context
    before = git(rig.remote, "rev-parse", "main")
    for n, (data, actor, human, code) in enumerate(REFUSALS):
        rig.context = replace(owner, actor=actor) if actor else owner
        result = revise(rig, wi, data, request=f"refused-{n}", human=human)
        assert result["status"] == "rejected" and result["error"]["code"] == code, (n, result)
    assert git(rig.remote, "rev-parse", "main") == before


MOVED = [
    # A native draft holding only the policy default (wi_20261015_938's shape).
    (DEFINITION["why"], {"type": "portfolio_default", "ref": "PORTFOLIO-DEFAULT", "work_class": None}),
    # No recorded decision; prose names a CR (wi_20261015_422's shape).
    ("Receives the CR-ONE agent-harness work",
     {"type": "current_result", "ref": "CR-ONE", "work_class": None}),
]


def test_revision_moves_default_or_prose_cr_to_an_operating_area(tmp_path):
    rig = Rig(tmp_path)
    publish_policy(rig, POLICY)
    for n, (why, previous) in enumerate(MOVED):
        moves(rig, make(rig, why, f"create-{n}"), previous, f"start-{n}")


def moves(rig, wi, previous, start):
    assert observed(rig, wi)["reference"] == {"type": previous["type"], "ref": previous["ref"]}
    area = {"type": "operating_area", "ref": "OA-PULSE", "work_class": "improvement",
            "reason": "Human moved this WI to the platform operating area"}
    assert revise(rig, wi, area)["status"] == "local_committed"
    current = state(rig, wi)[0]["affiliation"]
    assert current["revised_from"] == previous
    assert current["operating_area_work_class"] == "improvement"
    view = observed(rig, wi)
    assert view["state"] == "admitted"
    assert view["reference"] == {"type": "operating_area", "ref": "OA-PULSE"}
    assert view.get("superseded", []) == (
        [] if previous["type"] == "portfolio_default"
        else [{"type": "current_result", "ref": "CR-ONE"}])
    started = rig.kernel.execute(Command(start, "start_work", {}, wi), "native")
    assert started["status"] == "local_committed", started


def test_the_released_reader_refuses_revision_events(tmp_path):
    """The kernel on main before revisions, not a patched function, reads the events."""
    root = Path(__file__).resolve().parents[2]
    old = tmp_path / "released"
    old.mkdir()
    try:
        base = "ce8ed39a41e71217de13832a845504ec4a768742"
        archive = subprocess.run(["git", "-C", str(root), "archive", base, "agent-harness/harness_rewrite"],
                                 check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("released kernel source unavailable")
    subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
    (tmp_path / "rig").mkdir()
    rig = Rig(tmp_path / "rig")
    publish_policy(rig, POLICY)
    wi = create(rig)
    assert revise(rig, wi, TARGET)["status"] == "local_committed"
    events = [e for h in state(rig, wi)[1] for e in h["events"]]
    script = ("import json,sys; sys.path.insert(0, sys.argv[1]);"
              "from harness_rewrite.domain import reduce; from harness_rewrite.model import Rejected\n"
              "try:\n reduce(None, json.load(sys.stdin)); print('read')\n"
              "except Rejected as exc: print(exc.code)")
    result = subprocess.run([sys.executable, "-I", "-c", script, str(old / "agent-harness")],
                            input=json.dumps(events), capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "journal_reader_upgrade_required"


def test_changing_only_the_work_class_is_a_revision(tmp_path):
    rig = Rig(tmp_path)
    publish_policy(rig, POLICY)
    why = "operating_area_ref=OA-PULSE; operating_area_work_class=improvement"
    for recorded in (False, True):
        reclassifies(rig, make(rig, why, f"create-{recorded}"), recorded)


def reclassifies(rig, wi, recorded):
    area = {"type": "operating_area", "ref": "OA-PULSE", "reason": "Human set the work class"}
    if recorded:
        first = {**area, "work_class": "improvement"}
        rig.context = replace(rig.context, source=SourceInput(
            "native-confirmation", "native_human", b"Confirmed", "record_affiliation",
            digest({"intent": "record_affiliation", "work_item_id": wi, "input": first})))
        recorded_result = rig.kernel.execute(Command("record-" + wi, "record_affiliation", first, wi), "native")
        assert recorded_result["status"] == "local_committed", recorded_result
    assert observed(rig, wi)["work_class"] == "improvement"
    assert revise(rig, wi, {**area, "work_class": "improvement"}, request="same-" + wi)["status"] == (
        "local_committed")
    assert revised_events(rig, wi) == []
    assert revise(rig, wi, {**area, "work_class": "operations"})["status"] == "local_committed"
    [event] = revised_events(rig, wi)
    assert event["record"]["revised_from"] == {
        "type": "operating_area", "ref": "OA-PULSE", "work_class": "improvement"}
    assert event["previous_observation"]["work_class"] == "improvement"
    view = observed(rig, wi)
    assert view["state"] == "admitted" and view["work_class"] == "operations"
    assert view["superseded"] == [{"type": "operating_area_work_class", "ref": "improvement"}]


def test_override_area_keeps_the_source_work_class(tmp_path):
    rig = Rig(tmp_path)
    publish_policy(rig, POLICY)
    wi = create(rig, definition={**DEFINITION, "why": "operating_area_work_class=improvement"})
    publish_policy(rig, {**POLICY, "work_item_operating_area_overrides": {wi: "OA-PULSE"}})
    view = observed(rig, wi)
    assert view["reference"] == {"type": "operating_area", "ref": "OA-PULSE"}
    assert view["work_class"] == "improvement"
    area = {"type": "operating_area", "ref": "OA-PULSE", "reason": "Human set the work class"}
    assert revise(rig, wi, {**area, "work_class": "improvement"}, request="same-" + wi)["status"] == (
        "local_committed")
    assert revised_events(rig, wi) == []
    assert revise(rig, wi, {**area, "work_class": "operations"})["status"] == "local_committed"
    [event] = revised_events(rig, wi)
    assert event["record"]["revised_from"] == {
        "type": "operating_area", "ref": "OA-PULSE", "work_class": "improvement"}
    assert event["previous_observation"]["work_class"] == "improvement"
    assert observed(rig, wi)["work_class"] == "operations"


def test_a_refused_affiliation_names_its_cause():
    from harness_rewrite import affiliation

    policy = {
        "legacy_cutoff": "2026-08-18T00:00:00+08:00",
        "required_result_refs": ["CR-05", "CR-06"],
        "registered_operating_area_refs": ["OA-MARSH-01"],
        "registered_operating_area_work_classes": ["operating_evidence"],
    }
    record = {"work_item_id": "wi_x", "created_at": "2026-11-04T12:59:16+00:00",
              "title": "Marsh 为《AI真人秀》供稿", "source": "挂 CR-06，参考 CR-05 的做法"}
    admission = {"work_item_id": "wi_x", "admission_ref_type": "operating_area",
                 "admission_ref": "OA-MARSH-01", "operating_area_work_class": "operating_evidence"}
    result = affiliation.evaluate(record, policy, admission)
    assert (result["state"], result["reason"]) == ("unavailable", "affiliation_source_invalid")
    assert result["detail"]["code"] == "decision_conflicts_with_source"
    assert "CR-05, CR-06" in result["detail"]["message"]
    assert "OA-MARSH-01" in result["detail"]["message"]
    assert "revise_affiliation" in result["detail"]["message"]

    revised = {**admission, "revised_from": {"type": "current_result", "ref": "CR-06"}}
    assert affiliation.evaluate(record, policy, revised)["state"] == "admitted"

    unregistered = {**admission, "admission_ref": "OA-NOBODY-01"}
    detail = affiliation.evaluate(record, policy, unregistered)["detail"]
    assert detail["code"] == "decision_unregistered" and "OA-NOBODY-01" in detail["message"]

    assert affiliation.evaluate({"work_item_id": "wi_x"}, policy, None)["detail"]["code"] == (
        "malformed_record_or_policy")
