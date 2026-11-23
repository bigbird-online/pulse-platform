"""PR output gate: a registered PR, a heterogeneous flagship review, a kernel merge.

The author's session registers one open, unmerged pull request head on its WI
(standard or lightweight); a repository and PR number belong to one WI only. The
author is the session whose own native record shows it created that head commit,
or a sub-agent whose spawning that record proves, with the model of that turn;
registering for any other session's commit is refused.
Another session of the same Agent, on the other model family, in the exact
current flagship of the Relay `review` profile and at reasoning effort high or
above, that did not commit any head of the PR, records a 0-100 score. A passing
score (85 or more) requests the `merge_pull_request` effect bound to the reviewed
head commit. After three reviewed rounds, passed or not, only a human decision
on the latest head can request the merge; for a head registered after them, the
Agent's standing merge authority (`merge_authority`) may stand in. The merge waits for every check on the
reviewed head to pass; a failed check blocks it until a new head or a human
confirmation that explicitly overrides the checks. Registering the same head again
after its checks are rerun keeps that head's passed review (not a new round) and
requests the merge again. Reviewer facts come only from the
Host (the session's own native record and the Relay profile resolution) and are
frozen in the first committed decision; a missing record makes the reviewer
ineligible.

Graph candidates and the independent Pi verifier are unchanged; this gate keeps
its own records and never sets the WI's active candidate.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from . import guide
from .model import Decision, HostContext, Json, Rejected, fields, human_decision
from .publication import commit
from .workflow import nonempty

INTENTS = frozenset(
    {
        "submit_pull_request",
        "withdraw_pull_request",
        "receive_pull_request_review",
        "record_pull_request_review",
        "confirm_pull_request_merge",
    }
)
# Intents whose Host context carries the caller's observed computation.
COMPUTE_INTENTS = frozenset(
    {"submit_pull_request", "receive_pull_request_review", "record_pull_request_review"}
)
MERGE_KIND = "merge_pull_request"
# Durable review request; the Relay review gate (`pr-review-dispatch`) executes it.
DISPATCH_KIND = "pr_review_dispatch"
PASS_SCORE = 85
MAX_ROUNDS = 3
REVIEW_PROFILE = "review"
REVIEW_EFFORTS = frozenset({"high", "xhigh", "max"})
# The reviewer runs on the runtime of the other model family.
# A Gemini author (agy) is reviewed in Codex: the GPT family, a flagship review runtime.
REVIEW_RUNTIME = {"claude": "codex", "gpt": "cc", "gemini": "codex"}
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def family(model: object) -> str | None:
    if not isinstance(model, str):
        return None
    value = model.lower()
    if value.startswith("claude-"):
        return "claude"
    if value.startswith(("gpt-", "codex-")):
        return "gpt"
    if value.startswith("gemini-"):
        return "gemini"
    return None


def repository(value: Any) -> str:
    if not isinstance(value, str) or not REPOSITORY.fullmatch(value):
        raise Rejected("invalid_input", "Repository must be an owner/name pair")
    return value


def key(repo: str, number: int) -> str:
    return f"{repo.lower()}#{number}"


def input_key(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    repo, number = data.get("repository"), data.get("number")
    if not isinstance(repo, str) or not REPOSITORY.fullmatch(repo) or type(number) is not int:
        return None
    return key(repo, number)


def compute_turns(compute: Json | None) -> list[Json] | None:
    """Distinct observed (model, effort) turns, or None when the record is unusable."""
    if (
        not isinstance(compute, dict)
        or compute.get("schema") != "harness.session-compute/v1"
        or not isinstance(compute.get("turns"), list)
        or not compute["turns"]
    ):
        return None
    for turn in compute["turns"]:
        if not isinstance(turn, dict) or set(turn) != {"model", "effort"}:
            return None
        if any(not isinstance(v, str) or v == "unknown" for v in turn.values()):
            return None  # The Host marks a turn without model or effort as unknown.
    return list(compute["turns"])


def sources(compute: Json | None) -> list[tuple[Any, Json]]:
    """The session record and its Host-proven sub-agent records, with the sub-agents'
    own session references (None stands for the calling session itself; False for a
    sub-agent without a reference, which can never be an author)."""
    if not isinstance(compute, dict):
        return []
    return [(None, compute)] + [
        (s["session"] if isinstance(s.get("session"), str) else False, s)
        for s in compute.get("subagents") or [] if isinstance(s, dict)
    ]


def evidence_rows(rows: Any) -> list[Json]:
    return [c for c in (rows if isinstance(rows, list) else []) if isinstance(c, dict)]


def matching(rows: Any, head: str) -> list[Json]:
    """Commit evidence rows for this head (abbreviated SHA prefix)."""
    return [
        c for c in evidence_rows(rows)
        if isinstance(c.get("sha"), str) and len(c["sha"]) >= 7
        and head.startswith(c["sha"])
    ]


def committed(compute: Json | None, head: str) -> list[Json]:
    """Commit evidence in a session record or its sub-agents for this head: confirmed
    by the repository, or merely printed by a committing call (`[branch <sha>]`)."""
    rows = [c for _, source in sources(compute) for c in evidence_rows(source.get("commits"))]
    printed = [
        {**c, "sha": sha} for c in rows if isinstance(c.get("printed"), list)
        for sha in c["printed"] if isinstance(sha, str)
    ]
    return matching(rows, head) + matching(printed, head)


def head_authors(
    compute: Json | None, head: str, session: str,
    observed: Json | None = None, predecessors: frozenset[str] = frozenset(),
) -> list[Json]:
    """The sessions (the registering one, a sub-agent it spawned, or a former holder
    session of this WI in `predecessors`) whose turn created the head commit, with
    that turn's model, as confirmed by the repository's own objects and reflog (Host
    `verify_commits`, and `find_authors` for the PR's commit authors in `observed`)."""
    evidence = [
        (session if owner is None else owner, c) for owner, source in sources(compute)
        for c in matching(source.get("commits"), head) if c.get("verified") is True
    ] + [
        (a["session"], a) for a in evidence_rows((observed or {}).get("authors"))
        if a.get("sha") == head and a.get("session") in predecessors
    ]
    models = {c.get("model") for _, c in evidence}
    if not evidence or any(not isinstance(owner, str) for owner, _ in evidence):
        raise Rejected(
            "source_unavailable",
            "The registering session, a sub-agent it spawned, or a session that held this "
            "WI before it (rollover or continuation) must be the one that created the head "
            "commit. Make the head with git commit in this session (one command per commit, "
            "starting with cd <literal absolute workspace path> &&), or in a sub-agent it "
            "spawns; a commit made by rebase, cherry-pick or any other session does not "
            "count: recreate it here (git reset --soft <base>, then one git commit), push, "
            "and register the new head",
        )
    if len(models) != 1 or family(next(iter(models))) is None:
        raise Rejected("source_unavailable", "The head commit has no single known author model")
    model = str(models.pop())
    return [{"sha": head, "session": owner, "model": model}
            for owner in dict.fromkeys(owner for owner, _ in evidence)]


def reviewed_rounds(pr: Json) -> int:
    # A review carried over to a resubmitted identical head is not a new round.
    return sum(1 for r in pr["rounds"] if r["review"] is not None and not r["review"].get("reused_from"))


def flagship(model: str, profile: Json) -> bool:
    """The exact current flagship: Relay resolves review to a full model on both sides
    (CC: the newest claude-opus-* in its model catalog; Codex: the newest Sol slug).
    A short alias such as `opus` would also admit an older version and never matches."""
    target = profile.get("model")
    return isinstance(target, str) and bool(target) and model == target


def reviewer_problem(
    pr: Json, compute: Json | None, profile: Json | None, wi: str
) -> str | None:
    """The first unmet reviewer requirement, or None when eligible."""
    turns = compute_turns(compute)
    if turns is None:
        return "reviewer_record_missing: model and reasoning effort must be in the native record"
    assert compute is not None
    runtime = compute.get("runtime")
    if runtime not in ("cc", "codex"):
        return "reviewer_runtime_unsupported: review runs in Claude Code or Codex"
    families = {family(t["model"]) for t in turns}
    if None in families or len(families) != 1:
        return "reviewer_family_unknown: one known model family is required"
    if families & author_families(pr):
        return "reviewer_same_family: the reviewer must use the other model family"
    if any(committed(compute, r["head_sha"]) for r in pr["rounds"]):
        return "reviewer_committed_candidate: the reviewer created a head of this pull request"
    if (
        not isinstance(profile, dict)
        or profile.get("profile") != REVIEW_PROFILE
        or profile.get("provider") != runtime
    ):
        return "review_profile_unavailable: the Relay review profile was not resolved"
    if any(not flagship(t["model"], profile) for t in turns):
        return "reviewer_not_flagship: the model is not the review profile's current flagship"
    if any(t["effort"] not in REVIEW_EFFORTS for t in turns):
        return "reviewer_effort_below_high: reasoning effort must be high or above"
    if compute.get("declared_wis") != [wi]:
        # One WI per review session, declared before the review: its cost is this WI's.
        return "reviewer_wi_undeclared: the review session must declare exactly this WI"
    return None


def records(state: Json) -> Json:
    return state.get("pull_requests") or {}


def find(state: Json, candidate_id: Any) -> tuple[Json, Json]:
    if isinstance(candidate_id, str):
        for pr in records(state).values():
            for item in pr["rounds"]:
                if item["candidate_id"] == candidate_id:
                    return pr, item
    raise Rejected("domain_conflict", "Unknown pull request candidate")


def failed_reviews(pr: Json) -> int:
    return sum(1 for r in pr["rounds"] if r["review"] is not None and not r["review"]["passed"])


def merge_status(state: Json, item: Json) -> str | None:
    effect = state["effects"].get(item["merge_effect"]) if item["merge_effect"] else None
    return None if effect is None else str(effect["status"])


def author_families(pr: Json) -> set[str | None]:
    return {family(a["model"]) for r in pr["rounds"] for a in r.get("authors") or [
        {"model": r["author_model"]}]}


def author_sessions(pr: Json) -> set[Any]:
    return {r["author"]["session"] for r in pr["rounds"]} | {
        a["session"] for r in pr["rounds"] for a in r.get("authors") or []
    }


def last_observation(state: Json, item: Json) -> Json | None:
    effect = state["effects"].get(item["merge_effect"]) if item["merge_effect"] else None
    if effect is None or not effect["observations"]:
        return None
    details = effect["observations"][-1]["details"]
    return details if isinstance(details, dict) else None


def merge_block(state: Json, item: Json) -> Json | None:
    """Why the latest merge cannot proceed by itself: failed checks, or retries spent."""
    from .delivery import retry_policy

    effect = state["effects"].get(item["merge_effect"]) if item["merge_effect"] else None
    observed = last_observation(state, item) or {}
    reason, checks = observed.get("reason"), observed.get("checks") or {}
    human = ("or, with a human's decision in this session, confirm_pull_request_merge "
             "{candidate_id, reason, override_checks: true}")
    if effect is None:
        return None
    if effect["status"] == "observed_failure" and reason == "checks_failed":
        return {
            "reason": "checks_failed",
            "failed_checks": checks.get("failed", []),
            # Failed checks beyond the names kept in the observation.
            "failed_checks_omitted": (checks.get("omitted") or {}).get("failed", 0),
            "recovery": "submit_pull_request",
            "agent_action": "Fix the failing checks and register the new head (or rerun them "
            "until green and register the same head as a new request: a new request_id; "
            "the original request_id only returns its original result) " + human,
        }
    if effect["status"] == "observed_absence" and retry_policy(effect)["attempts_remaining"] == 0:
        return {
            "reason": "merge_retry_exhausted",
            "last_reason": reason,
            "pending_checks": checks.get("pending", []),
            "recovery": "retry_effect",
            "agent_action": "Find why checks are still pending, missing or unreadable (or why "
            "GitHub refused the merge), repair it, then the WI owner requests retry_effect "
            "{effect_id, reason}"
            + ("; " + human if reason == "checks_missing" else ""),
        }
    return None


def status(state: Json, pr: Json) -> str:
    if pr.get("withdrawn"):
        return "withdrawn"
    item = pr["rounds"][-1]
    merge = merge_status(state, item)
    if merge == "observed_success":
        return "merged"
    if merge == "observed_failure" and (last_observation(state, item) or {}).get(
        "reason"
    ) == "checks_failed":
        return "checks_failed"
    if merge == "observed_failure":
        return "merge_failed"
    if merge == "cancelled":
        return "cancelled"
    if merge is not None:
        return "merge_pending"
    if item["review"] is None:
        return "awaiting_review" if item["review_request"] else "human_confirmation_required"
    if reviewed_rounds(pr) >= MAX_ROUNDS and not item["review"]["passed"]:
        return "human_confirmation_required"
    return "changes_requested"


def merge_request(
    state: Json, item: Json, effect_id: str, repo: str, number: int, override: bool = False
) -> Json:
    return event(
        "EffectRequested",
        effect_id=effect_id,
        effect_kind=MERGE_KIND,
        candidate_id=item["candidate_id"],
        generation=state["generation"],
        artifact=None,
        target=key(repo, number),
        required=False,
        node_id=None,
        snapshot={
            "repository": repo,
            "number": number,
            "head_sha": item["head_sha"],
            "base": "main",
            "candidate_id": item["candidate_id"],
            # Only a human confirmation lets failed or missing checks through.
            **({"override_checks": True} if override else {}),
        },
    )


def decide(
    state: Json, intent: str, data: Json, context: HostContext, operation: str, wi: str, at: str
) -> Decision:
    if state["status"] != "active":
        raise Rejected("domain_conflict", "Pull request review requires active work")
    if intent == "submit_pull_request":
        return submit(state, data, context, operation, wi, at)
    if intent == "withdraw_pull_request":
        return withdraw(state, data, context, at)
    if intent == "record_pull_request_review":
        return review(state, data, context, operation, wi, at)
    if intent == "receive_pull_request_review":
        return receive(state, data, context, wi, at)
    fields(data, {"candidate_id", "reason"}, {"override_checks"})
    nonempty(data["reason"])
    override = data.get("override_checks", False)
    if type(override) is not bool:
        raise Rejected("invalid_input", "override_checks must be true or false")
    pr, item = find(state, data["candidate_id"])
    current = item is pr["rounds"][-1] and not pr.get("withdrawn")
    unconfirmed = status(state, pr) == "human_confirmation_required" and item["confirmation"] is None
    try:
        source = human_decision(context, intent, data, wi)
        grant = None
    except Rejected as missing:
        # A standing merge authority stands in for the human only on a head fixed
        # after three reviewed rounds, and never lets a check through.
        grant = context.merge_authority[0] if context.merge_authority else None
        if grant is None or override or not (
            current and unconfirmed and item.get("review_block") == "three_rounds"
        ):
            raise Rejected(missing.code, missing.reason + "; a standing merge authority "
                           "(grant_merge_authority) covers only a head registered after three "
                           "reviewed rounds, without override_checks") from None
        # Standing in for the human needs the WI's execution authority itself: the
        # current holder session at the current generation, not another session.
        if state["holder"] != context.principal() or context.generation != state["generation"]:
            raise Rejected("authority_denied", "A standing merge authority confirms only "
                           "from the current holder session of this WI")
        source = grant["source"]
    if state["holder"] is None or state["holder"]["actor"] != context.actor:
        raise Rejected("authority_denied", "The holder's Agent carries the human confirmation")
    # Blocked checks of the latest head: failed, or retries spent on missing checks.
    checks = merge_block(state, item) if current else None
    checks_blocked = checks is not None and (
        checks["reason"] == "checks_failed" or checks.get("last_reason") == "checks_missing"
    )
    if not current or not (unconfirmed or checks_blocked):
        raise Rejected(
            "domain_conflict",
            "Human confirmation applies to the latest head after three reviewed rounds "
            "or when its checks block the merge",
        )
    effect_id = operation + ".merge"
    cancelled = (
        [event("EffectCancelled", effect_id=item["merge_effect"],
               reason="pull_request_merge_reconfirmed")]
        if pending(state, item["merge_effect"]) else []
    )
    return Decision(
        cancelled + [
            event(
                "PullRequestMergeConfirmed",
                key=key(pr["repository"], pr["number"]),
                candidate_id=item["candidate_id"],
                merge_effect=effect_id,
                confirmation={
                    "reason": data["reason"],
                    "override_checks": override,
                    "source_digest": source,
                    "actor": context.actor,
                    "at": at,
                    **({"merge_authority": {k: grant[k] for k in ("grant_id", "work_item_id")}}
                       if grant is not None else {}),
                },
            ),
            merge_request(state, item, effect_id, pr["repository"], pr["number"], override),
        ],
        {"candidate_id": item["candidate_id"], "merge_effect_id": effect_id,
         "override_checks": override},
    )


def withdraw(state: Json, data: Json, context: HostContext, at: str) -> Decision:
    """Release a PR registered on the wrong WI; the reason stays in this WI's history."""
    fields(data, {"repository", "number", "reason"})
    nonempty(data["reason"])
    pr = records(state).get(input_key(data) or "")
    if state["holder"] is None or state["holder"]["actor"] != context.actor:
        raise Rejected("authority_denied", "Only the holder's Agent withdraws its pull request")
    if pr is None or pr.get("withdrawn"):
        raise Rejected("domain_conflict", "No registered pull request to withdraw")
    last = pr["rounds"][-1]
    if merge_status(state, last) in {"attempt_started", "unknown", "observed_success"}:
        raise Rejected("domain_conflict", "A merged or possibly merging pull request stays here")
    events = [
        event("EffectCancelled", effect_id=effect_id, reason="pull_request_withdrawn")
        for effect_id in (last["merge_effect"], last.get("review_dispatch"))
        if effect_id and state["effects"].get(effect_id, {}).get("status")
        in {"requested", "observed_absence"}
    ]
    events.append(event("PullRequestWithdrawn", key=key(pr["repository"], pr["number"]),
                        withdrawal={"reason": data["reason"], "actor": context.actor, "at": at}))
    return Decision(events, {"withdrawn": key(pr["repository"], pr["number"])})


def authorship(observed: Json, head: str, first: list[Json]) -> tuple[list[Json], str | None]:
    """Authors of every commit the PR introduces, and why a review cannot be automatic."""
    commits = observed.get("commits")
    if not isinstance(commits, list) or head not in commits:
        raise Rejected("source_unavailable", "The pull request commits could not be read from GitHub")
    known = {(a["sha"], a["session"]) for a in first}
    authors = first + [
        {"sha": a["sha"], "session": a["session"], "model": a["model"]}
        for a in observed.get("authors") or []
        if a.get("sha") in commits and (a["sha"], a["session"]) not in known
    ]
    attributed = {a["sha"] for a in authors}
    families = {family(a["model"]) for a in authors}
    if not observed.get("commits_complete") or any(sha not in attributed for sha in commits):
        return authors, "unattributed_commits"
    if any(len({a["session"] for a in authors if a["sha"] == sha}) > 1 for sha in attributed):
        # Several sessions' commit windows cover one HEAD move: the author is unknown.
        return authors, "ambiguous_commit_authors"
    if None in families or len(families) != 1:
        return authors, "mixed_author_families"
    return authors, None


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def charged_to(wi: str, parent: Any, evidence: Json) -> bool:
    """ComputeCost charges every turn of the marked sub-agent to child `wi`.

    `dispatch_declared_wis` (Host `child_dispatch`) are the WIs ComputeCost
    charges the turns to before its child rule: the child itself, its parent,
    or a sibling the export names under that parent (`cost_served`), when
    ComputeCost's registry exports the child (`cost_child`) as an open child
    of that parent created no later than the first turn (`dispatch_since`).
    One request may so advance several children, each by its marked sub-agent.
    """
    dispatch = evidence.get("dispatch_declared_wis")
    served = set(dispatch) if isinstance(dispatch, list) and dispatch else None
    if served is None:
        return False
    if served == {wi}:
        return True
    exported = evidence.get("cost_served")
    exported = exported if isinstance(exported, dict) else {}
    if not all(
        other in {wi, parent}
        or isinstance(exported.get(other), dict) and exported[other].get("parent") == parent
        for other in served
    ):
        return False
    cost = evidence.get("cost_child")
    created, since = (
        _instant(cost.get("created_at")) if isinstance(cost, dict) else None,
        _instant(evidence.get("dispatch_since")),
    )
    return (
        isinstance(cost, dict) and cost.get("parent") == parent and cost.get("status") == "active"
        and cost.get("closed_at") is None and created is not None and since is not None
        and created <= since
    )


def author_wis(declared: Any, wi: str, proofs: list[Any]) -> list[Any]:
    """A session author's WI by ComputeCost's per-request rule.

    `declared` is every WI the session declared. When it names another WI,
    the commit still belongs to the one WI every request that made it is
    charged to: `proofs` are the Host's `request_declared_wis` of the session's
    committing calls (null when unproven, and then the declaration stands)."""
    names: list[Any] = list(declared) if isinstance(declared, list) else []
    found = {tuple(p) if isinstance(p, list) and p else None for p in proofs}
    if not names or names == [wi] or len(found) != 1 or None in found:
        return names
    return list(found.pop() or ())


def attribution(
    state: Json, wi: str, declared: Any, marker: Any, own: Any = None, evidence: Any = None
) -> list[Any]:
    """The WI a commit author's output belongs to, by ComputeCost's rule.

    `declared` is the session's declaration; `own` a sub-agent's own (normally
    none, checked separately to be exactly the registration target); `evidence`
    the sub-agent's Host-read cost evidence (`charged_to`). Registering on a
    child, a sub-agent whose first prompt names `[WI: <child>]` belongs to it
    when ComputeCost provably charges every turn of it there; registering on a
    parent, a marked sub-agent belongs to its open child when the session
    declared only the parent, its turns are charged to the parent or that
    child, or, with declared siblings among them (one request advancing
    several children in parallel), `charged_to` proves them by the export.
    Any other marked sub-agent whose turns are all charged to its marker WI
    belongs to that WI. Otherwise it belongs to its own declaration, else its
    session's. The child is open when registering on it (submission needs
    active work); a parent's children are those the kernel read in this
    transaction.
    """
    names: list[Any] = list(declared) if isinstance(declared, list) else []
    mine: list[Any] = list(own) if isinstance(own, list) else []
    facts: Json = evidence if isinstance(evidence, dict) else {}
    dispatch = facts.get("dispatch_declared_wis")
    served = set(dispatch) if isinstance(dispatch, list) and dispatch else None
    parent = state.get("parent")
    open_children = {c["id"] for c in state.get("children") or []
                     if c["status"] not in {"completed", "terminated"}}
    if isinstance(marker, str) and (
        parent is not None and marker == wi and charged_to(wi, parent, facts)
        or parent is None and marker in open_children and (
            names == [wi] or served is not None and served <= {wi, marker}
            # A declared sibling moves the turns only with ComputeCost's export proof.
            or charged_to(marker, wi, facts))
    ):
        return [marker]
    if isinstance(marker, str) and served == {marker}:
        return [marker]
    return mine or names


def family_of(state: Json, wi: str) -> set[Any]:
    """`wi` with its parent and children: one holder's lineage."""
    parent = state.get("parent")
    return {wi, *([parent] if parent is not None else []),
            *(c["id"] for c in state.get("children") or [])}


def pending(state: Json, effect_id: str | None) -> bool:
    """An effect that has not started or was read back as not executed."""
    effect = state["effects"].get(effect_id) if effect_id else None
    return effect is not None and effect["status"] in {"requested", "observed_absence"}


def submit(
    state: Json, data: Json, context: HostContext, operation: str, wi: str, at: str
) -> Decision:
    fields(data, *guide.shape("submit_pull_request"))
    immediately = data.get("immediately", False)
    if type(immediately) is not bool:
        raise Rejected("invalid_input", "immediately must be true or false")
    repo = repository(data["repository"])
    number = data["number"]
    if type(number) is not int or not 1 <= number <= 10**9:
        raise Rejected("invalid_input", "Pull request number must be a positive integer")
    head = commit(data["head_sha"])
    holder = state["holder"]
    if holder is None or holder["actor"] != context.actor or context.generation != state["generation"]:
        raise Rejected(
            "authority_denied",
            "Only the holder's Agent registers its pull request; "
            + ("this WI has no holder" if holder is None else f"it is held by {holder['actor']}")
            + ": register on a WI your Agent holds",
        )
    if repo not in context.merge_repositories:
        raise Rejected(
            "authority_denied",
            "Repository is not admitted for kernel merge; admitted (exact case): "
            + ", ".join(sorted(context.merge_repositories)),
        )
    observed = context.pull_request_observation
    if not isinstance(observed, dict) or (observed.get("repository"), observed.get("number")) != (
        repo, number
    ):
        raise Rejected("source_unavailable", "The pull request could not be read from GitHub")
    if observed.get("merged") or observed.get("state") != "open":
        raise Rejected("domain_conflict", "Only an open, unmerged pull request can be registered")
    if observed.get("head_sha") != head or observed.get("base") != "main":
        raise Rejected("domain_conflict", "The pull request head or base differs on GitHub")
    # A session that held this WI before the registering one (same Agent): its
    # Host-proven head commit stays its own, with its own model.
    predecessors = frozenset(
        h["session"] for h in state.get("former_holders") or []
        if h["actor"] == context.actor and h["session"] != context.session
    )
    first = head_authors(context.session_compute, head, context.session, observed, predecessors)
    model = first[0]["model"]
    listed = observed.get("commits")
    commits: list[Any] = listed if isinstance(listed, list) else []
    compute = context.session_compute or {}
    # The session's own head commit belongs to its session's declaration, or to
    # the one WI the requests that made it are charged to.
    session_declared = author_wis(compute.get("declared_wis") or [], wi, [
        c.get("request_declared_wis") for c in matching(compute.get("commits"), head)
        if c.get("verified") is True])
    # A parent holds full authority over its children (大鸟 11-13): output of any
    # WI in this lineage may be registered on any of them; the cost split stays
    # ComputeCost's per-request business, not a registration gate.
    lineage = family_of(state, wi)
    if (
        any(a["session"] == context.session for a in first)
        and session_declared and not set(session_declared) <= lineage
    ):
        # The session's own head commit; a sub-agent's is judged by `attribution`.
        raise Rejected(
            "domain_conflict",
            "The author session declared " + ", ".join(map(str, session_declared))
            + "; its output and cost belong to exactly one WI, register the pull request there",
        )
    agents = {owner: source for owner, source in sources(compute) if owner is not None}
    others = [a for a in observed.get("authors") or [] if a.get("sha") in commits]
    declarations = [
        # A sub-agent's own declaration (sub-agents normally declare nothing).
        ("A sub-agent of the author session", source.get("declared_wis"))
        for source in agents.values()
    ] + [
        # The same for a sub-agent among the other commit authors; its session's
        # declaration comes apart, as `session_declared_wis`.
        ("A sub-agent author of commit " + str(a["sha"])[:12], a.get("declared_wis"))
        for a in others if "session_declared_wis" in a
    ] + [
        # Each head author's WI: a sub-agent's valid child marker, else its own or
        # its session's declaration; a predecessor's is judged as another author's.
        ("The author of commit " + head[:12],
         session_declared if a["session"] == context.session else attribution(
             state, wi, session_declared, agents[a["session"]].get("wi_marker"),
             agents[a["session"]].get("declared_wis"), agents[a["session"]]))
        for a in first if a["session"] == context.session or a["session"] in agents
    ] + [
        # Every other commit author, by the same rule.
        ("The author of commit " + str(a["sha"])[:12],
         attribution(state, wi, a["session_declared_wis"], a.get("wi_marker"),
                     a.get("declared_wis"), a)
         if "session_declared_wis" in a
         else attribution(state, wi, author_wis(
             a.get("declared_wis"), wi, [a.get("request_declared_wis")]), a.get("wi_marker")))
        for a in others
    ]
    for who, declared in declarations:
        if declared and not set(declared) <= lineage:
            # A WI outside this parent/child lineage names where the output belongs.
            # The same holds for every session whose commit the pull request carries.
            raise Rejected(
                "domain_conflict",
                who + " declared " + ", ".join(map(str, declared))
                + "; output outside this WI's parent/child lineage belongs to that WI,"
                " register the pull request there",
            )
    authors, block = authorship(observed, head, first)
    events: list[Json] = []
    reviewed = 0
    reused: Json | None = None
    pr = records(state).get(key(repo, number))
    if pr is not None:
        last = pr["rounds"][-1]
        merge = merge_status(state, last)
        if merge == "observed_success":
            raise Rejected("domain_conflict", "This pull request is already merged")
        if merge in {"attempt_started", "unknown"}:
            raise Rejected("domain_conflict", "Read back the merge attempt before a new round")
        reviewed = reviewed_rounds(pr)
        if last["head_sha"] == head and merge != "observed_failure" and not pr.get("withdrawn"):
            raise Rejected("domain_conflict", "A new round requires a new head commit")
        if (
            last["head_sha"] == head and merge == "observed_failure" and not pr.get("withdrawn")
            and last["review"] is not None and last["review"]["passed"]
        ):
            # A passed review of this exact head whose merge was refused (e.g. failed
            # checks, now rerun): the review stands; only the merge is requested again.
            reused = last
        for effect_id in (last["merge_effect"], last.get("review_dispatch")):
            if pending(state, effect_id):
                events.append(
                    event("EffectCancelled", effect_id=effect_id,
                          reason="pull_request_head_resubmitted")
                )
    # The reviewer must differ from the authors of every round of this PR, the
    # same scope the review itself checks; without such a family a human decides.
    families = {family(a["model"]) for a in authors} | (author_families(pr) if pr else set())
    if block is None and (None in families or len(families) != 1):
        block = "mixed_author_families"
    # After three reviewed rounds, or without one independent reviewer family for
    # every commit's author, the head goes to a human instead of a review.
    if block is not None:
        reused = None
    block = block or ("three_rounds" if reviewed >= MAX_ROUNDS and reused is None else None)
    request = None if block or reused is not None else {
        "actor": context.actor,
        "runtime": REVIEW_RUNTIME[str(next(iter(families)))],
        "profile": REVIEW_PROFILE,
        "minimum_effort": "high",
    }
    events.append(
        event(
            "PullRequestSubmitted",
            key=key(repo, number),
            repository=repo,
            number=number,
            head_sha=head,
            candidate_id=operation,
            author=context.principal(),
            # How the registering session was entered: a scheduler run takes no
            # delivery, so the inbox routes its results to the holder.
            author_entry=context.entry,
            author_model=model,
            authors=authors,
            review_block=block,
            author_compute=context.session_compute,
            observed=observed,
            review_request=request,
            review_dispatch=None if request is None else operation + ".review",
            immediately=immediately,
            submitted_at=at,
            **({"review_reused_from": reused["candidate_id"], "merge_effect": operation + ".merge"}
               if reused is not None else {}),
        )
    )
    if reused is not None:
        events.append(merge_request(state, {"candidate_id": operation, "head_sha": head},
                                    operation + ".merge", repo, number))
    if request is not None:
        events.append(
            event(
                "EffectRequested",
                effect_id=operation + ".review",
                effect_kind=DISPATCH_KIND,
                candidate_id=operation,
                generation=state["generation"],
                artifact=None,
                target=key(repo, number),
                required=False,
                node_id=None,
                snapshot={
                    "work_item_id": wi,
                    "candidate_id": operation,
                    "repository": repo,
                    "number": number,
                    "head_sha": head,
                    "immediately": immediately,
                    "submitted_at": at,
                    **request,
                },
            )
        )
    return Decision(events, {
        "candidate_id": operation, "review_request": request,
        **({"review_reused_from": reused["candidate_id"], "merge_effect_id": operation + ".merge"}
           if reused is not None else {}),
    })


def review(
    state: Json, data: Json, context: HostContext, operation: str, wi: str, at: str
) -> Decision:
    fields(data, {"candidate_id", "score", "reason"})
    pr, item = find(state, data["candidate_id"])
    score = data["score"]
    if type(score) is not int or not 0 <= score <= 100:
        raise Rejected("invalid_input", "Review score must be an integer from 0 to 100")
    if len(nonempty(data["reason"])) > 16000:
        raise Rejected("invalid_input", "Review reason exceeds its bound")
    if pr.get("withdrawn"):
        raise Rejected("domain_conflict", "This pull request was withdrawn from this WI")
    if item is not pr["rounds"][-1] or item["review"] is not None or not item["review_request"]:
        raise Rejected("domain_conflict", "Only the current unreviewed round can be reviewed")
    if context.actor != item["author"]["actor"]:
        raise Rejected("authority_denied", "The review must come from the author's own Agent")
    if context.session in author_sessions(pr):
        raise Rejected("authority_denied", "An author session cannot review its own pull request")
    problem = reviewer_problem(pr, context.session_compute, context.review_profile, wi)
    if problem is not None:
        raise Rejected("reviewer_ineligible", problem)
    effect = state["effects"].get(item.get("review_dispatch") or "")
    if effect is not None and effect["attempts"] and not item.get("receipts"):
        # Once the gate started a session, only a receiving session may score;
        # a manual review is for rounds the gate has not dispatched.
        raise Rejected(
            "authority_denied", "A dispatched round is reviewed by its receiving session"
        )
    if item.get("receipts"):
        # Only the receiver of the current dispatch attempt may score this round.
        current = effect["attempts"][-1]["id"] if effect and effect["attempts"] else None
        receiver = next(
            (r["session"] for r in item["receipts"] if r["attempt_id"] == current), None
        )
        if receiver != context.principal():
            raise Rejected(
                "authority_denied", "Only the current dispatch attempt's receiver may review"
            )
    passed = score >= PASS_SCORE
    number = reviewed_rounds(pr) + 1
    effect_id = operation + ".merge" if passed else None
    events = [
        event(
            "PullRequestReviewed",
            key=key(pr["repository"], pr["number"]),
            candidate_id=item["candidate_id"],
            merge_effect=effect_id,
            review={
                "score": score,
                "reason": data["reason"],
                "passed": passed,
                "round": number,
                "reviewer": context.principal(),
                "compute": context.session_compute,
                "profile": context.review_profile,
                "at": at,
            },
        )
    ]
    if effect_id is not None:
        events.append(merge_request(state, item, effect_id, pr["repository"], pr["number"]))
    if pending(state, item.get("review_dispatch")):
        # Reviewed before the gate started a session: the request is answered.
        events.append(event("EffectCancelled", effect_id=item["review_dispatch"],
                            reason="pull_request_reviewed"))
    following = (
        "merge" if passed else "human_confirmation" if number >= MAX_ROUNDS else "revise"
    )
    return Decision(
        events,
        {"passed": passed, "round": number, "next": following, "merge_effect_id": effect_id},
    )


def receive(state: Json, data: Json, context: HostContext, wi: str, at: str) -> Decision:
    """The started review session's receipt of one dispatch attempt."""
    fields(data, {"candidate_id", "attempt_id"})
    pr, item = find(state, data["candidate_id"])
    effect = state["effects"].get(item.get("review_dispatch") or "")
    if (
        effect is None
        or pr.get("withdrawn")
        or item is not pr["rounds"][-1]
        or item["review"] is not None
        or effect["status"] not in {"attempt_started", "unknown"}
        or not effect["attempts"]
        or effect["attempts"][-1]["id"] != data["attempt_id"]
    ):
        raise Rejected("domain_conflict", "Only the current dispatch attempt can be received")
    if context.actor != item["review_request"]["actor"] or context.session in {
        r["author"]["session"] for r in pr["rounds"]
    }:
        raise Rejected("authority_denied", "The receipt must come from a reviewing session")
    compute = context.session_compute or {}
    if compute.get("runtime") != item["review_request"]["runtime"] or compute.get(
        "declared_wis"
    ) != [wi]:
        # Cost is bound before the review starts: one session, this WI only.
        raise Rejected(
            "reviewer_ineligible",
            "reviewer_wi_undeclared: declare exactly this WI in the requested runtime first",
        )
    receipt = {"attempt_id": data["attempt_id"], "session": context.principal(), "at": at}
    holder = next((r["session"] for r in item.get("receipts", [])
                   if r["attempt_id"] == data["attempt_id"]), None)
    if holder == receipt["session"]:
        return Decision([], {"candidate_id": item["candidate_id"], "received": True})
    if holder is not None:
        # One attempt starts one session; a duplicate start must stop, not review.
        raise Rejected("domain_conflict", "This dispatch attempt was received by another session")
    return Decision(
        [event("PullRequestReviewReceived", key=key(pr["repository"], pr["number"]),
               candidate_id=item["candidate_id"], receipt=receipt)],
        {"candidate_id": item["candidate_id"], "received": True},
    )


def can_begin(state: Json, effect: Json) -> None:
    pr, item = find(state, effect["candidate_id"])
    if effect["effect_kind"] == DISPATCH_KIND:
        if (
            state["status"] != "active"
            or pr.get("withdrawn")
            or item is not pr["rounds"][-1]
            or item["review"] is not None
            or item.get("review_dispatch") != effect["effect_id"]
        ):
            raise Rejected("domain_conflict", "The review request is no longer current")
        return
    if (
        state["status"] not in {"active", "completed"}
        or pr.get("withdrawn")
        or item is not pr["rounds"][-1]
        or item["merge_effect"] != effect["effect_id"]
    ):
        raise Rejected("domain_conflict", "The merge no longer matches the latest reviewed round")


def startable(state: Json, effect: Json) -> bool:
    """Whether `can_begin` admits a new attempt of this merge or review request now.

    A WI no longer active or completed, a withdrawn PR or a superseded round
    refuses every new attempt for good; an open attempt is still read back."""
    try:
        can_begin(state, effect)
    except Rejected:
        return False
    return True


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "PullRequestSubmitted":
        prs = state.setdefault("pull_requests", {})
        pr = prs.setdefault(
            data["key"], {"repository": data["repository"], "number": data["number"], "rounds": []}
        )
        if pr["rounds"] and pr["rounds"][-1]["status"] == "awaiting_review":
            pr["rounds"][-1]["status"] = "superseded"
        pr["withdrawn"] = None
        pr["rounds"].append(
            {
                "candidate_id": data["candidate_id"],
                "head_sha": data["head_sha"],
                "author": data["author"],
                **({"author_entry": data["author_entry"]} if "author_entry" in data else {}),
                "author_model": data["author_model"],
                "authors": data.get("authors"),
                "review_block": data.get("review_block"),
                "author_compute": data["author_compute"],
                "review_request": data["review_request"],
                "review_dispatch": data.get("review_dispatch"),
                "submitted_at": data["submitted_at"],
                "immediately": data.get("immediately", False),
                "status": "awaiting_review",
                "review": None,
                "confirmation": None,
                "merge_effect": None,
            }
        )
        source = data.get("review_reused_from")
        if source is not None:
            prior = next(r for r in pr["rounds"] if r["candidate_id"] == source)
            pr["rounds"][-1].update(
                status="reviewed", review={**prior["review"], "reused_from": source},
                merge_effect=data["merge_effect"],
            )
    elif kind == "PullRequestWithdrawn":
        pr = state["pull_requests"][data["key"]]
        pr["withdrawn"] = data["withdrawal"]
        pr.setdefault("withdrawals", []).append(data["withdrawal"])
    elif kind == "PullRequestReviewReceived":
        item = next(
            r for r in state["pull_requests"][data["key"]]["rounds"]
            if r["candidate_id"] == data["candidate_id"]
        )
        item.setdefault("receipts", []).append(data["receipt"])
    elif kind in {"PullRequestReviewed", "PullRequestMergeConfirmed"}:
        item = next(
            r for r in state["pull_requests"][data["key"]]["rounds"]
            if r["candidate_id"] == data["candidate_id"]
        )
        if kind == "PullRequestReviewed":
            item.update(status="reviewed", review=data["review"])
        else:
            item["confirmation"] = data["confirmation"]
        item["merge_effect"] = data["merge_effect"]
    else:
        return False
    return True


def public(state: Json) -> list[Json]:
    """Sessions stay private; actors, scores and observed computation are public."""
    rows = []
    for name, pr in sorted(records(state).items()):
        rounds = []
        for item in pr["rounds"]:
            effect = state["effects"].get(item["merge_effect"]) if item["merge_effect"] else None
            observed = effect["observations"][-1]["details"] if effect and effect["observations"] else None
            review_value = item["review"]
            rounds.append(
                {
                    "candidate_id": item["candidate_id"],
                    "head_sha": item["head_sha"],
                    "status": item["status"],
                    "author": item["author"]["actor"],
                    "author_model": item["author_model"],
                    # Commit authorship without private session identities.
                    "authors": [{"sha": a["sha"], "model": a["model"]}
                                for a in item.get("authors") or []],
                    "review_block": item.get("review_block"),
                    "review_request": item["review_request"],
                    "immediately": item.get("immediately", False),
                    "review": None
                    if review_value is None
                    else {
                        **{k: review_value[k] for k in ("score", "reason", "passed", "round", "compute", "at")},
                        "reviewer": review_value["reviewer"]["actor"],
                        **({"reused_from": review_value["reused_from"]}
                           if review_value.get("reused_from") else {}),
                    },
                    "confirmation": item["confirmation"],
                    "review_dispatch": dispatch_view(state, item),
                    "merge": None
                    if effect is None
                    else {"effect_id": effect["effect_id"], "status": effect["status"], "observation": observed},
                    # The checks read on the reviewed head before the last merge attempt.
                    "checks": (observed or {}).get("checks"),
                }
            )
        rows.append(
            {
                "key": name,
                "repository": pr["repository"],
                "number": pr["number"],
                "status": status(state, pr),
                "failed_reviews": failed_reviews(pr),
                "reviewed_rounds": reviewed_rounds(pr),
                "withdrawals": pr.get("withdrawals", []),
                "merge_blocked": None if pr.get("withdrawn") else merge_block(state, pr["rounds"][-1]),
                "rounds": rounds,
            }
        )
    return rows


def dispatch_view(state: Json, item: Json) -> Json | None:
    """Dispatch progress; an exhausted retry budget names its block and recovery."""
    from .delivery import retry_policy

    effect = state["effects"].get(item.get("review_dispatch") or "")
    if effect is None:
        return None
    exhausted = (
        effect["status"] == "observed_absence" and retry_policy(effect)["attempts_remaining"] == 0
    )
    return {
        "effect_id": effect["effect_id"],
        "status": effect["status"],
        "attempts": len(effect["attempts"]),
        "received_attempts": sorted({r["attempt_id"] for r in item.get("receipts", [])}),
        "blocked": {
            "reason": "review_dispatch_retry_exhausted",
            "recovery": "retry_effect",
            "agent_action": "Find why review sessions did not start or finish (Relay review job, "
            "runtime, profile), repair it, then the WI owner requests retry_effect "
            "{effect_id, reason}",
        }
        if exhausted
        else None,
    }


def next_action(state: Json, context: HostContext) -> Json | None:
    """What the WI waits on from this Agent's sessions for its pull requests."""
    if state["status"] != "active":
        return None
    for pr in records(state).values():
        item = pr["rounds"][-1]
        if item["author"]["actor"] != context.actor:
            continue
        value = status(state, pr)
        blocked = (dispatch_view(state, item) or {}).get("blocked")
        if value == "awaiting_review" and blocked and context.actor == state["definition"]["owner"]:
            return {
                "intent": "retry_effect",
                "reason": blocked["reason"],
                "effect_id": item["review_dispatch"],
                "agent_action": blocked["agent_action"],
            }
        if value == "awaiting_review" and context.session not in {
            *author_sessions(pr)
        }:
            return {
                "intent": "record_pull_request_review",
                "reason": "pull_request_awaiting_review",
                "candidate_id": item["candidate_id"],
                "runtime": item["review_request"]["runtime"],
                "compute_profile": REVIEW_PROFILE,
            }
        merge_blocked = merge_block(state, item)
        if value == "merge_pending" and merge_blocked and context.actor == state["definition"]["owner"]:
            return {
                "intent": "retry_effect",
                "reason": merge_blocked["reason"],
                "effect_id": item["merge_effect"],
                "agent_action": merge_blocked["agent_action"],
            }
        if value == "checks_failed" and merge_blocked:
            return {
                "intent": "submit_pull_request",
                "reason": "pull_request_checks_failed",
                "candidate_id": item["candidate_id"],
                "failed_checks": merge_blocked["failed_checks"],
                "failed_checks_omitted": merge_blocked["failed_checks_omitted"],
                "agent_action": merge_blocked["agent_action"],
            }
        if value in {"changes_requested", "merge_failed"}:
            return {
                "intent": "submit_pull_request",
                "reason": "pull_request_" + value,
                "candidate_id": item["candidate_id"],
            }
        if value == "human_confirmation_required":
            return {
                "intent": "confirm_pull_request_merge",
                "reason": "pull_request_human_confirmation_required",
                "candidate_id": item["candidate_id"],
                **({"agent_action": "A standing merge authority of your Agent confirms this "
                    "fixed head without human_input_ref; otherwise ask the human"}
                   if item.get("review_block") == "three_rounds" else {}),
            }
    return None
