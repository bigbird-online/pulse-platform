import copy
import json
from pathlib import Path

import pytest

from harness_rewrite import policy, relay_admission as admission


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


@pytest.fixture
def scene(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    pulse = workspace / "pulse"
    old = pulse / ("releases/PULSE-" + "a" * 40)
    new = pulse / ("releases/PULSE-" + "b" * 40)
    node = workspace / "node"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"node fixture")
    old_cli = old / "Relay/dist/cli.js"
    old_cli.parent.mkdir(parents=True)
    old_cli.write_bytes(b"old")
    assets = {}
    builds = {}
    for relative in [r + "/asset.txt" for r in admission.ROOTS] + [
        "Relay/dist/cli.js",
        "Relay/dist/helper.js",
    ]:
        path = new / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
        (builds if relative.startswith("Relay/dist/") else assets)[relative] = (
            admission._sha(path.read_bytes())
        )
    manifest = {
        "schema": "pulse_muse_release/v1",
        "sha": "b" * 40,
        "cli_sha256": builds["Relay/dist/cli.js"],
        "runtime_assets_sha256": assets,
        "relay_build_sha256": builds,
    }
    write(new / ".pulse-muse-release.json", manifest)
    receipt = {
        "schema": "pulse_muse_relay_experiment/v1",
        "state": "activated",
        "sha": "b" * 40,
        "release": str(new),
        "previous_release": str(old),
        "health_status": "ok",
        "work_item_id": "wi_20260922_018",
        "verification_run_id": 123,
        "verification_sha256": "c" * 64,
    }
    state = pulse / ".local/state/muse-cd"
    write(state / ("experiment-" + "b" * 40 + ".json"), receipt)
    write(state / "latest.json", receipt)
    write(
        pulse / ".local/state/node-supervisor/config.json",
        {
            "purpose": "experiment",
            "experiment_work_item": "wi_20260922_018",
            "script": str(workspace / "bin/pulse-node.py"),
        },
    )
    (workspace / "bin").mkdir()
    (workspace / "bin/pulse-node.py").write_bytes(b"guardian")
    (workspace / "bin/start-relay.sh").write_text('RELEASE="' + str(new) + '"\n')
    pidfile = pulse / ".local/share/pulse/data/relay/relay.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("1234\n")
    proc = tmp_path / "proc"
    process = proc / "1234"
    process.mkdir(parents=True)
    (process / "cmdline").write_bytes(
        b"node\0" + str(new / "Relay/dist/cli.js").encode() + b"\0"
    )
    (process / "exe").symlink_to(node)
    monkeypatch.setattr(admission, "PROC", proc)
    previous = {
        "relay_entry": str(old_cli),
        "producer_node": str(node),
        "namespace": "original",
        "database": "/unchanged/facts",
        "profiles": [{"actor": "original"}],
    }
    proposed = {**previous, "relay_entry": str(new / "Relay/dist/cli.js")}
    return previous, proposed, new, workspace, state, process


def test_exact_adoption_is_read_only_and_idempotent(scene):
    before, after, new, workspace, _, _ = scene
    snapshot = {str(p): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="use migration"):
        policy.compatible(before, after)
    result = policy.compatible(before, after, relay_release=new)
    assert result["execution_authority"] is False
    assert result == policy.compatible(after, after, relay_release=new)
    assert {
        str(p): p.read_bytes() for p in workspace.rglob("*") if p.is_file()
    } == snapshot


def test_adoption_may_open_external_entries_with_the_relay_release(scene):
    before, after, new, *_ = scene
    after = copy.deepcopy(after)
    after["external_entries"] = {"root": "/entries", "owner": "1", "entries": ["chatgpt_web"]}
    assert policy.compatible(before, after, relay_release=new)["execution_authority"] is False
    after["namespace"] = "another"
    with pytest.raises(ValueError, match="exceeds"):
        policy.compatible(before, after, relay_release=new)


def test_adoption_may_open_the_pull_request_gate_bound_to_this_release(scene):
    before, after, new, *_ = scene
    gate = {"pulse_gh": str(new / "Scripts/pulse-gh.py"), "gh": "/opt/homebrew/bin/gh",
            "relay_paths": "/home/relay/paths.json", "merge_method": "merge",
            "repositories": ["bigbird-online/PULSE", "bigbird-online/CollaborateWithAI", "bigbird-online/Oikos"]}
    assert policy.compatible(before, {**after, "pull_request_gate": gate}, relay_release=new)[
        "execution_authority"] is False
    for wrong in ({"pulse_gh": "/elsewhere/pulse-gh.py"}, {"repositories": ["someone/else"]},
                  {"repositories": []}, {"gh": "gh"}, {"merge_method": "squash"}, {"extra": 1}):
        with pytest.raises(ValueError, match="pull request gate"):
            policy.compatible(before, {**after, "pull_request_gate": {**gate, **wrong}}, relay_release=new)


def test_adoption_may_move_where_new_workspaces_go(scene):
    before, after, new, *_ = scene
    repository = {"repo_id": "one", "source": "/src/one", "common_dir": "/src/one/.git",
                  "managed_root": "/external/one", "remote_url": "github.com/owner/one"}
    before = {**before, "repositories": [repository]}
    after = {**after, "repositories": [{**repository, "managed_root": "/internal/one"}]}
    assert policy.compatible(before, after, relay_release=new)["execution_authority"] is False
    for changed in ({"source": "/elsewhere"}, {"remote_url": "github.com/other/one"}):
        moved = {**after, "repositories": [{**repository, "managed_root": "/internal/one", **changed}]}
        with pytest.raises(ValueError, match="managed root move"):
            policy.compatible(before, moved, relay_release=new)
    with pytest.raises(ValueError, match="managed root move"):
        policy.compatible(before, {**after, "repositories": []}, relay_release=new)


@pytest.mark.parametrize(
    "key,value",
    [
        ("namespace", "another"),
        ("database", "/elsewhere/facts"),
        ("profiles", [{"actor": "another"}]),
        ("execution", {"mode": "automatic", "batch_budget": 64}),
        ("producer_node", "/another/node"),
    ],
)
def test_rotation_cannot_change_identity_permissions_or_roots(scene, key, value):
    before, after, new, *_ = scene
    after = copy.deepcopy(after)
    after[key] = value
    with pytest.raises(ValueError, match="exceeds"):
        policy.compatible(before, after, relay_release=new)


@pytest.mark.parametrize(
    "kind",
    [
        "changed_asset",
        "missing_asset",
        "added_asset",
        "redirected_asset",
        "wrong_predecessor",
        "stale_latest",
        "failed_receipt",
        "wrong_sha",
        "wrong_node_config",
        "wrong_launcher",
        "wrong_process",
        "missing_process",
        "wrong_node",
        "redirected_receipt",
    ],
)
def test_invalid_release_or_live_proof_rejects(scene, kind):
    before, after, new, workspace, state, process = scene
    receipt_path = state / ("experiment-" + "b" * 40 + ".json")
    if kind == "changed_asset":
        (new / "Relay/dist/helper.js").write_bytes(b"changed")
    elif kind == "missing_asset":
        (new / "Scripts/asset.txt").unlink()
    elif kind == "added_asset":
        (new / "Relay/dist/added.js").write_bytes(b"added")
    elif kind == "redirected_asset":
        path = new / "Scripts/asset.txt"
        path.unlink()
        path.symlink_to(new / "ComputeCost/asset.txt")
    elif kind in {"wrong_predecessor", "failed_receipt", "wrong_sha"}:
        record = json.loads(receipt_path.read_text())
        key, value = {
            "wrong_predecessor": (
                "previous_release",
                str(new.parent / ("PULSE-" + "d" * 40)),
            ),
            "failed_receipt": ("state", "failed"),
            "wrong_sha": ("sha", "d" * 40),
        }[kind]
        record[key] = value
        write(receipt_path, record)
        write(state / "latest.json", record)
    elif kind == "stale_latest":
        write(state / "latest.json", {})
    elif kind == "wrong_node_config":
        write(
            workspace / "pulse/.local/state/node-supervisor/config.json",
            {"purpose": "production"},
        )
    elif kind == "wrong_launcher":
        (workspace / "bin/start-relay.sh").write_text('RELEASE="/old"\n')
    elif kind == "wrong_process":
        (process / "cmdline").write_bytes(b"node\0/old/cli.js\0")
    elif kind == "missing_process":
        (process / "cmdline").unlink()
    elif kind == "wrong_node":
        other = workspace / "another-node"
        other.write_bytes(b"other")
        (process / "exe").unlink()
        (process / "exe").symlink_to(other)
    elif kind == "redirected_receipt":
        receipt_path.unlink()
        receipt_path.symlink_to(state / "latest.json")
    with pytest.raises((ValueError, OSError)):
        policy.compatible(before, after, relay_release=new)


def test_another_release_root_rejects(scene):
    before, after, new, workspace, *_ = scene
    another = workspace / ("elsewhere/releases/PULSE-" + "a" * 40) / "Relay/dist/cli.js"
    another.parent.mkdir(parents=True)
    another.write_bytes(b"old")
    before["relay_entry"] = str(another)
    with pytest.raises(ValueError, match="another root"):
        policy.compatible(before, after, relay_release=new)


@pytest.mark.parametrize("drift_during_install", [False, True])
def test_real_install_keeps_original_store_and_rechecks_evidence(
    scene, tmp_path, monkeypatch, drift_during_install
):
    import shutil
    import sqlite3
    import subprocess
    import sys
    from dataclasses import replace

    from harness_rewrite import Command, Kernel, SourceInput, release
    from harness_rewrite.domain import INTENTS
    from harness_rewrite.model import writer_identity
    from tests_rewrite.test_workflow import BASE, DEFINITION, original

    previous, _, successor, workspace, state, _ = scene
    root = workspace / "lab"
    root.mkdir()
    sessions = workspace / "native-sessions"
    sessions.mkdir()
    previous.update(
        surface="fixture",
        database=str(root / "facts.sqlite"),
        views_root=str(root / "views"),
        delivery_root=str(root / "delivery"),
        dispatch_root=str(root / "dispatch"),
        verification_actor="verifier",
        python=sys.executable,
        node=sys.executable,
        pi_binary=sys.executable,
        profiles=[
            {
                "role": "producer",
                "actor": "producer",
                "session_root": str(sessions),
                "artifact_roots": [str(root / "work")],
            },
            {
                "role": "verifier",
                "actor": "verifier",
                "session_root": str(root / "dispatch"),
                "artifact_roots": [str(root / "dispatch")],
            },
        ],
    )
    source = tmp_path / "bundle-source"
    package = source / "agent-harness/harness_rewrite"
    package.mkdir(parents=True)
    for path in Path(release.__file__).parent.iterdir():
        if path.suffix in {".py", ".mjs"}:
            shutil.copyfile(path, package / path.name)
    doc = source / "agent-harness/project-specs/rewrite-native-operations.md"
    doc.parent.mkdir()
    doc.write_text("Isolated test bundle.")

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(source), *args], stderr=subprocess.STDOUT
        )

    git("init")
    git("add", "agent-harness")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Synthetic Relay adoption fixture",
    )
    archive = tmp_path / "candidate.pyz"
    built = release.build(source, git("rev-parse", "HEAD").decode().strip(), archive)
    before_file = tmp_path / "before-policy.json"
    write(before_file, previous)
    first = release.install(archive, built["sha256"], root, before_file)
    host = replace(
        BASE,
        allowed_intents=INTENTS,
        source=SourceInput("create", "native_agent", b"Isolated fixture creation"),
    )
    kernel = Kernel(
        root / "facts.sqlite",
        lambda _: host,
        writer_id=writer_identity(built["sha256"], previous),
    )
    original(kernel.execute(Command("create", "create_draft", DEFINITION), object()))

    tables = (
        "originals",
        "journal",
        "journal_heads",
        "store_binding",
        "store_custody",
        "runtime_contract",
    )

    def facts():
        with sqlite3.connect(root / "facts.sqlite") as db:
            return {
                table: db.execute("SELECT * FROM " + table).fetchall()
                for table in tables
            }

    saved = facts()
    old_active = (root / "active").readlink()
    proposed = {**previous, "relay_entry": str(successor / "Relay/dist/cli.js")}
    proposal_file = tmp_path / "next-policy.json"
    write(proposal_file, proposed)
    with pytest.raises(ValueError, match="use migration"):
        release.install(archive, built["sha256"], root, proposal_file)
    assert facts() == saved and (root / "active").readlink() == old_active
    if drift_during_install:
        preflight = policy.preflight

        def drift(p, r):
            result = preflight(p, r)
            write(state / "latest.json", {})
            return result

        monkeypatch.setattr(policy, "preflight", drift)
        with pytest.raises(ValueError, match="formal experiment receipt"):
            release.install(
                archive, built["sha256"], root, proposal_file, relay_release=successor
            )
        assert facts() == saved and (root / "active").readlink() == old_active
    else:
        second = release.install(
            archive, built["sha256"], root, proposal_file, relay_release=successor
        )
        assert second["writer_admission"] != first["writer_admission"]
        assert facts() == saved
        evidence = json.loads((root / "active/relay-adoption.json").read_text())
        assert evidence["release"] == str(successor)
        assert second == release.install(
            archive, built["sha256"], root, proposal_file, relay_release=successor
        )
        assert facts() == saved
        # Preparing the running program again (a rollback cohort after its
        # original deployment state was lost) keeps its original adoption proof.
        proof_file = root / "active/relay-adoption.json"
        adopted = {**evidence, "previous_release": "/original/predecessor"}
        proof_file.write_text(json.dumps(adopted))
        assert second == release.install(
            archive, built["sha256"], root, proposal_file, relay_release=successor
        )
        assert json.loads(proof_file.read_text()) == adopted
        proof_file.write_text(json.dumps({**adopted, "release": "/another/relay"}))
        with pytest.raises(ValueError, match="original adoption proof"):
            release.install(archive, built["sha256"], root, proposal_file, relay_release=successor)
        proof_file.write_text(json.dumps(evidence))
        comparison = policy.review(
            root, proposal_file, before_file, relay_release=successor
        )["comparison"]
        assert comparison["requires_migration"] == []
        assert comparison["changed_sections"] == ["relay_entry"]
        assert comparison["relay_adoption"] == evidence


def test_relay_adoption_can_converge_a_stale_producer_path_to_existing_canonical_node(scene):
    before, after, new, workspace, _, process = scene
    canonical = before["producer_node"]
    stale = workspace / "old-platform-node"
    stale.write_bytes(b"stale platform executable")
    before.update(node=canonical, producer_node=str(stale))
    after.update(node=canonical, producer_node=canonical)
    result = policy.compatible(before, after, relay_release=new)
    assert result["execution_authority"] is False
    assert before["producer_node"] == str(stale)  # Does not edit an existing policy.
    # The permitted target still requires the real release process to use it.
    (process / "exe").unlink()
    (process / "exe").symlink_to(stale)
    with pytest.raises(ValueError, match="live Node executable differs"):
        policy.compatible(before, after, relay_release=new)


def test_managed_cd_preparation_requires_exact_release_and_capacity(scene):
    before, after, new, workspace, _, _ = scene
    capacity = workspace / "capacity.json"
    write(capacity, {"admission_allowed": True})
    record = {"schema": "pulse_relay_immutable_release/v1",
              "build_provenance": "exact_git_archive", "source_sha": "b" * 40,
              "relay_cli_sha256": admission._sha((new / "Relay/dist/cli.js").read_bytes()),
              "capacity_admission_receipt": str(capacity),
              "capacity_admission_sha256": admission._sha(capacity.read_bytes())}
    write(new / ".pulse-release-manifest.json", record)
    assert policy.compatible(before, after, relay_release=new)["execution_authority"] is False
    write(capacity, {"admission_allowed": False})
    with pytest.raises(ValueError, match="capacity admission"):
        policy.compatible(before, after, relay_release=new)


@pytest.mark.parametrize('fault',[None,'redirected','different','replaced'])
def test_original_kernel_source_first_admission_only(scene,fault):
    before,after,new,workspace,*_=scene
    account=workspace/'account';original=account/'scl-kernel';original.mkdir(parents=True)
    before['human_inputs']=str(account/'relay'/'human-inputs');after['human_inputs']=before['human_inputs']
    after['historical_kernel_root']=str(original)
    if fault=='redirected':
        other=account/'elsewhere';original.rename(other);original.symlink_to(other)
    elif fault=='different':after['historical_kernel_root']=str(workspace)
    elif fault=='replaced':before['historical_kernel_root']=str(workspace)
    if fault:
        with pytest.raises(ValueError,match='existing account kernel'):policy.compatible(before,after,relay_release=new)
    else:
        assert policy.compatible(before,after,relay_release=new)['execution_authority'] is False


def test_prepared_integration_admits_exact_pair_before_process_cutover(scene):
    before, after, new, workspace, state, process = scene
    record = {'schema': 'pulse_muse_integration_preparation/v1', 'sha': 'b' * 40,
              'release': str(new), 'previous_release': str(Path(before['relay_entry']).parents[2]),
              'repository': 'bigbird-online/PULSE', 'ref': 'refs/heads/main',
              'deployment_id': 17, 'verification_run_id': 19,
              'manifest_sha256': admission._sha((new / '.pulse-muse-release.json').read_bytes())}
    proof = state / ('integration-' + 'b' * 40 + '.json')
    write(proof, record)
    proof.chmod(0o600)
    # A prepared pair may be validated while the predecessor is still running.
    (process / 'cmdline').write_bytes(b'node\0' + before['relay_entry'].encode() + b'\0')
    result = policy.compatible(before, after, relay_release=new)
    assert result['execution_authority'] is False
    assert 'preparation_sha256' in result
    # It is not permission to change any business binding or select edited code.
    (new / 'ComputeCost/asset.txt').write_bytes(b'changed after preparation')
    with pytest.raises(ValueError, match='inventory differs'):
        policy.compatible(before, after, relay_release=new)


@pytest.mark.parametrize('fault',[None,'missing','public','config','policy','predecessor','manifest','namespace'])
def test_managed_native_consumer_cohort_requires_exact_originals(scene,fault):
    from harness_rewrite.model import digest
    before,after,new,workspace,*_=scene
    root=workspace/'native';root.mkdir()
    before['views_root']=str(root/'views');after['views_root']=before['views_root']
    after['profiles']=[{'actor':'original','runtime':'codex','executable':str(new/'codex')}]
    capacity=workspace/'capacity.json';write(capacity,{'admission_allowed':True})
    manifest=new/'.pulse-release-manifest.json'
    write(manifest,{'schema':'pulse_relay_immutable_release/v1','build_provenance':'exact_git_archive',
        'source_sha':'b'*40,'relay_cli_sha256':admission._sha((new/'Relay/dist/cli.js').read_bytes()),
        'capacity_admission_receipt':str(capacity),'capacity_admission_sha256':admission._sha(capacity.read_bytes())})
    config=workspace/'original-config.json';write(config,{'users':[{'id':'original'}]})
    proof=root/'consumer-admissions'/('b'*40+'.json')
    record={'schema':'pulse_native_consumer_preparation/v1','source_revision':'b'*40,'release':str(new),
        'previous_policy_sha256':digest(before),'proposed_policy_sha256':digest(after),
        'config_path':str(config),'config_sha256':admission._sha(config.read_bytes()),
        'manifest_sha256':admission._sha(manifest.read_bytes())}
    if fault=='predecessor':record['previous_policy_sha256']='c'*64
    if fault=='manifest':record['manifest_sha256']='c'*64
    write(proof,record);proof.chmod(0o600)
    if fault=='missing':proof.unlink()
    if fault=='public':proof.chmod(0o644)
    if fault=='config':write(config,{'users':[{'id':'other'}]})
    if fault=='policy':after['profiles'][0]['actor']='other'
    if fault=='namespace':after['namespace']='another'
    if fault:
        with pytest.raises(ValueError):policy.compatible(before,after,relay_release=new)
    else:
        assert policy.compatible(before,after,relay_release=new)['execution_authority'] is False



@pytest.mark.parametrize("fault", [None, "other_surface", "replace_account",
                                  "different_launcher", "missing_resource", "missing_preparation"])
def test_managed_oracle_adoption_admits_only_the_original_shared_codex_account(scene, fault):
    from harness_rewrite.model import digest

    before, after, new, workspace, _, _ = scene
    root = workspace / "native"
    root.mkdir()
    oracle = {
        "views_root": str(root / "views"),
        "namespace": "pulse-production",
        "surface": "surface_v1_cloud_linux_c",
        "production_ledger": {"identity": "github.com/bigbird-online/PULSE"},
    }
    before.update(oracle)
    after.update(oracle)
    binding = {"credential_file": str(new.parent.parent / ".codex/auth.json"), "codex": "/usr/bin/codex"}
    after["pi_codex_oauth"] = binding
    if fault == "other_surface":
        before["surface"] = after["surface"] = "another-surface"
    elif fault == "replace_account":
        before["pi_codex_oauth"] = {**binding, "credential_file": "/original/auth.json"}
    elif fault == "different_launcher":
        after["pi_codex_oauth"] = {**binding, "codex": "/another/codex"}
    runtime = new / "Relay/runtime"
    runtime.mkdir()
    (runtime / "pi-codex-oauth.mjs").write_text("fixture")
    if fault != "missing_resource":
        (runtime / "pi-codex-oauth.py").write_text("fixture")
    capacity = workspace / "capacity.json"
    write(capacity, {"admission_allowed": True})
    manifest = new / ".pulse-release-manifest.json"
    write(manifest, {"schema": "pulse_relay_immutable_release/v1", "build_provenance": "exact_git_archive",
                     "source_sha": "b" * 40, "relay_cli_sha256": admission._sha((new / "Relay/dist/cli.js").read_bytes()),
                     "capacity_admission_receipt": str(capacity), "capacity_admission_sha256": admission._sha(capacity.read_bytes())})
    config = workspace / "original-config.json"
    write(config, {"users": [{"id": "original"}]})
    proof = root / "consumer-admissions" / ("b" * 40 + ".json")
    if fault != "missing_preparation":
        write(proof, {"schema": "pulse_native_consumer_preparation/v1", "source_revision": "b" * 40,
                      "release": str(new), "previous_policy_sha256": digest(before), "proposed_policy_sha256": digest(after),
                      "config_path": str(config), "config_sha256": admission._sha(config.read_bytes()),
                      "manifest_sha256": admission._sha(manifest.read_bytes())})
        proof.chmod(0o600)
    if fault is None:
        assert policy.compatible(before, after, relay_release=new)["execution_authority"] is False
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            policy.compatible(before, after, relay_release=new)
