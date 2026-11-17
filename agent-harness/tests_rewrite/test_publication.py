"""Real Git bundle / remote / consumer experiments; no fake push receipts."""

from dataclasses import replace
from pathlib import Path

import pytest

from harness_rewrite.effects import Effects
from harness_rewrite.git_publication import GitPublication
from harness_rewrite.git_workspaces import GitWorkspaces
from harness_rewrite.model import PublicationTarget, SourceConsumer, Rejected
from harness_rewrite.publication import EFFECT_KINDS
from harness_rewrite.source_capture import capture, retained
from tests_rewrite.test_workspaces import setup, allocate, drain, rows, git  # noqa: F401
from tests_rewrite.test_workflow import original, error
from tests_rewrite.test_review import candidate_id, assign, verify, approve, VERIFIER


@pytest.fixture(params=[False, True], ids=["full", "incremental"])
def publishing(setup, tmp_path, request):  # noqa: F811
    rig, bindings, commits, worker = setup
    source = Path(bindings[0].source)
    remote = tmp_path / "published.git"
    git(source, "init", "--bare", str(remote))
    git(source, "push", str(remote), commits["one"] + ":refs/heads/main")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "fetch", "origin", "main")
    rig.contexts["owner"] = replace(rig.contexts["owner"], repositories=tuple(replace(b, remote_url=str(remote)) if b.repo_id == "one" and request.param else b for b in rig.contexts["owner"].repositories))
    original(allocate(setup))
    drain(setup)
    resource = rows(rig)[0]
    path = Path(resource["path"])
    consumer = tmp_path / "consumer"
    git(source, "clone", "--branch", "main", str(remote), str(consumer))
    (path / "a/result").write_text("fixed candidate source result")
    git(path, "add", "a/result")
    git(path, "commit", "-qm", "candidate result")
    head = git(path, "rev-parse", "HEAD")
    context = replace(
        rig.contexts["owner"],
        repositories=tuple(replace(b, remote_url=str(remote)) if b.repo_id == "one" and request.param else b
                           for b in rig.contexts["owner"].repositories),
        publication_targets=(PublicationTarget("origin", "one", str(remote)),),
        source_consumers=(
            SourceConsumer(
                "consumer", "one", str(consumer), str(consumer / ".git"), str(remote)
            ),
        ),
    )
    rig.contexts["owner"] = context
    metadata, artifact = capture(resource, context, head)
    if request.param:
        assert metadata["bundle_prerequisite"] == commits["one"]
        assert ("-" + commits["one"]).encode() in artifact.content.split(b"\n\n", 1)[0]
    else:
        assert "bundle_prerequisite" not in metadata
    rig.contexts["capture"] = replace(
        context, git_source=metadata, artifacts=(artifact,)
    )
    snapshot = original(
        rig.send(
            "capture_source",
            {"resource_id": resource["resource_id"], "commit": head},
            handle="capture",
            request="capture",
        )
    )["snapshot"]
    # Existing work graph has no named outputs. Adopt an explicitly confirmed
    # revised plan so the real captured bundle is part of the fixed candidate.
    from tests_rewrite.test_workflow import graph, node

    plan = graph(rig, [node("work", outputs=["source"])], revision=2)
    original(
        rig.approve(
            "revise_plan",
            {
                "previous_revision": 1,
                "graph": plan,
                "plan_text": "Freeze exact Git source",
                "reuse_results": [],
                "reason": "Use captured source",
            },
            actor=context,
        )
    )
    attempt = original(rig.claim("work", revision=2))["attempt_id"]
    original(rig.result(attempt, artifacts={"source": artifact}))
    candidate = candidate_id(rig)
    rig.contexts["verifier"] = VERIFIER
    original(verify(rig, candidate, assign(rig, candidate)))
    original(approve(rig, candidate))
    data = {
        "candidate_id": candidate,
        "snapshot_id": snapshot["snapshot_id"],
        "target_id": "origin",
        "expected_head": commits["one"],
        "consumers": [{"consumer_id": "consumer", "expected_head": commits["one"]}],
    }
    original(rig.send("declare_source_publication", data, request="publish"))
    evidence = tmp_path / "publication"
    evidence.mkdir()

    def source_worker(artifacts):
        handle = worker(artifacts)
        rig.contexts[handle] = replace(
            rig.contexts[handle],
            effect_kinds=rig.contexts[handle].effect_kinds | EFFECT_KINDS,
        )
        return handle

    return (
        rig,
        resource,
        path,
        remote,
        consumer,
        head,
        artifact,
        evidence,
        source_worker,
        data,
    )


def publish(fixture, fault=None, effect_fault=None):
    rig, _, _, _, _, _, _, root, worker, _ = fixture
    return Effects(
        rig.kernel, GitPublication(root, fault=fault), worker, fault=effect_fault
    ).drain(rig.wi)


def source_effects(rig):
    return [e for e in rig.state()["effects"] if e["effect_kind"] in EFFECT_KINDS]


def test_fixed_source_published_adopted_then_retired_without_losing_bundle(publishing):
    rig, resource, path, remote, consumer, head, artifact, root, worker, data = (
        publishing
    )
    assert (
        error(rig.send("complete", {"candidate_id": data["candidate_id"]}))
        == "domain_conflict"
    )
    # Product publishes captured source, even if live worktree later gains dirt.
    (path / "uncommitted").write_text("keep unique later work")
    publish(publishing)
    assert git(remote, "rev-parse", "main") == head
    assert git(consumer, "rev-parse", "HEAD") == head
    assert (consumer / "a/result").read_text() == "fixed candidate source result"
    assert all(e["status"] == "observed_success" for e in source_effects(rig))
    assert (path / "uncommitted").read_text() == "keep unique later work"
    original(rig.send("complete", {"candidate_id": data["candidate_id"]}))
    original(
        rig.approve(
            "retire_resources",
            {
                "resource_ids": [resource["resource_id"]],
                "reason": "Approved exact inventory",
            },
            actor=rig.contexts["owner"],
        )
    )
    Effects(rig.kernel, GitWorkspaces(), worker).drain(rig.wi)
    assert path.exists()  # Unique later data must block retirement.
    # Move it to an explicitly retained evidence file, not delete or reset it.
    (path / "uncommitted").rename(root / "preserved-human-draft")
    Effects(rig.kernel, GitWorkspaces(), worker).drain(rig.wi)
    assert rows(rig)[0]["status"] == "observed_absence"
    Effects(rig.kernel, GitWorkspaces(), worker).drain(rig.wi)
    assert rows(rig)[0]["status"] == "retired"
    assert not path.exists()
    assert git(remote, "rev-parse", "main") == head
    prerequisites = [line.split()[0][1:].decode() for line in artifact.content.split(b"\n\n", 1)[0].splitlines() if line.startswith(b"-")]
    suffix = "." + prerequisites[0] if prerequisites else ""
    bundle = (
        Path(resource["path"]).parents[1]
        / ".harness-resource-evidence"
        / (resource["resource_id"] + "." + head + suffix + ".bundle")
    )
    assert bundle.read_bytes() == artifact.content
    restored = retained(rig.kernel, rig.wi, "owner", resource["resource_id"], head)
    assert restored is not None
    metadata, saved = restored
    assert saved == artifact
    rig.contexts["recovered"] = replace(
        rig.contexts["owner"], git_source=metadata, artifacts=(saved,)
    )
    before = rig.state()["work_item"]["local_revision"]
    original(
        rig.send(
            "capture_source",
            {"resource_id": resource["resource_id"], "commit": head},
            handle="recovered",
            request="capture",
        )
    )
    assert rig.state()["work_item"]["local_revision"] == before
    assert (
        error(
            rig.send(
                "capture_source",
                {"resource_id": resource["resource_id"], "commit": head},
                handle="recovered",
                request="new-after-retirement",
            )
        )
        == "authority_denied"
    )
    assert (
        retained(rig.kernel, rig.wi, "owner", resource["resource_id"], "0" * 40) is None
    )


@pytest.mark.parametrize("stage", ["after_push", "after_adoption"])
def test_ambiguous_send_readback_keeps_original_attempt(publishing, stage):
    class Crash(BaseException):
        pass

    def fault(at):
        if at == stage:
            raise Crash()

    with pytest.raises(Crash):
        publish(publishing, fault=fault)
    publish(publishing)
    rig = publishing[0]
    assert all(e["status"] == "observed_success" for e in source_effects(rig))
    assert all(len(e["attempts"]) == 1 for e in source_effects(rig))


def test_marked_but_unobserved_push_never_blindly_repeats(publishing):
    class Crash(BaseException):
        pass

    def fault(at):
        if at == "before_push":
            raise Crash()

    with pytest.raises(Crash):
        publish(publishing, fault=fault)
    publish(publishing)
    rig, _, _, remote, consumer, head, *_ = publishing
    assert git(remote, "rev-parse", "main") != head
    assert git(consumer, "rev-parse", "HEAD") != head
    assert source_effects(rig)[0]["status"] == "unknown"
    assert len(source_effects(rig)[0]["attempts"]) == 1


@pytest.mark.parametrize("dirty", ["tracked", "untracked", "ignored"])
def test_dirty_consumer_preserved_and_not_reported_adopted(publishing, dirty):
    rig, _, _, _, consumer, head, *_ = publishing
    target = (
        consumer
        / ({"tracked": "a/result", "untracked": "draft", "ignored": "ignored"}[dirty])
    )
    target.write_text("human-owned content")
    publish(publishing)
    assert source_effects(rig)[0]["status"] == "observed_success"
    assert source_effects(rig)[1]["status"] != "observed_success"
    assert target.read_text() == "human-owned content"
    assert git(consumer, "rev-parse", "HEAD") != head


def test_unknown_remote_change_preserved_and_blocks_consumer(publishing):
    rig, _, path, remote, consumer, head, *_ = publishing
    git(consumer, "config", "user.name", "Other writer")
    git(consumer, "config", "user.email", "other@example.invalid")
    (consumer / "a/result").write_text("other result")
    git(consumer, "add", "a/result")
    git(consumer, "commit", "-qm", "other result")
    other = git(consumer, "rev-parse", "HEAD")
    git(consumer, "push", "origin", "main")
    publish(publishing)
    assert git(remote, "rev-parse", "main") == other != head
    assert source_effects(rig)[0]["status"] == "observed_failure"
    assert source_effects(rig)[1]["attempts"] == []


def test_capture_rejects_claimed_commit_and_moving_or_dirty_source(publishing):
    rig, resource, path, _, _, head, *_ = publishing
    with pytest.raises(Rejected):
        capture(resource, rig.contexts["owner"], resource["base_commit"])
    (path / "a/result").write_text("later edits")
    with pytest.raises(OSError):
        capture(resource, rig.contexts["owner"], head)


def test_late_publication_cannot_mutate_after_human_stop(publishing):
    rig, _, _, remote, consumer, head, *rest = publishing
    original(
        rig.approve("stop", {"reason": "Stop before send"}, actor=rig.contexts["owner"])
    )
    publish(publishing)
    assert git(remote, "rev-parse", "main") != head
    assert git(consumer, "rev-parse", "HEAD") != head
    assert all(e["attempts"] == [] for e in source_effects(rig))


def test_exact_publication_retry_does_not_add_external_attempts(publishing):
    rig, _, _, _, _, _, _, _, _, data = publishing
    original(rig.send("declare_source_publication", data, request="publish"))
    publish(publishing)
    original(rig.send("declare_source_publication", data, request="publish"))
    assert publish(publishing) == []
    assert len(source_effects(rig)) == 2
    assert all(len(e["attempts"]) == 1 for e in source_effects(rig))
    changed = {**data, "expected_head": "0" * 40}
    assert (
        error(rig.send("declare_source_publication", changed, request="publish"))
        == "request_conflict"
    )


def test_new_commit_after_published_snapshot_is_preserved_on_retirement(publishing):
    rig, resource, path, _, _, _, _, _, worker, data = publishing
    publish(publishing)
    (path / "a/result").write_text("unique newer unpublished commit")
    git(path, "add", "a/result")
    git(path, "commit", "-qm", "newer unique result")
    later = git(path, "rev-parse", "HEAD")
    original(rig.send("complete", {"candidate_id": data["candidate_id"]}))
    original(
        rig.approve(
            "retire_resources",
            {
                "resource_ids": [resource["resource_id"]],
                "reason": "Retire only safe published source",
            },
            actor=rig.contexts["owner"],
        )
    )
    Effects(rig.kernel, GitWorkspaces(), worker).drain(rig.wi)
    assert path.exists() and git(path, "rev-parse", "HEAD") == later
    assert rows(rig)[0]["status"] != "retired"


def test_historical_readback_after_stop_observes_send_without_starting_adoption(
    publishing,
):
    class Crash(BaseException):
        pass

    def fault(at):
        if at == "after_push":
            raise Crash()

    with pytest.raises(Crash):
        publish(publishing, fault=fault)
    rig, _, _, remote, consumer, head, *_ = publishing
    original(
        rig.approve(
            "stop", {"reason": "Stop after uncertain send"}, actor=rig.contexts["owner"]
        )
    )
    publish(publishing)
    assert git(remote, "rev-parse", "main") == head
    assert git(consumer, "rev-parse", "HEAD") != head
    assert source_effects(rig)[0]["status"] == "observed_success"
    assert source_effects(rig)[1]["attempts"] == []
