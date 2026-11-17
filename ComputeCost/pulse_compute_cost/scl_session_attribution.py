"""Join native Codex sessions to existing SCL attachments, never to an active-WI guess.

This read-side adapter opens ONLY the explicitly registered active kernel, in
SQLite read-only mode. It does not instantiate SCLKernel (which migrates state),
change authorization, collect tokens, or open retired/legacy fallback databases.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Mapping

from .events import parse_timestamp, utc_text
from .receipts import Receipt, ReceiptError, canonical, reference

POLICY = "scl_host_session_binding.v1"
HOST = re.compile(r"session_v1_[a-f0-9]{64}")
WI = re.compile(r"wi_\d{8}_\d{3}")
AGENT = re.compile(r"[a-z][a-z0-9_-]{0,63}")
MAX_ATTACHMENTS = 20000


def codex_host_ref(native_id: str) -> str:
    """Same domain as harness.scl_protocol.opaque_host_session_ref(adapter='codex')."""
    if not isinstance(native_id, str) or not native_id.strip():
        raise ReceiptError("native Codex session identity is missing")
    return "session_v1_" + hashlib.sha256(b"scl-codex-session-v1\0" + native_id.encode()).hexdigest()


def pi_host_ref(native_id: str) -> str:
    """Same domain as Relay's stable native Pi session identity."""
    if not isinstance(native_id, str) or not native_id.strip():
        raise ReceiptError("native Pi session identity is missing")
    return "session_v1_" + hashlib.sha256(b"pulse-session-v1\0" + native_id.encode()).hexdigest()


def _owned(path: Path, *, directory: bool = False) -> None:
    st = path.lstat()
    expected = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if path.is_symlink() or not expected or st.st_uid != os.getuid():
        raise ReceiptError("unsafe registered SCL source")


def _pointer(root: Path) -> tuple[bytes, dict[str, Any], Path]:
    if not root.is_absolute():
        raise ReceiptError("SCL kernel root must be explicitly absolute")
    _owned(root, directory=True)
    pointer = root / "active-lineage.json"
    _owned(pointer)
    if pointer.stat().st_size > 32768:
        raise ReceiptError("SCL lineage pointer exceeds its bound")
    raw = pointer.read_bytes()
    value = json.loads(raw)
    lineage = value.get("lineage_id")
    if (value.get("schema_version") != "scl_active_lineage/v1"
            or not isinstance(lineage, str) or not re.fullmatch(r"lineage_v1_[a-f0-9]{64}", lineage)
            or type(value.get("generation")) is not int or value["generation"] < 1
            or value.get("kernel_path") != "lineages/" + lineage):
        raise ReceiptError("invalid active SCL lineage")
    _owned(root / "lineages", directory=True)
    active = root / value["kernel_path"]
    _owned(active, directory=True)
    return raw, value, active


@dataclass(frozen=True)
class SessionBinding:
    wi: str
    agent: str
    host_ref: str
    start: datetime
    end: datetime | None
    evidence_ref: str



def read_native_bindings(source: Mapping[str, Any], hosts: set[str]) -> dict[tuple, dict]:
    """Wire existing SCL native receipts into the existing exact-prefix Pi join.

    Do not derive time-only assignments or require a parent's attachment.
    The native join still checks file prefixes, baseline, request partitions,
    model and every Token class; this read supplies only recorded authority.
    """
    from .pi_usage import PI_SNAPSHOT_SCHEMA_V1, validate_pi_session_snapshot
    root = Path(source["scl_kernel_root"])
    surface = source["scl_surface_id"]
    if not isinstance(surface, str) or not re.fullmatch(r"surface_v1_[a-z0-9_]+", surface):
        raise ReceiptError("registered SCL surface is invalid")
    raw_pointer, pointer, active = _pointer(root)
    database = active / "kernel.sqlite3"
    _owned(database)
    result = {}
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            columns = {r[1] for r in connection.execute("PRAGMA table_info(measurement_epochs)")}
            if not {"measurement_epoch_ref", "runtime_class", "status", "created_at"} <= columns:
                return {}  # Older schema retains the existing attachment join.
            rows = connection.execute(
                "SELECT m.*, w.state_json, w.updated_at AS wi_updated_at FROM measurement_epochs m "
                "JOIN work_items w USING(work_item_id) WHERE m.surface_id=? "
                "AND m.runtime_class IN ('pi','resident_harness') LIMIT ?",
                (surface, MAX_ATTACHMENTS + 1)).fetchall()
            if len(rows) > MAX_ATTACHMENTS:
                raise ReceiptError("SCL native measurement inventory exceeds its bound")
            for row in rows:
                host = row["host_session_ref"]
                if host not in hosts or row["status"] not in {"open", "sealed"}:
                    continue
                wi, agent, epoch = row["work_item_id"], row["agent_id"], row["execution_epoch"]
                if (not HOST.fullmatch(str(host)) or not WI.fullmatch(str(wi))
                        or not AGENT.fullmatch(str(agent)) or type(epoch) is not int or epoch < 1):
                    raise ReceiptError("invalid SCL measurement identity")
                raw = row["attribution_json"]
                if hashlib.sha256(raw.encode()).hexdigest() != row["attribution_digest"]:
                    raise ReceiptError("SCL measurement identity digest differs")
                identity = json.loads(raw)
                if isinstance(identity, dict) and identity.get("execution_runtime") in {"cc", "cc_sdk"}:
                    # 本读取只服务 Pi 原生会话连接。CC 的同一 epoch 由 bind_cc_boundaries
                    # （Relay Host 回执）与 scl_cc_usage_measurement/v1 读取侧消费。
                    continue
                expected = {"schema": "work_item_cost_attribution/v1", "work_item_id": wi,
                    "execution_epoch": epoch, "agent_id": agent,
                    "measurement_epoch_ref": row["measurement_epoch_ref"],
                    "boundary_policy": "prospective_exact_lineage_no_backfill"}
                if (not isinstance(identity, dict) or any(identity.get(k) != v for k,v in expected.items())
                        or identity.get("execution_runtime") not in {"pi", row["runtime_class"]}):
                    raise ReceiptError("SCL measurement identity differs")
                if parse_timestamp(identity["started_at"], "start") != parse_timestamp(row["created_at"], "creation"):
                    raise ReceiptError("SCL measurement start differs")
                snapshots = []
                snapshot_bindings = {}
                for native in connection.execute(
                        "SELECT native_json,native_digest FROM measurement_native_facts "
                        "WHERE measurement_epoch_ref=?", (row["measurement_epoch_ref"],)):
                    raw = native["native_json"]
                    if hashlib.sha256(raw.encode()).hexdigest() != native["native_digest"]:
                        raise ReceiptError("SCL native measurement digest differs")
                    snapshot = json.loads(raw)
                    validate_pi_session_snapshot(snapshot)
                    # Valid v1 observations remain readable, but carry no
                    # request boundary. Do not upgrade them or let their
                    # expected missing v2 fields poison neighboring v2 facts.
                    # Validate first: corruption is still a source failure.
                    if snapshot.get("schema") == PI_SNAPSHOT_SCHEMA_V1:
                        continue
                    expected = {"work_item_id": wi, "agent_id": agent, "epoch": epoch, "runtime": "pi",
                        "execution_surface": surface, "authority_ref": host, "phase": identity.get("execution_phase")}
                    if (snapshot.get("schema") != "pi_session_usage_snapshot/v2"
                            or any(snapshot.get(k) != v for k,v in expected.items())):
                        raise ReceiptError("SCL native measurement scope differs")
                    if snapshot["summary"]["event_count"]:
                        snapshots.append(snapshot["snapshot_sha256"])
                        snapshot_bindings[snapshot["snapshot_sha256"]] = snapshot["runtime_binding_sha256"]
                if not snapshots:
                    continue
                state = json.loads(row["state_json"])
                if not isinstance(state, dict) or state.get("work_item_id") != wi:
                    raise ReceiptError("SCL work item identity differs")
                end = identity.get("ended_at")
                if state.get("lifecycle_status") in {"completed", "failed", "cancelled", "superseded", "migrated"}:
                    end = min(filter(None, (end, row["wi_updated_at"])), key=lambda v:parse_timestamp(v,"end"))
                key = (wi, agent, epoch, identity["execution_phase"], "pi", host)
                result[key] = {"runtime": "resident_harness@" + surface,
                    "not_before": identity["started_at"], "not_after": end,
                    "native_snapshots": frozenset(snapshots),
                    "native_binding_snapshots": snapshot_bindings,
                    "evidence_ref": reference("scl_native_measurement", [pointer["lineage_id"],
                        row["measurement_epoch_ref"], row["attribution_digest"], host, surface])}
        finally:
            connection.rollback()
    if (root / "active-lineage.json").read_bytes() != raw_pointer:
        raise ReceiptError("active SCL lineage changed during attribution read")
    return result


def _native_measurement_bindings(connection, source, hosts, lineage, attachments):
    """A recorded native cost boundary need not be the primary attachment.

    A pending Token baseline does not invalidate the already recorded WI and
    native Host identity. This only reads cost-purpose evidence. It does not
    declare execution available, seal quantities, or update SCL state.
    """
    if source.get('kind') != 'codex':
        return []
    columns = {r[1] for r in connection.execute('PRAGMA table_info(measurement_epochs)')}
    required = {'measurement_epoch_ref','work_item_id','execution_epoch',
                'host_session_ref','logical_source_ref','surface_id','runtime_class',
                'agent_id','status','created_at','attribution_json','attribution_digest'}
    if not required <= columns:
        return []  # Historical schemas retain their existing attachment join.
    rows = [dict(r) for r in connection.execute(
        "SELECT m.*,w.state_json,w.updated_at AS wi_updated_at FROM measurement_epochs m "
        "JOIN work_items w USING(work_item_id) WHERE m.surface_id=? "
        "AND m.runtime_class IN ('resident_harness','codex') LIMIT ?",
        (source['scl_surface_id'], MAX_ATTACHMENTS+1))]
    if len(rows) > MAX_ATTACHMENTS:
        raise ReceiptError('SCL native task inventory exceeds its bound')
    future = defaultdict(list)
    for row in rows:
        if type(row['execution_epoch']) is not int or row['execution_epoch'] < 1:
            raise ReceiptError('invalid SCL native task epoch')
        future[row['work_item_id']].append((row['execution_epoch'],parse_timestamp(row['created_at'],'measurement start')))
    result = []
    for row in rows:
        host = row['host_session_ref']
        if (host not in hosts or row['logical_source_ref'] != host
                or row['status'] not in {'baseline_pending','open','sealed'}):
            continue
        wi,agent,epoch = row['work_item_id'],row['agent_id'],row['execution_epoch']
        measure = row['measurement_epoch_ref']
        if (not HOST.fullmatch(str(host)) or not WI.fullmatch(str(wi))
                or not AGENT.fullmatch(str(agent)) or not isinstance(measure,str)
                or not re.fullmatch(r'measurement_epoch_v1_[a-f0-9]{64}',measure)):
            raise ReceiptError('invalid SCL native task identity')
        raw = row['attribution_json']
        if hashlib.sha256(raw.encode()).hexdigest() != row['attribution_digest']:
            raise ReceiptError('SCL native task identity digest differs')
        identity = json.loads(raw)
        expected = {'schema':'work_item_cost_attribution/v1','work_item_id':wi,
                    'execution_epoch':epoch,'agent_id':agent,'measurement_epoch_ref':measure,
                    'boundary_policy':'prospective_exact_lineage_no_backfill'}
        if (not isinstance(identity,dict) or any(identity.get(k)!=v for k,v in expected.items())
                or identity.get('execution_runtime') not in {'codex',row['runtime_class']}):
            raise ReceiptError('SCL native task identity differs')
        start = parse_timestamp(row['created_at'],'measurement creation')
        if parse_timestamp(identity['started_at'],'native task start') != start:
            raise ReceiptError('SCL native task start differs')
        ends = []
        if identity.get('ended_at') is not None:
            ends.append(parse_timestamp(identity['ended_at'],'native task end'))
        # Later recorded epochs and a genuine terminal WI bound the claim.
        # A different primary attachment never lends its own start to this Host.
        ends.extend(at for number,at in future[wi] if number>epoch and at>=start)
        for attachment in attachments:
            if attachment['work_item_id'] != wi:
                continue
            at = parse_timestamp(attachment['created_at'],'attachment start')
            if attachment['epoch']>epoch and at>=start:
                ends.append(at)
            if (attachment['epoch']==epoch and attachment['host_session_ref']==host
                    and attachment.get('superseded_at')):
                ends.append(parse_timestamp(attachment['superseded_at'],'attachment end'))
        state=json.loads(row['state_json'])
        if not isinstance(state,dict) or state.get('work_item_id') != wi:
            raise ReceiptError('SCL native task WI differs')
        if state.get('lifecycle_status') in {'completed','failed','cancelled','superseded','migrated'}:
            ends.append(parse_timestamp(row['wi_updated_at'],'terminal WI'))
        end=min(ends) if ends else None
        if end is not None and end<start:
            continue  # No interval survives a prior terminal boundary.
        evidence=reference('scl_native_task_measurement',
            [lineage,measure,row['attribution_digest'],host,row['surface_id']])
        result.append(SessionBinding(wi,agent,host,start,end,evidence))
    return result


def read_bindings(source: Mapping[str, Any], hosts: set[str]) -> list[SessionBinding]:
    """Read attachment/history bounds in ONE database snapshot.

    Historical Agent identity comes from that epoch's measurement or checkpoint,
    not today's owner. An open measurement is sufficient identity evidence: a
    token seal/accepted Outcome is neither required nor manufactured here.
    """
    root = Path(source["scl_kernel_root"])
    surface = source["scl_surface_id"]
    if not isinstance(surface, str) or not re.fullmatch(r"surface_v1_[a-z0-9_]+", surface):
        raise ReceiptError("registered SCL surface is invalid")
    raw_pointer, pointer, active = _pointer(root)
    database = active / "kernel.sqlite3"
    _owned(database)
    result: list[SessionBinding] = []
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            # Bounded reads; no schema creation, migrations, WAL mode changes or writes.
            rows = [dict(r) for r in connection.execute(
                "SELECT work_item_id, epoch, logical_source_ref, host_session_ref, surface_id, "
                "rationale_digest, operation_id, created_at, NULL AS superseded_at FROM source_attachments UNION ALL "
                "SELECT work_item_id, epoch, logical_source_ref, host_session_ref, surface_id, "
                "rationale_digest, operation_id, created_at, superseded_at "
                "FROM source_attachment_history LIMIT ?", (MAX_ATTACHMENTS + 1,))]
            if len(rows) > MAX_ATTACHMENTS:
                raise ReceiptError("SCL attachment inventory exceeds its bound")
            next_epochs: dict[str, list[tuple[int, datetime]]] = defaultdict(list)
            for row in rows:
                if not WI.fullmatch(str(row["work_item_id"])) or type(row["epoch"]) is not int or row["epoch"] < 1:
                    raise ReceiptError("invalid SCL attachment identity")
                next_epochs[row["work_item_id"]].append((row["epoch"], parse_timestamp(row["created_at"], "attachment start")))
            agent_cache: dict[tuple[str, int, str, str], str | None] = {}
            state_cache: dict[str, tuple[dict, str]] = {}
            for row in rows:
                if row["host_session_ref"] not in hosts or row["surface_id"] != surface:
                    continue
                wi, epoch, host = row["work_item_id"], row["epoch"], row["host_session_ref"]
                if not HOST.fullmatch(host) or not HOST.fullmatch(row["logical_source_ref"]):
                    raise ReceiptError("invalid SCL session reference")
                start = parse_timestamp(row["created_at"], "attachment start")
                ends = [at for number, at in next_epochs[wi] if number > epoch and at >= start]
                if row["superseded_at"]:
                    ends.append(parse_timestamp(row["superseded_at"], "attachment superseded"))
                if wi not in state_cache:
                    state = connection.execute("SELECT state_json, updated_at FROM work_items WHERE work_item_id = ?", (wi,)).fetchone()
                    if state is None:
                        raise ReceiptError("attachment has no existing work item")
                    state_cache[wi] = (json.loads(state["state_json"]), state["updated_at"])
                state, updated = state_cache[wi]
                if state.get("work_item_id") != wi:
                    raise ReceiptError("SCL work item identity differs")
                if state.get("lifecycle_status") in {"completed", "failed", "cancelled", "superseded", "migrated"}:
                    ends.append(parse_timestamp(updated, "terminal work item"))
                key = (wi, epoch, host, row["logical_source_ref"])
                if key not in agent_cache:
                    agents = set()
                    for measurement in connection.execute(
                        "SELECT * FROM measurement_epochs "
                        "WHERE work_item_id = ? AND execution_epoch = ? AND host_session_ref = ? AND surface_id = ?",
                        (wi, epoch, host, surface)):
                        # A scoped measurement must not lend its Agent to an
                        # unrelated parent attachment and backfill the warmup.
                        if ("logical_source_ref" in measurement.keys()
                                and measurement["logical_source_ref"] != row["logical_source_ref"]):
                            continue
                        text = measurement["attribution_json"]
                        if hashlib.sha256(text.encode()).hexdigest() != measurement["attribution_digest"]:
                            raise ReceiptError("SCL measurement identity digest differs")
                        a = json.loads(text)
                        if (a.get("work_item_id") != wi or a.get("execution_epoch") != epoch
                                or a.get("agent_id") != measurement["agent_id"]):
                            raise ReceiptError("SCL measurement identity differs")
                        agents.add(measurement["agent_id"])
                    # The checkpoint is already sealed identity evidence, not a Token seal.
                    checkpoint = connection.execute(
                        "SELECT content_json, content_digest FROM checkpoints WHERE work_item_id = ? "
                        "AND epoch = ? ORDER BY revision DESC LIMIT 1", (wi, epoch)).fetchone()
                    if not agents and checkpoint is not None:
                        text = checkpoint["content_json"]
                        if hashlib.sha256(text.encode()).hexdigest() != checkpoint["content_digest"]:
                            raise ReceiptError("SCL checkpoint identity digest differs")
                        cp = json.loads(text)
                        if cp.get("work_item_id") != wi or cp.get("epoch") != epoch:
                            raise ReceiptError("SCL checkpoint identity differs")
                        if cp.get("surface_id") == surface and cp.get("agent_id"):
                            agents.add(cp["agent_id"])
                    # A current attachment can be identified before the first checkpoint.
                    if (not agents and state.get("epoch") == epoch and state.get("surface_id") == surface
                            and state.get("session_ref") == row["logical_source_ref"] and state.get("active_executor")):
                        agents.add(state["active_executor"])
                    if any(not isinstance(a, str) or not AGENT.fullmatch(a) for a in agents) or len(agents) > 1:
                        raise ReceiptError("conflicting SCL executor evidence")
                    agent_cache[key] = next(iter(agents)) if agents else None
                agent = agent_cache[key]
                if agent is None:
                    continue
                end = min(ends) if ends else None
                if end is not None and end < start:
                    raise ReceiptError("SCL attachment interval is reversed")
                # Stable origin evidence: later checkpoints/end bounds do not create
                # competing attribution heads for already-settled native quantities.
                evidence = reference("scl_source_attachment", [pointer["lineage_id"], wi, epoch,
                    host, row["logical_source_ref"], surface, row["operation_id"], row["created_at"], agent])
                result.append(SessionBinding(wi, agent, host, start, end, evidence))
            result.extend(_native_measurement_bindings(connection, source, hosts, pointer["lineage_id"], rows))
        finally:
            connection.rollback()
    if (root / "active-lineage.json").read_bytes() != raw_pointer:
        raise ReceiptError("active SCL lineage changed during attribution read")
    return list(set(result))


def attribution_receipts(rows: list[Receipt], hosts_by_usage: Mapping[str, str],
                         source: Mapping[str, Any], observed: str) -> list[Receipt]:
    bindings = read_bindings(source, set(hosts_by_usage.values()))
    by_host: dict[str, list[SessionBinding]] = defaultdict(list)
    for binding in bindings:
        by_host[binding.host_ref].append(binding)
    result = []
    for usage in rows:
        if usage.kind != "usage" or usage.entity_ref not in hosts_by_usage:
            continue
        p = usage.payload
        first, last = p.get("interval_start"), p.get("interval_end")
        # A native Pi request is a point observation; a cumulative Codex
        # aggregate still requires both independently observed bounds.
        if first is None and last is None and p.get("granularity") == "request":
            first = last = usage.occurred_at
        if first is None or last is None:
            continue
        start, end = parse_timestamp(first, "usage start"), parse_timestamp(last, "usage end")
        candidates = [b for b in by_host[hosts_by_usage[usage.entity_ref]]
                      if (b.end is None or start < b.end) and (b.start < end or start == end == b.start)]
        if not candidates:
            continue
        identities = {(b.wi, b.agent) for b in candidates}
        # Epoch rollover does not change the WI. Adjacent same-identity
        # intervals may jointly prove an aggregate, but a gap may not.
        cursor = start
        covered = False
        for binding in sorted(candidates, key=lambda b: b.start):
            if binding.start > cursor:
                break
            if binding.end is None or binding.end >= end:
                covered = True
                break
            cursor = max(cursor, binding.end)
        assigned = covered and len(identities) == 1
        wi, agent = next(iter(identities)) if assigned else (None, None)
        result.append(Receipt.make("attribution", [POLICY, usage.entity_ref], {
            "target_ref": usage.entity_ref, "wi": wi, "executor": agent,
            "responsible": None, "delegator": None,
            "state": "assigned" if assigned else "ambiguous", "policy": POLICY,
            "effective_at": first,
        }, occurred_at=first, observed_at=observed, producer=POLICY,
            evidence=tuple(sorted({b.evidence_ref for b in candidates}))))
    return result
