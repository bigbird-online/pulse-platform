"""Real Git publication without interactive identity; bounded directory indexing."""

from pathlib import PurePosixPath

import pytest

from harness_rewrite import Query
from harness_rewrite import production_history
from tests_rewrite.test_production_ledger import Rig, WI, git


@pytest.mark.parametrize("ambient_identity", [False, True])
def test_native_publication_owns_its_git_identity_without_changing_account_or_repo(
    tmp_path, monkeypatch, ambient_identity
):
    rig = Rig(tmp_path)
    home = tmp_path / "isolated-relay-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "absent-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(key, raising=False)
    for repo in (rig.a, rig.b):
        git(repo, "config", "--unset-all", "user.name")
        git(repo, "config", "--unset-all", "user.email")
    if ambient_identity:
        for role in ("AUTHOR", "COMMITTER"):
            monkeypatch.setenv("GIT_" + role + "_NAME", "Unrelated operator")
            monkeypatch.setenv("GIT_" + role + "_EMAIL", "unrelated@example.invalid")
    before_config = (rig.a / ".git/config").read_bytes()
    result = rig.approve()
    assert result["status"] == "local_committed", result
    commit = git(rig.remote, "rev-parse", "main")
    assert git(rig.remote, "log", "-1", "--format=%an <%ae>|%cn <%ce>") == (
        "SCL Production Ledger <scl-ledger@invalid.local>|"
        "SCL Production Ledger <scl-ledger@invalid.local>"
    )
    peer = rig.client(rig.backend(rig.b))
    observed = peer.inspect(Query(WI), "native")
    assert observed["result"]["current_observation"]["affiliation"]["state"] == "admitted"
    assert rig.approve(peer)["status"] == "local_committed"
    assert git(rig.remote, "rev-parse", "main") == commit
    assert (rig.a / ".git/config").read_bytes() == before_config
    assert list(home.iterdir()) == []


def test_related_document_index_preserves_exact_path_matching_with_one_scan(monkeypatch):
    identities = {WI, "wi_20261008_5500", "wi_20261008_551"}
    tree = {
        f".harness/work-items/{WI}.yaml": "a",
        f".harness/events/{WI}.events.yaml": "b",
        f".harness/evidence/{WI}/nested/wi_20261008_551.notes.md": "c",
        f".harness/evidence/{WI}.other/file.json": "d",
        ".harness/work-items/wi_20261008_5500.yaml": "e",
        f".harness/evidence/not-{WI}.json": "f",
        ".harness/other.yaml": "g",
    }
    expected = {
        wi: {p: sha for p, sha in tree.items()
             if wi in PurePosixPath(p).parts or PurePosixPath(p).name.startswith(wi + ".")}
        for wi in identities
    }
    calls = []
    def counted(path):
        calls.append(path)
        return PurePosixPath(path)
    monkeypatch.setattr(production_history, "PurePosixPath", counted)
    actual = production_history.related_document_index(tree, identities)
    assert actual == expected
    assert len(calls) == len(tree)
    calls.clear()
    larger = identities | {f"wi_20261008_{n}" for n in range(600, 900)}
    actual = production_history.related_document_index(tree, larger)
    assert {wi: actual[wi] for wi in identities} == expected
    assert len(calls) == len(tree)


def test_production_reads_build_the_directory_index_once_per_fresh_transaction(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    original = production_history.related_document_index
    calls = []
    def counted(tree, identities):
        calls.append((len(tree), len(identities)))
        return original(tree, identities)
    monkeypatch.setattr(production_history, "related_document_index", counted)
    before = rig.kernel.inspect(Query(WI), "native")
    assert before["status"] == "read_only"
    assert len(calls) == 1
    path = rig.a / ".harness/evidence" / WI / "later.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"evidence":"new original"}')
    git(rig.a, "add", str(path))
    git(rig.a, "commit", "-m", "Later related original")
    git(rig.a, "push", "origin", "main")
    after = rig.kernel.inspect(Query(WI), "native")
    assert len(calls) == 2
    related = after["result"]["current_observation"]["production_history"]["related_documents"]
    assert str(path.relative_to(rig.a)) in related
    assert before["result"]["current_observation"]["definition"] == after["result"]["current_observation"]["definition"]
