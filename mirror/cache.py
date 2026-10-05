"""On-disk cache: atomic artifact writes, quarantine, TTL metadata, eviction."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True)
class MetaEntry:
    """Cached index metadata with freshness information."""

    body: bytes
    content_type: str
    fetched_at: float
    expires_at: float
    negative: bool = False


@dataclass(frozen=True)
class MetaLookup:
    """Result of a metadata lookup."""

    entry: MetaEntry | None
    fresh: bool


class CacheStore:
    """Filesystem-backed cache rooted at ``cache_dir``."""

    def __init__(self, cache_dir: str | Path, max_bytes: int = 1 << 30) -> None:
        self._root = Path(cache_dir)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    # --- path helpers ---
    @staticmethod
    def _segments(key: str) -> list[str]:
        key = key or "_root"
        parts = key.split("/")
        if any(not p or p in (".", "..") for p in parts):
            raise ValueError(f"unsafe cache key: {key!r}")
        return [quote(p, safe="") for p in parts]

    @classmethod
    def _flat(cls, key: str) -> str:
        return "%2F".join(cls._segments(key))

    def _tmp_path(self, key: str) -> Path:
        flat = "%2F".join(self._segments(key))
        return self._root / "tmp" / f"{flat}.{os.getpid()}.{uuid.uuid4().hex}.part"

    # --- artifacts ---
    def artifact_path(self, key: str) -> Path:
        return self._root.joinpath("artifacts", *self._segments(key))

    def has_artifact(self, key: str) -> bool:
        return self.artifact_path(key).is_file()

    def open_artifact(self, key: str) -> bytes | None:
        path = self.artifact_path(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        os.utime(path, None)  # LRU: refresh mtime on hits
        return data

    def store_artifact(self, key: str, data: bytes) -> Path:
        """Atomically publish ``data`` under ``key`` (tmp file + rename)."""
        target = self.artifact_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp_path(key)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)  # atomic: readers see old or new, never partial
        return target

    def quarantine_artifact(self, key: str, data: bytes, reason: str) -> Path:
        """Isolate a failed-verification artifact outside the servable cache."""
        qdir = self._root / "quarantine"
        qdir.mkdir(parents=True, exist_ok=True)
        flat = "%2F".join(self._segments(key))
        target = qdir / f"{flat}.{int(time.time())}"
        with self._lock:
            target.write_bytes(data)
            target.with_suffix(target.suffix + ".reason").write_text(reason)
        return target

    # --- metadata paths ---
    def _meta_json(self, key: str) -> Path:
        return self._root / "meta" / f"{self._flat(key)}.json"

    def _meta_body(self, key: str) -> Path:
        return self._root / "meta" / f"{self._flat(key)}.body"

    # --- metadata ---
    def get_meta(self, key: str) -> MetaLookup:
        try:
            raw = json.loads(self._meta_json(key).read_text())
            body = self._meta_body(key).read_bytes()
            entry = MetaEntry(
                body=body,
                content_type=raw["content_type"],
                fetched_at=raw["fetched_at"],
                expires_at=raw["expires_at"],
                negative=raw["negative"],
            )
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
            return MetaLookup(entry=None, fresh=False)
        return MetaLookup(entry=entry, fresh=time.time() < entry.expires_at)

    def put_meta(self, key: str, entry: MetaEntry) -> None:
        jpath, bpath = self._meta_json(key), self._meta_body(key)
        jpath.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "content_type": entry.content_type,
            "fetched_at": entry.fetched_at,
            "expires_at": entry.expires_at,
            "negative": entry.negative,
        }
        with self._lock:
            tmp = jpath.with_suffix(".body.tmp")
            tmp.write_bytes(entry.body)
            os.replace(tmp, bpath)
            tmp = jpath.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, jpath)

    # --- maintenance ---
    def _total_bytes(self) -> int:
        total = 0
        for sub in ("artifacts", "meta"):
            base = self._root / sub
            if base.is_dir():
                for path in base.rglob("*"):
                    if path.is_file():
                        total += path.stat().st_size
        return total

    def evict_expired_and_overflow(self, protected: set[str] | None = None) -> int:
        """Remove expired metadata and LRU-overflow artifacts; returns freed bytes."""
        protected_flat = {self._flat(k) for k in (protected or set())}
        freed = 0
        now = time.time()
        with self._lock:
            # Expired negative entries are useless once stale; drop eagerly.
            meta_dir = self._root / "meta"
            if meta_dir.is_dir():
                for jpath in meta_dir.glob("*.json"):
                    if jpath.stem in protected_flat:
                        continue
                    try:
                        raw = json.loads(jpath.read_text())
                    except json.JSONDecodeError:
                        continue
                    if raw.get("negative") and now >= raw.get("expires_at", 0):
                        bpath = jpath.with_suffix(".body")
                        freed += jpath.stat().st_size
                        freed += bpath.stat().st_size if bpath.is_file() else 0
                        jpath.unlink(missing_ok=True)
                        bpath.unlink(missing_ok=True)
            # Overflow: evict LRU artifacts, then stale meta, never protected keys.
            overflow = self._total_bytes() - self._max_bytes
            if overflow > 0:
                art_dir = self._root / "artifacts"
                files = []
                if art_dir.is_dir():
                    files = [p for p in art_dir.rglob("*") if p.is_file()]
                files.sort(key=lambda p: p.stat().st_mtime)
                for path in files:
                    if overflow <= 0:
                        break
                    rel = path.relative_to(art_dir)
                    if "%2F".join(rel.parts) in protected_flat:
                        continue
                    size = path.stat().st_size
                    path.unlink(missing_ok=True)
                    freed += size
                    overflow -= size
        return freed

    def recover(self) -> None:
        """Crash recovery: drop leftover temp files, keep only complete entries."""
        tmp_dir = self._root / "tmp"
        if tmp_dir.is_dir():
            for path in tmp_dir.glob("*.part"):
                path.unlink(missing_ok=True)
        meta_dir = self._root / "meta"
        if meta_dir.is_dir():
            for jpath in meta_dir.glob("*.json"):
                bpath = jpath.with_suffix(".body")
                try:
                    raw = json.loads(jpath.read_text())
                    valid = all(k in raw for k in ("content_type", "fetched_at", "expires_at", "negative"))
                except json.JSONDecodeError:
                    valid = False
                if not valid or not bpath.is_file():
                    jpath.unlink(missing_ok=True)
                    bpath.unlink(missing_ok=True)
