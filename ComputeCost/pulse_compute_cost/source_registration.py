"""Prepare explicit native inputs for the existing collector; never activate them.

The destination is a NEW owner-only candidate registry. Existing Host/Harness,
ledger root and other settings are preserved. No directory discovery, migration,
permission changes, provider calls or independent ledger is performed here.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from .receipt_sources import REGISTRY_SCHEMA
from .receipts import ReceiptError, canonical, valid_node_identifier


def _normalized_root(value: object) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ReceiptError("native roots must be explicit absolute paths")
    return os.path.normpath(value)


def _registered_roots(source: Mapping[str, Any]) -> frozenset[str]:
    from .source_layout import manifest_paths, codex_calendar_paths
    resolved = codex_calendar_paths(source)
    if resolved is not None:
        calendar = Path(source['calendar_root'])
        resolved = [p for p in resolved if calendar not in p.parents] + [calendar]
    else:
        resolved = manifest_paths(source)
    raw_paths = [str(p) for p in resolved] if resolved is not None else source.get("paths", [source.get("path")])
    if not isinstance(raw_paths, list):
        raise ReceiptError("existing native roots must be explicit absolute paths")
    return frozenset(_normalized_root(path) for path in raw_paths)


def _roots_overlap(left: frozenset[str], right: frozenset[str]) -> bool:
    return any(
        os.path.commonpath((first, second)) in {first, second}
        for first in left for second in right
    )



_PRINCIPAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PRINCIPAL_HISTORY_FIELDS = {"alias", "principal", "effective_from", "config_sha256"}


def _principal_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ReceiptError("invalid Relay principal mapping timestamp") from exc
    if parsed.tzinfo is None:
        raise ReceiptError("Relay principal mapping timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _relay_principal_history(config: Path, existing: object) -> list[dict[str, str]]:
    """Append explicit channel-alias principal changes without rewriting history."""
    if not config.is_absolute():
        raise ReceiptError("Relay config path must be explicit and absolute")
    st = config.lstat()
    if (config.is_symlink() or not config.is_file() or st.st_uid != os.getuid()
            or st.st_mode & 0o077 or st.st_size > 16 * 1024 * 1024):
        raise ReceiptError("Relay config must be an owner-only regular file")
    raw = config.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, Mapping) or not isinstance(document.get("users"), list):
        raise ReceiptError("Relay config does not contain a valid user registry")
    current: dict[str, str] = {}
    for user in document["users"]:
        if not isinstance(user, Mapping) or not isinstance(user.get("agents"), list):
            raise ReceiptError("Relay config contains an invalid agent registry")
        for agent in user["agents"]:
            if not isinstance(agent, Mapping):
                raise ReceiptError("Relay config contains an invalid agent")
            alias = agent.get("id")
            principal = agent.get("harnessAgentId", alias)
            if (not isinstance(alias, str) or not _PRINCIPAL_ID.fullmatch(alias)
                    or not isinstance(principal, str) or not _PRINCIPAL_ID.fullmatch(principal)):
                raise ReceiptError("Relay principal mapping is invalid")
            if alias in current and current[alias] != principal:
                raise ReceiptError("Relay alias maps to multiple Harness principals")
            current[alias] = principal
    if not current:
        raise ReceiptError("Relay config contains no agents")

    history = deepcopy(existing) if existing is not None else []
    if not isinstance(history, list):
        raise ReceiptError("Relay principal history must be a list")
    latest: dict[str, tuple[datetime, dict[str, str]]] = {}
    boundaries: set[tuple[str, datetime]] = set()
    for entry in history:
        if (not isinstance(entry, dict) or set(entry) != _PRINCIPAL_HISTORY_FIELDS
                or not isinstance(entry["alias"], str) or not _PRINCIPAL_ID.fullmatch(entry["alias"])
                or not isinstance(entry["principal"], str) or not _PRINCIPAL_ID.fullmatch(entry["principal"])
                or not isinstance(entry["config_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["config_sha256"])):
            raise ReceiptError("Relay principal history is invalid")
        when = _principal_instant(entry["effective_from"])
        boundary = (entry["alias"], when)
        if boundary in boundaries:
            raise ReceiptError("Relay principal history has a duplicate boundary")
        boundaries.add(boundary)
        prior = latest.get(entry["alias"])
        if prior is None or when > prior[0]:
            latest[entry["alias"]] = (when, entry)

    effective = datetime.fromtimestamp(st.st_mtime, timezone.utc)
    effective_text = effective.isoformat(timespec="microseconds").replace("+00:00", "Z")
    config_sha = sha256(raw).hexdigest()
    for alias, principal in sorted(current.items()):
        prior = latest.get(alias)
        if prior and prior[1]["principal"] == principal:
            continue
        if prior and effective <= prior[0]:
            raise ReceiptError("Relay principal mapping changed without a newer config boundary")
        entry = {"alias": alias, "principal": principal,
                 "effective_from": effective_text, "config_sha256": config_sha}
        history.append(entry)
        latest[alias] = (effective, entry)
    return sorted(history, key=lambda row: (row["alias"], row["effective_from"], row["config_sha256"]))


def candidate_registry(registry: Mapping[str, Any], native: Mapping[str, list[str]]) -> dict[str, Any]:
    if registry.get("schema") != REGISTRY_SCHEMA or not isinstance(registry.get("sources"), list):
        raise ReceiptError("invalid existing source registry")
    if not isinstance(registry.get("ledger_root"), str) or not registry["ledger_root"]:
        raise ReceiptError("existing ledger root is required")
    if any(kind not in {"pi", "codex", "cc"} for kind in native):
        raise ReceiptError("unsupported native registration")
    result = deepcopy(dict(registry))
    ids = [s.get("id") for s in result["sources"]]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ReceiptError("existing source identities must be unique")
    # Every candidate converges on the repository's retirement declaration.
    from .source_retirement import load_retirements
    retired = load_retirements()
    result["sources"] = [s for s in result["sources"] if s["id"] not in retired]
    for kind, raw_paths in sorted(native.items()):
        if not raw_paths:
            continue
        paths = sorted({_normalized_root(path) for path in raw_paths})
        # These are declared roots, not an instruction to scan home/workspaces.
        for text in paths:
            path = Path(text)
            st = path.lstat()
            if path.is_symlink() or not path.is_dir() or st.st_uid != os.getuid():
                raise ReceiptError("native root must be an owned regular directory")
        groups = [paths] if kind == "codex" else [[p] for p in paths]
        for group in groups:
            wanted = frozenset(group)
            overlapping = [
                source for source in result["sources"]
                if source.get("kind") == kind
                and _roots_overlap(wanted, _registered_roots(source))
            ]
            if overlapping:
                # A normalized spelling of the same declared scope is already
                # reconciled. Preserve that source's original paths and ID.
                if len(overlapping) == 1 and _registered_roots(overlapping[0]) == wanted:
                    continue
                raise ReceiptError("overlapping native roots require explicit registry reconciliation")
            identity = "native-" + kind + "-" + sha256(canonical(group)).hexdigest()[:16]
            if identity in ids:
                raise ReceiptError("candidate source identity collision")
            row = {"id": identity, "kind": kind}
            row.update({"paths": group} if kind == "codex" else {"path": group[0]})
            result["sources"].append(row)
            ids.append(identity)
    return result


def runtime_candidate_registry(registry: Mapping[str, Any], manifest: Path, *,
                               harness_root: Path | None, receipt_root: Path,
                               relay_config: Path | None = None) -> dict[str, Any]:
    """Use the inspected running Relay's exact manifest and authority root.

    Preserve old explicit native inputs as historical evidence. Replace the
    existing Harness selector rather than reading two roots as equal authority.
    A future root change in this registered manifest is followed on collection.
    Only an existing, unretired Harness selector is refreshed; a deployment
    never enrolls a Harness root, so a retired one is not re-registered.
    """
    from .source_layout import LAYOUT, manifest_paths
    manifest = manifest.absolute()
    profiles = json.loads(manifest.read_bytes()).get("profiles", {})
    pi_profiles = sorted(name for name, row in profiles.items() if row.get("sessionRoot"))
    authority_profiles = sorted(name for name, row in profiles.items() if row.get("computeCostHost", {}).get("sclHarnessRoot"))
    if not pi_profiles:
        raise ReceiptError("runtime manifest lacks session configuration")
    candidate = deepcopy(dict(registry))
    # candidate_registry validates the existing IDs and explicitly owned roots.
    pi_selector = {"id": "relay-native-pi", "kind": "pi", "runtime_manifest": str(manifest),
                   "profiles": pi_profiles, "layout": LAYOUT}
    roots = [str(p) for p in manifest_paths(pi_selector)]
    candidate = candidate_registry(candidate, {"pi": roots})
    matched = [s for s in candidate["sources"] if s["kind"] == "pi" and _registered_roots(s) <= frozenset(roots)]
    if set().union(*(_registered_roots(s) for s in matched)) != set(roots):
        raise ReceiptError("runtime roots overlap existing inputs ambiguously")
    # Keep the durable source identity of the first existing/enrolled root.
    selector_fields = {"id", "kind", "path", "paths", "runtime_manifest", "profiles", "layout"}
    contexts = [{k:v for k,v in source.items() if k not in selector_fields} for source in matched]
    if any(context != contexts[0] for context in contexts[1:]):
        raise ReceiptError("runtime roots have incompatible attribution context; explicit source reconciliation required")
    # Refreshing a manifest selector must not erase the previously enrolled
    # actor/channel/kernel wiring, optional scope or source metadata.
    pi_selector = {**contexts[0], **pi_selector}
    pi_selector["id"] = matched[0]["id"]
    candidate["sources"] = [s for s in candidate["sources"] if s not in matched] + [pi_selector]
    harness = [s for s in candidate["sources"] if s["kind"] == "harness_root"]
    hosts = [s for s in candidate["sources"] if s["kind"] == "relay_host"]
    if len(harness) > 1 or len(hosts) != 1:
        raise ReceiptError("at most one authoritative Harness and one Host source are required")
    if harness:
        if harness_root is None or not authority_profiles:
            raise ReceiptError("runtime manifest lacks authority configuration")
        selector = {**{k:v for k,v in harness[0].items() if k not in selector_fields},
                    "id": harness[0]["id"], "kind": "harness_root", "runtime_manifest": str(manifest),
                    "profiles": authority_profiles}
        if manifest_paths(selector) != [harness_root]:
            raise ReceiptError("loaded Relay and manifest Harness roots disagree")
        candidate["sources"] = [selector if s is harness[0] else s for s in candidate["sources"]]
    if not receipt_root.is_absolute() or receipt_root.is_symlink() or not receipt_root.is_dir():
        raise ReceiptError("loaded Host receipt root is invalid")
    hosts[0]["path"] = str(receipt_root)
    if relay_config is not None:
        hosts[0]["principal_alias_history"] = _relay_principal_history(
            relay_config, hosts[0].get("principal_alias_history"))
    return candidate


def _declared_workspace_rules(registry: Mapping[str, Any], site: str) -> tuple[list[dict[str, str]], str | None]:
    """Read the explicitly enrolled team registry, never infer owners from bots.

    Returns the rules and the SHA-256 of the registry bytes they came from."""
    from .receipts import reference
    from .agent_tenants import read_team_registry
    found = read_team_registry(registry)
    if found is None:
        return [], None
    document, raw = found
    root = Path(_normalized_root(document["repo_root"]))
    result = []
    for agent, entry in document["agents"].items():
        if not isinstance(entry, Mapping) or not entry.get("workspace"):
            continue
        if not isinstance(entry["workspace"], str):
            raise ReceiptError("invalid registered Agent workspace")
        relative = Path(entry["workspace"])
        if (not isinstance(agent, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", agent)
                or relative.is_absolute() or ".." in relative.parts or str(relative) == "."):
            raise ReceiptError("invalid registered Agent workspace")
        result.append({"workspace_root": str(root / relative), "agent": agent, "node": site,
            "runtime": "codex", "registry_ref": reference("workspace_registry",
                [sha256(raw).hexdigest(), agent, str(relative), site])})
    if len({r["workspace_root"] for r in result}) != len(result):
        raise ReceiptError("team registry assigns one workspace to multiple Agents")
    return result, sha256(raw).hexdigest()


def actor_candidate_registry(registry: Mapping[str, Any], surfaces: Mapping[str, Any], *, site: str) -> dict[str, Any]:
    """Derive actor rules from the existing workspace authority, without activation.

    Native source location is explicitly enrolled as execution provenance;
    a received peer bundle retains its own provenance, never the collector's.
    """
    from .receipts import reference
    if registry.get("schema") != REGISTRY_SCHEMA or not valid_node_identifier(site):
        raise ReceiptError("invalid actor source enrollment")
    rules, team_sha = _declared_workspace_rules(registry, site)
    kernel_locations = set()
    site_surfaces = set()
    for surface in surfaces.get("surfaces", []):
        deployment = surface.get("deployment", {})
        if deployment.get("site") != site:
            continue
        surface_id = surface.get("surface_id")
        if isinstance(surface_id, str):
            site_surfaces.add(surface_id)
        if not deployment.get("pulse_home"):
            continue
        if deployment.get("kernel_root"):
            surface_id = surface.get("surface_id")
            if not isinstance(surface_id, str) or not __import__('re').fullmatch(r'surface_v1_[a-z0-9_]+', surface_id):
                raise ReceiptError("invalid registered kernel surface")
            kernel_locations.add((_normalized_root(deployment['kernel_root']), surface_id))
        workspace, agent = surface.get("workspace"), surfaces.get("agent_id")
        if (not isinstance(workspace, str) or not workspace or '/' in workspace or workspace in {'.', '..'}
                or not isinstance(agent, str) or not agent):
            raise ReceiptError("invalid canonical workspace mapping")
        rules.append({"workspace_root": _normalized_root(str(Path(deployment['pulse_home']) / workspace)),
            "agent": agent, "node": site, "runtime": "codex",
            "registry_ref": reference("workspace_registry", [surfaces.get('schema_version'), surface, agent])})
    # Workspace ownership and the actual execution node are independent. A Mac
    # client can open this repository's declared cloud surface directory.
    homes = {s.get("deployment", {}).get("pulse_home") for s in surfaces.get("surfaces", [])
             if s.get("deployment", {}).get("site") == site and s.get("deployment", {}).get("pulse_home")}
    for home in homes:
        for surface in surfaces.get("surfaces", []):
            workspace = surface.get("workspace")
            if (not isinstance(workspace, str) or not workspace or '/' in workspace
                    or workspace in {'.', '..'}):
                raise ReceiptError("invalid canonical workspace mapping")
            rule = {"workspace_root": _normalized_root(str(Path(home) / workspace)),
                "agent": surfaces["agent_id"], "node": site, "runtime": "codex",
                "registry_ref": reference("workspace_registry", [surfaces.get('schema_version'), surface, surfaces["agent_id"]])}
            if rule not in rules:
                rules.append(rule)
    if len(kernel_locations) > 1:
        raise ReceiptError('ambiguous registered native kernel location')
    result = deepcopy(dict(registry))
    # The adopted team registry; workspace_actor_drift compares against it.
    if team_sha is not None:
        result['workspace_registry_sha256'] = team_sha
    else:
        result.pop('workspace_registry_sha256', None)
    for source in result['sources']:
        if source['kind'] not in {'codex', 'pi', 'cc', 'relay_host'}:
            continue
        source.update(node=site, actor_attribution=True)
        if source['kind'] in {'codex', 'pi', 'cc'}:
            if len(site_surfaces) > 1:
                raise ReceiptError('ambiguous registered native execution surface')
            if site_surfaces:
                source['scl_surface_id'] = next(iter(site_surfaces))
            if kernel_locations:
                source['scl_kernel_root'], source['scl_surface_id'] = next(iter(kernel_locations))
        if source['kind'] in {'codex', 'pi', 'cc'}:
            combined = {r['workspace_root']: deepcopy(r) for r in source.get('workspace_actors', [])}
            for rule in rules:
                rule = {**deepcopy(rule), 'runtime': source['kind']}
                prior = combined.get(rule['workspace_root'])
                if prior and any(prior[k] != rule[k] for k in ('agent', 'node', 'runtime')):
                    raise ReceiptError('registered workspace ownership changed; explicit actor review required')
                combined[rule['workspace_root']] = rule
            source['workspace_actors'] = list(combined.values())
    return result


def workspace_actor_drift(registry: Mapping[str, Any]) -> list[dict[str, str]]:
    """Report where adopted workspace rules no longer match the team registry.

    A workspace renamed on disk, or a changed team registry, leaves the adopted
    `workspace_actors` stale until the sources are adopted again; usage from
    the new path then has no Agent. This only reports; it changes nothing.
    """
    sources = [s for s in registry.get('sources', [])
               if s.get('actor_attribution') is True and s.get('kind') in {'codex', 'pi', 'cc'}
               and isinstance(s.get('node'), str)]
    if not sources or registry.get('workspace_registry_path') is None:
        return []
    from .agent_tenants import read_team_registry
    try:
        _, raw = read_team_registry(registry)
    except (OSError, ReceiptError):
        return [{'reason': 'workspace_registry_unreadable'}]
    findings = []
    adopted = registry.get('workspace_registry_sha256')
    if adopted != sha256(raw).hexdigest():
        findings.append({'reason': 'workspace_registry_changed' if adopted else 'workspace_registry_unadopted'})
    missing = set()
    for source in sources:
        declared, _ = _declared_workspace_rules(registry, source['node'])
        rules = {(r.get('workspace_root'), r.get('agent')) for r in source.get('workspace_actors', [])}
        for rule in declared:
            root, agent = rule['workspace_root'], rule['agent']
            if not os.path.isdir(root):
                missing.add((agent, root))
            elif (root, agent) not in rules:
                findings.append({'reason': 'workspace_rule_unadopted', 'source': source['id'],
                                 'agent': agent, 'workspace_root': root})
    findings += [{'reason': 'workspace_root_missing', 'agent': agent, 'workspace_root': root}
                 for agent, root in sorted(missing)]
    return findings


def tenant_candidate_registry(registry: Mapping[str, Any], declaration: Path) -> dict[str, Any]:
    """Prepare explicit product membership; no route/path owner inference or activation."""
    from .tenant_context import FEATURE, load_tenant_registry, load_tenant_document
    from .relay_tenant_context import FEATURE as RELAY_FEATURE, SCHEMA as RELAY_SCHEMA
    document=load_tenant_document(declaration)
    load_tenant_registry(declaration)
    result = candidate_registry(registry, {})
    result['tenant_registry_path'] = str(declaration)
    features={FEATURE}
    if document['schema']==RELAY_SCHEMA:features.add(RELAY_FEATURE)
    result['required_receipt_features'] = sorted(set(result.get('required_receipt_features', [])) | features)
    return result


def verification_candidate_registry(registry, *, registration_root: Path, authority_root: Path, host: str, surface_id: str):
    """Enroll an inspected historical identity source; never activate it."""
    from .verification_workspace import FEATURE, historical_bindings
    from .source_layout import manifest_paths
    result = candidate_registry(registry, {})
    authorities = [p for source in result['sources'] if source['kind'] == 'harness_root'
                   for p in (manifest_paths(source) or [Path(source['path'])])]
    if authorities != [authority_root]:
        raise ReceiptError('verification authority must be the single existing Harness root')
    sources = [s for s in result['sources'] if s['kind'] == 'codex' and s.get('actor_attribution') is True
               and s.get('scl_surface_id') == surface_id]
    if len(sources) != 1:
        raise ReceiptError('verification native source must be exact and unique')
    sources[0]['verification_workspace_registry'] = {'registration_root':str(registration_root),
        'authority_root':str(authority_root),'host':host,'surface_id':surface_id}
    historical_bindings(sources[0], [])
    result['required_receipt_features'] = sorted(set(result.get('required_receipt_features', [])) | {FEATURE})
    return result


def stage_ledger_root(registry: Mapping[str, Any]) -> Path:
    """An enrolled stage writer may only use an existing owned store."""
    if registry.get('schema') != REGISTRY_SCHEMA or not isinstance(registry.get('sources'), list):
        raise ReceiptError('invalid existing stage source registry')
    value = registry.get('ledger_root')
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ReceiptError('stage ledger must be an explicit absolute directory')
    root = Path(value)
    try:
        if root.resolve(strict=True) != root or not root.is_dir() or root.stat().st_uid != os.getuid():
            raise ValueError('ownership')
    except (OSError, ValueError) as exc:
        raise ReceiptError('stage ledger must be an existing registered owned directory') from exc
    return root


def stage_candidate_registry(registry: Mapping[str, Any], *, observation_core_entrypoint: str | None = None) -> dict[str, Any]:
    """Prepare the same registry for stage readers/writer, without activation.

    The publication gate must still verify every installed consumer before using
    this candidate. A declared feature is not an installation receipt.
    """
    from .receipts import SUPPORTED_RECEIPT_FEATURES
    stage_ledger_root(registry)
    result = candidate_registry(registry, {})
    features = result.get('required_receipt_features', [])
    if (not isinstance(features, list)
            or any(not isinstance(f, str) or f not in SUPPORTED_RECEIPT_FEATURES for f in features)):
        raise ReceiptError('unsupported source registry features')
    result['required_receipt_features'] = sorted(set(features) | {'stage_semantics.v1'})
    if observation_core_entrypoint is not None:
        entry = Path(observation_core_entrypoint)
        if not entry.is_absolute() or entry.name != 'harness.pyz' or entry.parent.name != 'active':
            raise ReceiptError('observations require the installed core entry')
        previous = result.get('observation_core_entrypoint')
        if previous is not None and previous != str(entry):
            raise ReceiptError('observation authority cannot be silently replaced')
        result['observation_core_entrypoint'] = str(entry)
        result['required_receipt_features'] = sorted(set(result['required_receipt_features']) | {'production_observations.v1'})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    for kind in ("pi", "codex", "cc"):
        parser.add_argument("--" + kind, action="append", default=[])
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--harness-root", type=Path)
    parser.add_argument("--receipt-root", type=Path)
    parser.add_argument("--relay-config", type=Path)
    parser.add_argument("--tenant-registry", type=Path, help="explicit product membership declaration; separate candidate only")
    parser.add_argument('--verification-registration-root', type=Path)
    parser.add_argument('--verification-authority-root', type=Path)
    parser.add_argument('--verification-host')
    parser.add_argument('--verification-surface')
    parser.add_argument('--stage-semantics', action='store_true', help='prepare stage feature on the existing store; never activate')
    parser.add_argument('--observation-core-entrypoint', type=Path)
    parser.add_argument('--stage-ledger-root', type=Path,
        help='first stage-only enrollment of an explicit existing data root; registry must be absent, no activation or collection')
    args = parser.parse_args()
    try:
        if args.stage_ledger_root is not None:
            # First enrollment is an operator candidate, not discovery, a new
            # ledger, or permission to replace an existing Host registration.
            if (not args.stage_semantics or args.registry.exists() or args.registry.is_symlink()
                    or not args.registry.is_absolute() or args.registry.resolve() != args.registry
                    or any((args.pi, args.codex, args.cc, args.runtime_manifest,
                        args.harness_root, args.receipt_root, args.relay_config, args.tenant_registry,
                        args.verification_registration_root, args.verification_authority_root,
                        args.verification_host, args.verification_surface))):
                raise ReceiptError('first stage enrollment requires an absent registry and no collection selectors')
            result = {'schema': REGISTRY_SCHEMA, 'ledger_root': str(args.stage_ledger_root), 'sources': []}
            stage_ledger_root(result)
        else:
            st = args.registry.lstat()
            if args.registry.is_symlink() or not args.registry.is_file() or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise ReceiptError("existing registry must be owner-only")
            result = candidate_registry(json.loads(args.registry.read_text()), {k:getattr(args,k) for k in ("pi","codex","cc")})
        if args.runtime_manifest:
            if not args.receipt_root:
                raise ReceiptError("inspected runtime receipt root is required")
            result = runtime_candidate_registry(result, args.runtime_manifest,
                harness_root=args.harness_root, receipt_root=args.receipt_root,
                relay_config=args.relay_config)
        if args.tenant_registry:
            result = tenant_candidate_registry(result, args.tenant_registry)
        verification_args = [args.verification_registration_root,args.verification_authority_root,
                             args.verification_host,args.verification_surface]
        if any(verification_args):
            if not all(verification_args):
                raise ReceiptError('all inspected verification source fields are required')
            result = verification_candidate_registry(result, registration_root=args.verification_registration_root,
                authority_root=args.verification_authority_root, host=args.verification_host, surface_id=args.verification_surface)
        if args.stage_semantics:
            result = stage_candidate_registry(result, observation_core_entrypoint=
                str(args.observation_core_entrypoint) if args.observation_core_entrypoint else None)
        parent = args.output.parent
        if (args.output.resolve() == args.registry.resolve() or parent.resolve() != parent.absolute()
                or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077):
            raise ReceiptError("separate private candidate destination is required")
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd,"wb") as out:
            out.write(canonical(result)+b"\n")
            out.flush()
            os.fsync(out.fileno())
        print(json.dumps({"status":"candidate_only","source_count":len(result["sources"]),"ledger":"existing_unchanged"}))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print("Source registration rejected; existing configuration unchanged.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
