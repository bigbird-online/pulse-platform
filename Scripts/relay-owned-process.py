#!/usr/bin/env python3
"""Exec a Relay child, retaining a guardian in its private process group.

fd 3 is the owner's lifetime socket, independent of the command's stdin.
fd 4 reports exec errors and closes on successful exec. Neither reaches the
command. The guardian keeps the group identity alive even after its leader
exits, so cleanup never relies on a PID file or a process-name scan.
"""

import errno
import json
import os
import select
import signal
import sys
import time


def guard(owner: int, ready: int) -> None:
    group = os.getpgrp()
    if group != owner:
        os._exit(125)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    for fd in (0, 1, 2, 4):
        os.close(fd)
    os.write(ready, b"1")
    os.close(ready)
    try:
        while os.getppid() == owner:
            readable, _, _ = select.select([3], [], [], 0.1)
            if readable and not os.read(3, 4096):
                break
    finally:
        # This live group member reserves the group ID throughout cleanup.
        # TERM gives cooperative children time to finish; KILL includes us.
        os.killpg(group, signal.SIGTERM)
        time.sleep(2)
        os.killpg(group, signal.SIGKILL)


def main() -> None:
    try:
        if os.getpgrp() != os.getpid():
            raise OSError(errno.EINVAL, "owned process requires a private process group")
        read_fd, write_fd = os.pipe()
        owner = os.getpid()
        if os.fork() == 0:
            os.close(read_fd)
            guard(owner, write_fd)
            os._exit(125)
        os.close(write_fd)
        ready = os.read(read_fd, 1)
        os.close(read_fd)
        if ready != b"1":
            raise OSError(errno.EIO, "process guardian did not start")
        os.close(3)
        os.set_inheritable(4, False)
        # Match subprocess.Popen's restore_signals behavior after Python startup.
        for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), signal.SIG_DFL)
        os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
    except OSError as error:
        os.write(4, json.dumps({"code": errno.errorcode.get(error.errno, "EIO"),
                               "message": str(error)}).encode())
        os._exit(127)


if __name__ == "__main__":
    main()
