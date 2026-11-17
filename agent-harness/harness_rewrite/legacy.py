"""Retired legacy draft import: reducer, public view and read-only catalog.

Importing old drafts is retired with the old track and every import intent is
rejected. Historical LegacyDraft* events still replay, and the original source
catalog stays readable without granting execution.
"""
from __future__ import annotations

import json

from .model import Decision, HostContext, Json, Rejected, fields, identity

MAX_BYTES = 16 * 1024 * 1024
INTENTS = frozenset({"prepare_legacy_draft", "activate_legacy_draft"})


def decide(state: Json | None, intent: str, data: Json, context: HostContext) -> Decision:
    raise Rejected(
        "track_retired",
        "Legacy draft import is retired with the old track; history remains readable",
    )


def blocked(state: Json, intent: str) -> None:
    if state["status"] == "legacy_prepared" and intent != "activate_legacy_draft":
        raise Rejected("migration_prepared", "Activate the same authenticated import first")


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "LegacyDraftPrepared":
        state.update(status="legacy_prepared", legacy_import=data)
        return True
    if kind == "LegacyDraftActivated":
        state["status"] = "draft"
        state["legacy_import"]["activated"] = True
        return True
    return False


def public(state: Json) -> Json | None:
    value = state.get("legacy_import")
    if value is None:
        return None
    original = value["original_metadata"]
    return {
        "migration_id": value["migration_id"], "activated": value.get("activated", False),
        "original_status": original["status"],
        "created_at": original["created_at"], "updated_at": original["updated_at"],
        "main_only": original["main_only"], "cross_tenant": original["cross_tenant"],
        "source": original["source"], "archive": value["archive"],
        "retirement": value["retirement"],
    }


def catalog_observation(policy: Json, response: Json, *, wi: str | None = None,
                        listing: bool = False, after: int = 0, limit: int = 100) -> Json:
    """Expose original formal records separately from executable native facts."""
    import os
    from pathlib import Path
    import stat

    path = policy.get("legacy_catalog")
    if path is None:
        return response
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_size > MAX_BYTES):
                raise ValueError("Unsafe catalog")
            catalog = json.loads(stream.read(MAX_BYTES + 1))
        fields(catalog, {"schema", "source", "items"})
        if catalog["schema"] != "harness.legacy_catalog/v1" or not isinstance(catalog["items"], list):
            raise ValueError("Unsupported catalog")
        seen = set()
        for item in catalog["items"]:
            fields(item, {"work_item_id", "record", "conversion_reasons"})
            key = identity(item["work_item_id"])
            if key in seen or not isinstance(item["record"], dict):
                raise ValueError("Conflicting source identity")
            seen.add(key)
        summary = {"state": "snapshot", "source": catalog["source"],
                   "execution_authority": False, "total": len(catalog["items"])}
        if listing:
            summary.update(items=catalog["items"][after:after + limit],
                           next_offset=after + limit if after + limit < len(catalog["items"]) else None)
        elif wi:
            found = [item for item in catalog["items"] if item["work_item_id"] == wi]
            summary["items"] = found
            if found and response.get("error", {}).get("reason") == "Work item does not exist":
                response = {**response, "error": {
                    "code": "legacy_not_imported",
                    "reason": "Original formal WorkItem exists only in the read-only source snapshot; the old-track import is retired, never recreate it",
                }}
        return {**response, "legacy_source": summary}
    except (OSError, ValueError, TypeError, KeyError, Rejected):
        return {**response, "legacy_source": {
            "state": "unavailable", "execution_authority": False,
            "reason": "Source inventory cannot establish global absence",
        }}
