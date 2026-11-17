"""An Agent's own /wi switch: the holder asks, the Host-attested target session continues."""

from __future__ import annotations

from dataclasses import replace

import pytest

from harness_rewrite import Command, Kernel, Query, SourceInput
from harness_rewrite.model import digest
from tests_rewrite.test_kernel import INPUT, OTHER, OWNER, Host, checkpoint, code, start

EXTRA = {"request_switch", "continue_here", "create_child", "start_work"}


def create(kernel, request, *, draft=False, handle="create", owner="producer"):
    intent = "create_draft" if draft else "create_lightweight"
    result = kernel.execute(Command(request, intent, {**INPUT, "owner": owner}), handle)
    assert result["status"] == "local_committed", result
    return result["work_item"]["id"]


@pytest.fixture
def rig(tmp_path):
    host = Host()
    for name in ("owner", "other"):
        host.contexts[name] = replace(
            host.contexts[name], allowed_intents=host.contexts[name].allowed_intents | EXTRA
        )
    host.contexts["sibling"] = replace(host.contexts["owner"], session="private-session-sibling")
    kernel = Kernel(tmp_path / "facts.sqlite", host.authenticate)
    wi = start(kernel)
    # The same Agent's other root WI, held by its sibling session.
    other = create(kernel, "create-other")
    assert kernel.execute(Command("start-other", "start_work", {}, other), "sibling")["status"] == (
        "local_committed"
    )
    return kernel, host, wi, other


def ask(kernel, wi, target, handle="owner", request="ask"):
    data = {"target": target, "summary": "Review returned; continue the other WI"}
    return kernel.execute(Command(request, "request_switch", data, wi), handle)


def test_holder_may_request_a_switch_and_nothing_moves(rig):
    kernel, _, wi, other = rig
    assert kernel.execute(checkpoint(wi), "owner")["status"] == "local_committed"
    asked = ask(kernel, wi, other)
    assert asked["status"] == "local_committed", asked
    request = asked["result"]["original_request_result"]["switch_request"]
    assert request["source"] == OWNER.principal() and request["target"] == other
    assert kernel.inspect(Query(other), "owner")["work_item"]["generation"] == 1
    # The left WI stays held here; the request records nothing after the checkpoint.
    assert kernel.execute(checkpoint(wi, "after"), "owner")["status"] == "local_committed"


def test_request_needs_the_own_checkpoint_last_the_holder_and_the_same_agent(rig):
    kernel, _, wi, other = rig
    assert code(ask(kernel, wi, other, request="none")) == "domain_conflict"
    assert kernel.execute(checkpoint(wi, "sibling-note"), "sibling")["status"] == "local_committed"
    refused = ask(kernel, wi, other, request="sibling-frontier")
    assert code(refused) == "domain_conflict" and "request_switch" in refused["error"]["reason"]
    assert kernel.execute(checkpoint(wi), "owner")["status"] == "local_committed"
    assert code(ask(kernel, wi, other, "other", "thief")) == "authority_denied"
    assert code(ask(kernel, wi, other, "sibling", "sibling-ask")) == "authority_denied"
    assert ask(kernel, wi, other, request="ready")["status"] == "local_committed"


def test_this_wi_or_its_child_needs_no_switch_and_no_checkpoint(rig):
    kernel, host, wi, _ = rig
    host.contexts["owner"] = replace(
        host.contexts["owner"], source=SourceInput("child", "native_agent", b"Create child")
    )
    child_input = {k: v for k, v in INPUT.items() if k != "owner"} | {"parent": wi}
    created = kernel.execute(Command("child", "create_child", child_input), "owner")
    child = created["result"]["original_request_result"]["work_item_id"]
    host.contexts["owner"] = replace(host.contexts["owner"], source=None)
    for target, words in ((child, "子 WI"), (wi, "就是本会话")):
        answer = ask(kernel, wi, target, request="ask-" + target)
        assert answer["status"] == "local_committed", answer
        result = answer["result"]["original_request_result"]
        assert "switch_request" not in result and words in result["no_switch"]
    # A child names its parent's holder; the request is made on the parent.
    assert code(ask(kernel, child, wi, request="on-child")) == "domain_conflict"


def test_targets_that_cannot_take_the_conversation_are_refused(rig):
    kernel, host, wi, other = rig
    assert kernel.execute(checkpoint(wi), "owner")["status"] == "local_committed"
    assert "does not exist" in ask(kernel, wi, "wi_20990101_404", request="missing")["error"]["reason"]
    foreign = create(kernel, "own-draft", draft=True)  # The owner's draft is enterable.
    assert ask(kernel, wi, foreign, request="draft")["status"] == "local_committed"
    host.contexts["create-other"] = replace(host.contexts["create"], actor="collaborator")
    theirs = create(kernel, "theirs", handle="create-other", owner="collaborator")
    assert code(ask(kernel, wi, theirs, request="their-draft")) == "authority_denied"
    assert kernel.execute(Command("their-start", "start_work", {}, theirs), "other")["status"] == (
        "local_committed"
    )
    assert code(ask(kernel, wi, theirs, request="their-active")) == "authority_denied"
    parked = {"reason": "parked"}
    host.contexts["human"] = replace(host.contexts["sibling"], source=SourceInput(
        "human-stop", "native_human", b"/wi stop", "stop",
        digest({"intent": "stop", "work_item_id": other, "input": parked})))
    stopped = kernel.execute(Command("stop", "stop", parked, other), "human")
    assert stopped["status"] == "local_committed", stopped
    assert "only active work" in ask(kernel, wi, other, request="stopped")["error"]["reason"]


def test_continue_here_accepts_the_host_attested_switch_of_the_same_agent_only(rig):
    kernel, host, wi, other = rig
    assert kernel.execute(checkpoint(wi), "owner")["status"] == "local_committed"
    assert ask(kernel, wi, other)["status"] == "local_committed"
    proof = {"source": OWNER.principal(), "source_wi": wi, "request_id": "ask", "target": other}
    fresh = replace(host.contexts["owner"], session="private-session-fresh")
    enter = Command("enter", "continue_here", {"reason": "Agent switch"}, other)
    host.contexts["plain"] = fresh
    assert code(kernel.execute(enter, "plain")) == "source_invalid"  # No human, no attested switch.
    host.contexts["elsewhere"] = replace(fresh, session_switch={**proof, "target": wi})
    assert code(kernel.execute(Command("e2", "continue_here", {"reason": "x"}, other), "elsewhere")) == (
        "authority_denied"
    )
    host.contexts["thief"] = replace(OTHER, allowed_intents=fresh.allowed_intents, session_switch=proof)
    assert code(kernel.execute(Command("e3", "continue_here", {"reason": "x"}, other), "thief")) == (
        "authority_denied"
    )
    host.contexts["fresh"] = replace(fresh, session_switch=proof)
    entered = kernel.execute(Command("e4", "continue_here", {"reason": "Agent switch"}, other), "fresh")
    assert entered["status"] == "local_committed", entered
    assert entered["work_item"]["generation"] == 2
    # The left WI is still held by the session that asked.
    assert kernel.execute(checkpoint(wi, "back"), "owner")["status"] == "local_committed"
