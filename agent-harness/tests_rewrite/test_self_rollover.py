"""An Agent's own rollover: the holder asks, the Host fences and binds a successor."""

from __future__ import annotations

from dataclasses import replace

import pytest

from harness_rewrite import Command, Kernel, Query, self_rollover
from tests_rewrite.test_kernel import OTHER, OWNER, Host, checkpoint, code, start

ASK = {"summary": "Continue from the latest checkpoint"}


@pytest.fixture
def rig(tmp_path):
    host = Host()
    intents = OWNER.allowed_intents | {"request_rollover", "prepare_rollover"}
    for name in ("owner", "other"):
        host.contexts[name] = replace(host.contexts[name], allowed_intents=intents)
    host.contexts["sibling"] = replace(host.contexts["owner"], session="private-session-sibling")
    kernel = Kernel(tmp_path / "facts.sqlite", host.authenticate)
    return kernel, host, start(kernel)


def ask(kernel, wi, handle="owner", request="ask"):
    return kernel.execute(Command(request, "request_rollover", ASK, wi), handle)


def test_holder_with_its_checkpoint_last_may_request_and_nothing_moves(rig):
    kernel, _, wi = rig
    assert kernel.execute(checkpoint(wi), "owner")["status"] == "local_committed"
    asked = ask(kernel, wi)
    assert asked["status"] == "local_committed", asked
    request = asked["result"]["original_request_result"]["rollover_request"]
    assert request["source"] == OWNER.principal() and request["summary"] == ASK["summary"]
    state = kernel.inspect(Query(wi), "owner")
    assert state["work_item"]["generation"] == 1
    # The holder keeps its authority until the Host fences the source.
    assert kernel.execute(checkpoint(wi, "after"), "owner")["status"] == "local_committed"


def test_request_is_refused_without_a_latest_own_checkpoint(rig):
    kernel, _, wi = rig
    assert code(ask(kernel, wi, request="none")) == "domain_conflict"
    # A sibling session of the same Agent may checkpoint, but that is not this session's frontier.
    assert kernel.execute(checkpoint(wi, "sibling-note"), "sibling")["status"] == "local_committed"
    assert code(ask(kernel, wi, request="sibling-frontier")) == "domain_conflict"
    assert kernel.execute(checkpoint(wi, "own"), "owner")["status"] == "local_committed"
    assert ask(kernel, wi, request="ready")["status"] == "local_committed"


def test_another_agent_or_a_non_holder_session_cannot_request(rig):
    kernel, _, wi = rig
    assert kernel.execute(checkpoint(wi), "owner")["status"] == "local_committed"
    assert code(ask(kernel, wi, "other")) == "authority_denied"
    assert code(ask(kernel, wi, "sibling", "sibling-ask")) == "authority_denied"


def test_doubtful_effects_block_both_the_request_and_the_fence():
    state = {"records": [{"kind": "CheckpointRecorded", "author_session": OWNER.session}],
             "effects": {}}
    assert self_rollover.blocker(state, OWNER) is None
    for status in ("attempt_started", "unknown"):
        state["effects"] = {"effect.merge": {"status": status}}
        assert "effect.merge" in self_rollover.blocker(state, OWNER)
    state["effects"] = {"effect.merge": {"status": "observed_success"}}
    assert self_rollover.blocker(state, OWNER) is None


def test_host_fence_of_an_agent_request_rechecks_readiness_and_keeps_the_agent(rig):
    kernel, host, wi = rig
    assert kernel.execute(checkpoint(wi), "owner")["status"] == "local_committed"
    assert ask(kernel, wi)["status"] == "local_committed"
    host.contexts["host"] = replace(
        host.contexts["owner"],
        session_handover={"source": OWNER.principal(), "agent_request": "ask"},
    )
    # Work recorded after the request leaves the checkpoint behind: the fence refuses.
    assert kernel.execute(checkpoint(wi, "later"), "sibling")["status"] == "local_committed"
    fence = Command("fence", "prepare_rollover", ASK, wi)
    assert code(kernel.execute(fence, "host")) == "domain_conflict"
    assert kernel.execute(checkpoint(wi, "again"), "owner")["status"] == "local_committed"
    fenced = kernel.execute(Command("fence-2", "prepare_rollover", ASK, wi), "host")
    assert fenced["status"] == "local_committed", fenced
    transfer = fenced["result"]["original_request_result"]["transfer_id"]
    accept = Command("accept", "accept_transfer", {"transfer_id": transfer}, wi)
    # Another Agent cannot become the successor of an Agent's own rollover.
    host.contexts["thief"] = replace(
        OTHER, session_handover={"source": OWNER.principal(), "transfer_id": transfer}
    )
    assert code(kernel.execute(accept, "thief")) == "authority_denied"
    host.contexts["successor"] = replace(
        OWNER, session="private-session-successor",
        session_handover={"source": OWNER.principal(), "transfer_id": transfer},
    )
    accepted = kernel.execute(accept, "successor")
    assert accepted["status"] == "local_committed", accepted
    assert accepted["work_item"]["generation"] == 2
