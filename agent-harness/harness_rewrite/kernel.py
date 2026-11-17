"""One transactional admission boundary with replay from immutable private inputs."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator, TYPE_CHECKING, overload

from .domain import advance, decide, observation, reduce, public_state
from . import collaboration, location, measurements, migration, watch, notifications, legacy, existing_execution
from . import children, pull_requests, self_switch
from .workspaces import session_write
from .repository_identity import semantic_identity
from .model import (
    Authenticate,
    Command,
    Decision,
    HostContext,
    Json,
    Query,
    Rejected,
    digest,
    encoded,
    identity,
    operation_id,
)

if TYPE_CHECKING:
    from .production_ledger import ProductionLedger

SCHEMA = 1
WRITER_CONTRACT = 3
LOCATION_CONTRACT = 2
EMPTY = "0" * 64
# Intents that create a WorkItem; its identity is assigned by the kernel.
CREATION = frozenset({"create_draft", "create_lightweight", "create_child"})
DDL = (
    "CREATE TABLE originals (namespace TEXT, digest TEXT, content BLOB NOT NULL, "
    "PRIMARY KEY(namespace,digest))",
    "CREATE TABLE journal (namespace TEXT, wi TEXT, revision INTEGER, request_id TEXT, "
    "semantic_digest TEXT NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL, "
    "PRIMARY KEY(namespace,wi,revision), UNIQUE(namespace,request_id))",
    # This is the integrity footer of the authoritative journal, not a state cache.
    "CREATE TABLE journal_heads (namespace TEXT, wi TEXT, revision INTEGER NOT NULL, "
    "digest TEXT NOT NULL, PRIMARY KEY(namespace,wi))",
)


def storage_error(exc: Exception) -> Rejected:
    corrupt = getattr(exc, "sqlite_errorcode", None) in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
    }
    return Rejected(
        "journal_invalid" if corrupt else "storage_unavailable",
        "Journal integrity failed" if corrupt else "Journal operation could not be completed",
    )


def standing_rows(wi: str, state: Json, actor: str | None, workspaces: Json | None) -> list[Json]:
    """Relay's view of the standing grants a WI journal carries."""
    from . import access

    return [
        {key: grant[key] for key in ("grant_id", "paths", "mode", "reason", "admission")}
        | {"actor": grant["principal"]["actor"], "work_item_id": wi}
        for grant in access.standing(state, actor, workspaces or {})
    ]


class Kernel:
    """Private database and mandatory authenticating host boundary.

    Constructing a kernel does not open or mutate storage. inspect/resume use a
    read-only SQLite connection and never mint grants. A trusted embedding must
    supply authenticate; caller-supplied HostContext objects are not accepted by
    default. There is intentionally no CLI or production activation in S1.
    """

    def __init__(
        self,
        database: Path | None,
        authenticate: Authenticate,
        *,
        clock: Callable[[], str] | None = None,
        fault: Callable[[str], None] | None = None,
        writer_id: str | None = None,
        storage: ProductionLedger | None = None,
        pages: Path | None = None,
    ):
        self._database = database
        self._storage = storage
        self._authenticate = authenticate
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._fault = fault or (lambda stage: None)
        self._writer_id = writer_id
        # Disposable project pages beside the JSON views; None disables them.
        self._pages = pages

    def _host(self, handle: object) -> HostContext:
        try:
            context = self._authenticate(handle)
        except (PermissionError, KeyError) as exc:
            raise Rejected("authority_denied", "Host authentication failed") from exc
        if not isinstance(context, HostContext):
            raise Rejected("authority_denied", "Host adapter supplied no validated context")
        context.validate()
        return replace(
            context,
            fact_store=None,
            effect_binding=None,
            related_parent=None,
            recipient_work=None,
            merge_authority=(),
            switch_target=None,
        )

    def _database_path(self) -> Path:
        if self._database is None:
            raise Rejected("operation_scope", "Production has no independent SQLite fact store")
        return self._database

    @contextmanager
    def _connection(
        self, *, write: bool, activation: bool = False, location_upgrade: bool = False,
        work_item_id: str | None = None, collection: str | None = None
    ) -> Iterator[sqlite3.Connection]:
        if self._storage is not None:
            with self._storage.connection(write=write, work_item_id=work_item_id, collection=collection) as conn:
                yield conn
            return
        database = self._database_path()
        if database.is_symlink():
            raise Rejected("storage_unavailable", "Journal path cannot be a symbolic link")
        if write:
            try:
                fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        mode = "rw" if write else "ro"
        uri = database.absolute().as_uri() + "?mode=" + mode
        conn = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.create_function("harness_writer_id", 0, lambda: self._writer_id)
        conn.create_function("harness_writer_contract", 0, lambda: WRITER_CONTRACT)
        conn.create_function("harness_store_location", 0, lambda: location.current(conn, database))
        conn.create_function("harness_custody_id", 0, lambda: location.custody_id(conn, database))
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and write:
                if conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise Rejected("journal_invalid", "Unrecognized existing database")
                for statement in DDL:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={SCHEMA}")
            elif version != SCHEMA:
                raise Rejected("journal_invalid", "Unsupported journal schema")
            if (
                write
                and not location.admitted(conn, database)
                and not (activation and location_upgrade and location.custody(conn) is None)
            ):
                raise Rejected(
                    "authority_location_changed",
                    "This fact-store copy is read-only; use its admitted location or a sealed transfer",
                )
            if write and not activation and not self._program_admitted(conn):
                raise Rejected(
                    "program_changed",
                    "Use the installed active program; this writer is no longer admitted",
                )
            yield conn
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def _program_admitted(self, conn: sqlite3.Connection) -> bool:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_binding'"
        ).fetchone():
            return True
        binding = conn.execute("SELECT program FROM runtime_binding WHERE singleton=1").fetchone()
        return binding is not None and binding["program"] == self._writer_id

    @staticmethod
    def _replay(
        conn: sqlite3.Connection, namespace: str, wi: str, at: int | None = None
    ) -> tuple[Json | None, int, str, list[Json]]:
        rows = conn.execute(
            "SELECT * FROM journal WHERE namespace=? AND wi=? ORDER BY revision",
            (namespace, wi),
        ).fetchall()
        head = conn.execute(
            "SELECT * FROM journal_heads WHERE namespace=? AND wi=?", (namespace, wi)
        ).fetchone()
        state, revision, previous, sequence = None, 0, EMPTY, 0
        history: list[Json] = []
        selected: Json | None = None
        try:
            for row in rows:
                revision += 1
                content = json.loads(row["content"])
                if (
                    row["revision"] != revision
                    or content["revision"] != revision
                    or content["namespace"] != namespace
                    or content["wi"] != wi
                    or content["previous"] != previous
                    or digest(content) != row["digest"]
                    or content["request_id"] != row["request_id"]
                    or content["semantic_digest"] != row["semantic_digest"]
                    or digest(content["semantics"]) != content["semantic_digest"]
                ):
                    raise ValueError("Invalid chain")
                for source_digest in content["originals"]:
                    validate_original = getattr(conn, "validate_original", None)
                    if validate_original is not None:
                        validate_original(namespace, source_digest)
                        continue
                    original = conn.execute(
                        "SELECT content FROM originals WHERE namespace=? AND digest=?",
                        (namespace, source_digest),
                    ).fetchone()
                    if original is None or hashlib.sha256(original[0]).hexdigest() != source_digest:
                        raise ValueError("Original missing or corrupted")
                for item in content["events"]:
                    sequence += 1
                    if item["sequence"] != sequence:
                        raise ValueError("Invalid event sequence")
                history.append(content)
                previous = row["digest"]
            if (
                bool(rows) != (head is not None)
                or head is not None
                and (head["revision"] != revision or head["digest"] != previous)
            ):
                raise ValueError("Invalid journal footer")
            # Prove the whole chain before classifying a newer event contract.
            # Unknown semantics cannot be treated as damaged original bytes.
            # The replay owns its state: each decision applies to it once. An
            # earlier decision applies as a private parse of its row; the state
            # returned holds its own decision's payloads, as `reduce` does.
            for index, content in enumerate(history):
                returned = at == content["revision"] or at is None and index == len(history) - 1
                events = content["events"] if returned else json.loads(rows[index]["content"])["events"]
                state = advance(state, events)
                if at == content["revision"]:
                    selected, state = state, deepcopy(state)
        except (ValueError, TypeError, KeyError, IndexError, Rejected) as exc:
            if isinstance(exc, Rejected) and exc.code == "journal_reader_upgrade_required":
                raise Rejected(exc.code,
                    f"This reader cannot interpret all journal events for {wi}; install a compatible core") from exc
            raise Rejected("journal_invalid", "Journal or original input integrity failed") from exc
        if at is not None:
            if at < 1 or at > revision:
                raise Rejected("invalid_input", "Requested revision is outside this history")
            return selected, at, digest(history[at - 1]), history[:at]
        return state, revision, previous, history

    @overload
    def _family(self, conn: sqlite3.Connection, namespace: str, wi: str, state: Json) -> Json: ...

    @overload
    def _family(
        self, conn: sqlite3.Connection, namespace: str, wi: str, state: None
    ) -> None: ...

    @overload
    def _family(
        self, conn: sqlite3.Connection, namespace: str, wi: str, state: Json | None
    ) -> Json | None: ...

    def _family(
        self, conn: sqlite3.Connection, namespace: str, wi: str, state: Json | None
    ) -> Json | None:
        """Family facts read in this transaction: a parent's children's status, or a
        child's execution authority taken from its parent (`children.follow`).

        Each child's `ChildCreated` names its parent; only a WorkItem that is not a
        child itself is searched. Neither side is stored in the other's journal.
        """
        if state is None:
            return state
        if state.get("parent") is not None:
            # A child's execution authority is its parent's current control.
            if self._storage is not None:
                self._storage.load_journal(conn, state["parent"])
            parent, _, _, _ = self._replay(conn, namespace, state["parent"])
            return children.follow(state, parent)
        if self._storage is not None:
            found = self._storage.children(conn, wi)
        else:
            found = [
                row["wi"]
                for row in conn.execute(
                    "SELECT wi,content FROM journal WHERE namespace=? AND revision=1 "
                    "AND content LIKE '%\"ChildCreated\"%'",
                    (namespace,),
                ).fetchall()
                if any(
                    e["kind"] == "ChildCreated" and e["payload"]["parent"] == wi
                    for e in json.loads(row["content"])["events"]
                )
            ]
        family = []
        for child in sorted(set(found)):
            child_state, _, _, _ = self._replay(conn, namespace, child)
            if child_state is None or child_state.get("parent") != wi:
                raise Rejected("journal_invalid", "A child claim differs from its journal")
            shown = public_state(child_state)
            family.append({
                "id": child,
                "status": child_state["status"],
                "business_progress": shown["business_progress"],
                # Display facts for the parent's page; never a lifecycle input.
                "summary": shown.get("summary") or shown.get("title"),
                "pull_requests": [
                    {"repository": pr["repository"], "number": pr["number"],
                     "status": pr["status"]}
                    for pr in shown.get("pull_requests") or []
                ],
            })
        return {**state, "children": family}

    @staticmethod
    def _error(error: Rejected, request_id: str | None = None) -> Json:
        recovery = {
            "effect_retry_backoff": "resume_after_effect_not_before",
            "effect_retry_exhausted": "repair_then_retry_effect",
            "storage_unavailable": "repair_storage_and_retry_original_request",
            "evidence_write_unavailable": "repair_private_custody_and_retry_original_request",
            "ledger_busy": "retry_original_request",
            "ledger_conflict": "retry_original_request",
            "ledger_unavailable": "retry_original_request",
            "ledger_commit_unknown": "inspect_original_request",
            "ledger_publication_denied": "repair_production_publication",
            "ledger_publication_rejected": "repair_production_publication",
            "ledger_limit_exceeded": "repair_production_storage",
            "evidence_unavailable": "retrieve_original_from_custodian",
            "disk_quota_exceeded": "remove_workspace_garbage_or_request_quota_raise",
        }.get(error.code)
        return {
            "schema": "harness.response/v1",
            "status": "unknown" if error.code == "ledger_commit_unknown" else "rejected",
            "request_id": request_id,
            "operation_id": None,
            "work_item": None,
            "result": None,
            "capabilities": [
                {
                    "name": "conversation",
                    "state": "available",
                    "reason": "independent",
                    "source_revision": None,
                }
            ],
            "effects": [],
            "next_action": None if recovery is None else {"intent": recovery, "request_id": request_id},
            "error": {
                "code": error.code,
                "reason": error.reason,
                "retryable": error.code in {"storage_unavailable", "ledger_unavailable", "ledger_conflict", "ledger_busy", "evidence_write_unavailable", "effect_retry_backoff", "human_input_unavailable"},
            },
        }

    @staticmethod
    def _response(
        wi: str,
        state: Json | None,
        revision: int,
        context: HostContext,
        *,
        request_id: str | None = None,
        result: Json | None = None,
        operation: str | None = None,
    ) -> Json:
        current = observation(state, context, revision)
        return {
            "schema": "harness.response/v1",
            "status": "read_only" if result is None else "local_committed",
            "request_id": request_id,
            "operation_id": operation,
            "work_item": {
                "id": wi,
                "local_revision": revision,
                "generation": state["generation"] if state else None,
                "business_progress": current["state"]["business_progress"] if state else "unknown",
            },
            "result": {
                "original_request_result": result,
                "current_observation": current["state"],
            },
            "capabilities": current["capabilities"],
            "effects": current["effects"],
            "next_action": current["next_action"],
            "error": None,
        }

    def execute(self, command: Command, host_context: object) -> Json:
        context: HostContext | None = None
        wi: str | None = None
        normalized: Json | None = None
        committed: Json | None = None
        try:
            normalized = command.normalized()
            context = self._host(host_context)
            source = context.source
            semantics = {
                "command": normalized,
                "namespace": context.namespace,
                "principal": context.principal(),
                "generation": context.generation,
                "source": None
                if source is None
                else {
                    "reference": source.reference,
                    "sha256": source.sha256,
                    "kind": source.kind,
                    "intent": source.approved_intent,
                    "approval": source.approved_input_digest,
                },
            }
            if context.artifacts:
                semantics["artifacts"] = [a.reference() for a in context.artifacts]
            if context.verification_actor is not None:
                semantics["verification_actor"] = context.verification_actor
            if command.intent in {"allocate_workspaces", "replace_workspace"}:
                from dataclasses import asdict

                semantics["repositories"] = [asdict(r) for r in context.repositories]
            if context.git_source is not None:
                semantics["git_source"] = context.git_source
            # Session computation, profile resolution and GitHub state are frozen
            # in the first committed decision; a retry of the same request returns
            # it even after the native record grows.
            if command.intent == "submit_pull_request":
                semantics["merge_repositories"] = sorted(context.merge_repositories)
            if command.intent == "declare_source_publication":
                from dataclasses import asdict

                semantics["publication_targets"] = [asdict(t) for t in context.publication_targets]
                semantics["source_consumers"] = [asdict(c) for c in context.source_consumers]
            if command.intent in {"record_notice", "flush_notices"}:
                from dataclasses import asdict

                semantics["notification_routes"] = [
                    asdict(r)
                    for r in context.notification_routes
                    if r.route_id == normalized["input"].get("route_id")
                ]
            semantic_digest = digest(semantics)
            wi = normalized["work_item_id"]
            if command.intent in CREATION:
                if wi is not None:
                    raise Rejected(
                        "invalid_input",
                        "Creation identity is assigned by the kernel: omit work_item_id from "
                        + command.intent
                        + (" and name the parent WI in input.parent" if command.intent == "create_child" else "")
                        + "; the result returns the new WI ID",
                    )
                wi = "wi_" + digest([context.namespace, command.request_id])[:32]
            elif wi is None:
                raise Rejected("invalid_input", "Work item is required")
            if not context.can_read(wi):
                raise Rejected("authority_denied", "Caller cannot access this work item")
            if command.intent in migration.INTENTS | legacy.INTENTS | {existing_execution.INTENT}:
                raise Rejected(
                    "track_retired",
                    "The old track is retired: migration, legacy import and original-execution "
                    "continuation are rejected; recorded history remains readable",
                )
            operation = operation_id(context.namespace, command.request_id)
            # A child is created inside its parent's transaction: the parent's
            # journal is the decision's dependency.
            scope = wi
            if command.intent == "create_child":
                scope = children.parent_id(normalized["input"])
                if not context.can_read(scope):
                    raise Rejected("authority_denied", "Caller cannot access the parent work item")
            if self._storage is not None and command.intent in CREATION:
                with self._connection(write=False, work_item_id=scope) as selection:
                    wi = self._storage.assign_identity(selection, command.request_id, self._clock())
                if command.intent != "create_child":
                    scope = wi
            # Read before the write transaction: the target WI is a delivery
            # address whose holder is read again at delivery.
            actors = collaboration.untargeted_agents(command.intent, normalized["input"])
            if actors:
                context = replace(context, recipient_work=self._recipient_work(context, wi, actors))
            if command.intent == "confirm_pull_request_merge" and (
                context.source is None or context.source.kind != "native_human"
            ):
                context = replace(context, merge_authority=self._merge_authority(context))
            with self._connection(write=True, work_item_id=scope) as conn:
                if self._storage is not None:
                    if command.intent in CREATION:
                        if wi != self._storage.assign_identity(conn, command.request_id, self._clock()):
                            raise Rejected("ledger_conflict", "Creation identity changed; retry the same request")
                        if scope != wi:
                            # A retried create_child reads the child it already created,
                            # so the original result returns without a new decision.
                            self._storage.load_journal(conn, wi)
                    if not context.can_read(wi):
                        raise Rejected("authority_denied", "Caller cannot access this work item")
                    context = self._storage.context(conn, wi, context)
                    self._storage.check_request(conn, wi, command.request_id)
                context = replace(context, fact_store=location.binding(conn))
                original = conn.execute(
                    "SELECT wi,content,semantic_digest FROM journal "
                    "WHERE namespace=? AND request_id=?",
                    (context.namespace, command.request_id),
                ).fetchone()
                if original is not None:
                    if original["wi"] != wi:
                        raise Rejected("request_conflict", "Request belongs to another work item")
                    state, revision, _, history = self._replay(conn, context.namespace, wi)
                    prior = next(
                        item for item in history if item["request_id"] == command.request_id
                    )
                    # Generation is current Host authority, not user intent. An exact
                    # retry returns the immutable original result and a fresh view;
                    # it never reacquires an earlier execution grant.
                    if semantic_identity(
                        {k: v for k, v in prior["semantics"].items() if k != "generation"}
                    ) != semantic_identity(
                        {k: v for k, v in semantics.items() if k != "generation"}
                    ):
                        raise Rejected(
                            "request_conflict",
                            "Request identity already has different semantics",
                        )
                    # The original committed result is already authoritative.
                    # A projection repair/publish cannot veto its readback.
                    return self._response(
                        wi,
                        self._family(conn, context.namespace, wi, state),
                        revision,
                        context,
                        request_id=command.request_id,
                        result=prior["result"],
                        operation=operation,
                    )
                state, revision, previous, history = self._replay(conn, context.namespace, wi)
                state = self._family(conn, context.namespace, wi, state)
                context = replace(context, observed_revision=revision)
                if command.intent == self_switch.INTENT:
                    # The target is read in this transaction; its journal is a dependency.
                    target = self_switch.target_id(normalized["input"])
                    found = None
                    if target != wi:
                        if self._storage is not None:
                            self._storage.load_journal(conn, target)
                        found, _, _, _ = self._replay(conn, context.namespace, target)
                    context = replace(context, switch_target={"id": target, "state": found})
                if command.intent == "create_child" and scope != wi:
                    parent, _, _, _ = self._replay(conn, context.namespace, scope)
                    context = replace(context, related_parent={
                        "id": scope,
                        "state": self._family(conn, context.namespace, scope, parent),
                    })
                if context.merge_authority and state is not None:
                    context = replace(context, merge_authority=self._current_merge_authority(
                        conn, context, wi, state))
                admitted_at = self._clock()
                decision = decide(
                    state,
                    command.intent,
                    normalized["input"],
                    context,
                    operation,
                    wi,
                    admitted_at,
                )
                if command.intent == "submit_pull_request":
                    self._unique_pull_request(conn, context, wi, normalized["input"])
                if command.intent == "grant_merge_authority":
                    self._unused_merge_approval(conn, context, decision)
                if command.intent == "record_usage" and self._storage is not None:
                    self._storage.check_usage(conn, context, normalized["input"])
                elif command.intent == "record_usage":
                    # Serialize attribution against every WI in this fact-store namespace.
                    # Replayed journal facts are the authority; there is no usage cache.
                    for other in conn.execute(
                        "SELECT wi FROM journal_heads WHERE namespace=? AND wi<>?",
                        (context.namespace, wi),
                    ).fetchall():
                        prior_state, _, _, _ = self._replay(conn, context.namespace, other["wi"])
                        if prior_state is not None:
                            measurements.reject_duplicate_usage(
                                prior_state, context, normalized["input"]
                            )
                sequence = sum(len(entry["events"]) for entry in history)
                events = [
                    {**item, "sequence": sequence + index + 1}
                    for index, item in enumerate(decision.events)
                ]
                new_state = reduce(state, decision.events)
                revision += 1
                result = {
                    **decision.result,
                    "work_item_id": wi,
                    "revision": revision,
                    "operation_id": operation,
                }
                originals: list[str] = []
                # Preserve the normalized business command and the exact authenticated source.
                for content in (
                    [encoded(normalized)]
                    + ([] if source is None else [source.content])
                    + [a.content for a in context.artifacts]
                ):
                    content_digest = hashlib.sha256(content).hexdigest()
                    conn.execute(
                        "INSERT OR IGNORE INTO originals VALUES (?,?,?)",
                        (context.namespace, content_digest, content),
                    )
                    stored = conn.execute(
                        "SELECT content FROM originals WHERE namespace=? AND digest=?",
                        (context.namespace, content_digest),
                    ).fetchone()[0]
                    if stored != content:
                        raise Rejected(
                            "journal_invalid",
                            "Stored input differs from its content address",
                        )
                    originals.append(content_digest)
                self._fault("after_inputs")
                entry = {
                    "namespace": context.namespace,
                    "wi": wi,
                    "revision": revision,
                    "request_id": command.request_id,
                    "semantic_digest": semantic_digest,
                    "semantics": semantics,
                    "originals": originals,
                    "events": events,
                    "result": result,
                    "previous": previous,
                    "at": admitted_at,
                }
                entry_digest = digest(entry)
                conn.execute(
                    "INSERT INTO journal VALUES (?,?,?,?,?,?,?)",
                    (
                        context.namespace,
                        wi,
                        revision,
                        command.request_id,
                        semantic_digest,
                        encoded(entry).decode(),
                        entry_digest,
                    ),
                )
                self._fault("after_journal")
                conn.execute(
                    "INSERT INTO journal_heads VALUES (?,?,?,?) ON CONFLICT(namespace,wi) "
                    "DO UPDATE SET revision=excluded.revision,digest=excluded.digest",
                    (context.namespace, wi, revision, entry_digest),
                )
                self._fault("before_commit")
                conn.commit()
                committed = self._response(
                    wi,
                    new_state,
                    revision,
                    context,
                    request_id=command.request_id,
                    result=result,
                    operation=operation,
                )
                # A process crash here is recovered through the immutable original request.
                self._fault("after_commit")
            # The connection (and any ledger publication) is closed: the revision is
            # durable. The page is derived output and can never fail or undo it.
            page = self._publish_page(committed, host_context, admitted_at)
            if page is not None:
                committed["project_page"] = page
            self._publish_parent_page(committed, host_context, admitted_at)
            return committed
        except Rejected as exc:
            response = self._error(exc, command.request_id)
            # An input/authority/storage rejection does not hide separately provable read facts.
            if context is not None and wi is not None and context.can_read(wi):
                current = self.inspect(Query(wi), host_context)
                if current["status"] == "read_only":
                    for key in ("work_item", "capabilities", "effects", "next_action"):
                        if key == "next_action" and response[key] is not None:
                            continue
                        response[key] = current[key]
                    response["result"] = current["result"]
            return response
        except (sqlite3.Error, OSError) as exc:
            if committed is not None:
                return committed
            # A COMMIT error may be ambiguous. Read the original identity before
            # reporting failure; never issue another business command here.
            if context is not None and wi is not None:
                try:
                    with self._connection(write=False, work_item_id=wi) as conn:
                        state, revision, _, history = self._replay(conn, context.namespace, wi)
                        recovered = next(
                            (row for row in history if row["request_id"] == command.request_id),
                            None,
                        )
                        if recovered is not None and recovered["semantics"] == semantics:
                            return self._response(
                                wi,
                                state,
                                revision,
                                context,
                                request_id=command.request_id,
                                result=recovered["result"],
                                operation=recovered["result"]["operation_id"],
                            )
                except (sqlite3.Error, OSError, Rejected):
                    pass
            failure = storage_error(exc)
            code = failure.code
            response = self._error(failure, command.request_id)
            if context is not None and wi is not None and context.can_read(wi):
                current = self.inspect(Query(wi), host_context)
                if current["status"] == "read_only":
                    response.update(
                        work_item=current["work_item"],
                        result=current["result"],
                        capabilities=current["capabilities"],
                        effects=current["effects"],
                        next_action=response["next_action"] or current["next_action"],
                    )
                    # A readable grant does not imply storage is currently writable.
                    response["capabilities"].append(
                        {
                            "name": "lifecycle_write",
                            "state": "unavailable",
                            "reason": code,
                            "source_revision": current["work_item"]["local_revision"],
                        }
                    )
            return response

    def _publish_page(self, committed: Json, host_context: object, at: str) -> Json | None:
        if self._pages is None:
            return None
        try:
            from . import project_page

            wi = committed["work_item"]["id"]
            root = self._pages.absolute()
            with project_page.lock(root):
                # A child's later change, even the first child's creation, can redraw
                # this page at the same revision: then the family taken at commit is stale.
                drawn = project_page.shown(root / (identity(wi) + ".html"), wi)
                if committed["result"]["current_observation"].get("children") or (
                    drawn is not None and drawn.get("children")
                ):
                    committed = self.inspect(Query(wi), host_context)
                return project_page.write(root, committed, as_of=at)
        except Exception:  # noqa: BLE001 - derived output never vetoes a commit.
            return None

    def _publish_parent_page(self, committed: Json, host_context: object, at: str) -> None:
        """A child's change shows on its parent's page, whose own revision did not move."""
        if self._pages is None:
            return
        try:
            parent = committed["result"]["current_observation"].get("parent")
            if parent is None:
                return
            from . import project_page

            root = self._pages.absolute()
            # The parent's revision does not move with its children, so a snapshot
            # read before the lock could replace a newer one: read it under the lock.
            with project_page.lock(root):
                project_page.write(root, self.inspect(Query(parent), host_context), as_of=at)
        except Exception:  # noqa: BLE001 - derived output never vetoes a commit.
            return

    def _recipient_work(self, context: HostContext, wi: str, actors: frozenset[str]) -> Json:
        """The readable active WIs, other than `wi`, that each recipient Agent holds."""
        found: Json = {actor: [] for actor in sorted(actors)}
        with self._connection(write=False, collection="list") as conn:
            for row in conn.execute(
                "SELECT wi FROM journal_heads WHERE namespace=? AND wi<>? ORDER BY wi",
                (context.namespace, wi),
            ).fetchall():
                other = row["wi"]
                if not context.can_read(other):
                    continue
                try:
                    state, _, _, _ = self._replay(conn, context.namespace, other)
                except Rejected:
                    continue
                if state is None:
                    continue
                actor = collaboration.held_by(state)
                if actor in found:
                    found[actor].append(
                        {"work_item_id": other, "title": public_state(state)["title"]}
                    )
        return found

    def _merge_authority(self, context: HostContext) -> tuple[Json, ...]:
        """Where the caller Agent's unrevoked standing merge grants are, found before
        the write; `_current_merge_authority` decides with them. An unreadable
        journal grants none."""
        from . import merge_authority

        found: list[Json] = []
        with self._connection(write=False, collection="merge_authority") as conn:
            for row in conn.execute(
                "SELECT wi FROM journal_heads WHERE namespace=? ORDER BY wi", (context.namespace,)
            ).fetchall():
                try:
                    state, _, _, _ = self._replay(conn, context.namespace, row["wi"])
                except Rejected:
                    continue
                if state is None:
                    continue
                found += [
                    {"grant_id": grant["grant_id"], "work_item_id": row["wi"],
                     "source": grant["source"]}
                    for grant in merge_authority.standing(state, context.actor)
                ]
        return tuple(found)

    def _current_merge_authority(
        self, conn: sqlite3.Connection, context: HostContext, wi: str, state: Json
    ) -> tuple[Json, ...]:
        """The found grants as they stand in this write transaction.

        Each carrier journal is read again here and becomes a publication
        dependency, so a revocation committed before this decision publishes
        turns it into a conflict; the retry reads the revocation.
        """
        from . import merge_authority

        found: list[Json] = []
        for carrier in sorted({grant["work_item_id"] for grant in context.merge_authority}):
            current: Json | None = state
            if carrier != wi:
                if self._storage is not None:
                    self._storage.load_journal(conn, carrier)
                try:
                    current = self._replay(conn, context.namespace, carrier)[0]
                except Rejected:
                    continue
            if current is None:
                continue
            found += [
                {"grant_id": grant["grant_id"], "work_item_id": carrier, "source": grant["source"]}
                for grant in merge_authority.standing(current, context.actor)
            ]
        return tuple(found)

    def _unused_merge_approval(
        self, conn: sqlite3.Connection, context: HostContext, decision: Decision
    ) -> None:
        """One human approval registers one merge authority across the fact store,
        whether that authority is still active or revoked."""
        source = decision.events[0]["payload"]["source"]
        if self._storage is not None:
            self._storage.check_merge_authority_source(conn, source)
            return
        for other in conn.execute(
            "SELECT wi FROM journal_heads WHERE namespace=?", (context.namespace,)
        ).fetchall():
            prior, _, _, _ = self._replay(conn, context.namespace, other["wi"])
            if prior is not None and any(
                grant["source"] == source for grant in prior.get("merge_authority", {}).values()
            ):
                raise Rejected(
                    "domain_conflict",
                    "This human approval already registered a merge authority; a new grant "
                    "needs the human's new approval",
                )

    def _unique_pull_request(
        self, conn: sqlite3.Connection, context: HostContext, wi: str, data: Json
    ) -> None:
        """A repository and PR number belong to one WI across the fact store."""
        name = pull_requests.input_key(data)
        if name is None:
            return  # The decision rejects malformed input.
        if self._storage is not None:
            self._storage.check_pull_request(conn, wi, name)
            return
        for other in conn.execute(
            "SELECT wi FROM journal_heads WHERE namespace=? AND wi<>?", (context.namespace, wi)
        ).fetchall():
            prior, _, _, _ = self._replay(conn, context.namespace, other["wi"])
            held = pull_requests.records(prior).get(name) if prior is not None else None
            if held is not None and not held.get("withdrawn"):
                raise Rejected(
                    "domain_conflict",
                    f"This pull request belongs to {other['wi']}; withdraw it there first",
                )

    def standing_access(self, namespace: str, workspaces: Json) -> Json:
        """One consistent read of every unrevoked standing grant, for every Agent.

        A standing grant serves its Agent, so every WI journal is read: the WI
        it was requested on is only its audit carrier. `workspaces` is the
        installed layout that family-rule grants must still match.
        """
        grants: list[Json] = []
        unavailable: list[Json] = []
        with self._connection(write=False, collection="standing_access") as conn:
            if self._storage is not None:
                unavailable = [
                    {"wi": row["wi"], "code": row["code"]}
                    for row in conn.execute("SELECT wi, code FROM collection_errors ORDER BY wi")
                ]
            for head in conn.execute(
                "SELECT wi FROM journal_heads WHERE namespace=? ORDER BY wi", (namespace,)
            ).fetchall():
                try:
                    state = self._replay(conn, namespace, head["wi"])[0]
                    if state is None:
                        raise Rejected("journal_invalid", "Collection entry has no original facts")
                except Rejected as exc:
                    unavailable.append({"wi": head["wi"], "code": exc.code})
                    continue
                grants += standing_rows(head["wi"], state, None, workspaces)
        return {
            "schema": "harness.standing_access/v1",
            "grants": grants,
            "unavailable_items": unavailable,
            "coverage": "partial" if unavailable else "complete",
            "read_only": True,
        }

    def registered_workspaces(self, namespace: str) -> Json:
        """One consistent read of every workspace path any WI journal registers.

        The disk quota counts these through their WI; Agent copy discovery
        must not count them again. Retired paths stay listed: a retired path
        still on disk is its WI's to remove.
        """
        from . import workspaces

        paths: set[str] = set()
        unavailable = 0
        with self._connection(write=False, collection="registered_workspaces") as conn:
            if self._storage is not None:
                unavailable = len(conn.execute("SELECT wi FROM collection_errors").fetchall())
            for head in conn.execute(
                "SELECT wi FROM journal_heads WHERE namespace=? ORDER BY wi", (namespace,)
            ).fetchall():
                try:
                    state = self._replay(conn, namespace, head["wi"])[0]
                    if state is None:
                        raise Rejected("journal_invalid", "Collection entry has no original facts")
                except Rejected:
                    unavailable += 1
                    continue
                paths.update(row["path"] for row in workspaces.public(state))
        return {"paths": sorted(paths), "coverage": "partial" if unavailable else "complete"}

    def _listing(self, query: Query, context: HostContext) -> Json:
        if (
            query.work_item_id is not None
            or query.revision is not None
            or query.artifact_ref is not None
            or query.compare_plan_text is not None
            or any(
                v is not None and not isinstance(v, str)
                for v in (query.status, query.owner, query.text)
            )
        ):
            raise Rejected("invalid_input", "Invalid collection query")
        items: list[Json] = []
        # The same read yields the caller Agent's standing grants for Relay, and
        # the workspaces of every WI (children included) this session holds.
        standing: list[Json] = []
        held_workspaces: list[Json] = []
        clock = self._clock() if query.view == "scan" else None
        scan_clock = watch.instant(clock) if clock else None
        errors: list[Json] = []
        with self._connection(write=False, collection=query.view) as conn:
            if self._storage is not None:
                errors = [dict(row) for row in conn.execute("SELECT * FROM collection_errors ORDER BY wi")
                          if context.can_read(row["wi"])]
            identifiers = conn.execute(
                "SELECT wi FROM journal_heads WHERE namespace=? ORDER BY wi",
                (context.namespace,),
            ).fetchall()
            # One read transaction: each parent is replayed once for all its children.
            parents: dict[str, Json | None | Rejected] = {}

            def parent_state(other: str) -> Json | None:
                if other not in parents:
                    try:
                        parents[other] = self._replay(conn, context.namespace, other)[0]
                    except Rejected as exc:
                        parents[other] = exc
                found = parents[other]
                if isinstance(found, Rejected):
                    raise found
                return found

            for row in identifiers:
                wi = row["wi"]
                if not context.can_read(wi):
                    continue
                try:
                    state, revision, _, history = self._replay(conn, context.namespace, wi)
                    if state is None:
                        raise Rejected("journal_invalid", "Collection entry has no original facts")
                except Rejected as exc:
                    errors.append({"wi": wi, "code": exc.code, "reason": exc.reason})
                    continue
                state = children.followed(state, parent_state)
                if query.view == "bindings":
                    standing += standing_rows(wi, state, context.actor, context.agent_workspaces)
                if query.view == "bindings" and not (
                    state["holder"] == context.principal()
                    and state["status"] == "active"
                    or state["status"] == "draft"
                    and history[0]["semantics"]["principal"] == context.principal()
                ):
                    continue
                if query.view == "bindings" and state["holder"] == context.principal():
                    held_workspaces += session_write(wi, state)
                view = public_state(state)
                if (
                    query.status is not None
                    and query.status not in {state["status"], view["business_progress"]}
                    or query.owner is not None
                    and query.owner != state["definition"]["owner"]
                    or query.text is not None
                    and query.text.casefold()
                    not in " ".join(
                        filter(None, (view["title"], view.get("summary"),
                                      state["definition"]["what"], state["definition"]["why"]))
                    ).casefold()
                ):
                    continue
                scan: Json = {}
                if query.view == "scan":
                    assert clock is not None
                    observed = watch.observe(
                        state,
                        history,
                        revision,
                        Query(
                            wi,
                            view="watch",
                            limit=1,
                            age_threshold_seconds=query.age_threshold_seconds,
                        ),
                        clock,
                        context.runtime_observations,
                    )
                    scan = {
                        "watch": {
                            key: observed[key]
                            for key in (
                                "source_revision",
                                "as_of",
                                "age_threshold_seconds",
                                "summary",
                                "archive_scan",
                            )
                        }
                    }
                items.append(
                    {
                        **scan,
                        "id": wi,
                        "local_revision": revision,
                        "generation": state["generation"],
                        "business_progress": view["business_progress"],
                        "title": view["title"],
                        "owner": state["definition"]["owner"],
                        "profile": state["profile"],
                        "awaiting_response": collaboration.awaiting(
                            state, context.actor, context.principal()
                        ),
                        "awaiting_settlement": collaboration.awaiting_settlement(
                            state, context.actor
                        ),
                        "source": "journal",
                        # A session holding a parent holds its children through it.
                        **({"parent": state.get("parent")} if query.view == "bindings" else {}),
                    }
                )
        # Main-only contracts owed to the caller's Agent come first, then answers
        # it still has to settle; order is otherwise kept.
        items.sort(
            key=lambda item: (
                not any(owed["main_only"] for owed in item["awaiting_response"]),
                not item["awaiting_settlement"],
            )
        )
        return {
            "schema": "harness.response/v1",
            "status": "read_only",
            "request_id": None,
            "operation_id": None,
            "work_item": None,
            "result": {
                "items": items[query.after : query.after + query.limit],
                "coverage": "partial" if errors else "complete",
                "unavailable_items": errors,
                "next_offset": query.after + query.limit
                if len(items) > query.after + query.limit
                else None,
                "source": "journal",
                "consistency": "single_read_transaction",
                **(
                    {"standing_access": standing, "workspace_access": held_workspaces}
                    if query.view == "bindings"
                    else {}
                ),
                **(
                    {
                        "view": "scan",
                        "schema": "harness.scan/v1",
                        "total_count": len(items),
                        "as_of": scan_clock.isoformat() if scan_clock else None,
                        "lifecycle_mutation": False,
                    }
                    if query.view == "scan"
                    else {}
                ),
            },
            "capabilities": [
                {
                    "name": "local_read",
                    "state": "available",
                    "reason": "authenticated_journal_read",
                    "source_revision": None,
                }
            ],
            "effects": [],
            "next_action": None,
            "error": None,
        }

    def inspect(self, query: Query, read_context: object) -> Json:
        try:
            context = self._host(read_context)
            if (
                type(query.after) is not int
                or query.after < 0
                or type(query.limit) is not int
                or not 1 <= query.limit <= 500
                or query.revision is not None
                and type(query.revision) is not int
            ):
                raise Rejected("invalid_input", "Invalid bounded query")
            if (query.request_id is not None) != (query.view == "request"):
                raise Rejected(
                    "invalid_input",
                    "Request readback requires an exact request ID: view request goes with "
                    "request_id, and request_id only with view request",
                )
            if query.request_id is not None:
                identity(query.request_id)
                if query.revision is not None:
                    raise Rejected("invalid_input", "Request recovery reads the current ledger")
            if query.age_threshold_seconds is not None and (
                query.view not in {"watch", "scan"}
                or type(query.age_threshold_seconds) is not int
                or query.age_threshold_seconds <= 0
            ):
                raise Rejected(
                    "invalid_input",
                    "Age threshold requires a positive watch or scan query value",
                )
            if query.artifact_ref is not None and query.view not in {
                "artifact",
                "checks",
            }:
                raise Rejected(
                    "invalid_input",
                    "Artifact reference requires artifact or checks view",
                )
            if query.view in {"list", "bindings", "scan"}:
                return self._listing(query, context)
            if query.work_item_id is None:
                raise Rejected("invalid_input", "An exact work item is required")
            identity(query.work_item_id)
            if not context.can_read(query.work_item_id):
                raise Rejected("authority_denied", "Caller cannot access this work item")
            if (
                query.view
                not in {
                    "state",
                    "history",
                    "request",
                    "plan",
                    "artifact",
                    "checks",
                    "tasks",
                    "collaborations",
                    "candidates",
                    "records",
                    "effects",
                    "measurements",
                    "watch",
                    "notices",
                }
                or query.owner is not None
                or query.text is not None
                or (query.status is not None and query.view != "notices")
            ):
                raise Rejected(
                    "invalid_input",
                    "Invalid query view or filter; views: state, history, request, plan, "
                    "artifact, checks, tasks, collaborations, candidates, records, effects, "
                    "measurements, watch, notices (pull requests are in state)",
                )
            with self._connection(write=False, work_item_id=query.work_item_id) as conn:
                if self._storage is not None:
                    context = self._storage.context(conn, query.work_item_id, context)
                context = replace(context, fact_store=location.binding(conn))
                state, revision, _, history = self._replay(
                    conn, context.namespace, query.work_item_id, query.revision
                )
                if state is None:
                    raise Rejected("domain_conflict", "Work item does not exist")
                if query.revision is None:
                    state = self._family(conn, context.namespace, query.work_item_id, state)
                response = self._response(query.work_item_id, state, revision, context)
                if query.view == "request":
                    prior = next(
                        (entry for entry in history if entry["request_id"] == query.request_id),
                        None,
                    )
                    response["request_id"] = query.request_id
                    response["result"]["original_request_result"] = (
                        prior["result"] if prior else None
                    )
                    response["result"]["request_observation"] = {
                        "state": "committed" if prior else "absent",
                        "source": "journal",
                        "source_revision": revision,
                    }
                if query.view in {
                    "tasks",
                    "collaborations",
                    "candidates",
                    "records",
                    "effects",
                    "measurements",
                }:
                    view = response["result"]["current_observation"]
                    response["result"]["items"] = (
                        view["workflow"]
                        if query.view == "tasks"
                        else response["effects"]
                        if query.view == "effects"
                        else view[query.view]
                    )
                if query.view == "measurements":
                    response["result"]["outcome_record_times"] = measurements.outcome_record_times(history)
                if query.view == "notices":
                    response["result"]["items"] = notifications.query(state, query, revision)
                if query.view == "watch":
                    response["result"]["items"] = watch.observe(
                        state,
                        history,
                        revision,
                        query,
                        self._clock(),
                        context.runtime_observations,
                    )
                    response["result"]["current_observation"] = {
                        key: response["result"]["current_observation"][key]
                        for key in ("source", "generation", "business_progress")
                    }
                    response["result"]["effects_scope"] = "watch_page"
                    page_effects = {
                        item["id"]
                        for item in response["result"]["items"]["items"]
                        if item["kind"] == "effect"
                    }
                    response["effects"] = [
                        {
                            key: effect[key]
                            for key in (
                                "effect_id",
                                "effect_kind",
                                "status",
                                "required",
                            )
                        }
                        for effect in response["effects"]
                        if effect["effect_id"] in page_effects
                    ]
                if query.view in {"artifact", "checks"}:
                    from .workflow import artifact_ref

                    ref = query.artifact_ref
                    artifact_ref(ref)  # type: ignore[arg-type]
                    assert ref is not None
                    references = [
                        a for entry in history for a in entry["semantics"].get("artifacts", [])
                    ]
                    if (
                        ref not in references
                        or ref["access"] == "private"
                        and context.actor != state["definition"]["owner"]
                        and not context.effect_kinds
                    ):
                        raise Rejected(
                            "authority_denied",
                            "Artifact is not readable in this WI scope",
                        )
                    row = conn.execute(
                        "SELECT content FROM originals WHERE namespace=? AND digest=?",
                        (context.namespace, ref["sha256"]),
                    ).fetchone()
                    if self._storage is not None:
                        content = self._storage.original(conn, ref["sha256"])
                    elif row is None or hashlib.sha256(row[0]).hexdigest() != ref["sha256"]:
                        raise Rejected("journal_invalid", "Original artifact is unavailable")
                    else:
                        content = bytes(row[0])
                    if query.view == "artifact":
                        response["result"]["artifact"] = {
                            "reference": ref,
                            "base64": base64.b64encode(content).decode("ascii"),
                        }
                        if ref["access"] == "private" and ref["object"] == "native/usage/range":
                            from .measurements import saved_usage_sources

                            def read_original(sha: str) -> bytes:
                                if self._storage is not None:
                                    return self._storage.original(conn, sha)
                                saved = conn.execute(
                                    "SELECT content FROM originals WHERE namespace=? AND digest=?",
                                    (context.namespace, sha),
                                ).fetchone()
                                if saved is None:
                                    raise Rejected("journal_invalid", "Original source is unavailable")
                                # _replay already verified the original BLOB and its digest.
                                source_bytes: bytes = saved[0]
                                return source_bytes

                            response["result"]["artifact"]["native_usage_sources"] = saved_usage_sources(
                                state, history, ref, read_original
                            )
                    else:
                        from .checks import assess

                        response["result"]["items"] = assess(content, ref, revision, query)
                        response["result"]["current_observation"] = {
                            key: response["result"]["current_observation"][key]
                            for key in ("source", "generation", "business_progress")
                        }
                        response["result"]["effects_scope"] = "checks_read_only"
                        response["effects"] = []
                if query.compare_plan_text is not None:
                    if query.view != "plan" or not isinstance(query.compare_plan_text, str):
                        raise Rejected(
                            "invalid_input",
                            "Plan comparison requires an explicit text query",
                        )
                    response["result"]["plan_matches"] = bool(
                        state["plans"]
                        and state["plans"][-1]["plan_digest"] == digest(query.compare_plan_text)
                    )
                if query.revision is not None:
                    # Historical authority can be observed, never reused as current permission.
                    current, current_revision, _, _ = self._replay(
                        conn, context.namespace, query.work_item_id
                    )
                    response["capabilities"] = observation(current, context, current_revision)[
                        "capabilities"
                    ]
                    response["next_action"] = observation(current, context, current_revision)[
                        "next_action"
                    ]
                    response["result"]["current_revision"] = current_revision
                    response["result"]["observation_is_historical"] = True
                if query.view == "history":
                    batch = [item for item in history if item["revision"] > query.after][
                        : query.limit
                    ]
                    response["result"]["history"] = [
                        {
                            "revision": item["revision"],
                            "request_id": item["request_id"],
                            "at": item["at"],
                            "event_kinds": [e["kind"] for e in item["events"]],
                        }
                        for item in batch
                    ]
                    response["result"]["next_cursor"] = batch[-1]["revision"] if batch else None
                if not self._program_admitted(conn):
                    for capability in response["capabilities"]:
                        if capability["name"] != "local_read":
                            capability.update(
                                state="unavailable", reason="inactive_installed_program"
                            )
                    response["next_action"] = {
                        "intent": "use_active_program",
                        "reason": "program_changed",
                    }
                if self._pages is not None and query.view == "state" and query.revision is None:
                    try:
                        from . import project_page

                        response["project_page"] = project_page.located(
                            self._pages, query.work_item_id
                        )
                    except Exception:  # noqa: BLE001 - a derived view never breaks a query.
                        response["project_page"] = None
                if self._storage is None and not location.admitted(conn, self._database_path()):
                    for capability in response["capabilities"]:
                        if capability["name"] != "local_read":
                            capability.update(state="unavailable", reason="foreign_fact_store")
                    response["next_action"] = {
                        "intent": "inspect",
                        "reason": "copied_fact_store_requires_sealed_transfer",
                    }
                return response
        except Rejected as exc:
            return self._error(exc)
        except (sqlite3.Error, OSError) as exc:
            return self._error(storage_error(exc))

    def resume(self, work_item_id: str, host_context: object) -> Json:
        """Observe the original request and its next decision without minting a grant."""
        response = self.inspect(Query(work_item_id), host_context)
        if (
            response.get("next_action", {})
            and response["next_action"]["intent"] == "resolve_business_affiliation"
        ):
            response["status"] = "human_decision_required"
            response["error"] = {
                "code": response["next_action"]["reason"],
                "reason": "The original Agent must resolve the undecided business affiliation with the human",
                "retryable": False,
            }
        return response
