"""Native usage capture compares session identity, not every header field.

Synthetic logs only. A Host may register just {type, id}; the original first
row also carries version, timestamp and cwd.
"""

import os

import pytest

from harness_rewrite import native_usage
from harness_rewrite.model import Rejected, encoded

IDENTITY = {"type": "session", "id": "synthetic-native-session"}
FULL = {**IDENTITY, "version": 3, "timestamp": "2026-10-10T00:59:00Z",
        "cwd": "/synthetic/private-workspace"}
PRIVATE = ("PRIVATE_MESSAGE", "private-workspace", "synthetic-native-session", "/synthetic/")


def row(output=5):
    return {"type": "message", "message": {"role": "assistant", "model": "m", "provider": "p",
            "content": "PRIVATE_MESSAGE", "usage": {"input": 1, "output": output}}}


def log(tmp_path, *rows, header=FULL):
    path = tmp_path / "original.jsonl"
    path.write_bytes(b"".join(encoded(x) + b"\n" for x in [header, *rows]))
    return path.resolve()


def refused(path, header, start, end, cause, code):
    with pytest.raises(native_usage.SourceRejected) as exc:
        native_usage.capture(path, header, start, end)
    assert (exc.value.cause, exc.value.code) == (cause, code)
    assert isinstance(exc.value, Rejected)
    assert not any(p in str(exc.value) for p in PRIVATE)


@pytest.mark.parametrize("expected", [IDENTITY, FULL], ids=["identity_only", "full"])
def test_identity_header_accepts_full_original_header_from_zero_and_later(tmp_path, expected):
    path = log(tmp_path, row(5))
    end = path.stat().st_size
    _, first = native_usage.capture(path, expected, 0, end)
    assert first["groups"][0]["tokens"]["output"]["value"] == 5
    with path.open("ab") as stream:
        stream.write(encoded(row(7)) + b"\n")
    _, later = native_usage.capture(path, expected, end, path.stat().st_size)
    assert later["groups"][0]["tokens"]["output"]["value"] == 7
    # Same bytes, same packet: a retried request cannot mint another quantity.
    assert native_usage.capture(path, expected, 0, end)[1] == first


def test_identity_fields_are_type_and_id_only():
    assert native_usage.same_session(IDENTITY, FULL)
    assert native_usage.same_session(FULL, {**IDENTITY, "cwd": "/elsewhere"})
    assert not native_usage.same_session(IDENTITY, {**FULL, "id": "forged"})
    assert not native_usage.same_session(IDENTITY, {**FULL, "type": "branch"})
    assert not native_usage.same_session(IDENTITY, {"id": IDENTITY["id"]})
    assert not native_usage.same_session({"id": IDENTITY["id"]}, FULL)
    assert not native_usage.same_session({"type": "session", "id": ""}, {"type": "session", "id": ""})
    assert not native_usage.same_session(IDENTITY, ["session", IDENTITY["id"]])


@pytest.mark.parametrize("header", [{**FULL, "id": "forged"}, {**FULL, "type": "branch"},
                                    {"cwd": "/synthetic/private-workspace"}])
def test_forged_or_different_header_is_refused(tmp_path, header):
    path = log(tmp_path, row(), header=header)
    refused(path, IDENTITY, 0, path.stat().st_size, "header_identity_mismatch", "source_invalid")


def test_unparseable_header_is_refused(tmp_path):
    path = tmp_path / "original.jsonl"
    path.write_bytes(b'{"type":"session","id":\n' + encoded(row()) + b"\n")
    refused(path.resolve(), IDENTITY, 0, path.stat().st_size, "header_invalid", "source_invalid")


def test_redirected_or_foreign_source_is_refused(tmp_path, monkeypatch):
    path = log(tmp_path, row())
    end = path.stat().st_size
    link = tmp_path / "redirect.jsonl"
    link.symlink_to(path)
    refused(link, IDENTITY, 0, end, "path_mismatch", "authority_denied")
    refused(tmp_path / "missing.jsonl", IDENTITY, 0, end, "source_missing", "source_unavailable")
    uid = os.getuid()
    monkeypatch.setattr(native_usage.os, "getuid", lambda: uid + 1)
    refused(path, IDENTITY, 0, end, "owner_mismatch", "authority_denied")


def test_range_boundaries_tail_and_bad_rows_are_classified(tmp_path):
    path = log(tmp_path, row())
    end = path.stat().st_size
    refused(path, IDENTITY, 3, end, "range_not_row_boundary", "invalid_input")
    refused(path, IDENTITY, end, end, "invalid_range", "invalid_input")
    with path.open("ab") as stream:
        stream.write(encoded(row(9)))
    refused(path, IDENTITY, 0, path.stat().st_size, "incomplete_tail", "source_unavailable")
    refused(path, IDENTITY, 0, path.stat().st_size + 10, "incomplete_tail", "source_unavailable")
    with path.open("ab") as stream:
        stream.write(b"\n" + b'{"type":"message","PRIVATE_MESSAGE":\n')
    refused(path, IDENTITY, end, path.stat().st_size, "record_invalid", "source_invalid")
