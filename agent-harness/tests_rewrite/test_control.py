"""Human authority changes and non-delivery endings retain the same facts."""

from dataclasses import replace
import pytest
from harness_rewrite import Query
from harness_rewrite.domain import INTENTS
from tests_rewrite.test_workflow import Rig, BASE, graph, node, original, error
from tests_rewrite.test_review import (
    ARTIFACT,
    VERIFIER,
    assign,
    verify,
    approve,
    candidate_id,
)


@pytest.fixture
def rig(tmp_path):
    value = Rig(tmp_path)
    value.contexts["owner"] = replace(BASE, allowed_intents=INTENTS)
    return value


def human(rig, intent, data, actor=None, request=None):
    return rig.approve(
        intent,
        data,
        actor=replace(actor or BASE, allowed_intents=INTENTS),
        request=request,
    )


@pytest.mark.parametrize("kind", ["cancelled", "failed", "superseded"])
def test_non_delivery_termination_keeps_attempts_and_does_not_accept_outcome(rig, kind):
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    data = {"kind": kind, "reason": "Explicit human decision"}
    if kind == "superseded":
        data["successor"] = "wi_followup"
    assert error(rig.send("terminate_work", data)) == "source_invalid"
    result = human(rig, "terminate_work", data, request="terminate")
    original(result)
    assert original(
        human(rig, "terminate_work", data, request="terminate")
    ) == original(result)
    state = rig.state()["result"]["current_observation"]
    assert state["business_progress"] == "terminated"
    assert state["terminal"]["termination"] == kind
    assert state["terminal"]["resources_retired"] is False
    assert state["candidates"]["active"] is None
    assert original(rig.result(attempt))["current"] is False
    assert error(rig.claim("work")) == "authority_denied"
    history = rig.state(query=Query(rig.wi, view="history"))
    assert history["error"] is None


def test_human_can_take_control_without_old_holder_or_cache_but_no_human_means_no_grant(
    rig,
):
    rig.adopt(graph(rig))
    old = original(rig.claim("work"))["attempt_id"]
    other = replace(
        BASE,
        actor="successor",
        session="successor-session",
        generation=0,
        allowed_intents=INTENTS,
    )
    rig.contexts["new"] = other
    assert (
        error(rig.send("assume_control", {"reason": "human takeover"}, handle="new"))
        == "source_invalid"
    )
    first = human(
        rig,
        "assume_control",
        {"reason": "human takeover"},
        actor=other,
        request="takeover",
    )
    original(first)
    rig.contexts["new"] = replace(other, generation=2)
    assert original(
        human(
            rig,
            "assume_control",
            {"reason": "human takeover"},
            actor=rig.contexts["new"],
            request="takeover",
        )
    ) == original(first)
    assert original(rig.result(old))["current"] is False
    assert error(rig.claim("work")) == "authority_changed"
    original(rig.claim("work", handle="new"))
    assert (
        rig.state()["result"]["current_observation"]["definition"]["owner"]
        == "successor"
    )


def test_human_stop_from_draft_and_reopen_keeps_draft_without_invented_plan(rig):
    other = replace(BASE, actor="new-owner", session="new-session", generation=0)
    original(human(rig, "stop", {"reason": "Pause planning"}, actor=other))
    assert rig.state()["work_item"]["business_progress"] == "stopped"
    assert (
        error(human(rig, "assume_control", {"reason": "implicit restart"}, actor=other))
        == "domain_conflict"
    )
    original(
        human(
            rig, "reopen_work", {"reason": "Resume planning", "nodes": []}, actor=other
        )
    )
    state = rig.state()["result"]["current_observation"]
    assert state["business_progress"] == "draft"
    assert state["workflow"]["graph"] is None


def test_reopened_completed_work_requires_new_candidate_and_preserves_old_approval(rig):
    rig.adopt(graph(rig, [node("work", outputs=["report"])]))
    old_attempt = original(rig.claim("work"))["attempt_id"]
    original(rig.result(old_attempt, artifacts={"report": ARTIFACT}))
    old = candidate_id(rig)
    rig.contexts["verifier"] = VERIFIER
    original(verify(rig, old, assign(rig, old)))
    original(approve(rig, old))
    original(rig.send("complete", {"candidate_id": old}))
    original(
        human(
            rig,
            "reopen_work",
            {"reason": "Another actual iteration", "nodes": ["work"]},
        )
    )
    rig.contexts["owner"] = replace(rig.contexts["owner"], generation=2)
    assert error(rig.send("complete", {"candidate_id": old})) == "domain_conflict"
    state = rig.state()["result"]["current_observation"]
    assert state["candidates"]["active"] is None
    assert state["candidates"]["items"][0]["review"]["decision"] == "approve"
    new_attempt = original(rig.claim("work"))["attempt_id"]
    original(rig.result(new_attempt, artifacts={"report": ARTIFACT}))
    new = candidate_id(rig)
    assert new != old
    assert rig.state()["work_item"]["business_progress"] == "awaiting_verification"


def test_same_agent_continues_from_another_entry_in_one_step(rig):
    from harness_rewrite import channel

    rig.adopt(graph(rig))
    old = original(rig.claim("work"))["attempt_id"]
    here = replace(BASE, session="feishu-session", allowed_intents=INTENTS)
    rig.contexts["here"] = here
    observed = rig.state(handle="here")
    work = next(c for c in observed["capabilities"] if c["name"] == "local_work")
    assert work["reason"] == "held_by_same_agent_in_another_session"
    envelope = channel.continue_envelope("f" * 64, rig.wi, observed)
    assert envelope["intent"] == "continue_here"
    data = envelope["input"]
    assert error(rig.send("continue_here", data, handle="here")) == "source_invalid"
    original(human(rig, "continue_here", data, actor=here, request="continue"))
    # The previous session's work in flight can no longer settle or claim.
    assert original(rig.result(old))["current"] is False
    assert error(rig.claim("work")) in {"authority_changed", "authority_denied"}
    rig.contexts["here"] = replace(here, generation=2)
    original(rig.claim("work", handle="here"))
    state = rig.state(handle="here")["result"]["current_observation"]
    assert state["generation"] == 2
    assert state["definition"]["owner"] == BASE.actor
    assert state["control_history"][-1]["kind"] == "SessionContinued"
    assert "previous" not in state["control_history"][-1]
    assert original(
        human(rig, "continue_here", data, actor=rig.contexts["here"], request="again")
    )["already_holder"] is True


def test_another_agent_cannot_continue_here(rig):
    from harness_rewrite import channel

    rig.adopt(graph(rig))
    stranger = replace(BASE, actor="helper", session="helper-session", allowed_intents=INTENTS)
    rig.contexts["stranger"] = stranger
    observed = rig.state(handle="stranger")
    assert channel.continue_envelope("f" * 64, rig.wi, observed) is None
    assert error(
        human(rig, "continue_here", {"reason": "try"}, actor=stranger, request="stranger")
    ) == "authority_denied"
