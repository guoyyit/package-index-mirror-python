"""Orchestration: dedup in-flight fetches, verify, cache, serve."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .cache import CacheStore, MetaEntry
from .config import ConfigProvider, Settings
from .integrity import parse_fragment_digest, verify_bytes
from .upstream import UpstreamClient, UpstreamError, UpstreamNotFound


@dataclass(frozen=True)
class IndexResult:
    body: bytes
    content_type: str
    source: str  # "upstream" | "cache" | "stale" | "negative-cache"
    fetched_at: float | None


class IndexNotFoundError(Exception):
    """Project/page unknown upstream (and no usable cache)."""

    def __init__(self, source: str) -> None:
        super().__init__(f"index not found (source={source})")
        self.source = source


class UpstreamUnavailableError(Exception):
    """Upstream unreachable and no cached fallback available."""


@dataclass(frozen=True)
class ArtifactResult:
    data: bytes
    source: str  # "cache" | "upstream"


class IntegrityFailedError(Exception):
    """Artifact failed verification against upstream-declared digest."""

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"integrity check failed for {key}: {reason}")
        self.key = key
        self.reason = reason


class _Flight:
    """Shared state for one in-flight artifact fetch (single-flight)."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: ArtifactResult | None = None
        self.error: Exception | None = None


class MirrorService:
    """Coordinates upstream fetches, dedup, verification and the cache."""

    def __init__(self, cache: CacheStore, config: ConfigProvider) -> None:
        self._cache = cache
        self._config = config
        self._inflight: dict[str, _Flight] = {}
        self._inflight_lock = threading.Lock()
        self._active: dict[str, int] = {}

    # --- helpers ---
    def _client(self, settings: Settings) -> UpstreamClient:
        return UpstreamClient(settings.upstream_url, settings.request_timeout, settings.retries)

    def _track_active(self, key: str, delta: int) -> None:
        with self._inflight_lock:
            count = self._active.get(key, 0) + delta
            if count <= 0:
                self._active.pop(key, None)
            else:
                self._active[key] = count

    def _protected_keys(self) -> set[str]:
        with self._inflight_lock:
            return set(self._active)

    # --- index ---
    def get_index(self, project: str | None) -> IndexResult:
        settings = self._config.get()  # snapshot: in-flight requests keep it
        key = project or ""
        lookup = self._cache.get_meta(key)
        if lookup.fresh and lookup.entry is not None:
            if lookup.entry.negative:
                raise IndexNotFoundError("negative-cache")
            return IndexResult(lookup.entry.body, lookup.entry.content_type, "cache", lookup.entry.fetched_at)
        path = f"/simple/{project}/" if project else "/simple/"
        try:
            resp = self._client(settings).fetch(path)
        except UpstreamNotFound:
            now = time.time()
            self._cache.put_meta(key, MetaEntry(b"", "text/html", now, now + settings.negative_ttl, negative=True))
            raise IndexNotFoundError("upstream")
        except UpstreamError as exc:
            if settings.stale_if_error and lookup.entry is not None and not lookup.entry.negative:
                return IndexResult(lookup.entry.body, lookup.entry.content_type, "stale", lookup.entry.fetched_at)
            raise UpstreamUnavailableError(str(exc)) from exc
        now = time.time()
        entry = MetaEntry(resp.body, resp.content_type, now, now + settings.index_ttl)
        self._cache.put_meta(key, entry)
        return IndexResult(resp.body, resp.content_type, "upstream", now)

    # --- artifacts ---
    def get_artifact(self, key: str, upstream_path: str, fragment: str) -> ArtifactResult:
        settings = self._config.get()
        self._track_active(key, +1)
        try:
            cached = self._cache.open_artifact(key)
            if cached is not None:
                return ArtifactResult(cached, "cache")
            with self._inflight_lock:
                flight = self._inflight.get(key)
                if flight is None:
                    flight = _Flight()
                    self._inflight[key] = flight
                    leader = True
                else:
                    leader = False
            if leader:
                try:
                    flight.result = self._fetch_and_cache(key, upstream_path, fragment, settings)
                except Exception as exc:  # shared with all waiters
                    flight.error = exc
                finally:
                    with self._inflight_lock:
                        self._inflight.pop(key, None)
                    flight.event.set()
            else:
                flight.event.wait()
            if flight.error is not None:
                raise flight.error
            assert flight.result is not None
            return flight.result
        finally:
            self._track_active(key, -1)

    def _fetch_and_cache(
        self, key: str, upstream_path: str, fragment: str, settings: Settings
    ) -> ArtifactResult:
        digest = parse_fragment_digest(fragment)
        client = self._client(settings)
        attempts = 1 + (settings.verify_retries if digest is not None else 0)
        reason = "no digest declared by upstream"
        for _ in range(attempts):
            resp = client.fetch(upstream_path)  # raises UpstreamError / UpstreamNotFound
            if digest is None:
                break
            if verify_bytes(resp.body, digest):
                break
            reason = f"{digest.algorithm} mismatch: expected {digest.hexdigest}"
            self._cache.quarantine_artifact(key, resp.body, reason)
        else:
            raise IntegrityFailedError(key, reason)
        self._cache.store_artifact(key, resp.body)
        self._cache.evict_expired_and_overflow(protected=self._protected_keys())
        return ArtifactResult(resp.body, "upstream")
