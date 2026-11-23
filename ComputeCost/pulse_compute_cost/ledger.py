"""Idempotent, append-only month ledger for usage_event.v1."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
from typing import Iterable, Iterator, Callable, Any
from datetime import datetime, timezone, timedelta
import tempfile
from hashlib import sha256, file_digest

from .receipts import Receipt, canonical, digest, _ReceiptReadPool
from .events import parse_timestamp, utc_text

from .events import UsageEvent, UsageEventError
from .ingest_lookup import IndexLookup, TableLookup, assertion
from .observation_checkpoint import (Checkpoint, CheckpointMiss, SegmentWriter, batch_digest, logic_version,
                                     receipt_from_canonical)


class LedgerError(RuntimeError):
    """Raised before writes when the ledger is malformed or evidence conflicts."""


class SnapshotChanged(LedgerError):
    """Another writer appended after this collection read; nothing was written.

    The collection is derived from the snapshot it read, so it must be
    recollected from the current stream, never rebased onto it.
    """


@dataclass(frozen=True)
class LedgerIngestResult:
    inserted: int
    duplicates: int
    usage_inserted: int
    corrections_inserted: int

    def to_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "usage_inserted": self.usage_inserted,
            "corrections_inserted": self.corrections_inserted,
        }


def _read_file(path: Path) -> list[UsageEvent]:
    if not path.exists():
        return []
    events: list[UsageEvent] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LedgerError(f"cannot read ledger partition {path.name}") from exc
    for position, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            events.append(UsageEvent.from_dict(json.loads(raw_line)))
        except (json.JSONDecodeError, ValueError, UsageEventError) as exc:
            raise LedgerError(
                f"malformed ledger partition {path.name} at line {position}"
            ) from exc
    return events


class _ReceiptFold:
    """Derived rows of one verified read: batch metadata, receipts, prefix digests.

    The complete path (`add`) and the checkpoint path (`add_verified`) produce
    identical state; the canonical fact bytes stream into a checkpoint segment.
    """

    def __init__(self, checkpoint: Checkpoint | None):
        self.checkpoint = checkpoint
        self.writer: SegmentWriter | None = None
        self.result: list[dict[str, Any]] = []
        self.rows: list[list[Receipt]] = []
        self.states: list[Any] = []
        self.prefix = sha256(b"[")
        self.previous: str | None = None
        self.previous_at: datetime | None = None
        self.pool = _ReceiptReadPool()

    def sink(self, meta: dict[str, Any], lines: list[bytes]) -> None:
        if self.checkpoint is None:
            return
        try:
            if self.writer is None:
                self.writer = self.checkpoint.writer()
                if self.writer is None:
                    self.checkpoint = None
                    return
            self.writer.add(meta, lines)
        except OSError:
            self.discard()
            self.checkpoint = None

    def discard(self) -> None:
        if self.writer is not None:
            self.writer.abort()
            self.writer = None

    def _append(self, meta: dict[str, Any], lines: list[bytes], parsed: list[Receipt],
                instant: datetime) -> None:
        for i, line in enumerate(lines):
            if self.result or i:
                self.prefix.update(b",")
            # Raw JSON is already validated. Never decode payload bytes
            # again just to hash the exact same canonical receipt array.
            self.prefix.update(line)
        self.states.append(self.prefix.copy())
        self.previous, self.previous_at = meta["sha256"], instant
        # Retain validated chain metadata and the Receipt objects once.
        # Raw facts duplicate their payloads for the lifetime of a refresh.
        self.result.append(meta)
        self.rows.append(parsed)

    def add(self, batch: dict[str, Any]) -> None:
        expected = len(self.result) + 1
        try:
            if set(batch) != {"schema", "sequence", "recorded_at", "previous", "facts", "sha256"}:
                raise LedgerError("receipt transaction fields are invalid")
            instant = parse_timestamp(batch["recorded_at"], "recorded_at")
            if (batch["schema"] != "computecost_ledger_transaction/v1"
                    or batch["sequence"] != expected or batch["previous"] != self.previous
                    or (self.previous_at is not None and instant <= self.previous_at)
                    or batch["sha256"] != digest({k: v for k, v in batch.items() if k != "sha256"})):
                raise LedgerError("receipt transaction digest/order mismatch")
            if not isinstance(batch["facts"], list) or not batch["facts"]:
                raise LedgerError("receipt transaction has no facts")
            parsed = [Receipt.from_verified(raw, _pool=self.pool) for raw in batch["facts"]]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise LedgerError("invalid receipt transaction") from exc
        lines = [canonical(raw) for raw in batch["facts"]]
        meta = {k: v for k, v in batch.items() if k != "facts"}
        self.sink(meta, lines)
        self._append(meta, lines, parsed, instant)

    def add_verified(self, meta: dict[str, Any], lines: list[bytes]) -> None:
        """A checkpoint batch whose own digest and chain the loader verified."""
        instant = parse_timestamp(meta["recorded_at"], "recorded_at")
        if self.previous_at is not None and instant <= self.previous_at:
            raise CheckpointMiss("checkpoint batch order differs")
        pool = self.pool
        self._append(meta, lines, [receipt_from_canonical(line, pool) for line in lines], instant)


def _decide_ingest(candidates: list[Receipt], lookup: TableLookup | IndexLookup
                   ) -> tuple[list[Receipt], int, dict[str, str]]:
    """The rows one ingest appends, how many candidates were already stored, and
    which stored or appended fact each candidate ref resolved to."""
    pending, duplicates = [], 0
    rewritten: dict[str, str] = {}

    def evidence_successor(ref: str) -> str:
        # An assertion revision based on P must follow P's pure
        # evidence refinements P->P', without following a genuinely
        # different assertion Q or selecting among conflicting heads.
        visited: set[str] = set()
        while (fact := lookup.get(ref)) is not None and ref not in visited:
            visited.add(ref)
            successors = [r for r in lookup.children(ref) if assertion(fact) == assertion(r)]
            if len(successors) != 1:
                break
            ref = successors[0].fact_ref
        return ref

    for row in candidates:
        original_ref = row.fact_ref
        # An exact source fact may already be in this ledger. Do
        # not rebase it onto its own evidence successor on replay.
        if lookup.get(row.fact_ref) is None and row.supersedes:
            predecessor = evidence_successor(rewritten.get(row.supersedes, row.supersedes))
            if predecessor != row.supersedes:
                row = Receipt.make(row.kind, None, row.payload, occurred_at=row.occurred_at,
                    observed_at=row.observed_at, producer=row.producer, evidence=row.evidence,
                    entity_ref=row.entity_ref, supersedes=predecessor)
        prior = lookup.get(row.fact_ref)
        if prior is not None:
            if prior.semantic() != row.semantic():
                raise LedgerError("immutable receipt identity conflict")
            if set(row.evidence) <= set(prior.evidence):
                # This exact fact and all supplied provenance are
                # already stored. A replay of history must not copy
                # an old branch's evidence onto another current head.
                rewritten[original_ref] = evidence_successor(row.fact_ref)
                duplicates += 1
                continue
            # Evidence is a revision chain of the current assertion,
            # never a fork from its historical first observation.
            # Recompute against pending rows too: P->Q->R must make
            # R supersede Q, not create two children of P.
            active = lookup.active(row)
            if not active:
                # A duplicate of a superseded historical assertion
                # cannot revive it or attach a fresh snapshot head.
                rewritten[original_ref] = evidence_successor(row.fact_ref)
                duplicates += 1
                continue
            current = active[-1]
            evidence = tuple(sorted(set(current.evidence) | set(row.evidence)))
            if evidence == current.evidence:
                rewritten[original_ref] = current.fact_ref
                duplicates += 1
                continue
            row = Receipt.make(row.kind, None, row.payload, occurred_at=row.occurred_at,
                               observed_at=row.observed_at, producer=row.producer,
                               evidence=evidence, entity_ref=row.entity_ref,
                               supersedes=current.fact_ref)
        if row.supersedes:
            prior_revision = lookup.get(row.supersedes)
            if prior_revision is None or (prior_revision.kind, prior_revision.entity_ref) != (row.kind, row.entity_ref):
                raise LedgerError("receipt revision has no matching predecessor")
        rewritten[original_ref] = row.fact_ref
        lookup.add(row)
        pending.append(row)
    return pending, duplicates, rewritten


class UsageLedger:
    """A local store; late cross-month evidence is appended as a correction."""

    # Process-wide hook: an optional zero-argument context manager factory
    # held around publication of one transaction. A host process sets it on
    # the class to mark this short critical section, e.g. so a deployment
    # waits for it but not for the whole collection cycle.
    append_section: Callable[[], Any] | None = None
    # Process-wide hook: an optional factory `(ledger) -> FactIndex` a host
    # process sets on the class to have ingest point-read a fact index instead
    # of building whole-ledger tables (refresh.json `incremental.ingest`).
    # Unset, no index file is opened or created.
    index_provider: Callable[["UsageLedger"], Any] | None = None

    def __init__(self, root: str | Path, *, clock: Callable[[], datetime] | None = None, authority=None,
                 checkpoint: bool = True, read_only: bool = False, index: Any = None):
        self.root = Path(root)
        # A fact index bound to this ledger (see `fact_index`). Derived and
        # disposable: any doubt means this ingest uses the tables instead.
        self._index = index
        # How the last ingest looked facts up: table, index, index_unavailable
        # or index_stale (the index was emptied for a rebuild).
        self.last_ingest_lookup: str | None = None
        self.authority = authority
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Process-local acceleration only. Transaction files remain the source
        # of truth. Verify their bytes before reusing parsed receipt objects.
        self._receipt_cache_signature = None
        self._receipt_cache_batches = None
        self._receipt_cache_rows = None
        self._receipt_cache_snapshot = None
        self._receipt_cache_digest_states = []
        # Private verified-prefix checkpoint shared across processes; a miss or
        # any doubt means the complete stream is read and verified as before.
        self.checkpoint = checkpoint
        # Offline readers: use a valid checkpoint but never write one, and never
        # create the lock file; nothing under the ledger root changes.
        self.read_only = read_only
        self._checkpoint_store: Checkpoint | None = None
        self._checkpoint_manifest: dict[str, Any] | None = None

    @staticmethod
    def _receipt_signature(directory: Path) -> tuple:
        paths = sorted(directory.glob("*.json"))
        result = []
        for path in paths:
            # Permission changes can share a timestamp on coarse filesystems.
            # Include the link's own identity so a warm cache never conceals
            # changed permissions, ownership, type or a symlink replacement.
            info = path.lstat()
            if path.is_symlink():
                raise LedgerError("receipt transaction sequence is incomplete")
            if info.st_mode & 0o077:
                raise LedgerError("receipt transaction is not private")
            # A same-size overwrite can share both filesystem timestamps.
            # Metadata is not evidence that the immutable transaction bytes
            # remain intact. Stream hashing retains the decode/memory savings.
            with path.open("rb") as stream:
                content_digest = file_digest(stream, "sha256").digest()
            result.append((path.name, info.st_dev, info.st_ino, info.st_mode,
                           info.st_uid, info.st_gid, info.st_nlink, info.st_size,
                           info.st_mtime_ns, info.st_ctime_ns, content_digest))
        return tuple(result)

    @contextmanager
    def _shared_lock(self) -> Iterator[None]:
        """The local ledger's reader lock; a read-only reader only takes an existing one."""
        path = self.root / ".usage-ledger.lock"
        if self.read_only:
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                yield  # No writer ever ran here.
                return
            stream = os.fdopen(fd, "rb")
        else:
            stream = path.open("a+")
        with stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            yield

    def _checkpoint(self) -> Checkpoint | None:
        """The private verified-prefix checkpoint for this authority, if supported."""
        if not self.checkpoint or self.authority is None:
            return None
        if self._checkpoint_store is None:
            describe = getattr(self.authority, "checkpoint_binding", None)
            if describe is None:
                return None
            try:
                binding, core_source = describe()
                self._checkpoint_store = Checkpoint(self.root, binding, logic_version(core_source))
            except Exception:
                self.checkpoint = False
                return None
        return self._checkpoint_store

    def _receipt_batches(self) -> list[dict[str, Any]]:
        """Verify the immutable transaction sequence; partial temp files are invisible."""
        directory = self.root / "receipts"
        if directory.is_symlink():
            raise LedgerError("receipt directory must not be a symlink")
        authoritative = None
        streamed = self.authority is not None and hasattr(self.authority, 'scan')
        checkpoint = manifest = None
        accepted = False
        if streamed:
            checkpoint = self._checkpoint()
            manifest = checkpoint.manifest() if checkpoint is not None else None
            if manifest is not None and hasattr(self.authority, "scan_after"):
                revision, authoritative, accepted = self.authority.scan_after(manifest["prefix"])
            else:
                revision, authoritative = self.authority.scan()
            signature = ('production_git', revision)
        else:
            authoritative = self.authority.read() if self.authority is not None else None
            signature = (tuple(b['sha256'] for b in authoritative) if authoritative is not None
                         else self._receipt_signature(directory))
        if (signature == self._receipt_cache_signature and self._receipt_cache_batches is not None
                and self._receipt_cache_rows is not None):
            return self._receipt_cache_batches
        sink = None if self.read_only else checkpoint
        fold = _ReceiptFold(sink)
        if accepted:
            # Verified prefix rows first; the core then reads only later batches.
            try:
                checkpoint.load(manifest, fold.add_verified)  # type: ignore[union-attr, arg-type]
            except CheckpointMiss:
                accepted = False
                fold = _ReceiptFold(sink)
                revision, authoritative = self.authority.scan()  # type: ignore[union-attr]
        if authoritative is None:
            paths = sorted(directory.glob("*.json"))
            for expected, path in enumerate(paths, 1):
                if path.is_symlink() or path.name != f"{expected:020d}.json":
                    raise LedgerError("receipt transaction sequence is incomplete")
                try:
                    batch = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise LedgerError("invalid receipt transaction") from exc
                fold.add(batch)
        else:
            try:
                for batch in authoritative:
                    if accepted and batch["sequence"] == 1:
                        # The core no longer recognised the prefix and reads it all.
                        accepted = False
                        fold = _ReceiptFold(sink)
                    fold.add(batch)
                if accepted and not self.authority.resume_confirmed(manifest["prefix"]["sequence"]):  # type: ignore[union-attr, index]
                    # The core did not continue after the loaded prefix (e.g. no head):
                    # never keep checkpoint rows it did not stand behind.
                    raise LedgerError("observation stream did not continue the checkpoint")
            except Exception:
                if not accepted:
                    fold.discard()
                    raise
                # Any doubt about the checkpoint: the complete read decides.
                fold.discard()
                accepted = False
                fold = _ReceiptFold(sink)
                revision, authoritative = self.authority.scan()  # type: ignore[union-attr]
                try:
                    for batch in authoritative:
                        fold.add(batch)
                except BaseException:
                    fold.discard()
                    raise
        verified = getattr(self.authority, "verified_signature", None) if streamed else None
        if verified is not None:
            # The authority names the objects this complete read consumed.
            final = verified()
            signature = ("production_git", final) if final is not None else None
        self._checkpoint_manifest = None
        if checkpoint is not None:
            if accepted and fold.writer is None:
                self._checkpoint_manifest = manifest  # Nothing appended since.
            else:
                self._checkpoint_manifest = self._commit_checkpoint(
                    fold, manifest if accepted else None,
                    rejected=manifest["digest"] if manifest is not None and not accepted else None)
        fold.discard()
        self._receipt_cache_signature = signature
        self._receipt_cache_batches = fold.result
        self._receipt_cache_rows = fold.rows
        self._receipt_cache_digest_states = fold.states
        return fold.result

    def _commit_checkpoint(self, fold: "_ReceiptFold", base: dict[str, Any] | None, *,
                           rejected: str | None = None) -> dict[str, Any] | None:
        """Persist the prefix this process verified; failure only slows the next read."""
        checkpoint = None if self.read_only else self._checkpoint()
        identity = getattr(self.authority, "checkpoint_prefix", None)
        if checkpoint is None or identity is None or not fold.result:
            return None
        try:
            prefix = identity()
        except Exception:
            prefix = None
        if (prefix is None or prefix["sequence"] != len(fold.result)
                or prefix["watermark"] != fold.result[-1]["sha256"]):
            return None
        writer, fold.writer = fold.writer, None
        return checkpoint.commit(writer, prefix, base, rejected=rejected)

    def _continue_after_own_append(self, batch: dict[str, Any], batches: list[dict[str, Any]]) -> None:
        """Extend the verified cache with this process's own confirmed append.

        Only when the authority proves its stream head is exactly this batch,
        which directly follows the cached chain. The returned signature is the
        same scan identity a later read compares, so any other node's append
        changes it and that read verifies the whole stream again.
        """
        self._receipt_cache_snapshot = None
        signature = None
        base, self._checkpoint_manifest = self._checkpoint_manifest, None
        appended = getattr(self.authority, "appended_signature", None)
        if (appended is not None and self._receipt_cache_signature is not None
                and self._receipt_cache_batches is batches
                and batch["sequence"] == len(batches) + 1
                and batch["previous"] == (batches[-1]["sha256"] if batches else None)):
            try:
                signature = appended(batch)
            except Exception:
                signature = None
        if signature is None:
            self._receipt_cache_signature = None
            return
        pool = _ReceiptReadPool()
        parsed = [Receipt.from_dict(raw, _pool=pool) for raw in batch["facts"]]
        prefix_digest = self._receipt_cache_digest_states[-1].copy() if batches else sha256(b"[")
        lines = [canonical(raw) for raw in batch["facts"]]
        for i, line in enumerate(lines):
            if batches or i:
                prefix_digest.update(b",")
            prefix_digest.update(line)
        meta = {k: v for k, v in batch.items() if k != "facts"}
        self._receipt_cache_batches = [*batches, meta]
        self._receipt_cache_rows = [*self._receipt_cache_rows, parsed]
        self._receipt_cache_digest_states = [*self._receipt_cache_digest_states, prefix_digest]
        self._receipt_cache_signature = ("production_git", signature)
        checkpoint = None if self.read_only else self._checkpoint()
        if checkpoint is not None and (not batches if base is None
                                       else base["prefix"]["sequence"] == len(batches)):
            fold = _ReceiptFold(checkpoint)
            fold.result = self._receipt_cache_batches
            fold.sink(meta, lines)
            self._checkpoint_manifest = self._commit_checkpoint(fold, base)
            fold.discard()

    def stream_batches(self, *, after: int = 0) -> Iterator[tuple[dict[str, Any], list[bytes]]]:
        """Yield each verified batch after `after` as (metadata, canonical fact lines).

        Reads the verified checkpoint prefix, then the authority after it (or
        the whole stream), and keeps no rows: memory is one batch. Every batch
        is checked against the chain; a source that restarts the stream must
        repeat the digests already yielded. Writes no checkpoint and no cache.
        """
        if type(after) is not int or after < 0:
            raise LedgerError("invalid stream position")
        digests: list[str] = []
        previous_at: list[datetime] = []

        def verified(meta: dict[str, Any], lines: list[bytes]) -> bool:
            sequence = meta["sequence"]
            if type(sequence) is int and 1 <= sequence <= len(digests):
                if digests[sequence - 1] != meta["sha256"]:
                    raise LedgerError("observation stream changed during the read")
                return False
            instant = parse_timestamp(meta["recorded_at"], "recorded_at")
            if (meta["schema"] != "computecost_ledger_transaction/v1" or sequence != len(digests) + 1
                    or meta["previous"] != (digests[-1] if digests else None) or not lines
                    or (previous_at and instant <= previous_at[0])
                    or batch_digest(meta, lines) != meta["sha256"]):
                raise LedgerError("receipt transaction digest/order mismatch")
            digests.append(meta["sha256"])
            previous_at[:] = [instant]
            return sequence > after

        def split(batch: dict[str, Any]) -> tuple[dict[str, Any], list[bytes]]:
            if set(batch) != {"schema", "sequence", "recorded_at", "previous", "facts", "sha256"} \
                    or not isinstance(batch["facts"], list):
                raise LedgerError("receipt transaction fields are invalid")
            return {k: v for k, v in batch.items() if k != "facts"}, [canonical(raw) for raw in batch["facts"]]

        if self.authority is None:
            directory = self.root / "receipts"
            if not directory.exists():
                return
            if directory.is_symlink():
                raise LedgerError("receipt directory must not be a symlink")
            with self._shared_lock():
                for expected, path in enumerate(sorted(directory.glob("*.json")), 1):
                    if path.is_symlink() or path.name != f"{expected:020d}.json":
                        raise LedgerError("receipt transaction sequence is incomplete")
                    try:
                        meta, lines = split(json.loads(path.read_bytes()))
                    except (OSError, ValueError) as exc:
                        raise LedgerError("invalid receipt transaction") from exc
                    if verified(meta, lines):
                        yield meta, lines
            return
        if not hasattr(self.authority, "scan"):
            for batch in self.authority.read():
                meta, lines = split(batch)
                if verified(meta, lines):
                    yield meta, lines
            return
        checkpoint = self._checkpoint()
        manifest = checkpoint.manifest() if checkpoint is not None else None
        resumed = False
        if manifest is not None and hasattr(self.authority, "scan_after"):
            # The core first confirms it still holds this exact prefix and a head
            # reaching it; only then may a consumer that stops early see its rows.
            _, authoritative, resumed = self.authority.scan_after(manifest["prefix"])
        if resumed:
            try:
                for meta, lines in checkpoint.iter_load(manifest):  # type: ignore[union-attr]
                    if verified(meta, lines):
                        yield meta, lines
            except CheckpointMiss:
                resumed = False
        if not resumed:
            _, authoritative = self.authority.scan()
        loaded = len(digests)
        reached = [0]

        def read(source: Iterable[dict[str, Any]]) -> Iterator[tuple[dict[str, Any], list[bytes]]]:
            for batch in source:
                meta, lines = split(batch)
                reached[0] = meta["sequence"]
                if verified(meta, lines):
                    yield meta, lines

        yield from read(authoritative)
        if resumed and self.authority.resume_confirmed(manifest["prefix"]["sequence"]):  # type: ignore[index]
            return
        if resumed:
            # The core did not continue after the prefix: the complete read decides.
            reached[0] = 0
            _, authoritative = self.authority.scan()
            yield from read(authoritative)
        if reached[0] < loaded:
            raise LedgerError("observation stream no longer holds the checkpoint prefix")

    def receipt_snapshot(self, *, as_of: str | None = None,
                         watermark: int | None = None) -> dict[str, Any]:
        """Select what the ledger knew then, NOT old source observation timestamps."""
        if watermark is not None and (isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0):
            raise LedgerError("invalid receipt watermark")
        cutoff = parse_timestamp(as_of, "as_of") if as_of else None
        if self.authority is None and not self.root.exists():
            return {"facts": [], "watermark": 0, "recorded_at": None, "sha256": digest([])}
        try:
            if self.authority is not None:
                batches = self._receipt_batches()
            else:
                with self._shared_lock():
                    batches = self._receipt_batches()
        except OSError as exc:
            raise LedgerError("cannot read receipt ledger") from exc
        if as_of is None and watermark is None and self._receipt_cache_snapshot is not None:
            cached_signature, cached = self._receipt_cache_snapshot
            if cached_signature == self._receipt_cache_signature:
                return {**cached, "facts": list(cached["facts"])}
        selected_indexes = [i for i,b in enumerate(batches) if (watermark is None or b["sequence"] <= watermark)
                    and (cutoff is None or parse_timestamp(b["recorded_at"], "recorded_at") <= cutoff)]
        selected = [batches[i] for i in selected_indexes]
        facts = [row for i in selected_indexes for row in self._receipt_cache_rows[i]]
        last = selected[-1] if selected else None
        prefix_digest = self._receipt_cache_digest_states[selected_indexes[-1]].copy() if last else sha256(b"[")
        prefix_digest.update(b"]")
        result = {"facts": facts, "watermark": last["sequence"] if last else 0,
                  "recorded_at": last["recorded_at"] if last else None,
                  "sha256": prefix_digest.hexdigest()}
        if as_of is None and watermark is None:
            self._receipt_cache_snapshot = (self._receipt_cache_signature,
                {**result, "facts": tuple(facts)})
        return result

    def ingest_receipts(self, incoming: Iterable[Receipt], *,
                        expected_watermark: int | None = None) -> dict[str, int]:
        """Append one all-or-nothing batch in the existing ledger's lock domain.

        v1 usage/corrections remain read-only-compatible. New receipts are never
        also inserted into v1; compatibility is a projection, not a second bill.
        """
        if self.read_only:
            raise LedgerError("a read-only ledger reader cannot append")
        if expected_watermark is not None and (type(expected_watermark) is not int or expected_watermark < 0):
            raise LedgerError("invalid expected receipt watermark")
        candidates = list(incoming)
        if any(not isinstance(r, Receipt) for r in candidates):
            raise LedgerError("receipt input must contain validated domain facts")
        # Even callers constructing dataclasses directly must pass the closed wire contract.
        for row in candidates:
            Receipt.from_dict(row.to_dict())
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.root / "receipts"
        if directory.is_symlink():
            raise LedgerError("receipt directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        self.last_ingest_lookup = None
        index = self._ingest_index()  # Opened (and first built) before the exclusive lock.
        try:
            with (self.root / ".usage-ledger.lock").open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                batches = self._receipt_batches()
                # Derived claims must still refer to the snapshot the collector read.
                # Reject before ingest; never rebase differing evidence or choose a winner.
                if expected_watermark is not None and len(batches) != expected_watermark:
                    raise SnapshotChanged("receipt ledger snapshot changed; recollect from current watermark")
                # Lookups only: no second store and no change to fact
                # identity, predecessor checks or recorded-at barriers.
                lookup = self._index_lookup(index, batches) if index is not None else None
                if self._index is None:
                    index = None  # Emptied as stale: this ingest's batch is not indexed either.
                if lookup is None:
                    lookup = TableLookup(self._receipt_cache_rows)
                    if self.last_ingest_lookup is None:
                        self.last_ingest_lookup = "table"
                reader = index if isinstance(lookup, IndexLookup) else None
                try:
                    pending, duplicates, _ = _decide_ingest(candidates, lookup)
                except LedgerError:
                    raise
                except Exception:
                    if reader is None:
                        raise
                    # A failed index read decides nothing: the tables decide this ingest.
                    self._index = index = None
                    self.last_ingest_lookup = "index_unavailable"
                    pending, duplicates, _ = _decide_ingest(candidates, TableLookup(self._receipt_cache_rows))
                finally:
                    if reader is not None:
                        try:
                            reader.end_read()  # Before any write; the snapshot served the decision.
                        except Exception:
                            pass
                if reader is not None and index is not None and pending:
                    # A new row must be new to the ledger itself, not only to its
                    # derived index: one ref set from the verified read confirms it.
                    stored = {row.fact_ref for rows in self._receipt_cache_rows for row in rows}
                    if any(row.fact_ref in stored for row in pending):
                        self._discard_index(index)
                        index = None
                        pending, duplicates, _ = _decide_ingest(candidates, TableLookup(self._receipt_cache_rows))
                if not pending:
                    return {"inserted": 0, "duplicates": duplicates, "watermark": len(batches)}
                now = self._clock()
                if now.tzinfo is None:
                    raise LedgerError("ledger clock must be timezone aware")
                if batches:
                    now = max(now, parse_timestamp(batches[-1]["recorded_at"], "recorded_at") + timedelta(microseconds=1))
                batch = {"schema": "computecost_ledger_transaction/v1", "sequence": len(batches) + 1,
                         "recorded_at": utc_text(now), "previous": batches[-1]["sha256"] if batches else None,
                         "facts": [r.to_dict() for r in pending]}
                batch["sha256"] = digest(batch)
                factory = type(self).append_section
                section = factory() if factory is not None else nullcontext()
                if self.authority is not None:
                    with section:
                        self.authority.append(batch)
                    self._continue_after_own_append(batch, batches)
                    self._index_append(index, batch)  # After the publication section, under the lock.
                    return {"inserted": len(pending), "duplicates": duplicates, "watermark": batch["sequence"]}
                with section:
                    target = directory / f"{batch['sequence']:020d}.json"
                    fd, temp_name = tempfile.mkstemp(prefix=".pending-", dir=directory)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(canonical(batch) + b"\n")
                            handle.flush()
                            os.fsync(handle.fileno())
                        if target.exists():
                            raise LedgerError("receipt transaction already exists")
                        os.replace(temp_name, target)
                        dir_fd = os.open(directory, os.O_RDONLY)
                        try:
                            os.fsync(dir_fd)
                        finally:
                            os.close(dir_fd)
                        prefix_digest = self._receipt_cache_digest_states[-1].copy() if batches else sha256(b"[")
                        for i, raw in enumerate(batch["facts"]):
                            if batches or i:
                                prefix_digest.update(b",")
                            prefix_digest.update(canonical(raw))
                        self._receipt_cache_digest_states.append(prefix_digest)
                        self._receipt_cache_batches = [*batches, {k: v for k, v in batch.items() if k != "facts"}]
                        self._receipt_cache_rows = [*self._receipt_cache_rows, list(pending)]
                        self._receipt_cache_signature = self._receipt_signature(directory)
                        self._receipt_cache_snapshot = None
                    finally:
                        if os.path.exists(temp_name):
                            os.unlink(temp_name)
                # Outside the publication section a deployment waits for, still under the lock.
                self._index_append(index, batch)
                return {"inserted": len(pending), "duplicates": duplicates, "watermark": batch["sequence"]}
        except OSError as exc:
            raise LedgerError("receipt publication failed; replay the same batch, not the business request") from exc

    def _ingest_index(self) -> Any:
        """This ledger's fact index for ingest, from the class provider on first use."""
        provider = type(self).index_provider
        if self._index is None and provider is not None:
            try:
                self._index = provider(self)
            except Exception:
                self._index = None
                self.last_ingest_lookup = "index_unavailable"
        return self._index

    def _index_lookup(self, index: Any, batches: list[dict[str, Any]]) -> IndexLookup | None:
        """Point reads on `index` once it holds exactly `batches`; else None (use the tables).

        Called under the exclusive lock after the verified read: batches other
        writers appended since the index last saw this ledger come from that
        read, never from a second read of the stream. The prefix check and
        every point read of the decision share one pinned SQLite snapshot
        (ended by the caller via `end_read`), so a reset another process
        commits meanwhile cannot hide stored facts from this decision.
        """
        from .fact_index import IndexMismatch
        heads = [b["sha256"] for b in batches]
        try:
            bound = index.binding()
            if bound.sequence > len(heads) or (bound.sequence and heads[bound.sequence - 1] != bound.head_sha256):
                raise IndexMismatch("index is not a prefix of this ledger")
            for meta, rows in zip(batches[bound.sequence:], self._receipt_cache_rows[bound.sequence:]):
                index.apply_batch(meta, [canonical(row.to_dict()) for row in rows])
            index.begin_read()
            bound = index.binding()
            if bound.sequence != len(heads) or (heads and bound.head_sha256 != heads[-1]):
                raise IndexMismatch("index does not hold exactly this ledger")
            # The binding names the prefix; the rows must also all be there.
            facts = sum(len(rows) for rows in self._receipt_cache_rows)
            if index.row_counts() != (len(heads), facts, facts):
                index.end_read()
                self._discard_index(index)
                return None
        except IndexMismatch:
            index.end_read()
            # Empty it only when it is truly another history, judged in one write
            # transaction; an index merely behind (e.g. just reset) is kept.
            try:
                cleared = index.clear_unless_prefix(heads)
            except Exception:
                cleared = False
            if cleared:
                self._index, self.last_ingest_lookup = None, "index_stale"
            else:
                self.last_ingest_lookup = "index_unavailable"
            return None
        except Exception:
            try:
                index.end_read()
            except Exception:
                pass
            self.last_ingest_lookup = "index_unavailable"
            return None
        self.last_ingest_lookup = "index"
        return IndexLookup(index)

    def _discard_index(self, index: Any) -> None:
        """The index lost rows its binding claims: empty it for a rebuild; the tables decide."""
        try:
            index.clear()
        except Exception:
            pass
        self._index, self.last_ingest_lookup = None, "index_stale"

    @staticmethod
    def _index_append(index: Any, batch: dict[str, Any]) -> None:
        """Index this process's own appended batch; failure leaves it one batch behind."""
        if index is None:
            return
        try:
            index.apply_batch({k: v for k, v in batch.items() if k != "facts"},
                              [canonical(raw) for raw in batch["facts"]])
        except Exception:
            pass  # The next ingest catches the index up from its verified read.

    def _month_paths(self, month: str) -> tuple[Path, Path]:
        return (
            self.root / "usage" / f"{month}.jsonl",
            self.root / "corrections" / f"{month}.jsonl",
        )

    def read_month(self, month: str) -> list[UsageEvent]:
        usage, corrections = self._month_paths(month)
        events = _read_file(usage) + _read_file(corrections)
        seen: set[str] = set()
        for event in events:
            if event.event_id in seen:
                raise LedgerError(f"duplicate event id in month ledger: {event.event_id}")
            seen.add(event.event_id)
        return sorted(events, key=lambda event: (event.occurred_at, event.event_id))

    def ingest(self, incoming: Iterable[UsageEvent]) -> LedgerIngestResult:
        candidates = list(incoming)
        if not candidates:
            return LedgerIngestResult(0, 0, 0, 0)

        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".usage-ledger.lock"
        try:
            lock_handle = lock_path.open("a+", encoding="utf-8")
        except OSError as exc:
            raise LedgerError("cannot open usage ledger lock") from exc

        inserted = duplicates = usage_inserted = corrections_inserted = 0
        with lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            all_pending: dict[Path, list[UsageEvent]] = {}
            known: dict[str, UsageEvent] = {}
            existing_paths = sorted(
                list((self.root / "usage").glob("*.jsonl"))
                + list((self.root / "corrections").glob("*.jsonl"))
            )
            for existing_path in existing_paths:
                for existing_event in _read_file(existing_path):
                    if existing_event.event_id in known:
                        raise LedgerError(
                            f"duplicate event id already exists in ledger: {existing_event.event_id}"
                        )
                    known[existing_event.event_id] = existing_event

            for event in sorted(
                candidates,
                key=lambda item: (item.product_month, item.occurred_at, item.event_id),
            ):
                prior = known.get(event.event_id)
                if prior is not None:
                    if prior.semantic_dict() != event.semantic_dict():
                        raise LedgerError(
                            f"event id conflict for immutable evidence: {event.event_id}"
                        )
                    duplicates += 1
                    continue
                usage_path, correction_path = self._month_paths(event.product_month)
                target = correction_path if event.late_event else usage_path
                all_pending.setdefault(target, []).append(event)
                known[event.event_id] = event

            # All partitions have passed malformed/conflict checks before the
            # first append, so an evidence conflict cannot partially ingest a
            # different month from the same batch.
            for target in sorted(all_pending):
                events = all_pending[target]
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(fd, "a", encoding="utf-8") as handle:
                        for event in events:
                            handle.write(
                                json.dumps(
                                    event.to_dict(),
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                )
                                + "\n"
                            )
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError as exc:
                    raise LedgerError(f"cannot append ledger partition {target.name}") from exc
                inserted += len(events)
                if target.parent.name == "corrections":
                    corrections_inserted += len(events)
                else:
                    usage_inserted += len(events)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return LedgerIngestResult(
            inserted=inserted,
            duplicates=duplicates,
            usage_inserted=usage_inserted,
            corrections_inserted=corrections_inserted,
        )
