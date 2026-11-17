from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from harness_rewrite import Command, Query
from harness_rewrite.cli import main
from tests_rewrite import test_collaboration
from tests_rewrite.test_workflow import DEFINITION, graph, node, original

rig = test_collaboration.rig


def second(rig):
    return original(
        rig.kernel.execute(
            Command("second", "create_draft", {**DEFINITION, "what": "Second report"}),
            "create",
        )
    )["work_item_id"]


def scan(rig, handle="owner", **kwargs):
    result = rig.kernel.inspect(Query(view="scan", **kwargs), handle)
    assert result["status"] == "read_only", result
    return result


def test_scan_counts_whole_waiting_set_and_filters_without_mutation(rig, tmp_path):
    rig.adopt(graph(rig, [{**node("work"), "deadline": "2026-10-07T00:00:10Z"}]))
    original(rig.claim("work"))
    other = second(rig)
    rig.kernel._clock = lambda: "2026-10-07T00:00:15Z"
    before = hashlib.sha256((tmp_path / "journal.sqlite").read_bytes()).hexdigest()
    pages = [scan(rig, after=i, limit=1, age_threshold_seconds=10) for i in [0, 1]]
    assert {row["id"] for p in pages for row in p["result"]["items"]} == {rig.wi, other}
    assert all(p["result"]["total_count"] == 2 for p in pages)
    current = next(
        row for p in pages for row in p["result"]["items"] if row["id"] == rig.wi
    )
    watch = rig.kernel.inspect(
        Query(rig.wi, view="watch", limit=1, age_threshold_seconds=10), "owner"
    )["result"]["items"]
    assert len(watch["items"]) == 1 and watch["summary"]["waiting_items"] == 2
    assert current["watch"]["summary"] == watch["summary"]
    assert watch["summary"]["by_kind"] == {"execution": 1, "task": 1}
    assert watch["summary"]["overdue"] == {"overdue": 1, "not_due": 0, "unknown": 1}
    assert watch["summary"]["record_age"] == {"stale": 2, "recent": 0, "unknown": 0}
    assert watch["summary"]["executors"] == {
        "total": 1,
        "presence_unknown": 1,
        "observed_present": 0,
    }
    filtered = scan(rig, status="draft", owner="producer", text="SECOND")["result"][
        "items"
    ]
    assert [row["id"] for row in filtered] == [other]
    assert scan(rig, status="active")["result"]["items"][0]["id"] == rig.wi
    assert (
        pages[0]["result"]["next_offset"] == 1
        and pages[1]["result"]["next_offset"] is None
    )
    assert all(c["name"] == "local_read" for p in pages for c in p["capabilities"])
    assert all(p["effects"] == [] and p["operation_id"] is None for p in pages)
    assert (
        hashlib.sha256((tmp_path / "journal.sqlite").read_bytes()).hexdigest() == before
    )
    for forbidden in [
        "private-producer-session",
        "dispatch_snapshot",
        "last_authenticated_record",
        "confirmed_plan",
    ]:
        assert forbidden not in json.dumps(pages)


def test_scan_observes_one_clock_and_scope_without_leaking_inaccessible_corruption(
    rig, tmp_path
):
    other = second(rig)
    c = sqlite3.connect(tmp_path / "journal.sqlite")
    c.execute("UPDATE journal SET digest='corrupt' WHERE wi=?", (other,))
    c.commit()
    c.close()
    rig.contexts["limited"] = replace(
        rig.contexts["owner"], readable_wis=frozenset({rig.wi})
    )
    calls = []

    def clock():
        calls.append("observed")
        return "2026-10-08T00:00:00Z"

    rig.kernel._clock = clock
    result = scan(rig, "limited")["result"]
    assert calls == ["observed"] and result["total_count"] == 1
    assert result["items"][0]["id"] == rig.wi
    assert result["as_of"] == result["items"][0]["watch"]["as_of"]
    partial = rig.kernel.inspect(Query(view="scan"), "owner")["result"]
    assert partial["coverage"] == "partial"
    assert partial["unavailable_items"][0]["code"] == "journal_invalid"
    assert [item["id"] for item in partial["items"]] == [rig.wi]
    rig.contexts["elsewhere"] = replace(rig.contexts["owner"], namespace="elsewhere")
    assert scan(rig, "elsewhere")["result"]["total_count"] == 0


def test_scan_clock_and_missing_threshold_stay_unknown(rig):
    rig.adopt(graph(rig))
    rig.kernel._clock = lambda: "invalid"
    result = scan(rig)["result"]
    assert result["as_of"] is None and result["items"][0]["watch"]["as_of"] is None
    summary = result["items"][0]["watch"]["summary"]
    assert summary["record_age"] == {"stale": 0, "recent": 0, "unknown": 1}


@pytest.mark.parametrize(
    "options",
    [
        {"work_item_id": "wi_example"},
        {"revision": 1},
        {"age_threshold_seconds": 0},
        {"age_threshold_seconds": True},
        {"limit": 0},
    ],
)
def test_scan_rejects_incompatible_or_unbounded_options(rig, options):
    assert (
        rig.kernel.inspect(Query(view="scan", **options), "owner")["error"]["code"]
        == "invalid_input"
    )


@pytest.mark.parametrize("action", ["identity", "resume", "list"])
def test_scan_view_never_silently_ignored_by_another_action(action, capsys):
    assert main([action, "--view", "scan"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"
