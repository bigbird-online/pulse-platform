"""agy transcript usage: reconciled to the transcript, unknown kept unknown. Fixtures from wi666 N01."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from pulse_compute_cost import agy_usage
from pulse_compute_cost.collaboration_query import (
    CollaborationQuery, _value_cost, query_collaboration, reconcile_usage)
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources, usage_receipt
from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.runtime_binding import build_runtime_usage_binding

FIXTURES = Path(__file__).parent / "fixtures/agy"
RESUMED = "8f0067ec-b62d-4ecf-bd95-04cae57649ac"  # two turns, two models
TOOL = "894ff93b-0000-4000-8000-000000000000"  # tool call with cache reads
WI = "wi_20261120_943"
OBSERVED = "2026-11-20T12:00:00Z"


def planner_steps(path: Path) -> list[dict]:
    """The transcript's own model steps, read independently of the adapter."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [row for row in rows if row["type"] == "PLANNER_RESPONSE"]


class BrainCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.brain = self.root / "brain"

    def place(self, fixture: str, conversation: str) -> Path:
        path = self.brain / conversation / agy_usage.TRANSCRIPT
        path.parent.mkdir(parents=True)
        shutil.copyfile(FIXTURES / fixture, path)
        # agy writes the same rows to transcript.jsonl; it is never a second source.
        shutil.copyfile(FIXTURES / fixture, path.parent / "transcript.jsonl")
        return path


class AgyTranscriptTest(BrainCase):
    def test_every_model_step_reconciles_with_the_transcript(self):
        for fixture, conversation in (("c-transcript_full-8f0067ec.jsonl", RESUMED),
                                      ("c-transcript_full-g02-894ff93b.jsonl", TOOL)):
            path = self.place(fixture, conversation)
            rows = agy_usage.read_agy(path, OBSERVED)
            steps = planner_steps(path)
            self.assertEqual(len(rows), len(steps))
            for row, step in zip(rows, steps):
                p = row.payload
                self.assertEqual(p["runtime"], "agy")
                self.assertEqual(p["mapping"], agy_usage.TRANSCRIPT_MAPPING)
                self.assertEqual(p["granularity"], "request")
                self.assertEqual(p["model"], step["model"])
                self.assertEqual(row.occurred_at, step["created_at"])
                self.assertEqual(p["native_scope_ref"], reference("native_session", ["agy", conversation]))
                self.assertEqual(p["tokens"], {"uncached_input": step["input_tokens"],
                    "cache_read": step["cache_read_tokens"], "cache_write": 0,
                    "output": step["output_tokens"]})
                self.assertTrue(p["final"])
            self.assertEqual(agy_usage.transcript_files(self.brain).count(path), 1)
        self.assertEqual(len(agy_usage.transcript_files(self.brain)), 2)
        # The resumed conversation records each step's own model.
        resumed = agy_usage.read_agy(self.brain / RESUMED / agy_usage.TRANSCRIPT, OBSERVED)
        self.assertEqual([r.payload["model"] for r in resumed], ["gemini-3.8-flash-low", "gemini-4-neon-high"])
        tool = agy_usage.read_agy(self.brain / TOOL / agy_usage.TRANSCRIPT, OBSERVED)
        self.assertEqual(sum(r.payload["tokens"]["cache_read"] for r in tool), 7767)

    def test_missing_token_fields_stay_unknown_and_agy_is_never_priced(self):
        path = self.place("c-transcript_full-8f0067ec.jsonl", RESUMED)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        del rows[1]["output_tokens"]
        rows[4]["cache_read_tokens"] = "n/a"
        torn = json.dumps({"step_index": 5, "type": "PLANNER_RESPONSE", "model": "x", "input_tokens": 1})
        path.write_text("".join(json.dumps(r) + "\n" for r in rows) + torn[:20])
        first, second = agy_usage.read_agy(path, OBSERVED)  # the torn line is the next collection's
        self.assertEqual(first.payload["tokens"],
                         {"uncached_input": 9766, "cache_read": 0, "cache_write": 0, "output": None})
        self.assertEqual(second.payload["tokens"]["cache_read"], None)
        self.assertEqual(_value_cost(first, None), (None, "unknown_token_quantity"))
        # Even a catalog price for the same model name is not agy's cost.
        catalog = {"data": [{"id": "gemini-4-neon-high", "pricing": {"prompt": "1", "completion": "1",
                   "input_cache_read": "1", "input_cache_write": "1"}}]}
        complete = agy_usage.read_agy(self.place("c-transcript_full-g02-894ff93b.jsonl", TOOL), OBSERVED)[0]
        self.assertEqual(_value_cost(complete, catalog), (None, agy_usage.UNPRICED_REASON))
        no_step = {"type": "PLANNER_RESPONSE", "model": "m"}
        path.write_text(json.dumps(no_step) + "\n")
        with self.assertRaises(ValueError):
            agy_usage.read_agy(path, OBSERVED)

    def test_cache_write_is_zero_only_for_gemini(self):
        path = self.brain / TOOL / agy_usage.TRANSCRIPT
        path.parent.mkdir(parents=True)
        step = {"type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-11-20T10:00:05Z",
                "input_tokens": 10, "cache_read_tokens": 4, "output_tokens": 2}
        models = ["gemini-4-neon-high", "claude-opus-5-5-high", "gpt-oss-120b-medium", None]
        path.write_text("".join(json.dumps({**step, "step_index": n, **({"model": m} if m else {})}) + "\n"
                                for n, m in enumerate(models)))
        gemini, claude, gpt, unknown = agy_usage.read_agy(path, OBSERVED)
        # ACP reports Gemini's cachedWriteTokens as 0; the CLI reports no class at all.
        self.assertEqual(gemini.payload["tokens"],
                         {"uncached_input": 10, "cache_read": 4, "cache_write": 0, "output": 2})
        for row in (claude, gpt, unknown):
            self.assertIsNone(row.payload["tokens"]["cache_write"])
            self.assertEqual(row.payload["tokens"]["uncached_input"], 10)
            self.assertEqual(_value_cost(row, None), (None, "unknown_token_quantity"))

    def test_claude_step_usage_is_not_shown_as_known(self):
        path = self.brain / TOOL / agy_usage.TRANSCRIPT
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE",
            "created_at": "2026-11-20T10:00:05Z", "model": "claude-opus-5-5-high",
            "input_tokens": 10, "cache_read_tokens": 0, "output_tokens": 2}) + "\n")
        ledger = UsageLedger(self.root / "ledger")
        registry = {"schema": "computecost_source_registry/v1",
                    "sources": [{"id": "native-agy", "kind": "agy", "path": str(self.brain)}]}
        collect_registered_sources(registry, ledger)
        summary = query_collaboration(ledger, CollaborationQuery(scope="wi_lifetime"))["summary"]
        usage = summary["usage"]
        self.assertEqual(usage["event_count"], 1)
        # The step's cache writes are unknown: not a known 0, and not a complete usage.
        self.assertEqual(usage["tokens"]["cache_write"], {"known": None, "unknown_event_count": 1})
        self.assertEqual(usage["tokens"]["uncached_input"], {"known": 10, "unknown_event_count": 0})
        self.assertEqual({u["reason"] for u in summary["cost"]["unpriced"]}, {"unknown_token_quantity"})

    def test_conversation_comes_only_from_the_brain_layout(self):
        with self.assertRaises(ValueError):
            agy_usage.conversation_id(self.root / "transcript_full.jsonl")
        with self.assertRaises(ValueError):
            agy_usage.conversation_id(self.root / "bad id" / agy_usage.TRANSCRIPT)
        self.assertEqual(agy_usage.conversation_id(self.root / RESUMED / agy_usage.TRANSCRIPT), RESUMED)

    def test_a_conversation_linked_out_of_the_brain_root_is_refused(self):
        outside = self.root / "elsewhere" / RESUMED
        (outside / agy_usage.TRANSCRIPT).parent.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "c-transcript_full-8f0067ec.jsonl", outside / agy_usage.TRANSCRIPT)
        self.brain.mkdir()
        (self.brain / RESUMED).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            agy_usage.transcript_files(self.brain)

    def test_a_bad_conversation_leaves_the_others_collected_and_the_source_partial(self):
        good = self.place("c-transcript_full-g02-894ff93b.jsonl", TOOL)
        expected = len(agy_usage.read_agy(good, OBSERVED))
        registry = {"schema": "computecost_source_registry/v1",
                    "sources": [{"id": "native-agy", "kind": "agy", "path": str(self.brain)}]}
        outside = self.root / "elsewhere" / RESUMED
        (outside / agy_usage.TRANSCRIPT).parent.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "c-transcript_full-8f0067ec.jsonl", outside / agy_usage.TRANSCRIPT)
        bad = self.brain / RESUMED
        logs = bad / agy_usage.TRANSCRIPT.parent

        def in_place(make):
            logs.mkdir(parents=True, exist_ok=True)
            make(bad / agy_usage.TRANSCRIPT)

        cases = {  # name: (make the bad conversation, listed by enumeration)
            "redirected": (lambda: bad.symlink_to(outside, target_is_directory=True), False),
            "dangling": (lambda: in_place(lambda p: p.symlink_to(self.root / "gone")), False),
            "not_a_file": (lambda: in_place(lambda p: p.mkdir()), False),
            "read_error": (lambda: in_place(lambda p: (shutil.copyfile(outside / agy_usage.TRANSCRIPT, p),
                                                       p.chmod(0))), True),
        }
        for name, (make, listed) in cases.items():
            with self.subTest(name):
                make()
                rejected = []
                self.assertEqual(agy_usage.transcript_files(self.brain, rejected),
                                 sorted([good, bad / agy_usage.TRANSCRIPT]) if listed else [good])
                self.assertEqual(rejected, [] if listed else [bad / agy_usage.TRANSCRIPT])
                ledger = UsageLedger(self.root / ("ledger-" + name))
                source, = collect_registered_sources(registry, ledger)["sources"]
                # The healthy conversation is still collected; the source says it is incomplete.
                self.assertEqual(source["status"], "partial")
                self.assertEqual(source["reason"], "native_files_incomplete")
                usage = [r for r in ledger.receipt_snapshot()["facts"] if r.kind == "usage"]
                self.assertEqual(len(usage), expected)
            if bad.is_symlink():
                bad.unlink()
            else:
                shutil.rmtree(bad)

    def test_a_conversation_deleted_during_collection_leaves_the_others_collected(self):
        good = self.place("c-transcript_full-g02-894ff93b.jsonl", TOOL)
        gone = self.place("c-transcript_full-8f0067ec.jsonl", RESUMED)
        listed = agy_usage.transcript_files

        def listed_then_deleted(root, rejected=None):
            files = listed(root, rejected)
            shutil.rmtree(self.brain / RESUMED)  # agy removes an old conversation meanwhile.
            return files

        registry = {"schema": "computecost_source_registry/v1",
                    "sources": [{"id": "native-agy", "kind": "agy", "path": str(self.brain)}]}
        ledger = UsageLedger(self.root / "ledger")
        with mock.patch.object(agy_usage, "transcript_files", listed_then_deleted):
            source, = collect_registered_sources(registry, ledger)["sources"]
        self.assertEqual((source["status"], source["reason"]), ("partial", "native_files_incomplete"))
        usage = [r for r in ledger.receipt_snapshot()["facts"] if r.kind == "usage"]
        self.assertEqual(len(usage), len(agy_usage.read_agy(good, OBSERVED)))
        self.assertFalse(gone.exists())

    def test_runtime_binding_admits_agy_with_unknown_procurement(self):
        binding = build_runtime_usage_binding(work_item_id=WI, agent_id="pulse", phase="execute", epoch=1,
                                              runtime="agy", procurement_source="unknown")
        self.assertEqual((binding.runtime, binding.procurement_source), ("agy", "unknown"))


def host_turn(conversation: str, *, start: str, end: str, usage: dict, model="gemini-4-neon-high",
              attempt="attempt-2",
              bounded=True):
    """The Relay Host's agy turn under its runtime usage binding (wi666 N02 contract).

    Relay names the turn's model (native_model_usage); its tests use the step's.
    """
    common = {"wi": WI, "executor": "pulse", "responsible": None, "delegator": None}
    binding = build_runtime_usage_binding(work_item_id=WI, agent_id="pulse", phase="execute", epoch=1,
                                          runtime="agy", procurement_source="unknown")
    execution_ref = reference("attempt", attempt)
    execution = Receipt.make("execution", None, {**common, "runtime": "agy",
        "assignment_ref": reference("assignment", binding.binding_sha256), "parent_ref": None,
        "state": "completed", "expected_usage": True}, occurred_at=start, observed_at=end,
        producer="relay_host.v1", entity_ref=execution_ref)
    turn = usage_receipt("agy", [attempt, "result"], usage, at=end, observed=end, model=model,
        execution_ref=execution_ref, common=common, granularity="turn_aggregate", final=True,
        mapping=agy_usage.RESULT_MAPPING, interval=(start, end) if bounded else None,
        native_scope_ref=reference("native_session", ["agy", conversation]))
    return execution, turn


class AgyHostJoinTest(BrainCase):
    # Second turn of the resumed conversation: step 4, 10:31:10Z, gemini-4-neon-high.
    START, END = "2026-11-20T10:31:10.400Z", "2026-11-20T10:31:20.000Z"
    TURN = {"input_tokens": 10044, "output_tokens": 38, "thinking_tokens": 36, "cache_read_tokens": 0,
            "total_tokens": 10082}

    def steps(self):
        return agy_usage.read_agy(self.place("c-transcript_full-8f0067ec.jsonl", RESUMED), OBSERVED)

    def test_exact_turn_gives_its_steps_the_host_wi(self):
        steps = self.steps()
        execution, turn = host_turn(RESUMED, start="2026-11-20T10:31:09.000Z", end=self.END, usage=self.TURN)
        active, diagnostics = reconcile_usage([*steps, turn], executions=[execution])
        self.assertEqual(diagnostics["conflicting_refs"], [])
        self.assertEqual(diagnostics["suppressed_aggregate_refs"], [turn.entity_ref])
        joined = {r.entity_ref: r.payload for r in active}
        self.assertEqual(joined[steps[1].entity_ref]["wi"], WI)
        self.assertEqual(joined[steps[1].entity_ref]["executor"], "pulse")
        # The earlier turn of the same conversation is outside the interval: no WI.
        self.assertIsNone(joined[steps[0].entity_ref]["wi"])

    def test_step_in_the_dispatch_second_keeps_the_host_turn(self):
        steps = self.steps()
        execution, turn = host_turn(RESUMED, start=self.START, end=self.END, usage=self.TURN)
        active, diagnostics = reconcile_usage([*steps, turn], executions=[execution])
        self.assertEqual(diagnostics["conflicting_refs"], [])
        self.assertNotIn(steps[1].entity_ref, {r.entity_ref for r in active})
        self.assertEqual({r.entity_ref: r.payload["wi"] for r in active}[turn.entity_ref], WI)

    def test_disagreeing_or_unknown_quantities_remain_conflicts(self):
        steps = self.steps()
        execution, turn = host_turn(RESUMED, start="2026-11-20T10:31:09.000Z", end=self.END,
                                    usage={**self.TURN, "output_tokens": 39})
        _, diagnostics = reconcile_usage([*steps, turn], executions=[execution])
        self.assertEqual(set(diagnostics["conflicting_refs"]), {turn.entity_ref, steps[1].entity_ref})
        execution, turn = host_turn(RESUMED, start="2026-11-20T10:31:09.000Z", end=self.END,
                                    usage=self.TURN, model="gemini-3.8-flash-low")
        _, diagnostics = reconcile_usage([*steps, turn], executions=[execution])
        self.assertIn(turn.entity_ref, diagnostics["conflicting_refs"])

    def synthetic_step(self, *, at: str, model: str) -> Receipt:
        """One collected model step of 10 input tokens (a synthetic ledger, as the review built it)."""
        path = self.brain / RESUMED / agy_usage.TRANSCRIPT
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE",
            "created_at": at, "model": model, "input_tokens": 10, "cache_read_tokens": 0,
            "output_tokens": 0}) + "\n")
        step, = agy_usage.read_agy(path, OBSERVED)
        return step

    TEN = {"input_tokens": 10, "output_tokens": 0, "cache_read_tokens": 0, "total_tokens": 10}

    def test_step_an_adjacent_turn_may_hold_proves_neither_turn(self):
        # Disjoint turns; the second's start rounds down into the first's last second.
        step = self.synthetic_step(at="2026-11-20T10:00:05Z", model="gemini-m")
        first_ex, first = host_turn(RESUMED, start="2026-11-20T10:00:00.500Z", end="2026-11-20T10:00:05.200Z",
                                    usage=self.TEN, model="gemini-m", attempt="attempt-1")
        second_ex, second = host_turn(RESUMED, start="2026-11-20T10:00:05.700Z", end="2026-11-20T10:00:09.000Z",
                                      usage=self.TEN, model="gemini-m", attempt="attempt-2")
        active, diagnostics = reconcile_usage([step, first, second], executions=[first_ex, second_ex])
        # 20 tokens were used; the one step cannot stand for both turns (10, "reconciled").
        self.assertEqual(diagnostics["status"], "conflict")
        self.assertEqual(set(diagnostics["conflicting_refs"]), {step.entity_ref, first.entity_ref, second.entity_ref})
        self.assertEqual(diagnostics["suppressed_aggregate_refs"], [])
        self.assertEqual(active, [])

    def test_one_turn_naming_two_models_stays_a_conflict(self):
        step = self.synthetic_step(at="2026-11-20T10:00:05Z", model="gemini-transcript")
        execution, turn = host_turn(RESUMED, start="2026-11-20T10:00:00.500Z", end="2026-11-20T10:00:09.000Z",
                                    usage=self.TEN, model="gemini-host")
        active, diagnostics = reconcile_usage([step, turn], executions=[execution])
        # Both sides report 10 of the same turn: not 20, and not reconciled.
        self.assertEqual(diagnostics["status"], "conflict")
        self.assertEqual(set(diagnostics["conflicting_refs"]), {step.entity_ref, turn.entity_ref})
        self.assertIn("agy_turn_model_conflict", diagnostics["conflict_reasons"][turn.entity_ref])
        self.assertEqual(active, [])

    def test_an_undated_step_the_turn_may_hold_proves_nothing(self):
        path = self.brain / RESUMED / agy_usage.TRANSCRIPT
        path.parent.mkdir(parents=True)
        row = {"type": "PLANNER_RESPONSE", "status": "DONE", "model": "gemini-m",
               "input_tokens": 10, "cache_read_tokens": 0, "output_tokens": 0}
        path.write_text(json.dumps({**row, "step_index": 1, "created_at": "2026-11-20T10:00:05Z"}) + "\n"
                        + json.dumps({**row, "step_index": 2}) + "\n")
        dated, undated = agy_usage.read_agy(path, OBSERVED)
        self.assertIsNone(undated.occurred_at)
        execution, turn = host_turn(RESUMED, start="2026-11-20T10:00:00.500Z", end="2026-11-20T10:00:09.000Z",
                                    usage=self.TEN, model="gemini-m")
        active, diagnostics = reconcile_usage([dated, undated, turn], executions=[execution])
        # The undated step may belong to the turn: not 20 reconciled with no conflict.
        self.assertEqual(diagnostics["status"], "conflict")
        self.assertEqual(diagnostics["suppressed_aggregate_refs"], [])
        self.assertIn(turn.entity_ref, diagnostics["conflicting_refs"])
        self.assertIn(undated.entity_ref, diagnostics["conflicting_refs"])

    def test_an_unbounded_turn_naming_another_model_stays_a_conflict(self):
        step = self.synthetic_step(at="2026-11-20T10:00:05Z", model="gemini-transcript")
        execution, turn = host_turn(RESUMED, start="2026-11-20T10:00:00.500Z", end="2026-11-20T10:00:09.000Z",
                                    usage=self.TEN, model="gemini-host", bounded=False)
        active, diagnostics = reconcile_usage([step, turn], executions=[execution])
        self.assertEqual(diagnostics["status"], "conflict")
        self.assertEqual(set(diagnostics["conflicting_refs"]), {step.entity_ref, turn.entity_ref})
        self.assertIn("agy_turn_model_conflict", diagnostics["conflict_reasons"][turn.entity_ref])
        self.assertEqual(active, [])

    def test_collected_transcript_reaches_the_wi_with_unknown_cost(self):
        self.place("c-transcript_full-8f0067ec.jsonl", RESUMED)
        ledger = UsageLedger(self.root / "ledger")
        execution, turn = host_turn(RESUMED, start="2026-11-20T10:31:09.000Z", end=self.END, usage=self.TURN)
        ledger.ingest_receipts([execution, turn])
        registry = {"schema": "computecost_source_registry/v1",
                    "sources": [{"id": "native-agy", "kind": "agy", "path": str(self.brain)}]}
        collected = collect_registered_sources(registry, ledger)
        self.assertGreater(collected["inserted"], 0)
        self.assertEqual(collect_registered_sources(registry, ledger)["inserted"], 0)
        result = query_collaboration(ledger, CollaborationQuery(scope="wi_lifetime", work_items=(WI,)))
        usage = result["summary"]["usage"]
        self.assertEqual(usage["event_count"], 1)
        self.assertEqual(usage["known_total_tokens"], 10044 + 38)
        cost = result["summary"]["cost"]
        self.assertIsNone(cost["machine_cash_usd"])
        self.assertEqual(cost["priced_event_count"], 0)
        # The step comes from the transcript, at its own time.
        self.assertEqual(usage["first_usage_at"], "2026-11-20T10:31:10Z")
        self.assertEqual({u["reason"] for u in cost["unpriced"]}, {agy_usage.UNPRICED_REASON})


if __name__ == "__main__":
    unittest.main()
