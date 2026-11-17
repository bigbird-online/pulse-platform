"""Host-registered session identity vs. the original Pi log's full header.

Synthetic logs only. The Host may register just the session identity
({type, id}); the original first row also carries non-identity fields. The
whole chain stage-source -> record-stage -> persisted facts -> same-watermark
query must accept that, and must still refuse a different session, a
redirected or foreign-owned log, bad rows and incomplete tails.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_rewrite import native_usage
from harness_rewrite.native_usage import capture as native_capture
from pulse_compute_cost import cli, native_stage_host
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.native_stage_host import NativeStageHost
from pulse_compute_cost.receipt_sources import read_pi
from pulse_compute_cost.receipts import ReceiptError, canonical
from pulse_compute_cost.stage_record import source_projection
from test_stage_record import ASSISTANT, OBSERVED, USER, command, encoded

SESSION = "synthetic-header-session"
IDENTITY = {"type": "session", "id": SESSION}
FULL = {**IDENTITY, "version": 3, "timestamp": "2026-10-10T00:59:00Z",
        "cwd": "/synthetic/private-workspace"}
LATER_USER = {**USER, "id": "u2", "timestamp": OBSERVED}
LATER = {**ASSISTANT, "id": "a2", "parentId": "u2", "timestamp": OBSERVED}
PRIVATE = [b"private raw", b"private-workspace", SESSION.encode(), b"/synthetic/"]


def private_leak(text):
    data = text.encode() if isinstance(text, str) else text
    return [p for p in PRIVATE if p in data]


@pytest.fixture
def registered(tmp_path):
    """Admitted parent registry and empty ledger, as in the installed Host."""
    ledger_root = tmp_path / "ledger"
    ledger_root.mkdir(mode=0o700)
    registry = tmp_path / "sources.json"
    registry.write_text(json.dumps({"schema": "computecost_source_registry/v1", "sources": [],
        "required_receipt_features": ["stage_semantics.v1"], "ledger_root": str(ledger_root)}))
    registry.chmod(0o600)
    proc = tmp_path / "proc" / "101"
    proc.mkdir(parents=True)
    (proc / "environ").write_bytes(b"PULSE_COMPUTECOST_SOURCES=" + str(registry).encode() + b"\0")
    return proc.parent, ledger_root


def write_log(tmp_path, rows, header=FULL):
    path = tmp_path / "sessions" / f"2026-10-10_{SESSION}.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(encoded([header, *rows]))
    return path.resolve()


def adapter(path, proc, native_header=IDENTITY):
    host = SimpleNamespace(profile={"role": "producer"}, execution={"mode": "automatic"},
        session_file=path, native_header=native_header, proc=proc, native_pid=101,
        base=SimpleNamespace(principal=lambda: {"actor": "synthetic-producer"}))
    return NativeStageHost(host, native_capture)


def report(root, **kwargs):
    return query_collaboration(UsageLedger(root), CollaborationQuery(**kwargs))


def run_cli(capsys, argv):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if code == 0 else None), err


@pytest.mark.parametrize("native_header", [IDENTITY, FULL], ids=["identity_only", "full"])
def test_stage_source_record_stage_and_same_watermark_query(tmp_path, registered, monkeypatch,
                                                            capsys, native_header):
    proc, root = registered
    path = write_log(tmp_path, [USER, ASSISTANT])
    first_end = path.stat().st_size
    host = adapter(path, proc, native_header)
    monkeypatch.setattr(native_stage_host, "installed_host", lambda: host)

    code, summary, err = run_cli(capsys, ["stage-source", "--start-byte", "0", "--end-byte", str(first_end)])
    assert (code, err) == (0, "")
    assert summary["range"] == {"start_byte": 0, "end_byte": first_end}
    assert [a["role"] for a in summary["activities"]] == ["user", "assistant"]
    assert not private_leak(json.dumps(summary))

    request = tmp_path / "command.json"
    request.write_bytes(canonical(command({"range": summary["range"]})))
    code, saved, err = run_cli(capsys, ["record-stage", "--input", str(request)])
    assert (code, err) == (0, "")
    assert saved["status"] == "saved" and saved["inserted"] == 2
    persisted = b"".join(p.read_bytes() for p in (root / "receipts").glob("*.json"))
    assert persisted and not private_leak(persisted)
    at_first = report(root, watermark=saved["watermark"])
    assert at_first["summary"]["usage"]["known_total_tokens"] == 35

    # Same request retried (lost reply / restart): no second quantity.
    code, again, _ = run_cli(capsys, ["record-stage", "--input", str(request)])
    assert code == 0 and again["inserted"] == 0 and again["watermark"] == saved["watermark"]
    assert report(root)["summary"]["usage"]["known_total_tokens"] == 35

    # A later nonzero range of the same original log.
    with path.open("ab") as stream:
        stream.write(encoded([LATER_USER, LATER]))
    later = {"start_byte": first_end, "end_byte": path.stat().st_size}
    code, summary, _ = run_cli(capsys, ["stage-source", "--start-byte", str(later["start_byte"]),
                                        "--end-byte", str(later["end_byte"])])
    assert code == 0 and summary["range"] == later
    request.write_bytes(canonical(command({"range": later}, changes=[])))
    code, saved_later, _ = run_cli(capsys, ["record-stage", "--input", str(request)])
    assert code == 0 and saved_later["inserted"] == 1
    assert report(root)["summary"]["usage"]["known_total_tokens"] == 70
    # The earlier watermark still answers exactly as before.
    assert report(root, watermark=saved["watermark"]) == at_first

    # The full original collector observes the same invocations: no double count.
    ledger = UsageLedger(root)
    ledger.ingest_receipts(read_pi(path, OBSERVED))
    assert report(root)["summary"]["usage"]["known_total_tokens"] == 70
    assert len([r for r in ledger.receipt_snapshot()["facts"] if r.kind == "usage"]) == 2


def refusal(tmp_path, registered, capsys, monkeypatch, path, start=0, end=None, native_header=IDENTITY):
    proc, root = registered
    host = adapter(path, proc, native_header)
    monkeypatch.setattr(native_stage_host, "installed_host", lambda: host)
    end = path.stat().st_size if end is None else end
    code, _, err = run_cli(capsys, ["stage-source", "--start-byte", str(start), "--end-byte", str(end)])
    assert code == 1
    assert "retain the original range" in err
    assert not private_leak(err)
    request = tmp_path / "refused-command.json"
    request.write_bytes(canonical(command({"range": {"start_byte": start, "end_byte": end}})))
    code, _, err_record = run_cli(capsys, ["record-stage", "--input", str(request)])
    assert code == 1 and not private_leak(err_record)
    # Missing cost is not saved as zero, or as anything.
    assert not list((root / "receipts").glob("*.json")) if (root / "receipts").exists() else True
    assert report(root)["summary"]["usage"]["known_total_tokens"] in (0, None)
    assert report(root)["summary"]["usage"].get("event_count", 0) == 0
    return err


@pytest.mark.parametrize("header,reason", [
    ({**FULL, "id": "forged-session"}, "header_identity_mismatch"),
    ({**FULL, "type": "branch"}, "header_identity_mismatch"),
    ({"id": SESSION, "cwd": "/synthetic/private-workspace"}, "header_identity_mismatch"),
])
def test_forged_or_different_session_header_is_refused(tmp_path, registered, capsys, monkeypatch,
                                                       header, reason):
    path = write_log(tmp_path, [USER, ASSISTANT], header=header)
    assert f"reason={reason}" in refusal(tmp_path, registered, capsys, monkeypatch, path)


def test_host_session_id_differs_from_log(tmp_path, registered, capsys, monkeypatch):
    path = write_log(tmp_path, [USER, ASSISTANT])
    err = refusal(tmp_path, registered, capsys, monkeypatch, path,
                  native_header={**IDENTITY, "id": "another-session"})
    assert "reason=header_identity_mismatch" in err


def test_redirected_source_path_is_refused(tmp_path, registered, capsys, monkeypatch):
    original = write_log(tmp_path, [USER, ASSISTANT])
    link = tmp_path / "sessions" / "redirect.jsonl"
    link.symlink_to(original)
    assert "reason=path_mismatch" in refusal(tmp_path, registered, capsys, monkeypatch, link,
                                             end=original.stat().st_size)


def test_foreign_owned_source_is_refused(tmp_path, registered, capsys, monkeypatch):
    path = write_log(tmp_path, [USER, ASSISTANT])
    uid = os.getuid()
    monkeypatch.setattr(native_usage.os, "getuid", lambda: uid + 1)
    assert "reason=owner_mismatch" in refusal(tmp_path, registered, capsys, monkeypatch, path)


def test_incomplete_tail_retains_the_range(tmp_path, registered, capsys, monkeypatch):
    path = write_log(tmp_path, [USER])
    with path.open("ab") as stream:
        stream.write(canonical(ASSISTANT))  # no newline yet
    assert "reason=incomplete_tail" in refusal(tmp_path, registered, capsys, monkeypatch, path)


def test_bad_row_is_refused(tmp_path, registered, capsys, monkeypatch):
    path = write_log(tmp_path, [USER])
    with path.open("ab") as stream:
        stream.write(b'{"type":"message","private raw":\n')
    assert "reason=record_invalid" in refusal(tmp_path, registered, capsys, monkeypatch, path)


def test_nonzero_range_must_start_at_row_boundary(tmp_path, registered, capsys, monkeypatch):
    path = write_log(tmp_path, [USER, ASSISTANT])
    err = refusal(tmp_path, registered, capsys, monkeypatch, path, start=3)
    assert "reason=range_not_row_boundary" in err


def test_nonzero_range_cannot_contain_another_session_header(tmp_path, registered, capsys, monkeypatch):
    path = write_log(tmp_path, [USER])
    start = path.stat().st_size
    with path.open("ab") as stream:
        stream.write(encoded([{**FULL, "id": "second"}, ASSISTANT]))
    err = refusal(tmp_path, registered, capsys, monkeypatch, path, start=start)
    assert "reason=session_header_in_range" in err


def test_projection_compares_identity_fields_only():
    raw = encoded([FULL, USER, ASSISTANT])
    source = source_projection(Path("/synthetic/session.jsonl"), IDENTITY, raw, start=0, end=len(raw),
                               principal="producer", observed_at=OBSERVED)
    assert len(source["activities"]) == 2
    forged = encoded([{**FULL, "id": "forged"}, USER, ASSISTANT])
    with pytest.raises(ReceiptError, match="reason=header_identity_mismatch"):
        source_projection(Path("/synthetic/session.jsonl"), IDENTITY, forged, start=0, end=len(forged),
                          principal="producer", observed_at=OBSERVED)


def test_saved_usage_import_from_zero_accepts_full_original_header(tmp_path):
    from test_native_scl_usage import PRINCIPAL, RECORD, WI, query as scl_query
    from pulse_compute_cost.native_scl_usage import import_saved_usage
    from pulse_compute_cost.receipts import digest
    path = write_log(tmp_path, [ASSISTANT])
    raw = path.read_bytes()
    _, packet = native_capture(path, IDENTITY, 0, len(raw))
    session = "measurement-session." + digest(PRINCIPAL)
    record = {**packet, "kind": "usage", "record_id": RECORD, "recorded_at": OBSERVED,
              "generation": 1, "interval_id": "op_interval", "task_attempt": "op_attempt",
              "session": session, "actor": "pulse"}
    data = {"source": "journal", "coverage": "recorded_intervals_only", "usage": [record],
            "attention": [], "quality": [], "outcomes": [], "business_progress": "active",
            "terminal": None, "intervals": [{"interval_id": "op_interval",
                "task_attempt": "op_attempt", "start_byte": 0, "end_byte": None,
                "closed": False, "end_boundary": "open", "phase": "active",
                "generation": 1, "session": session}]}
    query = {"schema": "harness.response/v1", "status": "read_only", "error": None,
             "work_item": {"id": WI, "generation": 1, "local_revision": 8, "business_progress": "active"},
             "capabilities": [{"name": "local_read", "state": "available", "source_revision": 8}],
             "result": {"items": data, "current_observation": {"source": "journal",
                 "generation": 1, "business_progress": "active", "measurements": data}}}
    ledger = UsageLedger(tmp_path / "ledger", clock=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))
    result = import_saved_usage(ledger, query, WI, RECORD, raw, path=path, header=IDENTITY,
                                principal=PRINCIPAL, observed_at=OBSERVED)
    assert result["status"] == "saved"
    assert scl_query(ledger, work_items=(WI,))["summary"]["usage"]["known_total_tokens"] == 35
    with pytest.raises(ReceiptError):
        import_saved_usage(ledger, query, WI, RECORD, raw, path=path,
                           header={**IDENTITY, "id": "forged"}, principal=PRINCIPAL, observed_at=OBSERVED)
