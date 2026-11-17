"""Child WorkItems a subagent may be dispatched to, from the core's stats facts.

A subagent never declares. The main request declares its WI; the main session
dispatches a subagent to a child WI by writing ``[WI: <child>]`` as the first
line of the subagent's first prompt. The collector reads that line from the
native transcript, never from the subagent's own statements, and accepts it
only when the core's ``stats-facts`` export names the declared WI as the
child's ``parent`` and the child was open when the usage happened. Otherwise
the subagent keeps the declared WI.

An export from a core without the ``parent`` field, a missing or unreadable
export and a malformed item all yield no children: every subagent inherits.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from .events import parse_timestamp

SCHEMA = "harness.stats_facts/v1"
MAX_BYTES = 8 * 1024 * 1024
MARKER = re.compile(r"\[WI: (wi_\d{8}_\d{3})\]")
_WI = re.compile(r"wi_\d{8}_\d{3}")


def dispatch_marker(text: str | None) -> str | None:
    """The child WI named by the first line of a subagent's first prompt."""
    if not isinstance(text, str):
        return None
    match = MARKER.match(text.split("\n", 1)[0])
    return match[1] if match else None


def _export_path(registry: Mapping[str, Any]) -> Path | None:
    holder = registry.get("core_session_writes")
    if isinstance(holder, Mapping) and isinstance(holder.get("path"), str):
        return Path(holder["path"])
    paths = [s.get("path") for s in registry.get("sources", [])
             if isinstance(s, Mapping) and s.get("kind") == "harness_core"]
    return Path(paths[0]) if len(paths) == 1 and isinstance(paths[0], str) else None


def read_children(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """{child WI: {parent, created_at, closed_at, open}} from the private export.

    ``open`` is the core's current status. A child that has ended counts as
    open at an instant only when the export also says when it closed.
    """
    path = _export_path(registry)
    if path is None:
        return {}
    try:
        info = path.lstat()
        if (not path.is_absolute() or path.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
            return {}
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    if (not isinstance(document, Mapping) or document.get("schema") != SCHEMA
            or document.get("read_only") is not True or not isinstance(document.get("items"), list)):
        return {}
    children = {}
    for item in document["items"]:
        if not isinstance(item, Mapping) or "parent" not in item:
            continue
        wi, parent = item.get("id"), item.get("parent")
        if (not isinstance(wi, str) or not _WI.fullmatch(wi)
                or not isinstance(parent, str) or not _WI.fullmatch(parent) or parent == wi):
            continue
        try:
            created = parse_timestamp(item["created_at"], "child WI creation")
            closed = (parse_timestamp(item["closed_at"], "child WI closure")
                      if item.get("closed_at") is not None else None)
        except (KeyError, TypeError, ValueError):
            continue
        children[wi] = {"parent": parent, "created_at": created, "closed_at": closed,
                        "open": item.get("status") == "active"}
    return children


def child_of(children: Mapping[str, Mapping[str, Any]], marker: str | None, declared: str | None,
             at: datetime) -> bool:
    """Whether ``marker`` is an open child of the declared WI at ``at``."""
    child = children.get(marker) if marker else None
    if child is None or not declared or child["parent"] != declared or child["created_at"] > at:
        return False
    if child["closed_at"] is not None:
        return at < child["closed_at"]
    return child["open"]
