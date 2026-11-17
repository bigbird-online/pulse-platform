"""One read-only boundary for isolated policy preflight and review."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import stat
from typing import Any

from .model import Json, digest, encoded, identity
from .repository_identity import repository_values

MAX_BYTES = 1024 * 1024
BOUND = (
    "production_ledger",
    "historical_kernel_root",
    "legacy_catalog",
    "namespace",
    "surface",
    "database",
    "views_root",
    "delivery_root",
    "dispatch_root",
    "verification_actor",
    "human_inputs",
    "agent_config_dir",
    "git_config_file",
    "python",
    "node",
    "pi_binary",
    "producer_node",
    "relay_entry",
    "notification_transport",
)
REQUIRED = {
    "namespace",
    "surface",
    "views_root",
    "delivery_root",
    "dispatch_root",
    "verification_actor",
    "python",
    "node",
    "pi_binary",
    "profiles",
}
OPTIONAL = {
    "historical_kernel_root",
    "git_config_file",
    "database",
    "production_ledger",
    "legacy_catalog",
    "execution",
    "notification_transport",
    "notification_routes",
    "verification_compute",
    "human_inputs",
    "agent_config_dir",
    "producer_node",
    "relay_entry",
    "repositories",
    "publication_targets",
    "source_consumers",
    "publication_root",
    "external_entries",
    "pull_request_gate",
    "pi_codex_oauth",
}
GROUPS = {
    "profiles": (
        {"role", "actor", "session_root", "artifact_roots"},
        {
            "runtime",
            "executable",
            "executable_root",
            "workspace_roots",
            "original_workspace_roots",
            "binding_root",
            "relay_tenants",
            "standalone",
        },
    ),
    "repositories": (
        {"repo_id", "source", "common_dir", "managed_root"},
        {"remote_url"},
    ),
    "publication_targets": ({"target_id", "repo_id", "remote_url"}, {"ref"}),
    "source_consumers": (
        {"consumer_id", "repo_id", "path", "common_dir", "remote_url"},
        {"branch"},
    ),
}


def installed(profile: Json) -> tuple[str, str]:
    """One exact program, or a provider version directory that survives upgrades."""
    if ("executable" in profile) == ("executable_root" in profile):
        raise ValueError("Non-Pi profile requires one installed executable or version root")
    key = "executable" if "executable" in profile else "executable_root"
    path = Path(profile[key])
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Non-Pi profile requires the actual installed executable")
    if key == "executable" and not path.is_file():
        raise ValueError("Non-Pi profile requires the actual installed executable")
    if key == "executable_root" and (
        not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022
    ):
        raise ValueError("Native version root must be a protected account directory")
    return key, str(path)


def _pairs(rows: list[tuple[str, Any]]) -> Json:
    value: Json = {}
    for key, item in rows:
        if key in value:
            raise ValueError("Duplicate policy member")
        value[key] = item
    return value


def _finite(value: str) -> None:
    raise ValueError("Nonfinite policy value")


def load(path: Path) -> Json:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Expected a bounded regular policy file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Policy exceeds its bound")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_finite)
    except RecursionError:
        raise ValueError("Policy nesting exceeds its bound") from None
    shape(value)
    return dict(value)


def _string(value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
        raise ValueError("Expected a bounded nonempty policy string")


def shape(policy: Json) -> None:
    if (
        not isinstance(policy, dict)
        or not REQUIRED <= policy.keys()
        or policy.keys() - REQUIRED - OPTIONAL
    ):
        raise ValueError("Missing or unsupported policy field")
    production = policy.get("production_ledger")
    if production is None:
        if "database" not in policy:
            raise ValueError("An isolated fact store requires database")
    elif "database" in policy or "legacy_catalog" in policy:
        raise ValueError(
            "Production uses its one registered ledger, without a second database or transfer catalog"
        )
    elif not isinstance(production, dict) or set(production) != {
        "repository",
        "remote",
        "ref",
        "identity",
    }:
        raise ValueError("Production ledger binding is incomplete")
    else:
        for value in production.values():
            _string(value)
    if "historical_kernel_root" in policy and production is None:
        raise ValueError("Original production execution requires the production Git authority")
    if len(encoded(policy)) > MAX_BYTES:
        raise ValueError("Policy exceeds its bound")
    for key, value in policy.items():
        if key not in GROUPS and key not in {
            "production_ledger",
            "execution",
            "verification_compute",
            "notification_transport",
            "notification_routes",
            "external_entries",
            "pull_request_gate",
            "pi_codex_oauth",
        }:
            _string(value)
    if "external_entries" in policy:
        from .external_entry import shape as entry_shape

        if "human_inputs" not in policy:
            raise ValueError("External entries require Host human input custody")
        entry_shape(policy["external_entries"])
    from .notice_transport import configured_routes

    configured_routes(policy)
    execution(policy)
    verification_compute(policy)
    if "pull_request_gate" in policy:
        from .github_merge import shape as merge_shape

        merge_shape(policy["pull_request_gate"])
    if "pi_codex_oauth" in policy:
        binding = policy["pi_codex_oauth"]
        if production is None or not isinstance(binding, dict) or set(binding) != {"credential_file", "codex"}:
            raise ValueError("Codex OAuth requires its registered production installation")
        for value in binding.values():
            _string(value)
            if not Path(value).is_absolute():
                raise ValueError("Codex OAuth binding requires absolute paths")
        if Path(binding["credential_file"]).name != "auth.json":
            raise ValueError("Codex OAuth binding requires the original auth.json")
    for key in ("namespace", "surface", "verification_actor"):
        identity(policy[key])
    for name, (required, optional) in GROUPS.items():
        rows = policy.get(name, [])
        # One Host carries about four producer profiles per Relay tenant and runtime.
        if not isinstance(rows, list) or len(rows) > 512:
            raise ValueError("Policy rows exceed their bound or have an invalid type")
        for row in rows:
            if (
                not isinstance(row, dict)
                or not required <= row.keys()
                or row.keys() - required - optional
            ):
                raise ValueError("Missing or unsupported nested policy field")
            for key, value in row.items():
                if key in {"artifact_roots", "workspace_roots", "original_workspace_roots", "relay_tenants"}:
                    if not isinstance(value, list) or not 1 <= len(value) <= 128:
                        raise ValueError("Artifact roots require a bounded nonempty list")
                    for item in value:
                        _string(item)
                    if len(set(value)) != len(value):
                        raise ValueError("Duplicate artifact root")
                elif name == "profiles" and key == "standalone":
                    if value is not True:
                        raise ValueError("Standalone profile must be explicitly enabled")
                elif not (name == "repositories" and key == "remote_url" and value is None):
                    _string(value)
    if "legacy_catalog" in policy:
        catalog = Path(policy["legacy_catalog"])
        if not catalog.is_absolute() or catalog.resolve() != catalog:
            raise ValueError("Legacy catalog requires an actual absolute file")
    profiles = policy["profiles"]
    if not profiles:
        raise ValueError("Native profile actors are missing")
    # One actor may have distinct TG and Feishu transcripts. Identity is bound
    # by the non-overlapping physical log roots, not by a channel-owned label.
    for profile in profiles:
        if profile["role"] == "producer" and profile["actor"] == policy["verification_actor"]:
            raise ValueError("Producer and independent verifier actors overlap")
    roots: list[tuple[Path, Json]] = []
    for profile in profiles:
        identity(profile["actor"])
        runtime = profile.get("runtime", "pi")
        if runtime not in {"pi", "cc", "codex"}:
            raise ValueError("Unsupported native runtime")
        if "original_workspace_roots" in profile:
            if runtime != "codex" or not profile.get("workspace_roots"):
                raise ValueError("Codex original roots require a registered current workspace")
            for value in profile["original_workspace_roots"]:
                path = Path(value)
                if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts or str(path) != value:
                    raise ValueError("Invalid Codex original workspace path")
        if runtime != "pi":
            installed(profile)
        if profile["role"] not in {"producer", "verifier"}:
            raise ValueError("Unknown profile role")
        path = Path(profile["session_root"])
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("Native session root redirects")
        for key in ("workspace_roots",):
            for item in profile.get(key, []):
                workspace = Path(item)
                if not workspace.is_absolute() or workspace.resolve() != workspace:
                    raise ValueError("Native workspace roots redirect")
        if profile.get("binding_root"):
            binding = Path(profile["binding_root"])
            if not binding.is_absolute() or binding.resolve() != binding:
                raise ValueError("Native session binding root redirects")
        if "standalone" in profile and (
            profile["standalone"] is not True
            or runtime != "pi"
            or not profile.get("binding_root")
            or not profile.get("workspace_roots")
            or profile.get("relay_tenants")
        ):
            raise ValueError("Standalone Pi requires its own original source and workspace binding")
        if profile.get("relay_tenants") and not profile.get("binding_root"):
            raise ValueError("Relay-specific profile requires physical process proof")
        for other, prior in roots:
            if runtime != prior.get("runtime", "pi") or (
                runtime != "pi" and installed(profile) != installed(prior)
            ):
                continue
            if path == other or path in other.parents or other in path.parents:
                tenants, before_tenants = profile.get("relay_tenants"), prior.get("relay_tenants")
                if tenants and before_tenants and set(tenants).isdisjoint(before_tenants):
                    continue
                if bool(tenants) != bool(before_tenants):
                    # Relay-bound and standalone profiles cannot claim each other.
                    continue
                scopes = profile.get("workspace_roots", [])
                before = prior.get("workspace_roots", [])
                if (
                    (
                        runtime == "pi"
                        and not (profile.get("standalone") and prior.get("standalone"))
                    )
                    or not scopes
                    or not before
                    or any(
                        Path(a) == Path(b)
                        or Path(a) in Path(b).parents
                        or Path(b) in Path(a).parents
                        for a in scopes
                        for b in before
                    )
                ):
                    raise ValueError("Native session profiles overlap")
        roots.append((path, profile))
    verifiers = [
        p
        for p in profiles
        if p["role"] == "verifier" and p["actor"] == policy["verification_actor"]
    ]
    if len(verifiers) != 1 or verifiers[0]["session_root"] != policy["dispatch_root"]:
        raise ValueError("Verifier dispatch profile mismatch")


def preflight(policy: Json, root: Path) -> tuple[Path, ...]:
    shape(policy)
    production = policy.get("production_ledger") is not None
    if production:
        from .production_ledger import ProductionLedger

        ProductionLedger(policy["production_ledger"], policy["namespace"])
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Installation root must be an existing real directory")
    if policy.get("git_config_file"):
        config = Path(policy["git_config_file"])
        if (
            not config.is_absolute()
            or config.resolve() != config
            or not config.is_file()
            or config.stat().st_mode & 0o077
            or config.stat().st_uid != os.getuid()
        ):
            raise ValueError("Git authentication configuration must be an owned private file")
    directories: list[Path] = []

    def directory(path: Path) -> None:
        if any(p.exists() and not p.is_dir() for p in (path, *path.parents)):
            raise ValueError("Managed directory has an incompatible existing path")
        if path not in directories:
            directories.append(path)

    # The installer and review use the same isolated physical boundary. Operator repository bindings
    # cannot turn a lab upgrade into access to a production/source checkout.
    repository_ids: set[str] = set()
    from dataclasses import asdict
    from .model import RepositoryBinding, identity
    from .git_workspaces import GitWorkspaces

    for row in policy.get("repositories", []):
        binding = RepositoryBinding(**row)
        identity(binding.repo_id)
        if binding.repo_id in repository_ids:
            raise ValueError("Duplicate repository binding")
        repository_ids.add(binding.repo_id)
        for value in (binding.source, binding.common_dir, binding.managed_root):
            path = Path(value)
            if (
                path.resolve() != path
                or not path.is_absolute()
                or (not production and root not in path.parents)
            ):
                raise ValueError("Repository scope is outside isolated installation")
        try:
            GitWorkspaces().layout({"binding": asdict(binding), "target": "policy-validation"})
        except OSError as exc:
            raise ValueError("Repository binding is not physically available") from exc
    from .model import PublicationTarget, SourceConsumer, HostContext

    targets = tuple(PublicationTarget(**t) for t in policy.get("publication_targets", []))
    consumers = tuple(SourceConsumer(**c) for c in policy.get("source_consumers", []))
    HostContext(
        "installer",
        "installer",
        "installer",
        "installer",
        0,
        frozenset(),
        frozenset(),
        publication_targets=targets,
        source_consumers=consumers,
    ).validate()
    publications: list[PublicationTarget | SourceConsumer] = [*targets, *consumers]
    for publication_binding in publications:
        if publication_binding.repo_id not in repository_ids:
            raise ValueError("Publication repository is not admitted")
        # Production targets must match the exact operator-registered repository.
        if production:
            repository = next(
                r
                for r in policy.get("repositories", [])
                if r["repo_id"] == publication_binding.repo_id
            )
            if publication_binding.remote_url != repository_values(repository).get("remote_url"):
                raise ValueError("Publication target differs from its registered repository")
            continue
        remote = Path(publication_binding.remote_url)
        if (
            remote.resolve() != remote
            or (not production and root not in remote.parents)
            or not remote.is_dir()
        ):
            raise ValueError("Publication remote is outside isolated installation")
    for consumer in consumers:
        for value in (consumer.path, consumer.common_dir):
            path = Path(value)
            if (
                path.resolve() != path
                or not path.is_absolute()
                or (not production and root not in path.parents)
                or not path.is_dir()
            ):
                raise ValueError("Source consumer is outside isolated installation")
    if targets or consumers:
        publication_root = Path(policy["publication_root"])
        if publication_root.resolve() != publication_root or root not in publication_root.parents:
            raise ValueError("Publication evidence is outside isolated installation")
        directory(publication_root)
    if policy.get("notification_transport"):
        receipt_root = Path(policy["notification_transport"]["receipts_root"])
        if receipt_root.resolve() != receipt_root or root not in receipt_root.parents:
            raise ValueError("Notification receipts are outside isolated installation")
        directory(receipt_root)
    # Only the explicit isolated installation root receives mutable new state.
    for key in (
        ("views_root", "delivery_root", "dispatch_root")
        if production
        else ("database", "views_root", "delivery_root", "dispatch_root")
    ):
        path = Path(policy[key])
        if path.resolve() != path or root not in path.parents:
            raise ValueError("State scope is outside installation root")
        if key == "database" and path.exists() and not path.is_file():
            raise ValueError("Database path is not a regular file")
        directory(path.parent if key == "database" else path)
    profiles = policy["profiles"]
    verifiers = [
        p
        for p in profiles
        if p["role"] == "verifier" and p["actor"] == policy["verification_actor"]
    ]
    if len(verifiers) != 1 or Path(verifiers[0]["session_root"]) != Path(policy["dispatch_root"]):
        raise ValueError("Verifier dispatch profile mismatch")
    for profile in profiles:
        if profile["role"] not in {"producer", "verifier"}:
            raise ValueError("Unknown profile role")
        if not Path(profile["session_root"]).is_dir() and not (
            profile["role"] == "verifier"
            and Path(profile["session_root"]) == Path(policy["dispatch_root"])
        ):
            raise ValueError("Native session root unavailable")
        for artifact_root in profile["artifact_roots"]:
            path = Path(artifact_root)
            if (
                path.resolve() != path
                or not path.is_absolute()
                or (not production and root not in path.parents)
            ):
                raise ValueError("Artifact scope is outside isolated installation")
            directory(path)
    for key in ("python", "node", "pi_binary"):
        if not Path(policy[key]).is_file():
            raise ValueError("Existing runtime executable unavailable")
    return tuple(directories)


def migration_fields(previous: Json, proposed: Json) -> list[str]:
    return [key for key in BOUND if key in previous and previous[key] != proposed.get(key)]


def compatible(previous: Json, proposed: Json, *, relay_release: Path | None = None) -> Json | None:
    if relay_release is not None:
        from .relay_admission import inspect

        return inspect(previous, proposed, relay_release)
    if migration_fields(previous, proposed):
        raise ValueError("Installation identity or physical endpoint changed; use migration")
    return None


def describe(policy: Json, *, validation: str = "installed_policy") -> Json:
    shape(policy)
    return {
        "schema": "harness.policy-review/v1",
        "read_only": True,
        "execution_authority": False,
        "validation": validation,
        "policy_sha256": digest(policy),
        "execution": execution(policy),
        "verification_compute": verification_compute(policy),
        "sections": {key: digest(value) for key, value in sorted(policy.items())},
        "counts": {key: len(policy.get(key, [])) for key in (*GROUPS, "notification_routes")},
        "repositories": repository_values(policy.get("repositories", [])),
        "publication_targets": repository_values(policy.get("publication_targets", [])),
        "roles": {
            role: sum(p["role"] == role for p in policy["profiles"])
            for role in ("producer", "verifier")
        },
    }


def review(
    root: Path,
    proposed: Path,
    previous: Path | None = None,
    *,
    relay_release: Path | None = None,
) -> Json:
    policy = load(proposed)
    preflight(policy, root)
    result = describe(policy, validation="current_preflight")
    if previous is not None:
        before = load(previous)
        preflight(before, root)
        changed = sorted(
            key
            for key in set(before) | set(policy)
            if (key in before) != (key in policy) or before.get(key) != policy.get(key)
        )
        adoption = (
            compatible(before, policy, relay_release=relay_release)
            if relay_release is not None
            else None
        )
        result["comparison"] = {
            "previous_policy_sha256": digest(before),
            "changed_sections": changed,
            "requires_migration": ([] if adoption else migration_fields(before, policy)),
            "relay_adoption": adoption,
            "operator_review_required": bool(changed),
            "permission_direction": "unchanged" if not changed else "not_inferred",
        }
    return result


def execution(policy: Json) -> Json:
    value = policy.get("execution", {"mode": "manual", "batch_budget": 8})
    if (
        not isinstance(value, dict)
        or set(value) != {"mode", "batch_budget"}
        or not isinstance(value["mode"], str)
        or value["mode"] not in {"manual", "audit", "automatic"}
        or type(value["batch_budget"]) is not int
        or not 1 <= value["batch_budget"] <= 64
    ):
        raise ValueError("Invalid explicit execution policy")
    return dict(value)


def verification_compute(policy: Json) -> Json:
    value = policy.get(
        "verification_compute",
        {
            "provider": "openai-codex",
            "model": "gpt-6.1-sol",
            "thinking": "high",
            "tools": ["read", "bash", "edit", "write"],
        },
    )
    if not isinstance(value, dict) or set(value) != {
        "provider",
        "model",
        "thinking",
        "tools",
    }:
        raise ValueError("Invalid verifier computation policy")
    for key in ("provider", "model"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", value[key]
        ):
            raise ValueError("Invalid exact verifier model identifier")
    if not isinstance(value["thinking"], str) or value["thinking"] not in {
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise ValueError("Unsupported verifier thinking level")
    names = value["tools"]
    if (
        not isinstance(names, list)
        or not 1 <= len(names) <= 8
        or any(
            not isinstance(n, str)
            or n not in {"read", "bash", "powershell", "edit", "write", "grep", "find", "ls"}
            for n in names
        )
        or len(set(names)) != len(names)
    ):
        raise ValueError("Invalid explicit verifier tools")
    return {**value, "tools": list(names)}
