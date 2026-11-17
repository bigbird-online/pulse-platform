"""Real runner + synthetic SDK. This proves continuation, never a human verdict."""

import json
import os
import subprocess

import pytest

from harness_rewrite.native_compute import launch_config, continuation_config, session_proof
from harness_rewrite.verifier_recovery import recover
from harness_rewrite.pi_dispatch import PiDispatch
from tests_rewrite.test_compute_selection import sdk_fixture
from tests_rewrite.test_dispatch import configured


def interrupted(tmp_path):
    rig, candidate, worker = configured(tmp_path)
    effect = rig.state(handle=worker(()))["effects"][0]
    dispatch = tmp_path / "dispatch"
    dispatch.mkdir()
    root = dispatch / effect["effect_id"]
    config = sdk_fixture(root, "model-error")
    config.update(
        effect_id=effect["effect_id"],
        candidate_id=candidate,
        native_session_id=effect["native_session_id"],
    )
    (root / "launch-config.json").write_text(json.dumps(config))
    (root / "launch.json").write_text(
        json.dumps(
            {
                "effect_id": effect["effect_id"],
                "candidate_id": candidate,
                "phase": "launch_requested",
            }
        )
    )
    (root / "candidate.json").write_text(json.dumps({"candidate_id": candidate}))
    run = subprocess.run(
        config["argv"],
        cwd=root,
        env={**os.environ, "HARNESS_REWRITE_DISPATCH": effect["effect_id"]},
        capture_output=True,
        timeout=20,
    )
    assert run.returncode == 2
    (root / "spawn.json").write_text(
        json.dumps(
            {
                "effect_id": effect["effect_id"],
                "native_session_id": effect["native_session_id"],
                "process": {"pid": 42, "start_ticks": "123", "boot": "old-boot"},
            }
        )
    )
    proc = tmp_path / "proc"
    boot = proc / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("new-boot")
    policy = {
        "dispatch_root": str(dispatch),
        "delivery_root": str(tmp_path),
        "python": "/canonical/python",
        "agent_config_dir": config["agent_dir"],
    }
    adapter = PiDispatch(rig.kernel, policy, worker, rig.wi, tmp_path / "bundle.pyz")
    return rig, effect, adapter, root, proc


def test_resume_retains_assignment_session_original_proofs_and_does_not_double_launch(
    tmp_path, monkeypatch
):
    rig, effect, adapter, root, proc = interrupted(tmp_path)
    before = {p.name: p.read_bytes() for p in root.glob("*.json")}
    session = next((root / "session").glob("*.jsonl"))
    prefix = session.read_bytes()
    launches = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        "harness_rewrite.verifier_recovery.subprocess.Popen",
        lambda *a, **k: launches.append((a, k)),
    )
    results = recover(adapter, proc=proc)
    assert results[0]["state"] == "continuation_requested"
    assert results[0]["native_session_id"] == effect["native_session_id"]
    assert len(launches) == 1
    assert (
        recover(adapter, proc=proc)[0]["reason"] == "original_launch_result_unconfirmed"
    )  # The normally scheduled wrapper gets time to record its native claim.
    assert len(launches) == 1
    config = continuation_config(root, "000001")
    # Run the real new runner through the SDK double; it must OPEN, never create.
    monkeypatch.setattr("harness_rewrite.verifier_recovery.subprocess.Popen", real_popen)
    actual = subprocess.run(
        config["argv"],
        cwd=root,
        env={
            **os.environ,
            "HARNESS_REWRITE_DISPATCH": effect["effect_id"],
            "HARNESS_REWRITE_ATTEMPT": "000001",
        },
        capture_output=True,
        timeout=20,
    )
    assert actual.returncode == 2  # The test SDK still reports a model failure.
    result = json.loads((root / "attempts/000001/runner-result.json").read_bytes())
    assert result["phase"] == "model_interrupted" and result["model_invoked"]
    assert session.read_bytes().startswith(prefix)
    assert len(list((root / "session").glob("*.jsonl"))) == 1
    session_proof(root, session, launch_config(root))
    assert all((root / name).read_bytes() == raw for name, raw in before.items())
    assert (
        rig.state()["result"]["current_observation"]["candidates"]["items"][0]["verification"]
        is None
    )


def test_live_original_process_is_not_restarted(tmp_path, monkeypatch):
    rig, effect, adapter, root, proc = interrupted(tmp_path)
    monkeypatch.setattr("harness_rewrite.verifier_recovery.presence", lambda *a: "running")
    monkeypatch.setattr(
        "harness_rewrite.verifier_recovery.subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
    )
    assert recover(adapter, proc=proc)[0]["state"] == "running"
    assert not list((root / "attempts").iterdir())


def test_unconfirmed_launch_can_recover_after_a_proven_new_boot(tmp_path, monkeypatch):
    _, _, adapter, root, proc = interrupted(tmp_path)
    launches = []
    monkeypatch.setattr(
        "harness_rewrite.verifier_recovery.subprocess.Popen",
        lambda *a, **k: launches.append((a, k)),
    )
    assert recover(adapter, proc=proc)[0]["state"] == "continuation_requested"
    marker = root / "attempts/000001/launch.json"
    original = json.loads(marker.read_bytes())
    original["at"] = "2026-01-01T00:00:00+00:00"
    marker.write_text(json.dumps(original))
    assert recover(adapter, proc=proc)[0]["attempt"] == "000002"
    (proc / "sys/kernel/random/boot_id").write_text("another-proven-boot")
    assert recover(adapter, proc=proc)[0]["reason"] == "original_launch_result_unconfirmed"
    assert len(launches) == 2
    assert json.loads(marker.read_bytes()) == original


def test_partial_attempt_directory_and_original_missing_spawn_are_recoverable(tmp_path, monkeypatch):
    _, effect, adapter, root, proc = interrupted(tmp_path)
    (root / 'spawn.json').unlink()  # Fixture: wrapper never reached its claim.
    attempts = root / 'attempts'; attempts.mkdir()
    partial = attempts / '000001'; partial.mkdir()
    (partial / 'unfinished-runner').write_text('crash during preparation')
    calls = []
    monkeypatch.setattr('harness_rewrite.verifier_recovery.subprocess.Popen',
                        lambda *a, **k: calls.append((a, k)))
    assert recover(adapter, proc=proc)[0]['attempt'] == '000002'
    fence = json.loads((partial / 'spawn.json').read_bytes())
    assert fence['schema'] == 'harness.verifier_launch_fence/v1'
    assert 'process' not in fence and len(calls) == 1
    import pytest
    with pytest.raises(FileExistsError):  # A late wrapper cannot claim/exec.
        (partial / 'spawn.json').open('xb')
    assert (partial / 'unfinished-runner').read_text() == 'crash during preparation'


def test_never_spawned_assignment_can_start_its_first_original_session(tmp_path, monkeypatch):
    _, _, adapter, root, proc = interrupted(tmp_path)
    (root / 'spawn.json').unlink()
    for p in (root / 'session').glob('*.jsonl'):
        p.unlink()  # Fixture: no process ever reached session creation.
    marker = root / 'launch.json'
    os.utime(marker, (1, 1))
    original_popen = subprocess.Popen
    monkeypatch.setattr('harness_rewrite.verifier_recovery.subprocess.Popen', lambda *a, **k: None)
    assert recover(adapter, proc=proc)[0]['attempt'] == '000001'
    config = continuation_config(root, '000001')
    assert config['session_file'] is None
    monkeypatch.setattr('harness_rewrite.verifier_recovery.subprocess.Popen', original_popen)
    run = subprocess.run(config['argv'], cwd=root, capture_output=True, timeout=20,
        env={**os.environ, 'HARNESS_REWRITE_DISPATCH': config['effect_id'],
             'HARNESS_REWRITE_ATTEMPT': '000001'})
    assert run.returncode == 2  # Synthetic SDK reports a model error after creation.
    session = next((root / 'session').glob('*.jsonl'))
    session_proof(root, session, launch_config(root))


def test_empty_spawn_proof_is_a_pre_exec_failure_not_an_error(tmp_path, monkeypatch):
    _, _, adapter, root, proc = interrupted(tmp_path)
    (root / 'spawn.json').write_bytes(b'')  # Older wrapper failed before writing.
    for p in (root / 'session').glob('*.jsonl'):
        p.unlink()  # The wrapper never exec'd, so no session exists.
    launches = []
    monkeypatch.setattr('harness_rewrite.verifier_recovery.subprocess.Popen',
                        lambda *a, **k: launches.append((a, k)))
    # The fixture has procfs: a paused older wrapper could still exec, so wait.
    held = recover(adapter, proc=proc)[0]
    assert held['reason'] == 'original_launch_result_unconfirmed' and not launches
    monkeypatch.setattr('harness_rewrite.native_compute.procfs_absent', lambda proc: True)
    assert recover(adapter, proc=proc)[0]['attempt'] == '000001'
    marker = json.loads((root / 'attempts/000001/launch.json').read_bytes())
    assert marker['previous_launch_failed_before_exec'] is True
    assert marker['previous_process'] is None and marker['boot'] == 'new-boot'
    assert continuation_config(root, '000001')['session_file'] is None
    assert (root / 'spawn.json').read_bytes() == b''  # Original evidence kept.
    assert len(launches) == 1


def test_exclusive_proof_appears_whole_or_not_at_all(tmp_path):
    from harness_rewrite.native_compute import write_exclusive
    target = tmp_path / 'spawn.json'
    write_exclusive(target, b'{"a": 1}')
    assert target.read_bytes() == b'{"a": 1}'
    with pytest.raises(FileExistsError):
        write_exclusive(target, b'{"b": 2}')
    assert target.read_bytes() == b'{"a": 1}' and sorted(p.name for p in tmp_path.iterdir()) == ['spawn.json']
