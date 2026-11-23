"""Account service consumes existing native resource/source intentions only.

No agent session or human input is invented. The installed, account-owned
program is the service identity; the kernel still decides every effect step.
This interface cannot create work, grant scope, or end a WI. It requests one
cleanup: the workspaces of a WI a human has already ended (completed or
terminated), which physical retirement still refuses while dirty or unpublished.
"""

from __future__ import annotations

import argparse
import fcntl
from dataclasses import replace
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import Callable

from .delivery import WORKER_INTENTS
from .effects import Effects
from .kernel import Kernel
from .model import ArtifactInput, Command, HostContext, Json, Query, Rejected, digest, encoded, writer_identity
from .policy import execution, load
from .production_install import require_active_program
from .production_ledger import ProductionLedger
from .repository_identity import public_response
from .git_workspaces import GitWorkspaces
from .git_publication import GitPublication
from .workspaces import EFFECT_KINDS as WORKSPACE_KINDS
from .publication import EFFECT_KINDS as SOURCE_KINDS

from .github_merge import PullRequestMerge, merge_markers
from .pull_requests import MERGE_KIND

KINDS = WORKSPACE_KINDS | SOURCE_KINDS


def kinds(policy: Json) -> frozenset[str]:
    """Kernel merges run here only where the operator admitted GitHub access."""
    return KINDS | ({MERGE_KIND} if "pull_request_gate" in policy else frozenset())


class ServiceHost:
    def __init__(self, entry: Path):
        self.entry = entry
        self.policy_path = entry.parent / "host-policy.json"
        self.policy = load(self.policy_path)
        if "production_ledger" not in self.policy:
            raise Rejected(
                "authority_denied", "Account service requires the installed production authority"
            )
        self.seal = self._seal()
        self.handle = object()
        audit = execution(self.policy)["mode"] == "audit"
        self.current = HostContext(
            self.policy["namespace"],
            "delivery-worker",
            "delivery-service",
            self.policy["surface"],
            0,
            frozenset({"*"}),
            frozenset() if audit else WORKER_INTENTS,
            effect_kinds=frozenset() if audit else kinds(self.policy),
            read_only_reason="policy_audit" if audit else None,
        )

    def _seal(self) -> tuple[str, str]:
        for path in (self.entry, self.policy_path, self.entry.parent / "installation.json"):
            info = path.lstat()
            if (
                path.resolve() != path
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o022
            ):
                raise Rejected(
                    "authority_denied", "Service installation is not account-owned and protected"
                )
        require_active_program(self.entry, self.policy)
        return (
            hashlib.sha256(self.entry.read_bytes()).hexdigest(),
            hashlib.sha256(self.policy_path.read_bytes()).hexdigest(),
        )

    def authenticate(self, handle: object) -> HostContext:
        if handle is not self.handle:
            raise PermissionError("Unknown service invocation")
        if self._seal() != self.seal:
            raise Rejected("program_changed", "Service installation changed during this invocation")
        return self.current

    def worker(self, artifacts: tuple[ArtifactInput, ...]) -> object:
        self.current = replace(self.current, artifacts=artifacts)
        return self.handle


class MaintenanceAdapter:
    def __init__(self, policy: Json):
        self.surface: str | None = policy.get("surface")
        self.workspaces = GitWorkspaces(git_config_file=policy.get("git_config_file"))
        self.publication = GitPublication(
            Path(str(policy.get("publication_root", policy["delivery_root"]))),
            git_config_file=policy.get("git_config_file"),
        )
        self.merge = (
            PullRequestMerge(policy["pull_request_gate"], policy["python"],
                             markers=merge_markers(policy),
                             # Disposable, content-addressed workflow file cache.
                             cache=Path(str(policy["views_root"])) / "workflow-blobs"
                             if policy.get("views_root") else None)
            if "pull_request_gate" in policy
            else None
        )

    def adapter(self, effect: Json) -> GitWorkspaces | GitPublication | PullRequestMerge:
        if effect["effect_kind"] == MERGE_KIND and self.merge is not None:
            return self.merge
        if effect["effect_kind"] in WORKSPACE_KINDS:
            return self.workspaces
        if effect["effect_kind"] in SOURCE_KINDS:
            return self.publication
        raise Rejected("authority_denied", "Service cannot execute this effect kind")

    def local(self, effect: Json) -> bool:
        """A workspace effect runs only on the host that holds the workspace."""
        from .workspaces import on_host

        return effect["effect_kind"] not in WORKSPACE_KINDS or on_host(effect, self.surface)

    def apply(self, effect: Json, content: bytes) -> tuple[str, Json]:
        return self.adapter(effect).apply(effect, content)

    def readback(self, effect: Json) -> tuple[str, Json]:
        return self.adapter(effect).readback(effect)

    def deferrable(self, effect: Json) -> bool:
        adapter = self.adapter(effect)
        return isinstance(adapter, PullRequestMerge) and adapter.deferrable(effect)

    def defer(self, effect: Json) -> str | None:
        adapter = self.adapter(effect)
        return adapter.defer(effect) if isinstance(adapter, PullRequestMerge) else None

    def probe(self, effect: Json) -> tuple[str, Json]:
        """Read-only readback of an obsolete resource intention (resource effects only)."""
        return self.workspaces.probe(effect)


def effect_observation(effect: Json) -> Json:
    last = effect["observations"][-1] if effect.get("observations") else {}
    reason = last.get("details", {}).get("reason")
    return {
        **{k: effect.get(k) for k in ("effect_id", "effect_kind", "status", "retry")},
        **({"startable": effect["startable"]} if "startable" in effect else {}),
        "observation_reason": reason
        if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,96}", reason)
        else None,
    }


def observe(kernel: Kernel, host: ServiceHost, wi: str) -> Json:
    result = kernel.inspect(Query(wi), host.worker(()))
    if result["status"] == "rejected":
        return result
    # No raw evidence, candidate contents, repository credentials, or path-based
    # deletion authorization escapes this observation.
    view = result["result"]["current_observation"]
    resources = view.get("workspace_settlement", {}).get("resources", [])
    disk = view.get("disk_quota") or {}
    return {
        "schema": "harness.effect-service/v1",
        "status": "read_only",
        "work_item": result["work_item"],
        "source": "production_git",
        "business_authority": "native_core",
        "business_mutation": False,
        "terminal": bool(view.get("terminal")),
        "disk_quota": {k: disk.get(k) for k in ("level", "bytes", "limit_bytes", "measured_at")},
        "resources": [
            {k: r.get(k) for k in ("resource_id", "repo_id", "status", "retirement")}
            for r in resources
        ],
        "effects": [
            effect_observation(e) for e in result["effects"] if e["effect_kind"] in KINDS | {MERGE_KIND}
        ],
    }


def reclaim_terminal(kernel: Kernel, host: ServiceHost, wi: str) -> Json | None:
    """Request retirement of a terminal WI's ready, unretired workspaces."""
    inspected = kernel.inspect(Query(wi), host.worker(()))
    if inspected["status"] == "rejected":
        return None
    view = inspected["result"]["current_observation"]
    if not view.get("terminal"):
        return None
    pending = sorted(
        row["resource_id"]
        for row in view["workspace_settlement"]["resources"]
        if row.get("retirement") is None
        and row["status"] in {"observed_success", "existing_allocation_observed"}
    )
    if not pending:
        return None
    return kernel.execute(
        Command(reclaim_request(wi, pending, inspected["effects"]),
                "retire_terminal_resources", {}, wi),
        host.worker(()),
    )


def reclaim_request(wi: str, pending: list[str], effects: list[Json]) -> str:
    """The request ID of one reclaim decision.

    A deferred reclaim commits its "unsettled_effects" answer under this ID and
    an exact retry replays it, so the ID names every effect that can defer it,
    with its status: each one neither succeeded nor cancelled, a failure
    included, since a failed file delivery of the active candidate still defers
    and its recovery to success must ask again. Any such status change asks
    again; an unchanged deferral or a refused retirement is not requested again.
    The list is part of every ID, empty included, so a reclaim an older
    installation deferred under the bare resource list is asked again once (a
    refused resource already carries its retirement and is no longer pending)."""
    waiting = sorted(
        [e["effect_id"], e["status"]]
        for e in effects
        if e["status"] not in {"observed_success", "cancelled"}
        and e["effect_kind"] != "workspace_retire"
    )
    return "reclaim." + digest({"wi": wi, "resources": pending, "waiting": waiting})


Copies = Callable[[], "list[Json] | None"]


def known_work_items(ledger: ProductionLedger, revision: str) -> dict[str, str]:
    """WI identity -> blob of its head record, which changes with every journal write."""
    prefix = ".harness/core/work-items/"
    return {
        Path(p).stem: sha
        for p, sha in ledger.tree(revision).items()
        if p.startswith(prefix) and p.endswith(".json") and "/" not in p[len(prefix):]
    }


def copies_once(kernel: Kernel, policy: Json, known: frozenset[str]) -> Copies:
    """One discovery of Agent-made copies shared by every WI measured in a run.

    Every path a WI journal registers is that WI's workspace, never an Agent
    copy; an unreadable journal leaves discovery failed rather than risk
    counting a workspace twice.
    """
    from . import agent_copies

    cache: list[list[Json] | None] = []

    def discovered() -> list[Json] | None:
        if not cache:
            registered = kernel.registered_workspaces(policy["namespace"])
            cache.append(
                agent_copies.discover(policy, known, frozenset(registered["paths"]))
                if registered["coverage"] == "complete" else None
            )
        return cache[0]

    return discovered


def measure_disk(kernel: Kernel, host: ServiceHost, wi: str, copies: Copies | None = None) -> Json | None:
    """Report this WI's disk bytes (AH-086).

    Roots are the Host's repository bindings plus the roots of this WI's
    recorded workspaces; `<root>/<wi>` is measured whole, so copies made
    without allocate_workspaces count as unregistered. Copies Agents made
    elsewhere that name this WI are reported as named, and an active WI also
    reports the copies its holding Agent made that name no WI. The kernel keeps
    one record per Host and journals only a level or material change. An unreadable tree or failed discovery leaves the WI
    unmeasured rather than reporting a guessed size.
    """
    from . import agent_copies, quota

    inspected = kernel.inspect(Query(wi), host.worker(()))
    if inspected["status"] == "rejected":
        return None
    view = inspected["result"]["current_observation"]
    discovered = (copies or copies_once(kernel, host.policy, frozenset({wi})))()
    if discovered is None:
        return None
    own, stray = agent_copies.charged(discovered, wi, view["disk_quota"].get("agent"))
    if any(size is None for size in (*own.values(), *stray.values())):
        return None
    roots = {str(r["managed_root"]) for r in host.policy.get("repositories", [])}
    registered = {}
    for row in view["workspace_settlement"]["resources"]:
        suffix = "/" + row["target"]
        if row["target"].startswith(wi + "/") and row["path"].endswith(suffix):
            roots.add(row["path"][: -len(suffix)])
        if row["status"] != "retired":
            registered[row["resource_id"]] = row["path"]
    data = quota.measure(wi, roots, registered)
    if data is None:
        return None
    if own:
        data["named"] = {p: size or 0 for p, size in sorted(own.items())}
    if stray:
        largest = sorted(stray.items(), key=lambda item: (-(item[1] or 0), item[0]))
        data["agent_copies"] = {
            "actor": view["disk_quota"]["agent"],
            "bytes": sum(size or 0 for size in stray.values()),
            "copies": {p: size or 0 for p, size in sorted(largest[: quota.REPORTED_UNREGISTERED])},
        }
    mine = view["disk_quota"]["hosts"].get(host.policy["surface"])
    # Nothing to report, unless an older program's record waits to be replaced.
    if (not data["resources"] and not data["unregistered"] and not own and not stray
            and mine is None and quota.LEGACY_HOST not in view["disk_quota"]["hosts"]):
        return None
    proof = ArtifactInput("observations/disk." + wi, digest(data), encoded(data))
    # This Host's recorded usage is part of the identity: the same bytes measured
    # again after a recorded change (garbage removed) are a new observation, not a
    # replay. The Host is too: another Host measuring identical bytes is its own request.
    previous = [(mine or {}).get(k) for k in ("bytes", "measured_at", "named_bytes", "agent_copies_bytes")]
    identity = {**data, "wi": wi, "previous": previous, "host": host.policy["surface"]}
    return kernel.execute(
        Command("disk." + digest(identity), "observe_disk_usage",
                {**data, "evidence": [proof.reference()]}, wi),
        host.worker((proof,)),
    )


def agent_report(copies: Copies) -> Json | None:
    """Per-Agent copy totals of this run, or None when discovery failed."""
    from . import agent_copies

    discovered = copies()
    return None if discovered is None else agent_copies.summary(discovered)


# Scheduling hints only (seconds). The kernel rereads every fact before acting.
SETTLED_REVISIT = 6 * 3600
# The schedule: one file per WI under `views_root`, each at most ENTRY_LIMIT bytes,
# holding at most PENDING_LIMIT open effects; an older program kept one cursor.
STORE = "effect-service-schedule"
ENTRY_LIMIT = 64 * 1024
PENDING_LIMIT = 128
EFFECT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
WI_NAME = re.compile(r"wi_[A-Za-z0-9_-]{1,93}")
LEGACY = "effect-service-cursor.json"
LEGACY_LIMIT = 1 << 20
FINAL_EFFECTS = frozenset({"observed_success", "cancelled"})
# Only these start a new attempt; an exhausted budget leaves them to retry_effect.
# An open attempt (attempt_started, unknown, observed_failure) is still read back.
NEW_ATTEMPT = frozenset({"requested", "observed_absence"})
# Queues of `schedule`, in the order their lead rotates.
QUEUES = 3
JOURNALS = ".harness/core/journals/"
OBJECTS = ".harness/core/objects/"
BLOB = re.compile(r"(?:100755:blob:)?(?:[0-9a-f]{40}|[0-9a-f]{64})")


def settled(current: Json) -> bool:
    """A terminal WI with nothing the service could still advance or reclaim.

    Its files can change only outside the ledger, so it is revisited rarely;
    any ledger change to it makes it a priority again.
    """
    if not current.get("terminal") or current.get("disk_quota", {}).get("level") == "exceeded":
        return False
    for effect in current.get("effects", []):
        retry = effect.get("retry") or {}
        if not (
            effect.get("status") in FINAL_EFFECTS
            # A failed merge is final; an exhausted budget waits for retry_effect.
            or effect.get("status") == "observed_failure" and effect.get("effect_kind") == MERGE_KIND
            or effect.get("status") in NEW_ATTEMPT and (
                retry.get("attempts_remaining") == 0 or effect.get("startable") is False)
        ):
            return False
    return not any(
        r.get("retirement") is None
        and r.get("status") in {"observed_success", "existing_allocation_observed"}
        for r in current.get("resources", [])
    )


def advanceable(current: Json, service_kinds: frozenset[str]) -> list[Json]:
    """The effects a next visit would act on: begin, wait on or read back an attempt.

    An exhausted budget waits for retry_effect, a merge the kernel no longer
    admits (`startable` false: its WI ended, its PR was withdrawn, a later round
    replaced it) never begins again, and a failure is not resumed, so none is
    listed. An open attempt is read back whatever the WI's state.
    """
    found = []
    for effect in current.get("effects", []):
        if effect.get("effect_kind") not in service_kinds:
            continue
        status, retry = effect.get("status"), effect.get("retry") or {}
        if status in {"attempt_started", "unknown"} or (
            status in NEW_ATTEMPT and retry.get("attempts_remaining") != 0
            and effect.get("startable") is not False
        ):
            found.append(effect)
    return found


def progress(current: Json, service_kinds: frozenset[str]) -> str:
    """What the advanceable effects showed: a visit that changes none made no progress."""
    return digest(sorted(
        [e.get("effect_id"), e.get("status"), (e.get("retry") or {}).get("attempts_remaining"),
         e.get("observation_reason")]
        for e in advanceable(current, service_kinds)
    ))


def due(current: Json, service_kinds: frozenset[str], previous: Json | None,
        results: list[Json]) -> bool:
    """Whether the next visit can be expected to move an advanceable effect.

    It can when this visit moved one (begun, read back to a new outcome); when
    one waits on a condition that changes without any ledger write: a merge,
    whose checks, mergeability and readback live on GitHub (a deferral while
    checks run, an absence, an unreadable readback), or a retryable refusal
    (retry backoff, ledger busy); or when the last visit saw a different state.
    A merge leaves this queue by its own bounds: eight attempts, `PENDING_WAIT`
    per deferral, or the kernel no longer admitting it (`advanceable`, or a
    begin refused for good this visit); the queues' turns keep every other
    WI's bound meanwhile.
    A visit that left every other advanceable effect as it found it made no
    progress (a workspace read back as the same unknown, a workspace on another
    Host, a refused begin): repeating it soon changes nothing, so the WI goes
    back to rotation until its journal or its effects change.
    """
    # A begin the kernel refused for good this visit: that effect cannot move.
    refused = {
        str(r.get("request_id"))[len("send."):].rpartition(".")[0]
        for r in results
        if r.get("status") == "rejected" and (r.get("error") or {}).get("retryable") is False
        and str(r.get("request_id")).startswith("send.")
    }
    pending = [e for e in advanceable(current, service_kinds)
               if not (e.get("status") in NEW_ATTEMPT and e.get("effect_id") in refused)]
    if not pending:
        return False
    if any(e.get("effect_kind") == MERGE_KIND for e in pending):
        return True
    if any(r.get("status") == "deferred"
           or r.get("status") == "rejected" and (r.get("error") or {}).get("retryable") is True
           for r in results):
        return True
    return previous is None or previous.get("progress") != progress(current, service_kinds)


def journal_revisions(tree: dict[str, str]) -> dict[str, int]:
    """WI identity -> its newest journal entry in this production tree."""
    found: dict[str, int] = {}
    for path in tree:
        if not path.startswith(JOURNALS):
            continue
        wi, _, name = path[len(JOURNALS):].partition("/")
        if "/" not in name and re.fullmatch(r"[0-9]{12}\.json", name):
            found[wi] = max(found.get(wi, 0), int(name[:12]))
    return found


def fold(pending: dict[str, int], entry: Json, number: int,
         service_kinds: frozenset[str]) -> bool:
    """Advance one WI's open service effects (effect ID -> revision of its latest
    request or retry) by one journal entry, as the kernel's effect events do.

    An effect of the service's kinds opens when requested and again on a repair
    retry; an observed success or failure, or a cancellation, closes it. An
    absence or unknown keeps it open. A malformed entry raises; the caller
    takes that as this WI's read failure. Returns whether the entry retried an
    effect not tracked here (dropped past `PENDING_LIMIT`, or of another kind):
    the caller then visits the WI, whose kernel view decides."""
    untracked = False
    events = entry["events"]
    if not isinstance(events, list):
        raise TypeError("events")
    for event in events:
        kind, payload = event["kind"], event["payload"]
        effect = payload["effect_id"] if str(kind).startswith("Effect") else None
        if kind == "EffectRequested":
            if payload.get("effect_kind") in service_kinds:
                if not isinstance(effect, str) or not EFFECT_ID.fullmatch(effect):
                    raise ValueError("effect_id")
                pending[effect] = number
        elif effect not in pending:
            untracked = untracked or kind == "EffectRetryRequested"
            continue
        elif kind == "EffectRetryRequested":
            pending[effect] = number
        elif kind == "EffectCancelled" or kind == "EffectObserved" and (
                payload["observation"]["outcome"] in {"success", "failure"}):
            del pending[effect]
    return untracked


def read_journals(ledger: ProductionLedger, revision: str, known: dict[str, Json],
                  service_kinds: frozenset[str]) -> tuple[dict[str, Json], set[str]]:
    """Every WI's open service effects as of this production revision, and the WIs
    to visit whatever they hold: an entry could not be read this tick, or one
    retried an effect not tracked here.

    `known` is the last tick's result (`{"revision": n, "pending": {...}}` per
    WI); only entries after it are read, so a cold start, a lost cursor or an
    older program's cursor reads each journal once in full. An entry that cannot
    be read or does not have the expected shape is skipped and its WI reported
    unreadable once: the visit it causes rereads everything through the kernel.
    A failed read of the whole batch keeps the last state and reports every WI
    it covered unreadable.
    """
    from . import production_segments as segments

    tree = ledger.tree(revision)
    journals: dict[str, Json] = {}
    unread: list[tuple[str, int, str]] = []
    for wi, until in journal_revisions(tree).items():
        last = known.get(wi) or {"revision": 0, "pending": {}}
        journals[wi] = {"revision": last["revision"], "pending": dict(last["pending"])}
        if until < last["revision"]:  # A rewritten history: read it again.
            journals[wi] = {"revision": 0, "pending": {}}
        unread += [(wi, n, segments.entry_path(wi, n))
                   for n in range(journals[wi]["revision"] + 1, until + 1)]
    failed: set[str] = set()
    if not unread:
        return journals, failed
    try:
        # Every journal and object blob is selectable, so a packed entry finds its chunks.
        blobs = {path: sha for path, sha in tree.items()
                 if path.startswith((JOURNALS, OBJECTS)) and BLOB.fullmatch(sha)}
        with ledger.object_reader(blobs) as read:
            for wi, number, path in unread:
                try:
                    entry, _ = segments.load_entry(path, read)
                    if fold(journals[wi]["pending"], entry, number, service_kinds):
                        failed.add(wi)
                except Rejected as exc:
                    if exc.code == "ledger_unavailable":
                        raise  # The object stream failed: no later read can succeed.
                    failed.add(wi)
                except (ValueError, TypeError, KeyError, AttributeError):
                    failed.add(wi)
                journals[wi]["revision"] = number
    except (Rejected, OSError):
        for wi, _, _ in unread:
            journals[wi] = known.get(wi) or {"revision": 0, "pending": {}}
            failed.add(wi)
    return journals, failed


def journal_news(journals: dict[str, Json], failed: set[str],
                 seen: dict[str, Json]) -> set[str]:
    """WIs with service work their last visit has not seen: an open effect
    requested or retried after the journal revision that visit recorded (none
    recorded, never visited, or an older program's cursor: every open effect),
    an entry that could not be read, or a retry of an effect not tracked."""
    news = set(failed)
    for wi, journal in journals.items():
        visited = (seen.get(wi) or {}).get("revision", 0)
        if any(number > visited for number in journal["pending"].values()):
            news.add(wi)
    return news


def schedule(heads: dict[str, str], seen: dict[str, Json], now: float, turn: int = 0,
             news: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Three queues, interleaved; `turn` (one per tick) rotates which leads.

    Due: a WI with service work its last visit has not seen (`journal_news`: a
    merge just confirmed; on a cold start or an older cursor, any open effect),
    or whose last visit left an effect it can expect to move (`due`). Priority: any other WI whose head another
    writer changed since, or that was over its disk quota (re-measure soon).
    Rotation: every other WI, never visited first. Each queue is purely least
    recently visited first, so a WI waiting in it is passed by each other WI of
    that queue at most once. With one visit per tick each queue gets at least
    every third tick (more while another is empty): a WI waits at most
    3 x (WIs in its queue during the wait) ticks. A settled terminal WI waits
    `SETTLED_REVISIT` unless its head changes.
    """
    queues: list[list[tuple[tuple[float, str], str]]] = [[] for _ in range(QUEUES)]
    for wi in sorted(heads):
        entry = seen.get(wi)
        if entry is None:
            queues[0 if wi in news else 2].append(((0.0, wi), wi))
            continue
        changed = entry["head"] != heads[wi]
        if (entry["settled"] and not changed and wi not in news
                and now - entry["at"] < SETTLED_REVISIT):
            continue
        queue = (0 if wi in news or entry.get("due") else
                 1 if changed or entry["exceeded"] else 2)
        queues[queue].append(((entry["at"], wi), wi))
    ordered = [[wi for _, wi in sorted(q)] for q in queues]
    lead = turn % QUEUES
    ordered = ordered[lead:] + ordered[:lead]
    order: list[str] = []
    for depth in range(max(map(len, ordered))):
        order += [q[depth] for q in ordered if depth < len(q)]
    return order


def valid_seen(entry: object) -> Json | None:
    """One WI's visit record, or None when it is malformed or unbounded."""
    if not (
        isinstance(entry, dict)
        and isinstance(entry.get("head"), str) and len(entry["head"]) <= 128
        and type(entry.get("at")) in (int, float)
        and type(entry.get("settled")) is bool
        and type(entry.get("exceeded")) is bool
        # An older program's `active` meant an advanceable effect: due once more.
        and type(entry.get("due", entry.get("active", False))) is bool
        and type(entry.get("revision", 0)) is int
        and isinstance(entry.get("progress", ""), str) and len(entry.get("progress", "")) <= 128
    ):
        return None
    record = {k: entry[k] for k in ("head", "at", "settled", "exceeded", "revision", "progress")
              if k in entry}
    return {**record, "due": entry.get("due", entry.get("active", False))}


def valid_journal(entry: object) -> Json | None:
    """One WI's `read_journals` state, or None when it is malformed or unbounded."""
    if not (
        isinstance(entry, dict)
        and type(entry.get("revision")) is int and entry["revision"] >= 0
        and isinstance(entry.get("pending"), dict)
        and len(entry["pending"]) <= PENDING_LIMIT
        and all(EFFECT_ID.fullmatch(e) and type(n) is int for e, n in entry["pending"].items())
    ):
        return None
    return {"revision": entry["revision"], "pending": dict(entry["pending"])}


def bounded_read(path: Path, limit: int) -> object:
    """A small regular file's JSON, or None. Deleting or corrupting it only
    repeats observations."""
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
            return None
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def schedule_store(root: Path) -> Path | None:
    directory = root / STORE
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        return None
    return directory


def read_store(root: Path, heads: dict[str, str]) -> tuple[dict[str, Json], dict[str, Json], int]:
    """The visit records, journal states and turn the last tick left.

    One file per WI, each bounded by construction (`write_entry`), so the
    schedule never outgrows what it can read back however many WIs there are.
    With no record at all, an older program's single cursor supplies the visits.
    """
    seen: dict[str, Json] = {}
    journals: dict[str, Json] = {}
    directory = schedule_store(root)
    if directory is not None and directory.is_dir():
        for wi in heads:
            if not WI_NAME.fullmatch(wi):
                continue
            data = bounded_read(directory / f"{wi}.json", ENTRY_LIMIT)
            if not isinstance(data, dict):
                continue
            if (visit := valid_seen(data.get("seen"))) is not None:
                seen[wi] = visit
            if (journal := valid_journal(data.get("journal"))) is not None:
                journals[wi] = journal
    if not seen and not journals:
        return read_schedule(root / LEGACY, heads), {}, 0
    data = bounded_read(directory / "turn.json", ENTRY_LIMIT) if directory else None
    data = data if isinstance(data, dict) else {}
    lost = data.get("lost")
    for wi, at in (lost.items() if isinstance(lost, dict) else []):
        # A visit whose own record could not be written keeps its place in line.
        if wi in heads and wi not in seen and type(at) in (int, float):
            seen[wi] = {"head": "", "at": at, "settled": False, "exceeded": False,
                        "due": False, "lost": True}
    turn = data.get("turn")
    return seen, journals, turn if type(turn) is int and 0 <= turn < QUEUES else 0


def read_schedule(cursor: Path, heads: dict[str, str]) -> dict[str, Json]:
    """An older program's single cursor: its visit records only."""
    cached = bounded_read(cursor, LEGACY_LIMIT)
    cached = cached.get("seen") if isinstance(cached, dict) else None
    if not isinstance(cached, dict):
        return {}
    return {wi: visit for wi, entry in cached.items()
            if wi in heads and (visit := valid_seen(entry)) is not None}


def replace_file(directory: Path, name: str, content: bytes) -> None:
    """Replace one record. The store is this service's private, disposable cache,
    so whatever occupies a record's path (a directory, a link) is removed first."""
    target = directory / name
    if target.is_dir() and not target.is_symlink():
        for parent, directories, files in os.walk(target, topdown=False):
            for name in files + [d for d in directories if os.path.islink(os.path.join(parent, d))]:
                os.unlink(os.path.join(parent, name))
            for name in directories:
                if not os.path.islink(os.path.join(parent, name)):
                    os.rmdir(os.path.join(parent, name))
        os.rmdir(target)
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".effect-cursor-", delete=False) as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
    try:
        os.replace(out.name, target)
    finally:
        Path(out.name).unlink(missing_ok=True)


def unrecorded(what: str, exc: OSError) -> None:
    """A schedule record is disposable: a failed write or removal loses only that
    record (the WI is visited as if new) and never the tick."""
    print(f"effect-service: schedule {what} not recorded: {type(exc).__name__}",
          file=sys.stderr)


def write_entry(root: Path, wi: str, seen: Json | None, journal: Json | None) -> bool:
    """Replace one WI's record, and say whether it was written. It holds at most
    `PENDING_LIMIT` open effects (the most recently requested, which decide news)
    of bounded IDs, so it always fits `ENTRY_LIMIT`; an unnamed WI is not
    recorded and is simply visited as new."""
    try:
        store_entry(root, wi, seen, journal)
    except OSError as exc:
        unrecorded(wi, exc)
        return False
    return True


def store_entry(root: Path, wi: str, seen: Json | None, journal: Json | None) -> None:
    directory = schedule_store(root)
    if directory is None or not WI_NAME.fullmatch(wi):
        return
    directory.mkdir(mode=0o700, exist_ok=True)
    if journal is not None:
        newest = sorted(journal["pending"].items(), key=lambda item: (-item[1], item[0]))
        journal = {"revision": journal["revision"],
                   "pending": dict(newest[:PENDING_LIMIT])}
    content = json.dumps({"seen": seen, "journal": journal}, sort_keys=True).encode()
    if len(content) > ENTRY_LIMIT:  # Unreachable for valid records; never unreadable.
        content = json.dumps({"seen": seen, "journal": None}, sort_keys=True).encode()
    replace_file(directory, f"{wi}.json", content)


def write_turn(root: Path, turn: int, lost: dict[str, float] | None = None) -> None:
    """The next tick's lead, and when each WI whose own record could not be
    written was last visited (the newest `PENDING_LIMIT`), so such a WI is not
    taken for never visited and put first again."""
    newest = sorted((lost or {}).items(), key=lambda item: -item[1])[:PENDING_LIMIT]
    directory = schedule_store(root)
    try:
        if directory is not None:
            directory.mkdir(mode=0o700, exist_ok=True)
            replace_file(directory, "turn.json",
                         json.dumps({"turn": turn, "lost": dict(newest)}).encode())
    except OSError as exc:
        unrecorded("turn", exc)


def prune_store(root: Path, heads: dict[str, str]) -> None:
    """Forget WIs the ledger no longer names, and the older program's cursor."""
    directory = schedule_store(root)
    if directory is None or not directory.is_dir():
        return
    stale = [root / LEGACY]
    try:
        stale += [path for path in directory.glob("*.json")
                  if path.name != "turn.json" and path.stem not in heads]
    except OSError as exc:
        unrecorded("listing", exc)
    for path in stale:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            unrecorded(path.name, exc)


def visited_head(ledger: ProductionLedger, wi: str, before: tuple[str, int],
                 results: list[Json]) -> tuple[str, int]:
    """The head and journal revision this visit's own writes left, read before
    the effects are observed.

    Its own writes are not news, so only another writer's change makes the WI a
    priority. Read before `observe`, a write landing after this read (a merge
    confirmed meanwhile) is either in the observation or a head change next tick,
    whose journal entries the next tick reads from this revision on.
    """
    if not any(r.get("status") == "local_committed" for r in results):
        return before
    try:
        revision = ledger.refresh()
        head = known_work_items(ledger, revision).get(wi)
        if head is None:
            return before
        return head, journal_revisions(ledger.tree(revision)).get(wi, before[1])
    except (Rejected, ValueError, OSError):
        return before  # Unread: the visit's own write counts as a change once.


def unobserved(previous: Json | None, now: float) -> Json:
    """The cursor entry of a visit whose observation failed or was rejected.

    Nothing was seen, so the head, revision and flags of the last successful
    observation stay: a write since then (a merge just confirmed) still reads as
    a change and news, and a due effect stays due. Never observed, the empty
    head differs from any head. `at` moves on, so a WI that keeps failing takes
    one turn of its queue, never the front of it.
    """
    entry = {
        "head": previous["head"] if previous else "",
        "at": now,
        "settled": False,
        "exceeded": bool(previous and previous["exceeded"]),
        "due": bool(previous and previous.get("due")),
    }
    for key in ("revision", "progress"):
        if previous and key in previous:
            entry[key] = previous[key]
    return entry


def tick(
    kernel: Kernel, host: ServiceHost, ledger: ProductionLedger, *, budget: int, work_items: int
) -> Json:
    """The disposable visit schedule is scheduling only, never a work-state source."""
    if execution(host.policy)["mode"] != "automatic":
        raise Rejected("policy_read_only", "Automatic effects are not enabled in this installation")
    root = Path(host.policy["views_root"])
    if root.resolve() != root:
        raise Rejected("service_unavailable", "Service cache root redirects")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise Rejected("service_unavailable", "Service cache root is not protected")
    fd = os.open(root / ".effect-service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "schema": "harness.effect-service/v1",
                "status": "busy",
                "business_mutation": False,
            }
        revision = ledger.refresh()
        heads = known_work_items(ledger, revision)
        copies = copies_once(kernel, host.policy, frozenset(heads))
        seen, known, turn = read_store(root, heads)
        lost = {wi: entry["at"] for wi, entry in seen.items() if entry.get("lost")}
        service_kinds = kinds(host.policy)
        revisions = journal_revisions(ledger.tree(revision))
        journals, unreadable = read_journals(ledger, revision, known, service_kinds)
        news = journal_news(journals, unreadable, seen)
        for wi, journal in journals.items():
            if wi in heads and journal != known.get(wi):
                write_entry(root, wi, seen.get(wi), journal)
        prune_store(root, heads)
        items: list[Json] = []
        start = time.monotonic()
        for wi in schedule(heads, seen, time.time(), turn, news)[:work_items]:
            if items and time.monotonic() - start >= 60:
                break
            # Selection is only a hint. The normal kernel rereads current facts
            # before every attempt; a retired program or changed intention fails.
            head = heads[wi], revisions.get(wi, 0)  # Used only once `observe` succeeds.
            try:
                reclaimed = reclaim_terminal(kernel, host, wi)
                # Measure first: removed garbage lifts a quota block before effects run.
                disk = measure_disk(kernel, host, wi, copies)
                results = Effects(kernel, MaintenanceAdapter(host.policy), host.worker).drain(
                    wi, budget=budget, kinds=service_kinds
                )
                if disk is not None:
                    results.insert(0, disk)
                if reclaimed is not None:
                    results.insert(0, reclaimed)
                head = visited_head(ledger, wi, head, results)
                current = observe(kernel, host, wi)
            except (Rejected, ValueError, OSError) as exc:
                current = {
                    "error": {
                        "code": exc.code
                        if isinstance(exc, Rejected)
                        else "service_item_unavailable",
                        "reason": "Original effect observation unavailable; original work retained",
                    }
                }
                results = []
            items.append(
                {
                    "work_item_id": wi,
                    "effects": current.get("effects", []),
                    "error": current.get("error"),
                    "progress": [
                        {k: r.get(k) for k in ("status", "request_id", "operation_id", "error")}
                        for r in results
                    ],
                }
            )
            if current.get("status") == "read_only":
                seen[wi] = {
                    "head": head[0],
                    "revision": head[1],
                    "at": time.time(),
                    "settled": settled(current),
                    "exceeded": current["disk_quota"].get("level") == "exceeded",
                    "due": due(current, service_kinds, seen.get(wi), results),
                    "progress": progress(current, service_kinds),
                }
            else:
                seen[wi] = unobserved(seen.get(wi), time.time())
            if write_entry(root, wi, seen[wi], journals.get(wi)):
                lost.pop(wi, None)
            else:
                lost[wi] = seen[wi]["at"]
            write_turn(root, (turn + 1) % QUEUES, lost)
        failed = any(
            item["error"] or any(p.get("status") == "rejected" for p in item["progress"])
            for item in items
        )
        return {
            "schema": "harness.effect-service/v1",
            "status": "partial" if failed else "observed",
            "source": "production_git",
            "selection_revision": revision,
            "items": items,
            "agent_copies": agent_report(copies) if items else None,
            "business_mutation": any(
                p.get("status") == "local_committed" for item in items for p in item["progress"]
            ),
        }


def main(entry: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Read or advance already admitted native maintenance effects"
    )
    parser.add_argument("action", choices=["inspect", "measure", "drain", "tick"])
    parser.add_argument("--wi")
    parser.add_argument("--work-items", type=int, default=4)
    parser.add_argument("--budget", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.budget <= 64:
            raise Rejected("invalid_input", "Effect service requires a bounded batch")
        if (args.action == "tick") == (args.wi is not None) or not 1 <= args.work_items <= 16:
            raise Rejected(
                "invalid_input", "Tick discovers original native work; other actions require one WI"
            )
        host = ServiceHost(entry)
        ledger = ProductionLedger(host.policy["production_ledger"], host.policy["namespace"])
        kernel = Kernel(
            None,
            host.authenticate,
            storage=ledger,
            writer_id=writer_identity(host.seal[0], host.policy),
            pages=Path(host.policy["views_root"]) if host.policy.get("views_root") else None,
        )
        if args.action == "tick":
            response = tick(kernel, host, ledger, budget=args.budget, work_items=args.work_items)
            print(json.dumps(public_response(response), ensure_ascii=False, sort_keys=True))
            return 2 if response["status"] == "partial" else 0
        response = observe(kernel, host, args.wi)
        if args.action in {"measure", "drain"} and response["status"] != "rejected":
            if execution(host.policy)["mode"] == "audit":
                raise Rejected("policy_read_only", "Audit installation cannot run effects")
            # Re-measure first, so removed garbage lifts a quota block at once.
            known = frozenset(known_work_items(ledger, ledger.refresh()))
            disk = measure_disk(kernel, host, args.wi, copies_once(kernel, host.policy, known))
            results = [] if disk is None else [disk]
            if args.action == "drain":
                results += Effects(kernel, MaintenanceAdapter(host.policy), host.worker).drain(
                    args.wi, budget=args.budget, kinds=kinds(host.policy)
                )
            response = observe(kernel, host, args.wi)
            response["progress"] = [
                {k: r.get(k) for k in ("status", "request_id", "operation_id", "error")}
                for r in results
            ]
            response["business_mutation"] = any(
                r.get("status") == "local_committed" for r in results
            )
        print(json.dumps(public_response(response), ensure_ascii=False, sort_keys=True))
        return 2 if response.get("status") == "rejected" else 0
    except (Rejected, ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, Rejected) else "service_unavailable"
        reason = (
            exc.reason
            if isinstance(exc, Rejected)
            else "Service installation or authority is unavailable"
        )
        print(json.dumps({"status": "rejected", "error": {"code": code, "reason": reason}}))
        return 2
