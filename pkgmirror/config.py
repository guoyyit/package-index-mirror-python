"""Runtime configuration for the mirror service."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Lock


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable configuration snapshot.

    In-flight requests capture the snapshot via :meth:`ConfigStore.get`
    at start and keep using it until completion; only requests started
    after an update observe the new values.
    """

    upstream_url: str
    request_timeout: float
    retry_attempts: int
    index_ttl_seconds: float
    negative_ttl_seconds: float
    max_cache_bytes: int
    artifact_verify_max_attempts: int
    serve_stale_on_failure: bool

    def __post_init__(self) -> None:
        if not self.upstream_url:
            raise ValueError("upstream_url must not be empty")
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if self.retry_attempts < 0:
            raise ValueError("retry_attempts must be >= 0")
        if self.index_ttl_seconds < 0:
            raise ValueError("index_ttl_seconds must be >= 0")
        if self.negative_ttl_seconds < 0:
            raise ValueError("negative_ttl_seconds must be >= 0")
        if self.max_cache_bytes < 0:
            raise ValueError("max_cache_bytes must be >= 0")
        if self.artifact_verify_max_attempts < 1:
            raise ValueError("artifact_verify_max_attempts must be >= 1")

    @property
    def upstream_base(self) -> str:
        return self.upstream_url.rstrip("/")


class ConfigStore:
    """Holds the current :class:`Config` snapshot."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = Lock()

    def get(self) -> Config:
        return self._config

    def update(self, **changes: object) -> Config:
        with self._lock:
            self._config = replace(self._config, **changes)
            return self._config

    def replace(self, config: Config) -> Config:
        with self._lock:
            self._config = config
            return self._config
