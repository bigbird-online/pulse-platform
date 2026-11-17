"""A Pi session's own commits: read from its native record, proven by the workspace HEAD log."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from harness_rewrite.model import native_session_ref
from harness_rewrite.session_compute import find_authors, observe, verify_commits
from tests_rewrite.test_lightweight import light
from tests_rewrite.test_pull_request_host import commit_now, committed_repository, git
from tests_rewrite.test_pull_requests import AUTHOR, GATE, opened, submit
from tests_rewrite.test_workflow import error, original

SESSION = "0b1c2d3e-0000-4000-8000-00000000a11d"
OTHER = "0b1c2d3e-0000-4000-8000-00000000b0b0"
MODEL = "gpt-6.1-sol"
COMPUTE_COST = Path(__file__).resolve().parents[2] / "ComputeCost"


def ack(wi):
    return json.dumps({"schema": "pi_request_wi/v1", "wi": wi, "origin": "agent_declaration"})


class Pi:
    """A synthetic Pi session record: header, model and thinking level, then messages."""

    def __init__(self, session=SESSION, cwd="/synthetic", start=None):
        self.at = start or datetime(2026, 10, 17, 5, 0, tzinfo=timezone.utc)
        self.session, self.last, self.count = session, None, 0
        self.rows = [{"type": "session", "version": 3, "id": session,
                      "timestamp": self.stamp(), "cwd": cwd}]
        self.entry({"type": "model_change", "provider": "openai-codex", "modelId": MODEL})
        self.entry({"type": "thinking_level_change", "thinkingLevel": "medium"})

    def stamp(self, at=None):
        return (at or self.at).isoformat().replace("+00:00", "Z")

    def entry(self, row, at=None):
        self.count += 1
        ident = f"e{self.count:04d}"
        self.rows.append({**row, "id": ident, "parentId": self.last, "timestamp": self.stamp(at)})
        self.last = ident

    def message(self, message, at=None):
        self.entry({"type": "message", "message": message}, at)

    def user(self):
        self.message({"role": "user", "content": [{"type": "text", "text": "synthetic request"}]})

    def assistant(self, *calls, at=None, model=MODEL):
        self.message({"role": "assistant", "model": model, "provider": "openai-codex",
                      "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0,
                                "totalTokens": 2},
                      "content": [{"type": "toolCall", "id": i, "name": n, "arguments": a}
                                  for i, n, a in calls] or [{"type": "text", "text": "work"}]}, at)

    def result(self, call, name, text, at=None, failed=False):
        self.message({"role": "toolResult", "toolCallId": call, "toolName": name,
                      "content": [{"type": "text", "text": text}], "isError": failed}, at)

    def declare(self, call, wi, failed=False, text=None):
        self.assistant((call, "declare_session_wi", {"wi": wi}))
        self.result(call, "declare_session_wi", text or ack(wi), failed=failed)

    def bash(self, call, command, start, end, failed=False, name="bash"):
        self.assistant((call, name, {"command": command, "timeout": 120}), at=start)
        self.result(call, name, "", end, failed)

    def fork(self, session=OTHER, at=None):
        """Pi's SessionManager.forkFrom: a new header naming the parent, then every
        parent entry copied unchanged (ids, parents and times included)."""
        child = Pi.__new__(Pi)
        child.at, child.session = at or datetime.now(timezone.utc), session
        child.last, child.count = self.last, self.count
        child.rows = [{"type": "session", "version": 3, "id": session,
                       "timestamp": child.stamp(), "cwd": self.rows[0]["cwd"],
                       "parentSession": "/sessions/parent.jsonl"}, *self.rows[1:]]
        return child

    def write(self, root, name=None):
        root.mkdir(parents=True, exist_ok=True)
        path = root / (name or f"2026-10-17T05-00-00-000Z_{self.session}.jsonl")
        path.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        return path


def test_pi_record_yields_turns_commit_windows_and_declarations(tmp_path):
    pi = Pi(cwd="/work")
    pi.user()
    pi.declare("d1", "wi_20261015_422")
    pi.declare("d2", "none")
    pi.declare("d3", "wi_20261015_999", failed=True)  # A refused call declares nothing.
    pi.declare("d4", "wi_20261015_998", text="ok")  # Nor does one without the extension's ack.
    pi.declare("d6", "wi_20261015_996", text=json.dumps(  # Nor another schema's.
        {"schema": "wrong/v1", "wi": "wi_20261015_996", "origin": "agent_declaration"}))
    pi.assistant(("d5", "declare_session_wi", {"wi": "wi_20261015_997", "why": "x"}))
    pi.result("d5", "declare_session_wi", ack("wi_20261015_997"))  # Extra arguments: not counted.
    t0, t1 = pi.at, pi.at + timedelta(seconds=5)
    pi.bash("c1", "cd /work/repo && git commit -q -m x", t0, t1)
    pi.bash("c2", "git commit -m y", t0, t1, failed=True)  # A failed call claims nothing.
    pi.bash("c3", "git commit -m z", t0, t1, name="read")  # Not a command execution.
    pi.assistant(("c4", "bash", {"command": "git commit -m w"}))
    pi.result("c4", "read", "")  # A result naming another tool is not this call's.
    path = pi.write(tmp_path)
    observed = observe("pi", path)
    assert observed["native_id"] == SESSION and observed["runtime"] == "pi"
    assert observed["turns"] == [{"model": MODEL, "effort": "medium"}]
    assert observed["declared_wis"] == ["wi_20261015_422"]
    assert observed["commits"] == [{
        "sha": None, "printed": [], "model": MODEL, "repository": "/work/repo",
        "started_at": pi.stamp(t0), "ended_at": pi.stamp(t1), "verified": False, "tool_use_id": "c1"}]
    assert "subagents" not in observed


def test_a_pi_record_without_its_header_first_is_unusable(tmp_path):
    pi = Pi()
    pi.rows = pi.rows[1:] + pi.rows[:1]
    assert observe("pi", pi.write(tmp_path)) is None
    pi = Pi()
    pi.rows.append({"type": "session", "id": OTHER})
    assert observe("pi", pi.write(tmp_path)) is None


def gate(tmp_path):
    """A lightweight WI with the PR gate, in its own directory."""
    (tmp_path / "rig").mkdir()
    rig = light(tmp_path / "rig")
    rig.contexts["owner"] = GATE
    return rig


def pi_commit(tmp_path, repo, session=SESSION, command=None, wi=None, shift=timedelta(0)):
    """A Pi session that ran the commit `committed_repository` just made."""
    _, head, before, after = repo
    pi = Pi(session, cwd=str(tmp_path))
    pi.user()
    if wi:
        pi.declare("d1", wi)
    pi.bash("c1", command or f"cd {repo[0]} && git commit -q -m x",
            before + shift, after + shift)
    return pi


def test_the_pi_session_that_ran_the_commit_registers_its_pull_request(tmp_path):
    repo = committed_repository(tmp_path)
    head = repo[1]
    rig = gate(tmp_path)
    path = pi_commit(tmp_path, repo, wi=rig.wi).write(tmp_path / "sessions")
    compute = verify_commits(observe("pi", path), head)
    assert [(c["sha"], c["verified"]) for c in compute["commits"]] == [(head, True)]
    candidate = original(submit(rig, head, replace(AUTHOR, session_compute=compute),
                                observed=opened(head)))
    assert candidate["candidate_id"]


@pytest.mark.parametrize("case", ["outside_window", "other_workspace", "printed_only"])
def test_a_pi_session_cannot_claim_a_commit_it_did_not_create(tmp_path, case):
    repo = committed_repository(tmp_path)
    head = repo[1]
    elsewhere = tmp_path / "elsewhere"
    git(tmp_path, "init", "-q", "-b", "main", str(elsewhere))
    pi = {
        # The same command, ten minutes after the commit happened.
        "outside_window": lambda: pi_commit(tmp_path, repo, shift=timedelta(minutes=10)),
        # A commit in another workspace in the same second.
        "other_workspace": lambda: pi_commit(
            tmp_path, repo, command=f"cd {elsewhere} && git commit -q -m x"),
        # Echoed commit text in a committing-looking command that never ran git commit.
        "printed_only": lambda: pi_commit(
            tmp_path, repo, command=f"printf '[main {head[:7]}] x' # git commit",
            shift=timedelta(minutes=10)),
    }[case]()
    rig = gate(tmp_path)
    compute = verify_commits(observe("pi", pi.write(tmp_path / "sessions")), head)
    assert [c["verified"] for c in compute["commits"]] == [False]
    refused = submit(rig, head, replace(AUTHOR, session_compute=compute), observed=opened(head))
    assert error(refused) == "source_unavailable"


def test_another_pi_session_registering_the_commit_is_refused(tmp_path):
    repo = committed_repository(tmp_path)
    head = repo[1]
    rig = gate(tmp_path)
    registrant = Pi(OTHER, cwd=str(repo[0]))
    registrant.user()
    registrant.declare("d1", rig.wi)
    compute = verify_commits(observe("pi", registrant.write(tmp_path / "sessions")), head)
    refused = submit(rig, head, replace(AUTHOR, session_compute=compute), observed=opened(head))
    assert error(refused) == "source_unavailable"


def test_pi_records_are_searched_at_their_own_place_only(tmp_path):
    repo = committed_repository(tmp_path)
    head = repo[1]
    root = tmp_path / "pi-root"
    pi_commit(tmp_path, repo).write(root)
    # The Pi process's HOME lies inside a Relay profile root: nothing there is a record.
    pi_commit(tmp_path, repo, OTHER).write(root / "agent" / ".pi" / "sessions")
    pi_commit(tmp_path, repo, OTHER).write(root / "agent")
    # A file whose name names another session than its header is not that session.
    pi_commit(tmp_path, repo, OTHER).write(root, f"2026-10-17T05-00-00-000Z_{SESSION}x.jsonl")
    found = find_authors([("pi", root)], [head], 0)
    assert found == {head: [{"native_id": SESSION, "runtime": "pi", "model": MODEL,
                             "declared_wis": [], "wi_marker": None}]}


def test_host_searches_the_agent_pi_roots_among_its_session_roots(tmp_path):
    from harness_rewrite import session_compute
    from harness_rewrite.native_host import NativeHost

    repo = committed_repository(tmp_path)
    head = repo[1]
    root = tmp_path / "pi-root"
    pi_commit(tmp_path, repo, wi="wi_20261015_422").write(root)
    host = NativeHost.__new__(NativeHost)
    host.profile = {"actor": "aki", "role": "producer"}
    host.policy = {"namespace": "team", "surface": "tcloud", "profiles": [
        # A Relay Pi profile names no runtime; the same root twice is read once.
        {"actor": "aki", "role": "producer", "session_root": str(root)},
        {"actor": "aki", "role": "producer", "runtime": "pi", "session_root": str(root)},
        {"actor": "other", "role": "producer", "session_root": str(tmp_path)},
    ]}
    assert host.pull_request_authors([head], session_compute) == [{
        "sha": head, "session": native_session_ref("team", "aki", SESSION, "tcloud"),
        "model": MODEL, "declared_wis": ["wi_20261015_422"], "wi_marker": None}]


def test_a_later_pi_commit_in_a_linked_worktree_is_confirmed(tmp_path):
    repo, _, _, _ = committed_repository(tmp_path)
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    head = commit_now(worktree, "-m", "y")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    pi = Pi(cwd=str(repo))
    pi.bash("c1", f"cd {worktree} && git commit -q -m y", before, after)
    compute = verify_commits(observe("pi", pi.write(tmp_path / "s")), head)
    assert [(c["repository"], c["verified"]) for c in compute["commits"]] == [(str(worktree), True)]


# The kernel admits a Pi author only when ComputeCost charges none of its usage elsewhere.

WI, OTHER_WI = "wi_20261017_150", "wi_20261017_151"


@pytest.fixture
def cost(monkeypatch):
    if not (COMPUTE_COST / "pulse_compute_cost").is_dir():
        pytest.skip("ComputeCost source unavailable")
    monkeypatch.syspath_prepend(str(COMPUTE_COST))
    from pulse_compute_cost import pi_request_purpose
    from pulse_compute_cost.receipt_sources import read_pi
    return pi_request_purpose, read_pi


def claims(cost, path):
    """Every (state, WI) ComputeCost assigns to this Pi session's usage."""
    purpose, read_pi = cost
    observed = "2026-10-18T00:00:00Z"
    usages = read_pi(path, observed)
    bindings = purpose.request_bindings([path], since="2026-10-01T00:00:00Z")
    return {(r.payload["state"], r.payload["wi"]) for r in purpose.request_attributions(
        [], usages, bindings, observed)}


@pytest.mark.parametrize("requests, admitted, charged", [
    ([WI], True, {("assigned", WI)}),
    # Chat or read-only first: ComputeCost keeps it non-WI, the kernel ignores `none`.
    (["none", WI], True, {("assigned", WI), ("assigned", None)}),
    # An undeclared request leaves its usage unclaimed; the declared one is the WI's.
    ([None, WI], True, {("assigned", WI)}),
    # Two WIs: ComputeCost charges part of the session elsewhere, so the kernel refuses.
    ([WI, OTHER_WI], False, {("assigned", WI), ("assigned", OTHER_WI)}),
])
def test_pi_registration_agrees_with_compute_cost_attribution(cost, tmp_path, requests,
                                                              admitted, charged):
    pi = Pi()
    for number, wi in enumerate(requests):
        pi.user()
        if wi is not None:
            pi.declare(f"d{number}", wi)
        pi.assistant()
    path = pi.write(tmp_path)
    assert claims(cost, path) == charged
    declared = observe("pi", path)["declared_wis"]
    assert (declared == [WI]) is admitted
    if not admitted:
        rig = gate(tmp_path)
        several = replace(AUTHOR, session_compute={**AUTHOR.session_compute, "declared_wis": declared})
        refused = submit(rig, "a" * 40, several)
        assert error(refused) == "domain_conflict" and "exactly one WI" in refused["error"]["reason"]


def test_a_declaration_counts_only_when_paired_within_its_own_request():
    pi = Pi()
    pi.user()
    pi.declare("twice", "wi_20261015_401")
    pi.result("twice", "declare_session_wi", ack("wi_20261015_401"))  # A second result.
    pi.user()
    pi.assistant(("moved", "declare_session_wi", {"wi": "wi_20261015_402"}))
    pi.user()  # The result answers under another request than its call.
    pi.result("moved", "declare_session_wi", ack("wi_20261015_402"))
    pi.assistant(("blocks", "declare_session_wi", {"wi": "wi_20261015_403"}))
    pi.message({"role": "toolResult", "toolCallId": "blocks", "toolName": "declare_session_wi",
                "content": [{"type": "text", "text": ack("wi_20261015_403")},
                            {"type": "text", "text": ""}]})
    pi.assistant(("bad", "declare_session_wi", {"wi": "wi_2026"}))
    pi.result("bad", "declare_session_wi", ack("wi_2026"))
    pi.declare("good", "wi_20261015_404")
    assert observe_rows(pi)["declared_wis"] == ["wi_20261015_404"]


def observe_rows(pi):
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        return observe("pi", pi.write(Path(directory)))


def test_a_forked_pi_session_never_claims_its_parent_history(tmp_path):
    repo = committed_repository(tmp_path)
    head = repo[1]
    rig = gate(tmp_path)
    parent = pi_commit(tmp_path, repo, wi="wi_20261015_401")
    root = tmp_path / "pi-root"
    parent.write(root)
    fork = parent.fork()
    fork.user()  # The fork's own request, after its header.
    fork.assistant()
    path = fork.write(root, f"2026-10-17T06-00-00-000Z_{OTHER}.jsonl")
    observed = observe("pi", path)
    # The copied commit window is the parent's; the copied declaration still binds the
    # fork's record, as ComputeCost reads it.
    assert observed["commits"] == [] and observed["declared_wis"] == ["wi_20261015_401"]
    assert observed["observed_turns"] == 1
    found = find_authors([("pi", root)], [head], 0)
    assert [a["native_id"] for a in found[head]] == [SESSION]
    refused = submit(rig, head, replace(AUTHOR, session_compute=verify_commits(observed, head)),
                     observed=opened(head))
    assert error(refused) == "source_unavailable"
    # A Pi branch (createBranchedSession) is the same: a new header over copied entries.
    branch = parent.fork(at=datetime.now(timezone.utc))
    assert observe_rows(branch)["commits"] == []


def test_a_forked_pi_session_claims_its_own_later_commit(tmp_path):
    repo, _, before, after = committed_repository(tmp_path)
    parent = pi_commit(tmp_path, (repo, None, before, after))
    fork = parent.fork(at=after)
    start = datetime.now(timezone.utc) - timedelta(seconds=1)
    head = commit_now(repo, "-m", "fork")
    end = datetime.now(timezone.utc) + timedelta(seconds=1)
    fork.user()
    fork.bash("f1", f"cd {repo} && git commit -q -m fork", max(start, after), end)
    compute = verify_commits(observe_rows(fork), head)
    assert [(c["started_at"], c["verified"]) for c in compute["commits"]] == [
        (fork.stamp(max(start, after)), True)]


def test_an_ack_compute_cost_rejects_declares_nothing_to_the_kernel(cost, tmp_path):
    pi = Pi()
    pi.user()
    pi.declare("d1", WI, text=json.dumps({"schema": "wrong/v1", "wi": WI,
                                          "origin": "agent_declaration"}))
    pi.assistant()
    path = pi.write(tmp_path)
    assert claims(cost, path) == set()
    assert observe("pi", path)["declared_wis"] == []


@pytest.mark.parametrize("kind", ["fork", "branch"])
@pytest.mark.parametrize("own, admitted", [(None, True), (WI, True), (OTHER_WI, False)])
def test_a_pi_fork_declares_what_compute_cost_charges_it(cost, tmp_path, kind, own, admitted):
    """A fork or branch record read by both: the copied declaration binds the fork's
    copied requests in ComputeCost, so the kernel counts it too."""
    parent = Pi()
    parent.user()
    parent.declare("p1", WI)
    parent.assistant()
    child = parent.fork(at=datetime.now(timezone.utc))
    if kind == "branch":
        child.rows[0]["parentSession"] = "/sessions/branched-from.jsonl"
    child.user()
    if own is not None:
        child.declare("c1", own)
    child.assistant()
    path = child.write(tmp_path, f"2026-10-17T06-00-00-000Z_{OTHER}.jsonl")
    charged = {wi for state, wi in claims(cost, path) if state == "assigned"}
    declared = observe("pi", path)["declared_wis"]
    assert set(declared) == charged == {WI, *([own] if own else [])}
    assert (declared == [WI]) is admitted
    if not admitted:
        rig = gate(tmp_path)
        several = replace(AUTHOR, session_compute={**AUTHOR.session_compute, "declared_wis": declared})
        refused = submit(rig, "a" * 40, several)
        assert error(refused) == "domain_conflict" and "exactly one WI" in refused["error"]["reason"]
