"""The WIs ComputeCost charges a marked CC sub-agent's turns, or a main turn, to.

A child WorkItem's pull request may carry a sub-agent's commit only when the
sub-agent's whole cost belongs to that child. ComputeCost decides the cost
(`cc_request_purpose.request_bindings` and `request_attributions`); this module
proves, on the same native records, a narrower shape in which ComputeCost's
answer is certain, and returns null for anything else. Null makes the kernel
ignore the marker, so every unproven case is refused rather than guessed.

The proven shape, with ComputeCost's rule each step relies on:

- Every keyed record on a walked chain is unique (no duplicated uuid), so none
  is a conflicting copy. The sub-agent's first keyed record is its marked
  dispatch prompt with no parent (ComputeCost's `dispatched`). No usage record
  lacks a uuid (such usage binds no request and stays unassigned).
- From each usage record (and the dispatch prompt), parent links to strictly
  earlier records of the same file and agent reach a text input or coordinator
  input (`nearest_text`). It carries no other native origin (a task notification
  would route elsewhere). Its `promptId` names exactly one main text input,
  written no later than it and no later than the usage (`request`).
- Each such main request has at least one declaration ComputeCost qualifies:
  a `declare_session_wi` call and its single result, both unique main records,
  the result's parent and `sourceToolAssistantUUID` the call, its `promptId`
  the request's, the call's own chain reaching that request (through a
  compaction summary of the same `promptId` too), and the server's exact ack
  (`decode_cc_completion`). Every declaration whose result carries that
  `promptId`, qualified or not, names one and the same WI; then ComputeCost's
  effective declaration at any instant is that WI. When they name several
  (`none` then a WI, or one WI then another), every one of them must qualify;
  then ComputeCost's effective declaration for a turn is the latest one whose
  result was written no later than the turn, or the first before any
  (`request_attributions`), and the declarations of that instant must name
  one WI.
- No main declaration fails to decode: ComputeCost would void every
  declaration of the session. A decoded declaration this proof cannot place
  (after a task notification, or a summary of an unknown prompt) voids
  nothing: ComputeCost binds a declaration at most to the request whose
  `promptId` its result carries, and the previous step counts every such
  declaration by that `promptId`.
- No other record file of the session (other sub-agents, fork copies, a nested
  `main.jsonl`) shares a uuid or usage message id with the sub-agent's or the
  main record (`copies`): ComputeCost merges records across files by those
  identities and a copy may re-link a turn under another request.

The result is the union of those WIs over the requests the sub-agent served,
with the earliest of its turns. Shapes ComputeCost also accepts but this proof
does not (records ordered only by timestamp, forks, deferred-tools records,
task-notification hops) are refused: stricter, never looser.

The same proof places a session's own turn: the main turn that ran a
`git commit` (`request_charged`) is charged to its request's WI.

A request declaring the child's parent moves the turns to the child only
through the export ComputeCost's registry names (`child_work_items`):
`exported_child` reads that same export the same way, so the kernel can require
the child there, under its parent, created no later than the first turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import pwd
import re
import stat
from typing import Any

from .model import Json

DECLARE_TOOL = "mcp__pulse-request-purpose__declare_session_wi"
DECLARE = "declare_session_wi"
ACK_SCHEMAS = frozenset({"cc_session_wi/v1", "cc_request_wi/v1"})
WI = re.compile(r"wi_[0-9]{8}_[0-9]{3}")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
MARKER = re.compile(r"\[WI: (wi_\d{8}_\d{3})\]")
# Text CC writes under the interrupted request's promptId; not another request.
INTERRUPTS = frozenset({"[Request interrupted by user]", "[Request interrupted by user for tool use]"})
MAX_BYTES = 8 * 1024 * 1024 * 1024
MAX_ACK = 4096
EXPORT_BYTES = 8 * 1024 * 1024  # ComputeCost's export limit.
UNKNOWN = "?"  # A declaration naming no readable WI: never one WI.


@dataclass(slots=True)
class Record:
    """One keyed record: a long session holds many, so only these fields stay."""
    position: int
    parent: Any
    at: Any
    session: Any
    type: Any
    stop: bool  # A text input or coordinator input: where a chain's request is read.
    compact: bool
    origin: bool  # A native origin other than a coordinator's.
    prompt: Any
    usage: bool
    source: Any
    message: str | None  # A usage record's message id.


@dataclass
class Index:
    """The keyed records of one transcript file, reduced to what the proof reads."""
    rows: dict[str, Record] = field(default_factory=dict)
    duplicates: set[str] = field(default_factory=set)
    first: str | None = None  # The first keyed record.
    marker: str | None = None  # The marker on the first record's first line.
    unkeyed_usage: bool = False
    # Every uuid and usage message id in the file, whatever its role or agent.
    uuids: set[str] = field(default_factory=set)
    messages: set[str] = field(default_factory=set)
    prompts: dict[str, list[str]] = field(default_factory=dict)  # Main text inputs by promptId.
    # Declaration calls and tool results by tool id, with their record's uuid.
    uses: dict[str, list[tuple[Any, str, Any]]] = field(default_factory=dict)
    results: dict[str, list[tuple[Any, bool, str | None]]] = field(default_factory=dict)
    # Main records by every tool call they make, and main usage records by message id.
    calls: dict[str, list[str]] = field(default_factory=dict)
    turns: dict[str, list[str]] = field(default_factory=dict)


def _text(row: dict[str, Any]) -> str | None:
    """ComputeCost's `_request_text`: a user text input, not an interrupt marker."""
    if row.get("type") != "user" or row.get("isMeta") is True:
        return None
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list) and content and all(
        isinstance(b, dict) and b.get("type") in {"text", "image"} for b in content
    ):
        content = "".join(b["text"] for b in content
                          if b.get("type") == "text" and isinstance(b.get("text"), str))
    if not isinstance(content, str) or content.strip() in INTERRUPTS:
        return None
    return content


def _coordinator(row: dict[str, Any]) -> bool:
    """A message the main agent sent the sub-agent (SendMessage)."""
    return (row.get("type") == "user" and row.get("isSidechain") is True
            and row.get("isMeta") is True and row.get("origin") == {"kind": "coordinator"}
            and isinstance(row.get("promptId"), str) and bool(row["promptId"]))


def _ack(block: dict[str, Any]) -> str | None:
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
          and set(content[0]) == {"type", "text"} and content[0]["type"] == "text"):
        text = content[0]["text"]
    else:
        return None
    return text if isinstance(text, str) and len(text) <= MAX_ACK else None


def index(path: Path, session: str, agent: str | None = None) -> Index | None:
    """Index the main record (`agent` None) or a sub-agent's; None if unreadable."""
    found = Index()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if os.fstat(stream.fileno()).st_size > MAX_BYTES:
                return None
            for position, line in enumerate(stream):
                if not line.endswith(b"\n"):
                    break
                if line.strip():
                    _add(found, json.loads(line), position, session, agent)
    except (OSError, ValueError):
        return None
    return found


def _add(found: Index, row: Any, position: int, session: str, agent: str | None) -> None:
    if not isinstance(row, dict):
        raise ValueError("row")
    side = agent is not None
    message: dict[str, Any] = row["message"] if isinstance(row.get("message"), dict) else {}
    uuid = row.get("uuid")
    content = message.get("content")
    if isinstance(uuid, str):
        found.uuids.add(uuid)
    if row.get("type") == "assistant" and message.get("usage") is not None:
        found.messages.add(str(message.get("id")))
    # Every declaration call and tool result in the file, whatever its role: a
    # pairing ComputeCost makes and cannot decode voids the session's declarations.
    for block in content if isinstance(content, list) and not side else []:
        if not isinstance(block, dict):
            continue
        if (row.get("type") == "assistant" and block.get("type") == "tool_use"
                and isinstance(block.get("name"), str) and block["name"].endswith(DECLARE)):
            found.uses.setdefault(str(block.get("id")), []).append((uuid, block["name"], block.get("input")))
        elif row.get("type") == "user" and block.get("type") == "tool_result":
            found.results.setdefault(str(block.get("tool_use_id")), []).append(
                (uuid, block.get("is_error") is True, _ack(block)))
    if row.get("isSidechain") is not side or (not side and row.get("sessionId") != session):
        return  # Another role's or session's record never joins this file's chains.
    if side and row.get("agentId") != agent:
        return
    usage = row.get("type") == "assistant" and message.get("usage") is not None
    if not isinstance(uuid, str) or not uuid:
        found.unkeyed_usage |= usage
        return
    if uuid in found.rows:
        found.duplicates.add(uuid)
        return
    text = _text(row)
    coordinator = _coordinator(row)
    found.rows[uuid] = Record(
        position, row.get("parentUuid"),
        row.get("timestamp"), row.get("sessionId"), row.get("type"),
        text is not None or coordinator, row.get("isCompactSummary") is True,
        "origin" in row and not coordinator, row.get("promptId"), usage,
        row.get("sourceToolAssistantUUID"),
        message["id"] if usage and isinstance(message.get("id"), str) and message["id"] else None)
    if found.first is None:
        found.first = uuid
        found.marker = (MARKER.match(text.split("\n", 1)[0]) or [None, None])[1] if text else None
    if not side:
        for block in content if isinstance(content, list) and row.get("type") == "assistant" else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                found.calls.setdefault(str(block.get("id")), []).append(uuid)
        if usage:
            found.turns.setdefault(str(message.get("id")), []).append(uuid)
    prompt = row.get("promptId")
    if not side and text is not None and not row.get("isCompactSummary") and isinstance(prompt, str) and prompt:
        found.prompts.setdefault(prompt, []).append(uuid)


def _at(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _row(found: Index, uuid: Any) -> Record | None:
    return found.rows.get(uuid) if isinstance(uuid, str) and uuid not in found.duplicates else None


def _nearest(found: Index, uuid: str) -> str | None:
    """The text or coordinator input `uuid` descends from, by earlier parents only."""
    seen: set[str] = set()
    while True:
        row = _row(found, uuid)
        if row is None:
            return None
        if row.stop:
            return uuid
        parent = _row(found, row.parent)
        if parent is None or parent.position >= row.position or row.parent in seen:
            return None
        seen.add(uuid)
        uuid = row.parent


def _request(main: Index, input_row: Record, start: datetime) -> str | None:
    """The unique main request input an input continues, written before `start`."""
    if input_row.origin:
        return None  # A task completion routes to its trigger, not to its promptId.
    candidates = main.prompts.get(input_row.prompt) if isinstance(input_row.prompt, str) else None
    if not candidates or len(candidates) != 1:
        return None
    request = _row(main, candidates[0])
    at, written, begun = (_at(request.at) if request else None), _at(input_row.at), start
    if request is None or request.origin or at is None or written is None or at > written or at > begun:
        return None
    return candidates[0]


def _decoded(main: Index, use_id: str, session: str) -> str | None:
    """The WI of a declaration ComputeCost decodes (`cc_native_completions`,
    `decode_cc_completion`): one call and one result, both main records of this
    session, the result naming the call and carrying the server's exact ack."""
    uses, results = main.uses.get(use_id, []), main.results.get(use_id, [])
    if len(uses) != 1 or len(results) != 1:
        return None
    (use_uuid, name, data), (result_uuid, failed, text) = uses[0], results[0]
    use, result = _row(main, use_uuid), _row(main, result_uuid)
    wi = data.get("wi") if isinstance(data, dict) and set(data) == {"wi"} else None
    if (use is None or result is None or failed or name != DECLARE_TOOL or not isinstance(wi, str)
            or not (wi == "none" or WI.fullmatch(wi)) or not 0 < len(use_id) <= 1024
            or any(ord(c) < 32 for c in use_id) or use.type != "assistant"
            or result.type != "user" or use.session != session or result.session != session
            or result.source != use_uuid or text is None):
        return None
    used, completed = _at(use.at), _at(result.at)
    try:
        ack = json.loads(text)
    except ValueError:
        return None
    if (used is None or completed is None or completed < used or not isinstance(ack, dict)
            or set(ack) != {"schema", "wi", "origin"} or ack["schema"] not in ACK_SCHEMAS
            or ack["wi"] != wi or ack["origin"] != "agent_declaration"):
        return None
    return wi


def _declared(main: Index, use_id: str, session: str) -> tuple[str, str, datetime] | None:
    """(WI, request input, `declared_at`: the result's time) of a declaration
    ComputeCost qualifies, else None."""
    wi = _decoded(main, use_id, session)
    if wi is None:
        return None
    (use_uuid, _, _), (result_uuid, _, _) = main.uses[use_id][0], main.results[use_id][0]
    use, result = main.rows[use_uuid], main.rows[result_uuid]
    used, completed = _at(use.at), _at(result.at)
    if result.parent != use_uuid or used is None or completed is None:
        return None
    request = _main_request(main, use_uuid, used)
    if request is None or result.prompt != main.rows[request].prompt:
        return None
    return wi, request, completed


def _main_request(main: Index, uuid: str, start: datetime) -> str | None:
    """The main request input a main record answers, through a compaction
    summary of the same `promptId` too, written no later than `start`."""
    stop = _nearest(main, uuid)
    row = _row(main, stop)
    if row is None:
        return None
    if row.compact:  # A summary continues the request whose promptId it carries.
        stop = _request(main, row, start)
        row = _row(main, stop)
        if row is None:
            return None
    request = _request(main, row, start)
    return request if request is not None and request == stop else None


def _valid(main: Index, session: str) -> bool:
    """No main declaration ComputeCost fails to decode: one such failure voids
    every declaration of the session. One this proof cannot place voids
    nothing; `charged` still counts it by its result's `promptId`."""
    for use_id, uses in main.uses.items():
        results = main.results.get(use_id, [])
        if not any(name == DECLARE_TOOL for _, name, _ in uses) or not results:
            continue
        if all(failed for _, failed, _ in results) and len(results) == 1 and len(uses) == 1:
            continue  # A refused call declared nothing.
        if _decoded(main, use_id, session) is None:
            return False
    return True


def copies(root: Path, keep: set[Path]) -> tuple[set[str], set[str]] | None:
    """Uuids and usage message ids of every other record file of the session.

    ComputeCost merges records across all of a session's files by uuid and
    message id (fork copies re-link records across files), so a proof that
    reads only some files holds only when no other file shares an identity.
    None when a file cannot be read, or the session also has a nested main record.
    """
    uuids: set[str] = set()
    messages: set[str] = set()
    if (root / "main.jsonl").exists():
        return None
    try:
        files = sorted(root.rglob("*.jsonl")) if root.is_dir() else []
    except OSError:
        return None
    for file in files:
        if file in keep:
            continue
        found = index(file, "", "")
        if found is None or file.is_symlink():
            return None
        uuids |= found.uuids
        messages |= found.messages
    return uuids, messages


def charged(
    main: Index | None, child: Index | None, session: str, marker: str | None,
    others: tuple[set[str], set[str]] | None,
) -> tuple[list[str], str] | None:
    """The WIs ComputeCost charges the sub-agent's turns to and the earliest of
    them, or None when unproven."""
    if (main is None or child is None or others is None or not UUID.fullmatch(session)
            or child.unkeyed_usage or child.marker is None or child.marker != marker
            or child.first is None or not _valid(main, session)):
        return None
    uuids, messages = others
    if (child.uuids & (uuids | main.uuids) or main.uuids & uuids
            or child.messages & (messages | main.messages)):
        return None  # A copy elsewhere may bind the same turn to another request.
    first = _row(child, child.first)
    if first is None or first.parent is not None or not first.stop:
        return None
    requests: dict[str, list[datetime]] = {}
    since: datetime | None = None
    for uuid, row in child.rows.items():
        if uuid != child.first and not row.usage:
            continue
        if row.session != session:
            return None
        start, stop = _at(row.at), _nearest(child, uuid)
        request = _request(main, child.rows[stop], start) if start and stop else None
        if request is None or start is None:
            return None
        requests.setdefault(request, []).append(start)
        since = start if since is None else min(since, start)
    wis = _wis(main, requests, session)
    if wis is None:
        return None
    assert since is not None  # The dispatch prompt always counts.
    return wis, since.isoformat().replace("+00:00", "Z")


def _wis(main: Index, turns: dict[str, list[datetime]], session: str) -> list[str] | None:
    """The WIs ComputeCost charges these turns to, by the main request each
    answers (`turns`: each request's turn times). A request whose declarations
    all name one WI, at least one qualified, charges that WI; one whose
    declarations name several, all qualified, charges each turn the WI
    declared at the latest moment no later than the turn, else at the first."""
    qualified: dict[str, dict[str, tuple[datetime, str]]] = {}
    for use_id in main.uses:
        found = _declared(main, use_id, session)
        if found is not None:
            qualified.setdefault(found[1], {})[use_id] = (found[2], found[0])
    wis: set[str] = set()
    for request, times in turns.items():
        prompt = main.rows[request].prompt
        proven = qualified.get(request, {})
        # Every declaration answered under this request, qualified or not.
        named: dict[str, set[str]] = {}
        for use_id, uses in main.uses.items():
            for result_uuid, _, _ in main.results.get(use_id, []):
                if getattr(_row(main, result_uuid), "prompt", None) == prompt:
                    named.setdefault(use_id, set()).update(
                        data["wi"] if isinstance(data, dict) and isinstance(data.get("wi"), str)
                        else UNKNOWN for _, _, data in uses)
        names = set().union(*named.values()) if named else set()
        if not proven:
            return None
        if len(names) == 1 and {wi for _, wi in proven.values()} == names:
            wis |= names
            continue
        if set(named) != set(proven):
            return None  # An unqualified declaration might switch the WI in ComputeCost.
        moments = sorted({at for at, _ in proven.values()})
        for at in times:
            current = max((m for m in moments if m <= at), default=moments[0])
            declared = {wi for m, wi in proven.values() if m == current}
            if len(declared) != 1:
                return None  # ComputeCost: ambiguous.
            wis |= declared
    return sorted(wis)


def request_charged(
    main: Index | None, session: str, call: str, others: tuple[set[str], set[str]] | None,
) -> list[str] | None:
    """The WIs ComputeCost charges the main turn that made tool call `call`
    (a session's own `git commit`) to, or None when unproven.

    The turn is every main usage record of the call's message id
    (`request_bindings` binds usage by that id); each must answer one and the
    same request, proven as a sub-agent's request is, and no other record file
    of the session may hold a copy of a main record or of that message.
    """
    if main is None or others is None or not UUID.fullmatch(session) or not _valid(main, session):
        return None
    holders = main.calls.get(call, [])
    row = _row(main, holders[0]) if len(holders) == 1 else None
    if row is None or not row.usage or row.message is None:
        return None
    uuids, messages = others
    if main.uuids & uuids or row.message in messages:
        return None
    requests: dict[str, list[datetime]] = {}
    for uuid in main.turns.get(row.message, []):
        turn = _row(main, uuid)
        start = _at(turn.at) if turn is not None else None
        request = _main_request(main, uuid, start) if start is not None else None
        if request is None or start is None:
            return None
        requests.setdefault(request, []).append(start)
    return _wis(main, requests, session) if len(requests) == 1 else None


def registry_path() -> Path:
    """The ComputeCost source registry the deployment enrolls (ComputeCost
    `cli._account_registry_path`), under the account home even when an Agent
    runs with an isolated HOME."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/pulse/compute-cost/sources.json"


def _private(path: Path, limit: int) -> bytes | None:
    """A private regular file of this account (ComputeCost's export rule), or None."""
    try:
        info = path.lstat()
        if (not path.is_absolute() or path.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit):
            return None
        return path.read_bytes()
    except OSError:
        return None


def exported_child(marker: str) -> Json | None:
    """The child WorkItem as the ComputeCost collector reads it
    (`child_work_items.read_children`): the export its registry names, or None."""
    try:
        registry = json.loads(registry_path().read_bytes())
        holder = registry.get("core_session_writes")
        if isinstance(holder, dict) and isinstance(holder.get("path"), str):
            export = Path(holder["path"])
        else:
            paths = [s.get("path") for s in registry.get("sources", [])
                     if isinstance(s, dict) and s.get("kind") == "harness_core"]
            if len(paths) != 1 or not isinstance(paths[0], str):
                return None
            export = Path(paths[0])
        raw = _private(export, EXPORT_BYTES)
        document = json.loads(raw) if raw is not None else None
    except (OSError, ValueError, AttributeError):
        return None
    if (not isinstance(document, dict) or document.get("schema") != "harness.stats_facts/v1"
            or document.get("read_only") is not True or not isinstance(document.get("items"), list)):
        return None
    for item in document["items"]:
        if not isinstance(item, dict) or item.get("id") != marker or "parent" not in item:
            continue
        parent, closed = item.get("parent"), item.get("closed_at")
        if (not isinstance(parent, str) or not WI.fullmatch(parent) or parent == marker
                or _at(item.get("created_at")) is None
                or closed is not None and _at(closed) is None):
            return None
        return {"parent": parent, "status": item.get("status"),
                "created_at": item["created_at"], "closed_at": closed}
    return None
