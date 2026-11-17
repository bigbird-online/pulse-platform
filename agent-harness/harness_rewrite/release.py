"""Exact, stdlib-only candidate bundles and isolated installations.

Installation is an operator action. It never creates or changes WorkItem facts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from typing import Callable

from .model import HostContext, Json, Rejected, encoded, writer_identity


# Non-Python package data read through importlib.resources (zip-safe).
RESOURCES = frozenset(
    {
        "harness_rewrite/verifier-runner.mjs",
        "harness_rewrite/project_page.html",
        "harness_rewrite/project_page.LICENSE",
    }
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(bundle: Path) -> Json:
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive member")
        manifest = json.loads(archive.read("build.json"))
        if manifest.get("schema") != "harness.rewrite_bundle/v1":
            raise ValueError("Unsupported bundle")
        expected = manifest["files"]
        if "harness_rewrite/native_compute.py" in expected and (
            "harness_rewrite/verifier-runner.mjs" not in expected
        ):
            raise ValueError("SDK bundle lacks its fixed runner resource")
        if set(names) != set(expected) | {"build.json"}:
            raise ValueError("Bundle inventory differs")
        for name, checksum in expected.items():
            if name not in {"__main__.py", "OPERATIONS.md"} | RESOURCES and not (
                name.startswith("harness_rewrite/")
                and name.endswith(".py")
                and len(Path(name).parts) == 2
            ):
                raise ValueError("Unexpected bundle member")
            if sha(archive.read(name)) != checksum:
                raise ValueError("Bundle bytes differ")
        return dict(manifest)


def build(repository: Path, revision: str, output: Path) -> Json:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(repository), *args])

    revision = git("rev-parse", revision + "^{commit}").decode().strip()
    paths = (
        git(
            "ls-tree",
            "-r",
            "--name-only",
            revision,
            "--",
            "agent-harness/harness_rewrite",
        )
        .decode()
        .splitlines()
    )
    files = {
        p.removeprefix("agent-harness/"): git("show", revision + ":" + p)
        for p in paths
        if p.endswith(".py") or p.removeprefix("agent-harness/") in RESOURCES
    }
    if "harness_rewrite/cli.py" not in files or "harness_rewrite/release.py" not in files:
        raise ValueError("Revision lacks installed entry")
    files["__main__.py"] = (
        b"import os, sys\n"
        b"archive = os.path.realpath(sys.argv[0])\n"
        b"if archive != os.path.abspath(sys.argv[0]):\n"
        b"    os.execv(sys.executable, [sys.executable, '-I', '-B', archive, *sys.argv[1:]])\n"
        b"from harness_rewrite.cli import main\nraise SystemExit(main())\n"
    )
    try:
        files["OPERATIONS.md"] = git(
            "show",
            revision + ":agent-harness/project-specs/rewrite-native-operations.md",
        )
    except subprocess.CalledProcessError:
        raise ValueError("Revision lacks matching native operations guide") from None
    manifest = {
        "schema": "harness.rewrite_bundle/v1",
        "source_revision": revision,
        "fact_schema": 1,
        "writer_contract": int(match[1])
        if (
            match := re.search(
                rb"^WRITER_CONTRACT = ([0-9]+)$",
                files["harness_rewrite/kernel.py"],
                re.MULTILINE,
            )
        )
        else 0,
        "location_contract": 2
        if b"\nLOCATION_CONTRACT = 2\n" in files["harness_rewrite/kernel.py"]
        else 0,
        "files": {k: sha(v) for k, v in sorted(files.items())},
    }
    if output.exists():
        raise ValueError("Build output already exists")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted({**files, "build.json": encoded(manifest)}.items()):
            item = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            archive.writestr(item, content)
    verify(output)
    return {
        "source_revision": revision,
        "sha256": sha(output.read_bytes()),
        "files": len(files),
    }


def fixed(path: Path, content: bytes, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise ValueError("Installation path redirects")
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("Installed original differs")
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".install-", delete=True) as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        try:
            os.link(stream.name, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError("Concurrent install differs")


def check_fact_compatibility(bundle: Path, policy: Json) -> None:
    database = Path(policy["database"])
    if not database.exists():
        return
    # Probe the proposed sealed program, not the installer's own reducer. No
    # business command, Host identity or mutable cache is used during admission.
    probe = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from harness_rewrite.kernel import Kernel
kernel = Kernel(Path(sys.argv[2]), lambda handle: None)
with kernel._connection(write=False) as connection:
    for row in connection.execute('SELECT namespace,wi FROM journal_heads').fetchall():
        kernel._replay(connection, row['namespace'], row['wi'])
"""
    result = subprocess.run(
        [policy["python"], "-I", "-B", "-c", probe, str(bundle), str(database)],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(
            "Proposed release cannot replay existing authoritative facts; active release preserved"
        )


def upgrade_contract(connection: sqlite3.Connection) -> None:
    """Monotonic protocol upgrade inside the existing activation transaction."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS runtime_contract (singleton INTEGER PRIMARY KEY "
        "CHECK(singleton=1), minimum INTEGER NOT NULL)"
    )
    connection.execute("INSERT INTO runtime_contract VALUES(1,3) ON CONFLICT(singleton) DO NOTHING")
    floor = connection.execute("SELECT minimum FROM runtime_contract").fetchone()[0]
    if floor not in {2, 3}:
        raise ValueError("Unsupported installed writer contract")
    # Only this supported installer replaces the old immutable-2 guard. Rollback
    # of the surrounding activation transaction also restores the old trigger.
    if floor == 2:
        connection.execute("DROP TRIGGER IF EXISTS runtime_contract_update")
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS runtime_contract_update BEFORE UPDATE ON runtime_contract "
        "WHEN NEW.singleton != OLD.singleton OR NEW.minimum < OLD.minimum "
        "OR harness_writer_contract() < NEW.minimum "
        "BEGIN SELECT RAISE(ABORT, 'immutable_writer_contract'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS runtime_contract_delete BEFORE DELETE ON runtime_contract "
        "BEGIN SELECT RAISE(ABORT, 'immutable_writer_contract'); END"
    )
    connection.execute("UPDATE runtime_contract SET minimum=3 WHERE singleton=1 AND minimum=2")


def install(
    bundle: Path,
    checksum: str,
    root: Path,
    policy_file: Path,
    *,
    fault: Callable[[str], None] | None = None,
    adopt_existing_store: bool = False,
    relay_release: Path | None = None,
    prepare_only: bool = False,
) -> Json:
    if sha(bundle.read_bytes()) != checksum:
        raise ValueError("Transport digest mismatch")
    manifest = verify(bundle)
    if manifest.get("writer_contract") != 3:
        raise ValueError("Proposed release lacks the current writer contract")
    if manifest.get("location_contract") != 2:
        raise ValueError("Proposed release lacks the current fact-store location contract")
    fault = fault or (lambda stage: None)
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Installation root must be an existing real directory")
    from . import policy as policies

    policy = policies.load(policy_file)
    if prepare_only and "production_ledger" not in policy:
        raise ValueError("Prepare-only deployment requires the original production ledger")
    # Fail an already visible identity change before creating preparation paths;
    # repeat this comparison under the activation lock for concurrent installs.
    relay_proof = None
    running = False
    if relay_release is not None and not (root / "active").is_symlink():
        raise ValueError("Relay adoption requires the existing active installation")
    predecessor_link = root / "active"
    if predecessor_link.is_symlink():
        predecessor = predecessor_link.resolve(strict=True)
        if predecessor.parent != root / "releases":
            raise ValueError("Unproven active installation")
        previous_policy = policies.load(predecessor / "host-policy.json")
        receipt_path = predecessor / "installation.json"
        installed = json.loads(receipt_path.read_bytes()) if receipt_path.is_file() else {}
        # The running program prepared again, as a rollback cohort: the same
        # program and policy were already adopted, so nothing new is admitted.
        running = previous_policy == policy and installed.get("bundle_sha256") == checksum
        # A changed database still goes through the store-custody boundary;
        # preserve its specific copy/replacement rejection and recovery guidance.
        if running:
            pass
        elif previous_policy.get("database") == policy.get("database") or relay_release is not None:
            relay_proof = policies.compatible(previous_policy, policy, relay_release=relay_release)
    for directory in policies.preflight(policy, root):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Each immutable release carries an exact operator policy. The one active
    # pointer is replaceable; SQLite state is shared by compatible releases.
    policy_digest = sha(encoded(policy))
    admission = writer_identity(checksum, policy)
    release_id = manifest["source_revision"][:12] + "-" + checksum[:16] + "-" + policy_digest[:16]
    releases = root / "releases"
    if releases.is_symlink():
        raise ValueError("Release root redirects")
    releases.mkdir(mode=0o700, exist_ok=True)
    release = releases / release_id
    if release.is_symlink():
        raise ValueError("Release redirects")
    release.mkdir(mode=0o700, exist_ok=True)
    fixed(release / "harness.pyz", bundle.read_bytes())
    fixed(release / "host-policy.json", encoded(policy))
    receipt = {
        "schema": "harness.rewrite_install/v1",
        "source_revision": manifest["source_revision"],
        "bundle_sha256": checksum,
        "policy_sha256": policy_digest,
        "writer_admission": admission,
        "release": release_id,
        "fact_schema": manifest["fact_schema"],
        "writer_contract": manifest["writer_contract"],
        "location_contract": manifest["location_contract"],
    }
    fixed(release / "installation.json", encoded(receipt))
    # The running program keeps the proof of its original adoption; recomputing
    # would name itself as predecessor and reread consumed preparation receipts.
    if running and relay_release is not None:
        original = release / "relay-adoption.json"
        if (
            predecessor_link.resolve(strict=True) != release.resolve()
            or original.is_symlink()
            or not original.is_file()
            or json.loads(original.read_bytes()).get("release") != str(relay_release)
        ):
            raise ValueError("The running program is prepared again only with its original adoption proof")
    elif relay_proof is not None:
        fixed(release / "relay-adoption.json", encoded(relay_proof))
    active = root / "active"
    if active.exists() and not active.is_symlink():
        raise ValueError("Unowned active path")
    if active.is_symlink():
        old = active.resolve(strict=True)
        if (
            old.parent != releases
            or json.loads((old / "installation.json").read_text())["fact_schema"]
            != manifest["fact_schema"]
        ):
            raise ValueError("Unproven or incompatible predecessor")
    if "production_ledger" in policy:
        from .production_install import activate

        return activate(root, release, policy, receipt, fault,
                        relay_release=None if running else relay_release,
                        prepare_only=prepare_only)

    from .kernel import Kernel

    def no_business_caller(handle: object) -> HostContext:
        raise PermissionError("Installation does not supply a business identity")

    kernel = Kernel(Path(policy["database"]), no_business_caller)
    if not Path(policy["database"]).exists():
        # Initialize the storage schema only. No WI or business input is created.
        with kernel._connection(write=True, activation=True) as connection:
            connection.commit()
    with kernel._connection(
        write=True, activation=True, location_upgrade=adopt_existing_store
    ) as connection:
        # BEGIN IMMEDIATE waits for already admitted writes and excludes later
        # writes while the exact new program replays the now stable facts.
        # Re-read the active pair under the same serialization boundary: a
        # concurrent install may have changed it since preparation began.
        if active.is_symlink():
            predecessor = active.resolve(strict=True)
            if predecessor.parent != releases:
                raise ValueError("Unproven active installation")
            previous_policy = json.loads((predecessor / "host-policy.json").read_text())
            current_proof = (
                None
                if running and previous_policy == policy
                else policies.compatible(previous_policy, policy, relay_release=relay_release)
            )
            if current_proof != relay_proof or (running and previous_policy != policy):
                raise ValueError("Relay admission evidence changed during installation")
        check_fact_compatibility(release / "harness.pyz", policy)
        fault("after_fact_gate")
        from . import location

        connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), program TEXT NOT NULL)"
        )
        location.enroll(
            connection,
            Path(policy["database"]),
            root,
            policy,
            release,
            adopt_existing=adopt_existing_store,
        )
        fault("after_store_binding")
        upgrade_contract(connection)
        for action in ("INSERT", "UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS runtime_binding_contract_{action.lower()} "
                f"BEFORE {action} ON runtime_binding "
                "WHEN harness_writer_contract()<(SELECT minimum FROM runtime_contract WHERE singleton=1) "
                "BEGIN SELECT RAISE(ABORT, 'retired_writer_contract'); END"
            )
        for table in ("originals", "journal", "journal_heads"):
            for action in ("INSERT", "UPDATE", "DELETE"):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS runtime_guard_{table}_{action.lower()} BEFORE {action} ON {table} "
                    "WHEN NOT EXISTS (SELECT 1 FROM runtime_binding WHERE singleton=1 AND program=harness_writer_id()) "
                    "BEGIN SELECT RAISE(ABORT, 'inactive_program'); END"
                )
        connection.execute(
            "INSERT INTO runtime_binding VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET program=excluded.program WHERE program<>excluded.program",
            (admission,),
        )
        fault("before_pointer")
        pointer = root / (".activate-" + release_id)
        if pointer.is_symlink():
            if os.readlink(pointer) != "releases/" + release_id:
                raise ValueError("Activation target differs")
        else:
            pointer.symlink_to("releases/" + release_id)
        os.replace(pointer, active)
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fault("after_pointer")
        connection.commit()
        fault("after_activation_commit")
    return {**receipt, "active": str(active / "harness.pyz")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    b = commands.add_parser("build")
    b.add_argument("--repository", type=Path, required=True)
    b.add_argument("--revision", required=True)
    b.add_argument("--output", type=Path, required=True)
    i = commands.add_parser("install")
    i.add_argument("--bundle", type=Path, required=True)
    i.add_argument("--sha256", required=True)
    i.add_argument("--root", type=Path, required=True)
    i.add_argument("--policy", type=Path, required=True)
    i.add_argument("--adopt-existing-store", action="store_true")
    i.add_argument("--adopt-relay-release", type=Path)
    v = commands.add_parser("verify")
    v.add_argument("--bundle", type=Path, required=True)
    storage = commands.add_parser("storage-convert")
    storage.add_argument("--root", type=Path, required=True)
    mode = storage.add_mutually_exclusive_group(required=True)
    mode.add_argument("--wi")
    mode.add_argument("--finalize", action="store_true")
    for name in ("policy-inspect", "policy-diff"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--policy", type=Path, required=True)
        if name == "policy-diff":
            command.add_argument("--against", type=Path, required=True)
            command.add_argument("--adopt-relay-release", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "build":
            result = build(args.repository, args.revision, args.output)
        elif args.action == "install":
            result = install(
                args.bundle,
                args.sha256,
                args.root,
                args.policy,
                adopt_existing_store=args.adopt_existing_store,
                relay_release=args.adopt_relay_release,
            )
        elif args.action in {"policy-inspect", "policy-diff"}:
            from .policy import review

            options = (
                {"relay_release": args.adopt_relay_release}
                if getattr(args, "adopt_relay_release", None)
                else {}
            )
            result = review(args.root, args.policy, getattr(args, "against", None), **options)
        elif args.action == "storage-convert":
            from .policy import load
            from .production_install import require_active_program

            entry = (args.root / "active/harness.pyz").resolve(strict=True)
            policy = load(entry.parent / "host-policy.json")
            require_active_program(entry, policy)
            # Maintenance always runs the admitted immutable program, not this
            # checkout's imports. It never constructs a native human identity.
            script = """import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from harness_rewrite.production_ledger import ProductionLedger
p=json.loads((Path(sys.argv[1]).parent/'host-policy.json').read_bytes())
ledger=ProductionLedger(p['production_ledger'],p['namespace'])
result=ledger.reformat(sys.argv[2]) if sys.argv[2] else ledger.finish_reformat()
print(json.dumps(result))
"""
            process = subprocess.run([policy["python"], "-I", "-B", "-c", script,
                                      str(entry), args.wi or ""], capture_output=True, timeout=120)
            if process.returncode:
                raise ValueError("Installed storage conversion did not complete; inspect the unchanged original request history")
            result = json.loads(process.stdout)
        else:
            result = verify(args.bundle)
        print(json.dumps(result, sort_keys=True))
    except (
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        subprocess.TimeoutExpired,
        Rejected,
    ) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}))
        sys.exit(2)


if __name__ == "__main__":
    main()
