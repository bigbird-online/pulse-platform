"""Recorded production journals from the retired track still replay unchanged.

The fixture holds journal events written before the old-track continuation was
retired: an original execution continued through ExistingExecutionContinued,
and a historical draft whose first plan declared its scope through
HistoricalScopeDeclared. Both must reduce to the state recorded at that time.
"""

import json
from pathlib import Path

import pytest

from harness_rewrite import Command
from harness_rewrite.domain import reduce
from tests_rewrite.test_production_ledger import Rig, RECORD, WI

JOURNALS = json.loads((Path(__file__).with_name("historical_journals.json")).read_text())


@pytest.mark.parametrize("name", ["existing_execution", "historical_scope"])
def test_recorded_history_replays_to_its_recorded_state(name):
    journal = JOURNALS[name]
    state = None
    for events in journal["history"]:
        state = reduce(state, events)
    assert json.loads(json.dumps(state, sort_keys=True, default=str)) == journal["state"]


def test_continued_original_execution_keeps_its_holder_and_retained_allocation():
    journal = JOURNALS["existing_execution"]
    kinds = [event["kind"] for events in journal["history"] for event in events]
    assert "ExistingExecutionContinued" in kinds
    state = None
    for events in journal["history"]:
        state = reduce(state, events)
    assert state is not None
    assert "historical_execution" not in state
    assert state["holder"] is not None and state["existing_execution"]["original_epoch"] == 7
    (resource,) = state["workspaces"].values()
    assert resource["existing_allocation"] and resource["existing_binding"]


def test_historical_scope_declaration_binds_the_recorded_resources():
    journal = JOURNALS["historical_scope"]
    declared = [
        event["payload"]["resources"]
        for events in journal["history"]
        for event in events
        if event["kind"] == "HistoricalScopeDeclared"
    ]
    assert declared == [["repo"]]
    state = None
    for events in journal["history"]:
        state = reduce(state, events)
    assert state is not None and state["resources"] == ["repo"]
    assert state["definition"]["resources"] == ["repo"]


@pytest.mark.parametrize(
    "intent",
    [
        "review_existing_execution",
        "prepare_legacy_draft",
        "activate_legacy_draft",
        "seal_migration",
        "prepare_migration",
        "activate_migration",
        "adopt_migrated_effects",
    ],
)
def test_retired_track_intents_are_rejected_without_writing(tmp_path, intent):
    from tests_rewrite.test_production_ledger import git

    rig = Rig(tmp_path, {**RECORD, "status": "in_progress"})
    before = git(rig.remote, "rev-parse", "main")
    result = rig.kernel.execute(Command("retired-" + intent, intent, {}, WI), "native")
    assert result["status"] == "rejected", result
    assert result["error"]["code"] == "track_retired"
    assert git(rig.remote, "rev-parse", "main") == before


def test_repository_identity_does_not_admit_same_slug_on_another_server():
    from harness_rewrite.existing_execution import retained_binding
    from harness_rewrite.model import Rejected

    resource = {"existing_binding": {"remote_url": "https://github.com/owner/repo.git"}}
    with pytest.raises(Rejected, match="no longer admitted"):
        retained_binding(resource, {"remote_url": "https://unrelated.example/owner/repo.git"})
