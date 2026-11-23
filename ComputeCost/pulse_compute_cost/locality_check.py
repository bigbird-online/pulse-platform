"""Offline locality check (WI754 gate G1): judge per session package, compare with the whole.

For one ledger prefix (watermark N), each phase runs in its own process and
leaves its results in the output directory:

* `index`: stream the fact index to N (`fact-index.sqlite`, `index.json`);
* `full`: `receipt_snapshot(watermark=N)` and one `_prepare_collaboration`;
  every row's outcome and the four global values (`full.sqlite`, `full.json`);
* `packages`: the same preparation per session package (with the shared
  facts, `session_packages.is_shared`) and once for the public package,
  compared row by row and for the global values; the eight largest packages
  are also prepared with the public package in a child process each, which
  reads nothing but those facts, to measure peak memory (`packages.sqlite`,
  `packages-diffs.jsonl`, `packages.json`);
* `stages`: the collection stages that depend only on the ledger and rule
  files, over all facts and then per package with the public package; the
  produced fact refs are compared stage by stage (`stages.json`);
* `report`: the summary and the gate checks (`locality-report.json`).

A difference names the entity, its package, the field and, per payload field,
the refs this package's facts name that lie in another package. Nothing here
writes the ledger or any production state.
"""
from __future__ import annotations

from collections import Counter
from hashlib import file_digest, sha256
from heapq import merge
import json
import multiprocessing
import os
from pathlib import Path
import resource
import sqlite3
import sys
from time import perf_counter
from typing import Any, Iterable, Iterator, Mapping

from .collaboration_query import _prepare_collaboration
from .events import parse_timestamp
from .fact_index import PUBLIC, FactIndex, stream_identity
from .ledger import LedgerError, UsageLedger
from .receipts import REF, Receipt, canonical, digest

PHASES = ("index", "full", "packages", "stages", "report")
PROBES = 8
MAX_DIFFS = 10_000
PROBE_LIMIT_BYTES = int(1.2 * 1024 ** 3)
LARGEST_SHARE = 0.25


def peak_bytes() -> int:
    """This process image's peak resident memory.

    Linux keeps `ru_maxrss` across exec, so a child would report the peak its
    parent had reached when it forked; `VmHWM` starts over with the new image.
    """
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def _write_json(path: Path, value: Any) -> None:
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(temp, path)


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise LedgerError(f"locality phase output missing: {path.name}")
    return json.loads(path.read_bytes())


def _row(row: Receipt | None) -> str | None:
    return canonical(row.to_dict()).decode() if row is not None else None


def outcome(prepared: tuple) -> tuple[Iterator[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Each row's reconciliation outcome (lazily, one row at a time) and the global values."""
    (active, conflicts, observation_groups, observation_attribution_conflicts, attr_conflicts,
     attribution_conflict_wis, non_wi_kinds, projected, all_wis, accepted_at, all_reconciled, reconciliation,
     actor_context, actor_evidence, actor_conflicts, _, _, _, attribution_sources) = prepared
    reconciled = {r.entity_ref: r for r in all_reconciled}
    conflicting, reasons = set(reconciliation["conflicting_refs"]), reconciliation["conflict_reasons"]
    suppressed = set(reconciliation["suppressed_aggregate_refs"])

    def rows() -> Iterator[tuple[str, dict[str, Any]]]:
        for r in projected:
            ref = r.entity_ref
            record = {"kind": r.kind, "projected": _row(r), "actor_context": actor_context.get(ref),
                      "actor_evidence": actor_evidence.get(ref), "actor_conflicts": actor_conflicts.get(ref),
                      "attr_conflict": ref in attr_conflicts}
            if r.kind == "usage":
                record.update(reconciled=ref in reconciled, row=_row(reconciled.get(ref)),
                              non_wi_kind=non_wi_kinds.get(ref), conflict=ref in conflicting,
                              conflict_reasons=reasons.get(ref), suppressed=ref in suppressed,
                              attribution_source=attribution_sources["sources"].get(ref),
                              observation_group=observation_groups.get(ref))
            if ref in conflicts:
                record["conflict_versions"] = sorted(v.fact_ref for v in conflicts[ref])
            yield ref, record
        shown = {r.entity_ref for r in projected}
        for ref, versions in conflicts.items():
            if ref not in shown:
                yield ref, {"kind": versions[0].kind, "conflict_versions": sorted(v.fact_ref for v in versions)}

    return rows(), {"all_wis": sorted(all_wis), "attribution_conflict_wis": sorted(attribution_conflict_wis),
                    "observation_attribution_conflicts": {k: sorted(v) for k, v in
                                                          observation_attribution_conflicts.items()},
                    "accepted_at": dict(accepted_at)}


def merge_globals(total: dict[str, Any], part: Mapping[str, Any]) -> None:
    for key in ("all_wis", "attribution_conflict_wis"):
        total[key] = sorted(set(total.get(key, [])) | set(part[key]))
    conflicts = total.setdefault("observation_attribution_conflicts", {})
    for ref, values in part["observation_attribution_conflicts"].items():
        conflicts[ref] = sorted(set(conflicts.get(ref, [])) | set(values))
    accepted = total.setdefault("accepted_at", {})
    for wi, at in part["accepted_at"].items():
        if wi not in accepted or parse_timestamp(at, "accepted_at") < parse_timestamp(accepted[wi], "accepted_at"):
            accepted[wi] = at


def _refs(value: Any, field: str) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        if REF.fullmatch(value):
            yield field, value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _refs(child, f"{field}.{key}" if field else key)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child, field)


def foreign_refs(index: FactIndex, component: int, facts: Iterable[Receipt], limit: int = 50) -> dict[str, list]:
    """Per payload field, refs these facts name that lie in another package (missing link keys)."""
    found: dict[str, set[str]] = {}
    for row in facts:
        for field, ref in [*_refs(row.payload, ""), *(("evidence", e) for e in row.evidence)]:
            other = index.component_of(ref)
            if other is not None and other != component and len(found.setdefault(field, set())) < limit:
                found[field].add(f"{ref}@{other}")
    return {field: sorted(refs) for field, refs in sorted(found.items()) if refs}


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _code_digest() -> str:
    """This package's source: the judgement every phase ran."""
    root = Path(__file__).parent
    return digest([(path.name, _file_digest(path)) for path in sorted(root.glob("*.py"))])


def _binding(ledger: UsageLedger, watermark: int, facts_sha256: str) -> dict[str, Any]:
    """Which ledger prefix a phase judged, and with which code."""
    return {"stream_identity": stream_identity(ledger), "watermark": watermark, "facts_sha256": facts_sha256,
            "code_sha256": _code_digest()}


def stage_inputs(registry: Mapping[str, Any]) -> dict[str, Any]:
    """Everything besides the ledger and the code that decides the stages' output.

    Each rule input is what the stage's own loader admits and reads (a refused
    file binds as its refusal), so a file the stage would ignore never matches
    one it used.
    """
    from .activity_classification import load_confirmations
    from .standing_non_wi import load_rules
    from .tenant_context import load_tenant_document

    def loaded(load) -> Any:
        try:
            return digest(load())
        except (OSError, ValueError) as exc:
            return {"refused": type(exc).__name__}

    path = registry.get("tenant_registry_path")
    return {"required_receipt_features": sorted(registry.get("required_receipt_features", [])),
            "tenant_registry_path": path,
            "tenant_registry": loaded(lambda: load_tenant_document(Path(path))) if isinstance(path, str) else None,
            "reviewed_activity": loaded(load_confirmations), "standing_non_wi": loaded(load_rules)}


def ledger_binding(ledger: UsageLedger, watermark: int) -> dict[str, Any]:
    """The registered ledger's prefix at the watermark now, read as a stream."""
    result, count, sequence = sha256(b"["), 0, 0
    for meta, lines in ledger.stream_batches():
        if meta["sequence"] > watermark:
            break
        sequence = meta["sequence"]
        for line in lines:
            if count:
                result.update(b",")
            result.update(line)
            count += 1
    if sequence != watermark:
        raise LedgerError("ledger is shorter than the watermark")
    result.update(b"]")
    return _binding(ledger, watermark, result.hexdigest())


def _require(name: str, found: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    if found.get("binding") != expected:
        raise LedgerError(f"{name} judged another ledger prefix; rerun the phases from index")


def _open_index(out: Path, ledger: UsageLedger, watermark: int) -> tuple[FactIndex, dict[str, Any]]:
    """The index file and the binding `index` recorded for it, checked against each other."""
    recorded = _read_json(out / "index.json")
    index = FactIndex.open(out / "fact-index.sqlite", stream_identity(ledger), cache_mib=256)
    bound = index.binding()
    if (bound.sequence != watermark or bound.head_sha256 != recorded.get("batch_sha256")
            or recorded.get("binding", {}).get("watermark") != watermark
            or recorded["binding"].get("stream_identity") != stream_identity(ledger)):
        raise LedgerError("fact index is not the indexed prefix at this watermark; run --phase index first")
    return index, recorded["binding"]


def phase_index(ledger: UsageLedger, out: Path, watermark: int) -> dict[str, Any]:
    started = perf_counter()
    index = FactIndex.open(out / "fact-index.sqlite", stream_identity(ledger), cache_mib=256)
    index.rebuild(ledger, upto=watermark)
    if index.binding().sequence != watermark:
        raise LedgerError("ledger is shorter than the watermark")
    sizes = [n for _, n, _ in index.packages()]
    stats = index.stats()
    buckets = Counter(10 ** len(str(n)) for n in sizes)
    result = {**stats, "seconds": round(perf_counter() - started, 3), "peak_bytes": peak_bytes(),
              "largest_share": round(stats["largest_package"] / stats["facts"], 6) if stats["facts"] else 0,
              "largest_packages": sizes[:PROBES], "package_sizes_below": dict(sorted(buckets.items())),
              "batch_sha256": index.binding().head_sha256,
              "binding": _binding(ledger, watermark, index.facts_digest())}
    _write_json(out / "index.json", result)
    return result


def phase_full(ledger: UsageLedger, out: Path, watermark: int) -> dict[str, Any]:
    started = perf_counter()
    snapshot = ledger.receipt_snapshot(watermark=watermark)
    if snapshot["watermark"] != watermark:
        raise LedgerError("ledger is shorter than the watermark")
    binding = _binding(ledger, watermark, snapshot["sha256"])
    rows, global_values = outcome(_prepare_collaboration(snapshot))
    del snapshot
    path = out / "full.sqlite"
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE result(entity TEXT PRIMARY KEY, record TEXT NOT NULL)")
    db.executemany("INSERT INTO result VALUES (?, ?)",
                   ((ref, canonical(record).decode()) for ref, record in rows))
    db.commit()
    count = db.execute("SELECT COUNT(*) FROM result").fetchone()[0]
    db.close()
    os.chmod(path, 0o600)
    result = {"watermark": watermark, "rows": count, "seconds": round(perf_counter() - started, 3),
              "peak_bytes": peak_bytes(), "globals": global_values, "binding": binding,
              "result_sha256": _file_digest(path)}
    _write_json(out / "full.json", result)
    return {k: v for k, v in result.items() if k != "globals"}


def package_facts(index: FactIndex, component: int, beside: list[tuple[int, Receipt]]) -> list[Receipt]:
    """A package's facts with `beside` (public facts with their positions), in ledger order."""
    return [row for _, row in merge(beside, index.facts_of(component, ids=True))]


def _probe(path: str, identity: str, component: int, connection) -> None:
    index = FactIndex.open(path, identity, cache_mib=16)
    started = perf_counter()
    _prepare_collaboration({"facts": package_facts(index, component, list(index.facts_of(PUBLIC, ids=True)))})
    connection.send({"seconds": round(perf_counter() - started, 3), "peak_bytes": peak_bytes()})


def probe(index: FactIndex, identity: str, component: int) -> dict[str, Any]:
    """Prepare one package with the public package in a fresh process; its time and peak resident memory.

    The child reads those facts from the index and nothing else of the ledger.
    """
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    child = context.Process(target=_probe, args=(str(index.path), identity, component, send))
    child.start()
    send.close()
    try:
        return receive.recv()
    except EOFError:
        return {"failed": True}
    finally:
        child.join()


def phase_packages(ledger: UsageLedger, out: Path, watermark: int) -> dict[str, Any]:
    index, binding = _open_index(out, ledger, watermark)
    recorded = _read_json(out / "full.json")
    _require("full", recorded, binding)
    if _file_digest(out / "full.sqlite") != recorded.get("result_sha256"):
        raise LedgerError("full results differ from the full phase record; rerun --phase full")
    expected = recorded["globals"]
    path = out / "packages.sqlite"
    path.unlink(missing_ok=True)
    db = sqlite3.connect(f"file:{path}", uri=True)
    db.execute("ATTACH DATABASE ? AS full", (f"file:{out / 'full.sqlite'}?mode=ro",))
    db.create_function("package", 1, index.component_of, deterministic=True)
    db.execute("CREATE TABLE placed AS SELECT entity, package(entity) AS component FROM full.result")
    db.execute("CREATE INDEX placed_component ON placed(component)")
    db.execute("CREATE TABLE package(id INTEGER PRIMARY KEY, facts INTEGER, usages INTEGER, seconds REAL, "
               "diffs INTEGER)")
    diffs_path = out / "packages-diffs.jsonl"
    fd = os.open(diffs_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    written, by_field, total = 0, Counter(), {}
    shared = list(index.shared_facts(ids=True))
    shared_entities = {row.entity_ref for _, row in shared}  # Judged with the public package.
    with os.fdopen(fd, "w", encoding="utf-8") as diffs:
        for component, facts_count, usages in [(PUBLIC, index.stats()["public_package"], 0), *index.packages()]:
            started = perf_counter()
            own = component == PUBLIC
            facts = list(index.facts_of(component)) if own else package_facts(index, component, shared)
            produced, part = outcome(_prepare_collaboration({"facts": facts}))
            rows = {ref: json.loads(canonical(record)) for ref, record in produced  # As stored by `full`.
                    if own or ref not in shared_entities}
            merge_globals(total, part)
            seconds = perf_counter() - started
            found = []
            stored = dict(db.execute("SELECT r.entity, r.record FROM full.result r JOIN placed p "
                                     "USING (entity) WHERE p.component = ?", (component,)))
            for ref in sorted(set(rows) | set(stored)):
                mine = rows.get(ref)
                theirs = json.loads(stored[ref]) if ref in stored else None
                if mine is None or theirs is None:
                    found.append((ref, "row", theirs is not None, mine is not None))
                    continue
                for field in sorted(set(mine) | set(theirs)):
                    if mine.get(field) != theirs.get(field):
                        found.append((ref, field, theirs.get(field), mine.get(field)))
            if found:
                foreign = foreign_refs(index, component, facts)
                for ref, field, full_value, package_value in found:
                    by_field[field] += 1
                    if written < MAX_DIFFS:
                        diffs.write(json.dumps({"entity": ref, "package": component, "field": field,
                                                "full": full_value, "package_value": package_value,
                                                "foreign_refs": foreign}, sort_keys=True) + "\n")
                        written += 1
            db.execute("INSERT INTO package VALUES (?, ?, ?, ?, ?)",
                       (component, facts_count, usages, round(seconds, 3), len(found)))
    db.commit()
    unplaced = db.execute("SELECT COUNT(*) FROM placed WHERE component IS NULL").fetchone()[0]
    global_diffs = {key: {"full_only": _difference(expected[key], total.get(key)),
                          "packages_only": _difference(total.get(key), expected[key])}
                    for key in expected if expected[key] != total.get(key)}
    identity = stream_identity(ledger)
    probes = {str(c): {"facts": n, **probe(index, identity, c)} for c, n, _ in index.packages()[:PROBES]}
    result = {"watermark": watermark, "binding": binding, "full_result_sha256": recorded["result_sha256"],
              "row_diffs": sum(by_field.values()), "row_diffs_by_field": dict(by_field),
              "unplaced_rows": unplaced, "global_diffs": global_diffs, "probes": probes,
              "seconds": round(db.execute("SELECT COALESCE(SUM(seconds), 0) FROM package").fetchone()[0], 3)}
    db.close()
    os.chmod(path, 0o600)
    _write_json(out / "packages.json", result)
    return result


def _difference(left: Any, right: Any) -> Any:
    left, right = left or {}, right or {}
    if isinstance(left, list) or isinstance(right, list):
        return sorted(set(left) - set(right))
    return {k: v for k, v in left.items() if right.get(k) != v}


def run_stages(facts: list[Receipt], registry: Mapping[str, Any], observed: str) -> dict[str, list[Receipt]]:
    """The ledger-only collection stages as `_collect_once` runs them, in its order.

    Same heads (`receipt_sources.ledger_heads`), same calls and the same actor
    settlement (`ActorHeads.settle_all`) for the stages the collection settles;
    each stage sees the rows of the earlier ones. Source-reading stages are absent.
    """
    from .activity_classification import collect_reviewed_activity
    from .native_inputs import merge_launches
    from .receipt_sources import ledger_heads
    from .runtime_subcalls import launch_attributions, subcall_purpose_attributions
    from .standing_non_wi import standing_non_wi_receipts
    from .tenant_context import collect_tenant_context
    from .turn_residual_purpose import turn_residual_receipts
    _, _, _, known_by_fact, actor_heads = ledger_heads(facts)
    batch: list[Receipt] = []
    produced: dict[str, list[Receipt]] = {}

    def stage(name, rows):
        produced[name] = list(rows)
        batch.extend(produced[name])

    stage("collect_reviewed_activity", actor_heads.settle_all(collect_reviewed_activity(facts + batch, registry,
                                                                                        observed)))
    if "runtime_subcall.v1" in registry.get("required_receipt_features", []):
        launches = merge_launches([], facts + batch)
        stage("launch_attributions", actor_heads.settle_all(launch_attributions(facts + batch, launches, observed)))
        stage("subcall_purpose_attributions", subcall_purpose_attributions(facts + batch, launches, observed))
    stage("turn_residual_receipts", turn_residual_receipts(facts + batch, observed))
    stage("standing_non_wi_receipts", standing_non_wi_receipts(facts + batch, observed))
    stage("collect_tenant_context", actor_heads.settle_all(collect_tenant_context(
        facts + batch, registry, observed, ledger_refs=known_by_fact)))
    return produced


def _target(row: Receipt) -> str:
    return row.payload.get("target_ref") if row.kind == "attribution" else row.entity_ref


def phase_stages(ledger: UsageLedger, registry: Mapping[str, Any], out: Path, watermark: int) -> dict[str, Any]:
    index, binding = _open_index(out, ledger, watermark)
    inputs = stage_inputs(registry)
    started = perf_counter()
    snapshot = ledger.receipt_snapshot(watermark=watermark)
    if _binding(ledger, snapshot["watermark"], snapshot["sha256"]) != binding:
        raise LedgerError("ledger prefix differs from the indexed prefix; rerun the phases from index")
    observed = snapshot["recorded_at"]
    whole = {name: {r.fact_ref: r for r in rows}
             for name, rows in run_stages(snapshot["facts"], registry, observed).items()}
    full_seconds = round(perf_counter() - started, 3)
    del snapshot
    parts: dict[str, dict[str, tuple[Receipt, int]]] = {name: {} for name in whole}
    started = perf_counter()
    public = list(index.facts_of(PUBLIC, ids=True))
    for component, _, _ in [(PUBLIC, 0, 0), *index.packages()]:
        facts = public if component == PUBLIC else merge(public, index.facts_of(component, ids=True))
        for name, rows in run_stages([r for _, r in facts], registry, observed).items():
            parts[name].update((r.fact_ref, (r, component)) for r in rows)
    foreign: dict[int, dict[str, list]] = {}

    def entry(ref: str, side: str, row: Receipt, component: int | None) -> dict[str, Any]:
        if component is not None and component not in foreign:
            foreign[component] = foreign_refs(index, component, index.facts_of(component))
        return {"fact_ref": ref, "side": side, "target": _target(row), "package": component,
                "foreign_refs": foreign.get(component, {})}

    diffs: dict[str, list[dict[str, Any]]] = {}
    for name in whole:
        listed = [entry(ref, "full_only", whole[name][ref], index.component_of(_target(whole[name][ref])))
                  for ref in sorted(set(whole[name]) - set(parts[name]))[:MAX_DIFFS]]
        listed += [entry(ref, "packages_only", *parts[name][ref])
                   for ref in sorted(set(parts[name]) - set(whole[name]))[:MAX_DIFFS]]
        if listed:
            diffs[name] = listed
    result = {"watermark": watermark, "binding": binding, "inputs": inputs, "full_seconds": full_seconds,
              "packages_seconds": round(perf_counter() - started, 3), "peak_bytes": peak_bytes(),
              "produced": {name: len(rows) for name, rows in whole.items()},
              "diff_counts": {name: len(listed) for name, listed in diffs.items()}, "diffs": diffs}
    _write_json(out / "stages.json", result)
    return {k: v for k, v in result.items() if k != "diffs"}


def phase_report(ledger: UsageLedger, registry: Mapping[str, Any], out: Path, watermark: int) -> dict[str, Any]:
    index, full = _read_json(out / "index.json"), _read_json(out / "full.json")
    packages, stages = _read_json(out / "packages.json"), _read_json(out / "stages.json")
    binding = index.get("binding")
    if not binding or binding.get("watermark") != watermark:
        raise LedgerError("locality phases ran at different watermarks")
    for name, found in (("full", full), ("packages", packages), ("stages", stages)):
        _require(name, found, binding)
    if packages.get("full_result_sha256") != full.get("result_sha256"):
        raise LedgerError("packages compared other full results; rerun --phase packages")
    if binding.get("code_sha256") != _code_digest():
        raise LedgerError("the phases ran other code; rerun them from index")
    if ledger_binding(ledger, watermark) != binding:
        raise LedgerError("the registered ledger is not the prefix the phases judged; rerun them from index")
    if stages.get("inputs") != stage_inputs(registry):
        raise LedgerError("stage configuration or rule files changed since --phase stages; rerun it")
    peaks = [p.get("peak_bytes") for p in packages["probes"].values()]
    gates = {"packages_zero_diff": packages["row_diffs"] == 0 and not packages["global_diffs"]
             and packages["unplaced_rows"] == 0,
             "stages_zero_diff": not stages["diff_counts"],
             "largest_package_within_share": index["largest_share"] <= LARGEST_SHARE,
             "probe_peak_within_limit": all(p is not None and p <= PROBE_LIMIT_BYTES for p in peaks)}
    report = {"watermark": watermark, "binding": binding, "inputs": stages["inputs"], "passed": all(gates.values()), "gates": gates,
              "index": index, "full": {k: v for k, v in full.items() if k != "globals"},
              "packages": packages, "stages": {k: v for k, v in stages.items() if k != "diffs"}}
    _write_json(out / "locality-report.json", report)
    return {"passed": report["passed"], "gates": gates, "report": str(out / "locality-report.json")}


def run(registry: Mapping[str, Any], ledger: UsageLedger, out: Path, watermark: int, phase: str) -> dict[str, Any]:
    if phase not in PHASES:
        raise LedgerError("unknown locality phase")
    if type(watermark) is not int or watermark < 1:
        raise LedgerError("locality check requires a positive watermark")
    target, root = out.resolve(), ledger.root.resolve()
    if target == root or root in target.parents:
        raise LedgerError("locality output must lie outside the ledger root")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if out.stat().st_mode & 0o077:
        raise LedgerError("locality output directory must be private")
    if phase == "index":
        return phase_index(ledger, out, watermark)
    if phase == "full":
        return phase_full(ledger, out, watermark)
    if phase == "packages":
        return phase_packages(ledger, out, watermark)
    if phase == "stages":
        return phase_stages(ledger, registry, out, watermark)
    return phase_report(ledger, registry, out, watermark)
