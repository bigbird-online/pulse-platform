"""A session's cost and output: the kernel's WI rule agrees with ComputeCost."""

import json
from pathlib import Path

import pytest

from harness_rewrite.pull_requests import attribution, reviewer_problem
from harness_rewrite.session_compute import observe

COMPUTE_COST = Path(__file__).resolve().parents[2] / "ComputeCost"


@pytest.fixture
def cost(monkeypatch):
    if not (COMPUTE_COST / "pulse_compute_cost").is_dir():
        pytest.skip("ComputeCost source unavailable")
    monkeypatch.syspath_prepend(str(COMPUTE_COST / "tests"))
    monkeypatch.syspath_prepend(str(COMPUTE_COST))
    import test_mcp_cc_session_wi as fixture
    return fixture


def collect(fixture, tmp_path, rows):
    from pulse_compute_cost import mcp_request_purpose as m
    from pulse_compute_cost.ledger import UsageLedger
    from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources
    from pulse_compute_cost.receipts import current_receipts

    project = tmp_path / "projects/-workspace"
    project.mkdir(parents=True)
    transcript = project / f"{fixture.SESSION}.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in rows))
    ledger = UsageLedger(tmp_path / "ledger")
    registry = {"schema": REGISTRY_SCHEMA, "sources": [{
        "id": "cc", "kind": "cc", "path": str(tmp_path / "projects"),
        "session_wi_mcp_since": fixture.BEFORE}]}
    collect_registered_sources(registry, ledger)
    active, _ = current_receipts(ledger.receipt_snapshot()["facts"])
    claims = {(r.payload["state"], r.payload["wi"]) for r in active
              if r.kind == "attribution" and r.producer == m.POLICY}
    return claims, observe("cc", transcript)


def reviewer(compute):
    item = {"author_model": "gpt-7.0-sol", "head_sha": "a" * 40}
    profile = {"profile": "review", "provider": "cc", "model": "openai/test"}
    return reviewer_problem({"rounds": [item]}, {**compute, "turns": [
        {"model": "claude-opus-5-5", "effort": "high"}]}, {**profile, "model": "claude-opus-5-5"},
        compute["declared_wis"][0] if compute["declared_wis"] else "none")


def test_one_wi_per_review_session_is_assigned_to_that_wi(cost, tmp_path):
    rows = [cost.usage(cost.SESSION, 0, "a"), *cost.declaration(cost.SESSION, 3, "toolu_1"),
            cost.usage(cost.SESSION, 5, "b")]
    claims, compute = collect(cost, tmp_path, rows)
    assert claims == {("assigned", cost.WI)}
    assert compute["declared_wis"] == [cost.WI]
    assert reviewer(compute) is None  # The kernel admits exactly what ComputeCost assigns.


def test_a_session_declaring_two_wis_is_ambiguous_and_refused(cost, tmp_path):
    rows = [cost.usage(cost.SESSION, 0, "a"), *cost.declaration(cost.SESSION, 3, "toolu_1"),
            *cost.declaration(cost.SESSION, 4, "toolu_2", cost.OTHER_WI)]
    claims, compute = collect(cost, tmp_path, rows)
    assert claims == {("ambiguous", None)}
    assert compute["declared_wis"] == sorted([cost.WI, cost.OTHER_WI])
    problem = reviewer_problem(
        {"rounds": [{"author_model": "gpt-7.0-sol", "head_sha": "a" * 40}]},
        {**compute, "turns": [{"model": "claude-opus-5-5", "effort": "high"}]},
        {"profile": "review", "provider": "cc", "model": "claude-opus-5-5"}, cost.WI)
    assert problem.startswith("reviewer_wi_undeclared")


CHILD = "wi_20260101_201"


def at(minute):
    return f"2026-01-01T01:{minute:02d}:00Z"


def request(fixture, rows, label, minute, wis, spawn=None, compact=False, apart=0):
    """A native CC request declaring each of `wis` in turn, `apart` minutes after
    the previous one (0: at one instant); it may dispatch `spawn`."""
    from pulse_compute_cost import mcp_request_purpose as m

    session = fixture.SESSION
    user = {"type": "user", "sessionId": session, "isSidechain": False, "uuid": "input-" + label,
            "parentUuid": rows[-1]["uuid"] if rows else None, "promptId": "prompt-" + label,
            "timestamp": at(minute), "message": {"content": "work " + label}}
    rows += [user, {**fixture.usage(session, minute + 1, label + "-before"), "parentUuid": user["uuid"]}]
    if compact:
        rows.append({**user, "uuid": "summary-" + label, "parentUuid": rows[-1]["uuid"],
                     "isCompactSummary": True, "timestamp": at(minute + 2),
                     "message": {"content": "summary of " + label}})
    for n, wi in enumerate(wis):
        use, done = fixture.declaration(session, minute + 3 + n * apart, f"tool-{label}{n}", wi)
        use["parentUuid"], done["parentUuid"], done["promptId"] = rows[-1]["uuid"], use["uuid"], user["promptId"]
        done["message"]["content"][0]["content"] = m.handle(
            fixture.call(wi), clock=lambda: use["timestamp"], host="cc")["content"]
        rows += [use, done]
    if spawn:
        call = {"type": "assistant", "sessionId": session, "isSidechain": False, "uuid": "spawn-" + label,
                "parentUuid": rows[-1]["uuid"], "timestamp": at(minute + 4), "message": {
                    "id": "msg-spawn-" + label, "model": "claude-opus-5-5", "content": [
                        {"type": "tool_use", "id": "toolu_" + label, "name": "Agent",
                         "input": {"prompt": "do it"}}]}}
        result = {"type": "user", "sessionId": session, "isSidechain": False, "uuid": "spawned-" + label,
                  "parentUuid": call["uuid"], "promptId": user["promptId"], "timestamp": at(minute + 4),
                  "sourceToolAssistantUUID": call["uuid"], "toolUseResult": {"agentId": spawn},
                  "message": {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "toolu_" + label, "content": "done"}]}}
        rows += [call, result]
    rows.append({**fixture.usage(session, minute + 5, label + "-after"), "parentUuid": rows[-1]["uuid"]})


def elsewhere(fixture, rows, shape, minute):
    """A main declaration of X in no request the sub-agent serves, as real sessions
    hold them: after a background task's completion notice, or after a compaction
    summary whose prompt has no main input. `undecodable` is an ordinary request
    whose ack names another WI than the call: ComputeCost voids the session."""
    from pulse_compute_cost import mcp_request_purpose as m

    session = fixture.SESSION
    entry = {"type": "user", "sessionId": session, "isSidechain": False, "uuid": "input-" + shape,
             "parentUuid": rows[-1]["uuid"], "promptId": "prompt-" + shape, "timestamp": at(minute),
             "message": {"content": "background task finished"}}
    if shape == "notification":
        entry["origin"] = {"kind": "task-notification", "producer": "session-task", "runId": "run-1"}
    if shape == "ghost_summary":
        entry.update(isCompactSummary=True, promptId="prompt-unknown")
    use, done = fixture.declaration(session, minute + 1, "tool-" + shape, fixture.OTHER_WI)
    use["parentUuid"], done["parentUuid"], done["promptId"] = entry["uuid"], use["uuid"], entry["promptId"]
    acked = fixture.WI if shape == "undecodable" else fixture.OTHER_WI
    done["message"]["content"][0]["content"] = m.handle(
        fixture.call(acked), clock=lambda: use["timestamp"], host="cc")["content"]
    rows += [entry, use, done]


def child_input(fixture, label, minute, prompt, parent, **extra):
    return {"type": "user", "sessionId": fixture.SESSION, "isSidechain": True, "agentId": "child",
            "uuid": "child-input-" + label, "parentUuid": parent, "promptId": prompt,
            "timestamp": at(minute), **extra}


def child_turn(fixture, label, minute, parent):
    return {**fixture.usage(fixture.SESSION, minute, "child-" + label, sidechain=True),
            "agentId": "child", "parentUuid": parent}


# Each case: the WIs of request A (which dispatches `[WI: C]`) and request B,
# and how the sub-agent continues. P is the parent, X another WI.
CASES = {
    "a_declares_x": ("X", "P", None),
    "a_declares_p": ("P", "X", None),
    "a_declares_c": ("C", "X", None),
    "a_undeclared": ("", "P", None),
    "a_switches_p_to_x": ("PX", "P", None),
    # Review round 2: A's declaration call is linked to a missing record.
    "a_declaration_unlinked": ("P", "X", "unlink"),
    # Review round 2: a later plain input of request B (declared X) continues the sub-agent.
    "b_input_continues_x": ("P", "X", "input"),
    "declared_after_compaction": ("P", "X", "compact"),
    "b_resumes_declared_x": ("P", "X", "resume"),
    "b_resumes_declared_p": ("P", "P", "resume"),
    "b_resumes_declared_c": ("P", "C", "resume"),
    "b_resumes_declared_none": ("P", "N", "resume"),
    # PULSE #1560: request B declares none, then P (or P, then X) before or after
    # it resumes the sub-agent; ComputeCost charges each turn the WI then in effect.
    "b_resumes_after_none_to_p": ("P", "NP", "resume_after_switch"),
    "b_resumes_before_none_to_p": ("P", "NP", "resume_before_switch"),
    "b_resumes_after_p_to_x": ("P", "PX", "resume_after_switch"),
    "b_resumes_before_p_to_x": ("P", "PX", "resume_before_switch"),
    # Review round 3: a fork copy re-links the same turn under request B (declared X).
    "fork_copy_relinks_turn_to_x": ("P", "X", "fork"),
    "other_file_repeats_turn_message": ("P", "X", "message_copy"),
    # Review round 3: ComputeCost's registry names no child export.
    "a_declares_p_without_export": ("P", "X", "no_export"),
    "a_declares_c_without_export": ("C", "X", "no_export"),
    "child_created_after_first_turn": ("P", "X", "late_child"),
    # Real sessions (PULSE #1541 follow-up): the session also declared X where no
    # request the sub-agent serves is affected; one undecodable declaration voids all.
    "x_declared_after_task_notification": ("P", "X", "notification"),
    "x_declared_after_summary_of_unknown_prompt": ("C", "X", "ghost_summary"),
    "undecodable_declaration_elsewhere": ("P", "X", "undecodable"),
}
ACCEPTED = {"a_declares_p", "a_declares_c", "declared_after_compaction",
            "b_resumes_declared_p", "b_resumes_declared_c", "a_declares_c_without_export",
            "x_declared_after_task_notification", "x_declared_after_summary_of_unknown_prompt",
            "b_resumes_after_none_to_p", "b_resumes_before_p_to_x"}
# Minutes between request B's declarations: the sub-agent resumes at minute 40,
# after the second one (33:30, 37:30) or between them (33:30, 43:30).
APART = {"resume_after_switch": 4, "resume_before_switch": 10}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_child_pr_admits_a_marked_sub_agent_only_when_its_whole_cost_is_the_child_s(
        cost, tmp_path, monkeypatch, case):
    """The kernel's verdict and the ComputeCost collector's on one set of native records."""
    from pulse_compute_cost.cc_request_purpose import POLICY
    from pulse_compute_cost.ledger import UsageLedger
    from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources
    from pulse_compute_cost.receipts import current_receipts, reference
    from harness_rewrite import child_dispatch

    parent = cost.WI
    names = {"P": parent, "X": cost.OTHER_WI, "C": CHILD, "N": "none"}
    first, second, shape = CASES[case]
    rows: list = []
    request(cost, rows, "a", 0, [names[c] for c in first], spawn="child", compact=shape == "compact")
    if shape in {"notification", "ghost_summary", "undecodable"}:
        elsewhere(cost, rows, shape, 15)
    request(cost, rows, "b", 30, [names[c] for c in second], apart=APART.get(shape, 0))
    if shape == "unlink":
        next(r for r in rows if r["uuid"] == "use-tool-a0")["parentUuid"] = "missing-record"
    child = [child_input(cost, "a", 6, "prompt-a", None,
                         message={"role": "user", "content": f"[WI: {CHILD}]\ndelegated task"}),
             child_turn(cost, "a", 45 if shape == "fork" else 25, "child-input-a")]
    if shape == "input":
        child += [child_input(cost, "b", 40, "prompt-b", "u-child-a",
                              message={"role": "user", "content": "more work"}),
                  child_turn(cost, "b", 41, "child-input-b")]
    if shape == "resume" or shape in APART:
        child += [child_input(cost, "b", 40, "prompt-b", "u-child-a", isMeta=True,
                              origin={"kind": "coordinator"},
                              message={"role": "user", "content": "continue"}),
                  child_turn(cost, "b", 41, "child-input-b")]
    project = tmp_path / "projects"
    (project / cost.SESSION / "subagents").mkdir(parents=True)
    transcript = project / f"{cost.SESSION}.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in rows))
    (project / cost.SESSION / "subagents" / "agent-child.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in child))
    if shape in {"fork", "message_copy"}:
        # Another record file of the session: a fork repeats the dispatch prompt and
        # the turn under its own agentId, re-linked to request B's input.
        fork = [{**r, "agentId": "fork"} for r in child]
        relinked = {**fork[1], "parentUuid": "fork-input-b"}
        if shape == "message_copy":
            fork[0] = {**fork[0], "uuid": "fork-prompt"}
            relinked["uuid"] = "fork-turn"
        fork[1:] = [child_input(cost, "fork-b", 40, "prompt-b", fork[0]["uuid"], agentId="fork",
                                uuid="fork-input-b",
                                message={"role": "user", "content": "go on"}), relinked]
        (project / cost.SESSION / "subagents" / "agent-fork.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in fork))
    facts = tmp_path / "facts.json"
    created = "2026-01-01T01:30:00Z" if shape == "late_child" else "2026-01-01T00:30:00Z"
    facts.write_text(json.dumps({"schema": "harness.stats_facts/v1", "read_only": True,
                                 "source": "journal", "namespace": "test", "items": [
                                     {"id": CHILD, "parent": parent, "status": "active",
                                      "created_at": created, "closed_at": None}]}))
    facts.chmod(0o600)
    registry = {"schema": REGISTRY_SCHEMA,
                "sources": [{"id": "cc", "kind": "cc", "path": str(project),
                             "session_wi_mcp_since": cost.BEFORE}]}
    if shape != "no_export":
        registry["core_session_writes"] = {"path": str(facts), "surface": "local"}
    # The Host reads the same registry ComputeCost collects with.
    (tmp_path / "sources.json").write_text(json.dumps(registry))
    monkeypatch.setattr(child_dispatch, "registry_path", lambda: tmp_path / "sources.json")
    ledger = UsageLedger(tmp_path / "ledger")
    collect_registered_sources(registry, ledger)
    active, _ = current_receipts(ledger.receipt_snapshot()["facts"])
    charged = {r.payload["target_ref"]: r.payload["wi"] for r in active
               if r.kind == "attribution" and r.producer == POLICY}
    turns = [r["message"]["id"] for r in child if r["type"] == "assistant"]
    paid = [charged.get(reference("usage", [cost.SESSION, t])) for t in turns]

    [agent] = observe("cc", transcript)["subagents"]
    session = sorted({names[c] for c in first + second} - {"none"})
    kernel = attribution({"parent": parent, "children": []}, CHILD, session, agent["wi_marker"],
                         agent["declared_wis"], agent)
    # The child's pull request admits the sub-agent only when ComputeCost charges
    # every one of its turns to the child.
    assert (kernel == [CHILD]) == (case in ACCEPTED)
    assert (paid == [CHILD] * len(turns)) == (case in ACCEPTED), paid


def commit_turn(fixture, rows, label, minute):
    """The main turn of request `label` that runs `git commit` in a Bash call."""
    request_prompt = next(r["promptId"] for r in rows if r["uuid"] == "input-" + label)
    call = {"type": "assistant", "sessionId": fixture.SESSION, "isSidechain": False,
            "uuid": "commit-" + label, "parentUuid": rows[-1]["uuid"], "timestamp": at(minute),
            "cwd": "/repo", "message": {
                "id": "msg-commit-" + label, "model": "openai/test", "content": [
                    {"type": "tool_use", "id": "toolu_commit_" + label, "name": "Bash",
                     "input": {"command": "git commit -qm change"}}],
                "usage": {"input_tokens": 100, "output_tokens": 10}}}
    done = {"type": "user", "sessionId": fixture.SESSION, "isSidechain": False,
            "uuid": "committed-" + label, "parentUuid": call["uuid"], "promptId": request_prompt,
            "timestamp": at(minute), "sourceToolAssistantUUID": call["uuid"],
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_commit_" + label, "content": ""}]}}
    rows += [call, done]


def charged_by_compute_cost(fixture, tmp_path, project):
    from pulse_compute_cost.cc_request_purpose import POLICY
    from pulse_compute_cost.ledger import UsageLedger
    from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources
    from pulse_compute_cost.receipts import current_receipts, reference

    registry = {"schema": REGISTRY_SCHEMA,
                "sources": [{"id": "cc", "kind": "cc", "path": str(project),
                             "session_wi_mcp_since": fixture.BEFORE}]}
    ledger = UsageLedger(tmp_path / "ledger")
    collect_registered_sources(registry, ledger)
    active, _ = current_receipts(ledger.receipt_snapshot()["facts"])
    charged = {r.payload["target_ref"]: r.payload["wi"] for r in active
               if r.kind == "attribution" and r.producer == POLICY}
    return lambda message: charged.get(reference("usage", [fixture.SESSION, message]))


# The session declared the parent P and its child C in different requests; the
# commit is made in request B. Registered on P, it is accepted exactly when
# ComputeCost charges the committing turn to P.
OWN_COMMITS = {
    "b_declares_p": ("C", "P", True),
    "b_declares_c": ("P", "C", False),
    "b_switches_p_to_c": ("P", "PC", False),
    "b_undeclared": ("PC", "", False),
    # B switches WI 4 minutes before the commit (minute 40), or 10 minutes after.
    "b_switches_c_to_p_before_commit": ("P", "CP", True, 4),
    "b_switches_p_to_c_after_commit": ("P", "PC", True, 10),
    "b_switches_p_to_c_before_commit": ("P", "PC", False, 4),
}


@pytest.mark.parametrize("case", sorted(OWN_COMMITS))
def test_a_session_s_own_commit_belongs_to_the_wi_its_request_is_charged_to(cost, tmp_path, case):
    from harness_rewrite.pull_requests import author_wis
    from harness_rewrite.session_compute import request_proof

    parent = cost.WI
    names = {"P": parent, "C": CHILD}
    first, second, accepted, *apart = OWN_COMMITS[case]
    rows: list = []
    request(cost, rows, "a", 0, [names[c] for c in first])
    request(cost, rows, "b", 30, [names[c] for c in second], apart=apart[0] if apart else 0)
    commit_turn(cost, rows, "b", 40)
    project = tmp_path / "projects"
    project.mkdir()
    transcript = project / f"{cost.SESSION}.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in rows))
    paid = charged_by_compute_cost(cost, tmp_path, project)("msg-commit-b")

    compute = observe("cc", transcript)
    [window] = compute["commits"]
    proof = request_proof(transcript)(window["tool_use_id"])
    assert compute["declared_wis"] == sorted({names[c] for c in first + second})
    assert (author_wis(compute["declared_wis"], parent, [proof]) == [parent]) == accepted
    assert (paid == parent) == accepted, paid


# A sub-agent marked `[WI: P]` dispatched by request B; the session declared C in
# request A. Registered on P, it is accepted exactly when ComputeCost charges
# every one of its turns to P.
MARKED_PARENT = {"b_declares_p": ("C", "P", True), "b_declares_c": ("P", "C", False)}


@pytest.mark.parametrize("case", sorted(MARKED_PARENT))
def test_a_sub_agent_marked_with_the_registration_wi_belongs_to_it_when_charged_there(
        cost, tmp_path, monkeypatch, case):
    from harness_rewrite import child_dispatch

    parent = cost.WI
    names = {"P": parent, "C": CHILD}
    first, second, accepted = MARKED_PARENT[case]
    rows: list = []
    request(cost, rows, "a", 0, [names[c] for c in first])
    request(cost, rows, "b", 30, [names[c] for c in second], spawn="child")
    child = [child_input(cost, "b", 36, "prompt-b", None,
                         message={"role": "user", "content": f"[WI: {parent}]\ndelegated task"}),
             child_turn(cost, "b", 37, "child-input-b")]
    project = tmp_path / "projects"
    (project / cost.SESSION / "subagents").mkdir(parents=True)
    transcript = project / f"{cost.SESSION}.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in rows))
    (project / cost.SESSION / "subagents" / "agent-child.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in child))
    monkeypatch.setattr(child_dispatch, "registry_path", lambda: tmp_path / "missing.json")
    paid = charged_by_compute_cost(cost, tmp_path, project)("msg-child-b")

    [agent] = observe("cc", transcript)["subagents"]
    state = {"parent": None, "children": [{"id": CHILD, "status": "completed"}]}
    session = sorted({parent, CHILD})
    kernel = attribution(state, parent, session, agent["wi_marker"], agent["declared_wis"], agent)
    assert (kernel == [parent]) == accepted
    assert (paid == parent) == accepted, paid
