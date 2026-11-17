"""Lossless historical views and compatible projections of the same ledger."""

from __future__ import annotations

import base64
from copy import deepcopy
from datetime import date, datetime
import hashlib
from pathlib import PurePosixPath
import re
from typing import Any

import yaml  # type: ignore[import-untyped]  # Existing canonical PyYAML; decoded data is validated below.

from .model import Json, Rejected, digest, encoded

TERMINAL = {"completed", "failed", "cancelled", "superseded", "migrated"}
STATES = TERMINAL | {"draft", "ready", "in_progress", "blocked", "awaiting_verification"}


def normalize(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def decode(content: bytes, *, many: bool = False) -> Any:
    try:
        return normalize(list(yaml.safe_load_all(content)) if many else yaml.safe_load(content))
    except (ValueError, yaml.YAMLError, RecursionError) as exc:
        raise Rejected("ledger_invalid", "A production record cannot be decoded") from exc


def related_document_index(tree: Json, identities: set[str]) -> dict[str, Json]:
    """Index one freshly observed Git tree, not one full scan per WorkItem.

    Preserve the original exact path-component / basename-prefix semantics.
    Nothing is cached across transactions or used as a second authority.
    """
    result: dict[str, Json] = {wi: {} for wi in identities}
    for path, sha in tree.items():
        parts = PurePosixPath(path).parts
        matches = identities.intersection(parts)
        basename = parts[-1].split(".", 1)[0]
        if basename in identities:
            matches.add(basename)
        for wi in matches:
            result[wi][path] = sha
    return result


def seed(
    namespace: str, wi: str, revision: str, documents: dict[str, bytes], related_documents: Json
) -> tuple[list[Json], dict[str, str]]:
    if not re.fullmatch(r"wi_[0-9]{8}_[0-9]{3,}", wi):
        raise Rejected("ledger_invalid", "Historical WI identity differs from its path")
    record = decode(documents[f".harness/work-items/{wi}.yaml"])
    if not isinstance(record, dict) or record.get("work_item_id") != wi:
        raise Rejected("ledger_invalid", "Historical definition identity differs")
    if not isinstance(record.get("title"), str) or not isinstance(record.get("owner"), str):
        raise Rejected(
            "ledger_invalid", "Historical definition lacks its responsible owner or title"
        )
    if record.get("status") not in STATES:
        raise Rejected("ledger_invalid", "Unknown historical business status")
    hashes = {p: hashlib.sha256(b).hexdigest() for p, b in documents.items()}
    originals = {hashes[p]: base64.b64encode(b).decode() for p, b in documents.items()}
    admission_path = f".harness/admissions/{wi}.yaml"
    event_path = f".harness/events/{wi}.events.yaml"
    payload = {
        "record": record,
        "source_revision": revision,
        "originals": hashes,
        "affiliation": decode(documents[admission_path]) if admission_path in documents else None,
        "history": decode(documents[event_path], many=True) if event_path in documents else [],
        "related_documents": dict(related_documents),
    }
    semantics = {
        "intent": "observe_existing_production_facts",
        "principal": None,
        "namespace": namespace,
        "work_item_id": wi,
        "originals": hashes,
    }
    request = "baseline." + digest(semantics)
    entry = {
        "namespace": namespace,
        "wi": wi,
        "revision": 1,
        "request_id": request,
        "semantic_digest": digest(semantics),
        "semantics": semantics,
        "originals": list(originals),
        "events": [{"kind": "ProductionHistoryObserved", "payload": payload, "sequence": 1}],
        "result": {"historical_observation": True, "execution_authority": False},
        "previous": "0" * 64,
        "at": record.get("updated_at", record.get("created_at")),
    }
    return [entry], originals


def restore(payload: Json) -> Json:
    from .domain import initial_state

    record = payload["record"]
    state = initial_state(
        {
            "why": record.get("source") or record["title"],
            "what": record["title"],
            "acceptance": deepcopy(record.get("acceptance_criteria", [])),
            "owner": record["owner"],
            "resources": [],
        },
        "standard",
    )
    status = record["status"]
    state.update(
        production=deepcopy(payload),
        affiliation=deepcopy(payload["affiliation"]),
        historical_progress=status,
        created_at=record.get("created_at"),
    )
    # Observing an old executor or approval never impersonates its native Session.
    # Original execution/candidate documents remain addressable and unchanged.
    if status == "completed":
        state.update(
            status="completed",
            terminal={"historical": True, "resources_retired": False, "original_status": status},
        )
    elif status in TERMINAL:
        state.update(
            status="terminated",
            terminal={
                "historical": True,
                "resources_retired": False,
                "termination": status,
                "original_status": status,
            },
        )
    elif status != "draft":
        state.update(status="active", historical_execution=True)
    return state


def project(wi: str, state: Json, history: list[Json], prior: dict[str, bytes]) -> dict[str, bytes]:
    definition_path, event_path = (
        f".harness/work-items/{wi}.yaml",
        f".harness/events/{wi}.events.yaml",
    )
    record = deepcopy(state.get("production", {}).get("record", {}))
    original = deepcopy(record)
    at = history[-1]["at"]
    if not record:
        record = {
            "work_item_id": wi,
            "created_at": state.get("created_at") or history[0]["at"],
            "updated_at": at,
            "acceptance_criteria": [],
            "artifact_refs": [],
            "active_executor": None,
            "main_only": True,
            "cross_tenant": False,
            "blocked_reason": None,
            "failure_reason": None,
            "source": state["definition"]["why"],
            "disposition_reason": None,
            "disposition_reason_code": None,
            "migrated_to": None,
            "superseded_by": None,
        }
    record.update(
        title=state.get("title", state["definition"]["what"]),
        owner=state["definition"]["owner"],
        acceptance_criteria=state["definition"]["acceptance"],
    )
    if state.get("historical_progress") is None:
        status = state["status"]
        record["status"] = {
            "draft": "draft",
            "active": "in_progress",
            "completed": "completed",
            "stopped": "blocked",
            "transferring": "blocked",
            "terminated": (state.get("terminal") or {}).get("termination", "cancelled"),
        }[status]
        if state["status"] == "active" and state.get("active_candidate") is not None:
            record["status"] = "awaiting_verification"
            record.update(
                outcome_claim=state["definition"]["what"],
                outcome_change_type=record.get("outcome_change_type") or "unknown",
                outcome_producer=state["definition"]["owner"],
                outcome_acceptance_fingerprint="sha256:"
                + hashlib.sha256(
                    encoded([c.strip() for c in state["definition"]["acceptance"]])
                ).hexdigest(),
            )
        record["active_executor"] = (
            (state.get("holder") or {}).get("actor") if record["status"] == "in_progress" else None
        )
        record["blocked_reason"] = (
            "Native work stopped or transferring" if record["status"] == "blocked" else None
        )
        record["failure_reason"] = (
            (state.get("terminal") or {}).get("reason") if record["status"] == "failed" else None
        )
        if record["status"] in {"cancelled", "superseded"}:
            record["disposition_reason"] = state["terminal"]["reason"]
            record["disposition_reason_code"] = "native_" + record["status"]
            record["source_epoch"] = state["terminal"].get("business_epoch") or record.get(
                "source_epoch"
            )
            record["target_epoch"] = record["source_epoch"]
            record["superseded_by"] = state["terminal"].get("successor")
        if record["status"] not in {"cancelled", "superseded", "migrated"}:
            for field in (
                "source_epoch",
                "target_epoch",
                "disposition_reason",
                "disposition_reason_code",
                "superseded_by",
                "migrated_to",
            ):
                record[field] = None
    if record != original:
        record["updated_at"] = at
    result = dict(prior)
    result[definition_path] = (
        prior[definition_path]
        if record == original and definition_path in prior
        else encoded(record) + b"\n"
    )
    # Legacy bytes remain exact. Native history has one authoritative journal;
    # this compatibility file carries only its current watermark, never a second
    # ever-growing copy of every event or captured payload.
    existing_events = prior.get(event_path, b"")
    latest = history[-1] if history else None
    if latest is not None and latest["semantics"].get("intent") != "observe_existing_production_facts":
        marker = {
            "event_id": "native_" + digest([wi, latest["request_id"]])[:24],
            "event_type": "work_item.native_journal_observed",
            "work_item_id": wi,
            "timestamp": latest["at"],
            "operator": latest["semantics"]["principal"]["actor"],
            "payload": {"native_revision": latest["revision"], "native_head": digest(latest),
                        "journal": f".harness/core/work-items/{wi}.json"},
        }
        result[event_path] = existing_events + (
            b"\n" if existing_events and not existing_events.endswith(b"\n") else b""
        ) + b"---\n" + encoded(marker) + b"\n"
    if state.get("affiliation"):
        path = f".harness/admissions/{wi}.yaml"
        if path not in prior or decode(prior[path]) != state["affiliation"]:
            result[path] = encoded(state["affiliation"]) + b"\n"
    return result
