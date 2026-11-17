"""Measurement collectors cannot veto the authenticated business path."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from harness_rewrite import Command, Kernel, Query
from harness_rewrite import native_usage
from harness_rewrite.domain import INTENTS
from harness_rewrite.model import Rejected
from harness_rewrite.native_host import NativeHost
from tests_rewrite.test_native_host import native as native
from tests_rewrite.test_measurements import values, admission, log, row
from tests_rewrite.test_workflow import BASE, DEFINITION, OTHER, Rig, graph, original, error


@pytest.mark.parametrize("fault", [OSError("collector failed"), TimeoutError("collector timeout"),
                                  Rejected("source_unavailable", "No bounded record")])
def test_business_preparation_never_calls_the_usage_collector(native, tmp_path, monkeypatch, fault):
    policy, proc, path, _ = native
    # This incomplete tail is real native source, not a complete captured record.
    with path.open("ab") as stream:
        stream.write(b'{"type":"message"')
    size = path.stat().st_size
    host = NativeHost(policy, proc=proc, pid=101)
    collector = Mock(side_effect=fault)
    monkeypatch.setattr(native_usage, "cursor", collector)
    kernel = Kernel(tmp_path / "noninterference.sqlite", host.authenticate)
    handle = host.prepare("create_lightweight", DEFINITION, None, 0, ())
    wi = original(kernel.execute(Command("create", "create_lightweight", DEFINITION), handle))["work_item_id"]
    handle = host.prepare("start_work", {}, wi, 1, ())
    original(kernel.execute(Command("start", "start_work", {}, wi), handle))
    artifact_path = path.parent / "work.md"
    artifact_path.write_text("Synthetic work result")
    artifact = host.artifact(str(artifact_path), "repo/work", "v1")
    data = {"summary": "Actual source work", "resources": ["repo"], "artifacts": [artifact.reference()]}
    handle = host.prepare("record_work", data, wi, 1, (artifact,))
    original(kernel.execute(Command("work", "record_work", data, wi), handle))
    assert host.authenticate(handle).native_cursor == size
    collector.assert_not_called()
    assert kernel.inspect(Query(wi), handle)["work_item"]["business_progress"] == "active"
    # Only the explicit metering operation needs a complete-row cursor.
    with pytest.raises(type(fault)):
        host.prepare("open_usage_interval", {"task_attempt": None}, wi, 1, ())
    assert collector.call_count == 1


@pytest.mark.parametrize("transition", ["result", "abandon", "rollover", "stop", "assume_control"])
@pytest.mark.parametrize("end", [None, 50])
def test_bad_measurement_end_does_not_reject_an_authorized_transition(tmp_path, transition, end):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(BASE, allowed_intents=INTENTS, native_cursor=100)
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    original(rig.send("open_usage_interval", {"task_attempt": attempt}))
    rig.contexts["owner"] = replace(rig.contexts["owner"], native_cursor=end)
    if transition == "result":
        result = rig.result(attempt)
    elif transition == "abandon":
        result = rig.send("abandon_task", {"attempt_id": attempt, "reason": "Real interruption"})
    elif transition == "rollover":
        result = rig.send("rollover", {"target": OTHER.principal(), "summary": "Same work"})
    else:
        result = rig.approve(transition, {"reason": "Exact human control"}, actor=rig.contexts["owner"])
    original(result)
    interval = values(rig)["intervals"][0]
    assert interval["closed"] is True and interval["end_byte"] is None
    assert interval["end_boundary"] == "unavailable"
    assert interval["end_boundary_reason"] == ("not_observed" if end is None else "source_moved_backwards")
    assert values(rig)["usage"] == []


def test_invalid_end_preserves_saved_usage_and_cannot_authorize_more_attribution(tmp_path):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(BASE, allowed_intents=INTENTS)
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    path = log(tmp_path, row({"input": 7}))
    data = admission(rig, path, attempt=attempt)
    original(rig.send("record_usage", data, handle="usage"))
    saved = values(rig)["usage"]
    # A cursor after the start but before already saved bytes is also invalid.
    rig.contexts["owner"] = replace(rig.contexts["owner"], native_cursor=1)
    original(rig.result(attempt))
    interval = values(rig)["intervals"][0]
    assert interval["end_byte"] is None and interval["end_boundary_reason"] == "source_moved_backwards"
    assert values(rig)["usage"] == saved
    assert error(rig.send("record_usage", data, handle="usage")) == "source_invalid"


def test_unavailable_extent_does_not_replace_native_identity_or_invent_zero(native, monkeypatch):
    policy, proc, _, _ = native
    with monkeypatch.context() as patch:
        patch.setattr("harness_rewrite.native_host.os.fstat", Mock(side_effect=OSError("metadata unavailable")))
        host = NativeHost(policy, proc=proc, pid=101)
    handle = host.prepare("checkpoint", {"summary": "Known work", "resources": ["repo"]}, "wi_fixture", 1, ())
    assert host.authenticate(handle).native_cursor is None
    assert host.authenticate(handle).principal() == host.base.principal()


def test_business_snapshot_does_not_read_a_later_appended_tail(native, monkeypatch):
    policy, proc, path, _ = native
    host = NativeHost(policy, proc=proc, pid=101)
    size = path.stat().st_size
    with path.open("ab") as stream:
        stream.write(b'{"unfinished":"tail')
    collector = Mock(side_effect=AssertionError("business must not enter pending collector"))
    monkeypatch.setattr(native_usage, "cursor", collector)
    handle = host.prepare("checkpoint", {"summary": "Known work", "resources": ["repo"]}, "wi_fixture", 1, ())
    assert host.authenticate(handle).native_cursor == size
    collector.assert_not_called()


def test_bad_native_header_is_still_a_hard_identity_failure(native):
    policy, proc, path, _ = native
    path.write_text('{"type":"session","id":42}\n')
    with pytest.raises(Rejected, match="session header disagree"):
        NativeHost(policy, proc=proc, pid=101)


def test_good_measurement_cannot_override_execution_or_human_authority(tmp_path):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(BASE, allowed_intents=INTENTS, native_cursor=100)
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    original(rig.send("open_usage_interval", {"task_attempt": attempt}))
    rig.contexts["helper"] = replace(OTHER, allowed_intents=INTENTS, native_cursor=200)
    assert error(rig.result(attempt, handle="helper")) == "authority_denied"
    assert error(rig.send("stop", {"reason": "Not a human decision"})) == "source_invalid"
    assert values(rig)["intervals"][0]["closed"] is False
