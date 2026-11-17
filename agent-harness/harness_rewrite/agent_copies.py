"""Repository copies Agents made outside the managed roots (AH-086).

Discovery reads every worktree Git records for the Host's repositories, and
scans each Agent workspace (at most `MAX_DEPTH` levels, links never followed)
for directories holding `.git` (worktrees and clones, including
`clone --shared`). A copy belongs to the Agent whose workspace contains it, or
whose name prefixes its branch (`<agent>/...`). A copy belongs to a WI when
its branch or path names exactly one existing WI (its full identity, or the
short `wiN` when only one WI ends in `_N`). The kernel's own workspaces (managed
roots, `harness/` branches, any path a WI journal registers) and the source
checkouts are measured elsewhere.
Nothing here deletes anything.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Iterable

from .model import Json
from .quota import allocated_bytes

MAX_DEPTH = 6
MAX_DIRECTORIES = 200_000
GIT_TIMEOUT = 30
SHORT = re.compile(r"(?<![a-z0-9])wi[-_]?(\d+)(?![0-9])")


def _under(path: str, roots: Iterable[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def worktrees(common_dir: str) -> list[tuple[str, str | None]]:
    """(path, branch) of every linked worktree; the main worktree is the checkout itself."""
    listed = subprocess.run(
        ["git", "--git-dir=" + common_dir, "worktree", "list", "--porcelain"],
        capture_output=True, timeout=GIT_TIMEOUT, check=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
             "HOME": os.environ.get("HOME", "/")},
    ).stdout.decode("utf-8", "surrogateescape")
    rows: list[tuple[str, str | None]] = []
    for block in listed.strip().split("\n\n")[1:]:
        fields = dict(line.partition(" ")[::2] for line in block.splitlines())
        if "worktree" in fields and "bare" not in fields:
            branch = fields.get("branch")
            rows.append((os.path.normpath(fields["worktree"]),
                         branch.removeprefix("refs/heads/") if branch else None))
    return rows


def scan(root: str, skip: frozenset[str]) -> list[str] | None:
    """Copies under one Agent workspace; None when a directory is unreadable or
    the tree too large, since an unseen copy would read as freed space."""
    found: list[str] = []
    stack, seen = [(root, 0)], 0
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as scanned:
                entries = [e for e in scanned if e.is_dir(follow_symlinks=False)]
        except FileNotFoundError:
            continue  # Removed while scanning.
        except OSError:
            return None
        for entry in entries:
            seen += 1
            if seen > MAX_DIRECTORIES:
                return None
            if entry.path in skip or entry.name == ".git":
                continue
            if os.path.lexists(os.path.join(entry.path, ".git")):
                found.append(entry.path)
            elif depth + 1 < MAX_DEPTH:
                stack.append((entry.path, depth + 1))
    return found


def head_branch(path: str) -> str | None:
    """The branch a copy has checked out, read from its HEAD; None if detached or unreadable."""
    git = os.path.join(path, ".git")
    try:
        if os.path.isfile(git) and not os.path.islink(git):
            with open(git, encoding="utf-8") as pointer:
                line = pointer.readline().strip()
            if not line.startswith("gitdir: "):
                return None
            git = os.path.join(path, line[len("gitdir: "):])
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as head:
            ref = head.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return ref[len("ref: refs/heads/"):] if ref.startswith("ref: refs/heads/") else None


def owner_wi(texts: Iterable[str], known: frozenset[str]) -> str | None:
    """The single existing WI the branch or path names, else None."""
    suffixes: dict[str, set[str]] = {}
    for wi in known:
        suffixes.setdefault(wi.rsplit("_", 1)[-1].lstrip("0") or "0", set()).add(wi)
    named: set[str] = set()
    for text in texts:
        lowered = text.lower()
        full = {wi for wi in known
                if re.search(r"(?<![a-z0-9])" + re.escape(wi.lower()) + r"(?![a-z0-9])", lowered)}
        named |= full
        if not full:
            for number in SHORT.findall(lowered):
                matches = suffixes.get(number.lstrip("0") or "0", set())
                if len(matches) == 1:
                    named |= matches
    return named.pop() if len(named) == 1 else None


def discover(policy: Json, known: frozenset[str], registered: frozenset[str] = frozenset()) -> list[Json] | None:
    """Every Agent-made copy with its Agent, WI and bytes; None if discovery fails.

    A copy whose size cannot be read keeps `bytes: None`, so only its owner
    stays unmeasured.
    """
    from .access import agent_workspaces

    workspaces = {a: [os.path.normpath(r) for r in roots]
                  for a, roots in agent_workspaces(policy).items()}
    managed = [os.path.normpath(str(r["managed_root"])) for r in policy.get("repositories", [])]
    checkouts = {os.path.normpath(str(r[k])) for r in policy.get("repositories", [])
                 for k in ("source", "common_dir")}
    checkouts |= {os.path.normpath(str(c["path"])) for c in policy.get("source_consumers", [])}
    # Both sources feed one candidate list, then one exclusion rule applies to all.
    candidates: dict[str, Json] = {}
    for repo in policy.get("repositories", []):
        try:
            listed = worktrees(str(repo["common_dir"]))
        except (OSError, subprocess.SubprocessError):
            return None
        for path, branch in listed:
            candidates[path] = {"path": path, "repo_id": repo["repo_id"], "branch": branch}
    roots = sorted({r for rs in workspaces.values() for r in rs})
    for root in roots:
        nested = (frozenset(r for r in roots if r != root and _under(r, [root]))
                  | frozenset(managed) | registered)
        found = scan(root, nested)
        if found is None:
            return None
        for path in found:
            if path not in candidates:
                candidates[path] = {"path": path, "repo_id": None, "branch": head_branch(path)}
    copies = {
        path: copy for path, copy in candidates.items()
        if not (path in checkouts or _under(path, managed) or _under(path, registered)
                or (copy["branch"] or "").startswith("harness/"))
    }
    inner = set(candidates) | registered | set(managed) | checkouts
    inodes: set[tuple[int, int]] = set()
    rows = []
    for path, copy in sorted(copies.items()):
        try:
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                continue  # Pruned worktree or a link: no bytes of its own here.
        except OSError:
            continue
        holders = [(len(r), a) for a, rs in workspaces.items() for r in rs if _under(path, [r])]
        branch = copy["branch"] or ""
        agent = max(holders)[1] if holders else (
            branch.split("/", 1)[0] if "/" in branch and branch.split("/", 1)[0] in workspaces else None
        )
        within = [path[len(r):] for _, a in holders for r in workspaces[a] if _under(path, [r])]
        rows.append({
            **copy,
            "agent": agent,
            "wi": owner_wi([branch, *(within or [path])], known),
            # A copy inside another copy is counted once, as itself; an excluded
            # path inside it (a registered workspace, a kernel copy) by its owner.
            "bytes": allocated_bytes(Path(path), inodes=inodes, exclude=frozenset(
                other for other in inner if other.startswith(path + "/"))),
        })
    return rows


def charged(copies: list[Json], wi: str, agent: str | None) -> tuple[dict[str, int | None], dict[str, int | None]]:
    """(copies counted in this WI, unattributed copies of the Agent holding it)."""
    own = {c["path"]: c["bytes"] for c in copies if c["wi"] == wi}
    stray = {c["path"]: c["bytes"] for c in copies
             if agent is not None and c["wi"] is None and c["agent"] == agent}
    return own, stray


def summary(copies: list[Json]) -> Json:
    """Per-Agent totals for the service report (unowned copies under `null`)."""
    rows: dict[str, Json] = {}
    for c in copies:
        row = rows.setdefault(c["agent"] or "null", {"bytes": 0, "unattributed_bytes": 0,
                                                       "copies": 0, "unmeasured": 0})
        row["copies"] += 1
        if c["bytes"] is None:
            row["unmeasured"] += 1
            continue
        row["bytes"] += c["bytes"]
        if c["wi"] is None:
            row["unattributed_bytes"] += c["bytes"]
    return dict(sorted(rows.items()))
