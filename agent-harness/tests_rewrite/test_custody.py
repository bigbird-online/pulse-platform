"""Persistent file ownership survives runtime replacement; copies do not inherit it."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from harness_rewrite import Command, Kernel, Query
from harness_rewrite import location
from harness_rewrite.model import Rejected, SourceInput, writer_identity
from harness_rewrite.release import build, install
from harness_rewrite.domain import INTENTS
from tests_rewrite.test_workflow import BASE, DEFINITION, original


@pytest.fixture
def retained_store(tmp_path, monkeypatch):
    source = tmp_path / "source"
    package = source / "agent-harness/harness_rewrite"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "harness_rewrite",
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    specs = source / "agent-harness/project-specs"
    specs.mkdir()
    (specs / "rewrite-native-operations.md").write_text(
        "Synthetic installation fixture"
    )

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(source), *args], stderr=subprocess.STDOUT
        )

    git("init")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Synthetic sealed candidate",
    )
    bundle = tmp_path / "candidate.pyz"
    built = build(source, "HEAD", bundle)
    root = tmp_path / "installed"
    root.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    policy = {
        "namespace": "fixture",
        "surface": "fixture",
        "database": str(root / "facts.sqlite"),
        "views_root": str(root / "views"),
        "delivery_root": str(root / "delivery"),
        "dispatch_root": str(root / "verifiers"),
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
                "session_root": str(root / "verifiers"),
                "artifact_roots": [str(root / "verifiers")],
            },
        ],
    }
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(policy))
    # Create an explicit legacy-format fixture using the actual contract-1 bind.
    # This is not native recovery evidence or a fabricated production receipt.
    with monkeypatch.context() as fixture:
        fixture.setattr(location, "enroll", lambda c, d, *a, **kw: location.bind(c, d))
        install(bundle, built["sha256"], root, policy_file)
    context = replace(
        BASE,
        allowed_intents=INTENTS,
        source=SourceInput("fixture-create", "native_agent", b"fixture source"),
    )
    kernel = Kernel(
        root / "facts.sqlite",
        lambda _: context,
        writer_id=writer_identity(built["sha256"], policy),
    )
    wi = original(
        kernel.execute(Command("fixture-create", "create_draft", DEFINITION), None)
    )["work_item_id"]
    (specs / "rewrite-native-operations.md").write_text("Exact successor fixture")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Distinct successor program",
    )
    bundle = tmp_path / "successor.pyz"
    built = build(source, "HEAD", bundle)
    kernel = Kernel(
        root / "facts.sqlite",
        lambda _: context,
        writer_id=writer_identity(built["sha256"], policy),
    )
    return root, bundle, built, policy_file, kernel, wi


def rows(database):
    with sqlite3.connect(database) as connection:
        return {
            name: connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
            for name in ("originals", "journal", "journal_heads", "store_binding")
        }


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
def test_original_store_conversion_keeps_facts_across_crashes_and_runtime_change(
    retained_store,
    monkeypatch,
    fault_stage,
):
    root, bundle, built, policy_file, kernel, wi = retained_store
    database = root / "facts.sqlite"
    before = rows(database)
    fixed = database.read_bytes()
    monkeypatch.setattr(location, "machine_identity", lambda: "different-runtime")
    assert kernel.inspect(Query(wi), None)["status"] == "read_only"
    with kernel._connection(write=False) as c:
        assert not location.admitted(c, database)
    with pytest.raises(Rejected, match="read-only"):
        install(bundle, built["sha256"], root, policy_file)
    assert database.read_bytes() == fixed

    class Crash(BaseException):
        pass

    def interrupt(stage):
        if stage == fault_stage:
            raise Crash()

    with pytest.raises(Crash):
        install(
            bundle,
            built["sha256"],
            root,
            policy_file,
            adopt_existing_store=True,
            fault=interrupt,
        )
    assert rows(database) == before
    if fault_stage != "after_activation_commit":
        assert database.read_bytes() == fixed
    installed = install(
        bundle, built["sha256"], root, policy_file, adopt_existing_store=True
    )
    assert rows(database) == before
    with kernel._connection(write=False) as c:
        assert location.admitted(c, database)
    receipt = (root / ".store-custody/receipt.json").read_bytes()
    settled = database.read_bytes()
    assert install(bundle, built["sha256"], root, policy_file) == installed
    assert database.read_bytes() == settled
    assert (root / ".store-custody/receipt.json").read_bytes() == receipt
    monkeypatch.setattr(
        location,
        "machine_identity",
        lambda: (_ for _ in ()).throw(Rejected("source_unavailable", "no runtime ID")),
    )
    with kernel._connection(write=False) as c:
        assert location.admitted(c, database)
    # Even if the old runtime identity reappears, the old writer cannot append
    # or reactivate: it has no custody function.
    with sqlite3.connect(database) as stale:
        stale.create_function(
            "harness_store_location", 0, lambda: before["store_binding"][0][2]
        )
        stale.create_function("harness_writer_contract", 0, lambda: 2)
        stale.create_function(
            "harness_writer_id", 0, lambda: installed["writer_admission"]
        )
        for sql in (
            "INSERT INTO originals VALUES('fixture','x',X'00')",
            "UPDATE runtime_binding SET program='obsolete'",
        ):
            with pytest.raises(sqlite3.OperationalError, match="harness_custody_id"):
                stale.execute(sql)
    assert rows(database) == before
    original(
        kernel.execute(Command("after-restoration", "create_draft", DEFINITION), None)
    )


@pytest.mark.parametrize(
    "change",
    ["copied_installation", "replaced_database", "missing_anchor", "redirected_anchor"],
)
def test_custody_does_not_admit_copies_or_replacement(retained_store, tmp_path, change):
    root, bundle, built, policy_file, kernel, wi = retained_store
    install(bundle, built["sha256"], root, policy_file, adopt_existing_store=True)
    database = root / "facts.sqlite"
    if change == "copied_installation":
        copied = tmp_path / "cold"
        shutil.copytree(root, copied, symlinks=True)
        policy = json.loads(policy_file.read_text().replace(str(root), str(copied)))
        policy_file.write_text(json.dumps(policy))
        root = copied
        database = root / "facts.sqlite"
        kernel = Kernel(database, kernel._authenticate, writer_id=kernel._writer_id)
    elif change == "replaced_database":
        replacement = root / "replacement.sqlite"
        shutil.copyfile(database, replacement)
        replacement.chmod(0o600)
        os.replace(replacement, database)
    elif change == "missing_anchor":
        (root / ".store-custody/database").unlink()
    else:
        (root / ".store-custody/database").unlink()
        (root / ".store-custody/database").symlink_to(database)
    checksum = hashlib.sha256(database.read_bytes()).hexdigest()
    assert kernel.inspect(Query(wi), None)["status"] == "read_only"
    with pytest.raises(Rejected, match="read-only"):
        install(bundle, built["sha256"], root, policy_file, adopt_existing_store=True)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == checksum


def test_legacy_copy_cannot_use_conversion_to_change_original_path(
    retained_store, tmp_path
):
    root, bundle, built, policy_file, _, _ = retained_store
    copied = tmp_path / "legacy-copy"
    shutil.copytree(root, copied, symlinks=True)
    copied_policy = tmp_path / "copy-policy.json"
    copied_policy.write_text(policy_file.read_text().replace(str(root), str(copied)))
    fixed = (copied / "facts.sqlite").read_bytes()
    with pytest.raises(
        (Rejected, ValueError), match="endpoint changed|admission differs"
    ):
        install(
            bundle, built["sha256"], copied, copied_policy, adopt_existing_store=True
        )
    assert (copied / "facts.sqlite").read_bytes() == fixed


@pytest.mark.parametrize("corruption", ["receipt", "writer", "frontier"])
def test_interrupted_conversion_rejects_changed_evidence(retained_store, corruption):
    root, bundle, built, policy_file, _, _ = retained_store

    class Crash(BaseException):
        pass

    def interrupt(stage):
        if stage == "after_store_binding":
            raise Crash()

    with pytest.raises(Crash):
        install(
            bundle,
            built["sha256"],
            root,
            policy_file,
            adopt_existing_store=True,
            fault=interrupt,
        )
    if corruption == "receipt":
        p = root / ".store-custody/receipt.json"
        value = json.loads(p.read_text())
        value["inode"] += 1
        p.write_text(json.dumps(value))
    else:
        with sqlite3.connect(root / "facts.sqlite") as c:
            if corruption == "writer":
                from harness_rewrite.kernel import WRITER_CONTRACT

                c.create_function("harness_writer_contract", 0, lambda: WRITER_CONTRACT)
                c.execute("UPDATE runtime_binding SET program='changed'")
            else:
                # Physical mutation is a test-only corruption fixture.
                c.create_function(
                    "harness_store_location",
                    0,
                    lambda: location.observed(root / "facts.sqlite"),
                )
                c.create_function(
                    "harness_writer_id",
                    0,
                    lambda: c.execute("SELECT program FROM runtime_binding").fetchone()[
                        0
                    ],
                )
                c.execute(
                    "INSERT INTO originals VALUES('fixture','unreferenced',X'00')"
                )
    fixed = (root / "facts.sqlite").read_bytes()
    with pytest.raises(Rejected, match="conversion differs|admission differs"):
        install(bundle, built["sha256"], root, policy_file, adopt_existing_store=True)
    assert (root / "facts.sqlite").read_bytes() == fixed



def test_first_installation_does_not_need_an_os_machine_identity(
    retained_store, tmp_path, monkeypatch
):
    old_root, bundle, built, old_policy_file, _, _ = retained_store
    root = tmp_path / "fresh-persistent-store"
    root.mkdir()
    policy = json.loads(old_policy_file.read_text().replace(str(old_root), str(root)))
    policy_file = tmp_path / "fresh-policy.json"
    policy_file.write_text(json.dumps(policy))
    monkeypatch.setattr(
        location,
        "machine_identity",
        lambda: (_ for _ in ()).throw(Rejected("source_unavailable", "no runtime ID")),
    )
    install(bundle, built["sha256"], root, policy_file)
    with sqlite3.connect(policy["database"]) as connection:
        assert location.admitted(connection, Path(policy["database"]))
        assert location.custody(connection) is not None
