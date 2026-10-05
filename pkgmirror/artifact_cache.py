"""Persistent atomic artifact cache with integrity verification."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import hashlib
import os
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .locks import ensure_dir


class ChecksumMismatchError(Exception):
    """Downloaded content failed upstream-declared integrity verification."""

    def __init__(self, algorithm: str, expected: str, actual: str, attempts: int) -> None:
        super().__init__(
            f"checksum mismatch after {attempts} attempt(s): "
            f"expected {algorithm}:{expected}, got {algorithm}:{actual}"
        )
        self.algorithm = algorithm
        self.expected = expected
        self.actual = actual
        self.attempts = attempts


@dataclass(slots=True)
class DownloadHandle:
    status_code: int
    content_type: str
    chunks: AsyncIterator[bytes]


class Downloader(Protocol):
    def __call__(self, upstream_url: str) -> AbstractAsyncContextManager[DownloadHandle]:
        """Open a 200 download; raise on transport error or non-200 status."""


def _sharded_path(directory: Path, key: str) -> Path:
    return directory / key[:2] / key


def _part_owner_pid(name: str) -> int | None:
    marker = ".part-"
    start = name.find(marker)
    if start < 0:
        return None
    tail = name[start + len(marker) :]
    digits = ""
    for char in tail:
        if char.isdigit():
            digits += char
        else:
            break
    return int(digits) if digits else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.asynccontextmanager
async def _locked(path: str, *, exclusive: bool, blocking: bool = True):
    fd = -1

    def _acquire() -> int:
        ensure_dir(os.path.dirname(path) or ".")
        handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(handle, flags)
        return handle

    fd = await asyncio.to_thread(_acquire)
    try:
        yield
    finally:
        def _release() -> None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        await asyncio.to_thread(_release)


class _Busy(Exception):
    pass


@contextlib.asynccontextmanager
async def _try_locked(path: str, *, exclusive: bool):
    try:
        async with _locked(path, exclusive=exclusive, blocking=False):
            yield True
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            yield False
        else:
            raise


class ArtifactCache:
    """Filesystem cache hit / download / verification machinery.

    Layout::

        artifacts/<kk>/<key>           final readable entries
        tmp/.<key>.part-*              in-flight downloads
        quarantine/<key>-<timestamp>   isolated bad downloads
        locks/<key>.lock               cross-process coordination

    Entries are published only via atomic ``os.replace`` after a full
    download + hash verification, so readers never see partial content and
    a crashed download leaves at most an ignored ``tmp`` file, which is
    swept on startup.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        root = Path(cache_dir)
        self._dir = root / "artifacts"
        self._tmp = root / "tmp"
        self._quarantine = root / "quarantine"
        self._locks = root / "locks"
        for directory in (self._dir, self._tmp, self._quarantine, self._locks):
            ensure_dir(str(directory))
        self.sweep_tmp()

    def sweep_tmp(self) -> int:
        """Delete partial downloads left by previously crashed processes.

        Only ``.<key>.part-<pid>-*`` files whose owning process no longer
        exists are removed, so an active peer process keeps its in-flight
        files.
        """
        removed = 0
        for child in self._tmp.iterdir():
            owner = _part_owner_pid(child.name)
            if owner is not None and _pid_alive(owner):
                continue
            with contextlib.suppress(FileNotFoundError):
                child.unlink()
                removed += 1
        return removed

    def entry_path(self, key: str) -> Path:
        return _sharded_path(self._dir, key)

    def lock_path(self, key: str) -> Path:
        return self._locks / f"{key}.lock"

    def has_entry(self, key: str) -> bool:
        return self.entry_path(key).is_file()

    def open_entry(self, key: str) -> bytes:
        return self.entry_path(key).read_bytes()

    @contextlib.asynccontextmanager
    async def reading(self, key: str) -> AsyncIterator[Path]:
        """Hold a shared lock while serving an entry (eviction-safe read)."""
        path = self.entry_path(key)
        async with _locked(str(self.lock_path(key)), exclusive=False):
            if not path.is_file():
                raise FileNotFoundError(key)
            yield path

    async def get_or_fetch(
        self,
        key: str,
        upstream_url: str,
        expected_hash: tuple[str, str] | None,
        downloader: Downloader,
        *,
        max_attempts: int,
    ) -> tuple[Path, bool]:
        """Return ``(entry_path, downloaded)``; one upstream fetch per key.

        In-process coalescing is provided by the caller's SingleFlight;
        here an exclusive per-key flock both deduplicates across processes
        and serialises eviction against the download.
        """
        final = self.entry_path(key)
        if final.is_file():
            return final, False

        async with _locked(str(self.lock_path(key)), exclusive=True):
            if final.is_file():  # another process finished while we waited
                return final, False

            ensure_dir(str(final.parent))
            mismatch: ChecksumMismatchError | None = None

            for attempt in range(1, max_attempts + 1):
                tmp_fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{key}.part-{os.getpid()}-", dir=str(self._tmp)
                )
                tmp_path = Path(tmp_name)
                try:
                    hasher = hashlib.new(expected_hash[0]) if expected_hash else None
                    async with downloader(upstream_url) as handle:
                        with os.fdopen(tmp_fd, "wb") as out:
                            tmp_fd = -1
                            async for chunk in handle.chunks:
                                out.write(chunk)
                                if hasher is not None:
                                    hasher.update(chunk)
                            out.flush()
                            os.fsync(out.fileno())

                    if hasher is not None:
                        actual = hasher.hexdigest()
                        if actual.lower() != expected_hash[1].lower():
                            mismatch = ChecksumMismatchError(
                                expected_hash[0], expected_hash[1], actual, attempt
                            )
                            quarantine_path = self._quarantine_name(key)
                            os.replace(tmp_name, quarantine_path)
                            continue

                    os.replace(tmp_path, final)
                    self._fsync_dir(final.parent)
                    return final, True
                finally:
                    if tmp_fd >= 0:
                        os.close(tmp_fd)
                    tmp_path.unlink(missing_ok=True)

            assert mismatch is not None
            raise mismatch

    def _quarantine_name(self, key: str) -> Path:
        stamp = time.strftime("%Y%m%d%H%M%S")
        candidate = self._quarantine / f"{key}-{stamp}"
        counter = 1
        while candidate.exists():
            candidate = self._quarantine / f"{key}-{stamp}-{counter}"
            counter += 1
        return candidate

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        with contextlib.suppress(OSError):
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def iter_entries(self) -> list[tuple[str, Path, os.stat_result]]:
        return [
            (item.name, item, item.stat())
            for item in self._dir.rglob("*")
            if item.is_file()
        ]

    def total_bytes(self) -> int:
        return sum(stat.st_size for _, _, stat in self.iter_entries())

    async def evict_lru(self, max_bytes: int) -> list[str]:
        """Evict least-recently-used entries until under ``max_bytes``.

        A candidate is removed only if its per-key lock can be acquired
        exclusively without blocking, so entries currently downloading or
        being read are skipped and never evicted mid-use.
        """
        if max_bytes <= 0:
            return []
        removed: list[str] = []
        # Oldest atime first: those are the least-recently-used entries.
        entries = sorted(
            self.iter_entries(), key=lambda item: item[2].st_atime, reverse=False
        )
        total = sum(stat.st_size for _, _, stat in entries)
        for name, path, stat in entries:
            if total <= max_bytes:
                break
            key = name
            async with _try_locked(str(self.lock_path(key)), exclusive=True) as got:
                if not got:
                    continue
                if path.exists():
                    path.unlink()
                    removed.append(str(path))
                    total -= stat.st_size
        return removed
