"""PR513 query-layer counterexamples; all receipts are synthetic facts."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration, reconcile_usage
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, reference
from test_collaboration_receipts import (AT, CATALOG, OTHER, PERIOD, WI, complete, coverage,
                                         delivery, execution, fact, review, usage)


def query(rows, **kwargs):
    with tempfile.TemporaryDirectory() as root:
        ledger = UsageLedger(Path(root))
        ledger.ingest_receipts(rows)
        return query_collaboration(ledger, CollaborationQuery(**kwargs), catalog=CATALOG,
                                   catalog_observed_at=AT)


class PR513QueryRegressions(unittest.TestCase):
    def test_coverage_is_a_per_selected_wi_denominator(self):
        rows = complete(WI) + [r for r in complete(OTHER)
                               if not (r.kind == "coverage" and r.payload["domain"] == "usage")]
        report = query(rows, work_items=(WI, OTHER), **PERIOD)
        self.assertIsNone(report["summary"]["cost"]["machine_cash_usd"])
        self.assertIn("source_coverage_unknown", report["summary"]["cost"]["reasons"])

    def test_agent_group_keeps_missing_expected_wi(self):
        rows = complete(WI) + [execution(OTHER, identity="missing-exec")]
        report = query(rows, work_items=(WI, OTHER), group_by=("agent",), **PERIOD)
        pulse = next(group for group in report["groups"] if group["key"] == {"agent": "pulse"})
        self.assertEqual(pulse["roi"]["cohort_count"], 2)
        self.assertEqual(pulse["roi"]["status"], "unavailable")

    def test_runtime_agent_bucket_preserves_execution_only_wi_and_unknown_model(self):
        rows = complete(WI) + [execution(OTHER, identity="missing-exec")]
        report = query(rows, work_items=(WI, OTHER), group_by=("agent", "runtime"), **PERIOD)
        bucket = next(group for group in report["groups"]
                      if group["key"] == {"agent": "pulse", "runtime": "cc"})
        self.assertEqual(bucket["roi"]["status"], "unavailable")
        self.assertEqual(bucket["roi"]["cohort_count"], 2)
        by_model = query(rows, work_items=(WI, OTHER), group_by=("agent", "runtime", "model"), **PERIOD)
        unknown = next(group for group in by_model["groups"]
                       if group["key"] == {"agent": "pulse", "runtime": "cc", "model": "unassigned"})
        self.assertEqual(unknown["work_items"], [OTHER])
        self.assertEqual(unknown["roi"]["status"], "unavailable")

    def test_idle_global_coverage_does_not_veto_a_wi_denominator(self):
        idle = fact("coverage", "idle-source", dict(domain="usage", wi=None,
                    source_ref=reference("source", "idle"), status="idle", expected=None,
                    observed=None, watermark=None, reason=None), at=None)
        report = query(complete() + [idle], work_items=(WI,), **PERIOD)
        self.assertEqual(report["summary"]["cost"]["machine_cash_usd"], "100")

    def test_independent_attribution_sources_that_agree_are_one_assignment(self):
        row = usage(identity="agreeing-attribution-target")
        target = row.entity_ref
        common = dict(target_ref=target, wi=WI, executor="pulse", responsible=None,
                      delegator=None, state="assigned", effective_at=AT)
        host = fact("attribution", "host-agrees", {**common, "policy":"scl_host_session_binding.v1"})
        native = Receipt.make("attribution", "native-agrees", {
            **common, "policy":"scl_verified_native_boundary.v1",
            "assignment_ref":reference("assignment", "same-boundary")},
            occurred_at=AT, observed_at=AT, producer="native")
        report = query([row, host, native], work_items=(WI,),
                       group_by=("wi", "agent"), **PERIOD)
        self.assertEqual(report["attribution_conflicts"], [])
        self.assertEqual(report["groups"][0]["key"], {"wi":WI, "agent":"pulse"})
        self.assertNotIn("attribution_unknown", report["summary"]["cost"]["reasons"])

    def test_normal_attribution_conflict_propagates_to_all_candidate_wis(self):
        row = usage(identity="attribution-target")
        target = row.entity_ref
        common = dict(target_ref=target, executor="pulse", responsible="pulse", delegator=None,
                      state="assigned", policy="synthetic", effective_at=AT)
        left = fact("attribution", "conflicted-attribution", {**common, "wi": WI})
        right = fact("attribution", "conflicted-attribution", {**common, "wi": OTHER})
        report = query(complete() + [left, right], work_items=(WI,), **PERIOD)
        self.assertIn("attribution_unknown", report["summary"]["cost"]["reasons"])

    def test_human_coverage_is_per_wi_and_human_summary_conflicts_block_roi(self):
        human = dict(executor="pulse", responsible="pulse", delegator=None, execution_ref=None,
                     origin="human", category=None, band=None, focused_seconds=3600,
                     method="trusted_telemetry")
        global_coverage = fact("coverage", "global-human", dict(domain="human", wi=None,
                               source_ref=reference("source", "global-human"), status="complete",
                               expected=2, observed=2, watermark="1", reason=None))
        rows = complete(WI) + complete(OTHER) + [
            fact("human", "human-wi", {**human, "wi": WI}),
            fact("human", "human-other", {**human, "wi": OTHER}),
            coverage("human", WI), global_coverage,
        ]
        report = query(rows, work_items=(WI, OTHER), cost_basis="machine_plus_measured_human",
                       human_hourly_rate="10", human_rate_policy="synthetic", **PERIOD)
        self.assertIn("incomplete_measured_human_cost", report["summary"]["roi"]["reasons"])

        summary = fact("human_summary", "summary-conflict", dict(wi=WI, count=1,
                       bands={"micro": 1, "bounded": 0, "judgment": 0, "framing": 0},
                       focused_seconds=None, method="synthetic"))
        changed = Receipt.make("human_summary", None, {**summary.payload, "count": 2},
                               entity_ref=summary.entity_ref, occurred_at=summary.occurred_at,
                               observed_at=summary.observed_at, producer=summary.producer)
        report = query(complete() + [summary, changed], work_items=(WI,), **PERIOD)
        self.assertEqual(report["summary"]["roi"]["status"], "unavailable")
        self.assertIn("evidence_revision_conflict", report["summary"]["roi"]["reasons"])

    def test_human_conflict_and_pricing_null_do_not_calculate_roi(self):
        human = dict(wi=WI, executor="pulse", responsible="pulse", delegator=None,
                     execution_ref=None, origin="human", category=None, band=None,
                     method="trusted_telemetry")
        h1 = fact("human", "human-conflict", {**human, "focused_seconds": 3600})
        h2 = fact("human", "human-conflict", {**human, "focused_seconds": 7200})
        report = query(complete() + [h1, h2, coverage("human", expected=1, observed=1)],
                       work_items=(WI,), cost_basis="machine_plus_measured_human",
                       human_hourly_rate="10", human_rate_policy="synthetic", **PERIOD)
        self.assertEqual(report["summary"]["roi"]["status"], "unavailable")
        null_catalog = {"data": [{"id": "openai/test", "pricing": None}]}
        with tempfile.TemporaryDirectory() as root:
            ledger = UsageLedger(Path(root)); ledger.ingest_receipts(complete())
            priced = query_collaboration(ledger, CollaborationQuery(work_items=(WI,), **PERIOD),
                                         catalog=null_catalog, catalog_observed_at=AT)
        self.assertIn("incomplete_price_or_quantity", priced["summary"]["cost"]["reasons"])

    def test_review_conflict_marks_quality_and_roi_unavailable(self):
        rows = complete()
        original = next(row for row in rows if row.kind == "review")
        conflict = Receipt.make("review", None, {**original.payload, "decision": "changes_requested"},
                                entity_ref=original.entity_ref, occurred_at=original.occurred_at,
                                observed_at=original.observed_at, producer=original.producer)
        report = query(rows + [conflict], work_items=(WI,), **PERIOD)
        self.assertEqual(report["summary"]["quality"]["status"], "conflict")
        self.assertEqual(report["summary"]["quality"]["conflicts"], 1)
        self.assertEqual(report["summary"]["quality"]["approved"]["denominator"], 1)
        self.assertEqual(report["summary"]["roi"]["status"], "unavailable")

    def test_cross_day_usage_links_prior_execution_and_review_uses_review_activity(self):
        previous = "2026-09-12T23:59:00Z"
        next_day = "2026-09-13T00:01:00Z"
        e = execution(identity="prior-execution")
        e = Receipt.make(e.kind, None, e.payload, entity_ref=e.entity_ref, occurred_at=previous,
                         observed_at=AT, producer=e.producer)
        u = usage(identity="next-day-usage", ex=e, at=next_day)
        d = delivery(at=previous)
        r = review(d); r = Receipt.make(r.kind, None, r.payload, entity_ref=r.entity_ref,
                                        occurred_at=next_day, observed_at=AT, producer=r.producer)
        report = query([e, u, coverage("usage"), coverage("execution"), coverage("value"), d, r],
                       work_items=(WI,), scope="activity", from_date="2026-09-13", to_date="2026-09-14",
                       timezone="UTC", value_from=PERIOD["value_from"], value_to=PERIOD["value_to"])
        self.assertNotIn("execution_link_unknown", report["summary"]["cost"]["reasons"])
        self.assertEqual(report["summary"]["quality"]["review_activity_count"], 1)
        grouped = query([e, u, coverage("usage"), coverage("execution"), coverage("value"), d, r],
                        work_items=(WI,), scope="activity", from_date="2026-09-13", to_date="2026-09-14",
                        timezone="UTC", group_by=("day",), value_from=PERIOD["value_from"],
                        value_to=PERIOD["value_to"])
        self.assertEqual(grouped["groups"][0]["quality"]["review_activity_count"], 1)
        self.assertEqual(grouped["groups"][0]["quality"]["delivery_count"], 1)

    def test_native_scope_supplements_legacy_observation_but_distinct_scopes_conflict(self):
        rows = complete()
        original = next(row for row in rows if row.kind == "usage")
        legacy_payload = dict(original.payload)
        del legacy_payload["native_scope_ref"]
        legacy = Receipt.make("usage", None, legacy_payload, entity_ref=original.entity_ref,
                              occurred_at=original.occurred_at, observed_at=original.observed_at,
                              producer=original.producer)
        def scoped(value):
            return Receipt.make("usage", None, {**original.payload,
                                "native_scope_ref": reference("scope", value)},
                                entity_ref=original.entity_ref, occurred_at=original.occurred_at,
                                observed_at=original.observed_at, producer=original.producer)
        merged = query([r for r in rows if r.kind != "usage"] + [legacy, scoped("one")], work_items=(WI,))
        self.assertNotIn(original.entity_ref, merged["revision_conflicts"])
        self.assertEqual(merged["summary"]["usage"]["event_count"], 1)
        conflicted = query([r for r in rows if r.kind != "usage"] + [scoped("one"), scoped("two")],
                           work_items=(WI,))
        self.assertIn(original.entity_ref, conflicted["revision_conflicts"])
        self.assertIsNone(conflicted["summary"]["cost"]["machine_cash_usd"])

    def test_native_scope_overlap_and_distinct_scopes_without_cover_are_unresolved(self):
        e = execution()
        detail = usage(identity="detail", ex=e)
        aggregate = usage(identity="aggregate", ex=e, granularity="turn_aggregate")
        def scope(row, value):
            return Receipt.make(row.kind, None, {**row.payload, "native_scope_ref": reference("scope", value)},
                                entity_ref=row.entity_ref, occurred_at=row.occurred_at,
                                observed_at=row.observed_at, producer=row.producer)
        kept, diag = reconcile_usage([scope(detail, "same"), scope(aggregate, "same")])
        self.assertEqual(kept, [])
        self.assertEqual(diag["status"], "conflict")
        kept, diag = reconcile_usage([scope(detail, "one"), scope(aggregate, "two")])
        self.assertEqual(kept, [])
        self.assertEqual(diag["status"], "conflict")

    def test_same_scope_cross_producer_aggregates_conflict_but_same_producer_turns_do_not(self):
        e = execution()
        left = usage(identity="host", ex=e, granularity="turn_aggregate")
        right = usage(identity="rollout", ex=e, granularity="turn_aggregate")
        scope = reference("scope", "codex-turn")
        left = Receipt.make(left.kind, None, {**left.payload, "native_scope_ref": scope},
                            entity_ref=left.entity_ref, occurred_at=left.occurred_at,
                            observed_at=left.observed_at, producer="codex_host.v1")
        right = Receipt.make(right.kind, None, {**right.payload, "native_scope_ref": scope},
                             entity_ref=right.entity_ref, occurred_at=right.occurred_at,
                             observed_at=right.observed_at, producer="codex_rollout.v1")
        kept, diag = reconcile_usage([left, right])
        self.assertEqual(kept, [])
        self.assertEqual(diag["status"], "conflict")
        same_source = Receipt.make(right.kind, None, right.payload, entity_ref=right.entity_ref,
                                   occurred_at=right.occurred_at, observed_at=right.observed_at,
                                   producer="codex_host.v1")
        kept, diag = reconcile_usage([left, same_source])
        self.assertEqual(len(kept), 2)
        self.assertEqual(diag["status"], "reconciled")

    def test_invalid_cli_query_does_not_collect(self):
        # The direct helper is intentionally tested: parser errors are already
        # rejected by argparse, while this protects semantic validation.
        from pulse_compute_cost import cli
        args = cli._parser().parse_args(["query", "--collaboration", "--ledger-root", "/tmp/no-ledger",
                                         "--from", "2026-09-14", "--to", "2026-09-13"])
        with patch.object(cli, "collect_registered_sources") as collect:
            with self.assertRaises(Exception):
                cli._collaboration_query(args)
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
