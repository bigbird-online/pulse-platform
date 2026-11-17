"""Synthetic stage inputs through the real receipt ledger and query, not UAT."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import LedgerError, UsageLedger
from pulse_compute_cost.receipts import Receipt, ReceiptError, reference
from test_collaboration_receipts import usage

AT = "2026-10-01T01:00:00Z"
LATER = "2026-10-02T01:00:00Z"
WI = "wi_20261008_550"
OTHER = "wi_20261008_551"
CONTEXT = reference("context", "synthetic-goal-input")
OBJECT = reference("object", "compute-wi-cost")
VERSION = reference("version", "synthetic-source-v1")
ACTIVITY = reference("activity", "synthetic-user-input")


def fact(kind, identity, payload, at=AT, **kwargs):
    return Receipt.make(kind, identity, payload, occurred_at=at, observed_at=LATER,
                        producer="synthetic_stage.v1", evidence=(ACTIVITY,), **kwargs)


def contribution(identity="formed", *, version=VERSION, change="new", at=AT, **kwargs):
    payload = dict(wi=None, context_ref=CONTEXT, activity_refs=[ACTIVITY],
                   object_ref=OBJECT, version_ref=version, name="计量方法", category="skill",
                   unit="capability", comparison_basis="usable-method", contribution=change,
                   extent="合成样本中的方法", quantity="1", epistemic="agent_judgment")
    payload.update(kwargs.pop("payload_changes", {}))
    return fact("stage_output", identity, payload, at=at, **kwargs)


def goal():
    return fact("stage_goal", "goal", dict(wi=None, context_ref=CONTEXT,
        activity_refs=[ACTIVITY], name="协作计量", desired_change="接续时不重复解释已知成果",
        expected=[dict(category="skill", unit="capability", comparison_basis="usable-method",
                       quantity="1", baseline=None, criterion="真实接续可读")],
        input_scope="该事务已有活动及机器用量", unknowns=["主动人时未核对"],
        epistemic="agent_judgment"))


def link(targets, wi=WI, identity="link", *, state="assigned", **kwargs):
    return fact("stage_link", identity, dict(wi=wi, context_ref=CONTEXT,
        target_refs=[r.entity_ref for r in targets], state=state,
        basis="合成原始活动边界", epistemic="agent_judgment"), at=LATER, **kwargs)


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / "ledger", clock=lambda: datetime(2026, 10, 3, tzinfo=timezone.utc))


def query(ledger, **kwargs):
    return query_collaboration(ledger, CollaborationQuery(**kwargs))


def test_pre_wi_persists_and_late_link_only_changes_membership(ledger):
    g, o, u = goal(), contribution(), usage(wi=None, identity="pre-wi", at=AT)
    ledger.ingest_receipts([g, o, u])
    before = query(ledger, watermark=1)
    assert before["stages"]["outputs"]["cumulative"]["object_count"] == 1
    assert before["stages"]["goals"][0]["wi"] is None
    assert before["stages"]["outputs"]["items"][0]["occurred_at"] == AT
    ledger.ingest_receipts([link([g, o, u])])
    after = query(ledger, work_items=(WI,))
    assert after["summary"]["usage"]["known_total_tokens"] == 100
    assert after["stages"]["outputs"]["cumulative"]["object_count"] == 1
    assert after["stages"]["goals"][0]["wi"] == WI
    old = query(ledger, watermark=before["watermark"])
    assert old == before
    assert query(ledger)["summary"]["usage"]["known_total_tokens"] == 100
    assert ledger.receipt_snapshot()["facts"][2].payload["wi"] is None


def test_same_session_other_activity_not_inherited(ledger):
    first = usage(wi=None, identity="a", at=AT)
    second = usage(wi=None, identity="b", at=AT)
    ledger.ingest_receipts([first, second, link([first])])
    result = query(ledger)
    assert result["summary"]["usage"]["known_total_tokens"] == 200
    assert result["summary"]["usage"]["unassigned_known_total_tokens"] == 100
    assert query(ledger, work_items=(WI,))["summary"]["usage"]["known_total_tokens"] == 100


def test_duplicate_observation_and_restart_do_not_add_output_or_tokens(ledger):
    o, u = contribution(), usage(wi=None, identity="u", at=AT)
    rows = [o, u, link([o, u])]
    ledger.ingest_receipts(rows)
    reopened = UsageLedger(ledger.root)
    assert reopened.ingest_receipts(rows)["inserted"] == 0
    r = query(reopened, work_items=(WI,))
    assert r["stages"]["outputs"]["cumulative"]["object_count"] == 1
    assert r["summary"]["usage"]["known_total_tokens"] == 100


def test_atomic_save_failure_then_same_batch_retry(ledger):
    rows = [goal(), contribution()]
    with patch("pulse_compute_cost.ledger.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(LedgerError):
            ledger.ingest_receipts(rows)
    assert ledger.receipt_snapshot()["facts"] == []
    assert ledger.ingest_receipts(rows)["inserted"] == 2
    assert ledger.ingest_receipts(rows)["inserted"] == 0


def test_new_improved_reused_and_versions_not_four_new_objects(ledger):
    original = contribution()
    improved = contribution("improvement", change="improved", version=reference("version", "v2"), at=LATER)
    reused = contribution("reuse", change="reused", at=LATER)
    revision = contribution(version=reference("version", "v1-corrected"), supersedes=original.fact_ref)
    ledger.ingest_receipts([original, improved, reused, revision])
    outputs = query(ledger)["stages"]["outputs"]
    assert outputs["cumulative"]["object_count"] == 1
    assert {r["contribution"]: r["object_count"] for r in outputs["formed"]} == {
        "new": 1, "improved": 1, "reused": 1}
    assert len(outputs["items"]) == 3
    assert outputs["accepted"]["status"] == "unavailable"


def test_formed_window_not_late_wi_link_time(ledger):
    o = contribution()
    ledger.ingest_receipts([o, link([o])])
    first = query(ledger, from_date="2026-10-01", to_date="2026-10-02", scope="activity", timezone="UTC")
    later = query(ledger, from_date="2026-10-02", to_date="2026-10-03", scope="activity", timezone="UTC")
    assert first["stages"]["outputs"]["formed"][0]["object_count"] == 1
    assert later["stages"]["outputs"]["formed"] == []
    assert later["stages"]["outputs"]["cumulative"]["object_count"] == 1


def test_conflicting_membership_quarantines_without_losing_quantity(ledger):
    o, u = contribution(), usage(wi=None, identity="conflict", at=AT)
    ledger.ingest_receipts([o, u, link([o, u]), link([o, u], OTHER, "other")])
    result = query(ledger)
    assert result["summary"]["usage"]["known_total_tokens"] == 100
    assert result["stages"]["outputs"]["items"][0]["wi"] == "ambiguous"
    assert result["stages"]["membership_conflicts"] == sorted([o.entity_ref, u.entity_ref])
    for wi in (WI, OTHER):
        selected = query(ledger, work_items=(wi,))
        assert selected["stages"]["outputs"]["cumulative"]["object_count"] == 0
        assert selected["stages"]["membership_conflicts"]


def test_existing_runtime_assignment_is_not_overridden_by_stage_link(ledger):
    u = usage(wi=OTHER, identity="already-assigned", at=AT)
    ledger.ingest_receipts([u, link([u])])
    r = query(ledger)
    assert r["summary"]["usage"]["known_total_tokens"] == 100
    assert u.entity_ref in r["attribution_conflicts"]
    assert r["stages"]["membership_conflicts"] == [u.entity_ref]


def test_shared_without_allocation_is_not_evenly_split(ledger):
    u = usage(wi=None, identity="shared", at=AT)
    ledger.ingest_receipts([u, link([u], wi=None, state="shared")])
    r = query(ledger)
    assert r["summary"]["usage"]["known_total_tokens"] == 100
    assert r["stages"]["shared_refs"] == [u.entity_ref]
    assert query(ledger, work_items=(WI,))["stages"]["outputs"]["items"] == []


def test_incompatible_quantities_are_not_summed_by_version(ledger):
    a = contribution("part-a", change="improved")
    b = contribution("part-b", change="improved", payload_changes={"quantity": "2"})
    ledger.ingest_receipts([a, b])
    group = query(ledger)["stages"]["outputs"]["formed"][0]
    assert group["object_count"] == 1
    assert group["quantity"] is None
    assert group["unresolved_object_refs"] == [OBJECT]


def test_goal_is_context_not_another_output_and_missing_baseline_not_zero(ledger):
    ledger.ingest_receipts([goal()])
    r = query(ledger)["stages"]
    assert r["outputs"]["cumulative"]["object_count"] == 0
    assert r["goals"][0]["expected"][0]["baseline"] is None


def test_correction_history_and_scope_are_preserved(ledger):
    o = contribution()
    ledger.ingest_receipts([o, link([o])])
    old = query(ledger, watermark=1)
    prior = ledger.receipt_snapshot()["facts"][-1]
    ledger.ingest_receipts([link([o], OTHER, supersedes=prior.fact_ref)])
    assert query(ledger, work_items=(WI,))["stages"]["outputs"]["items"] == []
    assert query(ledger, work_items=(OTHER,))["stages"]["outputs"]["items"][0]["occurred_at"] == AT
    assert query(ledger, watermark=old["watermark"]) == old


@pytest.mark.parametrize("changes", [
    {"wi": WI}, {"quantity": "NaN"}, {"quantity": "-1"}, {"quantity": 1},
    {"epistemic": "human_confirmed"}, {"activity_refs": []},
    {"version_ref": None}, {"object_ref": "a-local-path"},
    {"contribution": "accepted"}, {"extent": ""}, {"name": "x" * 257},
    {"owner": "pulse"}, {"execution_grant": True},
])
def test_unsafe_or_authority_shaped_semantics_rejected(changes):
    with pytest.raises(ReceiptError):
        contribution(payload_changes=changes)


def test_participation_estimate_and_observation_do_not_become_additive_time(ledger):
    payload = dict(wi=None, context_ref=CONTEXT, activity_refs=[ACTIVITY],
        purpose="correction", secondary_purposes=["reading"], basis="已知输入部分",
        active_seconds={"measured": None, "self_reported": None, "estimated": [30, 90]},
        estimator="stage.v1", configuration_ref=reference("configuration", "synthetic"),
        unobserved_scope=["线下资料查找未知"], epistemic="agent_judgment")
    p = fact("stage_participation", "participation", payload)
    ledger.ingest_receipts([p])
    result = query(ledger)["stages"]["participation"]
    assert result["items"][0]["active_seconds"]["estimated"] == [30, 90]
    assert result["total_active_seconds"] is None
    assert result["activity_count"] == 1
    unsafe = deepcopy(payload)
    unsafe["active_seconds"]["measured"] = [30, 90]
    with pytest.raises(ReceiptError):
        fact("stage_participation", "unsafe", unsafe)
    for bounds in ([90, 30], [-1, 5], [True, 5], [0, float("inf")]):
        unsafe = deepcopy(payload)
        unsafe["active_seconds"]["estimated"] = bounds
        with pytest.raises(ReceiptError):
            fact("stage_participation", "unsafe", unsafe)


def test_query_filter_does_not_leak_unscoped_semantics(ledger):
    ledger.ingest_receipts([contribution()])
    for kwargs in ({"agent": "pulse"}, {"runtime": "pi"}, {"tenant": "tenant-other"}):
        r = query(ledger, **kwargs)["stages"]
        assert r["status"] == "unsupported_filter"
        assert r["outputs"]["items"] == []


def test_cross_context_link_is_conflict_not_last_writer_wins(ledger):
    o = contribution()
    l = link([o])
    bad = fact("stage_link", "bad-context", {**l.payload, "context_ref": reference("context", "other")})
    ledger.ingest_receipts([o, bad])
    r = query(ledger)["stages"]
    assert r["outputs"]["items"][0]["wi"] == "ambiguous"
    assert r["membership_conflicts"] == [o.entity_ref]


def test_pending_link_resolves_only_when_original_target_arrives(ledger):
    u = usage(wi=None, identity="arrives-later", at=AT)
    ledger.ingest_receipts([link([u])])
    assert query(ledger)["stages"]["unresolved_link_targets"] == [u.entity_ref]
    ledger.ingest_receipts([u])
    assert query(ledger)["stages"]["unresolved_link_targets"] == []
    assert query(ledger, work_items=(WI,))["summary"]["usage"]["known_total_tokens"] == 100


def test_future_objects_not_in_earlier_cumulative_and_mixed_units_not_added(ledger):
    first = contribution()
    future = contribution("future", at="2026-11-01T01:00:00Z", payload_changes={
        "object_ref": reference("object", "future"), "unit": "document"})
    ledger.ingest_receipts([first, future])
    assert len(query(ledger)["stages"]["outputs"]["cumulative"]["groups"]) == 2
    r = query(ledger, from_date="2026-10-01", to_date="2026-10-03", scope="activity", timezone="UTC")
    assert r["stages"]["outputs"]["cumulative"]["object_count"] == 1


def test_unknown_and_zero_output_quantities_stay_distinct(ledger):
    a = contribution("unknown", payload_changes={"quantity": None})
    b = contribution("zero", payload_changes={"quantity": "0", "object_ref": reference("object", "zero")})
    ledger.ingest_receipts([a, b])
    r = query(ledger)["stages"]["outputs"]["formed"][0]
    assert r["quantity"] is None
    assert r["known_quantity"] == "0"
    assert r["unresolved_object_refs"] == [OBJECT]


def test_concurrent_stage_heads_remain_visible_conflicts(ledger):
    a = contribution()
    b = contribution(payload_changes={"extent": "与原声明不同"})
    ledger.ingest_receipts([a, b])
    r = query(ledger)["stages"]
    assert r["revision_conflicts"] == [a.entity_ref]
    assert r["status"] == "conflict"
    assert r["outputs"]["items"] == []


def test_aggregate_membership_is_not_a_whole_session_shortcut(ledger):
    u = usage(wi=None, identity="aggregate", at=AT, granularity="session_counter")
    ledger.ingest_receipts([u, link([u])])
    assert query(ledger)["stages"]["membership_conflicts"] == [u.entity_ref]


@pytest.mark.parametrize("field", ["epistemic", "contribution", "category", "unit"])
def test_malformed_semantics_fail_as_receipt_errors(field):
    with pytest.raises(ReceiptError):
        contribution(payload_changes={field: {}})


def test_feedback_never_manufactures_human_acceptance(ledger):
    o = contribution()
    payload = dict(wi=None, context_ref=CONTEXT, activity_refs=[ACTIVITY],
        object_ref=OBJECT, version_ref=VERSION, reason="合成缺陷", change="需修正遗漏",
        category="correction", epistemic="agent_judgment")
    f = fact("stage_feedback", "feedback", payload)
    ledger.ingest_receipts([o, f])
    result = query(ledger)["stages"]
    assert result["feedback"][0]["version_ref"] == VERSION
    assert result["outputs"]["accepted"]["status"] == "unavailable"
    with pytest.raises(ReceiptError):
        fact("stage_feedback", "approval", {**payload, "category": "human_accepted"})
