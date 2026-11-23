"""Retire registered sources by an explicit, reviewed repository declaration.

A retired source is no longer read on any node. Its coverage keeps every
historical version; the collector appends one excluded version per coverage
entity as that entity's current state. Absence from a registry is not
retirement, and retirement never removes usage, attribution or price facts.
"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .receipts import (Receipt, ReceiptError, SOURCE_RETIREMENT_PRODUCER, SOURCE_RETIREMENT_REASON,
                       is_source_retirement, reference)

SCHEMA = "computecost_retired_sources/v1"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "retired-sources.json"
KINDS = frozenset({"relay_host", "pi", "cc", "codex", "agy", "harness_bundle", "harness_root", "harness_core", "receipts"})
FIELDS = {"id", "kind", "retired_on", "authorization"}
OPTIONAL = {"note"}  # Human explanation only; not part of the declaration identity.


def load_retirements(path: Path = DEFAULT_PATH) -> dict[str, dict[str, str]]:
    """Source ID -> declaration. A malformed declaration stops collection."""
    document = json.loads(path.read_bytes())
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA or not isinstance(document.get("sources"), list):
        raise ReceiptError("invalid retired source declaration")
    result: dict[str, dict[str, str]] = {}
    for entry in document["sources"]:
        if (not isinstance(entry, Mapping) or not FIELDS <= set(entry) <= FIELDS | OPTIONAL
                or not isinstance(entry.get("note", ""), str)
                or not isinstance(entry["id"], str) or not entry["id"] or entry["id"] in result
                or entry["kind"] not in KINDS
                or not isinstance(entry["authorization"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["authorization"])
                or not isinstance(entry["retired_on"], str)):
            raise ReceiptError("invalid retired source declaration")
        date.fromisoformat(entry["retired_on"])
        result[entry["id"]] = dict(entry)
    return result


def declaration_ref(entry: Mapping[str, str]) -> str:
    return reference("source_retirement", [entry["id"], entry["kind"], entry["retired_on"], entry["authorization"]])


def retirement_receipts(active: list[Receipt], conflicts: Mapping[str, list[Receipt]],
                        retirements: Mapping[str, Mapping[str, str]], observed: str) -> list[Receipt]:
    """One excluded successor per coverage entity of each retired source.

    It supersedes the lowest head and names every current head in evidence, so
    it is the only successor of concurrent heads. Payload, predecessor and
    evidence are independent of the collecting node and time: every node
    appends the same fact, and an entity already retired gains nothing.
    """
    sources = {reference("source", source_id): entry for source_id, entry in retirements.items()}
    heads: dict[str, list[Receipt]] = {}
    for row in [*active, *(r for versions in conflicts.values() for r in versions)]:
        if (row.kind == "coverage" and row.producer in {"registered_collector.v1", SOURCE_RETIREMENT_PRODUCER}
                and row.payload["source_ref"] in sources):
            heads.setdefault(row.entity_ref, []).append(row)
    result = []
    for entity, rows in sorted(heads.items()):
        if len(rows) == 1 and is_source_retirement(rows[0]):
            continue
        first = rows[0].payload
        if any((r.payload["source_ref"], r.payload["domain"], r.payload["wi"])
               != (first["source_ref"], first["domain"], first["wi"]) for r in rows):
            raise ReceiptError("retired coverage entity has inconsistent identity")
        refs = sorted(r.fact_ref for r in rows)
        result.append(Receipt.make("coverage", None, {
            "domain": first["domain"], "wi": first["wi"], "source_ref": first["source_ref"],
            "status": "excluded", "expected": None, "observed": None, "watermark": None,
            "reason": SOURCE_RETIREMENT_REASON},
            occurred_at=None, observed_at=observed, producer=SOURCE_RETIREMENT_PRODUCER,
            evidence=tuple(sorted({*refs, declaration_ref(sources[first["source_ref"]])})),
            entity_ref=entity, supersedes=refs[0]))
    return result
