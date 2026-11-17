import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from harness_rewrite import cli, policy, release
from harness_rewrite.model import Rejected, digest


@pytest.fixture
def configured(tmp_path):
    root = tmp_path / "lab"
    root.mkdir()
    sessions = tmp_path / "private-native-sessions"
    sessions.mkdir()
    p = {
        "namespace": "fixture",
        "surface": "local-fixture",
        "database": str(root / "state/facts.sqlite"),
        "views_root": str(root / "views"),
        "delivery_root": str(root / "delivery"),
        "dispatch_root": str(root / "dispatch"),
        "verification_actor": "verifier",
        "python": sys.executable,
        "node": sys.executable,
        "pi_binary": sys.executable,
        "profiles": [
            {
                "role": "producer",
                "actor": "private-producer",
                "session_root": str(sessions),
                "artifact_roots": [str(root / "work")],
            },
            {
                "role": "verifier",
                "actor": "verifier",
                "session_root": str(root / "dispatch"),
                "artifact_roots": [str(root / "dispatch")],
            },
        ],
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(p))
    return root, p, path


def snapshot(path):
    return {
        str(p.relative_to(path)): (p.read_bytes() if p.is_file() else None)
        for p in path.rglob("*")
    }


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p.update(python="/missing-runtime"),
        lambda p: p.update(dispatch_mode="automatic"),
        lambda p: p.update(verification_compute={}),
        lambda p: p.update(
            verification_compute={**policy.verification_compute({}), "tools": ["all"]}
        ),
        lambda p: p["profiles"][1].update(role="producer"),
        lambda p: p["profiles"][1].update(actor="private-producer"),
        lambda p: p["profiles"][0].update(allowed_tools=["bash"]),
        lambda p: p["profiles"][0].update(artifact_roots=[]),
        lambda p: p["profiles"][0].update(artifact_roots=[False]),
        lambda p: p["profiles"][0].update(session_root=p["dispatch_root"]),
        lambda p: p["profiles"][0].update(
            session_root=str(Path(p["dispatch_root"]).parent)
        ),
        lambda p: p.update(repositories=[{}]),
        lambda p: p.update(source_consumers=True),
        lambda p: p.update(migration_peers=[{}]),
        lambda p: p.update(namespace=False),
        lambda p: p.update(views_root="relative-path"),
    ],
)
def test_bad_policy_install_rejects_before_any_directory(configured, monkeypatch, edit):
    root, p, path = configured
    edit(p)
    path.write_text(json.dumps(p))
    bundle = path.parent / "candidate.pyz"
    bundle.write_bytes(b"unused transport fixture")
    monkeypatch.setattr(
        release, "verify", lambda _: {"writer_contract": 3, "location_contract": 2}
    )
    before = snapshot(root)
    with pytest.raises((ValueError, Rejected)):
        release.install(bundle, release.sha(bundle.read_bytes()), root, path)
    assert snapshot(root) == before == {}


@pytest.mark.parametrize(
    "payload",
    [
        b'{"namespace":"one","namespace":"two"}',
        b"[]",
        b"null",
        b'{"python":NaN}',
        b'{"python":Infinity}',
        b'{"profiles":[{"actor":"a","actor":"b"}]}',
        b"\xff",
        b" " * (policy.MAX_BYTES + 1),
    ],
)
def test_bounded_strict_json(configured, payload):
    _, _, path = configured
    path.write_bytes(payload)
    with pytest.raises((ValueError, Rejected)):
        policy.load(path)


def test_policy_file_cannot_redirect(configured):
    _, _, path = configured
    link = path.with_name("link.json")
    link.symlink_to(path)
    with pytest.raises(OSError):
        policy.load(link)


@pytest.mark.parametrize(
    "kind", ["redirect", "file-directory", "directory-database", "outside"]
)
def test_existing_physical_conflicts_preserve_all_bytes(configured, kind):
    root, p, path = configured
    if kind == "redirect":
        (root / "views").symlink_to(path.parent)
    elif kind == "file-directory":
        (root / "views").write_bytes(b"preserve user file")
    elif kind == "directory-database":
        (root / "state/facts.sqlite").mkdir(parents=True)
    else:
        p["views_root"] = str(path.parent / "outside")
    path.write_text(json.dumps(p))
    with pytest.raises((ValueError, Rejected)):
        policy.review(root, path)
    assert not (root / "active").exists() and not (root / "releases").exists()
    if kind == "file-directory":
        assert (root / "views").read_bytes() == b"preserve user file"


def test_readonly_preview_exact_diff_and_migration_boundary(configured):
    root, p, path = configured
    before = snapshot(path.parent)
    report = policy.review(root, path, path)
    assert report["policy_sha256"] == digest(p)
    assert report["comparison"]["changed_sections"] == []
    assert report["comparison"]["permission_direction"] == "unchanged"
    assert snapshot(path.parent) == before
    assert report["validation"] == "current_preflight"
    assert report["read_only"] and not report["execution_authority"]
    public = json.dumps(report)
    for private in [
        str(root),
        "private-producer",
        "private-native-sessions",
        sys.executable,
    ]:
        assert private not in public
    changed = copy.deepcopy(p)
    changed["profiles"][0]["artifact_roots"].append(str(root / "additional-artifacts"))
    proposed = path.with_name("proposed.json")
    proposed.write_text(json.dumps(changed))
    before = snapshot(path.parent)
    diff = policy.review(root, proposed, path)["comparison"]
    assert diff["changed_sections"] == ["profiles"]
    assert diff["requires_migration"] == [] and diff["operator_review_required"]
    assert diff["permission_direction"] == "not_inferred"
    assert snapshot(path.parent) == before
    changed["namespace"] = "another-namespace"
    proposed.write_text(json.dumps(changed))
    diff = policy.review(root, proposed, path)["comparison"]
    assert diff["requires_migration"] == ["namespace"]
    with pytest.raises(ValueError, match="use migration"):
        policy.compatible(p, changed)
    assert not list(root.iterdir())


def test_native_policy_view_has_no_database_or_worker(configured, monkeypatch, capsys):
    root, p, _ = configured
    monkeypatch.setattr(cli, "NativeHost", lambda _: SimpleNamespace(policy=p))

    def forbidden(*args, **kwargs):
        raise AssertionError("Policy view must not construct business I/O")

    monkeypatch.setattr(cli, "Kernel", forbidden)
    assert cli.main(["inspect", "--view", "policy"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == policy.describe(p)
    assert report["validation"] == "installed_policy" and not list(root.iterdir())


@pytest.mark.parametrize(
    "args",
    [
        ["inspect", "--view", "policy", "--limit", "100"],
        ["inspect", "--view", "policy", "--after=0"],
        ["inspect", "--view", "policy", "--start-byte", "0"],
        ["inspect", "--view", "policy", "--input", "unread.json"],
        ["identity", "--view", "policy"],
        ["resume", "--view", "policy"],
        ["inspect", "--view", "policy", "--wi", "wi_fixture"],
        ["inspect", "--view", "policy", "--limit", "1"],
        ["inspect", "--view", "policy", "--revision", "1"],
        ["inspect", "--view", "policy", "--owner", "someone"],
        ["inspect", "--view", "policy", "--status", "active"],
        ["inspect", "--view", "policy", "--text", "q"],
        ["inspect", "--view", "policy", "--after", "1"],
        ["inspect", "--view", "policy", "--start-byte", "1"],
        ["inspect", "--view", "policy", "--age-threshold-seconds", "1"],
        ["inspect", "--view", "policy", "--artifact-ref", "file.json"],
    ],
)
def test_native_policy_rejects_unrelated_options(args, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid option cannot reach a native Host")

    monkeypatch.setattr(cli, "NativeHost", forbidden)
    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


@pytest.mark.parametrize("action", ["policy-inspect", "policy-diff"])
def test_management_entry_is_readonly(configured, monkeypatch, capsys, action):
    root, _, path = configured
    argv = ["release", action, "--root", str(root), "--policy", str(path)]
    if action == "policy-diff":
        argv += ["--against", str(path)]
    monkeypatch.setattr(sys, "argv", argv)
    before = snapshot(path.parent)
    release.main()
    assert json.loads(capsys.readouterr().out)["validation"] == "current_preflight"
    assert snapshot(path.parent) == before


def test_already_visible_identity_change_rejects_before_new_preparation(
    configured, monkeypatch
):
    root, p, path = configured
    previous = root / "releases" / "original"
    previous.mkdir(parents=True)
    (previous / "host-policy.json").write_text(json.dumps(p))
    (root / "active").symlink_to(previous)
    before = snapshot(root)
    p["namespace"] = "other"
    p["profiles"][0]["artifact_roots"].append(str(root / "new-artifacts"))
    path.write_text(json.dumps(p))
    bundle = path.with_name("candidate.pyz")
    bundle.write_bytes(b"unchanged bytes")
    monkeypatch.setattr(
        release, "verify", lambda _: {"writer_contract": 3, "location_contract": 2}
    )
    with pytest.raises(ValueError, match="use migration"):
        release.install(bundle, release.sha(bundle.read_bytes()), root, path)
    assert snapshot(root) == before


def test_deeply_nested_json_rejects_cleanly(configured):
    _, _, path = configured
    path.write_text("[" * 3000 + "0" + "]" * 3000)
    with pytest.raises(ValueError):
        policy.load(path)


def test_one_actor_can_use_disjoint_original_channel_logs(configured):
    root, p, _ = configured
    p["profiles"].append({**p["profiles"][0], "session_root": str(root / "feishu")})
    policy.shape(p)
    p["profiles"][-1]["session_root"] = p["profiles"][0]["session_root"]
    with pytest.raises(ValueError, match="overlap"):
        policy.shape(p)


def test_profile_rows_cover_a_full_relay_host_and_stay_bounded(configured):
    root, p, _ = configured
    producer = next(row for row in p["profiles"] if row["role"] == "producer")
    p["profiles"] += [
        {**producer, "session_root": str(root / f"tenant-{i}")}
        for i in range(512 - len(p["profiles"]))
    ]
    policy.shape(p)
    p["profiles"].append({**producer, "session_root": str(root / "tenant-512")})
    with pytest.raises(ValueError, match="exceed their bound"):
        policy.shape(p)

