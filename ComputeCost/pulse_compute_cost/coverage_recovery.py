"""Recover a forked coverage entity by an explicit, reviewed repository declaration.

A declaration names concurrent coverage heads that are not assertions of the
source's state, for example a transported copy whose predecessors a ledger
rebased. When exactly one other head remains, the source's own collector
appends its fresh snapshot as that head's successor and names the declared
heads in evidence, so the new version is the only successor of all heads.
No stored fact is rewritten; a head the declaration does not name still
conflicts.
"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
from typing import Mapping

from .receipts import Receipt, ReceiptError, reference

SCHEMA = "computecost_coverage_recoveries/v1"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "coverage-recoveries.json"
FIELDS = {"id", "domain", "wi", "abandoned_heads", "recovered_on", "authorization"}
OPTIONAL = {"note"}  # Human explanation only; not part of the declaration identity.
DIGEST = re.compile(r"[0-9a-f]{64}")
FACT_REF = re.compile(r"ccf1_[0-9a-f]{64}")


def load_recoveries(path: Path = DEFAULT_PATH) -> dict[str, dict]:
    """Coverage entity reference -> declaration. A malformed declaration stops collection."""
    document = json.loads(path.read_bytes())
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA or not isinstance(document.get("entities"), list):
        raise ReceiptError("invalid coverage recovery declaration")
    result: dict[str, dict] = {}
    for entry in document["entities"]:
        if (not isinstance(entry, Mapping) or not FIELDS <= set(entry) <= FIELDS | OPTIONAL
                or not isinstance(entry.get("note", ""), str)
                or not isinstance(entry["id"], str) or not entry["id"]
                or not isinstance(entry["domain"], str) or not entry["domain"]
                or not (entry["wi"] is None or isinstance(entry["wi"], str) and entry["wi"])
                or not isinstance(entry["abandoned_heads"], list) or not entry["abandoned_heads"]
                or any(not isinstance(r, str) or not FACT_REF.fullmatch(r) for r in entry["abandoned_heads"])
                or len(set(entry["abandoned_heads"])) != len(entry["abandoned_heads"])
                or not isinstance(entry["authorization"], str) or not DIGEST.fullmatch(entry["authorization"])
                or not isinstance(entry["recovered_on"], str)):
            raise ReceiptError("invalid coverage recovery declaration")
        date.fromisoformat(entry["recovered_on"])
        entity = coverage_entity(entry["id"], entry["domain"], entry["wi"])
        if entity in result:
            raise ReceiptError("invalid coverage recovery declaration")
        result[entity] = dict(entry)
    return result


def coverage_entity(source_id: str, domain: str, wi: str | None) -> str:
    return reference("coverage", [reference("source", source_id), domain, wi or "global"])


def declaration_ref(entry: Mapping) -> str:
    return reference("coverage_recovery", [entry["id"], entry["domain"], entry["wi"] or "global",
                                           *sorted(entry["abandoned_heads"]), entry["recovered_on"],
                                           entry["authorization"]])


def recovered_coverage(fresh: Receipt, heads: list[Receipt],
                       recoveries: Mapping[str, Mapping]) -> Receipt | None:
    """The fresh snapshot as the only successor of all heads, or None.

    Applies only while a declared head is still current and exactly one
    undeclared head remains. That head is the predecessor; the declared current
    heads and the declaration are the evidence.
    """
    entry = recoveries.get(fresh.entity_ref)
    if entry is None:
        return None
    abandoned = sorted(r.fact_ref for r in heads if r.fact_ref in set(entry["abandoned_heads"]))
    remaining = [r for r in heads if r.fact_ref not in set(abandoned)]
    if not abandoned or len(remaining) != 1:
        return None
    return Receipt.make("coverage", None, fresh.payload, occurred_at=None, observed_at=fresh.observed_at,
                        producer=fresh.producer, evidence=tuple(sorted({*abandoned, declaration_ref(entry)})),
                        entity_ref=fresh.entity_ref, supersedes=remaining[0].fact_ref)
