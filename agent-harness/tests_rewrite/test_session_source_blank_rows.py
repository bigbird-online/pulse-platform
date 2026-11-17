"""Blank native separators cannot destroy original input custody or weaken validation."""

import json

import pytest

from harness_rewrite.model import Rejected, digest
from harness_rewrite.native_host import NativeHost
from harness_rewrite.session_sources import original_user, rows
from tests_rewrite.test_native_host import native as native
from tests_rewrite.test_session_sources import extra_runtime


@pytest.mark.parametrize("runtime", ["cc", "codex", "pi"])
@pytest.mark.parametrize("blank", [b"\n", b"\r\n", b" \t\r\n"])
def test_empty_separators_preserve_exact_human_reference_and_latest_decision(tmp_path, runtime, blank):
    message = {"role": "user", "content": [{"type": "text", "text": "取消原任务"}]}
    row = ({"type": "response_item", "payload": message} if runtime == "codex"
           else {"type": "user" if runtime == "cc" else "message", "message": message})
    prefix = b'{"type":"cost-state"}\n' + blank + b'{"type":"queue-operation"}\n' + blank
    original = prefix + json.dumps(row, ensure_ascii=False).encode() + b"\n" + blank
    session = tmp_path / "original.jsonl"
    session.write_bytes(original)
    ref = digest([runtime, "fixture-session", len(prefix), row])
    selected, raw = original_user(session, runtime, "fixture-session", ref, latest=True)
    assert selected == ref
    proof = json.loads(raw)
    assert proof["offset"] == len(prefix)
    assert proof["original"] == row
    assert proof["source"]["message"] == "取消原任务"
    assert list(rows(session))[-1] == (len(prefix), row)
    assert session.read_bytes() == original
    # A later real human decision must still supersede the earlier authorization.
    message["content"][0]["text"] = "保留原任务"
    later = original + json.dumps(row, ensure_ascii=False).encode() + b"\n" + blank
    session.write_bytes(later)
    with pytest.raises(Rejected, match="superseded"):
        original_user(session, runtime, "fixture-session", ref, latest=True)
    assert original_user(session, runtime, "fixture-session", ref)[1] == raw
    assert session.read_bytes() == later


def test_cc_original_ingress_survives_blank_internal_record_separator(native, monkeypatch):
    policy, proc, session, inputs = extra_runtime(native, "cc", monkeypatch)
    source = {"channel": "telegram", "eventId": "cancel1", "humanRef": "human1", "message": "/wi cancel wi_fixture"}
    ref = digest(["telegram", "cancel1", "human1", "telegram:fixture"])
    (inputs / (ref + ".json")).write_text(json.dumps({"source": source, "poolKey": "telegram:fixture"}))
    with session.open("a") as stream:
        stream.write(json.dumps({"type": "user", "message": {"role": "user", "content":
            source["message"] + " [Host human input reference: " + ref + ".]"}}) + "\n")
    host = NativeHost(policy, proc=proc, pid=101)
    expected = host.human_input(ref, latest=True)
    with session.open("ab") as stream:
        stream.write(b'{"type":"cost-state"}\n\n{"type":"queue-operation"}\n')
    original = session.read_bytes()
    assert NativeHost(policy, proc=proc, pid=101).human_input(ref, latest=True) == expected
    assert session.read_bytes() == original


@pytest.mark.parametrize("broken", [
    b'{"type":"user","message":\n', b'{"type":"cost-state",\n',
    b'\xff\n', b'\v\n', b'null\n', b'[]\n',
])
def test_nonempty_invalid_complete_record_cannot_hide_a_possible_human_decision(tmp_path, broken):
    row = {"type": "user", "message": {"role": "user", "content": "original"}}
    ref = digest(["cc", "fixture-session", 0, row])
    original = json.dumps(row).encode() + b"\n\n" + broken
    session = tmp_path / "original.jsonl"
    session.write_bytes(original)
    with pytest.raises(Rejected) as error:
        original_user(session, "cc", "fixture-session", ref, latest=True)
    assert error.value.code == "source_invalid"
    assert session.read_bytes() == original


def test_unfinished_tail_is_not_a_blank_separator_or_a_new_human_decision(tmp_path):
    row = {"type": "user", "message": {"role": "user", "content": "original"}}
    ref = digest(["cc", "fixture-session", 0, row])
    original = json.dumps(row).encode() + b'\n\n{"type":"user"'
    session = tmp_path / "original.jsonl"
    session.write_bytes(original)
    assert original_user(session, "cc", "fixture-session", ref, latest=True)[0] == ref
    with session.open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(Rejected):
        original_user(session, "cc", "fixture-session", ref, latest=True)
