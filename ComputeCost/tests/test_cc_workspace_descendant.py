"""CC may change cwd within its enrolled workspace without changing actor."""

from pathlib import Path
import json
import subprocess
from copy import deepcopy

import pytest

from pulse_compute_cost.receipt_sources import (
    registered_workspace_actor,
    collect_registered_sources,
)
from pulse_compute_cost.source_registration import actor_candidate_registry
from test_cc_actor_attribution import setup_source, report


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def workspace(tmp_path):
    ledger, registry, surfaces, path, row = setup_source(tmp_path)
    repo = tmp_path / "PULSE"
    repo.mkdir()
    git(repo, "init", "-q")
    git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "fixture",
    )
    folder = Path(row["cwd"])
    folder.mkdir()
    child = folder / ".relay" / "tmp" / "evidence"
    child.mkdir(parents=True)
    source = actor_candidate_registry(registry, surfaces, site="ownermac")["sources"][0]
    return ledger, registry, surfaces, path, row, repo, folder, child, source


def test_cc_child_workspace_retains_actor_without_changing_measurement(tmp_path):
    ledger, registry, surfaces, path, row, _, _, child, _ = workspace(tmp_path)
    row["cwd"] = str(child)
    path.write_text(json.dumps(row) + "\n")
    collect_registered_sources(registry, ledger)
    before = ledger.receipt_snapshot()
    old = report(ledger)
    collect_registered_sources(
        actor_candidate_registry(registry, surfaces, site="ownermac"), ledger
    )
    out = report(ledger)
    assert [g["key"]["agent"] for g in out["groups"]] == ["pulse"]
    assert (
        out["summary"]["usage"]["known_total_tokens"]
        == old["summary"]["usage"]["known_total_tokens"]
        == 38
    )
    assert (
        out["summary"]["cost"]["partial_machine_cash_usd"]
        == old["summary"]["cost"]["partial_machine_cash_usd"]
    )
    assert (
        ledger.receipt_snapshot(watermark=before["watermark"])["sha256"]
        == before["sha256"]
    )
    again = collect_registered_sources(
        actor_candidate_registry(registry, surfaces, site="ownermac"), ledger
    )
    assert again["inserted"] == 0


def test_cc_linked_workspace_child_and_runtime_cache_separation(tmp_path):
    _, _, _, _, _, repo, _, _, source = workspace(tmp_path)
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
    child = linked / "local-pulse-space" / "nested"
    child.mkdir(parents=True)
    cache = {}
    other = deepcopy(source)
    for rule in other["workspace_actors"]:
        rule["runtime"] = "codex"
    assert (
        registered_workspace_actor(other, str(child), "codex", workspace_cache=cache)[
            "executor"
        ]
        is None
    )
    assert (
        registered_workspace_actor(source, str(child), "cc", workspace_cache=cache)[
            "executor"
        ]
        == "pulse"
    )
    assert (
        registered_workspace_actor(other, str(child), "codex", workspace_cache=cache)[
            "executor"
        ]
        is None
    )


@pytest.mark.parametrize("case", ["sibling", "symlink", "foreign_repo", "missing"])
def test_cc_descendant_requires_the_same_registered_git_workspace(tmp_path, case):
    _, _, _, _, _, repo, folder, child, source = workspace(tmp_path)
    if case == "sibling":
        child = repo / "local-pulse-space-copy"
        child.mkdir()
    elif case == "symlink":
        outside = repo / "outside"
        outside.mkdir()
        child = folder / "escape"
        child.symlink_to(outside, target_is_directory=True)
    elif case == "foreign_repo":
        git(child, "init", "-q")
    else:
        child = folder / "does-not-exist"
    assert registered_workspace_actor(source, str(child), "cc")["executor"] is None


def test_cc_overlapping_actor_authority_is_not_guessed(tmp_path):
    from pulse_compute_cost.receipts import ReceiptError, reference

    _, _, _, _, _, _, _, child, source = workspace(tmp_path)
    rule = source["workspace_actors"][0]
    source["workspace_actors"].append(
        {
            **rule,
            "workspace_root": str(child),
            "agent": "duke",
            "registry_ref": reference("registry", "duke"),
        }
    )
    with pytest.raises(ReceiptError, match="conflicting workspace actor"):
        registered_workspace_actor(source, str(child), "cc")


def test_cc_original_checkout_with_separate_git_metadata(tmp_path):
    _, _, _, _, _, repo, _, child, source = workspace(tmp_path)
    metadata = tmp_path / ".admin" / "PULSE"
    metadata.parent.mkdir()
    git(repo, "init", "-q", "--separate-git-dir", str(metadata))
    # Absorbed submodule layouts list their admin directory as the main entry.
    assert str(child.resolve()).startswith(str(repo.resolve()))
    assert registered_workspace_actor(source, str(child), "cc")["executor"] == "pulse"


def test_one_collection_checks_each_workspace_pair_once_across_sources(tmp_path, monkeypatch):
    ledger, registry, surfaces, path, row, _, _, child, _ = workspace(tmp_path)
    row["cwd"] = str(child)
    path.write_text(json.dumps(row) + "\n")
    other = tmp_path / "native-other"
    other.mkdir()
    second = {**row, "sessionId": "other-session", "uuid": "other-message",
              "message": {**row["message"], "id": "other-provider-message"}}
    (other / "other-session.jsonl").write_text(json.dumps(second) + "\n")
    registry["sources"].append({**registry["sources"][0], "id": "second-cc", "path": str(other)})
    enrolled = actor_candidate_registry(registry, surfaces, site="ownermac")
    assert [s["kind"] for s in enrolled["sources"]] == ["cc", "cc"]
    run = subprocess.run
    toplevels = []

    def counted(argv, *args, **kwargs):
        if argv[:1] == ["git"] and "--show-toplevel" in argv:
            toplevels.append(argv[2])
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counted)
    collect_registered_sources(enrolled, ledger)
    # Declared root and actual cwd, once for the whole collection.
    assert sorted(toplevels) == sorted({toplevels[0], toplevels[-1]}) and len(toplevels) == 2
    out = report(ledger)
    assert [g["key"]["agent"] for g in out["groups"]] == ["pulse"]
    assert out["summary"]["usage"]["known_total_tokens"] == 2 * 38


def test_folder_git_answers_are_shared_by_pairs_with_the_same_root(tmp_path, monkeypatch):
    _, _, _, _, _, repo, folder, child, source = workspace(tmp_path)
    sibling = folder / "other-child"
    sibling.mkdir()
    run = subprocess.run
    calls = []

    def counted(argv, *args, **kwargs):
        if argv[:1] == ["git"]:
            calls.append((argv[2], tuple(argv[3:])))
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counted)
    cache = {}
    for cwd in (child, sibling):
        assert registered_workspace_actor(source, str(cwd), "cc", workspace_cache=cache)["executor"] == "pulse"
    assert len(calls) == len(set(calls))
    uncached = []
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: (uncached.append(argv), run(argv, *a, **k))[1])
    for cwd in (child, sibling):
        assert registered_workspace_actor(source, str(cwd), "cc")["executor"] == "pulse"
    assert len(calls) < len(uncached)
