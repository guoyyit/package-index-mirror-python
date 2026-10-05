"""Orchestration: upstream access, caches, single flight and eviction."""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urldefrag

from .artifact_cache import ArtifactCache, DownloadHandle
from .config import Config, ConfigStore
from .index_cache import IndexCache
from .models import IndexFreshness
from .pep503 import parse_fragment_hash, rewrite_index_page
from .singleflight import SingleFlight
from .upstream import UpstreamClient, UpstreamUnreachableError


@dataclass(slots=True)
class IndexResponse:
    body: bytes
    content_type: str
    negative: bool
    freshness: IndexFreshness
    age_seconds: float


@dataclass(slots=True)
class ArtifactResponse:
    key: str
    path: Path
    source: str  # "cache" | "upstream"


class ArtifactNotFoundError(Exception):
    """Upstream responded 404 for the requested artifact."""


class MirrorService:
    def __init__(
        self,
        config_store: ConfigStore,
        upstream: UpstreamClient,
        index_cache: IndexCache,
        artifact_cache: ArtifactCache,
        index_flight: SingleFlight,
        artifact_flight: SingleFlight,
    ) -> None:
        self.config_store = config_store
        self.upstream = upstream
        self.index_cache = index_cache
        self.artifact_cache = artifact_cache
        self.index_flight = index_flight
        self.artifact_flight = artifact_flight

    @classmethod
    def build(cls, config: Config, cache_dir: str) -> MirrorService:
        return cls(
            config_store=ConfigStore(config),
            upstream=UpstreamClient(
                config.upstream_base,
                timeout=config.request_timeout,
                retry_attempts=config.retry_attempts,
            ),
            index_cache=IndexCache(
                cache_dir,
                ttl_seconds=config.index_ttl_seconds,
                negative_ttl_seconds=config.negative_ttl_seconds,
            ),
            artifact_cache=ArtifactCache(cache_dir),
            index_flight=SingleFlight(),
            artifact_flight=SingleFlight(),
        )

    @property
    def config(self) -> Config:
        return self.config_store.get()

    async def get_index(self, package: str | None, proxy_base: str) -> IndexResponse:
        config = self.config
        if package is None:
            cache_key = "root"
            upstream_path = "/simple/"
        else:
            cache_key = f"pkg:{package.lower()}"
            upstream_path = f"/simple/{package.lower()}/"
        page_url = config.upstream_base + upstream_path

        fresh = self.index_cache.get(cache_key)
        if fresh is not None:
            body, content_type, negative = fresh
            return IndexResponse(
                body=body,
                content_type=content_type,
                negative=negative,
                freshness=IndexFreshness.NEGATIVE if negative else IndexFreshness.FRESH,
                age_seconds=0.0,
            )

        async def _fetch_and_store() -> IndexResponse:
            try:
                result = await self.upstream.fetch(
                    upstream_path,
                    timeout=config.request_timeout,
                    retry_attempts=config.retry_attempts,
                )
            except UpstreamUnreachableError:
                if not config.serve_stale_on_failure:
                    raise
                stale = self.index_cache.get_stale(cache_key)
                if stale is None:
                    raise
                body, _ct, negative, age = stale
                return IndexResponse(
                    body=body,
                    content_type=_ct,
                    negative=negative,
                    freshness=IndexFreshness.STALE,
                    age_seconds=age,
                )

            if result.status_code == 404:
                self.index_cache.put(cache_key, b"", "text/html", negative=True)
                return IndexResponse(
                    body=b"",
                    content_type="text/html",
                    negative=True,
                    freshness=IndexFreshness.NEGATIVE,
                    age_seconds=0.0,
                )

            rewritten = rewrite_index_page(
                result.content, page_url=page_url, proxy_base=proxy_base
            )
            self.index_cache.put(cache_key, rewritten, result.content_type)
            return IndexResponse(
                body=rewritten,
                content_type=result.content_type,
                negative=False,
                freshness=IndexFreshness.FRESH,
                age_seconds=0.0,
            )

        return await self.index_flight.do(cache_key, _fetch_and_store)

    async def get_artifact(self, raw_url: str) -> ArtifactResponse:
        config = self.config
        clean_url, _fragment = urldefrag(raw_url)
        expected_hash = parse_fragment_hash(raw_url)
        if expected_hash is not None:
            key_material = f"{expected_hash[0]}:{expected_hash[1].lower()}"
        else:
            key_material = f"url:{clean_url}"
        key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()

        async def _download_once() -> ArtifactResponse:
            if self.artifact_cache.has_entry(key):
                return ArtifactResponse(key=key, path=self.artifact_cache.entry_path(key), source="cache")

            @contextlib.asynccontextmanager
            async def downloader(url: str):
                async with self.upstream.stream(
                    url,
                    timeout=config.request_timeout,
                    base_url=config.upstream_base,
                ) as response:
                    if response.status_code == 404:
                        raise ArtifactNotFoundError(url)
                    if response.status_code >= 400:
                        raise UpstreamUnreachableError(
                            f"upstream {url} returned {response.status_code}"
                        )
                    yield DownloadHandle(
                        status_code=response.status_code,
                        content_type=response.headers.get(
                            "content-type", "application/octet-stream"
                        ),
                        chunks=response.aiter_bytes(),
                    )

            path, downloaded = await self.artifact_cache.get_or_fetch(
                key,
                clean_url,
                expected_hash,
                downloader,
                max_attempts=config.artifact_verify_max_attempts,
            )
            return ArtifactResponse(
                key=key, path=path, source="upstream" if downloaded else "cache"
            )

        return await self.artifact_flight.do(f"artifact:{key}", _download_once)

    async def enforce_cache_limit(self, max_bytes: int | None = None) -> list[str]:
        config = self.config
        limit = config.max_cache_bytes if max_bytes is None else max_bytes
        if limit <= 0:
            return []
        return await self.artifact_cache.evict_lru(limit)

    async def aclose(self) -> None:
        await self.upstream.aclose()
