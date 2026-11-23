"""An agy session's own commits: read from its transcript, proven by the workspace HEAD log."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import re

import pytest

from harness_rewrite.model import native_session_ref
from harness_rewrite.pull_requests import family
from harness_rewrite.session_compute import agy_commit_shape, find_authors, observe, verify_commits
from harness_rewrite.session_sources import AGY_TRANSCRIPT
from tests_rewrite.test_pi_commit_authors import gate
from tests_rewrite.test_pull_request_host import commit_now, committed_repository, git
from tests_rewrite.test_pull_requests import AUTHOR, compute, opened, pr, submit
from tests_rewrite.test_workflow import error, original

CONVERSATION = "90c20de2-5130-4f23-8fec-13c4dfc64d95"
OTHER = "1669fdac-a28a-441a-9c4e-369300c4c4ae"
MODEL = "gemini-4-neon-high"
LOCAL = timezone(timedelta(hours=8))


class Agy:
    """A synthetic agy transcript in the step format agy 1.5.13 writes (wi666 n01)."""

    def __init__(self, conversation=CONVERSATION, start=None):
        self.conversation = conversation
        self.at = start or datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)
        self.rows = []
        self.step("USER_EXPLICIT", "USER_INPUT",
                  content="<USER_REQUEST>\nsynthetic request\n</USER_REQUEST>")

    def step(self, source, kind, at=None, status="DONE", **fields):
        self.rows.append({"step_index": len(self.rows), "source": source, "type": kind,
                          "status": status, "created_at": self.utc(at or self.at), **fields})

    @staticmethod
    def utc(at):
        return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def planner(self, *commands, at=None, model=MODEL, other_tools=()):
        calls = [{"name": "run_command",
                  "args": {"CommandLine": c, "WaitMsBeforeAsync": 10000, "toolSummary": "x"}}
                 for c in commands]
        calls += [{"name": n, "args": {"Path": "/tmp/x"}} for n in other_tools]
        self.step("MODEL", "PLANNER_RESPONSE", at, model=model, input_tokens=10,
                  cache_read_tokens=0, output_tokens=5, tool_calls=calls)

    def result(self, start, end, text=None, *, failed=False, output="", times=True):
        """A tool result as agy writes it: its time lines twice, then the tool's text."""
        stamp = (f"Created At: {start.astimezone(LOCAL).isoformat(timespec='seconds')}\n"
                 f"Completed At: {end.astimezone(LOCAL).isoformat(timespec='seconds')}\n")
        if text is None:
            text = ("Encountered error in step execution: permission check failed for "
                    'command "x": user denied permission to run command:\nx' if failed
                    else "Command exited with code 0.\n\n"
                    + (f"Command output:\n{output}" if output else "No output from the command."))
        extra = {"error": "permission check failed"} if failed else {}
        self.step("MODEL", "GENERIC", start, status="ERROR" if failed else "DONE",
                  content=(stamp * 2 if times else "") + text, **extra)

    def command(self, command, start, end, failed=False, output=""):
        self.planner(command, at=start)
        self.result(start, end, failed=failed, output=output)

    def write(self, root):
        path = root / self.conversation / AGY_TRANSCRIPT
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        return path


def agy_commit(repo, conversation=CONVERSATION, command=None, shift=timedelta(0), failed=False,
               output=None):
    """An agy session that ran the commit `committed_repository` just made; by default
    `git commit` printed its `[branch <sha>]` line."""
    _, head, before, after = repo
    agy = Agy(conversation)
    agy.command(command or f"cd {repo[0]} && git commit --no-verify -m x",
                before + shift, after + shift, failed,
                f"[main {head[:7]}] x\n 1 file changed" if output is None else output)
    agy.planner(at=after + shift)  # The model's next step reads the result.
    return agy


def test_agy_transcript_yields_turns_and_one_command_s_commit_window(tmp_path):
    agy = Agy()
    t0 = agy.at
    # One command and a read in one step: the command result is the only one of its kind.
    agy.planner(COMMIT, at=t0, model="gemini-3.8-flash-low", other_tools=["read_file"])
    agy.result(t0 + timedelta(seconds=1), t0 + timedelta(seconds=9), "Showing lines 1-1 of 1\n1: x")
    agy.result(t0 + timedelta(seconds=1), t0 + timedelta(seconds=3),
               output="[main abc1234] x\r\n 1 file changed")
    agy.planner("git commit --no-verify -m relative", at=t0 + timedelta(seconds=10))
    agy.result(t0 + timedelta(seconds=10), t0 + timedelta(seconds=11), output="[main def5678] y")
    observed = observe("agy", agy.write(tmp_path))
    assert observed["runtime"] == "agy" and observed["native_id"] == CONVERSATION
    assert observed["turns"] == [{"model": "gemini-3.8-flash-low", "effort": "low"},
                                 {"model": MODEL, "effort": "high"}]
    assert observed["declared_wis"] == []
    # The window ends at the command result's own `Completed At`, not the read's; a
    # commit without `cd <literal absolute dir> &&` is not the provable shape.
    assert observed["commits"] == [
        {"sha": None, "printed": ["abc1234"], "model": "gemini-3.8-flash-low",
         "repository": "/work/repo", "started_at": "2026-11-22T08:00:00Z",
         "ended_at": "2026-11-22T16:00:03+08:00", "verified": False, "tool_use_id": "1.0",
         "agy_commit": "abc1234"},
    ]


COMMIT = "cd /work/repo && git commit --no-verify -m x"


def unreadable_steps():
    """Steps whose command cannot be bound to one explicit successful result."""
    def step(build):
        agy = Agy()
        build(agy, agy.at, agy.at + timedelta(seconds=2))
        return agy
    return {
        # Two commands, one succeeded and one failed: which result is whose is unknown.
        "mixed_success_failure": step(lambda a, t, e: (
            a.planner(COMMIT, "cd /work/repo && git push", at=t),
            a.result(t, e, output="[main abc1234] x"), a.result(t, e, failed=True))),
        # Two commands, both succeeded, only one commits: still unknown.
        "two_commands_one_commit": step(lambda a, t, e: (
            a.planner(COMMIT, "ls", at=t),
            a.result(t, e, output="[main abc1234] x"), a.result(t, e, output="f"))),
        "missing_result": step(lambda a, t, e: a.planner(COMMIT, at=t)),
        "result_of_another_tool": step(lambda a, t, e: (
            a.planner(COMMIT, at=t, other_tools=["read_file"]),
            a.result(t, e, "Showing lines 1-1 of 1\n1: [main abc1234] x"))),
        "truncated_result": step(lambda a, t, e: (
            a.planner(COMMIT, at=t),
            a.result(t, e, "The tool's output was truncated due to being too long. "
                           "[main abc1234] x"))),
        "no_completed_at": step(lambda a, t, e: (
            a.planner(COMMIT, at=t),
            a.result(t, e, output="[main abc1234] x", times=False))),
        "failed": step(lambda a, t, e: (a.planner(COMMIT, at=t), a.result(t, e, failed=True))),
        "nonzero_exit": step(lambda a, t, e: (
            a.planner(COMMIT, at=t),
            a.result(t, e, "Command exited with code 1.\n\nCommand output:\n[main abc1234] x"))),
        "two_command_results": step(lambda a, t, e: (
            a.planner(COMMIT, at=t),
            a.result(t, e, output="[main abc1234] x"), a.result(t, e, output="[main abc1234] x"))),
    }


def test_a_record_whose_steps_repeat_or_go_back_claims_no_commit(tmp_path):
    t0 = Agy().at
    for shape in ("repeated", "backwards"):
        agy = Agy()
        agy.command(COMMIT, t0, t0 + timedelta(seconds=2), output="[main abc1234] x")
        # A later model step reuses an earlier index; its call has no command line, and
        # its result must not bind to the earlier step's command, model or start.
        agy.planner(at=t0 + timedelta(seconds=3))
        agy.rows[-1]["tool_calls"] = [{"name": "run_command", "args": {}}]
        agy.rows[-1]["step_index"] = 1 if shape == "repeated" else 0
        agy.result(t0 + timedelta(seconds=3), t0 + timedelta(seconds=4), output="[main def5678] y")
        observed = observe("agy", agy.write(tmp_path / shape))
        assert observed["commits"] == [], shape
        assert observed["turns"] == [{"model": MODEL, "effort": "high"}]


def test_a_call_never_carries_over_to_a_later_model_step(tmp_path):
    agy = Agy()
    t0 = agy.at
    agy.planner(COMMIT, at=t0)  # No result before the next model step.
    agy.planner(at=t0 + timedelta(seconds=3))
    agy.rows[-1]["tool_calls"] = [{"name": "run_command", "args": {}}]
    agy.result(t0 + timedelta(seconds=3), t0 + timedelta(seconds=4), output="[main abc1234] x")
    assert observe("agy", agy.write(tmp_path))["commits"] == []


@pytest.mark.parametrize("command", [
    "cd /work/repo && git commit -n -m 'fix: keep (all) of it'",
    "cd /work/repo && git commit --no-verify -am x",
    "cd /work/repo && git commit -m -q --no-verify",  # `-q` here is the message
])
def test_the_provable_commit_shape(command):
    assert agy_commit_shape(command) == "/work/repo"


@pytest.mark.parametrize("command", [
    "cd /work/repo && git commit -m x",  # hooks may print
    "cd /work/repo && git commit --no-verify -q -m x",
    "cd /work/repo && git commit -nqm x",
    "cd /work/repo && git commit --no-verify --quiet -m x",
    "cd /work/repo && git add f && git commit --no-verify -m x",
    "cd /work/repo && git commit --no-verify -m x && git log --oneline -1",
    "cd /work/repo && git commit --no-verify -m x; echo done",
    "cd /work/repo && git commit --no-verify -m x | cat",
    "(cd /work/repo && git commit --no-verify -m x)",
    "cd $REPO && git commit --no-verify -m x",
    "cd /work/repo && git commit --no-verify -m \"$(date)\"",
    "cd /work/repo && git commit -m x # --no-verify",
    "cd /work/repo && git commit -m --no-verify",  # the message, not the option
    "cd work/repo && git commit --no-verify -m x",
    "cd /work/../repo && git commit --no-verify -m x",
    "cd ~/repo && git commit --no-verify -m x",
    "git -C /work/repo commit --no-verify -m x",
    "cd /work/repo && git commit --no-verify -F - <<EOF\nx\nEOF",
])
def test_any_other_command_shape_claims_nothing(tmp_path, command):
    assert agy_commit_shape(command) is None
    agy = Agy()
    agy.command(command, agy.at, agy.at + timedelta(seconds=2), output="[main abc1234] x")
    agy.planner(at=agy.at + timedelta(seconds=3))
    assert observe("agy", agy.write(tmp_path))["commits"] == []


@pytest.mark.parametrize("output", [
    "",  # nothing printed
    "pre-commit: ok\n[main abc1234] x",  # a hook line before git's summary
    "warning: something\n[main abc1234] x",
    "abc1234 x",
    "HEAD is now at abc1234 x",
    "remote: [main abc1234] x",
])
def test_only_git_s_summary_as_the_first_output_line_names_the_commit(tmp_path, output):
    agy = Agy()
    agy.command(COMMIT, agy.at, agy.at + timedelta(seconds=2), output=output)
    agy.planner(at=agy.at + timedelta(seconds=3))
    assert observe("agy", agy.write(tmp_path))["commits"] == []


def test_a_root_commit_summary_names_the_commit(tmp_path):
    agy = Agy()
    agy.command(COMMIT, agy.at, agy.at + timedelta(seconds=2),
                output="[main (root-commit) abc1234] x\n 1 file changed")
    agy.planner(at=agy.at + timedelta(seconds=3))
    (commit,) = observe("agy", agy.write(tmp_path))["commits"]
    assert commit["agy_commit"] == "abc1234"


@pytest.mark.parametrize("case", sorted(unreadable_steps()))
def test_a_step_without_one_explicit_command_result_claims_nothing(tmp_path, case):
    agy = unreadable_steps()[case]
    agy.planner(at=agy.at + timedelta(seconds=5))
    assert observe("agy", agy.write(tmp_path))["commits"] == []


def test_an_agy_record_outside_its_step_format_or_place_is_unusable(tmp_path):
    agy = Agy()
    agy.rows.append({"type": "PLANNER_RESPONSE"})  # A complete row without its step index.
    assert observe("agy", agy.write(tmp_path)) is None
    stray = tmp_path / "loose" / "transcript_full.jsonl"
    stray.parent.mkdir()
    stray.write_text("".join(json.dumps(r) + "\n" for r in Agy().rows))
    assert observe("agy", stray) is None


def test_a_model_without_its_tier_is_an_unknown_effort(tmp_path):
    agy = Agy()
    agy.planner(model="gemini-pro-orchestrator-neon")
    assert observe("agy", agy.write(tmp_path))["turns"] == [
        {"model": "gemini-pro-orchestrator-neon", "effort": "unknown"}]


def test_gemini_is_its_own_family_and_unknown_families_stay_unknown():
    assert family("gemini-4-neon-high") == "gemini"
    assert family("gemini-3.8-flash-low") == "gemini"
    assert family("claude-opus-5-5-high") == "claude"  # agy also serves Claude models.
    assert family("gpt-oss-120b-medium") == "gpt"
    assert family("orchestrator-neon") is None


def test_the_agy_session_that_ran_the_commit_registers_and_codex_reviews(tmp_path):
    repo = committed_repository(tmp_path)
    head = repo[1]
    rig = gate(tmp_path)
    path = agy_commit(repo).write(tmp_path / "brain")
    observed = verify_commits(observe("agy", path), head, path)
    assert [(c["sha"], c["verified"]) for c in observed["commits"]] == [(head, True)]
    candidate = original(submit(rig, head, replace(AUTHOR, session_compute=observed),
                                observed=opened(head)))
    assert candidate["candidate_id"]
    state = pr(rig)
    assert state["status"] == "awaiting_review"
    assert state["rounds"][0]["author_model"] == MODEL
    assert state["rounds"][0]["review_request"]["runtime"] == "codex"


def second_commit_in_window(repo):
    """The window also holds a second commit-type HEAD move of the same workspace."""
    agy = agy_commit(repo)
    commit_now(repo[0], "-m", "another")
    # The call's result completes after the second commit: the window holds both moves.
    # Without the second move this very window proves the head (the registration test).
    window_end = (datetime.now(timezone.utc) + timedelta(seconds=5)).astimezone(LOCAL)
    agy.rows[-2]["content"] = re.sub(r"Completed At: \S+", "Completed At: "
                                     + window_end.isoformat(timespec="seconds"), agy.rows[-2]["content"])
    return agy


def test_overlapping_windows_of_two_sessions_leave_the_author_ambiguous(tmp_path):
    """Both sessions' windows hold the one commit: both are reported, and the kernel
    then treats the author as ambiguous (`ambiguous_commit_authors`)."""
    repo = committed_repository(tmp_path)
    head = repo[1]
    root = tmp_path / "brain"
    agy_commit(repo).write(root)
    agy_commit(repo, OTHER).write(root)
    found = find_authors([("agy", root)], [head], 0)
    assert sorted(a["native_id"] for a in found[head]) == sorted([CONVERSATION, OTHER])


def two_commands(repo):
    """The real commit ran beside another command in the same step: its result is unbound."""
    _, head, before, after = repo
    agy = Agy()
    agy.planner(f"cd {repo[0]} && git commit --no-verify -m x", f"git -C {repo[0]} status", at=before)
    agy.result(before, after, output=f"[main {head[:7]}] x")
    agy.result(before, after, output="clean")
    agy.planner(at=after)
    return agy


@pytest.mark.parametrize(
    "case", ["outside_window", "other_workspace", "failed", "quiet_commit", "other_sha",
             "quiet_commit_sha_elsewhere", "without_no_verify", "hook_text_first",
             "two_commits_in_window", "two_commands"])
def test_an_agy_session_cannot_claim_a_commit_it_did_not_create(tmp_path, case):
    repo = committed_repository(tmp_path)
    head = repo[1]
    elsewhere = tmp_path / "elsewhere"
    git(tmp_path, "init", "-q", "-b", "main", str(elsewhere))
    agy = {
        "outside_window": lambda: agy_commit(repo, shift=timedelta(minutes=10)),
        "other_workspace": lambda: agy_commit(repo, command=f"cd {elsewhere} && git commit --no-verify -m x"),
        "failed": lambda: agy_commit(repo, failed=True),
        # The HEAD move alone is not enough: the output must name the commit.
        "quiet_commit": lambda: agy_commit(repo, command=f"cd {repo[0]} && git commit -q -m x",
                                           output=""),
        "other_sha": lambda: agy_commit(repo, output="[main 0000000] x"),
        # A quiet commit whose output names the same sha outside a commit summary line.
        "quiet_commit_sha_elsewhere": lambda: agy_commit(
            repo, command=f"cd {repo[0]} && git commit -q -m x && git log --oneline -1",
            output=f"{repo[1][:7]} x"),
        "without_no_verify": lambda: agy_commit(repo, command=f"cd {repo[0]} && git commit -m x"),
        "hook_text_first": lambda: agy_commit(repo, output=f"pre-commit ok\n[main {head[:7]}] x"),
        # Another commit landed in the same workspace inside the call's window.
        "two_commits_in_window": lambda: second_commit_in_window(repo),
        "two_commands": lambda: two_commands(repo),
    }[case]()
    rig = gate(tmp_path)
    observed = verify_commits(observe("agy", agy.write(tmp_path / "brain")), head)
    assert all(not c["verified"] for c in observed["commits"])
    refused = submit(rig, head, replace(AUTHOR, session_compute=observed), observed=opened(head))
    assert error(refused) == "source_unavailable"


def test_an_unknown_author_family_is_still_refused(tmp_path):
    rig = gate(tmp_path)
    head = "a" * 40
    unknown = compute("agy", "gemini-pro-orchestrator-neon", "high", [head])
    unknown["turns"][0]["model"] = "orchestrator-neon"
    unknown["commits"][0]["model"] = "orchestrator-neon"
    refused = submit(rig, head, replace(AUTHOR, session_compute=unknown), observed=opened(head))
    assert error(refused) == "source_unavailable"
    assert "no single known author model" in refused["error"]["reason"]


def test_a_gemini_and_claude_mix_goes_to_a_human(tmp_path):
    rig = gate(tmp_path)
    head, body = "a" * 40, "c" * 40
    gemini = compute("agy", MODEL, "high", [head])
    opus_body = {"sha": body, "session": "session.opus-body", "model": "claude-opus-5-5",
                 "declared_wis": []}
    original(submit(rig, head, replace(AUTHOR, session_compute=gemini),
                    observed=opened(head, commits=[body, head], authors=[opus_body])))
    assert pr(rig)["rounds"][0]["review_block"] == "mixed_author_families"
    assert pr(rig)["status"] == "human_confirmation_required"


def test_agy_transcripts_are_searched_at_their_own_place_only(tmp_path):
    repo = committed_repository(tmp_path)
    head = repo[1]
    root = tmp_path / "brain"
    agy_commit(repo).write(root)
    # transcript.jsonl beside it, or a transcript one level deeper, is not a record.
    copy = root / CONVERSATION / ".system_generated" / "logs" / "transcript.jsonl"
    copy.write_text((root / CONVERSATION / AGY_TRANSCRIPT).read_text())
    agy_commit(repo, OTHER).write(root / "nested")
    found = find_authors([("agy", root)], [head], 0)
    assert found == {head: [{"native_id": CONVERSATION, "runtime": "agy", "model": MODEL,
                             "declared_wis": [], "wi_marker": None}]}


def test_host_searches_the_agent_agy_root_among_its_session_roots(tmp_path):
    from harness_rewrite import session_compute
    from harness_rewrite.native_host import NativeHost

    repo = committed_repository(tmp_path)
    head = repo[1]
    root = tmp_path / "brain"
    agy_commit(repo).write(root)
    host = NativeHost.__new__(NativeHost)
    host.profile = {"actor": "asobi", "role": "producer"}
    host.policy = {"namespace": "team", "surface": "ownermac", "profiles": [
        {"actor": "asobi", "role": "producer", "runtime": "agy", "session_root": str(root)},
    ]}
    assert host.pull_request_authors([head], session_compute) == [{
        "sha": head, "session": native_session_ref("team", "asobi", CONVERSATION, "ownermac"),
        "model": MODEL, "declared_wis": [], "wi_marker": None}]
