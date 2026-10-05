"""HTTP client for the upstream package index."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx


class UpstreamUnreachableError(Exception):
    """Upstream could not be reached or exhausted all transport retries."""


@dataclass(slots=True)
class FetchResult:
    status_code: int
    content: bytes
    content_type: str


class UpstreamClient:
    """Small async client around :class:`httpx.AsyncClient`.

    A single configured client is created lazily; per-request timeout and
    retry counts are read at request start, so config updates affect new
    requests without touching in-flight ones.
    """

    def __init__(self, base_url: str, *, timeout: float, retry_attempts: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._retry_attempts = retry_attempts
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    def configure(self, *, base_url: str, timeout: float, retry_attempts: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._retry_attempts = retry_attempts

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=True, trust_env=False)
        return self._client

    def _resolve(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        if path_or_url.startswith("/"):
            return f"{self._base_url}{path_or_url}"
        return f"{self._base_url}/{path_or_url}"

    async def fetch(
        self,
        path_or_url: str,
        *,
        timeout: float | None = None,
        retry_attempts: int | None = None,
    ) -> FetchResult:
        """Fetch a full body (index pages). 404 is a normal FetchResult.

        Connection errors and 5xx responses are retried; the final failure
        surfaces as :class:`UpstreamUnreachableError`, which is distinct
        from checksum failures handled at the cache layer.
        """
        attempts = self._retry_attempts if retry_attempts is None else retry_attempts
        request_timeout = self._timeout if timeout is None else timeout
        url = self._resolve(path_or_url)
        client = self._ensure_client()
        last_exc: Exception | None = None
        for attempt in range(attempts + 1):
            try:
                response = await client.get(url, timeout=request_timeout)
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if response.status_code < 500:
                    return FetchResult(
                        status_code=response.status_code,
                        content=response.content,
                        content_type=response.headers.get("content-type", "text/html"),
                    )
                last_exc = UpstreamUnreachableError(
                    f"upstream {url} returned {response.status_code}"
                )
            if attempt < attempts:
                await asyncio.sleep(0)
        raise UpstreamUnreachableError(f"upstream {url} unreachable") from last_exc

    @asynccontextmanager
    async def stream(
        self,
        path_or_url: str,
        *,
        timeout: float,
        base_url: str | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """Open a streaming GET for artifact download (no body retry here)."""
        base = self._base_url if base_url is None else base_url.rstrip("/")
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        elif path_or_url.startswith("/"):
            url = f"{base}{path_or_url}"
        else:
            url = f"{base}/{path_or_url}"
        client = self._ensure_client()
        try:
            async with client.stream("GET", url, timeout=timeout) as response:
                if response.status_code >= 500:
                    raise UpstreamUnreachableError(
                        f"upstream {url} returned {response.status_code}"
                    )
                yield response
        except httpx.HTTPError as exc:
            raise UpstreamUnreachableError(f"upstream {url} unreachable") from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
