"""Static registry diagnostics: one read-only decision boundary, no WI authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Callable, Mapping

MAX_BYTES = 2 * 1024 * 1024
Json = dict[str, Any]


class ConfigurationError(ValueError):
    """Invalid diagnostic configuration, distinct from observed drift."""


@dataclass(frozen=True)
class Projection:
    target: str
    method: str


@dataclass(frozen=True)
class Entry:
    path: str
    kind: str
    upstream: tuple[str, ...] = ()
    projections: tuple[Projection, ...] = ()


@dataclass(frozen=True)
class Agent:
    workspace: str
    identity: str
    depends_on: tuple[str, ...] = ()
    policy_path: str | None = None
    retired: bool = False


@dataclass(frozen=True)
class Registry:
    schema: str
    entries: Mapping[str, Entry]
    agents: Mapping[str, Agent]


def validate_dependencies(
    entries: Mapping[str, tuple[str, ...]], agents: Mapping[str, tuple[str, ...]]
) -> None:
    """Validate the entire bounded graph without recursive traversal."""
    if len(entries) + len(agents) > 10000:
        raise ConfigurationError("Registry entity limit exceeded")
    if sum(map(len, entries.values())) + sum(map(len, agents.values())) > 50000:
        raise ConfigurationError("Registry dependency limit exceeded")
    for family, objects in (("entries", entries), ("agents", agents)):
        for name, dependencies in objects.items():
            if not isinstance(name, str) or not name.strip():
                raise ConfigurationError("Registry identifiers must be nonempty strings")
            if len(set(dependencies)) != len(dependencies):
                raise ConfigurationError(f"Duplicate dependency in {family}.{name}")
            for dependency in dependencies:
                if dependency not in entries:
                    raise ConfigurationError(
                        f"{family}.{name} references unknown entry '{dependency}'"
                    )
    pending = {name: len(deps) for name, deps in entries.items()}
    downstream: dict[str, list[str]] = {name: [] for name in entries}
    for name, deps in entries.items():
        for dep in deps:
            downstream[dep].append(name)
    ready = [name for name, count in pending.items() if count == 0]
    visited = 0
    while ready:
        name = ready.pop()
        visited += 1
        for child in downstream[name]:
            pending[child] -= 1
            if pending[child] == 0:
                ready.append(child)
    if visited != len(entries):
        raise ConfigurationError("Circular dependency detected")


def _path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value.strip() or path.is_absolute() or ".." in path.parts
        or "\\" in value or "\x00" in value
    ):
        raise ConfigurationError("Registry paths must be safe relative paths")


def _selection(
    registry: Registry, agent: str | None, entry: str | None, mode: str
) -> tuple[list[str], list[str], str]:
    if registry.schema not in {"1.0", "2.0", "2.3.0"}:
        raise ConfigurationError("Unsupported registry schema")
    if mode not in {"quick", "full"}:
        raise ConfigurationError("Unknown check mode")
    if agent is not None and entry is not None:
        raise ConfigurationError("Agent and entry selectors are mutually exclusive")
    validate_dependencies(
        {key: value.upstream for key, value in registry.entries.items()},
        {key: value.depends_on for key, value in registry.agents.items()},
    )
    edges = sum(len(row.projections) + len(row.upstream) for row in registry.entries.values())
    edges += sum(len(agent.depends_on) for agent in registry.agents.values())
    if edges > 50000:
        raise ConfigurationError("Registry projection limit exceeded")
    for item in registry.entries.values():
        _path(item.path)
        if item.kind not in {"document", "package"}:
            raise ConfigurationError("Unknown entry kind")
        for projection in item.projections:
            _path(projection.target)
            if projection.method not in {"symlink", "reference", "embed"}:
                raise ConfigurationError("Unknown projection method")
    for row in registry.agents.values():
        _path(row.workspace)
        _path(row.identity)
        if row.policy_path is not None:
            _path(row.policy_path)
    if agent is not None:
        if agent not in registry.agents:
            raise ConfigurationError(f"Agent '{agent}' not found in registry")
        selected_agents = [agent]
        queue = list(registry.agents[agent].depends_on)
        scope = "agent:" + agent
    elif entry is not None:
        if entry not in registry.entries:
            raise ConfigurationError(f"Entry '{entry}' not found in registry")
        selected_agents, queue, scope = [], [entry], "entry:" + entry
    else:
        selected_agents = sorted(registry.agents)
        queue, scope = list(registry.entries), "all"
    selected: set[str] = set()
    while queue:
        key = queue.pop()
        if key not in selected:
            selected.add(key)
            queue.extend(registry.entries[key].upstream)
    return sorted(selected), selected_agents, scope


@dataclass(frozen=True)
class _File:
    state: str
    reason: str
    kind: str = ""
    size: int | None = None
    sha256: str | None = None
    resolved: Path | None = None
    symlink: bool = False
    content: bytes | None = None


class _Reader:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ConfigurationError("Repository root must be absolute")
        try:
            self.root = root.resolve()
        except (OSError, RuntimeError) as exc:
            raise ConfigurationError("Repository root cannot be resolved") from exc

    def observe(self, relative: str, *, content: bool = False) -> _File:
        """Only open in-root resolved paths; never follow swapped parent links."""
        descriptors: list[int] = []
        try:
            path = self.root / relative
            resolved = path.resolve()
            if not resolved.is_relative_to(self.root):
                return _File("outside", "path_outside_root")
            symlink = stat.S_ISLNK(path.lstat().st_mode)
            parts = resolved.relative_to(self.root).parts
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            descriptors.append(os.open(self.root, flags | os.O_DIRECTORY))
            for part in parts[:-1]:
                descriptors.append(
                    os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptors[-1])
                )
            if parts:
                descriptors.append(os.open(parts[-1], flags, dir_fd=descriptors[-1]))
            fd = descriptors[-1]
            before = os.fstat(fd)
            kind = (
                "file" if stat.S_ISREG(before.st_mode)
                else "directory" if stat.S_ISDIR(before.st_mode) else "special"
            )
            if not content or kind != "file":
                return _File(
                    "present", "path_observed", kind, before.st_size,
                    resolved=resolved, symlink=symlink,
                )
            if before.st_size > MAX_BYTES:
                return _File("unknown", "content_size_limit")
            chunks: list[bytes] = []
            count = 0
            while count <= MAX_BYTES:
                chunk = os.read(fd, min(65536, MAX_BYTES + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
            if count > MAX_BYTES:
                return _File("unknown", "content_size_limit")
            after = os.fstat(fd)
            def stamp(info: os.stat_result) -> tuple[int, int, int, int, int]:
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if stamp(before) != stamp(after) or count != before.st_size:
                return _File("unknown", "content_changed_during_read")
            data = b"".join(chunks)
            return _File(
                "present", "content_observed", kind, count,
                hashlib.sha256(data).hexdigest(), resolved, symlink, data,
            )
        except (FileNotFoundError, NotADirectoryError):
            return _File("absent", "path_missing")
        except (OSError, RuntimeError, ValueError):
            return _File("unknown", "path_observation_unavailable")
        finally:
            for fd in reversed(descriptors):
                os.close(fd)


def _file_status(file: _File, kind: str, *, nonempty: bool = False) -> tuple[str, str]:
    if file.state == "unknown":
        return "unknown", file.reason
    if file.state != "present":
        return "violated", file.reason
    if file.kind != kind:
        return "violated", "wrong_path_kind"
    if nonempty and file.size == 0:
        return "violated", "file_empty"
    return "passed", "path_observed"


def inspect_registry(
    registry: Registry,
    root: Path,
    *,
    agent: str | None = None,
    entry: str | None = None,
    mode: str = "full",
    registry_path: str = "",
    registry_sha256: str | None = None,
    decode_policy: Callable[[bytes], object] | None = None,
) -> Json:
    """Inspect a static snapshot; the report cannot authorize lifecycle actions."""
    entries, agents, scope = _selection(registry, agent, entry, mode)
    reader = _Reader(root)
    observations: list[Json] = []

    def record(
        check: str, target: str, result: tuple[str, str], severity: str,
        files: tuple[_File, ...] = (),
    ) -> None:
        status, reason = result
        proof = [
            {key: value for key, value in
             (("state", file.state), ("bytes", file.size), ("sha256", file.sha256))
             if value is not None}
            for file in files
        ]
        observations.append({
            "check_id": check, "check_layer": "content" if check.startswith("C") else "static",
            "target": target, "status": status, "reason": reason,
            "severity": "major" if status == "unknown" else severity,
            "message": reason, "evidence": json.dumps(proof, sort_keys=True),
            "suggestion": "Review the declared source and observation before changing configuration",
        })

    for key in entries:
        item = registry.entries[key]
        source = reader.observe(item.path)
        record("S1", item.path, _file_status(
            source, "directory" if item.kind == "package" else "file",
            nonempty=item.kind == "document",
        ), "critical", (source,))
        for projection in item.projections:
            target = reader.observe(projection.target)
            expected_kind = (
                "directory" if projection.method == "symlink" and item.kind == "package"
                else "file"
            )
            record("S5", projection.target, _file_status(target, expected_kind),
                   "major", (target,))
            if mode == "quick":
                continue
            if projection.method == "symlink":
                result = _file_status(target, expected_kind)
                if result[0] == "passed":
                    if not target.symlink:
                        result = "violated", "projection_not_symlink"
                    elif target.resolved != source.resolved:
                        result = "violated", "symlink_wrong_source"
                    else:
                        result = _file_status(
                            source, "directory" if item.kind == "package" else "file"
                        )
                record("C1", projection.target, result, "major", (source, target))
            else:
                if item.kind == "package":
                    record("C2", projection.target, ("unknown", "unsupported_package_comparison"),
                           "major")
                    continue
                left = reader.observe(item.path, content=True)
                right = reader.observe(projection.target, content=True)
                statuses = [_file_status(f, "file") for f in (left, right)]
                result = next(
                    (r for r in statuses if r[0] == "violated"),
                    next((r for r in statuses if r[0] == "unknown"), ("passed", "content_equal")),
                )
                if result[0] == "passed" and left.sha256 != right.sha256:
                    result = "violated", "content_mismatch"
                record("C2", projection.target, result, "major", (left, right))
    for key in agents:
        row = registry.agents[key]
        workspace, identity = reader.observe(row.workspace), reader.observe(row.identity)
        record("S2", row.workspace, _file_status(workspace, "directory"),
               "critical", (workspace,))
        record("S3", row.identity, _file_status(identity, "file", nonempty=True),
               "critical", (identity,))
        if row.policy_path is None:
            continue
        policy = reader.observe(row.policy_path, content=True)
        result = _file_status(policy, "file")
        severity = "major"
        if result[0] == "passed":
            if decode_policy is None:
                result = "unknown", "policy_decoder_unavailable"
            else:
                try:
                    data = decode_policy(policy.content or b"")
                    if not isinstance(data, dict) or not isinstance(data.get("agent_id"), str):
                        result = "unknown", "policy_identity_unavailable"
                    elif data["agent_id"] != key:
                        result, severity = ("violated", "agent_identity_mismatch"), "critical"
                    elif row.retired:
                        result, severity = ("violated", "retired_agent_has_policy"), "minor"
                    else:
                        result = "passed", "policy_identity_matches"
                except Exception:
                    result = "unknown", "policy_decode_failed"
        record("S6", key, result, severity, (policy,))
    findings = [row for row in observations if row["status"] != "passed"]
    violations = [row for row in observations if row["status"] == "violated"]
    counts = {s: sum(row["status"] == s for row in observations)
              for s in ("passed", "violated", "unknown")}
    verdict = (
        "fail" if any(row["severity"] == "critical" for row in violations)
        else "pass_with_findings" if violations
        else "unknown" if counts["unknown"] else "pass"
    )
    snapshot = {
        "schema": registry.schema, "observed_root": str(reader.root),
        "entries": {k: asdict(v) for k, v in registry.entries.items()},
        "agents": {k: asdict(v) for k, v in registry.agents.items()},
    }
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "registry_path": registry_path, "registry_sha256": registry_sha256,
        "diagnostic_snapshot_sha256": hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "scope": scope, "mode": mode, "findings": findings, "verdict": verdict,
        "summary": {"total": len(findings), **{
            s: sum(row["severity"] == s for row in findings)
            for s in ("critical", "major", "minor")
        }, "unknown": counts["unknown"]},
        "observations": observations, "counts": counts,
        "selected_entries": entries, "selected_agents": agents,
        "skipped_checks": ["C1", "C2"] if mode == "quick" else [],
        "unchecked_dimensions": [
            "owner_bindings", "repository_bindings", "channel_bindings",
            "external_surface_registry", "runtime_liveness", "policy_actions", "governance_prose",
        ],
        "temporal_scope": "current_filesystem_observation", "human_authority": False,
    }
