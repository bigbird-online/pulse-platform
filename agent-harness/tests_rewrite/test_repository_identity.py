"""Credential isolation across original facts, real Git resources and public discovery."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import pytest

from harness_rewrite.git_workspaces import GitWorkspaces
from harness_rewrite.model import PublicationTarget, RepositoryBinding, SourceConsumer
from harness_rewrite.repository_identity import public_response, remote_identity
from harness_rewrite import policy
from tests_rewrite.test_policy import configured
from tests_rewrite.test_workspaces import setup, allocate, drain, rows, git

PRIVATE = "https://operator:ghp_fixtureCredential@github.com/team/repository.git"
PUBLIC = "https://github.com/team/repository.git"


def test_host_bindings_never_promote_http_credentials_to_business_identity():
    values = [
        RepositoryBinding("repo", "/source", "/source/.git", "/work", PRIVATE),
        PublicationTarget("main", "repo", PRIVATE),
        SourceConsumer("consumer", "repo", "/source", "/source/.git", PRIVATE),
    ]
    assert all(asdict(row)["remote_url"] == PUBLIC for row in values)
    assert (
        remote_identity("git@github.com:team/repository.git")
        == "git@github.com:team/repository.git"
    )
    assert remote_identity("/private/local-remote.git") == "/private/local-remote.git"
    assert remote_identity("https://operator:password@other.example/repository.git") != PUBLIC


def test_policy_discovery_lists_admitted_resources_without_private_authentication(configured):
    _, original, _ = configured
    original["repositories"] = [
        {
            "repo_id": "repo",
            "source": "/source",
            "common_dir": "/source/.git",
            "managed_root": "/work",
            "remote_url": PRIVATE,
        }
    ]
    original["publication_targets"] = [
        {
            "target_id": "repo-main",
            "repo_id": "repo",
            "remote_url": PRIVATE,
        }
    ]
    report = policy.describe(original)
    assert report["repositories"][0]["source"] == "/source"
    assert report["repositories"][0]["remote_url"] == PUBLIC
    assert report["publication_targets"][0]["remote_url"] == PUBLIC
    assert "fixtureCredential" not in json.dumps(report)
    assert original["repositories"][0]["remote_url"] == PRIVATE


def test_historical_output_is_redacted_without_changing_original_facts():
    original = {
        "history": [
            {"remote_url": PRIVATE, "text": "record " + PRIVATE, "token": "github_pat_fixtureOnly"}
        ]
    }
    result = public_response(original)
    assert result["history"][0]["remote_url"] == PUBLIC
    assert result["history"][0]["text"] == "record " + PUBLIC
    assert result["history"][0]["token"] == "[redacted]"
    assert original["history"][0]["remote_url"] == PRIVATE


def test_new_workspace_records_only_repository_identity_and_survives_auth_rotation(setup):
    rig, bindings, _, _ = setup
    row = replace(bindings[0], remote_url=PRIVATE)
    git(Path(row.source), "remote", "add", "origin", PRIVATE)
    rig.contexts["owner"] = replace(rig.contexts["owner"], repositories=(row,))
    result = allocate(setup, request="allocate-safe")
    assert result["status"] == "local_committed"
    drain(setup)
    with rig.kernel._connection(write=False) as conn:
        original = conn.execute(
            "SELECT content FROM journal WHERE request_id='allocate-safe'"
        ).fetchone()[0]
    assert "fixtureCredential" not in str(original)
    git(
        Path(row.source),
        "remote",
        "set-url",
        "origin",
        "https://other:new-token@github.com/team/repository.git",
    )
    retry = allocate(setup, request="allocate-safe")
    assert retry["status"] == "local_committed"
    resource = rows(rig)[0]
    result = rig.send(
        "revise_workspace",
        {
            "resource_id": resource["resource_id"],
            "resources": ["repo"],
            "sparse": ["a"],
            "purpose": "Narrow original work",
        },
    )
    assert result["status"] == "local_committed"
    drain(setup)
    assert rows(rig)[0]["status"] == "observed_success"


def test_legacy_request_and_marker_keep_original_bytes_when_credentials_change(setup):
    rig, bindings, _, _ = setup
    row = replace(bindings[0], remote_url=PUBLIC)
    # Explicit synthetic historical Host: simulate the old credential-bearing binding.
    object.__setattr__(row, "remote_url", PRIVATE)
    git(Path(row.source), "remote", "add", "origin", PRIVATE)
    rig.contexts["owner"] = replace(rig.contexts["owner"], repositories=(row,))
    assert allocate(setup, request="legacy-request")["status"] == "local_committed"
    drain(setup)
    resource = rows(rig)[0]
    marker = (
        Path(row.managed_root) / ".harness-resource-evidence" / (resource["resource_id"] + ".json")
    )
    historical = json.loads(marker.read_bytes())
    historical["binding"]["remote_url"] = PRIVATE
    marker.write_text(json.dumps(historical))
    marker_bytes = marker.read_bytes()
    with rig.kernel._connection(write=False) as conn:
        journal_before = conn.execute(
            "SELECT content FROM journal WHERE request_id='legacy-request'"
        ).fetchone()[0]
    rig.contexts["owner"] = replace(
        rig.contexts["owner"], repositories=(replace(row, remote_url=PUBLIC),)
    )
    git(Path(row.source), "remote", "set-url", "origin", PUBLIC)
    assert allocate(setup, request="legacy-request")["status"] == "local_committed"
    revised = rig.send(
        "revise_workspace",
        {
            "resource_id": resource["resource_id"],
            "resources": ["repo"],
            "sparse": ["b"],
            "purpose": "Continue existing work",
        },
    )
    assert revised["status"] == "local_committed"
    drain(setup)
    assert rows(rig)[0]["status"] == "observed_success"
    assert marker.read_bytes() == marker_bytes
    with rig.kernel._connection(write=False) as conn:
        assert (
            conn.execute(
                "SELECT content FROM journal WHERE request_id='legacy-request'"
            ).fetchone()[0]
            == journal_before
        )
    git(
        Path(row.source),
        "remote",
        "set-url",
        "origin",
        "https://github.com/attacker/repository.git",
    )
    adapter = GitWorkspaces()
    with pytest.raises(OSError, match="remote identity"):
        adapter.layout({"binding": asdict(replace(row, remote_url=PUBLIC)), "target": "probe"})


def test_git_transport_uses_operator_provider_without_putting_credentials_in_url(tmp_path):
    source = tmp_path / "repository"
    source.mkdir()
    git(source, "init", "-q")
    config = tmp_path / "private-git.conf"
    config.write_text(
        '[credential]\n\thelper = "!f() { echo username=fixture; echo password=transport-only; }; f"\n'
    )
    config.chmod(0o600)
    adapter = GitWorkspaces(git_config_file=str(config))
    response = adapter.git(
        source, "credential", "fill", stdin=b"protocol=https\nhost=github.com\n\n"
    )
    assert b"password=transport-only" in response
