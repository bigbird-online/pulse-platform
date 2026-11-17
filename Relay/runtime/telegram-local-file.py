"""Read one bounded regular file through directory handles, never through links."""
import json
import os
from pathlib import PurePosixPath
import stat
import sys


def read_rooted(root, relative, limit):
    root_parts = PurePosixPath(root).parts
    parts = PurePosixPath(relative).parts
    if not root.startswith('/') or root == '/' or not parts or relative.startswith('/'):
        raise ValueError('unsafe_path')
    if any(p in ('..', '.') for p in (*root_parts[1:], *parts)):
        raise ValueError('unsafe_path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Open every component relative to its already-open parent. A rename or
        # symlink substitution cannot redirect a later open outside this tree.
        for part in (*root_parts[1:], *parts[:-1]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('not_regular')
            if info.st_size > limit:
                raise ValueError('too_large')
            chunks, size = [], 0
            while True:
                chunk = os.read(source, min(65536, limit + 1 - size))
                if not chunk:
                    return b''.join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise ValueError('too_large')
        finally:
            os.close(source)
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        sys.stdout.buffer.write(read_rooted(request['root'], request['relative'], request['limit']))
    except (OSError, ValueError, KeyError):
        # Neither file contents nor private paths belong in diagnostics.
        sys.stderr.write('local_file_unavailable')
        sys.exit(1)
