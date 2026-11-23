"""Observed model, reasoning effort and commits of one authenticated native session.

The PR review gate never trusts a session's own claim. The Host reads the
session's original Claude Code, Codex, Pi or agy record: CC assistant rows carry
`message.model` and `effort`; Codex `turn_context` rows carry `model` and
`effort`; a Pi assistant message carries `model`, and the latest
`thinking_level_change` row before it the effort; an agy `PLANNER_RESPONSE`
step carries the model ID, whose tier suffix (`-low` .. `-xhigh`) is the
effort. Every distinct pair is
reported; a turn without either value is reported as unknown, which the
kernel treats as a missing record. A complete row that cannot be read makes the
whole record unusable; only an unfinished last row is ignored.

The record also yields commit candidates: every command-execution tool call
running `git ... commit` (or merge, pull) is a commit window with the call's
model, workspace (its cwd, a `cd <dir>` before the commit, then `git -C`) and
start/end timestamps; the WIs the session declared with `declare_session_wi`
come along. The call's output is not needed (`git commit -q` prints nothing); a
`[branch <sha>]` line it happens to print is kept as `printed` only. Text alone
is never proof: the workspace's own HEAD log decides which commits a window
created. At registration the Host reads that workspace's HEAD log (a linked
worktree's `worktrees/<name>/logs/HEAD`) and takes the entries where HEAD moved
(old sha differs from new) by a commit-type operation (`commit`, `commit (amend)`,
`commit (merge)`, a non-fast-forward merge or pull) between the call's start
and end; a commit so reached that exists and is on a branch is that call's
commit (`verify_commits`). A successful no-op command moves nothing and claims
nothing. Only confirmed evidence binds a pull request's author and author model.

A Pi record starts with its `session` header (the native id and the cwd Pi
opens the session in, where its tools run); any other first row makes the
record unusable. A fork or branch (`parentSession`) copies the parent's entries
unchanged, ids and times included, before its own header is written: a message
older than the header (or without a readable time) is inherited history, never
this session's turn or commit. Its declarations still count: ComputeCost reads
the fork's record as is and binds the fork's requests by them, so a fork whose
whole record declares two WIs is refused, as ComputeCost charges it to both. A `toolCall` in an assistant
message starts a window at that row's timestamp (Pi appends the message before
it runs the tool) and its `toolResult` message (same `toolCallId` and tool
name) ends it; `isError` claims nothing, as CC's `is_error`. A
`declare_session_wi` counts as ComputeCost (`pi_request_purpose`) qualifies it:
one call with exactly the argument `wi`, its one non-error result whose single
text is exactly the Relay extension's ack, both on one request's parentId chain.

An agy (Antigravity CLI) transcript is `<brain>/<conversation>/` +
`session_sources.AGY_TRANSCRIPT`; the conversation directory names the session.
Every row is one step (`step_index`, `type`); a row without them makes the
record unusable. A `PLANNER_RESPONSE` step's `run_command` tool calls carry the
command (`CommandLine`) but no id, and the transcript records no working
directory: only a literal absolute `cd <dir>` or `git -C <dir>` names a commit's
workspace. The `GENERIC` steps after it, up to the next model step, are the
step's tool results; none names its call, a successful command result does not
echo its command, and parallel results are written in completion order. So a
result is bound to a command only when that is the one reading: the step ran
exactly one `run_command`, and exactly one result after it is a command result
(its text, after its leading `Created At:`/`Completed At:` lines, starts with
agy's command result line). The call's window runs from the step's time to that
result's `Completed At`. Output text has no source marking (a compound command or
a Git hook prints the same shapes), so an agy call proves a commit only in the one
shape that excludes other output statically (`agy_commit_shape`): the command
line is exactly `cd <literal absolute dir> && git commit <args>` (no other
operator, subshell, variable, substitution or comment), with `--no-verify` or
`-n` (no pre-commit or commit-msg hook) and without `-q`/`--quiet`; the result
says `Command exited with code 0.`, the step is not an error, and the first
output line is git's own summary `[<branch> <sha>] <subject>` (kept as
`agy_commit`). The workspace HEAD log must then hold exactly one commit-type move
inside the call's window, to `head`, named by that sha (`claimed`): a second
commit in the window (another call or session in the same workspace) leaves the
author unknown. Any other shape claims nothing. A step with several commands, a result without `Completed At`,
a truncated or unrecognised result, a failure or a missing result claims
nothing, and a record whose step indexes repeat or go back claims no commit at
all: the author stays unknown. agy has no `declare_session_wi`: its
declared WIs are empty.

A Claude Code session's sub-agents (Agent/Task tool) write their own records
at `<session>/subagents/agent-<agentId>.jsonl`. A child record counts only when
the parent record proves it spawned that child: an Agent/Task tool call whose
result row names the child (`toolUseResult.agentId`), the child's meta file (if
present) names the same tool call, and every message row of the child is a
sidechain row of that agentId and the parent's sessionId. Its commits are
verified like the parent's, with the child's own model and its own session
identity (`<parent>/subagents/agent-<agentId>`). Only direct children are read.
A child whose first prompt starts with the line `[WI: <wi>]` (the dispatch
marker for a child WorkItem) reports that WI as `wi_marker`, else null, and
`dispatch_declared_wis`: the WIs ComputeCost charges its turns to, as proven by
`child_dispatch` (null when that proof fails). The Host only reads these; the
kernel decides whether they attribute the child.

Threat model: this prevents mistakes and unintended self-review (a session
claiming another session's commit, echoed commit lines). An Agent that
deliberately forges Git objects or reflogs is outside the kernel's protection;
such residue is left to later audit (Stats anomalies).

The Relay `review` profile is resolved by the installed Relay's read-only
resolver in a fixed environment: no caller variable (NODE_OPTIONS, catalog
overrides) reaches it; the Relay path map comes from the installed policy.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import subprocess
from typing import Any, Callable

from . import child_dispatch
from .model import Json
from .session_sources import AGY_TRANSCRIPT
# `git`, its global options (`-C <dir>`, `-c <k=v>`, `--opt[=value]`), then the subcommand.
from .shell_scope import COMMITTING, _literal, _tokens, workspace

MAX_BYTES = 256 * 1024 * 1024
# A native session record is streamed row by row, so its size never bounds memory.
# A long-lived conversation (one CC session resumed for weeks) passes 600 MB.
MAX_RECORD_BYTES = 8 * 1024 * 1024 * 1024
# The prefilter reads a record in chunks; a match never spans more than this.
MENTION_OVERLAP = 4200
MAX_COMMITS = 256
UNKNOWN = "unknown"
COMMIT_LINE = re.compile(r"\[[^\]\s\\]{1,200}(?: \([a-z -]{1,40}\))? ([0-9a-f]{7,40})\]")
DECLARE = "declare_session_wi"
# Declaring "none" (chat, read-only) names no WI; it never counts toward output.
NO_WI = "none"
SPAWN_TOOLS = frozenset({"Agent", "Task"})
# Tools that execute a command: CC Bash; Codex shell, exec_command and code-mode scripts; Pi bash;
# agy run_command.
EXEC_TOOLS = frozenset(
    {"Bash", "shell", "shell_command", "exec_command", "exec", "js", "local_shell", "bash",
     "run_command"}
)
RUNTIMES = frozenset({"cc", "codex", "pi", "agy"})
# agy: the command tool, a model ID's tier suffix (its effort), and a result's own times.
AGY_COMMAND = "run_command"
AGY_TIER = re.compile(r"-(minimal|low|medium|high|xhigh|max)\Z")
AGY_TIMES = re.compile(r"(Created|Completed) At: (\S+)")
# The start of agy's own result of a command call (after the time lines).
AGY_COMMAND_RESULT = re.compile(
    r"Command exited with code -?\d+\.|Command failed to run:|Encountered error in step "
    r"execution: permission check failed for (?:command|unsandboxed) \"")
AGY_SUCCESS = re.compile(r"Command exited with code 0\.(?:\n|\Z)")
# The summary line `git commit` prints for the commit it created: `[<branch> <sha>]
# <subject>` or `[<branch> (root-commit) <sha>] <subject>`; an agy call's first output line.
AGY_COMMIT_SUMMARY = re.compile(r"\[[^\]\s\\]{1,200}(?: \(root-commit\))? ([0-9a-f]{7,64})\] .+")
AGY_OUTPUT = re.compile(r"Command exited with code 0\.\n\nCommand output:\n([^\n]*)")
# A literal absolute directory: no quoting, globbing, home or variable is needed to read it.
AGY_DIRECTORY = re.compile(r"/[A-Za-z0-9._/+@,:=-]*")
# `git commit` options that take the next word as their value (that word is no option).
COMMIT_VALUE_OPTIONS = frozenset({
    "-m", "--message", "-F", "--file", "-c", "-C", "--reuse-message", "--reedit-message",
    "--author", "--date", "-t", "--template", "--cleanup", "--fixup", "--squash", "--trailer",
    "--pathspec-from-file", "-S", "--gpg-sign"})
AGY_ID = re.compile(r"[A-Za-z0-9-]{1,128}\Z")
# A Pi record's file name: `<start time>_<session id>.jsonl`.
PI_RECORD = re.compile(r"\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-\d{3}Z_([A-Za-z0-9-]{1,128})\.jsonl")
# The Relay Pi extension's declare_session_wi ack, and the WIs it accepts.
PI_ACK = "pi_request_wi/v1"
PI_WI = re.compile(r"wi_[0-9]{8}_[0-9]{3}")
# Entries a Pi request's chain passes through on its way to the user message.
PI_REQUEST_PATH = frozenset({"message", "model_change", "thinking_level_change", "custom", "compaction"})
# Relay keeps Pi records directly in a profile's root; standalone Pi one directory deeper.
PI_DEPTH = 2
# The Pi process's HOME inside a Relay profile root: nothing there is a session record.
PI_HOME = "agent"
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_SUBAGENTS = 64
# Clock slack between a native row's timestamp and Git's reflog second.
SLACK_SECONDS = 2
# A sub-agent dispatched for a child WorkItem: the first line of its first prompt.
WI_MARKER = re.compile(r"\[WI: (wi_\d{8}_\d{3})\]")
# A marked sub-agent's cost evidence (`child_dispatch`), carried to the kernel.
DISPATCH = ("dispatch_declared_wis", "dispatch_since", "cost_child", "cost_served")


def _text(value: Any) -> str:
    return value if isinstance(value, str) and value and len(value) <= 256 else UNKNOWN


def _command(value: Any) -> str:
    """The shell command a tool call actually ran, decoded from its arguments."""
    if isinstance(value, dict):
        inner = value.get("command", value.get("cmd"))
        if isinstance(inner, list) and all(isinstance(v, str) for v in inner):
            return shlex.join(inner)
        return inner if isinstance(inner, str) else json.dumps(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return shlex.join(value)
    if not isinstance(value, str):
        return json.dumps(value)
    try:
        decoded = json.loads(value)
    except ValueError:
        decoded = None
    if isinstance(decoded, dict):
        return _command(decoded)
    # A Codex script call: tools.exec_command({cmd: "<JSON string>"}).
    match = re.search(r"\bcmd\s*:\s*(\"(?:[^\"\\]|\\.)*\")", value)
    if match:
        try:
            return str(json.loads(match.group(1)))
        except ValueError:
            pass
    return value


def _committing(command: str) -> bool:
    """A call that can create a commit: git commit, merge or pull anywhere in the
    command (after `cd ... &&`, inside a compound command or a script)."""
    return COMMITTING.search(command) is not None


def _workdir(raw: Any) -> str | None:
    """The working directory a Codex tool call names (`workdir`), if any."""
    value: Any = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            match = re.search(r"[\"']?workdir[\"']?\s*:\s*\"([^\"]+)\"", raw)
            return match.group(1) if match else None
    if isinstance(value, dict) and isinstance(value.get("workdir"), str):
        return str(value["workdir"])
    return None


def _acknowledged(text: str, wi: Any) -> bool:
    """The declaration server's own ack for this WI (as ComputeCost requires)."""
    try:
        value = json.loads(text)
    except ValueError:
        return False
    return (
        isinstance(value, dict)
        and value.get("wi") == wi
        and value.get("origin") == "agent_declaration"
    )


def _output(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_output(v.get("text") if isinstance(v, dict) else v) for v in value)
    return ""


def _directory(base: str | None, text: str) -> str | None:
    """The literal directory word at the start of `text`, resolved against `base`."""
    try:
        # Shell quoting decides the directory: quotes and spaces are respected.
        target = shlex.split(text)[0]
    except (ValueError, IndexError):
        return None
    if not target or target.startswith("-") or any(c in target for c in "$`~;&|"):
        return None  # A variable, `cd -` or no argument: the workspace is unknown.
    if target.startswith("/"):
        return target
    return None if base is None else str(Path(base) / target)


def _repository(cwd: Any, command: str) -> str | None:
    """The workspace a commit command ran in, as its shell scopes prove it
    (`shell_scope.workspace`); a non-committing call keeps its `git -C` or cwd."""
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return None
    if COMMITTING.search(command) is not None:
        return workspace(cwd, command)
    match = re.search(r"\bgit\s+-C\s+", command)
    return cwd if match is None else _directory(cwd, command[match.end():])


class Reader:
    def __init__(self, runtime: str, child: tuple[str, str] | None = None):
        self.runtime = runtime
        # (agentId, parent sessionId) when reading a CC sub-agent's own record.
        self.child = child
        self.spawns: set[str] = set()
        self.children: dict[str, str] = {}
        self.turns: dict[tuple[str, str], Json] = {}
        self.observed = 0
        self.model = UNKNOWN
        self.cwd: Any = None
        self.calls: dict[str, Json] = {}
        self.commits: dict[str, Json] = {}  # commit windows by tool call, oldest first
        self.declared: set[str] = set()
        self.native_id: str | None = None
        self.prompt: str | None = None  # A sub-agent's first prompt.
        self.effort = UNKNOWN  # Pi: the latest thinking level.
        self.opened: float | None = None  # Pi: the header's time; earlier rows are copied.
        self.entries: dict[str, Json | None] = {}  # Pi entries by id, None when conflicting.
        self.pi_calls: dict[str, list[tuple[Any, Any]]] = {}  # declare_session_wi calls
        self.pi_results: dict[str, list[tuple[Any, Json]]] = {}  # and their results
        # agy: the latest model step's command calls and the command results after it.
        self.agy_calls: list[str] = []
        self.agy_results: list[tuple[str | None, bool, str]] = []  # (completed, success, text)
        self.agy_step = -1  # The last step index read; steps must strictly increase.
        self.agy_disordered = False  # A repeated or out-of-order step: no commit is claimed.

    def turn(self, model: str, effort: str) -> None:
        self.observed += 1
        self.model = model
        self.turns.setdefault((model, effort), {"model": model, "effort": effort})

    def call(self, call_id: Any, name: Any, command: str, cwd: Any, at: Any, wi: Any = None) -> None:
        self.calls[str(call_id)] = {
            "name": name, "command": command, "model": self.model,
            "repository": _repository(cwd, command), "started_at": at, "wi": wi,
        }

    def result(self, call_id: Any, text: str, at: Any, failed: bool = False) -> None:
        call = self.calls.get(call_id) if isinstance(call_id, str) else None
        if call is None or failed:
            return
        if isinstance(call["name"], str) and call["name"].endswith(DECLARE):
            if isinstance(call["wi"], str) and call["wi"] != NO_WI and _acknowledged(text, call["wi"]):
                self.declared.add(call["wi"])
            return
        if call["name"] not in EXEC_TOOLS or not _committing(call["command"]):
            # Only a command execution can create a commit; an Agent/Task prompt that
            # mentions `git commit` only proves a spawn, never a commit.
            return
        if call_id not in self.commits and len(self.commits) >= MAX_COMMITS:
            # Keep the newest windows: a pull request's head is usually the last commit.
            del self.commits[next(iter(self.commits))]
        self.commits[call_id] = {
            # Only the repository sets `sha` (`verify_commits`); output is a hint.
            "sha": None, "printed": sorted(set(COMMIT_LINE.findall(text)))[:8],
            "model": call["model"], "repository": call["repository"],
            "started_at": call["started_at"], "ended_at": at, "verified": False,
            "tool_use_id": call_id,
        }

    def spawned(self, row: Json, content: list[Any]) -> None:
        """A sub-agent this session spawned: its Agent/Task result row names it."""
        if self.child is not None:
            return  # Only direct children of the registering session are read.
        results = [i for i in content if isinstance(i, dict) and i.get("type") == "tool_result"]
        meta = row.get("toolUseResult")
        if len(results) != 1 or results[0].get("tool_use_id") not in self.spawns or not isinstance(
            meta, dict
        ):
            return
        agent = meta.get("agentId")
        if isinstance(agent, str) and AGENT_ID.match(agent) and len(self.children) < MAX_SUBAGENTS:
            self.children.setdefault(agent, str(results[0]["tool_use_id"]))

    def pi_row(self, row: Json, at: Any) -> None:
        """One row of a Pi session record."""
        if row.get("type") == "session":
            if self.native_id is not None or not isinstance(row.get("id"), str) or not row["id"]:
                raise ValueError("Pi session header")
            self.native_id, self.cwd = row["id"], row.get("cwd")
            self.opened = _instant(row.get("timestamp"))
            return
        if self.native_id is None:
            raise ValueError("Pi record without its session header")
        ident = row.get("id")
        if isinstance(ident, str) and ident:
            # A conflicting copy of one entry proves nothing (as ComputeCost).
            seen = self.entries.get(ident, row)
            self.entries[ident] = row if seen == row else None
        if row.get("type") == "thinking_level_change":
            self.effort = _text(row.get("thinkingLevel"))
            return
        message = row.get("message")
        if row.get("type") != "message" or not isinstance(message, dict):
            return
        raw = message.get("content")
        content: list[Any] = raw if isinstance(raw, list) else []
        # Declarations count in the whole record, copied history included: ComputeCost
        # reads a fork's record as is, so its copied declarations bind its requests too.
        if message.get("role") == "assistant":
            for item in content:
                if isinstance(item, dict) and item.get("type") == "toolCall" and item.get("name") == DECLARE:
                    self.pi_calls.setdefault(str(item.get("id")), []).append((ident, item.get("arguments")))
        elif message.get("role") == "toolResult" and message.get("toolName") == DECLARE:
            self.pi_results.setdefault(str(message.get("toolCallId")), []).append((ident, message))
            return
        moment = _instant(at)
        if self.opened is None or moment is None or moment < self.opened:
            # A fork or branch copies its parent's entries with their own times
            # before writing its header: that history is the parent's execution.
            return
        if message.get("role") == "assistant":
            self.turn(_text(message.get("model")), self.effort)
            for item in content:
                if (isinstance(item, dict) and item.get("type") == "toolCall"
                        and item.get("name") != DECLARE):
                    self.call(item.get("id"), item.get("name"), _command(item.get("arguments")),
                              self.cwd, at)
        elif message.get("role") == "toolResult":
            call = self.calls.get(str(message.get("toolCallId")))
            if call is not None and call["name"] == message.get("toolName"):
                self.result(message.get("toolCallId"), _output(content), at,
                            message.get("isError") is True)

    def pi_request(self, start: Any) -> str | None:
        """The user message a Pi entry answers: its parentId chain, as ComputeCost walks it."""
        key, seen = start, set()
        while isinstance(key, str) and key in self.entries and key not in seen:
            seen.add(key)
            row = self.entries[key]
            if row is None:
                return None
            message = row.get("message") if row.get("type") == "message" else None
            if isinstance(message, dict) and message.get("role") == "user":
                return key
            if row.get("type") not in PI_REQUEST_PATH:
                return None
            key = row.get("parentId")
        return None

    def pi_declared(self) -> None:
        """Declarations ComputeCost (`pi_request_purpose`) qualifies: one call, its one
        result, both answering the same request, exactly `{wi}` and the exact ack."""
        for call_id, calls in self.pi_calls.items():
            results = self.pi_results.get(call_id, [])
            if len(calls) != 1 or len(results) != 1:
                continue
            (use, arguments), (answer, message) = calls[0], results[0]
            if (
                message.get("isError") is True
                or not isinstance(arguments, dict) or set(arguments) != {"wi"}
                or not isinstance(arguments["wi"], str)
                or (arguments["wi"] != NO_WI and not PI_WI.fullmatch(arguments["wi"]))
            ):
                continue
            content = message.get("content")
            if not (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
                    and content[0].get("type") == "text" and isinstance(content[0].get("text"), str)):
                continue
            try:
                acknowledged = json.loads(content[0]["text"]) == {
                    "schema": PI_ACK, "wi": arguments["wi"], "origin": "agent_declaration"}
            except ValueError:
                continue
            request = self.pi_request(use)
            if acknowledged and request is not None and self.pi_request(answer) == request:
                if arguments["wi"] != NO_WI:
                    self.declared.add(arguments["wi"])

    def agy_row(self, row: Json) -> None:
        """One step of an agy transcript."""
        if not isinstance(row.get("step_index"), int) or not isinstance(row.get("type"), str):
            raise ValueError("agy step")
        if row["step_index"] <= self.agy_step:
            self.agy_disordered = True
        self.agy_step = max(self.agy_step, row["step_index"])
        at = row.get("created_at")
        kind = row["type"]
        if kind == "PLANNER_RESPONSE":
            self.agy_settle()
            # A call belongs to its own model step only: nothing carries over to the next.
            self.calls = {}
            model = _text(row.get("model"))
            tier = AGY_TIER.search(model) if model != UNKNOWN else None
            self.turn(model, tier.group(1) if tier else UNKNOWN)
            calls = row.get("tool_calls")
            for index, call in enumerate(calls if isinstance(calls, list) else []):
                if not isinstance(call, dict) or call.get("name") != AGY_COMMAND:
                    continue
                args = call.get("args")
                command = args.get("CommandLine") if isinstance(args, dict) else None
                ident = f"{row['step_index']}.{index}"
                # Every command counts toward the step: two commands make it unreadable.
                self.agy_calls.append(ident)
                if isinstance(command, str):
                    self.calls[ident] = {
                        "name": AGY_COMMAND, "command": command, "model": self.model,
                        "repository": _agy_repository(command), "started_at": at, "wi": None,
                    }
        elif kind == "GENERIC" and self.agy_calls:
            content = row.get("content")
            text = content if isinstance(content, str) else ""
            # A result begins with its own `Created At:`/`Completed At:` lines (local
            # time with offset); the row's `created_at` is only when the tool started.
            lines = text.split("\n")
            completed: str | None = None
            index = 0
            while index < len(lines) and (found := AGY_TIMES.fullmatch(lines[index])):
                if found.group(1) == "Completed":
                    completed = found.group(2)
                index += 1
            body = "\n".join(lines[index:])
            if AGY_COMMAND_RESULT.match(body) is None:
                return  # Another tool's result, or a truncated one naming no command.
            success = (AGY_SUCCESS.match(body) is not None and row.get("status") == "DONE"
                       and not row.get("error"))
            self.agy_results.append((completed, success, body))

    def agy_settle(self) -> None:
        """Bind the latest model step's one command to its one command result."""
        calls, results = self.agy_calls, self.agy_results
        self.agy_calls, self.agy_results = [], []
        if len(calls) != 1 or len(results) != 1 or calls[0] not in self.calls:
            return  # No result, or no single reading: the author stays unknown.
        completed, success, body = results[0]
        if not success or completed is None or _instant(completed) is None:
            return
        self.result(calls[0], body, completed)
        window = self.commits.get(calls[0])
        if window is None:
            return
        shape = agy_commit_shape(self.calls[calls[0]]["command"])
        output = AGY_OUTPUT.match(body)
        summary = AGY_COMMIT_SUMMARY.fullmatch(output.group(1).rstrip("\r")) if output else None
        if shape is None or summary is None:
            del self.commits[calls[0]]  # Not the one provable shape: the author stays unknown.
            return
        window.update(repository=shape, agy_commit=summary.group(1))

    def row(self, row: Any) -> None:
        if not isinstance(row, dict):
            raise ValueError("row")
        if self.runtime == "agy":
            self.agy_row(row)
            return
        at = row.get("timestamp")
        if self.runtime == "pi":
            self.pi_row(row, at)
            return
        if self.runtime == "cc":
            message = row.get("message")
            if self.child is not None and row.get("type") in {"user", "assistant"} and (
                row.get("isSidechain") is not True
                or (row.get("agentId"), row.get("sessionId")) != self.child
            ):
                raise ValueError("row of another session in a sub-agent record")
            if not isinstance(message, dict) or (row.get("isSidechain") and self.child is None):
                return
            if self.native_id is None and isinstance(row.get("sessionId"), str):
                self.native_id = row["sessionId"]
            raw = message.get("content")
            content: list[Any] = raw if isinstance(raw, list) else []
            if row.get("type") == "assistant":
                if message.get("model") == "<synthetic>":
                    return  # Claude Code's local error placeholder, not a model turn.
                self.turn(_text(message.get("model")), _text(row.get("effort")))
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        if item.get("name") in SPAWN_TOOLS and isinstance(item.get("id"), str):
                            self.spawns.add(item["id"])
                        data = item.get("input")
                        wi = data.get("wi") if isinstance(data, dict) else None
                        self.call(item.get("id"), item.get("name"), _command(data),
                                  row.get("cwd"), at, wi)
            elif row.get("type") == "user":
                if self.child is not None and self.prompt is None:
                    self.prompt = _prompt(message.get("content"))
                self.spawned(row, content)
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        self.result(item.get("tool_use_id"), _output(item.get("content")), at,
                                    item.get("is_error") is True)
            return
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return
        if row.get("type") == "session_meta" and self.native_id is None:
            self.native_id = payload.get("id") if isinstance(payload.get("id"), str) else None
        if row.get("type") == "turn_context":
            self.cwd = payload.get("cwd")
            self.turn(_text(payload.get("model")), _text(payload.get("effort")))
        elif row.get("type") == "response_item" and payload.get("type") in {
            "function_call", "custom_tool_call", "local_shell_call",
        }:
            raw = payload.get("arguments", payload.get("input", payload.get("action")))
            command = _command(raw)
            # The tool's own workdir comes first; `git -C` is resolved against it.
            workdir = _workdir(raw)
            cwd = (workdir if workdir is None or workdir.startswith("/")
                   else str(Path(str(self.cwd)) / workdir)) or self.cwd
            name = "local_shell" if payload.get("type") == "local_shell_call" else payload.get("name")
            self.call(payload.get("call_id"), name, command, cwd, at)
        elif row.get("type") == "response_item" and payload.get("type") in {
            "function_call_output", "custom_tool_call_output", "local_shell_call_output",
        }:
            self.result(payload.get("call_id"), _output(payload.get("output")), at)
        elif row.get("type") == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item")
            if (
                isinstance(item, dict) and item.get("type") == "McpToolCall"
                and item.get("tool") == DECLARE and item.get("status") == "completed"
                and isinstance(item.get("arguments"), dict)
                and isinstance(item["arguments"].get("wi"), str)
                and item["arguments"]["wi"] != NO_WI
                and isinstance(item.get("result"), dict)
                and not item["result"].get("isError")
                and _acknowledged(_output(item["result"].get("content")), item["arguments"]["wi"])
            ):
                self.declared.add(item["arguments"]["wi"])


def agy_commit_shape(command: str) -> str | None:
    """The workspace of the only commit command an agy call can prove: exactly
    `cd <literal absolute dir> && git commit <literal args>` with `--no-verify` or
    `-n` and without `-q`/`--quiet`; None for any other shape."""
    tokens = _tokens(command)
    if tokens is None or len(tokens) < 5 or [k for k, _ in tokens[:5]] != ["w", "w", "op", "w", "w"]:
        return None
    words = [value for _, value in tokens]
    if (any(kind != "w" for kind, _ in tokens[5:]) or words[0] != "cd" or words[2] != "&&"
            or words[3:5] != ["git", "commit"]):
        return None
    directory, args = words[1], words[5:]
    if AGY_DIRECTORY.fullmatch(directory) is None or ".." in directory.split("/"):
        return None
    if any(not _literal(arg) or arg.startswith("#") for arg in args):
        return None  # A variable, a substitution or a comment: not read statically.
    flags = [arg for index, arg in enumerate(args)
             if index == 0 or args[index - 1] not in COMMIT_VALUE_OPTIONS]
    if "--no-verify" not in flags and "-n" not in flags:
        return None
    if any(flag in {"-q", "--quiet"} or re.fullmatch(r"-[A-Za-z]*q[A-Za-z]*", flag) for flag in flags):
        return None
    return os.path.normpath(directory)


def _agy_repository(command: str) -> str | None:
    """The workspace an agy command names itself: agy records no working directory."""
    if COMMITTING.search(command) is not None:
        return workspace(None, command)
    match = re.search(r"\bgit\s+-C\s+", command)
    return None if match is None else _directory(None, command[match.end():])


def agy_conversation(path: Path) -> str | None:
    """The conversation an agy transcript belongs to: its brain directory's name."""
    parts = path.parts[-len(AGY_TRANSCRIPT.parts) - 1:]
    if len(parts) != len(AGY_TRANSCRIPT.parts) + 1 or Path(*parts[1:]) != AGY_TRANSCRIPT:
        return None
    return parts[0] if AGY_ID.match(parts[0]) else None


def _prompt(content: Any) -> str:
    if isinstance(content, str):
        return content
    texts = [str(item["text"]) for item in content if isinstance(item, dict)
             and item.get("type") == "text" and isinstance(item.get("text"), str)
             ] if isinstance(content, list) else []
    return "\n".join(texts)


def wi_marker(prompt: str | None) -> str | None:
    """The child WorkItem a sub-agent's first prompt names on its first line."""
    found = WI_MARKER.match((prompt or "").split("\n", 1)[0])
    return found.group(1) if found else None


def _read(reader: Reader, path: Path) -> str | None:
    """Feed every complete row to the reader; the record's digest, or None."""
    sha = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if os.fstat(stream.fileno()).st_size > MAX_RECORD_BYTES:
                return None
            for line in stream:
                if not line.endswith(b"\n"):
                    break  # Only an unfinished last row is still being written.
                sha.update(line)
                if line.strip():
                    reader.row(json.loads(line))  # Every complete row must be readable.
    except (OSError, ValueError):
        return None
    return sha.hexdigest()


def subagent_dir(path: Path) -> Path:
    """Where Claude Code keeps the records of a session's sub-agents."""
    return path.with_suffix("") / "subagents"


def _subagents(path: Path, parent: Reader) -> list[Json]:
    """Direct sub-agents the parent record proves it spawned, with their own facts."""
    found: list[Json] = []
    if parent.native_id is None:
        return found
    directory = subagent_dir(path)
    main: list[Any] = []

    def charged(agent: str, child: Path, marker: str | None) -> Json:
        """What ComputeCost charges a marked sub-agent's turns to, and the child
        WorkItem as ComputeCost's registry exports it."""
        if marker is None:
            return {"dispatch_declared_wis": None}
        if not main:  # Read the session's records once more, only for a marked sub-agent.
            main.append(child_dispatch.index(path, path.stem))
        proof = child_dispatch.charged(
            main[0], child_dispatch.index(child, path.stem, agent), path.stem, marker,
            child_dispatch.copies(path.with_suffix(""), {child}))
        if proof is None:
            return {"dispatch_declared_wis": None}
        # Every other WI the turns were declared to, as the same export reads it:
        # a sibling under the child's parent leaves the turns to the child.
        return {"dispatch_declared_wis": proof[0], "dispatch_since": proof[1],
                "cost_child": child_dispatch.exported_child(marker),
                "cost_served": {wi: child_dispatch.exported_child(wi)
                                for wi in proof[0] if wi != marker}}

    for agent, call in sorted(parent.children.items()):
        meta = directory / f"agent-{agent}.meta.json"
        try:
            if meta.is_symlink() or (
                meta.exists() and json.loads(meta.read_text()).get("toolUseId") != call
            ):
                continue
        except (OSError, ValueError, AttributeError):
            continue
        child = Reader("cc", (agent, parent.native_id))
        digest = _read(child, directory / f"agent-{agent}.jsonl")
        if digest is None or not child.observed:
            continue
        found.append({
            "native_id": f"{parent.native_id}/subagents/agent-{agent}",
            "tool_use_id": call,
            "turns": [child.turns[k] for k in sorted(child.turns)],
            "commits": list(child.commits.values()),
            "declared_wis": sorted(child.declared),
            "wi_marker": wi_marker(child.prompt),
            **charged(agent, directory / f"agent-{agent}.jsonl", wi_marker(child.prompt)),
            "source_sha256": digest,
        })
    return found


def observe(runtime: str, path: Path | None) -> Json | None:
    """All observed turns, commits and WI declarations of the record, or None."""
    if runtime not in RUNTIMES or path is None:
        return None
    reader = Reader(runtime)
    digest = _read(reader, path)
    if digest is None or (runtime == "pi" and reader.native_id is None):
        return None
    if runtime == "pi":
        reader.pi_declared()
    if runtime == "agy":
        reader.agy_settle()
        if reader.agy_disordered:
            reader.commits = {}  # Steps repeat or go back: which result is whose is unknown.
        reader.native_id = agy_conversation(path)
        if reader.native_id is None:
            return None
    return {
        "schema": "harness.session-compute/v1",
        "runtime": runtime,
        "turns": [reader.turns[k] for k in sorted(reader.turns)],
        "observed_turns": reader.observed,
        "commits": list(reader.commits.values()),
        "declared_wis": sorted(reader.declared),
        "native_id": reader.native_id,
        "source_sha256": digest,
        **({"subagents": _subagents(path, reader)} if runtime == "cc" else {}),
    }


def _instant(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _git(repository: str, *args: str) -> str | None:
    environment = {"HOME": pwd.getpwuid(os.getuid()).pw_dir, "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
                   "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", repository, *args],
            capture_output=True, text=True, timeout=30, env=environment,
            stdin=subprocess.DEVNULL, cwd="/",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


# A HEAD move by a commit-type operation: (HEAD log file, line number, new sha, unix second).
Move = tuple[str, int, str, int]
# The commit-type HEAD moves of each workspace; None when its HEAD log is unreadable.
Reflogs = dict[str, list[Move] | None]
HEAD_LOG_LINE = re.compile(r"([0-9a-f]{40,64}) ([0-9a-f]{40,64}) [^\t]*> (\d+) [+-]\d{4}\t(.*)")


def _moves(workspace: str, reflogs: Reflogs) -> list[Move]:
    """Where this workspace's own HEAD was moved by creating a commit.

    Only the workspace's HEAD log counts: `<gitdir>/logs/HEAD`, for a linked
    worktree `<common>/worktrees/<name>/logs/HEAD`. Another worktree's commit, a
    branch moved elsewhere or a fast-forward never moves this HEAD to a new commit.
    """
    if workspace not in reflogs:
        moves: list[Move] | None = None
        found = _git(workspace, "rev-parse", "--path-format=absolute", "--git-path", "logs/HEAD")
        log = found.strip() if found else ""
        if log.startswith("/"):
            try:
                fd = os.open(log, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    data = stream.read(MAX_BYTES + 1)
            except OSError:
                data = None
            if data is not None and len(data) <= MAX_BYTES:
                moves = []
                for number, raw in enumerate(data.decode("utf-8", "replace").splitlines()):
                    entry = HEAD_LOG_LINE.fullmatch(raw)
                    if (
                        entry is not None
                        and entry.group(1) != entry.group(2)  # HEAD actually moved
                        and _creates(entry.group(4))
                    ):
                        moves.append((log, number, entry.group(2), int(entry.group(3))))
        reflogs[workspace] = moves
    return reflogs[workspace] or []


# HEAD log actions that create the commit HEAD moves to. Checkout, reset, a
# fast-forward and a rebase's `(start)`/`(finish)` move to existing objects.
CREATING_ACTIONS = (
    re.compile(r"commit(?: \((?:amend|merge|initial)\))?"),
    re.compile(r"(?:rebase|pull)(?: [^():]*)? \((?:pick|reword|edit|squash|fixup|continue)\)"),
)
MERGING_ACTION = re.compile(r"(?:merge|pull)(?: [^():]*)?")


def _creates(message: str) -> bool:
    """A HEAD log message of an operation that created the commit it moved to."""
    action, separator, detail = message.partition(":")
    if not separator:
        return False
    if any(pattern.fullmatch(action) for pattern in CREATING_ACTIONS):
        return True
    return MERGING_ACTION.fullmatch(action) is not None and detail.lstrip().startswith(
        "Merge made by"
    )


def claimed(evidence: Json, head: str, reflogs: Reflogs | None = None) -> Move | None:
    """The workspace HEAD move by which this tool call created `head`, if any: the
    call's own workspace HEAD log moved to `head` by a commit-type operation
    between the call's start and end (2 s slack), and the commit is on a branch. An
    agy call (`agy_commit` present) needs its git summary to name `head` and the
    window to hold no other commit-type move of that workspace."""
    workspace = evidence.get("repository")
    start, end = _instant(evidence.get("started_at")), _instant(evidence.get("ended_at"))
    if not isinstance(workspace, str) or start is None or end is None or end < start:
        return None
    inside = [m for m in _moves(workspace, {} if reflogs is None else reflogs)
              if start - SLACK_SECONDS <= m[3] <= end + SLACK_SECONDS]
    if "agy_commit" in evidence:
        sha = evidence["agy_commit"]
        if (not isinstance(sha, str) or len(sha) < 7 or not head.startswith(sha)
                or len(inside) != 1):
            return None  # Its own summary and the one commit of its window, or unknown.
    moves = [m for m in inside if m[2] == head]
    if not moves:
        return None
    if _git(workspace, "cat-file", "-e", head + "^{commit}") is None:
        return None
    branches = _git(workspace, "for-each-ref", "--contains", head, "--format=%(refname)",
                    "refs/heads", "refs/remotes")
    return moves[-1] if branches and branches.strip() else None


def committed_in_window(evidence: Json, head: str, reflogs: Reflogs | None = None) -> bool:
    return claimed(evidence, head, reflogs) is not None


def request_proof(record: Path) -> Callable[[Any], list[str] | None]:
    """The WIs ComputeCost charges the main turn of a CC record that made a tool
    call to (`child_dispatch.request_charged`); the record is indexed on first use."""
    read: list[Any] = []

    def proof(call: Any) -> list[str] | None:
        if not isinstance(call, str):
            return None
        if not read:
            read.extend([child_dispatch.index(record, record.stem),
                         child_dispatch.copies(record.with_suffix(""), set())])
        return child_dispatch.request_charged(read[0], record.stem, call, read[1])
    return proof


def verify_commits(compute: Json | None, head: str, record: Path | None = None) -> Json | None:
    """Bind each commit window that created `head` in its own workspace to it.

    Every commit a window created belongs to that call (one command may commit
    several times, or amend); the workspace's HEAD log decides, not the output.
    Two sessions claiming one HEAD move are both reported: the kernel then treats
    the commit's author as ambiguous. A CC session's own verified window
    carries `request_declared_wis`: the WIs ComputeCost charges the turn that
    ran it to, null when unproven (`request_proof` on `record`)."""
    if compute is None:
        return None
    reflogs: Reflogs = {}

    def checked(commits: list[Json]) -> list[Json]:
        return [{**c, "sha": head, "verified": True} if committed_in_window(c, head, reflogs)
                else {**c, "verified": False} for c in commits]

    verified = {**compute, "commits": checked(compute["commits"])}
    if record is not None and compute.get("runtime") == "cc":
        proof = request_proof(record)
        verified["commits"] = [
            {**c, "request_declared_wis": proof(c.get("tool_use_id"))} if c["verified"] else c
            for c in verified["commits"]]
    if "subagents" in compute:
        verified["subagents"] = [{**s, "commits": checked(s["commits"])}
                                 for s in compute["subagents"]]
    return verified


def find_authors(roots: list[tuple[str, Path]], shas: list[str], since: float) -> dict[str, list[Json]]:
    """Every session record under the Agent's roots that created one of `shas`.

    Each candidate passes the same workspace HEAD log check as the registering
    author. A CC session's proven sub-agents are searched with it, under their own
    identity. A Pi record counts only at Pi's own place and name for its header id
    (`PI_RECORD`, at most `PI_DEPTH` deep, outside `PI_HOME`). Each session appears
    once per commit; several sessions for one commit are all reported, and the
    kernel treats that author as ambiguous. An agy transcript counts only at its
    own place (`_agy_place`).
    """
    found: dict[str, list[Json]] = {sha: [] for sha in shas}
    reflogs: Reflogs = {}
    for runtime, root in roots:
        try:
            paths = sorted(root.rglob("*.jsonl"))
        except OSError:
            continue
        for path in paths:
            try:
                if (
                    path.is_symlink()
                    or "subagents" in path.parts
                    or path.stat().st_mtime < since
                    or path.stat().st_size > MAX_RECORD_BYTES
                    or not _mentions(path)
                ):
                    continue
            except OSError:
                continue
            if runtime == "pi" and not _pi_place(root, path):
                continue
            if runtime == "agy" and not _agy_place(root, path):
                continue
            compute = observe(runtime, path)
            if compute is None or compute.get("native_id") is None:
                continue
            if runtime == "pi" and not path.name.endswith(f"_{compute['native_id']}.jsonl"):
                continue  # The file names another session than its header.
            proof = request_proof(path) if runtime == "cc" else None
            for source in [compute, *compute.get("subagents", [])]:
                # A sub-agent keeps its own declarations apart from its session's:
                # the kernel checks each by its own rule.
                session = (
                    {} if source is compute
                    else {"session_declared_wis": sorted(compute["declared_wis"])}
                )
                for evidence in source["commits"]:
                    for sha in shas:
                        if not committed_in_window(evidence, sha, reflogs):
                            continue
                        known = [a for a in found[sha] if a["native_id"] == source["native_id"]]
                        if known:
                            # Every window that created the commit (adjacent requests'
                            # windows may overlap within the slack) must prove one WI.
                            if ("request_declared_wis" in known[0] and proof is not None
                                    and proof(evidence.get("tool_use_id"))
                                    != known[0]["request_declared_wis"]):
                                known[0]["request_declared_wis"] = None
                            continue
                        found[sha].append({
                                "native_id": source["native_id"], "runtime": runtime,
                                "model": evidence["model"],
                                "declared_wis": sorted(source["declared_wis"]),
                                **session, "wi_marker": source.get("wi_marker"),
                                **({} if source is not compute or proof is None else {
                                    "request_declared_wis": proof(evidence.get("tool_use_id"))}),
                                **({} if source is compute else {
                                    k: source[k] for k in DISPATCH if k in source}),
                            })
    return found


def _pi_place(root: Path, path: Path) -> bool:
    """A Pi session record's own place under a profile root."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return (len(parts) <= PI_DEPTH and parts[0] != PI_HOME
            and not any(p.startswith(".") for p in parts)
            and PI_RECORD.fullmatch(path.name) is not None)


def _agy_place(root: Path, path: Path) -> bool:
    """An agy transcript's own place: `<root>/<conversation>/` + AGY_TRANSCRIPT."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return len(parts) == len(AGY_TRANSCRIPT.parts) + 1 and agy_conversation(path) is not None


# A committing command in a record's raw JSON bytes: a cheap prefilter before parsing.
MENTIONS_COMMIT = re.compile(rb"\bgit\b[^\n]{0,4096}?\b(?:commit|merge|pull)\b")


def _chunk_mentions(file: Path, chunk: int = 16 * 1024 * 1024) -> bool:
    """MENTIONS_COMMIT in a file read in chunks, so a large record never loads whole."""
    tail = b""
    with file.open("rb") as stream:
        while True:
            data = stream.read(chunk)
            if not data:
                return False
            window = tail + data
            if MENTIONS_COMMIT.search(window):
                return True
            tail = window[-MENTION_OVERLAP:]


def _mentions(path: Path) -> bool:
    """The record, or one of its sub-agent records, runs a git commit command."""
    files = [path]
    directory = subagent_dir(path)
    if directory.is_dir() and not directory.is_symlink():
        files += sorted(directory.glob("agent-*.jsonl"))
    for file in files:
        if file.is_symlink() or file.stat().st_size > MAX_RECORD_BYTES:
            continue
        if _chunk_mentions(file):
            return True
    return False


def review_profile(policy: Json, runtime: str) -> Json | None:
    """Resolve the Relay `review` profile with the installed Relay in a fixed environment."""
    node, entry = policy.get("producer_node"), policy.get("relay_entry")
    gate = policy.get("pull_request_gate") or {}
    if runtime not in {"cc", "codex"} or not node or not entry or not gate.get("relay_paths"):
        return None
    resolver = Path(str(entry)).parent / "compute-profile-cli.js"
    environment = {
        # The account's Codex catalog and the installed Relay path map (model
        # catalog); nothing from the calling Agent session.
        "HOME": pwd.getpwuid(os.getuid()).pw_dir,
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PULSE_RELAY_PATHS": str(gate["relay_paths"]),
    }
    try:
        result = subprocess.run(
            [str(node), str(resolver), "resolve", "review", "--provider", runtime, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
            stdin=subprocess.DEVNULL,
        )
        value = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(value, dict):
        return None
    fields = ("profile", "provider", "model", "model_alias", "reasoning_effort")
    if any(not isinstance(value.get(k), str) for k in fields):
        return None
    return {k: value[k] for k in fields}
