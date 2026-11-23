"""An unattended gate reads one Agent's open contracts, minimally and read-only."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from harness_rewrite import cli, collaboration_inbox
from tests_rewrite.test_collaboration import respond
from tests_rewrite.test_cross_node_collaboration import cross as cross, offer  # noqa: F401
from tests_rewrite.test_collaboration import rig as rig  # noqa: F401
from tests_rewrite.test_effect_service import installed
from tests_rewrite.test_production_ledger import Rig as ProductionRig, git
from tests_rewrite.test_review import PROOF
from tests_rewrite.test_workflow import error, graph, original


def read(r, actor="hestia"):
    storage = SimpleNamespace(connection=r.kernel._connection)
    return collaboration_inbox.inbox(storage, "team", actor)


def test_inbox_names_only_the_minimal_facts_and_follows_the_answer(cross):
    cross.adopt(graph(cross))
    c = original(offer(cross))["contract_id"]
    before = (cross.kernel._database).read_bytes()
    inbox = read(cross)
    assert cross.kernel._database.read_bytes() == before
    assert inbox == {
        "schema": "harness.collaboration_inbox/v1",
        "status": "read_only",
        "actor": "hestia",
        "items": [
            {
                "role": "recipient",
                "work_item_id": cross.wi,
                "contract_id": c,
                "kind": "request",
                "status": "pending",
                "main_only": False,
                "offered_by": "producer",
                "offered_at": "2026-10-07T00:00:00Z",
            }
        ],
        "pull_requests": [],
        "held": [],
        "unavailable_items": [],
        "coverage": "complete",
        "source": "journal",
    }
    text = json.dumps(inbox)
    assert "living-room" not in text and "session" not in text and "repo" not in text
    assert read(cross, "third")["items"] == []
    original(respond(cross, c, "accept", handle="unattended"))
    assert [i["status"] for i in read(cross)["items"]] == ["accepted"]
    original(
        respond(cross, c, "complete", handle="interactive-proof", evidence=[PROOF.reference()])
    )
    assert read(cross)["items"] == []


def test_withdrawn_and_deadline(cross):
    cross.adopt(graph(cross))
    c = original(
        cross.send(
            "offer_collaboration",
            {
                "kind": "request",
                "recipients": [{"principal": {"actor": "hestia"}, "resources": ["repo"]}],
                "purpose": "Due soon",
                "required": False,
                "main_only": False,
                "deadline": "2026-10-21T00:00:00Z",
            },
        )
    )["contract_id"]
    assert read(cross)["items"][0]["deadline"] == "2026-10-21T00:00:00Z"
    original(cross.send("withdraw_collaboration", {"contract_id": c, "reason": "Not needed"}))
    assert read(cross)["items"] == []


def test_inbox_and_board_carry_main_only(cross):
    cross.adopt(graph(cross))
    c = original(offer_targeted(cross, "wi_20261001_001"))["contract_id"]
    [item] = read(cross)["items"]
    assert (item["contract_id"], item["main_only"]) == (c, True)
    [row] = board(cross)
    assert row["main_only"] is True


def test_same_agent_contract_reaches_the_offering_agents_inbox(cross):
    cross.adopt(graph(cross))
    c = original(offer(cross, principal={"actor": "producer"}))["contract_id"]
    [item] = read(cross, "producer")["items"]
    assert item["contract_id"] == c and item["offered_by"] == "producer"


def test_installed_cli_reads_without_a_native_session_and_writes_nothing(
    tmp_path, capsys, monkeypatch
):
    fixture = tmp_path / "git"
    fixture.mkdir()
    production = ProductionRig(fixture)
    entry = installed(tmp_path, production.ledger.config)
    monkeypatch.setattr(sys, "argv", [str(entry)])
    assert cli.main(["collaboration-inbox", "--actor", "hestia"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "read_only" and response["items"] == []
    assert response["ledger"]["ref"] == production.ledger.config["ref"]
    assert git(production.remote, "rev-parse", "main") == production.before
    assert collaboration_inbox.main(entry, ["--actor", "bad actor"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


def board(r, now="2026-10-07T01:00:00Z"):
    with r.kernel._connection(write=False, work_item_id=r.wi) as conn:
        state, _, _, history = r.kernel._replay(conn, "team", r.wi)
    return collaboration_inbox.board(r.wi, "Plan", state, history, now)


def test_board_follows_one_contract_from_offer_to_reassignment(cross):
    cross.adopt(graph(cross))
    c = original(offer(cross))["contract_id"]
    [row] = board(cross)
    assert row["offered_by"] == {"actor": "producer", "surface": "local"}
    assert row["recipients"] == [{"actor": "hestia"}]
    assert row["main_only"] is False
    assert (row["purpose"], row["status"], row["settled"]) == (
        "Read the living-room temperature", "pending", False)
    assert row["next"] == {"role": "recipient", "actors": ["hestia"], "actions": ["accept", "reject"]}
    original(respond(cross, c, "accept", handle="unattended"))
    assert board(cross)[0]["next"]["actions"] == ["complete", "return"]
    original(respond(cross, c, "return", handle="interactive"))
    [row] = board(cross)
    assert row["status"] == "returned"
    assert row["next"] == {"role": "requester", "actors": ["producer"],
                           "actions": ["resend", "reassign", "withdraw", "adopt_partial"]}
    assert [(s["action"], s["actor"]) for s in row["timeline"]] == [
        ("offered", "producer"), ("accepted", "hestia"), ("returned", "hestia")]
    assert row["timeline"][2] == {"at": "2026-10-07T00:00:00Z", "action": "returned",
                                  "actor": "hestia", "reason": "Actual recipient decision"}
    moved = original(cross.send("reassign_collaboration", {
        "contract_id": c, "recipients": [{"principal": {"actor": "third"}, "resources": ["repo"]}],
        "reason": "Hestia is busy\nsecond line"}))["contract_id"]
    old, new = board(cross)
    assert (old["status"], old["settled"], old["next"]) == ("withdrawn", True, None)
    assert old["timeline"][-1] == {"at": "2026-10-07T00:00:00Z", "action": "reassigned", "actor": "producer",
                                   "reason": "Hestia is busy", "successor_contract_id": moved}
    assert new["timeline"] == [{"at": "2026-10-07T00:00:00Z", "action": "offered", "actor": "producer",
                                "previous_contract_id": c}]
    text = json.dumps(board(cross))
    assert "session" not in text and "repo" not in text and "evidence" not in text
    # Settled contracts leave the board after 24 hours; open ones stay.
    assert [r["contract_id"] for r in board(cross, "2026-10-08T00:00:01Z")] == [moved]


def test_failed_check_names_fit_the_delivery_bounds():
    names = ["Matrix / job " + str(n) for n in range(20)] + ["x" * 300, "two\nlines"]
    shown = collaboration_inbox.shown_checks({"failed_checks": names, "failed_checks_omitted": 3})
    assert len(shown["failed_checks"]) == collaboration_inbox.CHECKS_SHOWN
    assert all(len(name) <= collaboration_inbox.CHECK_NAME and "\n" not in name for name in shown["failed_checks"])
    assert shown["failed_checks_omitted"] == 3 + len(names) - collaboration_inbox.CHECKS_SHOWN
    assert collaboration_inbox.shown_checks({"failed_checks": ["y" * 300]}) == {"failed_checks": ["y" * 199 + "…"]}


def test_purpose_and_reason_summaries_are_one_bounded_line():
    assert collaboration_inbox.summary("\n  first line \nsecond") == "first line"
    long = collaboration_inbox.summary("x" * 200)
    assert len(long) == 80 and long.endswith("…")


def test_completed_contract_stays_on_the_board_for_adoption(cross):
    cross.adopt(graph(cross))
    c = original(offer(cross))["contract_id"]
    original(respond(cross, c, "accept", handle="unattended"))
    original(respond(cross, c, "complete", handle="interactive-proof", evidence=[PROOF.reference()]))
    [row] = board(cross)
    assert (row["status"], row["settled"], row["next"]["actions"]) == ("completed", True, ["adopt"])


def hestia_work(cross, name="hestia"):
    """A WI that the recipient Agent holds from its interactive session."""
    from dataclasses import replace
    from harness_rewrite import Command, SourceInput
    cross.contexts[f"{name}-create"] = replace(
        cross.contexts["interactive"], generation=0,
        source=SourceInput(f"{name}-create", "native_agent", b"Create hestia work"))
    created = cross.kernel.execute(Command(f"{name}-create", "create_lightweight", {
        "summary": f"Hestia's own work {name}.", "why": "Handle a main-only request",
        "what": "Answer it with the human", "acceptance": ["Answered"],
        "owner": "hestia", "resources": ["repo"]}), f"{name}-create")
    wi = created["work_item"]["id"]
    started = cross.kernel.execute(Command(f"{name}-start", "start_work", {}, wi), "interactive")
    assert started["status"] == "local_committed", started
    return wi


def offer_targeted(cross, target, main_only=True, principal=None):
    return cross.send("offer_collaboration", {
        "kind": "request",
        "recipients": [{**(principal or {"actor": "hestia"}), "resources": ["repo"],
                        "target_work_item_id": target}],
        "purpose": "Decide with the human",
        "required": True,
        "main_only": main_only,
    })


def recorded_target(cross, contract_id):
    with cross.kernel._connection(write=False, work_item_id=cross.wi) as conn:
        state, _, _, _ = cross.kernel._replay(conn, "team", cross.wi)
    [recipient] = state["collaborations"][contract_id]["recipients"]
    return recipient.get("target_work_item_id")


def test_main_only_requires_an_explicit_choice_with_the_rule(cross):
    cross.adopt(graph(cross))
    refused = cross.send("offer_collaboration", {
        "kind": "request", "recipients": [{"actor": "hestia", "resources": ["repo"]}],
        "purpose": "Decide", "required": True})
    assert error(refused) == "invalid_input"
    reason = refused["error"]["reason"]
    assert "true" in reason and "false" in reason and "unmerged changes" in reason
    assert "只读" in reason and "target_work_item_id" in reason


def test_untargeted_main_only_contract_without_a_held_wi_is_refused_with_its_remedy(cross):
    # Delivery reaches a main session only through the recipient's WI; without
    # one the contract would wait in the inbox with nobody told.
    cross.adopt(graph(cross))
    refused = offer(cross, main_only=True)
    assert error(refused) == "invalid_input"
    reason = refused["error"]["reason"]
    assert "holds no active WI" in reason and "target_work_item_id" in reason
    assert "exact session" in reason and "main_only: false" not in reason
    original(offer(cross, main_only=False))
    original(offer_targeted(cross, "wi_20261001_001"))


def test_untargeted_main_only_contract_takes_the_one_wi_the_recipient_holds(cross):
    cross.adopt(graph(cross))
    target = hestia_work(cross)
    c = original(offer(cross, main_only=True))["contract_id"]
    assert recorded_target(cross, c) == target
    storage = SimpleNamespace(connection=cross.kernel._connection)
    item = next(i for i in collaboration_inbox.inbox(storage, "team", "hestia", "ownermac")["items"]
                if i["contract_id"] == c)
    assert item["target"]["holder"] == "recipient_here"
    # A main-only reassignment to the same Agent resolves the same way.
    moved = original(cross.send("reassign_collaboration", {
        "contract_id": c, "recipients": [{"actor": "hestia", "resources": ["repo"]}],
        "reason": "Resend"}))["contract_id"]
    assert recorded_target(cross, moved) == target


def test_untargeted_main_only_contract_lists_the_candidates_when_several(cross):
    cross.adopt(graph(cross))
    first, second = hestia_work(cross, "one"), hestia_work(cross, "two")
    refused = offer(cross, main_only=True)
    assert error(refused) == "invalid_input"
    reason = refused["error"]["reason"]
    assert "2 active WIs" in reason and first in reason and second in reason
    assert "Hestia's own work one" in reason
    c = original(offer_targeted(cross, second))["contract_id"]
    assert recorded_target(cross, c) == second


def test_main_only_target_resolves_the_recipients_holding_session(cross):
    cross.adopt(graph(cross))
    target = hestia_work(cross)
    c = original(offer_targeted(cross, target))["contract_id"]
    storage = SimpleNamespace(connection=cross.kernel._connection)
    here = collaboration_inbox.inbox(storage, "team", "hestia", "ownermac")
    item = next(i for i in here["items"] if i["contract_id"] == c)
    assert item["target"] == {
        "work_item_id": target, "holder": "recipient_here",
        "holder_key": collaboration_inbox.holder_key("hestia-interactive"),
    }
    text = json.dumps(here)
    assert "hestia-interactive" not in text and "session" not in text
    elsewhere = collaboration_inbox.inbox(storage, "team", "hestia", "tcloud")
    assert next(i for i in elsewhere["items"] if i["contract_id"] == c)["target"] == {
        "work_item_id": target, "holder": "recipient_elsewhere"}
    # A scheduled wake reaches the same session through the held list.
    assert {"work_item_id": target, "holder": "recipient_here",
            "holder_key": collaboration_inbox.holder_key("hestia-interactive")} in here["held"]
    assert {"work_item_id": target, "holder": "recipient_elsewhere"} in elsewhere["held"]
    assert all(row["work_item_id"] != target
               for row in collaboration_inbox.inbox(storage, "team", "producer", "ownermac")["held"])


def test_target_without_holder_or_held_by_another_agent_is_not_delivered(cross):
    cross.adopt(graph(cross))
    c = original(offer_targeted(cross, "wi_20990101_001"))["contract_id"]
    storage = SimpleNamespace(connection=cross.kernel._connection)
    item = collaboration_inbox.inbox(storage, "team", "hestia", "ownermac")["items"][0]
    assert item["contract_id"] == c
    assert item["target"] == {"work_item_id": "wi_20990101_001", "holder": "absent"}
    holders = {"wi_20990101_002": {"actor": "producer", "session": "s", "surface": "ownermac"}}
    assert collaboration_inbox.target("wi_20990101_002", "hestia", "ownermac", holders, set()) == {
        "work_item_id": "wi_20990101_002", "holder": "other"}
    assert collaboration_inbox.target("wi_20990101_002", "hestia", "ownermac", holders,
                                      {"wi_20990101_002"})["holder"] == "unavailable"


def test_target_is_only_for_an_agent_recipient_of_a_main_only_contract(cross):
    cross.adopt(graph(cross))
    assert error(offer_targeted(cross, "wi_20990101_001", main_only=False)) == "invalid_input"
    assert error(offer_targeted(cross, cross.wi)) == "invalid_input"
    assert error(offer_targeted(cross, "not-a-wi")) == "invalid_input"
    exact = {"principal": {"actor": "hestia", "session": "hestia-interactive"}}
    assert error(offer_targeted(cross, "wi_20990101_001", principal=exact)) == "invalid_input"
    plain = original(offer(cross))["contract_id"]
    assert "target" not in read(cross)["items"][0] and read(cross)["items"][0]["contract_id"] == plain


def test_recipient_may_use_the_shape_observations_show(cross):
    """{actor, resources} as listed in collaborations is accepted as an Agent recipient."""
    cross.adopt(graph(cross))
    c = original(cross.send("offer_collaboration", {
        "kind": "request", "recipients": [{"actor": "hestia", "resources": ["repo"]}],
        "purpose": "Review the rebuilt skill", "required": False, "main_only": False,
    }))["contract_id"]
    assert read(cross)["items"][0]["contract_id"] == c


def holding_session(r):
    with r.kernel._connection(write=False, work_item_id=r.wi) as conn:
        state, _, _, _ = r.kernel._replay(conn, "team", r.wi)
    return state["holder"]


def test_answered_contract_reaches_the_requester_until_it_settles(cross):
    # The requester settles on the WI it offered from; only its holder writes.
    cross.adopt(graph(cross))
    c = original(offer(cross))["contract_id"]
    assert read(cross, "producer")["items"] == []
    original(respond(cross, c, "accept", handle="unattended"))
    assert read(cross, "producer")["items"] == []
    original(
        respond(cross, c, "complete", handle="interactive-proof", evidence=[PROOF.reference()])
    )
    assert read(cross)["items"] == []
    holder = holding_session(cross)
    storage = SimpleNamespace(connection=cross.kernel._connection)
    here = collaboration_inbox.inbox(storage, "team", "producer", holder["surface"])
    assert here["items"] == [
        {
            "role": "requester",
            "work_item_id": cross.wi,
            "contract_id": c,
            "kind": "request",
            "status": "completed",
            "main_only": False,
            "offered_by": "producer",
            "offered_at": "2026-10-07T00:00:00Z",
            "answered_by": ["hestia"],
            "options": ["adopt"],
            "target": {
                "work_item_id": cross.wi,
                "holder": "recipient_here",
                "holder_key": collaboration_inbox.holder_key(holder["session"]),
            },
        }
    ]
    assert holder["session"] not in json.dumps(here)
    [elsewhere] = collaboration_inbox.inbox(storage, "team", "producer", "tcloud")["items"]
    assert elsewhere["target"] == {"work_item_id": cross.wi, "holder": "recipient_elsewhere"}
    # The board is unchanged: the requester still acts next.
    assert board(cross)[0]["next"] == {"role": "requester", "actors": ["producer"],
                                       "actions": ["adopt"]}
    original(cross.send("withdraw_collaboration", {"contract_id": c, "reason": "Adopted the reading"}))
    assert read(cross, "producer")["items"] == []


def test_returned_contract_offers_the_requester_its_options_and_reassignment_settles_it(cross):
    cross.adopt(graph(cross))
    c = original(offer(cross))["contract_id"]
    original(respond(cross, c, "accept", handle="unattended"))
    original(respond(cross, c, "return", handle="interactive"))
    [item] = read(cross, "producer")["items"]
    assert (item["role"], item["status"], item["answered_by"], item["options"]) == (
        "requester", "returned", ["hestia"], ["resend", "reassign", "withdraw", "adopt_partial"])
    moved = original(cross.send("reassign_collaboration", {
        "contract_id": c, "recipients": [{"principal": {"actor": "third"}, "resources": ["repo"]}],
        "reason": "Hestia is busy"}))["contract_id"]
    assert read(cross, "producer")["items"] == []
    [owed] = read(cross, "third")["items"]
    assert (owed["role"], owed["contract_id"]) == ("recipient", moved)


def test_rejected_main_only_contract_reaches_the_requester_as_main_only(cross):
    cross.adopt(graph(cross))
    target = hestia_work(cross)
    c = original(offer_targeted(cross, target))["contract_id"]
    original(respond(cross, c, "reject", handle="interactive"))
    [item] = read(cross, "producer")["items"]
    assert (item["role"], item["main_only"], item["status"], item["options"]) == (
        "requester", True, "rejected", ["reassign", "withdraw"])
    assert item["target"]["work_item_id"] == cross.wi
    assert all(i["contract_id"] != c for i in read(cross)["items"])


def results(rig, now="2026-10-07T01:00:00Z", actor="producer", surface="local"):
    storage = SimpleNamespace(connection=rig.kernel._connection)
    return collaboration_inbox.inbox(storage, "team", actor, surface, now)["pull_requests"]


def test_pull_request_results_reach_the_holder_of_the_registering_wi(tmp_path):
    from tests_rewrite import test_pull_requests as prs

    rig, candidate = prs.rig_with_pr(tmp_path)
    assert results(rig) == []  # Awaiting review: nothing for the author to do.
    original(prs.review(rig, candidate, 50))
    [row] = results(rig)
    assert row["target"]["holder"] == "recipient_here" and len(row["target"]["holder_key"]) == 64
    # The registering session made the head; here it also holds the WI.
    assert row.pop("author_key") == row.pop("target")["holder_key"]
    assert row.pop("author_ended") is False
    assert row == {
        "role": "submitter", "work_item_id": rig.wi, "candidate_id": candidate,
        "repository": prs.REPO, "number": 7, "status": "changes_requested",
        "head_sha": prs.HEAD, "at": "2026-10-07T00:00:00Z",
        "next": {"intent": "submit_pull_request", "agent_action":
                 "Revise the pull request per the review and register the new head"},
        "review": {"score": 50, "summary": "Read the diff"},
    }
    assert results(rig, actor="hestia") == []
    assert results(rig, now="2026-10-08T00:00:01Z") == []  # Older than 24 hours.
    second = original(prs.submit(rig, prs.NEXT))["candidate_id"]
    original(prs.review(rig, second, 92))
    prs.drain(rig, prs.FakeGitHub(head=prs.NEXT, runs=[("harness", "completed", "failure")]))
    [row] = results(rig)
    assert (row["candidate_id"], row["status"]) == (second, "checks_failed")
    assert row["next"]["intent"] == "submit_pull_request"
    assert row["next"]["failed_checks"] == ["Harness CI / harness: failure"]
    third = original(prs.submit(rig, "c" * 40))["candidate_id"]
    original(prs.review(rig, third, 95))
    prs.drain(rig, prs.FakeGitHub(head="c" * 40))
    [row] = results(rig)
    assert (row["candidate_id"], row["status"], row["head_sha"]) == (third, "merged", "c" * 40)
    assert "next" not in row and "review" not in row


def test_a_child_s_pull_request_result_follows_its_parent_s_holder(tmp_path):
    from dataclasses import replace

    from tests_rewrite import test_children as kids, test_pull_requests as prs
    from tests_rewrite.test_workflow import OTHER

    rig = kids.light(tmp_path)
    parent = rig.wi
    child = original(kids.spawn(rig))["work_item_id"]
    rig.wi = child
    candidate = original(prs.submit(rig, prs.HEAD))["candidate_id"]
    original(prs.review(rig, candidate, 50))
    rig.wi = parent
    [row] = results(rig)
    assert (row["work_item_id"], row["status"]) == (child, "changes_requested")
    assert row["target"]["holder"] == "recipient_here"
    before = row["target"]["holder_key"]
    # Continuing the parent in another session moves the child's delivery there.
    here = replace(kids.FULL, session="feishu-session")
    original(rig.approve("continue_here", {"reason": "continue here"}, actor=here))
    [row] = results(rig)
    assert row["target"]["holder"] == "recipient_here" and row["target"]["holder_key"] != before
    # Another session continuing the parent does not end the registering one:
    # the result still names only it.
    assert (row["author_key"], row["author_ended"]) == (before, False)
    # While the parent is in transfer nobody holds the child: nothing is deliverable.
    rig.contexts["here"] = replace(here, generation=kids.view(rig, parent)["generation"])
    original(rig.send("rollover", {"target": OTHER.principal(), "summary": "Resume"}, handle="here"))
    [row] = results(rig)
    assert row["target"]["holder"] != "recipient_here" and "holder_key" not in row["target"]
    # The continuing session rolled over, not the registering one.
    assert (row["author_key"], row["author_ended"]) == (before, False)


def test_a_pull_request_result_names_the_registering_session_until_it_rolls_over(tmp_path):
    from tests_rewrite import test_pull_requests as prs
    from tests_rewrite.test_workflow import OTHER

    rig, candidate = prs.rig_with_pr(tmp_path)
    original(prs.review(rig, candidate, 50))
    [row] = results(rig)
    author = row["author_key"]
    # Another node of the same Agent sees no key and no end: it must not deliver.
    [elsewhere] = results(rig, surface="remote")
    assert ("author_key" not in elsewhere, elsewhere["author_ended"]) == (True, False)
    assert elsewhere["target"]["holder"] == "recipient_elsewhere"
    # The registering session hands the WI over (rollover): the journal shows it ended.
    transfer = original(rig.send("rollover", {"target": OTHER.principal(), "summary": "Resume"},
                                 handle="owner"))["transfer_id"]
    assert results(rig) == []  # In transfer the WI is not open: nothing is listed.
    original(rig.send("accept_transfer", {"transfer_id": transfer}, handle="helper"))
    [row] = results(rig)
    assert (row["author_ended"], "author_key" in row) == (True, False)
    assert author not in row.values()


def test_a_scheduler_run_s_pull_request_result_goes_to_no_session(tmp_path):
    # A scheduler run is a fresh native session that never enters a conversation,
    # and its result is not the holder's to answer: the row is marked unattended,
    # not ended, and carries no key, so no gate delivers it.
    from dataclasses import replace

    from tests_rewrite import test_pull_requests as prs

    rig = prs.light(tmp_path)
    rig.contexts["reviewer"] = prs.REVIEWER
    run = replace(prs.AUTHOR, session="private-scheduled-run", entry="scheduler")
    candidate = original(prs.submit(rig, prs.HEAD, run))["candidate_id"]
    original(prs.review(rig, candidate, 50))
    [row] = results(rig)
    assert (row["author_unattended"], row["author_ended"], "author_key" in row) == (True, False, False)
    assert row["target"]["holder"] == "recipient_here"


def test_the_inbox_keeps_only_the_handoff_facts_of_each_journal():
    entry = {"at": "2026-10-07T00:00:00Z", "request_id": "r", "semantics": {"principal": {"session": "s"}},
             "result": {}, "events": [{"kind": "CheckpointRecorded", "payload": {"large": "x" * 4096}}]}
    assert collaboration_inbox.handoffs([entry] * 50) == {"transfers": [], "switches": [], "entered": set()}


def test_a_round_registered_before_entries_were_recorded_reaches_the_holder(tmp_path, monkeypatch):
    # A round from an older ledger has no `author_entry`: it may be a scheduler
    # run nothing can reach, so the holder hears of it rather than nobody.
    from tests_rewrite import test_pull_requests as prs

    rig, candidate = prs.rig_with_pr(tmp_path)
    original(prs.review(rig, candidate, 50))
    [row] = results(rig)
    assert row["author_ended"] is False and "author_key" in row
    records = collaboration_inbox.pull_requests.records

    def older(state):
        found = records(state)
        for pr in found.values():
            for item in pr["rounds"]:
                assert item.pop("author_entry", "absent") is None  # Recorded now: a conversation.
        return found

    monkeypatch.setattr(collaboration_inbox.pull_requests, "records", older)
    [row] = results(rig)
    assert (row["author_ended"], "author_key" in row) == (True, False)
