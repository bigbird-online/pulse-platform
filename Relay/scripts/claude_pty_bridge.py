#!/usr/bin/env python3
"""Spawn Claude Code inside a PTY and bridge stdio over plain pipes."""

from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import sys
import time
from typing import NoReturn


child: subprocess.Popen[bytes] | None = None
master_fd: int | None = None
termination_deadline: float | None = None
TERMINATION_GRACE_SECONDS = 2.0


def _forward_signal(signum: int, _frame: object) -> None:
    """Forward termination to the child process group."""
    global termination_deadline
    if child is None or child.poll() is not None:
        return
    if termination_deadline is None:
        termination_deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    try:
        os.killpg(child.pid, signum)
    except OSError:
        pass


def _exit(code: int) -> NoReturn:
    raise SystemExit(code)


def _reap_child() -> int:
    """Bound shutdown of the process group created by this bridge only."""
    if child is None:
        return 0
    if child.poll() is None:
        _forward_signal(signal.SIGTERM, None)
        remaining = max(0.0, (termination_deadline or time.monotonic()) - time.monotonic())
        try:
            return child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            # This unreaped direct child still owns the group's PID; do not
            # scan command lines or signal processes from a previous bridge.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return child.wait()


def main() -> None:
    global child, master_fd

    if len(sys.argv) < 2:
        print("usage: claude_pty_bridge.py <command> [args...]", file=sys.stderr)
        _exit(2)

    signal.signal(signal.SIGTERM, _forward_signal)
    signal.signal(signal.SIGINT, _forward_signal)

    master_fd, slave_fd = pty.openpty()
    child = subprocess.Popen(
        sys.argv[1:],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave_fd)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()

    try:
        while True:
            if termination_deadline is not None and time.monotonic() >= termination_deadline:
                break
            read_fds = [master_fd, stdin_fd]
            ready, _, _ = select.select(read_fds, [], [], 0.1)

            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                os.write(stdout_fd, chunk)

            if stdin_fd in ready:
                chunk = os.read(stdin_fd, 4096)
                if not chunk:
                    break
                os.write(master_fd, chunk)

            if child.poll() is not None and not ready:
                break
    except BrokenPipeError:
        # The owning Relay/Host may disappear while we forward output.
        pass
    finally:
        if master_fd is not None:
            os.close(master_fd)
        code = _reap_child()

    _exit(code)


if __name__ == "__main__":
    main()
