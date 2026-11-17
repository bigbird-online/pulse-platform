#!/usr/bin/env python3
"""Retire a fenced, no-longer-active Harness runtime after kernel migration.

This is a two-step local transaction. ``stage`` moves the exact historical
runtime, archived kernel, and its obsolete Host profiles into quarantine;
``purge`` removes that quarantine only after a separate live readback. Receipts
stay outside every retired tree. Neither operation changes the active kernel.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

class RetirementError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(regular_bytes(path))
    if not isinstance(value, dict):
        raise RetirementError(f"evidence is not an object: {path}")
    return value


def regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path != path.resolve(strict=True):
        raise RetirementError(f"evidence is unavailable or redirects: {path}")
    return path.read_bytes()


def physical_dir(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise RetirementError(f"directory is unavailable or redirects: {path}")
    if path != path.resolve(strict=True):
        raise RetirementError(f"directory ancestry redirects: {path}")


def active_readback(active_harness: Path, target_kernel: Path, legacy_state: Path) -> None:
    if not active_harness.is_symlink():
        raise RetirementError("active Harness is not a managed symlink")
    release = active_harness.resolve(strict=True)
    if release.is_relative_to(legacy_state) or (release / "pyvenv.cfg").exists():
        raise RetirementError("active Harness still uses a private legacy environment")
    if not (active_harness / "bin/harness").is_file():
        raise RetirementError("active Harness entry is unavailable")
    physical_dir(target_kernel)
    for name in ("active", "previous"):
        pointer = target_kernel / "runtime" / name
        if name == "previous" and not pointer.exists() and not pointer.is_symlink():
            continue
        if not pointer.is_symlink() or not (pointer / "bin/harness-scl").is_file():
            raise RetirementError(f"current SCL {name} executable is unavailable")
        outer = {}
        for line in (pointer.resolve(strict=True) / "release.txt").read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                outer[key] = value
        outer_path = Path(outer.get("outer_release", ""))
        if (not outer_path.is_absolute() or not outer_path.is_dir()
                or outer_path.is_relative_to(legacy_state)):
            raise RetirementError(f"current SCL {name} still depends on the legacy runtime")


def live_processes(*roots: Path) -> list[int]:
    try:
        import psutil
    except ModuleNotFoundError as exc:
        raise RetirementError("canonical Python cannot inspect live processes without psutil") from exc
    seen: list[int] = []
    own = {os.getpid(), os.getppid()}
    prefixes = tuple(str(root) + os.sep for root in roots)
    for process in psutil.process_iter(attrs=("pid", "uids", "exe", "cmdline", "cwd")):
        try:
            info = process.info
            if info["pid"] in own or info.get("uids") is None or info["uids"].real != os.geteuid():
                continue
            values = [info.get("exe") or "", info.get("cwd") or "", *(info.get("cmdline") or [])]
            if any(value == str(root) or value.startswith(prefix)
                   or f"={root}" in value or prefix in value
                   for value in values for root, prefix in zip(roots, prefixes)):
                seen.append(info["pid"])
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise RetirementError("cannot inspect an account process") from exc
    return sorted(set(seen))


def plan(*, legacy_state: Path, migration_intent: Path, profile_root: Path,
         active_harness: Path, quarantine: Path, receipt_dir: Path,
         root_work_item_id: str, authorize: str) -> dict:
    if not root_work_item_id or authorize != root_work_item_id:
        raise RetirementError("WorkItem retirement authorization differs")
    for path in (legacy_state, migration_intent, profile_root, active_harness,
                 quarantine, receipt_dir):
        if not path.is_absolute() or ".." in path.parts:
            raise RetirementError("retirement paths must be exact absolute paths")
    physical_dir(legacy_state)
    physical_dir(profile_root)
    if quarantine.exists() or quarantine.is_symlink():
        raise RetirementError("quarantine already exists")
    outer_intent = read_json(legacy_state / "baseline-apply/intent.json")
    outer_receipt_path = legacy_state / "receipts/first-new-mode-baseline.json"
    outer_receipt = read_json(outer_receipt_path)
    migration = read_json(migration_intent)
    migration_dir = migration_intent.parent
    archive = Path(migration["archive"])
    source = Path(migration["source_kernel"])
    target = Path(migration["target_kernel"])
    if any(quarantine.is_relative_to(root) or root.is_relative_to(quarantine)
           for root in (legacy_state, profile_root, archive, target, receipt_dir)):
        raise RetirementError("quarantine overlaps a source, current kernel, or receipt tree")
    physical_dir(quarantine.parent)
    if (archive != migration_dir / "source-kernel-archive"
            or Path(outer_intent["bindings"]["scl_kernel_root"]) != source
            or outer_receipt.get("status") != "applied"
            or outer_receipt.get("activation_mode") != "first_new_mode_baseline"
            or outer_receipt.get("state_dir") != str(legacy_state)
            or migration.get("retention_obligation") != "hold_all"
            or migration.get("authority_cutover") != "not_performed"
            or source.is_symlink() or not stat.S_ISREG(source.lstat().st_mode)):
        raise RetirementError("first-baseline migration binding differs")
    physical_dir(archive)
    inner_releases = archive / "runtime/releases"
    physical_dir(inner_releases)
    inner_count = sum(path.is_dir() for path in inner_releases.iterdir())
    if inner_count != 114:
        raise RetirementError("archived SCL release set differs from reviewed 114")
    fence = read_json(migration_dir / "source-fenced.json")
    if fence != {"archive_verified": True, "source_is_regular_tombstone": True,
                 "target_verified": True}:
        raise RetirementError("migration fence differs")
    archived_receipt = migration_dir / "source-baseline-receipt.json"
    inventory = migration_dir / "source-kernel-inventory.json"
    if (regular_bytes(archived_receipt) != regular_bytes(outer_receipt_path)
            or digest(regular_bytes(archived_receipt)) != migration["source_receipt_sha256"]
            or digest(regular_bytes(inventory)) != migration["source_inventory_sha256"]):
        raise RetirementError("migration source digests differ")
    for name in ("baseline-transition.json", "baseline-transition-completed.json"):
        if regular_bytes(archive / "runtime" / name) != regular_bytes(target / "runtime" / name):
            raise RetirementError("migration baseline journals differ")
    active_readback(active_harness, target, legacy_state)
    releases = legacy_state / "releases"
    physical_dir(releases)
    release_dirs = sorted(path for path in releases.iterdir() if path.is_dir() and not path.is_symlink())
    if len(release_dirs) != 12 or any(not (path / "pyvenv.cfg").is_file() for path in release_dirs):
        raise RetirementError("legacy release set differs from reviewed 12 private environments")
    profiles = []
    for directory in sorted(profile_root.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            raise RetirementError("Host profile directory redirects")
        profile = read_json(directory / "profile.json")
        harness = Path(str(profile.get("harness", "")))
        if harness.is_relative_to(releases):
            if (profile.get("profile_id") != directory.name
                    or harness.parent.parent.parent != releases
                    or harness.name != "harness" or harness.parent.name != "bin"):
                raise RetirementError("legacy Host profile binding differs")
            read_json(directory / "installation.json")
            profiles.append(directory)
    if len(profiles) != 7:
        raise RetirementError("legacy Host profile set differs from reviewed seven")
    busy = live_processes(legacy_state, archive, *profiles)
    if busy:
        raise RetirementError(f"legacy runtime is still used by process IDs: {busy}")
    if receipt_dir.is_relative_to(legacy_state) or receipt_dir.is_relative_to(archive):
        raise RetirementError("retirement receipt cannot live in a retired tree")
    result = {"schema": "migrated_harness_runtime_retirement_plan/v1", "work_item_id": root_work_item_id,
              "legacy_state": str(legacy_state), "archive": str(archive),
              "source_tombstone": str(source), "target_kernel": str(target),
              "active_harness": str(active_harness), "profile_root": str(profile_root),
              "profiles": [str(path) for path in profiles], "quarantine": str(quarantine),
              "receipt_dir": str(receipt_dir), "migration_intent": str(migration_intent),
              "migration_intent_sha256": digest(regular_bytes(migration_intent)),
              "source_inventory_sha256": migration["source_inventory_sha256"],
              "first_baseline_receipt_sha256": digest(regular_bytes(outer_receipt_path)),
              "release_names": [path.name for path in release_dirs],
              "archived_inner_release_count": inner_count}
    return result


@contextmanager
def locked(receipt_dir: Path):
    receipt_dir.mkdir(parents=True, exist_ok=True)
    physical_dir(receipt_dir)
    fd = os.open(receipt_dir / "retirement.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def write_once(path: Path, value: dict) -> None:
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def remove_quarantine(quarantine: Path) -> None:
    """Remove sealed historical releases without changing paths outside quarantine."""
    physical_dir(quarantine)

    def retry_readonly(function, name, error) -> None:
        if not isinstance(error[1], PermissionError):
            raise error[1]
        candidate = Path(name)
        parent = candidate if function is os.scandir else candidate.parent
        if (not parent.is_relative_to(quarantine) or parent.is_symlink()
                or parent != parent.resolve(strict=True)):
            raise RetirementError("read-only cleanup reached outside quarantine")
        metadata = parent.stat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise RetirementError("read-only quarantine directory is not owned by this account")
        os.chmod(parent, stat.S_IMODE(metadata.st_mode) | stat.S_IWUSR | stat.S_IXUSR)
        function(name)

    shutil.rmtree(quarantine, onerror=retry_readonly)


def stage(options: argparse.Namespace) -> dict:
    with locked(options.receipt_dir):
        if (options.receipt_dir / "staged.json").exists():
            raise RetirementError("a retirement stage receipt already exists")
        evidence = plan(**vars(options))
        quarantine = options.quarantine
        quarantine.mkdir(mode=0o700, parents=False)
        (quarantine / "profiles").mkdir(mode=0o700)
        moves = [(Path(evidence["archive"]), quarantine / "source-kernel-archive"),
                 (Path(evidence["legacy_state"]), quarantine / "legacy-state")]
        moves += [(Path(path), quarantine / "profiles" / Path(path).name)
                  for path in evidence["profiles"]]
        completed = []
        try:
            for source, target in moves:
                os.replace(source, target)
                completed.append((source, target))
            receipt = {**evidence, "status": "staged", "staged_at": datetime.now(UTC).isoformat()}
            write_once(options.receipt_dir / "staged.json", receipt)
            return receipt
        except BaseException:
            for source, target in reversed(completed):
                os.replace(target, source)
            (options.receipt_dir / "staged.json").unlink(missing_ok=True)
            (quarantine / "profiles").rmdir()
            if not any(quarantine.iterdir()):
                quarantine.rmdir()
            raise


def purge(receipt_dir: Path, root_work_item_id: str, authorize: str) -> dict:
    with locked(receipt_dir):
        receipt = read_json(receipt_dir / "staged.json")
        if receipt.get("schema") != "migrated_harness_runtime_retirement_plan/v1" or receipt.get("status") != "staged":
            raise RetirementError("retirement stage receipt is invalid")
        if (not root_work_item_id or authorize != root_work_item_id
                or receipt.get("work_item_id") != root_work_item_id
                or receipt.get("receipt_dir") != str(receipt_dir)):
            raise RetirementError("WorkItem retirement authorization differs")
        quarantine = Path(receipt["quarantine"])
        if (not quarantine.is_absolute() or ".." in quarantine.parts
                or receipt_dir.is_relative_to(quarantine)
                or quarantine.is_relative_to(receipt_dir)):
            raise RetirementError("quarantine or receipt path redirects")
        physical_dir(quarantine.parent)
        if any(Path(path).exists() or Path(path).is_symlink()
               for path in (receipt["legacy_state"], receipt["archive"], *receipt["profiles"])):
            raise RetirementError("retired source path reappeared")
        active_readback(Path(receipt["active_harness"]), Path(receipt["target_kernel"]), quarantine)
        busy = live_processes(quarantine)
        if busy:
            raise RetirementError(f"quarantine is still used by process IDs: {busy}")
        stage_sha = digest(regular_bytes(receipt_dir / "staged.json"))
        intent_path = receipt_dir / "purge-intent.json"
        if intent_path.exists() or intent_path.is_symlink():
            intent = read_json(intent_path)
            if (intent.get("schema") != "migrated_harness_runtime_retirement_purge/v1"
                    or intent.get("work_item_id") != root_work_item_id
                    or intent.get("quarantine") != str(quarantine)
                    or intent.get("stage_sha256") != stage_sha):
                raise RetirementError("purge intent differs from staged evidence")
        else:
            physical_dir(quarantine)
            physical_dir(quarantine / "legacy-state/releases")
            physical_dir(quarantine / "profiles")
            if set(path.name for path in (quarantine / "legacy-state/releases").iterdir()) != set(receipt["release_names"]):
                raise RetirementError("quarantined release set differs")
            if set(path.name for path in (quarantine / "profiles").iterdir()) != {Path(p).name for p in receipt["profiles"]}:
                raise RetirementError("quarantined Host profiles differ")
            intent = {"schema": "migrated_harness_runtime_retirement_purge/v1",
                      "work_item_id": root_work_item_id, "quarantine": str(quarantine),
                      "stage_sha256": stage_sha,
                      "started_at": datetime.now(UTC).isoformat()}
            write_once(intent_path, intent)
        purged_path = receipt_dir / "purged.json"
        if purged_path.exists() or purged_path.is_symlink():
            result = read_json(purged_path)
            if (result.get("status") != "purged" or result.get("stage_sha256") != stage_sha
                    or result.get("quarantine") != str(quarantine) or quarantine.exists()):
                raise RetirementError("purge completion receipt differs")
            return result
        if quarantine.exists() or quarantine.is_symlink():
            physical_dir(quarantine)
            remove_quarantine(quarantine)
        result = {**intent, "status": "purged", "completed_at": datetime.now(UTC).isoformat()}
        write_once(purged_path, result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "stage", "purge"))
    parser.add_argument("--legacy-state", type=Path)
    parser.add_argument("--migration-intent", type=Path)
    parser.add_argument("--profile-root", type=Path)
    parser.add_argument("--active-harness", type=Path)
    parser.add_argument("--quarantine", type=Path)
    parser.add_argument("--receipt-dir", required=True, type=Path)
    parser.add_argument("--root-work-item-id")
    parser.add_argument("--authorize")
    args = parser.parse_args()
    try:
        if args.action == "purge":
            result = purge(args.receipt_dir, args.root_work_item_id, args.authorize)
        else:
            required = ("legacy_state", "migration_intent", "profile_root", "active_harness",
                        "quarantine", "root_work_item_id", "authorize")
            if any(getattr(args, key) is None for key in required):
                parser.error("plan/stage requires exact legacy, migration, profile, active, quarantine and WI inputs")
            kwargs = {key: getattr(args, key) for key in (*required, "receipt_dir")}
            result = plan(**kwargs) if args.action == "plan" else stage(argparse.Namespace(**kwargs))
    except (RetirementError, OSError, KeyError, ValueError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
