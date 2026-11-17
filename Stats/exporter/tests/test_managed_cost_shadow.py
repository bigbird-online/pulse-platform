from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
import pathlib
from unittest import mock
import sys
import tempfile
import unittest


EXPORTER = pathlib.Path(__file__).parents[1]
COMPUTE_COST = pathlib.Path(__file__).parents[2] / "ComputeCost"
sys.path.insert(0, str(EXPORTER))
sys.path.insert(0, str(COMPUTE_COST))

from pulse_compute_cost.managed_snapshot import build_managed_cost_snapshot
from pulse_compute_cost.openrouter import OpenRouterPriceSnapshot
from pulse_stats.live import LiveSnapshotService
from pulse_stats.managed_cost_shadow import (
    BY_AGENT_METRIC_ID,
    SUMMARY_METRIC_ID,
    build_managed_cost_shadow_metrics,
    load_managed_cost_shadow,
)


NOW = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)


def price(retrieved_at: str = "2026-08-17T07:00:00Z") -> OpenRouterPriceSnapshot:
    return OpenRouterPriceSnapshot(
        model_id="deepseek/reference",
        canonical_slug="deepseek/reference-202608",
        retrieved_at=retrieved_at,
        prompt_per_token=Decimal("0.00000005"),
        completion_per_token=Decimal("0.00000008"),
        input_cache_read_per_token=Decimal("0.00000001"),
    )


def snapshot(
    *,
    product_month: str = "2026-08",
    generated_at: datetime = NOW,
    settlement_status: str = "provisional",
) -> dict:
    return build_managed_cost_snapshot(
        product_month=product_month,
        events=[],
        price=price((generated_at - timedelta(hours=1)).isoformat()),
        generated_at=generated_at,
        settlement_status=settlement_status,
    ).to_dict()


def write_snapshot(directory: str, value: dict) -> pathlib.Path:
    path = pathlib.Path(directory) / "managed-cost.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def reidentify(value: dict) -> None:
    content = dict(value)
    content.pop("snapshot_id", None)
    payload = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    value["snapshot_id"] = "mcs1_" + hashlib.sha256(payload).hexdigest()


class ManagedCostShadowTests(unittest.TestCase):
    def test_default_off_emits_no_metrics_and_does_not_read_path(self) -> None:
        self.assertEqual(
            build_managed_cost_shadow_metrics(
                enabled=False,
                path=pathlib.Path("relative-path-that-must-not-be-read"),
                now=NOW,
            ),
            [],
        )

    def test_compute_cost_output_crosses_stats_contract_with_exact_strings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_snapshot(directory, snapshot())
            loaded = load_managed_cost_shadow(path)
            metrics = build_managed_cost_shadow_metrics(
                enabled=True, path=path, now=NOW,
            )

        self.assertEqual(loaded["schema"], "managed_cost_snapshot.v1")
        self.assertEqual([metric["id"] for metric in metrics], [
            SUMMARY_METRIC_ID, BY_AGENT_METRIC_ID,
        ])
        self.assertTrue(all(metric["status"] == "available" for metric in metrics))
        self.assertTrue(all(metric["capability_id"] == "compute_cost.shadow" for metric in metrics))
        summary = metrics[0]["value"][0]
        self.assertEqual(summary["managed_cost_usd"], "200")
        self.assertEqual(summary["unallocated_cents"], 20_000)
        self.assertTrue(summary["usage_event_set_digest"].startswith("uevset1_"))
        self.assertEqual(metrics[1]["value"][0]["agent"], "unallocated")

    def test_tampered_conservation_or_identity_fails_as_atomic_unavailable_bundle(self) -> None:
        conservation = deepcopy(snapshot())
        conservation["totals"]["managed_cost_usd"] = "0"
        false_complete = deepcopy(snapshot())
        false_complete["quality"] = {"status": "complete", "flags": []}
        reidentify(false_complete)
        for damaged in (conservation, false_complete):
            with self.subTest(quality=damaged["quality"]):
                with tempfile.TemporaryDirectory() as directory:
                    path = write_snapshot(directory, damaged)
                    metrics = build_managed_cost_shadow_metrics(
                        enabled=True, path=path, now=NOW,
                    )
                self.assertEqual(len(metrics), 2)
                self.assertTrue(all(metric["status"] == "unknown" for metric in metrics))
                self.assertTrue(all(metric["quality"] == "unavailable" for metric in metrics))
                self.assertTrue(all(metric["value"] is None for metric in metrics))

    def test_enabled_missing_or_malformed_snapshot_fails_soft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "missing.json"
            malformed = pathlib.Path(directory) / "bad.json"
            malformed.write_text("{", encoding="utf-8")
            for path in (None, missing, malformed, pathlib.Path("relative.json")):
                with self.subTest(path=path):
                    metrics = build_managed_cost_shadow_metrics(
                        enabled=True, path=path, now=NOW,
                    )
                    self.assertEqual({metric["status"] for metric in metrics}, {"unknown"})
                    self.assertEqual({metric["quality"] for metric in metrics}, {"unavailable"})

    def test_old_month_closed_or_stale_snapshot_is_withheld(self) -> None:
        cases = (
            snapshot(product_month="2026-07"),
            snapshot(settlement_status="closed"),
            snapshot(generated_at=NOW - timedelta(minutes=46)),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, value in enumerate(cases):
                with self.subTest(index=index):
                    path = pathlib.Path(directory) / f"case-{index}.json"
                    path.write_text(json.dumps(value), encoding="utf-8")
                    metrics = build_managed_cost_shadow_metrics(
                        enabled=True, path=path, now=NOW,
                    )
                    self.assertTrue(all(metric["status"] == "unknown" for metric in metrics))
                    self.assertTrue(all(metric["value"] is None for metric in metrics))

    def test_default_service_requires_exact_flag_and_absolute_path(self) -> None:
        environment = {
            "PULSE_COMPUTE_COST_SHADOW_ENABLED": "true",
            "PULSE_COMPUTE_COST_SHADOW_PATH": "/tmp/candidate.json",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            service = LiveSnapshotService.default()
            self.assertFalse(service.paths.compute_cost_shadow_enabled)
            self.assertFalse(service.paths.emit_legacy_cost_metrics)

        with mock.patch.dict(
            os.environ,
            {
                "PULSE_COMPUTE_COST_SHADOW_ENABLED": "1",
                "PULSE_COMPUTE_COST_SHADOW_PATH": "/tmp/candidate.json",
            },
            clear=False,
        ):
            service = LiveSnapshotService.default()
            self.assertTrue(service.paths.compute_cost_shadow_enabled)
            self.assertEqual(service.paths.compute_cost_shadow, pathlib.Path("/tmp/candidate.json"))

        with mock.patch.dict(
            os.environ,
            {
                "PULSE_COMPUTE_COST_SHADOW_ENABLED": "1",
                "PULSE_COMPUTE_COST_SHADOW_PATH": "relative.json",
            },
            clear=False,
        ):
            service = LiveSnapshotService.default()
            self.assertTrue(service.paths.compute_cost_shadow_enabled)
            self.assertIsNone(service.paths.compute_cost_shadow)


if __name__ == "__main__":
    unittest.main()
