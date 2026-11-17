"""Real independent Git clients, malformed unrelated facts and uncertain publication."""

import json

from harness_rewrite import Query
from harness_rewrite.model import Rejected
from harness_rewrite.production_evidence import Evidence
from tests_rewrite.test_production_ledger import Rig, WI, RECORD, git


def publish(rig):
    git(rig.b, "add", ".harness")
    git(rig.b, "commit", "-qm", "External writer")
    git(rig.b, "push", "-q", "origin", "main")


def test_only_consumed_paths_validate_file_mode(tmp_path):
    rig = Rig(tmp_path)
    assert rig.approve()["status"] == "local_committed"
    git(rig.b, "pull", "--ff-only")
    link = rig.b / ".harness/evidence/link"
    link.parent.mkdir(exist_ok=True)
    link.symlink_to("../../README.md")
    publish(rig)
    assert rig.kernel.inspect(Query(WI), "native")["status"] == "read_only"
    assert rig.approve(request="with-unrelated-link")["status"] == "local_committed"
    git(rig.b, "pull", "--ff-only")
    target = rig.b / ".harness/core/work-items" / (WI + ".json")
    target.chmod(0o755)
    publish(rig)
    assert rig.kernel.inspect(Query(WI), "native")["status"] == "read_only"
    # A regular JSON blob's executable bit is not its content identity. A
    # redirect at the consumed fact path still cannot supply authoritative data.
    target.unlink()
    target.symlink_to("../../evidence/link")
    publish(rig)
    assert rig.kernel.inspect(Query(WI), "native")["error"]["code"] == "ledger_invalid"


def test_bad_legacy_is_reported_without_poisoning_binding_discovery(tmp_path):
    rig = Rig(tmp_path)
    assert rig.approve()["status"] == "local_committed"
    git(rig.b, "pull", "--ff-only")
    other = "wi_20260823_184"
    (rig.b / f".harness/work-items/{other}.yaml").write_text(
        json.dumps({**RECORD, "work_item_id": other, "status": "unrecognized"})
    )
    publish(rig)
    listed = rig.kernel.inspect(Query(view="list"), "native")
    assert listed["status"] == "read_only"
    assert [r["id"] for r in listed["result"]["items"]] == [WI]
    assert listed["result"]["coverage"] == "partial"
    assert listed["result"]["unavailable_items"][0]["wi"] == other
    assert rig.kernel.inspect(Query(view="bindings"), "native")["status"] == "read_only"
    assert rig.approve(request="still-writable")["status"] == "local_committed"


def test_original_readback_never_republishes_or_reads_private_custody(tmp_path):
    rig = Rig(tmp_path)
    first = rig.approve()
    rig.ledger.evidence = Evidence(tmp_path / "missing-parent" / "private")
    rig.ledger.fault = lambda stage: (_ for _ in ()).throw(AssertionError("No publication"))
    assert (
        rig.approve()["result"]["original_request_result"]
        == first["result"]["original_request_result"]
    )


def test_missing_custody_has_recoverable_storage_action_not_affiliation(tmp_path):
    rig = Rig(tmp_path)
    rig.ledger.evidence = Evidence(tmp_path / "missing-parent" / "private")
    failed = rig.approve()
    assert failed["error"]["code"] == "evidence_write_unavailable"
    assert failed["error"]["retryable"] is True
    assert failed["next_action"]["intent"] == "repair_private_custody_and_retry_original_request"
    assert git(rig.remote, "rev-parse", "main") == rig.before
    rig.ledger.evidence.root.parent.mkdir()
    assert rig.approve()["status"] == "local_committed"


def test_committed_but_unobservable_is_unknown_then_same_request_recovers(tmp_path):
    rig = Rig(tmp_path)
    original = rig.ledger.git
    armed = False

    def transport(*args, **kwargs):
        nonlocal armed
        if args[0] == "push":
            original(*args, **kwargs)
            armed = True
            raise Rejected("ledger_unavailable", "reply lost")
        if args[0] == "fetch" and armed:
            raise Rejected("ledger_unavailable", "offline")
        return original(*args, **kwargs)

    rig.ledger.git = transport
    result = rig.approve()
    assert result["status"] == "unknown"
    assert result["next_action"]["request_id"] == "affiliation"
    revision = git(rig.remote, "rev-parse", "main")
    rig.ledger.git = original
    assert rig.approve()["status"] == "local_committed"
    assert git(rig.remote, "rev-parse", "main") == revision


def test_owner_mode_does_not_hide_extended_acl_on_macos(tmp_path):
    import hashlib
    import subprocess
    import sys
    import pytest

    if sys.platform != "darwin":
        pytest.skip("macOS extended ACL contract")
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(root)], check=True)
    try:
        assert root.stat().st_mode & 0o777 == 0o700
        content = b"private fixture"
        with pytest.raises(Rejected) as failure:
            Evidence(root).put(hashlib.sha256(content).hexdigest(), content)
        assert failure.value.code == "evidence_write_unavailable"
        assert not list(root.iterdir())
    finally:
        subprocess.run(["/bin/chmod", "-N", str(root)], check=True)


def test_native_derived_projection_cannot_veto_original_fact_or_next_decision(tmp_path):
    rig = Rig(tmp_path)
    first = rig.approve()
    git(rig.b, "pull", "--ff-only")
    path = rig.b / f".harness/work-items/{WI}.yaml"
    path.unlink()
    path.symlink_to("../../private-nonexistent")
    publish(rig)
    assert (
        rig.approve()["result"]["original_request_result"]
        == first["result"]["original_request_result"]
    )
    assert rig.kernel.inspect(Query(view="bindings"), "native")["status"] == "read_only"
    assert rig.approve(request="repair-derived-output")["status"] == "local_committed"
    assert git(rig.remote, "ls-tree", "main", "--", f".harness/work-items/{WI}.yaml").startswith(
        "100644 blob"
    )
