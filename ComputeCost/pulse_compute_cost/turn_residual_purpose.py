"""A CC turn counter inherits the WI of the requests it accompanied.

A CC SDK result is a per-model counter of the session (`turn_aggregate`).
Models that write their own request records (the main model) are itemized and
attributed request by request. A model that writes none, such as Haiku called
inside a turn, leaves only the counter, which no request declaration reaches.

Such a counter takes the WI that the main-model requests of the same native
session, inside the counter's own interval, were attributed to by their
request purpose or by a human review — only when there is at least one such
request and all of them agree. Any request without one assigned WI in that
interval leaves the counter unassigned. An existing task claim of another
primary producer is never overridden; holder supplements are not claims.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from typing import Iterable

from .events import parse_timestamp
from .receipts import Receipt, current_receipts, reference, supplement_source

POLICY = "cc_turn_residual_purpose.v1"
# Primary claims that decide one request's WI.
REQUEST_POLICIES = frozenset({"cc_native_request_purpose.v1", "reviewed_activity.v1"})
_COUNTER = (b'"granularity":"turn_aggregate"', b'"mapping":"cc_sdk_result.v1"')
_REQUEST = (b'"granularity":"request"', b'"mapping":"cc_message.v1"')


def _field(row: Receipt, name: bytes) -> str | None:
    """A string field from canonical payload bytes, without decoding the row."""
    mark = b'"' + name + b'":"'
    at = row._payload_json.find(mark)
    if at < 0:
        return None
    start = at + len(mark)
    return row._payload_json[start:row._payload_json.index(b'"', start)].decode()


def _counters_and_requests(facts: Iterable[Receipt]) -> tuple[list[Receipt], list[Receipt]]:
    counters, requests = [], []
    for row in facts:
        if row.kind != "usage":
            continue
        raw = row._payload_json
        if all(mark in raw for mark in _COUNTER):
            counters.append(row)
        elif all(mark in raw for mark in _REQUEST):
            requests.append(row)
    return counters, requests


def _instant(row: Receipt) -> str | None:
    return row.payload.get("interval_end") or row.occurred_at


def turn_residual_receipts(facts: list[Receipt], observed: str) -> list[Receipt]:
    counters, requests = _counters_and_requests(facts)
    counters = [r for r in counters if r.payload.get("native_scope_ref") and r.payload.get("interval_start")
                and _instant(r) and r.payload.get("wi") is None]
    if not counters:
        return []
    scopes = {r.payload["native_scope_ref"] for r in counters}
    by_scope: dict[str, list[tuple]] = defaultdict(list)
    for row in requests:
        scope = _field(row, b"native_scope_ref")
        if scope in scopes and (instant := _instant(row)):
            by_scope[scope].append((parse_timestamp(instant, "request"), row.entity_ref))
    for rows in by_scope.values():
        rows.sort()
    times = {scope: [at for at, _ in rows] for scope, rows in by_scope.items()}
    targets = {r.entity_ref for r in counters} | {e for rs in by_scope.values() for _, e in rs}
    claims: dict[str, list[Receipt]] = defaultdict(list)
    attributions = [r for r in facts if r.kind == "attribution" and _field(r, b"target_ref") in targets]
    active, conflicts = current_receipts(attributions)
    heads = list(active) + [r for rows in conflicts.values() for r in rows]
    for row in heads:
        p = row.payload
        if p.get("scope") == "actor" or supplement_source(row) is not None:
            continue
        claims[p["target_ref"]].append(row)
    result = []
    for counter in sorted(counters, key=lambda r: r.fact_ref):
        if any(c.producer != POLICY for c in claims.get(counter.entity_ref, [])):
            continue
        start = parse_timestamp(counter.payload["interval_start"], "counter start")
        end = parse_timestamp(_instant(counter), "counter end")
        scope = counter.payload["native_scope_ref"]
        rows, at = by_scope.get(scope, []), times.get(scope, [])
        inside = {e for _, e in rows[bisect_left(at, start):bisect_right(at, end)]}
        if not inside:
            continue
        wis, evidence = set(), {counter.fact_ref}
        for entity in inside:
            decided = claims.get(entity, [])
            if (len(decided) != 1 or decided[0].payload.get("policy") not in REQUEST_POLICIES
                    or decided[0].payload.get("state") != "assigned" or not decided[0].payload.get("wi")):
                wis = set()
                break
            wis.add(decided[0].payload["wi"])
            evidence.add(decided[0].fact_ref)
        if len(wis) != 1:
            continue
        payload = {"target_ref": counter.entity_ref, "wi": next(iter(wis)), "executor": None,
                   "responsible": None, "delegator": None, "state": "assigned", "policy": POLICY,
                   "effective_at": counter.payload["interval_start"]}
        prior = [c for c in claims.get(counter.entity_ref, []) if c.producer == POLICY]
        if any(c.payload == payload for c in prior):
            continue
        entity = reference("attribution", [POLICY, counter.entity_ref])
        for predecessor in prior or [None]:
            result.append(Receipt.make("attribution", None, payload, entity_ref=entity,
                supersedes=predecessor.fact_ref if predecessor else None,
                occurred_at=counter.payload["interval_start"], observed_at=observed, producer=POLICY,
                evidence=tuple(sorted(evidence))))
    return result
