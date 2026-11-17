"""Accepted completions in the native core's stats facts reach the real query."""
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.kernel_outcomes import PRODUCER, completion_receipt, read_kernel_outcomes
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.receipts import Receipt, ReceiptError
from pulse_compute_cost.source_layout import source_detail

WI = "wi_20261018_052"
AT = "2026-10-19T06:03:05.835015+00:00"
NOW = datetime(2026, 10, 19, 7, tzinfo=timezone.utc)
OBSERVED = "2026-10-19T07:00:00Z"


def completion(**changes):
    return {"revision": 22, "at": AT, "actor": "aki", "profile": "lightweight",
            "title": "日推与伴读", "candidate": "c" * 64, "artifacts": "a" * 64,
            "acceptance": "d" * 64, "verification": "human", **changes}


def facts(*items, **changes):
    return {"schema": "harness.stats_facts/v1", "generated_at": NOW.isoformat(), "namespace": "pulse-production",
            "items": list(items), "collaborations": [], "unavailable_items": [], "coverage": "complete",
            "source": "journal", "read_only": True, **changes}


def item(wi=WI, completions=None, **changes):
    row = {"id": wi, "title": "日推与伴读", "owner": "aki", "status": "completed"}
    if completions is not None:
        row["completions"] = completions
    return {**row, **changes}


class KernelOutcomeTests(unittest.TestCase):
    def test_completion_is_one_accepted_delivery_with_a_node_independent_identity(self):
        row = completion_receipt("pulse-production", item(), completion(), OBSERVED)
        later = completion_receipt("pulse-production", item(), completion(), "2026-10-19T08:00:00Z")
        self.assertEqual(row.fact_ref, later.fact_ref)
        self.assertEqual((row.kind, row.producer, row.occurred_at), ("delivery", PRODUCER, "2026-10-19T06:03:05.835015Z"))
        p = row.payload
        self.assertEqual((p["wi"], p["responsible"], p["status"], p["accepted_at"]),
                         (WI, "aki", "accepted", row.occurred_at))
        self.assertEqual(p["outcome_context"], {"schema": "recorded_wi_outcome/v1", "title": "日推与伴读",
                                                "summary": None, "verification_level": "human"})
        self.assertEqual(Receipt.from_dict(row.to_dict()), row)
        # A later completion of the same WorkItem is another Outcome.
        again = completion_receipt("pulse-production", item(), completion(revision=40), OBSERVED)
        self.assertNotEqual(again.entity_ref, row.entity_ref)

    def test_child_review_completion_is_an_agent_review_level(self):
        row = completion_receipt("pulse-production", item(), completion(verification="agent_review"), OBSERVED)
        self.assertEqual(row.payload["outcome_context"]["verification_level"], "self_with_controls")
        self.assertEqual(Receipt.from_dict(row.to_dict()), row)
        for unknown in ("self_with_controls", "agent", None):
            with self.subTest(unknown=unknown), self.assertRaises(ReceiptError):
                completion_receipt("pulse-production", item(), completion(verification=unknown), OBSERVED)

    def test_incomplete_or_future_completions_are_rejected(self):
        for changes in ({"candidate": "x"}, {"verification": "self"}, {"revision": 0},
                        {"at": "2026-10-19T07:01:00Z"}, {"actor": 7}):
            with self.subTest(changes=changes), self.assertRaises(ReceiptError):
                completion_receipt("pulse-production", item(), completion(**changes), OBSERVED)

    def test_coverage_states_what_the_export_proves(self):
        def read(document):
            rows, detail = [], source_detail({"id": "core", "kind": "harness_core"})
            data = json.dumps(document).encode()
            return read_kernel_outcomes(Path("facts.json"), data, OBSERVED, rows, detail), rows, detail
        status, rows, _ = read(facts(item(completions=[completion()]), item("wi_20261020_210", completions=[])))
        self.assertEqual((status, len(rows)), (("complete", None, 2, 2), 1))
        # An older core exports no list: absence is not a confirmed zero.
        status, rows, _ = read(facts(item()))
        self.assertEqual((status, rows), (("unknown", "core_completions_not_exported", 1, 0), []))
        status, rows, detail = read(facts(item(completions=[completion(candidate="x")]),
                                          item("wi_20261020_210", completions=[completion()])))
        self.assertEqual((status, len(rows), detail["files_invalid"]), (("partial", "core_completions_invalid", 2, 1), 1, 1))
        status, _, _ = read(facts(item(completions=[]), unavailable_items=[{"wi": "wi_20261020_211", "code": "x"}],
                                  coverage="partial"))
        self.assertEqual(status[:2], ("partial", "core_items_unavailable"))
        self.assertEqual(read(facts())[0], ("idle", None, 0, 0))
        with self.assertRaises(ReceiptError):
            read(facts(item(completions=[]), read_only=False))

    def test_registered_core_source_through_collector_and_query(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "wi-facts-v1.json"
            source.write_text(json.dumps(facts(item(completions=[completion()]))))
            ledger = UsageLedger(root / "ledger", clock=lambda: NOW)
            registry = dict(schema="computecost_source_registry/v1", ledger_root=str(ledger.root),
                            sources=[dict(id="harness-core-outcomes", kind="harness_core", path=str(source))])
            result = collect_registered_sources(registry, ledger)
            self.assertEqual((result["sources"][0]["status"], result["sources"][0]["reason"]), ("complete", None))
            first = ledger.receipt_snapshot()
            [coverage] = [r for r in first["facts"] if r.kind == "coverage"]
            self.assertEqual((coverage.payload["domain"], coverage.payload["wi"], coverage.payload["status"],
                              coverage.payload["expected"], coverage.payload["observed"]), ("quality", None, "complete", 1, 1))
            q = CollaborationQuery(scope="activity", from_date="2026-10-19", to_date="2026-10-20")
            outcomes = query_collaboration(ledger, q)["summary"]["outcomes"]
            self.assertEqual((outcomes["status"], outcomes["count"]), ("observed", 1))
            self.assertEqual(outcomes["items"][0]["title"], "日推与伴读")
            # Rereading the same export appends nothing new and keeps one Outcome.
            collect_registered_sources(registry, ledger)
            facts_again = ledger.receipt_snapshot()["facts"]
            self.assertEqual(sum(r.kind == "delivery" for r in facts_again), 1)
            self.assertEqual(query_collaboration(ledger, q)["summary"]["outcomes"]["count"], 1)
            source.unlink()
            failed = collect_registered_sources(registry, ledger)
            self.assertEqual(failed["sources"][0]["status"], "unreachable")
            self.assertEqual(query_collaboration(ledger, q)["summary"]["outcomes"]["count"], 1)

    def test_an_agent_reviewed_child_reaches_the_query_as_accepted_with_its_level(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "wi-facts-v1.json"
            source.write_text(json.dumps(facts(
                item(completions=[completion()]),
                item("wi_20261020_210", completions=[completion(verification="agent_review", revision=5)],
                     parent=WI))))
            ledger = UsageLedger(root / "ledger", clock=lambda: NOW)
            registry = dict(schema="computecost_source_registry/v1", ledger_root=str(ledger.root),
                            sources=[dict(id="harness-core-outcomes", kind="harness_core", path=str(source))])
            collect_registered_sources(registry, ledger)
            q = CollaborationQuery(scope="activity", from_date="2026-10-19", to_date="2026-10-20")
            items = query_collaboration(ledger, q)["summary"]["outcomes"]["items"]
            # Accepted and counted, but its level says an Agent reviewed it, not a human.
            self.assertEqual({i["wi"]: (i["status"], i["counted"], i["verification_level"]) for i in items},
                             {WI: ("accepted", True, "human"),
                              "wi_20261020_210": ("accepted", True, "self_with_controls")})


if __name__ == "__main__":
    unittest.main()
