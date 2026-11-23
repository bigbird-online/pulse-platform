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
    SETTLED_REVISIT,
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
    return entry, Path(policy["views_root"]) / effect_service.STORE / f"{WI}.json"


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
            cursor.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            cursor.write_bytes(corrupted)
        assert main(entry, ["tick", "--work-items", "1"]) == 0
        response = json.loads(capsys.readouterr().out)
        assert [r["work_item_id"] for r in response["items"]] == [WI]
        assert response["items"][0]["progress"] == [] and not response["business_mutation"]
        seen = json.loads(cursor.read_bytes())["seen"]
        assert (seen["head"], seen["settled"], seen["exceeded"]) == (record, False, False)
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
    # An older program's single cursor is read once, then replaced by the store.
    legacy = cursor.parent.parent / effect_service.LEGACY
    legacy.parent.mkdir(mode=0o700)
    legacy.write_text(json.dumps({"seen": {WI: hint, "wi_gone": hint}}))
    assert main(entry, ["tick"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
    assert not legacy.exists() and json.loads(cursor.read_bytes())["journal"]["revision"] >= 1
    cursor.write_text(json.dumps({"seen": {**hint, "head": "older-record"}}))
    assert main(entry, ["tick"]) == 0
    assert [r["work_item_id"] for r in json.loads(capsys.readouterr().out)["items"]] == [WI]


def test_schedule_interleaves_due_changed_and_quiet_work_without_starving_any():
    now = 100_000.0

    def hint(at, head="h", settled=False, exceeded=False, due=False):
        return {"head": head, "at": now - at, "settled": settled, "exceeded": exceeded,
                "due": due}

    heads = {w: "h" for w in ("wi_a", "wi_b", "wi_c", "wi_d", "wi_e", "wi_f", "wi_g",
                              "wi_new")}
    heads["wi_c"] = heads["wi_g"] = "h2"
    seen = {
        "wi_a": hint(600),                                  # quiet, visited 10 min ago
        "wi_b": hint(60, settled=True),                     # settled terminal: skipped
        "wi_c": hint(60, settled=True),                     # settled, but its head changed
        "wi_d": hint(120, exceeded=True),                   # over quota: re-measure soon
        "wi_e": hint(1200),                                 # quiet for longest
        "wi_f": hint(30, due=True),                         # an effect it can still move
        "wi_g": hint(10),                                   # changed: new service work
    }
    # Due (wi_g, wi_f), priority (wi_d, wi_c) and rotation (wi_new, wi_e, wi_a) take
    # turns, the lead rotating with each tick.
    news = {"wi_g"}
    assert schedule(heads, seen, now, 0, news) == [
        "wi_f", "wi_d", "wi_new", "wi_g", "wi_c", "wi_e", "wi_a"]
    assert schedule(heads, seen, now, 1, news) == [
        "wi_d", "wi_new", "wi_f", "wi_c", "wi_e", "wi_g", "wi_a"]
    assert schedule(heads, seen, now, 2, news) == [
        "wi_new", "wi_f", "wi_d", "wi_e", "wi_g", "wi_c", "wi_a"]
    # Without news a changed head is only a priority.
    assert schedule(heads, seen, now) == [
        "wi_f", "wi_d", "wi_new", "wi_c", "wi_e", "wi_g", "wi_a"]
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


class Ledger:
    """Many WIs on one ledger. Every visit writes one disk observation, as every
    production visit did on 2026-11-21, so a head recorded before the visit always
    looks changed afterwards. Each WI's journal revision counts its writes;
    `pending` is what `read_journals` folds from them: each open service effect
    and the revision that requested it."""

    def __init__(self, count, now):
        self.now, self.writes = now, 0
        self.heads = {f"wi_{i:03d}": "h0" for i in range(count)}
        self.revisions = {wi: 1 for wi in self.heads}
        self.pending = {wi: {} for wi in self.heads}
        self.github = True  # False: every merge readback is unavailable.
        self.effects = {}  # WI -> effect statuses a visit observes and advances.
        self.green, self.visits, self.exceeded = False, [], set()
        self.quiet, self.during_visit = set(), None  # Visits that write nothing; a hook.

    def write(self, wi, work=False):
        self.writes += 1
        self.heads[wi] = f"h{self.writes}"
        self.revisions[wi] += 1
        if work:
            self.pending[wi][wi + ".effect"] = self.revisions[wi]

    def open(self, wi, state):
        """An effect another writer requested, left as `state`."""
        self.effects[wi] = state
        self.write(wi, work=True)

    def close(self, wi, state):
        self.effects[wi] = state
        self.pending[wi].pop(wi + ".effect", None)
        self.write(wi)

    def confirm(self, wi):
        """confirm_pull_request_merge, whose inline attempt found the checks unfinished."""
        self.open(wi, "merge_waiting")  # PullRequestMergeConfirmed, EffectRequested.
        self.write(wi)  # The inline attempt and its observed absence.

    def refresh(self):
        return "revision"

    def tree(self, revision):
        return {f".harness/core/journals/{wi}/{n:012d}.json": "blob"
                for wi, n in self.revisions.items()}

    def journals(self):
        return {wi: {"revision": self.revisions[wi], "pending": dict(self.pending[wi])}
                for wi in self.heads}

    def visit(self, wi):
        self.visits.append(wi)
        if self.during_visit:
            self.during_visit(wi)
        results = []
        if wi not in self.quiet:
            self.write(wi)  # The disk measurement.
            results.append({"status": "local_committed"})
        if self.effects.get(wi) == "merge_waiting":
            if self.green:
                self.close(wi, "merged")
                results.append({"status": "local_committed"})
            else:
                results.append({"status": "deferred", "reason": "checks_pending"})
        elif self.effects.get(wi) == "merge_unknown" and self.github:
            self.close(wi, "merged")  # The readback finds the merge GitHub made.
            results.append({"status": "local_committed"})
        elif self.effects.get(wi) == "allocation_requested":
            self.close(wi, "allocated")
            results.append({"status": "local_committed"})
        return results

    def observe(self, wi):
        kind, status = {
            "merge_waiting": ("merge_pull_request", "observed_absence"),
            "merged": ("merge_pull_request", "observed_success"),
            "unknown": ("workspace_retire", "unknown"),
            "merge_unknown": ("merge_pull_request", "unknown"),
            "allocation_requested": ("workspace_allocate", "requested"),
            "allocated": ("workspace_allocate", "observed_success"),
        }.get(self.effects.get(wi), (None, None))
        effects = [] if status is None else [
            {"effect_id": wi + ".effect", "effect_kind": kind, "status": status,
             "retry": {"attempts_remaining": 7},
             "observation_reason": {"unknown": "physical_resource_conflict_or_unavailable",
                                    "merge_unknown": "github_readback_unavailable"}.get(
                                        self.effects[wi])}]
        level = "exceeded" if wi in self.exceeded else "ok"
        return {"status": "read_only", "terminal": False, "disk_quota": {"level": level},
                "resources": [], "effects": effects}


def simulate(tmp_path, monkeypatch, world, cursor_seen, ticks, before_tick=None):
    """Run the real tick, one WI per tick, against `world` (no kernel or GitHub)."""
    from types import SimpleNamespace

    class Drain:
        def __init__(self, *args):
            pass

        def drain(self, wi, **kwargs):
            return world.visit(wi)

    monkeypatch.setattr(effect_service, "known_work_items", lambda ledger, rev: dict(world.heads))
    monkeypatch.setattr(effect_service, "read_journals",
                        lambda ledger, rev, known, kinds: (world.journals(), set()),
                        raising=False)
    monkeypatch.setattr(effect_service, "copies_once", lambda *a: (lambda: []))
    monkeypatch.setattr(effect_service, "agent_report", lambda copies: None)
    monkeypatch.setattr(effect_service, "reclaim_terminal", lambda *a: None)
    monkeypatch.setattr(effect_service, "measure_disk", lambda *a: None)
    monkeypatch.setattr(effect_service, "MaintenanceAdapter", lambda policy: None)
    monkeypatch.setattr(effect_service, "Effects", Drain)
    monkeypatch.setattr(effect_service, "observe", lambda kernel, host, wi: world.observe(wi))
    monkeypatch.setattr(effect_service, "time",
                        SimpleNamespace(time=lambda: world.now, monotonic=lambda: 0.0))
    root = tmp_path / "views"
    root.mkdir(mode=0o700, exist_ok=True)
    if cursor_seen is not None:
        (root / "effect-service-cursor.json").write_text(json.dumps({"seen": cursor_seen}))
    host = SimpleNamespace(policy={"views_root": str(root), "pull_request_gate": {},
                                   "execution": {"mode": "automatic", "batch_budget": 8}},
                           worker=lambda artifacts: None)
    for number in range(ticks):
        if before_tick:
            before_tick(number)
        effect_service.tick(None, host, world, budget=8, work_items=1)
        world.now += 180  # Production: about one WI per three-minute tick.


def longest_wait(world, wi, start=0):
    ticks = [n for n, visited in enumerate(world.visits) if visited == wi and n >= start]
    return max(b - a for a, b in zip([start - 1, *ticks], [*ticks, len(world.visits)]))


def production(count=60, stalled=17, busy=12):
    """The 2026-11-21/22 ownermac mix: `stalled` WIs whose open retirement reads back
    the same unknown every visit, `busy` WIs other writers change every few ticks
    (Agents, Relay, review dispatch), the rest quiet."""
    world = Ledger(count, 1_000_000.0)
    names = sorted(world.heads)
    for wi in names[1:1 + stalled]:
        world.open(wi, "unknown")
    churn = names[1 + stalled:1 + stalled + busy]

    def others_write(number):
        for wi in churn[number % 4::4]:
            world.write(wi)
    return world, others_write


def test_a_merge_confirmed_while_checks_run_merges_soon_after_green_in_a_busy_ledger(
        tmp_path, monkeypatch):
    """wi_20261120_666 / PR #1767 (2026-11-21): confirmed while its checks could not
    be read yet, the inline attempt recorded an absence; the service then did not
    return to the WI for over two hours, though its checks turned green, until a
    manual drain merged it. Each visit costs about one three-minute tick, and the
    priority queue it waited in held every WI whose open attempt read back the same
    unknown forever and every WI another writer had touched (about 30)."""
    world, others_write = production()
    target, confirm_at, green_at = "wi_000", 200, 210

    def before(number):
        others_write(number)
        if number == confirm_at:
            world.confirm(target)
        world.green = number >= green_at

    simulate(tmp_path, monkeypatch, world, None, 260, before)
    visits = [n for n, wi in enumerate(world.visits) if wi == target and n >= confirm_at]
    merged = [n for n in visits if n >= green_at]
    assert world.effects[target] == "merged"
    assert visits[0] - confirm_at <= 3, "the confirmation is news: visited within 3 ticks"
    assert merged[0] - green_at <= 3, "a merge waiting on checks is due: within 3 ticks"


def test_due_work_waits_at_most_three_ticks_per_due_wi_and_starves_nothing(
        tmp_path, monkeypatch):
    """AC-2: WIs with a merge waiting on its checks, or an allocation another writer
    requested, wait at most 3 x (due WIs) ticks, however many open attempts read back
    the same unknown and however many heads other writers change; every other WI
    still waits at most 3 x (the WIs of its queue) ticks."""
    world, others_write = production()
    waiting = ["wi_000", "wi_050", "wi_051"]
    requested = "wi_052"

    def before(number):
        others_write(number)
        if number == 200:
            for wi in waiting:
                world.confirm(wi)  # Their checks never finish within this run.
        if number == 250:
            world.open(requested, "allocation_requested")

    simulate(tmp_path, monkeypatch, world, None, 400, before)
    for wi in waiting:
        assert longest_wait(world, wi, 201) <= 3 * (len(waiting) + 1), wi
    allocated = [n for n, wi in enumerate(world.visits) if wi == requested and n >= 250]
    assert world.effects[requested] == "allocated" and allocated[0] - 250 <= 3 * 4
    for wi in world.heads:
        assert longest_wait(world, wi) <= 3 * len(world.heads), wi


def test_an_open_attempt_that_reads_back_unchanged_leaves_the_due_queue(
        tmp_path, monkeypatch):
    """A visit that leaves every advanceable effect as it found it made no progress;
    only a deferral, a retryable refusal or a changed observation keeps a WI due."""
    from harness_rewrite.effect_service import due, progress

    service = KINDS | {"merge_pull_request"}
    unknown = {"effects": [{"effect_id": "r", "effect_kind": "workspace_retire",
                            "status": "unknown", "retry": {"attempts_remaining": 5},
                            "observation_reason": "physical_resource_conflict_or_unavailable"}]}
    seen = {"progress": progress(unknown, service)}
    assert due(unknown, service, None, []), "a first observation is due once"
    assert not due(unknown, service, seen, [{"status": "local_committed"}])
    assert due(unknown, service, {"progress": "other"}, [])
    assert due(unknown, service, seen, [{"status": "deferred"}])
    backoff = {"status": "rejected", "error": {"code": "effect_retry_backoff", "retryable": True}}
    assert due(unknown, service, seen, [backoff])
    refused = {"status": "rejected", "error": {"code": "domain_conflict", "retryable": False}}
    assert not due(unknown, service, seen, [refused])
    exhausted = {"effects": [{**unknown["effects"][0], "status": "observed_absence",
                              "retry": {"attempts_remaining": 0}}]}
    assert not due(exhausted, service, None, [{"status": "deferred"}])


def journal_entry(wi, number, events, originals=None):
    return json.dumps({"originals": {} if originals is None else originals, "record": {
        "wi": wi, "revision": number, "originals": [], "events": events}}).encode()


class JournalLedger:
    """`read_journals`' view of a production ledger: a tree and its blobs."""

    def __init__(self, blobs, broken=False):
        self.blobs, self.broken, self.read = blobs, broken, []

    def tree(self, revision):
        return {path: "0" * 40 for path in self.blobs}

    def object_reader(self, mapping):
        from contextlib import contextmanager

        @contextmanager
        def reader():
            if self.broken:
                raise Rejected("ledger_unavailable", "Production object read timed out")

            def read(path):
                assert path in mapping
                self.read.append(path)
                if self.blobs[path] is None:  # The object stream dies here.
                    raise Rejected("ledger_unavailable", "Production object read timed out")
                return self.blobs[path]
            yield read
        return reader()


SERVICE = KINDS | {"merge_pull_request"}


def requested(effect, kind="merge_pull_request"):
    return {"kind": "EffectRequested", "payload": {"effect_id": effect, "effect_kind": kind}}


def observed(effect, outcome):
    return {"kind": "EffectObserved",
            "payload": {"effect_id": effect, "observation": {"outcome": outcome}}}


def test_read_journals_folds_open_service_effects_and_reads_only_new_entries():
    """Requests of the service's kinds open an effect, an absence or unknown keeps it,
    a success, failure or cancellation closes it, a repair retry reopens it; review
    dispatch and checkpoints are not service work. A second read reads nothing old."""
    from harness_rewrite.effect_service import journal_news, read_journals
    from harness_rewrite.production_segments import entry_path

    checkpoint = [{"kind": "WorkRecorded", "payload": {}}]
    entries = {
        ("wi_a", 1): [requested("m1"), {"kind": "PullRequestMergeConfirmed", "payload": {}}],
        ("wi_a", 2): [observed("m1", "absence")],
        ("wi_a", 3): checkpoint,
        ("wi_b", 1): [requested("m2"), requested("rev", "pr_review_dispatch")],
        ("wi_b", 2): [observed("m2", "success"), observed("rev", "unknown")],
        ("wi_c", 1): [requested("r1", "workspace_retire")],
        ("wi_c", 2): [observed("r1", "absence")],
        ("wi_c", 3): [{"kind": "EffectRetryRequested", "payload": {"effect_id": "r1"}}],
        ("wi_d", 1): [requested("a1", "workspace_allocate"),
                      {"kind": "EffectCancelled", "payload": {"effect_id": "a1"}}],
        ("wi_e", 1): [requested("m3"), observed("m3", "failure")],
    }
    ledger = JournalLedger({entry_path(*key): journal_entry(*key, events)
                            for key, events in entries.items()})
    journals, failed = read_journals(ledger, "rev", {}, SERVICE)
    assert failed == set()
    assert {wi: j["pending"] for wi, j in journals.items()} == {
        "wi_a": {"m1": 1}, "wi_b": {}, "wi_c": {"r1": 3}, "wi_d": {}, "wi_e": {}}
    assert {wi: j["revision"] for wi, j in journals.items()} == {
        "wi_a": 3, "wi_b": 2, "wi_c": 3, "wi_d": 1, "wi_e": 1}
    # Visited after the confirmation: no news; before the retry: news.
    seen = {"wi_a": {"revision": 2}, "wi_c": {"revision": 2}}
    assert journal_news(journals, failed, seen) == {"wi_c"}
    ledger.read.clear()
    again, _ = read_journals(ledger, "rev", journals, SERVICE)
    assert again == journals and ledger.read == []


@pytest.mark.parametrize("cursor", ["none", "older_program"])
def test_an_open_merge_is_due_on_a_cold_start_or_an_older_cursor(
        tmp_path, monkeypatch, cursor):
    """Review of #1820: news was read only after a revision the cursor recorded, so with
    no cursor, or one an older program wrote (no revision) while other writers kept
    changing heads, the one WI with a confirmed merge came 60th of 60. Every open
    service effect is news to a visit that has not recorded seeing it."""
    world, others_write = production(stalled=0)
    target = "wi_059"  # Sorted last, and visited longest ago.
    world.confirm(target)
    world.green = True
    seen = None
    if cursor == "older_program":
        seen = {wi: {"head": world.heads[wi], "at": world.now - 60 * (60 - i),
                     "settled": False, "exceeded": False, "active": False}
                for i, wi in enumerate(sorted(world.heads))}
        seen[target]["at"] = world.now  # Visited just before its merge was confirmed.
        seen[target]["head"] = "h0"
    simulate(tmp_path, monkeypatch, world, seen, 6, others_write)
    assert world.effects[target] == "merged"
    assert world.visits.index(target) <= 2, world.visits.index(target)


def test_a_malformed_journal_entry_is_only_its_wi_s_read_failure():
    """Review of #1820: an entry that decodes but has the wrong shape (originals a list,
    a null event) raised out of the tick before any WI was visited. It is now that
    WI's read failure: news once, and every other WI is read as usual."""
    from harness_rewrite.effect_service import journal_news, read_journals
    from harness_rewrite.production_segments import entry_path

    blobs = {
        entry_path("wi_ok", 1): journal_entry("wi_ok", 1, [requested("m")]),
        entry_path("wi_list", 1): journal_entry("wi_list", 1, [], originals=[]),
        entry_path("wi_null", 1): journal_entry("wi_null", 1, [None]),
        entry_path("wi_null", 2): journal_entry("wi_null", 2, [requested("n")]),
        entry_path("wi_shape", 1): journal_entry("wi_shape", 1, [{"kind": "EffectObserved",
                                                                  "payload": []}]),
        entry_path("wi_bytes", 1): b"\xff not JSON",
        entry_path("wi_after", 1): journal_entry("wi_after", 1, [requested("x")]),
    }
    journals, failed = read_journals(JournalLedger(blobs), "rev", {}, SERVICE)
    assert failed == {"wi_list", "wi_null", "wi_shape", "wi_bytes"}
    assert journals["wi_ok"]["pending"] == {"m": 1}
    assert journals["wi_after"]["pending"] == {"x": 1}
    assert journals["wi_null"] == {"revision": 2, "pending": {"n": 2}}, "later entries still read"
    assert journal_news(journals, failed, {}) >= failed
    # Skipped, not retried forever: the next tick reads nothing and reports nothing.
    again, failed_again = read_journals(JournalLedger(blobs), "rev", journals, SERVICE)
    assert failed_again == set() and again == journals
    # A failed batch read keeps the last state and reports every WI it covered.
    previous = {"wi_ok": {"revision": 0, "pending": {}}}
    kept, unread = read_journals(JournalLedger(blobs, broken=True), "rev", previous, SERVICE)
    assert kept["wi_ok"] == previous["wi_ok"] and unread == set(journals)
    # So does a stream that dies midway: no later entry is mistaken for malformed.
    dying = {**blobs, entry_path("wi_list", 1): None}
    kept, unread = read_journals(JournalLedger(dying), "rev", previous, SERVICE)
    assert kept["wi_ok"] == previous["wi_ok"] and unread == set(journals)


def test_a_tick_survives_a_malformed_journal_entry(tmp_path, capsys, monkeypatch):
    """The whole tick, on the real production ledger: a malformed entry in another
    WI's journal does not stop the visit of this one."""
    fixture = tmp_path / "git"
    fixture.mkdir()
    rig = ProductionRig(fixture)
    rig.approve()
    entry, cursor = automatic(tmp_path, rig)
    real = effect_service.read_journals

    def with_malformed(ledger, revision, known, kinds):
        tree = ledger.tree(revision)
        bad = ".harness/core/journals/wi_bad/000000000001.json"
        monkeypatch.setattr(ledger, "tree", lambda r: {**tree, bad: "0" * 40})
        return real(ledger, revision, known, kinds)
    monkeypatch.setattr(effect_service, "read_journals", with_malformed)
    assert main(entry, ["tick", "--work-items", "1"]) == 0
    assert [r["work_item_id"] for r in json.loads(capsys.readouterr().out)["items"]] == [WI]


def test_read_journals_reads_real_production_entries(tmp_path):
    """The production ledger's own entries load; a recorded decision that requests no
    service work opens nothing."""
    from harness_rewrite.effect_service import read_journals

    fixture = tmp_path / "git"
    fixture.mkdir()
    rig = ProductionRig(fixture)
    rig.approve()
    journals, failed = read_journals(rig.ledger, rig.ledger.refresh(), {}, SERVICE)
    assert failed == set() and journals[WI]["revision"] >= 1
    assert journals[WI]["pending"] == {}


def test_a_merge_whose_readback_is_unavailable_stays_due_without_starving_others(
        tmp_path, monkeypatch):
    """Review of #1820: a confirmed merge reading back `github_readback_unavailable`
    every visit fell to rotation, though GitHub recovering writes nothing to the
    ledger. A merge stays due; it is visited within 3 x (due WIs) ticks and merged
    within as many once GitHub answers, while every other WI keeps its bound."""
    world, others_write = production()
    unreadable = ["wi_000", "wi_050"]
    recover_at = 260

    def before(number):
        others_write(number)
        if number == 200:
            for wi in unreadable:
                world.open(wi, "merge_unknown")
        world.github = number >= recover_at

    simulate(tmp_path, monkeypatch, world, None, 320, before)
    for wi in unreadable:
        assert longest_wait(world, wi, 201) <= 3 * len(unreadable) or \
            world.effects[wi] == "merged", wi
        merged = [n for n, v in enumerate(world.visits) if v == wi and n >= recover_at]
        assert world.effects[wi] == "merged" and merged[0] - recover_at <= 3 * len(unreadable)
    for wi in world.heads:
        assert longest_wait(world, wi) <= 3 * len(world.heads), wi


def test_effect_service_rotation_bounds_every_wait(tmp_path, monkeypatch):
    """A WI with a merge waiting on its checks waits at most 3 x (such WIs) ticks;
    the others, including open attempts that never settle, still get every third
    tick at least, so they wait at most 3 x (the others) ticks."""
    world = Ledger(12, 1_000_000.0)
    waiting = ["wi_009", "wi_010", "wi_011"]
    for wi in waiting:
        world.effects[wi] = "merge_waiting"
    for wi in ("wi_000", "wi_001"):
        world.effects[wi] = "unknown"  # An attempt read back every visit, never settling.
    service = KINDS | {"merge_pull_request"}
    seen = {wi: {"head": "h0", "at": world.now - 60 * (i + 1), "settled": False,
                 "exceeded": False, "due": wi in waiting,
                 "progress": effect_service.progress(world.observe(wi), service)}
            for i, wi in enumerate(sorted(world.heads))}
    simulate(tmp_path, monkeypatch, world, seen, 72)
    for wi in world.heads:
        bound = 3 * len(waiting) if wi in waiting else 3 * (len(world.heads) - len(waiting))
        assert longest_wait(world, wi) <= bound, (wi, longest_wait(world, wi), bound)


def test_a_merge_confirmed_while_the_service_observes_is_not_taken_as_seen(
        tmp_path, monkeypatch):
    """Review of #1773: a confirmation landing after `observe` read the WI and before
    the cursor took its head must still count as a change."""
    world = Ledger(40, 1_000_000.0)
    target = "wi_000"
    seen = {wi: {"head": "h0", "at": world.now - 180 * (i + 1), "settled": False,
                 "exceeded": False} for i, wi in enumerate(sorted(world.heads)) if wi != target}
    seen[target] = {"head": "h0", "at": world.now - 60, "settled": False, "exceeded": False}
    world.write(target)  # An unrelated write: the target is visited first.
    world.green = True
    observe = world.observe

    def confirmed_meanwhile(wi):
        current = observe(wi)
        if wi == target and target not in world.effects:
            world.confirm(target)  # confirm_pull_request_merge lands now.
        return current
    world.observe = confirmed_meanwhile
    simulate(tmp_path, monkeypatch, world, seen, 6)
    visits = [n for n, wi in enumerate(world.visits) if wi == target]
    assert visits[0] == 0 and world.effects[target] == "merged"
    assert visits[1] <= 3, "the confirmation is news: back within 3 ticks"


def test_an_over_quota_wi_never_pushes_an_ordinary_wi_past_its_bound(tmp_path, monkeypatch):
    """Review of #1773: an over-quota boost let a persistently over-quota WI pass an
    ordinary one again and again (visited only at tick 8). Over quota is a priority
    reason; each queue keeps its 3 x (its WIs) bound."""
    world = Ledger(3, 1_000_000.0)
    waiting, over, plain = "wi_000", "wi_001", "wi_002"
    world.effects[waiting] = "merge_waiting"
    world.exceeded.add(over)
    seen = {wi: {"head": "h0", "at": world.now - 60 * (i + 1), "settled": False,
                 "exceeded": wi == over, "due": wi == waiting}
            for i, wi in enumerate(sorted(world.heads))}
    seen[plain]["at"] = world.now - 1  # Visited last: the worst start for it.
    simulate(tmp_path, monkeypatch, world, seen, 30)
    assert longest_wait(world, waiting) <= 3 and longest_wait(world, over) <= 3
    assert longest_wait(world, plain) <= 3


def waiting_merges(count):
    """`count` merges waiting on checks ahead of one runnable workspace allocation."""
    effects = [{"effect_id": f"merge{i}", "effect_kind": "merge_pull_request",
                "status": "observed_absence", "attempts": [{"id": "a", "at": "x"}],
                "required": False} for i in range(count)]
    return effects + [{"effect_id": "alloc", "effect_kind": "workspace_allocate",
                       "status": "requested", "attempts": [], "required": True,
                       "generation": 1, "snapshot": {"target": "t"}}]


class FakeKernel:
    def __init__(self, effects):
        self.effects, self.begun = effects, []

    def inspect(self, query, handle):
        return {"status": "read_only", "capabilities": [], "effects": self.effects,
                "work_item": {"local_revision": 1}, "result": {"current_observation": {}}}

    def execute(self, command, handle):
        if command.intent == "begin_effect":
            self.begun.append(command.input["effect_id"])
        return {"status": "local_committed", "result": {
            "current_observation": {}, "original_request_result": {"attempt_id": "n"}}}


class WaitingAdapter:
    def __init__(self):
        self.probed = []

    def deferrable(self, effect):
        return effect["effect_kind"] == "merge_pull_request"

    def defer(self, effect):
        self.probed.append(effect["effect_id"])
        return "checks_pending"

    def apply(self, effect, content):
        return "success", {}

    def readback(self, effect):
        return "success", {}


def test_deferred_merges_never_take_the_batch_from_a_runnable_effect():
    """Review of #1773: nine merges waiting on checks ahead of a runnable workspace
    allocation; with budget 8 the allocation must still begin in this drain."""
    kernel, adapter = FakeKernel(waiting_merges(9)), WaitingAdapter()
    results = Effects(kernel, adapter, lambda artifacts: None).drain("wi_x", budget=8)
    assert kernel.begun == ["alloc"]
    assert [r["status"] for r in results] == ["deferred"] * 9 + ["local_committed"]


def test_a_drain_probes_at_most_the_limit_and_rotates_the_rest_in():
    """Review of #1773: each probe reads GitHub, so a drain runs at most PROBE_LIMIT;
    the others wait (no write, no attempt), the runnable effect still begins, and
    every waiting merge is probed in some window (here within 40 of them)."""
    from harness_rewrite.effects import PROBE_LIMIT, PROBE_WINDOW

    probed = set()
    for window in range(40):
        kernel, adapter = FakeKernel(waiting_merges(5)), WaitingAdapter()
        results = Effects(kernel, adapter, lambda artifacts: None,
                          clock=lambda: window * PROBE_WINDOW).drain("wi_x", budget=8)
        assert len(adapter.probed) == PROBE_LIMIT and kernel.begun == ["alloc"]
        reasons = [r.get("reason") for r in results if r["status"] == "deferred"]
        assert sorted(reasons) == ["checks_pending"] * PROBE_LIMIT + ["probe_limit"] * 3
        probed.update(adapter.probed)
    assert probed == {f"merge{i}" for i in range(5)}


@pytest.mark.parametrize("failure", ["rejected", "raised"])
@pytest.mark.parametrize("case", ["merge_waiting", "confirmed_this_round"])
def test_a_failed_observation_keeps_the_last_observed_head_and_priority(
        tmp_path, monkeypatch, failure, case):
    """Review of #1773: one rejected readback after `visited_head` stored the new head
    and cleared `active` (now `due`). A merge already waiting lost its priority, and a
    confirmation landing in the same visit read as seen: merged only at tick 40 of 40."""
    world = Ledger(40, 1_000_000.0)
    target = "wi_000"
    seen = {wi: {"head": "h0", "at": world.now - 180 * (i + 1), "settled": False,
                 "exceeded": False} for i, wi in enumerate(sorted(world.heads)) if wi != target}
    seen[target] = {"head": "h0", "at": world.now - 60, "settled": False, "exceeded": False,
                    "due": case == "merge_waiting", "revision": 1}
    if case == "merge_waiting":
        world.effects[target] = "merge_waiting"
        world.quiet.add(target)  # Nothing new to write: the head stays h0.
    else:
        world.write(target)  # An unrelated write: the target is visited first.

        def confirm(wi):
            if wi == target and target not in world.effects:
                world.confirm(target)  # Lands before `visited_head`.
        world.during_visit = confirm
    observe, failures = world.observe, []

    def flaky(wi):
        if wi == target and not failures:
            failures.append(wi)
            if failure == "raised":
                raise Rejected("ledger_unavailable", "transient")
            return {"status": "rejected", "error": {"code": "ledger_unavailable"}}
        return observe(wi)
    world.observe = flaky

    def checks(number):
        world.green = number >= 1
    simulate(tmp_path, monkeypatch, world, seen, 6, checks)
    visits = [n for n, wi in enumerate(world.visits) if wi == target]
    assert visits[0] == 0 and failures == [target]
    assert world.effects[target] == "merged" and visits[1] <= 3


def test_one_wi_record_stays_within_its_limit_however_many_effects_are_open(tmp_path):
    """Review of #1820: the single cursor outgrew the limit its readers enforce. Each
    WI now has its own record, bounded by construction: at most PENDING_LIMIT open
    effects (the newest requests, which decide news) of bounded IDs."""
    from harness_rewrite.effect_service import (
        ENTRY_LIMIT, PENDING_LIMIT, STORE, read_store, write_entry)

    wi = "wi_20261123_001"
    pending = {("op_" + f"{n:x}".rjust(156, "f"))[:160]: n for n in range(1, 5 * PENDING_LIMIT)}
    visit = {"head": "h" * 64, "at": 1.0, "settled": False, "exceeded": False, "due": True,
             "revision": 10 ** 9, "progress": "p" * 64}
    write_entry(tmp_path, wi, visit, {"revision": 10 ** 9, "pending": pending})
    assert (tmp_path / STORE / f"{wi}.json").stat().st_size <= ENTRY_LIMIT
    seen, journals, _ = read_store(tmp_path, {wi: "h"})
    assert seen[wi] == visit
    kept = journals[wi]["pending"]
    assert len(kept) == PENDING_LIMIT and min(kept.values()) == 5 * PENDING_LIMIT - PENDING_LIMIT


def test_a_schedule_past_the_old_cursor_limit_reads_back_and_moves_on(tmp_path, monkeypatch):
    """Review of #1820: 4,000 visited WIs made a 1.2 MB cursor its readers refused, so
    the next tick started over. Per-WI records (6,000 here, over the old 1 MiB in
    all) read back whole, and the schedule keeps moving: each tick visits the least
    recently visited WI not yet visited."""
    from harness_rewrite.effect_service import STORE, read_store, write_entry

    world = Ledger(6000, 1_000_000.0)
    names = sorted(world.heads)
    (tmp_path / "views").mkdir(mode=0o700)
    for i, wi in enumerate(names):
        write_entry(tmp_path / "views", wi,
                    {"head": "h0", "at": world.now - 10 * (6000 - i), "settled": False,
                     "exceeded": False, "due": False, "revision": 1, "progress": "p" * 64},
                    {"revision": 1, "pending": {}})
    store = tmp_path / "views" / STORE
    assert sum(p.stat().st_size for p in store.iterdir()) > 1 << 20
    simulate(tmp_path, monkeypatch, world, None, 12)
    assert world.visits == names[:12], "least recently visited first, never from the start again"
    seen, journals, turn = read_store(tmp_path / "views", world.heads)
    assert len(seen) == len(journals) == 6000 and turn == 12 % 3
    assert all(seen[wi]["at"] >= world.now - 12 * 180 for wi in names[:12])


def test_a_directory_at_a_record_path_neither_stops_the_tick_nor_the_merge(
        tmp_path, monkeypatch):
    """Review of #1820 round 3: with one WI's record path a directory, every tick
    raised at its first record write and visited no WI, so a green merge elsewhere
    waited for good. The store is the service's private cache: the obstacle is
    replaced by the record."""
    from harness_rewrite.effect_service import STORE, read_store

    world = Ledger(3, 1_000_000.0)
    blocked, target = "wi_001", "wi_002"
    world.open(blocked, "unknown")
    world.confirm(target)
    world.green = True
    obstacle = tmp_path / "views" / STORE / f"{blocked}.json"
    (obstacle / "inside").mkdir(mode=0o700, parents=True)
    simulate(tmp_path, monkeypatch, world, None, 6)
    assert world.effects[target] == "merged" and world.visits.index(target) <= 3
    assert obstacle.is_file() and blocked in read_store(tmp_path / "views", world.heads)[0]


def test_a_record_write_that_fails_loses_only_that_record(tmp_path, monkeypatch, capsys):
    """Any other failure to write or remove one record is logged and loses only that
    record; the tick still visits, and every other record is written."""
    from harness_rewrite.effect_service import read_store

    world = Ledger(3, 1_000_000.0)
    blocked = "wi_001"
    real = effect_service.replace_file

    def failing(directory, name, content):
        if name == f"{blocked}.json":
            raise OSError(28, "No space left on device")
        real(directory, name, content)
    monkeypatch.setattr(effect_service, "replace_file", failing)
    simulate(tmp_path, monkeypatch, world, None, 9)
    assert "schedule wi_001 not recorded: OSError" in capsys.readouterr().err
    # It keeps its place in line instead of counting as never visited and taking
    # every turn: a priority WI (its head unknown) of one, the others rotate.
    assert sorted(world.visits[:3]) == sorted(world.heads)
    assert longest_wait(world, blocked) <= 3, world.visits
    assert all(longest_wait(world, wi) <= 3 * 2 for wi in ("wi_000", "wi_002")), world.visits
    seen = read_store(tmp_path / "views", world.heads)[0]
    assert {wi for wi, entry in seen.items() if not entry.get("lost")} == {"wi_000", "wi_002"}


def test_a_retry_of_an_effect_past_the_kept_limit_is_news():
    """Review of #1820 round 3: with 129 open effects the record keeps the newest 128;
    a retry of the oldest one then matched no tracked effect and was skipped. A
    retry of any effect not tracked makes its WI news."""
    from harness_rewrite.effect_service import (
        PENDING_LIMIT, journal_news, read_journals, read_store, write_entry)
    from harness_rewrite.production_segments import entry_path

    wi, count = "wi_20261123_002", PENDING_LIMIT + 1
    entries = {n: [requested(f"op_{n:04d}")] for n in range(1, count + 1)}
    blobs = {entry_path(wi, n): journal_entry(wi, n, events) for n, events in entries.items()}
    full, failed = read_journals(JournalLedger(blobs), "rev", {}, SERVICE)
    assert len(full[wi]["pending"]) == count and failed == set()
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_entry(root, wi, {"head": "h", "at": 1.0, "settled": False, "exceeded": False,
                               "due": False, "revision": count, "progress": ""}, full[wi])
        seen, known, _ = read_store(root, {wi: "h"})
    assert "op_0001" not in known[wi]["pending"]
    retry = {"kind": "EffectRetryRequested", "payload": {"effect_id": "op_0001"}}
    blobs[entry_path(wi, count + 1)] = journal_entry(wi, count + 1, [retry])
    journals, failed = read_journals(JournalLedger(blobs), "rev", known, SERVICE)
    assert failed == {wi} and journal_news(journals, failed, seen) == {wi}


class MergingKernel:
    """Merges of one WI waiting on checks; a begun merge is applied and observed."""

    def __init__(self, count):
        self.effects = [{"effect_id": f"merge{i}", "effect_kind": "merge_pull_request",
                         "status": "observed_absence", "attempts": [{"id": "a", "at": "x"}],
                         "required": True, "retry": {"attempts_remaining": 7},
                         "startable": True, "snapshot": {"number": i}}
                        for i in range(count)]
        self.begun = []

    def find(self, effect_id):
        return next(e for e in self.effects if e["effect_id"] == effect_id)

    def inspect(self, query, handle):
        return {"status": "read_only", "capabilities": [],
                "effects": [dict(e) for e in self.effects],
                "work_item": {"local_revision": 1}, "result": {"current_observation": {}}}

    def execute(self, command, handle):
        if command.intent == "begin_effect":
            self.begun.append(command.input["effect_id"])
            self.find(command.input["effect_id"])["status"] = "attempt_started"
        if command.intent == "observe_effect":
            self.find(command.input["effect_id"])["status"] = "observed_success"
        return {"status": "local_committed", "result": {
            "current_observation": {}, "original_request_result": {"attempt_id": "n"}}}


class GreenAdapter:
    """Every merge's checks have passed: a probe never defers, the merge succeeds."""

    def deferrable(self, effect):
        return effect["status"] == "observed_absence"

    def defer(self, effect):
        return None

    def apply(self, effect, content):
        return "success", {"reason": "merged_head_observed_on_main"}

    def readback(self, effect):
        return "success", {}


def test_merges_of_one_wi_turning_green_together_begin_two_per_visit_while_it_stays_due():
    """Review of #1820 round 3: one visit probes at most PROBE_LIMIT merges, so seven
    merges of one WI turning green together begin over ceil(7 / 2) = 4 visits, and
    the WI stays due until every one has begun."""
    from harness_rewrite.effects import PROBE_LIMIT, PROBE_WINDOW
    from harness_rewrite.effect_service import due

    kernel, previous = MergingKernel(7), None
    begun = []
    for visit in range(4):
        before = len(kernel.begun)
        results = Effects(kernel, GreenAdapter(), lambda artifacts: None,
                          clock=lambda: visit * PROBE_WINDOW).drain("wi_x", budget=8)
        begun.append(len(kernel.begun) - before)
        current = {"effects": [effect_service.effect_observation(e) for e in kernel.effects]}
        left = [e for e in kernel.effects if e["status"] != "observed_success"]
        assert due(current, SERVICE, previous, results) == bool(left), visit
        previous = {"progress": effect_service.progress(current, SERVICE)}
    assert begun == [PROBE_LIMIT] * 3 + [1] and len(set(kernel.begun)) == 7
