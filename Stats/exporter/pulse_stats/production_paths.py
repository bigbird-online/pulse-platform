"""Consume the installed Relay path map, not another process's environment."""
from dataclasses import replace
import json
from pathlib import Path

from .live_contract import LivePaths

# The Stats reader runs on ownermac, beside the App and the cost publisher.
READER_SITES = frozenset({"ownermac"})


def _absolute(value) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute normalized path required")
    return path


def authority_repository(surface_registry: Path, site: str) -> Path:
    """The Harness authority repository SCL registers for this reader's site."""
    data = json.loads(surface_registry.read_text(encoding="utf-8"))
    found = [surface["deployment"]["authority_repository"]
             for surface in data["surfaces"]
             if isinstance(surface.get("deployment"), dict) and surface["deployment"].get("site") == site]
    if len(found) != 1:
        raise ValueError("site authority repository is not uniquely registered")
    return _absolute(found[0])


def _harness(paths: LivePaths, root: Path) -> LivePaths:
    return replace(paths, harness_repo=root, alerts_dir=root/".harness/events",
                   work_items=root/".harness/work-items", handoffs=root/".harness/handoffs",
                   policies=root/".harness/policies")


def resolve(paths: LivePaths, map_path: Path) -> tuple[LivePaths, bool]:
    """Select Relay runtime paths and the site's Harness authority together.

    The boolean reports the Relay map only. An unregistered authority leaves
    the Harness sources unreadable instead of reading another checkout.
    """
    unavailable = map_path.parent / ".unavailable-stats-source"
    try:
        data = json.loads(map_path.read_text())
        site = data.get("site")
        if data.get("schema") != "pulse_relay_paths/v1" or site not in READER_SITES:
            raise ValueError("unsupported map")
        root = _absolute(data["root"])
        selected = [_absolute(data["paths"][key]) for key in ("dataDir", "observabilityDir")]
        for item in selected:
            if not item.is_relative_to(root):
                raise ValueError("runtime path outside the declared root")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        # No legacy fallback: a copied old success is not live evidence.
        return _harness(replace(paths, heartbeats=unavailable/"heartbeats.json",
                                obs=unavailable/"obs"), unavailable), False
    paths = replace(paths, heartbeats=selected[0]/"heartbeats.json", obs=selected[1])
    try:
        return _harness(paths, authority_repository(paths.surface_registry, site)), True
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return _harness(paths, unavailable), True
