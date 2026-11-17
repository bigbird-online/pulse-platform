"""MTD provenance collector and six-metric billing projection contract."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

from pulse_stats.billing import build_billing_metrics, collect_mtd_evidence


NOW = datetime(2026, 7, 25, 8, tzinfo=timezone.utc)


def config():
    return {
        "schema_version": 1,
        "components": [
            {
                "component_id": "claude_code", "amount_cents": 25_000,
                "currency": "USD", "effective_from": "2026-07-01",
                "effective_until": None, "source_ref": "owner-reviewed:cc",
                "attribution": "cc_mtd_output_share",
            },
            {
                "component_id": "codex", "amount_cents": 2_000,
                "currency": "USD", "effective_from": "2026-07-01",
                "effective_until": None, "source_ref": "owner-reviewed:codex",
                "attribution": "unattributed_no_agent_source",
            },
        ],
    }


class EvidenceTests(unittest.TestCase):
    def _lines(self, root, name, events):
        (root / name).write_text(
            "\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n",
            encoding="utf-8",
        )

    def _turn(self, **overrides):
        event = {
            "evt": "turn.end", "ts": "2026-07-20T01:00:00Z",
            "agent": "pulse", "outputTokens": 100,
            "billing_component_id": "claude_code",
        }
        event.update(overrides)
        return event

    def test_proven_events_aggregate_agents_and_keep_invalid_agent_in_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._lines(root, "obs-2026-07-20.jsonl", [
                self._turn(agent="pulse", outputTokens=25),
                self._turn(agent="pulse", outputTokens=75),
                self._turn(agent=" spaced ", outputTokens=10),
                self._turn(agent=None, outputTokens=5),
            ])
            found = collect_mtd_evidence(root, NOW)
        self.assertEqual((found.outcome, found.coverage_complete),
                         ("usable", False))
        self.assertEqual(found.agent_output_tokens, (("pulse", 100),))
        self.assertEqual(found.unattributed_output_tokens, 15)
        self.assertEqual(found.valid_turn_count, 4)

    def test_missing_gate_is_insufficient_and_runtime_provider_never_substitute(self):
        for extra in ({"runtime": "cc"}, {"provider": "claude_code"}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                self._lines(root, "obs-2026-07-20.jsonl", [
                    self._turn(billing_component_id=None, **extra),
                ])
                self.assertEqual(collect_mtd_evidence(root, NOW).outcome,
                                 "insufficient")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._lines(root, "obs-2026-07-20.jsonl", [
                self._turn(billing_component_id="codex", runtime="cc"),
            ])
            self.assertEqual(collect_mtd_evidence(root, NOW).outcome, "insufficient")

    def test_non_string_or_empty_gate_is_malformed_unavailable(self):
        for value in ("", 1, True, []):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                self._lines(root, "obs-2026-07-20.jsonl", [
                    self._turn(billing_component_id=value),
                ])
                self.assertEqual(collect_mtd_evidence(root, NOW).outcome,
                                 "unavailable")

    def test_event_time_not_filename_decides_month_and_future_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._lines(root, "obs-2026-06-30.jsonl", [
                self._turn(ts="2026-06-30T17:00:00Z", agent="july", outputTokens=10),
            ])
            self._lines(root, "obs-2026-07-25.jsonl", [
                self._turn(ts="2026-07-25T08:00:01Z", agent="future", outputTokens=99),
            ])
            found = collect_mtd_evidence(root, NOW)
        self.assertEqual(found.agent_output_tokens, (("july", 10),))
        self.assertEqual(found.valid_turn_count, 1)

    def test_bad_json_or_required_cc_field_is_unavailable(self):
        cases = (
            ["{broken"], [self._turn(ts="not-time")],
            [self._turn(outputTokens=True)], [self._turn(outputTokens=-1)],
        )
        for events in cases:
            with self.subTest(events=events), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                self._lines(root, "obs-2026-07-20.jsonl", events)
                self.assertEqual(collect_mtd_evidence(root, NOW).outcome,
                                 "unavailable")

    def test_empty_or_non_turn_source_cannot_claim_verified_zero(self):
        for events in ([], [{"evt": "req.inbound", "ts": "2026-07-20T01:00:00Z"}]):
            with tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                self._lines(root, "obs-2026-07-20.jsonl", events)
                found = collect_mtd_evidence(root, NOW)
                self.assertEqual(found.outcome, "insufficient")
                self.assertFalse(found.coverage_complete)


class MetricTests(unittest.TestCase):
    def _config(self, root):
        path = root / "subscriptions.json"
        path.write_text(json.dumps(config()), encoding="utf-8")
        return path

    def test_facts_survive_attribution_insufficient(self):
        with tempfile.TemporaryDirectory() as directory:
            root, obs = pathlib.Path(directory), pathlib.Path(directory) / "obs"
            obs.mkdir()
            (obs / "obs-2026-07-20.jsonl").write_text(json.dumps({
                "evt": "turn.end", "ts": "2026-07-20T01:00:00Z",
                "runtime": "cc", "agent": "pulse", "outputTokens": 100,
            }) + "\n", encoding="utf-8")
            by_id = {
                item["id"]: item
                for item in build_billing_metrics(self._config(root), obs, NOW)
            }
        self.assertEqual(len(by_id), 6)
        self.assertEqual(by_id["billing.mtd.total_cents"]["value"], 27_000)
        self.assertIsNone(by_id["billing.mtd.components"]["sample_count"])
        for metric_id in (
            "billing.mtd.period", "billing.mtd.by_agent",
            "billing.mtd.attributed_cents", "billing.mtd.unattributed_cents",
        ):
            self.assertEqual((by_id[metric_id]["status"], by_id[metric_id]["quality"]),
                             ("unknown", "insufficient"))
            self.assertIsNone(by_id[metric_id]["value"])
            self.assertIsNone(by_id[metric_id]["sample_count"])

    def test_bad_config_makes_exactly_six_unavailable_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "bad.json").write_text("{broken", encoding="utf-8")
            metrics = build_billing_metrics(root / "bad.json", root, NOW)
        self.assertEqual(len(metrics), 6)
        self.assertEqual(len({item["reason"] for item in metrics}), 1)
        self.assertTrue(all(
            (m["status"], m["quality"], m["value"], m["sample_count"], bool(m["reason"]))
            == ("unknown", "unavailable", None, None, True)
            for m in metrics
        ))


if __name__ == "__main__":
    unittest.main()
