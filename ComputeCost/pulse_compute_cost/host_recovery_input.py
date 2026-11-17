"""Read the existing durable recovery dispatch edge; never manufacture one."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from .events import parse_timestamp
from .pi_context import recovery_directories, load_dispatched_recovery, input_bytes
from .receipts import ReceiptError, digest, reference


def host_recovery_inputs(source, raw_records, rows, observed):
    if source.get("host_native_input") is not True:
        return []
    from .receipt_sources import actor_receipt
    dispatches = defaultdict(set)
    for raw in raw_records:
        if raw.get("kind") == "execution" and raw.get("dispatch_ref") and raw.get("attempt_ref"):
            dispatches[raw["dispatch_ref"]].add(raw["attempt_ref"])
    executions = {r.entity_ref:r for r in rows if r.kind == "execution" and r.producer == "relay_host.v1"}
    request_ids = set()
    for folder in recovery_directories(source):
        if not folder.exists() and not folder.is_symlink():
            continue
        st = folder.lstat()
        if folder.is_symlink() or not folder.is_dir() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ReceiptError("registered Host recovery directory is unsafe")
        request_ids.update(p.stem for p in folder.glob("operation_v1_feishu_recovery_*.json"))
    result = []
    for request_id in sorted(request_ids):
        record = load_dispatched_recovery(source, request_id)
        if record is None:
            continue
        # sendIdempotent records precisely this correlation and frozen session.
        dispatch = reference("dispatch", ["pi", "turn:entry:"+request_id, record["sessionId"]])
        candidates = dispatches.get(dispatch, set())
        if len(candidates) != 1:
            continue
        execution = executions.get(next(iter(candidates)))
        if execution is None:
            continue
        p = execution.payload
        if p["runtime"] != "pi" or p.get("wi") != record["workItemId"] or not execution.occurred_at:
            continue
        data = input_bytes(source, record["humanInputRef"])
        if data is None:
            continue
        original = json.loads(data)
        origin = original.get("source") or {}
        values = [origin.get("channel"), origin.get("eventId"), origin.get("humanRef"), original.get("poolKey")]
        if (values[0] != "feishu" or values[3] != record["poolKey"]
                or any(not isinstance(v, str) or not v for v in values)
                or hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(",", ":")).encode()).hexdigest() != record["humanInputRef"]):
            raise ReceiptError("recovery input identity does not match its original metadata")
        ms = original.get("observedAtMs")
        if type(ms) is not int or ms <= 0:
            raise ReceiptError("recovery input timestamp is invalid")
        if datetime.fromtimestamp(ms/1000, timezone.utc) > parse_timestamp(execution.occurred_at,"dispatch"):
            continue
        evidence = reference("input_metadata",hashlib.sha256(data).hexdigest())
        result.append(actor_receipt(execution,node=source.get("node"),executor=p.get("executor"),
            entrypoint="feishu",observed=observed,input_ref=evidence,path=["host_recovery_input",source.get("id")],
            evidence=(evidence, reference("host_recovery_journal",[request_id,digest(record)]), dispatch)))
    return result
