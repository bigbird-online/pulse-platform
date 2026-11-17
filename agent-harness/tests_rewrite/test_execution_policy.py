"""Synthetic native Host and real file effects; these are not Muse UAT receipts."""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from harness_rewrite import cli, location, policy, release, Query
from harness_rewrite.effects import Effects, FileDelivery
from harness_rewrite.model import ArtifactInput, Rejected
from harness_rewrite.native_host import NativeHost
from tests_rewrite.test_native_host import native as native
from tests_rewrite.test_custody import retained_store as retained_store


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"mode": "shadow", "batch_budget": 8},
        {"mode": "suggestion", "batch_budget": 8},
        {"mode": "automatic", "batch_budget": True},
        {"mode": "manual", "batch_budget": 0},
        {"mode": "manual", "batch_budget": 65},
        {"mode": "manual", "batch_budget": 1.5},
        {"mode": "audit", "batch_budget": 8, "allow_write": True},
    ],
)
def test_unknown_or_malformed_execution_policy_rejects(value):
    with pytest.raises(ValueError):
        policy.execution({"execution": value})


@pytest.mark.parametrize("mode", [None, "manual", "automatic"])
def test_real_native_effects_need_explicit_mode_and_obey_budget(
    native, tmp_path, monkeypatch, capsys, mode
):
    file, proc, _, _ = native
    data = json.loads(file.read_text())
    for name in ["delivery", "dispatch", "views"]:
        (tmp_path / name).mkdir()
    data.update(
        database=str(tmp_path / "facts.sqlite"),
        delivery_root=str(tmp_path / "delivery"),
        dispatch_root=str(tmp_path / "dispatch"),
        views_root=str(tmp_path / "views"),
    )
    if mode is not None:
        data["execution"] = {"mode": mode, "batch_budget": 1}
    file.write_text(json.dumps(data))
    monkeypatch.setattr(
        cli, "NativeHost", lambda _: NativeHost(file, proc=proc, pid=101)
    )
    monkeypatch.setattr(cli.sys, "argv", [str(Path(__file__).resolve())])
    command = tmp_path / "command.json"

    def run(args, expected=0):
        code = cli.main(args)
        r = json.loads(capsys.readouterr().out)
        assert code == expected, r
        return r

    def send(intent, payload, wi=None, files=None):
        command.write_text(
            json.dumps(
                {
                    "intent": intent,
                    "input": payload,
                    "work_item_id": wi,
                    "files": files or {},
                }
            )
        )
        return run(["execute", "--input", str(command)])

    wi = send(
        "create_lightweight",
        {
            "summary": "Fixture work for tests.",
            "why": "Explicit dispatch fixture",
            "what": "Deliver fixed bytes",
            "acceptance": ["Original delivery bytes"],
            "resources": ["repo"],
        },
    )["work_item"]["id"]
    started = send("start_work", {}, wi)
    assert (
        next(
            c["state"]
            for c in started["latest_observation"]["capabilities"]
            if c["name"] == "local_work"
        )
        == "available"
    )
    artifact = Path(data["profiles"][0]["artifact_roots"][0]) / "result.txt"
    artifact.write_bytes(b"Original delivery fixture\n")
    ref = ArtifactInput("repo/report", "v1", artifact.read_bytes()).reference()
    send(
        "record_work",
        {
            "summary": "Original result",
            "resources": ["repo"],
            "artifacts": [{"file": "report"}],
        },
        wi,
        {"report": {"path": str(artifact), "logical": "repo/report", "version": "v1"}},
    )
    declared = send(
        "declare_delivery",
        {
            "candidate_id": None,
            "deliveries": [
                {
                    "artifact": ref,
                    "target": f"result-{n}.txt",
                    "required": True,
                    "node_id": None,
                }
                for n in range(4)
            ],
        },
        wi,
    )
    destination = tmp_path / "delivery"
    expected = 1 if mode == "automatic" else 0
    assert len(list(destination.iterdir())) == expected
    assert declared["dispatch_policy"]["automatic_drain_enabled"] == (
        mode == "automatic"
    )
    if mode == "automatic":
        # Re-enter the exact durable declaration after a partial batch. It must
        # advance only an outstanding effect, without a second declaration or
        # another attempt for the already delivered file.
        first_file = (destination / "result-0.txt").read_bytes()
        repeated = run(["execute", "--input", str(command)])
        assert repeated["request_id"] == declared["request_id"]
        assert repeated["operation_id"] == declared["operation_id"]
        assert repeated["result"]["original_request_result"] == declared["result"][
            "original_request_result"
        ]
        assert repeated["work_item"] == declared["latest_observation"]["work_item"]
        assert len(repeated["effect_progress"]) == 1
        assert repeated["latest_observation"]["work_item"]["local_revision"] == (
            declared["latest_observation"]["work_item"]["local_revision"] + 2
        )
        assert (destination / "result-0.txt").read_bytes() == first_file
        effects = repeated["latest_observation"]["effects"]
        assert [len(effect["attempts"]) for effect in effects] == [1, 1, 0, 0]
        assert [effect["status"] for effect in effects] == [
            "observed_success",
            "observed_success",
            "requested",
            "requested",
        ]
        expected += 1
    if mode is None:
        assert declared["dispatch_policy"]["mode"] == "manual"
        assert declared["dispatch_policy"]["batch_budget"] == 8
        data["execution"] = {"mode": "manual", "batch_budget": 1}
        file.write_text(json.dumps(data))
    resumed = run(["resume", "--wi", wi])
    assert len(list(destination.iterdir())) == expected + 1
    assert len(resumed["effect_progress"]) == 1
    # Switching a fixture Host policy is not a native installation proof. Audit
    # blocks adapters even with real pending effects and the same native holder.
    data["execution"] = {"mode": "audit", "batch_budget": 64}
    file.write_text(json.dumps(data))
    db = tmp_path / "facts.sqlite"
    before = db.read_bytes()
    target_before = {p.name: p.read_bytes() for p in destination.iterdir()}
    views_before = {p.name: p.read_bytes() for p in (tmp_path / "views").iterdir()}
    for action in ["inspect", "resume", "drain"]:
        observed = run([action, "--wi", wi])
        assert all(
            c["state"] == "unavailable"
            for c in observed["capabilities"]
            if c["name"] != "local_read"
        )
        assert observed["next_action"] == {
            "intent": "inspect",
            "reason": "policy_audit",
        }
        assert observed["dispatch_policy"]["mode"] == "audit"
    denied = run(["execute", "--input", str(tmp_path / "missing-command.json")], 2)
    assert denied["error"]["code"] == "policy_read_only"
    assert db.read_bytes() == before
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == target_before
    assert {
        p.name: p.read_bytes() for p in (tmp_path / "views").iterdir()
    } == views_before
    host = NativeHost(file, proc=proc, pid=101)
    assert (
        not host.base.allowed_intents and host.base.read_only_reason == "policy_audit"
    )
    with pytest.raises(Rejected, match="audit"):
        host.prepare("checkpoint", {}, wi, 1, ())
    handle = host.worker(())
    worker = host.authenticate(handle)
    assert not worker.allowed_intents and not worker.effect_kinds
    from harness_rewrite import Kernel

    kernel = Kernel(db, host.authenticate)
    assert (
        Effects(kernel, FileDelivery(destination), host.worker).drain(wi)[0][
            "next_action"
        ]["reason"]
        == "policy_audit"
    )
    assert db.read_bytes() == before
    # A read-only observation does not revoke the original grant or consume work.
    data["execution"] = {"mode": "manual", "batch_budget": 1}
    file.write_text(json.dumps(data))
    observed = run(["inspect", "--wi", wi, "--view", "watch"])
    assert (
        next(c["state"] for c in observed["capabilities"] if c["name"] == "local_work")
        == "available"
    )
    assert db.read_bytes() == before
    run(["resume", "--wi", wi])
    assert len(list(destination.iterdir())) == expected + 2
    assert all(p.read_bytes() == artifact.read_bytes() for p in destination.iterdir())


def test_readonly_host_cannot_carry_an_execution_grant():
    from tests_rewrite.test_workflow import BASE

    with pytest.raises(Rejected, match="Read-only Host"):
        replace(BASE, read_only_reason="policy_audit").validate()


@pytest.mark.parametrize(
    "fault_stage", ["before_pointer", "after_pointer", "after_activation_commit"]
)
def test_contract2_fixture_upgrades_atomically_and_cannot_downgrade(
    retained_store, fault_stage
):
    root, bundle, built, file, kernel, wi = retained_store
    release.install(bundle, built["sha256"], root, file, adopt_existing_store=True)
    db = Path(kernel._database)
    # Explicit synthetic former contract-2 guard protocol, not production recovery.
    with sqlite3.connect(db) as c:
        c.execute("DROP TRIGGER runtime_contract_update")
        c.execute("UPDATE runtime_contract SET minimum=2")
        c.execute(
            "CREATE TRIGGER runtime_contract_update BEFORE UPDATE ON runtime_contract BEGIN SELECT RAISE(ABORT,'immutable_writer_contract'); END"
        )

    def originals():
        with sqlite3.connect("file:" + str(db) + "?mode=ro", uri=True) as c:
            return {
                t: c.execute("SELECT * FROM " + t).fetchall()
                for t in [
                    "originals",
                    "journal",
                    "journal_heads",
                    "store_binding",
                    "store_custody",
                ]
            }

    before = originals()
    previous = (root / "active").resolve()
    data = json.loads(file.read_text())
    data["execution"] = {"mode": "audit", "batch_budget": 1}
    file.write_text(json.dumps(data))

    class Interrupted(BaseException):
        pass

    def fault(at):
        if at == fault_stage:
            raise Interrupted()

    with pytest.raises(Interrupted):
        release.install(bundle, built["sha256"], root, file, fault=fault)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT minimum FROM runtime_contract").fetchone()[0] == (
            3 if fault_stage == "after_activation_commit" else 2
        )
    receipt = release.install(bundle, built["sha256"], root, file)
    assert receipt["writer_contract"] == 3 and originals() == before
    settled = db.read_bytes()
    assert release.install(bundle, built["sha256"], root, file) == receipt
    assert db.read_bytes() == settled
    assert previous != (root / "active").resolve()
    assert kernel.inspect(Query(wi), None)["next_action"]["reason"] == "program_changed"
    with sqlite3.connect(db) as c:
        c.create_function("harness_writer_contract", 0, lambda: 2)
        c.create_function("harness_custody_id", 0, lambda: location.custody_id(c, db))
        c.create_function("harness_store_location", 0, lambda: location.current(c, db))
        c.create_function("harness_writer_id", 0, lambda: receipt["writer_admission"])
        with pytest.raises(sqlite3.IntegrityError, match="retired_writer_contract"):
            c.execute("UPDATE runtime_binding SET program='writer.v2.old-policy'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable_writer_contract"):
            c.execute("UPDATE runtime_contract SET minimum=2")
        with pytest.raises(sqlite3.IntegrityError, match="immutable_writer_contract"):
            c.execute("DELETE FROM runtime_contract")
        assert c.execute("SELECT minimum FROM runtime_contract").fetchone()[0] == 3
    assert originals() == before
