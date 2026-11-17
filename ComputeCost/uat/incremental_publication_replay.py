#!/usr/bin/env python3
"""Benchmark actual frozen published receipts; no collection or production writes."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ComputeCost"))
from pulse_compute_cost.catalog_source import managed_state_root, load_catalog_snapshot
from pulse_compute_cost import collaboration_query as query
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.stats_dashboard import build_dashboard, _view, DEFAULT_FACTS
from pulse_compute_cost.stats_dashboard_wire import dashboard_bytes, expand_dashboard
from pulse_compute_cost.receipts import canonical


def run(output: Path):
    allowed = Path(os.environ["RUNNER_TEMP"]).resolve()
    if output.exists() or not output.resolve().is_relative_to(allowed) or output.resolve() == allowed:
        raise ValueError("new Actions-private output required")
    output.mkdir(parents=True, mode=0o700)
    state = managed_state_root()
    registry = json.loads((state / "compute-cost/sources.json").read_bytes())
    publication = expand_dashboard(json.loads((state / "stats/cost-dashboard-v1.json").read_bytes()))
    original = Path(registry["ledger_root"])
    live_ledger = UsageLedger(original)
    if publication.get("status") == "available" and publication.get("views"):
        watermark = publication["views"][0]["watermark"]
        replay_time = datetime.fromisoformat(publication["generated_at"].replace("Z", "+00:00"))
    else:
        watermark = live_ledger.receipt_snapshot()["watermark"]
        replay_time = datetime.now(timezone.utc)
    copied = output / "ledger"
    (copied / "receipts").mkdir(parents=True, mode=0o700)
    with (original / ".usage-ledger.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        for number in range(1, watermark + 1):
            path = original / "receipts" / f"{number:020d}.json"
            if path.is_symlink():
                raise ValueError("unexpected source symlink")
            shutil.copy2(path, copied / "receipts" / path.name)
    ledger = UsageLedger(copied)
    before = ledger.receipt_snapshot()
    now = replay_time
    catalog = load_catalog_snapshot(state / "stats/openrouter-catalog.json")
    if catalog is not None and datetime.fromisoformat(catalog["retrieved_at"].replace("Z", "+00:00")) > now:
        raise ValueError("price snapshot is newer than frozen publication")
    args = dict(subscriptions=json.loads(DEFAULT_FACTS.read_bytes()), source_revision=os.environ["PULSE_SOURCE_REVISION"],
                now=now, catalog_snapshot=catalog, identity_dimensions=True, activity_table=True, activity_only=True,
                calculation_cache=output / "calculation-cache")
    def measure(name, **extra):
        start = time.monotonic()
        with patch.object(query, "_value_cost", wraps=query._value_cost) as prices, \
             patch.object(query.CollaborationQuerySession, "query", autospec=True,
                          side_effect=query.CollaborationQuerySession.query) as queries:
            value = build_dashboard(UsageLedger(copied), **args, **extra)
        result = dict(seconds=round(time.monotonic() - start, 3), queries=queries.call_count,
                      price_calculations=prices.call_count, status=value["status"])
        print(json.dumps({"stage": name, **result}), flush=True)
        assert value["status"] == "available" and len(value["views"]) == 3
        assert len(dashboard_bytes(value)) <= 512*1024
        return value, result
    cold, cold_time = measure("cold")
    warm, warm_time = measure("warm_new_ledger_instance")
    forced, forced_time = measure("forced", force_recompute=True)
    assert canonical(cold) == canonical(warm) == canonical(forced), "cached/forced reports differ"
    assert warm_time["queries"] == 0 and warm_time["price_calculations"] == 0
    assert forced_time["queries"] == 3 and forced_time["price_calculations"] == cold_time["price_calculations"]
    # Verify the optimized full weekly query against the exact installed source.
    baseline = subprocess.run(["git", "-C", str(ROOT), "show",
        "f696ec0912a7112cef691a5beaf87cda5a34049b:ComputeCost/pulse_compute_cost/collaboration_query.py"],
        check=True, stdout=subprocess.PIPE).stdout
    path = output / "baseline_query.py"; path.write_bytes(baseline)
    name = "pulse_compute_cost._incremental_uat_baseline"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    class FrozenBaselineInput:
        # The historical query predates query sessions. Supply only its public
        # snapshot-read contract; no cache fields or new implementation code.
        def receipt_snapshot(self, *, as_of=None, watermark=None):
            if as_of is not None or watermark not in (None, before['watermark']):
                raise ValueError('baseline escaped the frozen watermark')
            return {**before, 'facts': list(before['facts'])}
    baseline_seconds = {}
    for period in ("today","week","month"):
        candidate = next(v for v in cold["views"] if v["dimension"] == "activity" and v["period"] == period)
        parameters = dict(candidate["query"])
        parameters["group_by"] = tuple(parameters["group_by"]); parameters["work_items"] = tuple(parameters["work_items"])
        start = time.monotonic()
        reference = module.query_collaboration(FrozenBaselineInput(), module.CollaborationQuery(**parameters),
            catalog=catalog["catalog"] if catalog else None, catalog_observed_at=catalog["retrieved_at"] if catalog else None)
        baseline_seconds[period] = round(time.monotonic() - start, 3)
        assert canonical(_view(reference, period, "activity")) == canonical(candidate), f"baseline {period} result changed"
    weekly = next(v for v in cold["views"] if v["dimension"] == "activity" and v["period"] == "week")
    after = ledger.receipt_snapshot(); assert after["sha256"] == before["sha256"]
    result = dict(revision=os.environ["PULSE_SOURCE_REVISION"], watermark=watermark,
        facts_sha256=before["sha256"], cold=cold_time, warm=warm_time, forced=forced_time,
        baseline_query_seconds=baseline_seconds, cached_equals_forced=True, baseline_activity_equal=True,
        publication_bytes=len(canonical(cold)), starting_publication_status=publication.get("status"),
        starting_publication_reason=publication.get("reason"),
        week_events=weekly["summary"]["usage"]["event_count"],
        week_usd=weekly["summary"]["cost"]["partial_machine_cash_usd"], production_modified=False)
    (output / "summary.json").write_bytes(canonical(result))
    shutil.rmtree(copied)
    shutil.rmtree(output / "calculation-cache")
    path.unlink()
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args().output)
