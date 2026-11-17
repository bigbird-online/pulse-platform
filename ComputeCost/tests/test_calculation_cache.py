"""Incremental calculation uses identical facts, never a second account of costs."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost import collaboration_query as query
from pulse_compute_cost.calculation_cache import CalculationCache
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import usage_receipt, actor_receipt
from pulse_compute_cost.stats_dashboard import build_dashboard
from test_stats_dashboard import NOW, REV, CATALOG, SUBSCRIPTIONS


class CalculationCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "cache"
        self.ledger = UsageLedger(self.root / "ledger", clock=lambda: NOW)
        self.row = self.usage("first", 100)
        self.ledger.ingest_receipts([self.row])
        self.now = NOW + timedelta(seconds=1)

    def usage(self, identity, n):
        return usage_receipt("pi", identity, {"input": n, "output": 10, "cacheRead": 30, "cacheWrite": 0},
            at=NOW.isoformat(), observed=NOW.isoformat(), model="openai/gpt-4o",
            common={"wi": "wi_20260825_300", "executor": "pulse", "responsible": "pulse", "delegator": None})

    def build(self, **kwargs):
        args = dict(subscriptions=SUBSCRIPTIONS, source_revision=REV, now=self.now,
                    catalog_snapshot=CATALOG, identity_dimensions=True, activity_table=True,
                    calculation_cache=self.path)
        args.update(kwargs)
        return build_dashboard(self.ledger, **args)

    def test_second_publication_reuses_all_21_views(self):
        cold = self.build()
        self.assertEqual(cold["status"], "available")
        self.assertEqual(len(cold["views"]), 21)
        with patch.object(query.CollaborationQuerySession, "query", side_effect=AssertionError("must reuse")):
            warm = self.build()
        self.assertEqual(warm, cold)
        self.assertEqual(self.build(calculation_cache=None), cold)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.path.glob("*.json")))

    def test_force_recompute_preserves_every_result(self):
        first = self.build()
        with patch.object(query.CollaborationQuerySession, "query", autospec=True, side_effect=query.CollaborationQuerySession.query) as spy:
            forced = self.build(force_recompute=True)
        self.assertEqual(spy.call_count, 21)
        self.assertEqual(first, forced)

    def test_new_usage_prices_only_the_new_quantity(self):
        self.build()
        self.ledger.ingest_receipts([self.usage("second", 321)])
        with patch.object(query, "_value_cost", wraps=query._value_cost) as price:
            changed = self.build()
        self.assertEqual(price.call_count, 1)
        self.assertEqual(changed, self.build(force_recompute=True))
        self.assertEqual(changed["views"][0]["summary"]["usage"]["event_count"], 2)

    def test_late_identity_changes_group_without_repricing_old_usage(self):
        self.build()
        self.ledger.ingest_receipts([actor_receipt(self.row, node="tcloud", executor="pulse",
            entrypoint="telegram", observed=NOW.isoformat())])
        with patch.object(query, "_value_cost", wraps=query._value_cost) as price:
            changed = self.build()
        self.assertEqual(price.call_count, 0)
        self.assertEqual(changed, self.build(force_recompute=True))
        activity = next(v for v in changed["views"] if v["dimension"] == "activity")
        self.assertEqual(activity["groups"][0]["key"]["entrypoint"], "telegram")

    def test_changed_prices_cannot_borrow_old_price_result(self):
        before = self.build()
        changed = deepcopy(CATALOG)
        changed["catalog"]["data"][0]["pricing"]["prompt"] = "0.05"
        with patch.object(query, "_value_cost", wraps=query._value_cost) as price:
            result = self.build(catalog_snapshot=changed)
        self.assertEqual(price.call_count, 1)
        self.assertNotEqual(before["views"][0]["summary"]["cost"], result["views"][0]["summary"]["cost"])
        self.assertEqual(result, self.build(catalog_snapshot=changed, force_recompute=True))
        missing = self.build(catalog_snapshot=None)
        self.assertIsNone(missing["views"][0]["summary"]["cost"]["partial_machine_cash_usd"])

    def test_calendar_boundary_and_code_change_invalidate_views(self):
        self.build()
        self.now += timedelta(days=1)
        with patch.object(query.CollaborationQuerySession, "query", autospec=True, side_effect=query.CollaborationQuerySession.query) as spy:
            changed = self.build()
        self.assertEqual(spy.call_count, 21)
        self.assertEqual(changed, self.build(force_recompute=True))
        with patch("pulse_compute_cost.calculation_cache.engine_digest", return_value="new-code"), \
             patch.object(query.CollaborationQuerySession, "query", autospec=True, side_effect=query.CollaborationQuerySession.query) as spy:
            self.build()
        self.assertEqual(spy.call_count, 21)

    def test_cache_corruption_recomputes_not_hides_source_error(self):
        expected = self.build()
        (self.path / "today-agent.json").write_text("{broken")
        self.assertEqual(expected, self.build())
        (self.ledger.root / "receipts/00000000000000000001.json").write_text("{broken")
        bad = self.build()
        self.assertEqual(bad["status"], "unavailable")
        self.assertEqual(bad["views"], [])

    def test_unsafe_cache_does_not_touch_link_target(self):
        target = self.root / "elsewhere"
        target.mkdir()
        self.path.symlink_to(target, target_is_directory=True)
        result = self.build()
        self.assertEqual(result["status"], "available")
        self.assertEqual(list(target.iterdir()), [])

    def test_mutating_returned_report_does_not_mutate_cache(self):
        first = self.build()
        wanted = deepcopy(first)
        first["views"][0]["summary"]["usage"]["event_count"] = 999
        self.assertEqual(self.build(), wanted)

    def test_cache_key_and_integrity_are_both_required(self):
        cache = CalculationCache(self.path)
        cache.put("small", "a", {"safe": True})
        self.assertEqual(cache.get("small", "a"), {"safe": True})
        self.assertIsNone(cache.get("small", "b"))
        path = self.path / "small.json"
        row = json.loads(path.read_bytes()); row["value"] = {"safe": False}
        path.write_text(json.dumps(row))
        self.assertIsNone(cache.get("small", "a"))
