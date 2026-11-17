"""Collaboration rights, preserved decisions and exact retries through the kernel."""

from dataclasses import replace
import json
import pytest
from harness_rewrite import SourceInput, Query
from harness_rewrite.domain import INTENTS
from tests_rewrite.test_workflow import Rig, graph, node, original, error
from tests_rewrite.test_review import (
    PROOF,
    ARTIFACT,
    VERIFIER,
    assign,
    verify,
    approve,
    candidate_id,
)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    for key, ctx in list(r.contexts.items()):
        r.contexts[key] = replace(
            ctx,
            allowed_intents=INTENTS,
            source=SourceInput(
                "source-" + key, "native_agent", b"Original collaboration decision"
            ),
        )
    r.contexts["third"] = replace(
        r.contexts["helper"], actor="third", session="third-session"
    )
    return r


def offer(r, kind="delegate", *, recipients=None, required=True, main_only=False):
    return original(
        r.send(
            "offer_collaboration",
            {
                "kind": kind,
                "recipients": recipients
                or [
                    {
                        "principal": r.contexts["helper"].principal(),
                        "resources": ["repo"],
                    }
                ],
                "purpose": "Complete the assigned responsibility",
                "required": required,
                "main_only": main_only,
            },
        )
    )["contract_id"]


def respond(r, c, decision, *, handle="helper", request=None, **extra):
    return r.send(
        "respond_collaboration",
        {
            "contract_id": c,
            "decision": decision,
            "reason": "Actual recipient decision",
            **extra,
        },
        handle=handle,
        request=request,
    )


def test_delegation_requires_exact_accept_and_return_revokes_live_attempt(rig):
    rig.adopt(graph(rig, [node("work", role="collaborator")]))
    c = offer(rig)
    assert error(respond(rig, c, "accept", handle="third")) == "authority_denied"
    assert error(rig.claim("work", handle="helper")) == "authority_denied"
    accepted = respond(rig, c, "accept", request="accept")
    assert original(accepted)["grant_id"]
    assert original(respond(rig, c, "accept", request="accept")) == original(accepted)
    attempt = original(rig.claim("work", handle="helper"))["attempt_id"]
    returned = respond(rig, c, "return")
    original(returned)
    original(respond(rig, c, "return"))
    assert error(rig.claim("work", handle="helper")) == "authority_denied"
    late = rig.result(attempt, handle="helper")
    assert original(late)["current"] is False
    state = rig.state()["result"]["current_observation"]
    assert state["definition"]["owner"] == "producer"
    assert len(state["collaborations"][0]["responses"]["helper"]["history"]) == 2
    assert "private-helper-session" not in json.dumps(state)


def test_request_does_not_grant_execution_and_required_completion_needs_real_proof(rig):
    rig.adopt(graph(rig, [node("work", outputs=["report"])]))
    c = offer(rig, "request")
    original(respond(rig, c, "accept"))
    assert (
        error(
            rig.send(
                "checkpoint",
                {"summary": "try", "resources": ["repo"]},
                handle="helper",
            )
        )
        == "authority_denied"
    )
    attempt = original(rig.claim("work"))["attempt_id"]
    original(rig.result(attempt, artifacts={"report": ARTIFACT}))
    candidate = candidate_id(rig)
    rig.contexts["verifier"] = VERIFIER
    original(verify(rig, candidate, assign(rig, candidate)))
    original(approve(rig, candidate))
    assert error(rig.send("complete", {"candidate_id": candidate})) == "domain_conflict"
    assert (
        error(respond(rig, c, "complete", evidence=[PROOF.reference()]))
        == "source_invalid"
    )
    rig.contexts["helper"] = replace(rig.contexts["helper"], artifacts=(PROOF,))
    original(respond(rig, c, "complete", evidence=[PROOF.reference()]))
    original(rig.send("complete", {"candidate_id": candidate}))


def test_full_responsibility_revokes_old_holder_and_retry_survives_generation(rig):
    rig.adopt(graph(rig))
    old_attempt = original(rig.claim("work"))["attempt_id"]
    c = offer(rig, "responsibility")
    accepted = respond(rig, c, "accept", request="transfer")
    original(accepted)
    rig.contexts["helper"] = replace(rig.contexts["helper"], generation=2)
    retry = respond(rig, c, "accept", request="transfer")
    assert original(retry) == original(accepted)
    assert retry["work_item"]["generation"] == 2
    assert error(rig.claim("work")) == "authority_changed"
    assert original(rig.result(old_attempt))["current"] is False
    assert original(rig.claim("work", handle="helper"))["attempt_id"]
    assert (
        rig.state()["result"]["current_observation"]["definition"]["owner"] == "helper"
    )
    changed = respond(rig, c, "reject", request="transfer")
    assert error(changed) == "request_conflict"
    assert (
        error(respond(rig, c, "accept", handle="owner", request="transfer"))
        == "request_conflict"
    )


def test_draft_transfer_never_starts_work_and_old_offers_cannot_restore_rights(rig):
    pending = offer(rig)
    c = offer(rig, "responsibility")
    original(respond(rig, c, "accept"))
    rig.contexts["helper"] = replace(rig.contexts["helper"], generation=2)
    response = rig.state(handle="helper")
    assert response["work_item"]["business_progress"] == "draft"
    assert (
        next(v["state"] for v in response["capabilities"] if v["name"] == "local_work")
        == "unavailable"
    )
    assert error(respond(rig, pending, "accept")) == "authority_changed"


def test_multi_recipient_conflict_reassignment_and_withdrawal_preserve_originals(rig):
    rig.adopt(graph(rig))
    recipients = [
        {"principal": rig.contexts[k].principal(), "resources": ["repo"]}
        for k in ("helper", "third")
    ]
    c = offer(rig, recipients=recipients)
    original(respond(rig, c, "reject"))
    original(respond(rig, c, "accept", handle="third"))
    new = original(
        rig.send(
            "reassign_collaboration",
            {
                "contract_id": c,
                "recipients": [recipients[0]],
                "reason": "Recipient changed",
            },
        )
    )["contract_id"]
    assert (
        error(
            rig.send(
                "checkpoint",
                {"summary": "old delegation", "resources": ["repo"]},
                handle="third",
            )
        )
        == "authority_denied"
    )
    original(respond(rig, new, "accept"))
    original(
        rig.send(
            "withdraw_collaboration", {"contract_id": new, "reason": "No longer needed"}
        )
    )
    original(
        rig.send(
            "withdraw_collaboration", {"contract_id": new, "reason": "No longer needed"}
        )
    )
    contracts = rig.state()["result"]["current_observation"]["collaborations"]
    assert len(contracts) == 2 and all(c["withdrawn"] for c in contracts)
    assert contracts[0]["responses"]["helper"]["status"] == "rejected"
    assert (
        error(respond(rig, new, "complete", evidence=[PROOF.reference()]))
        == "domain_conflict"
    )


def test_request_retry_ignores_only_host_generation_and_keeps_fresh_capability(rig):
    rig.adopt(graph(rig))
    data = {"summary": "Original checkpoint", "resources": ["repo"]}
    first = rig.send("checkpoint", data, request="old-request")
    original(first)
    c = offer(rig, "responsibility")
    original(respond(rig, c, "accept"))
    rig.contexts["owner"] = replace(rig.contexts["owner"], generation=2)
    retry = rig.send("checkpoint", data, request="old-request")
    assert original(retry) == original(first)
    assert (
        next(v["state"] for v in retry["capabilities"] if v["name"] == "local_work")
        == "unavailable"
    )
    rig.contexts["owner"] = replace(
        rig.contexts["owner"],
        source=SourceInput("other", "native_agent", b"Different original"),
    )
    assert (
        error(rig.send("checkpoint", data, request="old-request")) == "request_conflict"
    )
    history = rig.state(query=Query(rig.wi, view="history"))
    assert history["error"] is None


def test_worker_progress_cannot_expose_native_dispatch_capabilities():
    from harness_rewrite.cli import effect_progress

    raw = {
        "status": "local_committed",
        "request_id": "send",
        "operation_id": "op",
        "work_item": {"id": "wi"},
        "error": None,
        "effects": [{"native_session_id": "private-native-id"}],
        "result": {"private": "worker-only"},
    }
    projected = effect_progress([raw])
    assert projected[0]["operation_id"] == "op"
    assert "private" not in json.dumps(projected)
    assert "effects" not in projected[0]


@pytest.mark.parametrize("overlap", [False, True])
def test_multiple_acceptances_respect_exclusive_resource_scopes(rig, overlap):
    rig.adopt(
        graph(rig, [node("a", role="collaborator"), node("b", role="collaborator")])
    )
    recipients = [
        {"principal": rig.contexts["helper"].principal(), "resources": ["repo/a"]},
        {
            "principal": rig.contexts["third"].principal(),
            "resources": ["repo/a" if overlap else "repo/b"],
        },
    ]
    c = offer(rig, recipients=recipients)
    original(respond(rig, c, "accept"))
    reply = respond(rig, c, "accept", handle="third")
    if overlap:
        assert error(reply) == "domain_conflict"
    else:
        original(reply)
        original(rig.claim("a", handle="helper"))
        original(rig.claim("b", handle="third"))


def rolled_over(r, actor="producer", session="successor"):
    """The holder hands the WI to a successor session through the Host rollover."""
    successor = replace(r.contexts["owner"], actor=actor, session=actor + "-" + session)
    transfer = original(
        r.send("rollover", {"target": successor.principal(), "summary": "Resume"})
    )["transfer_id"]
    r.contexts["successor"] = successor
    original(r.send("accept_transfer", {"transfer_id": transfer}, handle="successor"))
    generation = r.state(handle="successor")["result"]["current_observation"]["generation"]
    r.contexts["owner"] = replace(successor, generation=generation)
    r.contexts["helper"] = replace(r.contexts["helper"], generation=generation)


def test_offer_from_before_own_rollover_stays_answerable(rig):
    rig.adopt(graph(rig))
    c = offer(rig, "request")
    rolled_over(rig)
    rolled_over(rig, session="second-successor")
    original(respond(rig, c, "accept"))


def test_takeover_or_other_agent_succession_still_ends_old_offers(rig):
    rig.adopt(graph(rig))
    c = offer(rig, "request")
    rolled_over(rig)
    original(
        rig.approve(
            "assume_control",
            {"reason": "human takeover"},
            actor=replace(rig.contexts["owner"], session="human-session"),
        )
    )
    assert error(respond(rig, c, "accept")) == "authority_changed"


def test_rollover_to_another_agent_ends_old_offers(rig):
    rig.adopt(graph(rig))
    c = offer(rig, "request")
    rolled_over(rig, actor="third")
    assert error(respond(rig, c, "accept")) == "authority_changed"


@pytest.mark.parametrize("kind", ["request", "delegate"])
def test_handing_back_after_another_agent_never_revives_old_offers(rig, kind):
    rig.adopt(graph(rig))
    c = offer(rig, kind)
    rolled_over(rig, actor="third")
    rolled_over(rig, session="returned")
    assert error(respond(rig, c, "accept")) == "authority_changed"
