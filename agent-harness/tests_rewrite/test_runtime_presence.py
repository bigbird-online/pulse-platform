"""Runtime presence is host evidence, independent from authority and journal age."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from harness_rewrite import Query
from harness_rewrite.model import RuntimeObservation, digest
from harness_rewrite.native_host import NativeHost
from tests_rewrite import test_native_host
from tests_rewrite import test_collaboration
from tests_rewrite.test_workflow import graph, original

rig = test_collaboration.rig
native = test_native_host.native


def process_metadata(proc, *, state="S", ticks="100", boot="original-boot"):
    fields = [state] + ["0"] * 18 + [ticks] + ["0"] * 2
    (proc / "101/stat").write_text("101 (native pi) " + " ".join(fields))
    path = proc / "sys/kernel/random/boot_id"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(boot)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing", "runtime_metadata_unavailable"),
        ("malformed", "runtime_metadata_unavailable"),
        ("reused", "runtime_identity_changed"),
        ("boot", "runtime_identity_changed"),
        ("zombie", "runtime_not_observed_running"),
        ("unsupported", "runtime_metadata_invalid"),
    ],
)
def test_actual_host_refresh_never_turns_missing_or_changed_process_into_death(
    native, change, reason
):
    policy, proc, _, _ = native
    process_metadata(proc)
    host = NativeHost(policy, proc=proc, pid=101)
    before = host.authenticate(host.handle)
    observation = before.runtime_observations[0]
    assert observation.presence == "observed_present"
    assert observation.principal_digest == digest(before.principal())
    assert observation.proof_digest and len(observation.proof_digest) == 64
    if change == "missing":
        (proc / "101/stat").unlink()
    elif change == "malformed":
        (proc / "101/stat").write_text("broken")
    elif change == "reused":
        process_metadata(proc, ticks="101")
    elif change == "boot":
        process_metadata(proc, boot="different-boot")
    elif change == "zombie":
        process_metadata(proc, state="Z")
    else:
        process_metadata(proc, state="?")
    after = host.authenticate(host.handle)
    assert after.allowed_intents == before.allowed_intents
    assert after.principal() == before.principal()
    assert after.runtime_observations[0].presence == "unknown"
    assert after.runtime_observations[0].reason == reason
    assert after.runtime_observations[0].proof_digest is None


def test_no_admission_stamp_cannot_borrow_a_later_pid_and_worker_is_not_pi(native):
    policy, proc, _, _ = native
    host = NativeHost(policy, proc=proc, pid=101)
    process_metadata(proc)
    observation = host.authenticate(host.handle).runtime_observations[0]
    assert observation.presence == "unknown"
    assert observation.reason == "admission_stamp_unavailable"
    host.worker(())
    worker = host.authenticate(host.handle)
    assert observation.principal_digest != digest(worker.principal())
    assert (
        worker.runtime_observations[0].principal_digest == observation.principal_digest
    )


def test_unreadable_metadata_is_unknown_without_losing_native_authority(
    native, monkeypatch
):
    policy, proc, _, _ = native
    process_metadata(proc)
    host = NativeHost(policy, proc=proc, pid=101)
    from harness_rewrite import runtime_presence

    def denied(*args):
        raise PermissionError("private path must not leak")

    monkeypatch.setattr(runtime_presence, "process_stamp", denied)
    result = host.authenticate(host.handle)
    assert result.allowed_intents == host.base.allowed_intents
    assert result.runtime_observations[0].reason == "runtime_metadata_unavailable"
    assert "private path" not in repr(result.runtime_observations)


def test_native_observation_reaches_watch_and_scan_without_writes_or_identity_leak(
    rig, native, tmp_path
):
    policy, proc, _, _ = native
    process_metadata(proc)
    host = NativeHost(policy, proc=proc, pid=101)
    observation = host.authenticate(host.handle).runtime_observations[0]
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    current = rig.contexts["owner"]
    observation = replace(observation, principal_digest=digest(current.principal()))
    rig.contexts["owner"] = replace(current, runtime_observations=(observation,))
    rig.kernel._clock = lambda: datetime.now(timezone.utc).isoformat()
    before = rig.state()
    checksum = hashlib.sha256((tmp_path / "journal.sqlite").read_bytes()).hexdigest()
    response = rig.kernel.inspect(Query(rig.wi, view="watch"), "owner")
    page = response["result"]["items"]
    assert {i["kind"] for i in page["items"]} >= {"execution", "task"}
    for item in page["items"]:
        if item["executor"]:
            executor = item["executor"]
            assert executor["presence"] == "observed_present"
            assert (
                executor["runtime_observation"]["temporal_scope"]
                == "current_observation"
            )
            assert (
                executor["runtime_observation"]["proof_digest"]
                == observation.proof_digest
            )
    assert page["summary"]["executors"] == {
        "total": 1,
        "presence_unknown": 0,
        "observed_present": 1,
    }
    scan = rig.kernel.inspect(Query(view="scan"), "owner")
    assert scan["result"]["items"][0]["watch"]["summary"] == page["summary"]
    historical = rig.kernel.inspect(
        Query(rig.wi, view="watch", revision=before["work_item"]["local_revision"]),
        "owner",
    )
    assert (
        historical["result"]["items"]["items"][0]["executor"]["runtime_observation"][
            "temporal_scope"
        ]
        == "current_observation"
    )
    public = json.dumps([response, scan, historical])
    for secret in (
        "original-boot",
        "test-session",
        "start_ticks",
        str(proc),
        "private-producer-session",
    ):
        assert secret not in public
    assert response["capabilities"] == before["capabilities"]
    assert response["next_action"] == before["next_action"]
    assert (
        hashlib.sha256((tmp_path / "journal.sqlite").read_bytes()).hexdigest()
        == checksum
    )
    assert original(rig.result(attempt))["current"] is True


@pytest.mark.parametrize(
    "case", ["other_session", "future", "naive", "bad_proof", "bad_source", "duplicate"]
)
def test_unusable_observation_never_establishes_presence(rig, case):
    rig.adopt(graph(rig))
    context = rig.contexts["owner"]
    principal = context.principal()
    if case == "other_session":
        principal = {**principal, "session": "another-session"}
    value = RuntimeObservation(
        digest(principal),
        "2026-10-07T00:00:00+00:00",
        "observed_present",
        "admitted_native_process_observed",
        "linux_native_process",
        "a" * 64,
    )
    if case == "future":
        value = replace(value, observed_at="2026-10-09T00:00:00+00:00")
    elif case == "naive":
        value = replace(value, observed_at="2026-10-07T00:00:00")
    elif case == "bad_proof":
        value = replace(value, proof_digest="private path")
    elif case == "bad_source":
        value = replace(value, source="caller supplied alive")
    observations = (value, value) if case == "duplicate" else (value,)
    rig.contexts["owner"] = replace(context, runtime_observations=observations)
    rig.kernel._clock = lambda: "2026-10-08T00:00:00+00:00"
    response = rig.kernel.inspect(Query(rig.wi, view="watch"), "owner")
    assert response["status"] == "read_only"
    executor = response["result"]["items"]["items"][0]["executor"]
    assert executor["presence"] == "unknown"
    assert "private path" not in json.dumps(response)


def without_procfs(tmp_path, monkeypatch, rows):
    """Host without procfs (macOS): ps/sysctl answers come from `rows`, never the host."""
    from harness_rewrite import runtime_presence

    absent = tmp_path / "no-procfs"
    monkeypatch.setattr(runtime_presence, "PROC", absent)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        code, out = rows[tuple(argv)]
        return SimpleNamespace(returncode=code, stdout=out)

    monkeypatch.setattr(runtime_presence.subprocess, "run", run)
    return absent, calls


def test_process_stamp_without_procfs_uses_ps_start_and_boot_session(
    tmp_path, monkeypatch
):
    from harness_rewrite import runtime_presence, session_sources

    started = "Wed Nov 11 15:13:02 2026"
    absent, calls = without_procfs(tmp_path, monkeypatch, {
        ("ps", "-p", "4242", "-o", "lstart="): (0, started + "\n"),
        ("ps", "-p", "4242", "-o", "state="): (0, "Us  \n"),
        ("sysctl", "-n", "kern.bootsessionuuid"): (0, "BOOT-UUID\n"),
        ("ps", "-p", "4343", "-o", "lstart="): (1, ""),
    })
    stamp = runtime_presence.process_stamp(4242, absent)
    assert stamp == {"pid": 4242, "started": started, "boot": "BOOT-UUID"}
    assert runtime_presence.process_stamp(4242, absent) == stamp  # Comparable.
    assert runtime_presence.process_state(4242, absent) == "D"
    assert runtime_presence.boot_identity(absent) == "BOOT-UUID"
    # Relay's macOS source proofs hold {pid, started}; that format is kept.
    assert session_sources.process_identity(4242, absent) == {
        "pid": 4242,
        "started": started,
    }
    with pytest.raises(FileNotFoundError):  # Ended PID, like a missing /proc entry.
        runtime_presence.process_stamp(4343, absent)
    assert all(argv[0] in {"ps", "sysctl"} for argv in calls)


def test_linux_stamp_format_is_unchanged_for_existing_proofs(native):
    from harness_rewrite import runtime_presence

    _, proc, _, _ = native
    process_metadata(proc, ticks="777", boot="linux-boot")
    assert runtime_presence.process_stamp(101, proc) == {
        "pid": 101,
        "start_ticks": "777",
        "boot": "linux-boot",
    }
