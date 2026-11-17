"""Bounded source -> existing ledger -> query. Synthetic sources, not channel UAT."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import LedgerError, UsageLedger
from pulse_compute_cost.receipt_sources import read_pi
from pulse_compute_cost.receipts import ReceiptError, reference, canonical
from pulse_compute_cost.stage_record import source_projection, record_stage
from pulse_compute_cost.native_stage_host import NativeStageHost

AT = "2026-10-10T01:00:00Z"
OBSERVED = "2026-10-10T02:00:00Z"
WI = "wi_20261008_550"
HEADER = {"type": "session", "id": "synthetic-private-session"}
USER = {"type": "message", "id": "u1", "timestamp": AT,
        "message": {"role": "user", "content": "private raw user utterance"}}
ASSISTANT = {"type": "message", "id": "a1", "timestamp": AT, "parentId": "u1",
    "message": {"role": "assistant", "content": "private raw answer", "model": "test-model",
        "provider": "synthetic", "usage": {"input": 10, "output": 5, "cacheRead": 20,
        "cacheWrite": 0, "totalTokens": 35, "reasoning": 2}}}


def encoded(rows):
    return b"".join(canonical(r) + b"\n" for r in rows)


def capture(rows=None, principal="producer", start=0):
    rows = [USER, ASSISTANT] if rows is None else rows
    raw = encoded(([HEADER] if start == 0 else []) + rows)
    return source_projection(Path("/synthetic/session.jsonl"), HEADER, raw, start=start,
        end=start + len(raw), principal=principal, observed_at=OBSERVED)


def command(source, **kwargs):
    result = {"source": source["range"], "context_ref": None, "changes": [{
        "key": "goal", "kind": "stage_goal", "at": 0, "activities": [0], "supersedes": None,
        "payload": {"name": "协作计量", "desired_change": "接续不用重复解释",
            "expected": [{"category": "skill", "unit": "capability", "comparison_basis": "usable-method",
                "quantity": "1", "baseline": None, "criterion": "原入口正常恢复"}],
            "input_scope": "有界活动及已知用量", "unknowns": ["主动人时未核对"]}}]}
    result.update(kwargs)
    return result


def link(source, context, targets):
    return command(source, context_ref=context, changes=[{"key": "association", "kind": "stage_link",
        "at": 0, "activities": [0], "supersedes": None,
        "payload": {"wi": WI, "state": "assigned", "basis": "本次明确关联这些事实",
                    "target_refs": targets}}])


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / "ledger", clock=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))


def query(ledger, **kwargs):
    return query_collaboration(ledger, CollaborationQuery(**kwargs))


def test_pre_wi_record_then_exact_link_preserves_quantity_time_and_old_watermark(ledger):
    source = capture()
    first = record_stage(ledger, source, command(source))
    before = query(ledger, watermark=first["watermark"])
    assert first["execution_authority"] is False
    assert first["status"] == "saved"
    assert before["stages"]["goals"][0]["wi"] is None
    assert before["summary"]["usage"]["known_total_tokens"] == 35
    goal = first["changes"][0]
    source2 = capture([{**USER, "id": "u2", "timestamp": OBSERVED}])
    record_stage(ledger, source2, link(source2, first["context_ref"],
        [goal["entity_ref"], source["usage"][0].entity_ref]))
    after = query(ledger, work_items=(WI,))
    assert after["summary"]["usage"]["known_total_tokens"] == 35
    assert after["stages"]["goals"][0]["occurred_at"] == AT
    assert query(ledger, watermark=first["watermark"]) == before


def test_normal_collector_and_stage_capture_share_native_invocation_identity(ledger):
    source = capture()
    record_stage(ledger, source, command(source))
    ledger.ingest_receipts(read_pi(Path("/synthetic/session.jsonl"), OBSERVED,
        data=encoded([HEADER, USER, ASSISTANT])))
    assert query(ledger)["summary"]["usage"]["known_total_tokens"] == 35
    assert len([r for r in ledger.receipt_snapshot()["facts"] if r.kind == "usage"]) == 1


def test_replay_restart_and_lost_reply_keep_same_facts(ledger):
    source = capture()
    first = record_stage(ledger, source, command(source))
    later = deepcopy(source)
    later["observed_at"] = "2026-10-10T03:00:00Z"
    again = record_stage(UsageLedger(ledger.root), later, command(later))
    assert again["inserted"] == 0
    assert again["watermark"] == first["watermark"]
    assert again["changes"] == first["changes"]


def test_real_retry_is_another_native_invocation(ledger):
    source = capture()
    record_stage(ledger, source, command(source))
    retried = capture([{**ASSISTANT, "id": "a2"}], start=100)
    record_stage(ledger, retried, command(retried, changes=[]))
    assert query(ledger)["summary"]["usage"]["known_total_tokens"] == 70


def test_private_source_is_not_copied_to_facts_or_response(ledger):
    source = capture()
    response = record_stage(ledger, source, command(source))
    public = canonical(response) + b"".join(p.read_bytes() for p in (ledger.root / "receipts").glob("*.json"))
    for private in [b"synthetic-private-session", b"private raw", b"/synthetic/"]:
        assert private not in public
    assert response["coverage"] == "bounded_source_only"


def test_atomic_failure_is_retryable_with_original_command(ledger):
    source = capture()
    with patch("pulse_compute_cost.ledger.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(LedgerError):
            record_stage(ledger, source, command(source))
    assert ledger.receipt_snapshot()["facts"] == []
    assert record_stage(ledger, source, command(source))["inserted"] == 2


def test_correction_preserves_original_and_requires_exact_predecessor(ledger):
    source = capture()
    original = record_stage(ledger, source, command(source))
    changed = command(source, context_ref=original["context_ref"])
    changed["changes"][0]["supersedes"] = original["changes"][0]["fact_ref"]
    changed["changes"][0]["payload"]["desired_change"] = "改进同一事务的接续"
    record_stage(ledger, source, changed)
    assert query(ledger)["stages"]["goals"][0]["desired_change"] == "改进同一事务的接续"
    assert len([r for r in ledger.receipt_snapshot()["facts"] if r.kind == "stage_goal"]) == 2


def test_cross_session_context_continues_but_does_not_copy_old_usage(ledger):
    source = capture()
    saved = record_stage(ledger, source, command(source))
    different = source_projection(Path("/synthetic/another.jsonl"), {**HEADER, "id": "another-session"},
        encoded([{**HEADER, "id": "another-session"}, USER]), start=0,
        end=len(encoded([{**HEADER, "id": "another-session"}, USER])), principal="successor", observed_at=OBSERVED)
    request = command(different, context_ref=saved["context_ref"])
    request["changes"][0]["key"] = "successor-observation"
    record_stage(ledger, different, request)
    assert query(ledger)["summary"]["usage"]["known_total_tokens"] == 35


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(actor="someone"),
    lambda c: c.update(ledger_root="/other"),
    lambda c: c.update(context_ref=reference("unknown", 1)),
    lambda c: c["changes"][0].update(key="ghp_" + "x" * 40),
    lambda c: c["changes"][0].update(at=True),
    lambda c: c["changes"][0].update(at=20),
    lambda c: c["changes"][0].update(activities=[]),
    lambda c: c["changes"][0].update(activities=[0, 0]),
    lambda c: c["changes"][0].update(supersedes=reference("absent", 1)),
    lambda c: c["changes"][0]["payload"].update(epistemic="human_confirmed"),
    lambda c: c["changes"][0]["payload"].update(execution_grant=True),
    lambda c: c["source"].update(end_byte=2),
])
def test_reject_invalid_or_authority_bearing_input_without_writes(ledger, mutation):
    source = capture()
    request = deepcopy(command(source))
    mutation(request)
    with pytest.raises(ReceiptError):
        record_stage(ledger, source, request)
    assert not ledger.root.exists()


def test_successor_correction_preserves_original_author_instead_of_impersonating(ledger):
    source = capture()
    saved = record_stage(ledger, source, command(source))
    successor = capture(principal="successor")
    request = command(successor, context_ref=saved["context_ref"])
    request["changes"][0]["supersedes"] = saved["changes"][0]["fact_ref"]
    request["changes"][0]["payload"]["desired_change"] = "后继提出明确修正"
    record_stage(ledger, successor, request)
    goals = [r for r in ledger.receipt_snapshot()["facts"] if r.kind == "stage_goal"]
    assert len(goals) == 2
    assert goals[0].entity_ref == goals[1].entity_ref
    assert goals[0].producer != goals[1].producer
    assert goals[1].supersedes == goals[0].fact_ref


def test_unknown_link_targets_are_not_saved(ledger):
    source = capture()
    saved = record_stage(ledger, source, command(source))
    with pytest.raises(ReceiptError):
        record_stage(ledger, source, link(source, saved["context_ref"], [reference("absent", 1)]))


def test_source_rejects_changed_header_and_duplicate_native_ids():
    with pytest.raises(ReceiptError):
        source_projection(Path("unused"), HEADER, encoded([{**HEADER, "id": "changed"}, USER]),
            start=0, end=len(encoded([{**HEADER, "id": "changed"}, USER])),
            principal="producer", observed_at=OBSERVED)
    with pytest.raises(ReceiptError):
        capture([USER, {**USER, "message": {"role": "user", "content": "changed"}}])


def test_partial_native_fields_remain_unknown(ledger):
    partial = deepcopy(ASSISTANT)
    partial["message"]["usage"] = {"output": 5}
    source = capture([USER, partial])
    record_stage(ledger, source, command(source))
    usage = [r for r in ledger.receipt_snapshot()["facts"] if r.kind == "usage"][0]
    assert usage.payload["tokens"]["uncached_input"] is None
    assert usage.payload["tokens"]["output"] == 5


def test_native_registry_uses_admitted_parent_not_caller_env(tmp_path, monkeypatch):
    (tmp_path / "ledger").mkdir(mode=0o700)
    registry = tmp_path / "sources.json"
    registry.write_text(json.dumps({"schema": "computecost_source_registry/v1", "sources": [],
        "required_receipt_features": ["stage_semantics.v1"], "ledger_root": str(tmp_path / "ledger")}))
    registry.chmod(0o600)
    proc = tmp_path / "proc" / "101"
    proc.mkdir(parents=True)
    (proc / "environ").write_bytes(b"PULSE_COMPUTECOST_SOURCES=" + str(registry).encode() + b"\0")
    monkeypatch.setenv("PULSE_COMPUTECOST_SOURCES", str(tmp_path / "caller-forgery"))
    host = SimpleNamespace(proc=proc.parent, native_pid=101, profile={"role": "producer"},
                           execution={"mode": "automatic"})
    adapter = NativeStageHost(host, None)
    assert adapter.ledger().root == tmp_path / "ledger"
    original = registry.read_bytes()
    registry.write_text(json.dumps({"schema": "computecost_source_registry/v1", "sources": [],
                                    "ledger_root": str(tmp_path / "ledger")}))
    with pytest.raises(ReceiptError):
        adapter.ledger()  # An old consumer registry must not silently get new kinds.
    registry.write_bytes(original)
    registry.chmod(0o644)
    with pytest.raises(ReceiptError):
        adapter.ledger()
    registry.chmod(0o600)
    (proc / "environ").write_bytes(b"")
    with pytest.raises(ReceiptError, match="registered"):
        adapter.ledger()


def procargs(argv, environment, apple=(b"executable_path=/x",)):
    """A synthetic KERN_PROCARGS2 block: argc, exec path, padding, argv, env, apple."""
    return (len(argv).to_bytes(4, sys.byteorder) + b"/native/exec\0\0\0\0"
            + b"".join(a + b"\0" for a in argv) + b"".join(e + b"\0" for e in environment)
            + b"\0\0\0" + b"".join(a + b"\0" for a in apple))


def test_macos_registry_reads_admitted_parent_procargs_not_caller_env(tmp_path, monkeypatch):
    from pulse_compute_cost import native_stage_host
    (tmp_path / "ledger").mkdir(mode=0o700)
    registry = tmp_path / "sources.json"
    registry.write_text(json.dumps({"schema": "computecost_source_registry/v1", "sources": [],
        "required_receipt_features": ["stage_semantics.v1"], "ledger_root": str(tmp_path / "ledger")}))
    registry.chmod(0o600)
    absent = tmp_path / "no-procfs"  # Stands in for /proc on macOS.
    monkeypatch.setattr(native_stage_host, "PROCFS", absent)
    entry = b"PULSE_COMPUTECOST_SOURCES="
    block = {"value": procargs([b"claude", entry + b"/argv-forgery"], [b"A=1", entry + str(registry).encode()],
                               apple=(entry + b"/apple-forgery",))}
    seen = []
    def read(pid):
        seen.append(pid)
        if isinstance(block["value"], Exception):
            raise block["value"]
        return block["value"]
    monkeypatch.setattr(native_stage_host, "_darwin_procargs", read)
    monkeypatch.setenv("PULSE_COMPUTECOST_SOURCES", str(tmp_path / "caller-forgery"))
    host = SimpleNamespace(proc=absent, native_pid=101, profile={"role": "producer"},
                           execution={"mode": "automatic"})
    adapter = NativeStageHost(host, None)
    assert adapter.ledger().root == tmp_path / "ledger"
    assert seen and set(seen) == {101}
    for value in (procargs([b"claude"], [b"A=1"]),  # Parent launched without the binding.
                  procargs([b"claude"], [entry + str(registry).encode()] * 2),
                  procargs([b"claude", entry + str(registry).encode()], [b"A=1"]),  # Only argv names it.
                  b"\x05\0\0\0/native/exec\0claude\0",  # Truncated block.
                  OSError(22, "sysctl")):  # Ended or another account's process.
        block["value"] = value
        with pytest.raises(ReceiptError, match="registered"):
            adapter.ledger()
    host.native_pid = None
    block["value"] = procargs([b"claude"], [entry + str(registry).encode()])
    with pytest.raises(ReceiptError, match="registered"):
        adapter.ledger()


@pytest.mark.skipif(sys.platform != "darwin", reason="real KERN_PROCARGS2 exists only on macOS")
def test_macos_launch_environment_reads_real_same_account_process():
    import subprocess
    import time
    from pulse_compute_cost.native_stage_host import PROCFS, launch_environment
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)",
                              "PULSE_COMPUTECOST_SOURCES=/argv-forgery"],
                             env={"PULSE_COMPUTECOST_SOURCES": "/launch-binding", "A": "1"})
    try:
        for _ in range(50):
            entries = launch_environment(child.pid, PROCFS)
            if b"A=1" in entries:
                break
            time.sleep(0.05)
        assert sorted(entries) == [b"A=1", b"PULSE_COMPUTECOST_SOURCES=/launch-binding"]
    finally:
        child.kill()
        child.wait()
    with pytest.raises(OSError):
        launch_environment(child.pid, PROCFS)


@pytest.mark.parametrize("role,mode", [("verifier", "automatic"), ("producer", "audit")])
def test_native_saving_does_not_expand_audit_or_verifier_rights(role, mode):
    with pytest.raises(ReceiptError):
        NativeStageHost(SimpleNamespace(profile={"role": role}, execution={"mode": mode}), None).record({})


def test_empty_save_does_not_claim_human_activity_was_persisted(ledger):
    source = capture([USER])
    with pytest.raises(ReceiptError, match="no declaration"):
        record_stage(ledger, source, command(source, changes=[]))
    assert not ledger.root.exists()


def test_source_window_does_not_inherit_neighbouring_transactions(ledger):
    first, other = capture(), capture([{**ASSISTANT, "id": "other"}], start=100)
    saved = record_stage(ledger, first, command(first))
    record_stage(ledger, other, command(other, changes=[]))
    record_stage(ledger, first, link(first, saved["context_ref"], [first["usage"][0].entity_ref]))
    assert query(ledger)["summary"]["usage"]["known_total_tokens"] == 70
    assert query(ledger, work_items=(WI,))["summary"]["usage"]["known_total_tokens"] == 35


def test_concurrent_snapshot_change_does_not_silently_rebase_declarations(ledger):
    source = capture()
    real_ingest = ledger.ingest_receipts
    def concurrent(rows, *, expected_watermark):
        real_ingest(source["usage"])
        return real_ingest(rows, expected_watermark=expected_watermark)
    with patch.object(ledger, "ingest_receipts", side_effect=concurrent):
        with pytest.raises(LedgerError, match="snapshot changed"):
            record_stage(ledger, source, command(source))
    assert all(r.kind == "usage" for r in ledger.receipt_snapshot()["facts"])
    assert record_stage(ledger, source, command(source))["inserted"] == 1


def test_cli_routes_to_host_without_identity_or_destination_flags(tmp_path, monkeypatch, capsys, ledger):
    from pulse_compute_cost import cli, native_stage_host
    source = capture()
    request = tmp_path / "command.json"
    request.write_bytes(canonical(command(source)))
    fake = SimpleNamespace(record=lambda c: record_stage(ledger, source, c), capture=lambda r: source)
    monkeypatch.setattr(native_stage_host, "installed_host", lambda: fake)
    assert cli.main(["record-stage", "--input", str(request)]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["status"] == "saved"
    assert cli.main(["stage-source", "--start-byte", "0", "--end-byte", str(source["range"]["end_byte"])]) == 0
    assert json.loads(capsys.readouterr().out)["read_only"] is True
    for forbidden in ["--ledger-root", "--source-registry", "--session", "--actor", "--human-input-ref"]:
        with pytest.raises(SystemExit):
            cli._parser().parse_args(["record-stage", "--input", str(request), forbidden, "caller-value"])


def test_program_change_after_capture_prevents_returning_a_trusted_source():
    from pulse_compute_cost.receipts import ReceiptError
    host = SimpleNamespace(profile={"role": "producer"}, execution={"mode": "automatic"},
        session_file=Path("/synthetic/session.jsonl"), native_header=HEADER,
        base=SimpleNamespace(principal=lambda: "producer"))
    raw = encoded([HEADER, USER, ASSISTANT])
    guard = iter([None, ReceiptError("program changed")])
    def check():
        value = next(guard)
        if value:
            raise value
    adapter = NativeStageHost(host, lambda *a: (SimpleNamespace(content=raw), {}), guard=check)
    with pytest.raises(ReceiptError, match="program changed"):
        adapter.capture({"start_byte": 0, "end_byte": len(raw)})


def test_installed_loader_refuses_missing_or_unreviewed_program(monkeypatch, tmp_path):
    from pulse_compute_cost.native_stage_host import installed_host
    monkeypatch.delenv("PULSE_WORK_ITEM_CORE_HOST", raising=False)
    with pytest.raises(ReceiptError):
        installed_host()
    active = tmp_path / "active"
    release = tmp_path / "releases" / "changed"
    release.mkdir(parents=True)
    (release / "harness.pyz").write_bytes(b"not an admitted bundle")
    (release / "installation.json").write_text(json.dumps({"bundle_sha256": "0" * 64}))
    active.symlink_to(release, target_is_directory=True)
    monkeypatch.setenv("PULSE_WORK_ITEM_CORE_HOST", json.dumps({"entrypoint": str(active / "harness.pyz")}))
    with pytest.raises(ReceiptError):
        installed_host()
