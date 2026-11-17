"""Immutable observations and exports; never a second task/completion authority."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Callable

from . import native_usage, review
from .model import (
    Decision,
    HostContext,
    Json,
    Rejected,
    digest,
    fields,
    human_decision,
    identity,
)

INTENTS = frozenset(
    {"open_usage_interval", "record_usage", "record_attention", "record_quality"}
)


def saved_usage_sources(state: Json, history: list[Json], ref: Json,
                        read_original: Callable[[str], bytes]) -> list[Json]:
    """Private artifact projection only; no new event, identity or grant.

    The exact interval-opening Host source already preserves the native header.
    Never search another interval/session or infer it from a current actor.
    """
    result = []
    for record in state.get("measurements", []):
        if record["kind"] != "usage" or record["evidence"] != ref:
            continue
        proof = {"schema": "harness.saved-usage-source/v1",
                 "record_id": record["record_id"], "status": "unavailable",
                 "reason": "original_interval_header_unavailable"}
        try:
            opening, = [entry for entry in history if any(
                e["kind"] == "UsageIntervalOpened"
                and e["payload"]["interval"]["interval_id"] == record["interval_id"]
                for e in entry["events"])]
            semantics = opening["semantics"]
            source = semantics["source"]
            principal = record["principal"]
            command = {"intent": "open_usage_interval", "work_item_id": opening["wi"],
                       "input": {"task_attempt": record["task_attempt"]}}
            if (source is None or source["kind"] != "native_agent"
                    or semantics["principal"] != principal
                    or any(semantics["command"].get(k) != v for k, v in command.items())):
                raise ValueError("source")
            raw = read_original(source["sha256"])
            if hashlib.sha256(raw).hexdigest() != source["sha256"]:
                raise ValueError("digest")
            document = json.loads(raw)
            header = document["native_header"]
            if (document["command"] != command or header.get("type") != "session"
                    or not isinstance(header.get("id"), str) or not header["id"]
                    or principal["session"] != "session." + digest([
                        semantics["namespace"], principal["actor"], header["id"], principal["surface"]])):
                raise ValueError("identity")
            proof = {"schema": "harness.saved-usage-source/v1", "status": "verified",
                     "record_id": record["record_id"], "interval_id": record["interval_id"],
                     "session": "measurement-session." + digest(principal),
                     "actor": principal["actor"], "native_header": header,
                     "source_sha256": record["source_sha256"],
                     "interval_source_sha256": source["sha256"]}
        except (KeyError, TypeError, ValueError, AttributeError):
            # Older/unsupported sources stay readable as originals, but do not
            # acquire a fabricated identity proof for a cross-session importer.
            pass
        result.append(proof)
    return result


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def executor(state: Json, context: HostContext, attempt_id: str | None) -> None:
    if state["generation"] != context.generation:
        raise Rejected(
            "authority_changed", "Observation requires current native authority"
        )
    attempt = None
    if attempt_id is not None:
        identity(attempt_id)
        attempt = state["attempts"].get(attempt_id)
        if attempt is None or attempt["principal"] != context.principal():
            raise Rejected(
                "authority_denied", "Usage belongs only to this actual task executor"
            )
    if (
        state["status"] in {"completed", "terminated"}
        and state["definition"]["owner"] == context.actor
    ):
        return
    if state["status"] == "active":
        if state["holder"] == context.principal():
            return
        if attempt is not None:
            grant = state["grants"].get(attempt["grant_id"])
            if (
                grant
                and grant["active"]
                and grant["principal"] == context.principal()
                and grant["generation"] == context.generation
            ):
                return
    raise Rejected("authority_denied", "Observation does not confer an execution grant")


def decide(
    state: Json,
    intent: str,
    data: Json,
    context: HostContext,
    operation: str,
    wi: str,
    at: str,
) -> Decision:
    records = state.get("measurements", [])
    if intent == "open_usage_interval":
        fields(data, {"task_attempt"})
        executor(state, context, data["task_attempt"])
        if type(context.native_cursor) is not int or context.native_cursor < 0:
            raise Rejected(
                "source_unavailable",
                "Only the native Host can locate an actual interval boundary",
            )
        if (
            data["task_attempt"] is not None
            and state["attempts"][data["task_attempt"]]["status"] != "running"
        ):
            raise Rejected(
                "domain_conflict", "An ended task cannot open a new execution interval"
            )
        if any(
            i["principal"] == context.principal()
            and i["task_attempt"] == data["task_attempt"]
            and not i.get("closed", False)
            for i in state.get("usage_intervals", {}).values()
        ):
            raise Rejected(
                "domain_conflict", "The original native interval is already open"
            )
        interval = {
            "interval_id": operation,
            "principal": context.principal(),
            "task_attempt": data["task_attempt"],
            "start_byte": context.native_cursor,
            "end_byte": None,
            "closed": False,
            "end_boundary": "open",
            "phase": state["status"],
            "generation": state["generation"],
        }
        return Decision(
            [event("UsageIntervalOpened", interval=interval)],
            {"interval_id": operation, "start_byte": context.native_cursor},
        )
    if intent == "record_usage":
        fields(data, {"interval_id", "task_attempt", "start_byte", "end_byte"})
        executor(state, context, data["task_attempt"])
        identity(data["interval_id"])
        interval = state.get("usage_intervals", {}).get(data["interval_id"])
        if (
            interval is None
            or interval["principal"] != context.principal()
            or interval["task_attempt"] != data["task_attempt"]
        ):
            raise Rejected(
                "authority_denied",
                "Usage needs its original native interval and task attribution",
            )
        packet, source = context.usage_snapshot, context.source
        if (
            packet is None
            or source is None
            or source.kind != "native_usage"
            or source.approved_intent != intent
            or source.approved_input_digest
            != digest({"intent": intent, "work_item_id": wi, "input": data})
        ):
            raise Rejected(
                "source_invalid", "Usage requires the Host's original native capture"
            )
        fields(
            packet,
            {
                "schema",
                "runtime",
                "start_byte",
                "end_byte",
                "source_sha256",
                "evidence",
                "groups",
                "coverage",
                "extraction",
            },
        )
        start, end = data["start_byte"], data["end_byte"]
        if (
            any(type(v) is not int for v in (start, end))
            or start < 0
            or end <= start
            or end - start > native_usage.MAX_RANGE
        ):
            raise Rejected("invalid_input", "Usage range is invalid")
        if (
            interval.get("closed", False)
            and interval["end_byte"] is None
            or start < interval["start_byte"]
            or interval["end_byte"] is not None
            and end > interval["end_byte"]
        ):
            raise Rejected(
                "source_invalid",
                "Usage range is outside its actual native execution interval",
            )
        captured = [
            a
            for a in context.artifacts
            if a.reference() == packet["evidence"] and a.access == "private"
        ]
        if (
            packet["schema"] != "harness.native-usage/v1"
            or packet["runtime"] != "pi"
            or packet["extraction"] != native_usage.extraction(packet["groups"])
            or packet["start_byte"] != start
            or packet["end_byte"] != end
            or len(captured) != 1
            or len(captured[0].content) != end - start
            or packet["source_sha256"] != captured[0].reference()["sha256"]
            or native_usage.extract(captured[0].content) != packet["groups"]
            or native_usage.coverage(packet["groups"]) != packet["coverage"]
        ):
            raise Rejected(
                "source_invalid", "Native usage does not match its fixed original bytes"
            )
        if any(
            r["kind"] == "usage"
            and r["principal"] == context.principal()
            and start < r["end_byte"]
            and r["start_byte"] < end
            for r in records
        ):
            raise Rejected(
                "domain_conflict",
                "This native interval already has an attribution; retry its original request",
            )
        record = {
            **packet,
            "kind": "usage",
            "task_attempt": data["task_attempt"],
            "interval_id": data["interval_id"],
            "principal": context.principal(),
        }
    elif intent == "record_attention":
        fields(data, {"task_attempt", "category", "duration_seconds"})
        executor(state, context, data["task_attempt"])
        evidence = human_decision(context, intent, data, wi)
        assert context.source is not None
        duration = data["duration_seconds"]
        if (
            data["category"]
            not in {"instruction", "clarification", "review", "correction", "other"}
            or duration is not None
            and (
                type(duration) not in {int, float}
                or not math.isfinite(duration)
                or not 0 <= duration <= 86400
            )
        ):
            raise Rejected(
                "invalid_input",
                "Attention requires a known category and explicit bounded duration or null",
            )
        if any(
            r["kind"] == "attention" and r["source_ref"] == context.source.reference
            for r in records
        ):
            raise Rejected(
                "domain_conflict", "The original human event cannot be counted twice"
            )
        record = {
            "kind": "attention",
            **data,
            "source_ref": context.source.reference,
            "source_sha256": evidence,
            "duration_basis": "unavailable"
            if duration is None
            else "human_self_report",
            "category_basis": "human_confirmed",
            "principal": context.principal(),
        }
    else:
        fields(data, {"candidate_id", "rubric", "version", "score", "evidence"})
        identity(data["rubric"])
        identity(data["version"])
        candidate = review.candidate(state, data["candidate_id"])
        assignment, source = candidate["assignment"], context.source
        if (
            assignment is None
            or not assignment["acknowledged"]
            or assignment["verifier"] != context.principal()
            or source is None
            or source.kind != "independent_result"
            or source.approved_intent != intent
            or source.approved_input_digest
            != digest({"intent": intent, "work_item_id": wi, "input": data})
        ):
            raise Rejected(
                "authority_denied",
                "Quality requires the exact original acknowledged verifier",
            )
        score = data["score"]
        if score is not None and (
            type(score) not in {int, float}
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise Rejected(
                "invalid_input",
                "Quality score is an explicit normalized value or unavailable",
            )
        review.validated_evidence(data["evidence"], context)
        if any(
            r["kind"] == "quality"
            and r["candidate_id"] == data["candidate_id"]
            and r["rubric"] == data["rubric"]
            and r["version"] == data["version"]
            for r in records
        ):
            raise Rejected(
                "domain_conflict", "A recorded candidate rubric cannot be overwritten"
            )
        record = {
            "kind": "quality",
            **data,
            "assignment_id": assignment["id"],
            "source_sha256": source.sha256,
            "principal": context.principal(),
            "coverage": "unavailable" if score is None else "observed",
        }
    record.update(record_id=operation, generation=state["generation"], recorded_at=at)
    return Decision(
        [event("MeasurementRecorded", record=record)],
        {"record_id": operation, "kind": record["kind"]},
    )


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "UsageIntervalOpened":
        interval = data["interval"]
        state.setdefault("usage_intervals", {})[interval["interval_id"]] = interval
        return True
    if kind == "UsageIntervalClosed":
        state["usage_intervals"][data["interval_id"]].update(
            end_byte=data["end_byte"],
            closed=True,
            end_boundary="observed" if data["end_byte"] is not None else "unavailable",
        )
        if "end_boundary_reason" in data:
            state["usage_intervals"][data["interval_id"]]["end_boundary_reason"] = data[
                "end_boundary_reason"
            ]
        return True
    if kind != "MeasurementRecorded":
        return False
    state.setdefault("measurements", []).append(data["record"])
    return True


def close_intervals(
    state: Json | None, events: list[Json], context: HostContext
) -> list[Json]:
    if state is None:
        return []
    close_all = any(
        e["kind"]
        in {
            "WorkCompleted",
            "WorkTerminated",
            "WorkStopped",
            "MigrationSealed",
            "TransferReserved",
            "HumanControlAssumed",
            "WorkReopened",
        }
        for e in events
    )
    finished = {
        e["payload"]["attempt_id"]
        for e in events
        if e["kind"] in {"TaskResultRecorded", "TaskAbandoned"}
    }
    closing = {
        key: item
        for key, item in state.get("usage_intervals", {}).items()
        if not item.get("closed", False)
        and (close_all or item["task_attempt"] in finished)
    }
    events = []
    for key, item in closing.items():
        end = (
            context.native_cursor
            if item["principal"] == context.principal()
            and type(context.native_cursor) is int
            else None
        )
        reason = "not_observed" if end is None else None
        known_end = max(
            [item["start_byte"]]
            + [r["end_byte"] for r in state.get("measurements", [])
               if r["kind"] == "usage" and r["interval_id"] == key]
        )
        if end is not None and end < known_end:
            # Preserve all accepted originals and close attribution, but do not
            # let a damaged measurement boundary veto Stop, transfer or work.
            end, reason = None, "source_moved_backwards"
        events.append(event(
            "UsageIntervalClosed", interval_id=key, end_byte=end,
            **({"end_boundary_reason": reason} if reason else {}),
        ))
    return events


def reject_duplicate_usage(state: Json, context: HostContext, data: Json) -> None:
    if any(
        record["kind"] == "usage"
        and record["principal"] == context.principal()
        and data["start_byte"] < record["end_byte"]
        and record["start_byte"] < data["end_byte"]
        for record in state.get("measurements", [])
    ):
        raise Rejected(
            "domain_conflict",
            "These native bytes already have an observation attribution",
        )


def outcome_record_times(history: list[Json]) -> list[Json]:
    """Original decision times, not the time a reader/importer happened to run.

    Supplement the existing measurement query without changing its closed public
    payload or exposing original human/verifier text. No event or grant is added.
    """
    names = {"VerificationRecorded": "verification", "ReviewRecorded": "review",
             "VerificationWaived": "waiver"}
    result = []
    for entry in history:
        for event in entry["events"]:
            kind, payload = event["kind"], event["payload"]
            name = names.get(kind)
            if name:
                record = payload[name]
                result.append({"candidate_id": payload["candidate_id"], "kind": name,
                               "record_id": record["id"], "recorded_at": entry["at"],
                               "revision": entry["revision"]})
            elif kind == "MeasurementRecorded" and payload["record"]["kind"] == "quality":
                record = payload["record"]
                result.append({"candidate_id": record["candidate_id"], "kind": "quality",
                               "record_id": record["record_id"], "recorded_at": entry["at"],
                               "revision": entry["revision"]})
    return result


def outcomes(state: Json) -> list[Json]:
    result = []
    for key, candidate in state["candidates"].items():
        verification = candidate["verification"]
        result.append(
            {
                "candidate_id": key,
                "current": key == state["active_candidate"],
                "result_ids": candidate["content"]["result_ids"],
                "definition_revision": candidate["content"]["definition_revision"],
                "graph_revision": candidate["content"]["graph_revision"],
                "verification": verification,
                "review": candidate["review"],
                "waiver": candidate["waiver"],
                "coverage": "unavailable" if verification is None else "observed",
            }
        )
    return result


def public(state: Json) -> Json:
    records = state.get("measurements", [])
    # The observation ranges, rather than elapsed WI lifetime, define coverage.
    return {
        "source": "journal",
        "intervals": [
            {k: v for k, v in item.items() if k != "principal"}
            | {"session": "measurement-session." + digest(item["principal"])}
            for item in state.get("usage_intervals", {}).values()
        ],
        "coverage": "recorded_intervals_only" if records else "unavailable",
        "usage": [
            {k: v for k, v in r.items() if k != "principal"}
            | {
                "session": "measurement-session." + digest(r["principal"]),
                "actor": r["principal"]["actor"],
            }
            for r in records
            if r["kind"] == "usage"
        ],
        "attention": [
            {k: v for k, v in r.items() if k not in {"principal", "source_ref"}}
            for r in records
            if r["kind"] == "attention"
        ],
        "quality": [
            {k: v for k, v in r.items() if k != "principal"}
            | {"verifier": r["principal"]["actor"]}
            for r in records
            if r["kind"] == "quality"
        ],
        "outcomes": outcomes(state),
        "business_progress": state["status"],
        "terminal": state["terminal"],
    }
