"""Where incremental collection runs in a Stats cycle (WI754 step 4).

- The publisher's collection under refresh.json `incremental.collection`:
  `on` judges incrementally (`publisher_round`); `shadow` collects in full as
  before and, before that, judges the same inputs incrementally without
  ingesting, writing the comparison to `incremental_collect_shadow`
  (`shadow_round`).
- A pushing peer with `collect_local_mode` `incremental` (`peer_round`):
  observe, judge and ingest itself; its position goes to the publisher.
- A read-only trial on an observing peer (`trial_round`): judge copies of the
  bundles it pushed, ingest nothing, record what it judged.

Local state lives under `<ledger root>/derived/` (0700): the fact index, the
local spool and, on the publisher, the local observe index. The shadow keeps
its own spool and observe index beside them and never touches the local
spool's backlog.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import resource
import sys
from time import perf_counter
from typing import Any, Callable, Mapping

from .events import utc_text
from .incremental_collect import (LIMIT_PEER, LIMIT_PUBLISHER, LOCAL_SOURCE_ID, collect_incremental,
                                  judging_registry, observe_into_spool, open_index)
from .incremental_modes import cache_mib
from .ledger import UsageLedger
from .receipts import canonical, digest, reference

SHADOW_SAMPLE = 5


def derived(ledger: UsageLedger) -> Path:
    path = ledger.root / "derived"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def local_spool(ledger: UsageLedger) -> Path:
    return derived(ledger) / "local-spool"


def shadow_spool(ledger: UsageLedger) -> Path:
    return derived(ledger) / "shadow-spool"


def _rules():
    from .coverage_recovery import load_recoveries
    from .source_retirement import load_retirements
    return load_retirements(), load_recoveries()


def peak_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def publisher_round(registry: Mapping[str, Any], ledger: UsageLedger, *, timings: dict[str, Any],
                    force_recompute: bool = False) -> dict[str, Any]:
    """`incremental.collection = on`: own sources observed into the local spool, then one incremental round."""
    retirements, recoveries = _rules()
    index = open_index(ledger, cache_mib=cache_mib())
    try:
        started = perf_counter()
        observe_into_spool(registry, spool=local_spool(ledger), state=derived(ledger) / "local-observe",
                           read_at=utc_text(datetime.now(timezone.utc)), retirements=retirements,
                           force_recompute=force_recompute)
        timings["observe_seconds"] = round(perf_counter() - started, 3)
        return collect_incremental(judging_registry(registry, local_spool(ledger), retirements), ledger, index,
                                   publisher=True, limit=LIMIT_PUBLISHER, retirements=retirements,
                                   recoveries=recoveries, timings=timings, force_recompute=force_recompute,
                                   rules_registry=registry)
    finally:
        index.close()


def shadow_round(registry: Mapping[str, Any], ledger: UsageLedger, full: Callable[[str], dict[str, Any]], *,
                 timings: dict[str, Any], force_recompute: bool = False) -> dict[str, Any]:
    """`incremental.collection = shadow`: the full collection decides and ingests as before.

    Before it, the same peer bundles and this node's sources (observed into
    the shadow's own spool and observe index, whose judged bundles are removed
    again: the full collection reads those sources itself) are judged
    incrementally at the index's prefix, ingesting nothing. The local spool of
    `on` and its observe index are not touched, so their backlog stays. After it, the facts it appended are compared with the facts the
    incremental judgement would have appended (deduplicated fact refs). A
    failing shadow never fails the collection.
    """
    from .native_observations import commit_pushed
    now = utc_text(datetime.now(timezone.utc))
    shadow: dict[str, Any] = {}
    incremental, index, sequence = None, None, None
    try:
        retirements, recoveries = _rules()
        index = open_index(ledger, cache_mib=cache_mib())
        spool = shadow_spool(ledger)
        observe_into_spool(registry, spool=spool, state=derived(ledger) / "shadow-observe", read_at=now,
                           retirements=retirements, force_recompute=force_recompute)
        judged: dict[str, Any] = {}
        started = perf_counter()
        incremental = collect_incremental(judging_registry(registry, spool, retirements), ledger, index,
                                          publisher=True, limit=LIMIT_PUBLISHER, judge_only=True,
                                          retirements=retirements, recoveries=recoveries, timings=judged, now=now,
                                          force_recompute=force_recompute, rules_registry=registry)
        sequence = judged["incremental"]["sequence"]
        shadow.update(seconds=round(perf_counter() - started, 3), incremental=judged["incremental"])
        commit_pushed([Path(p) for p in incremental["bundle_paths"] if Path(p).parent == spool])
    except Exception as exc:  # The shadow reports; it never stops the full collection.
        shadow.update(status="failed", error_type=type(exc).__name__)
    result = full(now)
    try:
        if incremental is not None:
            shadow.update(_compare(index, ledger, incremental, result, sequence))
    except Exception as exc:
        shadow.update(status="failed", error_type=type(exc).__name__)
    finally:
        if index is not None:
            index.close()
    timings["incremental_collect_shadow"] = shadow
    return result


def _bundles_read(result: Mapping[str, Any]) -> dict[str, int]:
    from .receipts import reference
    return {s["source_ref"]: s["source_detail"]["bundles"] for s in result.get("sources", [])
            if s.get("kind") == "native_push" and s["source_ref"] != reference("source", "local-observations")}


def _compare(index, ledger: UsageLedger, incremental: Mapping[str, Any], full: Mapping[str, Any],
             sequence: int) -> dict[str, Any]:
    """Both sides' new facts at one prefix; comparable only when the full collection appended right after it
    and both read the same peer bundles."""
    index.catch_up(ledger)
    watermark, inserted = full["watermark"], full["inserted"]
    if (watermark, bool(inserted)) not in ((sequence, False), (sequence + 1, True)):
        return {"status": "prefix_moved", "sequence": sequence, "full_watermark": watermark}
    if _bundles_read(incremental) != _bundles_read(full):
        return {"status": "bundles_differ"}
    rows = index._rows("SELECT body FROM fact WHERE sequence = ?", (sequence + 1,)) if inserted else []
    full_refs = {r.fact_ref: r.entity_ref for r in rows}
    new = set(incremental["new_refs"])
    mine = {r.fact_ref: r.entity_ref for r in incremental["batch"] or [] if r.fact_ref in new}
    differ = sorted(set(full_refs) ^ set(mine))
    entities = sorted({full_refs.get(ref) or mine[ref] for ref in differ})
    return {"status": "compared", "sequence": sequence, "full": len(full_refs), "incremental": len(mine),
            "symmetric_difference": len(differ), "entities": entities[:SHADOW_SAMPLE]}


def peer_round(registry: Mapping[str, Any], ledger: UsageLedger, *, state: Path, deadline: float | None,
               timings: dict[str, Any], force_recompute: bool = False) -> dict[str, Any]:
    """`collect_local_mode = incremental`: own sources observed (continuing the observe-mode index under
    `state`) into the local spool, judged and ingested here; nothing is pushed but the position."""
    retirements, recoveries = _rules()
    index = open_index(ledger, cache_mib=cache_mib())
    try:
        started = perf_counter()
        observe_into_spool(registry, spool=local_spool(ledger), state=state,
                           read_at=utc_text(datetime.now(timezone.utc)), retirements=retirements,
                           force_recompute=force_recompute, deadline=deadline)
        timings["observe_seconds"] = round(perf_counter() - started, 3)
        return collect_incremental(judging_registry(registry, local_spool(ledger), retirements), ledger, index,
                                   publisher=False, limit=LIMIT_PEER, retirements=retirements,
                                   recoveries=recoveries, timings=timings, force_recompute=force_recompute,
                                   rules_registry=registry)
    finally:
        index.close()


def trial_round(registry: Mapping[str, Any], ledger: UsageLedger, *, spool: Path, out: Path,
                peer: str) -> dict[str, Any]:
    """Read-only trial: judge the copied bundles at the current prefix, ingest nothing, append one record.

    The copies are this node's pushed bundles as `peer` sent them. Its own
    index lives beside the records (`out`); the bundles judged leave the
    trial spool once recorded, refused ones stay and are counted. The record
    carries the new fact refs, so the publisher's ledger can be checked for them.
    """
    from .native_observations import commit_pushed
    retirements, recoveries = _rules()
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    index = open_index(ledger, cache_mib=cache_mib(), path=out / "fact-index.sqlite")
    started, timings = perf_counter(), {}
    try:
        judged = collect_incremental(judging_registry(registry, spool, retirements, peer=peer), ledger, index,
                                     publisher=False, limit=LIMIT_PEER, judge_only=True, retirements=retirements,
                                     recoveries=recoveries, timings=timings, rules_registry=registry)
    finally:
        index.close()
    detail = next(s["source_detail"] for s in judged["sources"]
                  if s.get("kind") == "native_push" and s["source_ref"] == reference("source", LOCAL_SOURCE_ID))
    # Exactly the bundles this judgement read; a refused one is skipped and stays in the spool.
    read = [Path(p) for p in judged["bundle_paths"] if Path(p).parent == spool]
    record = {"at": utc_text(datetime.now(timezone.utc)), "sequence": timings["incremental"]["sequence"],
              "bundles": [p.name for p in read], "invalid_bundles": detail["invalid_bundles"],
              "seconds": round(perf_counter() - started, 3), "peak_bytes": peak_bytes(),
              "incremental": timings["incremental"], "new_refs": judged["new_refs"],
              "new_refs_sha256": digest(judged["new_refs"])}
    fd = os.open(out / "rounds.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as handle:
        handle.write(canonical(record) + b"\n")
    commit_pushed(read)
    return {k: v for k, v in record.items() if k != "new_refs"}


def trial_summary(out: Path) -> dict[str, Any]:
    """The trial's rounds: peak memory, median and longest round."""
    rows = [json.loads(line) for line in (out / "rounds.jsonl").read_text().splitlines()] \
        if (out / "rounds.jsonl").exists() else []
    seconds = sorted(r["seconds"] for r in rows)
    return {"rounds": len(rows), "peak_bytes": max((r["peak_bytes"] for r in rows), default=0),
            "median_seconds": seconds[len(seconds) // 2] if seconds else None,
            "longest_seconds": seconds[-1] if seconds else None}
