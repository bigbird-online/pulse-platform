"""One saved price source for CLI and Stats; resolution never fetches or writes.

Only the installed Host registry and its Stats-derived registry inherit the
existing managed cache. An unrelated registry or ledger-only historical read
must explicitly choose its price evidence. Explicit snapshots never fall back.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
from typing import Any, Mapping

from .events import parse_timestamp
from .receipts import ReceiptError


def managed_state_root() -> Path:
    # Same OS account as the installed publisher; do not borrow a spoofed HOME.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/pulse"


def managed_catalog_path(stats_state: Path | None = None) -> Path:
    return (stats_state if stats_state is not None else managed_state_root() / "stats") / "openrouter-catalog.json"


def configured_catalog_path(registry: Mapping[str, Any] | None) -> Path | None:
    if registry is None or "catalog_snapshot" not in registry:
        return None
    value = registry["catalog_snapshot"]
    if not isinstance(value, str) or not value.strip():
        raise ReceiptError("catalog_snapshot must name a saved catalog")
    return Path(value)


def resolve_catalog_path(*, explicit: Path | None = None,
                         registry: Mapping[str, Any] | None = None,
                         registry_path: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    configured = configured_catalog_path(registry)
    if configured is not None:
        return configured
    if registry is None or registry_path is None:
        return None
    root = managed_state_root()
    managed = {root / "compute-cost/sources.json", root / "stats/collector-sources.json"}
    if registry_path.absolute() not in managed:
        return None
    catalog = managed_catalog_path()
    # Missing default means unavailable. A malformed existing default is an
    # error, not permission to fetch new rates or hide missing price evidence.
    return catalog if catalog.exists() or catalog.is_symlink() else None


def validate_catalog_snapshot(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != {"retrieved_at", "catalog"}
            or not isinstance(value["catalog"], Mapping)):
        raise ReceiptError("catalog snapshot requires retrieved_at and catalog")
    if not isinstance(value["retrieved_at"], str):
        raise ReceiptError("catalog retrieved_at must be a timestamp")
    parse_timestamp(value["retrieved_at"], "retrieved_at")
    return value


def load_catalog_snapshot(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        if path.is_symlink() or not path.is_file():
            raise ReceiptError("catalog snapshot must be a regular file")
        return validate_catalog_snapshot(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ReceiptError("catalog snapshot unavailable or invalid") from exc


def observe_catalog_snapshot(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    """Price evidence may be unavailable without erasing known usage/outputs."""
    try:
        return load_catalog_snapshot(path), None
    except ReceiptError:
        return None, "catalog_snapshot_unavailable"
