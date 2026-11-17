"""Relay gate that starts heterogeneous PR reviews from durable kernel requests.

Registering a pull request records a `pr_review_dispatch` effect bound to its
WI, candidate, head SHA, Agent, reviewer runtime and the `review` profile.
`harness-native pr-review-dispatch --actor <agent> --runtime cc|codex` is the
pre-LLM gate of one Relay job per Agent and runtime (computeProfile `review`).
It runs as the protected active installation, holds only this effect kind and
never writes business decisions. Each run starts at most one review, so one
review session serves exactly one WI:

- a request not started, or read back as not executed, begins a new attempt
  under a request identity unique to this run (a lease): a concurrent or
  replayed run cannot obtain the same attempt, so only one run starts it;
- the started session first declares the WI and records
  `receive_pull_request_review {candidate_id, attempt_id}` (the runtime receipt);
- an attempt whose round has been reviewed is observed as success; one whose
  round was superseded, as failure;
- an attempt without receipt after the receipt timeout (the gate or Relay died
  before the session started), or with receipt but no review after the review
  cooldown, is observed as absence and begins again within the effect's retry
  budget and backoff; an exhausted budget shows its block and recovery on the WI;
- nothing to start prints PR_REVIEW_SKIP and exits 3; a failure exits 2.

Relay may run up to the job's `maxConcurrent` runs at once. A started attempt is
not startable again before its receipt timeout or cooldown, so each of them
starts a different request, and a lost attempt is restarted alone.

The job may run every minute. With `--default-minutes` (minutes of the hour,
e.g. `7,22,37,52`), a request registered without `immediately` starts only
after one of those slots has passed since it was registered, so the default
cadence stays; a request registered with `immediately: true` starts on the
next run.

A run that cannot have anything to do reads only the production head. After each
full check the gate keeps, beside its lock under `views_root`, the production
head it started from and the earliest instant at which an open request could
become startable without a new ledger write (a passed default slot, receipt
timeout, review cooldown or retry backoff). The next run reads the head with one
`git ls-remote` of the production ref; while that head, the gate parameters and
the installation are unchanged, the instant has not arrived and the record is
younger than `STATE_MAX_AGE`, it prints PR_REVIEW_SKIP without fetching or
replaying. Any ledger write (registration, receipt, review, retry) changes the
head, so the next run checks fully; an unreadable record or head also checks
fully.

Every Agent has its own pair of jobs, because a review session runs as its job's
owner and the kernel accepts only a session of the author's own Agent. The gates
of all Agents on one installation share two records beside a shared lock, so
GitHub reads do not grow with the number of Agents:

- the production head, read by one `git ls-remote` and reused by every gate for
  `HEAD_TTL`, which is shorter than the one-minute schedule;
- an index of the open review requests of every Agent and runtime at a head,
  written by the one gate that replayed the ledger for that head. A gate whose
  Agent and runtime have no open request in a fresh index at the current head
  prints PR_REVIEW_SKIP without fetching or replaying.

A cached head is at most `HEAD_TTL` old and was read before any check that uses
it, so a record or index naming it describes that head or a later state: a
ledger write delays a start by at most `HEAD_TTL`, never hides it.

A request is served only if its Agent's job for that runtime exists. Every gate
run that takes the shared lock, quiet or not, records in
`.pr-review-dispatch.seen.json` that its Agent and runtime has a live gate.
`harness-native pr-review-gate-audit` compares that record with the index: an
Agent and runtime with open requests whose gate has not run within the grace
period (or ever, once the record is older than the grace period) is printed as
missing, naming the job `<agent>-pr-review-<runtime>` to register, so a missing
job surfaces to the platform owner instead of leaving the request waiting
silently. Each missing gate is reported again only after `AUDIT_REPEAT`.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from typing import IO, Callable
import tempfile
import uuid

from .delivery import retry_policy
from .kernel import Kernel
from .model import ArtifactInput, Command, Json, Query, Rejected, digest, encoded, identity
from .pull_requests import DISPATCH_KIND

SKIP = "PR_REVIEW_SKIP"
SCHEMA = "pulse.pr_review_dispatch/v1"
STATE_SCHEMA = "pulse.pr_review_gate_state/v1"
HEAD_SCHEMA = "pulse.pr_review_gate_head/v1"
INDEX_SCHEMA = "pulse.pr_review_gate_index/v1"
SEEN_SCHEMA = "pulse.pr_review_gate_seen/v1"
AUDIT_SCHEMA = "pulse.pr_review_gate_audit/v1"
AUDIT_SKIP = "PR_REVIEW_GATE_AUDIT_SKIP"
# A live gate runs every minute; a default-slot request waits at most 15 minutes.
AUDIT_GRACE = timedelta(minutes=30)
# A missing gate stays reported until repaired, once per this interval.
AUDIT_REPEAT = timedelta(hours=6)
# A bound on any input outside the ledger head, gate parameters and installation.
STATE_MAX_AGE = timedelta(minutes=30)
# Shorter than the one-minute schedule: each minute's gates share one head read.
HEAD_TTL = timedelta(seconds=45)
# How long a gate waits for another gate's shared read before reading alone.
SHARED_WAIT_SECONDS = 60.0
Worker = Callable[[tuple[ArtifactInput, ...]], object]


def observe(kernel: Kernel, worker: Worker, wi: str, effect: Json, attempt: str,
            outcome: str, details: Json) -> Json:
    observation = {"effect_id": effect["effect_id"], "attempt_id": attempt,
                   "outcome": outcome, "details": details}
    proof = ArtifactInput("observations/" + effect["effect_id"], digest(observation),
                          encoded(observation))
    data = {**observation, "evidence": [proof.reference()]}
    return kernel.execute(Command("observe." + digest(data), "observe_effect", data, wi),
                          worker((proof,)))


def begin(kernel: Kernel, worker: Worker, wi: str, effect: Json, lease: str) -> str | None:
    """A new attempt for this run only; a replayed or concurrent begin is refused."""
    response = kernel.execute(
        Command(f"dispatch.{effect['effect_id']}.{len(effect['attempts'])}.{lease}",
                "begin_effect", {"effect_id": effect["effect_id"]}, wi),
        worker(()),
    )
    if response["status"] == "rejected":
        return None
    return str(response["result"]["original_request_result"]["attempt_id"])


def round_of(view: Json, candidate_id: str) -> tuple[Json, Json] | None:
    for pr in view.get("pull_requests") or []:
        for item in pr["rounds"]:
            if item["candidate_id"] == candidate_id:
                return pr, item
    return None


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


SETTLED = frozenset({"observed_success", "observed_failure", "cancelled"})


def due(snapshot: Json, now: datetime, slots: tuple[int, ...]) -> bool:
    """Whether a request may start now: immediately, or a default slot has passed."""
    submitted = snapshot.get("submitted_at")
    if not slots or snapshot.get("immediately") is True or not isinstance(submitted, str):
        return True
    hour = now.replace(minute=0, second=0, microsecond=0)
    last = max(
        start + timedelta(minutes=minute)
        for start in (hour - timedelta(hours=1), hour)
        for minute in slots
        if start + timedelta(minutes=minute) <= now
    )
    return last > instant(submitted)


def next_slot(snapshot: Json, now: datetime, slots: tuple[int, ...]) -> datetime:
    """The first instant after `now` at which a request not yet `due` becomes due."""
    submitted = snapshot.get("submitted_at")
    after = max(now, instant(submitted)) if isinstance(submitted, str) else now
    hour = after.replace(minute=0, second=0, microsecond=0)
    return min(
        start + timedelta(minutes=minute)
        for start in (hour, hour + timedelta(hours=1))
        for minute in slots
        if start + timedelta(minutes=minute) > after
    )


def open_requests(kernel: Kernel, namespace: str) -> dict[str, list[str]]:
    """`actor/runtime` -> WIs holding an open review request for it, from one read.

    Each ledger connection refreshes production Git, so the gates must not open
    one per WI, or one per Agent, merely to learn that nothing awaits review.
    """
    found: dict[str, list[str]] = {}
    with kernel._connection(write=False, collection="list") as conn:
        heads = conn.execute(
            "SELECT wi FROM journal_heads WHERE namespace=? ORDER BY wi", (namespace,)
        ).fetchall()
        for head in heads:
            try:
                state = Kernel._replay(conn, namespace, head["wi"])[0]
            except Rejected:
                continue  # dispatch() reads it again and reports nothing new.
            if state is None:
                continue
            keys = {
                f"{snapshot.get('actor')}/{snapshot.get('runtime')}"
                for effect in state.get("effects", {}).values()
                if effect["effect_kind"] == DISPATCH_KIND and effect["status"] not in SETTLED
                for snapshot in [effect.get("snapshot") or {}]
            }
            for name in keys:
                found.setdefault(name, []).append(head["wi"])
    return found


def pending(kernel: Kernel, namespace: str, actor: str, runtime: str) -> list[str]:
    """WIs holding an open review request for this Agent and runtime, from one read."""
    return open_requests(kernel, namespace).get(f"{actor}/{runtime}", [])


def dispatch(kernel: Kernel, worker: Worker, wis: list[str], actor: str, runtime: str,
             now: datetime, cooldown: timedelta, receipt_timeout: timedelta,
             lease: str | None = None, slots: tuple[int, ...] = (),
             wake: list[datetime] | None = None) -> list[Json]:
    """Advance this Agent's review requests for one runtime; start at most one.

    `wake` collects, for each request left waiting, the instant at which it could
    become startable without a ledger write; `now` means "check again at once".
    A request waiting only for a ledger write (blocked budget, superseded round)
    adds nothing.
    """
    lease = lease or uuid.uuid4().hex
    wake = [] if wake is None else wake
    for wi in wis:
        inspected = kernel.inspect(Query(wi), worker(()))
        if inspected["status"] == "rejected":
            wake.append(now)  # Not proven quiet; the next run checks again.
            continue
        view = inspected["result"]["current_observation"]
        for effect in inspected["effects"]:
            snapshot = effect.get("snapshot") or {}
            if (
                effect["effect_kind"] != DISPATCH_KIND
                or snapshot.get("actor") != actor
                or snapshot.get("runtime") != runtime
                or effect["status"] in {"observed_success", "observed_failure", "cancelled"}
            ):
                continue
            found = round_of(view, effect["candidate_id"])
            if found is None:
                continue
            pr, item = found
            current = (
                item is pr["rounds"][-1]
                and item["status"] == "awaiting_review"
                and pr["status"] != "withdrawn"
            )
            if effect["status"] in {"attempt_started", "unknown"}:
                attempt = effect["attempts"][-1]
                if item["review"] is not None:
                    observe(kernel, worker, wi, effect, attempt["id"], "success",
                            {"reason": "review_recorded", "round": item["review"]["round"]})
                    continue
                if not current:
                    observe(kernel, worker, wi, effect, attempt["id"], "failure",
                            {"reason": "round_superseded"})
                    continue
                received = attempt["id"] in (item.get("review_dispatch") or {}).get(
                    "received_attempts", [])
                limit = instant(attempt["at"]) + (cooldown if received else receipt_timeout)
                if now < limit:
                    wake.append(limit)
                    continue  # The session may still be starting or reviewing.
                observe(kernel, worker, wi, effect, attempt["id"], "absence",
                        {"reason": "no_review_after_cooldown" if received else "not_received"})
                effect = next(
                    e for e in kernel.inspect(Query(wi), worker(()))["effects"]
                    if e["effect_id"] == effect["effect_id"]
                )
            if not current:
                continue
            if not due(snapshot, now, slots):
                wake.append(next_slot(snapshot, now, slots))
                continue
            policy = retry_policy(effect)
            if policy["attempts_remaining"] == 0:
                continue
            if policy["not_before"] and now < instant(policy["not_before"]):
                wake.append(instant(policy["not_before"]))
                continue
            attempt_id = begin(kernel, worker, wi, effect, lease)
            if attempt_id is None:
                wake.append(now)
                continue
            return [{
                "work_item_id": wi,
                "candidate_id": effect["candidate_id"],
                "attempt_id": attempt_id,
                "repository": snapshot["repository"],
                "number": snapshot["number"],
                "head_sha": snapshot["head_sha"],
                "runtime": runtime,
                "compute_profile": snapshot["profile"],
                "attempt": len(effect["attempts"]) + 1,
                "commands": reviewer_commands(wi, effect["candidate_id"], attempt_id),
            }]
    return []


def reviewer_commands(wi: str, candidate: str, attempt: str) -> Json:
    """The exact receipt and score commands, so a reviewing session need not guess the
    envelope: execute reads JSON from a file or standard input and takes no --wi."""

    def command(intent: str, data: Json) -> str:
        envelope = json.dumps({"intent": intent, "work_item_id": wi, "input": data},
                              ensure_ascii=False)
        return f"harness-native execute --input - <<'JSON'\n{envelope}\nJSON"

    return {
        "declare": {"wi": wi},
        "receive": command("receive_pull_request_review",
                           {"candidate_id": candidate, "attempt_id": attempt}),
        "review": command("record_pull_request_review",
                          {"candidate_id": candidate, "score": "<0-100 integer>",
                           "reason": "<findings>"}),
    }


def exclusive(root: Path, actor: str, runtime: str) -> IO[bytes] | None:
    """The one running gate of an Agent and runtime on this installation, or None."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(root / f".pr-review-dispatch.{actor}.{runtime}.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    held = os.fdopen(descriptor, "rb")
    try:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        held.close()
        return None
    return held


def state_path(root: Path, actor: str, runtime: str) -> Path:
    """The gate's private record of its last full check, beside its lock."""
    return root / f".pr-review-dispatch.{actor}.{runtime}.state.json"


def recall(path: Path, schema: str = STATE_SCHEMA, limit: int = 4096) -> Json | None:
    """A gate record of this schema, or None when absent or unreadable."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            record = json.loads(stream.read(limit))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("schema") == schema else None


def remember(path: Path, record: Json | None) -> None:
    """Atomically replace the record, or remove it; a failure only costs a full check."""
    try:
        if record is None:
            path.unlink(missing_ok=True)
            return
        descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
    except OSError:
        pass


def quiet(record: Json | None, head: str, fingerprint: str, now: datetime) -> bool:
    """Whether the last full check proves nothing can start now at this head."""
    if record is None or record.get("head") != head or record.get("fingerprint") != fingerprint:
        return False
    try:
        checked = instant(record["checked_at"])
        if not checked <= now < checked + STATE_MAX_AGE:
            return False
        return record["next_due"] is None or now < instant(record["next_due"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


class Shared:
    """The records every gate of this installation shares, beside one shared lock."""

    INDEX_LIMIT = 1 << 20

    def __init__(self, root: Path, fingerprint: str, wait: float = SHARED_WAIT_SECONDS):
        self.root, self.fingerprint, self.wait = root, fingerprint, wait
        self.head_path = root / ".pr-review-dispatch.head.json"
        self.index_path = root / ".pr-review-dispatch.index.json"
        self.seen_path = root / ".pr-review-dispatch.seen.json"

    def hold(self) -> IO[bytes] | None:
        """The shared lock, waited for briefly; None lets a gate read alone."""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.root / ".pr-review-dispatch.shared.lock",
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        held = os.fdopen(descriptor, "rb")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return held
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    held.close()
                    return None
                time.sleep(0.2)

    def head(self, read: Callable[[], str], now: datetime) -> str:
        """The production head read by any gate within `HEAD_TTL`, or read now."""
        record = recall(self.head_path, HEAD_SCHEMA)
        try:
            if (record is not None and record.get("fingerprint") == self.fingerprint
                    and isinstance(record.get("head"), str)):
                read_at = instant(record["read_at"])
                if read_at <= now < read_at + HEAD_TTL:
                    return str(record["head"])
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        value = read()
        remember(self.head_path, {"schema": HEAD_SCHEMA, "fingerprint": self.fingerprint,
                                  "head": value, "read_at": now.isoformat()})
        return value

    def index(self, head: str, now: datetime) -> dict[str, list[str]] | None:
        """The open requests of every Agent at this head, while fresh."""
        record = recall(self.index_path, INDEX_SCHEMA, self.INDEX_LIMIT)
        if (record is None or record.get("head") != head
                or record.get("fingerprint") != self.fingerprint):
            return None
        try:
            checked = instant(record["checked_at"])
            requests = record["open"]
            if not checked <= now < checked + STATE_MAX_AGE or not isinstance(requests, dict):
                return None
            return {str(k): [str(wi) for wi in v] for k, v in requests.items()}
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    def record(self, head: str, now: datetime, requests: dict[str, list[str]]) -> None:
        remember(self.index_path, {"schema": INDEX_SCHEMA, "fingerprint": self.fingerprint,
                                   "head": head, "checked_at": now.isoformat(),
                                   "open": requests})

    def seen(self) -> tuple[datetime | None, dict[str, datetime]]:
        """Since when gate runs are recorded, and each Agent and runtime's last run."""
        record = recall(self.seen_path, SEEN_SCHEMA, self.INDEX_LIMIT)
        try:
            if record is None:
                return None, {}
            gates = {str(k): instant(v) for k, v in record["gates"].items()}
            return instant(record["since"]), gates
        except (KeyError, TypeError, ValueError, AttributeError):
            return None, {}

    def mark(self, actor: str, runtime: str, now: datetime) -> None:
        """Record that this Agent and runtime has a live gate; the caller holds the lock."""
        since, gates = self.seen()
        gates[f"{actor}/{runtime}"] = now
        remember(self.seen_path, {
            "schema": SEEN_SCHEMA, "since": (since or now).isoformat(),
            "gates": {k: v.isoformat() for k, v in sorted(gates.items())}})


def open_index(shared: Shared, now: datetime) -> dict[str, list[str]] | None:
    """The open requests of the latest valid index (any head, younger than
    `STATE_MAX_AGE`, this installation), or None when no valid index exists."""
    record = recall(shared.index_path, INDEX_SCHEMA, Shared.INDEX_LIMIT)
    try:
        if record is None or record.get("fingerprint") != shared.fingerprint:
            return None
        checked = instant(record["checked_at"])
        if not checked <= now < checked + STATE_MAX_AGE:
            return None
        return {str(k): [str(wi) for wi in v] for k, v in record["open"].items() if v}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def missing_gates(shared: Shared, now: datetime,
                  grace: timedelta = AUDIT_GRACE) -> list[Json] | None:
    """Agents and runtimes with open review requests whose gate has not run within `grace`.

    Reads only the shared records: the latest valid index and the gate runs. A
    gate never seen counts as missing only once runs have been recorded for
    longer than `grace`. None means no valid index: unknown, not proof that
    nothing is missing.
    """
    requests = open_index(shared, now)
    if requests is None:
        return None
    since, gates = shared.seen()
    found = []
    for key, wis in sorted(requests.items()):
        last = gates.get(key)
        if last is not None and now - last < grace:
            continue
        if last is None and (since is None or now - since < grace):
            continue  # Not yet observed for a full grace period.
        actor, _, runtime = key.partition("/")
        found.append({"actor": actor, "runtime": runtime, "job": f"{actor}-pr-review-{runtime}",
                      "work_items": wis, "last_gate_run": last.isoformat() if last else None})
    return found


def audit(shared: Shared, alerts: Path, now: datetime,
          grace: timedelta = AUDIT_GRACE) -> list[Json]:
    """Missing gates not reported within `AUDIT_REPEAT`; remembers what it reports.

    A report is forgotten only on proof of repair: a valid index without open
    requests for it, or a recorded run of its gate within `grace`. An unknown
    index, a missing or unreadable run record, or its warm-up keeps the report,
    so one missing gate is reported at most once per `AUDIT_REPEAT`.
    """
    found = missing_gates(shared, now, grace)
    if found is None:
        return []
    requests = open_index(shared, now) or {}
    _, gates = shared.seen()
    record = recall(alerts, AUDIT_SCHEMA) or {}
    try:
        last = {str(k): instant(v) for k, v in (record.get("reported") or {}).items()}
    except (TypeError, ValueError, AttributeError):
        last = {}

    def repaired(key: str) -> bool:
        run = gates.get(key)
        return key not in requests or (run is not None and now - run < grace)

    due_now = [m for m in found if not (
        f"{m['actor']}/{m['runtime']}" in last
        and now - last[f"{m['actor']}/{m['runtime']}"] < AUDIT_REPEAT)]
    reported = {k: v for k, v in last.items() if not repaired(k)}
    reported.update({f"{m['actor']}/{m['runtime']}": now for m in due_now})
    remember(alerts, {"schema": AUDIT_SCHEMA,
                      "reported": {k: v.isoformat() for k, v in sorted(reported.items())}})
    return due_now


def gate(open_kernel: Callable[[], Kernel], worker: Worker, namespace: str, actor: str,
         runtime: str, cooldown: timedelta, receipt_timeout: timedelta,
         slots: tuple[int, ...], *, state: Path, head: Callable[[], str], fingerprint: str,
         shared: Shared | None = None,
         clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> list[Json]:
    """One gate run: skip on an unchanged quiet head, otherwise check fully and record it.

    The record names the head read before the full check. Every read of that
    check sees this head or a later one, so the record proves quiet only while
    the head is still exactly the one read; a write during the check, including
    this run's own observations, makes the next run check fully again.

    With `shared`, the head read and the replay of all open requests are done by
    one gate for all gates, under the shared lock; a gate that cannot take it in
    time reads alone, as without `shared`.
    """
    started = clock()
    held = shared.hold() if shared is not None else None
    sharing = shared if held else None
    kernel: Kernel | None = None
    wis: list[str] | None = None
    try:
        if sharing is not None:
            sharing.mark(actor, runtime, started)
        try:
            observed: str | None = sharing.head(head, started) if sharing else head()
        except (Rejected, OSError):
            observed = None  # No cheap reading: check fully, as before this record existed.
        if observed is not None and quiet(recall(state), observed, fingerprint, started):
            return []
        if sharing is not None and observed is not None:
            requests = sharing.index(observed, started)
            if requests is None:
                kernel = open_kernel()
                requests = open_requests(kernel, namespace)
                sharing.record(observed, started, requests)
            wis = requests.get(f"{actor}/{runtime}", [])
            if not wis:
                return []  # The shared index already proves this gate quiet at this head.
    finally:
        if held:
            held.close()
    if kernel is None:
        kernel = open_kernel()
    wake: list[datetime] = []
    if wis is None:
        wis = pending(kernel, namespace, actor, runtime)
    batch = dispatch(kernel, worker, wis, actor, runtime, clock(), cooldown, receipt_timeout,
                     slots=slots, wake=wake) if wis else []
    if batch:
        remember(state, None)  # More requests may queue behind this one.
    elif observed is not None:
        remember(state, {"schema": STATE_SCHEMA, "head": observed, "fingerprint": fingerprint,
                         "checked_at": started.isoformat(),
                         "next_due": min(wake).isoformat() if wake else None})
    return batch


def installation_fingerprint(entry: Path, policy: Json) -> str:
    """The identity of this installation's shared gate records."""
    return hashlib.sha256(json.dumps(
        [str(entry), str(Path(__file__).resolve()), policy],
        sort_keys=True, default=str).encode()).hexdigest()


def audit_main(entry: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Report Agents and runtimes whose open review requests have no live gate")
    parser.add_argument("--grace-minutes", type=int, default=int(AUDIT_GRACE.total_seconds() // 60))
    args = parser.parse_args(argv)
    try:
        if not 10 <= args.grace_minutes <= 24 * 60:
            raise Rejected("invalid_input", "Audit requires a bounded grace period")
        from .effect_service import ServiceHost

        host = ServiceHost(entry)
        if "pull_request_gate" not in host.policy:
            raise Rejected("policy_read_only", "Review dispatch is not enabled in this installation")
        root = Path(host.policy["views_root"])
        found = audit(Shared(root, installation_fingerprint(entry, host.policy)),
                      root / ".pr-review-gate-audit.json", datetime.now(timezone.utc),
                      timedelta(minutes=args.grace_minutes))
    except (Rejected, ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, Rejected) else "service_unavailable"
        print(json.dumps({"status": "rejected", "error": {"code": code}}))
        return 2
    if not found:
        print(AUDIT_SKIP)
        return 3
    print(json.dumps({"schema": AUDIT_SCHEMA, "missing": found}, ensure_ascii=False,
                     sort_keys=True))
    return 0


def main(entry: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Start one pending PR review for one Agent")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--runtime", required=True, choices=["cc", "codex"])
    parser.add_argument("--cooldown-minutes", type=int, default=90)
    parser.add_argument("--receipt-minutes", type=int, default=15)
    parser.add_argument("--default-minutes", default="",
                        help="Minutes of the hour that start requests registered without "
                        "immediately, e.g. 7,22,37,52; empty starts every request at once")
    args = parser.parse_args(argv)
    try:
        identity(args.actor)
        slots = tuple(sorted({int(m) for m in args.default_minutes.split(",") if m.strip()}))
        if any(not 0 <= m <= 59 for m in slots):
            raise Rejected("invalid_input", "Default minutes must be within 0-59")
        if not 10 <= args.cooldown_minutes <= 24 * 60 or not 5 <= args.receipt_minutes <= 120:
            raise Rejected("invalid_input", "Gate requires bounded cooldown and receipt timeouts")
        from .effect_service import ServiceHost
        from .policy import execution
        from .production_ledger import ProductionLedger
        from .model import writer_identity

        host = ServiceHost(entry)
        if execution(host.policy)["mode"] == "audit" or "pull_request_gate" not in host.policy:
            raise Rejected("policy_read_only", "Review dispatch is not enabled in this installation")
        held = exclusive(Path(host.policy["views_root"]), args.actor, args.runtime)
        if held is None:
            print(SKIP)  # Another run of this job holds the lease.
            return 3
        with held:
            host.current = replace(host.current, effect_kinds=frozenset({DISPATCH_KIND}))
            ledger = ProductionLedger(host.policy["production_ledger"], host.policy["namespace"])
            root = Path(host.policy["views_root"])
            # Any change of policy or installed code invalidates the shared records;
            # any change of gate parameters also invalidates this gate's own record.
            installation = installation_fingerprint(entry, host.policy)
            fingerprint = hashlib.sha256(json.dumps(
                [args.actor, args.runtime, args.cooldown_minutes, args.receipt_minutes, slots,
                 installation], sort_keys=True).encode()).hexdigest()
            batch = gate(
                lambda: Kernel(None, host.authenticate, storage=ledger,
                               writer_id=writer_identity(host.seal[0], host.policy),
                               pages=root if host.policy.get("views_root") else None),
                host.worker, host.policy["namespace"], args.actor, args.runtime,
                timedelta(minutes=args.cooldown_minutes), timedelta(minutes=args.receipt_minutes),
                slots, state=state_path(root, args.actor, args.runtime),
                head=ledger.remote_head, fingerprint=fingerprint,
                shared=Shared(root, installation))
    except (Rejected, ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, Rejected) else "service_unavailable"
        print(json.dumps({"status": "rejected", "error": {"code": code}}))
        return 2
    if not batch:
        print(SKIP)
        return 3
    print(json.dumps({"schema": SCHEMA, "actor": args.actor, "runtime": args.runtime,
                      "items": batch}, ensure_ascii=False, sort_keys=True))
    return 0
