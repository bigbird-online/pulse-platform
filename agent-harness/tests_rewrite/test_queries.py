"""Collections and native binding selection are authenticated read-only facts."""

from dataclasses import replace
import hashlib
import pytest
from harness_rewrite import Command, Query
from harness_rewrite.cli import bound_work_item
from harness_rewrite.model import Rejected
from tests_rewrite.test_workflow import Rig, DEFINITION, original, graph, node


def test_filtered_collections_and_views_do_not_change_facts_or_grant_rights(tmp_path):
    r = Rig(tmp_path)
    r.adopt(graph(r, [node("a"), node("b")]))
    first = r.wi
    second = original(
        r.kernel.execute(
            Command("second", "create_draft", {**DEFINITION, "what": "Second report"}),
            "create",
        )
    )["work_item_id"]
    db = tmp_path / "journal.sqlite"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    all_items = r.kernel.inspect(Query(view="list", limit=1), "owner")
    assert len(all_items["result"]["items"]) == 1
    page = r.kernel.inspect(
        Query(view="list", after=all_items["result"]["next_offset"], limit=1), "owner"
    )
    assert {i["id"] for p in (all_items, page) for i in p["result"]["items"]} == {
        first,
        second,
    }
    assert (
        r.kernel.inspect(
            Query(view="list", text="SECOND", status="draft", owner="producer"), "owner"
        )["result"]["items"][0]["id"]
        == second
    )
    for view in (
        "tasks",
        "collaborations",
        "records",
        "effects",
        "candidates",
        "history",
        "plan",
    ):
        response = r.kernel.inspect(Query(first, view=view), "owner")
        assert response["status"] == "read_only", response
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert all(c["name"] != "local_work" for c in all_items["capabilities"])


def test_collection_and_binding_cannot_read_other_namespace_or_unapproved_wi(tmp_path):
    r = Rig(tmp_path)
    r.adopt(graph(r))
    second = original(
        r.kernel.execute(Command("second", "create_draft", DEFINITION), "create")
    )["work_item_id"]
    r.contexts["limited"] = replace(
        r.contexts["owner"], readable_wis=frozenset({second})
    )
    items = r.kernel.inspect(Query(view="list"), "limited")["result"]["items"]
    assert [i["id"] for i in items] == [second]
    r.contexts["different"] = replace(r.contexts["owner"], namespace="elsewhere")
    assert r.kernel.inspect(Query(view="list"), "different")["result"]["items"] == []
    r.contexts["stranger"] = replace(
        r.contexts["owner"], actor="stranger", session="stranger-session"
    )
    assert r.kernel.inspect(Query(view="bindings"), "stranger")["result"]["items"] == []


def test_omitted_wi_requires_unique_real_native_binding(tmp_path):
    r = Rig(tmp_path)
    assert bound_work_item(r.kernel, "owner") == r.wi
    r.adopt(graph(r))
    assert bound_work_item(r.kernel, "owner") == r.wi
    original(r.kernel.execute(Command("second", "create_draft", DEFINITION), "create"))
    with pytest.raises(Rejected, match="exactly one"):
        bound_work_item(r.kernel, "owner")
    with pytest.raises(Rejected, match="exactly one"):
        bound_work_item(r.kernel, "helper")


def test_invalid_query_and_corrupt_collection_refuse_unknown_facts(tmp_path):
    import sqlite3

    r = Rig(tmp_path)
    for q in (
        Query(view="list", limit=0),
        Query(view="list", owner=7),
        Query(r.wi, view="list"),
        Query(view="state"),
    ):
        assert r.kernel.inspect(q, "owner")["status"] == "rejected"
    conn = sqlite3.connect(tmp_path / "journal.sqlite")
    conn.execute("UPDATE journal SET digest='broken'")
    conn.commit()
    conn.close()
    result = r.kernel.inspect(Query(view="list"), "owner")["result"]
    assert result["coverage"] == "partial" and result["items"] == []
    assert result["unavailable_items"][0]["code"] == "journal_invalid"
    bindings = r.kernel.inspect(Query(view="bindings"), "owner")
    assert bindings['result']['coverage'] == 'partial'
    with pytest.raises(Rejected, match='Binding discovery is partial'):
        bound_work_item(r.kernel, 'owner')
