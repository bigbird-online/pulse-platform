"""Another session of the holder's Agent appends work records and nothing else (A-40).

Holder and generation stay with the holding session; the record keeps the
author session in the journal event, never in the public observation.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from harness_rewrite import Command, HostContext, Kernel, Query, SourceInput
from harness_rewrite.domain import INTENTS
from harness_rewrite.model import ArtifactInput, digest
from tests_rewrite.test_native_host import native as native  # noqa: F401

SOURCE = SourceInput("native-input-1", "native_agent", b"Create a bounded test work item")
CREATOR = HostContext(
    "team", "hestia", "session-creator", "local", 0,
    frozenset({"*"}), frozenset({"create_draft", "create_lightweight"}), SOURCE,
)
HOLDER = replace(CREATOR, session="session-holder", generation=1, allowed_intents=INTENTS,
                 source=None)
SIBLING = replace(HOLDER, session="session-dispatch", surface="surface_v1_local_macos")
STRANGER = replace(HOLDER, actor="pulse", session="session-stranger")
ARTIFACT = ArtifactInput("repo/src/report", "v1", b"Alert handled\n")
INPUT = {
    "summary": "Fixture work for tests.",
    "why": "Standing alert dispatch",
    "what": "Record every handled alert",
    "acceptance": ["Each dispatch leaves a record"],
    "owner": "hestia",
    "resources": ["repo/src", "repo/tests"],
}


class Host:
    def __init__(self):
        self.contexts = {
            "create": CREATOR, "holder": HOLDER, "sibling": SIBLING, "stranger": STRANGER,
        }

    def authenticate(self, handle):
        return self.contexts[handle]


@pytest.fixture
def rig(tmp_path):
    host = Host()
    kernel = Kernel(tmp_path / "facts.sqlite", host.authenticate)
    created = kernel.execute(Command("create", "create_lightweight", INPUT), "create")
    wi = created["work_item"]["id"]
    assert kernel.execute(Command("start", "start_work", {}, wi), "holder")["status"] == (
        "local_committed"
    )
    return kernel, host, wi


def code(result):
    return result["error"]["code"] if result["error"] else None


def checkpoint(wi, request, resources=None):
    data = {"summary": "Alert handled", "resources": resources or ["repo/src"]}
    return Command(request, "checkpoint", data, wi)


def record(wi, request):
    data = {"summary": "Alert handled", "resources": ["repo/src"],
            "artifacts": [ARTIFACT.reference()]}
    return Command(request, "record_work", data, wi)


def observed(kernel, wi, handle="holder"):
    return kernel.inspect(Query(wi), handle)["result"]["current_observation"]


def journal_records(kernel, wi):
    with kernel._connection(write=False, work_item_id=wi) as conn:
        state, _, _, _ = kernel._replay(conn, "team", wi)
    return state["records"]


def test_sibling_session_appends_records_without_taking_the_work(rig):
    kernel, host, wi = rig
    host.contexts["sibling-record"] = replace(SIBLING, artifacts=(ARTIFACT,))
    assert kernel.execute(checkpoint(wi, "own"), "holder")["status"] == "local_committed"
    first = kernel.execute(checkpoint(wi, "dispatch"), "sibling")
    assert first["status"] == "local_committed", first
    second = kernel.execute(record(wi, "dispatch-record"), "sibling-record")
    assert second["status"] == "local_committed", second
    state = observed(kernel, wi)
    assert state["generation"] == 1
    assert [r["kind"] for r in state["records"]] == [
        "CheckpointRecorded", "CheckpointRecorded", "WorkRecorded",
    ]
    assert all("author_session" not in r for r in state["records"])
    assert "session-dispatch" not in json.dumps(first) + json.dumps(state)
    assert [r["author_session"] for r in journal_records(kernel, wi)] == [
        "session-holder", "session-dispatch", "session-dispatch",
    ]
    # The holding session keeps control; the sibling still has no execution capability.
    assert kernel.execute(checkpoint(wi, "holder-after"), "holder")["status"] == "local_committed"
    work = next(
        c for c in kernel.resume(wi, "sibling")["capabilities"] if c["name"] == "local_work"
    )
    assert work["reason"] == "held_by_same_agent_in_another_session"


def test_sibling_record_still_needs_captured_artifact_bytes(rig):
    kernel, _, wi = rig
    assert code(kernel.execute(record(wi, "no-bytes"), "sibling")) == "source_invalid"


@pytest.mark.parametrize(
    "intent,data",
    [
        ("start_work", {}),
        ("rollover", {"summary": "Take it over", "target": SIBLING.principal()}),
        ("grant_scope", {"principal": STRANGER.principal(), "resources": ["repo/src"],
                         "intents": ["checkpoint"]}),
        ("revoke_scope", {"grant_id": "missing", "reason": "done"}),
        ("complete_lightweight", {"record_ids": ["x"], "acceptance_refs": ["AC-1"]}),
        ("revise_plan", {"graph": {}, "plan_text": "p", "previous_revision": 1,
                         "reuse_results": [], "reason": "r"}),
        ("rename_work", {"title": "Renamed", "reason": "r"}),
    ],
)
def test_sibling_session_cannot_control_the_work(rig, intent, data):
    kernel, host, wi = rig
    # Even an authenticated human decision does not make the sibling a holder.
    host.contexts["sibling-human"] = replace(
        SIBLING,
        source=SourceInput(
            "human", "native_human", b"Approved", intent,
            digest({"intent": intent, "work_item_id": wi, "input": data}),
        ),
    )
    before = kernel.inspect(Query(wi), "holder")["work_item"]
    for handle in ("sibling", "sibling-human"):
        result = kernel.execute(Command(f"{intent}-{handle}", intent, data, wi), handle)
        assert result["status"] == "rejected", result
    assert kernel.inspect(Query(wi), "holder")["work_item"] == before
    assert kernel.execute(checkpoint(wi, "still-holder"), "holder")["status"] == (
        "local_committed"
    )


def test_another_agent_session_cannot_append_records(rig):
    kernel, _, wi = rig
    assert code(kernel.execute(checkpoint(wi, "stranger"), "stranger")) == "authority_denied"


def test_sibling_cannot_supply_another_generation(rig):
    kernel, host, wi = rig
    host.contexts["stale"] = replace(SIBLING, generation=0)
    host.contexts["ahead"] = replace(SIBLING, generation=2)
    for handle in ("stale", "ahead"):
        assert code(kernel.execute(checkpoint(wi, handle), handle)) == "authority_changed"
    assert observed(kernel, wi)["generation"] == 1


def test_sibling_scope_is_the_holder_scope_minus_delegations(rig):
    kernel, _, wi = rig
    assert code(kernel.execute(checkpoint(wi, "outside", ["repo"]), "sibling")) == (
        "authority_denied"
    )
    granted = kernel.execute(
        Command("grant", "grant_scope", {"principal": STRANGER.principal(),
                                         "resources": ["repo/src"],
                                         "intents": ["checkpoint"]}, wi),
        "holder",
    )
    assert granted["status"] == "local_committed", granted
    assert code(kernel.execute(checkpoint(wi, "delegated"), "sibling")) == "authority_denied"
    assert kernel.execute(checkpoint(wi, "remaining", ["repo/tests"]), "sibling")["status"] == (
        "local_committed"
    )


def test_draft_work_has_no_sibling_records(tmp_path):
    host = Host()
    kernel = Kernel(tmp_path / "facts.sqlite", host.authenticate)
    created = kernel.execute(Command("create", "create_draft", INPUT), "create")
    wi = created["work_item"]["id"]
    assert code(kernel.execute(checkpoint(wi, "draft"), "sibling")) == "authority_denied"


def test_transferring_stopped_and_completed_work_reject_sibling_records(rig):
    kernel, host, wi = rig
    host.contexts["sibling-record"] = replace(SIBLING, artifacts=(ARTIFACT,))
    host.contexts["holder-record"] = replace(HOLDER, artifacts=(ARTIFACT,))
    work = kernel.execute(record(wi, "work"), "holder-record")
    record_id = work["result"]["original_request_result"]["record_id"]
    completion = {"record_ids": [record_id], "acceptance_refs": ["AC-1"]}
    host.contexts["holder-human"] = replace(
        HOLDER,
        source=SourceInput(
            "complete", "native_human", b"Complete it", "complete_lightweight",
            digest({"intent": "complete_lightweight", "work_item_id": wi, "input": completion}),
        ),
    )
    done = kernel.execute(Command("complete", "complete_lightweight", completion, wi),
                          "holder-human")
    assert done["work_item"]["business_progress"] == "completed", done
    assert code(kernel.execute(checkpoint(wi, "after-complete"), "sibling")) == "authority_denied"
    assert code(kernel.execute(record(wi, "after-complete-r"), "sibling-record")) == (
        "authority_denied"
    )

    second = kernel.execute(Command("create-2", "create_lightweight", INPUT), "create")
    other = second["work_item"]["id"]
    kernel.execute(Command("start-2", "start_work", {}, other), "holder")
    moved = kernel.execute(
        Command("rollover", "rollover", {"summary": "Continue", "target": STRANGER.principal()},
                other),
        "holder",
    )
    assert moved["status"] == "local_committed", moved
    assert code(kernel.execute(checkpoint(other, "transferring"), "sibling")) == (
        "authority_denied"
    )

    third = kernel.execute(Command("create-3", "create_lightweight", INPUT), "create")
    stopped = third["work_item"]["id"]
    kernel.execute(Command("start-3", "start_work", {}, stopped), "holder")
    data = {"reason": "Human stopped this work"}
    host.contexts["holder-stop"] = replace(
        HOLDER,
        source=SourceInput(
            "stop", "native_human", b"Stop", "stop",
            digest({"intent": "stop", "work_item_id": stopped, "input": data}),
        ),
    )
    halted = kernel.execute(Command("stop", "stop", data, stopped), "holder-stop")
    assert halted["work_item"]["business_progress"] == "stopped", halted
    assert code(kernel.execute(checkpoint(stopped, "stopped"), "sibling")) == "authority_denied"


def test_native_sibling_session_reaches_the_kernel_through_execute(
    native, tmp_path, monkeypatch, capsys
):
    """Two native sessions of one Agent: the CLI reads back the generation for both."""
    import os

    from harness_rewrite import cli
    from harness_rewrite.native_host import NativeHost

    file, proc, session, _ = native
    data = json.loads(file.read_text())
    for name in ["delivery", "dispatch", "views"]:
        (tmp_path / name).mkdir()
    data.update(
        database=str(tmp_path / "facts.sqlite"),
        delivery_root=str(tmp_path / "delivery"),
        dispatch_root=str(tmp_path / "dispatch"),
        views_root=str(tmp_path / "views"),
    )
    file.write_text(json.dumps(data))
    sibling_log = session.parent / "2026_dispatch-session.jsonl"
    sibling_log.write_text(json.dumps({"type": "session", "id": "dispatch-session"}) + "\n")
    sibling = proc / "102"
    sibling.mkdir()
    sibling.joinpath("status").write_text(f"Uid:\t{os.getuid()}\nPPid:\t1\n")
    sibling.joinpath("cmdline").write_bytes(
        b"\0".join(
            s.encode()
            for s in [(proc / "101/cmdline").read_bytes().split(b"\0")[0].decode(),
                      "--session", str(sibling_log)]
        )
    )
    sibling.joinpath("environ").write_bytes(b"PULSE_PI_TENANT_ID=telegram:dispatch\0")
    current = {"pid": 101}
    monkeypatch.setattr(
        cli, "NativeHost", lambda _: NativeHost(file, proc=proc, pid=current["pid"])
    )
    monkeypatch.setattr(cli.sys, "argv", [__file__])
    command = tmp_path / "command.json"

    def send(intent, payload, wi=None, files=None):
        command.write_text(json.dumps(
            {"intent": intent, "input": payload, "work_item_id": wi, "files": files or {}}
        ))
        cli.main(["execute", "--input", str(command)])
        return json.loads(capsys.readouterr().out)

    created = send("create_lightweight", {**INPUT, "owner": "producer"})
    assert created["status"] == "local_committed", created
    wi = created["work_item"]["id"]
    assert send("start_work", {}, wi)["status"] == "local_committed"
    current["pid"] = 102
    artifact = session.parent / "dispatch-report.txt"
    artifact.write_bytes(b"Alert handled\n")
    recorded = send(
        "record_work",
        {"summary": "Alert handled", "resources": ["repo/src"], "artifacts": [{"file": "r"}]},
        wi,
        {"r": {"path": str(artifact), "logical": "repo/src/report", "version": "v1"}},
    )
    assert recorded["status"] == "local_committed", recorded
    assert recorded["work_item"]["generation"] == 1
    assert send("rename_work", {"title": "Taken", "reason": "r"}, wi)["status"] == "rejected"
    current["pid"] = 101
    assert send("checkpoint", {"summary": "Holder", "resources": ["repo/src"]}, wi)["status"] == (
        "local_committed"
    )
