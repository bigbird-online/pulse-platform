"""Durable PR review requests started by the Relay review gate: lease, receipt, retry."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from harness_rewrite.model import digest
from harness_rewrite.pull_requests import DISPATCH_KIND
from harness_rewrite.review_dispatch import dispatch, due, pending
from tests_rewrite.test_pull_requests import (
    AUTHOR, NEXT, REPO, REVIEWER, WORKER, declared, drain, error, opened, original, pr, review,
    rig_with_pr, submit, view,
)

GATE_WORKER = replace(WORKER, effect_kinds=frozenset({DISPATCH_KIND}))
START = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
COOLDOWN = timedelta(minutes=90)
RECEIPT = timedelta(minutes=15)


def gate(rig, now, runtime="codex", actor=None, lease=None, slots=()):
    def worker(artifacts):
        rig.contexts["gate"] = replace(GATE_WORKER, artifacts=artifacts)
        return "gate"
    rig.kernel._clock = lambda: now.isoformat()  # Attempts carry the gate's instant.
    return dispatch(rig.kernel, worker, [rig.wi], actor or REVIEWER.actor, runtime, now,
                    COOLDOWN, RECEIPT, lease, slots)


def request(rig):
    return next(e for e in view(rig)["effects"] if e["effect_kind"] == DISPATCH_KIND
                and e["status"] != "cancelled")


def receive(rig, item, context=None):
    rig.contexts["receiver"] = declared(context or REVIEWER, [rig.wi])
    return rig.send("receive_pull_request_review",
                    {"candidate_id": item["candidate_id"], "attempt_id": item["attempt_id"]},
                    handle="receiver")


def test_registration_records_a_bound_review_request(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    effect = request(rig)
    assert effect["snapshot"] == {
        "work_item_id": rig.wi, "candidate_id": candidate, "repository": "bigbird-online/PULSE",
        "number": 7, "head_sha": "a" * 40, "actor": REVIEWER.actor, "runtime": "codex",
        "profile": "review", "minimum_effort": "high", "immediately": False,
        "submitted_at": "2026-10-07T00:00:00Z",
    }
    assert effect["status"] == "requested"


def test_two_gate_runs_on_one_stale_snapshot_start_the_review_once(tmp_path):
    # Codex repro: both runs read the same requested effect; only one may start it.
    rig, _ = rig_with_pr(tmp_path)
    stale = {**request(rig)}
    first = gate(rig, START, lease="run-a")
    from harness_rewrite.review_dispatch import begin

    def worker(artifacts):
        rig.contexts["gate"] = replace(GATE_WORKER, artifacts=artifacts)
        return "gate"
    assert len(first) == 1
    assert begin(rig.kernel, worker, rig.wi, stale, "run-b") is None
    assert gate(rig, START) == []
    # Every run takes a fresh lease, so a later run never replays an earlier begin.
    leases = set()
    for _ in range(2):
        rig.kernel._clock = lambda: START.isoformat()
        import harness_rewrite.review_dispatch as module
        leases.add(module.uuid.uuid4().hex)
    assert len(leases) == 2
    assert len(request(rig)["attempts"]) == 1


def test_one_review_per_run_and_per_wi(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    items = gate(rig, START)
    assert len(items) == 1 and items[0]["work_item_id"] == rig.wi


def test_receipt_then_cooldown_then_review_settles_the_request(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    assert gate(rig, START, runtime="cc") == []  # Another runtime's job does not start it.
    item = gate(rig, START)[0]
    assert item["attempt"] == 1 and item["head_sha"] == "a" * 40
    # The receipt binds the session: it must have declared exactly this WI first.
    undeclared = replace(REVIEWER, session_compute={**REVIEWER.session_compute, "declared_wis": []})
    assert error(receive(rig, item, undeclared)) == "reviewer_ineligible"
    original(receive(rig, item))
    assert pr(rig)["rounds"][0]["review_dispatch"]["received_attempts"] == [item["attempt_id"]]
    assert gate(rig, START + timedelta(minutes=60)) == []  # Received: wait for the review.
    other = replace(REVIEWER, session="private-other-reviewer")
    assert error(review(rig, candidate, 90, other)) == "authority_denied"
    original(review(rig, candidate, 90))
    assert gate(rig, START + timedelta(minutes=61)) == []
    effect = request(rig)
    assert effect["status"] == "observed_success"
    assert effect["observations"][-1]["details"]["reason"] == "review_recorded"


def test_crash_before_start_is_recovered_by_the_missing_receipt(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    lost = gate(rig, START)[0]  # The gate died before Relay started a session.
    assert gate(rig, START + timedelta(minutes=10)) == []
    again = gate(rig, START + RECEIPT + timedelta(minutes=1))[0]
    assert again["attempt"] == 2 and again["attempt_id"] != lost["attempt_id"]
    effect = request(rig)
    assert effect["observations"][-1]["details"]["reason"] == "not_received"
    assert error(receive(rig, lost)) == "domain_conflict"  # The old attempt cannot be received.
    original(receive(rig, again))


def test_received_but_unreviewed_is_retried_after_the_cooldown(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    original(receive(rig, gate(rig, START)[0]))
    assert gate(rig, START + RECEIPT + timedelta(minutes=1)) == []
    retried = gate(rig, START + COOLDOWN + timedelta(minutes=1))
    assert [i["attempt"] for i in retried] == [2]
    assert request(rig)["observations"][-1]["details"]["reason"] == "no_review_after_cooldown"


def test_a_new_head_cancels_or_retires_the_old_request(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    original(submit(rig, NEXT))
    effects = [e for e in view(rig)["effects"] if e["effect_kind"] == DISPATCH_KIND]
    assert [e["status"] for e in effects] == ["cancelled", "requested"]
    assert gate(rig, START)[0]["head_sha"] == NEXT
    original(submit(rig, "c" * 40))
    assert gate(rig, START + timedelta(minutes=5))[0]["head_sha"] == "c" * 40
    statuses = [e["status"] for e in view(rig)["effects"] if e["effect_kind"] == DISPATCH_KIND]
    assert statuses == ["cancelled", "observed_failure", "attempt_started"]


def test_a_review_before_the_gate_answers_the_request_and_drains_skip_it(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    assert drain(rig, None) == []  # Producer drains never execute review requests.
    original(review(rig, candidate, 90))
    assert [e["status"] for e in view(rig)["effects"] if e["effect_kind"] == DISPATCH_KIND] == [
        "cancelled"]
    assert gate(rig, START) == []


def test_exhausted_budget_names_the_block_and_the_owner_recovery(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    now, started = START, 0
    for _ in range(12):
        started += len(gate(rig, now))
        now += RECEIPT + timedelta(minutes=10)
    assert started == 8
    dispatch_state = pr(rig)["rounds"][0]["review_dispatch"]
    assert dispatch_state["blocked"]["reason"] == "review_dispatch_retry_exhausted"
    action = view(rig, "owner")["next_action"]
    assert action["intent"] == "retry_effect" and action["effect_id"] == dispatch_state["effect_id"]
    rig.kernel._clock = lambda: now.isoformat()
    owner = rig.contexts["owner"]
    rig.contexts["owner-retry"] = replace(owner, allowed_intents=owner.allowed_intents | {"retry_effect"})
    original(rig.send("retry_effect", {"effect_id": dispatch_state["effect_id"],
                                       "reason": "Relay review job repaired"}, handle="owner-retry"))
    assert len(gate(rig, now)) == 1


def test_two_gate_processes_hold_one_lease(tmp_path):
    import subprocess
    import sys
    from harness_rewrite.review_dispatch import exclusive

    held = exclusive(tmp_path, "pulse", "codex")
    assert held is not None
    code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]);"
            "from harness_rewrite.review_dispatch import exclusive;"
            "print(exclusive(Path(sys.argv[2]), 'pulse', 'codex') is None)")
    root = str(__import__("pathlib").Path(__file__).resolve().parents[1])
    other = subprocess.run([sys.executable, "-I", "-c", code, root, str(tmp_path)],
                           capture_output=True, text=True, check=True)
    assert other.stdout.strip() == "True"
    held.close()
    again = subprocess.run([sys.executable, "-I", "-c", code, root, str(tmp_path)],
                           capture_output=True, text=True, check=True)
    assert again.stdout.strip() == "False"


def test_one_attempt_binds_one_receiving_session(tmp_path):
    # Codex repro: a duplicate runtime start must not also receive the attempt.
    rig, _ = rig_with_pr(tmp_path)
    item = gate(rig, START)[0]
    original(receive(rig, item))
    original(receive(rig, item))  # The same session again is the same receipt.
    duplicate = replace(REVIEWER, session="private-duplicate-start")
    assert error(receive(rig, item, duplicate)) == "domain_conflict"
    assert len(pr(rig)["rounds"][0]["review_dispatch"]["received_attempts"]) == 1


def test_only_the_current_attempt_receiver_may_review(tmp_path):
    # Codex repro: after a re-dispatch the old receiver could still score.
    rig, candidate = rig_with_pr(tmp_path)
    first = gate(rig, START)[0]
    original(receive(rig, first))
    second = gate(rig, START + COOLDOWN + timedelta(minutes=1))[0]
    newer = replace(REVIEWER, session="private-second-reviewer")
    original(receive(rig, second, newer))
    assert error(review(rig, candidate, 90)) == "authority_denied"  # The first receiver.
    assert original(review(rig, candidate, 90, newer))["passed"]


def test_a_dispatched_round_without_receipt_cannot_be_scored_directly(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    item = gate(rig, START)[0]
    assert error(review(rig, candidate, 90)) == "authority_denied"
    original(receive(rig, item))
    assert original(review(rig, candidate, 90))["passed"]


def test_a_withdrawn_registration_stops_its_dispatch_and_receipts(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    item = gate(rig, START)[0]
    original(rig.send("withdraw_pull_request",
                      {"repository": "bigbird-online/PULSE", "number": 7, "reason": "Wrong WI"}))
    assert error(receive(rig, item)) == "domain_conflict"
    assert gate(rig, START + COOLDOWN + timedelta(minutes=1)) == []
    assert request(rig)["status"] == "observed_failure"


def test_one_read_finds_only_wis_with_an_open_request_for_this_agent_and_runtime(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    namespace = REVIEWER.namespace
    assert pending(rig.kernel, namespace, REVIEWER.actor, "codex") == [rig.wi]
    assert pending(rig.kernel, namespace, REVIEWER.actor, "cc") == []
    assert pending(rig.kernel, namespace, "someone-else", "codex") == []


def test_default_request_waits_for_a_slot_and_immediate_starts_at_once(tmp_path):
    # The job runs every minute; :07 is this job's default slot.
    rig, _ = rig_with_pr(tmp_path)
    assert gate(rig, START + timedelta(minutes=5), slots=(7,)) == []
    assert len(gate(rig, START + timedelta(minutes=7), slots=(7,))) == 1
    (tmp_path / "fast").mkdir()
    fast, _ = rig_with_pr(tmp_path / "fast")
    original(submit(fast, NEXT, immediately=True))
    assert request(fast)["snapshot"]["immediately"] is True
    assert pr(fast)["rounds"][-1]["immediately"] is True
    assert len(gate(fast, START + timedelta(minutes=1), slots=(7,))) == 1


def test_missed_slot_still_counts_and_old_requests_are_due(tmp_path):
    submitted = {"submitted_at": "2026-10-07T00:00:00Z"}
    later = START + timedelta(hours=2, minutes=3)  # The :07 run of 01:07 was missed.
    assert due(submitted, later, (7,))
    assert not due(submitted, START + timedelta(minutes=6, seconds=59), (7,))
    assert due({}, START, (7,))  # Registered before this field existed.
    assert due(submitted, START, ())  # No default slots: every request is due.


def test_immediately_must_be_boolean(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    assert error(submit(rig, NEXT, immediately="yes")) == "invalid_input"


class Run:
    """The gate entry against a fixture ledger whose head is its journal heads digest."""

    def __init__(self, rig, tmp_path, runtime="codex", slots=(), actor=None, shared=None):
        self.rig, self.runtime, self.slots, self.shared = rig, runtime, slots, shared
        self.actor = actor or REVIEWER.actor
        self.state = tmp_path / f".pr-review-dispatch.{self.actor}.{runtime}.state.json"
        self.opened = 0  # Full checks: each opens the ledger (in production, a Git fetch).
        self.heads = 0  # Head reads: in production, one `git ls-remote` each.

    def head(self):
        self.heads += 1
        with self.rig.kernel._connection(write=False) as conn:
            rows = conn.execute("SELECT wi, revision, digest FROM journal_heads ORDER BY wi")
            return digest([list(row) for row in rows])

    def __call__(self, now, fingerprint="fixed"):
        from harness_rewrite.review_dispatch import gate as run

        def worker(artifacts):
            self.rig.contexts["gate"] = replace(GATE_WORKER, artifacts=artifacts)
            return "gate"

        def open_kernel():
            self.opened += 1
            return self.rig.kernel
        self.rig.kernel._clock = lambda: now.isoformat()
        return run(open_kernel, worker, REVIEWER.namespace, self.actor, self.runtime,
                   COOLDOWN, RECEIPT, self.slots, state=self.state, head=self.head,
                   fingerprint=fingerprint + self.actor + self.runtime, shared=self.shared,
                   clock=lambda: now)


def test_an_unchanged_quiet_head_skips_without_reading_the_ledger(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    run = Run(rig, tmp_path, runtime="cc")  # Nothing awaits this runtime.
    assert run(START) == [] and run.opened == 1
    for minute in range(1, 40):
        assert run(START + timedelta(minutes=minute)) == []
    assert run.opened == 2  # Only the record's age bound forced one more full check.
    assert run(START + timedelta(minutes=40), fingerprint="new parameters") == []
    assert run.opened == 3


def test_a_changed_head_checks_fully_and_a_new_registration_starts_next_run(tmp_path):
    rig, candidate = rig_with_pr(tmp_path)
    run = Run(rig, tmp_path)
    original(receive(rig, run(START)[0]))
    original(review(rig, candidate, 90))
    assert run(START + timedelta(minutes=1)) == []  # Settles the answered request.
    assert run(START + timedelta(minutes=2)) == []
    opened = run.opened
    assert run(START + timedelta(minutes=3)) == [] and run.opened == opened
    original(submit(rig, NEXT, immediately=True))  # Registration writes the ledger head.
    items = run(START + timedelta(minutes=4))
    assert [i["head_sha"] for i in items] == [NEXT] and run.opened == opened + 1


def test_a_waiting_request_skips_until_its_slot_then_starts(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    run = Run(rig, tmp_path, slots=(7,))
    assert run(START + timedelta(minutes=1)) == [] and run.opened == 1
    for minute in range(2, 7):
        assert run(START + timedelta(minutes=minute)) == []
    assert run.opened == 1
    assert len(run(START + timedelta(minutes=7))) == 1 and run.opened == 2


def test_receipt_timeout_and_cooldown_bound_the_skip(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    run = Run(rig, tmp_path)
    lost = run(START)[0]
    assert run(START + timedelta(minutes=1)) == []  # Own write: the next run checks fully.
    opened = run.opened
    assert run(START + RECEIPT - timedelta(minutes=1)) == [] and run.opened == opened
    again = run(START + RECEIPT)
    assert again[0]["attempt"] == 2 and again[0]["attempt_id"] != lost["attempt_id"]
    original(receive(rig, again[0]))
    assert run(START + RECEIPT + timedelta(minutes=1)) == []
    opened = run.opened
    assert run(START + RECEIPT + timedelta(minutes=20)) == [] and run.opened == opened
    # The record's age bound may re-check before the cooldown; none starts early.
    assert run(START + RECEIPT + COOLDOWN - timedelta(seconds=1)) == []
    retried = run(START + RECEIPT + COOLDOWN)
    assert [i["attempt"] for i in retried] == [3]


def test_an_unreadable_record_or_head_checks_fully(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    run = Run(rig, tmp_path, runtime="cc")
    run(START)
    run.state.write_text("{not json")
    assert run(START + timedelta(minutes=1)) == [] and run.opened == 2
    assert run(START + timedelta(minutes=2)) == [] and run.opened == 2
    run.state.write_text('{"schema": "pulse.pr_review_gate_state/v1", "head": 7}')
    assert run(START + timedelta(minutes=3)) == [] and run.opened == 3

    def unavailable():
        from harness_rewrite.model import Rejected
        raise Rejected("ledger_unavailable", "offline")
    run.head = unavailable
    assert run(START + timedelta(minutes=4)) == [] and run.opened == 4
    assert run(START + timedelta(minutes=5)) == [] and run.opened == 5


def test_a_clock_before_the_record_checks_fully(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    run = Run(rig, tmp_path, runtime="cc")
    run(START)
    assert run(START - timedelta(minutes=1)) == [] and run.opened == 2


def test_next_slot_is_the_first_slot_after_now_and_registration():
    from harness_rewrite.review_dispatch import next_slot
    submitted = {"submitted_at": "2026-10-07T00:00:00Z"}
    assert next_slot(submitted, START + timedelta(minutes=3), (7, 52)) == START + timedelta(minutes=7)
    assert next_slot(submitted, START + timedelta(minutes=53), (7, 52)) == START + timedelta(minutes=67)
    assert next_slot({"submitted_at": "2026-10-07T00:08:00Z"}, START, (7,)) == START + timedelta(minutes=67)
    assert not due(submitted, next_slot(submitted, START, (7,)) - timedelta(seconds=1), (7,))
    assert due(submitted, next_slot(submitted, START, (7,)), (7,))


AGENTS = ("hestia", "amanda", "aki", "ken", "duke", "vigil")


def fleet(rig, tmp_path, wait=1.0):
    """Every Agent's pair of gates on one installation, sharing one head and one index."""
    from harness_rewrite.review_dispatch import Shared
    shared = Shared(tmp_path, "installation", wait=wait)
    runs = [Run(rig, tmp_path, runtime, actor=actor, shared=shared)
            for actor in (REVIEWER.actor, *AGENTS) for runtime in ("codex", "cc")]
    return shared, runs


def test_one_head_read_and_one_replay_serve_every_agent(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    started = [item for run in runs for item in run(START)]
    # Only the author's own Agent, in the author's opposite family, starts the review.
    assert [(i["work_item_id"], i["runtime"]) for i in started] == [(rig.wi, "codex")]
    assert sum(r.heads for r in runs) == 1 and sum(r.opened for r in runs) == 1
    # The start wrote the ledger: a minute later one gate re-reads the head and
    # replays once for all; the others reuse both.
    later = START + timedelta(minutes=1)
    assert [item for run in reversed(runs) for item in run(later)] == []
    assert sum(r.heads for r in runs) == 2
    assert sum(r.opened for r in runs) == 3  # One shared replay and the waiting gate's check.
    index = __import__("json").loads(shared.index_path.read_text())
    assert index["open"] == {f"{REVIEWER.actor}/codex": [rig.wi]}
    # Quiet minutes read the head once and nothing else.
    for minute in range(2, 6):
        for run in runs:
            assert run(START + timedelta(minutes=minute)) == []
    assert sum(r.heads for r in runs) == 6 and sum(r.opened for r in runs) == 3


def test_another_agents_gate_never_starts_this_agents_review(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    others = [r for r in runs if r.actor != REVIEWER.actor]
    assert [item for run in others for item in run(START)] == []
    assert sum(r.opened for r in others) == 1  # One replay; its index answers the rest.
    assert request(rig)["attempts"] == []
    mine = next(r for r in runs if r.actor == REVIEWER.actor and r.runtime == "codex")
    assert len(mine(START)) == 1 and mine.opened == 1  # The index named it: check and start.


def test_a_stale_or_unreadable_index_is_replayed_again(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    quiet_run = next(r for r in runs if r.actor == "hestia")
    quiet_run(START)
    shared.index_path.write_text("{not json")
    quiet_run(START + timedelta(minutes=1))
    assert quiet_run.opened == 2
    shared.record("another head", START, {})
    quiet_run(START + timedelta(minutes=2))
    assert quiet_run.opened == 3
    # Past the age bound the index is replayed again even at the same head.
    quiet_run(START + timedelta(minutes=40))
    assert quiet_run.opened == 4


def test_a_gate_that_cannot_take_the_shared_lock_reads_alone(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path, wait=0)
    other = shared.hold()  # Another gate is inside its shared read.
    assert other is not None
    mine = next(r for r in runs if r.actor == REVIEWER.actor and r.runtime == "codex")
    assert len(mine(START)) == 1 and mine.heads == 1
    assert not shared.head_path.exists() and not shared.index_path.exists()
    other.close()


def test_the_shared_head_expires_within_one_schedule_minute(tmp_path):
    from harness_rewrite.review_dispatch import HEAD_TTL
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    assert HEAD_TTL < timedelta(minutes=1)
    assert shared.head(lambda: "h1", START) == "h1"
    assert shared.head(lambda: "h2", START + HEAD_TTL - timedelta(seconds=1)) == "h1"
    assert shared.head(lambda: "h3", START + HEAD_TTL) == "h3"
    assert shared.head(lambda: "h4", START - timedelta(seconds=1)) == "h4"  # Clock went back.
    other = __import__("harness_rewrite.review_dispatch", fromlist=["Shared"]).Shared(
        tmp_path, "another installation")
    assert other.head(lambda: "h5", START + HEAD_TTL) == "h5"


def open_prs(rig, heads):
    """Register further PRs (8, 9, ...) on the rig's WI; each gets its own review request."""
    for number, head in enumerate(heads, start=8):
        rig.contexts["registrant"] = replace(AUTHOR, pull_request_observation=opened(head, number=number))
        original(rig.send("submit_pull_request",
                          {"repository": REPO, "number": number, "head_sha": head},
                          handle="registrant"))


def test_successive_runs_start_every_pr_once_while_earlier_reviews_run(tmp_path):
    # Relay runs up to `maxConcurrent` of these at once; each run starts one new review.
    rig, _ = rig_with_pr(tmp_path)
    open_prs(rig, ["c" * 40, "d" * 40, "e" * 40])
    started = [gate(rig, START + timedelta(minutes=m)) for m in range(5)]
    assert [len(items) for items in started] == [1, 1, 1, 1, 0]
    numbers = [items[0]["number"] for items in started[:4]]
    assert sorted(numbers) == [7, 8, 9, 10]
    requests = [e for e in view(rig)["effects"] if e["effect_kind"] == DISPATCH_KIND]
    assert [(e["status"], len(e["attempts"])) for e in requests] == [("attempt_started", 1)] * 4


def test_one_lost_attempt_restarts_alone_while_the_others_keep_running(tmp_path):
    rig, _ = rig_with_pr(tmp_path)
    open_prs(rig, ["c" * 40, "d" * 40])
    lost, *running = [gate(rig, START + timedelta(minutes=m))[0] for m in range(3)]
    for item in running:
        original(receive(rig, item))
    again = gate(rig, START + RECEIPT + timedelta(minutes=1))
    assert [(i["candidate_id"], i["attempt"]) for i in again] == [(lost["candidate_id"], 2)]
    assert gate(rig, START + RECEIPT + timedelta(minutes=2)) == []
    by_candidate = {e["candidate_id"]: e for e in view(rig)["effects"]
                    if e["effect_kind"] == DISPATCH_KIND}
    assert [len(by_candidate[i["candidate_id"]]["attempts"]) for i in running] == [1, 1]


def test_an_agent_without_a_gate_is_reported_missing_after_the_grace(tmp_path):
    # Marsh 11-06 and Steward 11-13: a request whose Agent has no review job waited
    # silently. Every other gate runs; the audit names the missing job.
    from harness_rewrite.review_dispatch import AUDIT_GRACE, missing_gates
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    others = [r for r in runs if r.actor != REVIEWER.actor]
    for minute in range(0, 26, 5):
        for run in others:
            run(START + timedelta(minutes=minute))
    assert missing_gates(shared, START + timedelta(minutes=25)) == []  # Within the grace.
    later = START + AUDIT_GRACE + timedelta(minutes=1)
    for run in others:
        run(later)
    found = missing_gates(shared, later)
    assert found == [{"actor": REVIEWER.actor, "runtime": "codex",
                      "job": f"{REVIEWER.actor}-pr-review-codex",
                      "work_items": [rig.wi], "last_gate_run": None}]
    # The request was neither attempted nor charged a retry.
    assert request(rig)["attempts"] == []


def test_a_registered_gate_clears_the_report_and_starts_the_review(tmp_path):
    from harness_rewrite.review_dispatch import AUDIT_GRACE, missing_gates
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    later = START + AUDIT_GRACE + timedelta(minutes=1)
    for now in (START, later):
        for run in runs:
            if run.actor != REVIEWER.actor:
                run(now)
    assert len(missing_gates(shared, later)) == 1
    mine = next(r for r in runs if r.actor == REVIEWER.actor and r.runtime == "codex")
    assert len(mine(later)) == 1  # The newly registered job starts the request at once.
    assert missing_gates(shared, later) == []


def test_a_live_gate_with_a_waiting_request_is_not_missing(tmp_path):
    from harness_rewrite.review_dispatch import missing_gates
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    mine = next(r for r in runs if r.actor == REVIEWER.actor and r.runtime == "codex")
    mine.slots = (37,)  # The request waits for its default slot; the gate still runs.
    for minute in range(0, 36):
        for run in runs:
            run(START + timedelta(minutes=minute))
    assert request(rig)["attempts"] == []
    assert missing_gates(shared, START + timedelta(minutes=35)) == []


def test_a_gate_that_stopped_running_is_reported_with_its_last_run(tmp_path):
    from harness_rewrite.review_dispatch import AUDIT_GRACE, missing_gates
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    mine = next(r for r in runs if r.actor == REVIEWER.actor and r.runtime == "codex")
    mine.slots = (59,)
    for run in runs:
        run(START)
    later = START + AUDIT_GRACE + timedelta(minutes=1)
    for run in runs:
        if run is not mine:
            run(later)
    found = missing_gates(shared, later)
    assert [m["last_gate_run"] for m in found] == [START.isoformat()]


def test_the_audit_reports_each_missing_gate_once_per_interval(tmp_path):
    from harness_rewrite.review_dispatch import AUDIT_GRACE, AUDIT_REPEAT, audit
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    alerts = tmp_path / "alerts.json"
    times = [START, START + AUDIT_GRACE + timedelta(minutes=1)]
    for now in times:
        for run in runs:
            if run.actor != REVIEWER.actor:
                run(now)
    assert len(audit(shared, alerts, times[1])) == 1
    assert audit(shared, alerts, times[1] + timedelta(minutes=15)) == []
    again = times[1] + AUDIT_REPEAT
    for run in runs:
        if run.actor != REVIEWER.actor:
            run(again)
    assert len(audit(shared, alerts, again)) == 1


def test_an_unreadable_or_foreign_index_is_unknown(tmp_path):
    from harness_rewrite.review_dispatch import AUDIT_GRACE, Shared, missing_gates
    shared = Shared(tmp_path, "installation")
    later = START + AUDIT_GRACE + timedelta(minutes=1)
    assert missing_gates(shared, later) is None
    shared.index_path.write_text("{not json")
    assert missing_gates(shared, later) is None
    Shared(tmp_path, "another installation").record("h", later, {"x/codex": ["wi_1"]})
    assert missing_gates(shared, later) is None


def test_an_expired_or_broken_index_keeps_the_report_until_repair_is_proven(tmp_path):
    # Review round 1: an index that just expired was taken as "nothing missing",
    # cleared the report, and the same missing gate alerted again 30 minutes later.
    from harness_rewrite.review_dispatch import AUDIT_GRACE, STATE_MAX_AGE, audit
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    others = [r for r in runs if r.actor != REVIEWER.actor]
    alerts = tmp_path / "alerts.json"
    first = START + AUDIT_GRACE + timedelta(minutes=1)
    for now in (START, first):
        for run in others:
            run(now)
    assert len(audit(shared, alerts, first)) == 1
    index = shared.index_path.read_text()
    expired = first + STATE_MAX_AGE
    assert audit(shared, alerts, expired) == []  # Unknown: no report, none forgotten.
    shared.index_path.write_text("{not json")
    assert audit(shared, alerts, expired + timedelta(seconds=1)) == []
    shared.index_path.write_text(index.replace(
        __import__("json").loads(index)["checked_at"], expired.isoformat()))
    assert audit(shared, alerts, expired + timedelta(seconds=2)) == []  # Still within 6 h.
    # A valid index proving the gate now runs forgets the report.
    mine = next(r for r in runs if r.actor == REVIEWER.actor and r.runtime == "codex")
    repaired = expired + timedelta(minutes=1)
    mine(repaired)
    for run in others:
        run(repaired)
    assert audit(shared, alerts, repaired) == []
    assert __import__("json").loads(alerts.read_text())["reported"] == {}


def test_a_lost_or_broken_run_record_keeps_the_report(tmp_path):
    # Review round 2: deleting the run record made every unseen gate warm up again,
    # cleared the report, and the same missing gate alerted again 33 minutes later.
    from harness_rewrite.review_dispatch import AUDIT_GRACE, audit
    rig, _ = rig_with_pr(tmp_path)
    shared, runs = fleet(rig, tmp_path)
    others = [r for r in runs if r.actor != REVIEWER.actor]
    alerts = tmp_path / "alerts.json"
    first = START + AUDIT_GRACE + timedelta(minutes=1)
    for now in (START, first):
        for run in others:
            run(now)
    assert len(audit(shared, alerts, first)) == 1
    for damage in ("delete", "corrupt"):
        if damage == "delete":
            shared.seen_path.unlink()
        else:
            shared.seen_path.write_text("{not json")
        now = first + timedelta(minutes=1)
        assert audit(shared, alerts, now) == []
        assert list(__import__("json").loads(alerts.read_text())["reported"]) == [
            f"{REVIEWER.actor}/codex"]
    # Other gates rebuild the record; past its warm-up the same gate is due again
    # by the record, but its report is not repeated within the interval.
    for minutes in (2, 33):
        for run in others:
            run(first + timedelta(minutes=minutes))
    assert audit(shared, alerts, first + timedelta(minutes=33)) == []


def test_the_batch_carries_the_exact_reviewer_commands(tmp_path):
    import json

    rig, candidate = rig_with_pr(tmp_path)
    item = gate(rig, START)[0]
    commands = item["commands"]
    assert commands["declare"] == {"wi": rig.wi}
    receive_command = commands["receive"]
    assert receive_command.startswith("harness-native execute --input - <<'JSON'\n")
    assert "--wi" not in receive_command
    envelope = json.loads(receive_command.split("\n")[1])
    assert envelope == {"intent": "receive_pull_request_review", "work_item_id": rig.wi,
                        "input": {"candidate_id": candidate, "attempt_id": item["attempt_id"]}}
    scored = json.loads(commands["review"].split("\n")[1])
    assert scored["intent"] == "record_pull_request_review"
    assert set(scored["input"]) == {"candidate_id", "score", "reason"}
