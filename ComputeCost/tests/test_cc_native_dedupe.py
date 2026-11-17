"""CC native transcript rows next to Host observations: one bill, no session quarantine."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration, reconcile_usage
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.receipts import Receipt, reference
from test_collaboration_receipts import CATALOG

SESSION = "telegram-main"
SCOPE = reference("native_session", ["cc", SESSION])
MODEL = "openai/test"
START, END = "2026-09-13T01:00:00Z", "2026-09-13T01:05:00Z"


def counters(output):
    return dict(input_tokens=2, output_tokens=output, cache_read_input_tokens=100, cache_creation_input_tokens=10)


def execution(key="attempt", start=START):
    return Receipt.make("execution", key, {"wi": None, "executor": "pulse", "responsible": None,
        "delegator": None, "runtime": "cc", "assignment_ref": None, "parent_ref": None,
        "state": "completed", "expected_usage": True}, occurred_at=start, observed_at=END,
        producer="relay_host.v1", evidence=(reference("host", key),))


def native(message, at, output, *, final=True, supersedes=None):
    row = usage_receipt("cc", [SESSION, message], counters(output), at=at, observed=at, model=MODEL,
                        final=final, mapping="cc_message.v1", native_scope_ref=SCOPE)
    if supersedes is None:
        return row
    return Receipt.make("usage", None, row.payload, entity_ref=row.entity_ref, occurred_at=at,
                        observed_at=at, producer=row.producer, supersedes=supersedes)


def host_message(message, ex, at, output):
    """The Host's streaming snapshot of the same message: identity, interval, final=False."""
    entity = native(message, at, output).entity_ref
    return usage_receipt("cc", ["host", message], counters(output), at=at, observed=at, model=MODEL,
        execution_ref=ex.entity_ref, coverage_ref=reference("coverage", [ex.entity_ref, MODEL]),
        common={"wi": None, "executor": "pulse", "responsible": None, "delegator": None},
        interval=(ex.occurred_at, at), final=False, mapping="cc_message.v1",
        entity_ref=entity, native_scope_ref=SCOPE)


def host_result(ex, *, start=START, end=END, output=999_999):
    """A covers-less Host turn result; its counter may be session-cumulative."""
    cumulative = dict(input_tokens=output, output_tokens=output, cache_read_input_tokens=output,
                      cache_creation_input_tokens=output)
    return usage_receipt("cc", ["result", ex.entity_ref], cumulative, at=end, observed=end,
        model=MODEL, execution_ref=ex.entity_ref,
        coverage_ref=reference("coverage", [ex.entity_ref, MODEL]),
        common={"wi": None, "executor": "pulse", "responsible": None, "delegator": None},
        granularity="turn_aggregate", covers=[], interval=(start, end), final=True,
        mapping="cc_sdk_result.v1", native_scope_ref=SCOPE)


def total(output):
    return 2 + output + 100 + 10


class HostNativeMessageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = UsageLedger(Path(self.tmp.name) / "ledger")

    def tearDown(self):
        self.tmp.cleanup()

    def query(self, rows, **kwargs):
        self.ledger.ingest_receipts(rows)
        return query_collaboration(self.ledger, CollaborationQuery(scope="activity", from_date="2026-09-13",
            to_date="2026-09-14", timezone="UTC", **kwargs), catalog=CATALOG, catalog_observed_at=END)

    def test_settled_native_counter_refines_host_stream_snapshot(self):
        ex = execution()
        host = host_message("m1", ex, "2026-09-13T01:00:02Z", 8)
        settled = native("m1", "2026-09-13T01:00:01Z", 241)
        out = self.query([ex, host, settled], group_by=("agent",))
        usage = out["summary"]["usage"]
        self.assertEqual(usage["event_count"], 1)
        self.assertEqual(usage["known_total_tokens"], total(241))
        self.assertEqual(usage["conflicted_event_count"], 0)
        self.assertEqual(out["revision_conflicts"], [])
        self.assertEqual(out["reconciliation"]["observation_groups"][host.entity_ref],
                         sorted([host.fact_ref, settled.fact_ref]))
        # Host identity is kept, not invented: the one event belongs to the Host executor.
        self.assertEqual([g["key"]["agent"] for g in out["groups"]], ["pulse"])

    def test_forked_identical_native_heads_and_host_resolve_once(self):
        ex = execution()
        host = host_message("m1", ex, "2026-09-13T01:00:02Z", 8)
        partial = native("m1", "2026-09-13T01:00:01Z", 5, final=False)
        left = native("m1", "2026-09-13T01:00:01Z", 241)
        right = native("m1", "2026-09-13T01:00:01.500Z", 241, supersedes=partial.fact_ref)
        out = self.query([ex, host, partial, left, right])
        self.assertEqual(out["summary"]["usage"]["event_count"], 1)
        self.assertEqual(out["summary"]["usage"]["known_total_tokens"], total(241))
        self.assertEqual(out["revision_conflicts"], [])

    def test_partial_above_final_or_two_finals_remain_conflicts(self):
        ex = execution()
        larger_partial = host_message("m1", ex, "2026-09-13T01:00:02Z", 300)
        settled = native("m1", "2026-09-13T01:00:01Z", 241)
        other_final = native("m1", "2026-09-13T01:00:01.200Z", 242)
        out = self.query([ex, larger_partial, settled])
        self.assertIn(settled.entity_ref, out["revision_conflicts"])
        self.assertEqual(out["summary"]["usage"]["event_count"], 0)
        out = self.query([other_final])
        self.assertIn(settled.entity_ref, out["revision_conflicts"])


class HostResultOverNativeDetailTest(unittest.TestCase):
    def test_native_detail_wins_over_coverless_cc_result(self):
        ex = execution()
        inside = native("m-in", "2026-09-13T01:01:00Z", 40)
        earlier = native("m-old", "2026-09-13T00:30:00Z", 50)
        aggregate = host_result(ex)
        rows, result = reconcile_usage([inside, earlier, aggregate], executions=[ex])
        self.assertEqual({r.entity_ref for r in rows}, {inside.entity_ref, earlier.entity_ref})
        self.assertEqual(result["conflicting_refs"], [])
        self.assertIn(aggregate.entity_ref, result["suppressed_aggregate_refs"])

    def test_unfinished_native_detail_stays_provisional_under_its_result(self):
        ex = execution()
        streaming = native("m-in", "2026-09-13T01:01:00Z", 40, final=False)
        earlier = native("m-old", "2026-09-13T00:30:00Z", 50)
        aggregate = host_result(ex)
        rows, result = reconcile_usage([streaming, earlier, aggregate], executions=[ex])
        self.assertEqual({r.entity_ref for r in rows}, {streaming.entity_ref, earlier.entity_ref})
        self.assertEqual(result["conflicting_refs"], [])
        self.assertIn(aggregate.entity_ref, result["suppressed_aggregate_refs"])

    def test_result_above_unfinished_detail_counter_stays_conflict(self):
        ex = execution()
        streaming = native("m-in", "2026-09-13T01:01:00Z", 40, final=False)
        aggregate = host_result(ex, output=30)
        rows, result = reconcile_usage([streaming, aggregate], executions=[ex])
        self.assertEqual(rows, [])
        self.assertEqual(set(result["conflicting_refs"]), {streaming.entity_ref, aggregate.entity_ref})
        self.assertEqual(result["conflict_reasons"][aggregate.entity_ref], ["unproven_session_overlap"])

    def test_result_without_detail_in_its_interval_stays_unresolved_alone(self):
        ex = execution()
        earlier = native("m-old", "2026-09-13T00:30:00Z", 50)
        aggregate = host_result(ex)
        rows, result = reconcile_usage([earlier, aggregate], executions=[ex])
        self.assertEqual([r.entity_ref for r in rows], [earlier.entity_ref])
        self.assertEqual(result["conflicting_refs"], [aggregate.entity_ref])

    def test_cumulative_result_listing_native_messages_is_not_added(self):
        ex = execution()
        first = native("m1", "2026-09-13T01:01:00Z", 40)
        second = native("m2", "2026-09-13T01:02:00Z", 50)
        aggregate = Receipt.make("usage", None, {**host_result(ex).payload,
            "covers": [first.entity_ref, second.entity_ref]}, entity_ref=host_result(ex).entity_ref,
            occurred_at=END, observed_at=END, producer="cc_sdk_result.v1")
        rows, result = reconcile_usage([first, second, aggregate], executions=[ex])
        self.assertEqual({r.entity_ref for r in rows}, {first.entity_ref, second.entity_ref})
        self.assertEqual(result["conflicting_refs"], [])
        # Listed messages larger than the result contradict it.
        small = Receipt.make("usage", None, {**aggregate.payload, "tokens": {**aggregate.payload["tokens"], "output": 60}},
            entity_ref=aggregate.entity_ref, occurred_at=END, observed_at=END, producer="cc_sdk_result.v1")
        _, result = reconcile_usage([first, second, small], executions=[ex])
        self.assertIn(small.entity_ref, result["conflicting_refs"])
        # A listed message that only the Host observed is not native detail.
        host_only = host_message("m3", ex, "2026-09-13T01:03:00Z", 8)
        listed = Receipt.make("usage", None, {**aggregate.payload, "covers": [first.entity_ref, host_only.entity_ref]},
            entity_ref=aggregate.entity_ref, occurred_at=END, observed_at=END, producer="cc_sdk_result.v1")
        _, result = reconcile_usage([first, host_only, listed], executions=[ex])
        self.assertIn(listed.entity_ref, result["conflicting_refs"])

    def test_session_view_counts_native_detail_once(self):
        ex = execution()
        host = host_message("m-in", ex, "2026-09-13T01:01:01Z", 8)
        settled = native("m-in", "2026-09-13T01:01:00Z", 40)
        earlier = native("m-old", "2026-09-13T00:30:00Z", 50)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(Path(tmp) / "ledger")
            ledger.ingest_receipts([ex, host, settled, earlier, host_result(ex)])
            out = query_collaboration(ledger, CollaborationQuery(scope="activity", from_date="2026-09-13",
                to_date="2026-09-14", timezone="UTC", runtime="cc"), catalog=CATALOG, catalog_observed_at=END)
        usage = out["summary"]["usage"]
        self.assertEqual(usage["event_count"], 2)
        self.assertEqual(usage["known_total_tokens"], total(40) + total(50))
        self.assertEqual(usage["conflicted_event_count"], 0)


if __name__ == "__main__":
    unittest.main()
