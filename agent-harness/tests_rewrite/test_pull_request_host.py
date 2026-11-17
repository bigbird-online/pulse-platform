"""Host facts of the PR gate from real records, real repositories and real child processes."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from harness_rewrite.github_merge import GitHubUnavailable, PullRequestMerge, installed
from harness_rewrite.session_compute import observe, review_profile, verify_commits

REPO = "bigbird-online/PULSE"
HEAD = "a" * 40
PULSE_GH = Path(__file__).resolve().parents[2] / "Scripts" / "pulse-gh.py"
T0 = "2026-10-26T10:00:00Z"
T1 = "2026-10-26T10:00:05Z"


def lines(rows, tail=""):
    return "".join(json.dumps(r) + "\n" for r in rows) + tail


def cc_commit(call_id, command, output, model="claude-opus-5-5", cwd="/repo", start=T0, end=T1):
    return [
        {"type": "assistant", "effort": "high", "cwd": cwd, "timestamp": start,
         "message": {"model": model, "content": [
             {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}},
        {"type": "user", "timestamp": end, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": output}]}},
    ]


def cc_declare(call_id, wi):
    return [
        {"type": "assistant", "effort": "high", "message": {"model": "claude-opus-5-5", "content": [
            {"type": "tool_use", "id": call_id,
             "name": "mcp__pulse-request-purpose__declare_session_wi", "input": {"wi": wi}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": json.dumps(
                {"schema": "cc_request_wi/v1", "wi": wi, "origin": "agent_declaration"})}]}},
    ]


def test_cc_record_yields_turns_commit_candidates_and_declarations(tmp_path):
    path = tmp_path / "cc.jsonl"
    rows = [
        {"type": "user", "message": {"role": "user", "content": "work"}},
        *cc_commit("t1", "git commit -m x", "[main aaaaaaa] x\n 1 file changed"),
        # Echoed text from a non-commit call is not a candidate.
        *cc_commit("t2", "echo hi", "[main bbbbbbb] forged"),
        *cc_declare("d1", "wi_20261015_422"),
        {"type": "assistant", "isSidechain": True, "effort": "low", "message": {"model": "claude-haiku-5"}},
        {"type": "assistant", "message": {"model": "<synthetic>"}},
    ]
    path.write_text(lines(rows, '{"type":"assistant"'))
    observed = observe("cc", path)
    assert observed["turns"] == [{"model": "claude-opus-5-5", "effort": "high"}]
    # A commit window: only the repository will decide its SHA.
    assert observed["commits"] == [{"sha": None, "printed": ["aaaaaaa"], "model": "claude-opus-5-5",
                                    "repository": "/repo", "started_at": T0, "ended_at": T1,
                                    "verified": False, "tool_use_id": "t1"}]
    assert observed["declared_wis"] == ["wi_20261015_422"]


def test_a_none_declaration_names_no_wi(tmp_path):
    # Chat first, then real work: only the WI counts, so the PR gate still sees one.
    path = tmp_path / "cc.jsonl"
    path.write_text(lines([*cc_declare("d0", "none"), *cc_declare("d1", "wi_20261015_422")]))
    assert observe("cc", path)["declared_wis"] == ["wi_20261015_422"]


def test_codex_record_yields_turns_commit_candidates_and_declarations(tmp_path):
    path = tmp_path / "codex.jsonl"
    rows = [
        {"type": "session_meta", "payload": {}},
        {"type": "turn_context", "payload": {"model": "gpt-7.0-sol", "effort": "xhigh", "cwd": "/work"}},
        {"type": "response_item", "timestamp": T0, "payload": {
            "type": "function_call", "call_id": "c1", "name": "exec_command",
            "arguments": '{"cmd":"git -C repo commit -m y"}'}},
        {"type": "response_item", "timestamp": T1, "payload": {
            "type": "function_call_output", "call_id": "c1", "output": "[feature/x (root-commit) ccccccc9] y"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "McpToolCall", "server": "pulse-request-purpose", "tool": "declare_session_wi",
            "arguments": {"wi": "wi_20261015_422"}, "status": "completed",
            "result": {"content": [{"type": "text", "text": json.dumps(
                {"wi": "wi_20261015_422", "origin": "agent_declaration"})}]}}}},
        # A declaration without the server's own ack declares nothing.
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "McpToolCall", "server": "pulse-request-purpose", "tool": "declare_session_wi",
            "arguments": {"wi": "wi_20261015_999"}, "status": "completed",
            "result": {"content": [{"type": "text", "text": "ok"}]}}}},
        {"type": "turn_context", "payload": {"model": "gpt-7.0-sol"}},
    ]
    path.write_text(lines(rows))
    observed = observe("codex", path)
    assert observed["turns"] == [
        {"model": "gpt-7.0-sol", "effort": "unknown"},
        {"model": "gpt-7.0-sol", "effort": "xhigh"},
    ]
    assert observed["commits"] == [{"sha": None, "printed": ["ccccccc9"], "model": "gpt-7.0-sol",
                                    "repository": "/work/repo", "started_at": T0, "ended_at": T1,
                                    "verified": False, "tool_use_id": "c1"}]
    assert observed["declared_wis"] == ["wi_20261015_422"]
    assert observe("pi", path) is None and observe("codex", tmp_path / "absent") is None


def test_a_complete_corrupt_row_makes_the_record_unusable(tmp_path):
    path = tmp_path / "cc.jsonl"
    good = cc_commit("t1", "git commit -m x", "[main aaaaaaa] x")
    path.write_text(lines(good) + "{not json}\n" + lines(good))
    assert observe("cc", path) is None
    path.write_text(lines(good) + '{"type": "assistant", "message": ')  # unfinished tail
    assert observe("cc", path)["observed_turns"] == 1


def git(repo, *args):
    environment = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=environment).stdout.strip()


def committed_repository(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("x")
    git(repo, "add", "f")
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    git(repo, "commit", "-q", "-m", "x")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    return repo, git(repo, "rev-parse", "HEAD"), before, after


def record(tmp_path, repo, head, command, start, end):
    path = tmp_path / "author.jsonl"
    output = f"[main {head[:7]}] x\n 1 file changed"
    path.write_text(lines(cc_commit("t1", command, output, cwd=str(repo),
                                    start=start.isoformat(), end=end.isoformat())))
    return verify_commits(observe("cc", path), head)["commits"]


def test_a_real_commit_in_the_call_window_is_confirmed(tmp_path):
    repo, head, before, after = committed_repository(tmp_path)
    assert [c["verified"] for c in record(tmp_path, repo, head, "git commit -m x", before, after)] == [True]


def test_printf_forged_commit_line_is_not_confirmed(tmp_path):
    # Codex repro: printf '[main <sha>] spoofed' # git commit, for another session's commit.
    repo, head, before, after = committed_repository(tmp_path)
    later = after + timedelta(minutes=10)
    forged = f"printf '[main {head[:7]}] spoofed\\n' # git commit"
    commits = record(tmp_path, repo, head, forged, later, later + timedelta(seconds=3))
    assert [c["verified"] for c in commits] == [False]


def test_reflog_outside_the_call_window_is_not_confirmed(tmp_path):
    repo, head, before, _ = committed_repository(tmp_path)
    earlier = before - timedelta(minutes=10)
    commits = record(tmp_path, repo, head, "git commit -m x", earlier, earlier + timedelta(seconds=3))
    assert [c["verified"] for c in commits] == [False]
    # An unreachable or absent object is never confirmed either.
    assert [c["verified"] for c in record(tmp_path, tmp_path / "absent", head, "git commit",
                                          before, before + timedelta(minutes=1))] == [False]


def window(tmp_path, cwd, command, start, end, output=""):
    """One committing Bash call of a CC session; `git commit -q` prints nothing."""
    path = tmp_path / "quiet.jsonl"
    path.write_text(lines(cc_commit("t1", command, output, cwd=str(cwd),
                                    start=start.isoformat(), end=end.isoformat())))
    return observe("cc", path)


def commit_now(repo, *flags):
    (repo / "f").write_text(str(datetime.now(timezone.utc)))
    git(repo, "add", "f")
    git(repo, "commit", "-q", *flags)
    return git(repo, "rev-parse", "HEAD")


def confirmed(observed, head):
    return [(c["sha"], c["verified"]) for c in verify_commits(observed, head)["commits"]]


def test_a_quiet_commit_is_confirmed_by_the_reflog_alone(tmp_path):
    # Production: a sub-agent's `git commit -q` printed no `[branch sha]` line.
    repo, head, before, after = committed_repository(tmp_path)
    observed = window(tmp_path, repo, 'git add f && git commit -q -m "x\n\nbody"', before, after)
    assert observed["commits"][0]["sha"] is None and observed["commits"][0]["printed"] == []
    assert confirmed(observed, head) == [(head, True)]


def test_every_commit_of_one_compound_command_belongs_to_the_call(tmp_path):
    from harness_rewrite.session_compute import find_authors

    repo, _, _, _ = committed_repository(tmp_path)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    first = commit_now(repo, "-m", "one")
    second = commit_now(repo, "-F", "/dev/null", "--allow-empty-message")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    command = (f"cd {repo} && git add f && git commit -q -m one && "
               "git commit --quiet -F - <<'EOF'\ntwo\nEOF")
    observed = window(tmp_path, repo, command, before, after)
    assert confirmed(observed, first) == [(first, True)]
    assert confirmed(observed, second) == [(second, True)]
    rows = [{**r, "sessionId": "s-compound"} for r in cc_commit(
        "t1", command, "", cwd=str(repo), start=before.isoformat(), end=after.isoformat())]
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "s-compound.jsonl").write_text(lines(rows))
    found = find_authors([("cc", root)], [first, second], 0)
    assert {sha: [a["native_id"] for a in authors] for sha, authors in found.items()} == {
        first: ["s-compound"], second: ["s-compound"]}


def test_a_quiet_commit_in_a_linked_worktree_through_git_dash_c_is_confirmed(tmp_path):
    worktree, head, before, after = worktree_commit(tmp_path)
    # The call ran in the main checkout and named the worktree with `git -C`.
    observed = window(tmp_path, tmp_path / "repo", f"git -C {worktree} commit -q -m y",
                      before, after)
    assert observed["commits"][0]["repository"] == str(worktree)
    assert confirmed(observed, head) == [(head, True)]
    # From the main checkout (a `cd <worktree> &&` prefix) the worktree's reflogs are read too.
    observed = window(tmp_path, tmp_path / "repo", f"cd {worktree} && git commit -q -m y",
                      before, after)
    assert confirmed(observed, head) == [(head, True)]


def test_an_amended_commit_belongs_to_the_amending_call(tmp_path):
    repo, original, _, _ = committed_repository(tmp_path)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    amended = commit_now(repo, "--amend", "-m", "x2")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert amended != original
    observed = window(tmp_path, repo, "git commit -q --amend -m x2", before, after)
    assert confirmed(observed, amended) == [(amended, True)]


def test_a_printed_line_without_a_commit_reflog_entry_is_not_confirmed(tmp_path):
    # The object exists and is on a branch, but no commit-type reflog entry created it.
    repo, base, _, _ = committed_repository(tmp_path)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    forged = git(repo, "commit-tree", tree, "-p", base, "-m", "elsewhere")
    git(repo, "update-ref", "refs/heads/side", forged)
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    command = f"printf '[side {forged[:7]}] x\\n' # git commit"
    observed = window(tmp_path, repo, command, before, after, output=f"[side {forged[:7]}] x")
    assert observed["commits"][0]["printed"] == [forged[:7]]
    assert confirmed(observed, forged) == [(None, False)]
    # A quiet commit outside the call's window is not the call's either.
    later = after + timedelta(minutes=10)
    head = commit_now(repo, "-m", "z")
    observed = window(tmp_path, repo, "git commit -q -m z", later, later + timedelta(seconds=3))
    assert confirmed(observed, head) == [(None, False)]


def session_file(root, session, cwd, command, start, end):
    """A CC session record with one committing Bash call."""
    root.mkdir(exist_ok=True)
    rows = [{**r, "sessionId": session} for r in cc_commit(
        "t1", command, "", cwd=str(cwd), start=start.isoformat(), end=end.isoformat())]
    (root / f"{session}.jsonl").write_text(lines(rows))


def two_worktrees(tmp_path):
    repo, _, _, _ = committed_repository(tmp_path)
    trees = []
    for name in ("w1", "w2"):
        git(repo, "worktree", "add", "-q", "-b", name, str(tmp_path / name))
        trees.append(tmp_path / name)
    return repo, trees


def test_a_successful_no_op_commit_claims_another_worktrees_commit_nowhere(tmp_path):
    # Codex repro: A runs a no-op commit on a clean main; B commits quietly in a
    # linked worktree within two seconds. Only B's own HEAD log moved.
    from harness_rewrite.session_compute import find_authors

    repo, (w1, _) = two_worktrees(tmp_path)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    b_head = commit_now(w1, "-m", "b")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    no_op = "git diff --cached --quiet || git commit -q -m nothing"
    assert confirmed(window(tmp_path, repo, no_op, before, after), b_head) == [(None, False)]
    root = tmp_path / "sessions"
    session_file(root, "s-a", repo, no_op, before, after)
    session_file(root, "s-b", w1, "git commit -q -m b", before, after)
    assert [a["native_id"] for a in find_authors([("cc", root)], [b_head], 0)[b_head]] == ["s-b"]


def test_concurrent_sessions_in_different_worktrees_claim_only_their_own(tmp_path):
    from harness_rewrite.session_compute import find_authors

    _, (w1, w2) = two_worktrees(tmp_path)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    a_head = commit_now(w1, "-m", "a")
    b_head = commit_now(w2, "-m", "b")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    root = tmp_path / "sessions"
    session_file(root, "s-a", w1, "git commit -q -m a", before, after)
    session_file(root, "s-b", tmp_path, f"cd {w2.name} && git commit -q -m b", before, after)
    found = find_authors([("cc", root)], [a_head, b_head], 0)
    assert {sha: [a["native_id"] for a in authors] for sha, authors in found.items()} == {
        a_head: ["s-a"], b_head: ["s-b"]}


def test_two_sessions_covering_one_workspace_commit_leave_its_author_ambiguous(tmp_path):
    from harness_rewrite.pull_requests import authorship
    from harness_rewrite.session_compute import find_authors

    _, (w1, _) = two_worktrees(tmp_path)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    head = commit_now(w1, "-m", "one")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    root = tmp_path / "sessions"
    session_file(root, "s-a", w1, "git commit -q -m one", before, after)
    session_file(root, "s-b", w1, "git diff --cached --quiet || git commit -q -m x", before, after)
    found = find_authors([("cc", root)], [head], 0)
    assert sorted(a["native_id"] for a in found[head]) == ["s-a", "s-b"]
    observed = {"commits": [head], "commits_complete": True, "authors": [
        {"sha": head, "session": a["native_id"], "model": a["model"]} for a in found[head]]}
    first = [{"sha": head, "session": "s-a", "model": "claude-opus-5-5"}]
    assert authorship(observed, head, first)[1] == "ambiguous_commit_authors"


def test_a_fast_forward_moves_head_but_creates_no_commit(tmp_path):
    repo, (w1, _) = two_worktrees(tmp_path)
    head = commit_now(w1, "-m", "feature")
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    git(repo, "merge", "-q", "--ff-only", "w1")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    observed = window(tmp_path, repo, "git merge --ff-only w1", before, after)
    assert confirmed(observed, head) == [(None, False)]


def test_a_cd_in_a_subshell_does_not_move_the_commit_to_another_worktree(tmp_path):
    # Codex repro: A's subshell visits B's worktree, then A's no-op commit runs in
    # the main checkout; B commits in its worktree within the same window.
    from harness_rewrite.session_compute import find_authors

    repo, (w1, w2) = two_worktrees(tmp_path)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    b_head = commit_now(w1, "-m", "b")
    x_head = commit_now(w2, "-m", "x")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    probe = f"(cd {w1} && git status --short); git diff --cached --quiet || git commit -q -m nothing"
    observed = window(tmp_path, repo, probe, before, after)
    assert observed["commits"][0]["repository"] == str(repo)
    assert confirmed(observed, b_head) == [(None, False)]
    root = tmp_path / "sessions"
    session_file(root, "s-a", repo, probe, before, after)
    session_file(root, "s-b", w1, "git commit -q -m b", before, after)
    assert [a["native_id"] for a in find_authors([("cc", root)], [b_head], 0)[b_head]] == ["s-b"]
    # The commit's own scope decides: a subshell's cd holds inside it, an outer cd
    # survives a later subshell.
    inside = window(tmp_path, repo, f"(cd {w1} && git commit -q -m b)", before, after)
    assert confirmed(inside, b_head) == [(b_head, True)]
    outer = window(tmp_path, repo, f"cd {w2}; (cd {w1}); git commit -q -m x", before, after)
    assert confirmed(outer, x_head) == [(x_head, True)]
    assert confirmed(outer, b_head) == [(None, False)]


@pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="zsh is not installed")
def test_a_piped_cd_that_zsh_runs_in_the_current_shell_claims_nothing(tmp_path):
    # Codex repro: zsh runs a pipeline's last segment in the current shell, so A's
    # no-op commit runs in w1 while B commits quietly in the main checkout.
    from harness_rewrite.session_compute import find_authors

    repo, (w1, _) = two_worktrees(tmp_path)
    probe = f"printf x | cd {w1}; pwd; git diff --cached --quiet || git commit -q -m nothing"
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    ran = subprocess.run(["/bin/zsh", "-f", "-c", probe], cwd=repo, capture_output=True, text=True)
    b_head = commit_now(repo, "-m", "b")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert ran.returncode == 0 and ran.stdout.strip() == str(w1.resolve())
    observed = window(tmp_path, repo, probe, before, after)
    assert observed["commits"][0]["repository"] is None
    assert confirmed(observed, b_head) == [(None, False)]
    wrapped = window(tmp_path, repo, f"/bin/zsh -c '{probe}'", before, after)
    assert confirmed(wrapped, b_head) == [(None, False)]
    root = tmp_path / "sessions"
    session_file(root, "s-a", repo, probe, before, after)
    session_file(root, "s-b", repo, "git commit -q -m b", before, after)
    assert [a["native_id"] for a in find_authors([("cc", root)], [b_head], 0)[b_head]] == ["s-b"]


def test_pull_rebase_claims_its_new_commits_but_not_the_upstream_start(tmp_path):
    upstream, _, _, _ = committed_repository(tmp_path, "upstream")
    mine = tmp_path / "mine"
    git(tmp_path, "clone", "-q", str(upstream), str(mine))
    upstream_head = commit_now(upstream, "-m", "upstream")  # another session's commit
    (mine / "g").write_text("local")
    git(mine, "add", "g")
    git(mine, "commit", "-q", "-m", "local")
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    git(mine, "pull", "-q", "--rebase", str(upstream), "main")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    rebased = git(mine, "rev-parse", "HEAD")
    assert git(mine, "rev-parse", "HEAD~1") == upstream_head
    log = git(mine, "reflog", "-3", "--format=%gs")
    assert "(start)" in log and "(pick)" in log
    observed = window(tmp_path, mine, f"git pull -q --rebase {upstream} main", before, after)
    assert confirmed(observed, upstream_head) == [(None, False)]
    assert confirmed(observed, rebased) == [(rebased, True)]


@pytest.mark.parametrize(("command", "expected"), [
    ("git commit -q -m x", "/r"),
    ("cd /x && git commit -q -m x", "/x"),
    ("(cd /x && git commit -q)", "/x"),
    ("cd /x; (cd /y); git commit", "/x"),
    ("(cd /w && git status --short); git diff --cached --quiet || git commit -q -m n", "/r"),
    ("cd /x || exit 1; git commit -q", "/x"),
    ("cd /x\ngit add .\ngit commit -m y 2>&1 | tail -3", "/x"),
    ("cd sub && git -C ../w commit -q", "/r/w"),
    ("cd /x && git commit --quiet -F - <<'EOF'\ntwo\nEOF", "/x"),
    ("bash -lc 'cd /x && git commit -m y'", "/x"),
    ("echo 'git commit' ; git commit -m y", "/r"),
    # A background cd runs in a subshell; a piped cd may run in this shell (zsh).
    ("cd /x | cat; git commit", None),
    ("printf x | cd /x; git commit", None),
    ("W=/x | cat; cd $W && git commit", None),
    ("cd /x & git commit", "/r"),
    # Conditional or opaque directory changes leave the workspace unknown.
    ("test -d /x && cd /x; git commit", None),
    ("if true; then cd /x; fi; git commit", None),
    ("{ cd /x; }; git commit", None),
    ("pushd /x; git commit", None),
    ("eval cd /x; git commit", None),
    ("f() { cd /x; }; f; git commit", None),
    ("cd $W && git commit", None),
    # A literal the same scope assigned expands; a substitution runs in a subshell.
    ("W=/x/y; cd $W; git add -A && git commit", "/x/y"),
    ('W=/x; cd "${W}/sub" && git commit', "/x/sub"),
    ("W=/x; git -C $W commit -q", "/x"),
    ("S=$(cd /y; git rev-parse HEAD); git commit", "/r"),
    ("cat > /tmp/m <<'EOF'\ncd /y\nEOF\ncd /x && git commit -F /tmp/m", "/x"),
    ("W=/x git status; cd $W && git commit", None),
    ("test -d /x && W=/x; cd $W && git commit", None),
    ("(W=/x); cd $W && git commit", None),
    ("cd $(pwd) && git commit", None),
    ("cat <<EOF\ngit commit\nEOF", None),
    ("cd ~/x && git commit", None),
    ("cd - && git commit", None),
    ("printf '[main abc] x' # git commit", None),
])
def test_the_commit_workspace_follows_shell_scopes(command, expected):
    from harness_rewrite.shell_scope import workspace

    assert workspace("/r", command) == expected


def test_a_printed_head_still_disqualifies_a_reviewer():
    from harness_rewrite.pull_requests import committed

    compute = {"commits": [{"sha": None, "printed": [HEAD[:9]], "verified": False}]}
    assert len(committed(compute, HEAD)) == 1 and committed(compute, "b" * 40) == []


def executable(path: Path, text: str) -> str:
    path.write_text(text)
    path.chmod(0o755)
    return str(path)


def test_profile_resolver_runs_in_a_fixed_environment(tmp_path, monkeypatch):
    seen = tmp_path / "environment.json"
    node = executable(tmp_path / "node", f"""#!{sys.executable}
import json, os, sys
json.dump({{"env": dict(os.environ), "argv": sys.argv}}, open({str(seen)!r}, "w"))
print(json.dumps({{"profile": "review", "provider": "codex", "model": "gpt-7.0-sol",
                  "model_alias": "codex-sol-latest", "reasoning_effort": "high"}}))
""")
    monkeypatch.setenv("NODE_OPTIONS", "--import=/tmp/forged-profile.mjs")
    monkeypatch.setenv("RELAY_MODEL_CATALOG_PATH", "/tmp/forged-models.json")
    monkeypatch.setenv("PULSE_RELAY_PATHS", "/tmp/forged-paths.json")
    policy = {"producer_node": node, "relay_entry": "/release/Relay/dist/cli.js",
              "pull_request_gate": {"relay_paths": "/installed/relay/paths.json"}}
    assert review_profile(policy, "codex")["model"] == "gpt-7.0-sol"
    recorded = json.loads(seen.read_text())
    assert set(recorded["env"]) <= {"HOME", "PATH", "LANG", "PULSE_RELAY_PATHS", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert recorded["env"]["PULSE_RELAY_PATHS"] == "/installed/relay/paths.json"
    assert "NODE_OPTIONS" not in recorded["env"]
    assert recorded["argv"][1:] == ["/release/Relay/dist/compute-profile-cli.js", "resolve",
                                    "review", "--provider", "codex", "--json"]
    assert review_profile({**policy, "pull_request_gate": {}}, "codex") is None
    assert review_profile({"producer_node": "/absent/node", "relay_entry": "/absent/cli.js",
                           "pull_request_gate": {"relay_paths": "/x"}}, "cc") is None


FAKE_GH = """#!/bin/sh
echo "$@" >> {log}
case "$*" in
  "api user --jq .login") echo example-owner ;;
  "api repos/{repo} --jq .full_name") echo {repo} ;;
  "api repos/{repo}/pulls/7") echo '{pr}' ;;
  "api repos/{repo}/pulls/7/commits?per_page=100&page=1") echo '[{{"sha": "{head}"}}]' ;;
  *) exit 1 ;;
esac
"""


@pytest.mark.skipif(not PULSE_GH.is_file(), reason="repository pulse-gh entry unavailable")
def test_merge_uses_only_the_installed_gh_through_pulse_gh(tmp_path, monkeypatch):
    trusted, forged = tmp_path / "trusted", tmp_path / "forged"
    trusted.mkdir()
    forged.mkdir()
    pr = json.dumps({"number": 7, "state": "open", "merged": False, "head": {"sha": HEAD},
                     "base": {"ref": "main", "repo": {"full_name": REPO,
                                                      "html_url": "https://github.com/" + REPO}}})
    gh = executable(trusted / "gh", FAKE_GH.format(log=tmp_path / "trusted.log", repo=REPO, pr=pr, head=HEAD))
    forged_pr = pr.replace('"merged": false', '"merged": true')
    executable(forged / "gh", FAKE_GH.format(log=tmp_path / "forged.log", repo=REPO, pr=forged_pr, head=HEAD))
    monkeypatch.setenv("PATH", str(forged) + ":" + os.environ["PATH"])
    monkeypatch.setenv("GH_TOKEN", "forged-token-value")
    # Codex repro: a worktree json.py imported by pulse-gh's inner Python.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    injected = tmp_path / "injected"
    (worktree / "json.py").write_text(f"open({str(injected)!r}, 'w').write('ran')\n"
                                      "raise SystemExit('forged json module')\n")
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("PYTHONPATH", str(worktree))
    config = {"pulse_gh": str(PULSE_GH), "gh": gh, "relay_paths": "/x", "repositories": [REPO]}
    observed = PullRequestMerge(config, sys.executable).observe(REPO, 7)
    assert observed == {"repository": REPO, "number": 7, "state": "open", "merged": False,
                        "head_sha": HEAD, "base": "main", "commits": [HEAD],
                        "commits_complete": True}
    assert not (tmp_path / "forged.log").exists()
    assert not injected.exists()
    assert "api repos/bigbird-online/PULSE/pulls/7" in (tmp_path / "trusted.log").read_text()


@pytest.mark.skipif(not PULSE_GH.is_file(), reason="repository pulse-gh entry unavailable")
def test_one_judgment_reuses_one_pulse_gh_preflight(tmp_path):
    pr = json.dumps({"number": 7, "state": "open", "merged": False, "head": {"sha": HEAD},
                     "base": {"ref": "main", "repo": {"full_name": REPO,
                                                      "html_url": "https://github.com/" + REPO}}})
    log = tmp_path / "gh.log"
    gh = executable(tmp_path / "gh", FAKE_GH.format(log=log, repo=REPO, pr=pr, head=HEAD))
    config = {"pulse_gh": str(PULSE_GH), "gh": gh, "relay_paths": "/x", "repositories": [REPO]}
    merge = PullRequestMerge(config, sys.executable)
    with merge.reading(REPO):
        for _ in range(3):
            assert merge.pull_request(REPO, 7)["head"]["sha"] == HEAD  # Read once, then reused.
        for _ in range(2):
            assert merge.commits(REPO, 7) == ([HEAD], True)
        with pytest.raises(GitHubUnavailable):
            merge.api(REPO, [f"repos/{REPO}/pulls/8"])  # Refused by gh: the session continues.
        assert merge.commits(REPO, 7) == ([HEAD], True)
    assert merge.session is None and merge.read_pulls == {}
    calls = log.read_text().splitlines()
    assert calls.count("api user --jq .login") == 1
    assert calls.count(f"api repos/{REPO}/pulls/7") == 1
    assert calls.count(f"api repos/{REPO}/pulls/7/commits?per_page=100&page=1") == 3
    # A prefetched answer serves only its first use; a re-read goes to GitHub again.
    commits = f"repos/{REPO}/pulls/7/commits?per_page=100&page=1"
    with merge.reading(REPO):
        merge.prefetch([commits, f"repos/{REPO}/pulls/7", f"repos/{REPO}/pulls/9"])
        before = log.read_text().splitlines().count("api " + commits)
        assert merge.commits(REPO, 7) == ([HEAD], True)
        assert log.read_text().splitlines().count("api " + commits) == before
        assert merge.commits(REPO, 7) == ([HEAD], True)
        assert log.read_text().splitlines().count("api " + commits) == before + 1
        with pytest.raises(GitHubUnavailable):
            merge.api(REPO, [f"repos/{REPO}/pulls/9"])  # A prefetched refusal stays a refusal.
    assert merge.prefetched == {}
    # Outside a session every request runs its own preflight again.
    merge.pull_request(REPO, 7)
    assert log.read_text().splitlines().count("api user --jq .login") == 3  # Two sessions, one call.


def test_installed_programs_must_be_protected_files(tmp_path):
    program = Path(executable(tmp_path / "gh", "#!/bin/sh\n"))
    assert installed(str(program), executable=True) == str(program)
    program.chmod(0o777)
    with pytest.raises(GitHubUnavailable):
        installed(str(program), executable=True)
    with pytest.raises(GitHubUnavailable):
        installed(str(tmp_path / "absent"), executable=True)


def test_a_package_link_resolves_to_its_protected_file(tmp_path):
    cellar = tmp_path / "Cellar/gh/2.103.0/bin"
    cellar.mkdir(parents=True)
    program = Path(executable(cellar / "gh", "#!/bin/sh\n"))
    link = tmp_path / "bin/gh"
    link.parent.mkdir()
    link.symlink_to(program)
    assert installed(str(link), executable=True) == str(program.resolve())
    program.chmod(0o777)
    with pytest.raises(GitHubUnavailable):
        installed(str(link), executable=True)
    program.chmod(0o755)
    program.unlink()
    with pytest.raises(GitHubUnavailable):
        installed(str(link), executable=True)
    with pytest.raises(GitHubUnavailable):
        installed("relative/gh", executable=True)


def codex_commit(repo_cwd, workdir, command, head, start, end):
    arguments = {"cmd": command, **({"workdir": workdir} if workdir else {})}
    return [
        {"type": "session_meta", "payload": {"id": "01a1-codex"}},
        {"type": "turn_context", "payload": {"model": "gpt-7.0-sol", "effort": "high", "cwd": repo_cwd}},
        {"type": "response_item", "timestamp": start.isoformat(), "payload": {
            "type": "function_call", "call_id": "c1", "name": "exec_command",
            "arguments": json.dumps(arguments)}},
        {"type": "response_item", "timestamp": end.isoformat(), "payload": {
            "type": "function_call_output", "call_id": "c1", "output": f"[main {head[:7]}] x"}},
    ]


def test_codex_commit_in_another_repository_uses_the_tool_workdir(tmp_path):
    # Codex repro: session cwd /repo-a, exec_command(workdir=/repo-b) commits in repo-b.
    repo, head, before, after = committed_repository(tmp_path)
    other = tmp_path / "repo-a"
    other.mkdir()
    path = tmp_path / "codex.jsonl"
    path.write_text(lines(codex_commit(str(other), str(repo), "git commit -m x", head, before, after)))
    observed = observe("codex", path)
    assert observed["commits"][0]["repository"] == str(repo)
    assert observed["native_id"] == "01a1-codex"
    assert [c["verified"] for c in verify_commits(observed, head)["commits"]] == [True]
    # A relative `git -C` resolves against the tool workdir, not the session cwd.
    path.write_text(lines(codex_commit(str(other), str(tmp_path), "git -C repo commit -m x",
                                       head, before, after)))
    assert observe("codex", path)["commits"][0]["repository"] == str(repo)


def test_authors_of_earlier_commits_are_found_in_the_agent_records(tmp_path):
    from harness_rewrite.session_compute import find_authors

    repo, head, before, after = committed_repository(tmp_path)
    root = tmp_path / "codex-sessions"
    root.mkdir()
    (root / "rollout.jsonl").write_text(lines(codex_commit(str(repo), None, "git commit -m x",
                                                           head, before, after)))
    (root / "unrelated.jsonl").write_text(lines([{"type": "session_meta", "payload": {"id": "x"}}]))
    found = find_authors([("codex", root)], [head, "f" * 40], 0)
    assert found == {head: [{"native_id": "01a1-codex", "runtime": "codex",
                             "model": "gpt-7.0-sol", "declared_wis": [], "wi_marker": None}], "f" * 40: []}


def test_host_maps_found_authors_to_kernel_session_references(tmp_path):
    from harness_rewrite import session_compute
    from harness_rewrite.model import native_session_ref
    from harness_rewrite.native_host import NativeHost

    repo, head, before, after = committed_repository(tmp_path)
    root = tmp_path / "codex-sessions"
    root.mkdir()
    (root / "rollout.jsonl").write_text(lines(codex_commit(str(repo), None, "git commit -m x",
                                                           head, before, after)))
    host = NativeHost.__new__(NativeHost)
    host.profile = {"actor": "pulse", "role": "producer"}
    host.policy = {"namespace": "team", "surface": "local", "profiles": [
        {"actor": "pulse", "role": "producer", "runtime": "codex", "session_root": str(root)},
        {"actor": "other", "role": "producer", "runtime": "codex", "session_root": str(tmp_path)},
    ]}
    assert host.pull_request_authors([head], session_compute) == [{
        "sha": head, "session": native_session_ref("team", "pulse", "01a1-codex", "local"),
        "model": "gpt-7.0-sol", "declared_wis": [], "wi_marker": None}]


def test_quoted_git_directory_with_spaces_is_decoded_before_parsing(tmp_path):
    # Codex repro: git -C "/repo-b" commit was parsed from JSON text with escapes.
    repo, head, before, after = committed_repository(tmp_path, "repo b")
    path = tmp_path / "codex.jsonl"
    path.write_text(lines(codex_commit(str(tmp_path), None, f'git -C "{repo}" commit -m x',
                                       head, before, after)))
    observed = observe("codex", path)
    assert observed["commits"][0]["repository"] == str(repo)
    assert [c["verified"] for c in verify_commits(observed, head)["commits"]] == [True]
    script = {"type": "response_item", "timestamp": before.isoformat(), "payload": {
        "type": "custom_tool_call", "call_id": "c1", "name": "exec",
        "input": "await tools.exec_command({cmd:" + json.dumps(f"git -C '{repo}' commit -m x")
        + ", workdir:\"/\"});"}}
    rows = codex_commit(str(tmp_path), None, "git commit", head, before, after)
    rows[2] = script
    rows[3]["payload"]["type"] = "custom_tool_call_output"
    path.write_text(lines(rows))
    assert observe("codex", path)["commits"][0]["repository"] == str(repo)
    commits = record(tmp_path, repo, head, f'git -C "{repo}" commit -m x', before, after)
    assert commits[0]["repository"] == str(repo) and commits[0]["verified"] is True


PARENT = "5e55a0b1-parent"
AGENT = "a0c04eacb04d14ee9"


def worktree_commit(tmp_path):
    """A sub-agent's commit in an isolated worktree of the main repository."""
    repo, _, _, _ = committed_repository(tmp_path)
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    (worktree / "g").write_text("y")
    git(worktree, "add", "g")
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    git(worktree, "commit", "-q", "-m", "y")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    return worktree, git(worktree, "rev-parse", "HEAD"), before, after


def spawning_parent(agent=AGENT, call="toolu_spawn"):
    """A main CC session that spawned one sub-agent with the Agent tool."""
    return [
        {"type": "assistant", "sessionId": PARENT, "effort": "high", "cwd": "/main",
         "message": {"model": "claude-opus-5-5", "content": [
             {"type": "tool_use", "id": call, "name": "Agent",
              "input": {"prompt": "commit it", "isolation": "worktree"}}]}},
        {"type": "user", "sessionId": PARENT, "toolUseResult": {"agentId": agent, "status": "completed"},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": call, "content": "done"}]}},
    ]


def child_rows(worktree, head, before, after, model="claude-sonnet-5", agent=AGENT, session=PARENT):
    rows = cc_commit("toolu_c1", "git commit -m y", f"[feature {head[:7]}] y", model=model,
                     cwd=str(worktree), start=before.isoformat(), end=after.isoformat())
    return [{**r, "isSidechain": True, "agentId": agent, "sessionId": session} for r in rows]


def session_with_child(tmp_path, parent_rows, child, meta_call="toolu_spawn"):
    root = tmp_path / "cc-sessions"
    (root / PARENT / "subagents").mkdir(parents=True)
    path = root / f"{PARENT}.jsonl"
    path.write_text(lines(parent_rows))
    (root / PARENT / "subagents" / f"agent-{AGENT}.jsonl").write_text(lines(child))
    if meta_call is not None:
        (root / PARENT / "subagents" / f"agent-{AGENT}.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "toolUseId": meta_call}))
    return root, path


def test_a_spawned_sub_agent_commit_in_a_worktree_is_confirmed_with_its_own_model(tmp_path):
    worktree, head, before, after = worktree_commit(tmp_path)
    _, path = session_with_child(tmp_path, spawning_parent(), child_rows(worktree, head, before, after))
    observed = verify_commits(observe("cc", path), head)
    assert observed["commits"] == []
    [child] = observed["subagents"]
    assert child["native_id"] == f"{PARENT}/subagents/agent-{AGENT}"
    assert child["turns"] == [{"model": "claude-sonnet-5", "effort": "high"}]
    assert [(c["model"], c["repository"], c["verified"]) for c in child["commits"]] == [
        ("claude-sonnet-5", str(worktree), True)]


def test_an_agent_prompt_and_summary_are_no_commit_evidence(tmp_path):
    # Codex repro: the Agent prompt says `git commit`, its summary echoes `[branch sha]`.
    from harness_rewrite.pull_requests import head_authors

    worktree, head, before, after = worktree_commit(tmp_path)
    parent = spawning_parent()
    parent[0]["timestamp"], parent[1]["timestamp"] = before.isoformat(), after.isoformat()
    parent[0]["message"]["content"][0]["input"]["prompt"] = "run git commit -m y in the worktree"
    parent[1]["message"]["content"][0]["content"] = f"Committed: [feature {head[:7]}] y"
    _, path = session_with_child(tmp_path, parent, child_rows(worktree, head, before, after))
    observed = verify_commits(observe("cc", path), head)
    assert observed["commits"] == []
    observed["subagents"][0]["session"] = "session.sub-agent"
    assert head_authors(observed, head, "session.main") == [
        {"sha": head, "session": "session.sub-agent", "model": "claude-sonnet-5"}]


@pytest.mark.parametrize("forgery",["no_spawn_call", "other_agent", "other_parent", "not_sidechain",
                                     "meta_other_call"])
def test_a_sub_agent_record_without_parent_proof_is_not_adopted(tmp_path, forgery):
    worktree, head, before, after = worktree_commit(tmp_path)
    parent = spawning_parent()
    child = child_rows(worktree, head, before, after)
    meta = "toolu_spawn"
    if forgery == "no_spawn_call":
        parent = [{**r, "message": {**r["message"], "content": [
            {**r["message"]["content"][0], "name": "Bash"}]}} if r["type"] == "assistant" else r
            for r in parent]
    elif forgery == "other_agent":
        parent = spawning_parent(agent="a1111111111111111")
    elif forgery == "other_parent":
        child = child_rows(worktree, head, before, after, session="another-session")
    elif forgery == "not_sidechain":
        child = [{**r, "isSidechain": False} for r in child]
    else:
        meta = "toolu_other"
    _, path = session_with_child(tmp_path, parent, child, meta)
    assert observe("cc", path)["subagents"] == []


def test_sub_agent_authors_are_found_and_mapped_to_their_own_session(tmp_path):
    from harness_rewrite import session_compute
    from harness_rewrite.model import native_session_ref
    from harness_rewrite.native_host import NativeHost

    worktree, head, before, after = worktree_commit(tmp_path)
    # The parent's declaration travels with its sub-agent's commit, kept apart from its own.
    root, path = session_with_child(tmp_path, spawning_parent() + cc_declare("d1", "wi_20261015_401"),
                                    child_rows(worktree, head, before, after))
    child_id = f"{PARENT}/subagents/agent-{AGENT}"
    assert session_compute.find_authors([("cc", root)], [head], 0) == {head: [{
        "native_id": child_id, "runtime": "cc", "model": "claude-sonnet-5",
        "declared_wis": [], "session_declared_wis": ["wi_20261015_401"],
        "wi_marker": None, "dispatch_declared_wis": None}]}
    host = NativeHost.__new__(NativeHost)
    host.profile = {"actor": "pulse", "role": "producer"}
    host.policy = {"namespace": "team", "surface": "local", "profiles": [
        {"actor": "pulse", "role": "producer", "runtime": "cc", "session_root": str(root)}]}
    mapped = host.subagent_sessions(observe("cc", path))
    assert mapped["subagents"][0]["session"] == native_session_ref("team", "pulse", child_id, "local")
    assert host.pull_request_authors([head], session_compute)[0]["session"] == native_session_ref(
        "team", "pulse", child_id, "local")


@pytest.mark.parametrize("prompt,marker", [
    ("[WI: wi_20261029_002] Build the child artifact\nDetails", "wi_20261029_002"),
    ([{"type": "text", "text": "[WI: wi_20261029_002]\nDetails"}], "wi_20261029_002"),
    (" [WI: wi_20261029_002] leading space", None),
    ("Context first\n[WI: wi_20261029_002]", None),
    ("[WI: wi_2026102_002]", None),
])
def test_a_sub_agent_reports_the_child_wi_its_first_prompt_line_names(tmp_path, prompt, marker):
    worktree, head, before, after = worktree_commit(tmp_path)
    first = {"type": "user", "isSidechain": True, "agentId": AGENT, "sessionId": PARENT,
             "message": {"role": "user", "content": prompt}}
    later = {**first, "message": {"role": "user", "content": "[WI: wi_20261029_003] later"}}
    child = [first] + child_rows(worktree, head, before, after) + [later]
    _, path = session_with_child(tmp_path, spawning_parent(), child)
    [observed] = observe("cc", path)["subagents"]
    assert observed["wi_marker"] == marker


def padded(rows, size):
    """`rows` behind complete filler rows until the record passes `size` bytes."""
    filler = json.dumps({"type": "system", "content": "x" * 900}) + "\n"
    return filler * (size // len(filler) + 1) + lines(rows)


def test_a_record_past_the_head_log_limit_still_yields_its_commit(tmp_path, monkeypatch):
    # Production 11-05: a CC session resumed for weeks reached 600 MB; the old
    # 256 MiB cap dropped the whole record, so none of its commits had an author.
    from harness_rewrite import session_compute
    from harness_rewrite.session_compute import find_authors

    monkeypatch.setattr(session_compute, "MAX_BYTES", 4096)
    repo, head, before, after = committed_repository(tmp_path)
    rows = [{**r, "sessionId": "s-long"} for r in cc_commit(
        "t1", f"cd {repo} && git commit -q -m x", "", cwd=str(repo),
        start=before.isoformat(), end=after.isoformat())]
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "s-long.jsonl"
    path.write_text(padded(rows, 3 * 4096))
    assert path.stat().st_size > session_compute.MAX_BYTES
    assert confirmed(observe("cc", path), head) == [(head, True)]
    assert [a["native_id"] for a in find_authors([("cc", root)], [head], 0)[head]] == ["s-long"]


def test_the_commit_prefilter_reads_in_chunks_and_matches_across_a_boundary(tmp_path):
    from harness_rewrite.session_compute import _chunk_mentions

    path = tmp_path / "r.jsonl"
    text = "y" * 1000 + " git -C /repo commit -q -m x " + "z" * 1000
    path.write_text(text)
    split = text.index("commit") - 3  # the match straddles the first chunk's end
    assert _chunk_mentions(path, chunk=split)
    path.write_text("y" * 5000)
    assert not _chunk_mentions(path, chunk=512)


@pytest.mark.parametrize("proofs, expected", [
    ({"t1": ["wi_20261015_401"], "t2": ["wi_20261015_402"]}, None),
    ({"t1": ["wi_20261015_401"], "t2": None}, None),
    ({"t1": None, "t2": ["wi_20261015_401"]}, None),
    ({"t1": ["wi_20261015_401"], "t2": ["wi_20261015_401"]}, ["wi_20261015_401"]),
])
def test_every_window_of_a_session_that_created_a_commit_must_prove_its_request_wi(
        tmp_path, monkeypatch, proofs, expected):
    # Review round 1: adjacent requests' windows within the slack both match one
    # commit; the first window's proof alone must not decide the author's WI.
    from harness_rewrite import session_compute
    from harness_rewrite.session_compute import find_authors

    repo, _, _, _ = committed_repository(tmp_path)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    head = commit_now(repo, "-m", "one")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    rows = [{**r, "sessionId": "s-overlap"} for call in ("t1", "t2") for r in cc_commit(
        call, f"cd {repo} && git commit -q -m one", "", cwd=str(repo),
        start=before.isoformat(), end=after.isoformat())]
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "s-overlap.jsonl").write_text(lines(rows))
    monkeypatch.setattr(session_compute, "request_proof", lambda _: proofs.get)
    [author] = find_authors([("cc", root)], [head], 0)[head]
    assert author["request_declared_wis"] == expected
