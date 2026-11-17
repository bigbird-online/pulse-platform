"""Installed service has no business authority and only consumes admitted effects."""

import json
import subprocess
import hashlib
from pathlib import Path
import sys

import pytest

from harness_rewrite import effect_service
from harness_rewrite.effect_service import (
    ServiceHost, MaintenanceAdapter, KINDS, observe, main, reclaim_request, reclaim_terminal, schedule, settled,
    CHANGED_PRIORITY, SETTLED_REVISIT,
)
from harness_rewrite.effects import Effects
from harness_rewrite.model import Command, Rejected, encoded
from tests_rewrite.test_workspaces import allocate, rows, retire, original
from tests_rewrite.test_workspaces import setup as workspace_setup

from tests_rewrite.test_production_ledger import Rig as ProductionRig, git, WI
from tests_rewrite.test_publication import publishing as publication_fixture, source_effects

setup = workspace_setup
publishing = publication_fixture


def installed(root, ledger=None, audit=False):
    directory = root / "core/releases/exact"
    directory.mkdir(parents=True)
    entry = directory / "harness.pyz"
    entry.write_bytes(b"isolated exact executable")
    entry.chmod(0o600)
    dispatch = root / "dispatch"
    dispatch.mkdir()
    (root / "deliveries").mkdir()
    policy = {
        "namespace": "team",
        "surface": "local",
        "production_ledger": ledger
        or {
            "repository": str(root / "source"),
            "identity": str(root / "remote"),
            "remote": "origin",
            "ref": "refs/heads/main",
        },
        "views_root": str(root / "views"),
        "delivery_root": str(root / "deliveries"),
        "dispatch_root": str(dispatch),
        "verification_actor": "verifier",
        "python": sys.executable,
        "node": sys.executable,
        "pi_binary": sys.executable,
        "profiles": [
            {
                "actor": "verifier",
                "role": "verifier",
                "session_root": str(dispatch),
                "artifact_roots": [str(root)],
            }
        ],
    }
    if audit:
        policy["execution"] = {"mode": "audit", "batch_budget": 8}
    path = directory / "host-policy.json"
    path.write_bytes(encoded(policy))
    path.chmod(0o600)
    import hashlib

    (directory / "installation.json").write_text(
        json.dumps(
            {
                "bundle_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
                "policy_sha256": hashlib.sha256(encoded(policy)).hexdigest(),
            }
        )
    )
    (root / "core/active").symlink_to("releases/exact")
    return entry


def attach(rig, host):
    old = rig.kernel._authenticate
    rig.kernel._authenticate = lambda handle: (
        host.authenticate(handle) if handle is host.handle else old(handle)
    )


def drain(rig, host, adapter=None):
    return Effects(rig.kernel, adapter or MaintenanceAdapter(host.policy), host.worker).drain(
        rig.wi, kinds=KINDS
    )


def test_service_cannot_create_scope_or_resource_intentions(setup, tmp_path):
    rig = setup[0]
    host = ServiceHost(installed(tmp_path))
    attach(rig, host)
    for intent, data in [
        ("create_lightweight", {}),
        ("grant_scope", {}),
        ("terminate_work", {}),
        ("retire_resources", {"resource_ids": ["unknown"], "reason": "not approved"}),
        ("retire_terminal_resources", {}),  # the WI is still active
        ("record_work", {}),
    ]:
        result = rig.kernel.execute(
            Command("forbidden." + intent, intent, data, rig.wi), host.worker(())
        )
        assert result["status"] == "rejected", result
    assert host.current.source is None
    assert host.current.principal() == {
        "actor": "delivery-worker",
        "session": "delivery-service",
        "surface": "local",
    }
    with pytest.raises(PermissionError):
        host.authenticate(object())


def test_service_runs_real_approved_resources_and_reclaims_a_terminal_wi(setup, tmp_path):
    rig, bindings, _, _ = setup
    host = ServiceHost(installed(tmp_path))
    attach(rig, host)
    original(allocate(setup))
    assert all(r["status"] == "local_committed" for r in drain(rig, host))
    path = Path(rows(rig)[0]["path"])
    assert path.exists()
    assert reclaim_terminal(rig.kernel, host, rig.wi) is None, "an active WI keeps its workspaces"
    original(
        rig.approve(
            "terminate_work",
            {"kind": "cancelled", "reason": "End fixture"},
            actor=rig.contexts["owner"],
        )
    )
    assert drain(rig, host) == [] and path.exists(), "draining alone never deletes"
    view = observe(rig.kernel, host, rig.wi)
    assert view["resources"][0]["resource_id"] == rows(rig)[0]["resource_id"]
    assert view["business_mutation"] is False
    original(reclaim_terminal(rig.kernel, host, rig.wi))
    assert all(r["status"] == "local_committed" for r in drain(rig, host))
    assert not path.exists() and not path.parent.exists() and drain(rig, host) == []
    assert rows(rig)[0]["status"] == "retired"
    assert reclaim_terminal(rig.kernel, host, rig.wi) is None
    assert (Path(bindings[0].source) / "human-draft").exists()


def test_terminal_reclaim_preserves_a_dirty_workspace(setup, tmp_path):
    rig = setup[0]
    host = ServiceHost(installed(tmp_path))
    attach(rig, host)
    original(allocate(setup))
    drain(rig, host)
    path = Path(rows(rig)[0]["path"])
    (path / "unsaved.txt").write_text("work in progress")
    original(
        rig.approve(
            "terminate_work",
            {"kind": "cancelled", "reason": "End fixture"},
            actor=rig.contexts["owner"],
        )
    )
    original(reclaim_terminal(rig.kernel, host, rig.wi))
    drain(rig, host)
    assert (path / "unsaved.txt").read_text() == "work in progress"
    assert rows(rig)[0]["status"] != "retired"
    assert reclaim_terminal(rig.kernel, host, rig.wi) is None, "a refused retirement is not re-requested"


def test_service_restarts_original_interrupted_retirement_once(setup, tmp_path):
    rig = setup[0]
    entry = installed(tmp_path)
    host = ServiceHost(entry)
    attach(rig, host)
    original(allocate(setup))
    drain(rig, host)
    original(retire(setup))

    class Crash(BaseException):
        pass

    def fault(stage):
        if stage == "after_worktree_remove":
            raise Crash()

    adapter = MaintenanceAdapter(host.policy)
    adapter.workspaces.fault = fault
    with pytest.raises(Crash):
        drain(rig, host, adapter)
    successor = ServiceHost(entry)
    attach(rig, successor)
    drain(rig, successor)
    assert rows(rig)[0]["status"] == "retired"
    effect = next(e for e in rig.state()["effects"] if e["effect_kind"] == "workspace_retire")
    assert len(effect["attempts"]) == 1 and effect["status"] == "observed_success"
    assert drain(rig, successor) == []


def test_retired_absolute_program_and_changed_policy_are_rejected(tmp_path):
    entry = installed(tmp_path)
    host = ServiceHost(entry)
    (entry.parent.parent.parent / "active").unlink()
    with pytest.raises(Rejected, match="currently installed"):
        host.authenticate(host.handle)
    (entry.parent.parent.parent / "active").symlink_to("releases/exact")
    path = entry.parent / "host-policy.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(Rejected, match="changed"):
        host.authenticate(host.handle)


def test_service_filters_other_effects_before_the_batch_budget(setup, tmp_path):
    rig = setup[0]
    host = ServiceHost(installed(tmp_path))
    attach(rig, host)
    original(allocate(setup))
    assert (
        Effects(rig.kernel, MaintenanceAdapter(host.policy), host.worker).drain(
            rig.wi, budget=1, kinds=frozenset({"publish_source"})
        )
        == []
    )
    assert not Path(rows(rig)[0]["path"]).exists()
    assert drain(rig, host)[0]["status"] == "local_committed"


def test_actual_production_reader_service_never_adopts_an_old_wi(tmp_path, capsys):
    fixture = tmp_path / "git"
    fixture.mkdir()
    rig = ProductionRig(fixture)
    entry = installed(tmp_path, rig.ledger.config)
    assert main(entry, ["inspect", "--wi", WI]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["work_item"]["id"] == WI
    assert response["resources"] == [] and response["effects"] == []
    assert main(entry, ["drain", "--wi", WI]) == 0
    assert git(rig.remote, "rev-parse", "main") == rig.before
    assert not (rig.a / ".harness/core").exists()


def test_audit_installation_cannot_drain(tmp_path, capsys):
    fixture = tmp_path / "git"
    fixture.mkdir()
    rig = ProductionRig(fixture)
    entry = installed(tmp_path, rig.ledger.config, audit=True)
    assert main(entry, ["drain", "--wi", WI]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "policy_read_only"
    assert git(rig.remote, "rev-parse", "main") == rig.before


def test_service_never_dispatches_models_or_notices(tmp_path):
    host = ServiceHost(installed(tmp_path))
    adapter = MaintenanceAdapter(host.policy)
    for kind in ("notification_send", "verification_dispatch", "deliver_artifact"):
        assert kind not in host.current.effect_kinds
        with pytest.raises(Rejected):
            adapter.adapter({"effect_kind": kind})


def automatic(tmp_path, rig):
    entry = installed(tmp_path, rig.ledger.config)
    policy_path = entry.parent / "host-policy.json"
    policy = json.loads(policy_path.read_bytes())
    policy["execution"] = {"mode": "automatic", "batch_budget": 8}
    policy_path.write_bytes(encoded(policy))
    receipt_path = entry.parent / "installation.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["policy_sha256"] = hashlib.sha256(encoded(policy)).hexdigest()
    receipt_path.write_bytes(encoded(receipt))
    return entry, Path(policy["views_root"]) / "effect-service-cursor.json"


def test_periodic_service_cursor_is_rebuildable_and_not_an_authority(tmp_path, capsys):
    fixture = tmp_path / "git"
    fixture.mkdir()
    rig = ProductionRig(fixture)
    rig.approve()
    entry, cursor = automatic(tmp_path, rig)
    original_head = git(rig.remote, "rev-parse", "main")
    record = git(rig.remote, "rev-parse", f"main:.harness/core/work-items/{WI}.json")
    for corrupted in (
        None,
        b"not JSON",
        b'{"last":"fabricated-deletion-approval"}',
        b'{"seen":{"%s":{"head":"x","at":"never","settled":true,"exceeded":false}}}' % WI.encode(),
    ):
        if corrupted is None:
            cursor.unlink(missing_ok=True)
        else:
            cursor.write_bytes(corrupted)
        assert main(entry, ["tick", "--work-items", "1"]) == 0
        response = json.loads(capsys.readouterr().out)
        assert [r["work_item_id"] for r in response["items"]] == [WI]
        assert response["items"][0]["progress"] == [] and not response["business_mutation"]
        seen = json.loads(cursor.read_bytes())["seen"]
        assert list(seen) == [WI]
        assert (seen[WI]["head"], seen[WI]["settled"], seen[WI]["exceeded"]) == (record, False, False)
    assert git(rig.remote, "rev-parse", "main") == original_head


def test_tick_skips_a_settled_wi_until_its_head_changes(tmp_path, capsys):
    fixture = tmp_path / "git"
    fixture.mkdir()
    rig = ProductionRig(fixture)
    rig.approve()
    entry, cursor = automatic(tmp_path, rig)
    record = git(rig.remote, "rev-parse", f"main:.harness/core/work-items/{WI}.json")
    now = __import__("time").time()
    hint = {"head": record, "at": now, "settled": True, "exceeded": False}
    cursor.parent.mkdir(mode=0o700)
    cursor.write_text(json.dumps({"seen": {WI: hint, "wi_gone": hint}}))
    assert main(entry, ["tick"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
    cursor.write_text(json.dumps({"seen": {WI: {**hint, "head": "older-record"}}}))
    assert main(entry, ["tick"]) == 0
    assert [r["work_item_id"] for r in json.loads(capsys.readouterr().out)["items"]] == [WI]


def test_schedule_prefers_changed_and_over_quota_work_without_starving_the_rest():
    now = 100_000.0

    def hint(at, head="h", settled=False, exceeded=False):
        return {"head": head, "at": now - at, "settled": settled, "exceeded": exceeded}

    heads = {w: "h" for w in ("wi_a", "wi_b", "wi_c", "wi_d", "wi_e", "wi_new")}
    heads["wi_c"] = "h2"
    seen = {
        "wi_a": hint(600),                                  # quiet, visited 10 min ago
        "wi_b": hint(60, settled=True),                     # settled terminal: skipped
        "wi_c": hint(60, settled=True),                     # settled, but its head changed
        "wi_d": hint(120, exceeded=True),                   # over quota: re-measure soon
        "wi_e": hint(CHANGED_PRIORITY + 300),               # quiet for longer than the boost
    }
    assert schedule(heads, seen, now) == ["wi_new", "wi_e", "wi_d", "wi_c", "wi_a"]
    later = now + SETTLED_REVISIT
    assert "wi_b" in schedule(heads, seen, later), "a settled WI is still revisited rarely"


def test_settled_means_terminal_with_nothing_left_to_advance_or_reclaim():
    done = {
        "terminal": True,
        "disk_quota": {"level": "ok"},
        "effects": [
            {"effect_kind": "workspace_retire", "status": "observed_success", "retry": {}},
            {"effect_kind": "merge_pull_request", "status": "observed_failure", "retry": {}},
            {"effect_kind": "workspace_allocate", "status": "observed_absence",
             "retry": {"attempts_remaining": 0, "next_action": "repair_then_retry_effect"}},
        ],
        "resources": [{"status": "observed_success", "retirement": {"effect_id": "x"}}],
    }
    assert settled(done)
    assert not settled({**done, "terminal": False})
    assert not settled({**done, "disk_quota": {"level": "exceeded"}})
    assert not settled({**done, "resources": [{"status": "observed_success", "retirement": None}]})
    for pending in ("requested", "attempt_started", "unknown", "observed_absence"):
        effect = {"effect_kind": "workspace_retire", "status": pending,
                  "retry": {"attempts_remaining": 3}}
        assert not settled({**done, "effects": [effect]}), pending
    # Eight attempts exhaust new sends, not the readback of the open original attempt.
    from harness_rewrite.delivery import retry_policy

    exhausted = retry_policy({"attempts": [{"at": "2026-11-01T00:00:00+00:00"}] * 8})
    assert exhausted["attempts_remaining"] == 0
    for open_attempt in ("attempt_started", "unknown", "observed_failure"):
        effect = {"effect_kind": "workspace_retire", "status": open_attempt, "retry": exhausted}
        assert not settled({**done, "effects": [effect]}), open_attempt


def test_measure_and_drain_remeasure_disk_before_effects(tmp_path, capsys, monkeypatch):
    fixture = tmp_path / "git"
    fixture.mkdir()
    rig = ProductionRig(fixture)
    entry = installed(tmp_path, rig.ledger.config)
    calls = []
    monkeypatch.setattr(effect_service, "measure_disk", lambda *a: calls.append("measure"))
    monkeypatch.setattr(Effects, "drain", lambda *a, **k: calls.append("drain") or [])
    assert main(entry, ["measure", "--wi", WI]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["work_item"]["id"] == WI and set(response["disk_quota"]) >= {"level", "bytes"}
    assert calls == ["measure"]
    assert main(entry, ["drain", "--wi", WI]) == 0
    assert calls == ["measure", "measure", "drain"]
    assert main(entry, ["measure"]) == 2, "one named WI is required"
    capsys.readouterr()
    assert git(rig.remote, "rev-parse", "main") == rig.before


def test_audit_installation_cannot_measure(tmp_path, capsys):
    fixture = tmp_path / "git"
    fixture.mkdir()
    rig = ProductionRig(fixture)
    entry = installed(tmp_path, rig.ledger.config, audit=True)
    assert main(entry, ["measure", "--wi", WI]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "policy_read_only"


def test_real_sealed_cli_runs_without_agent_session_or_legacy_imports(tmp_path):
    from harness_rewrite.release import build

    fixture = tmp_path / "git"
    fixture.mkdir()
    rig = ProductionRig(fixture)
    entry = installed(tmp_path, rig.ledger.config)
    source = tmp_path / "source"
    package = source / "agent-harness/harness_rewrite"
    package.mkdir(parents=True)
    origin = Path(__file__).resolve().parents[1] / "harness_rewrite"
    for path in origin.iterdir():
        if path.suffix in {".py", ".mjs"}:
            (package / path.name).write_bytes(path.read_bytes())
    docs = source / "agent-harness/project-specs"
    docs.mkdir()
    (docs / "rewrite-native-operations.md").write_text("Isolated exact service invocation")
    git(source, "init")
    git(source, "add", ".")
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Exact fixture",
    )
    entry.unlink()
    built = build(source, "HEAD", entry)
    entry.chmod(0o600)
    receipt_path = entry.parent / "installation.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["bundle_sha256"] = built["sha256"]
    receipt_path.write_bytes(encoded(receipt))
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(entry), "effect-service", "inspect", "--wi", WI],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["work_item"]["id"] == WI and response["source"] == "production_git"
    assert git(rig.remote, "rev-parse", "main") == rig.before
    import zipfile

    with zipfile.ZipFile(entry) as archive:
        assert not any(p.startswith("harness/") for p in archive.namelist())
    assert not list(tmp_path.rglob("kernel.sqlite3"))


@pytest.mark.parametrize("fault_stage", ["after_push", "after_adoption"])
def test_service_recovers_original_source_publication_and_adoption(
    publishing, tmp_path, fault_stage
):
    rig, _, path, remote, consumer, head, _, _, _, _ = publishing
    entry = installed(tmp_path)
    host = ServiceHost(entry)
    attach(rig, host)
    (path / "uncommitted").write_text("later human change stays local")

    class Crash(BaseException):
        pass

    def fault(stage):
        if stage == fault_stage:
            raise Crash()

    adapter = MaintenanceAdapter(host.policy)
    adapter.publication.fault = fault
    with pytest.raises(Crash):
        drain(rig, host, adapter)
    restarted = ServiceHost(entry)
    attach(rig, restarted)
    for _ in range(3):
        drain(rig, restarted)
    assert git(remote, "rev-parse", "main") == git(consumer, "rev-parse", "HEAD") == head
    assert all(e["status"] == "observed_success" for e in source_effects(rig))
    assert all(len(e["attempts"]) == 1 for e in source_effects(rig))
    assert (path / "uncommitted").read_text() == "later human change stays local"
    assert drain(rig, restarted) == []


@pytest.mark.parametrize("fault_stage", ["before_push", "before_adoption"])
def test_unobserved_send_is_reported_and_never_inferred_absent(publishing, tmp_path, fault_stage):
    rig, _, _, remote, consumer, head, _, _, _, _ = publishing
    entry = installed(tmp_path)
    host = ServiceHost(entry)
    attach(rig, host)

    class Crash(BaseException):
        pass

    def fault(stage):
        if stage == fault_stage:
            raise Crash()

    adapter = MaintenanceAdapter(host.policy)
    adapter.publication.fault = fault
    with pytest.raises(Crash):
        drain(rig, host, adapter)
    before_remote = git(remote, "rev-parse", "main")
    before_consumer = git(consumer, "rev-parse", "HEAD")
    for _ in range(2):
        drain(rig, host)
    assert git(remote, "rev-parse", "main") == before_remote
    assert git(consumer, "rev-parse", "HEAD") == before_consumer != head
    view = observe(rig.kernel, host, rig.wi)
    uncertain = next(e for e in view["effects"] if e["status"] == "unknown")
    assert uncertain["observation_reason"] in {
        "original_send_not_observed",
        "original_adoption_not_observed",
    }
    assert uncertain["retry"]["next_action"] == "readback_original_then_resume"
    assert all(len(e["attempts"]) == 1 for e in source_effects(rig) if e["attempts"])


def test_one_corrupt_work_item_does_not_stop_the_next_service_observation(tmp_path, capsys):
    fixture = tmp_path / "git"
    fixture.mkdir()
    rig = ProductionRig(fixture)
    rig.approve()
    git(rig.a, "fetch", "origin", "main")
    git(rig.a, "merge", "--ff-only", "origin/main")
    bad = "wi_20261008_001"
    path = rig.a / ".harness/core/work-items" / f"{bad}.json"
    path.write_text('{"broken":true}')
    git(rig.a, "add", ".")
    git(rig.a, "commit", "-m", "Isolated corrupt item")
    git(rig.a, "push", "origin", "main")
    head = git(rig.remote, "rev-parse", "main")
    entry, _ = automatic(tmp_path, rig)
    assert main(entry, ["tick"]) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "partial"
    assert [r["work_item_id"] for r in response["items"]] == [bad, WI]
    assert response["items"][0]["error"] and not response["items"][1]["error"]
    assert git(rig.remote, "rev-parse", "main") == head


def test_a_failed_effect_no_longer_holds_a_terminal_wi_s_workspaces():
    from harness_rewrite.workspaces import unsettled

    def state(*statuses):
        return {"effects": {str(i): {"status": s, "effect_kind": "verification_dispatch"}
                            for i, s in enumerate(statuses)}}

    assert not unsettled(state("observed_success", "cancelled", "observed_failure"))
    for open_status in ("requested", "attempt_started", "unknown", "observed_absence"):
        assert unsettled(state("observed_failure", open_status)), open_status
    assert not unsettled({"effects": {"r": {"status": "unknown", "effect_kind": "workspace_retire"}}})


def test_a_deferred_reclaim_is_asked_again_once_its_effects_settle():
    def effects(status, kind="verification_dispatch"):
        return [{"effect_id": "e", "status": status, "effect_kind": kind},
                {"effect_id": "retire", "status": "unknown", "effect_kind": "workspace_retire"}]

    waiting = reclaim_request("wi", ["w1", "w2"], effects("unknown"))
    assert reclaim_request("wi", ["w1", "w2"], effects("unknown")) == waiting  # unchanged: replayed
    settled_id = reclaim_request("wi", ["w1", "w2"], effects("observed_failure"))
    assert settled_id != waiting  # settled: a new decision
    # A failed delivery still defers; its recovery to success is a new decision too.
    failed = reclaim_request("wi", ["w1", "w2"], effects("observed_failure", "deliver_artifact"))
    assert failed != reclaim_request("wi", ["w1", "w2"], effects("observed_success", "deliver_artifact"))
    # Nor is it the bare-resources ID an older installation deferred under.
    from harness_rewrite.model import digest
    assert settled_id != "reclaim." + digest({"wi": "wi", "resources": ["w1", "w2"]})


def test_a_reclaim_deferred_by_an_open_effect_retires_once_it_settles(setup, tmp_path):
    rig = setup[0]
    host = ServiceHost(installed(tmp_path))
    attach(rig, host)
    original(allocate(setup, names=("one",)))
    drain(rig, host)
    # A second allocation still open when the WI ends: retirement has to wait for it.
    original(allocate(setup, names=("two",), request="late-allocation"))
    original(rig.approve("terminate_work", {"kind": "cancelled", "reason": "End fixture"},
                         actor=rig.contexts["owner"]))
    first = original(reclaim_terminal(rig.kernel, host, rig.wi))
    assert first["deferred"] == "unsettled_effects"
    # Unchanged, the same decision is only replayed.
    assert original(reclaim_terminal(rig.kernel, host, rig.wi)) == first
    drain(rig, host)  # The obsolete allocation settles without acting.
    assert [r["repo_id"] for r in rows(rig)] == ["one"]  # The same pending resources as before.
    retired = original(reclaim_terminal(rig.kernel, host, rig.wi))
    assert "deferred" not in retired and retired["effect_ids"]
    drain(rig, host)
    assert {r["status"] for r in rows(rig)} == {"retired"}
