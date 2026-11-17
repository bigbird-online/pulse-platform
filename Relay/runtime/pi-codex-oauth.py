"""Read the installed Codex account for Pi and let Codex refresh its own store."""

import base64
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

MAX_AUTH_BYTES = 128 * 1024
MIN_VALIDITY_SECONDS = 120
ACCESS_TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def private_fd(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(fd)
        raise ValueError("Codex credential inputs must be private account files")
    return fd


def read_expiry(path):
    with os.fdopen(private_fd(path, os.O_RDONLY), "rb") as handle:
        raw = handle.read(MAX_AUTH_BYTES + 1)
    if len(raw) > MAX_AUTH_BYTES:
        raise ValueError("Codex credential exceeds its bound")
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("auth_mode") != "chatgpt":
        raise ValueError("Codex is not using its managed ChatGPT login")
    token = document["tokens"]["access_token"]
    if not isinstance(token, str) or len(token) > 16384 or not ACCESS_TOKEN.fullmatch(token):
        raise ValueError("Codex access credential is malformed")
    payload = token.split(".")[1]
    expiry = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    if type(expiry) is not int:
        raise ValueError("Codex access expiry is missing")
    return expiry


@contextmanager
def refresh_lock(directory):
    path = directory / ".pulse-pi-auth-refresh.lock"
    with os.fdopen(private_fd(path, os.O_RDWR | os.O_CREAT), "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def refresh_account(codex, node, home):
    """Request the official managed refresh without moving its refresh token."""
    proc = subprocess.Popen(
        [str(node), str(codex), "app-server", "--listen", "stdio://"],
        cwd=home, env={"HOME": str(home.parent), "CODEX_HOME": str(home), "PATH": "/usr/bin:/bin"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    deadline = time.monotonic() + 15
    pending = b""
    received = 0

    def send(message):
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()

    def request(reader, identifier, method, params):
        nonlocal pending, received
        send({"id": identifier, "method": method, "params": params})
        while time.monotonic() < deadline:
            if b"\n" not in pending:
                if not reader.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(proc.stdout.fileno(), 65536)
                received += len(chunk)
                if not chunk or received > MAX_AUTH_BYTES:
                    raise ValueError("Codex account response is unavailable")
                pending += chunk
                continue
            line, pending = pending.split(b"\n", 1)
            response = json.loads(line)
            if response.get("id") == identifier and "method" not in response:
                if "error" in response:
                    raise ValueError("Codex account request failed")
                return response["result"]
            if "id" in response and "method" in response:
                send({"id": response["id"], "error": {"code": -32601, "message": "Unsupported request"}})
        raise TimeoutError("Codex account request expired")

    try:
        with selectors.DefaultSelector() as reader:
            reader.register(proc.stdout, selectors.EVENT_READ)
            request(reader, 1, "initialize", {"clientInfo": {
                "name": "pulse_pi_auth", "title": "PULSE Pi", "version": "1",
            }})
            send({"method": "initialized"})
            reply = request(reader, 2, "account/read", {"refreshToken": True})
        if (reply.get("account") or {}).get("type") != "chatgpt":
            raise ValueError("Codex managed account is unavailable")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.stdout.close()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=2)


def refresh_if_needed(path, codex, node):
    with refresh_lock(path.parent):
        if read_expiry(path) <= time.time() + MIN_VALIDITY_SECONDS:
            refresh_account(codex, node, path.parent)
        if read_expiry(path) <= time.time() + MIN_VALIDITY_SECONDS:
            raise ValueError("Codex did not return a current credential")


def main():
    try:
        file, launcher, node = map(Path, sys.argv[1:])
        parent = file.parent
        info = parent.stat()
        if (not file.is_absolute() or file.name != "auth.json" or not launcher.is_absolute()
                or not node.is_absolute()
                or parent.resolve(strict=True) != parent or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise ValueError("Codex account directory is invalid")
        refresh_if_needed(file, launcher, node)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.SubprocessError):
        print("Installed Codex login unavailable", file=sys.stderr)
        return 1
    return 0


def interrupted(_signum, _frame):
    raise InterruptedError("Codex credential request cancelled")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
