"""Product tenant of a known Agent, from the team registry.

The team registry (`ssot/registry.yaml`) binds every Agent to one tenant
(`agents.<id>.tenant`, a key of `owners`). Once a usage's actual Agent is
known, that binding names its tenant. A tenant proven by the session itself
(an authenticated Relay input or an explicit membership declaration) takes
precedence; when the two differ, the session tenant stays and the registry
tenant is reported as a conflict. An unknown or ambiguous Agent gets none.
Usage, WI, Agent and amounts are never changed here.
"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import re
from typing import Any, Mapping

from .receipts import ReceiptError, digest, reference

AGENT = re.compile(r"[a-z][a-z0-9_-]{0,63}")
TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
RESERVED = {"unassigned", "ambiguous", "unknown", "not_applicable"}


def read_team_registry(registry: Mapping[str, Any]) -> tuple[Mapping[str, Any], bytes] | None:
    """Read the enrolled team registry; None when none is enrolled.

    The file states its author's `repo_root`. A host reading its own checkout
    of the same file is enrolled with `workspace_registry_root`
    (`{"repo_root", "checkout_root"}`, from the surface deployment): the file
    must then state exactly that `repo_root` and lie at
    `checkout_root/ssot/registry.yaml`; the returned document names
    `checkout_root` as its root. The bytes are returned unchanged."""
    import yaml
    value = registry.get("workspace_registry_path")
    if value is None:
        return None
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ReceiptError("team registry path must be explicitly absolute")
    path = Path(os.path.normpath(value))
    st = path.lstat()
    if (path.is_symlink() or not path.is_file() or st.st_uid != os.getuid()
            or st.st_mode & 0o022 or st.st_size > 1024 * 1024):
        raise ReceiptError("workspace registry must be an owned, non-writable-by-others file")
    raw = path.read_bytes()
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ReceiptError("invalid team workspace registry") from exc
    if not isinstance(document, Mapping) or not isinstance(document.get("agents"), Mapping):
        raise ReceiptError("invalid team workspace registry")
    root = document.get("repo_root")
    mapping = registry.get("workspace_registry_root")
    if mapping is not None:
        if (not isinstance(mapping, Mapping) or set(mapping) != {"repo_root", "checkout_root"}
                or not all(isinstance(v, str) and Path(v).is_absolute() for v in mapping.values())):
            raise ReceiptError("invalid team registry root mapping")
        if root != mapping["repo_root"]:
            raise ReceiptError("workspace registry root identity differs")
        root = mapping["checkout_root"]
        document = {**document, "repo_root": root}
    if (not isinstance(root, str) or not Path(root).is_absolute()
            or path != Path(os.path.normpath(root)) / "ssot/registry.yaml" or path.resolve() != path):
        raise ReceiptError("workspace registry root identity differs")
    return document, raw


def load_agent_tenants(registry: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Agent id -> registered tenant and its opaque binding reference."""
    found = read_team_registry(registry)
    if found is None:
        return {}
    document, _ = found
    owners = document.get("owners")
    if not isinstance(owners, Mapping):
        raise ReceiptError("team registry lacks tenant owners")
    pairs = {}
    for agent, entry in document["agents"].items():
        tenant = entry.get("tenant") if isinstance(entry, Mapping) else None
        if tenant is None:
            continue
        if (not isinstance(agent, str) or not AGENT.fullmatch(agent) or not isinstance(tenant, str)
                or not TENANT.fullmatch(tenant) or tenant in RESERVED or tenant not in owners):
            raise ReceiptError("invalid registered Agent tenant")
        pairs[agent] = tenant
    # A binding reference names exactly one Agent->tenant pair; other registry
    # edits change no reference and no cached tenant view.
    return {agent: {"id": tenant, "name": tenant, "binding_ref": reference("agent_tenant_binding", [agent, tenant])}
            for agent, tenant in pairs.items()}


def agent_tenants_digest(agent_tenants: Mapping[str, Mapping[str, str]] | None) -> str | None:
    return digest(sorted((a, t["binding_ref"]) for a, t in agent_tenants.items())) if agent_tenants else None


def apply_agent_tenants(rows, contexts: dict, agent_tenants: Mapping[str, Mapping[str, str]]) -> None:
    """Complete each row's tenant context in place; see the module rules."""
    for row in rows:
        executor = row.payload.get("executor")
        context = contexts.setdefault(row.entity_ref, {})
        current = context.get("tenant")
        registered = agent_tenants.get(executor) if executor not in {None, "ambiguous"} else None
        if current is not None:
            conflict = (registered is not None and current["id"] != "ambiguous"
                        and registered["id"] != current["id"])
            context["tenant"] = {**current, "source": "session",
                                 "registry_conflict": registered["id"] if conflict else None}
        elif registered is not None:
            context["tenant"] = {"id": registered["id"], "name": registered["name"],
                                 "binding_refs": [registered["binding_ref"]],
                                 "source": "registry", "registry_conflict": None}
