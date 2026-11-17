"""Actual Git resources, preserved user state and original-attempt recovery."""

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from harness_rewrite import Query
from harness_rewrite.domain import INTENTS
from harness_rewrite.delivery import WORKER_INTENTS
from harness_rewrite.effects import Effects
from harness_rewrite.git_workspaces import GitWorkspaces
from harness_rewrite.model import RepositoryBinding, encoded
from harness_rewrite.workspaces import EFFECT_KINDS, session_write, writable
from tests_rewrite.test_historical_replay import JOURNALS
from tests_rewrite.test_workflow import Rig, BASE, graph, original, error


def git(path, *args):
    return (
        subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.PIPE)
        .decode()
        .strip()
    )


@pytest.fixture
def setup(tmp_path):
    rig = Rig(tmp_path)
    bindings = []
    commits = {}
    for name in ("one", "two"):
        source = tmp_path / name
        source.mkdir()
        git(source, "init", "-q")
        git(source, "config", "user.name", "Resource Test")
        git(source, "config", "user.email", "resource@example.invalid")
        (source / "a").mkdir()
        (source / "b").mkdir()
        (source / "a/result").write_text("first")
        (source / "b/result").write_text("second")
        (source / ".gitignore").write_text("ignored\n")
        git(source, "add", ".")
        git(source, "commit", "-qm", "base")
        commits[name] = git(source, "rev-parse", "HEAD")
        root = tmp_path / (name + "-workspaces")
        root.mkdir()
        bindings.append(
            RepositoryBinding(name, str(source), str(source / ".git"), str(root))
        )
        (source / "human-draft").write_text("Preserve unowned shared changes")
    rig.contexts["owner"] = replace(
        BASE, allowed_intents=INTENTS, repositories=tuple(bindings)
    )
    rig.adopt(graph(rig))

    def worker(artifacts):
        rig.contexts["worker"] = replace(
            BASE,
            actor="resource-worker",
            session="service",
            allowed_intents=WORKER_INTENTS,
            effect_kinds=EFFECT_KINDS,
            artifacts=artifacts,
        )
        return "worker"

    return rig, bindings, commits, worker


def allocate(setup, names=("one",), sparse=None, request=None):
    rig, _, commits, _ = setup
    return rig.send(
        "allocate_workspaces",
        {
            "workspaces": [
                {
                    "repo_id": name,
                    "base_commit": commits[name],
                    "resources": ["repo"],
                    "sparse": sparse or [],
                    "purpose": "Develop approved result",
                }
                for name in names
            ]
        },
        request=request,
    )


def rows(rig):
    return rig.state()["result"]["current_observation"]["workspace_settlement"][
        "resources"
    ]


def drain(setup, fault=None):
    rig, _, _, worker = setup
    return Effects(rig.kernel, GitWorkspaces(fault=fault), worker).drain(rig.wi)


def retire(setup):
    rig = setup[0]
    original(
        rig.approve(
            "terminate_work",
            {"kind": "cancelled", "reason": "Dispose isolated experiment"},
            actor=rig.contexts["owner"],
        )
    )
    return rig.approve(
        "retire_resources",
        {
            "resource_ids": [r["resource_id"] for r in rows(rig)],
            "reason": "Exact isolated inventory approved",
        },
        actor=rig.contexts["owner"],
    )


def test_real_multirepo_allocation_and_retirement_preserve_shared_dirt(setup):
    rig, bindings, _, _ = setup
    first = original(allocate(setup, ("one", "two"), request="allocate"))
    assert original(allocate(setup, ("one", "two"), request="allocate")) == first
    assert all(not Path(r["path"]).exists() for r in rows(rig))
    assert all(r["status"] == "local_committed" for r in drain(setup))
    assert len(rows(rig)) == 2
    assert all((Path(r["path"]) / "a/result").read_text() == "first" for r in rows(rig))
    for binding in bindings:
        assert (
            Path(binding.source) / "human-draft"
        ).read_text() == "Preserve unowned shared changes"
    paths = [Path(r["path"]) for r in rows(rig)]
    original(retire(setup))
    drain(setup)
    assert all(not path.exists() for path in paths)
    assert all(r["status"] == "retired" for r in rows(rig))
    for binding in bindings:
        assert git(Path(binding.source), "for-each-ref", "refs/heads/harness") == ""
        assert (
            len(
                git(Path(binding.source), "worktree", "list", "--porcelain").split(
                    "worktree "
                )
            )
            == 2
        )
        assert (Path(binding.source) / "human-draft").exists()
    assert rig.state()["result"]["current_observation"]["workspace_settlement"][
        "resources_retired"
    ]


def test_sparse_expansion_shrink_and_original_binding(setup):
    rig = setup[0]
    original(allocate(setup, sparse=["a"]))
    drain(setup)
    resource = rows(rig)[0]
    target = Path(resource["path"])
    assert (target / "a/result").exists() and not (target / "b/result").exists()
    for sparse in (["a", "b"], ["b"], []):
        original(
            rig.send(
                "revise_workspace",
                {
                    "resource_id": resource["resource_id"],
                    "resources": ["repo"],
                    "sparse": sparse,
                    "purpose": "Approved scope revision",
                },
            )
        )
        drain(setup)
        assert rows(rig)[0]["status"] == "observed_success"
        assert (target / "a/result").exists() == (not sparse or "a" in sparse)
        assert (target / "b/result").exists() == (not sparse or "b" in sparse)
    assert rows(rig)[0]["allocation"] == resource["allocation"]


@pytest.mark.parametrize("fault_stage", ["after_worktree_add", "after_sparse_checkout"])
def test_allocation_crash_resumes_original_owned_resource(setup, fault_stage):
    rig = setup[0]
    original(allocate(setup, sparse=["a"]))

    class Crash(BaseException):
        pass

    def fault(stage):
        if stage == fault_stage:
            raise Crash()

    with pytest.raises(Crash):
        drain(setup, fault)
    assert rig.state()["effects"][0]["status"] == "attempt_started"
    drain(setup)
    assert rows(rig)[0]["status"] == "observed_success"
    assert (Path(rows(rig)[0]["path"]) / "a/result").read_text() == "first"
    assert len(rig.state()["effects"][0]["attempts"]) == 1


@pytest.mark.parametrize(
    "fault_stage", ["after_worktree_remove", "after_branch_remove"]
)
def test_retirement_crash_finishes_same_attempt_without_dropping_proof(
    setup, fault_stage
):
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    original(retire(setup))

    class Crash(BaseException):
        pass

    def fault(stage):
        if stage == fault_stage:
            raise Crash()

    with pytest.raises(Crash):
        drain(setup, fault)
    drain(setup)
    assert rows(rig)[0]["status"] == "retired"
    effect = [
        e for e in rig.state()["effects"] if e["effect_kind"] == "workspace_retire"
    ][0]
    assert len(effect["attempts"]) == 1
    assert effect["observations"][-1]["details"]["registration_count"] == 0


@pytest.mark.parametrize(
    "state", ["dirty", "untracked", "ignored", "unique_commit", "locked"]
)
def test_retirement_preserves_every_non_disposable_result(setup, state):
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    target = Path(resource["path"])
    if state == "dirty":
        (target / "a/result").write_text("uncommitted result")
    elif state in {"untracked", "ignored"}:
        (target / state).write_text("unique data")
    elif state == "unique_commit":
        (target / "a/result").write_text("unique committed result")
        git(target, "add", "a/result")
        git(target, "commit", "-qm", "unique result")
    else:
        git(Path(setup[1][0].source), "worktree", "lock", str(target))
    original(retire(setup))
    drain(setup)
    assert target.exists()
    assert rows(rig)[0]["status"] != "retired"
    assert not rig.state()["result"]["current_observation"]["workspace_settlement"][
        "resources_retired"
    ]
    if state in {"dirty", "untracked", "ignored"}:
        effect = [
            e for e in rig.state()["effects"] if e["effect_kind"] == "workspace_retire"
        ][0]
        refused = [o["details"] for o in effect["observations"] if "cause" in o["details"]]
        assert refused[0]["cause"] == "preserved_changes_or_ignored_files"
        assert any(path.endswith("a/result" if state == "dirty" else state)
                   for path in refused[0]["paths"])


@pytest.mark.parametrize("pushed", [True, False])
def test_retirement_accepts_commits_the_bound_remote_already_holds(tmp_path, pushed):
    rig = Rig(tmp_path)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    source = tmp_path / "one"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Resource Test")
    git(source, "config", "user.email", "resource@example.invalid")
    (source / "a").mkdir()
    (source / "a/result").write_text("first")
    git(source, "add", ".")
    git(source, "commit", "-qm", "base")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "-q", "origin", "main")
    git(source, "fetch", "-q", "origin")
    root = tmp_path / "one-workspaces"
    root.mkdir()
    binding = RepositoryBinding("one", str(source), str(source / ".git"), str(root), str(remote))
    rig.contexts["owner"] = replace(BASE, allowed_intents=INTENTS, repositories=(binding,))
    rig.adopt(graph(rig))

    def worker(artifacts):
        rig.contexts["worker"] = replace(BASE, actor="resource-worker", session="service",
            allowed_intents=WORKER_INTENTS, effect_kinds=EFFECT_KINDS, artifacts=artifacts)
        return "worker"

    setup = (rig, [binding], {"one": git(source, "rev-parse", "HEAD")}, worker)
    original(allocate(setup))
    drain(setup)
    target = Path(rows(rig)[0]["path"])
    (target / "a/result").write_text("published result")
    git(target, "add", "a/result")
    git(target, "commit", "-qm", "published result")
    if pushed:
        git(target, "push", "-q", "origin", "HEAD:main")
        git(source, "fetch", "-q", "origin")
    original(retire(setup))
    drain(setup)
    assert target.exists() is not pushed
    assert (rows(rig)[0]["status"] == "retired") is pushed


def test_existing_unowned_path_and_redirected_git_identity_never_adopted(setup):
    rig = setup[0]
    original(allocate(setup))
    target = Path(rows(rig)[0]["path"])
    target.mkdir(parents=True)
    (target / "human").write_text("keep")
    drain(setup)
    assert (target / "human").read_text() == "keep"
    assert rows(rig)[0]["status"] == "observed_failure"
    assert git(Path(setup[1][0].source), "for-each-ref", "refs/heads/harness") == ""


def test_authority_overlap_and_exact_human_inventory_required(setup):
    rig = setup[0]
    original(allocate(setup))
    assert error(allocate(setup)) == "domain_conflict"
    drain(setup)
    inventory = {
        "resource_ids": [r["resource_id"] for r in rows(rig)],
        "reason": "cleanup",
    }
    assert error(rig.send("retire_resources", inventory)) == "source_invalid"
    assert (
        error(rig.approve("retire_resources", inventory, actor=rig.contexts["owner"]))
        == "authority_denied"
    )
    bad = {
        "workspaces": [
            {
                "repo_id": "elsewhere",
                "base_commit": setup[2]["one"],
                "resources": ["repo"],
                "sparse": [],
                "purpose": "outside",
            }
        ]
    }
    assert error(rig.send("allocate_workspaces", bad)) == "authority_denied"


def test_sparse_change_preserves_dirt_outside_new_scope(setup):
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    target = Path(resource["path"])
    (target / "b/result").write_text("unique work outside desired sparse scope")
    original(
        rig.send(
            "revise_workspace",
            {
                "resource_id": resource["resource_id"],
                "resources": ["repo"],
                "sparse": ["a"],
                "purpose": "shrink",
            },
        )
    )
    drain(setup)
    assert rows(rig)[0]["status"] == "unknown"
    assert (
        target / "b/result"
    ).read_text() == "unique work outside desired sparse scope"


def test_moved_managed_root_keeps_earlier_workspaces_where_they_were(setup, tmp_path):
    rig, bindings, commits, _ = setup
    original(allocate(setup))
    drain(setup)
    earlier = rows(rig)[0]
    moved = tmp_path / "internal-workspaces"
    moved.mkdir()
    admitted = tuple(replace(b, managed_root=str(moved)) for b in bindings)
    for name, context in list(rig.contexts.items()):
        if context.repositories:
            rig.contexts[name] = replace(context, repositories=admitted)
    original(
        rig.send(
            "revise_workspace",
            {
                "resource_id": earlier["resource_id"],
                "resources": ["repo"],
                "sparse": ["a"],
                "purpose": "narrow after the root moved",
            },
        )
    )
    drain(setup)
    revised = next(r for r in rows(rig) if r["resource_id"] == earlier["resource_id"])
    assert revised["path"] == earlier["path"] and Path(earlier["path"], "a/result").exists()
    original(allocate(setup, ("two",)))
    drain(setup)
    assert any(r["path"].startswith(str(moved)) for r in rows(rig))


def test_pending_resources_do_not_grant_execution_and_retirement_blocks_reopen(setup):
    rig = setup[0]
    original(allocate(setup))
    assert error(rig.claim("work")) == "authority_denied"
    drain(setup)
    original(retire(setup))
    data = {"reason": "Start a new round", "nodes": ["work"]}
    assert (
        error(rig.approve("reopen_work", data, actor=rig.contexts["owner"]))
        == "domain_conflict"
    )
    drain(setup)
    original(rig.approve("reopen_work", data, actor=rig.contexts["owner"]))


def test_unsettled_workspace_blocks_work_but_not_checkpoints(setup):
    rig = setup[0]
    original(allocate(setup))
    progress = {"summary": "Where the work stands", "resources": ["repo"]}
    assert error(rig.claim("work")) == "authority_denied"
    original(rig.send("checkpoint", progress))
    drain(setup)
    original(rig.claim("work"))


def test_authority_change_after_failed_allocation_cannot_resume_old_writer(setup):
    rig = setup[0]
    original(allocate(setup))

    class Crash(BaseException):
        pass

    def fault(stage):
        if stage == "after_worktree_add":
            raise Crash()

    with pytest.raises(Crash):
        drain(setup, fault)
    original(
        rig.approve(
            "assume_control",
            {"reason": "Explicit replacement authority"},
            actor=rig.contexts["owner"],
        )
    )
    results = drain(setup)
    # The obsolete writer is only read back: `worktree add` may have run, so it
    # stays unknown and is never resumed or cancelled.
    assert all(r["status"] == "local_committed" for r in results)
    assert not (Path(rows(rig)[0]["path"]) / "a/result").exists()
    assert rows(rig)[0]["status"] == "unknown"
    effect = rig.state()["effects"][0]
    assert effect["observations"][-1]["details"]["reason"] == "obsolete_action_incomplete"
    revision = rig.state()["work_item"]["local_revision"]
    drain(setup)  # The same read-only probe is a replay, not a new fact.
    assert rig.state()["work_item"]["local_revision"] == revision
    assert len(rig.state()["effects"][0]["attempts"]) == 1


@pytest.mark.parametrize("redirect", ["target", "source"])
def test_real_path_redirection_cannot_supply_resource_identity(setup, redirect):
    rig, bindings, _, _ = setup
    original(allocate(setup))
    target = Path(rows(rig)[0]["path"])
    if redirect == "target":
        target.parent.mkdir(parents=True)
        target.symlink_to(Path(bindings[1].source), target_is_directory=True)
    else:
        source = Path(bindings[0].source)
        saved = source.with_name("preserved-source")
        source.rename(saved)
        source.symlink_to(Path(bindings[1].source), target_is_directory=True)
    drain(setup)
    assert rows(rig)[0]["status"] == "unknown"
    assert (
        Path(bindings[1].source) / "human-draft"
    ).read_text() == "Preserve unowned shared changes"


def test_scope_shrink_cannot_remove_an_occupied_task_scope(setup):
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    original(rig.claim("work"))
    assert (
        error(
            rig.send(
                "revise_workspace",
                {
                    "resource_id": resource["resource_id"],
                    "resources": ["repo/other"],
                    "sparse": ["b"],
                    "purpose": "shrink",
                },
            )
        )
        == "domain_conflict"
    )


@pytest.mark.parametrize("left", ["nothing", "nothing_and_no_root", "branch", "registration"])
def test_retirement_of_a_workspace_already_gone_needs_no_ownership_proof(setup, left):
    # Removed outside the core, its ownership markers went with it. With no
    # path, registration or branch left in the bound repository there is
    # nothing to protect; anything left still needs the original proof.
    import shutil
    rig, bindings, _, _ = setup
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    source, root = Path(bindings[0].source), Path(bindings[0].managed_root)
    branch = resource["branch"]
    original(retire(setup))
    if left == "registration":
        shutil.rmtree(resource["path"])
    else:
        git(source, "worktree", "remove", "--force", resource["path"])
    if left != "branch":
        git(source, "update-ref", "-d", "refs/heads/" + branch)
    shutil.rmtree(root / ".harness-resource-evidence")
    if left == "nothing_and_no_root":
        shutil.rmtree(root)
    drain(setup)
    effect = [e for e in rig.state()["effects"] if e["effect_kind"] == "workspace_retire"][0]
    if left.startswith("nothing"):
        assert rows(rig)[0]["status"] == "retired"
        assert effect["observations"][-1]["details"]["reason"] == "physical_resources_already_absent"
    else:
        assert rows(rig)[0]["status"] != "retired"


@pytest.mark.parametrize("branch", ["base", "unpublished_commit"])
def test_retirement_finishes_a_worktree_removed_by_hand(setup, branch):
    # The worktree was removed outside the core but its ownership markers and
    # branch stayed. Nothing unsaved is left to protect, so retirement removes
    # the branch unless it holds a commit the remote does not.
    rig, bindings, _, _ = setup
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    source = Path(bindings[0].source)
    if branch == "unpublished_commit":
        target = Path(resource["path"])
        (target / "a/result").write_text("unpublished")
        git(target, "commit", "-qam", "unpublished")
    original(retire(setup))
    git(source, "worktree", "remove", "--force", resource["path"])
    drain(setup)
    effect = [e for e in rig.state()["effects"] if e["effect_kind"] == "workspace_retire"][0]
    refs = git(source, "for-each-ref", "refs/heads/" + resource["branch"])
    if branch == "base":
        assert rows(rig)[0]["status"] == "retired"
        assert effect["observations"][-1]["details"]["reason"] == "physical_resources_retired"
        assert refs == ""
    else:
        assert rows(rig)[0]["status"] != "retired"
        assert effect["observations"][-1]["details"]["reason"] == "unique_commits_preserved"
        assert refs != ""


def test_absent_retirement_names_retry_effect_as_next_action(setup):
    # A retirement that read back as never having acted waits for its owner;
    # the observation names the entry and the effect instead of a bare resume.
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    target = Path(rows(rig)[0]["path"])
    (target / "draft").write_text("dirty tree refuses the retirement")
    original(retire(setup))
    drain(setup)
    drain(setup)
    retirement = next(e for e in rig.state()["effects"] if e["effect_kind"] == "workspace_retire")
    assert retirement["status"] == "observed_absence"
    assert rig.state()["next_action"] == {
        "intent": "retry_effect",
        "reason": "physical_retirement_absent",
        "effect_id": retirement["effect_id"],
    }
    (target / "draft").unlink()
    rig.contexts["retry"] = replace(rig.contexts["owner"], allowed_intents=INTENTS | {"retry_effect"})
    original(rig.send("retry_effect", {"effect_id": retirement["effect_id"], "reason": "Draft removed"},
                      handle="retry"))
    drain(setup)
    assert rows(rig)[0]["status"] == "retired"
    assert rig.state()["next_action"]["intent"] == "inspect"


def test_ended_owner_cancels_proven_absent_revision_then_retires(setup):
    # A refused revision read back as absent (no action marker) blocks every
    # retirement of the ended WI until its owner cancels it with a human
    # decision; an uncertain effect or a retirement is never cancelled.
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    target = Path(resource["path"])
    (target / "draft").write_text("dirty tree refuses the revision")
    revise = original(
        rig.send(
            "revise_workspace",
            {
                "resource_id": resource["resource_id"],
                "resources": ["repo"],
                "sparse": ["a"],
                "purpose": "narrow",
            },
        )
    )["effect_ids"][0]

    def status():
        return next(e for e in rig.state()["effects"] if e["effect_id"] == revise)["status"]

    drain(setup)
    assert status() == "unknown"
    drain(setup)
    assert status() == "observed_absence"
    cancel = {"effect_id": revise, "reason": "Revision proven never applied"}
    original(
        rig.approve(
            "terminate_work",
            {"kind": "cancelled", "reason": "Dispose isolated experiment"},
            actor=rig.contexts["owner"],
        )
    )
    (target / "draft").unlink()
    inventory = {"resource_ids": [resource["resource_id"]], "reason": "Exact inventory"}
    assert error(rig.approve("retire_resources", inventory, actor=rig.contexts["owner"])) == (
        "domain_conflict"
    )
    assert error(rig.send("cancel_effect", cancel)) == "source_invalid"
    assert error(rig.approve("cancel_effect", cancel, actor=rig.contexts["helper"])) in {
        "authority_denied",
        "authority_changed",
    }
    original(rig.approve("cancel_effect", cancel, actor=rig.contexts["owner"]))
    assert status() == "cancelled"
    original(rig.approve("retire_resources", inventory, actor=rig.contexts["owner"]))
    retirement = next(
        e["effect_id"] for e in rig.state()["effects"] if e["effect_kind"] == "workspace_retire"
    )
    assert error(
        rig.approve(
            "cancel_effect",
            {"effect_id": retirement, "reason": "skip"},
            actor=rig.contexts["owner"],
        )
    ) == "domain_conflict"
    drain(setup)
    assert not target.exists()
    assert rows(rig)[0]["status"] == "retired"
    assert rig.state()["result"]["current_observation"]["workspace_settlement"][
        "resources_retired"
    ]


def test_ended_owner_cannot_cancel_an_uncertain_revision(setup):
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    (Path(resource["path"]) / "draft").write_text("dirty tree refuses the revision")
    revise = original(
        rig.send(
            "revise_workspace",
            {
                "resource_id": resource["resource_id"],
                "resources": ["repo"],
                "sparse": ["a"],
                "purpose": "narrow",
            },
        )
    )["effect_ids"][0]
    drain(setup)
    original(
        rig.approve(
            "terminate_work",
            {"kind": "cancelled", "reason": "Dispose isolated experiment"},
            actor=rig.contexts["owner"],
        )
    )
    cancel = {"effect_id": revise, "reason": "Outcome not read back"}
    assert error(rig.approve("cancel_effect", cancel, actor=rig.contexts["owner"])) == (
        "domain_conflict"
    )
    assert next(e for e in rig.state()["effects"] if e["effect_id"] == revise)["status"] == (
        "unknown"
    )


def take_over(rig):
    original(
        rig.approve(
            "assume_control",
            {"reason": "Explicit replacement authority"},
            actor=rig.contexts["owner"],
        )
    )
    # The holder continues under the new execution generation.
    rig.contexts["owner"] = replace(
        rig.contexts["owner"], generation=rig.state()["work_item"]["generation"]
    )


def effect_of(rig, effect_id):
    return next(e for e in rig.state()["effects"] if e["effect_id"] == effect_id)


def narrow(rig, resource):
    return original(
        rig.send(
            "revise_workspace",
            {
                "resource_id": resource["resource_id"],
                "resources": ["repo"],
                "sparse": ["a"],
                "purpose": "narrow",
            },
        )
    )["effect_ids"][0]


def test_obsolete_intention_never_sent_is_cancelled_without_action(setup):
    rig = setup[0]
    allocation = original(allocate(setup))["effect_ids"][0]
    take_over(rig)  # A new execution generation (for a child WI: its parent's).
    results = drain(setup)
    assert [r["result"]["original_request_result"].get("suppressed") for r in results] == [True]
    assert effect_of(rig, allocation)["status"] == "cancelled"
    assert rows(rig) == [], "a withdrawn allocation leaves no resource behind"
    assert drain(setup) == []
    original(allocate(setup))  # The same scope can be allocated again.
    drain(setup)
    assert rows(rig)[0]["status"] == "observed_success"


def test_obsolete_revision_proven_unapplied_is_cancelled_and_never_retried(setup):
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    (Path(resource["path"]) / "draft").write_text("dirty tree refuses the revision")
    revise = narrow(rig, resource)
    drain(setup)
    assert effect_of(rig, revise)["status"] == "unknown"
    take_over(rig)
    drain(setup)
    effect = effect_of(rig, revise)
    assert effect["status"] == "cancelled"
    assert effect["observations"][-1]["outcome"] == "absence"
    assert effect["observations"][-1]["details"]["reason"] == "no_original_action_marker"
    history = rig.state(query=Query(rig.wi, view="history"))["result"]["history"]
    assert history[-1]["event_kinds"] == [
        "EffectObserved", "EffectCancelled", "WorkspaceRequestWithdrawn"
    ]
    assert rows(rig)[0]["status"] == "observed_success", "the confirmed record is restored"
    revision = rig.state()["work_item"]["local_revision"]
    assert drain(setup) == []
    assert rig.state()["work_item"]["local_revision"] == revision
    assert (Path(resource["path"]) / "draft").exists()


@pytest.mark.parametrize("finished", [True, False])
def test_obsolete_revision_that_may_have_acted_is_read_back_not_cancelled(setup, finished):
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    target = Path(resource["path"])
    revise = narrow(rig, resource)

    class Crash(BaseException):
        pass

    def fault(stage):
        if stage == "after_sparse_checkout":
            raise Crash()

    with pytest.raises(Crash):
        drain(setup, fault)
    if not finished:
        git(target, "sparse-checkout", "disable")  # Interrupted between marker and sparse.
    take_over(rig)
    drain(setup)
    effect = effect_of(rig, revise)
    assert effect["observations"][-1]["details"]["obsolete_intention"] is True
    assert effect["status"] == ("observed_success" if finished else "unknown")
    assert len(effect["attempts"]) == 1
    if not finished:
        assert (target / "b/result").exists(), "an obsolete revision is never resumed"
        revision = rig.state()["work_item"]["local_revision"]
        drain(setup)  # An unchanged probe replays its observation.
        assert rig.state()["work_item"]["local_revision"] == revision


def obsolete_unapplied_revision(setup, adapter=None):
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    (Path(resource["path"]) / "draft").write_text("dirty tree refuses the revision")
    revise = narrow(rig, resource)
    drain(setup)
    assert effect_of(rig, revise)["status"] == "unknown"
    take_over(rig)
    worker = setup[3]
    Effects(rig.kernel, adapter or GitWorkspaces(), worker).drain(rig.wi)
    assert effect_of(rig, revise)["status"] == "cancelled"
    (Path(resource["path"]) / "draft").unlink()
    return rig, resource


def test_production_dispatch_adapter_reads_back_an_obsolete_revision(setup, tmp_path):
    from harness_rewrite.pi_dispatch import PiDispatch

    (tmp_path / "deliveries").mkdir()
    policy = {"delivery_root": str(tmp_path / "deliveries"), "dispatch_root": str(tmp_path)}
    rig = setup[0]
    adapter = PiDispatch(rig.kernel, policy, setup[3], rig.wi, tmp_path / "harness.pyz")
    _, resource = obsolete_unapplied_revision(setup, adapter)
    assert rows(rig)[0]["status"] == "observed_success"


def test_withdrawn_revision_restores_the_confirmed_workspace(setup):
    rig, resource = obsolete_unapplied_revision(setup)
    restored = rows(rig)[0]
    assert restored["status"] == "observed_success" and restored["sparse"] == []
    assert restored["effect_id"] == resource["effect_id"]
    revise = narrow(rig, restored)  # The current generation revises it again.
    drain(setup)
    assert effect_of(rig, revise)["status"] == "observed_success"
    assert rows(rig)[0]["sparse"] == ["a"]


def test_withdrawn_revision_can_be_replaced(setup):
    rig, resource = obsolete_unapplied_revision(setup)
    original(rig.send("replace_workspace", {
        "resource_id": resource["resource_id"], "snapshot_id": None,
        "resources": ["repo"], "sparse": [], "purpose": "fresh copy",
    }))
    drain(setup)
    old, new = rows(rig)
    assert old["superseded_by"] == new["resource_id"] and new["status"] == "observed_success"


def test_withdrawn_revision_is_reclaimed_when_the_wi_ends(setup):
    from harness_rewrite.model import Command

    rig, resource = obsolete_unapplied_revision(setup)
    original(
        rig.approve(
            "terminate_work",
            {"kind": "cancelled", "reason": "Dispose isolated experiment"},
            actor=rig.contexts["owner"],
        )
    )
    reclaimed = rig.kernel.execute(
        Command("reclaim", "retire_terminal_resources", {}, rig.wi), setup[3](())
    )
    assert reclaimed["result"]["original_request_result"]["effect_ids"], reclaimed
    drain(setup)
    assert rows(rig)[0]["status"] == "retired"
    assert not Path(resource["path"]).exists()


def begun_revision(setup):
    """An admitted revision attempt whose writer has not run yet."""
    from harness_rewrite.model import Command

    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    revise = narrow(rig, resource)
    started = rig.kernel.execute(
        Command("send." + revise + ".0", "begin_effect", {"effect_id": revise}, rig.wi),
        setup[3](()),
    )
    attempt = started["result"]["original_request_result"]["attempt_id"]
    return rig, resource, revise, attempt


def worker_view(setup, effect_id):
    """The effect as its worker sees it, with the resource snapshot."""
    rig = setup[0]
    effects = rig.kernel.inspect(Query(rig.wi), setup[3](()))["effects"]
    return next(e for e in effects if e["effect_id"] == effect_id)


def test_probe_never_settles_while_the_original_writer_holds_the_resource(setup):
    import threading

    rig, resource, revise, attempt = begun_revision(setup)
    target = Path(resource["path"])
    paused, resume = threading.Event(), threading.Event()

    def fault(stage):
        if stage == "before_action_marker":  # After clean(), before the marker.
            paused.set()
            assert resume.wait(30)

    snapshot = worker_view(setup, revise)
    outcome = {}
    writer = threading.Thread(
        target=lambda: outcome.update(result=GitWorkspaces(fault=fault).apply(
            snapshot, encoded(snapshot["snapshot"]))))
    writer.start()
    try:
        assert paused.wait(30)
        take_over(rig)
        drain(setup)
        effect = effect_of(rig, revise)
        assert effect["status"] == "unknown", "a held writer lock is never read as absence"
        assert effect["observations"][-1]["details"]["reason"] == "original_writer_may_be_running"
        assert rows(rig)[0]["sparse"] == ["a"], "the record is not withdrawn"
    finally:
        resume.set()
        writer.join(30)
    assert outcome["result"][0] == "success"
    assert not (target / "b/result").exists()
    drain(setup)  # The fenced probe now reads the writer's completed action.
    assert effect_of(rig, revise)["status"] == "observed_success"
    assert rows(rig)[0]["sparse"] == ["a"] and rows(rig)[0]["status"] == "observed_success"


def test_a_writer_arriving_after_the_fenced_withdrawal_never_acts(setup):
    rig, resource, revise, attempt = begun_revision(setup)
    target = Path(resource["path"])
    snapshot = worker_view(setup, revise)
    take_over(rig)
    drain(setup)
    assert effect_of(rig, revise)["status"] == "cancelled"
    assert rows(rig)[0]["sparse"] == []
    # The original writer resumes only now: the withdrawal marker fences it.
    outcome, details = GitWorkspaces().apply(snapshot, encoded(snapshot["snapshot"]))
    assert (outcome, details["reason"]) == ("absence", "withdrawn_intention_fenced")
    assert (target / "b/result").exists()
    # A late result never revives the settled intention; it asks for attention.
    late = Effects(rig.kernel, GitWorkspaces(), setup[3])._observe(
        rig.wi, snapshot, attempt, "success", {"reason": "late_writer_report"})
    assert late["status"] == "local_committed"
    effect = effect_of(rig, revise)
    assert effect["status"] == "cancelled"
    assert effect["attention"] == "late_result_after_cancel"
    assert rows(rig)[0]["sparse"] == [] and rows(rig)[0]["status"] == "observed_success"


def held_workspaces(rig, handle="owner"):
    """The rows relay-access opens: the kernel's candidates, proven on the host."""
    response = rig.kernel.inspect(Query(view="bindings", limit=2), handle)
    return writable(response["result"]["workspace_access"])


def files(*roots):
    """Every non-directory entry under the roots, with what a write changes."""
    found = {}
    for root in roots:
        for entry in [root, *root.rglob("*")]:
            info = entry.lstat()
            if not entry.is_dir() or entry.is_symlink():
                found[entry] = (info.st_ino, info.st_size, info.st_mtime_ns)
    return found


def within(path, roots):
    path = Path(os.path.realpath(path))
    return any(path == Path(os.path.realpath(r)) or Path(os.path.realpath(r)) in path.parents
               for r in roots)


SANDBOX = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
GIT = ["-c", "user.name=Resource Test", "-c", "user.email=resource@example.invalid",
       "-c", "gc.auto=0", "-c", "maintenance.auto=false"]


def sandboxed(row, common):
    """A Seatbelt prefix that, like CC's Bash sandbox, writes the shared Git directory only through the row."""
    if not SANDBOX:
        return []
    quoted = lambda paths: " ".join(f'(subpath "{os.path.realpath(p)}")' for p in paths)  # noqa: E731
    return [SANDBOX, "-p", "(version 1)(allow default)"
            f'(deny file-write* (subpath "{os.path.realpath(common)}"))'
            f'(allow file-write* {quoted(row["write"])})'
            f'(deny file-write* {quoted(row["deny"])})']


def commit_within(row, common, branch, name, others=()):
    """`git add` and `git commit` in the workspace write only under `write` and outside `deny`.

    Everywhere, the files the commit changes are checked against the row; on
    macOS the commit also runs under a Seatbelt profile that denies every other
    write to the shared Git directory, as CC's Bash sandbox does, and the same
    profile cannot stage in the `others` worktrees.
    """
    path = Path(row["path"])
    (path / "a/result").write_text("changed " + name)
    before = files(common, path)
    for args in (["add", "a/result"], ["commit", "-qm", name]):
        subprocess.run([*sandboxed(row, common), "git", "-C", str(path), *GIT, *args],
                       check=True, capture_output=True)
    after = files(common, path)
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    assert changed and all(
        within(p, row["write"]) and not within(p, row["deny"]) for p in changed
    ), sorted(map(str, changed))
    assert git(path, "log", "-1", "--format=%s") == name
    assert git(common, "rev-parse", "refs/heads/" + branch) == git(path, "rev-parse", "HEAD")
    for other in others:
        (Path(other) / "a/result").write_text("foreign")
        if SANDBOX:
            denied = subprocess.run([*sandboxed(row, common), "git", "-C", str(other), *GIT, "add", "a/result"],
                                    capture_output=True)
            assert denied.returncode != 0


def test_the_holding_session_reads_back_its_ready_workspaces_for_writing(setup):
    rig, bindings, _, _ = setup
    original(allocate(setup))
    # An allocation not yet observed opens nothing.
    assert held_workspaces(rig) == []
    drain(setup)
    [resource] = rows(rig)
    path = Path(resource["path"])
    common = Path(bindings[0].common_dir)
    admin = git(path, "rev-parse", "--absolute-git-dir")
    # The allocation recorded the administrative directory it proved.
    response = rig.kernel.inspect(Query(view="bindings", limit=2), "owner")
    [candidate] = response["result"]["workspace_access"]
    assert candidate["git_dir"] == os.path.realpath(admin)
    [row] = held_workspaces(rig)
    ref = common / "refs/heads" / resource["branch"]
    assert (row["work_item_id"], row["resource_id"], row["path"]) == (
        rig.wi, resource["resource_id"], str(path))
    # Its own branch only: the ref, its lock and its reflog, no ref directory.
    assert row["write"] == [str(path), os.path.realpath(admin), str(common / "objects"), str(ref),
                            str(ref) + ".lock", str(common / "logs/refs/heads" / resource["branch"])]
    assert str(path / ".git") in row["deny"]
    assert os.path.realpath(admin) + "/config.worktree" in row["deny"]
    # Another Agent, or another session of the same Agent, gets none of them.
    rig.contexts["peer"] = replace(rig.contexts["owner"], session="other-session")
    assert held_workspaces(rig, "helper") == held_workspaces(rig, "peer") == []
    commit_within(row, common, resource["branch"], "change")
    # A WI that ended asks for retirement; its workspace closes before it is gone.
    original(retire(setup))
    assert held_workspaces(rig) == []


def repository(tmp_path, *branches):
    source = tmp_path / "client-a"
    source.mkdir()
    git(source, "init", "-q")
    (source / "a").mkdir()
    (source / "a/result").write_text("first")
    git(source, "add", ".")
    git(source, *GIT, "commit", "-qm", "base")
    for branch in branches:
        git(source, "branch", branch)
    return source


def existing(source, root, target, branch, recorded=None):
    """The recorded continuation of an original allocation, bound to a real repository."""
    state = deepcopy(JOURNALS["existing_execution"]["state"])
    (resource,) = state["workspaces"].values()
    assert resource["existing_allocation"] and resource["effect_id"] is None
    resource.update(target=target, branch=branch)
    resource["binding"] = {**resource["binding"], "common_dir": str(source / ".git"),
                           "managed_root": str(root)}
    candidates = session_write(JOURNALS["existing_execution"]["wi"], state)
    return [{**c, "git_dir": recorded} for c in candidates] if recorded else candidates


def test_an_existing_allocation_on_its_original_branch_commits_within_its_rows(tmp_path):
    """The recorded continuation of an original allocation keeps its own branch name."""
    source = repository(tmp_path, "original-work", "other-work")
    root = tmp_path / "existing-workspaces"
    git(source, "worktree", "add", "-q", str(root / "producer"), "original-work")
    common = source / ".git"
    [row] = writable(existing(source, root, "producer", "original-work"))
    ref = common / "refs/heads/original-work"
    assert row["write"][3:] == [str(ref), str(ref) + ".lock", str(common / "logs/refs/heads/original-work")]
    assert not within(common / "refs/heads/other-work", row["write"])
    commit_within(row, common, "original-work", "continue")
    assert git(common, "rev-parse", "other-work") != git(common, "rev-parse", "original-work")


def test_same_named_worktrees_each_open_only_their_own_administrative_directory(tmp_path):
    """Git suffixes a clashing administrative directory (ws, ws1); it is proven, never guessed."""
    source = repository(tmp_path, "branch-a", "branch-b")
    common = source / ".git"
    managed = tmp_path / "managed"
    first, second = managed / "wi_a" / "ws", managed / "wi_b" / "ws"
    git(source, "worktree", "add", "-q", str(first), "branch-a")
    git(source, "worktree", "add", "-q", str(second), "branch-b")
    admin_a, admin_b = (git(p, "rev-parse", "--absolute-git-dir") for p in (first, second))
    assert Path(admin_a).name == "ws" and Path(admin_b).name == "ws1"
    [row_a] = writable(existing(source, managed / "wi_a", "ws", "branch-a"))
    [row_b] = writable(existing(source, managed / "wi_b", "ws", "branch-b"))
    assert row_a["write"][1] == os.path.realpath(admin_a)
    assert row_b["write"][1] == os.path.realpath(admin_b)
    assert not any(within(p, row_b["write"]) for p in (first, admin_a, common / "refs/heads/branch-a"))
    commit_within(row_b, common, "branch-b", "second", others=[first])
    commit_within(row_a, common, "branch-a", "first", others=[second])
    # A recorded directory that belongs to the other worktree proves nothing.
    assert writable(existing(source, managed / "wi_b", "ws", "branch-b", recorded=admin_a)) == []
    assert writable(existing(source, managed / "wi_b", "ws", "branch-b", recorded=admin_b)) == [row_b]


@pytest.mark.parametrize("damage", ["gitdir", "link", "head", "absent"])
def test_a_workspace_whose_ownership_cannot_be_proven_opens_nothing(tmp_path, damage):
    source = repository(tmp_path, "original-work", "other-work")
    root = tmp_path / "existing-workspaces"
    tree = root / "producer"
    git(source, "worktree", "add", "-q", str(tree), "original-work")
    admin = Path(git(tree, "rev-parse", "--absolute-git-dir"))
    if damage == "gitdir":
        (admin / "gitdir").write_text(str(tmp_path / "elsewhere/.git") + "\n")
    elif damage == "link":
        (tree / ".git").write_text("gitdir: " + str(source / ".git") + "\n")
    elif damage == "head":
        (admin / "HEAD").write_text("ref: refs/heads/other-work\n")
    else:
        (tree / ".git").unlink()
    assert writable(existing(source, root, "producer", "original-work")) == []


@pytest.mark.parametrize("branch", ["../config", "work/../../hooks/x", "work.lock", "/abs", ""])
def test_a_branch_that_is_no_plain_ref_path_opens_nothing(branch):
    state = deepcopy(JOURNALS["existing_execution"]["state"])
    (resource,) = state["workspaces"].values()
    resource["branch"] = branch
    assert session_write("wi", state) == []


def test_allocation_outside_the_wi_scope_names_the_wi_resources(setup):
    rig, _, commits, _ = setup
    refused = rig.send("allocate_workspaces", {"workspaces": [{
        "repo_id": "one", "base_commit": commits["one"], "resources": ["coevolution/skills"],
        "sparse": [], "purpose": "Outside the WI scope"}]})
    assert error(refused) == "authority_denied"
    reason = refused["error"]["reason"]
    assert reason.startswith("Current resource execution scope required: ")
    assert "['coevolution/skills']" in reason and "['repo']" in reason
