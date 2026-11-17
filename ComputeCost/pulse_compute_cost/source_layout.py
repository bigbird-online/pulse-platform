"""Enumerate an explicitly registered source without entering runtime HOME.

Relay writes sessionRoot/<tenant>/*.jsonl. Grandchildren such as agent/ and
compute-cost/ are not session directories; they are counted, not traversed.
Unknown files at the declared session level remain candidates and must report
parse errors. This module never moves, deletes, changes or repairs source files.
"""
from __future__ import annotations
from pathlib import Path
import json
import os
from typing import Any, Mapping

LAYOUT = "pi_session_root.v1"


def source_detail(source: Mapping[str, Any]) -> dict[str, Any]:
    return {"label": source.get("label", source["id"]), "kind": source["kind"],
            "layout": source.get("layout", "registered_recursive.v1"),
            "files_seen": 0, "files_read": 0, "files_invalid": 0,
            "excluded_directories": 0, "latest_usage_at": None, "errors": {}}


def failure(detail: dict[str, Any], category: str) -> None:
    detail["files_invalid"] += 1
    errors = detail["errors"]
    errors[category] = errors.get(category, 0) + 1


def pi_files(root: Path, layout: str | None, detail: dict[str, Any]) -> list[Path]:
    if layout not in {None, LAYOUT}:
        raise ValueError("unsupported Pi source layout")
    if root.is_symlink():
        raise ValueError("source symlink is not accepted")
    if root.is_file():
        return [root]
    if layout is None:
        return sorted(root.rglob("*.jsonl"))
    result: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            failure(detail, "unsafe_source")
        elif child.is_file() and child.suffix == ".jsonl":
            result.append(child)
        elif child.is_dir():
            for entry in sorted(child.iterdir()):
                if entry.is_symlink():
                    failure(detail, "unsafe_source")
                elif entry.is_file() and entry.suffix == ".jsonl":
                    result.append(entry)
                elif entry.is_dir():
                    detail["excluded_directories"] += 1
    return result


def manifest_paths(source: Mapping[str, Any]) -> list[Path] | None:
    """Follow an explicitly registered Relay manifest, never infer from HOME.

    The selector is stable across sessionRoot changes. Other runtimes' copied
    HOME directories are not automatically enrolled by this selector.
    """
    if "runtime_manifest" not in source:
        return None
    if source["kind"] not in {"pi", "harness_root"} or "paths" in source:
        raise ValueError("manifest selector is only supported for Pi/Harness")
    manifest = Path(source["runtime_manifest"])
    if not manifest.is_absolute() or manifest.is_symlink() or manifest.stat().st_uid != os.getuid():
        raise ValueError("unsafe runtime manifest")
    document = json.loads(manifest.read_bytes())
    profiles = document.get("profiles")
    names = source.get("profiles")
    if not isinstance(profiles, Mapping) or not isinstance(names, list) or not names:
        raise ValueError("explicit runtime profiles are required")
    result = set()
    for name in names:
        profile = profiles[name]
        value = profile.get("sessionRoot") if source["kind"] == "pi" else profile.get("computeCostHost", {}).get("sclHarnessRoot")
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("runtime source root is missing")
        result.add(Path(value))
    if source["kind"] == "harness_root" and len(result) != 1:
        raise ValueError("runtime profiles disagree on Harness authority")
    return sorted(result)


# The only manifest fields that decide which sessions a source enrolls, whether
# they are unattended and which Host kernel they bind to. Binaries, release
# paths, tool lists and skills change at every deployment and decide none.
COLLECTOR_PROFILE_FIELDS = ("sessionRoot", "unattended", "computeCostHost")
# Within computeCostHost only the Harness and kernel roots are read by
# collection. Its extension, python, pythonPath and sclExecutable name the
# current release and are rewritten by every deployment.
COLLECTOR_HOST_FIELDS = ("sclHarnessRoot", "sclKernelRoot")


def _collector_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    selected = {key: profile[key] for key in COLLECTOR_PROFILE_FIELDS if key in profile}
    host = selected.get("computeCostHost")
    if isinstance(host, Mapping):
        selected["computeCostHost"] = {key: host[key] for key in COLLECTOR_HOST_FIELDS if key in host}
    return selected


def manifest_identity(source: Mapping[str, Any]) -> str:
    """Digest the selected profiles' collector fields, not the whole manifest.

    Hashing release paths made every Relay deployment mint new evidence for
    every historical unattended Pi usage and invalidate every parse.
    """
    from .receipts import canonical
    import hashlib
    document = json.loads(Path(source["runtime_manifest"]).read_bytes())
    profiles = document["profiles"]
    selected = {name: _collector_profile(profiles[name]) for name in sorted(source["profiles"])}
    return hashlib.sha256(canonical({"schemaVersion": document.get("schemaVersion"),
                                     "profiles": selected})).hexdigest()


def unattended_pi_proof(source: Mapping[str, Any], path: Path) -> str | None:
    """Return evidence only for an explicitly selected unattended Pi profile.

    A managed unattended session can receive human work only through Relay's
    authenticated Host input path. Therefore an unannotated native input in
    this exact enrolled session root is an automated/system entrypoint. A bare
    Pi HOME, an interactive profile, a copied directory name, or an ambiguous
    profile selector proves nothing.
    """
    if source.get("kind") != "pi" or not source.get("runtime_manifest") or not source.get("profiles"):
        return None
    roots = manifest_paths(source)
    if roots is None:
        return None
    document = json.loads(Path(source["runtime_manifest"]).read_bytes())
    matches = []
    for name in source["profiles"]:
        profile = document["profiles"][name]
        root = Path(profile["sessionRoot"])
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) not in {1, 2}:
            continue
        matches.append((name, profile.get("unattended") is True, str(root)))
    if not matches or not all(item[1] for item in matches):
        return None
    roots = {item[2] for item in matches}
    if len(roots) != 1:
        return None
    from .receipts import reference
    return reference("unattended_runtime", ["collector_profiles.v2", manifest_identity(source),
        sorted(item[0] for item in matches)])


def native_binding_source(source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Use an enrolled kernel or the explicitly selected native manifest."""
    selected = dict(source)
    if selected.get("scl_kernel_root") and selected.get("scl_surface_id"):
        return selected
    if source.get("kind") != "pi" or not source.get("runtime_manifest") or not source.get("scl_surface_id"):
        return None
    manifest_paths(source)  # validate this exact manifest and profile selector
    document = json.loads(Path(source["runtime_manifest"]).read_bytes())
    roots = set()
    for name in source["profiles"]:
        profile = document["profiles"][name]
        # A canary/unmanaged launch declares no Host. It is not a second
        # kernel and must not disable the exact managed session lookup.
        if "computeCostHost" not in profile:
            continue
        host = profile["computeCostHost"]
        if not isinstance(host, Mapping):
            raise ValueError("native Host declaration is invalid")
        root = host.get("sclKernelRoot")
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise ValueError("native SCL kernel is not explicitly registered")
        roots.add(root)
    if not roots:
        return None
    if len(roots) != 1:
        raise ValueError("native profiles disagree on the existing SCL kernel")
    root = next(iter(roots))
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError("native SCL kernel is not explicitly registered")
    selected["scl_kernel_root"] = root
    return selected


def source_has_pending_usage(source: Mapping[str, Any]) -> bool:
    """Only the Host collector's explicitly classified nonterminal remainder."""
    return (source.get("kind") == "relay_host" and source.get("status") == "partial"
            and source.get("reason") == "host_usage_pending"
            and source.get("files_invalid") == 0 and not source.get("errors"))


def source_has_read_failure(source: Mapping[str, Any]) -> bool:
    return bool(source.get("files_invalid") or source.get("errors")
                or source.get("status") == "unreachable"
                or (source.get("status") == "partial" and not source_has_pending_usage(source)))

CODEX_CALENDAR_LAYOUT = 'codex_calendar_months.v1'


def codex_calendar_paths(source: Mapping[str, Any]) -> list[Path] | None:
    """Follow explicit native month roots from the enrolled month onward.

    Existing archive/month paths and the source identity remain unchanged.
    Only YYYY/MM children of the declared root are added, never another HOME
    or months before the explicit lower bound. No wall-clock rollover task.
    """
    import re
    from datetime import datetime
    if source.get('layout') != CODEX_CALENDAR_LAYOUT:
        if 'calendar_root' in source or 'calendar_from' in source:
            raise ValueError('calendar selector requires its declared layout')
        return None
    raw_paths = source.get('paths')
    if (source.get('kind') != 'codex' or 'path' in source or 'runtime_manifest' in source
            or not isinstance(raw_paths, list) or not raw_paths
            or any(not isinstance(p, str) or not Path(p).is_absolute() for p in raw_paths)):
        raise ValueError('calendar selector requires explicit Codex paths')
    value, first = source.get('calendar_root'), source.get('calendar_from')
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('explicit native calendar root required')
    if not isinstance(first, str) or re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', first) is None:
        raise ValueError('explicit first native month required')
    datetime.strptime(first, '%Y-%m')
    root = Path(value)
    if (not root.is_dir() or root.is_symlink() or root.stat().st_uid != os.getuid()
            or root/first[:4]/first[5:] not in [Path(p) for p in raw_paths]):
        raise ValueError('first native month is not an existing enrolled root')
    found = {Path(p) for p in raw_paths}
    for year in sorted(root.iterdir()):
        if re.fullmatch(r'\d{4}', year.name) is None or year.name < first[:4]:
            continue
        if year.is_symlink() or not year.is_dir() or year.stat().st_uid != os.getuid():
            raise ValueError('unsafe native calendar year')
        for month in sorted(year.iterdir()):
            if (re.fullmatch(r'0[1-9]|1[0-2]', month.name) is None
                    or year.name+'-'+month.name < first):
                continue
            if month.is_symlink() or not month.is_dir() or month.stat().st_uid != os.getuid():
                raise ValueError('unsafe native calendar month')
            found.add(month)
    return sorted(found)
