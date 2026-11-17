"""An unattended check reads back one work record by its exact ID and nothing else."""

from __future__ import annotations

import json
from dataclasses import replace
import sys

import pytest

from harness_rewrite import cli, record_receipt
from harness_rewrite import Command, HostContext, Kernel
from harness_rewrite.model import Rejected
from tests_rewrite.test_effect_service import installed
from tests_rewrite.test_production_ledger import Rig as ProductionRig, git, WI
from tests_rewrite.test_same_agent_records import (
    ARTIFACT,
    CREATOR,
    HOLDER,
    INPUT,
    checkpoint,
    record,
)

READER = HostContext(
    "team",
    "record-receipt",
    "record-receipt",
    "local",
    0,
    frozenset(),
    frozenset(),
    read_only_reason="record_receipt",
)


@pytest.fixture
def rig(tmp_path):
    contexts = {
        "create": CREATOR,
        "holder": HOLDER,
        "holder-record": replace(HOLDER, artifacts=(ARTIFACT,)),
    }
    clock = iter(f"2026-10-20T00:0{n}:00+00:00" for n in range(10))
    kernel = Kernel(tmp_path / "facts.sqlite", contexts.__getitem__, clock=lambda: next(clock))
    wis = []
    for request in ("create-a", "create-b"):
        created = kernel.execute(Command(request, "create_lightweight", INPUT), "create")
        wi = created["work_item"]["id"]
        assert (
            kernel.execute(Command("start-" + wi, "start_work", {}, wi), "holder")["status"]
            == "local_committed"
        )
        wis.append(wi)
    for wi in wis:
        contexts["reader-" + wi] = replace(READER, readable_wis=frozenset({wi}))
    return kernel, wis


def read(kernel, wi, record_id):
    return record_receipt.receipt(kernel, "reader-" + wi, "team", wi, record_id)


def test_receipt_names_author_revision_and_commit_time_only(rig):
    kernel, (wi, _) = rig
    committed = kernel.execute(record(wi, "dispatch-record"), "holder-record")
    assert committed["status"] == "local_committed", committed
    record_id = committed["result"]["original_request_result"]["record_id"]
    receipt = read(kernel, wi, record_id)
    assert receipt == {
        "schema": "harness.record_receipt/v1",
        "status": "found",
        "work_item_id": wi,
        "record_id": record_id,
        "kind": "WorkRecorded",
        "author": "hestia",
        "revision": 3,
        "recorded_at": "2026-10-20T00:04:00+00:00",
    }
    assert "Alert handled" not in json.dumps(receipt)


def test_pages_through_history_until_the_record(rig, monkeypatch):
    kernel, (wi, _) = rig
    for n in range(3):
        kernel.execute(checkpoint(wi, f"progress-{n}"), "holder")
    record_id = kernel.execute(record(wi, "late-record"), "holder-record")["result"][
        "original_request_result"
    ]["record_id"]
    monkeypatch.setattr(record_receipt, "PAGE", 2)
    assert read(kernel, wi, record_id)["revision"] == 6


@pytest.mark.parametrize("case", ["checkpoint", "unknown", "other_wi"])
def test_anything_but_this_wis_work_record_is_absent(rig, case):
    kernel, (wi, other) = rig
    if case == "checkpoint":
        record_id = kernel.execute(checkpoint(wi, "progress"), "holder")["result"][
            "original_request_result"
        ]["record_id"]
    elif case == "unknown":
        record_id = "op_" + "0" * 64
    else:
        record_id = kernel.execute(record(other, "elsewhere"), "holder-record")["result"][
            "original_request_result"
        ]["record_id"]
    with pytest.raises(Rejected) as rejected:
        read(kernel, wi, record_id)
    assert rejected.value.code == "record_absent"


def test_reader_is_scoped_to_the_named_wi(rig):
    kernel, (wi, other) = rig
    record_id = kernel.execute(record(other, "elsewhere"), "holder-record")["result"][
        "original_request_result"
    ]["record_id"]
    denied = record_receipt.receipt(kernel, "reader-" + wi, "team", other, record_id)
    assert denied["status"] == "rejected"
    assert denied["error"]["code"] == "authority_denied"


def test_installed_cli_reads_without_a_native_session_and_writes_nothing(
    tmp_path, capsys, monkeypatch
):
    fixture = tmp_path / "git"
    fixture.mkdir()
    production = ProductionRig(fixture)
    entry = installed(tmp_path, production.ledger.config)
    monkeypatch.setattr(sys, "argv", [str(entry)])
    assert cli.main(["record-receipt", "--wi", WI, "--record", "op_" + "1" * 64]) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "rejected"
    assert response["error"]["code"] == "record_absent"
    assert git(production.remote, "rev-parse", "main") == production.before
    assert record_receipt.main(entry, ["--wi", "bad id", "--record", "op_x"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"
