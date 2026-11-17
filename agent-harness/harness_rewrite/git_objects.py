"""One bounded Git object stream; no checkout or durable projection."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from typing import Callable, Iterator

from .model import Rejected
from .git_environment import repository_environment

READ_TIMEOUT = 60.0


@contextmanager
def object_reader(
    repository: Path, mapping: dict[str, str], *, ordered: bool = False
) -> Iterator[Callable[[str], bytes]]:
    """Read selected blobs; `ordered` streams every request while reading the
    responses, and accepts reads only in `mapping` order."""
    selected = {path: sha.removeprefix("100755:blob:") for path, sha in mapping.items()}
    order = list(selected) if ordered else []
    if any(not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) for sha in selected.values()):
        raise Rejected("ledger_invalid", "A consumed production record is not a regular Git blob")
    process = None
    try:
        process = subprocess.Popen(
            ["git", "-C", str(repository), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, env=repository_environment(),
        )
        assert process.stdin is not None and process.stdout is not None
        incoming, outgoing = process.stdout, process.stdin
        pending = bytearray()
        # Ordered requests are written only while waiting for a response, so
        # neither side ever blocks on a full pipe while the other waits.
        unsent = memoryview(b"".join((selected[path] + "\n").encode() for path in order))
        consumed = 0
        with selectors.DefaultSelector() as selector:
            selector.register(incoming, selectors.EVENT_READ)
            if unsent:
                os.set_blocking(outgoing.fileno(), False)
                selector.register(outgoing, selectors.EVENT_WRITE)

            def receive(deadline: float) -> bytes:
                nonlocal unsent
                while True:
                    remaining = deadline - time.monotonic()
                    ready = selector.select(remaining) if remaining > 0 else []
                    if not ready:
                        raise Rejected("ledger_unavailable", "Production object read timed out")
                    if any(key.fileobj is incoming for key, _ in ready):
                        return os.read(incoming.fileno(), 65536)
                    try:
                        unsent = unsent[os.write(outgoing.fileno(), unsent):]
                    except BlockingIOError:
                        continue
                    if not unsent:
                        selector.unregister(outgoing)

            def read(path: str) -> bytes:
                nonlocal consumed
                expected = selected.get(path)
                if expected is None:
                    raise Rejected("ledger_invalid", "Production object was not selected")
                deadline = time.monotonic() + READ_TIMEOUT
                if ordered:
                    if consumed == len(order) or order[consumed] != path:
                        raise Rejected("ledger_invalid", "Production object read out of order")
                    consumed += 1
                else:
                    # A response is fully consumed before the next short request, so
                    # neither pipe can fill while waiting on the other pipe.
                    outgoing.write((expected + "\n").encode())
                    outgoing.flush()
                while b"\n" not in pending:
                    if len(pending) >= 256:
                        raise Rejected("ledger_invalid", "Invalid production record header")
                    chunk = receive(deadline)
                    if not chunk:
                        raise Rejected("ledger_invalid", "Truncated production record")
                    pending.extend(chunk)
                end = pending.index(b"\n") + 1
                header = bytes(pending[:end])
                del pending[:end]
                try:
                    sha, kind, size = header.decode("ascii").rstrip("\n").split()
                    length = int(size)
                except (ValueError, UnicodeError) as exc:
                    raise Rejected("ledger_invalid", "Invalid production record header") from exc
                if end > 256 or sha != expected or kind != "blob" or length < 0 or str(length) != size:
                    raise Rejected("ledger_invalid", "Invalid production record framing")
                content = bytearray()
                # Never allocate a claimed length before receiving its bytes.
                # Large original records remain readable without an aggregate cap.
                while len(content) < length:
                    if not pending:
                        chunk = receive(deadline)
                        if not chunk:
                            raise Rejected("ledger_invalid", "Truncated production record")
                        pending.extend(chunk)
                    take = min(length - len(content), len(pending))
                    content.extend(pending[:take])
                    del pending[:take]
                if not pending:
                    pending.extend(receive(deadline))
                # Only an ordered read may already hold the next response.
                if pending[:1] != b"\n" or not ordered and len(pending) != 1:
                    raise Rejected("ledger_invalid", "Ambiguous production read")
                del pending[:1]
                checksum = hashlib.new("sha1" if len(expected) == 40 else "sha256")
                checksum.update(b"blob " + str(length).encode() + b"\0")
                checksum.update(content)
                if checksum.hexdigest() != expected:
                    raise Rejected("ledger_invalid", "Invalid production record identity")
                return bytes(content)

            yield read
            if consumed != len(order) or ordered and pending:
                raise Rejected("ledger_invalid", "Production objects were left unread")
            outgoing.close()
            # EOF and exit status belong to this read, including an empty set.
            # Do not accept delayed trailing bytes or a failed Git subprocess.
            if receive(time.monotonic() + READ_TIMEOUT):
                raise Rejected("ledger_invalid", "Ambiguous production read")
            if process.wait(timeout=5):
                raise Rejected("ledger_unavailable", "Production Git operation failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Rejected("ledger_unavailable", "Production authority could not be observed") from exc
    finally:
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
