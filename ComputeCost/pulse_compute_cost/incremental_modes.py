"""refresh.json `incremental`: the WI754 switches, each independent.

`ingest` and `close` are off|on; `views` and `collection` are off|shadow|on. A missing
section or key is off, the full computation as before. Any other key or value
is a configuration error, never a silent off.
"""
from __future__ import annotations

import sys
from typing import Any

MODES = {"ingest": ("off", "on"), "views": ("off", "shadow", "on"), "collection": ("off", "shadow", "on"),
         # Closed weeks and adjustment entries (step 5); needs views shadow or on.
         "close": ("off", "on")}


def incremental_modes(config: dict[str, Any]) -> dict[str, str]:
    section = config.get("incremental")
    if section is None:
        section = {}
    if not isinstance(section, dict) or not set(section) <= set(MODES):
        raise ValueError("incremental must map ingest, views, collection and close to a mode")
    result = {}
    for key, allowed in MODES.items():
        value = section.get(key, "off")
        if value not in allowed:
            raise ValueError(f"incremental.{key} must be one of {', '.join(allowed)}")
        result[key] = value
    return result


def cache_mib() -> int:
    """SQLite page cache for the derived stores: 256 MiB on the Linux publisher, else 128."""
    return 256 if sys.platform.startswith("linux") else 128
