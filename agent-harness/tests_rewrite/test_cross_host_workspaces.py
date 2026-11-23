"""An active WI's holder releases its own workspace on another host; the host that holds it removes it."""

from dataclasses import replace
from pathlib import Path

from harness_rewrite.effect_service import MaintenanceAdapter
from harness_rewrite.effects import Effects
from harness_rewrite.pi_dispatch import PiDispatch
from harness_rewrite.workspaces import on_host
import pytest
from tests_rewrite.test_workflow import error, original
from tests_rewrite.test_workspaces import allocate, git, rows, setup  # noqa: F401 (fixture)

HOST_A, HOST_B = "local", "ownermac"  # BASE sessions run on surface "local".


def drain_on(setup, surface, tmp_path):
    rig, _, _, worker = setup
    adapter = MaintenanceAdapter({"surface": surface, "delivery_root": str(tmp_path / "delivery")})
    return Effects(rig.kernel, adapter, worker).drain(rig.wi)


def move_holder(rig, surface):
    """The holder's Agent continues the WI from a session on another host."""
    here = replace(rig.contexts["owner"], session=surface + "-session", surface=surface)
    original(rig.approve("continue_here", {"reason": "continue on " + surface}, actor=here))
    rig.contexts["owner"] = replace(here, generation=rig.state()["work_item"]["generation"])


def retire(rig, ids):
    return rig.approve("retire_resources", {"resource_ids": ids, "reason": "Release old host"},
                       actor=rig.contexts["owner"])


def test_an_active_holder_releases_another_host_s_workspace_and_allocates_here(setup, tmp_path):
    rig, bindings, _, _ = setup
    original(allocate(setup))
    drain_on(setup, HOST_A, tmp_path)
    old = rows(rig)[0]
    assert old["surface"] == HOST_A and Path(old["path"]).is_dir()
    move_holder(rig, HOST_B)
    original(retire(rig, [old["resource_id"]]))
    # The scope stays with the old workspace until its host has removed it.
    assert error(allocate(setup)) == "domain_conflict"
    drain_on(setup, HOST_B, tmp_path)  # Not this host's workspace: nothing runs.
    assert rows(rig)[0]["status"] == "requested" and Path(old["path"]).is_dir()
    drain_on(setup, HOST_A, tmp_path)
    assert rows(rig)[0]["status"] == "retired" and not Path(old["path"]).exists()
    assert git(Path(bindings[0].source), "for-each-ref", "refs/heads/harness") == ""
    # Released: the same scope is allocated on this host, and runs here.
    original(allocate(setup))
    new = [r for r in rows(rig) if r["resource_id"] != old["resource_id"]][0]
    assert new["surface"] == HOST_B
    drain_on(setup, HOST_A, tmp_path)
    assert [r for r in rows(rig) if r["resource_id"] == new["resource_id"]][0]["status"] == "requested"
    drain_on(setup, HOST_B, tmp_path)
    assert Path([r for r in rows(rig) if r["resource_id"] == new["resource_id"]][0]["path"]).is_dir()


def test_a_running_task_or_an_unsettled_effect_keeps_the_workspace(setup, tmp_path):
    rig = setup[0]
    original(allocate(setup))
    resource = rows(rig)[0]["resource_id"]
    # The allocation has not been observed yet: its intention is unsettled.
    assert error(retire(rig, [resource])) == "domain_conflict"
    drain_on(setup, HOST_A, tmp_path)
    original(rig.claim("work"))  # A task on repo/work runs inside the workspace scope.
    refused = retire(rig, [resource])
    assert error(refused) == "domain_conflict" and "running task" in refused["error"]["reason"]


def test_an_unsettled_revision_keeps_the_workspace(setup, tmp_path):
    rig = setup[0]
    original(allocate(setup))
    drain_on(setup, HOST_A, tmp_path)
    resource = rows(rig)[0]["resource_id"]
    original(rig.send("revise_workspace", {"resource_id": resource, "resources": ["repo"],
                                           "sparse": ["a"], "purpose": "narrow"}))
    assert error(retire(rig, [resource])) == "domain_conflict"


def test_only_the_current_holder_retires_an_active_wi_s_workspace(setup, tmp_path):
    rig = setup[0]
    original(allocate(setup))
    drain_on(setup, HOST_A, tmp_path)
    resource = rows(rig)[0]["resource_id"]
    stale = rig.contexts["owner"]
    move_holder(rig, HOST_B)
    refused = rig.approve("retire_resources", {"resource_ids": [resource], "reason": "x"}, actor=stale)
    assert error(refused) == "authority_changed"  # The former holder's session.
    stranger = replace(rig.contexts["owner"], actor="stranger", session="stranger-session")
    refused = rig.approve("retire_resources", {"resource_ids": [resource], "reason": "x"},
                          actor=stranger)
    assert error(refused) == "authority_denied"
    # Without the human's decision for this exact inventory it is refused too.
    assert error(rig.send("retire_resources", {"resource_ids": [resource], "reason": "x"})) == (
        "source_invalid")


def test_a_pending_retirement_survives_a_handover_and_cannot_be_cancelled(setup, tmp_path):
    rig = setup[0]
    original(allocate(setup))
    drain_on(setup, HOST_A, tmp_path)
    old = rows(rig)[0]
    effect = original(retire(rig, [old["resource_id"]]))["effect_ids"][0]
    cancelled = rig.approve("cancel_effect", {"effect_id": effect, "reason": "changed my mind"},
                            actor=rig.contexts["owner"])
    assert error(cancelled) == "domain_conflict"
    move_holder(rig, HOST_B)
    drain_on(setup, HOST_A, tmp_path)
    assert rows(rig)[0]["status"] == "retired" and not Path(old["path"]).exists()


def test_dirty_work_on_the_old_host_is_preserved(setup, tmp_path):
    rig = setup[0]
    original(allocate(setup))
    drain_on(setup, HOST_A, tmp_path)
    old = rows(rig)[0]
    (Path(old["path"]) / "a/result").write_text("unsaved")
    original(retire(rig, [old["resource_id"]]))
    drain_on(setup, HOST_A, tmp_path)
    assert (Path(old["path"]) / "a/result").read_text() == "unsaved"
    assert rows(rig)[0]["status"] != "retired"
    # The scope is not released while the retirement has not succeeded.
    assert error(allocate(setup)) == "domain_conflict"


def test_a_workspace_runs_only_on_its_proven_host(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    binding = {"source": str(repo), "common_dir": str(repo / ".git")}
    effect = {"effect_kind": "workspace_retire", "host": HOST_A, "snapshot": {"binding": binding}}
    assert on_host(effect, HOST_A) and not on_host(effect, HOST_B)
    # No proven host: it runs nowhere, even where its repository exists.
    assert not on_host({**effect, "host": None}, HOST_A)
    # Only an installation without a surface falls back to its repository.
    assert on_host({**effect, "host": None}, None)
    service = MaintenanceAdapter({"surface": HOST_B, "delivery_root": str(tmp_path)})
    assert not service.local(effect)
    assert service.local({**effect, "effect_kind": "deliver_artifact"})
    dispatch = PiDispatch.__new__(PiDispatch)
    dispatch.policy = {"surface": HOST_B}
    assert not dispatch.local(effect)
    dispatch.policy = {"surface": HOST_A}
    assert dispatch.local(effect)


def writable(rig):
    """The workspaces the kernel opens to the holder's session for writing (bindings view)."""
    from harness_rewrite import Query

    response = rig.kernel.inspect(Query(view="bindings"), "owner")
    return [row["resource_id"] for row in response["result"]["workspace_access"]]


def stop(rig):
    original(rig.approve("stop", {"reason": "Pause the work"}, actor=rig.contexts["owner"]))


def reopen(rig):
    return rig.approve("reopen_work", {"reason": "Resume", "nodes": ["work"]}, actor=rig.contexts["owner"])


def test_an_approved_retirement_runs_after_the_wi_is_stopped_and_reopen_follows(setup, tmp_path):
    rig = setup[0]
    original(allocate(setup))
    drain_on(setup, HOST_A, tmp_path)
    old = rows(rig)[0]
    original(retire(rig, [old["resource_id"]]))
    stop(rig)
    assert rig.state()["work_item"]["business_progress"] == "stopped"
    # Before the retirement settles, renewed execution waits for it.
    refused = reopen(rig)
    assert error(refused) == "domain_conflict" and "retirement" in refused["error"]["reason"]
    drain_on(setup, HOST_A, tmp_path)
    assert rows(rig)[0]["status"] == "retired" and not Path(old["path"]).exists()
    original(reopen(rig))
    assert rig.state()["work_item"]["business_progress"] == "active"


def test_a_retirement_held_back_by_dirt_while_stopped_settles_once_cleared(setup, tmp_path):
    rig = setup[0]
    original(allocate(setup))
    drain_on(setup, HOST_A, tmp_path)
    old = rows(rig)[0]
    dirt = Path(old["path"]) / "a/result"
    dirt.write_text("unsaved")
    original(retire(rig, [old["resource_id"]]))
    stop(rig)
    drain_on(setup, HOST_A, tmp_path)
    # Not retired: the dirty file is kept, the effect stays retryable, reopen waits.
    assert rows(rig)[0]["status"] == "unknown" and dirt.read_text() == "unsaved"
    assert error(reopen(rig)) == "domain_conflict"
    dirt.write_text("first")  # The owner clears it.
    # The held-back attempt never acted, so readback proves its absence; the next
    # round retires the workspace, still while the WI is stopped.
    drain_on(setup, HOST_A, tmp_path)
    assert rows(rig)[0]["status"] == "observed_absence"
    drain_on(setup, HOST_A, tmp_path)
    assert rows(rig)[0]["status"] == "retired"
    original(reopen(rig))


def without_surface(monkeypatch):
    """Record new workspaces as before surfaces were kept (no `surface` field)."""
    from harness_rewrite import workspaces

    original_event = workspaces.event

    def event(kind, **payload):
        if kind == "WorkspaceRequested":
            payload = {**payload, "resource": {
                k: v for k, v in payload["resource"].items() if k != "surface"}}
        return original_event(kind, **payload)
    monkeypatch.setattr(workspaces, "event", event)


def observe_as(setup, surface, tmp_path, host_surface, fault=None):
    """Drain on `host_surface`, its observations committed by a worker of `surface`."""
    rig, _, _, worker = setup

    def on(artifacts):
        handle = worker(artifacts)
        rig.contexts[handle] = replace(rig.contexts[handle], surface=surface)
        return handle
    adapter = MaintenanceAdapter({"surface": host_surface, "delivery_root": str(tmp_path / "d")})

    def stage(name):
        if name == fault:
            raise RuntimeError(name)
    return Effects(rig.kernel, adapter, on, fault=stage).drain(rig.wi)


def test_a_workspace_recorded_without_surface_runs_on_its_journal_proven_host(
        setup, tmp_path, monkeypatch):
    rig = setup[0]
    without_surface(monkeypatch)
    original(allocate(setup))
    resource = rows(rig)[0]
    assert "surface" not in resource
    # A request proves no place: only the allocation's committed success does.
    assert {e["host"] for e in rig.state()["effects"]} == {None}
    observe_as(setup, HOST_A, tmp_path, None)
    assert {e["host"] for e in rig.state()["effects"]} == {HOST_A}
    original(retire(rig, [resource["resource_id"]]))
    # Host B has the same repository at the same path and remote: it must not act,
    # least of all settle the retirement as already absent.
    drain_on(setup, HOST_B, tmp_path)
    assert rows(rig)[0]["status"] == "requested" and Path(resource["path"]).is_dir()
    drain_on(setup, HOST_A, tmp_path)
    assert rows(rig)[0]["status"] == "retired"


def test_an_active_wi_cannot_retire_or_revise_a_workspace_of_unproven_host(setup, tmp_path, monkeypatch):
    rig = setup[0]
    without_surface(monkeypatch)
    original(allocate(setup))
    # The allocation ran on host A but broke off before its receipt; host B read it back as done.
    with pytest.raises(RuntimeError):
        observe_as(setup, HOST_A, tmp_path, None, fault="after_send")
    observe_as(setup, HOST_B, tmp_path, None)
    resource = rows(rig)[0]
    assert Path(resource["path"]).is_dir()
    assert {e["host"] for e in rig.state()["effects"]} == {None}
    # Nothing could ever run it: refused before it is recorded, and nothing changes.
    before = rig.state()["work_item"]["local_revision"]
    refused = retire(rig, [resource["resource_id"]])
    assert error(refused) == "domain_conflict" and "not proven" in refused["error"]["reason"]
    refused = rig.send("revise_workspace", {"resource_id": resource["resource_id"],
                                            "resources": ["repo"], "sparse": ["a"], "purpose": "narrow"})
    assert error(refused) == "domain_conflict" and "not proven" in refused["error"]["reason"]
    assert rig.state()["work_item"]["local_revision"] == before
    assert not [e for e in rig.state()["effects"] if e["effect_kind"] == "workspace_retire"]
    # The workspace stays in ordinary use: its scope is claimed and written as before.
    assert writable(rig) == [resource["resource_id"]]
    original(rig.claim("work"))
    assert error(allocate(setup)) == "domain_conflict"  # Its scope stays owned.


def test_a_retirement_refused_for_unique_commits_while_stopped_can_be_requested_again(setup, tmp_path):
    rig = setup[0]
    original(allocate(setup))
    drain_on(setup, HOST_A, tmp_path)
    old = rows(rig)[0]
    path = Path(old["path"])
    (path / "a/result").write_text("unique work")
    git(path, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qam", "unique")
    original(retire(rig, [old["resource_id"]]))
    stop(rig)
    drain_on(setup, HOST_A, tmp_path)
    # A failed retirement keeps the commit and the workspace, and it is final: later
    # drains neither read it back nor start it again, so the state does not move.
    assert rows(rig)[0]["status"] == "observed_failure" and path.is_dir()
    assert git(path, "log", "-1", "--format=%s") == "unique"
    before = rig.state()["work_item"]["local_revision"]
    for _ in range(2):
        drain_on(setup, HOST_A, tmp_path)
    assert rows(rig)[0]["status"] == "observed_failure"
    assert rig.state()["work_item"]["local_revision"] == before
    # It is settled: the WI reopens; only a new approved request retires the workspace.
    original(reopen(rig))
    rig.contexts["owner"] = replace(rig.contexts["owner"],
                                    generation=rig.state()["work_item"]["generation"])
    # A final failure is evidence, not a pending retirement: the kept workspace is in
    # ordinary use again. Its task is claimed, its bindings open, its scope stays owned.
    assert writable(rig) == [old["resource_id"]]
    attempt = original(rig.claim("work"))["attempt_id"]
    (path / "a/result").write_text("more work after reopening")
    git(path, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qam", "more")
    assert error(allocate(setup)) == "domain_conflict"
    # The task ends here (it would otherwise keep the workspace occupied).
    original(rig.send("abandon_task", {"attempt_id": attempt, "reason": "handled the commits"}))
    git(path, "reset", "-q", "--hard", "HEAD~2")
    failed = rows(rig)[0]["retirement"]
    assert original(retire(rig, [old["resource_id"]]))["effect_ids"] != [failed]
    drain_on(setup, HOST_A, tmp_path)
    assert rows(rig)[0]["status"] == "retired" and not path.exists()


def test_a_retained_allocation_revised_from_another_host_has_no_proven_host():
    """A historical allocation has no allocation effect; a revision request and its
    success elsewhere never prove where the retained workspace lives."""
    from harness_rewrite.domain import reduce
    from harness_rewrite.workspaces import host_of, recorded_hosts, with_hosts
    from tests_rewrite.test_historical_replay import JOURNALS

    journal = JOURNALS["existing_execution"]
    state = None
    history = []
    for events in journal["history"]:
        state = reduce(state, events)
        history.append({"semantics": {"principal": {"surface": "surface_v1_cloud_linux_a"}},
                        "events": events})
    (resource,) = state["workspaces"].values()
    revise = "op_revise.0"
    history.append({"semantics": {"principal": {"surface": "surface_v1_local_macos"},
                                  "command": {"intent": "revise_workspace", "input": {}}},
                    "events": [
                        {"kind": "WorkspaceRequested", "payload": {"resource": {**resource, "effect_id": revise}}},
                        {"kind": "EffectRequested", "payload": {"effect_id": revise, "effect_kind": "workspace_revise",
                                                                "snapshot": {"resource_id": resource["resource_id"]}}}]})
    history.append({"semantics": {"principal": {"surface": "surface_v1_local_macos"}},
                    "events": [{"kind": "EffectAttemptStarted", "payload": {"effect_id": revise}},
                               {"kind": "EffectObserved", "payload": {"effect_id": revise,
                                                                     "observation": {"outcome": "success"}}}]})
    assert recorded_hosts(history) == {}
    assert host_of(with_hosts(state, history), resource["resource_id"]) is None


def allocation(effect="op_a.0", resource="workspace.op_a.0"):
    return {"semantics": {"principal": {"surface": HOST_A}}, "events": [
        {"kind": "EffectRequested", "payload": {"effect_id": effect, "effect_kind": "workspace_allocate",
                                                "snapshot": {"resource_id": resource}}}]}


def record(surface, *events):
    return {"semantics": {"principal": {"surface": surface}}, "events": list(events)}


def test_hosts_come_from_committed_allocation_events_only():
    from harness_rewrite.workspaces import recorded_hosts

    started = {"kind": "EffectAttemptStarted", "payload": {"effect_id": "op_a.0"}}
    success = {"kind": "EffectObserved", "payload": {"effect_id": "op_a.0", "observation": {"outcome": "success"}}}
    unknown = {"kind": "EffectObserved", "payload": {"effect_id": "op_a.0", "observation": {"outcome": "unknown"}}}
    assert recorded_hosts([allocation()]) == {}  # Requested, never observed.
    assert recorded_hosts([allocation(), record(HOST_A, started, success)]) == {"workspace.op_a.0": HOST_A}
    # A duplicate success receipt from another host commits no event, so the proof
    # holds; a success commit's own surface must also have started every attempt.
    assert recorded_hosts([allocation(), record(HOST_A, started), record(HOST_B, unknown),
                           record(HOST_B, success)]) == {"workspace.op_a.0": None}
    assert recorded_hosts([allocation(), record(HOST_A, started), record(HOST_B, started),
                           record(HOST_A, success)]) == {"workspace.op_a.0": None}
    # A command naming a success that the kernel did not commit proves nothing.
    phantom = {"semantics": {"principal": {"surface": HOST_B}, "command": {
        "intent": "observe_effect", "input": {"effect_id": "op_a.0", "outcome": "success"}}}, "events": []}
    assert recorded_hosts([allocation(), record(HOST_A, started, success), phantom]) == {
        "workspace.op_a.0": HOST_A}


def test_a_duplicate_success_from_another_host_keeps_the_proven_host(setup, tmp_path, monkeypatch):
    """The kernel commits nothing for a second success receipt; the inspected host stays."""
    from harness_rewrite import Command

    rig, _, _, worker = setup
    without_surface(monkeypatch)
    original(allocate(setup))
    observe_as(setup, HOST_A, tmp_path, None)
    (effect,) = rig.state()["effects"]
    assert effect["host"] == HOST_A
    from harness_rewrite.effects import ArtifactInput
    from harness_rewrite.model import digest, encoded
    observation = {"effect_id": effect["effect_id"], "attempt_id": effect["attempts"][-1]["id"],
                   "outcome": "success", "details": {"reason": "physical_workspace_observed"}}
    proof = ArtifactInput("observations/" + effect["effect_id"], digest(observation), encoded(observation))
    handle = worker((proof,))
    rig.contexts[handle] = replace(rig.contexts[handle], surface=HOST_B)
    response = rig.kernel.execute(Command("observe.duplicate", "observe_effect",
                                          {**observation, "evidence": [proof.reference()]}, rig.wi), handle)
    assert response["status"] == "local_committed"
    # The write response and a later read agree: the same proven host.
    assert {e["host"] for e in response["effects"]} == {HOST_A}
    assert {e["host"] for e in rig.state()["effects"]} == {HOST_A}
