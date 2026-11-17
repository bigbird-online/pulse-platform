"""Request-purpose inputs as native input snapshots (`native_input.v1`).

A request reader's output for one key (a usage or a native session) is a
snapshot with one entity per class and key. The node that read it sends the
whole value, stamped with the time it read it; the collector that writes the
stream versions it against that entity's current head (``version_inputs``):
an equal value adds nothing, a different one supersedes the head, so A, B, A
again ends at A. Concurrent heads prove nothing and the key is dropped. Keys
read by this collection itself always win over recorded ones.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .events import parse_timestamp, utc_text
from .receipts import Receipt, ReceiptError, canonical, current_receipts, reference, REF, WI

CODEX_REQUEST = "codex_request_input.v1"
SESSION_DECLARATION = "session_declaration.v1"
CC_REQUEST = "cc_request_binding.v1"
PI_REQUEST = "pi_request_binding.v1"
HOLDER_SESSION = "holder_session.v1"
ROOT_LINK = "codex_root_link.v1"
MAX_VALUE_BYTES = 1024 * 1024
_DECLARATION = {"schema", "wi", "origin", "native_scope_ref", "declared_at", "native_evidence_ref"}
# Every object key a class's value may contain, at any depth.
KEYS = {
    CODEX_REQUEST: {"key", "data", "schema", "request_ref", "target_ref", "native_scope_ref", "captured_at",
                    "native_item_ref", "content_sha256", "prompt_sha256", "input_sha256", "purpose", "origin",
                    "declared_at", "native_evidence_ref"},
    SESSION_DECLARATION: {"key", "data"} | _DECLARATION,
    CC_REQUEST: {"key", "data", "declarations", "ancestry_refs", "qualified_declaration_refs",
                 "request_record_ref", "child_marker", "request_ref", "request_at", "tool_scope_ref"} | _DECLARATION,
    PI_REQUEST: {"key", "data", "request_ref", "wis", "none_until", "evidence"},
    HOLDER_SESSION: {"key", "data", "mode", "starts", "keys"},
    ROOT_LINK: {"key", "data", "parent", "child_scope", "parent_scope", "evidence"},
}
CONSTANTS = {"none", "agent_declaration", "codex_request_input/v1", "codex_native_request_input/v1",
             "codex_request_declaration/v1", "codex_session_wi/v1", "cc_session_wi/v1", "cc_request_wi/v1",
             "session", "request"}
_HEX = re.compile(r"^[0-9a-f]{64}$")


def _leaf(value: str) -> None:
    if REF.fullmatch(value) or WI.fullmatch(value) or _HEX.fullmatch(value) or value in CONSTANTS:
        return
    try:
        parse_timestamp(value, "request input time")
    except (ValueError, TypeError):
        raise ReceiptError("request input values must be references, WIs or times") from None


def _walk(value: object, keys: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key not in keys:
                raise ReceiptError("request input field is not allowlisted")
            _walk(child, keys)
    elif isinstance(value, list):
        for child in value:
            _walk(child, keys)
    elif isinstance(value, str):
        _leaf(value)
    elif value is not None and (type(value) is not int or value < 0) and type(value) is not bool:
        raise ReceiptError("request input value type is not allowed")


def _fail(message: str):
    raise ReceiptError("request input shape: " + message)


def _object(value: object, required: set[str], optional: set[str] = frozenset()) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not required <= set(value) <= required | optional:
        _fail("object fields differ")
    return value


def _refs(value: object, *, nullable: bool = False, many: bool = False) -> None:
    if many and not isinstance(value, list):
        _fail("a list of opaque references required")
    for item in (value if many else [value]):
        if not (nullable and item is None) and not (isinstance(item, str) and REF.fullmatch(item)):
            _fail("opaque reference required")


def _time(value: object, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str):
        _fail("time required")
    parse_timestamp(value, "request input time")


def _purpose(value: object) -> None:
    if not isinstance(value, str) or not (value == "none" or WI.fullmatch(value)):
        _fail("WI or none required")


def _declaration(value: object, key: str | None, extra: set[str] = frozenset()) -> Mapping[str, Any]:
    d = _object(value, {"schema", "wi", "origin", "declared_at", "native_evidence_ref"} | extra, {"native_scope_ref"})
    if d["schema"] not in CONSTANTS or d["origin"] != "agent_declaration":
        _fail("declaration protocol differs")
    _purpose(d["wi"]); _time(d["declared_at"]); _refs(d["native_evidence_ref"])
    if "native_scope_ref" in d and key is not None and d["native_scope_ref"] != key:
        _fail("declaration names another session")
    return d


def _codex(key: str, data: object) -> None:
    from .native_request_purpose import _input
    if not isinstance(data, list) or not data:
        _fail("Codex request items required")
    for item in data:
        if not isinstance(item, list) or len(item) != 2:
            _fail("Codex request item is an (input, declaration) pair")
        original, decision = item
        if _input(dict(original) if isinstance(original, Mapping) else original)["target_ref"] != key:
            _fail("Codex input names another usage")
        if decision is not None:
            # The same joins read_inputs requires before it keeps a declaration.
            from .native_request_purpose import DECLARATION_SCHEMA
            from .receipts import digest
            d = _object(decision, {"schema", "request_ref", "target_ref", "native_scope_ref", "input_sha256",
                                   "purpose", "origin", "declared_at", "native_evidence_ref"})
            _refs(d["native_evidence_ref"]); _purpose(d["purpose"]); _time(d["declared_at"])
            if (d["schema"] != DECLARATION_SCHEMA or d["origin"] != "agent_declaration"
                    or d["request_ref"] != original["request_ref"] or d["target_ref"] != key
                    or d["native_scope_ref"] != original["native_scope_ref"]
                    or d["input_sha256"] != digest(original)
                    or parse_timestamp(d["declared_at"], "declaration")
                    < parse_timestamp(original["captured_at"], "capture")):
                _fail("Codex declaration differs from its input")


def _sessions(key: str, data: object) -> None:
    if not isinstance(data, list):
        _fail("session declarations are a list")
    for item in data:
        _declaration(item, key)


def _cc(key: str, data: object) -> None:
    b = _object(data, {"declarations", "ancestry_refs", "qualified_declaration_refs", "request_record_ref",
                       "child_marker", "request_ref"})
    if not isinstance(b["declarations"], list):
        _fail("CC declarations are a list")
    for d in b["declarations"]:
        d = _declaration(d, None, {"request_ref", "request_at", "tool_scope_ref", "request_record_ref", "ancestry_refs"})
        _refs(d["request_ref"]); _time(d["request_at"]); _refs(d["tool_scope_ref"]); _refs(d["request_record_ref"])
        _refs(d["ancestry_refs"], many=True)
    _refs(b["ancestry_refs"], many=True); _refs(b["qualified_declaration_refs"], many=True)
    _refs(b["request_record_ref"], nullable=True); _refs(b["request_ref"], nullable=True)
    marker = b["child_marker"]
    if marker is not None and (not isinstance(marker, list) or len(marker) != 2
                               or not isinstance(marker[0], str) or not WI.fullmatch(marker[0])):
        _fail("child marker is (WI, reference)")
    if marker is not None:
        _refs(marker[1])


def _pi(key: str, data: object) -> None:
    b = _object(data, {"request_ref", "wis", "none_until", "evidence"})
    _refs(b["request_ref"]); _refs(b["evidence"], many=True); _time(b["none_until"], nullable=True)
    if not isinstance(b["wis"], list) or len(set(b["wis"])) != len(b["wis"]):
        _fail("Pi binding WIs are a distinct list (empty for an undeclared request)")
    for wi in b["wis"]:
        _purpose(wi)


def _holder(key: str, data: object) -> None:
    b = _object(data, {"mode", "starts", "keys"})
    if b["mode"] not in {"session", "request"} or not isinstance(b["starts"], list) or not isinstance(b["keys"], list):
        _fail("holder session is (mode, starts, keys)")
    if b["mode"] == "session" and b["starts"]:
        _fail("a whole-session holder has no request starts")
    for start in b["starts"]:
        _time(start)
    if any(not isinstance(k, str) or not _HEX.fullmatch(k) for k in b["keys"]) or len(set(b["keys"])) != len(b["keys"]):
        _fail("holder keys are distinct digests")


def _root(key: str, data: object) -> None:
    b = _object(data, {"parent", "child_scope", "parent_scope", "evidence"})
    _refs(b["parent"]); _refs(b["child_scope"]); _refs(b["parent_scope"]); _refs(b["evidence"], many=True)
    if b["parent"] == key or len(b["evidence"]) < 3:
        _fail("a root link names another parent and its witnesses")


SHAPES = {CODEX_REQUEST: _codex, SESSION_DECLARATION: _sessions, CC_REQUEST: _cc, PI_REQUEST: _pi,
          HOLDER_SESSION: _holder, ROOT_LINK: _root}


def validate_snapshot(input_class: str, value: Mapping[str, Any]) -> None:
    if set(value) != {"key", "data"} or not isinstance(value["key"], str) or not REF.fullmatch(value["key"]):
        raise ReceiptError("a request input names one opaque key")
    if len(canonical(value)) > MAX_VALUE_BYTES:
        raise ReceiptError("request input exceeds its bound")
    _walk(value["data"], KEYS[input_class])
    SHAPES[input_class](value["key"], value["data"])


def snapshot_entity(input_class: str, key: str) -> str:
    return reference("native_input", [input_class, key])


def snapshot_inputs(input_class: str, values: Mapping[str, Any], *, source_ref: str, read_at: str) -> list[Receipt]:
    """Writer side: one unversioned input per key, stamped with the time it was read."""
    return [Receipt.make("native_input", None, {"input_class": input_class, "source_ref": source_ref,
                                                 "value": {"key": key, "data": data}},
                         entity_ref=snapshot_entity(input_class, key), occurred_at=read_at, observed_at=read_at,
                         producer="native_input.v1", evidence=(source_ref,))
            for key, data in sorted(values.items())]


READ_MARKS_SCHEMA = "pulse_native_input_read_marks/v1"


def load_read_marks(path: Path) -> dict[str, str]:
    """{snapshot entity: latest instant an equal value was read} kept by the stream writer."""
    import os
    import stat as stat_module
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat_module.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ReceiptError("input read marks must be a private regular file")
    value = json.loads(path.read_bytes())
    if not isinstance(value, Mapping) or value.get("schema") != READ_MARKS_SCHEMA or not isinstance(value.get("marks"), Mapping):
        raise ReceiptError("invalid input read marks")
    return dict(value["marks"])


def save_read_marks(path: Path, marks: Mapping[str, str]) -> None:
    import os
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with open(temporary, "wb") as handle:
        handle.write(canonical({"schema": READ_MARKS_SCHEMA, "marks": dict(sorted(marks.items()))}))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def version_inputs(candidates: Iterable[Receipt], facts: Iterable[Receipt],
                   read_marks: dict[str, str] | None = None) -> list[Receipt]:
    """Stream writer side: version snapshot inputs against their entity's latest read.

    A key's latest read is its head's read time, or later when an equal value
    was read again since (``read_marks``, kept by the writer, updated in place;
    the equal read adds no version). An input read before that adds nothing,
    so a retried or reordered push never undoes a newer read. A later equal
    value only moves the mark; a later different one supersedes the single
    head. A different value read at the latest read's own instant is kept
    beside the head, so the key becomes a conflict and proves nothing.
    Immutable classes pass through.
    """
    marks = read_marks if read_marks is not None else {}
    rows = list(candidates)
    rows.sort(key=lambda r: (parse_timestamp(r.occurred_at, "input read time").timestamp()
                             if r.occurred_at else float("-inf"), r.fact_ref))
    snapshots = [r for r in rows if r.kind == "native_input" and r.payload["input_class"] in KEYS]
    if not snapshots:
        return rows
    entities = {r.entity_ref for r in snapshots}
    active, conflicts = current_receipts([r for r in facts if r.kind == "native_input" and r.entity_ref in entities])
    heads = {r.entity_ref: r for r in active}
    result = []
    for row in rows:
        if row not in snapshots:
            result.append(row)
            continue
        if row.entity_ref in conflicts:
            continue
        head = heads.get(row.entity_ref)
        if head is not None:
            at = parse_timestamp(row.occurred_at, "input read time")
            latest = parse_timestamp(head.occurred_at, "input read time")
            mark = marks.get(row.entity_ref)
            if mark is not None:
                latest = max(latest, parse_timestamp(mark, "input read time"))
            if at < latest:
                continue
            if head.payload == row.payload:
                if at > latest:
                    marks[row.entity_ref] = row.occurred_at
                continue
            if at == latest:
                conflicts[row.entity_ref] = [head, row]
                result.append(row)  # Same instant, different value: a visible conflict.
                continue
            row = Receipt.make("native_input", None, row.payload, entity_ref=row.entity_ref,
                               supersedes=head.fact_ref, occurred_at=row.occurred_at, observed_at=row.observed_at,
                               producer=row.producer, evidence=row.evidence)
        marks.pop(row.entity_ref, None)  # The new head's own read time is the latest read.
        heads[row.entity_ref] = row
        result.append(row)
    return result


def recorded(facts: Iterable[Receipt], input_class: str, exclude: set[str] = frozenset(), *,
             with_source: bool = False) -> dict[str, Any]:
    """Reader side: per key the current value; concurrent heads drop the key.

    ``with_source`` returns (value, recording source reference) per key.
    """
    rows = [r for r in facts if r.kind == "native_input" and r.payload["input_class"] == input_class]
    if not rows:
        return {}
    active, _ = current_receipts(rows)
    found = {}
    for row in active:
        payload = row.payload
        key = payload["value"]["key"]
        if key not in exclude and row.entity_ref == snapshot_entity(input_class, key):
            found[key] = (payload["value"]["data"], payload["source_ref"]) if with_source else payload["value"]["data"]
    return found


# --- projections: reader output <-> JSON value, both directions exact ---

def codex_values(inputs: Iterable[tuple]) -> dict[str, list]:
    grouped = defaultdict(list)
    for target, original, decision in inputs:
        grouped[target].append([original, decision])
    return dict(grouped)


def codex_tuples(values: Mapping[str, list]) -> list[tuple]:
    return [(target, original, decision) for target, items in values.items() for original, decision in items]


def pi_values(bindings: Mapping[tuple, Mapping[str, Any]]) -> dict[str, dict]:
    out = {}
    for (session, ident, activity), b in bindings.items():
        out[reference("usage", [session, ident, activity])] = {
            "request_ref": b["request_ref"], "wis": sorted(b["wis"]),
            "none_until": utc_text(b["none_until"]) if b.get("none_until") is not None else None,
            "evidence": list(b["evidence"])}
    return out


def pi_bindings(values: Mapping[str, Mapping[str, Any]]) -> dict[str, dict]:
    return {ref: {"request_ref": v["request_ref"], "wis": set(v["wis"]),
                  "none_until": parse_timestamp(v["none_until"], "Pi declaration") if v["none_until"] else None,
                  "evidence": tuple(v["evidence"])} for ref, v in values.items()}


def cc_values(bindings: Mapping[str, Mapping[str, Any]]) -> dict[str, dict]:
    return {ref: _json(b) for ref, b in bindings.items()}


def _json(value):
    if isinstance(value, Mapping):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


def usage_versions(active: list[Receipt], conflicts: Mapping[str, list[Receipt]],
                   entities: set[str], scopes: set[str]) -> list[Receipt]:
    """Current usage versions a recorded input names, by entity or native scope."""
    rows = [*active, *(r for versions in conflicts.values() for r in versions)]
    return [r for r in rows if r.kind == "usage"
            and (r.entity_ref in entities or r.payload.get("native_scope_ref") in scopes)]


def recorded_requests(facts: list[Receipt], active: list[Receipt], conflicts: Mapping[str, list[Receipt]], *,
                      codex_targets: set[str], session_scopes: set[str], cc_usages: set[str],
                      pi_usages: set[str]) -> dict[str, Any]:
    """Request inputs other nodes recorded for keys this collection did not read."""
    codex = recorded(facts, CODEX_REQUEST, codex_targets)
    sessions = recorded(facts, SESSION_DECLARATION, session_scopes)
    cc = recorded(facts, CC_REQUEST, cc_usages)
    pi = pi_bindings(recorded(facts, PI_REQUEST, pi_usages))
    named = set(cc) | set(pi)
    usages = usage_versions(active, conflicts, named, set(sessions)) if named or sessions else []
    return {"codex": codex_tuples(codex), "sessions": sessions, "cc": cc, "pi": pi, "usages": usages}


def holder_values(candidates: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    """Writer side of ``holder_supplement.session_candidates``."""
    return {c["scope"]: {"mode": c["mode"], "starts": [utc_text(t) for t in c["starts"]], "keys": list(c["keys"])}
            for c in candidates}


def recorded_holder_candidates(facts: Iterable[Receipt], exclude: set[str]) -> list[dict[str, Any]]:
    """Recorded session candidates, ready for ``holder_supplement.held_sessions``."""
    return [{"scope": scope, "mode": v["mode"], "keys": v["keys"],
             "starts": [parse_timestamp(t, "recorded request start") for t in v["starts"]]}
            for scope, v in sorted(recorded(facts, HOLDER_SESSION, exclude).items())]


def root_values(links: Mapping[str, Mapping[str, Any]]) -> dict[str, dict]:
    return {child: {k: (list(v) if k == "evidence" else v) for k, v in link.items()} for child, link in links.items()}


def recorded_root_links(facts: Iterable[Receipt], active: list[Receipt], exclude: set[str]) -> dict[str, dict]:
    """Recorded Codex root links whose child and parent both carry the recording source.

    The current versions of both usages must name the source that recorded
    the link; a link from another source or a parent read elsewhere lends no
    task identity.
    """
    links = recorded(facts, ROOT_LINK, exclude, with_source=True)
    if not links:
        return {}
    heads = {r.entity_ref: r for r in active if r.kind == "usage"}
    result = {}
    for child, (link, source_ref) in links.items():
        rows = (heads.get(child), heads.get(link["parent"]))
        if all(row is not None and source_ref in row.evidence for row in rows):
            result[child] = {**link, "evidence": tuple(link["evidence"])}
    return result
