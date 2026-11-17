"""AH-088 / A-41: Agent recipients across surfaces through the kernel."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest

from harness_rewrite import Command, Kernel, Query, collaboration
from tests_rewrite.test_collaboration import rig as collaboration_rig, respond
from tests_rewrite.test_review import PROOF
from tests_rewrite.test_workflow import DEFINITION, graph, node, original, error

rig = collaboration_rig
AGENT = {"actor": "hestia"}


@pytest.fixture
def cross(rig):
    base = rig.contexts["helper"]
    rig.contexts["interactive"] = replace(
        base, actor="hestia", session="hestia-interactive", surface="ownermac"
    )
    rig.contexts["unattended"] = replace(
        base, actor="hestia", session="hestia-unattended", surface="ownermac"
    )
    rig.contexts["unattended-proof"] = replace(
        rig.contexts["unattended"], artifacts=(PROOF,)
    )
    rig.contexts["interactive-proof"] = replace(
        rig.contexts["interactive"], artifacts=(PROOF,)
    )
    return rig


def offer(r, kind="request", principal=None, resources=("repo",), main_only=False):
    return r.send(
        "offer_collaboration",
        {
            "kind": kind,
            "recipients": [
                {"principal": principal or AGENT, "resources": list(resources)}
            ],
            "purpose": "Read the living-room temperature",
            "required": True,
            "main_only": main_only,
        },
    )


def journal(r):
    with r.kernel._connection(write=False, work_item_id=r.wi) as conn:
        state, _, _, history = r.kernel._replay(conn, "team", r.wi)
    responded = [
        e["payload"]
        for entry in history
        for e in entry["events"]
        if e["kind"] == "CollaborationResponded"
    ]
    return state, responded


def contract_view(r, handle="owner"):
    return r.state(handle=handle)["result"]["current_observation"]["collaborations"]


def test_agent_recipient_on_another_surface_answers_from_any_session(cross):
    cross.adopt(graph(cross))
    c = original(offer(cross))["contract_id"]
    assert error(respond(cross, c, "accept", handle="third")) == "authority_denied"
    original(respond(cross, c, "accept", handle="unattended"))
    assert error(respond(cross, c, "accept", handle="interactive")) == "domain_conflict"
    assert (
        error(respond(cross, c, "reject", handle="interactive")) == "domain_conflict"
    )
    original(
        respond(
            cross, c, "complete", handle="interactive-proof",
            evidence=[PROOF.reference()],
        )
    )
    state, responded = journal(cross)
    response = state["collaborations"][c]["responses"]["hestia"]
    assert response["status"] == "completed"
    assert response["principal"] == cross.contexts["unattended"].principal()
    assert [(e["decision"], e["principal"]["session"]) for e in responded] == [
        ("accept", "hestia-unattended"),
        ("complete", "hestia-interactive"),
    ]
    assert responded[1]["evidence"] == [PROOF.reference()]
    public = contract_view(cross)
    assert public[0]["recipients"] == [{"actor": "hestia", "resources": ["repo"]}]
    assert "hestia-unattended" not in json.dumps(public)
    assert "hestia-interactive" not in json.dumps(public)


def test_agent_delegation_binds_the_grant_to_the_accepting_session(cross):
    cross.adopt(graph(cross, [node("work", role="collaborator")]))
    c = original(offer(cross, "delegate"))["contract_id"]
    grant = original(respond(cross, c, "accept", handle="unattended"))["grant_id"]
    state, _ = journal(cross)
    assert state["grants"][grant]["principal"] == cross.contexts["unattended"].principal()
    assert error(cross.claim("work", handle="interactive")) == "authority_denied"
    original(cross.claim("work", handle="unattended"))
    original(respond(cross, c, "return", handle="interactive"))
    state, responded = journal(cross)
    assert state["grants"][grant]["active"] is False
    assert responded[-1]["principal"] == cross.contexts["interactive"].principal()
    assert error(cross.claim("work", handle="unattended")) == "authority_denied"


def test_concurrent_acceptance_has_exactly_one_bearer(cross):
    cross.adopt(graph(cross, [node("work", role="collaborator")]))
    c = original(offer(cross, "delegate"))["contract_id"]
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda h: respond(cross, c, "accept", handle=h, request="race-" + h),
                ["interactive", "unattended"],
            )
        )
    assert sorted(r["status"] for r in results) == ["local_committed", "rejected"]
    winner = "interactive" if results[0]["status"] == "local_committed" else "unattended"
    state, responded = journal(cross)
    assert [e["principal"] for e in responded] == [cross.contexts[winner].principal()]
    assert sum(g["active"] for g in state["grants"].values()) == 1


def test_exact_principal_keeps_exact_session_and_now_crosses_surfaces(cross):
    cross.adopt(graph(cross))
    exact = cross.contexts["interactive"].principal()
    c = original(offer(cross, principal=exact))["contract_id"]
    assert error(respond(cross, c, "accept", handle="unattended")) == "authority_denied"
    original(respond(cross, c, "accept", handle="interactive"))
    assert (
        error(
            respond(
                cross, c, "complete", handle="unattended-proof",
                evidence=[PROOF.reference()],
            )
        )
        == "authority_denied"
    )
    original(
        respond(
            cross, c, "complete", handle="interactive-proof",
            evidence=[PROOF.reference()],
        )
    )


def test_full_responsibility_still_requires_one_exact_local_session(cross):
    cross.adopt(graph(cross))
    assert error(offer(cross, "responsibility")) == "invalid_input"
    remote = cross.contexts["interactive"].principal()
    assert error(offer(cross, "responsibility", principal=remote)) == "domain_conflict"
    for bad in (
        {"actor": "hestia", "session": "hestia-interactive"},
        {"actor": "hestia", "surface": "ownermac"},
        cross.contexts["owner"].principal(),
    ):
        assert error(offer(cross, principal=bad)) == "invalid_input"


def test_recipient_sessions_discover_pending_contracts_and_replay_matches(cross, tmp_path):
    cross.adopt(graph(cross))
    c = original(offer(cross))["contract_id"]
    listed = cross.kernel.inspect(Query(view="list"), "interactive")["result"]["items"]
    assert listed[0]["awaiting_response"] == [
        {"contract_id": c, "kind": "request", "status": "pending", "required": True,
         "main_only": False}
    ]
    owner_list = cross.kernel.inspect(Query(view="list"), "owner")["result"]["items"]
    assert owner_list[0]["awaiting_response"] == []
    inspected = cross.state(handle="unattended")
    assert inspected["next_action"] == {
        "intent": "respond_collaboration",
        "reason": "collaboration_pending",
        "contract_id": c,
    }
    assert cross.kernel.resume(cross.wi, "interactive")["next_action"]["contract_id"] == c
    assert cross.state()["next_action"]["intent"] != "respond_collaboration"
    original(respond(cross, c, "accept", handle="unattended"))
    listed = cross.kernel.inspect(Query(view="list"), "interactive")["result"]["items"]
    assert listed[0]["awaiting_response"][0]["status"] == "accepted"
    watch = cross.state(query=Query(cross.wi, view="watch"))["result"]["items"]["items"]
    row = next(i for i in watch if i["kind"] == "collaboration")
    assert row["recorded_status"] == "accepted" and row["executor"] is not None
    original(
        respond(
            cross, c, "complete", handle="unattended-proof",
            evidence=[PROOF.reference()],
        )
    )
    listed = cross.kernel.inspect(Query(view="list"), "interactive")["result"]["items"]
    assert listed[0]["awaiting_response"] == []
    replayed = Kernel(
        tmp_path / "journal.sqlite",
        cross.contexts.__getitem__,
        clock=lambda: "2026-10-07T00:00:00Z",
    )
    assert (
        replayed.inspect(Query(cross.wi), "owner")["result"]["current_observation"]
        == cross.state()["result"]["current_observation"]
    )


def test_requester_is_pointed_at_answers_until_it_settles(cross):
    cross.adopt(graph(cross))
    other = cross.kernel.execute(
        Command("second", "create_draft", DEFINITION), "create")["work_item"]["id"]
    # The answered WorkItem is listed first even when its ID sorts last.
    first, last = sorted([cross.wi, other])
    cross.wi = last
    if last == other:
        cross.contexts["owner"] = replace(cross.contexts["owner"], generation=0)
    c = original(offer(cross))["contract_id"]
    original(respond(cross, c, "reject", handle="unattended"))
    listed = cross.kernel.inspect(Query(view="list"), "owner")["result"]["items"]
    assert [i["id"] for i in listed] == [last, first]
    assert listed[0]["awaiting_settlement"] == [{
        "contract_id": c, "kind": "request", "status": "rejected",
        "answered_by": ["hestia"], "options": ["reassign", "withdraw"]}]
    action = cross.state()["next_action"]
    assert (action["intent"], action["reason"], action["contract_id"], action["options"]) == (
        "settle_collaboration", "collaboration_rejected", c, ["reassign", "withdraw"])
    # The recipient's Agent is not the requester.
    assert cross.state(handle="unattended")["next_action"]["intent"] != "settle_collaboration"
    original(cross.send("withdraw_collaboration", {"contract_id": c, "reason": "Done without it"}))
    assert cross.state()["next_action"]["intent"] != "settle_collaboration"
    listed = cross.kernel.inspect(Query(view="list"), "owner")["result"]["items"]
    assert all(i["awaiting_settlement"] == [] for i in listed)


def test_completed_contract_waits_for_adoption(cross):
    cross.adopt(graph(cross))
    c = original(offer(cross))["contract_id"]
    original(respond(cross, c, "accept", handle="unattended"))
    original(respond(cross, c, "complete", handle="interactive-proof", evidence=[PROOF.reference()]))
    assert cross.state()["next_action"]["options"] == ["adopt"]


def test_every_offer_declares_main_only(cross):
    cross.adopt(graph(cross))
    base = {
        "kind": "request",
        "recipients": [{"principal": AGENT, "resources": ["repo"]}],
        "purpose": "Read the living-room temperature",
        "required": True,
    }
    for data in (base, {**base, "main_only": "yes"}):
        response = cross.send("offer_collaboration", data)
        assert error(response) == "invalid_input"
        assert "main_only" in response["error"]["reason"]


def test_main_only_contract_waits_for_an_attended_session(cross):
    cross.adopt(graph(cross))
    cross.contexts["scheduled"] = replace(cross.contexts["unattended"], entry="scheduler")
    plain = original(offer(cross))["contract_id"]
    main = original(offer(cross, principal={"actor": "third"}))["contract_id"]
    main = original(
        cross.send(
            "reassign_collaboration",
            {"contract_id": main, "recipients": [{"principal": AGENT, "resources": ["repo"],
                                                    "target_work_item_id": "wi_20261001_001"}],
             "reason": "Needs the phone and the human", "main_only": True},
        )
    )["contract_id"]
    listed = cross.kernel.inspect(Query(view="list"), "interactive")["result"]["items"]
    assert [(o["contract_id"], o["main_only"]) for o in listed[0]["awaiting_response"]] == [
        (main, True), (plain, False)]
    assert cross.state(handle="interactive")["next_action"] == {
        "intent": "respond_collaboration",
        "reason": "collaboration_pending",
        "contract_id": main,
        "main_only": True,
        "agent_action": "主身合同：在与人类的会话中处理",
    }
    denied = respond(cross, main, "accept", handle="scheduled")
    assert error(denied) == "authority_denied" and "main_only" in denied["error"]["reason"]
    original(respond(cross, plain, "accept", handle="scheduled"))
    original(respond(cross, main, "accept", handle="interactive"))
    assert error(respond(cross, main, "return", handle="scheduled")) == "authority_denied"
    # A reassignment keeps main_only unless the requester restates it.
    original(respond(cross, main, "return", handle="interactive"))
    kept = original(
        cross.send(
            "reassign_collaboration",
            {"contract_id": main, "recipients": [{"principal": AGENT, "resources": ["repo"],
                                                    "target_work_item_id": "wi_20261001_001"}],
             "reason": "Resend"},
        )
    )["contract_id"]
    views = {c["contract_id"]: c["main_only"] for c in contract_view(cross)}
    assert views[kept] is True and views[plain] is False


def test_contracts_offered_before_main_only_read_as_unattended(cross):
    cross.adopt(graph(cross))
    c = original(offer(cross))["contract_id"]
    with cross.kernel._connection(write=False, work_item_id=cross.wi) as conn:
        state, _, _, _ = cross.kernel._replay(conn, "team", cross.wi)
    del state["collaborations"][c]["main_only"]
    assert collaboration.awaiting(state, "hestia")[0]["main_only"] is False
    assert collaboration.public_contracts(state)[0]["main_only"] is False
