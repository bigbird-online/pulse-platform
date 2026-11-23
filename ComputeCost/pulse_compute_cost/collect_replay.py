"""Offline replay of observation bundles: full and incremental collection compared (WI754 step 4).

Two private copies of one ledger prefix (`full/ledger`, `incremental/ledger`)
take the same bundles one round at a time, each through its own collection:
the full one (`collect_registered_sources`) and the incremental one
(`incremental_collect.collect_incremental`), with the same observation time.
After every round the fact refs each ledger gained are compared (invariant 2:
the same deduplicated new facts); at the end, the two ledgers' fact refs.

Phases, one process each, all state under `--out` (0700/0600):

- `prepare`: copy the registered ledger's batches through `--watermark` into
  both copies, settle them with full collections until nothing is new (the
  prefix a full collection had judged), and build the incremental side's index;
- `replay`: judge the bundles of `--corpus` (spool file names, sequence order)
  not yet replayed; resumable; `--rounds` bounds one run;
- `report`: `replay-report.json`.

`rounds.jsonl` is the record of rounds, one per bundle name: a resumed replay
skips every bundle it names (also one logged just before an interruption that
missed `replay-state.json`), and the report counts its first line per bundle.

Nothing is written to the registered ledger or its spools.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Mapping

from .events import utc_text
from .incremental_collect import LIMIT_PUBLISHER, collect_incremental, open_index
from .ledger import LedgerError, UsageLedger
from .receipts import canonical

PHASES = ("prepare", "replay", "report")
SOURCE_ID = "replay-observations"
_BUNDLE = re.compile(r"^(\d{20})\.json\.gz$")
SETTLE_LIMIT = 5


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical(value) + b"\n")
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes()) if path.exists() else None


def _logged(log: Path) -> list[dict[str, Any]]:
    """The rounds of `rounds.jsonl`, the first line per bundle."""
    records: dict[str, dict[str, Any]] = {}
    for line in log.read_text().splitlines() if log.exists() else []:
        record = json.loads(line)
        records.setdefault(record["bundle"], record)
    return list(records.values())


def copy_prefix(ledger: UsageLedger, target: Path, watermark: int) -> int:
    """Write the ledger's batches through `watermark` as a local ledger at `target`; the batches written."""
    directory = target / "receipts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    written = 0
    for meta, lines in ledger.stream_batches():
        if meta["sequence"] > watermark:
            break
        batch = {**meta, "facts": [json.loads(line) for line in lines]}
        path = directory / f"{meta['sequence']:020d}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical(batch) + b"\n")
        written += 1
    if written != watermark:
        raise LedgerError("ledger is shorter than the watermark")
    return written


def replay_registry(registry: Mapping[str, Any], side: Path, peer: str) -> dict[str, Any]:
    """The registered rules and features; the only source is this side's spool."""
    return {**{k: v for k, v in registry.items() if k != "sources"}, "ledger_root": str(side / "ledger"),
            "sources": [{"id": SOURCE_ID, "kind": "native_push", "peer": peer, "path": str(side / "spool"),
                         "source_ids": sorted(_source_ids(registry, peer))}]}


def _source_ids(registry: Mapping[str, Any], peer: str) -> set[str]:
    ids = {s["id"] for s in registry.get("sources", []) if isinstance(s, Mapping) and s.get("kind") != "native_push"}
    for source in registry.get("sources", []):
        if isinstance(source, Mapping) and source.get("kind") == "native_push" and source.get("peer") == peer:
            ids |= set(source.get("source_ids", []))
    return ids


def _refs(ledger: UsageLedger) -> list[str]:
    return [row.fact_ref for rows in _rows(ledger) for row in rows]


def _rows(ledger: UsageLedger) -> list[list]:
    ledger.receipt_snapshot()
    return ledger._receipt_cache_rows or []


class Replay:
    """The two sides and their collections; a round stores one bundle in both spools and collects."""

    def __init__(self, registry: Mapping[str, Any], out: Path, peer: str, *,
                 retirements: Mapping[str, Any] | None = None, recoveries: Mapping[str, Any] | None = None,
                 limit: int = LIMIT_PUBLISHER, cache_mib: int = 256):
        self.out, self.peer = out, peer
        self.retirements = {} if retirements is None else retirements
        self.recoveries = {} if recoveries is None else recoveries
        self.registries = {}
        for side in ("full", "incremental"):
            self.registries[side] = replay_registry(registry, out / side, peer)
            (out / side / "spool").mkdir(mode=0o700, parents=True, exist_ok=True)
        self.ledgers = {side: UsageLedger(out / side / "ledger") for side in ("full", "incremental")}
        self.index = open_index(self.ledgers["incremental"], cache_mib=cache_mib,
                                path=out / "incremental" / "fact-index.sqlite")
        self.limit = limit

    def close(self) -> None:
        self.index.close()

    def settle(self, now: str) -> list[int]:
        """Full collections on both sides until neither appends; the facts each appended."""
        from .receipt_sources import collect_registered_sources
        inserted = []
        for _ in range(SETTLE_LIMIT):
            counts = [collect_registered_sources(self.registries[s], self.ledgers[s], retirements=self.retirements,
                                                 recoveries=self.recoveries, now=now)["inserted"]
                      for s in ("full", "incremental")]
            inserted.append(counts)
            if counts == [0, 0]:
                return inserted
        raise LedgerError("prefix does not settle under full collection")

    def round(self, data: bytes, now: str) -> dict[str, Any]:
        """Store the bundle in both spools, collect on each side; what each ledger gained."""
        from .native_observations import store
        from .receipt_sources import collect_registered_sources
        before = {side: set(_refs(self.ledgers[side])) for side in self.ledgers}
        for side in self.ledgers:
            store(self.out / side / "spool", self.peer, data)
        result: dict[str, Any] = {}
        started = perf_counter()
        collect_registered_sources(self.registries["full"], self.ledgers["full"], retirements=self.retirements,
                                   recoveries=self.recoveries, now=now)
        result["full_seconds"] = round(perf_counter() - started, 3)
        started, timings = perf_counter(), {}
        collect_incremental(self.registries["incremental"], self.ledgers["incremental"], self.index,
                            publisher=True, limit=self.limit, retirements=self.retirements,
                            recoveries=self.recoveries, timings=timings, now=now)
        result["incremental_seconds"] = round(perf_counter() - started, 3)
        result["incremental"] = timings.get("incremental", {})
        gained = {side: set(_refs(self.ledgers[side])) - before[side] for side in self.ledgers}
        difference = sorted(gained["full"] ^ gained["incremental"])
        result.update(full_new=len(gained["full"]), incremental_new=len(gained["incremental"]),
                      symmetric_difference=len(difference), sample=difference[:5],
                      spooled={side: len(list((self.out / side / "spool").glob("*.json.gz"))) for side in self.ledgers})
        return result


def _registry_ledger(registry: Mapping[str, Any]) -> UsageLedger:
    from .observation_authority import registered_ledger
    return registered_ledger(registry, read_only=True)


def run(registry: Mapping[str, Any], out: Path, phase: str, *, watermark: int | None = None,
        corpus: Path | None = None, peer: str = "ownermac", rounds: int | None = None,
        ledger: UsageLedger | None = None, now=None, limit: int = LIMIT_PUBLISHER) -> dict[str, Any]:
    """One phase; `now` (a callable giving the round's observation time) defaults to the wall clock."""
    from datetime import datetime, timezone
    from .coverage_recovery import load_recoveries
    from .source_retirement import load_retirements
    clock = now or (lambda: utc_text(datetime.now(timezone.utc)))
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = out / "replay-state.json"
    state = _read_json(state_path) or {}
    ids = _source_ids(registry, peer)

    def replay() -> Replay:
        return Replay(registry, out, peer, retirements=load_retirements(),
                      recoveries=load_recoveries(), limit=limit)
    if phase == "prepare":
        if watermark is None:
            raise LedgerError("prepare requires --watermark")
        if state:
            raise LedgerError("replay directory is already prepared")
        source = ledger or _registry_ledger(registry)
        for side in ("full", "incremental"):
            (out / side).mkdir(mode=0o700)
            copy_prefix(source, out / side / "ledger", watermark)
        started = perf_counter()
        sides = replay()
        try:
            settled = sides.settle(clock())  # The first incremental round takes this prefix as judged.
        finally:
            sides.close()
        state = {"watermark": watermark, "peer": peer, "source_ids": sorted(ids), "settled": settled,
                 "prepare_seconds": round(perf_counter() - started, 3), "replayed": []}
        _write_json(state_path, state)
        return state
    if not state:
        raise LedgerError("run --phase prepare first")
    if phase == "replay":
        if corpus is None:
            raise LedgerError("replay requires --corpus")
        log = out / "rounds.jsonl"
        logged = [r["bundle"] for r in _logged(log)]
        state["replayed"] += sorted(set(logged) - set(state["replayed"]))
        done = set(state["replayed"])
        pending = sorted(p for p in corpus.iterdir() if _BUNDLE.fullmatch(p.name) and p.name not in done)
        if rounds is not None:
            pending = pending[:rounds]
        sides = replay()
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as rows:
                for path in pending:
                    data = path.read_bytes()
                    record = {"bundle": path.name, "bytes": len(data), **sides.round(data, clock())}
                    rows.write(json.dumps(record, sort_keys=True) + "\n")
                    rows.flush()
                    state["replayed"].append(path.name)
                    _write_json(state_path, state)
        finally:
            sides.close()
        return {"replayed": len(pending), "total": len(state["replayed"])}
    if phase == "report":
        records = _logged(out / "rounds.jsonl")
        sides = {side: UsageLedger(out / side / "ledger") for side in ("full", "incremental")}
        final = {side: set(_refs(sides[side])) for side in sides}
        report = {"watermark": state["watermark"], "rounds": len(records),
                  "bundles_16mb": sum(r["bytes"] >= 16 * 1024 * 1024 for r in records),
                  "rounds_with_difference": [r["bundle"] for r in records if r["symmetric_difference"]],
                  "ledger_difference": len(final["full"] ^ final["incremental"]),
                  "unsettled_rounds": sum(bool(r["incremental"].get("working_set_unsettled")) for r in records),
                  "full_seconds": sum(r["full_seconds"] for r in records),
                  "incremental_seconds": sum(r["incremental_seconds"] for r in records)}
        report["gate"] = (report["rounds"] >= 200 and report["bundles_16mb"] >= 1
                          and not report["rounds_with_difference"] and not report["ledger_difference"])
        _write_json(out / "replay-report.json", report)
        return report
    raise LedgerError("unknown replay phase")
