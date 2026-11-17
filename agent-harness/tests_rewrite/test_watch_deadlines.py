from dataclasses import replace
import json

import pytest

from harness_rewrite.cli import main
from tests_rewrite import test_collaboration
from tests_rewrite.test_review import (
    ARTIFACT,
    VERIFIER,
    assign,
    verify,
    approve,
    candidate_id,
    control,
)
from tests_rewrite.test_workflow import graph, node, edge, original, error
from tests_rewrite.test_watch import query, items

rig = test_collaboration.rig


def deadline_offer(rig, due, kind="request", request="deadline-offer"):
    return rig.send(
        "offer_collaboration",
        {
            "kind": kind,
            "purpose": "Original bounded responsibility",
            "required": True,
            "main_only": False,
            "recipients": [
                {"principal": rig.contexts["helper"].principal(), "resources": ["repo"]}
            ],
            "deadline": due,
        },
        request=request,
    )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        10,
        "tomorrow",
        "2026-10-08",
        "2026-10-08T12:00:00",
        "2026-02-30T00:00:00Z",
        "2026-10-08T00:00:00+08:99",
        "2026-10-08T00:00:00-00:00",
    ],
)
def test_deadline_inputs_cannot_create_an_ambiguous_fact(rig, bad):
    due_node = {**node("work"), "deadline": bad}
    plan = graph(rig, [due_node])
    before = rig.state()["work_item"]
    assert (
        error(
            rig.approve(
                "adopt_plan", {"graph": plan, "plan_text": "Exact invalid deadline"}
            )
        )
        == "invalid_input"
    )
    assert rig.state()["work_item"] == before
    rig.adopt(graph(rig))
    before = rig.state()["work_item"]
    assert error(deadline_offer(rig, bad)) == "invalid_input"
    assert (
        error(
            rig.send(
                "rollover",
                {
                    "target": rig.contexts["helper"].principal(),
                    "summary": "Same work",
                    "deadline": bad,
                },
            )
        )
        == "invalid_input"
    )
    assert rig.state()["work_item"] == before


@pytest.mark.parametrize(
    "clock,state,lateness",
    [
        ("2026-10-07T00:00:04Z", "not_due", 0),
        ("2026-10-07T00:00:05Z", "overdue", 0),
        ("2026-10-07T00:00:08Z", "overdue", 3),
    ],
)
def test_confirmed_task_deadline_is_not_the_advisory_age_threshold(
    rig, clock, state, lateness
):
    literal = "2026-10-07T08:00:05+08:00"
    rig.adopt(graph(rig, [{**node("work"), "deadline": literal}]))
    attempt = original(rig.claim("work"))["attempt_id"]
    revision = rig.state()["work_item"]["local_revision"]
    rig.kernel._clock = lambda: clock
    result = next(i for i in items(rig) if i["kind"] == "task")
    assert result["record_age"]["classification"] == "unknown"
    assert result["deadline"] == {
        "at": literal,
        "source": "confirmed_plan",
        "graph_revision": 1,
        "node_id": "work",
    }
    assert result["overdue"] == {
        "state": state,
        "reason": "original_recorded_deadline",
        "lateness_seconds": lateness,
    }
    assert rig.state()["work_item"]["local_revision"] == revision
    assert original(rig.result(attempt))["current"] is True


@pytest.mark.parametrize(
    "clock,reason",
    [
        ("invalid", "observation_clock_unavailable"),
        ("2026-10-06T23:59:00Z", "observation_clock_before_record"),
    ],
)
def test_deadline_does_not_turn_missing_or_backward_clock_into_overdue(
    rig, clock, reason
):
    rig.adopt(graph(rig))
    original(deadline_offer(rig, "2026-10-06T00:00:00Z"))
    rig.kernel._clock = lambda: clock
    row = next(i for i in items(rig) if i["kind"] == "collaboration")
    assert row["overdue"] == {"state": "unknown", "reason": reason}


def test_overdue_delegation_still_requires_exact_recipient_and_can_be_accepted(rig):
    rig.adopt(graph(rig, [node("work", role="collaborator")]))
    due = "2026-10-06T00:00:00Z"
    offered = deadline_offer(rig, due, "delegate")
    contract = original(offered)["contract_id"]
    assert (
        next(i for i in items(rig) if i["kind"] == "collaboration")["overdue"]["state"]
        == "overdue"
    )
    assert original(deadline_offer(rig, due, "delegate")) == original(offered)
    assert (
        error(test_collaboration.respond(rig, contract, "accept", handle="third"))
        == "authority_denied"
    )
    original(test_collaboration.respond(rig, contract, "accept"))
    original(rig.claim("work", handle="helper"))


def test_reassigned_contract_preserves_deadline_and_history(rig):
    rig.adopt(graph(rig))
    due = "2026-10-07T00:00:00.123456Z"
    contract = original(deadline_offer(rig, due))["contract_id"]
    new = original(
        rig.send(
            "reassign_collaboration",
            {
                "contract_id": contract,
                "reason": "New exact recipient",
                "recipients": [
                    {
                        "principal": rig.contexts["third"].principal(),
                        "resources": ["repo"],
                    }
                ],
            },
        )
    )["contract_id"]
    row = next(i for i in items(rig) if i["kind"] == "collaboration")
    assert row["id"] == new and row["deadline"]["at"] == due
    contracts = rig.state()["result"]["current_observation"]["collaborations"]
    assert len(contracts) == 2 and all(c["deadline"] == due for c in contracts)


def test_overdue_transfer_is_not_cancelled_and_historical_authority_stays_current(rig):
    rig.adopt(graph(rig))
    original_transfer = original(
        rig.send(
            "rollover",
            {
                "target": rig.contexts["helper"].principal(),
                "summary": "Original same-WI transfer",
                "deadline": "2026-10-06T00:00:00Z",
            },
        )
    )
    revision = rig.state()["work_item"]["local_revision"]
    row = next(i for i in items(rig) if i["kind"] == "transfer")
    assert row["overdue"]["state"] == "overdue"
    assert row["deadline"]["transfer_id"] == original_transfer["transfer_id"]
    original(
        rig.send(
            "accept_transfer",
            {"transfer_id": original_transfer["transfer_id"]},
            handle="helper",
        )
    )
    rig.contexts["helper"] = replace(rig.contexts["helper"], generation=2)
    original(rig.claim("work", handle="helper"))
    snapshot = query(rig, revision=revision)
    assert snapshot["result"]["items"]["items"][0]["deadline"] == row["deadline"]
    assert snapshot["capabilities"] == rig.state()["capabilities"]
    assert error(rig.claim("work")) == "authority_changed"


def test_fixed_candidate_gates_keep_independent_deadlines(rig):
    rig.contexts["verifier"] = VERIFIER
    v = {
        **control("v", "verification", ["accepted"]),
        "deadline": "2026-10-07T00:00:05Z",
    }
    r = {**control("r", "review", ["approve"]), "deadline": "2026-10-07T00:00:10Z"}
    d = {
        **node("ship", kind="delivery", role="delivery", outputs=["report"]),
        "deadline": "2026-10-07T00:00:20Z",
    }
    rig.adopt(
        graph(
            rig,
            [node("work", outputs=["report"]), v, r, d],
            [
                edge("work", "v"),
                edge("v", "r", "accepted"),
                edge("r", "ship", "approve"),
            ],
        )
    )
    original(
        rig.result(
            original(rig.claim("work"))["attempt_id"], artifacts={"report": ARTIFACT}
        )
    )
    candidate = candidate_id(rig)
    rig.kernel._clock = lambda: "2026-10-07T00:00:06Z"
    waiting = next(i for i in items(rig) if i["kind"] == "verification")
    assert (
        waiting["deadline"]["node_id"] == "v"
        and waiting["overdue"]["state"] == "overdue"
    )
    original(verify(rig, candidate, assign(rig, candidate)))
    waiting = next(i for i in items(rig) if i["kind"] == "review")
    assert (
        waiting["deadline"]["node_id"] == "r"
        and waiting["overdue"]["state"] == "not_due"
    )
    original(approve(rig, candidate))
    original(
        rig.send(
            "declare_delivery",
            {
                "candidate_id": candidate,
                "deliveries": [
                    {
                        "artifact": ARTIFACT.reference(),
                        "target": "report.md",
                        "required": True,
                        "node_id": "ship",
                        "output": "report",
                    }
                ],
            },
        )
    )
    waiting = next(i for i in items(rig) if i["kind"] == "effect")
    assert (
        waiting["deadline"]["node_id"] == "ship"
        and waiting["overdue"]["state"] == "not_due"
    )


@pytest.mark.parametrize(
    "args",
    [
        ["identity", "--revision", "1"],
        ["inspect", "--revision", "0"],
        ["resume", "--revision", "1"],
    ],
)
def test_cli_rejects_unusable_revision_before_native_identity(args, capsys):
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"
