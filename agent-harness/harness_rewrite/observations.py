"""Typed observation facts on the same production Git authority, outside WI grants.

A pre-WI context is not a synthetic WorkItem. Receipt semantics belong to the
versioned metering producer; this module owns immutable admission, order and
publication. It cannot mint execution rights or change a lifecycle decision.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Callable, Iterable, Iterator

from .model import Json, Rejected, digest, encoded
from .production_ledger import ProductionLedger
from .production_segments import object_path, pack, unpack

PREFIX = ".harness/core/observations/computecost/"
HEAD = PREFIX + "head.json"
OBJECTS = ".harness/core/objects/"
SCHEMA = "harness.observation-stream/v1"
KINDS = frozenset({"stage_goal", "stage_output", "stage_feedback", "stage_participation", "stage_link",
    "output_assessment", "execution_expectation", "execution", "usage", "attribution", "human",
    "delivery", "review", "value", "human_summary", "coverage"})


class ObservationStore:
    def __init__(self, ledger: ProductionLedger, *, guard: Callable[[], None] = lambda: None):
        self.ledger, self.guard = ledger, guard
        self.revision: str | None = None
        self.watermark: str | None = None
        self.sequence = 0
        # Sequence of the verified prefix the last read resumed after (0: full read).
        self.resumed = 0

    @staticmethod
    def prefix_identity(tree: dict[str, str], sequence: int, chunks: Iterable[str]) -> str:
        """Git object identity of batches 1..sequence: wrappers plus the chunks they name.

        Object ids are content hashes, so an equal identity means every byte a
        complete verified read consumed for that prefix is unchanged.
        """
        objects = {f"{PREFIX}{i:020d}.json": tree.get(f"{PREFIX}{i:020d}.json", "missing")
                   for i in range(1, sequence + 1)}
        for path in chunks:
            if not isinstance(path, str) or not path.startswith(OBJECTS):
                raise Rejected("observation_invalid", "Observation prefix names a foreign object")
            objects[path] = tree.get(path, "missing")
        return digest(objects)

    def _resumable(self, tree: dict[str, str], head: Json, after: Json | None) -> Json | None:
        """The verified prefix to continue after, or None for a complete read."""
        if after is None:
            return None
        if (not isinstance(after, dict) or set(after) != {"sequence", "watermark", "objects", "chunks"}
                or type(after["sequence"]) is not int or after["sequence"] < 1
                or not isinstance(after["watermark"], str) or not re.fullmatch(r"[0-9a-f]{64}", after["watermark"])
                or not isinstance(after["objects"], str) or not isinstance(after["chunks"], list)
                or after["sequence"] > head["sequence"]):
            return None
        try:
            if self.prefix_identity(tree, after["sequence"], after["chunks"]) != after["objects"]:
                return None
        except Rejected:
            return None
        return after

    @staticmethod
    def validate(batch: Json, sequence: int, previous: str | None) -> None:
        if (set(batch) != {"schema", "sequence", "recorded_at", "previous", "facts", "sha256"}
                or batch["schema"] != "computecost_ledger_transaction/v1"
                or batch["sequence"] != sequence or batch["previous"] != previous
                or batch["sha256"] != digest({k: v for k, v in batch.items() if k != "sha256"})
                or not isinstance(batch["facts"], list) or not batch["facts"]):
            raise Rejected("observation_invalid", "Observation order or digest differs")
        for fact in batch["facts"]:
            if (not isinstance(fact, dict) or set(fact) != {"schema", "kind", "entity_ref", "fact_ref",
                    "occurred_at", "observed_at", "producer", "evidence", "supersedes", "payload"}
                    or fact["schema"] != "computecost_collaboration_fact/v1" or fact["kind"] not in KINDS):
                raise Rejected("observation_invalid", "Unsupported typed observation")
            refs = [fact["entity_ref"], fact["fact_ref"], *(fact["evidence"] if isinstance(fact["evidence"], list) else [None])]
            if fact["supersedes"] is not None:
                refs.append(fact["supersedes"])
            if (any(not isinstance(v, str) or not re.fullmatch(r"ccf1_[a-f0-9]{64}", v) for v in refs)
                    or fact["fact_ref"] != "ccf1_" + digest(["fact", {k: v for k, v in fact.items()
                        if k not in {"fact_ref", "observed_at", "evidence"}}])
                    or not isinstance(fact["producer"], str)
                    or not re.fullmatch(r"[a-zA-Z0-9_.:-][a-zA-Z0-9_.:/-]{0,127}", fact["producer"])
                    or not isinstance(fact["payload"], dict)):
                raise Rejected("observation_invalid", "Observation source identity differs")
            for at in [fact["observed_at"], *([fact["occurred_at"]] if fact["occurred_at"] is not None else [])]:
                if not isinstance(at, str) or datetime.fromisoformat(at).tzinfo is None:
                    raise Rejected("observation_invalid", "Observation time is unavailable")
            # Detailed domain validation belongs to its typed producer. The
            # shared fact boundary additionally refuses raw transcript/identity
            # containers and known credential forms, including nested values.
            stack = [fact["payload"]]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    if {str(k).lower() for k in value} & {"prompt", "content", "text", "response", "credential",
                            "session_id", "thread_id", "request_id", "message_id", "token", "api_key", "path", "raw_id"}:
                        raise Rejected("observation_invalid", "Private source bytes are not observation facts")
                    stack.extend(value.values())
                elif isinstance(value, list):
                    stack.extend(value)
                elif isinstance(value, str) and (value.startswith(("/", "file:", "sk-")) or "\x00" in value
                        or re.search(r"(?:github_pat_|gh[pousr]_|https?://[^/\s@]+@)", value)):
                    raise Rejected("observation_invalid", "Private source bytes are not observation facts")

    def read(self, *, revision: str | None = None) -> list[Json]:
        return list(self.iter_batches(revision=revision))

    def scan(self) -> tuple[str, Iterator[Json]]:
        """Cache by immutable fact objects, not unrelated commits on main.

        Include packed objects as well as stream wrappers: changing a historical
        object without changing its head must invalidate the verified cache.
        """
        self.guard()
        revision = self.ledger.refresh()
        tree = self.ledger.tree(revision)
        token = digest({p: oid for p, oid in tree.items()
                        if p.startswith((PREFIX, '.harness/core/objects/'))})
        return token, self.iter_batches(revision=revision)

    def iter_batches(self, *, revision: str | None = None, after: Json | None = None) -> Iterator[Json]:
        """Verify one immutable batch at a time, including during adoption.

        `after` names a prefix a previous complete read verified: its sequence,
        last digest, Git object identity and the chunk objects it consumed. When
        that identity still matches this revision's tree, only later batches are
        read and validated, chained to the prefix digest; otherwise (or without
        it) every batch is read. `resumed` tells the caller which happened.
        """
        self.guard()
        ledger = self.ledger
        revision = revision or ledger.refresh()
        tree = ledger.tree(revision)
        ledger.dependencies = {HEAD}
        ledger.prefixes = {PREFIX}
        # A partial/failed read cannot authorize an append using a stale head.
        self.revision = None
        self.resumed = 0
        if HEAD not in tree:
            self.watermark, self.sequence = None, 0
            self.revision = revision
            return
        def read(path: str) -> bytes:
            if path not in tree:
                raise Rejected("observation_invalid", "Observation object is missing")
            return ledger.blobs({path: tree[path]})[path]
        head = json.loads(read(HEAD))
        if (set(head) != {"schema", "namespace", "sequence", "sha256"}
                or head["schema"] != SCHEMA or head["namespace"] != ledger.namespace
                or type(head["sequence"]) is not int or head["sequence"] < 1):
            raise Rejected("observation_invalid", "Observation authority differs")
        prefix = self._resumable(tree, head, after)
        start, previous = (prefix["sequence"], prefix["watermark"]) if prefix is not None else (0, None)
        if prefix is not None:
            # Publication conflicts still watch every object the prefix consumed.
            ledger.dependencies.update(f"{PREFIX}{i:020d}.json" for i in range(1, start + 1))
            ledger.dependencies.update(prefix["chunks"])
        self.resumed = start
        paths = {f"{PREFIX}{i:020d}.json": tree.get(f"{PREFIX}{i:020d}.json", "missing")
                 for i in range(start + 1, head["sequence"] + 1)}
        # Read wrappers and only their referenced chunks in two fixed Git batches.
        # Reused streams keep a cold read bounded in memory without a process per
        # receipt/chunk, an aggregate disk spool or skipped validation.
        with ledger.object_reader(paths) as wrapper_read:
            objects = {}
            for path in paths:
                wrapper = json.loads(wrapper_read(path))
                if "record" not in wrapper:
                    for sha in wrapper["object"]["chunks"]:
                        source = object_path(sha)
                        if source not in tree:
                            raise Rejected("observation_invalid", "Observation object is missing")
                        objects[source] = tree[source]
                del wrapper
            with ledger.object_reader(objects) as object_read:
                for i, path in enumerate(paths, start + 1):
                    wrapper = json.loads(wrapper_read(path))
                    batch = (wrapper["record"] if "record" in wrapper
                             else json.loads(unpack(wrapper["object"], object_read)))
                    self.validate(batch, i, previous)
                    previous = batch["sha256"]
                    yield batch
                if head["sha256"] != previous:
                    raise Rejected("observation_invalid", "Observation head differs from facts")
        self.watermark, self.sequence = previous, head['sequence']
        self.guard()
        self.revision = revision

    def append(self, batch: Json) -> None:
        self.append_many([batch])

    def append_many(self, batches: list[Json]) -> None:
        """Publish a bounded group without rereading its entire prefix."""
        self.guard()
        if self.revision is None:
            raise Rejected("observation_invalid", "Read original observations before appending")
        if not batches:
            return
        updates: dict[str, bytes] = {}
        sequence, watermark, size = self.sequence, self.watermark, 0
        for batch in batches:
            self.validate(batch, sequence + 1, watermark)
            raw = encoded(batch)
            size += len(raw)
            if len(batches) > 1 and size > 16 * 1024 * 1024:
                raise Rejected('observation_invalid', 'Observation adoption batch exceeds its memory bound')
            wrapper = {"record": batch} if len(raw) <= 512 * 1024 else {"object": pack(raw, updates)}
            updates[f"{PREFIX}{batch['sequence']:020d}.json"] = encoded(wrapper)
            sequence, watermark = batch['sequence'], batch['sha256']
        updates[HEAD] = encoded({"schema": SCHEMA, "namespace": self.ledger.namespace,
            "sequence": sequence, "sha256": watermark})
        self.revision = self.ledger.publish(self.revision, updates)
        self.sequence, self.watermark = sequence, watermark
        self.guard()
