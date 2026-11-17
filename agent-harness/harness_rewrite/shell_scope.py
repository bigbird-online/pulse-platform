"""The workspace a recorded shell command's `git commit` ran in, read statically.

A commit window binds to one workspace's HEAD log, so a wrong workspace could
claim another session's commit. Only what the shell text proves counts: the
call's directory, then each `cd <literal dir>` that ran before the commit in the
same shell scope, then that git command's own `-C <dir>`.

- A `( ... )` subshell starts its own scope; its `cd` never leaves it.
- A `cd` in the background (`&`) runs in a subshell: no effect. A `cd` in a
  pipeline makes the workspace unknown: zsh (and bash with lastpipe) runs the
  last segment in the current shell.
- A `cd` reached only conditionally (after `||`, inside if/while/for/case or
  `{ ... }`), or followed by `&&`/`||` chains the commit does not depend on, makes
  the workspace unknown. `cd X && git commit` and `cd X || exit; git commit` hold.
- A `$NAME` expands only to a literal the same scope assigned unconditionally
  (`NAME=/dir` alone, or `export`); any other variable, a command substitution
  in the directory, `~` and `cd -` make the workspace unknown. A command
  substitution elsewhere runs in a subshell; a heredoc body is data.
- pushd/popd, eval, source, exec, function definitions and similar make the
  workspace unknown.

Unknown means the window claims nothing: a missed attribution goes to a human,
a wrong one would not.
"""

from __future__ import annotations

import os
from pathlib import Path
import re

COMMITTING = re.compile(
    r"\bgit(?:\s+(?:-[Cc]\s+(?:\"[^\"]*\"|'[^']*'|\S+)|--?[A-Za-z][\w-]*(?:=\S+)?))*"
    r"\s+(?:commit|merge|pull)\b"
)
OPERATORS = ("&&", "||", ";;", "|&", "<<<", "<<", ">>", "&>", ">&", "<&",
             ";", "&", "|", "(", ")", "\n", "<", ">")
REDIRECTS = {"<<<", ">>", "&>", ">&", "<&", "<", ">"}
LIST_ENDS = {";", "\n", "&", ";;"}
OPEN = {"if", "while", "until", "for", "case", "select", "{"}
CLOSE = {"fi", "done", "esac", "}"}
PREFIX = {"then", "do", "else", "elif", "!", "time"}
OPAQUE = {"pushd", "popd", "eval", "source", ".", "function", "exec", "alias", "builtin",
          "command", "unalias", "enable", "trap"}
SHELLS = {"bash", "sh", "zsh", "dash"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
Token = tuple[str, str]


# Markers inside a word: a run-time value (command substitution) and a quoted `$`.
DYNAMIC, QUOTED_DOLLAR = "\x00", "\x01"


def _substitution_end(text: str, i: int) -> int | None:
    """The index after the `)` closing the `$(` at `i` (it runs in a subshell)."""
    depth, j, n = 0, i + 1, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c in "'\"":
            end = text.find(c, j + 1)
            if end < 0:
                return None
            j = end + 1
            continue
        depth += {"(": 1, ")": -1}.get(c, 0)
        j += 1
        if depth == 0:
            return j
    return None


def _tokens(text: str) -> list[Token] | None:
    """('w', word) and ('op', operator) tokens; None when not statically readable.

    Command substitutions become a run-time marker in their word; heredoc bodies
    are skipped as data."""
    tokens: list[Token] = []
    word: list[str] | None = None
    heredocs: list[tuple[str, bool]] = []
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal word
        if word is not None:
            tokens.append(("w", "".join(word)))
            word = None

    def substitution(j: int) -> int | None:
        if text[j] == "`":
            end = j + 1
            while end < n and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            return end + 1 if end < n else None
        return _substitution_end(text, j)

    while i < n:
        c = text[i]
        if c in " \t\r":
            flush()
            i += 1
        elif c == "\\":
            if text[i + 1:i + 2] != "\n":
                word = (word or []) + [text[i + 1:i + 2]]
            i += 2
        elif c == "'":
            end = text.find("'", i + 1)
            if end < 0:
                return None
            word = (word or []) + [text[i + 1:end].replace("$", QUOTED_DOLLAR)]
            i = end + 1
        elif c == '"':
            j, part = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    part.append(text[j + 1])
                    j += 2
                    continue
                if text[j] == "`" or text.startswith("$(", j):
                    closing = substitution(j)
                    if closing is None:
                        return None
                    part.append(DYNAMIC)
                    j = closing
                    continue
                part.append(text[j])
                j += 1
            if j >= n:
                return None
            word = (word or []) + part
            i = j + 1
        elif c == "`" or text.startswith("$(", i):
            closing = substitution(i)
            if closing is None:
                return None
            word = (word or []) + [DYNAMIC]
            i = closing
        elif c == "#" and word is None:
            end = text.find("\n", i)
            i = n if end < 0 else end
        else:
            op = next((o for o in OPERATORS if text.startswith(o, i)), None)
            if op is None:
                word = (word or []) + [c]
                i += 1
                continue
            if op in {">", "<", ">>", ">&", "<&", "<<"} and word is not None and "".join(
                word
            ).isdigit():
                word = None  # The file descriptor of `2>&1`.
            flush()
            i += len(op)
            if op == "<<":
                strip = text.startswith("-", i)
                match = re.match(r"-?[ \t]*(['\"]?)([A-Za-z0-9_.-]+)\1", text[i:])
                if match is None:
                    return None
                heredocs.append((match.group(2), strip))
                i += match.end()
                continue
            tokens.append(("op", op))
            if op == "\n":
                for delimiter, strip in heredocs:
                    # The body is data up to the delimiter line; the commit is never in it.
                    while True:
                        end = text.find("\n", i)
                        if end < 0:
                            return None
                        line = text[i:end]
                        i = end + 1
                        if (line.lstrip("\t") if strip else line) == delimiter:
                            break
                heredocs = []
    flush()
    return tokens


VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _expand(word: str, variables: dict[str, str | None]) -> str | None:
    """`$NAME`/`${NAME}` replaced by a literal the same shell scope assigned; None
    when any part is decided at run time."""
    unknown = False

    def value(match: re.Match[str]) -> str:
        nonlocal unknown
        found = variables.get(match.group(1) or match.group(2))
        unknown = unknown or found is None
        return found or ""

    expanded = VARIABLE.sub(value, word)
    return None if unknown else expanded


def _literal(word: str) -> bool:
    return not any(c in word for c in ("$", "`", DYNAMIC, QUOTED_DOLLAR))


def _target(base: str | None, word: str | None) -> str | None:
    """A literal directory word resolved against `base`; None when not literal."""
    if not word or word.startswith("-") or not _literal(word) or any(c in word for c in "~*?[{"):
        return None
    if word.startswith("/"):
        return word
    # The shell resolves `..` logically, as `cd` does.
    return None if base is None else os.path.normpath(str(Path(base) / word))


def _commands(tokens: list[Token]) -> list[tuple[str, list[str], str]] | None:
    """Simple commands as (operator before, words, operator after), with `(`/`)`
    kept as their own entries (words empty); redirection targets dropped."""
    found: list[tuple[str, list[str], str]] = []
    words: list[str] = []
    before = "start"
    redirect = False
    for kind, value in tokens:
        if kind == "w":
            if not redirect:
                words.append(value)
            redirect = False
            continue
        if value in REDIRECTS:
            redirect = True
            continue
        if value == "(" and words:
            return None  # A function definition `name() ...`.
        if words or value not in {"(", ")"}:
            found.append((before, words, value))
            words = []
        if value in {"(", ")"}:
            found.append((before if value == "(" else ")", [], value))
        before = value
    found.append((before, words, "end"))
    return found


def workspace(cwd: str | None, command: str) -> str | None:
    """The workspace of the first `git commit`/merge/pull the command runs."""
    whole = _tokens(command)
    inner = None if whole is None else _wrapped(whole)
    if inner is not None:
        return workspace(cwd, inner)
    for match in COMMITTING.finditer(command):
        tokens = _tokens(command[:match.end()])
        if tokens is None:
            continue
        result = _scope(cwd, tokens)
        if result is not False:
            return result if isinstance(result, str) else None
    return None


def _wrapped(tokens: list[Token]) -> str | None:
    """The script of `bash -lc '<script>'` (a Codex argv), if that is the command."""
    words = [v for k, v in tokens if k == "w"]
    if len(words) == len(tokens) and len(words) >= 3 and Path(words[0]).name in SHELLS and (
        words[1] in {"-c", "-lc", "-cl"}
    ):
        return words[2]
    return None


def _scope(cwd: str | None, tokens: list[Token]) -> str | None | bool:
    """The commit command's workspace (None: unknown); False when the last command
    is not a git commit (the match was inside another word)."""
    commands = _commands(tokens)
    if commands is None:
        return None
    state = cwd
    control = 0
    # A conditional `cd` in the current and-or list: (ran only after &&, followed by
    # ||, guarded by `|| exit`, operators between it and now).
    cond: dict[str, object] | None = None
    # Shell variables this scope assigned: a literal, or None when decided at run time.
    variables: dict[str, str | None] = {}
    stack: list[tuple[str | None, int, dict[str, object] | None, dict[str, str | None]]] = []

    def end_list() -> None:
        nonlocal state, cond
        if cond is not None and not (cond["after_or"] and cond["guarded"] and not cond["after_and"]):
            state = None
        cond = None

    last = len(commands) - 1
    for index, (before, words, after) in enumerate(commands):
        if cond is not None and before in {"&&", "||"}:
            ops = cond["ops"]
            assert isinstance(ops, list)
            ops.append(before)
        if after == "(" and not words:
            stack.append((state, control, cond, dict(variables)))
            cond = None
            continue
        if after == ")" and not words:
            end_list()
            if not stack:
                return None
            state, control, cond, variables = stack.pop()
            continue
        while words and (words[0] in PREFIX or words[0] in OPEN or words[0] in CLOSE):
            if words[0] in OPEN:
                control += 1
            elif words[0] in CLOSE:
                control -= 1
            words = words[1:]
        assigned = words[1:] if words[:1] == ["export"] else words
        if assigned and all(ASSIGNMENT.match(w) for w in assigned):
            # `NAME=value` alone sets a variable of this shell; before a command it
            # only sets that command's environment.
            if after != "&":
                # A pipeline segment may run in this shell (zsh, bash lastpipe): unknown.
                piped = before in {"|", "|&"} or after in {"|", "|&"}
                certain = (control == 0 and not piped and before not in {"&&", "||"}
                           and after != "||")
                for word in assigned:
                    name, value = word.split("=", 1)
                    literal = _expand(value, variables) if certain else None
                    variables[name] = literal if literal is not None and _literal(literal) else None
            words = []
        while words and ASSIGNMENT.match(words[0]):
            words = words[1:]
        if index == last:
            if not words or Path(words[0]).name != "git" or words[-1] not in {
                "commit", "merge", "pull"
            }:
                return False
            if cond is not None:
                ops = cond["ops"]
                assert isinstance(ops, list)
                if cond["after_or"] or any(op != "&&" for op in ops):
                    return None
            here = state
            rest = iter(words[1:-1])
            for word in rest:
                if word in {"-C", "-c"}:
                    value = next(rest, "")
                    if word == "-C":
                        here = _target(here, _expand(value, variables))
            return here
        if not words:
            if after in LIST_ENDS:
                end_list()
            continue
        if words[0] in OPAQUE:
            state, cond = None, None
        elif words[0] in {"exit", "return"} and cond is not None and before == "||":
            ops = cond["ops"]
            assert isinstance(ops, list)
            if ops == ["||"]:
                cond["guarded"] = True
        elif words[0] == "cd" and after != "&":
            # A `cd` in a pipeline runs in this shell under zsh or bash lastpipe and in
            # a subshell otherwise: the workspace is unknown either way.
            if control > 0 or before in {"||", "|", "|&"} or after in {"|", "|&"} or len(
                words
            ) != 2:
                state, cond = None, None
            else:
                state = _target(state, _expand(words[1], variables))
                cond = ({"ops": [], "after_and": before == "&&", "after_or": after == "||",
                         "guarded": False} if before == "&&" or after == "||" else None)
        if after in LIST_ENDS:
            end_list()
    return None
