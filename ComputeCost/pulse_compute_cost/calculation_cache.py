"""Disposable private calculation results; never a source of business facts.

Keys include exact input/engine digests. Bad, absent or unsafe cache entries
are misses. Source/ledger validation still runs before a result can be reused.
Only JSON data is stored; no executable serialization or second ledger.
"""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

from .receipts import canonical, digest

SCHEMA = "computecost_calculation_cache/v1"
MAX_BYTES = 32 * 1024 * 1024


@lru_cache(maxsize=1)
def engine_digest() -> str:
    root = Path(__file__).parent
    return digest([(p.name, __import__("hashlib").sha256(p.read_bytes()).hexdigest())
                   for p in sorted(root.glob("*.py"))])


class CalculationCache:
    def __init__(self, root: Path, *, force: bool = False):
        self.root = Path(root)
        self.force = force
        self.hits = 0
        self.misses = 0

    def _safe_root(self) -> bool:
        try:
            parent = self.root.parent.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or parent.st_mode & 0o077):
                return False
            self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
            info = self.root.lstat()
            return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077)
        except OSError:
            return False

    def _path(self, slot: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_.-]{1,80}", slot):
            raise ValueError("invalid calculation cache slot")
        return self.root / (slot + ".json")

    def get(self, slot: str, key: object) -> Any | None:
        self.misses += 1
        if self.force or not self._safe_root():
            return None
        try:
            fd = os.open(self._path(slot), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
                    return None
                data = handle.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                return None
            item = json.loads(data)
            if (set(item) != {"schema", "key", "value", "sha256"}
                    or item["schema"] != SCHEMA or item["key"] != digest(key)
                    or item["sha256"] != digest({k:v for k,v in item.items() if k != "sha256"})):
                return None
            self.hits += 1
            self.misses -= 1
            return item["value"]
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return None

    def put(self, slot: str, key: object, value: Any) -> None:
        if not self._safe_root():
            return
        temp = None
        try:
            body = {"schema": SCHEMA, "key": digest(key), "value": value}
            data = canonical({**body, "sha256": digest(body)})
            if len(data) > MAX_BYTES:
                return
            path = self._path(slot)
            if path.is_symlink():
                return
            fd, temp = tempfile.mkstemp(prefix=".pending-", dir=self.root)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except (OSError, ValueError, TypeError, RecursionError):
            pass  # An unavailable acceleration cache never blocks publication.
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except FileNotFoundError:
                    pass
