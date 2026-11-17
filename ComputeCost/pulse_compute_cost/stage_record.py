"""One bounded native observation and its declarations in UsageLedger.

No source paths, counters, principal or timestamps are accepted in the business
command. The native adapter supplies them in process; this module grants no WI
rights and never persists the transcript. Original collectors share usage IDs.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re

from .events import parse_timestamp, utc_text
from .receipt_sources import read_pi, reconcile_native_usage
from .receipts import Receipt, ReceiptError, canonical, digest, reference, _ref
from .stage_facts import STAGE_FIELDS, SEMANTIC_KINDS, LINKABLE_KINDS, _text

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_ACTIVITIES = 256


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ReceiptError("stage input fields do not match the closed contract")


def validate_range(value):
    _fields(value, {"start_byte", "end_byte"})
    start, end = value["start_byte"], value["end_byte"]
    if (type(start) is not int or type(end) is not int
            or not 0 <= start < end or end - start > MAX_SOURCE_BYTES):
        raise ReceiptError("stage source requires explicit bounded byte boundaries")
    return start, end


class StageSourceError(ReceiptError):
    """A refused bounded source with a fixed, content-free cause.

    Causes name the fix (for example header_identity_mismatch or
    incomplete_tail); messages never carry native bytes, ids or paths.
    """

    def __init__(self, cause: str, message: str):
        super().__init__(f"{message} (reason={cause})")
        self.cause = cause


def session_identity(header):
    """Pi session identity is (type, id); version, timestamp, cwd only describe it."""
    if not isinstance(header, dict):
        return None
    kind, native_id = header.get("type"), header.get("id")
    if kind != "session" or not isinstance(native_id, str) or not native_id:
        return None
    return kind, native_id


def same_session(expected, observed) -> bool:
    identity = session_identity(expected)
    return identity is not None and identity == session_identity(observed)


def source_projection(path: Path, header, raw: bytes, *, start: int, end: int,
                      principal, observed_at: str):
    """Trusted-adapter helper. Raw bytes are used transiently, not returned."""
    validate_range({"start_byte": start, "end_byte": end})
    if len(raw) != end - start or not raw.endswith(b"\n"):
        raise StageSourceError("incomplete_tail", "stage source is truncated; retain its original boundaries")
    parse_timestamp(observed_at, "observation")
    if session_identity(header) is None:
        raise StageSourceError("header_identity_mismatch", "Host session identity is unavailable")
    cause = "record_invalid"
    try:
        rows = [json.loads(line) for line in raw.splitlines()]
        if not all(isinstance(r, dict) for r in rows):
            raise ValueError("record")
        if start == 0:
            cause = "header_identity_mismatch"
            if not rows or not same_session(header, rows[0]):
                raise ValueError("header")
            rows = rows[1:]
        elif any(r.get("type") == "session" for r in rows):
            cause = "session_header_in_range"
            raise ValueError("second session")
        cause = "activity_invalid"
        activities, ids = [], set()
        for row in rows:
            kind = row.get("type")
            role = row.get("message", {}).get("role") if kind == "message" else kind
            if role not in {"user", "assistant", "toolResult", "compaction", "branch_summary"}:
                continue
            native_id = row.get("id")
            if not isinstance(native_id, str) or not native_id or native_id in ids:
                raise ValueError("native activity identity")
            ids.add(native_id)
            at = utc_text(parse_timestamp(row.get("timestamp"), "native activity timestamp"))
            activities.append({"index": len(activities), "role": role, "occurred_at": at,
                "activity_ref": reference("native_activity", ["pi", header["id"], native_id, role]),
                "evidence_ref": reference("native_record", digest(row))})
        if not 1 <= len(activities) <= MAX_ACTIVITIES:
            cause = "activity_count_out_of_bounds"
            raise ValueError("activity range")
    except (ValueError, TypeError, AttributeError, IndexError) as exc:
        raise StageSourceError(cause, "invalid bounded native activity source") from exc
    # Use the original Pi parser, not an independently aggregated SCL counter.
    # A mid-session range cannot infer missing inherited model context.
    data = canonical(header) + b"\n" + b"".join(canonical(r) + b"\n" for r in rows)
    usage = read_pi(path, observed_at, data=data)
    return {"range": {"start_byte": start, "end_byte": end}, "activities": activities,
        "usage": usage, "observed_at": observed_at,
        "principal_ref": reference("stage_principal", principal),
        "source_ref": reference("native_stage_range", [header["id"], start, end, sha256(raw).hexdigest()]),
        "context_ref": reference("stage_context", activities[0]["activity_ref"])}


def source_summary(source):
    return {"schema": "computecost_stage_source/v1", "range": source["range"],
        "context_ref": source["context_ref"], "activities": source["activities"],
        "usage_refs": [r.entity_ref for r in source["usage"]],
        "source_ref": source["source_ref"], "coverage": "bounded_source_only",
        "read_only": True, "execution_authority": False}


def record_stage(ledger, source, command):
    """Atomic declaration save. Retry exactly; the existing ledger deduplicates."""
    _fields(command, {"source", "context_ref", "changes"})
    validate_range(command["source"])
    if command["source"] != source["range"]:
        raise ReceiptError("stage command differs from captured source boundary")
    changes = command["changes"]
    if not isinstance(changes, list) or len(changes) > 64:
        raise ReceiptError("stage changes must be a bounded list")
    context = command["context_ref"]
    _ref(context, "context_ref")
    snapshot = ledger.receipt_snapshot()
    original = snapshot["facts"]
    if context is not None and not any(r.kind in STAGE_FIELDS and r.payload["context_ref"] == context for r in original):
        raise ReceiptError("stage context must refer to an existing saved declaration")
    context = context or source["context_ref"]
    known = {r.fact_ref: r for r in original}
    targets = {r.entity_ref: r for r in [*original, *source["usage"]]}
    producer = "stage_host.v1/" + source["principal_ref"].removeprefix("ccf1_")
    pending = reconcile_native_usage(original, source["usage"])
    results, keys = [], set()
    for change in changes:
        _fields(change, {"key", "kind", "at", "activities", "payload", "supersedes"})
        key, kind, at, indices = (change[k] for k in ("key", "kind", "at", "activities"))
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", key)
                or key in keys or not isinstance(kind, str) or kind not in STAGE_FIELDS):
            raise ReceiptError("stage change needs a unique bounded key and supported kind")
        _text(key, "key", 64)
        keys.add(key)
        if (type(at) is not int or not isinstance(indices, list) or not 1 <= len(indices) <= MAX_ACTIVITIES
                or any(type(i) is not int or not 0 <= i < len(source["activities"]) for i in indices)
                or len(set(indices)) != len(indices) or at not in indices):
            raise ReceiptError("stage claim must select actual captured activities and timestamp")
        activities = [source["activities"][i] for i in indices]
        reserved = {"context_ref", "epistemic"} | ({"activity_refs", "wi"} if kind in SEMANTIC_KINDS else set())
        _fields(change["payload"], STAGE_FIELDS[kind] - reserved)
        payload = {**change["payload"], "context_ref": context, "epistemic": "agent_judgment"}
        if kind in SEMANTIC_KINDS:
            payload.update(wi=None, activity_refs=[a["activity_ref"] for a in activities])
        entity = reference(kind, [context, source["principal_ref"], key])
        predecessor = change["supersedes"]
        _ref(predecessor, "supersedes")
        if predecessor is not None:
            prior = known.get(predecessor)
            if prior is None or prior.kind != kind or prior.payload["context_ref"] != context:
                raise ReceiptError("stage correction requires the exact same-context predecessor")
            # A successor can correct a declaration, but the new fact retains
            # its own authenticated author; it cannot impersonate the old one.
            entity = prior.entity_ref
        row = Receipt.make(kind, None, payload, entity_ref=entity,
            occurred_at=source["activities"][at]["occurred_at"], observed_at=source["observed_at"],
            producer=producer, supersedes=predecessor,
            evidence=(source["principal_ref"], source["source_ref"], *(a["evidence_ref"] for a in activities)))
        if kind == "stage_link":
            for target_ref in payload["target_refs"]:
                target = targets.get(target_ref)
                if (target is None or target.kind not in LINKABLE_KINDS
                        or (target.kind == "usage" and target.payload["granularity"] != "request")
                        or (target.kind in SEMANTIC_KINDS and target.payload["context_ref"] != context)):
                    raise ReceiptError("stage link requires an existing bounded compatible fact")
        pending.append(row)
        targets[row.entity_ref] = row
        results.append({"key": key, "kind": kind, "entity_ref": row.entity_ref, "fact_ref": row.fact_ref})
    if not pending and not source["usage"]:
        raise ReceiptError("stage save has no declaration or native usage to preserve")
    saved = ledger.ingest_receipts(pending, expected_watermark=snapshot["watermark"])
    return {"schema": "computecost_stage_record/v1", "status": "saved", **saved,
        "context_ref": context, "changes": results, "source": source_summary(source),
        "coverage": "bounded_source_only", "execution_authority": False,
        "consumer_state": "saved_not_display_verified"}
