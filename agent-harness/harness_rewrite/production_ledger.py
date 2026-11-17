"""Production ledger transactions on the existing Git authority.

SQLite exists only in memory during one transaction. A successful commit means
the original production ref contains the journal and its compatibility view.
No checkout is reset, no WI is migrated, and no second durable business store is
created. The registered Git remote supplies authentication, never command input.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from typing import Iterator, Callable, Literal, overload
from urllib.parse import urlsplit
import uuid

from .model import Json, HostContext, Rejected, digest, encoded
from . import production_history
from . import production_segments as segments
from .production_evidence import Evidence
from .git_environment import repository_environment

PREFIX = ".harness/core"
FORMAT = PREFIX + "/format.json"
SCHEMA = "harness.production-ledger/v1"
# This service publishes the authenticated journal; it does not impersonate its
# human decision source or depend on an interactive account's Git configuration.
PUBLISHER_IDENTITY = {
    "GIT_AUTHOR_NAME": "SCL Production Ledger",
    "GIT_AUTHOR_EMAIL": "scl-ledger@invalid.local",
    "GIT_COMMITTER_NAME": "SCL Production Ledger",
    "GIT_COMMITTER_EMAIL": "scl-ledger@invalid.local",
}


# One journal's verified decisions: their journal rows, the original custody
# references in decision order, and every Git object they were read from
# (decision segments and chunks), by path and object ID.
Decisions = tuple[list[tuple[int, str, str, str, str]], list[tuple[str, Json]], dict[str, str]]


class CommitConnection(sqlite3.Connection):
    publish: Callable[[sqlite3.Connection], None] | None = None

    def commit(self) -> None:
        # Local calculation must not become a durable grant before publication.
        if self.publish is not None:
            self.publish(self)
        super().commit()

    def validate_original(self, namespace: str, sha: str) -> None:
        row = self.execute("SELECT content FROM originals WHERE namespace=? AND digest=?",
                           (namespace, sha)).fetchone()
        if row is not None:
            if hashlib.sha256(row[0]).hexdigest() != sha:
                raise ValueError("Original differs")
        elif not self.execute("SELECT 1 FROM original_refs WHERE digest=?", (sha,)).fetchone():
            raise ValueError("Original reference missing")


class ProductionLedger:
    def __init__(
        self, config: Json, namespace: str, *, fault: Callable[[str], None] | None = None
    ) -> None:
        if set(config) != {"repository", "remote", "ref", "identity"}:
            raise ValueError("Production ledger requires an exact registered binding")
        self.repository = Path(config["repository"])
        if not self.repository.is_absolute() or self.repository.resolve() != self.repository:
            raise ValueError("Production repository must be a physical absolute path")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", config["remote"]):
            raise ValueError("Expected a registered remote name")
        if not re.fullmatch(r"refs/heads/[A-Za-z0-9_./-]+", config["ref"]):
            raise ValueError("Expected an exact production branch")
        self.config, self.namespace = dict(config), namespace
        self.fault = fault or (lambda stage: None)
        self.last_revision: str | None = None
        remote = self.git("remote", "get-url", config["remote"]).decode().strip()
        if remote.startswith(("https://", "ssh://")):
            parsed = urlsplit(remote)
            actual = str(parsed.hostname) + parsed.path.removesuffix(".git")
        elif re.fullmatch(r"[^@]+@[^:]+:.+", remote):
            actual = remote.split("@", 1)[1].replace(":", "/", 1).removesuffix(".git")
        else:
            actual = str(Path(remote).resolve())
        if actual != config["identity"] and (
            remote.startswith("ssh://") or re.fullmatch(r"[^@]+@[^:]+:.+", remote)
        ):
            # Resolve the account's registered SSH alias without opening a connection.
            # Remote spelling may differ across hosts; business identity stays canonical.
            host, path = actual.split("/", 1)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host):
                raise ValueError("Invalid registered SSH host")
            try:
                resolved = subprocess.run(
                    ["ssh", "-G", host], capture_output=True, text=True, check=True, timeout=5
                )
                names = [line.split()[1] for line in resolved.stdout.splitlines()
                         if line.startswith("hostname ")]
                if len(names) != 1:
                    raise ValueError("SSH alias has no unique host")
                canonical = "github.com" if names[0] == "ssh.github.com" else names[0]
                actual = canonical + "/" + path
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError("Registered SSH authority cannot be resolved") from exc
        if actual != config["identity"]:
            raise ValueError("Registered production authority differs from this remote")
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
        self.evidence = Evidence(common / ("private-evidence-" + digest(namespace)[:16]))
        self.dependencies: set[str] = set()
        self.prefixes: set[str] = set()
        self.read_cache: dict[str, bytes] = {}
        self.baseline: dict[str, int] = {}
        # Process-local memos of deterministic functions of immutable Git
        # objects; see "Read reuse" in the production storage specification.
        self.trees: dict[str, dict[str, str]] = {}
        self.decisions: dict[str, tuple[str, Decisions]] = {}
        self.snapshot_open = False
        self.snapshot: str | None = None

    @overload
    def git(
        self,
        *args: str,
        data: bytes | None = None,
        env: dict[str, str] | None = None,
        check: Literal[True] = True,
    ) -> bytes: ...

    @overload
    def git(
        self,
        *args: str,
        data: bytes | None = None,
        env: dict[str, str] | None = None,
        check: Literal[False],
    ) -> subprocess.CompletedProcess[bytes]: ...

    def git(
        self,
        *args: str,
        data: bytes | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> bytes | subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repository), *args],
                input=data,
                capture_output=True,
                timeout=60,
                env=repository_environment(env),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Rejected(
                "ledger_unavailable", "Production authority could not be observed"
            ) from exc
        if check and result.returncode:
            # Git stderr may contain credentialized URLs; never expose it.
            raise Rejected("ledger_unavailable", "Production Git operation failed")
        return result.stdout if check else result

    def refresh(self) -> str:
        return self.refresh_ref(self.config["ref"])

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Read transactions opened inside observe one production revision.

        The first read observes the remote ref; later reads reuse that commit,
        so one command is one consistent observation with one remote round
        trip. A write transaction ends the snapshot: it and every later read
        observe the ref again.
        """
        self.snapshot_open, self.snapshot = True, None
        try:
            yield
        finally:
            self.snapshot_open, self.snapshot = False, None

    def observe(self, *, write: bool) -> str:
        if write:
            self.snapshot_open, self.snapshot = False, None
        if self.snapshot is not None:
            return self.snapshot
        revision = self.refresh()
        if self.snapshot_open:
            self.snapshot = revision
        return revision

    def refresh_ref(self, branch: str) -> str:
        ref = "refs/harness-core/read/" + uuid.uuid4().hex
        try:
            fetched = self.git(
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                self.config["remote"],
                branch + ":" + ref,
                check=False,
            )
            # Git also moves the remote-tracking ref; a concurrent fetch on this
            # checkout holds it and fails that update ("cannot lock ref") after
            # this fetch has written the private ref. Only this fetch can have
            # written that fresh ref, so its presence is the observation.
            observed = self.git("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
            if observed.returncode:
                if fetched.returncode:
                    raise Rejected("ledger_unavailable", "Production Git operation failed")
                raise Rejected("ledger_unavailable", "Production ref could not be observed")
            return observed.stdout.decode().strip()
        finally:
            self.git("update-ref", "-d", ref, check=False)

    def remote_head(self) -> str:
        """The production ref's commit as advertised now: one ref, no objects transferred."""
        rows = self.git(
            "ls-remote", "--refs", "--", self.config["remote"], self.config["ref"]
        ).decode().splitlines()
        exact = [row.split()[0] for row in rows
                 if len(row.split()) == 2 and row.split()[1] == self.config["ref"]]
        if len(exact) != 1 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", exact[0]):
            raise Rejected("ledger_unavailable", "Production head could not be observed")
        return exact[0]

    def tree(self, revision: str) -> dict[str, str]:
        # A commit's tree never changes; repeated transactions share one listing.
        if revision not in self.trees:
            self.trees = {revision: self.list_tree(revision)}
        return dict(self.trees[revision])

    def list_tree(self, revision: str) -> dict[str, str]:
        raw = self.git("ls-tree", "-r", "-z", revision, "--", ".harness")
        rows = {}
        for row in raw.split(b"\0"):
            if not row:
                continue
            header, name = row.split(b"\t", 1)
            mode, kind, sha = header.decode().split()
            # Preserve mode in the dependency identity, but validate only records
            # actually consumed. An unrelated symlink cannot poison this ledger.
            rows[name.decode()] = sha if (mode, kind) == ("100644", "blob") else f"{mode}:{kind}:{sha}"
        return rows

    def blobs(self, mapping: dict[str, str]) -> dict[str, bytes]:
        if not mapping:
            return {}
        with self.object_reader(mapping, ordered=True) as read:
            return {path: read(path) for path in mapping}

    @contextmanager
    def object_reader(
        self, mapping: dict[str, str], *, ordered: bool = False
    ) -> Iterator[Callable[[str], bytes]]:
        from .git_objects import object_reader
        self.dependencies.update(mapping)
        with object_reader(self.repository, mapping, ordered=ordered) as read:
            yield read

    @contextmanager
    def connection(self, *, write: bool, work_item_id: str | None = None,
                   reformat: bool = False, collection: str | None = None) -> Iterator[sqlite3.Connection]:
        from .kernel import DDL

        revision = self.observe(write=write)
        self.last_revision = revision
        tree = self.tree(revision)
        # Cross-WI request and usage dependencies are loaded explicitly below.
        # Historical validation is not a precondition for an unrelated WI write.
        scoped_wi = work_item_id
        self.dependencies = {FORMAT, ".harness/platform-epoch.yaml"}
        self.prefixes = set()
        self.read_cache = {}
        metadata = {FORMAT, ".harness/platform-epoch.yaml"}
        scoped_paths = {
            f".harness/work-items/{scoped_wi}.yaml",
            f".harness/admissions/{scoped_wi}.yaml",
            f".harness/events/{scoped_wi}.events.yaml",
        }
        if scoped_wi is not None:
            self.dependencies.update(scoped_paths | {f"{PREFIX}/work-items/{scoped_wi}.json"})
            self.prefixes.add(f"{PREFIX}/journals/{scoped_wi}/")
        core_paths = {
            Path(path).stem: path for path in tree
            if path.startswith(PREFIX + "/work-items/") and path.endswith(".json")
            and (scoped_wi is None or Path(path).stem == scoped_wi)
        }
        selected = {
            p: sha
            for p, sha in tree.items()
            if (scoped_wi is None or p in metadata | scoped_paths)
            and (
                p in metadata
                or p.startswith((
                    ".harness/work-items/", ".harness/admissions/", ".harness/events/",
                ))
            )
        }
        # Native compatibility documents are derived outputs. Even an invalid
        # mode or malformed replacement cannot veto authoritative facts.
        native_projections = {p for wi in core_paths for p in (
            f".harness/work-items/{wi}.yaml", f".harness/admissions/{wi}.yaml",
            f".harness/events/{wi}.events.yaml")}
        selected = {p: sha for p, sha in selected.items() if p not in native_projections}
        self.dependencies.difference_update(native_projections)
        # Collection discovery reads each WI inside its own validation boundary.
        blobs = self.blobs({p: sha for p, sha in selected.items() if collection is None or p in metadata})
        format_record = json.loads(blobs[FORMAT]) if FORMAT in blobs else None
        expected_format = {
            "schema": format_record.get("schema") if isinstance(format_record, dict) else segments.SCHEMA,
            "namespace": self.namespace,
            "authority": self.config["identity"],
            "ref": self.config["ref"],
        }
        if format_record is not None and (format_record != expected_format or
                format_record.get("schema") not in {SCHEMA, segments.SCHEMA}):
            raise Rejected(
                "ledger_identity_changed", "This is not the registered single production ledger"
            )
        conn = sqlite3.connect(":memory:", isolation_level=None, factory=CommitConnection)
        conn.row_factory = sqlite3.Row
        try:
            for statement in DDL:
                conn.execute(statement)
            conn.execute("CREATE TABLE original_refs (digest TEXT PRIMARY KEY, content TEXT NOT NULL)")
            conn.execute("CREATE TABLE production_format (schema TEXT NOT NULL)")
            conn.execute("INSERT INTO production_format VALUES (?)", (expected_format["schema"],))
            conn.execute("CREATE TABLE production_context (content TEXT NOT NULL)")
            conn.execute("CREATE TABLE production_objects (path TEXT PRIMARY KEY, sha TEXT NOT NULL)")
            conn.executemany("INSERT INTO production_objects VALUES (?,?)", tree.items())
            policy = (
                production_history.decode(blobs[".harness/platform-epoch.yaml"])
                if ".harness/platform-epoch.yaml" in blobs
                else None
            )
            conn.execute("INSERT INTO production_context VALUES (?)", (json.dumps(policy),))
            # Journals loaded later in the transaction (a parent's children) join it.
            baseline: dict[str, int] = {}
            self.baseline = baseline
            documents: dict[str, dict[str, bytes]] = {}
            conn.execute("CREATE TABLE collection_errors (wi TEXT PRIMARY KEY, code TEXT, reason TEXT)")
            identities = {
                Path(p).stem
                for p in selected
                if p.startswith(".harness/work-items/") and p.endswith(".yaml")
                and collection != "bindings"
            }
            if core_paths and format_record is None:
                raise Rejected("ledger_invalid", "Native journals require their authority binding")
            identities.update(core_paths)
            # Only a WI without a native journal is seeded from its documents.
            legacy = identities - set(core_paths)
            related_documents = production_history.related_document_index(tree, legacy) if legacy else {}
            for wi in sorted(identities):
                conn.execute("SAVEPOINT collection_item")
                try:
                    if collection is not None:
                        paths = {p: selected[p] for p in (
                            f".harness/work-items/{wi}.yaml", f".harness/admissions/{wi}.yaml",
                            f".harness/events/{wi}.events.yaml") if p in selected}
                        blobs.update(self.blobs(paths))
                    related = {
                        p: blobs[p]
                        for p in sorted((
                            f".harness/work-items/{wi}.yaml",
                            f".harness/admissions/{wi}.yaml",
                            f".harness/events/{wi}.events.yaml",
                        ))
                        if p in blobs
                    }
                    documents[wi] = related
                    if wi in core_paths:
                        path = core_paths[wi]
                        # Do not retain all encoded original containers alongside the
                        # decoded in-memory transaction. Git remains the only authority.
                        try:
                            record = json.loads(self.blobs({path: tree[path]})[path])
                        except (ValueError, UnicodeError) as exc:
                            raise Rejected("ledger_invalid", "Invalid production journal") from exc
                        if (
                            record.get("schema") not in {SCHEMA, segments.SCHEMA}
                            or record.get("namespace") != self.namespace
                            or record.get("work_item_id") != wi
                        ):
                            raise Rejected("ledger_invalid", "Native journal identity differs")
                        # Compatibility files and their recorded digests are outputs.
                        # Neither is allowed to veto or amend the authoritative journal.
                        if record["schema"] == segments.SCHEMA:
                            if type(record.get("revision")) is not int or record["revision"] < 1:
                                raise Rejected("ledger_invalid", "Invalid production head")
                            rows, originals = self.decision_rows(conn, wi, record), {}
                        else:
                            entries, originals = record["entries"], record["originals"]
                            rows = None
                    else:
                        entries, originals = production_history.seed(
                            self.namespace, wi, revision, related, related_documents[wi]
                        )
                        rows = None
                    for sha, value in originals.items():
                        content = base64.b64decode(value, validate=True)
                        if hashlib.sha256(content).hexdigest() != sha:
                            raise Rejected("ledger_invalid", "Original production bytes differ")
                        conn.execute(
                            "INSERT OR IGNORE INTO originals VALUES (?,?,?)",
                            (self.namespace, sha, content),
                        )
                    if rows is None:
                        rows = [self.journal_row(entry) for entry in entries]
                    self.insert_journal(conn, wi, rows)
                    baseline[wi] = len(rows)
                except (Rejected, ValueError, TypeError, KeyError) as exc:
                    conn.execute("ROLLBACK TO collection_item")
                    if collection is None:
                        raise
                    code = exc.code if isinstance(exc, Rejected) else "ledger_invalid"
                    conn.execute("INSERT INTO collection_errors VALUES (?,?,?)",
                                 (wi, code, "This WI's original facts cannot be read"))
                finally:
                    conn.execute("RELEASE collection_item")
            conn.execute("BEGIN")
            if write:

                def publish(connection: sqlite3.Connection) -> None:
                    self.fault("before_publication")
                    updates: dict[str, bytes] = {}
                    for row in connection.execute(
                        "SELECT wi,revision FROM journal_heads WHERE namespace=?", (self.namespace,)
                    ):
                        wi = row["wi"]
                        if row["revision"] == baseline.get(wi, 0) and not reformat:
                            continue
                        from .kernel import Kernel

                        state, _, _, history = Kernel._replay(connection, self.namespace, wi)
                        assert state is not None
                        private = segments.private_digests(history)
                        refs: Json = {}
                        for sha in {s for entry in history for s in entry["originals"]}:
                            old = connection.execute("SELECT content FROM original_refs WHERE digest=?", (sha,)).fetchone()
                            if old is not None:
                                refs[sha] = json.loads(old[0])
                                supplied = connection.execute(
                                    "SELECT content FROM originals WHERE namespace=? AND digest=?",
                                    (self.namespace, sha)).fetchone()
                                if supplied is not None and refs[sha]["access"] == "private":
                                    self.evidence.put(sha, bytes(supplied[0]))
                                continue
                            content = bytes(connection.execute(
                                "SELECT content FROM originals WHERE namespace=? AND digest=?",
                                (self.namespace, sha)).fetchone()[0])
                            if sha in private:
                                self.evidence.put(sha, content)
                                refs[sha] = {"access": "private", "size": len(content)}
                            else:
                                refs[sha] = {"access": "git", "size": len(content),
                                             "object": segments.pack(content, updates)}
                        for entry in history:
                            path = segments.entry_path(wi, entry["revision"])
                            if path not in tree:
                                representation = ({"record": entry} if len(encoded(entry)) < segments.CHUNK // 2
                                                  else {"entry": segments.pack(encoded(entry), updates)})
                                updates[path] = encoded({**representation,
                                    "originals": {sha: refs[sha] for sha in entry["originals"]}})
                            self.claims(entry, updates)
                        # Projections never participate in exact-request recovery.
                        prior = {path: self.original(connection, sha)
                                 for path, sha in state.get("production", {}).get("originals", {}).items()}
                        projection = production_history.project(wi, state, history, prior)
                        updates.update(projection)
                        updates[PREFIX + "/work-items/" + wi + ".json"] = encoded(
                            {
                                "schema": segments.SCHEMA,
                                "namespace": self.namespace,
                                "work_item_id": wi,
                                "revision": row["revision"],
                                "head": digest(history[-1]),
                            }
                        )
                    if not updates:
                        return
                    updates[FORMAT] = encoded(expected_format)
                    updates = {p: b for p, b in updates.items()
                               if tree.get(p) != self.object_id(b)}
                    if updates:
                        self.last_revision = self.publish(revision, updates)

                conn.publish = publish
            yield conn
        finally:
            conn.close()

    def reformat(self, wi: str) -> Json:
        """Operator maintenance: preserve decisions, replace only their encoding."""
        from .kernel import Kernel
        from .model import identity

        identity(wi)
        with self.connection(write=True, work_item_id=wi, reformat=True) as conn:
            native_path = f"{PREFIX}/work-items/{wi}.json"
            if not conn.execute("SELECT 1 FROM production_objects WHERE path=?", (native_path,)).fetchone():
                raise Rejected("operation_scope", "Only an existing native journal can be reformatted")
            _, revision, head, history = Kernel._replay(conn, self.namespace, wi)
            before = self.last_revision
            conn.commit()
        with self.connection(write=False, work_item_id=wi) as conn:
            _, after_revision, after_head, after_history = Kernel._replay(conn, self.namespace, wi)
            if (after_revision, after_head) != (revision, head) or after_history != history:
                # A concurrent business append is not rolled back or called lost.
                if after_history[:len(history)] != history:
                    raise Rejected("ledger_invalid", "Reformatted decisions differ from their originals")
            return {"schema": "harness.storage_conversion/v1", "work_item_id": wi,
                    "status": "verified", "source_revision": before,
                    "observed_revision": self.last_revision, "preserved_decisions": revision,
                    "preserved_head": head, "current_decisions": after_revision,
                    "business_mutation": False, "historical_git_objects_removed": False}

    def finish_reformat(self) -> Json:
        """Retire the v1 fallback only after every native head uses segmented facts."""
        with self.connection(write=False, work_item_id="storage-format-check") as conn:
            if conn.execute("SELECT schema FROM production_format").fetchone()[0] == segments.SCHEMA:
                return {"status": "verified", "storage_schema": segments.SCHEMA,
                        "observed_revision": self.last_revision, "business_mutation": False}
            if list(self.old_entries(conn)):
                raise Rejected("operation_scope", "Some native journals still require conversion")
            before = self.last_revision
            assert before is not None
            self.prefixes.add(PREFIX + "/work-items/")
            self.last_revision = self.publish(before, {FORMAT: encoded({
                "schema": segments.SCHEMA, "namespace": self.namespace,
                "authority": self.config["identity"], "ref": self.config["ref"]})})
            return {"status": "verified", "storage_schema": segments.SCHEMA,
                    "observed_revision": self.last_revision, "business_mutation": False}

    @staticmethod
    def object_id(content: bytes) -> str:
        return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()

    def read_path(self, conn: sqlite3.Connection, path: str) -> bytes:
        self.dependencies.add(path)
        if path in self.read_cache:
            return self.read_cache[path]
        row = conn.execute("SELECT sha FROM production_objects WHERE path=?", (path,)).fetchone()
        if row is None:
            raise Rejected("ledger_invalid", "An immutable production reference is missing")
        self.read_cache.update(self.blobs({path: row[0]}))
        return self.read_cache[path]

    def prefetch_entries(self, conn: sqlite3.Connection, wi: str, revision: int) -> dict[str, str]:
        """Read one journal's decision segments and chunks; return their object IDs."""
        paths = [segments.entry_path(wi, number) for number in range(1, revision + 1)]
        mapping = {}
        for path in paths:
            row = conn.execute("SELECT sha FROM production_objects WHERE path=?", (path,)).fetchone()
            if row is None:
                raise Rejected("journal_invalid", "A decision segment is missing")
            mapping[path] = row[0]
        self.read_cache.update(self.blobs(mapping))
        objects = {}
        try:
            for path in paths:
                wrapper = json.loads(self.read_cache[path])
                for sha in wrapper.get("entry", {}).get("chunks", []):
                    name = segments.object_path(sha)
                    row = conn.execute("SELECT sha FROM production_objects WHERE path=?", (name,)).fetchone()
                    if row is None:
                        raise Rejected("journal_invalid", "A decision object is missing")
                    objects[name] = row[0]
        except (ValueError, TypeError, KeyError) as exc:
            raise Rejected("ledger_invalid", "Invalid decision segment") from exc
        self.read_cache.update(self.blobs(objects))
        return mapping | objects

    @staticmethod
    def claims(entry: Json, updates: dict[str, bytes]) -> None:
        locator = {"wi": entry["wi"], "revision": entry["revision"], "digest": digest(entry)}
        updates[segments.request_path(entry["namespace"], entry["request_id"])] = encoded(locator)
        for event in entry["events"]:
            record = event.get("payload", {}).get("record", {})
            if event["kind"] == "MeasurementRecorded" and record.get("kind") == "usage":
                updates[segments.usage_prefix(record["principal"]) + digest(locator) + ".json"] = encoded(locator)
            if event["kind"] == "ChildCreated":
                updates[segments.child_prefix(event["payload"]["parent"]) + digest(locator) + ".json"] = encoded(locator)
            if event["kind"] in {"PullRequestSubmitted", "PullRequestWithdrawn"}:
                name = event["payload"]["key"]
                updates[segments.pull_request_prefix(name) + digest(locator) + ".json"] = encoded(locator)
            if event["kind"] == "MergeAuthorityGranted":
                prefix = segments.merge_authority_prefix(event["payload"]["source"])
                updates[prefix + digest(locator) + ".json"] = encoded(locator)

    def resolve_claim(self, conn: sqlite3.Connection, path: str) -> Json:
        try:
            locator = json.loads(self.read_path(conn, path))
            entry, _ = segments.load_entry(segments.entry_path(locator["wi"], locator["revision"]),
                                            lambda p: self.read_path(conn, p))
            if digest(entry) != locator["digest"] or entry["namespace"] != self.namespace:
                raise ValueError("Invalid claim")
            return entry
        except (ValueError, TypeError, KeyError) as exc:
            raise Rejected("ledger_invalid", "Claim differs from its original decision") from exc

    def old_entries(self, conn: sqlite3.Connection) -> Iterator[Json]:
        # Temporary backwards reader. Once all v1 native journals are converted,
        # only their small heads are inspected; no unrelated original is loaded.
        if conn.execute("SELECT schema FROM production_format").fetchone()[0] == segments.SCHEMA:
            return
        for row in conn.execute("SELECT path,sha FROM production_objects WHERE path GLOB ?",
                                (PREFIX + "/work-items/*.json",)).fetchall():
            record = json.loads(self.blobs({row["path"]: row["sha"]})[row["path"]])
            if record.get("schema") == segments.SCHEMA:
                self.dependencies.discard(row["path"])
                continue
            if record.get("schema") != SCHEMA or record.get("namespace") != self.namespace:
                raise Rejected("ledger_invalid", "Invalid historical journal binding")
            yield from record["entries"]

    def find_request(self, conn: sqlite3.Connection, request: str) -> Json | None:
        path = segments.request_path(self.namespace, request)
        self.dependencies.add(path)
        if conn.execute("SELECT 1 FROM production_objects WHERE path=?", (path,)).fetchone():
            entry = self.resolve_claim(conn, path)
            if entry["request_id"] != request:
                raise Rejected("ledger_invalid", "Request locator identity differs")
            return entry
        return next((e for e in self.old_entries(conn) if e["request_id"] == request), None)

    def check_request(self, conn: sqlite3.Connection, wi: str, request: str) -> None:
        prior = self.find_request(conn, request)
        if prior is not None and prior["wi"] != wi:
            raise Rejected("request_conflict", "Request belongs to another work item")

    def check_usage(self, conn: sqlite3.Connection, context: HostContext, data: Json) -> None:
        from .measurements import reject_duplicate_usage

        prefix = segments.usage_prefix(context.principal())
        self.prefixes.add(prefix)  # Includes new claims inserted during the transaction.
        entries = [self.resolve_claim(conn, row[0]) for row in conn.execute(
            "SELECT path FROM production_objects WHERE path GLOB ?", (prefix + "*.json",))]
        entries.extend(self.old_entries(conn))
        for entry in entries:
            records = [event["payload"]["record"] for event in entry["events"]
                       if event["kind"] == "MeasurementRecorded"]
            reject_duplicate_usage({"measurements": records}, context, data)

    def check_pull_request(self, conn: sqlite3.Connection, wi: str, name: str) -> None:
        prefix = segments.pull_request_prefix(name)
        self.prefixes.add(prefix)  # A concurrent claim changes the ledger head.
        latest: dict[str, tuple[int, str]] = {}
        for row in conn.execute(
            "SELECT path FROM production_objects WHERE path GLOB ?", (prefix + "*.json",)
        ).fetchall():
            entry = self.resolve_claim(conn, row[0])
            for event in entry["events"]:
                if event["kind"] in {"PullRequestSubmitted", "PullRequestWithdrawn"} and (
                    event["payload"]["key"] == name
                ):
                    mark = (entry["revision"], event["kind"])
                    latest[entry["wi"]] = max(latest.get(entry["wi"], mark), mark)
        for other, (_, kind) in latest.items():
            if other != wi and kind == "PullRequestSubmitted":
                raise Rejected(
                    "domain_conflict",
                    f"This pull request belongs to {other}; withdraw it there first",
                )

    def check_merge_authority_source(self, conn: sqlite3.Connection, source: str) -> None:
        """One human approval registers one merge authority, revoked or not."""
        prefix = segments.merge_authority_prefix(source)
        self.prefixes.add(prefix)  # A concurrent registration changes the decision.
        for row in conn.execute(
            "SELECT path FROM production_objects WHERE path GLOB ?", (prefix + "*.json",)
        ).fetchall():
            entry = self.resolve_claim(conn, row[0])
            if any(event["kind"] == "MergeAuthorityGranted"
                   and event["payload"]["source"] == source for event in entry["events"]):
                raise Rejected(
                    "domain_conflict",
                    "This human approval already registered a merge authority; a new grant "
                    "needs the human's new approval",
                )

    def children(self, conn: sqlite3.Connection, parent: str) -> list[str]:
        """The children created under `parent`, their journals loaded into `conn`."""
        prefix = segments.child_prefix(parent)
        self.prefixes.add(prefix)  # A concurrent child creation changes the decision.
        found = []
        for row in conn.execute(
            "SELECT path FROM production_objects WHERE path GLOB ?", (prefix + "*.json",)
        ).fetchall():
            entry = self.resolve_claim(conn, row[0])
            if not any(
                e["kind"] == "ChildCreated" and e["payload"]["parent"] == parent
                for e in entry["events"]
            ):
                raise Rejected("ledger_invalid", "Child claim differs from its original decision")
            self.load_journal(conn, entry["wi"])
            found.append(str(entry["wi"]))
        return found

    def load_journal(self, conn: sqlite3.Connection, wi: str) -> None:
        """Read one more native journal into this transaction, as a dependency only."""
        if conn.execute(
            "SELECT 1 FROM journal_heads WHERE namespace=? AND wi=?", (self.namespace, wi)
        ).fetchone():
            return
        path = f"{PREFIX}/work-items/{wi}.json"
        self.dependencies.add(path)
        if not conn.execute("SELECT 1 FROM production_objects WHERE path=?", (path,)).fetchone():
            return  # Not created yet.
        try:
            record = json.loads(self.read_path(conn, path))
        except (ValueError, UnicodeError) as exc:
            raise Rejected("ledger_invalid", "Invalid production journal") from exc
        if (
            record.get("schema") != segments.SCHEMA
            or record.get("namespace") != self.namespace
            or record.get("work_item_id") != wi
            or type(record.get("revision")) is not int
            or record["revision"] < 1
        ):
            raise Rejected("ledger_invalid", "Native journal identity differs")
        self.prefixes.add(f"{PREFIX}/journals/{wi}/")
        rows = self.decision_rows(conn, wi, record)
        self.insert_journal(conn, wi, rows)
        self.baseline[wi] = len(rows)

    @staticmethod
    def journal_row(entry: Json) -> tuple[int, str, str, str, str]:
        return (entry["revision"], entry["request_id"], entry["semantic_digest"],
                encoded(entry).decode(), digest(entry))

    def insert_journal(self, conn: sqlite3.Connection, wi: str,
                       rows: list[tuple[int, str, str, str, str]]) -> None:
        conn.executemany("INSERT INTO journal VALUES (?,?,?,?,?,?,?)",
                         [(self.namespace, wi, *row) for row in rows])
        if rows:
            conn.execute("INSERT INTO journal_heads VALUES (?,?,?,?)",
                         (self.namespace, wi, rows[-1][0], rows[-1][4]))

    def decision_rows(self, conn: sqlite3.Connection, wi: str,
                      record: Json) -> list[tuple[int, str, str, str, str]]:
        """A v2 journal's decisions, proven against its head, as journal rows.

        Their custody references join this transaction. The result is a
        function of the head and the Git objects it was read from; a process
        reuses it only while the head's revision and digest are unchanged and
        every one of those objects (each decision segment and chunk) still has
        the same path, mode and object ID in this transaction's tree. Any
        difference reads and verifies again. Failures are never reused.
        """
        segments_read: dict[str, str] = {}
        for number in range(1, record["revision"] + 1):
            path = segments.entry_path(wi, number)
            row = conn.execute("SELECT sha FROM production_objects WHERE path=?", (path,)).fetchone()
            if row is None:
                raise Rejected("journal_invalid", "A decision segment is missing")
            segments_read[path] = row[0]
        key = digest([self.namespace, wi, record["revision"], record.get("head"),
                      sorted(segments_read.items())])
        memo = self.decisions.get(wi)
        if memo is not None and memo[0] == key and all(
            (row := conn.execute("SELECT sha FROM production_objects WHERE path=?",
                                 (path,)).fetchone()) is not None and row[0] == sha
            for path, sha in memo[1][2].items()
        ):
            rows, custody, paths = memo[1]
            self.dependencies.update(paths)
            for sha, ref in custody:
                self.keep_custody(conn, sha, ref)
            return rows
        self.decisions.pop(wi, None)
        paths = self.prefetch_entries(conn, wi, record["revision"])
        entries, custody = [], []
        for number in range(1, record["revision"] + 1):
            entry, refs = segments.load_entry(
                segments.entry_path(wi, number), lambda p: self.read_path(conn, p))
            entries.append(entry)
            for sha, ref in refs.items():
                self.keep_custody(conn, sha, ref)
                custody.append((sha, ref))
        if digest(entries[-1]) != record.get("head"):
            raise Rejected("journal_invalid", "Production head differs from its journal")
        rows = [self.journal_row(entry) for entry in entries]
        self.decisions[wi] = (key, (rows, custody, paths))
        return rows

    @staticmethod
    def keep_custody(conn: sqlite3.Connection, sha: str, ref: Json) -> None:
        existing = conn.execute("SELECT content FROM original_refs WHERE digest=?", (sha,)).fetchone()
        if existing is not None and json.loads(existing[0]) != ref:
            raise Rejected("ledger_invalid", "Original custody changed within history")
        conn.execute("INSERT OR IGNORE INTO original_refs VALUES (?,?)", (sha, encoded(ref).decode()))

    def original(self, conn: sqlite3.Connection, sha: str) -> bytes:
        segments.checksum(sha)
        row = conn.execute("SELECT content FROM originals WHERE namespace=? AND digest=?",
                           (self.namespace, sha)).fetchone()
        if row is not None:
            return Evidence.checked(sha, bytes(row[0]))
        ref = conn.execute("SELECT content FROM original_refs WHERE digest=?", (sha,)).fetchone()
        if ref is None:
            raise Rejected("evidence_unavailable", "Original evidence is not available")
        reference = json.loads(ref[0])
        if reference["access"] == "git":
            return segments.unpack(reference["object"], lambda p: self.read_path(conn, p))
        return self.evidence.read(sha)

    def context(self, conn: sqlite3.Connection, wi: str, context: HostContext) -> HostContext:
        from dataclasses import replace

        policy = json.loads(conn.execute("SELECT content FROM production_context").fetchone()[0])
        if policy is None:
            raise Rejected(
                "policy_unavailable", "Production admission policy must be explicitly present"
            )
        return replace(context, admission_policy=policy, observed_work_item=wi)

    def assign_identity(self, conn: sqlite3.Connection, request: str, at: str) -> str:
        prior = self.find_request(conn, request)
        if prior:
            return str(prior["wi"])
        day = (
            datetime.fromisoformat(at.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .strftime("%Y%m%d")
        )
        start = int(digest([self.namespace, request])[:8], 16) % 1000
        self.prefixes.update({f"{PREFIX}/work-items/wi_{day}_", f".harness/work-items/wi_{day}_"})
        for offset in range(1000):
            wi = f"wi_{day}_{(start + offset) % 1000:03d}"
            if not conn.execute("SELECT 1 FROM production_objects WHERE path IN (?,?)",
                    (f"{PREFIX}/work-items/{wi}.json", f".harness/work-items/{wi}.yaml")).fetchone():
                return wi
        raise Rejected("identity_capacity", "No unused production identity remains for this date")

    def build_commit(self, expected: str, updates: dict[str, bytes]) -> str:
        with tempfile.TemporaryDirectory(prefix="harness-ledger-") as directory:
            index = str(Path(directory) / "index")
            env = {"GIT_INDEX_FILE": index}
            self.git("read-tree", expected, env=env)
            records = []
            paths = []
            for number, (path, content) in enumerate(sorted(updates.items())):
                if not path.startswith(".harness/") or "\n" in path or "\0" in path:
                    raise Rejected("ledger_invalid", "Publication escaped the registered ledger")
                original = Path(directory) / str(number)
                original.write_bytes(content)
                paths.append(original)
            # One Git process for the entire object batch. Files live only in
            # this owner-only transaction directory, never a shared checkout.
            hashes = self.git("hash-object", "-w", "--stdin-paths", data=(
                "".join(json.dumps(str(p), ensure_ascii=False) + "\n" for p in paths)).encode()).decode().splitlines()
            if len(hashes) != len(updates):
                raise Rejected("ledger_invalid", "Object publication batch is incomplete")
            for path, sha in zip(sorted(updates), hashes, strict=True):
                records.append(f"100644 {sha}\t{path}\0")
            self.git("update-index", "-z", "--index-info", data="".join(records).encode(), env=env)
            tree = self.git("write-tree", env=env).decode().strip()
            commit = (
                self.git(
                    "commit-tree",
                    tree,
                    "-p",
                    expected,
                    data=b"SCL: record native production decision\n",
                    env=PUBLISHER_IDENTITY,
                )
                .decode()
                .strip()
            )
        return commit

    def publish(self, expected: str, updates: dict[str, bytes]) -> str:
        # Ordinary facts and compatibility projections must stay bounded too.
        # Private or public artifact size never determines Git blob size.
        if any(len(content) > 8 * segments.CHUNK for content in updates.values()):
            raise Rejected("ledger_limit_exceeded", "A fact or projection exceeds the publication bound")
        baseline = self.tree(expected)
        watched = set(self.dependencies) | set(updates)
        prefixes = set(self.prefixes)
        for attempt in range(3):
            commit = self.build_commit(expected, updates)
            self.fault("before_push")
            try:
                result = self.git(
                    "push", "--porcelain",
                    "--force-with-lease=" + self.config["ref"] + ":" + expected,
                    self.config["remote"], commit + ":" + self.config["ref"], check=False)
                self.fault("after_push")
                observed = self.refresh()
            except (Rejected, OSError) as exc:
                try:
                    observed = self.refresh()
                    if self.git("merge-base", "--is-ancestor", commit, observed, check=False).returncode == 0:
                        return commit
                except (Rejected, OSError):
                    pass
                raise Rejected("ledger_commit_unknown",
                    "Publication may have committed; read back the original request before retrying") from exc
            if self.git("merge-base", "--is-ancestor", commit, observed, check=False).returncode == 0:
                return commit
            if observed != expected:
                if self.git("merge-base", "--is-ancestor", expected, observed, check=False).returncode:
                    raise Rejected("ledger_conflict", "Production history changed; read back the original request")
                current = self.tree(observed)
                changed = {p for p in set(baseline) | set(current) if baseline.get(p) != current.get(p)}
                dependent = {p for p in changed if p in watched or any(p.startswith(prefix) for prefix in prefixes)}
                # Concurrent creation of identical immutable content is harmless.
                dependent = {p for p in dependent if p not in updates or current.get(p) != self.object_id(updates[p])}
                if dependent:
                    raise Rejected("ledger_conflict",
                        "A decision dependency changed; retry the same original request")
                if attempt == 2:
                    raise Rejected("ledger_busy",
                        "Unrelated publications exhausted this bounded attempt; retry the original request")
                expected, baseline = observed, current
                continue
            if result.returncode:
                # Classification uses stderr privately; never echo its URLs or values.
                error = (result.stderr + result.stdout).lower()
                if any(v in error for v in (b"100.00 mb", b"100 mib", b"large files", b"file size limit")):
                    code = "ledger_limit_exceeded"
                elif any(v in error for v in (b"authentication", b"permission denied", b"protected branch", b"gh013", b"403")):
                    code = "ledger_publication_denied"
                else:
                    code = "ledger_publication_rejected"
                raise Rejected(code, "Production rejected publication; no decision was acknowledged")
            raise Rejected("ledger_commit_unknown", "Publication result is unconfirmed; read the original request")
        raise AssertionError("Bounded publication loop exhausted")
