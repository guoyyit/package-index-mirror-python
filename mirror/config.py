"""Configuration for the mirror service.

Config updates apply to *new* requests only; in-flight requests keep the
snapshot they started with (see :class:`ConfigProvider`).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of service settings."""

    upstream_url: str = "https://pypi.org"
    request_timeout: float = 10.0
    retries: int = 2
    index_ttl: float = 300.0
    negative_ttl: float = 60.0
    stale_if_error: bool = True
    cache_dir: str = ".mirror-cache"
    max_cache_bytes: int = 1 << 30
    verify_retries: int = 1


class ConfigProvider:
    """Thread-safe holder of the current :class:`Settings` snapshot."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._lock = threading.Lock()
        self._settings = settings or Settings()

    def get(self) -> Settings:
        """Return the current immutable snapshot."""
        with self._lock:
            return self._settings

    def update(self, **changes: object) -> Settings:
        """Atomically replace settings; returns the new snapshot."""
        with self._lock:
            self._settings = replace(self._settings, **changes)
            return self._settings
