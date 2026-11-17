"""One authority, real independent Git clients, and unchanged historical records."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from harness_rewrite import Command, Kernel, Query, SourceInput
from harness_rewrite.domain import INTENTS
from harness_rewrite.model import Rejected, digest
from harness_rewrite.production_ledger import ProductionLedger
from tests_rewrite.test_workflow import BASE

WI = "wi_20261008_550"
POLICY = {
    "schema_version": "1.0",
    "current_epoch": "production",
    "legacy_cutoff": "2026-01-01T00:00:00Z",
    "allow_work_items": [],
    "required_result_refs": ["CR-ONE"],
    "registered_operating_area_refs": ["OA-PULSE"],
    "registered_operating_area_work_classes": ["improvement"],
}
RECORD = {
    "work_item_id": WI,
    "title": "Preserve original production work",
    "owner": "producer",
    "status": "draft",
    "acceptance_criteria": ["first", "second"],
    "active_executor": None,
    "created_at": "2026-10-08T00:00:00Z",
    "updated_at": "2026-10-08T00:00:00Z",
    "main_only": True,
    "source": "Original confirmed requirements",
    "historical_extension": {"must": "survive"},
}


def git(path, *args):
    return (
        subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


def v1_record(rig, wi=WI):
    """Construct the former on-disk format from fixture facts for upgrade tests."""
    import base64
    from harness_rewrite.production_ledger import SCHEMA
    with rig.ledger.connection(write=False, work_item_id=wi) as conn:
        _, _, _, history = Kernel._replay(conn, BASE.namespace, wi)
        originals = {sha: base64.b64encode(rig.ledger.original(conn, sha)).decode()
                     for entry in history for sha in entry["originals"]}
    return {"schema": SCHEMA, "namespace": BASE.namespace, "work_item_id": wi,
            "entries": history, "originals": originals, "projection": {}}


class Rig:
    def __init__(self, root, record=None):
        self.remote = root / "authority.git"
        self.remote.mkdir()
        git(self.remote, "init", "--bare", "--initial-branch=main")
        self.a = root / "client-a"
        git(root, "clone", str(self.remote), str(self.a))
        for name, value in [
            ("user.name", "Test Writer"),
            ("user.email", "fixture@example.invalid"),
        ]:
            git(self.a, "config", name, value)
        self.record = dict(record or RECORD)
        for name, value in {
            f"work-items/{WI}.yaml": self.record,
            "platform-epoch.yaml": POLICY,
        }.items():
            path = self.a / ".harness" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
        event = self.a / ".harness/events" / (WI + ".events.yaml")
        event.parent.mkdir()
        event.write_text(
            "---\nevent_id: original\nevent_type: work_item.created\nwork_item_id: " + WI + "\n"
        )
        git(self.a, "add", ".harness")
        git(self.a, "commit", "-m", "Original production history")
        git(self.a, "push", "origin", "main")
        self.before = git(self.remote, "rev-parse", "main")
        self.b = root / "client-b"
        git(root, "clone", str(self.remote), str(self.b))
        for name, value in [
            ("user.name", "Test Writer"),
            ("user.email", "fixture@example.invalid"),
        ]:
            git(self.b, "config", name, value)
        self.context = replace(BASE, allowed_intents=INTENTS)
        self.ledger = self.backend(self.a)
        self.kernel = self.client(self.ledger)

    def backend(self, path):
        return ProductionLedger(
            {
                "repository": str(path),
                "remote": "origin",
                "ref": "refs/heads/main",
                "identity": str(self.remote),
            },
            BASE.namespace,
        )

    def client(self, ledger):
        return Kernel(
            self.a.parent / "must-not-exist.sqlite",
            lambda _: self.context,
            storage=ledger,
            clock=lambda: "2026-10-09T00:00:00Z",
        )

    def approve(self, kernel=None, request="affiliation"):
        data = {
            "type": "operating_area",
            "ref": "OA-PULSE",
            "work_class": "improvement",
            "reason": "Actual human decision in this original session",
        }
        self.context = replace(
            self.context,
            source=SourceInput(
                "native-confirmation",
                "native_human",
                b"Confirmed OA-PULSE improvement",
                "record_affiliation",
                digest({"intent": "record_affiliation", "work_item_id": WI, "input": data}),
            ),
        )
        return (kernel or self.kernel).execute(
            Command(request, "record_affiliation", data, WI), "native"
        )


def test_read_and_continue_existing_wi_do_not_create_another_ledger(tmp_path):
    rig = Rig(tmp_path)
    observed = rig.kernel.inspect(Query(WI), "native")
    assert observed["status"] == "read_only", observed
    view = observed["result"]["current_observation"]
    assert view["definition"]["acceptance"] == RECORD["acceptance_criteria"]
    assert view["production_history"]["record"] == RECORD
    resumed = rig.kernel.resume(WI, "native")
    assert resumed["status"] == "human_decision_required", resumed
    assert resumed["next_action"]["intent"] == "resolve_business_affiliation"
    assert resumed["result"]["current_observation"]["business_progress"] == "draft"
    assert git(rig.remote, "rev-parse", "main") == rig.before
    assert not (tmp_path / "must-not-exist.sqlite").exists()
    assert not (rig.a / ".harness/core").exists()


@pytest.mark.parametrize(
    "status",
    [
        "draft",
        "ready",
        "in_progress",
        "blocked",
        "awaiting_verification",
        "completed",
        "failed",
        "cancelled",
        "superseded",
        "migrated",
    ],
)
def test_every_historical_status_and_extension_is_visible_without_execution(tmp_path, status):
    rig = Rig(tmp_path, {**RECORD, "status": status, "active_executor": "historical-owner"})
    result = rig.kernel.inspect(Query(WI), "native")
    assert result["status"] == "read_only", result
    assert result["work_item"]["business_progress"] == status
    assert all(
        c["state"] == "unavailable" for c in result["capabilities"] if c["name"] == "local_work"
    )
    listing = rig.kernel.inspect(Query(view="list"), "native")
    assert listing["result"]["items"][0]["business_progress"] == status
    assert git(rig.remote, "rev-parse", "main") == rig.before


def test_human_decision_is_published_once_and_other_client_consumes_it(tmp_path):
    rig = Rig(tmp_path)
    original_definition = git(rig.remote, "show", f"main:.harness/work-items/{WI}.yaml")
    original_events = git(rig.remote, "show", f"main:.harness/events/{WI}.events.yaml")
    result = rig.approve()
    assert result["status"] == "local_committed", result
    committed = git(rig.remote, "rev-parse", "main")
    assert committed != rig.before
    assert git(rig.remote, "show", f"main:.harness/work-items/{WI}.yaml") == original_definition
    assert git(rig.remote, "show", f"main:.harness/events/{WI}.events.yaml").startswith(
        original_events
    )
    second = rig.client(rig.backend(rig.b))
    assert second.resume(WI, "native")["status"] == "read_only"
    assert rig.approve(second)["status"] == "local_committed"
    assert git(rig.remote, "rev-parse", "main") == committed
    assert git(rig.a, "rev-parse", "HEAD") == rig.before
    assert not (tmp_path / "must-not-exist.sqlite").exists()


def test_candidate_is_not_confirmation_and_missing_affiliation_writes_nothing(tmp_path):
    rig = Rig(tmp_path)
    result = rig.kernel.execute(Command("start", "start_work", {}, WI), "native")
    assert result["error"]["code"] == "business_affiliation_missing", result
    assert git(rig.remote, "rev-parse", "main") == rig.before
    data = {
        "type": "operating_area",
        "ref": "OA-PULSE",
        "work_class": "improvement",
        "reason": "guess",
    }
    assert (
        rig.kernel.execute(Command("guess", "record_affiliation", data, WI), "native")["status"]
        == "rejected"
    )
    assert git(rig.remote, "rev-parse", "main") == rig.before


def test_concurrent_new_decision_is_not_overwritten(tmp_path):
    rig = Rig(tmp_path)
    other = rig.client(rig.backend(rig.b))

    def race(stage):
        if stage == "before_push":
            rig.ledger.fault = lambda _: None
            assert rig.approve(other, request="competing")["status"] == "local_committed"

    rig.ledger.fault = race
    result = rig.approve()
    assert result["error"]["code"] == "ledger_conflict", result
    assert (
        other.inspect(Query(WI), "native")["result"]["current_observation"]["affiliation"]["state"]
        == "admitted"
    )
    assert rig.approve()["status"] == "local_committed"


def test_interruption_after_remote_commit_recovers_original_identity(tmp_path):
    rig = Rig(tmp_path)

    def fail(stage):
        if stage == "after_push":
            raise OSError("Synthetic acknowledgement loss")

    rig.ledger.fault = fail
    result = rig.approve()
    assert result["status"] == "local_committed", result
    committed = git(rig.remote, "rev-parse", "main")
    rig.ledger.fault = lambda _: None
    assert rig.approve()["status"] == "local_committed"
    assert git(rig.remote, "rev-parse", "main") == committed


def test_foreign_old_writer_cannot_silently_replace_native_fact(tmp_path):
    rig = Rig(tmp_path)
    assert rig.approve()["status"] == "local_committed"
    git(rig.b, "pull", "--ff-only")
    path = rig.b / ".harness/work-items" / (WI + ".yaml")
    path.write_text(json.dumps({**RECORD, "title": "Competing old writer"}))
    git(rig.b, "add", str(path))
    git(rig.b, "commit", "-m", "Competing old writer")
    git(rig.b, "push", "origin", "main")
    result = rig.kernel.inspect(Query(WI), "native")
    assert result["status"] == "read_only", result
    assert result["result"]["current_observation"]["definition"]["what"] == RECORD["title"]
    # The rejected old projection remains in Git history, but never becomes a fact.
    foreign = git(rig.remote, "rev-parse", "main")
    assert rig.approve()["status"] == "local_committed"
    assert git(rig.remote, "rev-parse", "main") == foreign  # Retry is readback only.
    rig.ledger.reformat(WI)  # Explicit projection reconstruction, independent of the result.
    assert git(rig.remote, "merge-base", "--is-ancestor", foreign, "main") == ""
    assert json.loads(git(rig.remote, "show", f"main:.harness/work-items/{WI}.yaml"))["title"] == RECORD["title"]


def workflow_rig(authority, profile):
    from tests_rewrite.test_workflow import Rig as WorkflowRig, DEFINITION, OTHER

    rig = WorkflowRig.__new__(WorkflowRig)
    rig.counter = 0
    rig.contexts = {
        "owner": replace(BASE, allowed_intents=INTENTS),
        "helper": OTHER,
        "create": replace(
            BASE,
            allowed_intents=INTENTS,
            generation=0,
            source=SourceInput("creation", "native_agent", b"CR-ONE agreed requirements"),
        ),
    }
    rig.kernel = Kernel(
        None,
        rig.contexts.__getitem__,
        storage=authority.backend(authority.a),
        clock=lambda: "2026-10-09T00:00:00Z",
    )
    result = rig.kernel.execute(
        Command(
            "new-" + profile, "create_" + profile, {**DEFINITION, "why": "CR-ONE: confirmed scope"}
        ),
        "create",
    )
    assert result["status"] == "local_committed", result
    rig.wi = result["work_item"]["id"]
    return rig


def test_standard_journey_uses_the_one_production_ref_through_completion(tmp_path):
    from tests_rewrite.test_review import produce, assign, verify, approve, VERIFIER
    from tests_rewrite.test_workflow import original

    authority = Rig(tmp_path)
    rig = workflow_rig(authority, "draft")
    rig.contexts["verifier"] = VERIFIER
    candidate = produce(rig)
    candidate_view = json.loads(
        git(authority.remote, "show", f"main:.harness/work-items/{rig.wi}.yaml")
    )
    assert candidate_view["status"] == "awaiting_verification"
    assignment = assign(rig, candidate)
    original(verify(rig, candidate, assignment))
    original(approve(rig, candidate))
    completed = rig.send("complete", {"candidate_id": candidate}, request="complete-standard")
    assert completed["work_item"]["business_progress"] == "completed", completed
    peer = Kernel(None, rig.contexts.__getitem__, storage=authority.backend(authority.b))
    assert peer.inspect(Query(rig.wi), "owner")["work_item"]["business_progress"] == "completed"
    original_record = json.loads(
        git(authority.remote, "show", f"main:.harness/work-items/{WI}.yaml")
    )
    assert original_record == RECORD
    published = json.loads(git(authority.remote, "show", f"main:.harness/work-items/{rig.wi}.yaml"))
    assert published["status"] == "completed"


def test_lightweight_journey_and_retry_keep_one_identity_and_publication(tmp_path):
    from tests_rewrite.test_lightweight import record
    from tests_rewrite.test_workflow import original

    authority = Rig(tmp_path)
    rig = workflow_rig(authority, "lightweight")
    original(rig.send("start_work", {}))
    work = record(rig)
    decision = {"record_ids": [work], "acceptance_refs": ["AC-1"]}
    done = rig.approve("complete_lightweight", decision, request="complete-light")
    assert done["work_item"]["business_progress"] == "completed", done
    head = git(authority.remote, "rev-parse", "main")
    assert (
        rig.approve("complete_lightweight", decision, request="complete-light")["status"]
        == "local_committed"
    )
    assert git(authority.remote, "rev-parse", "main") == head


from tests_rewrite.test_native_host import native as native


def test_native_command_returns_affiliation_to_original_agent_without_a_second_store(
    native, tmp_path, monkeypatch, capsys
):
    from harness_rewrite import cli
    from harness_rewrite.native_host import NativeHost

    authority_root = tmp_path / "authority-fixture"
    authority_root.mkdir()
    authority = Rig(authority_root)
    file, proc, session, inputs = native
    policy = json.loads(file.read_text())
    policy.update(
        namespace=BASE.namespace,
        production_ledger=authority.ledger.config,
        views_root=str(tmp_path / "views"),
        dispatch_root=str(tmp_path / "dispatch"),
        delivery_root=str(tmp_path / "delivery"),
        execution={"mode": "manual", "batch_budget": 1},
    )
    file.write_text(json.dumps(policy))
    monkeypatch.setattr(cli, "NativeHost", lambda _: NativeHost(file, proc=proc, pid=101))
    program_root = tmp_path / "installed"
    release = program_root / "releases/fixture"
    release.mkdir(parents=True)
    entry = release / "harness.pyz"
    entry.write_bytes(b"synthetic program identity")
    from harness_rewrite.model import encoded

    (release / "installation.json").write_text(
        json.dumps(
            {
                "bundle_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
                "policy_sha256": hashlib.sha256(encoded(policy)).hexdigest(),
            }
        )
    )
    (program_root / "active").symlink_to("releases/fixture")
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])
    assert cli.main(["resume", "--wi", WI]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "human_decision_required", result
    assert result["ledger"]["backend"] == "production_git"
    assert result["ledger"]["independent_database"] is False
    assert result["next_action"]["intent"] == "resolve_business_affiliation"
    assert result["work_item"]["id"] == WI
    assert git(authority.remote, "rev-parse", "main") == authority.before


def test_original_historical_draft_can_adopt_real_scope_and_finish_without_recreation(tmp_path):
    from tests_rewrite.test_workflow import Rig as WorkflowRig, BASE, graph, node, original
    from tests_rewrite.test_review import ARTIFACT, VERIFIER, candidate_id, assign, verify, approve

    authority = Rig(
        tmp_path,
        {
            **RECORD,
            "source": "CR-ONE: original scope",
            "acceptance_criteria": ["A fixed observable artifact"],
        },
    )
    rig = object.__new__(WorkflowRig)
    rig.counter = 0
    rig.wi = WI
    rig.contexts = {"owner": replace(BASE, allowed_intents=INTENTS), "verifier": VERIFIER}
    rig.kernel = Kernel(None, rig.contexts.__getitem__, storage=authority.ledger)
    original_definition = authority.record.copy()
    g = graph(rig, [node("work", outputs=["result"])])
    original(
        rig.approve(
            "adopt_plan", {"graph": g, "plan_text": "Original agreed plan", "resources": ["repo"]}
        )
    )
    original(rig.send("start_work", {"graph_revision": 1}))
    task = original(rig.claim("work"))["attempt_id"]
    original(rig.result(task, artifacts={"result": ARTIFACT}))
    candidate = candidate_id(rig)
    assignment = assign(rig, candidate)
    original(verify(rig, candidate, assignment))
    original(approve(rig, candidate))
    original(rig.send("complete", {"candidate_id": candidate}))
    published = json.loads(git(authority.remote, "show", f"main:.harness/work-items/{WI}.yaml"))
    assert published["status"] == "completed"
    assert published["work_item_id"] == WI
    assert published["created_at"] == original_definition["created_at"]
    assert published["historical_extension"] == original_definition["historical_extension"]
    history = rig.kernel.inspect(Query(WI), "owner")["result"]["current_observation"][
        "production_history"
    ]
    assert history["record"] == original_definition
    identities = git(authority.remote, "ls-tree", "--name-only", "main:.harness/work-items")
    assert identities == WI + ".yaml"


def test_production_install_checks_exact_program_protocol_and_preserves_facts(tmp_path):
    import sys
    from harness_rewrite.production_install import activate, require_active_program
    from harness_rewrite.release import build
    from harness_rewrite.model import encoded, Rejected

    authority = Rig(tmp_path)
    assert authority.approve()["status"] == "local_committed"
    source = tmp_path / "source"
    package = source / "agent-harness/harness_rewrite"
    package.mkdir(parents=True)
    for file in (Path(__file__).resolve().parents[1] / "harness_rewrite").iterdir():
        if file.suffix in {".py", ".mjs"}:
            (package / file.name).write_bytes(file.read_bytes())
    specs = source / "agent-harness/project-specs"
    specs.mkdir()
    (specs / "rewrite-native-operations.md").write_text("Exact fixture operations")
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
    root = tmp_path / "installed"
    release = root / "releases/first"
    release.mkdir(parents=True)
    policy = {
        "python": sys.executable,
        "namespace": BASE.namespace,
        "production_ledger": authority.ledger.config,
        "surface": "fixture", "node": sys.executable, "pi_binary": sys.executable,
        "views_root": str(root / "views"), "delivery_root": str(root / "delivery"),
        "dispatch_root": str(root / "dispatch"), "verification_actor": "verifier",
        "profiles": [{"actor": "verifier", "role": "verifier",
                      "session_root": str(root / "dispatch"), "artifact_roots": [str(root)]}],
    }

    def prepare(path):
        bundle = path / "harness.pyz"
        built = build(source, "HEAD", bundle)
        (path / "host-policy.json").write_bytes(encoded(policy))
        receipt = {
            "bundle_sha256": built["sha256"],
            "policy_sha256": hashlib.sha256(encoded(policy)).hexdigest(),
        }
        (path / "installation.json").write_bytes(encoded(receipt))
        return receipt

    receipt = prepare(release)
    before = git(authority.remote, "rev-parse", "main")
    prepared = activate(root, release, policy, receipt, lambda _: None, prepare_only=True)
    assert prepared["historical_items_read"] == 0
    assert prepared['historical_replay'] == 'not_performed'
    assert prepared["status"] == "prepared"
    assert not (root / "active").exists()
    assert git(authority.remote, "rev-parse", "main") == before
    result = activate(root, release, policy, receipt, lambda _: None)
    assert result["historical_items_read"] == 0
    assert result["observed_revision"] == before
    require_active_program(release / "harness.pyz", policy)
    from harness_rewrite.production_install import restore
    successor = root / "releases/second"
    successor.mkdir()
    second_receipt = prepare(successor)
    activate(root, successor, policy, second_receipt, lambda _: None)
    with pytest.raises(ValueError, match="changed after"):
        restore(root, "other-deployment", "releases/first")
    restore(root, "second", "releases/first")
    assert (root / "active").resolve() == release
    with pytest.raises(ValueError, match="no compatible predecessor"):
        restore(root, "first", None)
    assert (root / "active").resolve() == root / "releases/first"
    activate(root, release, policy, receipt, lambda _: None)
    broken = root / "releases/incompatible"
    broken.mkdir()
    domain = package / "production_segments.py"
    domain.write_text(
        domain.read_text().replace(
            'SCHEMA = "harness.production-ledger/v2"',
            'SCHEMA = "unsupported-program/v0"',
        )
    )
    git(source, "add", ".")
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Incompatible fixture",
    )
    broken_receipt = prepare(broken)
    with pytest.raises(ValueError, match="exact proposed program"):
        activate(root, broken, policy, broken_receipt, lambda _: None)
    assert (root / "active").resolve() == release
    with pytest.raises(Rejected, match="installed native production"):
        require_active_program(broken / "harness.pyz", policy)
    assert git(authority.remote, "rev-parse", "main") == before
    assert not list(root.rglob("*.sqlite*"))


def test_storage_contract_never_reads_bad_business_or_large_observation_bodies(tmp_path, monkeypatch):
    from harness_rewrite.storage_contract import inspect_contract
    from harness_rewrite.observations import HEAD, SCHEMA as OBS
    rig = Rig(tmp_path)
    rig.approve()
    git(rig.b, 'pull', '--ff-only')
    paths = {
        '.harness/core/work-items/wi_20261008_550.json': '{damaged',
        '.harness/core/work-items/wi_20261009_001.json': '{another damaged WI',
        HEAD: json.dumps({'schema': OBS, 'namespace': BASE.namespace, 'sequence': 999999, 'sha256': '0'*64}),
        '.harness/core/observations/computecost/00000000000000000001.json': '{damaged batch',
    }
    for name, value in paths.items():
        path = rig.b / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value)
    git(rig.b, 'add', '.'); git(rig.b, 'commit', '-m', 'Damaged fixture facts'); git(rig.b, 'push')
    before = git(rig.remote, 'rev-parse', 'main')
    read = rig.ledger.blobs
    def bounded(mapping):
        assert set(mapping) <= {'.harness/core/format.json', HEAD}
        return read(mapping)
    monkeypatch.setattr(rig.ledger, 'blobs', bounded)
    assert inspect_contract(rig.ledger)['observation_watermark'] == 999999
    assert git(rig.remote, 'rev-parse', 'main') == before


@pytest.mark.parametrize("remote,hostname", [
    ("git@github-bigbird-pulse:bigbird-online/PULSE.git", "github.com"),
    ("ssh://github-pulse/bigbird-online/PULSE.git", "ssh.github.com"),
])
def test_registered_ssh_alias_preserves_canonical_production_identity(tmp_path, monkeypatch, remote, hostname):
    rig = Rig(tmp_path)
    git(rig.a, "remote", "set-url", "origin", remote)
    original = subprocess.run
    def run(args, **kwargs):
        if args[:2] == ["ssh", "-G"]:
            return subprocess.CompletedProcess(args, 0, "hostname " + hostname + "\n", "")
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    config = {"repository": str(rig.a), "remote": "origin", "ref": "refs/heads/main",
              "identity": "github.com/bigbird-online/PULSE"}
    assert ProductionLedger(config, "pulse-production").config == config
    config["identity"] = "github.com/other/repository"
    with pytest.raises(ValueError, match="authority differs"):
        ProductionLedger(config, "pulse-production")


def test_native_journal_alone_rebuilds_views_and_original_result_on_a_fresh_client(tmp_path):
    from harness_rewrite.views import Views

    rig = Rig(tmp_path)
    original = rig.approve()
    before = rig.kernel.inspect(Query(WI), "native")
    journal_path = f".harness/core/work-items/{WI}.json"
    journal = git(rig.remote, "show", "main:" + journal_path)
    git(rig.b, "pull", "--ff-only")
    paths = [f".harness/work-items/{WI}.yaml", f".harness/admissions/{WI}.yaml",
             f".harness/events/{WI}.events.yaml"]
    expected = {p: git(rig.remote, "show", "main:" + p) for p in paths}
    git(rig.b, "rm", *paths)
    git(rig.b, "commit", "-m", "Drop all disposable projections")
    git(rig.b, "push", "origin", "main")
    absent = git(rig.remote, "rev-parse", "main")
    fresh = tmp_path / "fresh-client"
    git(tmp_path, "clone", str(rig.remote), str(fresh))
    peer = rig.client(rig.backend(fresh))
    assert peer.inspect(Query(WI), "native") == before
    assert peer.inspect(Query(view="list"), "native")["result"]["items"][0]["id"] == WI
    views = tmp_path / "views"
    views.mkdir()
    (views / (WI + ".json")).write_text('{"status":"completed","generation":999}')
    rebuilt = Views(peer, views).read(WI, "native")
    # The disposable page is rebuilt from the same facts and only names its location.
    assert rebuilt.pop("project_page")["revision"] == before["work_item"]["local_revision"]
    assert rebuilt == before
    assert git(rig.remote, "rev-parse", "main") == absent  # Reads remain read-only.
    replay = rig.approve(peer)
    assert replay["result"]["original_request_result"] == original["result"]["original_request_result"]
    assert git(rig.remote, "show", "main:" + journal_path) == journal
    rig.ledger.reformat(WI)
    for path in paths:
        assert git(rig.remote, "show", "main:" + path) == expected[path]
    assert peer.inspect(Query(WI), "native") == before
    assert not list(fresh.rglob("*.sqlite*"))


def test_native_journal_corruption_never_recovers_from_intact_projections(tmp_path):
    rig = Rig(tmp_path)
    assert rig.approve()["status"] == "local_committed"
    git(rig.b, "pull", "--ff-only")
    path = rig.b / f".harness/core/work-items/{WI}.json"
    record = json.loads(path.read_text())
    record["head"] = "f" * 64
    path.write_text(json.dumps(record))
    git(rig.b, "add", str(path))
    git(rig.b, "commit", "-m", "Corrupt authoritative journal with intact projections")
    git(rig.b, "push", "origin", "main")
    assert rig.kernel.inspect(Query(WI), "native")["error"]["code"] == "journal_invalid"


def test_exact_request_readback_survives_new_client_and_has_no_local_request_cache(tmp_path):
    rig = Rig(tmp_path)
    original = rig.approve(request="original-input")
    peer = rig.client(rig.backend(rig.b))
    head = git(rig.remote, "rev-parse", "main")
    readback = peer.inspect(Query(WI, view="request", request_id="original-input"), "native")
    assert readback["result"]["request_observation"]["state"] == "committed"
    assert readback["result"]["original_request_result"] == original["result"]["original_request_result"]
    missing = peer.inspect(Query(WI, view="request", request_id="uncommitted"), "native")
    assert missing["result"]["request_observation"]["state"] == "absent"
    assert missing["result"]["original_request_result"] is None
    assert git(rig.remote, "rev-parse", "main") == head
    for query in [Query(WI, view="request"), Query(WI, request_id="original-input"),
                  Query(WI, view="request", request_id="original-input", revision=1)]:
        assert peer.inspect(query, "native")["status"] == "rejected"


@pytest.mark.parametrize("corrupt", [False, True])
def test_newer_wi_does_not_poison_unrelated_reads_writes_or_projection_repair(tmp_path, corrupt):
    rig = Rig(tmp_path)
    assert rig.approve()["status"] == "local_committed"
    git(rig.b, "pull", "--ff-only")
    other = "wi_20260823_184"
    native = rig.b / f".harness/core/work-items/{WI}.json"
    record = v1_record(rig)
    record["work_item_id"] = other
    from harness_rewrite.kernel import EMPTY
    previous = EMPTY
    # A valid later writer emits a new event. Both clients share one Git ref.
    for entry in record["entries"]:
        entry["wi"] = other
        entry["request_id"] = "other-" + entry["request_id"]
        entry["previous"] = previous
        previous = digest(entry)
    record["entries"][-1]["events"][-1]["kind"] = "FutureExecutionContinued"
    if corrupt:
        record["entries"][-1]["events"][-1]["sequence"] = 999
    path = rig.b / f".harness/core/work-items/{other}.json"
    path.write_text(json.dumps(record))
    format_path = rig.b / ".harness/core/format.json"
    format_record = json.loads(format_path.read_text())
    format_record["schema"] = record["schema"]
    format_path.write_text(json.dumps(format_record))
    projection = rig.b / f".harness/work-items/{WI}.yaml"
    expected = projection.read_bytes()
    projection.unlink()
    git(rig.b, "add", ".harness")
    git(rig.b, "commit", "-m", "Newer writer and missing unrelated disposable view")
    git(rig.b, "push", "origin", "main")
    before = git(rig.remote, "show", "main:" + str(path.relative_to(rig.b)))
    observed = rig.kernel.inspect(Query(WI), "native")
    assert observed["status"] == "read_only", observed
    assert rig.approve()["status"] == "local_committed"  # exact request is read-only
    rig.ledger.reformat(WI)
    assert git(rig.remote, "show", "main:" + str(projection.relative_to(rig.b))) == expected.decode().strip()
    assert rig.approve(request="another-valid-confirmation")["status"] == "local_committed"
    code = "journal_invalid" if corrupt else "journal_reader_upgrade_required"
    error = rig.kernel.inspect(Query(other), "native")["error"]
    assert error["code"] == code, error
    listing = rig.kernel.inspect(Query(view="list"), "native")
    assert listing["result"]["coverage"] == "partial"
    assert listing["result"]["unavailable_items"][0]["code"] == code
    if not corrupt:
        assert other in error["reason"]
    assert git(rig.remote, "show", "main:" + str(path.relative_to(rig.b))) == before
    assert not list(tmp_path.rglob("*.sqlite*"))



def test_cumulative_journal_crosses_old_limit_and_remains_readable(tmp_path):
    rig = Rig(tmp_path)
    data = {"type": "operating_area", "ref": "OA-PULSE", "work_class": "improvement",
            "reason": "Confirmed original affiliation"}
    payloads = [bytes([n]) * (13 * 1024 * 1024) for n in (65, 66)]
    results = []
    for n, content in enumerate(payloads):
        rig.context = replace(rig.context, source=SourceInput(
            "native-confirmation", "native_human", content, "record_affiliation",
            digest({"intent": "record_affiliation", "work_item_id": WI, "input": data})))
        result = rig.kernel.execute(Command(f"large-original-{n}", "record_affiliation", data, WI), "native")
        assert result["status"] == "local_committed", result
        results.append(result)
    committed = git(rig.remote, "rev-parse", "main")
    raw = subprocess.check_output(["git", "-C", str(rig.remote), "show",
                                   f"main:.harness/core/work-items/{WI}.json"])
    assert len(raw) < 1024
    for content in payloads:
        assert rig.ledger.evidence.read(hashlib.sha256(content).hexdigest()) == content
    peer = rig.client(rig.backend(rig.b))
    for n, result in enumerate(results):
        readback = peer.inspect(Query(WI, view="request", request_id=f"large-original-{n}"), "native")
        assert readback["status"] == "read_only", readback
        assert readback["result"]["request_observation"]["state"] == "committed"
        assert readback["result"]["original_request_result"] == result["result"]["original_request_result"]
    assert peer.inspect(Query(view="list"), "native")["status"] == "read_only"
    replay = peer.execute(Command("large-original-1", "record_affiliation", data, WI), "native")
    assert replay["status"] == "local_committed", replay
    assert git(rig.remote, "rev-parse", "main") == committed
    assert not (tmp_path / "must-not-exist.sqlite").exists()
    # Continued writes must consume the same oversized history, not merely replay
    # a receipt cached before the threshold was crossed.
    appended = peer.execute(Command("after-old-limit", "record_affiliation", data, WI), "native")
    assert appended["status"] == "local_committed", appended
    assert peer.inspect(Query(WI, view="request", request_id="large-original-0"), "native")["result"]["original_request_result"] == results[0]["result"]["original_request_result"]


def test_exact_read_does_not_load_an_unrelated_malformed_container(tmp_path):
    rig = Rig(tmp_path)
    assert rig.approve()["status"] == "local_committed"
    git(rig.b, "pull", "--ff-only")
    path = rig.b / ".harness/core/work-items/wi_20260823_184.json"
    path.write_text("not a valid journal")
    git(rig.b, "add", str(path))
    git(rig.b, "commit", "-m", "Synthetic unrelated unreadable container")
    git(rig.b, "push", "origin", "main")
    before = git(rig.remote, "rev-parse", "main")
    result = rig.kernel.inspect(Query(WI, view="request", request_id="affiliation"), "native")
    assert result["status"] == "read_only", result
    assert result["result"]["request_observation"]["state"] == "committed"
    listing = rig.kernel.inspect(Query(view="list"), "native")
    assert listing["result"]["coverage"] == "partial"
    assert listing["result"]["unavailable_items"][0]["code"] == "ledger_invalid"
    bindings = rig.kernel.inspect(Query(view='bindings'), 'native')
    assert bindings['status'] == 'read_only'
    assert bindings['result']['coverage'] == 'partial'
    from harness_rewrite.cli import bound_work_item
    from harness_rewrite.model import Rejected
    with pytest.raises(Rejected, match='specify the original work item explicitly'):
        bound_work_item(rig.kernel, 'native')
    assert rig.kernel.inspect(Query("wi_20260823_184"), "native")["error"]["code"] == "ledger_invalid"
    assert git(rig.remote, "rev-parse", "main") == before


def test_executable_mode_does_not_change_own_json_facts(tmp_path):
    rig = Rig(tmp_path)
    rig.approve()
    before = rig.kernel.inspect(Query(WI), 'native')
    git(rig.b, 'pull', '--ff-only')
    path = '.harness/core/work-items/' + WI + '.json'
    git(rig.b, 'update-index', '--chmod=+x', path)
    git(rig.b, 'commit', '-m', 'Fixture regular file mode'); git(rig.b, 'push')
    assert rig.kernel.inspect(Query(WI), 'native') == before


@pytest.mark.parametrize("frame", [
    b"wrong blob 3\nabc\n", b"{sha} tree 3\nabc\n", b"{sha} blob -1\n\n",
    b"{sha} blob 4\nabc\n", b"{sha} blob 3\nabc!", b"{sha} blob 3\nabc\ntrailing",
    b"{sha} blob 99999999999999999999999\nabc\n", b"no-header-newline",
])
def test_git_object_transport_rejects_incomplete_or_ambiguous_frames(tmp_path, monkeypatch, frame):
    from harness_rewrite.model import Rejected

    rig = Rig(tmp_path)
    sha = "a" * 40
    payload = frame.replace(b"{sha}", sha.encode())
    import sys
    popen = subprocess.Popen
    def malformed(args, **kwargs):
        if "cat-file" not in args:
            return popen(args, **kwargs)
        code = "import sys; sys.stdin.buffer.readline(); sys.stdout.buffer.write(" + repr(payload) + "); sys.stdout.buffer.flush()"
        return popen([sys.executable, "-I", "-c", code], **kwargs)
    monkeypatch.setattr(subprocess, "Popen", malformed)
    with pytest.raises(Rejected, match="production (record|read)"):
        rig.ledger.blobs({".harness/core/work-items/example.json": sha})


def test_fixed_object_reader_bounds_memory_and_checks_out_of_order_reads(tmp_path):
    import tracemalloc
    from harness_rewrite.model import Rejected
    rig = Rig(tmp_path)
    data = b'public fact object' * 65536
    oid = rig.ledger.git('hash-object', '-w', '--stdin', data=data).decode().strip()
    paths = {f'.harness/fixture/{i}': oid for i in range(12)}
    tracemalloc.start()
    try:
        with rig.ledger.object_reader(paths) as read:
            for name in reversed(paths):
                assert read(name) == data
            assert read(next(iter(paths))) == data
            with pytest.raises(Rejected, match='not selected'):
                read('.harness/not-selected')
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024, 'Reading a fixed set must not retain all blob bodies'


def test_stalled_git_reader_is_bounded_and_reaps_its_own_process(tmp_path, monkeypatch):
    import sys
    from harness_rewrite import git_objects
    from harness_rewrite.model import Rejected
    rig = Rig(tmp_path)
    popen, children = subprocess.Popen, []
    def stalled(args, **kwargs):
        if 'cat-file' not in args:
            return popen(args, **kwargs)
        child = popen([sys.executable, '-I', '-c',
                       'import sys,time;sys.stdin.buffer.readline();time.sleep(30)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', stalled)
    monkeypatch.setattr(git_objects, 'READ_TIMEOUT', 0.1)
    with pytest.raises(Rejected, match='timed out'):
        rig.ledger.blobs({'.harness/stalled': 'a' * 40})
    assert len(children) == 1 and children[0].poll() is not None


def test_storage_contract_probe_retries_a_transient_read_and_reports_the_last_error():
    import subprocess as sp
    from harness_rewrite.production_install import PROBE_ATTEMPTS, run_probe
    calls, slept = [], []
    def flaky(command, **kwargs):
        calls.append(command)
        failed = len(calls) == 1
        return sp.CompletedProcess(command, 1 if failed else 0, b"" if failed else b"{}",
                                   b"fatal: unable to access remote\n" if failed else b"")
    assert run_probe(["probe"], runner=flaky, sleep=slept.append).stdout == b"{}"
    assert len(calls) == 2 and len(slept) == 1
    def broken(command, **kwargs):
        return sp.CompletedProcess(command, 1, b"", b"Traceback\nImportError: unsupported-program\n")
    with pytest.raises(ValueError, match=r"storage contract.*ImportError: unsupported-program"):
        run_probe(["probe"], runner=broken, sleep=lambda _: None)
    assert PROBE_ATTEMPTS == 3


def test_remote_head_reads_only_the_advertised_production_ref(tmp_path, monkeypatch):
    rig = Rig(tmp_path)

    def no_fetch(*_args, **_kwargs):
        raise AssertionError("A head reading must not fetch")
    monkeypatch.setattr(rig.ledger, "refresh_ref", no_fetch)
    before = git(rig.a, "for-each-ref")
    assert rig.ledger.remote_head() == rig.before
    (rig.b / "other.txt").write_text("unrelated production write")
    git(rig.b, "add", "other.txt")
    git(rig.b, "commit", "-m", "Another client writes production")
    git(rig.b, "push", "origin", "main")
    after = git(rig.remote, "rev-parse", "main")
    assert after != rig.before and rig.ledger.remote_head() == after
    assert git(rig.a, "for-each-ref") == before  # No ref written or left behind.
    assert subprocess.run(["git", "-C", str(rig.a), "cat-file", "-e", after]).returncode


def test_concurrent_fetch_losing_the_tracking_ref_still_observes_production(tmp_path):
    rig = Rig(tmp_path)
    (rig.b / "other.txt").write_text("another node publishes")
    git(rig.b, "add", "other.txt")
    git(rig.b, "commit", "-m", "Another client writes production")
    git(rig.b, "push", "origin", "main")
    after = git(rig.remote, "rev-parse", "main")
    # A concurrent fetch on the same checkout holds the remote-tracking ref.
    lock = rig.a / ".git/refs/remotes/origin/main.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    try:
        assert rig.ledger.refresh() == after
    finally:
        lock.unlink()
    assert git(rig.a, "for-each-ref", "refs/harness-core") == ""  # Private ref removed.
    git(rig.a, "remote", "set-url", "origin", str(tmp_path / "absent.git"))
    with pytest.raises(Rejected) as failed:
        rig.ledger.refresh()
    assert failed.value.code == "ledger_unavailable"
