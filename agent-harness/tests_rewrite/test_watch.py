"""Observation age and waiting never become another authority or runtime verdict."""

from dataclasses import replace
import hashlib
import json

import pytest

from harness_rewrite import Query
from harness_rewrite.cli import main
from harness_rewrite.effects import Effects, FileDelivery
from harness_rewrite.model import digest
from tests_rewrite.test_collaboration import offer, respond
from tests_rewrite import test_collaboration, test_publication
from tests_rewrite.test_delivery import ready  # noqa: F401
from tests_rewrite.test_lightweight import light, record
from tests_rewrite.test_review import VERIFIER, produce, assign, verify, approve
from tests_rewrite.test_workflow import graph, node, original, error


rig = test_collaboration.rig
setup = test_publication.setup
publishing = test_publication.publishing


def query(rig, **kwargs):
    result = rig.kernel.inspect(Query(rig.wi, view="watch", **kwargs), "owner")
    assert result["status"] == "read_only", result
    return result


def items(rig, **kwargs):
    return query(rig, **kwargs)["result"]["items"]["items"]


def test_watch_waiting_age_never_withdraws_live_authority_or_changes_storage(
    rig, tmp_path
):  # noqa: F811
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    before = rig.state()
    file = tmp_path / "journal.sqlite"
    checksum = hashlib.sha256(file.read_bytes()).hexdigest()
    rig.kernel._clock = lambda: "2026-10-08T00:00:00Z"
    response = query(rig, age_threshold_seconds=3600)
    task = next(i for i in response["result"]["items"]["items"] if i["kind"] == "task")
    assert task["id"] == attempt and task["recorded_status"] == "running"
    assert task["record_age"]["seconds"] == 86400
    assert task["record_age"]["classification"] == "stale"
    assert task["executor"]["presence"] == "unknown"
    assert task["executor"]["reference"] == "watch-executor." + digest(
        rig.contexts["owner"].principal()
    )
    assert task["overdue"] == {
        "state": "unknown",
        "reason": "no_original_deadline_recorded",
    }
    assert response["work_item"] == before["work_item"]
    assert response["capabilities"] == before["capabilities"]
    assert hashlib.sha256(file.read_bytes()).hexdigest() == checksum
    text = json.dumps(response)
    for private in (
        "private-producer-session",
        "Original collaboration decision",
        "native_session_id",
        "principal",
    ):
        assert private not in text
    assert set(response["result"]["current_observation"]) == {
        "source",
        "generation",
        "business_progress",
    }
    assert original(rig.result(attempt))["current"] is True


@pytest.mark.parametrize(
    "clock,threshold,classification,reason",
    [
        ("2026-10-07T00:00:05Z", 10, "recent", "caller_selected_age_threshold"),
        ("2026-10-07T00:00:10Z", 10, "stale", "caller_selected_age_threshold"),
        ("2026-10-07T00:00:10Z", None, "unknown", "no_query_threshold"),
        ("2026-10-06T00:00:00Z", 10, "unknown", "observation_clock_before_record"),
        ("invalid", 10, "unknown", "observation_clock_unavailable"),
        ("2026-10-07T00:00:00", 10, "unknown", "observation_clock_unavailable"),
    ],
)
def test_watch_clock_and_missing_threshold_are_explicit(
    rig, clock, threshold, classification, reason
):  # noqa: F811
    rig.adopt(graph(rig))
    rig.kernel._clock = lambda: clock
    row = items(rig, age_threshold_seconds=threshold)[0]
    assert row["record_age"]["classification"] == classification
    assert row["record_age"]["reason"] == reason
    if reason in {"observation_clock_before_record", "observation_clock_unavailable"}:
        assert row["record_age"]["seconds"] is None


def test_pending_recipient_and_rejection_remain_original_obligations(rig):  # noqa: F811
    rig.adopt(graph(rig))
    contract = offer(rig, "request")
    row = next(i for i in items(rig) if i["kind"] == "collaboration")
    assert row["id"] == contract and row["recorded_status"] == "pending"
    assert row["executor"]["last_authenticated_record"] is None
    original(respond(rig, contract, "reject"))
    row = next(i for i in items(rig) if i["kind"] == "collaboration")
    assert (
        row["recorded_status"] == "rejected" and row["detail"]["owner_attention_needed"]
    )
    assert row["executor"]["presence"] == "unknown"
    original(
        rig.send(
            "withdraw_collaboration",
            {"contract_id": contract, "reason": "Owner withdraws original request"},
        )
    )
    assert not any(i["kind"] == "collaboration" for i in items(rig))


def test_accepted_delegation_and_return_do_not_revive_old_attempt(rig):  # noqa: F811
    rig.adopt(graph(rig, [node("work", role="collaborator")]))
    contract = offer(rig)
    original(respond(rig, contract, "accept"))
    attempt = original(rig.claim("work", handle="helper"))["attempt_id"]
    row = next(i for i in items(rig) if i["kind"] == "task")
    assert row["id"] == attempt
    assert (
        row["executor"]["reference"]
        != next(i for i in items(rig) if i["kind"] == "execution")["executor"][
            "reference"
        ]
    )
    original(respond(rig, contract, "return"))
    assert not any(i["kind"] == "task" for i in items(rig))
    assert error(rig.claim("work", handle="helper")) == "authority_denied"


def test_current_verifier_review_and_rework_are_separate(rig):  # noqa: F811
    rig.contexts["verifier"] = VERIFIER
    candidate = produce(rig)
    row = next(i for i in items(rig) if i["kind"] == "verification")
    assert row["recorded_status"] == "unassigned"
    assignment = assign(rig, candidate)
    row = next(i for i in items(rig) if i["kind"] == "verification")
    assert row["recorded_status"] == "awaiting_result"
    assert row["executor"]["presence"] == "unknown"
    original(verify(rig, candidate, assignment, "rejected"))
    assert not any(i["kind"] == "verification" for i in items(rig))
    assert any(i["kind"] == "review" for i in items(rig))
    original(approve(rig, candidate, "changes_requested"))
    assert any(i["kind"] == "revision" and i["id"] == candidate for i in items(rig))


def test_unknown_effect_stays_pending_until_original_readback(ready):  # noqa: F811
    rig, _, effect_id, destination, worker = ready

    class Interrupted(BaseException):
        pass

    def fail(stage):
        if stage == "after_send":
            raise Interrupted()

    with pytest.raises(Interrupted):
        Effects(rig.kernel, FileDelivery(destination), worker, fault=fail).drain(rig.wi)
    before = rig.state()
    row = next(i for i in items(rig) if i["kind"] == "effect")
    assert row["id"] == effect_id and row["recorded_status"] == "attempt_started"
    assert row["detail"]["attempt_count"] == 1
    assert (destination / "result.txt").exists()
    assert rig.state()["effects"] == before["effects"]
    Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi)
    assert not any(i["kind"] == "effect" for i in items(rig))


def test_historical_watch_is_bounded_and_cannot_revive_old_execution(rig):  # noqa: F811
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    revision = rig.state()["work_item"]["local_revision"]
    original(rig.result(attempt))
    assert not any(i["kind"] == "task" for i in items(rig))
    snapshot = query(rig, revision=revision, limit=1)
    watch = snapshot["result"]["items"]
    assert watch["source_revision"] == revision and watch["next_offset"] == 1
    later = query(rig, revision=revision, after=1, limit=1)
    assert later["result"]["items"]["items"][0]["id"] == attempt
    assert snapshot["capabilities"] == rig.state()["capabilities"]
    assert snapshot["result"]["observation_is_historical"] is True
    assert all(
        (i["last_related_record"] or {"revision": 0})["revision"] <= revision
        for p in (watch, later["result"]["items"])
        for i in p["items"]
    )


def test_terminal_without_observed_inventory_is_not_an_archive_receipt(tmp_path):
    rig = light(tmp_path)
    wid = record(rig)
    original(
        rig.approve(
            "complete_lightweight", {"record_ids": [wid], "acceptance_refs": ["AC-1"]}
        )
    )
    watch = query(rig)["result"]["items"]
    assert watch["archive_scan"]["state"] == "unknown"
    assert watch["archive_scan"]["deletion_authority"] is False
    assert watch["archive_scan"]["required_originals_retained"] is True
    assert not watch["items"]


@pytest.mark.parametrize("threshold", [0, -1, True, "3600"])
def test_invalid_watch_option_is_rejected_before_reading(rig, threshold):  # noqa: F811
    assert (
        rig.kernel.inspect(
            Query(rig.wi, view="watch", age_threshold_seconds=threshold), "owner"
        )["error"]["code"]
        == "invalid_input"
    )
    assert (
        rig.kernel.inspect(Query(rig.wi, age_threshold_seconds=3600), "owner")["error"][
            "code"
        ]
        == "invalid_input"
    )


def test_watch_retains_namespace_and_exact_wi_access(rig):  # noqa: F811
    rig.contexts["stranger"] = replace(rig.contexts["owner"], readable_wis=frozenset())
    assert (
        rig.kernel.inspect(Query(rig.wi, view="watch"), "stranger")["error"]["code"]
        == "authority_denied"
    )
    rig.contexts["other"] = replace(rig.contexts["owner"], namespace="elsewhere")
    assert (
        rig.kernel.inspect(Query(rig.wi, view="watch"), "other")["error"]["code"]
        == "domain_conflict"
    )


def test_cli_does_not_silently_ignore_watch_only_option(capsys):
    assert main(["identity", "--age-threshold-seconds", "60"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


def test_watch_distinguishes_unacknowledged_verifier(rig):
    rig.contexts["verifier"] = VERIFIER
    candidate = produce(rig)
    assignment = original(
        rig.send(
            "assign_verification",
            {
                "candidate_id": candidate,
                "verifier": VERIFIER.principal(),
            },
        )
    )["assignment_id"]
    waiting = next(i for i in items(rig) if i["kind"] == "verification")
    assert waiting["recorded_status"] == "awaiting_ack"
    assert waiting["detail"]["assignment_id"] == assignment
    assert waiting["executor"]["last_authenticated_record"] is None
    original(
        rig.send(
            "acknowledge_verification",
            {
                "candidate_id": candidate,
                "assignment_id": assignment,
            },
            handle="verifier",
        )
    )
    waiting = next(i for i in items(rig) if i["kind"] == "verification")
    assert waiting["recorded_status"] == "awaiting_result"
    assert waiting["executor"]["last_authenticated_record"] is not None


def test_pending_transfer_never_revives_the_fenced_holder(rig):
    rig.adopt(graph(rig))
    transferred = rig.send(
        "rollover",
        {
            "summary": "Same outcome on original successor",
            "target": rig.contexts["helper"].principal(),
        },
    )
    transfer_id = original(transferred)["transfer_id"]
    response = query(rig)
    waiting = response["result"]["items"]["items"]
    assert len(waiting) == 1 and waiting[0]["kind"] == "transfer"
    assert waiting[0]["id"] == transfer_id
    assert waiting[0]["recorded_status"] == "awaiting_acceptance"
    assert response["capabilities"] == transferred["capabilities"]
    assert error(rig.claim("work")) == "authority_denied"


def test_journal_revision_defines_latest_interaction_despite_clock_rollback(rig):
    rig.adopt(graph(rig))
    rig.kernel._clock = lambda: "2026-10-06T23:00:00Z"
    claimed = rig.claim("work")
    original(claimed)
    revision = claimed["work_item"]["local_revision"]
    rig.kernel._clock = lambda: "2026-10-07T00:00:00Z"
    holder = next(
        i for i in items(rig, age_threshold_seconds=4000) if i["kind"] == "execution"
    )
    assert holder["executor"]["last_authenticated_record"] == {
        "revision": revision,
        "at": "2026-10-06T23:00:00Z",
    }
    assert holder["record_age"]["seconds"] == 3600


def test_archive_candidate_requires_actual_recorded_resource_retirement(publishing):
    from harness_rewrite.git_workspaces import GitWorkspaces

    rig, resource, path, _, _, _, _, _, worker, data = publishing
    test_publication.publish(publishing)
    original(rig.send("complete", {"candidate_id": data["candidate_id"]}))
    scan = query(rig)["result"]["items"]["archive_scan"]
    assert scan["state"] == "not_ready" and scan["unretired_resources"] == 1
    original(
        rig.approve(
            "retire_resources",
            {
                "resource_ids": [resource["resource_id"]],
                "reason": "Exact clean isolated resource",
            },
            actor=rig.contexts["owner"],
        )
    )
    Effects(rig.kernel, GitWorkspaces(), worker).drain(rig.wi)
    Effects(rig.kernel, GitWorkspaces(), worker).drain(rig.wi)
    assert not path.exists()
    before = rig.state()
    scan = query(rig)["result"]["items"]["archive_scan"]
    assert scan["state"] == "candidate" and scan["unretired_resources"] == 0
    assert scan["deletion_authority"] is False
    assert rig.state()["work_item"] == before["work_item"]
