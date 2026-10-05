"""Cross-process advisory file locks."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
import errno
import fcntl
import os


class LockError(RuntimeError):
    pass


@contextmanager
def file_lock(path: str, *, exclusive: bool = True, blocking: bool = True) -> Iterator[None]:
    """Hold an flock-based lock for the duration of the context."""
    ensure_dir(os.path.dirname(path) or ".")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, operation)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise LockError(f"lock busy: {path}") from exc
            raise
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
