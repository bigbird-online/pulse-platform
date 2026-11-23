"""Antigravity CLI (agy) transcript usage: one request per model step.

agy writes ``brain/<conversation>/.system_generated/logs/transcript_full.jsonl``
under the account HOME. Each ``PLANNER_RESPONSE`` row is one model step and
records its own ``model``, ``input_tokens`` (uncached input),
``cache_read_tokens``, ``output_tokens`` (thinking included) and
``created_at`` (UTC, whole seconds). The rows name no session: the conversation
directory does. A missing or non-count token field is an unknown quantity,
never zero. agy's CLI reports no cache-write class: it is recorded as 0 only
for a Gemini model beside an observed count (agy's ACP reports Gemini's
``cachedWriteTokens`` as 0); for any other or unknown model it is unknown.

The WI comes from the Host: Relay starts agy under a runtime usage binding
and records the turn's execution and usage (``agy_result.v1``) with this
conversation's native scope. ``collaboration_query.reconcile_usage`` joins
the steps to that turn only on exact token conservation. agy has no public
per-token price, so valuation reports these rows as unpriced.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Mapping

from .receipts import Receipt, ReceiptError, reference

RUNTIME = "agy"
TRANSCRIPT_MAPPING = "agy_transcript.v1"
# The Relay Host's per-turn aggregate of the steps' usage (stream-json step_update).
RESULT_MAPPING = "agy_result.v1"
TRANSCRIPT = Path(".system_generated/logs/transcript_full.jsonl")
TOKEN_NAMES = ("input_tokens", "cache_read_tokens", "output_tokens")
UNPRICED_REASON = "agy_price_unknown"
_CONVERSATION = re.compile(r"^[A-Za-z0-9-]{1,128}$")


def conversation_id(path: Path) -> str:
    """The conversation a transcript belongs to: its brain directory name."""
    parts = path.parts
    tail = TRANSCRIPT.parts
    if len(parts) < len(tail) + 1 or parts[-len(tail):] != tail:
        raise ReceiptError("agy transcript is outside its conversation layout")
    name = parts[-len(tail) - 1]
    if not _CONVERSATION.fullmatch(name):
        raise ReceiptError("agy conversation identity is invalid")
    return name


def transcript_files(root: Path, rejected: list[Path] | None = None) -> list[Path]:
    """Every conversation's full transcript; transcript.jsonl is a copy, never read.

    Every directory below the registered root must be itself, not a link out
    of it: a transcript is read only at its physical place there, as a regular
    file. Given ``rejected``, any other path is listed there and skipped, so
    one bad conversation does not hide the others; without it, it raises.
    """
    if root.is_file():
        return [root]
    base = root.resolve(strict=True)
    files = []
    for path in sorted(root.glob("*/" + TRANSCRIPT.as_posix())):
        try:
            placed = path.resolve(strict=True) == base / path.relative_to(root) and path.is_file()
        except (OSError, RuntimeError):  # Dangling, looping or unreadable link.
            placed = False
        if placed:
            files.append(path)
        elif rejected is None:
            raise ReceiptError("agy transcript is outside the registered brain root")
        else:
            rejected.append(path)
    return files


def native_scope(conversation: str) -> str:
    return reference("native_session", [RUNTIME, conversation])


def step_usage(row: Mapping[str, object]) -> dict[str, int]:
    """The step's own token counts; an absent or invalid field stays absent (unknown)."""
    return {name: row[name] for name in TOKEN_NAMES
            if type(row.get(name)) is int and row[name] >= 0}  # type: ignore[operator]


def read_agy(path: Path, observed: str, *, into: list[Receipt] | None = None,
             data: bytes | None = None) -> list[Receipt]:
    """Usage receipts of every complete model step in one transcript prefix.

    agy appends while a turn runs; a final line without its newline belongs
    to the next collection, so it is not read here.
    """
    from .receipt_sources import _revision, usage_receipt

    result = into if into is not None else []
    conversation = conversation_id(path)
    scope = native_scope(conversation)
    content = path.read_bytes() if data is None else data
    content = content[:content.rfind(b"\n") + 1]
    for raw in content.splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        if not isinstance(row, Mapping):
            raise ReceiptError("agy transcript row is not an object")
        if row.get("type") != "PLANNER_RESPONSE":
            continue
        step = row.get("step_index")
        if type(step) is not int or step < 0:
            raise ReceiptError("agy model step lacks its index")
        model = row.get("model")
        at = row.get("created_at")
        item = usage_receipt(RUNTIME, [conversation, step], step_usage(row),
            at=at if isinstance(at, str) and at else None, observed=observed,
            model=model if isinstance(model, str) and model else None,
            final=row.get("status") == "DONE", mapping=TRANSCRIPT_MAPPING, native_scope_ref=scope)
        _revision(result, item)
    return result
