"""Cold file copies retain readable facts but cannot inherit execution authority."""

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from harness_rewrite import Command, Kernel, Query
from harness_rewrite import location
from harness_rewrite.model import Rejected
from tests_rewrite.test_lightweight import light
from tests_rewrite.test_workflow import original


def bind(rig):
    with rig.kernel._connection(write=True, activation=True) as conn:
        result = location.bind(conn, rig.kernel._database)
        conn.commit()
    return result


def test_cold_copy_preserves_reading_but_denies_same_native_holder_and_installer(
    tmp_path,
):
    rig = light(tmp_path)
    bound = bind(rig)
    before = rig.state()
    copied = tmp_path / "cold.sqlite"
    shutil.copyfile(rig.kernel._database, copied)
    checksum = hashlib.sha256(copied.read_bytes()).hexdigest()
    cold = Kernel(copied, rig.contexts.__getitem__)
    view = cold.inspect(Query(rig.wi), "owner")
    assert view["work_item"] == before["work_item"]
    assert view["result"] == before["result"]
    assert (
        next(c for c in view["capabilities"] if c["name"] == "local_read")["state"]
        == "available"
    )
    assert all(
        c["state"] == "unavailable"
        for c in view["capabilities"]
        if c["name"] != "local_read"
    )
    assert view["next_action"]["reason"] == "copied_fact_store_requires_sealed_transfer"
    denied = cold.execute(
        Command(
            "cold-write",
            "checkpoint",
            {"summary": "Must not write", "resources": ["repo"]},
            rig.wi,
        ),
        "owner",
    )
    assert denied["error"]["code"] == "authority_location_changed"
    with pytest.raises(Rejected, match="read-only"):
        with cold._connection(write=True, activation=True) as conn:
            location.bind(conn, copied)
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == checksum
    assert bind(rig) == bound
    original(
        rig.send(
            "checkpoint",
            {"summary": "Original location still valid", "resources": ["repo"]},
        )
    )


def test_install_location_binding_does_not_change_business_rows_and_retry_is_readonly(
    tmp_path,
):
    rig = light(tmp_path)

    def facts():
        with sqlite3.connect(rig.kernel._database) as conn:
            return {
                name: conn.execute("SELECT * FROM " + name).fetchall()
                for name in ("journal", "originals", "journal_heads")
            }

    original_rows = facts()
    bound = bind(rig)
    assert facts() == original_rows
    fixed = rig.kernel._database.read_bytes()
    assert bind(rig) == bound
    assert rig.kernel._database.read_bytes() == fixed
    with sqlite3.connect(rig.kernel._database) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable_store_binding"):
            conn.execute("UPDATE store_binding SET location='foreign'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable_store_binding"):
            conn.execute("DELETE FROM store_binding")


@pytest.mark.parametrize("observation", ["another-real-host", None])
def test_host_change_or_unavailable_native_identity_never_uses_cached_grants(
    tmp_path, monkeypatch, observation
):
    rig = light(tmp_path)
    bind(rig)
    fixed = rig.kernel._database.read_bytes()

    def machine():
        if observation is None:
            raise Rejected("source_unavailable", "Synthetic unavailable machine")
        return observation

    monkeypatch.setattr(location, "machine_identity", machine)
    view = rig.state()
    assert view["status"] == "read_only"
    assert (
        next(c for c in view["capabilities"] if c["name"] == "local_work")["state"]
        == "unavailable"
    )
    result = rig.send(
        "checkpoint", {"summary": "No new authority", "resources": ["repo"]}
    )
    assert result["error"]["code"] == "authority_location_changed"
    assert rig.kernel._database.read_bytes() == fixed


def test_older_sql_writer_cannot_bypass_location_guard(tmp_path):
    rig = light(tmp_path)
    bind(rig)
    with sqlite3.connect(rig.kernel._database) as conn:
        with pytest.raises(sqlite3.OperationalError, match="harness_store_location"):
            conn.execute("INSERT INTO originals VALUES('other','digest',X'00')")
        conn.create_function("harness_store_location", 0, lambda: "another-location")
        with pytest.raises(sqlite3.IntegrityError, match="foreign_fact_store"):
            conn.execute("INSERT INTO originals VALUES('other','digest',X'00')")


def test_interrupted_first_binding_leaves_no_partial_authority_metadata(tmp_path):
    rig = light(tmp_path)
    fixed = rig.kernel._database.read_bytes()

    class Crash(BaseException):
        pass

    with pytest.raises(Crash):
        with rig.kernel._connection(write=True, activation=True) as conn:
            location.bind(conn, rig.kernel._database)
            raise Crash()
    assert rig.kernel._database.read_bytes() == fixed
    bound = bind(rig)
    assert bound["store_id"].startswith("store.")
    original(
        rig.send(
            "checkpoint",
            {"summary": "Retry installation succeeded", "resources": ["repo"]},
        )
    )


@pytest.mark.parametrize("raw", ["", "0" * 32, "not-a-native-machine-identity"])
def test_invalid_linux_machine_identity_fails_closed(monkeypatch, raw):
    location.machine_identity.cache_clear()
    monkeypatch.setattr(location.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "read_text", lambda _: raw)
    with pytest.raises(Rejected, match="cannot be established"):
        location.machine_identity()
    location.machine_identity.cache_clear()
