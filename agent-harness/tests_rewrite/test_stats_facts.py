"""Stats reads WorkItem facts from the journal, never lifecycle authority."""

from types import SimpleNamespace

from harness_rewrite import stats
from tests_rewrite.test_relay_session import rig as rig, process  # noqa: F401
from tests_rewrite.test_native_host import native as native  # noqa: F401
from tests_rewrite.test_session_access import human
from harness_rewrite import Command
from harness_rewrite.model import Rejected
import pytest


def test_facts_report_each_work_item_from_its_journal_without_writing(rig):
    _, _, kernel, original, wi, _ = rig
    storage = SimpleNamespace(connection=kernel._connection)
    namespace = original.policy["namespace"]
    before = kernel._database.read_bytes()
    result = stats.facts(storage, namespace, "2026-10-16T00:00:00+00:00")
    assert kernel._database.read_bytes() == before
    assert result["schema"] == "harness.stats_facts/v1" and result["read_only"] is True
    assert result["coverage"] == "complete" and result["unavailable_items"] == []
    assert result["collaborations"] == []
    [item] = [row for row in result["items"] if row["id"] == wi]
    assert item["status"] == "active" and item["closed_at"] is None
    assert item["created_at"] <= item["updated_at"]
    assert item["holder"] == original.base.actor
    assert {"title", "owner", "profile", "business_progress", "local_revision"} <= item.keys()


def test_closed_at_is_the_closing_event_not_a_later_record(monkeypatch):
    history = [
        {"at": "2026-10-01T00:00:00+00:00", "events": [{"kind": "WorkItemCreated"}]},
        {"at": "2026-10-02T00:00:00+00:00", "events": [{"kind": "WorkCompleted"}]},
        {"at": "2026-10-03T00:00:00+00:00", "events": [{"kind": "NoticeRecorded"}]},
    ]
    state = {"status": "completed", "definition": {"owner": "pulse", "what": "x"}, "holder": None}
    monkeypatch.setattr(stats, "public_state", lambda s: {"title": "x", "business_progress": "completed"})
    monkeypatch.setattr(stats, "completions", lambda history: [])
    row = stats.row("wi_x", {**state, "profile": "lightweight"}, 3, history)
    assert (row["created_at"], row["updated_at"], row["closed_at"]) == (
        "2026-10-01T00:00:00+00:00",
        "2026-10-03T00:00:00+00:00",
        "2026-10-02T00:00:00+00:00",
    )
    assert row["holder"] is None


def test_main_only_label_comes_from_the_original_record(monkeypatch):
    base = {"status": "active", "definition": {"owner": "aki", "what": "x"}, "holder": None,
            "profile": "lightweight"}
    history = [{"at": "2026-10-01T00:00:00+00:00", "events": []}]
    monkeypatch.setattr(stats, "public_state", lambda s: {"title": "x", "business_progress": "active"})
    assert stats.row("wi_a", {**base, "production": {"record": {"main_only": True}}}, 1, history)["main_only"]
    legacy = {"legacy_import": {"original_metadata": {"main_only": True}}}
    assert stats.row("wi_b", {**base, **legacy}, 1, history)["main_only"]
    assert stats.row("wi_c", base, 1, history)["main_only"] is False
    assert stats.row("wi_d", base, 1, history, human=True)["main_only"] is True
    assert stats.row("wi_e", {**base, "profile": "standard"}, 1, history)["main_only"] is True
    # A recorded label wins over every fallback.
    assert stats.row("wi_f", {**base, "main_only": False}, 1, history, human=True)["main_only"] is False


def test_creation_records_the_main_body_label_from_profile_and_source(rig):
    _, _, kernel, original, wi, _ = rig
    payload = {"summary": "Fixture work for tests.", "why": "w", "what": "x", "acceptance": ["a"], "owner": "producer", "resources": ["repo"]}

    def create(request, intent, ref=None):
        original.prepare(intent, payload, None, 0, (), ref)
        return kernel.execute(Command(request, intent, payload), original.handle)["work_item"]["id"]

    discussed = create("discussed", "create_lightweight", human(original, "discuss", "建一个 WI 跟进"))
    standard = create("standard", "create_draft")
    storage = SimpleNamespace(connection=kernel._connection)
    items = stats.facts(storage, original.policy["namespace"], "t")["items"]
    labels = {r["id"]: r["main_only"] for r in items}
    assert {r["id"]: r["summary"] for r in items}[standard] == "Fixture work for tests."
    # The rig's own WorkItem is a headless Agent creation: automatic.
    assert (labels[wi], labels[discussed], labels[standard]) == (False, True, True)


def test_owner_relabels_main_body_with_a_human_decision_only(rig):
    from harness_rewrite import channel
    from tests_rewrite.test_session_access import execute

    _, _, kernel, original, wi, _ = rig
    assert channel.parse("/wi label " + wi + " 主身 切换脚本代建") == {
        "action": "execute", "wi": wi, "command": "label", "text": "切换脚本代建", "main_only": True}
    assert channel.parse("/wi label 自动")["main_only"] is False
    with pytest.raises(Rejected):
        channel.parse("/wi label wi_x maybe")
    data = {"main_only": True, "reason": "切换脚本代建"}
    assert execute(kernel, original, "agent-label", "set_main_only", data, wi)["status"] == "rejected"
    ref = human(original, "label", "/wi label " + wi + " 主身 切换脚本代建")
    assert execute(kernel, original, "label", "set_main_only", data, wi, ref)["status"] == "local_committed"
    storage = SimpleNamespace(connection=kernel._connection)
    labels = {r["id"]: r["main_only"] for r in stats.facts(storage, original.policy["namespace"], "t")["items"]}
    assert labels[wi] is True


def test_completions_describe_each_accepted_outcome_as_of_its_revision(monkeypatch):
    from harness_rewrite.model import digest

    artifacts = [{"object": "repo/x", "sha256": "a" * 64}]
    record = {"kind": "WorkRecorded", "record_id": "op_1", "record": {"artifacts": artifacts}}
    candidate = {"content": {"artifacts": artifacts}, "content_digest": "c" * 64}
    states = iter([
        {"profile": "lightweight", "records": [record], "candidates": {}, "title": "first"},
        {"profile": "lightweight", "records": [record], "candidates": {}, "title": "renamed"},
        {"profile": "standard", "records": [], "candidates": {"cand": candidate}, "title": "renamed"},
    ])
    monkeypatch.setattr(stats, "advance", lambda state, events: next(states))
    monkeypatch.setattr(stats, "public_state", lambda s: {"title": s["title"]})
    principal = {"semantics": {"principal": {"actor": "aki", "session": "session.x", "surface": "s"}}}
    history = [
        {"revision": 1, "at": "2026-10-17T00:00:00+00:00", **principal, "events": [{"kind": "WorkCompleted",
            "payload": {"profile": "lightweight", "record_ids": ["op_1"], "acceptance_refs": ["AC-1"],
                        "source_digest": "h" * 64}}]},
        {"revision": 2, "at": "2026-10-18T00:00:00+00:00", "events": [{"kind": "WorkRenamed", "payload": {}}]},
        {"revision": 3, "at": "2026-10-19T00:00:00+00:00", "events": [{"kind": "WorkCompleted",
            "payload": {"candidate_id": "cand", "review_id": "r", "verification_id": "v", "waiver_id": None}}]},
    ]
    first, second = stats.completions(history)
    # The title and accepted records are those of the completing revision.
    assert first == {"revision": 1, "at": "2026-10-17T00:00:00+00:00", "actor": "aki",
                     "profile": "lightweight", "title": "first",
                     "candidate": digest(["records", ["op_1"]]), "artifacts": digest(artifacts),
                     "acceptance": digest(["h" * 64, ["AC-1"]]), "verification": "human"}
    assert (second["candidate"], second["verification"], second["actor"]) == ("c" * 64, "independent", None)
    assert second["acceptance"] == digest(["r", "v", None]) and second["profile"] == "standard"
    # No path, prose or session leaves the core.
    assert "repo/x" not in str([first, second]) and "session.x" not in str([first, second])


def test_open_work_has_no_completions():
    history = [{"revision": 1, "at": "2026-10-01T00:00:00+00:00", "events": [{"kind": "WorkStarted"}]}]
    assert stats.completions(history) == []


def test_session_writes_export_only_a_domain_separated_session_key():
    from harness_rewrite.model import digest, native_session_ref

    ref = native_session_ref("ns", "pulse", "native-uuid", "surface_v1_x")
    history = [
        {"at": "2026-10-01T00:00:00+00:00", "semantics": {"principal": {
            "actor": "pulse", "session": ref, "surface": "surface_v1_x"}}, "events": [{"kind": "WorkRecorded"}]},
        # A read-only replay commit, the delivery service and a principal-less import are not session writes.
        {"at": "2026-10-02T00:00:00+00:00", "semantics": {"principal": {
            "actor": "pulse", "session": ref, "surface": "surface_v1_x"}}, "events": []},
        {"at": "2026-10-03T00:00:00+00:00", "semantics": {"principal": {
            "actor": "delivery-worker", "session": "delivery-service", "surface": "surface_v1_x"}},
         "events": [{"kind": "EffectObserved"}]},
        {"at": "2026-10-04T00:00:00+00:00", "semantics": {"intent": "x"}, "events": [{"kind": "X"}]},
    ]
    writes = stats.session_writes(history)
    assert writes == [{"session": digest(["stats_session_write", ref]), "actor": "pulse",
                       "at": "2026-10-01T00:00:00+00:00"}]
    # The core's principal reference, native ID and surface do not leave the core.
    assert ref not in str(writes) and "native-uuid" not in str(writes) and "surface_v1_x" not in str(writes)


def test_facts_carry_the_holding_session_write(rig):
    from harness_rewrite.model import digest

    _, _, kernel, original, wi, _ = rig
    storage = SimpleNamespace(connection=kernel._connection)
    [item] = [r for r in stats.facts(storage, original.policy["namespace"], "t")["items"] if r["id"] == wi]
    assert digest(["stats_session_write", original.base.session]) in {w["session"] for w in item["session_writes"]}
    assert {w["actor"] for w in item["session_writes"]} == {original.base.actor}



def test_facts_name_each_rendered_project_page(rig, tmp_path):
    from harness_rewrite import project_page

    _, _, kernel, original, wi, _ = rig
    storage = SimpleNamespace(connection=kernel._connection)
    namespace = original.policy["namespace"]
    [item] = [r for r in stats.facts(storage, namespace, "t")["items"] if r["id"] == wi]
    assert item["project_page"] is None
    pages = tmp_path / "views"
    assert stats.facts(storage, namespace, "t", pages)["items"][0]["project_page"] is None
    response = kernel.inspect(__import__("harness_rewrite").Query(wi), original.handle)
    project_page.publish(pages, response, as_of="2026-10-28T00:00:00+00:00")
    [item] = [r for r in stats.facts(storage, namespace, "t", pages)["items"] if r["id"] == wi]
    assert item["project_page"] == str((pages / (wi + ".html")).absolute())

def test_facts_export_only_pull_requests_the_core_merged(tmp_path):
    from harness_rewrite.kernel import Kernel
    from tests_rewrite.test_pull_requests import FakeGitHub, drain, review, rig_with_pr
    from tests_rewrite.test_workflow import BASE, original as committed

    def merged(rig):
        with rig.kernel._connection(write=False) as conn:
            state, revision, _, history = Kernel._replay(conn, BASE.namespace, rig.wi)
        return stats.row(rig.wi, state, revision, history)["merged_pull_requests"]

    (tmp_path / "kernel").mkdir()
    rig, candidate = rig_with_pr(tmp_path / "kernel")
    assert merged(rig) == []  # Registered is not yet output.
    committed(review(rig, candidate, 92))
    assert merged(rig) == []  # Nor is a passed review.
    drain(rig, FakeGitHub())
    [row] = merged(rig)
    assert row == {"repository": "bigbird-online/PULSE", "number": 7, "head_sha": "a" * 40,
                   "merge_commit": "f" * 40, "merged_at": "2026-10-26T10:00:00Z",
                   "observed_at": row["observed_at"], "score": 92, "reviewed_rounds": 1,
                   "decision": "review"}
    assert isinstance(row["observed_at"], str) and "session" not in str(row)
    # Merged on GitHub without the core's own attempt: not the core's output.
    (tmp_path / "outside").mkdir()
    outside, candidate = rig_with_pr(tmp_path / "outside")
    committed(review(outside, candidate, 92))
    drain(outside, FakeGitHub(merged=True))
    assert merged(outside) == []

