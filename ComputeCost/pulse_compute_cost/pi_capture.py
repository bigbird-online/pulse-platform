"""Private Pi snapshot publication; the actual writer owns the OS lock.

A persistent lock *file* is only an inode, never evidence of a live owner.
The kernel releases its flock when the capturing process exits or is killed.
Do not unlink the lock file: contenders must continue locking the same inode.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, Mapping

from .pi_usage import (
    PiUsageBinding, PiUsageError, build_pi_session_snapshot,
    validate_pi_session_snapshot, verify_pi_snapshot_source_prefix,
    _validate_baseline_for_binding,
)


def _private_read(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PiUsageError('Pi capture artifact mode is invalid')
        value = json.load(stream)
    validate_pi_session_snapshot(value)
    return value


def _validate_existing(value: Mapping[str, Any], binding: PiUsageBinding,
                       role: str, baseline: Mapping[str, Any] | None) -> None:
    expected = {
        'work_item_id': binding.work_item_id, 'agent_id': binding.agent_id,
        'phase': binding.phase, 'epoch': binding.epoch,
        'procurement_source': binding.procurement_source,
        'authority_ref': binding.authority_ref,
        'execution_surface': binding.execution_surface,
        'runtime_binding_sha256': binding.runtime_binding_sha256,
        'measurement_role': role,
        'lifecycle_state': 'sealed' if role == 'final' else 'open',
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise PiUsageError('Pi capture artifact does not match runtime authority')
    if role != 'baseline':
        checked = _validate_baseline_for_binding(baseline, binding)
        boundary = value['attribution_boundary']
        if (boundary['baseline_snapshot_sha256'] != checked['snapshot_sha256']
                or boundary['baseline'] != checked['attribution_boundary']['baseline']):
            raise PiUsageError('Pi capture final does not descend from the immutable baseline')


def capture_pi_session_snapshot(
    path: str | Path, binding: PiUsageBinding, *, output_path: str | Path,
    lifecycle_state: str, measurement_role: str,
    baseline_snapshot: Mapping[str, Any] | None = None,
    unpersisted_baseline: bool = False,
) -> dict[str, Any]:
    """Sample and publish under one native lock; immutable roles replay the winner.

    This runs inside the native probe process, not in a lock-holder sidecar.
    A killed host can leave a still-running probe, which retains the lock until
    its publication finishes. A killed probe cannot leave a live lock behind.
    """
    target = Path(output_path)
    if not binding.managed or not target.is_absolute() or target.parent.is_symlink():
        raise PiUsageError('Pi capture requires a managed binding and private absolute output')
    artifact_state = 'baseline' if measurement_role == 'baseline' else lifecycle_state
    expected_name = f'{binding.work_item_id}-{binding.runtime_binding_sha256}-{artifact_state}.json'
    allowed_names = {expected_name}
    if measurement_role == 'baseline':
        allowed_names.add(expected_name + '.origin-candidate.json')
    if target.name not in allowed_names:
        raise PiUsageError('Pi capture output does not match its binding and role')
    if measurement_role not in {'baseline', 'progress', 'final'} or (
        (lifecycle_state == 'sealed') != (measurement_role == 'final')
    ):
        raise PiUsageError('Pi capture lifecycle and measurement role disagree')
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = target.with_name(target.name + '.capture-lock')
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'rb') as lock:
            metadata = os.fstat(lock.fileno())
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PiUsageError('Pi capture lock mode is invalid')
            deadline = time.monotonic() + 20
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PiUsageError('Pi host usage seal lock did not converge')
                    time.sleep(0.05)
            current_lock = lock_path.lstat()
            if (current_lock.st_dev, current_lock.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise PiUsageError('Pi capture lock inode changed')
            previous = _private_read(target)
            if previous is not None:
                _validate_existing(previous, binding, measurement_role, baseline_snapshot)
                if measurement_role != 'progress':
                    verify_pi_snapshot_source_prefix(
                        path, previous, position='baseline' if measurement_role == 'baseline' else 'final')
                    return {'schema': 'pi_atomic_capture/v1', 'snapshot': previous, 'idempotent_replay': True}
            snapshot = build_pi_session_snapshot(
                path, binding, lifecycle_state=lifecycle_state,
                measurement_role=measurement_role, baseline_snapshot=baseline_snapshot,
                unpersisted_baseline=unpersisted_baseline,
            )
            _validate_existing(snapshot, binding, measurement_role, baseline_snapshot)
            verify_pi_snapshot_source_prefix(
                path, snapshot, position='baseline' if measurement_role == 'baseline' else 'final')
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                                 prefix=target.name + '.', suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(snapshot, stream, ensure_ascii=False, sort_keys=True, indent=2)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return {'schema': 'pi_atomic_capture/v1', 'snapshot': snapshot, 'idempotent_replay': False}
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, PiUsageError):
            raise
        raise PiUsageError('Pi capture publication or private readback failed') from exc
