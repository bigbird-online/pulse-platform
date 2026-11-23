"""Resolved store: each session package's prepared slice, kept current (WI754 step 3).

`<ledger root>/derived/resolved.sqlite`, private, on the publisher only. It
derives from the fact index and is rebuilt from it: the store binds the index
build, the judgement code and the tenant registry digest, and any change
empties it. `refresh` recomputes exactly the packages whose facts changed since
the slices were taken, and the public package when it changed; `load` merges a
window's slices with every package's global contributions into one
preparation for `_query_snapshot`.

`usage_resolved` holds each reconciled usage row once per attribution week
(metered rows once per day of their metering points): its grouping keys,
Token counts and time. `reconcile` sums it over a view's window and compares
the result with the view's groups; a publication that disagrees is withheld.

Invariants: a usage entity lies in exactly one package's slice and in no
other package's `usage_resolved` rows; a store built from empty equals one
refreshed batch by batch; the merged views equal the whole-ledger views apart
from `report_sha256`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from . import package_slices
from .collaboration_query import Prepared, UNKNOWN, dimension_key, wi_work_key
from .events import parse_timestamp
from .fact_index import PUBLIC, FactIndex
from .locality_check import package_facts
from .ledger import LedgerError
from .receipts import TOKEN_FIELDS, digest

SCHEMA_VERSION = "3"
# A second writer waits for the first refresh (publications are serialized by cost-refresh.lock anyway).
BUSY_SECONDS = 3600
ZONE = ZoneInfo("Asia/Shanghai")
DIMENSIONS = ("tenant", "agent", "wi", "entrypoint", "node", "runtime", "model")
_TABLES = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS package_slice(component INTEGER PRIMARY KEY, sequence INTEGER NOT NULL,
  slice BLOB NOT NULL, slice_sha256 TEXT NOT NULL, t_lo TEXT, t_hi TEXT, open_ended INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS package_slice_time ON package_slice(t_lo, t_hi);
CREATE TABLE IF NOT EXISTS package_global(component INTEGER PRIMARY KEY, value BLOB NOT NULL,
  value_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage_resolved(entity_ref BLOB NOT NULL, week_start TEXT NOT NULL, part TEXT NOT NULL,
  component INTEGER NOT NULL, included INTEGER NOT NULL, timing TEXT NOT NULL, t_lo TEXT, t_hi TEXT,
  tenant TEXT NOT NULL, agent TEXT NOT NULL, wi TEXT NOT NULL, entrypoint TEXT NOT NULL, node TEXT NOT NULL,
  runtime TEXT NOT NULL, model TEXT NOT NULL,
  uncached_input INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER,
  reported_total INTEGER, final INTEGER NOT NULL,
  PRIMARY KEY(entity_ref, week_start, part)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS usage_resolved_component ON usage_resolved(component);
CREATE INDEX IF NOT EXISTS usage_resolved_week ON usage_resolved(week_start);
CREATE TABLE IF NOT EXISTS package_report(component INTEGER PRIMARY KEY, value BLOB NOT NULL,
  value_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS package_assignment(ref TEXT NOT NULL, component INTEGER NOT NULL,
  PRIMARY KEY(ref, component)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS package_assignment_component ON package_assignment(component);
"""


def _chunks(values: list, size: int = 500):
    for start in range(0, len(values), size):
        yield values[start:start + size]


class _index_snapshot:
    """One read transaction on the fact index connection: every read sees one index state."""
    def __init__(self, index: FactIndex):
        self.db = index._db

    def __enter__(self):
        if not self.db.in_transaction:
            self.db.execute("BEGIN")
            self.db.execute("SELECT COUNT(*) FROM meta").fetchone()
            self.opened = True
        else:
            self.opened = False

    def __exit__(self, *exc):
        if self.opened and self.db.in_transaction:
            self.db.execute("ROLLBACK")


class StoreMismatch(LedgerError):
    """The store does not derive from this index prefix; rebuild it."""


def store_path(ledger_root: Path) -> Path:
    return Path(ledger_root) / "derived" / "resolved.sqlite"


def _monday(moment: datetime) -> str:
    day = moment.astimezone(ZONE).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def usage_parts(row, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The row's time parts: one, or one per local day of its metering points.

    `timing` says how `_period` reads it: `interval` (start..end), `point`
    (occurred_at), `counter` (an end without start), `none` (no time) or
    `metered` (exact day amounts).
    """
    metering = payload.get("metering")
    tokens = payload["tokens"]
    if metering:
        days: dict[str, list] = {}
        for point in metering["points"]:
            days.setdefault(parse_timestamp(point["at"], "metered at").astimezone(ZONE).date().isoformat(),
                            []).append(point)
        return [{"part": day, "week_start": (date.fromisoformat(day) - timedelta(
                    days=date.fromisoformat(day).weekday())).isoformat(), "timing": "metered",
                 "t_lo": package_slices.instant_text(points[0]["at"]),
                 "t_hi": package_slices.instant_text(points[-1]["at"]),
                 "tokens": {k: sum(p["tokens"][k] for p in points) for k in TOKEN_FIELDS},
                 "reported_total": None}
                for day, points in sorted(days.items())]
    start, end = payload.get("interval_start"), payload.get("interval_end")
    if start is not None:
        timing, low, high = "interval", start, end
    elif payload.get("granularity") in package_slices.COUNTER_GRANULARITIES:
        timing, low, high = ("counter", None, row.occurred_at) if row.occurred_at else ("none", None, None)
    elif row.occurred_at is None:
        timing, low, high = "none", None, None
    else:
        timing, low, high = "point", row.occurred_at, row.occurred_at
    week = ""
    if low is not None and high is not None:
        a, b = parse_timestamp(low, "start"), parse_timestamp(high, "end")
        last = b - timedelta(microseconds=1) if b > a else b
        week = _monday(a) if _monday(a) == _monday(last) else ""
    return [{"part": "", "week_start": week, "timing": timing,
             "t_lo": package_slices.instant_text(low) if low else None,
             "t_hi": package_slices.instant_text(high) if high else None,
             "tokens": dict(tokens), "reported_total": payload.get("reported_total_tokens")}]


def resolved_rows(prepared: Prepared, component: int, agent_role: str = "executor") -> list[tuple]:
    """`usage_resolved` rows of one package's preparation, keyed as `_query_snapshot` groups them."""
    prepared = Prepared(*prepared)
    reconciled = {r.entity_ref for r in prepared.all_reconciled}
    rows = []
    seen = set()
    for row in [*prepared.all_reconciled,
                *(r for r in prepared.projected if r.kind == "usage" and r.entity_ref not in reconciled)]:
        if row.entity_ref in seen:
            continue
        seen.add(row.entity_ref)
        p = row.payload
        included = row.entity_ref in reconciled and p.get("wi") != "excluded"

        def work(r, p=p):
            return wi_work_key(r, p, prepared.non_wi_kinds, prepared.attr_conflicts)

        keys = [dimension_key(d, row, p, prepared.actor_context, agent_role, work) for d in DIMENSIONS]
        for part in usage_parts(row, p):
            rows.append((bytes.fromhex(row.entity_ref[5:]), part["week_start"], part["part"], component,
                         int(included), part["timing"], part["t_lo"], part["t_hi"], *keys,
                         *(part["tokens"].get(k) for k in TOKEN_FIELDS), part["reported_total"], int(bool(p["final"]))))
    return rows


class ResolvedStore:
    def __init__(self, connection: sqlite3.Connection, path: Path):
        self._db = connection
        self.path = path

    @classmethod
    def open(cls, path: str | Path, *, index_identity: str, engine: str, agent_tenants_sha256: str | None,
             cache_mib: int = 256) -> "ResolvedStore":
        """Open or create the private store; a store of another binding is emptied first."""
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))
        if path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
            raise LedgerError("resolved store must be private")
        db = sqlite3.connect(path, isolation_level=None, timeout=BUSY_SECONDS)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA mmap_size=0")
        db.execute(f"PRAGMA cache_size=-{int(cache_mib) * 1024}")
        db.executescript(_TABLES)
        store = cls(db, path)
        wanted = {"schema_version": SCHEMA_VERSION, "index_identity": index_identity, "engine": engine,
                  "agent_tenants_sha256": agent_tenants_sha256 or ""}
        meta = dict(db.execute("SELECT key, value FROM meta"))
        if any(meta.get(k) != v for k, v in wanted.items()):
            store._reset(wanted)
        return store

    def close(self) -> None:
        self.end_read()
        self._db.close()

    def _transaction(self):
        db = self._db

        class Transaction:
            def __enter__(self):
                if db.in_transaction:
                    db.execute("ROLLBACK")  # A pinned read snapshot of this connection ends with a write.
                db.execute("BEGIN IMMEDIATE")

            def __exit__(self, kind, value, traceback):
                db.execute("COMMIT" if kind is None else "ROLLBACK")

        return Transaction()

    def _reset(self, meta: Mapping[str, str]) -> None:
        with self._transaction():
            for table in ("meta", "package_slice", "package_global", "package_report", "usage_resolved", "package_assignment"):
                self._db.execute(f"DELETE FROM {table}")
            self._db.executemany("INSERT INTO meta VALUES (?, ?)", [
                *meta.items(), ("sequence", "0"), ("head_sha256", ""), ("public_state", ""), ("shared", "{}"), ("facts_sha256", "")])

    def begin_read(self) -> None:
        """Pin one read snapshot: every later read on this connection sees the same store state.

        A refresh by another connection (another publication) commits beside it
        (WAL) and becomes visible only after `end_read`.
        """
        self._db.execute("BEGIN")
        self._db.execute("SELECT COUNT(*) FROM meta").fetchone()  # The snapshot starts at the first read.

    def end_read(self) -> None:
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")

    def meta(self) -> dict[str, str]:
        return dict(self._db.execute("SELECT key, value FROM meta"))

    # -- writing -----------------------------------------------------------
    def refresh(self, index: FactIndex, agent_tenants: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Recompute the packages changed since the stored sequence; drop merged-away ones.

        The whole derivation reads one fact index snapshot (another connection
        may extend the index meanwhile) and runs inside this store's write
        transaction, where the starting sequence is checked: two refreshes
        never interleave, label a prefix they did not read, or move it back.
        """
        with _index_snapshot(index), self._transaction():
            meta = self.meta()
            if meta["index_identity"] != index.identity():
                raise StoreMismatch("resolved store binds another fact index build")
            stored, bound = int(meta["sequence"]), index.binding()
            if stored > bound.sequence or (stored and (index.batch(stored) or (None, None))[1] != meta["head_sha256"]):
                raise StoreMismatch("resolved store is not a prefix of the fact index")
            live = {c for c, _, _ in index.packages()}
            public_state = "%d:%d" % tuple(index.public_state())
            shared = list(index.shared_facts(ids=True))
            # Shared facts (binding aliases) are judged with every package: a package
            # naming an assignment whose alias changed is recomputed.
            now = {row.fact_ref: sorted({row.payload.get("target_ref") or "", row.payload.get("assignment_ref") or ""})
                   for _, row in shared}
            before = json.loads(meta.get("shared") or "{}")
            touched = {ref for fact in set(now) ^ set(before) for ref in (now.get(fact) or before.get(fact)) if ref}
            changed = set(index.components_changed_since(stored))
            for chunk in _chunks(sorted(touched)):
                changed.update(c for (c,) in self._db.execute(
                    "SELECT DISTINCT component FROM package_assignment WHERE ref IN (" + ",".join("?" * len(chunk))
                    + ")", chunk) if c in live)
            recompute = ([PUBLIC] if public_state != meta["public_state"] else []) + sorted(changed)
            gone = [c for (c,) in self._db.execute("SELECT component FROM package_slice WHERE component != ?",
                                                   (PUBLIC,)) if c not in live]
            # Package ids are reused after a merge: clear every stale slice before writing any.
            for component in (*gone, *recompute):
                self._drop(component)
            shared_rows = [row for _, row in shared]
            for component in recompute:
                if component == PUBLIC:
                    piece = package_slices.prepare_public(list(index.facts_of(PUBLIC)), agent_tenants)
                    facts = ()
                else:
                    facts = package_facts(index, component, shared)
                    piece = package_slices.prepare_package(facts, agent_tenants, shared=shared_rows)
                self._write(component, bound.sequence, piece, facts)
            self._db.executemany("UPDATE meta SET value = ? WHERE key = ?", [
                (str(bound.sequence), "sequence"), (bound.head_sha256 or "", "head_sha256"),
                (public_state, "public_state")])
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('shared', ?)", (json.dumps(now, sort_keys=True),))
            if bound.sequence != stored or not meta.get("facts_sha256"):
                # One pass over the indexed facts in ledger order, inside the same index snapshot.
                self._db.execute("INSERT OR REPLACE INTO meta VALUES ('facts_sha256', ?)",
                                 (index.facts_digest() if bound.sequence else digest([]),))
        return {"sequence": bound.sequence, "recomputed": len(recompute), "dropped": len(gone),
                "public_recomputed": bool(recompute and recompute[0] == PUBLIC)}

    def rebuild(self, index: FactIndex, agent_tenants: Mapping[str, Any] | None = None) -> dict[str, Any]:
        meta = self.meta()
        self._reset({k: meta[k] for k in ("schema_version", "index_identity", "engine", "agent_tenants_sha256")})
        return self.refresh(index, agent_tenants)

    def _drop(self, component: int) -> None:
        for table in ("package_slice", "package_global", "package_report", "usage_resolved", "package_assignment"):
            self._db.execute(f"DELETE FROM {table} WHERE component = ?", (component,))

    def _write(self, component: int, sequence: int, piece: Mapping[str, Any], facts: Iterable = ()) -> None:
        self._drop(component)
        self._db.executemany("INSERT OR IGNORE INTO package_assignment VALUES (?, ?)", (
            (ref, component) for ref in {r.payload.get("assignment_ref") for r in facts
                                         if r.kind in {"usage", "execution", "attribution"}} if ref))
        body = package_slices.encode_slice({k: piece[k] for k in (*package_slices.WINDOW_FIELDS, "history")})
        values = package_slices.encode_slice(piece["globals"])
        report = package_slices.encode_slice({k: piece[k] for k in package_slices.REPORT_FIELDS})
        self._db.execute("INSERT INTO package_report VALUES (?, ?, ?)", (component, report, sha256(report).hexdigest()))
        low, high, open_ended = piece["bounds"]
        self._db.execute("INSERT INTO package_slice VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (component, sequence, body, sha256(body).hexdigest(), low, high, int(open_ended)))
        self._db.execute("INSERT INTO package_global VALUES (?, ?, ?)", (component, values, sha256(values).hexdigest()))
        prepared = Prepared(active=[], all_wis=set(), attribution_conflict_wis=set(),
                            observation_attribution_conflicts={}, accepted_at={},
                            **{k: piece[k] for k in package_slices.ROW_FIELDS})
        self._db.executemany("INSERT INTO usage_resolved VALUES (" + ",".join("?" * 21) + ")",
                             resolved_rows(prepared, component))

    # -- reading -----------------------------------------------------------
    def window_components(self, low: datetime, high: datetime) -> list[int]:
        """Packages whose rows may fall in [low, high): a superset of those the window needs."""
        lo, hi = package_slices.instant_text(low), package_slices.instant_text(high)
        return [c for (c,) in self._db.execute(
            "SELECT component FROM package_slice WHERE component != ? AND (open_ended = 1 OR "
            "((t_lo IS NULL OR t_lo <= ?) AND t_hi >= ?)) ORDER BY component", (PUBLIC, hi, lo))]

    def digests(self, components: Iterable[int]) -> dict[str, Any]:
        """Slice digests of these packages and the public one, and the digest of all globals."""
        wanted = [PUBLIC, *components]
        slices = {}
        for chunk in range(0, len(wanted), 500):
            part = wanted[chunk:chunk + 500]
            slices.update(self._db.execute("SELECT component, slice_sha256 FROM package_slice WHERE component IN ("
                                           + ",".join("?" * len(part)) + ")", part))
        return {"slices": digest(sorted(slices.items())),
                "globals": digest(self._db.execute("SELECT component, value_sha256 FROM package_global "
                                                   "ORDER BY component").fetchall()),
                "report": digest(self._db.execute("SELECT component, value_sha256 FROM package_report "
                                                  "ORDER BY component").fetchall())}

    def report_values(self) -> dict[str, Any]:
        """Every package's report fields, merged (see `package_slices.REPORT_FIELDS`)."""
        return package_slices.merge_report(package_slices.decode_slice(value) for (value,) in
                                           self._db.execute("SELECT value FROM package_report ORDER BY component"))

    def piece(self, component: int) -> dict[str, Any]:
        """One stored package: window fields, report fields and history."""
        (body,) = self._db.execute("SELECT slice FROM package_slice WHERE component = ?", (component,)).fetchone()
        (report,) = self._db.execute("SELECT value FROM package_report WHERE component = ?", (component,)).fetchone()
        return {**package_slices.decode_slice(body), **package_slices.decode_slice(report)}

    def global_values(self) -> dict[str, Any]:
        return package_slices.merge_globals(package_slices.decode_slice(value) for (value,) in
                                            self._db.execute("SELECT value FROM package_global ORDER BY component"))

    def load(self, low: datetime, high: datetime, global_values: Mapping[str, Any] | None = None,
             report: Mapping[str, Any] | None = None) -> tuple[Prepared, list]:
        """The window's preparation (its packages, the public package, every global and report field)
        and stage history."""
        components = [PUBLIC, *self.window_components(low, high)]
        pieces = []
        for chunk in range(0, len(components), 500):
            part = components[chunk:chunk + 500]
            pieces += [package_slices.decode_slice(body) for (body,) in self._db.execute(
                "SELECT slice FROM package_slice WHERE component IN (" + ",".join("?" * len(part)) + ") "
                "ORDER BY component", part)]
        return package_slices.merge_slices(pieces, global_values or self.global_values(),
                                           report or self.report_values())

    def snapshot(self, index: FactIndex) -> dict[str, Any]:
        """The snapshot fields a report names: the index prefix this store was refreshed to."""
        meta = self.meta()
        sequence = int(meta["sequence"])
        batch = index.batch(sequence) if sequence else (None, "")
        if batch is None or batch[1] != meta["head_sha256"]:
            raise StoreMismatch("resolved store is not a prefix of the fact index")
        # The prefix digest of the facts, as `UsageLedger.receipt_snapshot` defines `sha256`.
        return {"watermark": sequence, "recorded_at": batch[0], "sha256": meta["facts_sha256"] or digest([])}

    def reconcile(self, view: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Differences between `usage_resolved` summed over a view's window and the view's groups."""
        query = view["query"]
        tz = ZoneInfo(query["timezone"])
        start, end = date.fromisoformat(query["from_date"]), date.fromisoformat(query["to_date"])
        low = datetime.combine(start, datetime.min.time(), tz)
        high = datetime.combine(end, datetime.min.time(), tz)
        lo, hi = package_slices.instant_text(low), package_slices.instant_text(high)
        dims = [d for d in query["group_by"]]
        totals: dict[tuple, dict[str, Any]] = {}
        counted: set[tuple] = set()
        for row in self._db.execute(
                "SELECT entity_ref, part, timing, t_lo, t_hi, " + ", ".join(DIMENSIONS) + ", "
                + ", ".join(TOKEN_FIELDS) + " FROM usage_resolved WHERE included = 1 AND timing != 'none' "
                "AND (t_hi IS NULL OR t_hi >= ?) AND (t_lo IS NULL OR t_lo <= ?)", (lo, hi)):
            entity, part, timing, t_lo, t_hi = row[:5]
            keys = dict(zip(DIMENSIONS, row[5:12]))
            tokens = dict(zip(TOKEN_FIELDS, row[12:]))
            if timing == "metered":
                inside = start <= date.fromisoformat(part) < end
            elif timing == "point" or (timing == "interval" and t_lo == t_hi):
                inside = lo <= t_lo < hi
            elif timing == "interval":
                inside = t_lo >= lo and t_hi <= hi
            else:  # A counter without start is never exactly inside a window.
                inside = False
            if not inside:
                continue
            key = tuple(keys[d] for d in dims)
            group = totals.setdefault(key, {"event_count": 0, "tokens": {k: [] for k in TOKEN_FIELDS}})
            if (entity, key) not in counted:
                counted.add((entity, key))
                group["event_count"] += 1
            group.setdefault("parts", {})
            for field in TOKEN_FIELDS:
                group["parts"].setdefault(entity, {}).setdefault(field, []).append(tokens[field])
        expected: dict[tuple, dict[str, Any]] = {}
        for key, group in totals.items():
            result = {"event_count": group["event_count"], "tokens": {}}
            for field in TOKEN_FIELDS:
                values = [sum(v) if None not in v else None for v in
                          (parts[field] for parts in group.get("parts", {}).values())]
                known = [v for v in values if v is not None]
                result["tokens"][field] = {"known": sum(known) if known or not values else None,
                                           "unknown_event_count": len(values) - len(known)}
            expected[key] = result
        found = {tuple(g["key"].get(d, UNKNOWN) for d in dims): g["usage"] for g in view["groups"]}
        diffs = []
        for key in sorted(set(expected) | set(found), key=repr):
            mine, theirs = expected.get(key), found.get(key)
            mine = mine and {"event_count": mine["event_count"], "tokens": mine["tokens"]}
            theirs = theirs and {"event_count": theirs["event_count"], "tokens": theirs["tokens"]}
            if mine is None and theirs["event_count"] == 0:
                continue  # A group seeded by executions alone has no usage to sum.
            if mine != theirs:
                diffs.append({"key": dict(zip(dims, key)), "resolved": mine, "view": theirs})
        return diffs
