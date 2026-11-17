"""Read accepted WI Outcomes from the installed native core's stats facts.

The core's read-only ``stats-facts`` export (``harness.stats_facts/v1``) lists
each WorkItem's accepted completions as opaque digests. A completion is the
core's own human-decided acceptance of fixed work records or of a reviewed
candidate; reading it is not a new review. The same export bytes produce the
same facts on every node, so a second reader never forks a delivery.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .canonical_outcomes import CONTEXT_SCHEMA, display_text
from .events import parse_timestamp, utc_text
from .receipts import Receipt, ReceiptError, reference
from .source_layout import failure

SOURCE_KIND = "harness_core"
SCHEMA = "harness.stats_facts/v1"
PRODUCER = "harness_core_outcome.v1"
MAX_BYTES = 8 * 1024 * 1024
# Core verification -> Outcome verification level. A child WI the parent's
# holder session closed by review_child is an Agent review, not a human or
# independent acceptance.
LEVELS = {"human": "human", "independent": "independent", "agent_review": "self_with_controls"}
_WI = re.compile(r"wi_\d{8}_\d{3}")
_SHA = re.compile(r"[0-9a-f]{64}")


def completion_receipt(namespace: str, item: Mapping[str, Any], completion: Mapping[str, Any],
                       observed: str) -> Receipt:
    """One accepted completion; its identity is the completing journal revision."""
    wi = item["id"]
    revision = completion["revision"]
    if type(revision) is not int or revision < 1:
        raise ReceiptError("invalid core completion revision")
    for field in ("candidate", "artifacts", "acceptance"):
        if not isinstance(completion.get(field), str) or not _SHA.fullmatch(completion[field]):
            raise ReceiptError("core completion identity is incomplete")
    if completion.get("verification") not in LEVELS:
        raise ReceiptError("invalid core completion verification")
    actor = completion.get("actor")
    if actor is not None and not isinstance(actor, str):
        raise ReceiptError("invalid core completion actor")
    at = utc_text(parse_timestamp(completion["at"], "completion at"))
    if parse_timestamp(at, "completion at") > parse_timestamp(observed, "observed_at"):
        raise ReceiptError("core completion is from the future")
    origin = [namespace, wi, revision]
    return Receipt.make("delivery", ["harness_core", *origin], {
        "wi": wi, "responsible": actor,
        "candidate_ref": reference("candidate", ["harness_core", namespace, wi, completion["candidate"]]),
        "artifact_ref": reference("artifact", ["harness_core", completion["artifacts"]]),
        "acceptance_ref": reference("acceptance", ["harness_core", completion["acceptance"]]),
        "status": "accepted", "accepted_at": at,
        "outcome_context": {"schema": CONTEXT_SCHEMA, "title": display_text(completion.get("title"), 240),
                            "summary": None, "verification_level": LEVELS[completion["verification"]]},
    }, occurred_at=at, observed_at=observed, producer=PRODUCER,
        evidence=(reference("harness_core_completion", [*origin, completion["candidate"]]),))


def read_kernel_outcomes(path: Path, data: bytes, observed: str, into: list[Receipt],
                         detail: dict[str, Any] | None = None) -> tuple[str, str | None, int, int]:
    """Append accepted completions; return (coverage status, reason, expected, observed).

    The denominator is the exported WorkItems; ``complete`` means each one was
    readable and carried its completion list. An export from a core that
    predates the list is ``unknown``: absence of completions there is not a
    confirmed zero.
    """
    if len(data) > MAX_BYTES:
        raise ReceiptError("core stats facts exceed their bound")
    document = json.loads(data)
    if (not isinstance(document, Mapping) or document.get("schema") != SCHEMA
            or document.get("read_only") is not True or document.get("source") != "journal"
            or not isinstance(document.get("namespace"), str) or not document["namespace"]
            or not isinstance(document.get("items"), list)):
        raise ReceiptError("unsupported core stats facts")
    namespace = document["namespace"]
    exported, readable, invalid = True, 0, 0
    for item in document["items"]:
        if detail is not None:
            detail["files_seen"] += 1
        try:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not _WI.fullmatch(item["id"]):
                raise ReceiptError("invalid core WorkItem identity")
            if "completions" not in item:
                exported = False
                continue
            if not isinstance(item["completions"], list):
                raise ReceiptError("invalid core completion list")
            rows = [completion_receipt(namespace, item, c, observed) for c in item["completions"]]
        except (ValueError, TypeError, KeyError, ReceiptError):
            if detail is None:
                raise
            failure(detail, "invalid_native_record")
            invalid += 1
            continue
        if detail is not None:
            detail["files_read"] += 1
        readable += 1
        into.extend(rows)
    expected = len(document["items"])
    if document.get("unavailable_items") or document.get("coverage") != "complete":
        return "partial", "core_items_unavailable", expected, readable
    if invalid:
        return "partial", "core_completions_invalid", expected, readable
    if not exported:
        return "unknown", "core_completions_not_exported", expected, readable
    if not expected:
        return "idle", None, expected, readable
    return "complete", None, expected, readable
