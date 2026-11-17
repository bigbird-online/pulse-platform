"""Private original custody, outside Git and independent of business replay."""

from __future__ import annotations

import errno
import ctypes
import sys
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from .model import Rejected


class Evidence:
    def __init__(self, root: Path):
        self.root = root

    def directory(self, *, create: bool) -> int:
        if create:
            self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        # A setgid parent can propagate its group and this bit to new
        # directories. It grants no access; owner-only rwx must still be exact.
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in (0o700, 0o2700):
            os.close(fd)
            raise Rejected("evidence_unavailable", "Private custody directory is not owner-only")
        try:
            self.private_acl(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def private_acl(fd: int) -> None:
        # POSIX ACL grants are restricted by the exact owner-only mode/mask.
        # macOS extended ACL grants are independent of those permission bits.
        if sys.platform != "darwin":
            return
        libc = ctypes.CDLL(None, use_errno=True)
        libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.acl_get_fd_np.restype = ctypes.c_void_p
        libc.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        libc.acl_free.argtypes = [ctypes.c_void_p]
        acl = libc.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED
        if not acl and ctypes.get_errno() == errno.ENOENT:
            return  # Filesystem explicitly reports no extended ACL.
        if not acl:
            raise Rejected("evidence_unavailable", "Private custody ACL cannot be verified")
        try:
            entry = ctypes.c_void_p()
            if libc.acl_get_entry(acl, 0, ctypes.byref(entry)) == 0:
                raise Rejected("evidence_unavailable", "Private custody has an extended ACL")
        finally:
            libc.acl_free(acl)

    @staticmethod
    def checked(sha: str, content: bytes) -> bytes:
        if hashlib.sha256(content).hexdigest() != sha:
            raise Rejected("evidence_invalid", "Original evidence differs from its content address")
        return content

    def read(self, sha: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise Rejected("evidence_invalid", "Invalid private content address")
        try:
            directory = self.directory(create=False)
            try:
                fd = os.open(sha, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                            or stat.S_IMODE(info.st_mode) != 0o600):
                        raise Rejected("evidence_unavailable", "Private original is not owner-only")
                    self.private_acl(stream.fileno())
                    return self.checked(sha, stream.read())
            finally:
                os.close(directory)
        except OSError as exc:
            raise Rejected(
                "evidence_unavailable", "Original bytes are unavailable at this private custodian"
            ) from exc

    def put(self, sha: str, content: bytes) -> None:
        try:
            self._put(sha, content)
        except (OSError, Rejected) as exc:
            if isinstance(exc, Rejected) and exc.code == "evidence_invalid":
                raise
            raise Rejected("evidence_write_unavailable",
                "Required original was not saved; verify owner-only custody and retry the original request") from exc

    def _put(self, sha: str, content: bytes) -> None:
        self.checked(sha, content)
        directory = self.directory(create=True)
        name = ".pending-" + uuid.uuid4().hex
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                self.private_acl(stream.fileno())
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, sha, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False)
            except FileExistsError:
                if self.read(sha) != content:
                    raise Rejected("evidence_invalid", "Private content address already differs")
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory)
