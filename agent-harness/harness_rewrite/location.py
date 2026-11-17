"""A fact store belongs to its actual host and path, independently of its program."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import platform
import os
import json
import stat
import plistlib
import sqlite3
import subprocess
import uuid

from .model import Json, Rejected, digest, encoded, writer_identity


@lru_cache(maxsize=1)
def machine_identity() -> str:
    """Read the existing native machine identity; never create an identity file."""
    try:
        if platform.system() == "Linux":
            identity = Path("/etc/machine-id").read_text().strip()
        elif platform.system() == "Darwin":
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-a", "-r", "-d1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                check=True,
                timeout=5,
            )
            rows = plistlib.loads(result.stdout)
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError("Ambiguous native machine identity")
            identity = rows[0]["IOPlatformUUID"]
        else:
            raise ValueError("Unsupported native platform")
        if not isinstance(identity, str) or uuid.UUID(identity).int == 0:
            raise ValueError("Native machine identity unavailable")
        return digest(["native-machine/v1", platform.system(), identity])
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        raise Rejected(
            "source_unavailable", "Native fact-store location cannot be established"
        ) from exc


def observed(database: Path) -> str:
    if not database.is_absolute() or database.resolve() != database:
        raise Rejected(
            "authority_denied", "Fact-store location must be a real absolute path"
        )
    return digest(["fact-store-location/v1", machine_identity(), str(database)])


def binding(connection: sqlite3.Connection) -> dict[str, str] | None:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_binding'"
    ).fetchone():
        return None
    rows = connection.execute("SELECT store_id,location FROM store_binding").fetchall()
    if len(rows) != 1:
        raise Rejected("journal_invalid", "Fact-store binding is invalid")
    return {"store_id": rows[0][0], "location": rows[0][1]}


def admitted(connection: sqlite3.Connection, database: Path) -> bool:
    expected = binding(connection)
    if expected is None:
        return True
    try:
        return expected["location"] == current(connection, database)
    except Rejected:
        return False


def bind(connection: sqlite3.Connection, database: Path) -> dict[str, str]:
    """Operator installation pins a previously unbound store; it never rebinds one."""
    actual = observed(database)
    existing = binding(connection)
    if existing is not None:
        if existing["location"] != actual:
            raise Rejected(
                "authority_location_changed", "Copied fact store remains read-only"
            )
        return existing
    result = {"store_id": "store." + uuid.uuid4().hex, "location": actual}
    return _insert_binding(connection, result)


def _insert_binding(
    connection: sqlite3.Connection, result: dict[str, str]
) -> dict[str, str]:
    connection.execute(
        "CREATE TABLE store_binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
        "store_id TEXT NOT NULL, location TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO store_binding VALUES(1,?,?)",
        (result["store_id"], result["location"]),
    )
    for action in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER store_binding_{action.lower()} BEFORE {action} ON store_binding "
            "BEGIN SELECT RAISE(ABORT, 'immutable_store_binding'); END"
        )
    # Existing runtimes that do not implement the location contract cannot write
    # even if an old installer makes their program active. No function means deny.
    for table in ("originals", "journal", "journal_heads"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER store_guard_{table}_{action.lower()} BEFORE {action} ON {table} "
                "WHEN NOT EXISTS (SELECT 1 FROM store_binding WHERE singleton=1 "
                "AND location=harness_store_location()) "
                "BEGIN SELECT RAISE(ABORT, 'foreign_fact_store'); END"
            )
    return result


def custody(connection: sqlite3.Connection) -> Json | None:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_custody'"
    ).fetchone():
        return None
    rows = connection.execute("SELECT content,digest FROM store_custody").fetchall()
    if len(rows) != 1:
        raise Rejected("journal_invalid", "Store custody is invalid")
    record = json.loads(rows[0][0])
    if digest(record) != rows[0][1]:
        raise Rejected("journal_invalid", "Store custody digest differs")
    return dict(record)


def private_path(path: Path, *, directory: bool = False) -> os.stat_result:
    observed_stat = path.stat()
    if (
        not path.is_absolute()
        or path.resolve() != path
        or observed_stat.st_uid != os.getuid()
        or observed_stat.st_mode & 0o077
        or not (
            stat.S_ISDIR(observed_stat.st_mode)
            if directory
            else stat.S_ISREG(observed_stat.st_mode)
        )
    ):
        raise Rejected(
            "authority_location_changed", "Store custody path is not private and real"
        )
    return observed_stat


def custody_matches(record: Json, database: Path) -> bool:
    try:
        anchor = Path(record["anchor"])
        actual = private_path(database)
        private_path(anchor.parent, directory=True)
        linked = private_path(anchor)
        receipt = anchor.parent / "receipt.json"
        private_path(receipt)
        return bool(
            record["database"] == str(database)
            and record["inode"] == actual.st_ino
            and (actual.st_dev, actual.st_ino) == (linked.st_dev, linked.st_ino)
            and json.loads(receipt.read_text()) == record
        )
    except (OSError, ValueError, KeyError, TypeError, Rejected):
        return False


def current(connection: sqlite3.Connection, database: Path) -> str:
    record = custody(connection)
    if record is None:
        return observed(database)
    if not custody_matches(record, database) or record["store"] != binding(connection):
        return "unadmitted-custody"
    return str(record["store"]["location"])


def custody_id(connection: sqlite3.Connection, database: Path) -> str:
    record = custody(connection)
    if record is None or not custody_matches(record, database):
        return ""
    return digest(record)


def frontier(connection: sqlite3.Connection) -> str:
    # Include unreferenced originals as well as all journal rows and heads.
    result = {}
    for table in ("originals", "journal", "journal_heads"):
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        result[table] = [
            [
                digest_bytes(value) if isinstance(value, bytes) else value
                for value in row
            ]
            for row in rows
        ]
    return digest(result)


def digest_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def enroll(
    connection: sqlite3.Connection,
    database: Path,
    root: Path,
    policy: Json,
    proposed: Path,
    *,
    adopt_existing: bool,
) -> Json:
    """Installation metadata only; never creates a native principal or WI grant."""
    existing_custody = custody(connection)
    if existing_custody is not None:
        if not custody_matches(existing_custody, database):
            raise Rejected(
                "authority_location_changed",
                "This fact-store copy is read-only; custody differs",
            )
        return existing_custody
    existing = binding(connection)
    if existing is not None and not adopt_existing:
        raise Rejected(
            "authority_location_changed",
            "Existing store requires explicit operator custody conversion",
        )
    directory = root / ".store-custody"
    anchor = directory / "database"
    receipt_path = directory / "receipt.json"
    database_stat = private_path(database)
    if database.resolve() != database or root not in database.parents:
        raise Rejected(
            "authority_location_changed", "Database is outside original installation"
        )
    directory.mkdir(mode=0o700, exist_ok=True)
    private_path(directory, directory=True)
    saved = None
    if receipt_path.exists() or receipt_path.is_symlink():
        private_path(receipt_path)
        saved = json.loads(receipt_path.read_text())
        if (
            not custody_matches(saved, database)
            or saved["proposed"] != str(proposed)
            or saved["frontier"] != frontier(connection)
            or existing is not None
            and saved["store"] != existing
        ):
            raise Rejected(
                "authority_location_changed", "Original custody conversion differs"
            )
    predecessor: Path | None = None
    writer = None
    if existing is not None:
        if not (root / "active").is_symlink():
            raise Rejected(
                "authority_location_changed", "Original active installation unavailable"
            )
        active = (root / "active").resolve(strict=True)
        predecessor = Path(saved["predecessor"]) if saved is not None else active
        if (
            predecessor.parent != root / "releases"
            or predecessor.resolve() != predecessor
            or active not in {predecessor, proposed}
        ):
            raise Rejected("authority_location_changed", "Original predecessor differs")
        from .release import verify, sha

        archive = predecessor / "harness.pyz"
        manifest = verify(archive)
        previous = json.loads((predecessor / "host-policy.json").read_text())
        receipt = json.loads((predecessor / "installation.json").read_text())
        checksum = sha(archive.read_bytes())
        writer = writer_identity(checksum, previous)
        row = connection.execute("SELECT program FROM runtime_binding").fetchone()
        if (
            previous["database"] != str(database)
            or receipt["bundle_sha256"] != checksum
            or receipt["policy_sha256"] != digest(previous)
            or receipt["source_revision"] != manifest["source_revision"]
            or receipt["writer_admission"] != writer
            or row is None
            or row[0] != writer
            or saved is not None
            and saved["writer"] != writer
        ):
            raise Rejected(
                "authority_location_changed", "Original writer admission differs"
            )
        for key in (
            "namespace",
            "surface",
            "database",
            "views_root",
            "delivery_root",
            "dispatch_root",
            "verification_actor",
            "human_inputs",
            "agent_config_dir",
            "python",
            "node",
            "pi_binary",
            "producer_node",
            "relay_entry",
        ):
            if key in previous and previous[key] != policy.get(key):
                raise Rejected(
                    "authority_location_changed",
                    "Original installation endpoint differs",
                )
    elif connection.execute("SELECT count(*) FROM journal").fetchone()[0]:
        raise Rejected(
            "authority_location_changed",
            "Unbound nonempty store requires supported conversion",
        )
    if saved is None:
        store = existing or {
            "store_id": "store." + uuid.uuid4().hex,
            "location": digest(
                ["persistent-store/v2", str(database), uuid.uuid4().hex]
            ),
        }
        saved = {
            "schema": "harness.store-custody/v1",
            "database": str(database),
            "anchor": str(anchor),
            "inode": database_stat.st_ino,
            "store": store,
            "predecessor": str(predecessor) if predecessor is not None else None,
            "writer": writer,
            "proposed": str(proposed),
            "frontier": frontier(connection),
        }
        if not anchor.exists():
            os.link(database, anchor)
        if anchor.is_symlink() or not anchor.samefile(database):
            raise Rejected(
                "authority_location_changed", "Custody anchor already belongs elsewhere"
            )
        from .release import fixed

        fixed(receipt_path, encoded(saved))
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if not custody_matches(saved, database):
        raise Rejected(
            "authority_location_changed", "Custody enrollment is not durable"
        )
    if existing is None:
        _insert_binding(connection, saved["store"])
    connection.execute(
        "CREATE TABLE store_custody (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
        "content TEXT NOT NULL, digest TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO store_custody VALUES(1,?,?)",
        (encoded(saved).decode(), digest(saved)),
    )
    for action in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER store_custody_{action.lower()} BEFORE {action} ON store_custody "
            "BEGIN SELECT RAISE(ABORT, 'immutable_store_custody'); END"
        )
    for table in ("originals", "journal", "journal_heads", "runtime_binding"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            # runtime_binding is created by the installer before enrollment.
            connection.execute(
                f"CREATE TRIGGER custody_guard_{table}_{action.lower()} BEFORE {action} ON {table} "
                "WHEN harness_custody_id()<>(SELECT digest FROM store_custody WHERE singleton=1) "
                "BEGIN SELECT RAISE(ABORT, 'foreign_store_custody'); END"
            )
    return dict(saved)
