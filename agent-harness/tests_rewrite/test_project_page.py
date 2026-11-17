"""AC-1 project page: rendered after every committed revision, never vetoing it."""

from copy import deepcopy
import json
import re

from harness_rewrite import Command, Kernel, Query, project_page
from harness_rewrite.views import Views
from tests_rewrite.test_workflow import DEFINITION, OTHER, Rig, edge, error, graph, node, original

CLOCK = "2026-10-07T00:00:00Z"


def paged(tmp_path):
    rig = Rig(tmp_path)
    rig.kernel = Kernel(
        tmp_path / "journal.sqlite", rig.contexts.__getitem__, clock=lambda: CLOCK,
        pages=tmp_path / "views",
    )
    return rig


def page(rig, tmp_path):
    return (tmp_path / "views" / (rig.wi + ".html")).read_text()


def state_block(text):
    match = re.search(
        r'<script type="application/json" id="artifact-state">(.*?)</script>', text, re.S
    )
    assert match
    return json.loads(match[1])


def test_standard_page_follows_every_commit_with_plan_and_delta(tmp_path):
    rig = paged(tmp_path)
    rig.adopt(graph(rig, [node("first"), node("second")], [edge("first", "second")]))
    text = page(rig, tmp_path)
    for pane in ('id="over"', 'id="work"', 'id="pr"', 'id="collab"', 'id="log"'):
        assert pane in text
    assert "after first" in text and "AC-1" in text and "unverified" in text
    assert "Fixture work for tests." in text and "2026-10-07 08:00 UTC+8" in text
    assert "领取节点 first" in text and "<!-- slot:narrative -->" in text
    first = state_block(text)
    assert first["nodes"] == {"first": "ready", "second": "waiting"}
    claimed = rig.claim("first")
    assert claimed["project_page"]["revision"] == claimed["work_item"]["local_revision"]
    second = state_block(page(rig, tmp_path))
    assert second["since_revision"] == first["revision"]
    assert "节点 first：ready → running" in second["changes"]
    read = rig.kernel.inspect(Query(rig.wi), "owner")
    assert read["project_page"] == claimed["project_page"]


def test_lightweight_page_has_facts_only(tmp_path):
    rig = paged(tmp_path)
    created = rig.kernel.execute(Command("light", "create_lightweight", DEFINITION), "create")
    rig.wi = original(created)["work_item_id"]
    original(rig.send("start_work", {}))
    original(rig.send("checkpoint", {"summary": "Light progress", "resources": ["repo"]}))
    text = page(rig, tmp_path)
    assert 'id="work"' not in text and 'id="collab"' not in text
    assert 'id="over"' in text and 'id="pr"' in text and 'id="log"' in text
    assert "Light progress" in text and "档位 lightweight" in text


def hostile_response(tmp_path):
    rig = paged(tmp_path)
    response = deepcopy(rig.kernel.inspect(Query(rig.wi), "owner"))
    view = response["result"]["current_observation"]
    view["summary"] = "</script><script>alert(1)</script>"
    view["definition"]["why"] = '<img src=x onerror="alert(2)">'
    view["records"] = [{"kind": "CheckpointRecorded", "author": "x", "record": {
        "summary": '<a href="javascript:alert(3)">click</a>'}}]
    view["pull_requests"] = [
        {"key": "a#7", "repository": "bigbird-online/PULSE", "number": 7, "status": "merged",
         "reviewed_rounds": 1, "merge_blocked": None,
         "rounds": [{"review": {"score": 90},
                     "merge": {"observation": {"merge_commit": "abcdef1234567890"}}}]},
        {"key": "b#8", "repository": "javascript:alert(4)//x", "number": 8,
         "status": "human_confirmation_required", "reviewed_rounds": 0,
         "merge_blocked": None, "rounds": []},
    ]
    return response


def test_hostile_text_is_escaped_and_only_web_links_survive(tmp_path):
    text, state = project_page.render(hostile_response(tmp_path), as_of=CLOCK, baseline=None)
    assert "<script>alert" not in text and "<img" not in text
    assert 'href="javascript' not in text
    assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert '<a href="https://github.com/bigbird-online/PULSE/pull/7"' in text
    assert text.count("<script") == 2  # the state block and the tab script only
    block = text.split('id="artifact-state">', 1)[1].split("</script>", 1)[0]
    assert "<" not in block and json.loads(block)["pull_requests"]["b#8"] == state[
        "pull_requests"]["b#8"]
    assert "确认 PR javascript:alert(4)//x#8 是否合并" in text
    assert project_page.link("javascript:alert(1)", "x") == "x"
    assert project_page.link("data:text/html,x", "<b>") == "&lt;b&gt;"


def test_render_failure_never_fails_or_rolls_back_the_commit(tmp_path, monkeypatch):
    rig = paged(tmp_path)
    rig.adopt(graph(rig))

    def broken(*args, **kwargs):
        raise RuntimeError("template defect")

    monkeypatch.setattr(project_page, "render", broken)
    claimed = rig.send("checkpoint", {"summary": "Still recorded", "resources": ["repo"]})
    assert claimed["status"] == "local_committed" and "project_page" not in claimed
    assert rig.kernel.inspect(Query(rig.wi), "owner")["work_item"]["local_revision"] == 4
    for item in (tmp_path / "views").iterdir():
        item.unlink()
    (tmp_path / "views").rmdir()
    (tmp_path / "views").write_text("not a directory")
    monkeypatch.undo()
    again = rig.send("checkpoint", {"summary": "Also recorded", "resources": ["repo"]})
    assert again["status"] == "local_committed" and "project_page" not in again


def test_page_write_is_atomic_and_never_moves_backwards(tmp_path, monkeypatch):
    rig = paged(tmp_path)
    rig.adopt(graph(rig))
    old = rig.kernel.inspect(Query(rig.wi), "owner")
    original(rig.send("checkpoint", {"summary": "Newer fact", "resources": ["repo"]}))
    root = tmp_path / "views"
    newer = page(rig, tmp_path)
    assert project_page.write(root, old, as_of=CLOCK)["revision"] == 4
    assert page(rig, tmp_path) == newer

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(project_page.os, "replace", fail)
    current = rig.kernel.inspect(Query(rig.wi), "owner")
    try:
        project_page.write(root, current, as_of=CLOCK)
    except OSError:
        pass
    assert page(rig, tmp_path) == newer
    assert not list(root.glob(".generation-*"))


def test_view_read_fills_a_missing_page_without_rewriting_a_current_one(tmp_path):
    rig = Rig(tmp_path)
    root = tmp_path / "views"
    response = Views(rig.kernel, root).read(rig.wi, "owner")
    assert response["project_page"] == {"path": str(root / (rig.wi + ".html")), "revision": 1}
    stamp = (root / (rig.wi + ".html")).stat().st_mtime_ns
    Views(rig.kernel, root).read(rig.wi, "owner")
    assert (root / (rig.wi + ".html")).stat().st_mtime_ns == stamp


def test_acceptance_follows_the_current_candidate_through_rework(tmp_path):
    from tests_rewrite.test_review import ARTIFACT, VERIFIER, approve, assign, produce, verify

    def ac1():
        view = rig.state()["result"]["current_observation"]
        assert "AC-1" in page(rig, tmp_path)
        return dict((ref, status) for ref, _, status in project_page.acceptance(view))["AC-1"]

    rig = paged(tmp_path)
    rig.contexts["verifier"] = VERIFIER
    old = produce(rig)
    original(verify(rig, old, assign(rig, old)))
    assert ac1() == "verified"
    original(approve(rig, old, decision="changes_requested"))
    original(rig.send("revise_work", {"candidate_id": old, "nodes": ["work"],
                                      "reason": "Changed work needs fresh checks"}))
    assert ac1() == "unverified"
    original(rig.result(original(rig.claim("work"))["attempt_id"],
                        artifacts={"result": ARTIFACT}, request="again"))
    current = rig.state()["result"]["current_observation"]["candidates"]
    assert current["active"] not in (None, old)
    assert ac1() == "unverified"


def test_changes_requested_pr_names_the_resubmit_step(tmp_path):
    from tests_rewrite.test_pull_requests import review, rig_with_pr, view

    rig, candidate = rig_with_pr(tmp_path)
    original(review(rig, candidate, 60))
    response = view(rig)
    assert response["next_action"]["intent"] == "submit_pull_request"
    steps = project_page.next_steps(response)
    assert any("重新登记" in action and "60" in why for _, action, why in steps)
    assert "无待办" not in project_page.next_strip(response)


def test_failed_sidecar_write_neither_misreports_nor_lets_an_old_commit_win(
    tmp_path, monkeypatch
):
    rig = paged(tmp_path)
    rig.adopt(graph(rig))
    root = tmp_path / "views"
    old = rig.kernel.inspect(Query(rig.wi), "owner")
    real = project_page.atomic

    def no_sidecar(where, target, content):
        if target.name.endswith(".state.json"):
            raise OSError("disk full")
        real(where, target, content)

    monkeypatch.setattr(project_page, "atomic", no_sidecar)
    newer = original(rig.send("checkpoint", {"summary": "Newer", "resources": ["repo"]}))
    monkeypatch.setattr(project_page, "atomic", real)
    revision = newer["revision"]
    assert project_page.located(root, rig.wi)["revision"] == revision
    text = page(rig, tmp_path)
    assert project_page.write(root, old, as_of=CLOCK)["revision"] == revision
    assert page(rig, tmp_path) == text


def test_damaged_derived_files_never_break_the_kernel_query(tmp_path):
    rig = paged(tmp_path)
    rig.adopt(graph(rig))
    root = tmp_path / "views"
    (root / (rig.wi + ".state.json")).write_text(json.dumps(
        {"schema": project_page.SCHEMA, "current": {"work_item_id": rig.wi}}))
    read = rig.kernel.inspect(Query(rig.wi), "owner")
    assert read["status"] == "read_only" and read["project_page"]["path"].endswith(".html")
    (root / (rig.wi + ".html")).write_text("<html>truncated")
    read = rig.kernel.inspect(Query(rig.wi), "owner")
    assert read["status"] == "read_only" and read["project_page"] is None
    original(rig.send("checkpoint", {"summary": "Heals the page", "resources": ["repo"]}))
    assert project_page.located(root, rig.wi) is not None


def test_narrative_is_escaped_linked_only_over_http_and_marked_stale(tmp_path):
    rig = paged(tmp_path)
    rig.adopt(graph(rig))
    hostile = {
        "banner": "Ship <script>alert(1)</script> today",
        "next_steps": ["Read https://example.com/plan?a=1&b=2 first", "Never javascript:alert(2)"],
        "risks": ["Disk near 81%"],
        "decisions": ["大鸟 10-28: fold 771 into 253"],
        "background": "line one\nsee http://example.org/x\n\n",
    }
    original(rig.send("record_project_narrative", hostile))
    view = rig.state()["result"]["current_observation"]
    assert view["narrative"]["narrative"]["banner"] == hostile["banner"]
    text = page(rig, tmp_path)
    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;" in text
    assert '<a href="https://example.com/plan?a=1&amp;b=2" rel="noopener noreferrer">' in text
    assert 'href="javascript' not in text and "Never javascript:alert(2)" in text
    assert "可能已过时" not in text
    original(rig.send("checkpoint", {"summary": "Newer fact", "resources": ["repo"]}))
    assert "可能已过时" in page(rig, tmp_path) and "之后又有 1 条进展记录" in page(rig, tmp_path)
    later = rig.state()
    aged = project_page.narrative_section(later["result"]["current_observation"],
                                          "2026-10-20T00:00:00+00:00")
    assert "已超过 3 天" in aged


def test_narrative_input_is_bounded(tmp_path):
    rig = paged(tmp_path)
    rig.adopt(graph(rig))
    for bad in ({}, {"banner": ""}, {"banner": "x" * 301}, {"risks": []}, {"risks": "one"},
                {"risks": ["a"] * 6}, {"color": "red"}, {"banner": "bell\u0007"}):
        assert error(rig.send("record_project_narrative", bad)) == "invalid_input", bad


def test_only_the_working_agent_writes_the_narrative(tmp_path):
    rig = paged(tmp_path)
    rig.adopt(graph(rig))
    rig.contexts["other"] = OTHER
    assert error(rig.send("record_project_narrative", {"banner": "Not mine"}, handle="other")) == (
        "authority_denied")


def test_malformed_url_in_narrative_never_stops_page_updates(tmp_path):
    rig = paged(tmp_path)
    rig.adopt(graph(rig))
    root = tmp_path / "views"
    recorded = original(rig.send("record_project_narrative", {"banner": "See http://[broken"}))
    assert project_page.located(root, rig.wi)["revision"] == recorded["revision"]
    assert "See http://[broken" in page(rig, tmp_path) and 'href="http://[' not in page(rig, tmp_path)
    later = original(rig.send("checkpoint", {"summary": "Still updating", "resources": ["repo"]}))
    assert project_page.located(root, rig.wi)["revision"] == later["revision"]


def test_reading_the_page_marks_an_expired_narrative_once(tmp_path):
    # Written on the fixture clock (2026-10-07); read with the wall clock, days later.
    rig = paged(tmp_path)
    rig.adopt(graph(rig))
    root = tmp_path / "views"
    original(rig.send("record_project_narrative", {"banner": "Fresh judgement"}))
    assert "可能已过时" not in page(rig, tmp_path)
    Views(rig.kernel, root).read(rig.wi, "owner")
    assert "已超过 3 天" in page(rig, tmp_path)
    stamp = (root / (rig.wi + ".html")).stat().st_mtime_ns
    Views(rig.kernel, root).read(rig.wi, "owner")
    assert (root / (rig.wi + ".html")).stat().st_mtime_ns == stamp


def test_parent_page_lists_children_and_child_page_links_back(tmp_path):
    from dataclasses import replace

    from tests_rewrite.test_children import light as family, send, spawn
    from tests_rewrite.test_review import ARTIFACT
    from tests_rewrite.test_workflow import BASE

    rig = family(tmp_path)
    rig.kernel = Kernel(tmp_path / "journal.sqlite", rig.contexts.__getitem__,
                        clock=lambda: CLOCK, pages=tmp_path / "views")
    child = original(spawn(rig, summary="Child <b>summary</b> line."))["work_item_id"]
    views = tmp_path / "views"
    # Creating the child re-renders the parent's page, whose revision did not move.
    parent_text = (views / (rig.wi + ".html")).read_text()
    assert "<h2>子 WI</h2>" in parent_text and f'href="{child}.html"' in parent_text
    assert "Child &lt;b&gt;summary&lt;/b&gt; line." in parent_text and "<b>summary" not in parent_text
    assert state_block(parent_text)["children"] == {child: "active"}
    assert "子 WI 的成本见 Stats" in parent_text
    child_text = (views / (child + ".html")).read_text()
    assert f'父 WI <a href="{rig.wi}.html">' in child_text and "<h2>子 WI</h2>" not in child_text
    # The child's approval completes it; the parent page follows without a parent commit.
    rig.contexts["work"] = replace(BASE, artifacts=(ARTIFACT,))
    original(send(rig, child, "record_work", {
        "summary": "Child artifact", "resources": ["repo/child"],
        "artifacts": [ARTIFACT.reference()]}, "work"))
    send(rig, child, "review_child", {"decision": "approved", "reason": "Both met"})
    after = state_block((views / (rig.wi + ".html")).read_text())
    assert after["children"] == {child: "completed"}


def test_child_status_change_shows_in_the_next_revision_delta():
    before = {"business_progress": "active", "execution_progress": "x", "nodes": {},
              "pull_requests": {}, "children": {"wi_a": "active"}}
    after = {**before, "children": {"wi_a": "completed", "wi_b": "active"}}
    assert project_page.changes(before, after) == [
        "子 WI wi_a：active → completed", "子 WI wi_b：新增 → active"]
    # A snapshot rendered before children were recorded compares as none.
    old = {k: v for k, v in before.items() if k != "children"}
    assert project_page.changes(old, before) == ["子 WI wi_a：新增 → active"]


def test_child_rows_show_pull_requests_and_never_link_odd_identities(tmp_path):
    response = hostile_response(tmp_path)
    view = response["result"]["current_observation"]
    view["parent"] = "../evil"
    view["children"] = [
        {"id": "wi_20261030_001", "status": "active", "business_progress": "active",
         "summary": "First child", "pull_requests": [
             {"repository": "bigbird-online/PULSE", "number": 12, "status": "merged"}]},
        {"id": "x\"><script>", "status": "active", "business_progress": "active",
         "summary": None, "pull_requests": []},
    ]
    text, state = project_page.render(response, as_of=CLOCK, baseline=None)
    assert 'href="wi_20261030_001.html"' in text and "1 个" in text
    assert 'href="https://github.com/bigbird-online/PULSE/pull/12"' in text
    assert 'href="../evil.html"' not in text and "父 WI <code>../evil</code>" in text
    assert "<script>\"" not in text and 'href="x' not in text
    assert set(state["children"]) == {"wi_20261030_001", "x\"><script>"}


def test_a_parent_snapshot_read_before_a_newer_child_change_never_wins(tmp_path):
    """Review repro: a parent read paused while a child's approval refreshed the page."""
    from dataclasses import replace
    import threading

    from tests_rewrite.test_children import light as family, send, spawn
    from tests_rewrite.test_review import ARTIFACT
    from tests_rewrite.test_workflow import BASE

    rig = family(tmp_path)
    rig.kernel = Kernel(tmp_path / "journal.sqlite", rig.contexts.__getitem__,
                        clock=lambda: CLOCK, pages=tmp_path / "views")
    child = original(spawn(rig))["work_item_id"]
    rig.contexts["work"] = replace(BASE, artifacts=(ARTIFACT,))
    inspect, armed, approval = rig.kernel.inspect, [True], []

    def interleaved(query, handle):
        response = inspect(query, handle)
        if armed and query.work_item_id == rig.wi:
            armed.clear()
            # The child is approved while this parent snapshot (child active) is in hand.
            approval.append(threading.Thread(target=send, args=(
                rig, child, "review_child", {"decision": "approved", "reason": "Both met"})))
            approval[0].start()
            approval[0].join(timeout=1)
        return response

    rig.kernel.inspect = interleaved
    original(send(rig, child, "record_work", {
        "summary": "Child artifact", "resources": ["repo/child"],
        "artifacts": [ARTIFACT.reference()]}, "work"))
    approval[0].join(timeout=10)
    assert not approval[0].is_alive()
    rig.kernel.inspect = inspect
    assert inspect(Query(rig.wi), "owner")["result"]["current_observation"]["children"][0][
        "business_progress"] == "completed"
    shown = state_block((tmp_path / "views" / (rig.wi + ".html")).read_text())
    assert shown["children"] == {child: "completed"}


def test_a_parent_commit_published_after_a_child_approval_keeps_the_newer_family(tmp_path):
    """Review repro: the parent's own response, taken before a child approval, publishes last."""
    from dataclasses import replace

    from tests_rewrite.test_children import light as family, send, spawn
    from tests_rewrite.test_review import ARTIFACT
    from tests_rewrite.test_workflow import BASE

    rig = family(tmp_path)
    rig.kernel = Kernel(tmp_path / "journal.sqlite", rig.contexts.__getitem__,
                        clock=lambda: CLOCK, pages=tmp_path / "views")
    child = original(spawn(rig))["work_item_id"]
    rig.contexts["work"] = replace(BASE, artifacts=(ARTIFACT,))
    original(send(rig, child, "record_work", {
        "summary": "Child artifact", "resources": ["repo/child"],
        "artifacts": [ARTIFACT.reference()]}, "work"))
    publish, armed = rig.kernel._publish_page, [True]

    def late(committed, handle, at):
        if armed and committed["work_item"]["id"] == rig.wi:
            armed.clear()
            # The parent's response still shows the child active when this approval lands.
            original(send(rig, child, "review_child", {"decision": "approved", "reason": "Met"}))
        return publish(committed, handle, at)

    rig.kernel._publish_page = late
    original(send(rig, rig.wi, "checkpoint", {"summary": "Parent note", "resources": ["repo/x"]}))
    assert not armed
    shown = state_block((tmp_path / "views" / (rig.wi + ".html")).read_text())
    assert shown["children"] == {child: "completed"}


def test_a_parent_commit_published_after_its_first_child_keeps_that_child(tmp_path):
    """Review repro: a childless parent response publishes after the first child's redraw."""
    from tests_rewrite.test_children import light as family, send, spawn

    rig = family(tmp_path)
    rig.kernel = Kernel(tmp_path / "journal.sqlite", rig.contexts.__getitem__,
                        clock=lambda: CLOCK, pages=tmp_path / "views")
    publish, created = rig.kernel._publish_page, []

    def late(committed, handle, at):
        if not created and committed["work_item"]["id"] == rig.wi:
            # The parent's response has no children when its first child is created.
            created.append(original(spawn(rig))["work_item_id"])
        return publish(committed, handle, at)

    rig.kernel._publish_page = late
    original(send(rig, rig.wi, "checkpoint", {"summary": "Parent note", "resources": ["repo/x"]}))
    shown = state_block((tmp_path / "views" / (rig.wi + ".html")).read_text())
    assert shown["children"] == {created[0]: "active"}
