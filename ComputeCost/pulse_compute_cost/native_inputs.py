"""Native input receipts: what a node read from its own files, for another node to judge.

A collector on the node that holds the native files extracts the inputs a
derivation stage needs (runtime launch links here; request purposes in
``request_inputs``) and records each as an immutable ``native_input`` fact. Any collector that reads the shared stream
then runs the stage over the union of its own extraction and these facts.
An input is content-addressed: the same extraction always has the same
``fact_ref``, so a repeated push is a duplicate, never a new version.
Inputs are collector evidence, not reportable activity; queries skip them.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .events import parse_timestamp
from .receipts import Receipt, ReceiptError, canonical, digest, valid_node_identifier, REF

FEATURE = "native_input.v1"
PRODUCER = "native_input.v1"
LAUNCH = "runtime_launch.v1"
MAX_EVIDENCE = 16
_LAUNCH_FIELDS = {"context", "node", "entrypoint", "started_at", "ended_at", "evidence"}


def _validate_launch(value: Mapping[str, Any]) -> None:
    from .runtime_subcalls import ENTRIES, validate_subcall
    if not _LAUNCH_FIELDS <= set(value) <= _LAUNCH_FIELDS | {"parent_usage_ref"}:
        raise ReceiptError("runtime launch input fields do not match")
    validate_subcall(value["context"])
    if value["context"]["basis"] != "native_tool_result":
        raise ReceiptError("a runtime launch input is read from a native tool result")
    if value["node"] is not None and not valid_node_identifier(value["node"]):
        raise ReceiptError("invalid runtime launch node")
    if value["entrypoint"] not in ENTRIES:
        raise ReceiptError("invalid runtime launch entrypoint")
    if "parent_usage_ref" in value and (not isinstance(value["parent_usage_ref"], str)
                                        or not REF.fullmatch(value["parent_usage_ref"])):
        raise ReceiptError("invalid launching usage reference")
    for field in ("started_at", "ended_at"):
        if not isinstance(value[field], str):
            raise ReceiptError("runtime launch boundaries are required")
    if parse_timestamp(value["started_at"], "launch") > parse_timestamp(value["ended_at"], "result"):
        raise ReceiptError("runtime launch boundary is reversed")
    evidence = value["evidence"]
    if (not isinstance(evidence, list) or not 0 < len(evidence) <= MAX_EVIDENCE
            or any(not isinstance(v, str) or not REF.fullmatch(v) for v in evidence)):
        raise ReceiptError("runtime launch evidence must be opaque references")


VALIDATORS = {LAUNCH: _validate_launch}


def validate_input(payload: Mapping[str, Any]) -> None:
    """Closed per-class contract; called from ``receipts.validate_payload``."""
    from .request_inputs import KEYS, validate_snapshot
    input_class = payload.get("input_class")
    if not isinstance(payload.get("value"), Mapping):
        raise ReceiptError("native input value must be an object")
    if input_class in KEYS:
        validate_snapshot(input_class, payload["value"])
        return
    validator = VALIDATORS.get(input_class)
    if validator is None:
        raise ReceiptError("unsupported native input class")
    validator(payload["value"])


def input_receipt(input_class: str, value: Mapping[str, Any], *, source_ref: str, observed: str,
                  occurred_at: str | None) -> Receipt:
    payload = {"input_class": input_class, "source_ref": source_ref, "value": dict(value)}
    return Receipt.make("native_input", [input_class, digest(payload)], payload,
                        occurred_at=occurred_at, observed_at=observed, producer=PRODUCER,
                        evidence=tuple(value.get("evidence", ())) + (source_ref,))


def launch_inputs(launches: Iterable[Mapping[str, Any]], *, source_ref: str, observed: str) -> list[Receipt]:
    """One immutable input per extracted launch (writer side)."""
    return [input_receipt(LAUNCH, launch, source_ref=source_ref, observed=observed,
                          occurred_at=launch["started_at"]) for launch in launches]


def stream_inputs(facts: Iterable[Receipt], input_class: str) -> list[Mapping[str, Any]]:
    """Every distinct input value of one class in the stream (reader side)."""
    found: dict[bytes, Mapping[str, Any]] = {}
    for row in facts:
        if row.kind == "native_input":
            payload = row.payload
            if payload["input_class"] == input_class:
                found.setdefault(canonical(payload["value"]), payload["value"])
    return list(found.values())


def merge_launches(local: list[Mapping[str, Any]], facts: Iterable[Receipt]) -> list[Mapping[str, Any]]:
    """This collection's own launches, then those other nodes recorded, without repeats."""
    seen = {canonical(launch) for launch in local}
    merged = list(local)
    for launch in stream_inputs(facts, LAUNCH):
        if canonical(launch) not in seen:
            seen.add(canonical(launch))
            merged.append(dict(launch))
    return merged


def reportable(facts: Iterable[Receipt]) -> list[Receipt]:
    """Facts a query reports on: collector inputs are evidence, not activity."""
    return [row for row in facts if row.kind != "native_input"]

