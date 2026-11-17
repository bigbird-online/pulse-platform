"""A resumed desktop thread keeps its ID while its physical rollout changes."""

import json
import os
import sqlite3

import pytest

from harness_rewrite.model import Rejected
from harness_rewrite.native_host import NativeHost
from harness_rewrite.session_sources import original_user, source_files
from tests_rewrite.test_session_sources import extra_runtime
from tests_rewrite.test_native_host import native as native


def transcript(path, session, cwd, text):
    rows = [
        {"type": "session_meta", "payload": {"id": session, "cwd": str(cwd)}},
        {"type": "response_item", "payload": {
            "role": "user", "content": [{"type": "input_text", "text": text}],
        }},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def index(root, session, selected, version=5):
    database = root.parent / f"state_{version}.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
        conn.execute("INSERT INTO threads VALUES (?, ?)", (session, str(selected)))
    return database


def profile(root):
    return {"runtime": "codex", "session_root": str(root), "artifact_roots": [str(root)]}


def test_current_index_finds_continuation_and_never_uses_newer_mtime(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    old, current = root / "rollout-thread-id.jsonl", root / "rollout-thread-id_resume-id.jsonl"
    transcript(old, "thread-id", root, "Earlier instruction")
    transcript(current, "thread-id", root, "Current instruction")
    os.utime(old, (2_000_000_000, 2_000_000_000))
    database = index(root, "thread-id", current)
    before = database.read_bytes()
    assert list(source_files(profile(root), "thread-id")) == [current]
    _, raw = original_user(current, "codex", "thread-id", None, latest=True)
    assert json.loads(raw)["source"]["message"] == "Current instruction"
    assert database.read_bytes() == before


def test_unique_suffix_without_index_is_an_original_not_a_fork(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    current = root / "rollout-thread-id_resume-id.jsonl"
    transcript(current, "thread-id", root, "Continue")
    assert list(source_files(profile(root), "thread-id")) == [current]


def test_multiple_originals_without_locator_do_not_guess_from_mtime(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    for suffix in ["", "_resume-id"]:
        transcript(root / f"rollout-thread-id{suffix}.jsonl", "thread-id", root, "Continue")
    with pytest.raises(Rejected, match="current rollout"):
        list(source_files(profile(root), "thread-id"))


@pytest.mark.parametrize("fault", ["missing", "outside", "symlink", "other_thread", "fork", "scope", "writable_index"])
def test_locator_never_bypasses_original_source_validation(tmp_path, fault):
    root = tmp_path / "sessions"
    root.mkdir()
    old, current = root / "rollout-thread-id.jsonl", root / "rollout-thread-id_resume-id.jsonl"
    transcript(old, "thread-id", root, "Stale instruction")
    transcript(current, "thread-id", root, "Current instruction")
    selected = current
    if fault == "missing":
        current.unlink()
    elif fault == "outside":
        selected = tmp_path / "outside.jsonl"
        transcript(selected, "thread-id", root, "Outside")
    elif fault == "symlink":
        target = root / "actual.jsonl"
        current.rename(target)
        current.symlink_to(target)
    elif fault in {"other_thread", "fork", "scope"}:
        rows = current.read_text().splitlines()
        head = json.loads(rows[0])
        head["payload"].update({
            "other_thread": {"id": "other-id"},
            "fork": {"forked_from_id": "parent-id"},
            "scope": {"cwd": str(tmp_path / "outside")},
        }[fault])
        current.write_text(json.dumps(head) + "\n" + rows[1] + "\n")
    database = index(root, "thread-id", selected)
    if fault == "writable_index":
        database.chmod(0o666)
    if fault == "scope":
        assert list(source_files(profile(root), "thread-id")) == []
        return
    with pytest.raises(Rejected):
        list(source_files(profile(root), "thread-id"))


def test_native_process_still_selects_thread_not_callers_environment(native, monkeypatch):
    policy, proc, old, _ = extra_runtime(native, "codex", monkeypatch)
    current = old.parent / "rollout-fixture-session_resume-id.jsonl"
    transcript(current, "fixture-session", old.parent, "Current approved work")
    index(old.parent, "fixture-session", current)
    monkeypatch.setenv("CODEX_THREAD_ID", "another-thread")
    host = NativeHost(policy, proc=proc, pid=101)
    assert host.session_file == current
    ref, raw = original_user(current, "codex", "fixture-session", None)
    assert host.human_input(ref, latest=True) == raw
    assert host.native_header["id"] == "fixture-session"


def test_newest_native_schema_is_locator_not_a_second_lifecycle_store(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    old, current = root / "rollout-thread-id.jsonl", root / "rollout-thread-id_resume-id.jsonl"
    transcript(old, "thread-id", root, "Old")
    transcript(current, "thread-id", root, "New")
    index(root, "thread-id", old, version=4)
    index(root, "thread-id", current, version=5)
    assert list(source_files(profile(root), "thread-id")) == [current]


def test_current_original_selects_only_its_registered_workspace_profile(native, monkeypatch):
    policy, proc, old, _ = extra_runtime(native, "codex", monkeypatch)
    current = old.parent / "rollout-fixture-session_resume-id.jsonl"
    transcript(current, "fixture-session", old.parent, "Continue original work")
    index(old.parent, "fixture-session", current)
    value = json.loads(policy.read_text())
    value["profiles"][0]["workspace_roots"] = [str(old.parent)]
    other = {**value["profiles"][0], "actor": "other", "workspace_roots": [str(old.parent / "other")]}
    value["profiles"].insert(0, other)
    policy.write_text(json.dumps(value))
    assert NativeHost(policy, proc=proc, pid=101).base.actor == "producer"


def test_broken_native_locator_does_not_fall_back_to_stale_authorization(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    old = root / "rollout-thread-id.jsonl"
    transcript(old, "thread-id", root, "Old approval")
    (root.parent / "state_5.sqlite").write_bytes(b"corrupt locator")
    with pytest.raises(Rejected, match="current rollout locator is unavailable"):
        list(source_files(profile(root), "thread-id"))
