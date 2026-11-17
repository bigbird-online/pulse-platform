"""Join native requests to existing Harness authority and immutable Pi bounds.

No current route/model or directory name supplies attribution. The native
session hash, canonical WI execution, immutable baseline and verified source
prefix must all agree. This module only emits ordinary UsageLedger receipts.
"""
from __future__ import annotations
from collections import defaultdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping
import yaml
from .events import parse_timestamp, utc_text
from .cc_usage import (
    CC_SNAPSHOT_SCHEMA, CcUsageError, cc_authority_ref, cc_opaque, cc_session_sources, cc_source_ref,
    validate_cc_session_snapshot,
)
from .pi_usage import validate_pi_session_snapshot, _opaque
from .receipts import Receipt, ReceiptError, reference, TOKEN_FIELDS, digest, current_receipts

SNAPSHOT_NAME = re.compile(r"(wi_\d{8}_\d{3})-([a-f0-9]{64})-(baseline|open|sealed)\.json$")
CORE = ("work_item_id", "agent_id", "epoch", "phase", "runtime", "session_ref")


def execution_bindings(roots: list[Path]) -> dict[tuple[Any, ...], Mapping[str, Any]]:
    result = {}
    for root in roots:
        root = root / ".harness" if (root / ".harness").is_dir() else root
        folder = root / "executions"
        if root.is_symlink() or folder.is_symlink():
            raise ReceiptError("unsafe canonical execution root")
        for path in sorted(folder.glob("wi_*.yaml")):
            if path.is_symlink() or not path.is_file():
                raise ReceiptError("unsafe canonical execution binding")
            row = yaml.safe_load(path.read_bytes())
            if not isinstance(row, Mapping) or row.get("schema_version") != "work_item_execution_binding/v1":
                raise ReceiptError("unsupported canonical execution binding")
            if path.stem != row.get("work_item_id") or type(row.get("epoch")) is not int or row["epoch"] < 1:
                raise ReceiptError("canonical execution identity mismatch")
            runtime = row.get("runtime")
            # Harness names the execution surface, not necessarily the model
            # runtime. The native header and bounded snapshot prove Pi later.
            if runtime != "pi" and not re.fullmatch(r"resident_harness@surface_v1_[a-z0-9_]+", str(runtime)):
                continue
            if not re.fullmatch(r"session_v1_[a-f0-9]{64}", str(row.get("session_ref"))):
                raise ReceiptError("canonical execution session identity invalid")
            key = tuple("pi" if k == "runtime" else row.get(k) for k in CORE)
            result[key] = row
    return result


def _private_snapshot(path: Path) -> Mapping[str, Any]:
    st = path.lstat()
    if path.is_symlink() or not path.is_file() or st.st_uid != os.getuid():
        raise ReceiptError("Pi attribution boundary must be an owner-only regular file")
    value = json.loads(path.read_bytes())
    if value.get("schema") != "pi_session_usage_snapshot/v2":
        return value  # Legacy formats carry no request-level attribution authority.
    if st.st_mode & 0o077:
        raise ReceiptError("bounded Pi snapshot must be owner-only")
    validate_pi_session_snapshot(value)
    return value


_HOST_USAGE_MAPPING = {"pi": "relay_pi.v1", "cc": "cc_message.v1"}


def _host_authorities(facts: list[Receipt], runtime: str = "pi") -> dict[tuple[str, str], dict[str, Any]]:
    """Host froze this assignment before dispatch; no current route is consulted.

    CC 的 Host 回执以 cc_message.v1 携带同一 native session 作用域；cc_sdk 与 cc
    共用 CC transcript，因此两者的执行均可证明 CC 原生会话的归属。
    """
    current, _ = current_receipts(list({r.fact_ref: r for r in facts}.values()))
    executions = {r.entity_ref: r for r in current if r.kind == "execution" and r.producer == "relay_host.v1"}
    runtimes = {"pi"} if runtime == "pi" else {"cc", "cc_sdk"}
    grouped = defaultdict(list)
    for usage in current:
        if usage.kind != "usage" or usage.payload["mapping"] != _HOST_USAGE_MAPPING[runtime]:
            continue
        p = usage.payload; ex = executions.get(p["execution_ref"])
        if not ex or not ex.payload["assignment_ref"] or not p.get("native_scope_ref"):
            continue
        ep = ex.payload
        if not ep["wi"] or not ep["executor"] or ep["runtime"] not in runtimes or ex.occurred_at is None:
            continue
        grouped[(ep["assignment_ref"], p["native_scope_ref"])].append(ex)
    result = {}
    for key, rows in grouped.items():
        if len({(r.payload["wi"], r.payload["executor"]) for r in rows}) != 1:
            continue  # Conflicting authority never becomes last-writer-wins.
        first = min(rows, key=lambda r: parse_timestamp(r.occurred_at, "Host assignment"))
        result[key] = {"wi": first.payload["wi"], "executor": first.payload["executor"],
                       "not_before": first.occurred_at, "evidence": first.entity_ref}
    return result


def bind_pi_boundaries(path: Path, data: bytes, rows: list[Receipt],
                       bindings: Mapping[tuple[Any, ...], Mapping[str, Any]], observed: str, *, host_facts: list[Receipt] | None = None,
                       host_authorities: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
                       native_bindings: Mapping[tuple[Any, ...], Mapping[str, Any]] | None = None,
                       registered_native_paths: list[Path] | None = None,
                       diagnostics: list[str] | None = None) -> list[Receipt]:
    folder = path.parent / "compute-cost"
    if not folder.is_dir():
        return []
    if folder.is_symlink():
        raise ReceiptError("unsafe Pi attribution directory")
    header = json.loads(data.splitlines()[0])
    session = header.get("id")
    if header.get("type") != "session" or not isinstance(session, str):
        return []
    authority = "session_v1_" + sha256(("pulse-session-v1\0" + session).encode()).hexdigest()
    by_entity = {r.entity_ref: r for r in rows if r.kind == "usage"}
    positions = {}; cursor = 0
    activities = {"assistant": "assistant_turn", "tool_summary": "nested_tool_llm",
                  "compaction": "compaction", "branch_summary": "branch_summary"}
    for line in data.splitlines(keepends=True):
        start = cursor; cursor += len(line)
        if not line.endswith(b"\n"):
            break
        try:
            raw = json.loads(line)
        except ValueError:
            break
        activity = raw.get("type")
        if activity == "message":
            role = raw.get("message", {}).get("role")
            activity = "assistant" if role == "assistant" else "tool_summary" if role == "toolResult" else None
        if activity not in activities or not raw.get("id"):
            continue
        entity = reference("usage", [session, raw["id"], activity])
        if entity in by_entity:
            native = _opaque("pi_usage", f"{session}\0{raw['id']}\0{activities[activity]}")
            positions[native] = (start, cursor, by_entity[entity])
    result = []
    if host_authorities is None:
        host_authorities = _host_authorities(host_facts or [])
    for candidate in sorted(folder.glob("wi_*.json")):
        match = SNAPSHOT_NAME.fullmatch(candidate.name)
        if not match or match[3] == "baseline":
            continue
        snap = _private_snapshot(candidate)
        if snap.get("schema") != "pi_session_usage_snapshot/v2" or snap["authority_ref"] != authority:
            continue
        key = (snap["work_item_id"], snap["agent_id"], snap["epoch"], snap["phase"], "pi", authority)
        native = (native_bindings or {}).get(key)
        if native and snap["snapshot_sha256"] not in native["native_snapshots"]:
            native = None  # An unrecorded local file cannot claim SCL acceptance.
        canonical = native or bindings.get(key)
        if canonical and canonical["runtime"].startswith("resident_harness@"):
            if canonical["runtime"].split("@", 1)[1] != snap["execution_surface"]:
                raise ReceiptError("canonical execution surface disagrees with native boundary")
        assignment = reference("assignment", snap["runtime_binding_sha256"])
        scope = reference("native_session", ["pi", session])
        host = host_authorities.get((assignment, scope))
        if canonical is None and (host is None or host["wi"] != snap["work_item_id"] or host["executor"] != snap["agent_id"]):
            continue
        if snap["work_item_id"] != match[1] or snap["runtime_binding_sha256"] != match[2]:
            raise ReceiptError("Pi snapshot filename disagrees with binding")
        base_path = folder / f"{match[1]}-{match[2]}-baseline.json"
        if not base_path.exists() and not base_path.is_symlink():
            # An abandoned capture left a final snapshot without its baseline.
            # It proves nothing; the session's other snapshots still bind.
            if diagnostics is not None:
                diagnostics.append("pi_snapshot_baseline_missing")
            continue
        base = _private_snapshot(base_path)
        identity = ("work_item_id", "agent_id", "epoch", "phase", "authority_ref", "runtime_binding_sha256", "execution_surface")
        boundary = snap["attribution_boundary"]
        if any(base[k] != snap[k] for k in identity) or boundary["baseline_snapshot_sha256"] != base["snapshot_sha256"]:
            raise ReceiptError("Pi snapshot does not descend from its exact baseline")
        if boundary["baseline"] != base["attribution_boundary"]["baseline"]:
            raise ReceiptError("Pi baseline position changed")
        matches_this_file = all(edge["source_bytes"] <= len(data)
            and sha256(data[:edge["source_bytes"]]).hexdigest() == edge["source_prefix_sha256"]
            for edge in (boundary["baseline"], boundary["final"]))
        if not matches_this_file:
            if _other_registered_prefix(path, session, boundary, registered_native_paths or []):
                continue  # This snapshot belongs to a different enrolled native file.
            raise ReceiptError("Pi attribution source prefix no longer matches")
        start, end = boundary["baseline"]["source_bytes"], boundary["final"]["source_bytes"]
        lower = base["generated_at"]
        if native and parse_timestamp(native["not_before"], "measurement start") > parse_timestamp(lower,"baseline"):
            lower = native["not_before"]
        if canonical is None and parse_timestamp(host["not_before"], "assignment") > parse_timestamp(lower, "baseline"):
            lower = host["not_before"]
        selected = []
        for part in snap["summary"]["request_partitions"]:
            source = positions.get(part["fact_ref"])
            if source is None or not (start <= source[0] and source[1] <= end):
                raise ReceiptError("Pi bounded request missing from verified native prefix")
            row = source[2]; p = row.payload
            if row.occurred_at is None or parse_timestamp(row.occurred_at, "native time") < parse_timestamp(base["generated_at"], "baseline time"):
                raise ReceiptError("native request precedes the witnessed binding baseline")
            expected = dict(zip(TOKEN_FIELDS, (part["native_input"], part["cache_read"], part["cache_write"], part["output"])))
            if any(p["tokens"][k] is not None and p["tokens"][k] != expected[k] for k in TOKEN_FIELDS):
                raise ReceiptError("Pi attribution quantities disagree with native request")
            if p["model"] is not None and p["model"] != part["model"]:
                raise ReceiptError("Pi attribution model contradicts native request")
            if native and native["not_after"] and parse_timestamp(row.occurred_at, "native time") >= parse_timestamp(native["not_after"],"measurement end"):
                continue
            if parse_timestamp(row.occurred_at, "native time") >= parse_timestamp(lower, "assignment start"):
                selected.append(row)
        common = {"wi": snap["work_item_id"], "executor": snap["agent_id"], "responsible": None, "delegator": None}
        evidence = (reference("scl_assignment", list(key)), reference("pi_baseline", base["snapshot_sha256"]))
        if native:
            evidence = tuple(sorted((*evidence, native["evidence_ref"])))
        elif canonical is None:
            evidence = tuple(sorted((*evidence, host["evidence"])))
        for row in selected:
            if row.occurred_at is None:
                continue
            result.append(Receipt.make("attribution", [row.entity_ref, snap["runtime_binding_sha256"]],
                {**common, "target_ref": row.entity_ref, "assignment_ref": assignment, "state": "assigned",
                 "policy": "scl_verified_native_boundary.v1", "effective_at": row.occurred_at},
                occurred_at=row.occurred_at, observed_at=observed, producer="scl_native_binding.v1", evidence=evidence))
    return result


def _other_registered_prefix(path: Path, session: str, boundary: Mapping[str, Any], registered: list[Path]) -> bool:
    """A session id may survive a native file replacement; byte proofs may not.

    Select only another actually enrolled file carrying BOTH exact prefixes.
    No directory scan, name/mtime winner, or mismatched-prefix forgiveness.
    A missing last valid original remains the existing source error.
    """
    import stat
    for candidate in registered:
        if candidate == path or candidate.parent != path.parent:
            continue
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid():
                raise ReceiptError('unsafe registered Pi prefix witness')
            try:
                header = json.loads(stream.readline(2 * 1024 * 1024))
            except (ValueError, UnicodeError):
                continue
            if header.get('type') != 'session' or header.get('id') != session:
                continue
            matches = True
            for edge in (boundary['baseline'], boundary['final']):
                length = edge['source_bytes']
                if length > meta.st_size:
                    matches = False; break
                stream.seek(0); remaining = length; hasher = sha256()
                while remaining:
                    chunk = stream.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    hasher.update(chunk); remaining -= len(chunk)
                if remaining or hasher.hexdigest() != edge['source_prefix_sha256']:
                    matches = False; break
            if matches:
                return True
    return False


def _private_cc_snapshot(path: Path) -> Mapping[str, Any]:
    st = path.lstat()
    if path.is_symlink() or not path.is_file() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ReceiptError("CC attribution boundary must be an owner-only regular file")
    value = json.loads(path.read_bytes())
    validate_cc_session_snapshot(value)
    return value


def _cc_file_positions(session: str, files: list[tuple[str, bytes]],
                       by_entity: Mapping[str, Receipt]) -> dict[str, tuple[str, int, int, Receipt]]:
    """native fact id -> (source_ref, first line start, last line end, receipt)."""
    positions: dict[str, tuple[str, int, int, Receipt]] = {}
    for name, data in files:
        ref = cc_source_ref(session, name)
        cursor = 0
        for line in data.splitlines(keepends=True):
            start = cursor; cursor += len(line)
            if not line.endswith(b"\n"):
                break
            try:
                raw = json.loads(line)
            except ValueError:
                break
            message = raw.get("message") if isinstance(raw, dict) else None
            if raw.get("type") != "assistant" or not isinstance(message, dict):
                continue
            identity = message.get("id") or raw.get("uuid")
            entity = reference("usage", [raw.get("sessionId"), identity])
            if not identity or entity not in by_entity:
                continue
            native = cc_opaque("cc_usage", f"{session}\0{identity}")
            first = positions.get(native)
            if first is not None and first[0] != ref:
                raise ReceiptError("CC native request spans transcript files")
            positions[native] = (ref, first[1] if first else start, cursor, by_entity[entity])
    return positions


def _cc_edge(edge: Mapping[str, Any], files: Mapping[str, bytes]) -> dict[str, int]:
    """Every file recorded at this edge still starts with its recorded prefix."""
    result = {}
    for item in edge["sources"]:
        data = files.get(item["source_ref"], b"")
        size = item["source_bytes"]
        if size > len(data) or sha256(data[:size]).hexdigest() != item["source_prefix_sha256"]:
            raise ReceiptError("CC attribution source prefix no longer matches")
        result[item["source_ref"]] = size
    return result


def bind_cc_boundaries(path: Path, data: bytes, rows: list[Receipt], observed: str, *,
                       host_authorities: Mapping[tuple[str, str], Mapping[str, Any]]) -> list[Receipt]:
    """Join CC session requests to a Relay-bound, byte-bounded CC snapshot.

    与 Pi 相同的证据链：transcript 文件名即原生 session，其哈希必须等于快照
    authority；baseline/final 逐文件前缀摘要（主 transcript 与子代理）必须与
    当前文件一致；每个有界请求必须落在所在文件的已验证前缀区间内且计数一致；
    Host 在派发前冻结的 WI/执行者必须与快照一致。`rows` 为该会话全部文件的
    usage 回执。
    """
    folder = path.parent / "compute-cost"
    if not folder.is_dir():
        return []
    if folder.is_symlink():
        raise ReceiptError("unsafe CC attribution directory")
    session = path.stem
    authority = cc_authority_ref(session)
    by_entity = {r.entity_ref: r for r in rows if r.kind == "usage"}
    try:
        files = [(name, data if name == "main" else item.read_bytes()) for name, item in cc_session_sources(path)]
    except CcUsageError as exc:
        raise ReceiptError(str(exc)) from exc
    by_ref = {cc_source_ref(session, name): raw for name, raw in files}
    positions = _cc_file_positions(session, files, by_entity)
    result: list[Receipt] = []
    for candidate in sorted(folder.glob("wi_*.json")):
        match = SNAPSHOT_NAME.fullmatch(candidate.name)
        if not match or match[3] == "baseline":
            continue
        value = json.loads(candidate.read_bytes())
        if value.get("schema") != CC_SNAPSHOT_SCHEMA or value.get("authority_ref") != authority:
            continue
        snap = _private_cc_snapshot(candidate)
        if snap["work_item_id"] != match[1] or snap["runtime_binding_sha256"] != match[2]:
            raise ReceiptError("CC snapshot filename disagrees with binding")
        assignment = reference("assignment", snap["runtime_binding_sha256"])
        host = host_authorities.get((assignment, reference("native_session", ["cc", session])))
        if host is None or host["wi"] != snap["work_item_id"] or host["executor"] != snap["agent_id"]:
            continue  # 没有 Host 冻结的同一授权，不凭本地文件归属。
        base = _private_cc_snapshot(folder / f"{match[1]}-{match[2]}-baseline.json")
        boundary = snap["attribution_boundary"]
        identity_fields = ("work_item_id", "agent_id", "epoch", "phase", "authority_ref",
                           "runtime_binding_sha256", "execution_surface", "runtime")
        if any(base[k] != snap[k] for k in identity_fields) or boundary["baseline_snapshot_sha256"] != base["snapshot_sha256"]:
            raise ReceiptError("CC snapshot does not descend from its exact baseline")
        if boundary["baseline"] != base["attribution_boundary"]["baseline"]:
            raise ReceiptError("CC baseline position changed")
        starts = _cc_edge(boundary["baseline"], by_ref)
        ends = _cc_edge(boundary["final"], by_ref)
        lower = max(parse_timestamp(base["generated_at"], "baseline"), parse_timestamp(host["not_before"], "assignment"))
        common = {"wi": snap["work_item_id"], "executor": snap["agent_id"], "responsible": None, "delegator": None}
        evidence = tuple(sorted((reference("cc_baseline", base["snapshot_sha256"]), host["evidence"])))
        for part in snap["summary"]["request_partitions"]:
            source = positions.get(part["fact_ref"])
            if source is None or source[0] not in ends or not (
                    starts.get(source[0], 0) <= source[1] and source[2] <= ends[source[0]]):
                raise ReceiptError("CC bounded request missing from verified native prefix")
            row = source[3]; p = row.payload
            expected = dict(zip(TOKEN_FIELDS, (part["native_input"], part["cache_read"], part["cache_write"], part["output"])))
            if any(p["tokens"][k] is not None and p["tokens"][k] != expected[k] for k in TOKEN_FIELDS):
                raise ReceiptError("CC attribution quantities disagree with native request")
            if p["model"] is not None and p["model"] != part["model"]:
                raise ReceiptError("CC attribution model contradicts native request")
            if row.occurred_at is None or parse_timestamp(row.occurred_at, "native time") < lower:
                continue
            result.append(Receipt.make("attribution", [row.entity_ref, snap["runtime_binding_sha256"]],
                {**common, "target_ref": row.entity_ref, "assignment_ref": assignment, "state": "assigned",
                 "policy": "scl_verified_native_boundary.v1", "effective_at": row.occurred_at},
                occurred_at=row.occurred_at, observed_at=observed, producer="scl_native_binding.v1", evidence=evidence))
    return result
