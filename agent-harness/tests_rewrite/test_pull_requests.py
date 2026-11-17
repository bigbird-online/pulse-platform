"""PR output gate: heterogeneous flagship review, three rounds, exact kernel merge."""

import base64
from dataclasses import replace

import pytest

from harness_rewrite import Command, Query
from harness_rewrite.delivery import WORKER_INTENTS
from harness_rewrite.effects import Effects
from harness_rewrite.github_merge import GitHubUnavailable, PullRequestMerge, git_blob
from harness_rewrite.model import encoded
from harness_rewrite.merge_authority import INTENTS as STANDING_INTENTS
from harness_rewrite.pull_requests import INTENTS
from tests_rewrite.test_lightweight import light
from tests_rewrite.test_workflow import BASE, DEFINITION, error, original

REPO = "bigbird-online/PULSE"
HEAD = "a" * 40
NEXT = "b" * 40
GATE_POLICY = {"pulse_gh": "/fixture/pulse-gh.py", "gh": "/fixture/gh",
               "relay_paths": "/fixture/paths.json", "repositories": [REPO]}


def compute(runtime, model, effort, commits=()):
    return {
        "schema": "harness.session-compute/v1",
        "runtime": runtime,
        "turns": [{"model": model, "effort": effort}],
        "observed_turns": 2,
        "commits": [{"sha": sha[:9], "model": model, "verified": True} for sha in commits],
        "source_sha256": "0" * 64,
    }


def opened(head, **changes):
    return {"repository": REPO, "number": 7, "state": "open", "merged": False,
            "head_sha": head, "base": "main", "commits": [head], "commits_complete": True,
            "authors": [], **changes}


OPUS = compute("cc", "claude-opus-5-5", "high", [HEAD, NEXT, "c" * 40, "d" * 40, "e" * 40])
SOL = compute("codex", "gpt-7.0-sol", "xhigh")
CODEX_REVIEW = {"profile": "review", "provider": "codex", "model": "gpt-7.0-sol",
                "model_alias": "codex-sol-latest", "reasoning_effort": "high"}
CC_REVIEW = {"profile": "review", "provider": "cc", "model": "claude-opus-5-5",
             "model_alias": "claude-opus-latest", "reasoning_effort": "high"}
GATE = replace(BASE, allowed_intents=BASE.allowed_intents | INTENTS | STANDING_INTENTS)
AUTHOR = replace(GATE, merge_repositories=frozenset({REPO}), session_compute=OPUS)
REVIEWER = replace(GATE, session="private-reviewer-session", session_compute=SOL,
                   review_profile=CODEX_REVIEW)
WORKER = replace(BASE, actor="delivery-worker", session="delivery-service",
                 allowed_intents=WORKER_INTENTS, effect_kinds=frozenset({"merge_pull_request"}))


def rig_with_pr(tmp_path, head=HEAD):
    rig = light(tmp_path)
    rig.markers = tmp_path / "merge-markers"
    rig.contexts["author"] = AUTHOR
    rig.contexts["reviewer"] = REVIEWER
    rig.contexts["owner"] = GATE
    return rig, original(submit(rig, head))["candidate_id"]


def submit(rig, head, context=AUTHOR, observed=None, request=None, **extra):
    rig.contexts["registrant"] = replace(
        context, pull_request_observation=observed if observed is not None else opened(head))
    return rig.send("submit_pull_request",
                    {"repository": REPO, "number": 7, "head_sha": head, **extra},
                    handle="registrant", request=request)


def declared(context, wis):
    compute = context.session_compute
    if compute is None or "declared_wis" in compute:
        return context
    return replace(context, session_compute={**compute, "declared_wis": wis})


def review(rig, candidate, score, context=None, request=None):
    rig.contexts["candidate-reviewer"] = declared(context or REVIEWER, [rig.wi])
    return rig.send("record_pull_request_review",
                    {"candidate_id": candidate, "score": score, "reason": "Read the diff"},
                    handle="candidate-reviewer", request=request)


def view(rig, handle="owner"):
    return rig.kernel.inspect(Query(rig.wi), handle)


def pr(rig):
    return view(rig)["result"]["current_observation"]["pull_requests"][0]


def test_lightweight_pr_passes_heterogeneous_flagship_review_and_requests_exact_merge(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    assert pr(rig)["status"] == "awaiting_review"
    assert pr(rig)["rounds"][0]["review_request"]["runtime"] == "codex"
    assert pr(rig)["rounds"][0]["author_model"] == "claude-opus-5-5"
    waiting = view(rig, "reviewer")["next_action"]
    assert waiting["intent"] == "record_pull_request_review" and waiting["candidate_id"] == candidate
    result = original(review(rig, candidate, 85))
    assert result["passed"] and result["next"] == "merge"
    effect = next(e for e in view(rig)["effects"] if e["effect_kind"] == "merge_pull_request")
    assert effect["effect_id"] == result["merge_effect_id"]
    assert effect["snapshot"] == {"repository": REPO, "number": 7, "head_sha": HEAD,
                                  "base": "main", "candidate_id": candidate}
    assert pr(rig)["status"] == "merge_pending"
    assert pr(rig)["rounds"][0]["review"]["reviewer"] == BASE.actor


@pytest.mark.parametrize(
    "context,problem",
    [
        (replace(REVIEWER, session_compute=compute("cc", "claude-opus-5-5", "high"),
                 review_profile=CC_REVIEW), "reviewer_same_family"),
        (replace(REVIEWER, session_compute=compute("codex", "gpt-6.1-sol", "high")),
         "reviewer_not_flagship"),
        (replace(REVIEWER, session_compute=compute("codex", "gpt-7.0-astra", "high")),
         "reviewer_not_flagship"),
        (replace(REVIEWER, session_compute=compute("codex", "gpt-7.0-sol", "medium")),
         "reviewer_effort_below_high"),
        (replace(REVIEWER, session_compute=None), "reviewer_record_missing"),
        (replace(REVIEWER, session_compute=compute("codex", "gpt-7.0-sol", "unknown")),
         "reviewer_record_missing"),
        (replace(REVIEWER, review_profile=None), "review_profile_unavailable"),
        (replace(REVIEWER, review_profile={**CODEX_REVIEW, "profile": "frontier"}),
         "review_profile_unavailable"),
        (replace(REVIEWER, session_compute=compute("codex", "gpt-7.0-sol", "high", [HEAD])),
         "reviewer_committed_candidate"),
    ],
)
def test_ineligible_reviewer_is_refused_without_a_record(tmp_path, context, problem):
    rig, candidate = rig_with_pr(tmp_path)
    refused = review(rig, candidate, 99, context)
    assert error(refused) == "reviewer_ineligible"
    assert refused["error"]["reason"].startswith(problem)
    assert pr(rig)["status"] == "awaiting_review"


def test_review_session_must_declare_exactly_this_wi(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    for wis in ([], ["wi_20260101_001"], [rig.wi, "wi_20260101_001"]):
        refused = review(rig, candidate, 90, declared(REVIEWER, wis))
        assert refused["error"]["reason"].startswith("reviewer_wi_undeclared")
    assert original(review(rig, candidate, 90))["passed"]


def test_claude_reviewer_must_be_the_newest_opus_in_the_relay_catalog(tmp_path):
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    sol_author = replace(AUTHOR, session_compute=compute("codex", "gpt-7.0-sol", "high", [HEAD]))
    candidate = original(submit(rig, HEAD, sol_author))["candidate_id"]
    claude = replace(REVIEWER, session_compute=compute("cc", "claude-opus-5-5", "high"),
                     review_profile=CC_REVIEW)
    sonnet = replace(claude, session_compute=compute("cc", "claude-sonnet-5", "high"))
    older = replace(claude, session_compute=compute("cc", "claude-opus-5", "high"))
    alias = replace(claude, review_profile={**CC_REVIEW, "model": "opus"})
    for ineligible in (sonnet, older, alias):
        refused = review(rig, candidate, 90, ineligible)
        assert error(refused) == "reviewer_ineligible"
        assert refused["error"]["reason"].startswith("reviewer_not_flagship")
    missing = replace(claude, review_profile=None)
    assert review(rig, candidate, 90, missing)["error"]["reason"].startswith(
        "review_profile_unavailable")
    assert original(review(rig, candidate, 90, claude))["passed"]


def test_author_is_the_session_that_created_the_head_commit(tmp_path):
    # Codex repro: an Opus session commits, a Sol session registers, Opus reviews.
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    sol_registrant = replace(AUTHOR, session="private-sol-registrant",
                             session_compute=compute("codex", "gpt-7.0-sol", "high"))
    refused = submit(rig, HEAD, sol_registrant)
    assert error(refused) == "source_unavailable"
    assert "created the head commit" in refused["error"]["reason"]
    # Tool output without a commit evidence (only another commit) does not count.
    other = replace(AUTHOR, session_compute=compute("cc", "claude-opus-5-5", "high", ["f" * 40]))
    assert error(submit(rig, HEAD, other)) == "source_unavailable"
    # Commit text the repository did not confirm does not count either.
    unconfirmed = compute("cc", "claude-opus-5-5", "high", [HEAD])
    unconfirmed["commits"][0]["verified"] = False
    assert error(submit(rig, HEAD, replace(AUTHOR, session_compute=unconfirmed))) == "source_unavailable"
    candidate = original(submit(rig, HEAD))["candidate_id"]
    # The committing session cannot review, even from another family's profile.
    author_review = replace(REVIEWER, session=BASE.session)
    assert error(review(rig, candidate, 90, author_review)) == "authority_denied"


def test_registration_requires_an_admitted_open_unmerged_pull_request(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    assert error(review(rig, candidate, 90, replace(REVIEWER, actor="helper"))) == "authority_denied"
    assert error(review(rig, candidate, 101)) == "invalid_input"
    assert error(review(rig, candidate, True)) == "invalid_input"
    assert error(submit(rig, NEXT, replace(AUTHOR, merge_repositories=frozenset()))) == "authority_denied"
    upper = replace(AUTHOR, merge_repositories=frozenset({REPO.upper()}))
    assert error(submit(rig, NEXT, upper)) == "authority_denied"  # Exact whitelist match.
    assert error(submit(rig, NEXT, replace(AUTHOR, session_compute=None))) == "source_unavailable"
    rig.contexts["registrant"] = replace(AUTHOR, pull_request_observation=None)
    data = {"repository": REPO, "number": 7, "head_sha": NEXT}
    assert error(rig.send("submit_pull_request", data, handle="registrant")) == "source_unavailable"
    assert error(submit(rig, NEXT, observed=opened(NEXT, merged=True, state="closed"))) == "domain_conflict"
    assert error(submit(rig, NEXT, observed=opened(NEXT, state="closed"))) == "domain_conflict"
    assert error(submit(rig, NEXT, observed=opened(HEAD))) == "domain_conflict"
    assert error(submit(rig, NEXT, observed=opened(NEXT, base="release"))) == "domain_conflict"
    assert error(submit(rig, HEAD)) == "domain_conflict"  # Same head is not a new round.
    replaced = original(submit(rig, NEXT))["candidate_id"]
    assert [r["status"] for r in pr(rig)["rounds"]] == ["superseded", "awaiting_review"]
    assert error(review(rig, candidate, 90)) == "domain_conflict"
    assert original(review(rig, replaced, 90))["round"] == 1


def test_a_pull_request_belongs_to_one_wi_until_withdrawn_there(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    first = rig.wi
    created = rig.kernel.execute(Command("light-2", "create_lightweight", DEFINITION), "create")
    rig.wi = original(created)["work_item_id"]
    original(rig.send("start_work", {}))
    refused = submit(rig, NEXT)
    assert error(refused) == "domain_conflict" and first in refused["error"]["reason"]
    second, rig.wi = rig.wi, first
    withdrawal = {"repository": REPO, "number": 7, "reason": "Registered on the wrong WI"}
    rig.contexts["other-agent"] = replace(GATE, actor="helper", session="private-helper")
    assert error(rig.send("withdraw_pull_request", withdrawal, handle="other-agent")) == "authority_denied"
    original(rig.send("withdraw_pull_request", withdrawal))
    assert pr(rig)["status"] == "withdrawn"
    assert pr(rig)["withdrawals"][0]["reason"] == "Registered on the wrong WI"  # Auditable.
    assert [e["status"] for e in view(rig)["effects"]
            if e["effect_kind"] != "merge_pull_request"] in ([], ["cancelled"])
    rig.wi = second
    original(submit(rig, NEXT))
    rig.wi = first
    assert error(submit(rig, "c" * 40)) == "domain_conflict"


def test_a_withdrawn_registration_can_neither_be_reviewed_nor_merged(tmp_path):
    # Codex repro: after moving the PR to the right WI, the old review still merged here.
    rig, candidate = rig_with_pr(tmp_path)
    first = rig.wi
    created = rig.kernel.execute(Command("light-2", "create_lightweight", DEFINITION), "create")
    second = original(created)["work_item_id"]
    withdrawal = {"repository": REPO, "number": 7, "reason": "Registered on the wrong WI"}
    original(rig.send("withdraw_pull_request", withdrawal))
    rig.wi = second
    original(rig.send("start_work", {}))
    original(submit(rig, HEAD))
    rig.wi = first
    assert error(review(rig, candidate, 95)) == "domain_conflict"
    github = FakeGitHub()
    assert drain(rig, github) == []
    assert not any(c[:2] == ["--method", "PUT"] for c in github.calls)


def test_a_passed_review_withdrawn_before_merge_never_merges(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    effect_id = original(review(rig, candidate, 95))["merge_effect_id"]
    original(rig.send("withdraw_pull_request",
                      {"repository": REPO, "number": 7, "reason": "Wrong WI"}))
    github = FakeGitHub()
    drain(rig, github)
    assert next(e for e in view(rig)["effects"] if e["effect_id"] == effect_id)["status"] == "cancelled"
    assert not any(c[:2] == ["--method", "PUT"] for c in github.calls)


def test_a_rewrite_by_the_other_family_goes_to_a_human_at_registration(tmp_path):
    # Codex repro: Opus first round, Sol rewrote every commit; no family is independent.
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 60))
    sol = replace(AUTHOR, session="private-sol-rewrite",
                  session_compute=compute("codex", "gpt-7.0-sol", "high", [NEXT]))
    rewritten = original(submit(rig, NEXT, sol))["candidate_id"]
    assert pr(rig)["status"] == "human_confirmation_required"
    assert pr(rig)["rounds"][-1]["review_block"] == "mixed_author_families"
    opus = replace(REVIEWER, session_compute=compute("cc", "claude-opus-5-5", "high"),
                   review_profile=CC_REVIEW)
    assert error(review(rig, rewritten, 95, opus)) == "domain_conflict"
    data = {"candidate_id": rewritten, "reason": "Human reviewed the rewrite"}
    original(rig.approve("confirm_pull_request_merge", data, actor=GATE))


def test_an_author_session_declaring_several_wis_cannot_register(tmp_path):
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    several = replace(AUTHOR, session_compute={**OPUS, "declared_wis": [rig.wi, "wi_20261015_401"]})
    refused = submit(rig, HEAD, several)
    assert error(refused) == "domain_conflict" and "exactly one WI" in refused["error"]["reason"]


def test_registration_follows_the_author_session_declared_wi(tmp_path):
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    elsewhere = replace(AUTHOR, session_compute={**OPUS, "declared_wis": ["wi_20261015_401"]})
    refused = submit(rig, HEAD, elsewhere)
    assert error(refused) == "domain_conflict"
    assert "wi_20261015_401" in refused["error"]["reason"]
    original(submit(rig, HEAD, replace(AUTHOR, session_compute={**OPUS, "declared_wis": [rig.wi]})))


def test_a_session_of_several_wis_registers_a_commit_its_request_charged_to_the_target(tmp_path):
    # Hestia Oikos#204: the session declared the parent and a child in different
    # requests, then committed in a request that declared only the parent.
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    other = "wi_20261015_401"

    def author(proof, found=None, second=None):
        # `second`: another window of the session that also created the head.
        commits = [{**c, "request_declared_wis": proof} for c in OPUS["commits"]] + (
            [{**OPUS["commits"][0], "request_declared_wis": second}] if second else [])
        listed = [{"sha": HEAD, "session": BASE.session, "model": "claude-opus-5-5",
                   "declared_wis": [rig.wi, other], "wi_marker": None,
                   "request_declared_wis": proof if found is None else found}]
        return (replace(AUTHOR, session_compute={**OPUS, "commits": commits,
                                                 "declared_wis": [rig.wi, other]}),
                opened(HEAD, authors=listed))

    for proof, found in ((None, None), ([other], None), ([rig.wi], [other]), ([rig.wi, other], None)):
        refused = submit(rig, HEAD, *author(proof, found))
        assert error(refused) == "domain_conflict" and other in refused["error"]["reason"]
    refused = submit(rig, HEAD, *author([rig.wi], second=[other]))
    assert error(refused) == "domain_conflict" and other in refused["error"]["reason"]
    original(submit(rig, HEAD, *author([rig.wi], second=[rig.wi])))


def test_a_sub_agent_marked_with_the_target_belongs_to_it_when_its_dispatch_declared_only_it(
        tmp_path):
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    other = "wi_20261015_401"

    def marked(served):
        context = spawner(wi_marker=rig.wi, dispatch_declared_wis=served)
        return replace(context, session_compute={
            **context.session_compute, "declared_wis": [rig.wi, other]})

    for served in (None, [other], [rig.wi, other]):
        assert error(submit(rig, HEAD, marked(served))) == "domain_conflict"
    original(submit(rig, HEAD, marked([rig.wi])))


def rolled_over(rig, session="private-successor-session"):
    """The holder session hands this WI over to a successor session of its Agent."""
    successor = replace(BASE, session=session)
    transfer = original(rig.send("rollover", {"target": successor.principal(),
                                              "summary": "Resume"}))["transfer_id"]
    rig.contexts["successor"] = successor
    original(rig.send("accept_transfer", {"transfer_id": transfer}, handle="successor"))
    return replace(AUTHOR, session=session, generation=view(rig)["result"]["current_observation"]["generation"],
                   session_compute=compute("cc", "claude-opus-5-5", "high"))


def made_by(session, sha=HEAD, model="claude-opus-5-5", wis=None):
    # Host `find_authors`: a session record whose own commit window created `sha`.
    return {"sha": sha, "session": session, "model": model, "declared_wis": wis or [],
            "wi_marker": None}


def ledger_state(tmp_path, wi):
    import json
    import sqlite3

    from harness_rewrite.domain import reduce

    rows = sqlite3.connect(tmp_path / "journal.sqlite").execute(
        "SELECT content FROM journal WHERE wi=? ORDER BY revision", (wi,)).fetchall()
    return reduce(None, [e for (content,) in rows for e in json.loads(content)["events"]])


def test_a_successor_registers_the_head_commit_of_its_predecessor_session(tmp_path):
    # WI520 repro: the session that made 7c0260fa rolled over; its successor registers.
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    successor = rolled_over(rig)
    candidate = original(submit(rig, HEAD, successor, opened(HEAD, authors=[
        made_by(BASE.session, wis=[rig.wi])])))["candidate_id"]
    state = ledger_state(tmp_path, rig.wi)
    assert state["former_holders"] == [BASE.principal()]
    item = state["pull_requests"][f"{REPO.lower()}#7"]["rounds"][0]
    # Authorship stays the original session and its model, not the registrant.
    assert item["authors"] == [{"sha": HEAD, "session": BASE.session, "model": "claude-opus-5-5"}]
    assert item["author_model"] == "claude-opus-5-5" and item["review_block"] is None
    assert pr(rig)["status"] == "awaiting_review"
    # The predecessor session cannot review its own head.
    own = replace(REVIEWER, session=BASE.session, generation=successor.generation)
    refused = review(rig, candidate, 90, own)
    assert error(refused) == "authority_denied" and "author session" in refused["error"]["reason"]


def test_only_a_former_holder_of_this_wi_counts_as_the_predecessor(tmp_path):
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    # Another WI's former holder: its rollover is no part of this WI's chain.
    rig.contexts["create"] = replace(rig.contexts["create"], session="private-other-wi-session")
    rig.contexts["other-holder"] = replace(BASE, session="private-other-wi-session")
    created = rig.kernel.execute(Command("light-2", "create_lightweight", DEFINITION), "create")
    other = original(created)["work_item_id"]
    mine = rig.wi
    rig.wi = other
    original(rig.send("start_work", {}, handle="other-holder"))
    rig.contexts["other-successor"] = replace(BASE, session="private-other-successor")
    transfer = original(rig.send("rollover", {
        "target": rig.contexts["other-successor"].principal(), "summary": "Resume"},
        handle="other-holder"))["transfer_id"]
    original(rig.send("accept_transfer", {"transfer_id": transfer}, handle="other-successor"))
    rig.wi = mine
    successor = rolled_over(rig)
    for session in ("session.unrelated", "private-other-wi-session", successor.session):
        refused = submit(rig, HEAD, successor, opened(HEAD, authors=[made_by(session)]))
        assert error(refused) == "source_unavailable"
        assert "held this WI before it" in refused["error"]["reason"]
    # The predecessor's commit still answers to its own declaration.
    elsewhere = submit(rig, HEAD, successor, opened(HEAD, authors=[
        made_by(BASE.session, wis=[other])]))
    assert error(elsewhere) == "domain_conflict"
    # A predecessor of another model family than a later commit: a human decides.
    sol = compute("codex", "gpt-7.0-sol", "high", [NEXT])
    mixed = original(submit(rig, NEXT, replace(successor, session_compute=sol), opened(
        NEXT, commits=[HEAD, NEXT], authors=[made_by(BASE.session, wis=[rig.wi])])))
    assert mixed["candidate_id"] and pr(rig)["rounds"][0]["review_block"] == "mixed_author_families"


def test_every_commit_author_counts_and_mixed_authors_go_to_a_human(tmp_path):
    # Codex repro: Opus wrote the body, a Sol session added a fix and registered;
    # the original Opus session must not review its own code.
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    body = "c" * 40
    opus_body = {"sha": body, "session": "session.opus-body", "model": "claude-opus-5-5",
                 "declared_wis": []}
    sol_fix = replace(AUTHOR, session="private-sol-fix",
                      session_compute=compute("codex", "gpt-7.0-sol", "high", [HEAD]))
    candidate = original(submit(rig, HEAD, sol_fix, opened(HEAD, commits=[body, HEAD],
                                                           authors=[opus_body])))["candidate_id"]
    state = pr(rig)
    assert state["status"] == "human_confirmation_required"
    assert state["rounds"][0]["review_block"] == "mixed_author_families"
    assert {a["model"] for a in state["rounds"][0]["authors"]} == {"claude-opus-5-5", "gpt-7.0-sol"}
    opus_reviewer = replace(REVIEWER, session="session.opus-body",
                            session_compute=compute("cc", "claude-opus-5-5", "high"),
                            review_profile=CC_REVIEW)
    assert error(review(rig, candidate, 99, opus_reviewer)) == "domain_conflict"
    data = {"candidate_id": candidate, "reason": "Human reviewed the mixed authorship"}
    original(rig.approve("confirm_pull_request_merge", data, actor=GATE))


def test_a_commit_without_author_evidence_goes_to_a_human(tmp_path):
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    original(submit(rig, HEAD, AUTHOR, opened(HEAD, commits=["c" * 40, HEAD])))
    assert pr(rig)["rounds"][0]["review_block"] == "unattributed_commits"
    assert pr(rig)["status"] == "human_confirmation_required"
    original(submit(rig, NEXT, AUTHOR, opened(NEXT, commits=[NEXT], commits_complete=False)))
    assert pr(rig)["rounds"][-1]["review_block"] == "unattributed_commits"


def test_an_earlier_author_session_of_the_same_family_cannot_review(tmp_path):
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    body = "c" * 40
    sol_author = replace(AUTHOR, session_compute=compute("codex", "gpt-7.0-sol", "high", [HEAD]))
    earlier = {"sha": body, "session": "session.earlier-claude", "model": "gpt-7.0-sol",
               "declared_wis": []}
    candidate = original(submit(rig, HEAD, sol_author, opened(HEAD, commits=[body, HEAD],
                                                              authors=[earlier])))["candidate_id"]
    assert pr(rig)["status"] == "awaiting_review"
    claude = replace(REVIEWER, session_compute=compute("cc", "claude-opus-5-5", "high"),
                     review_profile=CC_REVIEW)
    assert error(review(rig, candidate, 90, replace(claude, session="session.earlier-claude"))) == "authority_denied"
    assert original(review(rig, candidate, 90, claude))["passed"]


def spawner(model="claude-sonnet-5", verified=True, **child):
    """A main CC session whose Host-proven sub-agent created the head commit."""
    return replace(AUTHOR, session_compute={**compute("cc", "claude-opus-5-5", "high"), "subagents": [{
        "native_id": "parent/subagents/agent-a1", "session": "session.sub-agent",
        "turns": [{"model": model, "effort": "high"}], "declared_wis": [],
        "commits": [{"sha": HEAD[:9], "model": model, "verified": verified}], **child}]})


def test_a_main_session_registers_its_sub_agent_commit(tmp_path):
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    candidate = original(submit(rig, HEAD, spawner()))["candidate_id"]
    state = pr(rig)
    assert state["status"] == "awaiting_review"
    assert state["rounds"][0]["author_model"] == "claude-sonnet-5"
    assert state["rounds"][0]["review_request"]["runtime"] == "codex"
    # Neither the sub-agent nor its main session may review.
    for session in ("session.sub-agent", BASE.session):
        assert error(review(rig, candidate, 90, replace(REVIEWER, session=session))) == "authority_denied"
    claude = replace(REVIEWER, session_compute=compute("cc", "claude-opus-5-5", "high"),
                     review_profile=CC_REVIEW)
    refused = review(rig, candidate, 90, claude)
    assert refused["error"]["reason"].startswith("reviewer_same_family")
    assert original(review(rig, candidate, 90))["passed"]


def test_a_sub_agent_commit_needs_host_proof_and_one_wi(tmp_path):
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    # Unconfirmed by the repository, or without a Host session reference.
    assert error(submit(rig, HEAD, spawner(verified=False))) == "source_unavailable"
    assert error(submit(rig, HEAD, spawner(session=None))) == "source_unavailable"
    assert error(submit(rig, HEAD, spawner(model="unknown"))) == "source_unavailable"
    refused = submit(rig, HEAD, spawner(declared_wis=["wi_20261015_401"]))
    assert error(refused) == "domain_conflict" and "sub-agent" in refused["error"]["reason"]
    original(submit(rig, HEAD, spawner(declared_wis=[rig.wi])))


def test_an_earlier_commit_by_another_session_s_sub_agent_must_share_the_wi(tmp_path):
    # Codex repro: the body came from another main session's sub-agent working on WI B.
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    body = "c" * 40
    elsewhere = {"sha": body, "session": "session.other-sub-agent", "model": "claude-sonnet-5",
                 "declared_wis": ["wi_20261015_401"]}
    refused = submit(rig, HEAD, AUTHOR, opened(HEAD, commits=[body, HEAD], authors=[elsewhere]))
    assert error(refused) == "domain_conflict"
    assert "wi_20261015_401" in refused["error"]["reason"]
    same = {**elsewhere, "declared_wis": [rig.wi]}
    original(submit(rig, HEAD, AUTHOR, opened(HEAD, commits=[body, HEAD], authors=[same])))
    assert pr(rig)["status"] == "awaiting_review"


def test_a_reviewer_whose_sub_agent_committed_a_head_is_ineligible(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    sol = {**SOL, "subagents": [{"session": "session.x", "declared_wis": [],
                                 "commits": [{"sha": HEAD[:9], "verified": True}]}]}
    refused = review(rig, candidate, 90, replace(REVIEWER, session_compute=sol))
    assert refused["error"]["reason"].startswith("reviewer_committed_candidate")


def test_three_failed_rounds_require_human_confirmation_before_merge(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    heads = ["c" * 40, "d" * 40]
    for index in range(3):
        result = original(review(rig, candidate, 84))
        assert not result["passed"] and result["round"] == index + 1
        if index < 2:
            assert pr(rig)["status"] == "changes_requested"
            assert view(rig, "author")["next_action"]["intent"] == "submit_pull_request"
            candidate = original(submit(rig, heads[index]))["candidate_id"]
    assert pr(rig)["status"] == "human_confirmation_required"
    assert view(rig, "author")["next_action"]["intent"] == "confirm_pull_request_merge"
    data = {"candidate_id": candidate, "reason": "Human accepts the remaining findings"}
    assert error(rig.send("confirm_pull_request_merge", data)) == "source_invalid"
    confirmed = original(rig.approve("confirm_pull_request_merge", data, actor=GATE))
    effect = next(e for e in view(rig)["effects"] if e["effect_id"] == confirmed["merge_effect_id"])
    assert effect["snapshot"]["head_sha"] == "d" * 40
    assert error(rig.approve("confirm_pull_request_merge", data, request="again", actor=GATE)) == "domain_conflict"


def test_passed_rounds_whose_merge_failed_still_count_toward_three(tmp_path):
    # Codex repro: pass, then a moved head fails the merge, repeated into a fourth review.
    rig, candidate = rig_with_pr(tmp_path)
    heads = [HEAD, NEXT, "c" * 40, "d" * 40]
    for index in range(3):
        original(review(rig, candidate, 95))
        drain(rig, FakeGitHub(head=heads[index + 1]))
        assert pr(rig)["status"] == "merge_failed"
        candidate = original(submit(rig, heads[index + 1]))["candidate_id"]
    assert pr(rig)["reviewed_rounds"] == 3
    assert pr(rig)["status"] == "human_confirmation_required"
    assert pr(rig)["rounds"][-1]["review_request"] is None
    assert error(review(rig, candidate, 99)) == "domain_conflict"
    data = {"candidate_id": candidate, "reason": "Final head confirmed by the human"}
    confirmed = original(rig.approve("confirm_pull_request_merge", data, actor=GATE))
    effect = next(e for e in view(rig)["effects"] if e["effect_id"] == confirmed["merge_effect_id"])
    assert effect["snapshot"]["head_sha"] == "d" * 40


def test_retry_returns_the_frozen_review_after_the_record_grows(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    first = original(review(rig, candidate, 90, request="review-once"))
    grown = replace(REVIEWER, session_compute={**SOL, "observed_turns": 9, "source_sha256": "1" * 64},
                    review_profile={**CODEX_REVIEW, "reasoning_effort": "xhigh"})
    assert original(review(rig, candidate, 90, grown, request="review-once")) == first
    assert pr(rig)["rounds"][0]["review"]["compute"]["observed_turns"] == 2


GREEN = [("harness", "completed", "success")]
HARNESS_WORKFLOW = "on:\n  pull_request:\n    branches: [main]\n    paths:\n      - 'agent-harness/**'\n"
PASSING = frozenset({"success", "skipped", "neutral"})
BASE_SHA = "c" * 40  # The commit main names while the checks are read.


def workflow_path(workflow):
    return ".github/workflows/" + workflow.lower().replace(" ", "-") + ".yml"


class FakeGitHub:
    """A pull request head with GitHub Actions runs, other apps' checks and statuses,
    paged like GitHub.

    `runs` are jobs: (name, status, conclusion[, workflow[, attempt[, run[, id]]]]) in
    list order. Workflow defaults to "Harness CI", attempt to 1, run to 0; one workflow
    run per (workflow, run), judged by its highest attempt. A workflow "app:<slug>"
    is another app's check run instead. `run_state` overrides a workflow run's
    (status, conclusion); `extra_suites` adds raw check suites. `workflows` are the
    base branch workflow files (path -> YAML text) and `files` the changed files.
    `statuses` are (context, state); `checks_down` fails the check endpoints;
    `drift(kind, page)` runs before each listed page is answered."""

    def __init__(self, head=HEAD, merged=False, state="open", refuse=False, on_main=True,
                 repo=REPO, runs=GREEN, statuses=(), checks_down=False, files=None,
                 workflows=None):
        self.head, self.merged, self.state, self.refuse, self.on_main = head, merged, state, refuse, on_main
        self.repo = repo
        self.runs, self.statuses, self.checks_down = list(runs), list(statuses), checks_down
        self.files = ["agent-harness/harness_rewrite/github_merge.py"] if files is None else files
        self.workflows = ({workflow_path("Harness CI"): HARNESS_WORKFLOW} if workflows is None
                          else workflows)
        self.run_state, self.extra_suites, self.drift, self.truncated = {}, [], None, False
        # The absence proof: `.github` in the root (its type) and its listing; a forced answer.
        self.github_type, self.github_dir, self.directory_answer = "dir", None, None
        # Workflow files the PR adds or changes (text) or removes (None); suite count overrides.
        self.pr_workflows, self.counts = {}, {}
        self.calls = []

    def file_rows(self):
        rows = [{"filename": f, "status": "modified", "sha": "a" * 40}
                for f in self.files if f not in self.pr_workflows]
        return rows + [{"filename": f, "status": "removed" if text is None else "added",
                        "sha": "0" * 40 if text is None else git_blob(text.encode())}
                       for f, text in sorted(self.pr_workflows.items())]

    def puts(self):
        return sum(c[:2] == ["--method", "PUT"] for c in self.calls)

    def jobs(self):
        rows = []
        for index, job in enumerate(self.runs):
            name, status, conclusion, workflow, attempt, run, ident = (*job, *[None] * 4)[:7]
            rows.append({"id": ident or 1000 + index, "name": name, "status": status,
                         "conclusion": conclusion, "workflow": workflow or "Harness CI",
                         "attempt": attempt or 1, "run": run or 0, "head_sha": self.head,
                         "html_url": f"https://github.com/{self.repo}/actions/runs/1/job/{1000 + index}"})
        return rows

    def model(self):
        """(check suites, workflow runs, {(run id, attempt): jobs}, {suite id: app checks})."""
        suites, runs, jobs, app_checks = [], [], {}, {}
        rows = self.jobs()
        keys = sorted({(j["workflow"], j["run"]) for j in rows})
        for number, (workflow, run) in enumerate(keys):
            mine = [j for j in rows if (j["workflow"], j["run"]) == (workflow, run)]
            suite_id = 500 + number
            if workflow.startswith("app:"):
                latest = {}
                for j in mine:
                    latest[j["name"]] = {**j, "check_suite": {"id": suite_id}}
                done = all(j["status"] == "completed" for j in latest.values())
                suites.append({"id": suite_id, "head_sha": self.head,
                               "status": "completed" if done else "in_progress",
                               "conclusion": None, "latest_check_runs_count": len(latest),
                               "app": {"id": 9, "slug": workflow[4:]}})
                app_checks[suite_id] = list(latest.values())
                continue
            attempt = max(j["attempt"] for j in mine)
            current = [j for j in mine if j["attempt"] == attempt]
            # The suite's latest check runs: this attempt's jobs, plus one for each job
            # name a partial re-run did not repeat.
            repeated = len({j["name"] for j in mine} - {j["name"] for j in current})
            run_id = 3000 + number
            done = all(j["status"] == "completed" for j in current)
            status, conclusion = self.run_state.get((workflow, run), (
                "completed" if done else "in_progress",
                None if not done else "success" if all(j["conclusion"] in PASSING for j in current)
                else "failure"))
            suites.append({"id": suite_id, "head_sha": self.head, "status": status,
                           "conclusion": conclusion,
                           "latest_check_runs_count": self.counts.get(
                               (workflow, run), len(current) + repeated),
                           "app": {"id": 15368, "slug": "github-actions"}})
            runs.append({"id": run_id, "check_suite_id": suite_id, "head_sha": self.head,
                         "name": workflow, "path": workflow_path(workflow), "event": "pull_request",
                         "run_attempt": attempt, "status": status, "conclusion": conclusion})
            for j in mine:
                jobs.setdefault((run_id, j["attempt"]), []).append(
                    {**j, "run_id": run_id, "run_attempt": j["attempt"]})
        return suites + self.extra_suites, runs, jobs, app_checks

    @staticmethod
    def window(path, rows):
        query = dict(part.split("=") for part in path.split("?")[1].split("&"))
        size, number = int(query["per_page"]), int(query["page"])
        return rows[(number - 1) * size:number * size]

    def checks(self, path):
        if self.checks_down:
            raise GitHubUnavailable("checks unavailable")
        kind = path.split("?")[0].rsplit("/", 1)[1]
        if self.drift and "page=" in path:
            self.drift(kind, int(path.split("page=")[-1]))
        suites, runs, jobs, app_checks = self.model()
        if kind == "status":
            rows = [{"id": 2000 + i, "context": c, "state": s} for i, (c, s) in enumerate(self.statuses)]
            return {"total_count": len(rows), "statuses": self.window(path, rows),
                    "sha": self.head, "repository": {"full_name": self.repo}}
        if kind == "files":
            return {"items": self.window(path, self.file_rows())}
        if kind == "jobs":
            run_id = int(path.split("/runs/")[1].split("/")[0])
            attempt = int(path.split("/attempts/")[1].split("/")[0])
            rows, key = jobs.get((run_id, attempt), []), "jobs"
        elif kind == "workflows":
            rows, key = [{"id": 70 + i, "path": p, "state": "active"}
                         for i, p in enumerate(sorted(self.workflows))], "workflows"
        else:
            rows, key = {
                "check-suites": (suites, "check_suites"),
                "runs": (runs, "workflow_runs"),
            }.get(kind) or (app_checks.get(int(path.split("/check-suites/")[1].split("/")[0]), []),
                            "check_runs")
        return {"total_count": len(rows), key: self.window(path, rows)}

    def __call__(self, repo, args):
        self.calls.append(args)
        path = args[0]
        if args[:2] == ["--method", "PUT"]:
            if self.refuse or not any(a == f"sha={self.head}" for a in args):
                raise GitHubUnavailable("refused")
            self.merged = True
            return {"merged": True}
        if path.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main", "object": {"sha": BASE_SHA, "type": "commit"}}
        if "?ref=main" in path:
            raise AssertionError("workflow files are read at the base commit, not the branch name")
        if f"/contents/.github?ref={BASE_SHA}" in path:
            if self.github_dir is None:
                raise GitHubUnavailable("not found")
            return self.github_dir
        if f"/contents/.github/workflows?ref={BASE_SHA}" in path:
            if self.directory_answer is not None:
                return self.directory_answer
            if not self.workflows:
                raise GitHubUnavailable("not found")
            rows = [{"name": p.rsplit("/", 1)[1], "path": p, "type": "file",
                     "sha": git_blob(text.encode())} for p, text in sorted(self.workflows.items())]
            if self.truncated:  # GitHub stops listing a directory at 1000 entries.
                rows += [{"name": f"x{i}.txt", "path": f".github/workflows/x{i}.txt",
                          "type": "file", "sha": "0" * 40} for i in range(1000 - len(rows))]
            return {"items": rows}
        if f"/contents/?ref={BASE_SHA}" in path:
            return {"items": [{"name": "README.md", "path": "README.md", "type": "file"}] + (
                [{"name": ".github", "path": ".github", "type": self.github_type}] if (self.workflows or self.github_dir is not None) else [])}
        if "/git/blobs/" in path:
            sha = path.split("/git/blobs/")[1]
            text = next(t for t in [*self.workflows.values(), *self.pr_workflows.values()]
                        if t is not None and git_blob(t.encode()) == sha)
            return {"sha": sha, "encoding": "base64",
                    "content": base64.b64encode(text.encode()).decode()}
        if any(k in path for k in ("/check-runs?", "/status?", "/check-suites?", "/actions/runs?",
                                   "/jobs?", "/actions/workflows?", "/files?")):
            return self.checks(path)
        if "/commits?" in path:
            return {"items": [{"sha": self.head}]}
        if "/compare/" in path:
            return {"status": "ahead" if self.on_main else "diverged"}
        return {"number": 7, "head": {"sha": self.head}, "changed_files": len(self.file_rows()),
                "base": {"ref": "main", "repo": {"full_name": self.repo,
                                                 "html_url": "https://github.com/" + self.repo}},
                "merged": self.merged, "state": "closed" if self.merged else self.state,
                "merge_commit_sha": "f" * 40 if self.merged else None,
                "merged_at": "2026-10-26T10:00:00Z", "merged_by": {"login": "example-owner"}}


def drain(rig, github, fault=None):
    def host(artifacts):
        rig.contexts["worker"] = replace(WORKER, artifacts=artifacts)
        return "worker"
    adapter = PullRequestMerge(GATE_POLICY, "/fixture/python", api=github, fault=fault,
                               markers=rig.markers)
    return Effects(rig.kernel, adapter, host).drain(rig.wi)


def test_kernel_merge_binds_reviewed_head_and_reads_back_main(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    github = FakeGitHub()
    results = drain(rig, github)
    assert [r["status"] for r in results] == ["local_committed"]
    merge = pr(rig)["rounds"][0]["merge"]
    assert pr(rig)["status"] == "merged" and merge["status"] == "observed_success"
    assert merge["observation"]["merge_commit"] == "f" * 40
    assert any(c[:2] == ["--method", "PUT"] for c in github.calls)
    assert drain(rig, github) == []  # Observed success is final.
    assert error(submit(rig, NEXT)) == "domain_conflict"


def test_merged_before_the_kernel_attempt_is_not_a_kernel_merge(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    github = FakeGitHub(merged=True)
    drain(rig, github)
    assert pr(rig)["status"] == "merge_failed"
    assert pr(rig)["rounds"][0]["merge"]["observation"]["reason"] == "merged_outside_kernel"
    # Codex repro: a second drain must not turn the failure into a merge.
    assert drain(rig, github) == []
    assert pr(rig)["status"] == "merge_failed"
    assert not any(c[:2] == ["--method", "PUT"] for c in github.calls)


def test_crash_before_the_merge_request_never_credits_a_later_merge(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    github = FakeGitHub()

    def crash(stage):
        if stage == "before_merge":
            raise RuntimeError("process died before sending")
    with pytest.raises(RuntimeError):
        drain(rig, github, crash)
    github.merged = True  # Someone merged it by hand meanwhile.
    drain(rig, github)
    assert pr(rig)["status"] == "merge_failed"
    assert pr(rig)["rounds"][0]["merge"]["observation"]["reason"] == "merged_outside_kernel"
    assert not any(c[:2] == ["--method", "PUT"] for c in github.calls)


def test_crash_after_merge_recovers_by_readback_without_a_second_merge(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    github = FakeGitHub()

    def crash(stage):
        if stage == "after_merge":
            raise RuntimeError("process died after the merge call")
    with pytest.raises(RuntimeError):
        drain(rig, github, crash)
    assert pr(rig)["rounds"][0]["merge"]["status"] == "attempt_started"
    drain(rig, github)
    assert pr(rig)["status"] == "merged"
    assert sum(c[:2] == ["--method", "PUT"] for c in github.calls) == 1


def test_moved_head_refuses_merge_and_requires_a_new_reviewed_round(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 95))
    github = FakeGitHub(head=NEXT)
    drain(rig, github)
    assert not any(c[:2] == ["--method", "PUT"] for c in github.calls)
    assert pr(rig)["rounds"][0]["merge"]["observation"]["reason"] == "head_changed"
    assert view(rig, "author")["next_action"]["intent"] == "submit_pull_request"
    replaced = original(submit(rig, NEXT))["candidate_id"]
    assert pr(rig)["status"] == "awaiting_review"
    original(review(rig, replaced, 90))


def test_refused_merge_is_absence_and_new_head_cancels_the_pending_merge(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    effect_id = original(review(rig, candidate, 90))["merge_effect_id"]
    drain(rig, FakeGitHub(refuse=True))
    effect = next(e for e in view(rig)["effects"] if e["effect_id"] == effect_id)
    assert effect["status"] == "observed_absence"
    assert effect["observations"][-1]["details"]["reason"] == "merge_refused"
    original(submit(rig, NEXT))
    effect = next(e for e in view(rig)["effects"] if e["effect_id"] == effect_id)
    assert effect["status"] == "cancelled"


def merge_effect(rig):
    return [e for e in view(rig)["effects"] if e["effect_kind"] == "merge_pull_request"][-1]


def advance(rig):
    """Move the kernel clock past the retry backoff of the last attempt."""
    from datetime import datetime, timedelta, timezone
    now = getattr(rig, "now", datetime(2026, 10, 27, tzinfo=timezone.utc)) + timedelta(minutes=6)
    rig.now = now
    rig.kernel._clock = lambda: now.isoformat()


def test_all_green_checks_merge_and_skipped_jobs_do_not_block(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    # Workflow jobs excluded from pull requests (`deploy`, `natural-acceptance`) are skipped.
    github = FakeGitHub(runs=[("harness", "completed", "success"),
                              ("deploy", "completed", "skipped"),
                              ("natural-acceptance", "completed", "skipped"),
                              ("lint", "completed", "neutral")],
                        statuses=[("ci/external", "success")])
    drain(rig, github)
    assert pr(rig)["status"] == "merged" and github.puts() == 1
    checks = pr(rig)["rounds"][0]["checks"]
    assert checks["state"] == "passed"
    assert checks["passed"] == ["Harness CI / harness", "ci/external"]
    assert checks["skipped"] == ["Harness CI / deploy", "Harness CI / lint", "Harness CI / natural-acceptance"]
    assert checks["basis"] == {"base": "main", "workflows_read": [".github/workflows/harness-ci.yml"],
                               "changed_files": 1, "ignored_suites": [], "base_sha": BASE_SHA,
                               "workflow_blobs": {".github/workflows/harness-ci.yml": git_blob(HARNESS_WORKFLOW.encode())},
                               "workflows_from_pr": []}
    assert pr(rig)["merge_blocked"] is None


def test_pending_checks_wait_and_retry_without_a_review_round(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    github = FakeGitHub(runs=[("harness", "in_progress", None), ("relay", "completed", "success")])
    advance(rig)
    drain(rig, github)
    assert github.puts() == 0
    assert merge_effect(rig)["status"] == "observed_absence"
    assert pr(rig)["status"] == "merge_pending"
    assert pr(rig)["rounds"][0]["merge"]["observation"]["reason"] == "checks_pending"
    assert pr(rig)["rounds"][0]["checks"]["pending"] == ["Harness CI (attempt 1)"]
    github.statuses = [("ci/external", "pending")]
    github.runs = GREEN
    advance(rig)
    drain(rig, github)
    assert github.puts() == 0 and pr(rig)["rounds"][0]["checks"]["pending"] == ["ci/external"]
    github.statuses = [("ci/external", "success")]
    advance(rig)
    drain(rig, github)
    assert pr(rig)["status"] == "merged" and github.puts() == 1
    assert pr(rig)["reviewed_rounds"] == 1


@pytest.mark.parametrize("runs,statuses,named", [
    ([("harness", "completed", "failure"), ("relay", "completed", "success")], [],
     "Harness CI / harness: failure"),
    ([("harness", "completed", "cancelled")], [], "Harness CI / harness: cancelled"),
    ([("harness", "completed", "timed_out"), ("relay", "queued", None, "Relay CI")], [],
     "Harness CI / harness: timed_out"),
    (GREEN, [("ci/external", "error")], "ci/external: error"),
])
def test_a_failed_check_blocks_the_merge_and_names_it(tmp_path, runs, statuses, named):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    github = FakeGitHub(runs=runs, statuses=statuses)
    drain(rig, github)
    assert github.puts() == 0
    assert pr(rig)["status"] == "checks_failed"
    assert pr(rig)["merge_blocked"]["reason"] == "checks_failed"
    assert pr(rig)["merge_blocked"]["failed_checks"] == [named]
    waiting = view(rig, "author")["next_action"]
    assert waiting["intent"] == "submit_pull_request"
    assert waiting["reason"] == "pull_request_checks_failed" and waiting["failed_checks"] == [named]
    assert waiting["failed_checks_omitted"] == 0
    assert "override_checks" in waiting["agent_action"]
    assert drain(rig, github) == [] and github.puts() == 0  # The failure is final.


def test_a_cancelled_run_with_green_jobs_still_fails(tmp_path):
    github = FakeGitHub()
    github.run_state[("Harness CI", 0)] = ("completed", "cancelled")
    outcome, details = verdict(tmp_path, github)
    assert outcome == "failure" and details["checks"]["failed"] == ["Harness CI (run): cancelled"]


def test_rerun_checks_keep_the_passed_review_of_the_same_head(tmp_path):
    # 10-29 #1450/#1453: a passed review whose checks failed, rerun green, cost a new round.
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    drain(rig, FakeGitHub(runs=[("harness", "completed", "failure")]))
    result = original(submit(rig, HEAD))
    assert result["review_request"] is None and result["review_reused_from"] == candidate
    assert pr(rig)["status"] == "merge_pending" and pr(rig)["reviewed_rounds"] == 1
    latest = pr(rig)["rounds"][-1]
    assert latest["review"]["score"] == 92 and latest["review"]["reused_from"] == candidate
    assert latest["review_dispatch"] is None
    assert not [e for e in view(rig)["effects"] if e["effect_kind"] == "pr_review_dispatch"
                and e["candidate_id"] == result["candidate_id"]]
    assert error(review(rig, result["candidate_id"], 99)) == "domain_conflict"
    github = FakeGitHub()
    drain(rig, github)
    assert pr(rig)["status"] == "merged" and github.puts() == 1
    assert pr(rig)["reviewed_rounds"] == 1


def test_a_failed_review_still_needs_a_new_head(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 60))
    assert error(submit(rig, HEAD)) == "domain_conflict"


def test_a_passed_third_round_with_failed_checks_merges_without_a_human(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    for head in (NEXT, "c" * 40):
        original(review(rig, candidate, 60))
        candidate = original(submit(rig, head))["candidate_id"]
    original(review(rig, candidate, 95))
    drain(rig, FakeGitHub(head="c" * 40, runs=[("harness", "completed", "failure")]))
    assert pr(rig)["status"] == "checks_failed" and pr(rig)["reviewed_rounds"] == 3
    result = original(submit(rig, "c" * 40))
    assert result["review_reused_from"] == candidate and pr(rig)["status"] == "merge_pending"
    assert pr(rig)["reviewed_rounds"] == 3


@pytest.mark.parametrize("github,reason", [
    # Only skipped jobs: nothing passed.
    (FakeGitHub(runs=[("deploy", "completed", "skipped")]), "checks_missing"),
    # The change selects Harness CI on main, but no run started on the head.
    (FakeGitHub(runs=[]), "checks_pending"),
    (FakeGitHub(checks_down=True), "checks_unavailable"),
])
def test_missing_or_unreadable_checks_never_merge(tmp_path, github, reason):
    rig, candidate = rig_with_pr(tmp_path)
    effect_id = original(review(rig, candidate, 92))["merge_effect_id"]
    for _ in range(8):
        advance(rig)
        drain(rig, github)
        assert pr(rig)["rounds"][0]["merge"]["observation"]["reason"] == reason
    assert github.puts() == 0 and merge_effect(rig)["status"] == "observed_absence"
    blocked = pr(rig)["merge_blocked"]
    assert blocked["reason"] == "merge_retry_exhausted" and blocked["last_reason"] == reason
    owner = view(rig)["next_action"]
    assert owner["intent"] == "retry_effect" and owner["effect_id"] == effect_id
    rig.contexts["repair"] = replace(GATE, allowed_intents=GATE.allowed_intents | {"retry_effect"})
    original(rig.send("retry_effect", {"effect_id": effect_id, "reason": "CI repaired"},
                      handle="repair"))
    advance(rig)
    github.runs, github.checks_down = GREEN, False
    drain(rig, github)
    assert pr(rig)["status"] == "merged" and github.puts() == 1


def test_a_change_no_workflow_selects_merges_without_checks(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    github = FakeGitHub(runs=[], files=["pulse-space/memo/2026-10/note.md", "README.md"])
    drain(rig, github)
    assert pr(rig)["status"] == "merged" and github.puts() == 1
    checks = pr(rig)["rounds"][0]["checks"]
    assert checks == {"state": "not_required", "total": 0, "basis": {
        "base": "main", "workflows_read": [".github/workflows/harness-ci.yml"],
        "changed_files": 2, "ignored_suites": [], "base_sha": BASE_SHA,
        "workflow_blobs": {".github/workflows/harness-ci.yml": git_blob(HARNESS_WORKFLOW.encode())},
                               "workflows_from_pr": []}}


def test_no_checks_required_needs_an_empty_head(tmp_path):
    # Not selected by any workflow, yet a status exists on the head: it is judged.
    github = FakeGitHub(runs=[], files=["README.md"], statuses=[("ci/external", "pending")])
    outcome, details = verdict(tmp_path, github)
    assert (outcome, details["reason"]) == ("absence", "checks_pending")


@pytest.mark.parametrize("workflows,files", [
    ({".github/workflows/harness-ci.yml": "on: [pull_request\n"}, ["README.md"]),
    ({".github/workflows/harness-ci.yml": "jobs: {}\n"}, ["README.md"]),
    ({".github/workflows/harness-ci.yml": "on:\n  pull_request:\n    paths: ['docs/[a-']\n"},
     ["README.md"]),
    ({".github/workflows/harness-ci.yml":
      "on:\n  pull_request:\n    paths: [a]\n    paths-ignore: [b]\n"}, ["README.md"]),
])
def test_an_unreadable_workflow_never_releases_the_merge(tmp_path, workflows, files):
    outcome, details = verdict(tmp_path, FakeGitHub(runs=[], files=files, workflows=workflows))
    assert (outcome, details["reason"]) == ("absence", "checks_unavailable")


def test_an_incomplete_changed_file_list_never_releases_the_merge(tmp_path):
    class Truncated(FakeGitHub):
        def __call__(self, repo, args):
            answer = super().__call__(repo, args)
            if "changed_files" in answer:
                answer["changed_files"] += 1
            return answer
    outcome, details = verdict(tmp_path, Truncated(runs=[], files=["README.md"]))
    assert (outcome, details["reason"]) == ("absence", "checks_unavailable")


def test_workflow_trigger_filters_follow_github_patterns():
    from harness_rewrite.ci_triggers import starts

    def doc(text):
        import yaml
        return yaml.safe_load(text)
    paths = doc("on:\n  pull_request:\n    paths: ['agent-harness/**', '!agent-harness/memo/**']\n")
    assert starts(paths, ["agent-harness/harness_rewrite/x.py"], "main")
    assert not starts(paths, ["agent-harness/memo/x.md"], "main")
    assert not starts(paths, ["README.md"], "main")
    ignore = doc("on:\n  pull_request:\n    paths-ignore: ['**/*.md']\n")
    assert not starts(ignore, ["README.md", "docs/a/b.md"], "main")
    assert starts(ignore, ["README.md", "Scripts/x.py"], "main")
    branches = doc("on:\n  pull_request:\n    branches: [release/*]\n")
    assert not starts(branches, ["x"], "main") and starts(branches, ["x"], "release/1")
    assert starts(doc("on: pull_request\n"), ["x"], "main")
    assert starts(doc("on: [push, pull_request]\n"), ["x"], "main")
    assert not starts(doc("on:\n  push:\n    branches: [main]\n"), ["x"], "main")
    assert not starts(doc("on:\n  pull_request:\n    types: [labeled]\n"), ["x"], "main")
    assert starts(doc("on:\n  pull_request:\n    paths: ['Stats/**', 'ComputeCost/**']\n"),
                  ["ComputeCost/a/b.py"], "main")


def test_the_repository_workflows_are_readable():
    """Every workflow file in this repository parses with these trigger rules."""
    from pathlib import Path

    import yaml

    from harness_rewrite.ci_triggers import starts
    root = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    for path in sorted(root.glob("*.yml")):
        starts(yaml.safe_load(path.read_text()), ["README.md"], "main")
    harness = yaml.safe_load((root / "harness-ci.yml").read_text())
    assert starts(harness, ["agent-harness/harness_rewrite/github_merge.py"], "main")
    assert not starts(harness, ["pulse-space/memo/2026-10/note.md"], "main")


def confirm(rig, candidate, request, **extra):
    data = {"candidate_id": candidate, "reason": "Human accepts the red checks", **extra}
    return rig.approve("confirm_pull_request_merge", data, actor=GATE, request=request)


def test_a_human_overrides_failed_checks_only_explicitly(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    github = FakeGitHub(runs=[("harness", "completed", "failure")])
    drain(rig, github)
    data = {"candidate_id": candidate, "reason": "Override", "override_checks": True}
    assert error(rig.send("confirm_pull_request_merge", data)) == "source_invalid"
    assert error(confirm(rig, candidate, "bad", override_checks="yes")) == "invalid_input"
    # Without the override the human confirmation still requires green checks.
    plain = original(confirm(rig, candidate, "plain"))
    assert plain["override_checks"] is False
    assert "override_checks" not in merge_effect(rig)["snapshot"]
    drain(rig, github)
    assert pr(rig)["status"] == "checks_failed" and github.puts() == 0
    forced = original(confirm(rig, candidate, "forced", override_checks=True))
    assert merge_effect(rig)["snapshot"]["override_checks"] is True
    assert pr(rig)["rounds"][0]["confirmation"]["override_checks"] is True
    github.runs = [("harness", "completed", "failure"), ("relay", "in_progress", None, "Relay CI")]
    drain(rig, github)
    assert github.puts() == 0  # Even an override waits for a running check.
    github.runs = [("harness", "completed", "failure"), ("relay", "completed", "success", "Relay CI")]
    advance(rig)
    drain(rig, github)
    assert pr(rig)["status"] == "merged" and github.puts() == 1
    merged = next(e for e in view(rig)["effects"] if e["effect_id"] == forced["merge_effect_id"])
    assert merged["observations"][-1]["details"]["checks"]["overridden"] is True


def test_a_human_overrides_missing_checks_after_retries_are_spent(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    first = original(review(rig, candidate, 92))["merge_effect_id"]
    github = FakeGitHub(runs=[("deploy", "completed", "skipped")])  # Nothing passed.
    assert error(confirm(rig, candidate, "early", override_checks=True)) == "domain_conflict"
    for _ in range(8):
        advance(rig)
        drain(rig, github)
    original(confirm(rig, candidate, "missing", override_checks=True))
    assert next(e for e in view(rig)["effects"] if e["effect_id"] == first)["status"] == "cancelled"
    advance(rig)
    drain(rig, github)
    assert pr(rig)["status"] == "merged" and github.puts() == 1


def test_an_override_never_passes_unreadable_checks_or_a_moved_head(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    drain(rig, FakeGitHub(runs=[("harness", "completed", "failure")]))
    original(confirm(rig, candidate, "forced", override_checks=True))
    down = FakeGitHub(checks_down=True)
    drain(rig, down)
    assert down.puts() == 0
    assert pr(rig)["rounds"][0]["merge"]["observation"]["reason"] == "checks_unavailable"
    moved = FakeGitHub(head=NEXT)
    advance(rig)
    drain(rig, moved)
    assert moved.puts() == 0
    assert pr(rig)["status"] == "merge_failed"
    assert pr(rig)["rounds"][0]["merge"]["observation"]["reason"] == "head_changed"


MERGE = {"effect_id": "op.merge", "effect_kind": "merge_pull_request", "attempts": [],
         "snapshot": {"repository": REPO, "number": 7, "head_sha": HEAD, "base": "main",
                      "candidate_id": "c"}}


def verdict(tmp_path, github, cache=None):
    """One merge attempt by the adapter: (outcome, details); fresh send markers each time."""
    markers = tmp_path / f"markers-{len(list(tmp_path.glob('markers-*')))}"
    adapter = PullRequestMerge(GATE_POLICY, "/fixture/python", api=github, markers=markers,
                               cache=cache)
    return adapter.apply(MERGE, encoded(MERGE["snapshot"]))


def blob_reads(github):
    return sum("/git/blobs/" in c[0] for c in github.calls)


def test_workflow_files_are_read_once_per_blob_name(tmp_path):
    cache = tmp_path / "workflow-blobs"
    first = FakeGitHub()
    assert verdict(tmp_path, first, cache)[0] == "success" and blob_reads(first) == 1
    again = FakeGitHub()
    assert verdict(tmp_path, again, cache)[0] == "success" and blob_reads(again) == 0
    # The cache is only a copy: bytes that no longer hash to their name are read again.
    (stored,) = cache.iterdir()
    stored.write_text("on:\n  pull_request:\n    paths: ['nothing/**']\n")
    repaired = FakeGitHub()
    assert verdict(tmp_path, repaired, cache)[0] == "success" and blob_reads(repaired) == 1
    assert git_blob(stored.read_bytes()) == stored.name
    # A changed workflow file has a new name and is read; the old entry is never consulted.
    changed = FakeGitHub(workflows={workflow_path("Harness CI"): HARNESS_WORKFLOW + "# edited\n"})
    assert verdict(tmp_path, changed, cache)[0] == "success" and blob_reads(changed) == 1
    assert len(list(cache.iterdir())) == 2
    # Without a usable cache every judgment reads the files; it never fails because of it.
    cache_file = tmp_path / "not-a-directory"
    cache_file.write_text("")
    plain = FakeGitHub()
    assert verdict(tmp_path, plain, cache_file / "blobs")[0] == "success" and blob_reads(plain) == 1


def test_a_truncated_tree_or_a_mismatched_blob_never_releases_the_merge(tmp_path):
    truncated = FakeGitHub(runs=[], files=["README.md"])
    truncated.truncated = True
    assert verdict(tmp_path, truncated)[1]["reason"] == "checks_unavailable"

    class Forged(FakeGitHub):
        def __call__(self, repo, args):
            answer = super().__call__(repo, args)
            if "/git/blobs/" in args[0]:
                answer["content"] = base64.b64encode(b"on: push\n").decode()
            return answer
    assert verdict(tmp_path, Forged(runs=[], files=["README.md"]))[1]["reason"] == "checks_unavailable"


def test_a_base_without_workflows_requires_none(tmp_path):
    github = FakeGitHub(runs=[], files=["README.md"], workflows={})
    outcome, details = verdict(tmp_path, github)
    assert outcome == "success" and details["checks"]["state"] == "not_required"
    assert details["checks"]["basis"]["workflows_read"] == []


def test_checks_of_another_commit_attempt_or_repository_are_unreadable(tmp_path):
    class Elsewhere(FakeGitHub):
        def __call__(self, repo, args):
            answer = super().__call__(repo, args)
            for row in answer.get("jobs", []) + answer.get("check_runs", []):
                row.update(self.change)
            return answer

    for runs, change in [
        (GREEN, {"head_sha": NEXT}),
        (GREEN, {"html_url": "https://github.com/other/repo/runs/1"}),
        (GREEN, {"run_attempt": 2}),
        (GREEN, {"run_id": 1}),
        ([("harness", "completed", "success", "app:other-ci")], {"check_suite": {"id": 1}}),
    ]:
        github = Elsewhere(runs=runs)
        github.change = change
        outcome, details = verdict(tmp_path, github)
        assert (outcome, details["reason"]) == ("absence", "checks_unavailable")
        assert github.puts() == 0


@pytest.mark.parametrize("runs,state", [
    # Disk precheck failed; "re-run" made attempt 2 of the same run, which passed.
    ([("harness", "completed", "failure", "Harness CI", 1),
      ("harness", "completed", "success", "Harness CI", 2)], "passed"),
    # The current attempt decides: a later failure is not hidden by an earlier success.
    ([("harness", "completed", "success", "Harness CI", 1),
      ("harness", "completed", "failure", "Harness CI", 2)], "failed"),
    # A queued rerun is not covered by the earlier success.
    ([("harness", "completed", "success", "Harness CI", 1),
      ("harness", "queued", None, "Harness CI", 2)], "pending"),
    # Another run of the same workflow does not replace this one: both count.
    ([("harness", "completed", "failure", "Harness CI", 1, 0),
      ("harness", "completed", "success", "Harness CI", 1, 1)], "failed"),
    # Jobs of the same name in one attempt all count.
    ([("matrix", "completed", "success"), ("matrix", "completed", "failure")], "failed"),
    # The same job name in another workflow or another app is another check.
    ([("test", "completed", "failure", "Relay CI"), ("test", "completed", "success")], "failed"),
    ([("harness", "completed", "failure", "app:other-ci"), ("harness", "completed", "success")],
     "failed"),
])
def test_each_check_is_judged_by_its_current_attempt(tmp_path, runs, state):
    outcome, details = verdict(tmp_path, FakeGitHub(runs=runs))
    assert details["checks"]["state"] == state
    assert outcome == {"passed": "success", "failed": "failure", "pending": "absence"}[state]


def test_rerun_after_a_failed_disk_precheck_merges_through_the_kernel(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    github = FakeGitHub(runs=[("harness", "completed", "failure", "Harness CI", 1),
                              ("harness", "queued", None, "Harness CI", 2)])
    drain(rig, github)
    assert pr(rig)["rounds"][0]["merge"]["observation"]["reason"] == "checks_pending"
    assert pr(rig)["rounds"][0]["checks"]["pending"] == ["Harness CI (attempt 2)"]
    github.runs[1] = ("harness", "completed", "success", "Harness CI", 2)
    advance(rig)
    drain(rig, github)
    assert pr(rig)["status"] == "merged" and github.puts() == 1


def test_a_status_and_a_check_run_of_the_same_name_stay_separate(tmp_path):
    outcome, details = verdict(tmp_path, FakeGitHub(statuses=[("Harness CI / harness", "failure")]))
    assert outcome == "failure"
    assert details["checks"]["passed"] == ["Harness CI / harness"]
    assert details["checks"]["failed"] == ["Harness CI / harness: failure"]


def suite(app="github-actions", **fields):
    return {"id": 900, "head_sha": HEAD, "conclusion": None,
            "app": {"id": 15368 if app == "github-actions" else 9, "slug": app}, **fields}


@pytest.mark.parametrize("status,count,reason", [
    # Proven empty: ignored once completed, pending while queued for its run.
    ("completed", 0, None),
    ("queued", 0, "checks_pending"),
    # Checks without their workflow run cannot be judged, whatever the suite's status.
    ("completed", 1, "checks_unavailable"),
    ("queued", 2, "checks_unavailable"),
    ("completed", None, "checks_unavailable"),
])
def test_an_actions_suite_without_its_run_is_ignored_only_when_proven_empty(
        tmp_path, status, count, reason):
    github = FakeGitHub()
    github.extra_suites.append(suite(status=status, latest_check_runs_count=count))
    outcome, details = verdict(tmp_path, github)
    if reason is None:
        assert outcome == "success"
        assert details["checks"]["basis"]["ignored_suites"] == [
            {"app": "github-actions", "suite": 900, "status": "completed"}]
    else:
        assert (outcome, details["reason"]) == ("absence", reason)
    if reason == "checks_pending":
        assert details["checks"]["pending"] == ["github-actions (suite 900)"]


def test_a_run_without_its_suite_or_disagreeing_with_it_is_unavailable(tmp_path):
    class Unsuited(FakeGitHub):
        def __call__(self, repo, args):
            answer = super().__call__(repo, args)
            if "/check-suites?" in args[0]:
                answer.update(check_suites=[], total_count=0)  # A run without its suite.
            return answer
    assert verdict(tmp_path, Unsuited())[1]["reason"] == "checks_unavailable"

    class Disagreeing(FakeGitHub):
        def model(self):
            suites, runs, jobs, apps = super().model()
            suites[0] = {**suites[0], "conclusion": "failure"}  # The run says success.
            return suites, runs, jobs, apps
    assert verdict(tmp_path, Disagreeing())[1]["reason"] == "checks_unavailable"


@pytest.mark.parametrize("count,checks", [
    (0, [("preview", "completed", "failure", "app:vercel")]),  # Count says empty, listing not.
    (2, [("preview", "completed", "success", "app:vercel")]),  # Count says more than listed.
])
def test_an_app_suite_count_must_match_its_listing(tmp_path, count, checks):
    class Miscounted(FakeGitHub):
        def model(self):
            suites, runs, jobs, apps = super().model()
            index = next(i for i, s in enumerate(suites) if s["app"]["slug"] == "vercel")
            suites[index] = {**suites[index], "latest_check_runs_count": count}
            return suites, runs, jobs, apps
    outcome, details = verdict(tmp_path, Miscounted(runs=GREEN + checks))
    assert (outcome, details["reason"]) == ("absence", "checks_unavailable")


def test_an_override_never_passes_a_miscounted_suite(tmp_path):
    class Miscounted(FakeGitHub):
        def model(self):
            suites, runs, jobs, apps = super().model()
            return [{**s, "latest_check_runs_count": 0} if s["app"]["slug"] == "vercel" else s
                    for s in suites], runs, jobs, apps
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    drain(rig, FakeGitHub(runs=[("harness", "completed", "failure")]))
    original(confirm(rig, candidate, "forced", override_checks=True))
    github = Miscounted(runs=[("harness", "completed", "failure"),
                              ("preview", "completed", "failure", "app:vercel")])
    drain(rig, github)
    assert github.puts() == 0
    assert pr(rig)["rounds"][0]["merge"]["observation"]["reason"] == "checks_unavailable"


def test_workflows_are_absent_only_when_proven(tmp_path):
    def docs(**fields):
        github = FakeGitHub(runs=[], files=["README.md"], workflows={})
        for key, value in fields.items():
            setattr(github, key, value)
        return github
    # Proven: the root has no `.github`, or `.github` has no `workflows`.
    for github in (docs(), docs(github_dir={"items": [
            {"name": "CODEOWNERS", "path": ".github/CODEOWNERS", "type": "file"}]})):
        outcome, details = verdict(tmp_path, github)
        assert outcome == "success" and details["checks"]["state"] == "not_required"
        assert details["checks"]["basis"]["base_sha"] == BASE_SHA
    # Not proven: a malformed or refused answer is unreadable, never "no workflows".
    for github in (
        docs(github_dir={"message": "Not Found"}),  # `.github` listing without entries.
        docs(github_dir={"items": [{"name": "workflows", "path": ".github/workflows",
                                    "type": "dir"}]}),  # It exists, yet was refused.
        docs(github_dir={"items": [{"name": "workflows", "type": "file"}]}),
        docs(github_type="file", github_dir={"items": []}),
        docs(directory_answer={"message": "moved"}),  # A workflow listing without entries.
        docs(directory_answer={"items": [{"path": "x.yml"}]}),  # Entries without name or type.
    ):
        outcome, details = verdict(tmp_path, github)
        assert (outcome, details["reason"]) == ("absence", "checks_unavailable"), github.__dict__


def test_app_placeholder_suites_are_not_checks(tmp_path):
    """Real PULSE heads carry queued Vercel, Claude and Cursor suites that never run."""
    placeholders = [suite(app=name, id=900 + i, status="queued", latest_check_runs_count=0)
                    for i, name in enumerate(["vercel", "claude", "cursor"])]
    github = FakeGitHub()
    github.extra_suites += placeholders
    outcome, details = verdict(tmp_path, github)
    assert outcome == "success" and details["checks"]["state"] == "passed"
    assert [row["app"] for row in details["checks"]["basis"]["ignored_suites"]] == [
        "vercel", "claude", "cursor"]
    # A documentation-only head with only placeholders needs no checks.
    docs = FakeGitHub(runs=[], files=["pulse-space/memo/2026-10/note.md"])
    docs.extra_suites += placeholders
    outcome, details = verdict(tmp_path, docs)
    assert outcome == "success" and details["checks"]["state"] == "not_required"
    assert not any("/actions/workflows" in c[0] for c in docs.calls)  # No registry listing.
    # An app suite that has a check run is judged, and waits while unfinished.
    busy = FakeGitHub(runs=GREEN + [("preview", "in_progress", None, "app:vercel")])
    outcome, details = verdict(tmp_path, busy)
    assert (outcome, details["reason"]) == ("absence", "checks_pending")


def test_an_unfinished_suite_of_a_finished_run_is_pending(tmp_path):
    github = FakeGitHub()

    class Lagging(FakeGitHub):
        def model(self):
            suites, runs, jobs, apps = super().model()
            suites[0] = {**suites[0], "status": "in_progress"}
            return suites, runs, jobs, apps
    outcome, details = verdict(tmp_path, Lagging())
    assert (outcome, details["reason"]) == ("absence", "checks_pending")
    assert verdict(tmp_path, github)[0] == "success"


def test_checks_that_change_while_read_are_void(tmp_path):
    github = FakeGitHub()
    reads = []

    def drift(kind, page):
        if kind == "check-suites":
            reads.append(page)
            if len(reads) == 2:  # The confirming read after every run was judged.
                github.runs.append(("harness", "queued", None, "Harness CI", 2))
    github.drift = drift
    outcome, details = verdict(tmp_path, github)
    assert (outcome, details["reason"]) == ("absence", "checks_unavailable")
    assert github.puts() == 0


def test_long_check_lists_are_paged_completely(tmp_path):
    runs = [(f"job-{i:03}", "completed", "success") for i in range(150)]
    statuses = [(f"ci/{i:03}", "success") for i in range(120)]
    github = FakeGitHub(runs=runs, statuses=statuses)
    outcome, details = verdict(tmp_path, github)
    assert outcome == "success" and details["checks"]["total"] == 270
    assert sum("/jobs?" in c[0] for c in github.calls) == 2
    assert sum("/status?" in c[0] for c in github.calls) == 2
    assert details["checks"]["omitted"] == {"passed": 220}


def test_a_list_beyond_the_page_limit_is_unavailable(tmp_path):
    runs = [(f"job-{i:03}", "completed", "success") for i in range(301)]
    outcome, details = verdict(tmp_path, FakeGitHub(runs=runs))
    assert (outcome, details["reason"]) == ("absence", "checks_unavailable")


@pytest.mark.parametrize("move", ["duplicate", "grow"])
def test_a_list_that_moves_while_paged_never_merges(tmp_path, move):
    # The page-1 job is failing; a list that shifts must not drop it from the verdict.
    runs = [("broken", "completed", "failure", "Harness CI", 1, 0, 10)] + [
        (f"job-{i:03}", "completed", "success", "Harness CI", 1, 0, 100 + i) for i in range(149)]
    github = FakeGitHub(runs=runs)

    def drift(kind, page):
        if kind == "jobs" and page == 2 and len(github.runs) == 150:
            github.runs.insert(0, ("new", "completed", "success", "Harness CI", 1, 0, 999))
            if move == "duplicate":
                github.runs.pop()  # Same total; page 2 repeats the last row of page 1.
    github.drift = drift
    outcome, details = verdict(tmp_path, github)
    assert (outcome, details["reason"]) == ("absence", "checks_unavailable")
    assert github.puts() == 0


def test_many_failed_checks_are_counted_beyond_the_kept_names(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 92))
    drain(rig, FakeGitHub(runs=[(f"job-{i:02}", "completed", "failure") for i in range(60)]))
    blocked = pr(rig)["merge_blocked"]
    assert len(blocked["failed_checks"]) == 50 and blocked["failed_checks_omitted"] == 10
    assert pr(rig)["rounds"][0]["checks"]["omitted"] == {"failed": 10}
    assert view(rig, "author")["next_action"]["failed_checks_omitted"] == 10


def test_records_from_before_the_check_gate_still_read(tmp_path):
    """Merge observations without `checks` and confirmations without `override_checks`."""
    from harness_rewrite import pull_requests
    at = "2026-10-26T00:00:00+00:00"
    effect = {"effect_id": "m", "effect_kind": "merge_pull_request", "status": "observed_failure",
              "attempts": [{"id": "a", "at": at}],
              "observations": [{"details": {"reason": "head_changed"}}],
              "snapshot": {"repository": REPO, "number": 7, "head_sha": HEAD, "base": "main",
                           "candidate_id": "c"}}
    item = {"candidate_id": "c", "head_sha": HEAD, "status": "awaiting_review",
            "author": {"actor": "pulse", "session": "s"}, "author_model": "claude-opus-5-5",
            "authors": None, "review_block": "three_rounds", "author_compute": None,
            "review_request": None, "review_dispatch": None, "submitted_at": at, "review": None,
            "confirmation": {"reason": "Human", "source_digest": "0" * 64, "actor": "pulse",
                             "at": at},
            "merge_effect": "m"}
    state = {"effects": {"m": effect},
             "pull_requests": {"k": {"repository": REPO, "number": 7, "rounds": [item]}}}
    row = pull_requests.public(state)[0]
    assert row["status"] == "merge_failed" and row["merge_blocked"] is None
    assert row["rounds"][0]["checks"] is None
    assert "override_checks" not in row["rounds"][0]["confirmation"]
    effect.update(status="observed_absence", observations=[{"details": {"reason": "merge_refused"}}],
                  attempts=[{"id": str(i), "at": at} for i in range(8)])
    blocked = pull_requests.public(state)[0]["merge_blocked"]
    assert blocked["reason"] == "merge_retry_exhausted" and blocked["pending_checks"] == []
    # An old snapshot has no override: failed checks block it.
    assert verdict(tmp_path, FakeGitHub(runs=[("harness", "completed", "failure")]))[0] == "failure"

@pytest.mark.parametrize("entry", [
    {"name": "harness-ci.yml", "type": "file", "sha": "e" * 40},  # No path.
    {"name": "harness-ci.yml", "type": "file", "path": ".github/workflows/other.yml",
     "sha": "e" * 40},  # Path contradicts the name.
    {"name": "harness-ci.yml", "type": "file", "path": ".github/workflows/harness-ci.yml"},
    {"name": "harness-ci.yml", "type": "file", "path": ".github/workflows/harness-ci.yml",
     "sha": "not-a-blob"},
    {"name": "a/b.yml", "type": "file", "path": ".github/workflows/a/b.yml", "sha": "e" * 40},
])
def test_an_incomplete_workflow_directory_entry_is_unreadable(tmp_path, entry):
    # Code changed and no check exists: skipping the entry would wrongly pass the head.
    github = FakeGitHub(runs=[], files=["agent-harness/x.py"])
    github.directory_answer = {"items": [entry]}
    outcome, details = verdict(tmp_path, github)
    assert (outcome, details["reason"]) == ("absence", "checks_unavailable")


@pytest.mark.parametrize("runs,count,state,conclusion", [
    # The suite declares a check, the attempt lists no job, the run says success.
    ([("harness", "completed", "success")], 1, "unavailable", None),
    # More or fewer jobs than the suite's checks.
    ([("harness", "completed", "success")], 2, "unavailable", None),
    ([("a", "completed", "success"), ("b", "completed", "success")], 1, "unavailable", None),
])
def test_jobs_must_account_for_the_suite_checks(tmp_path, runs, count, state, conclusion):
    class Emptied(FakeGitHub):
        def model(self):
            suites, workflow_runs, jobs, apps = super().model()
            if count == 1 and len(runs) == 1:
                jobs = {}  # The jobs endpoint answers with no job.
            return suites, workflow_runs, jobs, apps
    github = Emptied(runs=runs)
    github.counts[("Harness CI", 0)] = count
    outcome, details = verdict(tmp_path, github)
    assert (outcome, details["reason"]) == ("absence", "checks_unavailable")


def test_a_run_without_jobs_is_judged_by_its_own_failure_only(tmp_path):
    class Jobless(FakeGitHub):
        def model(self):
            suites, runs, jobs, apps = super().model()
            return suites, runs, {}, apps
    failed = Jobless()
    failed.run_state[("Harness CI", 0)] = ("completed", "startup_failure")
    failed.counts[("Harness CI", 0)] = 0
    outcome, details = verdict(tmp_path, failed)
    assert outcome == "failure" and details["checks"]["failed"] == ["Harness CI (run): startup_failure"]
    claimed = Jobless()
    claimed.counts[("Harness CI", 0)] = 0  # No check, no job, yet "success".
    assert verdict(tmp_path, claimed)[1]["reason"] == "checks_unavailable"


@pytest.mark.parametrize("runs,state", [
    # Re-run failed jobs: attempt 2 lists only `a`; `b` keeps its attempt-1 success.
    ([("a", "completed", "failure", "Harness CI", 1), ("b", "completed", "success", "Harness CI", 1),
      ("a", "completed", "success", "Harness CI", 2)], "passed"),
    # Re-run of one job only: `b` failed in attempt 1 and was not re-run.
    ([("a", "completed", "failure", "Harness CI", 1), ("b", "completed", "failure", "Harness CI", 1),
      ("a", "completed", "success", "Harness CI", 2)], "failed"),
])
def test_a_partial_rerun_keeps_the_jobs_it_did_not_repeat(tmp_path, runs, state):
    outcome, details = verdict(tmp_path, FakeGitHub(runs=runs))
    assert details["checks"]["state"] == state
    if state == "failed":
        assert details["checks"]["failed"] == ["Harness CI / b: failure"]


NEW_CI = "on:\n  pull_request:\n    paths: ['docs/**']\n"


def test_a_workflow_the_pr_adds_must_run(tmp_path):
    # Platform CI passed; the PR's own new workflow selects its change but has no run yet.
    github = FakeGitHub(files=["docs/guide.md"], runs=[("contracts", "completed", "success",
                                                        "Platform CI")])
    github.pr_workflows = {".github/workflows/new-ci.yml": NEW_CI}
    outcome, details = verdict(tmp_path, github)
    assert (outcome, details["reason"]) == ("absence", "checks_pending")
    assert details["checks"]["pending"] == [".github/workflows/new-ci.yml (not started)"]
    assert details["checks"]["basis"]["workflows_from_pr"] == [".github/workflows/new-ci.yml"]
    # Once it runs and passes, the head merges.
    github.runs.append(("docs", "completed", "success", "New CI"))
    assert verdict(tmp_path, github)[0] == "success"


def test_the_pr_version_of_a_workflow_replaces_the_base_version(tmp_path):
    cache = tmp_path / "blobs"
    narrowed = HARNESS_WORKFLOW.replace("agent-harness/**", "agent-harness/never/**")
    # The PR narrows Harness CI's paths: its own version decides, nothing is required.
    github = FakeGitHub(runs=[], files=["agent-harness/x.py"])
    github.pr_workflows = {workflow_path("Harness CI"): narrowed}
    outcome, details = verdict(tmp_path, github, cache)
    assert outcome == "success" and details["checks"]["state"] == "not_required"
    assert details["checks"]["basis"]["workflow_blobs"] == {
        workflow_path("Harness CI"): git_blob(narrowed.encode())}
    assert (cache / git_blob(narrowed.encode())).is_file()  # Cached by blob name as well.
    # A workflow the PR removes is not required either.
    removed = FakeGitHub(runs=[], files=["agent-harness/x.py"])
    removed.pr_workflows = {workflow_path("Harness CI"): None}
    assert verdict(tmp_path, removed)[1]["checks"]["state"] == "not_required"


@pytest.mark.parametrize("text", ["on: [pull_request\n", "jobs: {}\n"])
def test_an_unreadable_workflow_from_the_pr_never_releases_the_merge(tmp_path, text):
    github = FakeGitHub(runs=[], files=["README.md"])
    github.pr_workflows = {".github/workflows/new-ci.yml": text}
    outcome, details = verdict(tmp_path, github)
    assert (outcome, details["reason"]) == ("absence", "checks_unavailable")


def test_merge_adapter_readback_outcomes(tmp_path):
    def adapter(github):
        return PullRequestMerge(GATE_POLICY, "/fixture/python", api=github, markers=tmp_path)
    effect = {"effect_id": "op.merge", "effect_kind": "merge_pull_request", "attempts": [{"id": "a"}],
              "snapshot": {"repository": REPO, "number": 7, "head_sha": HEAD, "base": "main",
                           "candidate_id": "c"}}
    adapter(FakeGitHub()).mark(effect, 0)
    assert adapter(FakeGitHub()).readback(effect)[0] == "absence"
    assert adapter(FakeGitHub(state="closed")).readback(effect)[1]["reason"] == "pull_request_closed"
    assert adapter(FakeGitHub(merged=True, on_main=False)).readback(effect)[0] == "unknown"
    assert adapter(FakeGitHub(merged=True, head=NEXT)).readback(effect)[1]["reason"] == "merged_different_head"
    assert adapter(FakeGitHub(repo="other/repo")).readback(effect)[0] == "unknown"
    assert adapter(FakeGitHub()).apply(effect, b"{}")[1]["reason"] == "fixed_merge_intention_mismatch"
    assert adapter(FakeGitHub()).observe(REPO, 7) == {
        k: v for k, v in opened(HEAD).items() if k != "authors"}
    assert adapter(FakeGitHub(repo="other/repo")).observe(REPO, 7) is None


def test_gate_policy_shape():
    from harness_rewrite.github_merge import shape
    shape({**GATE_POLICY, "merge_method": "merge",
           "repositories": [REPO, "bigbird-online/CollaborateWithAI", "bigbird-online/Oikos"]})
    for bad in (
        {**GATE_POLICY, "pulse_gh": "relative.py"},
        {**GATE_POLICY, "gh": "gh"},
        {key: v for key, v in GATE_POLICY.items() if key != "relay_paths"},
        {**GATE_POLICY, "repositories": []},
        {**GATE_POLICY, "repositories": [REPO, REPO.upper()]},
        {**GATE_POLICY, "merge_method": "force"},
        {**GATE_POLICY, "token": "x"},
    ):
        with pytest.raises(ValueError):
            shape(bad)


def test_an_old_reader_refuses_new_pull_request_events(tmp_path, monkeypatch):
    rig, _ = rig_with_pr(tmp_path)
    from harness_rewrite import pull_requests
    monkeypatch.setattr(pull_requests, "apply_event", lambda state, kind, data: False)
    refused = view(rig)
    assert refused["status"] == "rejected"
    assert refused["error"]["code"] == "journal_reader_upgrade_required"


def test_the_released_reader_refuses_pull_request_events(tmp_path):
    """The kernel on main before this gate, not a patched function, reads the events."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    old = tmp_path / "released"
    old.mkdir()
    try:
        # The last main commit before the gate merged (#1403 merge); fixed, not merge-base,
        # because the gate itself is on main now.
        base = "e7fec2599de6f82a9b16449cb97bf89cef0668fe"
        archive = subprocess.run(["git", "-C", str(root), "archive", base, "agent-harness/harness_rewrite"],
                                 check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("released kernel source unavailable")
    subprocess.run(["tar", "-x", "-C", str(old)], input=archive, check=True)
    rig, _ = rig_with_pr(tmp_path)
    import sqlite3
    rows = sqlite3.connect(tmp_path / "journal.sqlite").execute(
        "SELECT content FROM journal WHERE wi=? ORDER BY revision", (rig.wi,)).fetchall()
    events = [e for (content,) in rows for e in json.loads(content)["events"]]
    assert any(e["kind"] == "PullRequestSubmitted" for e in events)
    script = ("import json,sys; sys.path.insert(0, sys.argv[1]);"
              "from harness_rewrite.domain import reduce; from harness_rewrite.model import Rejected\n"
              "try:\n reduce(None, json.load(sys.stdin)); print('read')\n"
              "except Rejected as exc: print(exc.code)")
    result = subprocess.run([sys.executable, "-I", "-c", script, str(old / "agent-harness")],
                            input=json.dumps(events), capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "journal_reader_upgrade_required"


def test_production_ledger_keeps_one_wi_per_pull_request_across_clients(tmp_path):
    from harness_rewrite import Kernel
    from tests_rewrite.test_production_ledger import Rig as Authority, workflow_rig

    authority = Authority(tmp_path)
    rig = workflow_rig(authority, "lightweight")
    rig.contexts["owner"] = replace(rig.contexts["owner"], allowed_intents=GATE.allowed_intents)
    original(rig.send("start_work", {}))
    original(submit(rig, HEAD))
    other = rig.kernel.execute(
        Command("second-light", "create_lightweight", {**DEFINITION, "why": "CR-ONE: second"}),
        "create")
    first, rig.wi = rig.wi, original(other)["work_item_id"]
    original(rig.send("start_work", {}))
    assert error(submit(rig, NEXT)) == "domain_conflict"
    rig.kernel = Kernel(None, rig.contexts.__getitem__, storage=authority.backend(authority.b))
    assert error(submit(rig, NEXT)) == "domain_conflict"
    second, rig.wi = rig.wi, first
    original(rig.send("withdraw_pull_request",
                      {"repository": REPO, "number": 7, "reason": "Wrong WI"}))
    rig.wi = second
    original(submit(rig, NEXT))  # The ledger claim follows the withdrawal on both clients.
    rig.wi = first
    assert error(submit(rig, "c" * 40)) == "domain_conflict"


def test_a_same_head_retry_after_failed_checks_is_a_new_request_not_a_changed_flag(tmp_path):
    # The request ID, not `immediately`, makes the new attempt: a first registration that
    # already carried immediately:true, replayed after rerun checks, returns its original result.
    rig = light(tmp_path)
    rig.markers = tmp_path / "merge-markers"
    rig.contexts["author"] = AUTHOR
    rig.contexts["reviewer"] = REVIEWER
    rig.contexts["owner"] = GATE
    candidate = original(submit(rig, HEAD, request="submit-7-first", immediately=True))["candidate_id"]
    original(review(rig, candidate, 92))
    drain(rig, FakeGitHub(runs=[("harness", "completed", "failure")]))
    assert pr(rig)["status"] == "checks_failed"
    replay = submit(rig, HEAD, request="submit-7-first", immediately=True)
    assert original(replay)["candidate_id"] == candidate
    assert pr(rig)["status"] == "checks_failed" and len(pr(rig)["rounds"]) == 1
    retry = original(submit(rig, HEAD, request="submit-7-retry-2", immediately=True))
    assert retry["review_reused_from"] == candidate and pr(rig)["status"] == "merge_pending"
    second = pr(rig)["rounds"][-1]["candidate_id"]
    # Recovered once, failed again: the recovery request is just as spent.
    drain(rig, FakeGitHub(runs=[("harness", "completed", "failure")]))
    assert pr(rig)["status"] == "checks_failed"
    assert original(submit(rig, HEAD, request="submit-7-retry-2", immediately=True))["candidate_id"] == second
    assert pr(rig)["status"] == "checks_failed"
    original(submit(rig, HEAD, request="submit-7-retry-3", immediately=True))
    assert pr(rig)["status"] == "merge_pending" and pr(rig)["reviewed_rounds"] == 1


def fixed_after_three_rounds(rig, candidate):
    """Three rejected rounds, then a fixed head that goes to confirmation, not a review."""
    for head in ("c" * 40, "d" * 40, None):
        original(review(rig, candidate, 70))
        if head:
            candidate = original(submit(rig, head))["candidate_id"]
    rejected = candidate
    fixed = original(submit(rig, "e" * 40))["candidate_id"]
    assert pr(rig)["rounds"][-1]["review_block"] == "three_rounds"
    return rejected, fixed


def standing_confirm(rig, candidate, request=None, **extra):
    return rig.send("confirm_pull_request_merge",
                    {"candidate_id": candidate, "reason": "Fixed the third round findings", **extra},
                    handle="author", request=request)


def test_a_standing_merge_authority_merges_a_head_fixed_after_three_rounds_on_green_checks(tmp_path):
    rig = light(tmp_path)
    rig.markers = tmp_path / "merge-markers"
    rig.contexts.update(author=AUTHOR, reviewer=REVIEWER, owner=GATE)
    # The grant needs the human's own decision and lives on its carrier WI.
    reason = {"reason": "大鸟 11-05: 修完直接合并"}
    assert error(rig.send("grant_merge_authority", reason)) == "source_invalid"
    grant = original(rig.approve("grant_merge_authority", reason, actor=GATE))["grant_id"]
    assert view(rig)["result"]["current_observation"]["merge_authority"][0]["active"] is True
    # It serves the Agent on another WI it holds.
    created = rig.kernel.execute(Command("light-b", "create_lightweight",
                                         {**DEFINITION, "resources": ["other"]}), "create")
    carrier, rig.wi = rig.wi, original(created)["work_item_id"]
    original(rig.send("start_work", {}))
    candidate = original(submit(rig, HEAD))["candidate_id"]
    _, fixed = fixed_after_three_rounds(rig, candidate)
    assert pr(rig)["status"] == "human_confirmation_required"
    assert view(rig, "author")["next_action"]["agent_action"].startswith("A standing merge")
    # Never an override of the checks.
    assert error(standing_confirm(rig, fixed, override_checks=True)) == "source_invalid"
    confirmed = original(standing_confirm(rig, fixed))
    confirmation = pr(rig)["rounds"][-1]["confirmation"]
    assert confirmation["merge_authority"] == {"grant_id": grant, "work_item_id": carrier}
    assert "override_checks" not in merge_effect(rig)["snapshot"]
    assert confirmed["override_checks"] is False
    # One confirmation per head: a second use of the same grant finds it confirmed.
    assert error(standing_confirm(rig, fixed, request="twice")) == "source_invalid"
    # The merge waits for every check of the fixed head to pass.
    github = FakeGitHub(head="e" * 40, runs=[("harness", "in_progress", None)])
    drain(rig, github)
    assert pr(rig)["status"] == "merge_pending" and github.puts() == 0
    github.runs = GREEN
    advance(rig)
    drain(rig, github)
    assert pr(rig)["status"] == "merged" and github.puts() == 1


def test_a_standing_merge_authority_never_passes_failed_checks(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(rig.approve("grant_merge_authority", {"reason": "Standing"}, actor=GATE))
    _, fixed = fixed_after_three_rounds(rig, candidate)
    original(standing_confirm(rig, fixed))
    red = FakeGitHub(head="e" * 40, runs=[("harness", "completed", "failure")])
    drain(rig, red)
    assert pr(rig)["status"] == "checks_failed" and red.puts() == 0
    # Blocked checks still need a new head or the human.
    assert error(standing_confirm(rig, fixed, request="again")) == "source_invalid"


def test_a_standing_merge_authority_never_covers_the_rejected_third_round_head(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(rig.approve("grant_merge_authority", {"reason": "Standing"}, actor=GATE))
    for head in ("c" * 40, "d" * 40, None):
        original(review(rig, candidate, 70))
        if head:
            candidate = original(submit(rig, head))["candidate_id"]
    assert pr(rig)["status"] == "human_confirmation_required"
    assert "agent_action" not in view(rig, "author")["next_action"]
    assert error(standing_confirm(rig, candidate)) == "source_invalid"


def test_a_revoked_merge_authority_no_longer_stands_in_for_the_human(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    grant = original(rig.approve("grant_merge_authority", {"reason": "Standing"}, actor=GATE))["grant_id"]
    revoke = {"grant_id": grant, "reason": "大鸟 withdrew it"}
    rig.contexts["stranger"] = replace(GATE, actor="another-agent")
    assert error(rig.send("revoke_merge_authority", revoke, handle="stranger")) == "authority_denied"
    original(rig.send("revoke_merge_authority", revoke))
    assert error(rig.send("revoke_merge_authority", revoke)) == "domain_conflict"
    shown = view(rig)["result"]["current_observation"]["merge_authority"][0]
    assert shown["active"] is False and shown["revocation"]["source"] == "grant_actor"
    _, fixed = fixed_after_three_rounds(rig, candidate)
    assert error(standing_confirm(rig, fixed)) == "source_invalid"
    original(rig.approve("confirm_pull_request_merge",
                         {"candidate_id": fixed, "reason": "Human confirms"}, actor=GATE))


def test_a_merge_authority_revoked_after_it_was_found_does_not_confirm(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    grant = original(rig.approve("grant_merge_authority", {"reason": "Standing"}, actor=GATE))["grant_id"]
    _, fixed = fixed_after_three_rounds(rig, candidate)
    found = rig.kernel._merge_authority

    def revoked_meanwhile(context):
        grants = found(context)
        original(rig.send("revoke_merge_authority", {"grant_id": grant, "reason": "Withdrawn"}))
        return grants

    rig.kernel._merge_authority = revoked_meanwhile
    assert error(standing_confirm(rig, fixed)) == "source_invalid"
    assert pr(rig)["rounds"][-1]["confirmation"] is None
    assert not any(e["effect_kind"] == "merge_pull_request" and e["status"] == "requested"
                   for e in view(rig)["effects"])


def test_one_human_approval_registers_one_merge_authority_even_after_revocation(tmp_path):
    rig = light(tmp_path)
    rig.contexts["owner"] = GATE
    standing = {"reason": "Standing"}
    grant = original(rig.approve("grant_merge_authority", standing, actor=GATE))["grant_id"]
    again = rig.approve("grant_merge_authority", standing, request="again", actor=GATE)
    assert error(again) == "domain_conflict"
    original(rig.send("revoke_merge_authority", {"grant_id": grant, "reason": "Withdrawn"}))
    replayed = rig.approve("grant_merge_authority", standing, request="replayed", actor=GATE)
    assert error(replayed) == "domain_conflict"
    # Nor on another WI the Agent holds.
    created = rig.kernel.execute(Command("light-b", "create_lightweight",
                                         {**DEFINITION, "resources": ["other"]}), "create")
    rig.wi = original(created)["work_item_id"]
    original(rig.send("start_work", {}))
    elsewhere = rig.approve("grant_merge_authority", standing, request="elsewhere", actor=GATE)
    assert error(elsewhere) == "domain_conflict"
    assert view(rig)["result"]["current_observation"]["merge_authority"] == []


def test_a_standing_merge_authority_confirms_only_from_the_holder_session(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(rig.approve("grant_merge_authority", {"reason": "Standing"}, actor=GATE))
    _, fixed = fixed_after_three_rounds(rig, candidate)
    data = {"candidate_id": fixed, "reason": "Fixed the third round findings"}
    for name, context in (
        ("parallel", replace(AUTHOR, session="another-session-of-the-agent")),
        ("stale", replace(AUTHOR, generation=AUTHOR.generation + 1)),
    ):
        rig.contexts[name] = context
        assert error(rig.send("confirm_pull_request_merge", data, handle=name)) in {
            "authority_denied", "authority_changed"}
    assert pr(rig)["rounds"][-1]["confirmation"] is None
    assert not any(e["effect_kind"] == "merge_pull_request" for e in view(rig)["effects"])
    github = FakeGitHub(head="e" * 40, runs=GREEN)
    drain(rig, github)
    assert github.puts() == 0 and pr(rig)["status"] == "human_confirmation_required"
    # The holder session itself confirms.
    original(standing_confirm(rig, fixed, request="holder"))
    assert pr(rig)["rounds"][-1]["confirmation"]["merge_authority"]


def test_an_explicit_human_confirmation_keeps_its_holder_agent_rule(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    original(rig.approve("grant_merge_authority", {"reason": "Standing"}, actor=GATE))
    _, fixed = fixed_after_three_rounds(rig, candidate)
    data = {"candidate_id": fixed, "reason": "Human confirms"}
    stranger = replace(GATE, actor="another-agent")
    assert error(rig.approve("confirm_pull_request_merge", data, actor=stranger)) == "authority_denied"
    other_session = replace(GATE, session="another-session-of-the-agent")
    confirmed = original(rig.approve("confirm_pull_request_merge", data, request="human",
                                     actor=other_session))
    assert "merge_authority" not in pr(rig)["rounds"][-1]["confirmation"]
    assert confirmed["override_checks"] is False
