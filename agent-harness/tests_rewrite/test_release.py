import json
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import zipfile
import shutil

import pytest

from harness_rewrite.release import build, install, sha, verify
from harness_rewrite import Kernel, Command, Query, SourceInput
from harness_rewrite.domain import INTENTS
from harness_rewrite.model import digest, writer_identity
from tests_rewrite.test_workflow import BASE, DEFINITION, original


@pytest.mark.parametrize(
    "fault_stage",
    [
        "after_fact_gate",
        "after_store_binding",
        "before_pointer",
        "after_pointer",
        "after_activation_commit",
    ],
)
def test_exact_bundle_runs_without_checkout_and_install_retry_keeps_facts(
    tmp_path, fault_stage
):
    source = tmp_path / "source"
    package = source / "agent-harness" / "harness_rewrite"
    package.mkdir(parents=True)
    for path in (Path(__file__).resolve().parents[1] / "harness_rewrite").glob("*"):
        if path.suffix not in {".py", ".mjs", ".html", ".LICENSE"}:
            continue
        (package / path.name).write_bytes(path.read_bytes())
    specs = source / "agent-harness" / "project-specs"
    specs.mkdir()
    (specs / "rewrite-native-operations.md").write_bytes(
        (
            Path(__file__).resolve().parents[1]
            / "project-specs"
            / "rewrite-native-operations.md"
        ).read_bytes()
    )

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
        "fixture bundle",
    )
    revision = git("rev-parse", "HEAD").decode().strip()
    # Uncommitted source cannot enter a fixed-revision build.
    (package / "cli.py").write_text('raise RuntimeError("uncommitted")')
    bundle = tmp_path / "candidate.pyz"
    built = build(source, revision, bundle)
    assert verify(bundle)["source_revision"] == revision
    resource_sha = subprocess.check_output(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys;sys.path.insert(0,sys.argv[1]);"
            "from harness_rewrite.native_compute import runner_bytes,checksum;"
            "print(checksum(runner_bytes()))",
            str(bundle),
        ],
        text=True,
    ).strip()
    assert resource_sha == verify(bundle)["files"]["harness_rewrite/verifier-runner.mjs"]
    # The project page template and its license notice ship as zip-safe package data.
    page_sha = subprocess.check_output(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys,hashlib;sys.path.insert(0,sys.argv[1]);"
            "from harness_rewrite.project_page import template;"
            "print(hashlib.sha256(template().encode()).hexdigest())",
            str(bundle),
        ],
        text=True,
    ).strip()
    assert page_sha == verify(bundle)["files"]["harness_rewrite/project_page.html"]
    assert "harness_rewrite/project_page.LICENSE" in verify(bundle)["files"]
    # This second exact program deliberately lacks the existing stop event.
    # It represents a rollback program that cannot consume later journal facts.
    old_domain = package / "domain.py"
    old_domain.write_text(
        old_domain.read_text().replace(
            '        kind, payload = item["kind"], item["payload"]',
            '        kind, payload = item["kind"], item["payload"]\n        if kind == "WorkStopped": raise ValueError("Unsupported old reader")',
        )
    )
    git("add", "agent-harness/harness_rewrite/domain.py")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture older reader",
    )
    incompatible = tmp_path / "older-reader.pyz"
    older = build(source, "HEAD", incompatible)
    old_domain.write_text(
        old_domain.read_text().replace(
            '\n        if kind == "WorkStopped": raise ValueError("Unsupported old reader")',
            "",
        )
    )
    (specs / "rewrite-native-operations.md").write_text("Compatible next fixture guide")
    git(
        "add",
        "agent-harness/harness_rewrite/domain.py",
        "agent-harness/project-specs/rewrite-native-operations.md",
    )
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "compatible successor",
    )
    successor = tmp_path / "successor.pyz"
    successor_built = build(source, "HEAD", successor)
    source.rename(tmp_path / "unavailable-source")
    run = subprocess.run(
        [sys.executable, "-I", "-B", str(bundle), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0 and "execute" in run.stdout
    root = tmp_path / "installed"
    root.mkdir()
    sessions = tmp_path / "native-sessions"
    sessions.mkdir()
    dispatch = root / "verifiers"
    policy = {
        "namespace": "fixture",
        "surface": "fixture",
        "database": str(root / "facts.sqlite"),
        "views_root": str(root / "views"),
        "delivery_root": str(root / "delivery"),
        "dispatch_root": str(dispatch),
        "verification_actor": "verifier",
        "python": sys.executable,
        "node": sys.executable,
        "pi_binary": sys.executable,
        "profiles": [
            {
                "role": "producer",
                "actor": "producer",
                "session_root": str(sessions),
                "artifact_roots": [str(root / "work")],
            },
            {
                "role": "verifier",
                "actor": "verifier",
                "session_root": str(dispatch),
                "artifact_roots": [str(dispatch)],
            },
        ],
    }
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                **policy,
                "repositories": [
                    {
                        "repo_id": "outside",
                        "source": str(tmp_path),
                        "common_dir": str(tmp_path),
                        "managed_root": str(root),
                        "remote_url": None,
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="Repository scope"):
        install(bundle, built["sha256"], root, policy_file)
    assert not (root / "active").exists()
    policy_file.write_text(json.dumps(policy))
    first = install(bundle, built["sha256"], root, policy_file)
    contexts = {
        "create": replace(
            BASE,
            allowed_intents=INTENTS,
            source=SourceInput("create", "native_agent", b"Original creation"),
        )
    }
    kernel = Kernel(
        root / "facts.sqlite",
        contexts.__getitem__,
        writer_id=writer_identity(built["sha256"], policy),
    )
    wi = original(
        kernel.execute(Command("create", "create_draft", DEFINITION), "create")
    )["work_item_id"]
    data = {"reason": "Explicit stop"}
    contexts["human"] = replace(
        BASE,
        allowed_intents=INTENTS,
        source=SourceInput(
            "human",
            "native_human",
            b"Stop this draft",
            "stop",
            digest({"intent": "stop", "work_item_id": wi, "input": data}),
        ),
    )
    original(kernel.execute(Command("stop", "stop", data, wi), "human"))
    preserved = (root / "facts.sqlite").read_bytes()
    second = install(bundle, built["sha256"], root, policy_file)
    assert first == second
    assert (root / "facts.sqlite").read_bytes() == preserved
    # An actual copied installation is not an admitted successor, including
    # when the operator points a valid new release at the copied database.
    from harness_rewrite.model import Rejected

    # Cross-store migration peers are retired. Reject such a policy before
    # creating a release or changing either installation or authoritative bytes.
    pairing_key = root / "test-pairing-key"
    pairing_key.write_bytes(b"synthetic-pairing-key-material-123456")
    pairing_key.chmod(0o600)
    malformed_pair = tmp_path / "malformed-pair.json"
    malformed_pair.write_text(
        json.dumps(
            {
                **policy,
                "migration_peers": [
                    {
                        "alias": "destination",
                        "store_id": "62d6dc2f1961466dba1520be5f942ac1",
                        "location": "1" * 64,
                        "key_file": str(pairing_key),
                    }
                ],
            }
        )
    )
    releases_before = set((root / "releases").iterdir())
    active_before = (root / "active").readlink()
    with pytest.raises(ValueError, match="unsupported policy field"):
        install(bundle, built["sha256"], root, malformed_pair)
    assert (root / "facts.sqlite").read_bytes() == preserved
    assert (root / "active").readlink() == active_before
    assert set((root / "releases").iterdir()) == releases_before

    cold = tmp_path / "cold-installed"
    shutil.copytree(root, cold, symlinks=True)
    cold_policy = tmp_path / "cold-policy.json"
    cold_policy.write_text(json.dumps(policy).replace(str(root), str(cold)))
    with pytest.raises(Rejected, match="fact-store copy is read-only"):
        install(successor, successor_built["sha256"], cold, cold_policy)
    assert (cold / "facts.sqlite").read_bytes() == preserved
    assert (cold / "active/harness.pyz").read_bytes() == bundle.read_bytes()
    missing_location_contract = tmp_path / "missing-location-contract.pyz"
    with (
        zipfile.ZipFile(bundle) as source_bundle,
        zipfile.ZipFile(missing_location_contract, "w") as old_contract,
    ):
        for member in source_bundle.infolist():
            content = source_bundle.read(member.filename)
            if member.filename == "build.json":
                old_manifest = json.loads(content)
                old_manifest.pop("location_contract")
                content = json.dumps(old_manifest).encode()
            old_contract.writestr(member, content)
    with pytest.raises(ValueError, match="location contract"):
        install(
            missing_location_contract,
            sha(missing_location_contract.read_bytes()),
            root,
            policy_file,
        )
    assert (root / "facts.sqlite").read_bytes() == preserved
    assert (root / "active/harness.pyz").read_bytes() == bundle.read_bytes()
    with pytest.raises(ValueError, match="cannot replay"):
        install(incompatible, older["sha256"], root, policy_file)
    assert (root / "active" / "harness.pyz").read_bytes() == bundle.read_bytes()
    assert (root / "facts.sqlite").read_bytes() == preserved
    assert (root / "active" / "harness.pyz").read_bytes() == bundle.read_bytes()
    assert not list(root.rglob("__pycache__"))
    with pytest.raises(ValueError, match="Transport digest"):
        install(bundle, "0" * 64, root, policy_file)
    corrupted = tmp_path / "corrupt.pyz"
    with (
        zipfile.ZipFile(bundle) as archived,
        zipfile.ZipFile(corrupted, "w") as changed,
    ):
        for item in archived.infolist():
            changed.writestr(
                item,
                b"invalid"
                if item.filename == "harness_rewrite/cli.py"
                else archived.read(item.filename),
            )
    with pytest.raises(ValueError, match="bytes differ"):
        install(corrupted, sha(corrupted.read_bytes()), root, policy_file)
    assert (root / "active" / "harness.pyz").read_bytes() == bundle.read_bytes()

    # The legacy SQL writer has no program admission function and cannot append
    # authoritative inputs after the installation contract is active.
    import sqlite3

    with sqlite3.connect(root / "facts.sqlite") as legacy:
        with pytest.raises(
            sqlite3.OperationalError,
            match="harness_(writer_id|store_location|custody_id)",
        ):
            legacy.execute("INSERT INTO originals VALUES('foreign','digest',X'00')")

    class Crash(BaseException):
        pass

    def interrupted(stage):
        if stage == fault_stage:
            raise Crash()

    with pytest.raises(Crash):
        install(
            successor, successor_built["sha256"], root, policy_file, fault=interrupted
        )
    assert (root / "active" / "harness.pyz").read_bytes() == (
        successor.read_bytes()
        if fault_stage in {"after_pointer", "after_activation_commit"}
        else bundle.read_bytes()
    )
    if fault_stage != "after_activation_commit":
        assert (root / "facts.sqlite").read_bytes() == preserved
    next_kernel = Kernel(
        root / "facts.sqlite",
        contexts.__getitem__,
        writer_id=writer_identity(successor_built["sha256"], policy),
    )
    # Pointer switched but SQLite rolled back: new writes are closed until the
    # exact installation is retried. No old business fact is lost.
    if fault_stage != "after_activation_commit":
        rejected = next_kernel.execute(
            Command("stop-after-interruption", "stop", data, wi), "human"
        )
        assert rejected["error"]["code"] == "program_changed"
    assert next_kernel.inspect(Query(wi), "human")["work_item"]["local_revision"] == 2
    install(successor, successor_built["sha256"], root, policy_file)
    assert (
        kernel.execute(Command("old-writer", "stop", data, wi), "human")["error"][
            "code"
        ]
        == "program_changed"
    )
    inactive = kernel.inspect(Query(wi), "human")
    assert inactive["next_action"]["intent"] == "use_active_program"
    assert all(
        c["state"] == "unavailable"
        for c in inactive["capabilities"]
        if c["name"] != "local_read"
    )
    original(next_kernel.execute(Command("current-writer", "stop", data, wi), "human"))
    assert next_kernel.inspect(Query(wi), "human")["work_item"]["local_revision"] == 3

    # The stable entry resolves its immutable archive before importing the kernel.
    help_result = subprocess.run(
        [sys.executable, "-I", "-B", str(root / "active" / "harness.pyz"), "--help"],
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0 and "execute" in help_result.stdout
    with sqlite3.connect(root / "facts.sqlite") as stale:
        stale.create_function("harness_writer_id", 0, lambda: built["sha256"])
        from harness_rewrite.location import observed

        stale.create_function(
            "harness_store_location", 0, lambda: observed(root / "facts.sqlite")
        )
        with pytest.raises(
            (sqlite3.IntegrityError, sqlite3.OperationalError),
            match="inactive_program|harness_custody_id",
        ):
            stale.execute("INSERT INTO originals VALUES('foreign','digest',X'00')")

    # A real in-flight kernel transaction must settle before a rollback program
    # can inspect the authoritative frontier. The independent gate runs after it.
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    in_transaction, let_commit, installing = Event(), Event(), Event()

    def hold(stage):
        if stage == "before_commit":
            in_transaction.set()
            assert let_commit.wait(5)

    running = Kernel(
        root / "facts.sqlite",
        contexts.__getitem__,
        writer_id=writer_identity(successor_built["sha256"], policy),
        fault=hold,
    )

    def restore():
        installing.set()
        return install(bundle, built["sha256"], root, policy_file)

    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(
            running.execute, Command("settle-before-switch", "stop", data, wi), "human"
        )
        assert in_transaction.wait(5)
        upgrade = pool.submit(restore)
        assert installing.wait(5)
        assert not upgrade.done()
        let_commit.set()
        original(write.result(timeout=5))
        upgrade.result(timeout=10)
    assert kernel.inspect(Query(wi), "human")["work_item"]["local_revision"] == 4
    assert (
        next_kernel.execute(Command("inactive-successor", "stop", data, wi), "human")[
            "error"
        ]["code"]
        == "program_changed"
    )
    original(kernel.execute(Command("restored-program", "stop", data, wi), "human"))

    # A configuration-only update has a distinct immutable release and writer,
    # even with precisely the same program. Every activation fault is exercised.
    changed_policy = json.loads(json.dumps(policy))
    changed_policy["profiles"][0]["artifact_roots"].append(str(root / "more-artifacts"))
    changed_file = tmp_path / "changed-policy.json"
    changed_file.write_text(json.dumps(changed_policy))
    previous_release = (root / "active").resolve()
    previous_receipt = json.loads((previous_release / "installation.json").read_text())
    with pytest.raises(Crash):
        install(bundle, built["sha256"], root, changed_file, fault=interrupted)
    changed = install(bundle, built["sha256"], root, changed_file)
    assert changed["release"] != previous_receipt["release"]
    assert changed["bundle_sha256"] == previous_receipt["bundle_sha256"]
    assert changed["writer_admission"] != previous_receipt["writer_admission"]
    assert json.loads((previous_release / "host-policy.json").read_text()) == policy
    assert install(bundle, built["sha256"], root, changed_file) == changed
    assert (
        kernel.inspect(Query(wi), "human")["next_action"]["reason"] == "program_changed"
    )
    assert (
        kernel.execute(Command("old-policy", "stop", data, wi), "human")["error"][
            "code"
        ]
        == "program_changed"
    )
    configured = Kernel(
        root / "facts.sqlite",
        contexts.__getitem__,
        writer_id=changed["writer_admission"],
    )
    original(configured.execute(Command("new-policy", "stop", data, wi), "human"))
    with sqlite3.connect(root / "facts.sqlite") as legacy_installer:
        with pytest.raises(
            sqlite3.OperationalError, match="harness_writer_contract|harness_custody_id"
        ):
            legacy_installer.execute(
                "UPDATE runtime_binding SET program=?", (built["sha256"],)
            )
        legacy_installer.create_function("harness_writer_contract", 0, lambda: 1)
        with pytest.raises(
            (sqlite3.IntegrityError, sqlite3.OperationalError),
            match="retired_writer_contract|harness_custody_id",
        ):
            legacy_installer.execute(
                "UPDATE runtime_binding SET program=?", (built["sha256"],)
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable_writer_contract"):
            legacy_installer.execute("UPDATE runtime_contract SET minimum=1")
    drifted = {**changed_policy, "delivery_root": str(root / "different-delivery")}
    changed_file.write_text(json.dumps(drifted))
    with pytest.raises(ValueError, match="physical endpoint changed"):
        install(bundle, built["sha256"], root, changed_file)
    assert (root / "active").resolve().name == changed["release"]

    if fault_stage == "after_fact_gate":
        # Concurrent policy updates serialize on the authoritative store. The
        # last pointer and its admission must agree without any business edit.
        def business_rows():
            with sqlite3.connect(root / "facts.sqlite") as connection:
                return {
                    table: connection.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    ).fetchall()
                    for table in ("originals", "journal", "journal_heads")
                }

        preserved_rows = business_rows()
        gate, release_gate, competing = Event(), Event(), Event()
        final_policy = json.loads(json.dumps(changed_policy))
        final_policy["profiles"][0]["artifact_roots"].append(
            str(root / "final-artifacts")
        )
        final_file = tmp_path / "final-policy.json"
        final_file.write_text(json.dumps(final_policy))

        def hold_install(stage):
            if stage == "before_pointer":
                gate.set()
                assert release_gate.wait(10)

        def install_competing():
            competing.set()
            return install(bundle, built["sha256"], root, final_file)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_install = pool.submit(
                install, bundle, built["sha256"], root, policy_file, fault=hold_install
            )
            assert gate.wait(10)
            final_install = pool.submit(install_competing)
            assert competing.wait(5)
            assert not final_install.done()
            release_gate.set()
            first_install.result(timeout=10)
            final_receipt = final_install.result(timeout=10)
        assert (root / "active").resolve().name == final_receipt["release"]
        assert (
            json.loads((root / "active" / "host-policy.json").read_text())
            == final_policy
        )
        with sqlite3.connect(root / "facts.sqlite") as connection:
            assert (
                connection.execute("SELECT program FROM runtime_binding").fetchone()[0]
                == final_receipt["writer_admission"]
            )
        assert business_rows() == preserved_rows
