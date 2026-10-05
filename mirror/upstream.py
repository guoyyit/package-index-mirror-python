"""HTTP client for the upstream package index with timeout and retries."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

class UpstreamError(Exception):
    """Upstream unreachable or unusable (transport error / 5xx / timeouts)."""


class UpstreamNotFound(Exception):
    """Upstream returned 404 for the requested resource."""


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    body: bytes
    content_type: str


class UpstreamClient:
    """Fetches index pages and artifacts from the configured upstream."""

    def __init__(self, base_url: str, timeout: float = 10.0, retries: int = 2) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._retries = retries

    def fetch(self, path: str) -> UpstreamResponse:
        """GET ``path`` from upstream with retry/timeout policy."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        last_error: Exception | None = None
        for _ in range(self._retries + 1):
            try:
                resp = httpx.get(url, timeout=self._timeout, follow_redirects=True)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if resp.status_code == 404:
                raise UpstreamNotFound(path)
            if resp.status_code >= 500:
                last_error = UpstreamError(f"upstream {resp.status_code} for {path}")
                continue
            if resp.status_code >= 400:
                raise UpstreamError(f"upstream {resp.status_code} for {path}")
            return UpstreamResponse(
                status=resp.status_code,
                body=resp.content,
                content_type=resp.headers.get("content-type", "application/octet-stream"),
            )
        raise UpstreamError(f"upstream unreachable for {path}: {last_error}")
