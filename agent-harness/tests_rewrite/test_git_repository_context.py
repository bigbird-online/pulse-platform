"""Git hooks cannot redirect the production ledger or its object/index IO."""

import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from harness_rewrite.git_objects import object_reader
from tests_rewrite.test_production_ledger import Rig, git


def foreign_repository(tmp_path):
    source = tmp_path / "foreign"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Foreign Producer")
    git(source, "config", "user.email", "foreign@example.invalid")
    git(source, "remote", "add", "origin", str(tmp_path / "other.git"))
    (source / "business.txt").write_text("original\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "original")
    target = tmp_path / "foreign-worktree"
    git(source, "worktree", "add", "-b", "work", str(target))
    return source, target


def test_real_foreign_hook_reads_and_publishes_only_registered_ledger(tmp_path):
    rig = Rig(tmp_path)
    foreign, target = foreign_repository(tmp_path)
    script = tmp_path / "read-ledger.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from harness_rewrite.production_ledger import ProductionLedger\n"
        f"ledger=ProductionLedger({rig.ledger.config!r}, {rig.ledger.namespace!r})\n"
        "revision=ledger.refresh()\n"
        "assert ledger.blobs(ledger.tree(revision))\n"
        "ledger.publish(revision, {'.harness/hook-observation.json': b'{}'})\n"
    )
    hook = foreign / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", "-B", str(script)]) + "\n")
    hook.chmod(0o700)
    (target / "business.txt").write_text("new result\n")
    git(target, "add", "business.txt")
    result = subprocess.run(["git", "-C", str(target), "commit", "-m", "business result"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert git(target, "show", "HEAD:business.txt") == "new result"
    assert git(target, "status", "--porcelain") == ""
    assert git(target, "ls-tree", "-r", "--name-only", "HEAD") == "business.txt"
    assert git(rig.remote, "show", "main:.harness/hook-observation.json") == "{}"
    assert git(rig.a, "rev-parse", "HEAD") == rig.before
    assert git(rig.a, "status", "--porcelain") == ""


def test_object_stream_does_not_inherit_foreign_hook_repository(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    _, target = foreign_repository(tmp_path)
    mapping = rig.ledger.tree(rig.before)
    expected = rig.ledger.blobs(mapping)
    monkeypatch.setenv("GIT_DIR", git(target, "rev-parse", "--absolute-git-dir"))
    with object_reader(rig.a, mapping) as read:
        assert {path: read(path) for path in mapping} == expected


def test_hook_environment_cannot_hide_a_real_wrong_remote(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    foreign, _ = foreign_repository(tmp_path)
    # A caller pointing at a good repository cannot launder a wrong binding.
    monkeypatch.setenv("GIT_DIR", str(rig.a / ".git"))
    with pytest.raises(ValueError, match="authority differs"):
        rig.backend(foreign)


def test_repository_environment_keeps_transport_and_only_core_index_override(monkeypatch):
    from harness_rewrite.git_environment import repository_environment

    for name in ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"]:
        monkeypatch.setenv(name, "caller-repository-context")
    monkeypatch.setenv("GIT_SSH_COMMAND", "registered transport")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/operator/private-provider.conf")
    clean = repository_environment({"GIT_INDEX_FILE": "/core/transaction/index"})
    assert clean["GIT_INDEX_FILE"] == "/core/transaction/index"
    assert clean["GIT_SSH_COMMAND"] == "registered transport"
    assert clean["GIT_CONFIG_GLOBAL"] == "/operator/private-provider.conf"
    assert "caller-repository-context" not in clean.values()
    assert os.environ["GIT_DIR"] == "caller-repository-context"
