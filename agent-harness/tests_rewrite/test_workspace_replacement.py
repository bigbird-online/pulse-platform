"""Replacement advances one resource lineage without erasing its old checkout."""

# ruff: noqa: F811 -- the imported pytest fixture is injected by name

from dataclasses import replace
from pathlib import Path

import pytest

from harness_rewrite.source_capture import capture
from tests_rewrite.test_workspaces import setup, allocate, drain, rows, git  # noqa: F401
from tests_rewrite.test_workflow import original, error


def request(resource, snapshot=None):
    return {
        "resource_id": resource["resource_id"],
        "snapshot_id": snapshot,
        "resources": resource["resources"],
        "sparse": ["a"],
        "purpose": "Replace physical checkout while preserving originals",
    }


def test_replacement_from_fixed_source_preserves_old_unique_dirt_and_identity(setup):  # noqa: F811
    rig, _, _, _ = setup
    original(allocate(setup))
    drain(setup)
    old = rows(rig)[0]
    path = Path(old["path"])
    (path / "a/result").write_text("captured new source")
    git(path, "add", "a/result")
    git(path, "commit", "-qm", "new source")
    head = git(path, "rev-parse", "HEAD")
    metadata, artifact = capture(old, rig.contexts["owner"], head)
    rig.contexts["capture"] = replace(
        rig.contexts["owner"], git_source=metadata, artifacts=(artifact,)
    )
    source = original(
        rig.send(
            "capture_source",
            {"resource_id": old["resource_id"], "commit": head},
            handle="capture",
        )
    )["snapshot"]
    (path / "a/result").write_text("later tracked dirt")
    (path / "unique").write_text("later untracked data")
    (path / "ignored").write_text("later ignored data")
    data = request(old, source["snapshot_id"])
    accepted = original(rig.send("replace_workspace", data, request="replacement"))
    assert error(rig.claim("work")) == "authority_denied"
    assert (
        original(rig.send("replace_workspace", data, request="replacement")) == accepted
    )
    drain(setup)
    old_now, new = rows(rig)
    assert old_now["superseded_by"] == new["resource_id"] == accepted["resource_ids"][0]
    assert new["replaces"] == old["resource_id"]
    assert new["path"] != old["path"] and new["branch"] != old["branch"]
    assert git(Path(new["path"]), "rev-parse", "HEAD") == head
    assert (Path(new["path"]) / "a/result").read_text() == "captured new source"
    assert not (Path(new["path"]) / "b").exists()
    assert (path / "a/result").read_text() == "later tracked dirt"
    assert (path / "unique").read_text() == "later untracked data"
    assert (path / "ignored").read_text() == "later ignored data"
    assert not old_now["retirement"]
    assert (
        error(
            rig.send(
                "revise_workspace",
                {k: v for k, v in data.items() if k != "snapshot_id"},
            )
        )
        == "domain_conflict"
    )
    assert error(rig.send("replace_workspace", data)) == "domain_conflict"
    assert (
        error(
            rig.send(
                "capture_source",
                {"resource_id": old["resource_id"], "commit": head},
                handle="capture",
            )
        )
        == "domain_conflict"
    )


def test_missing_old_checkout_does_not_block_replacement_from_registered_base(
    setup, tmp_path
):  # noqa: F811
    rig, bindings, _, _ = setup
    original(allocate(setup))
    drain(setup)
    old = rows(rig)[0]
    # Keep all bytes in a separate fixture path; no old registration repair or delete.
    moved = tmp_path / "externally-moved-checkout"
    Path(old["path"]).rename(moved)
    original(rig.send("replace_workspace", request(old)))
    drain(setup)
    new = rows(rig)[1]
    assert new["status"] == "observed_success"
    assert (moved / "a/result").read_text() == "first"
    assert (Path(new["path"]) / "a/result").read_text() == "first"
    assert (Path(bindings[0].source) / "human-draft").exists()
    assert old["branch"] in git(
        Path(bindings[0].source), "worktree", "list", "--porcelain"
    )


def test_replacement_requires_settled_owned_unoccupied_original_and_known_snapshot(
    setup,
):  # noqa: F811
    rig = setup[0]
    original(allocate(setup))
    old = rows(rig)[0]
    assert error(rig.send("replace_workspace", request(old))) == "domain_conflict"
    drain(setup)
    assert (
        error(rig.send("replace_workspace", request(old, "source.missing")))
        == "source_invalid"
    )
    original(rig.claim("work"))
    assert error(rig.send("replace_workspace", request(old))) == "domain_conflict"
    assert len(rows(rig)) == 1


def test_replacement_interruption_reuses_original_physical_attempt(setup):  # noqa: F811
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    original(
        rig.send("replace_workspace", request(rows(rig)[0]), request="replacement")
    )

    class Crash(BaseException):
        pass

    def fault(stage):
        if stage == "after_worktree_add":
            raise Crash()

    with pytest.raises(Crash):
        drain(setup, fault=fault)
    drain(setup)
    new = rows(rig)[1]
    assert new["status"] == "observed_success"
    effect = next(
        e for e in rig.state()["effects"] if e["effect_id"] == new["effect_id"]
    )
    assert len(effect["attempts"]) == 1
    assert Path(rows(rig)[0]["path"]).exists()
