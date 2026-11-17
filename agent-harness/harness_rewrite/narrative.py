"""Agent-written judgement for the WI project page (`record_project_narrative`).

Facts on the page come from the kernel; the narrative is the owning Agent's
judgement: a banner, next steps, risks, decisions and background. The kernel
bounds and validates it, keeps only the latest one, and the page escapes every
character and links only http(s). A narrative is shown as possibly stale once
newer progress is recorded or it is older than STALE_AFTER.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .model import Json, Rejected, fields

INTENT = "record_project_narrative"
TEXT_LIMIT = 300
BACKGROUND_LIMIT = 3000
LIST_LIMITS = {"next_steps": 5, "risks": 5, "decisions": 10}
STALE_AFTER = timedelta(days=3)


def _text(value: Any, limit: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Rejected("invalid_input", f"Narrative text must be non-empty and at most {limit} characters")
    allowed = "\n" if multiline else ""
    if any(not (ch.isprintable() or ch in allowed) for ch in value):
        raise Rejected("invalid_input", "Narrative text must be printable")
    return value.strip()


def validate(data: Json) -> Json:
    """The bounded narrative; at least one part, every part plain text."""
    fields(data, set(), {"banner", *LIST_LIMITS, "background"})
    if not data:
        raise Rejected("invalid_input", "A narrative needs at least one part")
    clean: Json = {}
    if "banner" in data:
        clean["banner"] = _text(data["banner"], TEXT_LIMIT)
    for name, limit in LIST_LIMITS.items():
        if name in data:
            items = data[name]
            if not isinstance(items, list) or not items or len(items) > limit:
                raise Rejected("invalid_input", f"{name} takes 1 to {limit} items")
            clean[name] = [_text(item, TEXT_LIMIT) for item in items]
    if "background" in data:
        clean["background"] = _text(data["background"], BACKGROUND_LIMIT, multiline=True)
    return clean
