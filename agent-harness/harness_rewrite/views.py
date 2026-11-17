"""Rebuild disposable views from the normal authenticated query boundary."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import tempfile

from . import project_page
from .kernel import Kernel
from .model import Json, Query, encoded, identity


class Views:
    def __init__(self, kernel: Kernel, root: Path):
        self.kernel, self.root = kernel, root.absolute()

    def read(self, wi: str, host: object) -> Json:
        # Files are outputs only; no cache value participates in a decision.
        response = self.kernel.inspect(Query(wi), host)
        if response["status"] == "rejected":
            return response
        try:
            identity(wi)
            if self.root.resolve() != self.root:
                raise OSError("View root cannot redirect writes")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = self.root / ".publication.lock"
            fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "rb") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                # Query under the publication lock, preventing an old build from
                # replacing a newer one. No mutable cache watermark is trusted.
                response = self.kernel.inspect(Query(wi), host)
                if response["status"] == "rejected":
                    return response
                target = self.root / (wi + ".json")
                if target.is_symlink():
                    raise OSError("View target cannot redirect writes")
                content = encoded(
                    {
                        "revision": response["work_item"]["local_revision"],
                        "state": response["result"]["current_observation"],
                        "effects": response["effects"],
                    }
                )
                temporary: str | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=self.root, prefix=".generation-", delete=False
                    ) as out:
                        temporary = out.name
                        out.write(content)
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(temporary, target)
                    temporary = None
                finally:
                    if temporary is not None:
                        os.unlink(temporary)
                try:
                    # Fill in a page missing since before this program; a current one stays.
                    response["project_page"] = project_page.write(
                        self.root, response,
                        as_of=datetime.now(timezone.utc).isoformat(), refresh=False,
                    )
                except Exception:  # noqa: BLE001 - the page is optional derived output.
                    response["project_page"] = None
        except OSError:
            response["view_diagnostic"] = "view_unavailable"
        return response
