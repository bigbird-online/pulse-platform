"""Child launch ordering and crash boundaries, independent of model outcomes."""

import json
import os
from pathlib import Path

import pytest

from harness_rewrite import native_launch


def fixture(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.chdir(root)
    monkeypatch.setenv("HARNESS_REWRITE_DISPATCH", "effect-fixture")
    config = {
        "effect_id": "effect-fixture",
        "candidate_id": "candidate-fixture",
        "native_session_id": "session-fixture",
        "argv": ["/admitted/node", "/admitted/pi", "--session-id", "session-fixture"],
    }
    (root / "launch-config.json").write_text(json.dumps(config))
    (root / "candidate.json").write_text(
        json.dumps({"candidate_id": "candidate-fixture"})
    )
    (root / "launch.json").write_text(
        json.dumps(
            {
                "effect_id": "effect-fixture",
                "candidate_id": "candidate-fixture",
                "phase": "launch_requested",
            }
        )
    )
    # Portable fixture for Linux kernel metadata. Production reads /proc itself.
    monkeypatch.setattr(
        native_launch,
        "process_stamp",
        lambda pid: {
            "pid": pid,
            "start_ticks": "kernel-fixture",
            "boot": "boot-fixture",
        },
    )
    return root, config


def test_child_owns_durable_identity_before_exec_and_never_replaces_it(
    tmp_path, monkeypatch
):
    root, config = fixture(tmp_path, monkeypatch)
    syncs = []
    real_sync = os.fsync

    def synced(fd):
        syncs.append(fd)
        real_sync(fd)

    def exec_failed(executable, argv, environment):
        proof = json.loads((root / "spawn.json").read_text())
        assert proof["process"]["pid"] == os.getpid()
        assert proof["effect_id"] == config["effect_id"]
        assert proof["native_session_id"] == config["native_session_id"]
        assert len(syncs) == 2  # Both proof bytes and directory entry persisted.
        assert executable == argv[0] == config["argv"][0]
        assert environment["HARNESS_REWRITE_DISPATCH"] == config["effect_id"]
        raise OSError("exec failure after durable proof")

    monkeypatch.setattr(native_launch.os, "fsync", synced)
    monkeypatch.setattr(native_launch.os, "execve", exec_failed)
    with pytest.raises(OSError, match="exec failure"):
        native_launch.launch(root)
    original = (root / "spawn.json").read_bytes()
    with pytest.raises(FileExistsError):
        native_launch.launch(root)
    assert (root / "spawn.json").read_bytes() == original


@pytest.mark.parametrize("fault", ["stamp", "fsync", "config", "redirect"])
def test_missing_durable_child_proof_never_starts_native_model(
    tmp_path, monkeypatch, fault
):
    root, config = fixture(tmp_path, monkeypatch)
    executions = []
    monkeypatch.setattr(
        native_launch.os, "execve", lambda *args: executions.append(args)
    )

    def failed(*args):
        raise OSError("injected before exec")

    if fault == "stamp":
        monkeypatch.setattr(native_launch, "process_stamp", failed)
    elif fault == "fsync":
        monkeypatch.setattr(native_launch.os, "fsync", failed)
    elif fault == "config":
        config["candidate_id"] = "different"
        (root / "launch-config.json").write_text(json.dumps(config))
    else:
        (root / "spawn.json").symlink_to(root / "other")
    with pytest.raises(OSError):
        native_launch.launch(root)
    assert not executions
    if fault == "stamp":  # No identity: no proof file at all, not an empty one.
        assert not (root / "spawn.json").exists()


def test_other_working_directory_is_not_a_dispatch_launch(tmp_path, monkeypatch):
    root, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.chdir(root.parent)
    with pytest.raises(OSError, match="directory differs"):
        native_launch.launch(root)
    assert not (Path(root) / "spawn.json").exists()
