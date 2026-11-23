"""What `UsageLedger.ingest_receipts` asks about the ledger it appends to (WI754).

Two implementations answer the same four questions: does this fact exist,
which facts revise it, which unrevised facts make the same assertion, and
record a fact accepted earlier in this transaction. `TableLookup` builds
whole-ledger dictionaries from the verified read; `IndexLookup` asks the fact
index and keeps this transaction's accepted rows in memory over it. For any
candidate sequence both give the same answers, so the decisions, inserted
rows, duplicate counts and errors of an ingest do not depend on the choice.

One fact ref can occur in several batches: the ref omits `observed_at` and
`evidence`, and history holds such repeats. The tables keep, per ref, the
latest occurrence at the position of the first; the index returns every
occurrence in ledger order and `get` the latest, so `IndexLookup` folds its
lists the same way.
"""
from __future__ import annotations

from typing import Any, Iterable

from .receipts import Receipt


def assertion(row: Receipt) -> tuple:
    """The ledger's assertion identity: an evidence-only revision keeps it."""
    return (row.kind, row.entity_ref, row.occurred_at, row.producer, row._payload_json)


def _latest(rows: Iterable[Receipt]) -> list[Receipt]:
    """Rows in ledger order, one per fact ref: the latest occurrence at the first one's place."""
    by_ref: dict[str, Receipt] = {}
    for row in rows:
        by_ref[row.fact_ref] = row
    return list(by_ref.values())


class TableLookup:
    """Whole-ledger dictionaries, built once per transaction."""

    def __init__(self, batches: Iterable[Iterable[Receipt]]):
        self.known: dict[str, Receipt] = {row.fact_ref: row for rows in batches for row in rows}
        self.superseded = {r.supersedes for r in self.known.values() if r.supersedes}
        self._children: dict[str, dict[str, Receipt]] = {}
        self._active: dict[tuple, dict[str, Receipt]] = {}
        for fact in self.known.values():
            if fact.supersedes:
                self._children.setdefault(fact.supersedes, {})[fact.fact_ref] = fact
            if fact.fact_ref not in self.superseded:
                self._active.setdefault(assertion(fact), {})[fact.fact_ref] = fact

    def get(self, ref: str) -> Receipt | None:
        return self.known.get(ref)

    def children(self, ref: str) -> list[Receipt]:
        return list(self._children.get(ref, {}).values())

    def active(self, row: Receipt) -> list[Receipt]:
        """Unrevised facts asserting what `row` asserts, in ledger order."""
        return list(self._active.get(assertion(row), {}).values())

    def add(self, row: Receipt) -> None:
        if row.supersedes:
            self.superseded.add(row.supersedes)
            predecessor = self.known[row.supersedes]
            self._active.get(assertion(predecessor), {}).pop(row.supersedes, None)
            self._children.setdefault(row.supersedes, {})[row.fact_ref] = row
        self.known[row.fact_ref] = row
        if row.fact_ref not in self.superseded:
            self._active.setdefault(assertion(row), {})[row.fact_ref] = row


class IndexLookup:
    """Point reads on a fact index bound to exactly the ledger being appended."""

    def __init__(self, index: Any):
        from .fact_index import assertion_key  # fact_index imports the ledger module.
        self._index = index
        self._key = assertion_key
        self._read: dict[str, Receipt | None] = {}
        self._pending: dict[str, Receipt] = {}
        self._superseded: set[str] = set()
        self._children: dict[str, list[Receipt]] = {}
        self._active: dict[tuple, dict[str, Receipt]] = {}

    def get(self, ref: str) -> Receipt | None:
        row = self._pending.get(ref)
        if row is not None:
            return row
        if ref not in self._read:
            self._read[ref] = self._index.get(ref)
        return self._read[ref]

    def children(self, ref: str) -> list[Receipt]:
        return [*_latest(self._index.children(ref)), *self._children.get(ref, ())]

    def active(self, row: Receipt) -> list[Receipt]:
        wanted = assertion(row)
        # The stored key is a digest of the assertion; compare the whole of it.
        stored = [r for r in _latest(self._index.active_by_assertion(self._key(row)))
                  if assertion(r) == wanted and r.fact_ref not in self._superseded]
        return stored + list(self._active.get(wanted, {}).values())

    def add(self, row: Receipt) -> None:
        if row.supersedes:
            self._superseded.add(row.supersedes)
            predecessor = self.get(row.supersedes)
            self._active.get(assertion(predecessor), {}).pop(row.supersedes, None)
            self._children.setdefault(row.supersedes, []).append(row)
        self._pending[row.fact_ref] = row
        if row.fact_ref not in self._superseded:
            self._active.setdefault(assertion(row), {})[row.fact_ref] = row
