"""Read speed never changes what a read returns.

Replay applies each decision once to a state it owns; one command's reads share
one observed revision; a process reuses a journal's verified decisions while
their Git objects are unchanged; ordered blob reads are pipelined. Each result is
compared with the plain computation it replaces.
"""

import hashlib
import json
import pickle
import subprocess
import threading

import pytest

from harness_rewrite import Kernel, Query
from harness_rewrite.domain import reduce
from harness_rewrite.model import Rejected, encoded
from harness_rewrite.production_ledger import ProductionLedger
from harness_rewrite.production_segments import entry_path
from tests_rewrite.test_production_ledger import Rig, WI, git, workflow_rig
from tests_rewrite.test_workflow import BASE
from tests_rewrite.test_native_host import native as native  # noqa: F401 - fixture


def journey(tmp_path):
    """A standard WI through verification and completion: many event kinds."""
    from tests_rewrite.test_review import produce, assign, verify, approve, VERIFIER
    from tests_rewrite.test_workflow import original

    authority = Rig(tmp_path)
    rig = workflow_rig(authority, "draft")
    rig.contexts["verifier"] = VERIFIER
    candidate = produce(rig)
    assignment = assign(rig, candidate)
    original(verify(rig, candidate, assignment))
    original(approve(rig, candidate))
    completed = rig.send("complete", {"candidate_id": candidate}, request="complete-standard")
    assert completed["work_item"]["business_progress"] == "completed", completed
    return authority, rig


def copying_replay(conn, wi, at=None):
    """The former replay: a full copy of the state for every decision."""
    rows = conn.execute(
        "SELECT content FROM journal WHERE namespace=? AND wi=? ORDER BY revision",
        (BASE.namespace, wi),
    ).fetchall()
    history = [json.loads(row[0]) for row in rows]
    state, selected = None, None
    for content in history:
        state = reduce(state, content["events"])
        if at == content["revision"]:
            selected = state
    return (selected, history[:at]) if at is not None else (state, history)


def test_in_place_replay_equals_copying_replay_at_every_revision(tmp_path):
    authority, rig = journey(tmp_path)
    with authority.ledger.connection(write=False, work_item_id=rig.wi) as conn:
        state, revision, _, history = Kernel._replay(conn, BASE.namespace, rig.wi)
        assert revision > 5
        expected_state, expected_history = copying_replay(conn, rig.wi)
        assert encoded(state) == encoded(expected_state)
        assert encoded(history) == encoded(expected_history)
        for at in range(1, revision + 1):
            selected, _, _, prefix = Kernel._replay(conn, BASE.namespace, rig.wi, at)
            expected_selected, expected_prefix = copying_replay(conn, rig.wi, at)
            assert encoded(selected) == encoded(expected_selected)
            assert encoded(prefix) == encoded(expected_prefix)


def test_replay_shares_no_object_between_history_and_earlier_states(tmp_path):
    authority, rig = journey(tmp_path)
    with authority.ledger.connection(write=False, work_item_id=rig.wi) as conn:
        rows = [json.loads(row[0]) for row in conn.execute(
            "SELECT content FROM journal WHERE wi=? ORDER BY revision", (rig.wi,))]
        selected, _, _, prefix = Kernel._replay(conn, BASE.namespace, rig.wi, 2)
        frozen = pickle.dumps(selected)
        state, _, _, history = Kernel._replay(conn, BASE.namespace, rig.wi)
    # Later decisions neither changed the selected state nor any decision.
    assert pickle.dumps(selected) == frozen
    assert history == rows and prefix == rows[:2]
    # As with a copying replay, only the last decision's payloads may be held.
    held = {id(value) for value in _objects(state)}
    assert not held & {id(value) for entry in history[:-1] for value in _objects(entry)}


def _objects(value):
    if isinstance(value, dict | list):
        yield value
        for item in value.values() if isinstance(value, dict) else value:
            yield from _objects(item)


def test_reused_decisions_equal_a_fresh_ledger_and_skip_segment_reads(tmp_path, monkeypatch):
    authority, rig = journey(tmp_path)
    ledger = authority.backend(authority.a)
    kernel = Kernel(None, rig.contexts.__getitem__, storage=ledger)
    first = kernel.inspect(Query(rig.wi), "owner")
    read: list[str] = []
    original_blobs = ProductionLedger.blobs

    def counted(self, mapping):
        read.extend(mapping)
        return original_blobs(self, mapping)

    monkeypatch.setattr(ProductionLedger, "blobs", counted)
    again = kernel.inspect(Query(rig.wi), "owner")
    assert not [path for path in read if "/journals/" in path]
    fresh = Kernel(None, rig.contexts.__getitem__, storage=authority.backend(authority.a))
    assert encoded(again) == encoded(first) == encoded(fresh.inspect(Query(rig.wi), "owner"))
    with ledger.connection(write=False, work_item_id=rig.wi):
        assert {entry_path(rig.wi, 1)} <= ledger.dependencies


def test_new_decision_or_changed_segment_is_never_served_from_reuse(tmp_path):
    rig = Rig(tmp_path)
    assert rig.approve()["status"] == "local_committed"
    before = rig.kernel.inspect(Query(WI), "native")
    assert rig.ledger.decisions[WI]
    # Another client records a decision: the head and its segments change.
    other = rig.client(rig.backend(rig.b))
    assert rig.approve(other, request="elsewhere")["status"] == "local_committed"
    after = rig.kernel.inspect(Query(WI), "native")
    assert after["work_item"]["local_revision"] == before["work_item"]["local_revision"] + 1
    fresh = rig.client(rig.backend(rig.a)).inspect(Query(WI), "native")
    assert encoded(after) == encoded(fresh)
    # A rewritten segment has another Git object ID; its corruption is detected,
    # and the failure is not reused as a result either.
    git(rig.b, "pull", "--ff-only")
    segment = rig.b / entry_path(WI, 1)
    text = segment.read_text()
    assert '"revision":1,' in text
    segment.write_text(text.replace('"revision":1,', '"revision":2,', 1))
    git(rig.b, "commit", "-am", "Corrupt an immutable decision")
    git(rig.b, "push", "origin", "main")
    for _ in range(2):
        result = rig.kernel.inspect(Query(WI), "native")
        assert result["status"] == "rejected", result
        assert result["error"]["code"] in {"ledger_invalid", "journal_invalid"}, result


@pytest.mark.parametrize("change", ["delete", "replace"])
def test_changed_chunk_is_rejected_by_reuse_fresh_reads_and_writes_alike(
    tmp_path, monkeypatch, change
):
    from harness_rewrite import production_segments

    monkeypatch.setattr(production_segments, "CHUNK", 1024)  # Decisions are chunked.
    rig = Rig(tmp_path)
    assert rig.approve()["status"] == "local_committed"
    assert rig.kernel.inspect(Query(WI), "native")["status"] == "read_only"
    git(rig.b, "pull", "--ff-only")
    wrapper = json.loads((rig.b / entry_path(WI, 1)).read_text())
    assert "entry" in wrapper, "the fixture decision must be stored as chunks"
    chunk = rig.b / production_segments.object_path(wrapper["entry"]["chunks"][0])
    if change == "delete":
        git(rig.b, "rm", "-q", str(chunk))
    else:
        chunk.write_bytes(b"x" * len(chunk.read_bytes()))
        git(rig.b, "add", str(chunk))
    git(rig.b, "commit", "-m", "Change one chunk; head and segments stay")
    git(rig.b, "push", "origin", "main")
    reused = rig.kernel.inspect(Query(WI), "native")
    fresh = rig.client(rig.backend(rig.a)).inspect(Query(WI), "native")
    assert reused["status"] == fresh["status"] == "rejected", reused
    assert reused["error"] == fresh["error"]
    expected = ("journal_invalid",) if change == "delete" else ("ledger_invalid",)
    assert fresh["error"]["code"] == expected[0], fresh
    written = rig.approve(request="after-change")
    assert written["status"] == "rejected" and written["error"]["code"] == expected[0], written


def test_one_command_reads_one_revision_until_its_first_write(tmp_path):
    rig = Rig(tmp_path)
    other = rig.client(rig.backend(rig.b))
    fetched: list[str] = []
    refresh = rig.ledger.refresh

    def counted():
        fetched.append("fetch")
        return refresh()

    rig.ledger.refresh = counted
    with rig.ledger.read_snapshot():
        first = rig.kernel.inspect(Query(WI), "native")
        assert rig.approve(other, request="elsewhere")["status"] == "local_committed"
        # The snapshot is one observation: no second fetch, the same revision.
        assert encoded(rig.kernel.inspect(Query(WI), "native")) == encoded(first)
        assert fetched == ["fetch"]
        # A write observes the ref again and ends the snapshot.
        assert rig.approve(request="here")["status"] == "local_committed"
        assert len(fetched) > 1 and not rig.ledger.snapshot_open
        observed = len(fetched)
        rig.kernel.inspect(Query(WI), "native")
        assert len(fetched) == observed + 1
    rig.kernel.inspect(Query(WI), "native")
    rig.kernel.inspect(Query(WI), "native")
    assert len(fetched) == observed + 3  # Outside a snapshot every read observes the ref.
    revision = rig.kernel.inspect(Query(WI), "native")["work_item"]["local_revision"]
    assert revision > first["work_item"]["local_revision"]


def test_cli_inspect_observes_the_ref_once(native, tmp_path, monkeypatch, capsys):
    from harness_rewrite import cli
    from harness_rewrite.native_host import NativeHost

    authority_root = tmp_path / "authority-fixture"
    authority_root.mkdir()
    authority = Rig(authority_root)
    file, proc, _, _ = native
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
    release = tmp_path / "installed/releases/fixture"
    release.mkdir(parents=True)
    entry = release / "harness.pyz"
    entry.write_bytes(b"synthetic program identity")
    (release / "installation.json").write_text(json.dumps({
        "bundle_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
        "policy_sha256": hashlib.sha256(encoded(policy)).hexdigest(),
    }))
    (tmp_path / "installed/active").symlink_to("releases/fixture")
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])
    fetched: list[str] = []
    refresh = ProductionLedger.refresh

    def counted(self):
        fetched.append("fetch")
        return refresh(self)

    monkeypatch.setattr(ProductionLedger, "refresh", counted)
    cli.main(["inspect", "--wi", WI])  # Four reads: generation, current, view build.
    result = json.loads(capsys.readouterr().out)
    assert result["work_item"]["id"] == WI, result
    assert fetched == ["fetch"]


def test_ordered_reads_are_pipelined_without_blocking_on_full_pipes(tmp_path):
    from harness_rewrite.git_objects import object_reader

    rig = Rig(tmp_path)
    expected = {f".harness/fixture/{index}": (f"object {index} " * (1 + index % 50)).encode()
                for index in range(3000)}  # Requests and responses each exceed a pipe buffer.
    mapping = dict(zip(expected, git_hash_many(rig.a, tmp_path / "bodies", list(expected.values()))))
    done: list[dict[str, bytes]] = []
    worker = threading.Thread(target=lambda: done.append(rig.ledger.blobs(mapping)))
    worker.start()
    worker.join(60)
    assert done and done[0] == expected
    with pytest.raises(Rejected, match="out of order"):
        with object_reader(rig.a, mapping, ordered=True) as read:
            read(list(mapping)[1])
    with pytest.raises(Rejected, match="left unread"):
        with object_reader(rig.a, mapping, ordered=True) as read:
            read(next(iter(mapping)))


def git_hash_many(path, scratch, bodies):
    """Write many blobs with one git process."""
    scratch.mkdir()
    files = []
    for index, body in enumerate(bodies):
        files.append(scratch / str(index))
        files[-1].write_bytes(body)
    names = "".join(f"{file}\n" for file in files).encode()
    out = subprocess.run(["git", "-C", str(path), "hash-object", "-w", "--stdin-paths"],
                         input=names, capture_output=True, check=True).stdout.decode().split()
    assert len(out) == len(bodies)
    return out
