"""Time-bounded positive and negative cache for index pages."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .locks import ensure_dir


def _safe_key(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{digest[:2]}/{digest[2:]}.json"


class IndexCache:
    """Filesystem cache for PEP 503 index pages with TTL semantics.

    Each entry is one JSON file written atomically (temp file + fsync +
    ``os.replace``), so a crash never leaves a torn entry behind. The stored
    timestamp is wall-clock based, making freshness decisions stable across
    process restarts.
    """

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        ttl_seconds: float,
        negative_ttl_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = Path(cache_dir) / "index"
        ensure_dir(str(self._root))
        self._ttl = ttl_seconds
        self._negative_ttl = negative_ttl_seconds
        self._clock = clock

    def _path_for(self, key: str) -> Path:
        return self._root / _safe_key(key)

    def get(self, key: str) -> tuple[bytes, str, bool] | None:
        """Return ``(body, content_type, is_negative)`` if fresh, else None."""
        stale = self.get_stale(key)
        if stale is None:
            return None
        body, content_type, negative, age = stale
        ttl = self._negative_ttl if negative else self._ttl
        if age > ttl:
            return None
        return body, content_type, negative

    def get_stale(self, key: str) -> tuple[bytes, str, bool, float] | None:
        """Return ``(body, content_type, is_negative, age_seconds)`` ignoring TTL."""
        path = self._path_for(key)
        try:
            with open(path, "rb") as handle:
                raw = json.loads(handle.read().decode("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, KeyError):
            return None
        try:
            stored_at = float(raw["stored_at"])
            body = bytes.fromhex(raw["body_hex"])
            content_type = str(raw["content_type"])
            negative = bool(raw["negative"])
        except (KeyError, TypeError, ValueError):
            return None
        age = max(0.0, self._clock() - stored_at)
        return body, content_type, negative, age

    def put(self, key: str, body: bytes, content_type: str, *, negative: bool = False) -> None:
        path = self._path_for(key)
        ensure_dir(str(path.parent))
        payload = json.dumps(
            {
                "stored_at": self._clock(),
                "content_type": content_type,
                "negative": negative,
                "body_hex": body.hex(),
            }
        ).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            self._fsync_dir(path.parent)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
