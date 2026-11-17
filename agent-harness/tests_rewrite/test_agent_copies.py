"""Copies Agents make outside the managed roots count against a quota (AH-086)."""

from dataclasses import asdict, replace
import itertools
import os
from pathlib import Path
import subprocess

from harness_rewrite import agent_copies, quota
from harness_rewrite.effect_service import measure_disk
from tests_rewrite.test_disk_quota import Host, disk
from harness_rewrite.delivery import WORKER_INTENTS
from harness_rewrite.workspaces import EFFECT_KINDS
from tests_rewrite.test_workflow import BASE, error, original
from tests_rewrite.test_workspaces import allocate, drain, git, setup  # noqa: F401  (fixture)

BIG = 2 << 20


def agent_host(setup, tmp_path):
    """The rig's holder ("producer") and another Agent, each with an installed workspace."""
    host = Host(setup)
    roots = {name: tmp_path / (name + "-space") for name in ("producer", "other")}
    for root in roots.values():
        root.mkdir()
    host.policy["profiles"] = [
        {"role": "producer", "actor": name, "session_root": str(tmp_path),
         "artifact_roots": [str(root)], "workspace_roots": [str(root)]}
        for name, root in roots.items()
    ]
    return host, roots


def clone(source, target, *flags):
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", *flags, str(source), str(target)], check=True)
    (target / "garbage").write_bytes(b"x" * BIG)


def measured(setup, host):
    response = measure_disk(setup[0].kernel, host, setup[0].wi)
    assert response is not None and response["status"] == "local_committed", response
    return response["result"]["original_request_result"]


def test_discovery_finds_worktrees_and_clones_but_not_links_depth_or_kernel_copies(setup, tmp_path):
    rig, bindings, commits, _ = setup
    host, roots = agent_host(setup, tmp_path)
    source = bindings[0].source
    git(source, "worktree", "add", "-q", "-b", "producer/scratch", str(roots["producer"] / "tmp/wt"),
        commits["one"])
    clone(source, roots["producer"] / "repos/shared", "--shared")
    git(source, "worktree", "add", "-q", "-b", "other/elsewhere", str(tmp_path / "loose"), commits["one"])
    inner = roots["producer"] / "repos/shared/inner"
    git(source, "worktree", "add", "-q", "-b", "producer/inner", str(inner), commits["one"])
    (inner / "inner-garbage").write_bytes(b"x" * BIG)
    git(source, "worktree", "add", "-q", "-b", "harness/" + rig.wi + "/x",
        str(tmp_path / "kernel-made"), commits["one"])
    deep = roots["other"].joinpath(*["d"] * agent_copies.MAX_DEPTH, "too-deep")
    clone(source, deep)
    os.symlink(roots["producer"] / "repos", roots["other"] / "linked")
    copies = {c["path"]: c for c in agent_copies.discover(host.policy, frozenset({rig.wi}))}
    assert set(copies) == {str(roots["producer"] / "tmp/wt"), str(roots["producer"] / "repos/shared"),
                           str(tmp_path / "loose"), str(inner)}
    assert copies[str(roots["producer"] / "tmp/wt")]["branch"] == "producer/scratch"
    assert copies[str(tmp_path / "loose")]["agent"] == "other"  # by branch prefix
    assert all(c["agent"] in {"producer", "other"} and c["wi"] is None for c in copies.values())
    shared = copies[str(roots["producer"] / "repos/shared")]["bytes"]
    assert BIG <= shared < 2 * BIG and copies[str(inner)]["bytes"] >= BIG  # each byte once
    summary = agent_copies.summary(list(copies.values()))
    assert summary["producer"]["copies"] == 3 and summary["other"]["copies"] == 1


def test_attribution_names_one_existing_wi_or_none():
    known = frozenset({"wi_20261015_422", "wi_20261015_938", "wi_20261106_938", "wi_20261104_103"})
    assert agent_copies.owner_wi(["marsh/wi103-skill"], known) == "wi_20261104_103"
    assert agent_copies.owner_wi(["", "/tmp/wi_20261015_422-copy"], known) == "wi_20261015_422"
    assert agent_copies.owner_wi(["hestia/wi938-rebuild"], known) is None  # two WIs end in _938
    assert agent_copies.owner_wi(["wi422-a", "/x/wi103"], known) is None  # names two WIs
    assert agent_copies.owner_wi(["wi4220", "kiwi422"], known) is None


def test_unattributed_copies_block_the_holder_until_removed(setup, tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "AGENT_COPIES_ENFORCED", True)
    monkeypatch.setattr(quota, "AGENT_LIMIT", 1 << 20)
    rig, bindings, _, _ = setup
    host, roots = agent_host(setup, tmp_path)
    original(allocate(setup))
    drain(setup)
    assert measured(setup, host)["level"] == "ok"
    assert disk(rig)["agent"] == "producer" and disk(rig)["agent_copies"] is None
    clone(bindings[0].source, roots["other"] / "theirs")  # Another Agent's copy: not ours.
    assert measured(setup, host)["recorded"] is False
    copy = roots["producer"] / "tmp/clone"
    clone(bindings[0].source, copy)
    assert measured(setup, host)["level"] == "exceeded"
    view = disk(rig)
    assert view["agent_copies"]["actor"] == "producer" and list(view["agent_copies"]["copies"]) == [str(copy)]
    assert view["agent_copies"]["level"] == "exceeded" and view["unregistered"] == view["named_copies"] == {}
    assert "agent_copies" in view["agent_action"]
    assert rig.state()["next_action"]["reason"] == "disk_quota_exceeded"
    assert error(allocate(setup, ("two",))) == "disk_quota_exceeded"
    subprocess.run(["rm", "-rf", str(copy)], check=True)
    assert measured(setup, host)["level"] == "ok"
    assert disk(rig)["agent_copies"] is None
    original(allocate(setup, ("two",)))


def test_a_copy_naming_the_wi_counts_in_its_quota(setup, tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "AGENT_COPIES_ENFORCED", True)
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    rig, bindings, _, _ = setup
    host, roots = agent_host(setup, tmp_path)
    copy = roots["other"] / ("tmp/" + rig.wi + "-fix")
    clone(bindings[0].source, copy)
    assert measured(setup, host)["level"] == "exceeded"
    view = disk(rig)
    assert list(view["named_copies"]) == [str(copy)] and view["agent_copies"] is None
    assert view["unregistered"] == {} and view["bytes"] == 0
    assert error(allocate(setup)) == "disk_quota_exceeded"


def test_agent_copies_are_observed_without_blocking_by_default(setup, tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "AGENT_LIMIT", 1 << 20)
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    rig, bindings, _, _ = setup
    host, roots = agent_host(setup, tmp_path)
    original(allocate(setup))
    drain(setup)
    clone(bindings[0].source, roots["producer"] / "stray")
    clone(bindings[0].source, roots["other"] / (rig.wi + "-copy"))
    assert measured(setup, host)["level"] == "ok"
    view = disk(rig)
    assert view["agent_copies_enforced"] is False and view["level_with_agent_copies"] == "exceeded"
    assert view["agent_copies"]["level"] == "exceeded" and view["named_copies"]
    assert view["hosts"]["local"]["level"] == "ok"
    original(allocate(setup, ("two",)))


def test_registered_workspaces_are_never_agent_copies(setup, tmp_path):
    rig, bindings, commits, _ = setup
    host, roots = agent_host(setup, tmp_path)
    original(allocate(setup))
    drain(setup)
    allocated = rig.kernel.registered_workspaces("team")
    assert allocated["coverage"] == "complete" and len(allocated["paths"]) == 1
    adopted = roots["producer"] / "adopted"
    git(bindings[0].source, "worktree", "add", "-q", "-b", "producer/adopted", str(adopted), commits["one"])
    clone(bindings[0].source, adopted / "inner")
    assert {c["path"] for c in agent_copies.discover(host.policy, frozenset())} == {str(adopted)}
    assert agent_copies.discover(host.policy, frozenset(), frozenset({str(adopted)})) == []


def test_failed_discovery_or_unreadable_copy_never_blocks(setup, tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "AGENT_LIMIT", 1 << 20)
    rig, bindings, _, _ = setup
    host, roots = agent_host(setup, tmp_path)
    copy = roots["producer"] / "clone"
    clone(bindings[0].source, copy)

    def broken(common_dir):
        raise subprocess.TimeoutExpired("git", 1)

    with monkeypatch.context() as patch:
        patch.setattr(agent_copies, "worktrees", broken)
        assert measure_disk(rig.kernel, host, rig.wi) is None
    with monkeypatch.context() as patch:
        patch.setattr(agent_copies, "MAX_DIRECTORIES", 0)
        assert measure_disk(rig.kernel, host, rig.wi) is None
    (copy / "sealed").mkdir()
    (copy / "sealed").chmod(0)
    try:
        assert measure_disk(rig.kernel, host, rig.wi) is None
    finally:
        (copy / "sealed").chmod(0o700)
    assert disk(rig)["level"] == "unmeasured"
    original(allocate(setup))


def test_observation_rejects_malformed_agent_copies(setup):
    rig, _, _, worker = setup
    bad = {"actor": "producer", "bytes": -1, "copies": {}}
    request = {"bytes": 0, "resources": {}, "evidence": ["x"], "agent_copies": bad}
    assert error(rig.send("observe_disk_usage", request, handle=worker(()))) == "invalid_input"


def test_a_scanned_clone_is_attributed_by_its_checked_out_branch(setup, tmp_path):
    rig, bindings, _, _ = setup
    host, roots = agent_host(setup, tmp_path)
    scratch = roots["producer"] / "scratch"
    clone(bindings[0].source, scratch)
    git(scratch, "checkout", "-q", "-b", "producer/" + rig.wi + "-copy")
    worktree = roots["producer"] / "linked"
    git(scratch, "worktree", "add", "-q", "-b", "producer/" + rig.wi + "-linked", str(worktree))
    rows = {c["path"]: c for c in agent_copies.discover(host.policy, frozenset({rig.wi}))}
    assert rows[str(scratch)]["branch"] == "producer/" + rig.wi + "-copy"
    assert rows[str(worktree)]["branch"] == "producer/" + rig.wi + "-linked"
    assert rows[str(scratch)]["wi"] == rows[str(worktree)]["wi"] == rig.wi


def test_an_unreadable_directory_in_a_workspace_voids_discovery(setup, tmp_path):
    rig, bindings, _, _ = setup
    host, roots = agent_host(setup, tmp_path)
    hidden = roots["producer"] / "hidden"
    clone(bindings[0].source, hidden / "copy")
    hidden.chmod(0)
    try:
        assert agent_copies.discover(host.policy, frozenset()) is None
        assert measure_disk(rig.kernel, host, rig.wi) is None
        roots["producer"].chmod(0)
        try:
            assert agent_copies.discover(host.policy, frozenset()) is None
        finally:
            roots["producer"].chmod(0o700)
    finally:
        hidden.chmod(0o700)
    assert disk(rig)["level"] == "unmeasured"


def test_kernel_copies_are_excluded_whichever_source_finds_them(setup, tmp_path):
    rig, bindings, commits, _ = setup
    host, roots = agent_host(setup, tmp_path)
    source = bindings[0].source
    kernel_made = roots["producer"] / "kernel-made"  # found by both the list and the scan
    git(source, "worktree", "add", "-q", "-b", "harness/" + rig.wi + "/x", str(kernel_made), commits["one"])
    switched = roots["producer"] / "switched"  # a clone on a kernel branch: scan only
    clone(source, switched)
    git(switched, "checkout", "-q", "-b", "harness/" + rig.wi + "/y")
    registered = roots["producer"] / "registered"  # a registered path: both sources
    git(source, "worktree", "add", "-q", "-b", "producer/registered", str(registered), commits["one"])
    checkout = roots["producer"] / "checkout"  # the Host's source checkout inside a workspace
    clone(source, checkout)
    host.policy["source_consumers"] = [{"path": str(checkout)}]
    assert agent_copies.discover(host.policy, frozenset({rig.wi}), frozenset({str(registered)})) == []


class SiteHost:
    """Another Host's effect service: its own surface and its own disk for the managed roots."""

    def __init__(self, setup, surface, disk=None):
        rig, bindings = setup[0], setup[1]
        self.rig, self.surface = rig, surface
        # Each measurement happens at its own time, as on a real Host.
        ticks = itertools.count()
        rig.kernel._clock = lambda: "2026-11-08T00:00:00.{:06d}Z".format(next(ticks))
        self.policy = {
            "repositories": [{**asdict(b), "managed_root": str(Path(disk) / b.repo_id) if disk
                              else b.managed_root} for b in bindings],
            "namespace": BASE.namespace, "surface": surface,
        }

    def worker(self, artifacts):
        handle = "worker-" + self.surface
        self.rig.contexts[handle] = replace(
            BASE, actor="resource-worker", session="service-" + self.surface, surface=self.surface,
            allowed_intents=WORKER_INTENTS, effect_kinds=EFFECT_KINDS, artifacts=artifacts)
        return handle

    def junk(self, size):
        """Set this Host's unregistered bytes under the WI directory on its disk."""
        directory = Path(self.policy["repositories"][0]["managed_root"]) / self.rig.wi
        directory.mkdir(parents=True, exist_ok=True)
        if size:
            (directory / "junk").write_bytes(b"x" * size)
        else:
            (directory / "junk").unlink(missing_ok=True)

    def measure(self):
        response = measure_disk(self.rig.kernel, self, self.rig.wi)
        assert response is not None and response["status"] == "local_committed", response
        return response["result"]["original_request_result"]


def test_two_hosts_with_identical_first_measurements_each_get_a_record(setup):
    mac, cloud = SiteHost(setup, "mac"), SiteHost(setup, "cloud")
    mac.junk(4096)  # Both Hosts read the same paths and bytes.
    assert mac.measure()["recorded"] and cloud.measure()["recorded"]
    assert set(disk(setup[0])["hosts"]) == {"mac", "cloud"}


def test_each_host_replaces_only_its_own_record(setup, tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    rig = setup[0]
    mac, cloud = SiteHost(setup, "mac"), SiteHost(setup, "cloud", tmp_path / "cloud-disk")
    mac.junk(2 << 20)
    cloud.junk(4096)
    assert mac.measure()["level"] == "exceeded"
    # The other Host has none of these bytes: its record is separate and lifts nothing.
    assert cloud.measure()["recorded"] and disk(rig)["level"] == "exceeded"
    recorded = disk(rig)["hosts"]
    for _ in range(3):  # Alternating unchanged measurements: no flip, no new record.
        assert mac.measure()["recorded"] is False and cloud.measure()["recorded"] is False
        assert disk(rig)["level"] == "exceeded" and disk(rig)["hosts"] == recorded
        revision = rig.state()["work_item"]["local_revision"]
    mac.measure(), cloud.measure()  # The same request again is a replay: nothing journaled.
    assert rig.state()["work_item"]["local_revision"] == revision
    cloud.junk(2 << 20)
    assert cloud.measure()["recorded"]
    mac.junk(0)
    assert mac.measure()["recorded"]  # Mac cleaned up: only its record clears.
    assert disk(rig)["level"] == "exceeded" and disk(rig)["host"] == "cloud"
    assert error(allocate(setup)) == "disk_quota_exceeded"
    cloud.junk(0)
    cloud.measure()
    assert disk(rig)["level"] == "ok" and set(disk(rig)["hosts"]) == {"mac", "cloud"}
    original(allocate(setup))


def test_a_hosts_own_threshold_crossing_is_recorded_while_another_host_masks_it(
        setup, tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    rig, kib = setup[0], 1 << 10
    mac, cloud = SiteHost(setup, "mac"), SiteHost(setup, "cloud", tmp_path / "cloud-disk")
    mac.junk(1000 * kib)  # warning: under 1 MiB
    mac.measure()
    cloud.junk(2048 * kib)
    cloud.measure()
    mac.junk(1060 * kib)  # over 1 MiB, far below a material size change
    assert mac.measure()["recorded"]
    cloud.junk(0)
    cloud.measure()
    assert disk(rig)["level"] == "exceeded"  # Mac is still over its quota.
    cloud.junk(2048 * kib)
    cloud.measure()
    mac.junk(1000 * kib)
    assert mac.measure()["recorded"]  # and back under it, also recorded
    cloud.junk(0)
    cloud.measure()
    assert disk(rig)["level"] == "warning"


def test_an_older_programs_record_cannot_lift_a_host_block():
    """Replay of a journal that mixes older (Host-less) and Host-keyed measurements."""
    def replay(state, size, host=None):
        payload = {"bytes": size, "resources": {}, "unregistered": {}, "measured_at": "x",
                   "evidence": [], **({"host": host} if host else {})}
        assert quota.apply_event(state, "DiskUsageObserved", payload)

    state = {"status": "active", "holder": None}
    replay(state, 2 << 30, "mac")
    replay(state, 0)  # An older program updates only the shared legacy record.
    assert quota.level(state) == "exceeded" and quota.LEGACY_HOST in quota.records(state)
    replay(state, 0, "cloud")  # A Host-keyed measurement retires the legacy record.
    assert quota.LEGACY_HOST not in quota.records(state) and quota.level(state) == "exceeded"
    stale = {"status": "active", "holder": None}
    replay(stale, 3 << 30)  # A legacy record over quota ...
    assert quota.level(stale) == "exceeded"
    replay(stale, 0, "mac")  # ... does not outlive the first Host-keyed measurement.
    assert quota.level(stale) == "ok"


def test_a_registered_workspace_inside_a_copy_is_not_counted_in_the_copy(setup, tmp_path):
    rig, bindings, _, _ = setup
    host, roots = agent_host(setup, tmp_path)
    outer = roots["producer"] / "outer"
    clone(bindings[0].source, outer)
    nested = outer / "nested"
    nested.mkdir()
    (nested / "data").write_bytes(b"x" * BIG)
    whole = agent_copies.discover(host.policy, frozenset())
    trimmed = agent_copies.discover(host.policy, frozenset(), frozenset({str(nested)}))
    assert [c["path"] for c in trimmed] == [str(outer)]
    assert trimmed[0]["bytes"] <= whole[0]["bytes"] - BIG


def test_an_emptied_wi_replaces_an_older_programs_record(setup, monkeypatch):
    monkeypatch.setattr(quota, "DEFAULT_LIMIT", 1 << 20)
    rig = setup[0]
    mac = SiteHost(setup, "mac")
    real = quota.event

    def older(kind, **payload):  # What a program without Host records wrote.
        payload.pop("host", None)
        return real(kind, **payload)

    mac.junk(2 << 20)
    with monkeypatch.context() as patch:
        patch.setattr(quota, "event", older)
        mac.measure()
    assert disk(rig)["level"] == "exceeded" and set(disk(rig)["hosts"]) == {quota.LEGACY_HOST}
    mac.junk(0)
    (Path(mac.policy["repositories"][0]["managed_root"]) / rig.wi).rmdir()  # Garbage all gone.
    assert mac.measure()["recorded"]
    assert disk(rig)["level"] == "ok" and set(disk(rig)["hosts"]) == {"mac"}
    original(allocate(setup))
