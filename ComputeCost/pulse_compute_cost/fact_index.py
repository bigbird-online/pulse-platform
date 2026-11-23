"""Fact index: a disposable SQLite view of one verified ledger prefix (WI754).

It answers three questions without reading the whole ledger: was this fact
ref ingested, which versions does this entity have, which facts belong to
this session package (see `session_packages`). The ledger stays the only
authority. The index binds the stream identity, the applied batch sequence,
that batch's digest and the link-key code; anything else is discarded and
rebuilt from the ledger.

Invariants: every ledger fact before `sequence` (by batch and position in
the batch) has exactly one row whose body is its canonical ledger bytes, so
the rows in id order are `receipt_snapshot(...)["facts"]`; an entity's facts
and a fact's predecessor share one package; the partition into packages does
not depend on how the stream was batched or interrupted (package ids may
differ).

One fact ref may recur, across batches or within one: historical ingest
re-stored a known fact with newer `observed_at` and `evidence`, which the ref
does not hash, and the observation store accepts such facts in one verified
batch. Every occurrence keeps its row with its own bytes, in ledger order
(batch, then position in the batch); all share the entity and so one
package. A point read by ref follows ingest's `{fact_ref: row}` view of the
ledger: one entry per distinct ref, ordered by its first occurrence, carrying
the latest (largest id) occurrence.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator
import zlib

from . import session_packages
from .ledger import LedgerError, UsageLedger
from .observation_checkpoint import batch_digest, receipt_from_canonical
from .receipts import Receipt, canonical, digest

SCHEMA_VERSION = "2"
PUBLIC = 0
_TABLES = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS batch(sequence INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL,
  sha256 TEXT NOT NULL, fact_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fact(id INTEGER PRIMARY KEY, fact_ref BLOB NOT NULL,
  entity_ref BLOB NOT NULL, kind TEXT NOT NULL, producer TEXT NOT NULL,
  sequence INTEGER NOT NULL REFERENCES batch(sequence), supersedes BLOB,
  assertion_key BLOB NOT NULL, component INTEGER NOT NULL, body BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS fact_by_ref ON fact(fact_ref, id);
CREATE INDEX IF NOT EXISTS fact_entity ON fact(entity_ref);
CREATE INDEX IF NOT EXISTS fact_supersedes ON fact(supersedes) WHERE supersedes IS NOT NULL;
CREATE INDEX IF NOT EXISTS fact_assertion ON fact(assertion_key);
CREATE INDEX IF NOT EXISTS fact_component ON fact(component, id);
CREATE TABLE IF NOT EXISTS link(node BLOB PRIMARY KEY, component INTEGER NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS link_component ON link(component);
CREATE TABLE IF NOT EXISTS component(id INTEGER PRIMARY KEY, fact_count INTEGER NOT NULL,
  usage_count INTEGER NOT NULL, changed_sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rule_state(rule TEXT NOT NULL, key TEXT NOT NULL, sha256 TEXT NOT NULL,
  PRIMARY KEY(rule, key)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rejudge_queue(component INTEGER PRIMARY KEY, reason TEXT NOT NULL,
  enqueued_sequence INTEGER NOT NULL);
"""
_TABLE_NAMES = ("meta", "batch", "fact", "link", "component", "rule_state", "rejudge_queue")
# Judgement state of incremental collection (`incremental_collect`): a ledger
# position and rule digests, which outlive a rebuild of the index, and package
# ids (queued, deferred), which do not.
_JUDGED = "judged_sequence"
_DEFERRED = "deferred_components"
# Where two indexes could serve a statement, INDEXED BY pins the selective one:
# the index has no ANALYZE statistics, and without them SQLite picked
# fact_component for the public package (tens of thousands of rows) per link.
_MOVE_PUBLIC = "UPDATE fact INDEXED BY fact_entity SET component = ? WHERE entity_ref = ? AND component = ?"


class IndexMismatch(LedgerError):
    """The index is not a prefix of this ledger; rebuild it."""


@dataclass(frozen=True)
class Binding:
    sequence: int
    head_sha256: str | None
    link_logic: str


def stream_identity(ledger: UsageLedger) -> str:
    """The observation stream a ledger reads: its core binding, else its local root."""
    describe = getattr(ledger.authority, "checkpoint_binding", None)
    if describe is not None:
        return digest(["observations", describe()[0]])
    return digest(["local", str(ledger.root.resolve())])


def assertion_key(row: Receipt) -> bytes:
    """The ledger's assertion identity (kind, entity, time, producer, payload), 16 bytes."""
    head = canonical([row.kind, row.entity_ref, row.occurred_at, row.producer])
    return sha256(head + b"\n" + row._payload_json).digest()[:16]


def _node(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else session_packages.node(value)


def ledger_head(ledger: UsageLedger) -> tuple[int, str | None] | None:
    """(sequence, sha256) of the ledger's last batch without reading its history; None when unknown.

    A local ledger reads only its last transaction file and recomputes that
    batch's digest from its facts, so a damaged tail is never taken for the
    indexed one (the full stream then reports it). A production ledger
    asks the core whether anything follows its checkpoint prefix; only a
    confirmed empty continuation names the prefix as the head. Anything else
    is unknown, and the caller streams and verifies as before.
    """
    try:
        if ledger.authority is None:
            directory = ledger.root / "receipts"
            paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
            if not paths:
                return 0, None
            if paths[-1].name != f"{len(paths):020d}.json" or paths[-1].is_symlink():
                return None
            last = json.loads(paths[-1].read_bytes())
            if (not isinstance(last, dict) or last.get("sequence") != len(paths)
                    or not isinstance(last.get("facts"), list)):
                return None
            meta = {k: v for k, v in last.items() if k != "facts"}
            if batch_digest(meta, [canonical(raw) for raw in last["facts"]]) != last.get("sha256"):
                return None
            return last["sequence"], last["sha256"]
        checkpoint = ledger._checkpoint() if hasattr(ledger.authority, "scan_after") else None
        manifest = checkpoint.manifest() if checkpoint is not None else None
        if manifest is None:
            return None
        prefix = manifest["prefix"]
        _, following, resumed = ledger.authority.scan_after(prefix)
        if not resumed or any(True for _ in following):
            return None
        if not ledger.authority.resume_confirmed(prefix["sequence"]):
            return None
        return prefix["sequence"], prefix["watermark"]
    except (OSError, ValueError, KeyError, TypeError, LedgerError):
        return None


class FactIndex:
    def __init__(self, connection: sqlite3.Connection, path: Path):
        self._db = connection
        self.path = path

    @classmethod
    def open(cls, path: str | Path, stream_identity: str, *, cache_mib: int = 128) -> "FactIndex":
        """Open or create a private index; a different binding is emptied first."""
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))
        if path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
            raise LedgerError("fact index must be private")
        db = sqlite3.connect(path, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA mmap_size=0")
        db.execute(f"PRAGMA cache_size=-{int(cache_mib) * 1024}")
        index = cls(db, path)
        wanted = {"schema_version": SCHEMA_VERSION, "stream_identity": stream_identity,
                  "link_logic": session_packages.LINK_LOGIC}
        # Version check, drop, create, the binding judgment and any reset are one
        # write transaction: a judgment made on what another opener had not yet
        # committed would empty the index that opener has since built and an
        # ingest relies on.
        try:
            with index._transaction():
                if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() and \
                        db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() != (SCHEMA_VERSION,):
                    # Another schema is not migrated: drop it; the empty index must be rebuilt.
                    for table in _TABLE_NAMES:
                        db.execute(f"DROP TABLE IF EXISTS {table}")
                for statement in _TABLES.split(";"):
                    if statement.strip():
                        db.execute(statement)  # Not executescript: it would commit first.
                meta = index._meta()
                if any(meta.get(k) != v for k, v in wanted.items()):
                    index._clear(wanted)
        except BaseException:
            db.close()
            raise
        return index

    def close(self) -> None:
        self._db.close()

    def _clear(self, meta: dict[str, str]) -> None:
        """Empty every table and bind `meta`; the caller holds the write transaction.

        Within one stream the judged position and the rule digests survive:
        they name ledger batches and rule entries, not packages. Queued or
        deferred packages cannot be carried to the new ids, so when any were
        waiting the judged position restarts at 0 and every package is judged
        again (bounded per round by the working-set limit).
        """
        db = self._db
        kept, rules = None, []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'rule_state'").fetchone():
            old = self._meta()
            if old.get("stream_identity") == meta.get("stream_identity") and _JUDGED in old:
                waiting = db.execute("SELECT COUNT(*) FROM rejudge_queue").fetchone()[0] or \
                    json.loads(old.get(_DEFERRED) or "[]")
                kept = "0" if waiting else old[_JUDGED]
                rules = db.execute("SELECT rule, key, sha256 FROM rule_state").fetchall()
        for table in _TABLE_NAMES:
            db.execute(f"DELETE FROM {table}")
        # A new build: package ids of the old one no longer mean anything (see identity()).
        db.executemany("INSERT INTO meta VALUES (?, ?)",
                       [*meta.items(), ("sequence", "0"), ("head_sha256", ""),
                        ("build_id", os.urandom(16).hex()), *([(_JUDGED, kept)] if kept is not None else [])])
        db.executemany("INSERT INTO rule_state VALUES (?, ?, ?)", rules)

    def _binding_meta(self) -> dict[str, str]:
        return dict(self._db.execute("SELECT key, value FROM meta WHERE key IN "
                                     "('schema_version', 'stream_identity', 'link_logic')"))

    def _transaction(self):
        db = self._db

        class Transaction:
            def __enter__(self):
                db.execute("BEGIN IMMEDIATE")

            def __exit__(self, kind, value, traceback):
                db.execute("COMMIT" if kind is None else "ROLLBACK")

        return Transaction()

    def begin_read(self) -> None:
        """Pin one consistent view for the reads until `end_read` (WAL keeps it)."""
        self._db.execute("BEGIN")
        self._db.execute("SELECT 1 FROM meta LIMIT 1").fetchone()  # The snapshot starts here.

    def end_read(self) -> None:
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")

    def _meta(self) -> dict[str, str]:
        return dict(self._db.execute("SELECT key, value FROM meta"))

    def row_counts(self) -> tuple[int, int, int]:
        """(batches, facts the batch rows declare, fact rows): a cheap completeness check."""
        db = self._db
        batches, declared = db.execute("SELECT COUNT(*), COALESCE(SUM(fact_count), 0) FROM batch").fetchone()
        return batches, declared, db.execute("SELECT COUNT(*) FROM fact").fetchone()[0]

    def binding(self) -> Binding:
        meta = self._meta()
        return Binding(int(meta["sequence"]), meta["head_sha256"] or None, meta["link_logic"])

    def identity(self) -> str:
        """This build of the index: stream, link logic and a token a rebuild renews.

        Package ids are only meaningful within one build; state keyed by them
        (the resolved store) binds this identity.
        """
        meta = dict(self._db.execute("SELECT key, value FROM meta"))
        if "build_id" not in meta:
            meta["build_id"] = os.urandom(16).hex()
            self._db.execute("INSERT INTO meta VALUES ('build_id', ?)", (meta["build_id"],))
        return digest([meta["stream_identity"], meta["link_logic"], meta["build_id"]])

    def batch(self, sequence: int) -> tuple[str, str] | None:
        """(recorded_at, sha256) of an indexed batch."""
        return self._db.execute("SELECT recorded_at, sha256 FROM batch WHERE sequence = ?", (sequence,)).fetchone()

    def public_state(self) -> tuple[int, int]:
        """(public facts, latest batch adding one): changes whenever the public package does."""
        return self._db.execute("SELECT COUNT(*), COALESCE(MAX(sequence), 0) FROM fact INDEXED BY fact_component "
                                "WHERE component = ?", (PUBLIC,)).fetchone()

    # -- writing -----------------------------------------------------------
    def apply_batch(self, meta: dict[str, Any], lines: list[bytes]) -> None:
        """Index one verified batch in one transaction: insert, link, merge packages."""
        if batch_digest(meta, lines) != meta["sha256"]:
            raise IndexMismatch("batch does not extend the indexed prefix")
        sequence = meta["sequence"]
        db = self._db
        with self._transaction():
            bound = self.binding()  # Inside the write transaction: no other writer moves it.
            if meta["sequence"] != bound.sequence + 1 or meta["previous"] != bound.head_sha256:
                raise IndexMismatch("batch does not extend the indexed prefix")
            db.execute("INSERT INTO batch VALUES (?, ?, ?, ?)",
                       (sequence, meta["recorded_at"], meta["sha256"], len(lines)))
            for line in lines:
                self._insert(receipt_from_canonical(line, None), line, sequence)
            db.executemany("UPDATE meta SET value = ? WHERE key = ?",
                           [(str(sequence), "sequence"), (meta["sha256"], "head_sha256")])

    def _insert(self, row: Receipt, line: bytes, sequence: int) -> None:
        db = self._db
        entity = _node(row.entity_ref)
        nodes = session_packages.link_nodes(row)
        if not nodes:
            # A public fact joins its entity's package if another version linked it.
            found = db.execute("SELECT component FROM link WHERE node = ?", (entity,)).fetchone()
            nodes = (entity,) if found else ()
        if nodes:
            marks = ",".join("?" * len(nodes))
            found = {c for (c,) in db.execute(f"SELECT DISTINCT component FROM link WHERE node IN ({marks})", nodes)}
            component = self._merge(found, sequence) if found else db.execute(
                "INSERT INTO component VALUES (NULL, 0, 0, ?)", (sequence,)).lastrowid
            moved = 0
            for n in nodes:
                if db.execute("INSERT OR IGNORE INTO link VALUES (?, ?)", (n, component)).rowcount:
                    # A public fact whose entity just became a node joins this package.
                    moved += db.execute(_MOVE_PUBLIC, (component, n, PUBLIC)).rowcount
            db.execute("UPDATE component SET fact_count = fact_count + ?, usage_count = usage_count + ?, "
                       "changed_sequence = ? WHERE id = ?", (1 + moved, int(row.kind == "usage"),
                                                             sequence, component))
        else:
            component = PUBLIC
        db.execute("INSERT INTO fact VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            _node(row.fact_ref), entity, row.kind, row.producer, sequence,
            _node(row.supersedes) if row.supersedes else None, assertion_key(row), component,
            zlib.compress(line, 1)))

    def _merge(self, found: set[int], sequence: int) -> int:
        """The larger package absorbs the others; returns the survivor."""
        db = self._db
        if len(found) == 1:
            return next(iter(found))
        marks = ",".join("?" * len(found))
        sizes = db.execute(f"SELECT id, fact_count, usage_count FROM component WHERE id IN ({marks})",
                           tuple(found)).fetchall()
        winner = min(sizes, key=lambda s: (-s[1], s[0]))[0]
        for loser, facts, usages in sizes:
            if loser == winner:
                continue
            db.execute("UPDATE fact INDEXED BY fact_component SET component = ? WHERE component = ?",
                       (winner, loser))
            db.execute("UPDATE link SET component = ? WHERE component = ?", (winner, loser))
            db.execute("UPDATE component SET fact_count = fact_count + ?, usage_count = usage_count + ? "
                       "WHERE id = ?", (facts, usages, winner))
            db.execute("DELETE FROM component WHERE id = ?", (loser,))
            # A queued package stays queued under the package that absorbed it.
            db.execute("UPDATE OR IGNORE rejudge_queue SET component = ? WHERE component = ?", (winner, loser))
            db.execute("DELETE FROM rejudge_queue WHERE component = ?", (loser,))
        db.execute("UPDATE component SET changed_sequence = ? WHERE id = ?", (sequence, winner))
        return winner

    def catch_up(self, ledger: UsageLedger, *, upto: int | None = None) -> int:
        """Apply the ledger's batches after the indexed prefix (to `upto`); count applied.

        Raises IndexMismatch when the index is not a prefix of the ledger.
        """
        bound = self.binding()
        if upto is not None and upto < bound.sequence:
            raise IndexMismatch("index is ahead of the requested watermark")
        if ledger_head(ledger) == (bound.sequence, bound.head_sha256):
            return 0  # Nothing new: the indexed prefix stays verified, no batch is reread.
        applied, confirmed = 0, bound.sequence == 0
        for meta, lines in ledger.stream_batches(after=max(bound.sequence - 1, 0)):
            if meta["sequence"] <= bound.sequence:
                if meta["sha256"] != bound.head_sha256:
                    raise IndexMismatch("ledger differs from the indexed prefix")
                confirmed = True
                continue
            if upto is not None and meta["sequence"] > upto:
                break
            self.apply_batch(meta, lines)
            applied += 1
        if not confirmed:
            raise IndexMismatch("ledger is shorter than the indexed prefix")
        return applied

    def clear(self) -> None:
        """Empty the index, keeping its binding; the next `catch_up` rebuilds it."""
        with self._transaction():
            self._clear(self._binding_meta())

    def clear_unless_prefix(self, heads: list[str]) -> bool:
        """Empty the index unless it is a prefix of the batches with these digests; True if emptied.

        Judged and done in one write transaction, so an index another process
        has just reset or extended is never emptied on a stale reading.
        """
        with self._transaction():
            bound = self.binding()
            if bound.sequence <= len(heads) and (bound.sequence == 0
                                                 or heads[bound.sequence - 1] == bound.head_sha256):
                return False
            self._clear(self._binding_meta())
            return True

    def rebuild(self, ledger: UsageLedger, *, upto: int | None = None) -> int:
        self.clear()
        return self.catch_up(ledger, upto=upto)

    # -- point reads ---------------------------------------------------------
    def _rows(self, sql: str, args: tuple) -> list[Receipt]:
        return [receipt_from_canonical(zlib.decompress(body), None) for (body,) in self._db.execute(sql, args)]

    def _distinct(self, index: str, where: str, args: tuple) -> list[Receipt]:
        """One fact per distinct ref matching `where`: its latest occurrence, in first-occurrence order."""
        return self._rows(f"SELECT f.body FROM (SELECT MIN(id) AS first, MAX(id) AS last FROM fact f0 "
                          f"INDEXED BY {index} {where} GROUP BY f0.fact_ref) g "
                          "JOIN fact f ON f.id = g.last ORDER BY g.first", args)

    def get(self, fact_ref: str) -> Receipt | None:
        """The latest occurrence of this fact ref."""
        rows = self._rows("SELECT body FROM fact INDEXED BY fact_by_ref WHERE fact_ref = ? "
                          "ORDER BY id DESC LIMIT 1", (_node(fact_ref),))
        return rows[0] if rows else None

    def versions(self, entity_ref: str) -> list[Receipt]:
        return self._distinct("fact_entity", "WHERE entity_ref = ?", (_node(entity_ref),))

    def children(self, fact_ref: str) -> list[Receipt]:
        return self._distinct("fact_supersedes", "WHERE supersedes = ?", (_node(fact_ref),))

    def active_by_assertion(self, key: bytes) -> list[Receipt]:
        """Unsuperseded facts with this assertion key, in ingest order."""
        return self._distinct("fact_assertion", "WHERE assertion_key = ? AND NOT EXISTS (SELECT 1 FROM fact c "
                              "INDEXED BY fact_supersedes WHERE c.supersedes = f0.fact_ref)", (key,))

    # -- packages ------------------------------------------------------------
    def component_of(self, node: str | bytes) -> int | None:
        """The package of a link node, else of the fact with this entity or fact ref."""
        value = _node(node)
        for sql in ("SELECT component FROM link WHERE node = ?",
                    "SELECT component FROM fact INDEXED BY fact_entity WHERE entity_ref = ? LIMIT 1",
                    "SELECT component FROM fact INDEXED BY fact_by_ref WHERE fact_ref = ? LIMIT 1"):
            found = self._db.execute(sql, (value,)).fetchone()
            if found:
                return found[0]
        return None

    def facts_of(self, component: int, upto_sequence: int | None = None, *, ids: bool = False) -> Iterator[Any]:
        """A package's facts in ledger order, each occurrence of a ref; with `ids`, (position, fact) pairs."""
        sql, args = "SELECT id, body FROM fact INDEXED BY fact_component WHERE component = ?", (component,)
        if upto_sequence is not None:
            sql, args = sql + " AND sequence <= ?", (component, upto_sequence)
        for position, body in self._db.execute(sql + " ORDER BY id", args):
            row = receipt_from_canonical(zlib.decompress(body), None)
            yield (position, row) if ids else row

    def public_facts(self, upto_sequence: int | None = None) -> Iterator[Receipt]:
        return self.facts_of(PUBLIC, upto_sequence)

    def shared_facts(self, *, ids: bool = False) -> Iterator[Any]:
        """The public facts every package's judgement reads (`session_packages.is_shared`), in ledger order."""
        marks = ",".join("?" * len(session_packages.SHARED_PRODUCERS))
        for position, body in self._db.execute(
                "SELECT id, body FROM fact INDEXED BY fact_component WHERE component = ? AND kind = 'attribution' "
                f"AND producer IN ({marks}) ORDER BY id", (PUBLIC, *sorted(session_packages.SHARED_PRODUCERS))):
            row = receipt_from_canonical(zlib.decompress(body), None)
            yield (position, row) if ids else row

    def package_size(self, component: int) -> int:
        """Facts of a package (the public package counted from its rows); 0 when gone."""
        if component == PUBLIC:
            return self.stats()["public_package"]
        found = self._db.execute("SELECT fact_count FROM component WHERE id = ?", (component,)).fetchone()
        return found[0] if found else 0

    def usage_packages(self) -> list[int]:
        return [c for (c,) in self._db.execute("SELECT id FROM component WHERE usage_count > 0 ORDER BY id")]

    # -- judgement state (incremental collection) -------------------------------
    def judged_sequence(self) -> int:
        """The batch through which every package a batch touched was judged; 0 before any."""
        found = self._db.execute("SELECT value FROM meta WHERE key = ?", (_JUDGED,)).fetchone()
        return int(found[0]) if found else 0

    def judgement_started(self) -> bool:
        return self._db.execute("SELECT 1 FROM meta WHERE key = ?", (_JUDGED,)).fetchone() is not None

    def deferred(self) -> list[int]:
        found = self._db.execute("SELECT value FROM meta WHERE key = ?", (_DEFERRED,)).fetchone()
        return json.loads(found[0]) if found else []

    def record_judgement(self, sequence: int, deferred: list[int]) -> None:
        """After a round: judged through `sequence`, these packages wait for the next round, oldest first."""
        with self._transaction():
            self._db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                                 [(_JUDGED, str(sequence)), (_DEFERRED, json.dumps(list(dict.fromkeys(deferred))))])

    def rule_state(self, rule: str) -> dict[str, str]:
        return dict(self._db.execute("SELECT key, sha256 FROM rule_state WHERE rule = ?", (rule,)))

    def set_rule_state(self, rule: str, entries: dict[str, str]) -> None:
        with self._transaction():
            self._db.execute("DELETE FROM rule_state WHERE rule = ?", (rule,))
            self._db.executemany("INSERT INTO rule_state VALUES (?, ?, ?)",
                                 [(rule, key, value) for key, value in sorted(entries.items())])

    def enqueue(self, components: Iterable[int], reason: str, sequence: int) -> int:
        """Queue packages for rejudgement (publisher only); a queued package keeps its first reason."""
        with self._transaction():
            return sum(self._db.execute("INSERT OR IGNORE INTO rejudge_queue VALUES (?, ?, ?)",
                                        (c, reason, sequence)).rowcount for c in components)

    def queued(self, budget: int) -> list[int]:
        """Queued packages in queue order whose facts fit `budget`; the first one always."""
        chosen, used = [], 0
        for component, size in self._db.execute(
                "SELECT q.component, COALESCE(c.fact_count, 0) FROM rejudge_queue q "
                "LEFT JOIN component c ON c.id = q.component ORDER BY q.enqueued_sequence, q.component"):
            if chosen and used + size > budget:
                break
            chosen.append(component)
            used += size
        return chosen

    def dequeue(self, components: Iterable[int]) -> None:
        with self._transaction():
            self._db.executemany("DELETE FROM rejudge_queue WHERE component = ?", [(c,) for c in components])

    def queue_length(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM rejudge_queue").fetchone()[0]

    def components_changed_since(self, sequence: int) -> list[int]:
        return [c for (c,) in self._db.execute(
            "SELECT id FROM component WHERE changed_sequence > ? ORDER BY id", (sequence,))]

    def facts_digest(self) -> str:
        """The ledger's prefix digest of the indexed facts (`receipt_snapshot(...)["sha256"]`)."""
        result = sha256(b"[")
        for i, (body,) in enumerate(self._db.execute("SELECT body FROM fact ORDER BY id")):
            if i:
                result.update(b",")
            result.update(zlib.decompress(body))
        result.update(b"]")
        return result.hexdigest()

    def packages(self) -> list[tuple[int, int, int]]:
        """(id, facts, usage facts) of every session package, largest first."""
        return self._db.execute("SELECT id, fact_count, usage_count FROM component "
                                "ORDER BY fact_count DESC, id").fetchall()

    def stats(self) -> dict[str, int]:
        db = self._db
        packages, largest = db.execute("SELECT COUNT(*), COALESCE(MAX(fact_count), 0) FROM component").fetchone()
        return {"sequence": self.binding().sequence,
                "facts": db.execute("SELECT COUNT(*) FROM fact").fetchone()[0],
                "packages": packages, "largest_package": largest,
                "public_package": db.execute("SELECT COUNT(*) FROM fact WHERE component = ?",
                                             (PUBLIC,)).fetchone()[0]}


INDEX_FILE = "fact-index.sqlite"


def index_path(ledger: UsageLedger) -> Path:
    """`<ledger root>/derived/fact-index.sqlite`."""
    return ledger.root / "derived" / INDEX_FILE


def ingest_index_provider(*, cache_mib: int = 128):
    """A `UsageLedger.index_provider`: open this ledger's index, building it when empty.

    The first build streams the whole ledger once, before ingest takes its
    exclusive lock; later batches are added by ingest itself.
    """
    def provide(ledger: UsageLedger) -> FactIndex:
        index = FactIndex.open(index_path(ledger), stream_identity(ledger), cache_mib=cache_mib)
        try:
            if index.binding().sequence == 0:
                index.catch_up(ledger)
        except BaseException:
            index.close()
            raise
        return index
    return provide
