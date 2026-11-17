"""AH-088 / A-41: one Agent's session asks another session of the same Agent.

The requesting side (the offering session, or the same Agent's session now holding
the WI) never accepts, rejects, returns or completes its own contract.
"""

from dataclasses import replace
import json

import pytest

from harness_rewrite import Query
from harness_rewrite import collaboration
from tests_rewrite.test_collaboration import rig as collaboration_rig, respond
from tests_rewrite.test_cross_node_collaboration import journal, offer
from tests_rewrite.test_review import PROOF
from tests_rewrite.test_workflow import graph, node, original, error

rig = collaboration_rig
SELF = {"actor": "producer"}


@pytest.fixture
def same(rig):
    base = rig.contexts["owner"]
    for handle, session, surface in (
        ("sibling", "producer-sibling", "ownermac"),
        ("later", "producer-later", "local"),
    ):
        rig.contexts[handle] = replace(base, session=session, surface=surface)
        rig.contexts[handle + "-proof"] = replace(
            rig.contexts[handle], artifacts=(PROOF,)
        )
    rig.contexts["owner-proof"] = replace(base, artifacts=(PROOF,))
    return rig


def test_request_to_own_agent_is_answered_by_another_session_only(same):
    same.adopt(graph(same))
    c = original(offer(same, principal=SELF))["contract_id"]
    assert error(respond(same, c, "accept")) == "authority_denied"
    assert error(respond(same, c, "reject")) == "authority_denied"
    original(respond(same, c, "accept", handle="sibling"))
    # The bearer's peers settle with evidence; the requester never does.
    assert (
        error(
            respond(same, c, "complete", handle="owner-proof", evidence=[PROOF.reference()])
        )
        == "authority_denied"
    )
    assert error(respond(same, c, "return")) == "authority_denied"
    original(
        respond(same, c, "complete", handle="later-proof", evidence=[PROOF.reference()])
    )
    state, responded = journal(same)
    item = state["collaborations"][c]
    assert item["offered_by"] == same.contexts["owner"].principal()
    assert item["responses"]["producer"]["principal"] == same.contexts["sibling"].principal()
    assert [(e["decision"], e["principal"]["session"]) for e in responded] == [
        ("accept", "producer-sibling"),
        ("complete", "producer-later"),
    ]
    public = same.state()["result"]["current_observation"]["collaborations"][0]
    assert public["offered_by"] == "producer"
    assert public["recipients"] == [{"actor": "producer", "resources": ["repo"]}]
    assert public["responses"]["producer"]["status"] == "completed"
    assert "producer-sibling" not in json.dumps(public)


def test_delegation_to_own_agent_moves_the_scope_to_the_accepting_session(same):
    same.adopt(graph(same, [node("work", role="collaborator")]))
    c = original(offer(same, "delegate", principal=SELF))["contract_id"]
    grant = original(respond(same, c, "accept", handle="sibling"))["grant_id"]
    state, _ = journal(same)
    assert state["grants"][grant]["principal"] == same.contexts["sibling"].principal()
    # The holder no longer writes the delegated scope concurrently.
    assert error(same.claim("work")) == "authority_denied"
    original(same.claim("work", handle="sibling"))
    assert error(respond(same, c, "return")) == "authority_denied"
    original(respond(same, c, "return", handle="later"))
    state, _ = journal(same)
    assert state["grants"][grant]["active"] is False


def test_exact_recipient_must_differ_from_the_offering_session(same):
    same.adopt(graph(same))
    assert (
        error(offer(same, principal=same.contexts["owner"].principal()))
        == "invalid_input"
    )
    exact = same.contexts["sibling"].principal()
    c = original(offer(same, principal=exact))["contract_id"]
    assert error(respond(same, c, "accept", handle="later")) == "authority_denied"
    original(respond(same, c, "accept", handle="sibling"))


def test_responsibility_within_one_agent_still_moves_by_rollover(same):
    same.adopt(graph(same))
    local = replace(same.contexts["owner"], session="producer-successor").principal()
    assert error(offer(same, "responsibility", principal=local)) == "invalid_input"
    assert error(offer(same, "responsibility", principal=SELF)) == "invalid_input"


def test_mixed_recipients_keep_one_answer_per_agent(same):
    same.adopt(graph(same))
    helper = {"principal": same.contexts["helper"].principal(), "resources": ["repo"]}
    mine = {"principal": SELF, "resources": ["repo"]}
    request = {"kind": "request", "purpose": "Two readings", "required": True, "main_only": False}
    c = original(
        same.send("offer_collaboration", {**request, "recipients": [mine, helper]})
    )["contract_id"]
    assert error(
        same.send("offer_collaboration", {**request, "recipients": [mine, mine]})
    ) == "invalid_input"
    original(respond(same, c, "accept", handle="helper"))
    original(respond(same, c, "reject", handle="sibling"))
    state, _ = journal(same)
    assert {k: v["status"] for k, v in state["collaborations"][c]["responses"].items()} == {
        "producer": "rejected",
        "helper": "accepted",
    }


def test_only_other_sessions_are_pointed_at_the_contract(same):
    same.adopt(graph(same))
    c = original(offer(same, principal=SELF))["contract_id"]
    mine = same.kernel.inspect(Query(view="list"), "owner")["result"]["items"]
    assert mine[0]["awaiting_response"] == []
    assert same.state()["next_action"]["intent"] != "respond_collaboration"
    sibling = same.kernel.inspect(Query(view="list"), "sibling")["result"]["items"]
    assert sibling[0]["awaiting_response"] == [
        {"contract_id": c, "kind": "request", "status": "pending", "required": True,
         "main_only": False}
    ]
    assert same.state(handle="sibling")["next_action"]["contract_id"] == c


def test_the_holder_continuing_the_offer_is_on_the_requesting_side():
    offered = {"actor": "producer", "session": "first", "surface": "local"}
    successor = {**offered, "session": "successor"}
    item = {"offered_by": offered}
    assert collaboration.requesting({"holder": successor}, item, successor)
    assert collaboration.requesting({"holder": successor}, item, offered)
    peer = {**offered, "session": "peer"}
    assert not collaboration.requesting({"holder": successor}, item, peer)
    # Another Agent holding the WI is a counterparty, not the requester.
    other = {"actor": "helper", "session": "h", "surface": "local"}
    assert not collaboration.requesting({"holder": other}, item, other)
