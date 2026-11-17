"""Policy defaults are core facts, never guessed human approval or an entrypoint patch."""

from dataclasses import replace
import json

import pytest

from harness_rewrite import Command, Query, SourceInput
from harness_rewrite import affiliation
from harness_rewrite.model import digest
from tests_rewrite.test_production_ledger import POLICY, RECORD, Rig, WI, git
from tests_rewrite.test_workflow import DEFINITION

DEFAULT_POLICY = {**POLICY, "default_portfolio_ref": "PORTFOLIO-DEFAULT"}


def publish_policy(rig, policy):
    git(rig.a, "pull", "--ff-only")
    path = rig.a / ".harness/platform-epoch.yaml"
    path.write_text(json.dumps(policy))
    git(rig.a, "add", str(path))
    git(rig.a, "commit", "-m", "Fixture production policy")
    git(rig.a, "push", "origin", "main")


def create(rig, intent="create_lightweight", definition=None):
    rig.context = replace(rig.context, source=SourceInput(
        "native-request", "native_agent", b"Bounded native request"))
    result = rig.kernel.execute(Command("create", intent, definition or DEFINITION), "native")
    assert result["status"] == "local_committed", result
    return result["work_item"]["id"]


def state(rig, wi):
    with rig.ledger.connection(write=False, work_item_id=wi) as conn:
        from harness_rewrite import Kernel
        current, _, _, history = Kernel._replay(conn, rig.context.namespace, wi)
    return current, history


@pytest.mark.parametrize("default_only", [False, True])
def test_lightweight_create_start_and_peer_replay_need_no_extra_human_choice(tmp_path, default_only):
    rig = Rig(tmp_path)
    policy = {**DEFAULT_POLICY, **({"required_result_refs": [], "registered_operating_area_refs": [],
        "registered_operating_area_work_classes": []} if default_only else {})}
    publish_policy(rig, policy)
    wi = create(rig)
    current, history = state(rig, wi)
    record = current["affiliation"]
    assert record["admission_ref_type"] == "portfolio_default"
    assert record["admission_ref"] == "PORTFOLIO-DEFAULT"
    assert record["decided_by"] == "policy:production"
    assert current["status"] == "draft" and current["holder"] is None
    event = next(e for h in history for e in h["events"] if e["kind"] == "AffiliationRecorded")
    assert event["payload"]["source"] == {
        "kind": "production_policy", "sha256": digest(policy)}
    assert not rig.context.source.approved_intent
    peer = rig.client(rig.backend(rig.b))
    before = git(rig.remote, "rev-parse", "main")
    observed = peer.inspect(Query(wi), "native")
    assert observed["result"]["current_observation"]["affiliation"]["reference"]["ref"] == "PORTFOLIO-DEFAULT"
    assert git(rig.remote, "rev-parse", "main") == before
    started = peer.execute(Command("start", "start_work", {}, wi), "native")
    assert started["status"] == "local_committed", started
    committed = git(rig.remote, "rev-parse", "main")
    assert rig.kernel.execute(Command("start", "start_work", {}, wi), "native")["status"] == "local_committed"
    assert git(rig.remote, "rev-parse", "main") == committed
    current, history = state(rig, wi)
    assert current["status"] == "active"
    assert sum(e["kind"] == "AffiliationRecorded" for h in history for e in h["events"]) == 1


def test_existing_native_lightweight_draft_uses_default_atomically_on_first_start(tmp_path):
    rig = Rig(tmp_path)
    wi = create(rig)
    assert not state(rig, wi)[0].get("affiliation")
    publish_policy(rig, DEFAULT_POLICY)
    before = git(rig.remote, "rev-parse", "main")
    observed = rig.kernel.resume(wi, "native")
    assert observed["next_action"]["intent"] == "start_work", observed
    assert observed["result"]["current_observation"]["affiliation"]["reason"] == "policy_default"
    assert git(rig.remote, "rev-parse", "main") == before
    owner = rig.context
    rig.context = replace(owner, actor="outsider")
    denied = rig.kernel.execute(Command("outsider", "start_work", {}, wi), "native")
    assert denied["error"]["code"] == "authority_denied"
    assert git(rig.remote, "rev-parse", "main") == before
    rig.context = owner
    started = rig.kernel.execute(Command("start", "start_work", {}, wi), "native")
    assert started["status"] == "local_committed", started
    current, history = state(rig, wi)
    assert current["affiliation"]["admission_ref"] == "PORTFOLIO-DEFAULT"
    assert [e["kind"] for e in history[-1]["events"]][:2] == ["AffiliationRecorded", "WorkStarted"]


@pytest.mark.parametrize("kind", ["standard", "historical", "missing_policy", "malformed_policy"])
def test_default_does_not_bypass_other_admission_requirements(tmp_path, kind):
    rig = Rig(tmp_path)
    if kind != "missing_policy":
        publish_policy(rig, {**DEFAULT_POLICY, **(
            {"default_portfolio_ref": "unregistered"} if kind == "malformed_policy" else {})})
    wi = WI if kind == "historical" else create(
        rig, "create_draft" if kind == "standard" else "create_lightweight")
    before = git(rig.remote, "rev-parse", "main")
    result = rig.kernel.execute(Command("start", "start_work", {}, wi), "native")
    assert result["error"]["code"] == (
        "affiliation_source_invalid" if kind == "malformed_policy" else "business_affiliation_missing")
    assert git(rig.remote, "rev-parse", "main") == before


@pytest.mark.parametrize("source,expected", [
    ("CR-ONE explicit result", {"type": "current_result", "ref": "CR-ONE"}),
    ("operating_area_ref=OA-PULSE; operating_area_work_class=improvement",
     {"type": "operating_area", "ref": "OA-PULSE"}),
])
def test_explicit_affiliation_precedes_default(tmp_path, source, expected):
    rig = Rig(tmp_path)
    publish_policy(rig, DEFAULT_POLICY)
    wi = create(rig, definition={**DEFINITION, "why": source})
    assert not state(rig, wi)[0].get("affiliation")
    observation = rig.kernel.inspect(Query(wi), "native")["result"]["current_observation"]
    assert observation["affiliation"]["reference"] == expected
    assert rig.kernel.execute(Command("start", "start_work", {}, wi), "native")["status"] == "local_committed"


@pytest.mark.parametrize("record,reason", [
    ({**RECORD, "created_at": "2025-12-01T00:00:00Z"}, "legacy_epoch_quarantined"),
    ({**RECORD, "source": "CR-ONE; operating_area_ref=OA-PULSE; operating_area_work_class=improvement"},
     "business_affiliation_ambiguous"),
    ({**RECORD, "source": "operating_area_ref=OA-UNKNOWN; operating_area_work_class=improvement"},
     "affiliation_source_invalid"),
])
def test_default_never_resolves_quarantine_ambiguity_or_invalid_source(record, reason):
    assert affiliation.evaluate(record, DEFAULT_POLICY, None, default_eligible=True)["reason"] == reason


def test_later_policy_change_does_not_silently_reassign_a_recorded_default(tmp_path):
    rig = Rig(tmp_path)
    publish_policy(rig, DEFAULT_POLICY)
    wi = create(rig)
    original = state(rig, wi)[0]["affiliation"]
    publish_policy(rig, {**DEFAULT_POLICY, "default_portfolio_ref": "PORTFOLIO-OTHER"})
    observed = rig.kernel.inspect(Query(wi), "native")["result"]["current_observation"]
    assert observed["affiliation"]["state"] == "unavailable"
    assert state(rig, wi)[0]["affiliation"] == original
