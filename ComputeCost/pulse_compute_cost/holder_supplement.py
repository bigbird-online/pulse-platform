"""Holder supplement: undeclared work takes the one WI its own session wrote.

A declaration remains the purpose of a request. Where a request carries no
declaration, the native core's ``stats-facts`` export supplies a narrower,
separately labelled fact: the WorkItems this native session committed writes
to. The core exports each write as ``digest(["stats_session_write",
native_session_ref])``; only a reader holding the native session ID, as this
collector does, can recompute the key. No native ID leaves either side.

A CC or Pi request runs from its human input to the next one. When that
session wrote exactly one WI during the request, the request's usage is
``assigned`` to it; several WIs make it ``ambiguous``; none adds nothing. A
Codex session is judged whole: one WI ever written assigns all its usage.
Each answer is an attribution under ``holder_supplement.v1``. The query
applies it only where no declaration, Host assignment or reviewed
classification exists, so a supplement never replaces a declaration.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Mapping

from .events import parse_timestamp
from dataclasses import replace
from .receipts import EXPLORATION_POLICY, Receipt, ReceiptError, canonical, current_receipts, digest, reference, supplement_source

POLICY = "holder_supplement.v1"
SCHEMA = "harness.stats_facts/v1"
MAX_BYTES = 8 * 1024 * 1024
_WI = re.compile(r"wi_\d{8}_\d{3}")
_KEY = re.compile(r"[0-9a-f]{64}")
_ACTOR = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def session_key(namespace: str, actor: str, native_id: str, surface: str) -> str:
    """The core's native_session_ref, then the export's domain-separated key."""
    return digest(["stats_session_write", "session." + digest([namespace, actor, native_id, surface])])


def read_session_writes(path: Path) -> tuple[str, dict[str, list[tuple[datetime, str]]], set[str]]:
    """(namespace, {session key: sorted [(at, wi)]}, actors) from a private export."""
    info = path.lstat()
    if (not path.is_absolute() or path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
        raise ReceiptError("core session writes must be a private owned export")
    document = json.loads(path.read_bytes())
    if (not isinstance(document, Mapping) or document.get("schema") != SCHEMA
            or document.get("read_only") is not True or document.get("source") != "journal"
            or not isinstance(document.get("namespace"), str) or not document["namespace"]
            or not isinstance(document.get("items"), list)):
        raise ReceiptError("unsupported core stats facts")
    writes: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    actors: set[str] = set()
    for item in document["items"]:
        if not isinstance(item, Mapping):
            raise ReceiptError("invalid core WorkItem")
        if not isinstance(item.get("id"), str) or not _WI.fullmatch(item["id"]):
            continue  # Only a canonical WI can be an attribution.
        for write in item.get("session_writes", []):
            if (not isinstance(write, Mapping) or set(write) != {"session", "actor", "at"}
                    or not isinstance(write["session"], str) or not _KEY.fullmatch(write["session"])
                    or not isinstance(write["actor"], str) or not _ACTOR.fullmatch(write["actor"])):
                raise ReceiptError("invalid core session write")
            writes[write["session"]].append((parse_timestamp(write["at"], "session write"), item["id"]))
            actors.add(write["actor"])
    return document["namespace"], {k: sorted(v) for k, v in writes.items()}, actors


def read_creations(path: Path) -> dict[str, datetime]:
    """When each canonical WI of the same private export was created."""
    document = json.loads(path.read_bytes())
    return {item["id"]: parse_timestamp(item["created_at"], "WI creation")
            for item in document.get("items", [])
            if isinstance(item, Mapping) and isinstance(item.get("id"), str) and _WI.fullmatch(item["id"])
            and isinstance(item.get("created_at"), str)}


def cc_request_starts(paths: Iterable[Path]) -> dict[str, list[datetime]]:
    """Each main-session human input instant, by CC session ID.

    Subagent prompts, compaction summaries, runtime task notifications and
    interrupt markers continue the request they belong to.
    """
    from .cc_input_context import _task_origin
    from .cc_input_context import _request_text
    result: dict[str, set[datetime]] = defaultdict(set)
    for path in paths:
        if path.parent.name == "subagents" or path.is_symlink() or not path.is_file():
            continue
        sid = path.parent.name if path.name == "main.jsonl" else path.stem
        for raw in path.read_bytes().splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                break  # A torn tail ends the verified prefix.
            if (not isinstance(row, dict) or row.get("isSidechain") is not False
                    or row.get("sessionId") != sid or row.get("isCompactSummary")
                    or _task_origin(row.get("origin"))):
                continue
            text = _request_text(row)
            if text is None or text.lstrip().startswith("<task-notification>"):
                continue
            try:
                result[sid].add(parse_timestamp(row.get("timestamp"), "CC request"))
            except ValueError:
                continue
    return {sid: sorted(values) for sid, values in result.items()}


def pi_request_starts(paths: Iterable[Path]) -> dict[str, list[datetime]]:
    """Each user message instant, by Pi session header ID."""
    result: dict[str, set[datetime]] = defaultdict(set)
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        lines = path.read_bytes().splitlines()
        try:
            header = json.loads(lines[0]) if lines else None
        except ValueError:
            continue
        if not isinstance(header, dict) or header.get("type") != "session" or not isinstance(header.get("id"), str):
            continue
        starts = result[header["id"]]
        for raw in lines[1:]:
            try:
                row = json.loads(raw)
            except ValueError:
                break
            message = row.get("message") if isinstance(row, dict) and row.get("type") == "message" else None
            if isinstance(message, dict) and message.get("role") == "user":
                try:
                    starts.add(parse_timestamp(row.get("timestamp"), "Pi request"))
                except ValueError:
                    continue
    return {sid: sorted(values) for sid, values in result.items()}


def _primary_claims(rows: Iterable[Receipt]) -> set[str]:
    """Targets that already have a declaration, Host assignment or classification."""
    claimed = set()
    for row in rows:
        p = row.payload
        if (row.kind == "attribution" and p.get("scope") != "actor"
                and supplement_source(row) is None
                and (p.get("state") != "unassigned" or p.get("wi") is not None or p.get("work_kind") is not None)):
            claimed.add(p["target_ref"])
    return claimed


def supplement_attributions(facts: list[Receipt], sessions: list[Mapping[str, Any]],
                            writes: Mapping[str, list[tuple[datetime, str]]], observed: str,
                            created: Mapping[str, datetime] | None = None) -> list[Receipt]:
    """Holder supplements for the usage of the given native sessions.

    ``sessions`` items: ``{"scope", "key", "mode": "request"|"session",
    "starts": [...]}``. Existing supplement heads are revised, never forked.
    """
    by_scope = {s["scope"]: s for s in sessions if s["key"] in writes}
    if not by_scope:
        return []
    # Decode only usage and attribution payloads, and resolve heads only
    # among the facts these sessions can touch.
    usage_facts = [r for r in facts if r.kind == "usage" and r.payload.get("native_scope_ref") in by_scope]
    current_usage, observed_twice = current_receipts(usage_facts)
    # One request is usually observed twice (Host and native transcript); the
    # query joins them by entity. Use one observation per entity, and treat a
    # WI any observation carries as that entity's own WI.
    for rows in observed_twice.values():
        rows = [r for r in rows if r.payload.get("native_scope_ref") in by_scope]
        if rows:
            first = min(rows, key=lambda r: (r.payload.get("interval_start") or r.occurred_at or "", r.fact_ref))
            known = {r.payload.get("wi") for r in rows} - {None}
            current_usage.append(first if not known else replace(first, _payload_json=canonical(
                {**first.payload, "wi": next(iter(known)) if len(known) == 1 else "ambiguous"})))
    targets = set(by_scope) | {r.entity_ref for r in current_usage}
    targets |= {r.payload["execution_ref"] for r in current_usage if r.payload.get("execution_ref")}
    attributions = [r for r in facts if r.kind == "attribution"
                    and (r.producer == POLICY or r.payload.get("target_ref") in targets)]
    active, conflicts = current_receipts(attributions)
    claimed = _primary_claims([*active, *(r for rs in conflicts.values() for r in rs)])
    usage: dict[str, list[Receipt]] = defaultdict(list)
    declared: dict[str, list[Receipt]] = defaultdict(list)
    for row in current_usage:
        p = row.payload
        if p.get("wi") is None and row.entity_ref not in claimed and p.get("execution_ref") not in claimed:
            usage[p["native_scope_ref"]].append(row)
        else:
            declared[p["native_scope_ref"]].append(row)
    heads = {r.entity_ref: [r] for r in active if r.producer == POLICY}
    heads.update({k: rs for k, rs in conflicts.items() if any(r.producer == POLICY for r in rs)})
    output: list[Receipt] = []

    def emit(target: str, wis: set[str], start: str, proofs: set[str]) -> None:
        entity = reference("attribution", [POLICY, target])
        prior = heads.get(entity, [])
        if not wis and not prior:
            return
        state = "assigned" if len(wis) == 1 else "ambiguous" if wis else "unassigned"
        payload = {"target_ref": target, "wi": next(iter(wis)) if len(wis) == 1 else None,
                   "executor": None, "responsible": None, "delegator": None,
                   "state": state, "policy": POLICY, "effective_at": start}
        if prior and all(r.payload == payload for r in prior):
            return
        for predecessor in prior or [None]:
            output.append(Receipt.make("attribution", None, payload, entity_ref=entity,
                supersedes=predecessor.fact_ref if predecessor else None, occurred_at=start,
                observed_at=observed, producer=POLICY, evidence=tuple(sorted(proofs))))

    def explore(scope: str, starts: list[datetime], rows: list[Receipt], declared_rows: list[Receipt],
                held: list[tuple[datetime, str]]) -> None:
        windows: dict[int, dict[str, Any]] = defaultdict(lambda: {"writes": set(), "declared": False, "rows": []})
        for at, wi in held:
            if (i := bisect_right(starts, at) - 1) >= 0:
                windows[i]["writes"].add(wi)
        for row in declared_rows:
            if (i := bisect_right(starts, boundary(row)[1]) - 1) >= 0:
                windows[i]["declared"] = True
        for row in rows:
            if (i := bisect_right(starts, boundary(row)[1]) - 1) >= 0:
                windows[i]["rows"].append(row)
        anchors = [j for j, w in windows.items() if w["writes"] or w["declared"]]
        first_anchor = min(anchors) if anchors else None
        for index, window in sorted(windows.items()):
            if not window["rows"]:
                continue
            if window["writes"] or window["declared"]:
                # A request with its own WI evidence is no exploration; an
                # earlier conclusion about its usage is withdrawn below.
                decided, wi, opening = False, None, False
            else:
                decided, wi = _exploration_target(windows, index, starts, created or {})
                # Only a session's opening exploration, before any declared or
                # written WI, becomes non-WI work when no WI follows. A later
                # undeclared request continues the session's work: it stays
                # open unless the next request creates a WI.
                opening = first_anchor is not None and index < first_anchor
            outcome = ("wi", wi) if decided and wi else ("none", None) if decided and opening else None
            for row in window["rows"]:
                entity = reference("attribution", [EXPLORATION_POLICY, row.entity_ref])
                prior = heads.get(entity, [])
                if outcome is None and not prior:
                    continue
                start = boundary(row)[0]
                payload = {"target_ref": row.entity_ref, "wi": wi if outcome and outcome[0] == "wi" else None,
                           "executor": None, "responsible": None, "delegator": None,
                           "state": "assigned" if outcome else "unassigned", "policy": EXPLORATION_POLICY,
                           "effective_at": start, **({"work_kind": "none"} if outcome and outcome[0] == "none" else {})}
                if prior and all(r.payload == payload for r in prior):
                    continue
                proofs = {row.fact_ref, reference("holder_request", [scope, starts[index].isoformat()])}
                if outcome and outcome[0] == "wi":
                    proofs.add(reference("holder_creation", [wi, created[wi].isoformat()]))
                for predecessor in prior or [None]:
                    output.append(Receipt.make("attribution", None, payload, entity_ref=entity,
                        supersedes=predecessor.fact_ref if predecessor else None, occurred_at=start,
                        observed_at=observed, producer=POLICY, evidence=tuple(sorted(proofs))))

    def boundary(row: Receipt) -> tuple[str, datetime]:
        p = row.payload
        start = p.get("interval_start") or row.occurred_at
        return start, parse_timestamp(p.get("interval_end") or row.occurred_at, "usage end")

    for scope, session in sorted(by_scope.items()):
        rows = [r for r in usage.get(scope, []) if (r.payload.get("interval_start") or r.occurred_at)]
        if not rows:
            continue
        held = writes[session["key"]]
        write_proof = lambda items: {reference("core_session_write", [session["key"], wi, at.isoformat()])
                                     for at, wi in items}
        if session["mode"] == "session":
            first = min((boundary(r)[0] for r in rows), key=lambda t: (parse_timestamp(t, "usage"), t))
            emit(scope, {wi for _, wi in held}, first,
                 write_proof(held) | {reference("holder_session", [scope])})
            continue
        starts = session["starts"]
        for row in sorted(rows, key=lambda r: r.entity_ref):
            start, end = boundary(row)
            index = bisect_right(starts, end) - 1
            if index < 0:
                continue
            low = starts[index]
            high = starts[index + 1] if index + 1 < len(starts) else None
            inside = [(at, wi) for at, wi in held if low <= at and (high is None or at < high)]
            emit(row.entity_ref, {wi for _, wi in inside}, start,
                 write_proof(inside) | {row.fact_ref, reference("holder_request", [scope, low.isoformat()])})
        if created is not None:
            explore(scope, starts, rows, declared.get(scope, []), held)
    return output


def _exploration_target(windows: Mapping[int, Mapping[str, Any]], index: int, starts: list[datetime],
                        created: Mapping[str, datetime]) -> tuple[bool, str | None]:
    """(decided, WI) for an undeclared request without writes.

    The next request that declares or writes decides: a WI it creates is the
    purpose of the exploration before it; otherwise the exploration created
    none. With no later deciding request the purpose stays open.
    """
    later = sorted(j for j, w in windows.items() if j > index and (w["writes"] or w["declared"]))
    if not later:
        return False, None
    j = later[0]
    low, high = starts[j], (starts[j + 1] if j + 1 < len(starts) else None)
    made = {wi for wi in windows[j]["writes"]
            if wi in created and low <= created[wi] and (high is None or created[wi] < high)}
    if len(made) > 1:
        return False, None
    return True, next(iter(made), None)


def native_sessions(kind: str, files: Iterable[Path], namespace: str, actors: set[str], surface: str,
                    writes: Mapping[str, list[tuple[datetime, str]]]) -> list[dict[str, Any]]:
    """The source's native sessions that wrote any WI, with their request starts."""
    return held_sessions(session_candidates(kind, files, namespace, actors, surface,
                                            wanted=lambda keys: held_key(keys, writes) is not None), writes)


def held_key(keys: Iterable[str], writes: Mapping[str, Any]) -> str | None:
    """The one candidate key the core exported writes for, else None."""
    found = set(keys) & set(writes)
    return next(iter(found)) if len(found) == 1 else None


def held_sessions(candidates: Iterable[Mapping[str, Any]], writes: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Candidates whose one key wrote a WI, in the form supplement_attributions reads."""
    result = []
    for candidate in candidates:
        key = held_key(candidate["keys"], writes)
        if key is not None:
            result.append({"scope": candidate["scope"], "key": key, "mode": candidate["mode"],
                           "starts": list(candidate["starts"])})
    return result


def session_candidates(kind: str, files: Iterable[Path], namespace: str, actors: set[str], surface: str, *,
                       wanted=lambda keys: True) -> list[dict[str, Any]]:
    """Each native session with its key for every exported actor.

    Every session is listed; request starts are read only for sessions
    ``wanted`` selects by their keys (the others list none).
    A node holding the native IDs records these as inputs; a node without them
    matches the keys against a later export.
    """
    def keys(native_id: str) -> list[str]:
        return sorted({session_key(namespace, actor, native_id, surface) for actor in actors})

    files = sorted(set(files))
    if kind == "codex":
        from .receipt_sources import _codex_rollout_file_ids
        threads = {ids[0] for f in files if (ids := _codex_rollout_file_ids(f)) is not None}
        return [{"scope": reference("native_session", ["codex", thread]), "keys": keys(thread), "mode": "session",
                 "starts": []} for thread in sorted(threads)]
    if kind == "cc":
        groups = {}
        for f in files:
            if f.parent.name != "subagents":
                groups.setdefault(f.parent.name if f.name == "main.jsonl" else f.stem, []).append(f)
        reader, runtime = cc_request_starts, "cc"
    elif kind == "pi":
        groups = {}
        for f in files:
            if f.is_symlink() or not f.is_file():
                continue
            with f.open("rb") as handle:
                try:
                    header = json.loads(handle.readline(2 * 1024 * 1024))
                except ValueError:
                    continue
            if isinstance(header, dict) and header.get("type") == "session" and isinstance(header.get("id"), str):
                groups.setdefault(header["id"], []).append(f)
        reader, runtime = pi_request_starts, "pi"
    else:
        return []
    every = {sid: keys(sid) for sid in groups}
    starts = reader([f for sid, k in every.items() if wanted(k) for f in groups[sid]])
    return [{"scope": reference("native_session", [runtime, sid]), "keys": k, "mode": "request",
             "starts": starts.get(sid, []) if wanted(k) else []} for sid, k in sorted(every.items())]
