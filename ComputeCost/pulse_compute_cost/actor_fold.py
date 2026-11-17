"""Read-time compact view of registered runtime actor heads in one query.

Many actor heads of one target assert the same identity and differ only in
their evidence (and therefore their entity and fact refs). The collaboration
query reads such a head only through its payload, producer and whether it has
any evidence; identity evidence lists every fact ref. So one representative
per group stands for the group, and the group keeps all its fact refs.

Nothing is written. The ledger, the snapshot and the collection path keep
every fact; only this query's current-head list is shorter. A head whose own
entity is the target of another claim (a task attribution or a stage link)
stays unfolded, so a lookup by that entity sees exactly what it saw before.
Set PULSE_COMPUTE_COST_ACTOR_FOLD=0 to read every head unfolded.
"""
from __future__ import annotations

import os
from typing import Iterable

from .receipts import Receipt

ACTOR_FOLD_ENV = "PULSE_COMPUTE_COST_ACTOR_FOLD"
ACTOR_PRODUCER = "registered_runtime_actor.v1"


def actor_fold_enabled(value: bool | None = None) -> bool:
    """An explicit choice, else the environment; folding is the default."""
    if value is not None:
        return bool(value)
    return os.environ.get(ACTOR_FOLD_ENV, "1").strip() != "0"


def fold_actor_heads(active: list[Receipt], conflict_rows: Iterable[Receipt]
                     ) -> tuple[list[Receipt], dict[str, tuple[str, ...]]]:
    """(active with one representative per actor group, rep fact ref -> all fact refs).

    A group is the actor heads with the same producer, occurred_at and exact
    payload bytes: the same target, executor, context, usage version and
    effective time. Order is kept; the representative is the group's first
    head that has evidence, else its first head. Only groups of two or more
    heads appear in the returned map.
    """
    claimed: set[str] = set()
    candidates: dict[bytes, list[Receipt]] = {}
    for row in active:
        if row.kind == "attribution" and row.producer == ACTOR_PRODUCER:
            candidates.setdefault(row._payload_json, []).append(row)
        elif row.kind == "attribution":
            claimed.add(row.payload["target_ref"])
        elif row.kind == "stage_link":
            claimed.update(row.payload["target_refs"])
    for row in conflict_rows:
        if row.kind == "attribution" and row.payload.get("scope") != "actor":
            claimed.add(row.payload["target_ref"])
        elif row.kind == "stage_link":
            claimed.update(row.payload["target_refs"])
    groups = []
    for rows in candidates.values():
        p = rows[0].payload  # Equal bytes: one decode decides every row.
        if p.get("scope") != "actor":
            # A non-actor claim of this producer targets its usage like any other.
            claimed.add(p["target_ref"])
        elif len(rows) > 1:
            by_time: dict[str | None, list[Receipt]] = {}
            for row in rows:
                by_time.setdefault(row.occurred_at, []).append(row)
            groups.extend(same for same in by_time.values() if len(same) > 1)
    del candidates
    dropped: set[int] = set()
    members: dict[str, tuple[str, ...]] = {}
    for rows in groups:
        foldable = [row for row in rows if row.entity_ref not in claimed]
        if len(foldable) < 2:
            continue
        rep = next((row for row in foldable if row.evidence), foldable[0])
        members[rep.fact_ref] = tuple(row.fact_ref for row in foldable)
        dropped.update(id(row) for row in foldable if row is not rep)
    if not dropped:
        return active, members
    return [row for row in active if id(row) not in dropped], members
