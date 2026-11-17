"""Read-only proof for adopting an already activated Muse Relay release."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from .model import Json

PROC = Path("/proc")
ROOTS = ("Scripts", "config", "ComputeCost", "Stats/exporter")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError("Relay release adoption: " + message)


def _bytes(path: Path) -> bytes:
    _require(
        path.resolve(strict=True) == path and path.is_file(),
        "redirected or missing evidence",
    )
    _require(path.stat().st_size <= 16 * 1024 * 1024, "oversized evidence")
    return path.read_bytes()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(path: Path) -> tuple[Json, str]:
    data = _bytes(path)
    value = json.loads(data)
    _require(isinstance(value, dict), "invalid evidence object")
    return value, _sha(data)


def _inventory(release: Path, roots: tuple[str, ...], expected: object) -> None:
    _require(isinstance(expected, dict) and bool(expected), "missing asset inventory")
    actual = {}
    for root_name in roots:
        root = release / root_name
        _require(
            root.resolve(strict=True) == root and root.is_dir(),
            "redirected or missing asset root",
        )
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(release)
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            _require(not path.is_symlink(), "redirected asset")
            if path.is_file():
                actual[str(relative)] = _sha(_bytes(path))
    _require(actual == expected, "asset inventory differs")


def _root_move_only(previous: object, proposed: object) -> bool:
    """Same repositories in the same order; only where new workspaces go changed.

    Earlier workspaces keep their snapshotted binding (workspaces.same_repository).
    """
    if not isinstance(previous, list) or not isinstance(proposed, list):
        return False
    if len(previous) != len(proposed):
        return False
    for old, new in zip(previous, proposed):
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        if {k: v for k, v in old.items() if k != "managed_root"} != {
            k: v for k, v in new.items() if k != "managed_root"
        }:
            return False
    return True


# The kernel merges reviewed PRs only in these repositories (human decision, WI422, 2026-10-26).
GATE_REPOSITORIES = frozenset(
    {"bigbird-online/PULSE", "bigbird-online/CollaborateWithAI", "bigbird-online/Oikos"}
)


def _gate_follows_release(gate: object, release: Path) -> bool:
    """The gate's pulse-gh moves with each release; nothing else may widen."""
    if gate is None:
        return True
    if not isinstance(gate, dict) or set(gate) != {
        "pulse_gh", "gh", "relay_paths", "repositories", "merge_method"
    }:
        return False
    repositories = gate["repositories"]
    return (
        gate["pulse_gh"] == str(release / "Scripts/pulse-gh.py")
        and all(isinstance(gate[k], str) and Path(gate[k]).is_absolute() for k in ("gh", "relay_paths"))
        and isinstance(repositories, list) and bool(repositories)
        and len(set(repositories)) == len(repositories)
        and set(repositories) <= GATE_REPOSITORIES
        and gate["merge_method"] == "merge"
    )


def inspect(previous: Json, proposed: Json, release: Path) -> Json:
    """No writes, process actions, business identity or permission inference."""
    changed = {
        key
        for key in previous.keys() | proposed.keys()
        if previous.get(key) != proposed.get(key)
    }
    if "historical_kernel_root" in changed:
        expected = Path(str(previous.get("human_inputs", ""))).parent.parent / "scl-kernel"
        _require("historical_kernel_root" not in previous
                 and proposed.get("historical_kernel_root") == str(expected)
                 and expected.is_absolute() and expected.resolve() == expected and expected.is_dir(),
                 "original execution source must be the existing account kernel")
    if "repositories" in changed:
        _require(
            _root_move_only(previous.get("repositories"), proposed.get("repositories")),
            "repository change exceeds a managed root move",
        )
    _require(
        # External entries open with the platform bot; the policy shape and the
        # core still require owner confirmation for every binding (AH-084).
        changed <= {"relay_entry", "producer_node", "historical_kernel_root", "profiles",
                    "external_entries", "repositories", "pull_request_gate", "pi_codex_oauth"},
        "policy change exceeds the Relay entry and existing canonical Node",
    )
    if "pull_request_gate" in changed:
        _require(_gate_follows_release(proposed.get("pull_request_gate"), release),
                 "pull request gate must use this release's pulse-gh and the admitted repositories")
    if "producer_node" in changed:
        _require(
            isinstance(previous.get("node"), str)
            and proposed.get("producer_node") == previous["node"],
            "producer Node change exceeds the already admitted canonical toolchain",
        )
    _require(
        release.is_absolute() and release.resolve(strict=True) == release,
        "release must be a physical absolute directory",
    )
    name = re.fullmatch(r"PULSE-([0-9a-f]{40})", release.name)
    _require(
        name is not None and release.parent.name == "releases",
        "not an exact managed release",
    )
    sha = release.name[6:]
    pulse = release.parent.parent
    workspace = pulse.parent
    entry = release / "Relay/dist/cli.js"
    _require(proposed.get("relay_entry") == str(entry), "proposed entry differs")
    old_entry = Path(str(previous.get("relay_entry", "")))
    _require(
        old_entry.is_absolute() and old_entry.parts[-3:] == ("Relay", "dist", "cli.js"),
        "previous entry is not a managed CLI",
    )
    old_release = old_entry.parents[2]
    _require(
        old_release.parent == release.parent
        and re.fullmatch(r"PULSE-[0-9a-f]{40}", old_release.name) is not None,
        "previous release belongs to another root",
    )
    _bytes(old_entry)
    prepared = pulse / f".local/state/muse-cd/integration-{sha}.json"
    managed = release / ".pulse-release-manifest.json"
    if "pi_codex_oauth" in changed:
        _require(
            managed.is_file() and not prepared.exists()
            and proposed.get("namespace") == "pulse-production"
            and proposed.get("surface") == "surface_v1_cloud_linux_c"
            and "production_ledger" in proposed
            and "pi_codex_oauth" not in previous
            and proposed.get("pi_codex_oauth") == {
                "credential_file": str(pulse / ".codex/auth.json"),
                "codex": "/usr/bin/codex",
            },
            "Codex OAuth requires the original Oracle account and managed release",
        )
        for resource_name in ("pi-codex-oauth.mjs", "pi-codex-oauth.py"):
            _bytes(release / "Relay/runtime" / resource_name)
    if prepared.exists():
        # The formal CD operator prepares this exact immutable pair before
        # stopping the predecessor. Readiness is not a claim of activation.
        info = prepared.stat()
        _require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                 "integration preparation must be private and account-owned")
        record, record_sha = _record(prepared)
        manifest, manifest_sha = _record(release / ".pulse-muse-release.json")
        _require(record.get("schema") == "pulse_muse_integration_preparation/v1"
                 and record.get("sha") == sha and record.get("release") == str(release)
                 and record.get("repository") == "bigbird-online/PULSE"
                 and record.get("ref") == "refs/heads/main"
                 and type(record.get("deployment_id")) is int
                 and record["deployment_id"] > 0
                 and type(record.get("verification_run_id")) is int
                 and record["verification_run_id"] > 0
                 and old_release in {Path(str(record.get("previous_release", ""))), release}
                 and record.get("manifest_sha256") == manifest_sha,
                 "no exact-main integration preparation")
        _require(manifest.get("schema") == "pulse_muse_release/v1"
                 and manifest.get("sha") == sha
                 and manifest.get("cli_sha256") == _sha(_bytes(entry)),
                 "prepared release bytes differ")
        if "profiles" in changed:
            from .model import digest
            _require(record.get("host_policy_sha256") == digest(proposed), "Host profiles differ from the exact CD preparation")
            consumer = record.get("consumer_config", {})
            _require(isinstance(consumer.get("path"), str) and _sha(_bytes(Path(consumer["path"]))) == consumer.get("sha256"),
                     "Host consumer configuration changed")
        _inventory(release, ROOTS, manifest.get("runtime_assets_sha256"))
        _inventory(release, ("Relay/dist",), manifest.get("relay_build_sha256"))
        return {"schema": "harness.relay-release-adoption/v1", "read_only": True,
                "execution_authority": False, "release": str(release),
                "previous_release": record["previous_release"], "relay_entry": str(entry),
                "manifest_sha256": manifest_sha, "preparation_sha256": record_sha}
    if managed.exists():
        manifest, manifest_sha = _record(managed)
        _require(manifest.get("schema") == "pulse_relay_immutable_release/v1"
                 and manifest.get("build_provenance") == "exact_git_archive"
                 and manifest.get("source_sha") == sha
                 and manifest.get("relay_cli_sha256") == _sha(_bytes(entry)),
                 "managed writer release bytes differ")
        if changed & {"profiles", "pi_codex_oauth"}:
            from .model import digest
            admission_path = Path(str(previous["views_root"])).parent / "consumer-admissions" / (sha + ".json")
            _require(admission_path.exists(), "missing exact native consumer preparation")
            _require(admission_path.resolve(strict=True) == admission_path
                     and admission_path.stat().st_uid == os.getuid()
                     and not admission_path.stat().st_mode & 0o077,
                     "consumer preparation must be private and account-owned")
            admission, _ = _record(admission_path)
            _require(admission.get("schema") == "pulse_native_consumer_preparation/v1"
                     and admission.get("source_revision") == sha and admission.get("release") == str(release)
                     and admission.get("previous_policy_sha256") == digest(previous)
                     and admission.get("proposed_policy_sha256") == digest(proposed)
                     and admission.get("manifest_sha256") == manifest_sha,
                     "consumer preparation differs from the exact native cohort")
            _require(isinstance(admission.get("config_path"), str)
                     and _sha(_bytes(Path(admission["config_path"]))) == admission.get("config_sha256"),
                     "consumer source changed after preparation")
        capacity, capacity_sha = _record(Path(manifest["capacity_admission_receipt"]))
        _require(capacity_sha == manifest.get("capacity_admission_sha256")
                 and capacity.get("admission_allowed") is True,
                 "managed release lacks capacity admission")
        return {"schema": "harness.relay-release-adoption/v1", "read_only": True,
                "execution_authority": False, "release": str(release),
                "previous_release": str(old_release), "relay_entry": str(entry),
                "manifest_sha256": manifest_sha}
    _require("profiles" not in changed, "Host profile change exceeds the exact integrated CD preparation")
    _bytes(workspace / "bin/pulse-node.py")
    node, _ = _record(pulse / ".local/state/node-supervisor/config.json")
    receipt, receipt_sha = _record(
        pulse / f".local/state/muse-cd/experiment-{sha}.json"
    )
    latest, latest_sha = _record(pulse / ".local/state/muse-cd/latest.json")
    _require(
        node.get("purpose") == "experiment"
        and node.get("script") == str(workspace / "bin/pulse-node.py")
        and receipt.get("work_item_id") == node.get("experiment_work_item")
        and isinstance(receipt.get("work_item_id"), str)
        and re.fullmatch(r"wi_\d{8}_\d{3,}", receipt["work_item_id"]) is not None,
        "unregistered experiment selection",
    )
    _require(
        receipt == latest
        and receipt_sha == latest_sha
        and receipt.get("schema") == "pulse_muse_relay_experiment/v1"
        and receipt.get("state") == "activated"
        and receipt.get("sha") == sha
        and receipt.get("release") == str(release)
        and receipt.get("health_status") == "ok"
        and type(receipt.get("verification_run_id")) is int
        and receipt["verification_run_id"] > 0
        and isinstance(receipt.get("verification_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", receipt["verification_sha256"]) is not None,
        "no current successful formal experiment receipt",
    )
    predecessor = Path(str(receipt.get("previous_release", "")))
    _require(
        predecessor.parent == release.parent
        and re.fullmatch(r"PULSE-[0-9a-f]{40}", predecessor.name) is not None
        and old_release in {predecessor, release},
        "formal predecessor differs",
    )
    manifest, manifest_sha = _record(release / ".pulse-muse-release.json")
    _require(
        manifest.get("schema") == "pulse_muse_release/v1"
        and manifest.get("sha") == sha
        and manifest.get("cli_sha256") == _sha(_bytes(entry)),
        "release manifest or CLI differs",
    )
    _inventory(release, ROOTS, manifest.get("runtime_assets_sha256"))
    _inventory(release, ("Relay/dist",), manifest.get("relay_build_sha256"))
    launcher = _bytes(workspace / "bin/start-relay.sh").decode()
    selected = re.findall(r'^RELEASE="([^"]+)"$', launcher, flags=re.M)
    _require(selected == [str(release)], "launcher selection differs")
    pid_bytes = _bytes(pulse / ".local/share/pulse/data/relay/relay.pid")
    _require(re.fullmatch(rb"[1-9][0-9]*\s*", pid_bytes) is not None, "invalid PID")
    process = PROC / pid_bytes.decode().strip()
    arguments = (process / "cmdline").read_bytes().split(b"\0")
    _require(
        len(arguments) >= 2 and arguments[1].decode() == str(entry),
        "live process does not use the selected CLI",
    )
    _require(
        isinstance(proposed.get("producer_node"), str)
        and (process / "exe").resolve(strict=True)
        == Path(proposed["producer_node"]).resolve(strict=True),
        "live Node executable differs",
    )
    # Receipt hashes remain stable across a guardian's exact-release restart.
    # The live PID is verified above, not used as persistent native identity.
    return {
        "schema": "harness.relay-release-adoption/v1",
        "read_only": True,
        "execution_authority": False,
        "release": str(release),
        "previous_release": str(predecessor),
        "relay_entry": str(entry),
        "manifest_sha256": manifest_sha,
        "deployment_receipt_sha256": receipt_sha,
        "verification_run_id": receipt["verification_run_id"],
    }
