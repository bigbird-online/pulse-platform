"""Child WorkItems: created and accepted by the parent's holder session."""

from dataclasses import replace

import pytest
from types import SimpleNamespace

from harness_rewrite import Command, collaboration, collaboration_inbox, stats
from harness_rewrite.domain import INTENTS
from harness_rewrite.model import Query, SourceInput, digest
from tests_rewrite.test_lightweight import light as light_rig, record
from tests_rewrite.test_review import ARTIFACT
from tests_rewrite.test_workflow import BASE, OTHER, error, original

CHILD = {
    "parent": None,
    "summary": "Child fixture work for tests.",
    "why": "A new item with its own acceptance",
    "what": "Produce the child artifact",
    "acceptance": ["A child artifact", "A second check"],
    "resources": ["repo/child"],
}


FULL = replace(BASE, allowed_intents=INTENTS)


def light(tmp_path):
    rig = light_rig(tmp_path)
    for name in ("owner", "create", "helper"):
        rig.contexts[name] = replace(rig.contexts[name], allowed_intents=INTENTS)
    rig.contexts["spawn"] = replace(FULL, source=SourceInput("child", "native_agent", b"Create child"))
    approve = rig.approve
    rig.approve = lambda intent, data, actor=FULL, **kw: approve(intent, data, actor=actor, **kw)
    return rig


def spawn(rig, request="child", handle="spawn", **changes):
    data = {**CHILD, "parent": rig.wi, **changes}
    return rig.kernel.execute(Command(request, "create_child", data), handle)


def send(rig, wi, intent, data, handle="owner", request=None):
    rig.counter += 1
    return rig.kernel.execute(
        Command(request or f"c-{rig.counter}", intent, data, wi), handle
    )


def view(rig, wi):
    return rig.kernel.inspect(Query(wi), "owner")["result"]["current_observation"]


def test_child_inherits_owner_label_and_holder_and_starts_active(tmp_path):
    rig = light(tmp_path)
    created = spawn(rig)
    child = original(created)["work_item_id"]
    assert original(created)["parent"] == rig.wi and created["work_item"]["business_progress"] == "active"
    state = view(rig, child)
    parent = view(rig, rig.wi)
    assert state["parent"] == rig.wi and state["children"] == []
    assert state["definition"]["owner"] == parent["definition"]["owner"] == "producer"
    assert parent["parent"] is None
    assert parent["children"] == [{"id": child, "status": "active", "business_progress": "active",
                                   "summary": CHILD["summary"], "pull_requests": []}]
    # The holder session records work on the child without any start or transfer.
    rig.contexts["work"] = replace(BASE, artifacts=())
    assert send(rig, child, "checkpoint", {"summary": "Started", "resources": ["repo/child"]},
                "work")["status"] == "local_committed"
    # The exact retry returns the original creation.
    assert original(spawn(rig))["work_item_id"] == child


def test_child_rejects_owner_input_other_sessions_and_a_second_level(tmp_path):
    rig = light(tmp_path)
    assert error(spawn(rig, "owned", owner="helper")) == "invalid_input"
    rig.contexts["other"] = replace(OTHER, allowed_intents=INTENTS, source=SourceInput("x", "native_agent", b"x"))
    assert error(spawn(rig, "foreign", "other")) == "authority_denied"
    # The holder's session at an earlier generation is not the current holder.
    assert error(spawn(rig, "stale", "create")) == "authority_denied"
    assert error(spawn(rig, "missing", parent="wi_absent")) == "domain_conflict"
    child = original(spawn(rig))["work_item_id"]
    grandchild = rig.kernel.execute(
        Command("grandchild", "create_child", {**CHILD, "parent": child}), "spawn"
    )
    assert error(grandchild) == "domain_conflict"
    assert "one level" in grandchild["error"]["reason"]


def test_open_child_blocks_parent_completion_and_termination(tmp_path):
    rig = light(tmp_path)
    work = record(rig)
    child = original(spawn(rig))["work_item_id"]
    done = rig.approve("complete_lightweight", {"record_ids": [work], "acceptance_refs": ["AC-1"]})
    assert error(done) == "domain_conflict" and child in done["error"]["reason"]
    ended = rig.approve("terminate_work", {"reason": "Stop", "kind": "cancelled"})
    assert error(ended) == "domain_conflict" and child in ended["error"]["reason"]


def test_review_child_approval_completes_the_child_then_the_parent(tmp_path):
    rig = light(tmp_path)
    parent_work = record(rig)
    child = original(spawn(rig))["work_item_id"]
    assert error(send(rig, child, "review_child", {"decision": "approved", "reason": "ok"})) == (
        "domain_conflict"
    )  # Nothing recorded yet.
    rig.contexts["work"] = replace(BASE, artifacts=(ARTIFACT,))
    work = original(send(rig, child, "record_work", {
        "summary": "Child artifact", "resources": ["repo/child"],
        "artifacts": [ARTIFACT.reference()]}, "work"))["record_id"]
    closing = {"record_ids": [work], "acceptance_refs": ["AC-1", "AC-2"]}
    refused = send(rig, child, "complete_lightweight", closing)
    assert error(refused) == "domain_conflict" and "review_child" in refused["error"]["reason"]
    assert error(send(rig, child, "review_child", {"decision": "approved", "reason": "ok"},
                      "helper")) == "authority_denied"
    assert error(send(rig, child, "review_child", {"decision": "maybe", "reason": "?"})) == (
        "invalid_input"
    )
    returned = send(rig, child, "review_child", {"decision": "returned", "reason": "Missing AC-2"})
    assert returned["work_item"]["business_progress"] == "active"
    assert original(returned)["completed"] is False
    approved = send(rig, child, "review_child", {"decision": "approved", "reason": "Both met"})
    assert approved["work_item"]["business_progress"] == "completed"
    state = view(rig, child)
    assert state["terminal"]["review_id"] == original(approved)["review_id"]
    assert state["terminal"]["source_digest"] is None
    assert state["terminal"]["record_ids"] == [work]
    assert state["terminal"]["acceptance_refs"] == ["AC-1", "AC-2"]
    assert [r["decision"] for r in state["child_reviews"]] == ["returned", "approved"]
    assert view(rig, rig.wi)["children"][0]["status"] == "completed"
    # The child's end has a time in the Stats facts.
    storage = SimpleNamespace(connection=rig.kernel._connection)
    facts = stats.facts(storage, BASE.namespace, "2026-10-29T00:00:00+00:00")
    [row] = [item for item in facts["items"] if item["id"] == child]
    assert row["status"] == "completed" and row["closed_at"] == "2026-10-07T00:00:00Z"
    # The parent still needs the human, and now may end.
    finished = rig.approve(
        "complete_lightweight", {"record_ids": [parent_work], "acceptance_refs": ["AC-1"]}
    )
    assert finished["work_item"]["business_progress"] == "completed"


def test_a_terminated_child_no_longer_blocks_its_parent(tmp_path):
    rig = light(tmp_path)
    work = record(rig)
    child = original(spawn(rig))["work_item_id"]
    data = {"reason": "Not needed", "kind": "cancelled"}
    rig.contexts["cancel"] = replace(FULL, source=SourceInput(
        "h", "native_human", b"cancel", "terminate_work",
        digest({"intent": "terminate_work", "work_item_id": child, "input": data})))
    assert send(rig, child, "terminate_work", data, "cancel")["status"] == "local_committed"
    assert view(rig, rig.wi)["children"][0]["status"] == "terminated"
    done = rig.approve("complete_lightweight", {"record_ids": [work], "acceptance_refs": ["AC-1"]})
    assert done["work_item"]["business_progress"] == "completed"


def test_stats_facts_name_each_childs_parent(tmp_path):
    rig = light(tmp_path)
    child = original(spawn(rig))["work_item_id"]
    storage = SimpleNamespace(connection=rig.kernel._connection)
    facts = stats.facts(storage, BASE.namespace, "2026-10-29T00:00:00+00:00")
    parents = {item["id"]: item["parent"] for item in facts["items"]}
    assert parents[child] == rig.wi and parents[rig.wi] is None


def test_review_child_applies_only_to_children(tmp_path):
    rig = light(tmp_path)
    record(rig)
    assert error(rig.send("review_child", {"decision": "approved", "reason": "ok"})) == (
        "domain_conflict"
    )


def test_production_ledger_finds_children_through_their_claims(tmp_path):
    from tests_rewrite.test_production_ledger import Rig as Ledger, git

    rig = Ledger(tmp_path.resolve())
    rig.context = replace(rig.context, generation=0,
                          source=SourceInput("initial", "native_agent", b"Create"))
    created = rig.kernel.execute(Command("parent", "create_lightweight", {
        **{k: v for k, v in CHILD.items() if k != "parent"}, "owner": BASE.actor}), "native")
    parent = original(created)["work_item_id"]
    decided = {"type": "operating_area", "ref": "OA-PULSE", "work_class": "improvement",
               "reason": "Human decision"}
    rig.context = replace(rig.context, generation=1, source=SourceInput(
        "h", "native_human", b"OA-PULSE", "record_affiliation",
        digest({"intent": "record_affiliation", "work_item_id": parent, "input": decided})))
    assert rig.kernel.execute(Command("oa", "record_affiliation", decided, parent), "native")[
        "status"] == "local_committed"
    assert rig.kernel.execute(Command("start", "start_work", {}, parent), "native")[
        "status"] == "local_committed"
    child = original(rig.kernel.execute(
        Command("child", "create_child", {**CHILD, "parent": parent}), "native"))["work_item_id"]
    claims = git(rig.remote, "ls-tree", "-r", "--name-only", "main", "--",
                 f".harness/core/children/{parent}/")
    assert claims.strip()
    # Another client reads the child's journal inside the parent's own read.
    peer = rig.client(rig.backend(rig.b))
    seen = peer.inspect(Query(parent), "native")["result"]["current_observation"]
    assert seen["children"] == [{"id": child, "status": "active", "business_progress": "active",
                                   "summary": CHILD["summary"], "pull_requests": []}]
    child_view = peer.inspect(Query(child), "native")["result"]["current_observation"]
    assert child_view["parent"] == parent
    # The parent's business affiliation is the child's.
    assert child_view["affiliation"]["state"] == "admitted"
    assert child_view["affiliation"]["reference"] == {"type": "operating_area", "ref": "OA-PULSE"}
    assert child_view["affiliation"]["inherited_from"] == parent
    rig.context = replace(rig.context, source=SourceInput(
        "h", "native_human", b"cancel", "terminate_work",
        digest({"intent": "terminate_work", "work_item_id": parent,
                "input": {"reason": "Stop", "kind": "cancelled"}})))
    ended = peer.execute(Command("end", "terminate_work", {"reason": "Stop", "kind": "cancelled"},
                                 parent), "native")
    assert error(ended) == "domain_conflict" and child in ended["error"]["reason"]


def recorded_child(rig):
    child = original(spawn(rig))["work_item_id"]
    rig.contexts["work"] = replace(BASE, artifacts=(ARTIFACT,))
    original(send(rig, child, "record_work", {
        "summary": "Child artifact", "resources": ["repo/child"],
        "artifacts": [ARTIFACT.reference()]}, "work"))
    return child


def test_review_child_requires_the_holder_s_current_generation(tmp_path):
    rig = light(tmp_path)
    child = recorded_child(rig)
    rig.contexts["stale"] = replace(FULL, generation=0)
    stale = send(rig, child, "review_child", {"decision": "approved", "reason": "ok"}, "stale")
    assert error(stale) == "authority_changed"
    assert view(rig, child)["business_progress"] == "active" and view(rig, child)["child_reviews"] == []


def test_stats_facts_mark_a_child_completion_as_agent_review(tmp_path):
    rig = light(tmp_path)
    child = recorded_child(rig)
    review_id = original(send(rig, child, "review_child", {"decision": "approved", "reason": "ok"}))[
        "review_id"]
    storage = SimpleNamespace(connection=rig.kernel._connection)
    facts = stats.facts(storage, BASE.namespace, "2026-10-29T00:00:00+00:00")
    [row] = [item for item in facts["items"] if item["id"] == child]
    [completion] = row["completions"]
    assert completion["verification"] == "agent_review"
    assert completion["acceptance"] == digest(["child_review", review_id, ["AC-1", "AC-2"]])
    assert row["parent"] == rig.wi and row["closed_at"] is not None


def test_production_create_child_retry_returns_the_original_child(tmp_path):
    from tests_rewrite.test_production_ledger import Rig as Ledger

    rig = Ledger(tmp_path.resolve())
    rig.context = replace(rig.context, generation=0,
                          source=SourceInput("initial", "native_agent", b"Create"))
    parent = original(rig.kernel.execute(Command("parent", "create_lightweight", {
        **{k: v for k, v in CHILD.items() if k != "parent"}, "owner": BASE.actor}), "native"))[
        "work_item_id"]
    decided = {"type": "operating_area", "ref": "OA-PULSE", "work_class": "improvement",
               "reason": "Human decision"}
    rig.context = replace(rig.context, generation=1, source=SourceInput(
        "h", "native_human", b"OA-PULSE", "record_affiliation",
        digest({"intent": "record_affiliation", "work_item_id": parent, "input": decided})))
    original(rig.kernel.execute(Command("oa", "record_affiliation", decided, parent), "native"))
    original(rig.kernel.execute(Command("start", "start_work", {}, parent), "native"))
    agent = SourceInput("child", "native_agent", b"Create child")
    rig.context = replace(rig.context, source=agent)
    command = Command("child", "create_child", {**CHILD, "parent": parent})
    first = original(rig.kernel.execute(command, "native"))
    child = first["work_item_id"]
    note = {"summary": "Child progress", "resources": ["repo/child"]}
    # A child's write reads its parent's journal for the current holder.
    assert rig.kernel.execute(Command("note", "checkpoint", note, child), "native")[
        "status"] == "local_committed"
    # The parent is no longer active; the exact retry still returns the creation.
    stop = {"reason": "Pause"}
    rig.context = replace(rig.context, source=SourceInput(
        "s", "native_human", b"stop", "stop",
        digest({"intent": "stop", "work_item_id": parent, "input": stop})))
    original(rig.kernel.execute(Command("stop", "stop", stop, parent), "native"))
    rig.context = replace(rig.context, source=agent)
    assert original(rig.kernel.execute(command, "native")) == first
    # Nobody holds the child of a stopped parent.
    assert error(rig.kernel.execute(Command("note-2", "checkpoint", note, child), "native")) == (
        "authority_denied")
    # A changed Host generation is not a different request either.
    rig.context = replace(rig.context, generation=7)
    assert original(rig.kernel.execute(command, "native")) == first


REVIEW = {"decision": "approved", "reason": "Both met"}


def child_work(rig, child, handle):
    rig.contexts[handle + "-work"] = replace(rig.contexts[handle], artifacts=(ARTIFACT,))
    return send(rig, child, "record_work", {
        "summary": "More child work", "resources": ["repo/child"],
        "artifacts": [ARTIFACT.reference()]}, handle + "-work")


def moved(rig, child, new, old_session, same_agent=False):
    """After the parent moved to `new`, the child's writes follow it."""
    # The former holder, stale or at the parent's current generation, is refused.
    assert error(send(rig, child, "review_child", REVIEW)) in {"authority_changed", "authority_denied"}
    rig.contexts["former"] = replace(old_session, generation=view(rig, rig.wi)["generation"])
    assert error(send(rig, child, "review_child", REVIEW, "former")) == "authority_denied"
    # Another session of the holder's own Agent may still append records (as on
    # any WI); a different Agent may not.
    assert error(child_work(rig, child, "former")) == (None if same_agent else "authority_denied")
    # The child reports its parent's control.
    assert view(rig, child)["generation"] == view(rig, rig.wi)["generation"]
    assert child_work(rig, child, new)["status"] == "local_committed"
    done = send(rig, child, "review_child", REVIEW, new)
    assert done["work_item"]["business_progress"] == "completed"


def test_a_parent_takeover_moves_review_and_work_on_its_children(tmp_path):
    rig = light(tmp_path)
    child = recorded_child(rig)
    taker = replace(FULL, actor="successor", session="successor-session", generation=0)
    original(rig.approve("assume_control", {"reason": "human takeover"}, actor=taker))
    rig.contexts["taker"] = replace(taker, generation=view(rig, rig.wi)["generation"])
    # Collection reads (Stats facts, listings) show the parent's holder on the child too.
    storage = SimpleNamespace(connection=rig.kernel._connection)
    facts = stats.facts(storage, BASE.namespace, "2026-10-29T00:00:00+00:00")
    assert {item["id"]: item["holder"] for item in facts["items"]}[child] == "successor"
    moved(rig, child, "taker", FULL)


def test_continuing_the_parent_elsewhere_moves_its_children(tmp_path):
    rig = light(tmp_path)
    child = recorded_child(rig)
    here = replace(FULL, session="feishu-session")
    original(rig.approve("continue_here", {"reason": "continue here"}, actor=here))
    rig.contexts["here"] = replace(here, generation=view(rig, rig.wi)["generation"])
    moved(rig, child, "here", FULL, same_agent=True)


def test_a_parent_rollover_moves_its_children_and_pauses_them_in_transfer(tmp_path):
    rig = light(tmp_path)
    child = recorded_child(rig)
    transfer = original(rig.send("rollover", {"target": OTHER.principal(), "summary": "Resume"}))[
        "transfer_id"]
    # While the parent is in transfer nobody holds its children.
    assert view(rig, child)["business_progress"] == "active"
    assert error(send(rig, child, "review_child", REVIEW)) == "authority_denied"
    original(rig.send("accept_transfer", {"transfer_id": transfer}, handle="helper"))
    rig.contexts["successor"] = replace(OTHER, allowed_intents=INTENTS,
                                        generation=view(rig, rig.wi)["generation"])
    moved(rig, child, "successor", FULL)


def test_control_moves_only_on_the_parent(tmp_path):
    rig = light(tmp_path)
    child = original(spawn(rig))["work_item_id"]
    refused = send(rig, child, "rollover", {"target": OTHER.principal(), "summary": "Hand over"})
    assert error(refused) == "domain_conflict" and "parent" in refused["error"]["reason"]


def test_an_ended_child_has_no_holder_in_the_facts(tmp_path):
    rig = light(tmp_path)
    child = recorded_child(rig)
    original(send(rig, child, "review_child", REVIEW))
    storage = SimpleNamespace(connection=rig.kernel._connection)
    facts = stats.facts(storage, BASE.namespace, "2026-10-29T00:00:00+00:00")
    holders = {item["id"]: item["holder"] for item in facts["items"]}
    assert holders[child] is None and holders[rig.wi] == "producer"


def delegated(rig):
    """A child whose holder delegated checkpoints and records on it to the helper."""
    child = recorded_child(rig)
    original(send(rig, child, "grant_scope", {
        "principal": OTHER.principal(), "resources": ["repo/child"],
        "intents": ["checkpoint", "record_work"]}))
    rig.contexts["helper-1"] = replace(OTHER, allowed_intents=INTENTS, generation=1,
                                       artifacts=(ARTIFACT,))
    return child


NOTE = {"summary": "Delegated progress", "resources": ["repo/child"]}
WORK = {"summary": "Delegated work", "resources": ["repo/child"],
        "artifacts": [ARTIFACT.reference()]}


def paused_writes_refused(rig, child, handle):
    for intent, data in (("checkpoint", NOTE), ("record_work", WORK)):
        refused = send(rig, child, intent, data, handle)
        assert error(refused) == "authority_denied" and "parent" in refused["error"]["reason"]
    # The holder Agent's other session cannot append either; reads still work.
    rig.contexts["same-agent"] = replace(FULL, session="other-session",
                                         generation=view(rig, rig.wi)["generation"])
    assert error(send(rig, child, "checkpoint", NOTE, "same-agent")) == "authority_denied"
    assert rig.kernel.inspect(Query(child), handle)["status"] == "read_only"
    work = next(c for c in rig.kernel.inspect(Query(child), handle)["capabilities"]
                if c["name"] == "local_work")
    assert work["state"] == "unavailable"


def test_a_stopped_parent_pauses_delegated_work_on_its_child(tmp_path):
    rig = light(tmp_path)
    child = delegated(rig)
    assert send(rig, child, "checkpoint", NOTE, "helper-1")["status"] == "local_committed"
    original(rig.approve("stop", {"reason": "Pause"}))
    paused_writes_refused(rig, child, "helper-1")
    # Reopening the parent starts a new generation: the old delegation does not return.
    original(rig.approve("reopen_work", {"reason": "Resume", "nodes": []}))
    generation = view(rig, rig.wi)["generation"]
    rig.contexts["helper-2"] = replace(rig.contexts["helper-1"], generation=generation)
    assert error(send(rig, child, "checkpoint", NOTE, "helper-2")) == "authority_denied"
    rig.contexts["holder-2"] = replace(FULL, generation=generation)
    original(send(rig, child, "grant_scope", {
        "principal": OTHER.principal(), "resources": ["repo/child"],
        "intents": ["checkpoint"]}, "holder-2"))
    assert send(rig, child, "checkpoint", NOTE, "helper-2")["status"] == "local_committed"


def test_a_parent_in_transfer_pauses_delegated_work_on_its_child(tmp_path):
    rig = light(tmp_path)
    child = delegated(rig)
    original(rig.send("rollover", {"target": BASE.principal() | {"session": "next-session"},
                                   "summary": "Resume"}))
    paused_writes_refused(rig, child, "helper-1")
    # The human can still end the child itself.
    data = {"reason": "Not needed", "kind": "cancelled"}
    rig.contexts["cancel"] = replace(FULL, source=SourceInput(
        "h", "native_human", b"cancel", "terminate_work",
        digest({"intent": "terminate_work", "work_item_id": child, "input": data})))
    assert send(rig, child, "terminate_work", data, "cancel")["status"] == "local_committed"


def gate(tmp_path):
    from tests_rewrite.test_pull_requests import AUTHOR

    rig = light(tmp_path)
    child = original(spawn(rig))["work_item_id"]
    rig.contexts["registrant"] = replace(AUTHOR, allowed_intents=INTENTS)
    return rig, child


def register(rig, wi, context, observed=None):
    from tests_rewrite.test_pull_requests import HEAD, REPO, opened

    rig.contexts["registrant"] = replace(
        context, allowed_intents=INTENTS,
        pull_request_observation=observed or opened(HEAD))
    return send(rig, wi, "submit_pull_request",
                {"repository": REPO, "number": 7, "head_sha": HEAD}, "registrant")


def charged(parent, marker, dispatch=None, **changes):
    """Host cost evidence: ComputeCost charges the turns to `dispatch` (the parent by
    default) and exports `marker` as an open child of `parent` created before them."""
    return {"dispatch_declared_wis": [parent] if dispatch is None else dispatch,
            "dispatch_since": "2026-10-30T00:00:00Z",
            "cost_child": {"parent": parent, "status": "active",
                           "created_at": "2026-10-29T00:00:00Z", "closed_at": None},
            **changes}


def dispatched(parent, marker, dispatch=None, declared=None, **changes):
    """The parent's holder session (declared on the parent) and its sub-agent's commit,
    dispatched from a request that declared `dispatch` (the parent by default)."""
    from tests_rewrite.test_pull_requests import spawner

    context = spawner(wi_marker=marker, **charged(parent, marker, dispatch, **changes))
    return replace(context, session_compute={**context.session_compute,
                                             "declared_wis": declared or [parent]})


def test_a_child_pull_request_is_the_marked_sub_agent_s_output(tmp_path):
    rig, child = gate(tmp_path)
    assert register(rig, child, dispatched(rig.wi, child))["status"] == "local_committed"


def test_a_child_pull_request_takes_any_output_of_its_parent_lineage(tmp_path):
    # 大鸟 11-13: a parent holds full authority over its children.
    from tests_rewrite.test_pull_requests import AUTHOR, OPUS

    def cases(rig, child):
        return {
            "unmarked": dispatched(rig.wi, None),
            "other_marker": dispatched(rig.wi, "wi_20261029_999"),
            # The parent's holder session committed the head itself.
            "parent_session": replace(AUTHOR, session_compute={**OPUS, "declared_wis": [rig.wi]}),
            "both": replace(AUTHOR, session_compute={**OPUS,
                                                     "declared_wis": sorted([rig.wi, child])}),
        }
    for case in ("unmarked", "other_marker", "parent_session", "both"):
        (tmp_path / case).mkdir()
        rig, child = gate(tmp_path / case)
        accepted = register(rig, child, cases(rig, child)[case])
        assert accepted["status"] == "local_committed", case
    (tmp_path / "outside").mkdir()
    rig, child = gate(tmp_path / "outside")
    outside = replace(AUTHOR, session_compute={**OPUS,
                                               "declared_wis": sorted([rig.wi, "wi_20261015_401"])})
    refused = register(rig, child, outside)
    assert error(refused) == "domain_conflict" and "wi_20261015_401" in refused["error"]["reason"]


def test_a_long_session_s_marked_sub_agent_follows_its_dispatching_request(tmp_path):
    # Amanda repro (op_3ab7ae5a): the parent's holder session declared one WI per
    # request over a long conversation; its sub-agent marked for the child committed.
    from tests_rewrite.test_pull_requests import AUTHOR, OPUS

    other = "wi_20261015_401"
    for case in ("parent", "child", "both"):
        (tmp_path / case).mkdir()
        rig, child = gate(tmp_path / case)
        served = {"parent": [rig.wi], "child": [child], "both": sorted([rig.wi, child])}[case]
        session = sorted([other, rig.wi, child])
        accepted = register(rig, child, dispatched(rig.wi, child, served, session))
        assert accepted["status"] == "local_committed", case
    session = sorted([other, rig.wi])
    # Review repro: request A declared X and dispatched `[WI: C]`, another request
    # declared P. ComputeCost charges the sub-agent to X, so the PR is not C's.
    for served in ([other], sorted([other, rig.wi]), ["none"], [rig.wi, "none"], None):
        refused = register(rig, child, dispatched(rig.wi, child, served, session))
        assert error(refused) == "domain_conflict", served
    # A sub-agent marked for another WI, or unmarked, inherits the ambiguous declarations.
    for marker in ("wi_20261029_999", None):
        refused = register(rig, child, dispatched(rig.wi, marker, [rig.wi], session))
        assert error(refused) == "domain_conflict" and other in refused["error"]["reason"]
    # The session's own head stays ambiguous.
    own = replace(AUTHOR, session_compute={**OPUS, "declared_wis": sorted([other, rig.wi, child])})
    refused = register(rig, child, own)
    assert error(refused) == "domain_conflict" and "author session declared" in refused["error"]["reason"]


def test_one_request_advances_two_children_each_by_its_marked_sub_agent(tmp_path):
    # The main session declared the parent, then each child, in one request and
    # dispatched `[WI: A]` and `[WI: B]`: ComputeCost leaves each sub-agent's turns
    # to its marker (a sibling's declaration does not take them).
    for case in ("first", "second", "first_on_parent", "second_on_parent"):
        (tmp_path / case).mkdir()
        rig, first = gate(tmp_path / case)
        second = original(spawn(rig, request="child-2"))["work_item_id"]
        served = sorted([rig.wi, first, second])
        marker, sibling = (first, second) if case.startswith("first") else (second, first)
        evidence = {"cost_served": {rig.wi: None, sibling: charged(rig.wi, None)["cost_child"]}}
        context = dispatched(rig.wi, marker, served, list(served), **evidence)
        target = rig.wi if case.endswith("on_parent") else marker
        assert register(rig, target, context)["status"] == "local_committed", case


def test_a_marked_sub_agent_without_matching_charge_evidence_is_refused(tmp_path):
    # Registering on the child, the session declared a WI outside the child's
    # lineage too; only ComputeCost's evidence can place the sub-agent.
    other = "wi_20261015_401"
    cases = {
        # The sibling is not exported under the child's parent.
        "sibling_unexported": {"cost_served": {}},
        "sibling_other_parent": {"cost_served": {"SIBLING": {
            **charged(None, None)["cost_child"], "parent": other}}},
        # The child itself is not exported as an open child of the parent.
        "child_unexported": {"cost_child": None},
        "child_closed": {"cost_child": {**charged(None, None)["cost_child"], "status": "completed",
                                        "closed_at": "2026-10-31T00:00:00Z"}},
    }
    for case, changes in cases.items():
        (tmp_path / case).mkdir()
        rig, first = gate(tmp_path / case)
        second = original(spawn(rig, request="child-2"))["work_item_id"]
        served = sorted([rig.wi, first, second])
        session = sorted([other, rig.wi, first, second])
        evidence = {"cost_served": {rig.wi: None, second: charged(rig.wi, None)["cost_child"]}}
        for key, value in changes.items():
            evidence[key] = ({second: value["SIBLING"]} if key == "cost_served" and value else value)
        refused = register(rig, first, dispatched(rig.wi, first, served, session, **evidence))
        assert error(refused) == "domain_conflict", case
    # A sub-agent whose turns ran in a request declaring an unrelated WI stays refused.
    (tmp_path / "unrelated").mkdir()
    rig, first = gate(tmp_path / "unrelated")
    served = sorted([rig.wi, first, other])
    refused = register(rig, first, dispatched(rig.wi, first, served, sorted([other, rig.wi, first]),
                                              cost_served={rig.wi: None, other: None}))
    assert error(refused) == "domain_conflict"


def test_a_parent_declared_dispatch_registers_on_the_child_without_a_cost_export(tmp_path):
    # Whether ComputeCost moves the turns to the child is a cost question; the
    # parent's output may be registered on its child either way.
    cases = {
        "missing": {"cost_child": None},
        "other_parent": {"cost_child": {**charged(None, None)["cost_child"],
                                        "parent": "wi_20261015_401"}},
        "closed": {"cost_child": {**charged(None, None)["cost_child"], "status": "completed",
                                  "closed_at": "2026-10-31T00:00:00Z"}},
        "created_after_first_turn": {"dispatch_since": "2026-10-28T00:00:00Z"},
        "no_first_turn": {"dispatch_since": None},
    }
    for case, changes in cases.items():
        (tmp_path / case).mkdir()
        rig, child = gate(tmp_path / case)
        accepted = register(rig, child, dispatched(rig.wi, child, **changes))
        assert accepted["status"] == "local_committed", case


def test_a_parent_pull_request_takes_its_child_s_output(tmp_path):
    # Aki repro (WI293/094): after a sub-agent worked a child, the same session
    # registers on the parent, from its own head or another sub-agent's.
    from tests_rewrite.test_pull_requests import AUTHOR, OPUS

    for case in ("marked", "unmarked", "own_head"):
        (tmp_path / case).mkdir()
        rig, child = gate(tmp_path / case)
        both = sorted([rig.wi, child])
        context = {
            "marked": dispatched(rig.wi, child),
            "unmarked": dispatched(rig.wi, None, [rig.wi], both),
            "own_head": replace(AUTHOR, session_compute={**OPUS, "declared_wis": both}),
        }[case]
        assert register(rig, rig.wi, context)["status"] == "local_committed", case


def test_earlier_commit_authors_follow_the_same_child_attribution(tmp_path):
    from tests_rewrite.test_pull_requests import HEAD, opened

    rig, child = gate(tmp_path)
    body = "c" * 40
    author = {"sha": body, "session": "session.other-sub-agent", "model": "claude-sonnet-5",
              "declared_wis": [], "session_declared_wis": [rig.wi], "wi_marker": None,
              **charged(rig.wi, child)}
    outside = {**author, "session_declared_wis": ["wi_20261015_401"]}
    observed = opened(HEAD, commits=[body, HEAD], authors=[outside])
    assert error(register(rig, child, dispatched(rig.wi, child), observed)) == "domain_conflict"
    observed = opened(HEAD, commits=[body, HEAD], authors=[author])
    assert register(rig, child, dispatched(rig.wi, child), observed)["status"] == "local_committed"


def test_a_child_pull_request_follows_a_parent_takeover(tmp_path):
    rig, child = gate(tmp_path)
    taker = replace(FULL, actor="successor", session="successor-session", generation=0)
    original(rig.approve("assume_control", {"reason": "human takeover"}, actor=taker))
    generation = view(rig, rig.wi)["generation"]
    former = replace(dispatched(rig.wi, child), generation=generation)
    assert error(register(rig, child, former)) == "authority_denied"
    successor = replace(former, actor="successor", session="successor-session")
    assert register(rig, child, successor)["status"] == "local_committed"


def host_authors(tmp_path, parent_wi, own, marker):
    """Host-found authors of a sub-agent's HEAD commit: its session declared the parent,
    it declared `own` itself and its first prompt names `marker`."""
    from harness_rewrite import session_compute
    from tests_rewrite.test_pull_request_host import (
        AGENT, PARENT, cc_declare, child_rows, session_with_child, spawning_parent,
        worktree_commit)

    tmp_path.mkdir(parents=True)
    worktree, head, before, after = worktree_commit(tmp_path)
    side = {"isSidechain": True, "agentId": AGENT, "sessionId": PARENT}
    prompt = {"type": "user", **side, "message": {"role": "user", "content": f"[WI: {marker}] do it"}}
    declared = [{**row, **side} for wi in own for row in cc_declare("own-" + wi, wi)]
    root, _ = session_with_child(
        tmp_path, spawning_parent() + cc_declare("d1", parent_wi),
        [prompt, *declared, *child_rows(worktree, head, before, after)])
    found = session_compute.find_authors([("cc", root)], [head], 0)[head]
    return head, [{"sha": head, "session": "session.sub-agent", "model": entry["model"],
                   **{k: entry[k] for k in ("declared_wis", "session_declared_wis", "wi_marker")}}
                  for entry in found]


def test_host_authors_keep_a_sub_agent_s_own_declaration_apart(tmp_path):
    from tests_rewrite.test_pull_requests import opened, spawner

    rig, child = gate(tmp_path)
    head, authors = host_authors(tmp_path / "host", rig.wi, [child], child)
    assert authors[0]["declared_wis"] == [child]
    assert authors[0]["session_declared_wis"] == [rig.wi]
    context = spawner(wi_marker=child, declared_wis=[child])
    context = replace(context, session_compute={
        **context.session_compute, "declared_wis": [rig.wi],
        "subagents": [{**s, "commits": [{"sha": head[:9], "model": "claude-sonnet-5",
                                         "verified": True}]}
                      for s in context.session_compute["subagents"]]})

    def submit(observed):
        rig.contexts["registrant"] = replace(context, allowed_intents=INTENTS,
                                             pull_request_observation=observed)
        rig.counter += 1
        return rig.kernel.execute(Command(f"h-{rig.counter}", "submit_pull_request", {
            "repository": observed["repository"], "number": 7, "head_sha": head}, child),
            "registrant")

    # The sub-agent declared the child it was dispatched for: a lawful child PR.
    assert submit(opened(head, authors=authors))["status"] == "local_committed"


def test_host_authors_still_refuse_a_sub_agent_declaring_another_wi(tmp_path):
    from tests_rewrite.test_pull_requests import HEAD, opened

    rig, child = gate(tmp_path)
    # An earlier commit of the pull request, found by the Host.
    head, authors = host_authors(tmp_path / "host", rig.wi, ["wi_20261015_401"], child)
    observed = opened(HEAD, commits=[head, HEAD], authors=authors)
    refused = register(rig, child, dispatched(rig.wi, child), observed)
    assert error(refused) == "domain_conflict" and "wi_20261015_401" in refused["error"]["reason"]
    # Declarations within the parent/child lineage are one holder's output.
    head, authors = host_authors(tmp_path / "again", rig.wi, [child, rig.wi], child)
    observed = opened(HEAD, commits=[head, HEAD], authors=authors)
    accepted = register(rig, child, dispatched(rig.wi, child), observed)
    assert accepted["status"] == "local_committed"


def test_child_creation_with_a_work_item_id_says_to_use_input_parent(tmp_path):
    rig = light(tmp_path)
    refused = rig.kernel.execute(
        Command("named", "create_child", {**CHILD, "parent": rig.wi}, rig.wi), "spawn"
    )
    assert error(refused) == "invalid_input"
    assert "omit work_item_id from create_child" in refused["error"]["reason"]
    assert "input.parent" in refused["error"]["reason"]


def test_a_record_refused_to_another_agent_names_the_holder_and_the_way_in(tmp_path):
    rig = light(tmp_path)
    rig.contexts["other"] = replace(OTHER, allowed_intents=INTENTS)
    refused = send(rig, rig.wi, "checkpoint", {"summary": "Not mine", "resources": ["repo"]}, "other")
    assert error(refused) == "authority_denied"
    assert "is held by producer" in refused["error"]["reason"]
    assert "offer_collaboration" in refused["error"]["reason"]


def granted(rig, wi):
    """A standing merge authority registered on `wi` by its holder."""
    standing = {"reason": "Standing"}
    rig.contexts["grant"] = replace(FULL, source=SourceInput(
        "g", "native_human", b"grant", "grant_merge_authority",
        digest({"intent": "grant_merge_authority", "work_item_id": wi, "input": standing})))
    return original(send(rig, wi, "grant_merge_authority", standing, "grant"))["grant_id"]


def revoked_from_another_session(rig, wi, grant):
    rig.contexts["other-session"] = replace(FULL, session="another-session-of-the-agent")
    assert rig.kernel._merge_authority(FULL)
    revoke = {"grant_id": grant, "reason": "Withdrawn"}
    assert original(send(rig, wi, "revoke_merge_authority", revoke, "other-session"))["revoked"]
    assert rig.kernel._merge_authority(FULL) == ()


def test_a_merge_authority_on_a_child_is_revocable_after_the_parent_completes(tmp_path):
    rig = light(tmp_path)
    parent_work = record(rig)
    child = recorded_child(rig)
    grant = granted(rig, child)
    original(send(rig, child, "review_child", REVIEW))
    original(rig.approve("complete_lightweight",
                         {"record_ids": [parent_work], "acceptance_refs": ["AC-1"]}))
    revoked_from_another_session(rig, child, grant)


def test_a_merge_authority_on_a_child_is_revocable_while_the_parent_is_stopped(tmp_path):
    rig = light(tmp_path)
    child = original(spawn(rig))["work_item_id"]
    grant = granted(rig, child)
    original(rig.approve("stop", {"reason": "Pause"}))
    note = {"summary": "Child progress", "resources": ["repo/child"]}
    assert error(send(rig, child, "checkpoint", note)) == "authority_denied"
    revoked_from_another_session(rig, child, grant)


@pytest.mark.parametrize("ending", ["stop", "terminate_work", "complete_lightweight"])
def test_a_merge_authority_is_revocable_whatever_its_carrier_state(tmp_path, ending):
    rig = light(tmp_path)
    work = record(rig)
    grant = granted(rig, rig.wi)
    data = {"stop": {"reason": "Pause"},
            "terminate_work": {"reason": "Stop", "kind": "cancelled"},
            "complete_lightweight": {"record_ids": [work], "acceptance_refs": ["AC-1"]}}[ending]
    original(rig.approve(ending, data))
    assert view(rig, rig.wi)["merge_authority"][0]["active"] is True
    revoked_from_another_session(rig, rig.wi, grant)


def test_a_parent_registration_takes_a_declared_sibling_only_with_the_export_proof(tmp_path):
    # Registering on the parent, a `[WI: A]` sub-agent whose turns were declared
    # to sibling B belongs to A only when ComputeCost's export moves them to A.
    from harness_rewrite.pull_requests import attribution

    rig, first = gate(tmp_path)
    second = original(spawn(rig, request="child-2"))["work_item_id"]
    state = view(rig, rig.wi)
    names = sorted([rig.wi, first, second])
    unproven = {"dispatch_declared_wis": [second], "dispatch_since": "2026-10-30T00:00:00Z"}
    assert attribution(state, rig.wi, names, first, None, unproven) != [first]
    proven = {**charged(rig.wi, first, [second]),
              "cost_served": {second: charged(rig.wi, None)["cost_child"]}}
    assert attribution(state, rig.wi, names, first, None, proven) == [first]


def test_resume_gives_the_latest_checkpoint_s_own_place_before_later_work(tmp_path):
    # A work record after the handover checkpoint must not move its position.
    rig = light(tmp_path)
    handover = original(send(rig, rig.wi, "checkpoint", {"summary": "交接：下一步复核", "resources": ["repo"]}))
    rig.contexts["work"] = replace(BASE, artifacts=(ARTIFACT,))
    original(send(rig, rig.wi, "record_work", {
        "summary": "Later work", "resources": ["repo"], "artifacts": [ARTIFACT.reference()]}, "work"))
    response = rig.kernel.resume(rig.wi, "owner")
    records = response["result"]["current_observation"]["records"]
    latest = response["result"]["latest_checkpoint"]
    assert [r["kind"] for r in records][-2:] == ["CheckpointRecorded", "WorkRecorded"]
    assert (latest["record_id"], latest["summary"]) == (handover["record_id"], "交接：下一步复核")
    assert latest["position"] == len(records) - 1
    assert records[latest["position"] - 1]["record_id"] == handover["record_id"]


def offer_on(rig, wi, kind="request", resources=("repo/child",)):
    rig.contexts["offerer"] = replace(FULL, source=SourceInput("offer", "native_agent", b"Offer"))
    offered = send(rig, wi, "offer_collaboration", {
        "kind": kind,
        "recipients": [{"principal": OTHER.principal(), "resources": list(resources)}],
        "purpose": "Answer on the child", "required": True, "main_only": False}, "offerer")
    return original(offered)["contract_id"]


def child_offer(rig, kind="request"):
    """A contract the parent's holder offered on its child to another Agent."""
    child = recorded_child(rig)
    return child, offer_on(rig, child, kind)


def child_answer(rig, child, contract):
    rig.contexts["answer"] = replace(
        OTHER, allowed_intents=INTENTS, generation=view(rig, rig.wi)["generation"],
        source=SourceInput("answer", "native_agent", b"Accept"))
    return send(rig, child, "respond_collaboration", {
        "contract_id": contract, "decision": "accept", "reason": "Actual recipient decision"},
        "answer")


def own_rollover(rig, actor="producer", session="successor-session"):
    successor = replace(rig.contexts["owner"], actor=actor, session=session)
    transfer = original(rig.send("rollover", {"target": successor.principal(),
                                              "summary": "Resume"}))["transfer_id"]
    rig.contexts["successor"] = successor
    original(send(rig, rig.wi, "accept_transfer", {"transfer_id": transfer}, "successor"))
    rig.contexts["owner"] = replace(successor, generation=view(rig, rig.wi)["generation"])


def test_a_child_offer_stays_answerable_after_the_parent_holders_own_rollover(tmp_path):
    rig = light(tmp_path)
    child, contract = child_offer(rig)
    own_rollover(rig)
    own_rollover(rig, session="second-session")
    assert view(rig, child)["generation"] == view(rig, rig.wi)["generation"] == 3
    original(child_answer(rig, child, contract))


def test_a_child_offer_stays_answerable_after_the_parent_continues_elsewhere(tmp_path):
    rig = light(tmp_path)
    child, contract = child_offer(rig)
    here = replace(FULL, session="feishu-session")
    original(rig.approve("continue_here", {"reason": "continue here"}, actor=here))
    original(child_answer(rig, child, contract))


def test_a_parent_takeover_ends_child_offers(tmp_path):
    rig = light(tmp_path)
    child, contract = child_offer(rig)
    taker = replace(FULL, actor="successor", session="successor-session", generation=0)
    original(rig.approve("assume_control", {"reason": "human takeover"}, actor=taker))
    assert error(child_answer(rig, child, contract)) == "authority_changed"


def test_a_parent_rollover_to_another_agent_ends_child_offers_for_good(tmp_path):
    rig = light(tmp_path)
    child, contract = child_offer(rig)
    own_rollover(rig, actor="third", session="third-session")
    assert error(child_answer(rig, child, contract)) == "authority_changed"
    # Handing back to the offering Agent never revives the offer.
    own_rollover(rig, actor="producer", session="returned-session")
    assert error(child_answer(rig, child, contract)) == "authority_changed"


def test_reopening_the_parent_ends_child_offers(tmp_path):
    rig = light(tmp_path)
    child, contract = child_offer(rig)
    original(rig.approve("stop", {"reason": "Pause"}))
    original(rig.approve("reopen_work", {"reason": "Resume", "nodes": []}))
    assert error(child_answer(rig, child, contract)) == "authority_changed"


def offer_refused(refused):
    assert error(refused) == "domain_conflict"
    assert refused["error"]["reason"] == collaboration.CHILD_TRANSFER_REFUSED


def test_a_child_refuses_offering_full_responsibility(tmp_path):
    rig = light(tmp_path)
    child = recorded_child(rig)
    rig.contexts["offerer"] = replace(FULL, source=SourceInput("offer", "native_agent", b"Offer"))
    offer_refused(send(rig, child, "offer_collaboration", {
        "kind": "responsibility",
        "recipients": [{"principal": OTHER.principal(), "resources": ["repo/child"]}],
        "purpose": "Move the child", "required": True, "main_only": False}, "offerer"))
    assert view(rig, child)["collaborations"] == []


def test_a_parent_responsibility_offer_still_moves_the_parent(tmp_path):
    rig = light(tmp_path)
    contract = offer_on(rig, rig.wi, "responsibility", resources=("repo",))
    original(child_answer(rig, rig.wi, contract))
    assert view(rig, rig.wi)["definition"]["owner"] == "helper"


def old_child_transfer(rig, monkeypatch, required=True):
    """A full responsibility offered on a child before offers of it were refused."""
    child = recorded_child(rig)
    with monkeypatch.context() as patch:
        patch.setattr(collaboration, "refuse_child_transfer", lambda state, kind: None)
        rig.contexts["offerer"] = replace(FULL, source=SourceInput("old", "native_agent", b"Old"))
        contract = original(send(rig, child, "offer_collaboration", {
            "kind": "responsibility",
            "recipients": [{"principal": OTHER.principal(), "resources": ["repo/child"]}],
            "purpose": "Move the child", "required": required, "main_only": False},
            "offerer"))["contract_id"]
    return child, contract


def inbox(rig, actor):
    storage = SimpleNamespace(connection=rig.kernel._connection)
    return collaboration_inbox.inbox(storage, BASE.namespace, actor)["items"]


def test_an_old_child_transfer_is_never_accepted_nor_reassigned(tmp_path, monkeypatch):
    rig = light(tmp_path)
    child, contract = old_child_transfer(rig, monkeypatch)
    offer_refused(child_answer(rig, child, contract))
    # Nor after the parent holder's own rollover, which keeps other offers answerable.
    own_rollover(rig)
    offer_refused(child_answer(rig, child, contract))
    assert view(rig, child)["definition"]["owner"] == "producer"
    # Reassigning keeps the kind, so it is refused at the same offer entry.
    offer_refused(send(rig, child, "reassign_collaboration", {
        "contract_id": contract, "reason": "Someone else",
        "recipients": [{"principal": replace(OTHER, actor="third").principal(),
                        "resources": ["repo/child"]}]}))


def test_an_old_required_child_transfer_is_offered_only_for_withdrawal(tmp_path, monkeypatch):
    rig = light(tmp_path)
    child, contract = old_child_transfer(rig, monkeypatch)
    # The recipient is not asked to accept; the requester is asked only to withdraw.
    assert inbox(rig, "helper") == []
    [owed] = inbox(rig, "producer")
    assert (owed["role"], owed["contract_id"], owed["options"]) == (
        "requester", contract, ["withdraw"])
    watched = rig.kernel.inspect(Query(child, view="watch"), "owner")["result"]["items"]["items"]
    row = next(i for i in watched if i["kind"] == "collaboration")
    assert row["id"] == contract and row["detail"]["owner_attention_needed"] is True
    refused = send(rig, child, "review_child", REVIEW)
    assert error(refused) == "domain_conflict" and contract in refused["error"]["reason"]
    original(send(rig, child, "withdraw_collaboration",
                  {"contract_id": contract, "reason": "A child cannot be transferred"}))
    assert inbox(rig, "producer") == []
    done = send(rig, child, "review_child", REVIEW)
    assert done["work_item"]["business_progress"] == "completed"


def test_an_old_child_transfer_accepted_under_the_old_rule_asks_for_nothing(tmp_path, monkeypatch):
    rig = light(tmp_path)
    child, contract = old_child_transfer(rig, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(collaboration, "refuse_child_transfer", lambda state, kind: None)
        original(child_answer(rig, child, contract))
    # Settled by its acceptance then, and withdraw refuses an accepted transfer:
    # no new item asks anyone to withdraw it.
    assert inbox(rig, "producer") == [] and inbox(rig, "helper") == []
    assert error(send(rig, child, "withdraw_collaboration",
                      {"contract_id": contract, "reason": "Try"})) == "domain_conflict"


def test_an_old_child_transfer_is_withdrawn_by_the_current_holder(tmp_path, monkeypatch):
    rig = light(tmp_path)
    child, contract = old_child_transfer(rig, monkeypatch)
    own_rollover(rig, actor="third", session="third-session")
    # The requester no longer holds the WI; the holder now, who can withdraw, is asked.
    assert inbox(rig, "producer") == []
    [owed] = inbox(rig, "third")
    assert (owed["role"], owed["contract_id"], owed["options"], owed["offered_by"]) == (
        "requester", contract, ["withdraw"], "producer")
    # Held by the recipient itself (this inbox read names no surface).
    assert owed["target"] == {"work_item_id": child, "holder": "recipient_elsewhere"}
    original(send(rig, child, "withdraw_collaboration",
                  {"contract_id": contract, "reason": "A child cannot be transferred"}))
    assert inbox(rig, "third") == []
