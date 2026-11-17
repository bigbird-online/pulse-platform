"""Keep one entity per registered runtime actor claim and revise it in place.

An actor entity names only what stays fixed for one claim: the target, the
producing path, node, entrypoint, workspace, input, subcall and tenant (see
`receipt_sources.actor_receipt`). Executor, effective time, evidence and a
tenant's usage version live in the payload. A collection settles each new
claim against the current heads:

- A claim whose entity already has a head is decided by that head alone:
  equal content (payload bytes and occurred_at) adds nothing, any other
  content supersedes the head and keeps its evidence. A head of another
  entity never stands in for it, since paths and sources are distinct
  claimants that may disagree and later agree.
- A claim whose entity has no head yet adds nothing when the older identity
  formula (target, executor, context, effective time and evidence) names a
  current head with its exact content: that head is this claim as the
  previous producer wrote it, so changing the formula appends no fact for
  an unchanged target. Equal content under any other entity, of the stable
  formula or with other evidence, proves no common source or path; the
  claim opens its own entity beside it.
- Otherwise the claim opens its entity and is appended as is.
- Within one settled list the last claim of an entity is the claim; earlier
  ones of the same list are dropped, so the same input settles the same way
  every collection and two claims never take turns revising one entity.
- A collector claim is anchored on the target's current version (the
  ledger head, or this collection's accepted revision), not on whichever
  candidate this collection re-parsed.

A claim bound to one exact usage version (top-level `usage_fact_ref`) keeps
its version in its identity and passes through unchanged.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from .receipts import Receipt, reference

PRODUCER = "registered_runtime_actor.v1"
_TARGET = b'"target_ref":"'


def _target(row: Receipt) -> str | None:
    # Canonical payloads sort keys: the nested context precedes the top-level
    # target_ref, so the last occurrence is the top-level one.
    data = row._payload_json
    start = data.rfind(_TARGET)
    if start < 0:
        return None
    start += len(_TARGET)
    return data[start:data.index(b'"', start)].decode()


def _legacy_entity(row: Receipt) -> str:
    """The entity the older identity formula gave this exact claim.

    That formula named a claim by the evidence it emitted; source evidence
    the collector adds to the stored head keeps the entity unchanged.
    """
    p = row.payload
    return reference(row.kind, ["runtime_actor", [p["target_ref"], p["executor"], p["context"],
                                                  p["effective_at"], list(row.evidence)]])


def _actor(row: Receipt) -> bool:
    return row.kind == "attribution" and row.producer == PRODUCER


def _anchored(row: Receipt, anchor: Receipt | None) -> Receipt:
    """The same claim with the effective time of the target's current version."""
    p = row.payload
    if (anchor is None or anchor.kind not in {"usage", "execution"}
            or anchor.entity_ref != p["target_ref"] or p["context"].get("tenant") is not None):
        return row
    effective = anchor.payload.get("interval_start") or anchor.occurred_at
    if effective is None or effective == p["effective_at"]:
        return row
    return Receipt.make(row.kind, None, {**p, "effective_at": effective}, occurred_at=effective,
                        observed_at=row.observed_at, producer=row.producer, evidence=row.evidence,
                        entity_ref=row.entity_ref)


class ActorHeads:
    """Current actor heads of one collection, by entity and by target.

    `heads` is the collector's entity -> current head map; settling a claim
    updates it in place, so later stages of the same collection see it.
    """

    def __init__(self, heads: dict[str, Receipt], rows: Iterable[Receipt]):
        self.heads = heads
        self.by_target: dict[str, list[Receipt]] = {}
        for row in rows:
            if _actor(row) and (target := _target(row)) is not None:
                self.by_target.setdefault(target, []).append(row)

    def note(self, row: Receipt) -> None:
        """Track an actor fact the caller appended without settling it."""
        if not _actor(row) or (target := _target(row)) is None:
            return
        current = self.by_target.setdefault(target, [])
        gone = {row.fact_ref, row.supersedes}
        current[:] = [head for head in current if head.fact_ref not in gone]
        current.append(row)

    def pending(self, row: Receipt) -> bool:
        """Whether this row is a new claim that `settle` must decide."""
        return (_actor(row) and row.supersedes is None
                and row.payload.get("scope") == "actor" and row.payload.get("usage_fact_ref") is None)

    def settle(self, row: Receipt, anchor: Receipt | None = None) -> Receipt | None:
        """The fact to append for one new claim, or None when it adds nothing."""
        row = _anchored(row, anchor)
        def same(head: Receipt) -> bool:
            return head._payload_json == row._payload_json and head.occurred_at == row.occurred_at

        prior = self.heads.get(row.entity_ref)
        if prior is None:
            legacy = _legacy_entity(row)
            if any(head.entity_ref == legacy and same(head)
                   for head in self.by_target.get(row.payload["target_ref"], ())):
                return None
        elif same(prior):
            return None
        else:
            row = Receipt.make(row.kind, None, row.payload, occurred_at=row.occurred_at,
                               observed_at=row.observed_at, producer=row.producer,
                               evidence=tuple(sorted(set(prior.evidence) | set(row.evidence))),
                               entity_ref=row.entity_ref, supersedes=prior.fact_ref)
        self.note(row)
        self.heads[row.entity_ref] = row
        return row

    def settle_all(self, rows: Iterable[Receipt],
                   anchors: Mapping[str, Receipt] | None = None) -> list[Receipt]:
        """Settle one producer's output; other facts keep their place."""
        rows = list(rows)
        last = {row.entity_ref: index for index, row in enumerate(rows) if self.pending(row)}
        result = []
        for index, row in enumerate(rows):
            if self.pending(row):
                if last[row.entity_ref] != index:
                    continue
                anchor = anchors.get(row.payload["target_ref"]) if anchors is not None else None
                row = self.settle(row, anchor)
                if row is None:
                    continue
            else:
                self.note(row)
            result.append(row)
        return result
