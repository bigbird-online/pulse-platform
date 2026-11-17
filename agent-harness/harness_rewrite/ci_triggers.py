"""Which `pull_request` workflows a pull request's changes must start.

GitHub starts a `pull_request` workflow when the base branch passes its
`branches`/`branches-ignore` filter, the activity type is among `types`, and the
changed files pass its `paths`/`paths-ignore` filter. Filter patterns follow
GitHub's cheat sheet: `*` matches within one path segment, `**` across segments
(`**/` also matches no directory), `?` and `+` repeat the preceding character,
`[...]` is a character class, `\\` escapes, and a leading `!` excludes what
earlier patterns matched (the last matching pattern decides).

Anything this module cannot read with certainty raises ValueError: the caller
treats it as unknown, never as "no workflow starts".
"""

from __future__ import annotations

import re
from typing import Any

# A push to the pull request head is `synchronize`; opening is `opened` or `reopened`.
HEAD_TYPES = frozenset({"opened", "synchronize", "reopened"})


def pattern(text: Any) -> re.Pattern[str]:
    if not isinstance(text, str) or not text:
        raise ValueError("Workflow filter patterns must be non-empty strings")
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif text.startswith("**", i):
            out.append(".*")
            i += 2
        elif text[i] == "*":
            out.append("[^/]*")
            i += 1
        elif text[i] in "?+":
            if not out or out[-1][-1:] in {"?", "+"}:
                raise ValueError("A workflow filter repeats nothing")
            out.append(text[i])
            i += 1
        elif text[i] == "[":
            end = text.find("]", i + 2)
            if end < 0:
                raise ValueError("Unclosed workflow filter class")
            out.append("[" + text[i + 1:end].replace("\\", "\\\\") + "]")
            i = end + 1
        elif text[i] == "\\":
            if i + 1 >= len(text):
                raise ValueError("Dangling workflow filter escape")
            out.append(re.escape(text[i + 1]))
            i += 2
        else:
            out.append(re.escape(text[i]))
            i += 1
    try:
        return re.compile("".join(out))
    except re.error as exc:
        raise ValueError("Unreadable workflow filter") from exc


def matches(patterns: Any, value: str) -> bool:
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not patterns:
        raise ValueError("Workflow filters must be a non-empty list")
    result = False
    for text in patterns:
        negated = isinstance(text, str) and text.startswith("!")
        if pattern(text[1:] if negated else text).fullmatch(value):
            result = not negated
    return result


def events(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("A workflow file must be a mapping")
    # YAML 1.1 reads the bare key `on` as true.
    trigger = document["on"] if "on" in document else document.get(True)
    if isinstance(trigger, str):
        return {trigger: None}
    if isinstance(trigger, list) and all(isinstance(e, str) for e in trigger):
        return {e: None for e in trigger}
    if isinstance(trigger, dict) and all(isinstance(e, str) for e in trigger):
        return trigger
    raise ValueError("A workflow must name its triggering events")


def starts(document: Any, files: list[str], base: str) -> bool:
    """Whether a push of these changed files to a PR on `base` starts the workflow."""
    config = events(document).get("pull_request", False)
    if config is False:
        return False
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise ValueError("Unreadable pull_request trigger")
    if "paths" in config and "paths-ignore" in config or (
        "branches" in config and "branches-ignore" in config
    ):
        raise ValueError("A trigger cannot combine a filter with its ignore form")
    types = config.get("types")
    if types is not None:
        if isinstance(types, str):
            types = [types]
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
            raise ValueError("Unreadable pull_request types")
        if not HEAD_TYPES & set(types):
            return False
    if "branches" in config and not matches(config["branches"], base):
        return False
    if "branches-ignore" in config and matches(config["branches-ignore"], base):
        return False
    if "paths" in config:
        return any(matches(config["paths"], f) for f in files)
    if "paths-ignore" in config:
        return any(not matches(config["paths-ignore"], f) for f in files)
    return True
