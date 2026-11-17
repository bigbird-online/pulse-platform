"""Standing human decisions that one Agent's use of one entrypoint carries no WI.

A rule names an Agent, a node, a runtime and an entrypoint the operator
decided is never WI work (for example Pulse in the Codex desktop app on the
operator's own machine). Usage matching all of them by its recorded actor
identity, with no declaration, assignment or
classification of its own, is classified as non-WI work. A later declaration
is not overridden: a usage that already has a task claim is left alone.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping

from .events import parse_timestamp
from .receipts import Receipt, ReceiptError, digest, non_wi_attribution, reference

RULES = Path(__file__).resolve().parents[1] / "standing-non-wi.json"
SCHEMA = "computecost_standing_non_wi/v1"
_ACTOR_POLICY = b'"policy":"registered_runtime_actor.v1"'
_TARGET = b'"target_ref":"'


def load_rules(path: Path = RULES) -> list[Mapping[str, Any]]:
    if not path.is_file() or path.is_symlink():
        return []
    document = json.loads(path.read_bytes())
    if document.get("schema") != SCHEMA or not isinstance(document.get("rules"), list):
        raise ReceiptError("invalid standing non-WI rules")
    for rule in document["rules"]:
        if (not isinstance(rule, dict) or set(rule) != {"agent", "node", "runtime", "entrypoint", "decision_ref"}
                or not all(isinstance(rule[k], str) and rule[k] for k in rule)
                or not re.fullmatch(r"ccf1_[a-f0-9]{64}", rule["decision_ref"])):
            raise ReceiptError("invalid standing non-WI rule")
    return document["rules"]


def _target(row: Receipt) -> str | None:
    # Canonical payload bytes name the target without decoding every claim.
    at = row._payload_json.find(_TARGET)
    if at < 0:
        return None
    start = at + len(_TARGET)
    return row._payload_json[start:row._payload_json.index(b'"', start)].decode()


def standing_non_wi_receipts(facts: list[Receipt], observed: str,
                             rules: list[Mapping[str, Any]] | None = None) -> list[Receipt]:
    rules = load_rules() if rules is None else rules
    if not rules:
        return []
    marks = {rule["entrypoint"]: f'"entrypoint":"{rule["entrypoint"]}"'.encode() for rule in rules}
    matched: dict[str, tuple[Mapping[str, Any], Receipt]] = {}
    for row in facts:
        if (row.kind != "attribution" or row.producer != "registered_runtime_actor.v1"
                or _ACTOR_POLICY not in row._payload_json
                or not any(mark in row._payload_json for mark in marks.values())):
            continue
        p = row.payload
        for rule in rules:
            if (p.get("scope") == "actor" and p.get("executor") == rule["agent"]
                    and p["context"].get("node") == rule["node"]
                    and p["context"].get("entrypoint") == rule["entrypoint"]):
                matched[p["target_ref"]] = (rule, row)
    if not matched:
        return []
    claimed = set()
    for row in facts:
        if row.kind != "attribution" or row.producer == "registered_runtime_actor.v1":
            continue
        target = _target(row)
        if target in matched:
            p = row.payload
            # Any task claim, including an earlier classification, decides.
            if p.get("scope") != "actor" and (p.get("wi") is not None or p.get("work_kind") is not None
                                              or p.get("state") != "unassigned"):
                claimed.add(target)
    versions: dict[str, list[Receipt]] = {}
    for row in facts:
        if row.kind == "usage" and row.entity_ref in matched and row.entity_ref not in claimed:
            versions.setdefault(row.entity_ref, []).append(row)
    result = []
    for entity, rows in sorted(versions.items()):
        rule, actor = matched[entity]
        if any(r.payload.get("runtime") != rule["runtime"] or r.payload.get("wi") is not None for r in rows):
            continue
        # One deterministic version, so repeated collections assert one fact.
        first = min(rows, key=lambda r: (parse_timestamp(r.payload.get("interval_start") or r.occurred_at, "usage"),
                                         r.fact_ref))
        result.append(non_wi_attribution(first, work_kind="none", observed_at=observed, evidence=(
            rule["decision_ref"], reference("standing_non_wi_rule", digest(rule)), actor.fact_ref)))
    return result
