#!/usr/bin/env python3
"""One-shot canonical snapshot exporter with fixed production sources."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

# The App runs this file with `python -I`, which does not add the script's
# directory to sys.path; the installed release carries pulse_stats beside it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ComputeCost"))

from pulse_stats.producer import build_snapshot
from pulse_stats.reducer import IncidentEvent, reduce_incident_events
from pulse_stats.live import LiveSnapshotError, LiveSnapshotService
from pulse_stats.state import StateError
from pulse_stats.cost_dashboard import attach_cost_dashboard, decode_cost_dashboard, DEFAULT_PATH


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        help="normalized fixture input; omit to use the fixed production adapter",
    )
    parser.add_argument(
        "--compute-cost-shadow",
        type=Path,
        help="absolute managed_cost_snapshot.v1 path to consume in this live export",
    )
    parser.add_argument("--cost-dashboard", type=Path, help="private #513 query publication; unavailable degrades only cost")
    parser.add_argument("--cost-dashboard-only", action="store_true",
                        help="export only the explicit existing cost publication; no live collection or platform claims")
    parser.add_argument("--probe-cost-row-contexts", action="store_true",
                        help="verify support for the compact cost snapshot without reading production data")
    parser.add_argument("--probe-cost-compression", action="store_true",
                        help="verify bounded gzip cost snapshot support without reading production data")
    parser.add_argument("--json", action="store_true", required=True, help="emit one JSON object to stdout")
    return parser


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _event(raw: dict) -> IncidentEvent:
    return IncidentEvent(
        event_id=raw["event_id"],
        kind=raw["kind"],
        capability_id=raw["capability_id"],
        condition_key=raw["condition_key"],
        scope=raw["scope"],
        normalized_scope_key=raw["normalized_scope_key"],
        severity=raw["severity"],
        title=raw["title"],
        occurred_at=_date(raw["occurred_at"]),
        reason=raw["reason"],
        impact=raw.get("impact", "unknown"),
        suggested_action=raw.get("suggested_action", "Inspect source evidence"),
    )


def main(argv: list[str] = None, live_service_factory=LiveSnapshotService.default) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.probe_cost_row_contexts:
        probe = b'{"views":[{"groups":[{"outcomes":0}]}],"row_contexts":{"outcomes":[{"known_count":0}]}}'
        if decode_cost_dashboard(probe) != {'views': [{'groups': [{'outcomes': {'known_count': 0}}]}]}:
            return 2
        print(json.dumps({'schema': 'pulse_stats_cost_row_contexts/v1'}))
        return 0
    if args.probe_cost_compression:
        from pulse_compute_cost.stats_dashboard_wire import (
            COMPRESSION_METADATA, COMPRESSED_PROBE_SCHEMA, compressed_dashboard_bytes,
        )
        probe = {**dict.fromkeys(COMPRESSION_METADATA, 'probe'), 'views': [{'groups': [{'usage': {}}]}]}
        if decode_cost_dashboard(compressed_dashboard_bytes(probe)) != probe:
            return 2
        print(json.dumps({'schema': COMPRESSED_PROBE_SCHEMA}))
        return 0
    if args.cost_dashboard_only:
        if (args.fixture is not None or args.compute_cost_shadow is not None
                or args.cost_dashboard is None or not args.cost_dashboard.is_absolute()):
            parser.error("--cost-dashboard-only requires one absolute --cost-dashboard, without fixture or shadow")
        now = datetime.now(timezone.utc)
        base = build_snapshot(
            generated_at=now, expires_at=now + timedelta(minutes=45),
            capabilities=(), required_capability_ids=(), incidents=(),
            provenance={"producer": {"name": "pulse-stats-cost-publication", "version": "1"},
                        "source_revisions": {}, "sources": []},
            headline="Only the selected cost publication was read; platform and WI authority are unknown",
        )
        # Uses the same verifier and projection as live export. Original query
        # timestamps, watermark, revision and digest stay inside cost_dashboard.
        print(attach_cost_dashboard(json.dumps(base), args.cost_dashboard, now=now))
        return 0
    if args.fixture is None:
        try:
            service = (
                live_service_factory(compute_cost_shadow_path=args.compute_cost_shadow)
                if args.compute_cost_shadow is not None
                else live_service_factory()
            )
            rendered = attach_cost_dashboard(service.render_json(), args.cost_dashboard or DEFAULT_PATH)
        except (LiveSnapshotError, StateError, OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"live exporter error: {type(error).__name__}", file=sys.stderr)
            return 2
        print(rendered)
        return 0
    try:
        raw = json.loads(args.fixture.read_text(encoding="utf-8"))
        incidents, alerts = reduce_incident_events(_event(item) for item in raw.get("events", []))
        snapshot = build_snapshot(
            generated_at=_date(raw["generated_at"]),
            expires_at=_date(raw["expires_at"]),
            capabilities=raw["capabilities"],
            required_capability_ids=raw["required_capability_ids"],
            incidents=incidents,
            alerts=alerts,
            metrics=raw.get("metrics", []),
            provenance=raw["provenance"],
            headline=raw["headline"],
            baseline_version=raw.get("baseline_version", "0.2.1"),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"fixture error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
