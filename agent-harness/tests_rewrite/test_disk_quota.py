"""Per-WI disk quota: measured by the worker, enforced from the journal (A-39)."""

from dataclasses import asdict, replace
import os
from pathlib import Path

from harness_rewrite import quota
from harness_rewrite.delivery import WORKER_INTENTS
from harness_rewrite.domain import INTENTS
from harness_rewrite.effect_service import measure_disk
from tests_rewrite.test_workflow import BASE, error, original
from tests_rewrite.test_workspaces import allocate, drain, git, rows, setup  # noqa: F401  (fixture)


class Host:
    """The effect service's view: Host repository bindings and the worker handle."""

    def __init__(self, setup):
        self.policy = {"repositories": [asdict(b) for b in setup[1]],
                       "namespace": BASE.namespace, "surface": BASE.surface}
        self.worker = setup[3]


def measure(setup):
    response = measure_disk(setup[0].kernel, Host(setup), setup[0].wi)
    assert response is not None and response["status"] == "local_committed", response
    return response["result"]["original_request_result"]


def wi_directory(setup, name="one"):
    root = Path(next(b.managed_root for b in setup[1] if b.repo_id == name))
    return root / setup[0].wi


def disk(rig):
    return rig.state()["result"]["current_observation"]["disk_quota"]


def test_quota_blocks_new_work_until_garbage_is_removed(setup, monkeypatch):
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    target = Path(rows(rig)[0]["path"])
    assert measure(setup)["level"] == "ok"
    first = disk(rig)["measured_at"]
    (target / "a/small").write_bytes(b"x")
    unchanged = measure(setup)  # No material change: nothing journaled.
    assert (unchanged["recorded"], unchanged["level"], disk(rig)["measured_at"]) == (False, "ok", first)
    revision = rig.state()["work_item"]["local_revision"]
    measure(setup)  # The same measurement against the same record is a replay.
    assert rig.state()["work_item"]["local_revision"] == revision
    (target / "a/garbage").write_bytes(b"x" * (2 << 20))
    assert measure(setup)["level"] == "exceeded"
    assert disk(rig)["level"] == "exceeded"
    assert rig.state()["next_action"]["reason"] == "disk_quota_exceeded"
    assert error(allocate(setup, ("two",))) == "disk_quota_exceeded"
    (target / "a/garbage").unlink()
    assert measure(setup)["level"] == "ok"
    original(allocate(setup, ("two",)))


def test_exceeded_wi_can_still_record_ask_and_narrow_but_not_grow(setup, monkeypatch):
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    rig = setup[0]
    original(allocate(setup, sparse=["a", "b"]))
    drain(setup)
    resource = rows(rig)[0]
    (Path(resource["path"]) / "a/garbage").write_bytes(b"x" * (2 << 20))
    assert measure(setup)["level"] == "exceeded"
    # Recording where the work stands is never what fills a disk.
    original(rig.send("checkpoint", {"summary": "Over quota; narrowing the checkout", "resources": ["repo"]}))
    # Asking another Agent for help passes the quota gate (this fixture has no
    # collaboration source, so the contract itself is refused for that, not for disk);
    # recording new results stays refused.
    asked = rig.send("offer_collaboration", {
        "kind": "request", "purpose": "Please help free this WI's disk", "required": False,
        "main_only": False, "recipients": [{"actor": "helper", "resources": ["repo"]}]})
    assert error(asked) == "source_unavailable"
    assert error(rig.send("record_work", {"summary": "New result", "resources": ["repo"],
                                          "artifacts": []})) == "disk_quota_exceeded"
    revise = {"resource_id": resource["resource_id"], "resources": ["repo"], "purpose": "Free space"}
    assert error(rig.send("revise_workspace", {**revise, "sparse": ["a", "b", "c"]})) == "disk_quota_exceeded"
    assert error(rig.send("revise_workspace", {**revise, "sparse": []})) == "disk_quota_exceeded"
    assert error(allocate(setup, ("two",))) == "disk_quota_exceeded"
    # The garbage is gone but not yet re-measured: the journal still says exceeded.
    (Path(resource["path"]) / "a/garbage").unlink()
    original(rig.send("revise_workspace", {**revise, "sparse": ["b"]}))
    drain(setup)  # The admitted narrowing runs to the end while still exceeded.
    assert disk(rig)["level"] == "exceeded"
    assert rows(rig)[0]["status"] == "observed_success"
    assert git(Path(resource["path"]), "sparse-checkout", "list").splitlines() == ["/b"]


def test_malformed_workspace_id_is_a_structured_rejection(setup, monkeypatch):
    rig = setup[0]
    original(allocate(setup, sparse=["a", "b"]))
    drain(setup)
    resource = rows(rig)[0]
    for bad in ([], {}, 1):
        request = {"resource_id": bad, "resources": ["repo"], "sparse": ["a"], "purpose": "Invalid id"}
        assert error(rig.send("revise_workspace", request)) in {"invalid_input", "domain_conflict"}
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    (Path(resource["path"]) / "a/garbage").write_bytes(b"x" * (2 << 20))
    assert measure(setup)["level"] == "exceeded"
    for bad in ([], {}, 1):
        request = {"resource_id": bad, "resources": ["repo"], "sparse": ["a"], "purpose": "Invalid id"}
        assert error(rig.send("revise_workspace", request)) == "disk_quota_exceeded"


def test_narrows_compares_with_the_current_sparse_paths():
    state = {"workspaces": {"w": {"sparse": ["a", "b/c"]}, "full": {"sparse": []}}}
    assert quota.narrows(state, {"resource_id": "w", "sparse": ["b/c/d", "a"]})
    assert not quota.narrows(state, {"resource_id": "w", "sparse": ["b"]})
    assert not quota.narrows(state, {"resource_id": "w", "sparse": ["ab"]})
    assert not quota.narrows(state, {"resource_id": "w", "sparse": []})
    assert quota.narrows(state, {"resource_id": "full", "sparse": ["a"]})
    assert not quota.narrows(state, {"resource_id": "missing", "sparse": ["a"]})
    assert not quota.narrows(state, {"resource_id": ["w"], "sparse": ["a"]})
    assert not quota.narrows(state, {"resource_id": {"w": 1}, "sparse": ["a"]})


def test_only_a_human_decision_raises_one_wi_quota(setup, monkeypatch):
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    rig = setup[0]
    original(allocate(setup))
    drain(setup)
    (Path(rows(rig)[0]["path"]) / "a/result-data").write_bytes(b"x" * (2 << 20))
    measure(setup)
    request = {"limit_bytes": 8 << 20, "reason": "Large reviewed dataset"}
    assert error(rig.send("raise_disk_quota", request)) == "source_invalid"
    assert error(rig.approve("raise_disk_quota", {**request, "limit_bytes": 1 << 19},
                             actor=rig.contexts["owner"])) == "invalid_input"
    original(rig.approve("raise_disk_quota", request, actor=rig.contexts["owner"]))
    assert disk(rig)["level"] == "ok" and disk(rig)["raised_by_human"]
    original(allocate(setup, ("two",)))


def test_measurement_needs_captured_evidence_and_unmeasured_never_blocks(setup):
    rig, _, _, worker = setup
    original(allocate(setup))
    assert disk(rig)["level"] == "unmeasured"
    assert error(rig.send("observe_disk_usage", {"bytes": 1, "resources": {}, "evidence": []},
                          handle=worker(()))) == "source_invalid"
    # A human session is never offered worker intents (native_host.bind_context).
    rig.contexts["human"] = replace(rig.contexts["owner"], allowed_intents=INTENTS - WORKER_INTENTS)
    assert error(rig.send("observe_disk_usage", {"bytes": 1, "resources": {}, "evidence": []},
                          handle="human")) == "authority_denied"


def test_unregistered_copy_counts_once_and_names_its_path(setup, monkeypatch):
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    rig, bindings, commits, _ = setup
    original(allocate(setup))
    drain(setup)
    registered = Path(rows(rig)[0]["path"])
    assert measure(setup)["level"] == "ok"
    copy = wi_directory(setup, "two") / "by-hand"
    copy.parent.mkdir()
    git(bindings[1].source, "worktree", "add", "-q", "--detach", str(copy), commits["two"])
    (copy / "a/garbage").write_bytes(b"x" * (2 << 20))
    assert measure(setup)["level"] == "exceeded"
    view = disk(rig)
    assert list(view["unregistered"]) == [str(copy)]
    usage = rig.state()["result"]["current_observation"]
    assert view["bytes"] == view["unregistered"][str(copy)] + quota.allocated_bytes(registered)
    assert "allocate_workspaces" in view["agent_action"]
    assert "unregistered" in rig.state()["next_action"]["agent_action"]
    assert usage["workspace_settlement"]["resources"][0]["path"] == str(registered)
    rejected = allocate(setup, ("two",))
    assert error(rejected) == "disk_quota_exceeded"
    assert "allocate_workspaces (sparse)" in rejected["error"]["reason"]
    git(bindings[1].source, "worktree", "remove", "--force", str(copy))
    assert measure(setup)["level"] == "ok"
    assert disk(rig)["unregistered"] == {} and "agent_action" not in disk(rig)
    original(allocate(setup, ("two",)))


def test_measurement_counts_outside_resources_and_ignores_links(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "elsewhere"
    (root / "wi_1/copy").mkdir(parents=True)
    (root / "wi_1/workspace.a").mkdir()
    outside.mkdir()
    (outside / "data").write_bytes(b"x" * (1 << 20))
    (root / "wi_1/copy/data").write_bytes(b"y" * (1 << 20))
    os.link(outside / "data", root / "wi_1/copy/same-bytes")
    os.symlink(outside, root / "wi_1/link")
    os.symlink(outside, root / "wi_1/copy/inner-link")
    result = quota.measure("wi_1", [str(root), str(root) + "/"],
                           {"workspace.a": str(root / "wi_1/workspace.a"), "workspace.b": str(outside)})
    assert result is not None
    assert result["resources"]["workspace.b"] >= 1 << 20
    assert set(result["unregistered"]) == {str(root / "wi_1/copy"), str(root / "wi_1/link")}
    assert (1 << 20) <= result["unregistered"][str(root / "wi_1/copy")] < 2 << 20
    assert result["bytes"] < 3 << 20
    assert result["bytes"] == sum(result["resources"].values()) + sum(result["unregistered"].values())
    assert quota.measure("..", [str(root)], {}) is None


def test_failed_measurement_never_blocks_work(setup, monkeypatch):
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    rig = setup[0]
    copy = wi_directory(setup) / "unreadable"
    copy.mkdir(parents=True)
    (copy / "big").write_bytes(b"x" * (2 << 20))
    copy.chmod(0)
    try:
        assert measure_disk(rig.kernel, Host(setup), rig.wi) is None
    finally:
        copy.chmod(0o700)
    assert disk(rig)["level"] == "unmeasured"
    original(allocate(setup))

