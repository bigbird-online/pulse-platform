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

    def apply(self, effect: Json, content: bytes) -> tuple[str, Json]:
        return self.adapter(effect).apply(effect, content)

    def readback(self, effect: Json) -> tuple[str, Json]:
        return self.adapter(effect).readback(effect)

    def probe(self, effect: Json) -> tuple[str, Json]:
        """Read-only readback of an obsolete resource intention (resource effects only)."""
        return self.workspaces.probe(effect)


def effect_observation(effect: Json) -> Json:
    last = effect["observations"][-1] if effect.get("observations") else {}
    reason = last.get("details", {}).get("reason")
    return {
        **{k: effect.get(k) for k in ("effect_id", "effect_kind", "status", "retry")},
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
CHANGED_PRIORITY = 15 * 60
SETTLED_REVISIT = 6 * 3600
SCHEDULE_LIMIT = 1 << 20
FINAL_EFFECTS = frozenset({"observed_success", "cancelled"})
# Only these start a new attempt; an exhausted budget leaves them to retry_effect.
# An open attempt (attempt_started, unknown, observed_failure) is still read back.
NEW_ATTEMPT = frozenset({"requested", "observed_absence"})


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
            or effect.get("status") in NEW_ATTEMPT and retry.get("attempts_remaining") == 0
        ):
            return False
    return not any(
        r.get("retirement") is None
        and r.get("status") in {"observed_success", "existing_allocation_observed"}
        for r in current.get("resources", [])
    )


def schedule(heads: dict[str, str], seen: dict[str, Json], now: float) -> list[str]:
    """Least recently visited first; a WI whose ledger head changed since its
    last visit, never visited, or over its disk quota counts as visited
    `CHANGED_PRIORITY` earlier, so it goes first without starving the others.
    A settled terminal WI waits `SETTLED_REVISIT` unless its head changes.
    """
    keys = []
    for wi in sorted(heads):
        entry = seen.get(wi)
        if entry is None:
            keys.append(((0.0, wi), wi))
            continue
        changed = entry["head"] != heads[wi]
        if entry["settled"] and not changed and now - entry["at"] < SETTLED_REVISIT:
            continue
        boost = CHANGED_PRIORITY if changed or entry["exceeded"] else 0
        keys.append(((entry["at"] - boost, wi), wi))
    return [wi for _, wi in sorted(keys)]


def read_schedule(cursor: Path, heads: dict[str, str]) -> dict[str, Json]:
    """Deleting or corrupting the cache only repeats observations."""
    try:
        if not cursor.is_file() or cursor.is_symlink() or cursor.stat().st_size > SCHEDULE_LIMIT:
            return {}
        cached = json.loads(cursor.read_bytes()).get("seen")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(cached, dict):
        return {}
    return {
        wi: entry
        for wi, entry in cached.items()
        if wi in heads
        and isinstance(entry, dict)
        and isinstance(entry.get("head"), str)
        and type(entry.get("at")) in (int, float)
        and type(entry.get("settled")) is bool
        and type(entry.get("exceeded")) is bool
    }


def write_schedule(root: Path, cursor: Path, seen: dict[str, Json]) -> None:
    with tempfile.NamedTemporaryFile(dir=root, prefix=".effect-cursor-", delete=False) as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(json.dumps({"seen": seen}, sort_keys=True).encode())
        out.flush()
        os.fsync(out.fileno())
    try:
        os.replace(out.name, cursor)
    finally:
        Path(out.name).unlink(missing_ok=True)


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
        cursor = root / "effect-service-cursor.json"
        seen = read_schedule(cursor, heads)
        items: list[Json] = []
        start = time.monotonic()
        for wi in schedule(heads, seen, time.time())[:work_items]:
            if items and time.monotonic() - start >= 60:
                break
            # Selection is only a hint. The normal kernel rereads current facts
            # before every attempt; a retired program or changed intention fails.
            try:
                reclaimed = reclaim_terminal(kernel, host, wi)
                # Measure first: removed garbage lifts a quota block before effects run.
                disk = measure_disk(kernel, host, wi, copies)
                results = Effects(kernel, MaintenanceAdapter(host.policy), host.worker).drain(
                    wi, budget=budget, kinds=kinds(host.policy)
                )
                if disk is not None:
                    results.insert(0, disk)
                if reclaimed is not None:
                    results.insert(0, reclaimed)
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
            ok = current.get("status") == "read_only"
            seen[wi] = {
                "head": heads[wi],
                "at": time.time(),
                "settled": ok and settled(current),
                "exceeded": ok and current["disk_quota"].get("level") == "exceeded",
            }
            write_schedule(root, cursor, seen)
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
