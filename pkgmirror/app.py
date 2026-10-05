"""FastAPI HTTP layer exposing the PEP 503 simple index."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.types import Receive, Scope, Send

from .artifact_cache import ChecksumMismatchError
from .config import Config
from .service import ArtifactNotFoundError, MirrorService
from .upstream import UpstreamUnreachableError


def load_config(env: dict[str, str] | None = None) -> Config:
    """Build :class:`Config` from environment variables with defaults."""
    source = os.environ if env is None else env
    return Config(
        upstream_url=source.get("PIP_INDEX_URL", "https://pypi.org/simple"),
        request_timeout=float(source.get("MIRROR_TIMEOUT", "10")),
        retry_attempts=int(source.get("MIRROR_RETRIES", "2")),
        index_ttl_seconds=float(source.get("MIRROR_INDEX_TTL", "300")),
        negative_ttl_seconds=float(source.get("MIRROR_NEGATIVE_TTL", "60")),
        max_cache_bytes=int(source.get("MIRROR_MAX_CACHE_BYTES", str(1024**3))),
        artifact_verify_max_attempts=int(source.get("MIRROR_VERIFY_ATTEMPTS", "3")),
        serve_stale_on_failure=source.get("MIRROR_SERVE_STALE", "1")
        not in ("0", "false", "False", ""),
    )


def create_app(
    config: Config | None = None,
    *,
    cache_dir: str | None = None,
) -> FastAPI:
    config = config or load_config()
    cache_dir = cache_dir or os.environ.get("MIRROR_CACHE_DIR", ".mirror-cache")
    service = MirrorService.build(config, cache_dir)
    return build_app(service)


class LockedFileResponse(FileResponse):
    """FileResponse that holds the cache entry's shared lock while streaming."""

    def __init__(
        self,
        cache,
        key: str,
        path: str,
        source: str,
        on_stream_start=None,
    ) -> None:
        super().__init__(
            path,
            headers={
                "X-Mirror-Source": source,
                "Cache-Control": "public, max-age=0",
            },
        )
        self._cache = cache
        self._key = key
        self._lock_cm = None
        self._on_stream_start = on_stream_start

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self._lock_cm = self._cache.reading(self._key)
        await self._lock_cm.__aenter__()
        try:
            if self._on_stream_start is not None:
                await self._on_stream_start()
            await super().__call__(scope, receive, send)
        finally:
            await self._lock_cm.__aexit__(None, None, None)


def build_app(service: MirrorService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await service.aclose()

    app = FastAPI(title="Package Index Mirror", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    @app.get("/")
    async def root() -> Response:
        return JSONResponse(
            {
                "service": "package-index-mirror",
                "simple_index": "/simple/",
                "config": "/admin/config",
                "health": "/healthz",
            }
        )

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"status": "ok"})

    @app.get("/simple")
    @app.get("/simple/")
    async def simple_root() -> Response:
        return await _serve_index(None)

    @app.get("/simple/{package:path}")
    async def simple_package(package: str) -> Response:
        return await _serve_index(package.strip("/"))

    async def _serve_index(package: str | None) -> Response:
        try:
            result = await service.get_index(package, proxy_base="")
        except UpstreamUnreachableError as exc:
            return JSONResponse(
                {"error": "upstream_unreachable", "detail": str(exc)},
                status_code=502,
                headers={"X-Mirror-Error": "upstream_unreachable"},
            )
        headers = {
            "X-Mirror-Freshness": result.freshness.value,
            "X-Mirror-Age": f"{result.age_seconds:.3f}",
            "Cache-Control": "public, max-age=0",
        }
        if result.negative:
            headers["X-Mirror-Negative"] = "1"
        return Response(
            content=result.body,
            status_code=404 if result.negative else 200,
            media_type=result.content_type,
            headers=headers,
        )

    @app.get("/artifacts/")
    async def artifact(url: str = Query(...)) -> Response:
        try:
            result = await service.get_artifact(url)
        except ChecksumMismatchError as exc:
            return JSONResponse(
                {
                    "error": "checksum_mismatch",
                    "algorithm": exc.algorithm,
                    "expected": exc.expected,
                    "actual": exc.actual,
                    "attempts": exc.attempts,
                },
                status_code=502,
                headers={
                    "X-Mirror-Error": "checksum_mismatch",
                    "X-Mirror-Checksum-Actual": exc.actual,
                },
            )
        except UpstreamUnreachableError as exc:
            return JSONResponse(
                {"error": "upstream_unreachable", "detail": str(exc)},
                status_code=502,
                headers={"X-Mirror-Error": "upstream_unreachable"},
            )
        except ArtifactNotFoundError as exc:
            return JSONResponse(
                {"error": "artifact_not_found", "detail": str(exc)},
                status_code=404,
                headers={"X-Mirror-Error": "artifact_not_found"},
            )

        return LockedFileResponse(
            service.artifact_cache,
            result.key,
            str(result.path),
            result.source,
            on_stream_start=lambda: _evict_if_needed(service),
        )

    @app.get("/admin/config")
    async def get_config() -> Response:
        return JSONResponse(_config_dict(service.config))

    @app.put("/admin/config")
    async def put_config(request: Request) -> Response:
        payload = await request.json()
        changes = {key: value for key, value in payload.items() if key in _CONFIG_FIELDS}
        unknown = sorted(set(payload) - _CONFIG_FIELDS)
        if unknown:
            return JSONResponse({"error": "unknown_fields", "fields": unknown}, status_code=400)
        try:
            new_config = service.config_store.update(**changes)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": "invalid_config", "detail": str(exc)}, status_code=400)
        service.upstream.configure(
            base_url=new_config.upstream_base,
            timeout=new_config.request_timeout,
            retry_attempts=new_config.retry_attempts,
        )
        return JSONResponse(
            {
                "applied": changes,
                "config": _config_dict(new_config),
                "note": "requests in flight keep the old snapshot; new requests use this one",
            }
        )

    @app.post("/admin/evict")
    async def evict(max_bytes: int | None = None) -> Response:
        removed = await service.enforce_cache_limit(max_bytes)
        return JSONResponse({"removed": removed, "limit": max_bytes})

    return app


_CONFIG_FIELDS = {
    "upstream_url",
    "request_timeout",
    "retry_attempts",
    "index_ttl_seconds",
    "negative_ttl_seconds",
    "max_cache_bytes",
    "artifact_verify_max_attempts",
    "serve_stale_on_failure",
}


async def _evict_if_needed(service: MirrorService) -> None:
    limit = service.config.max_cache_bytes
    if limit > 0:
        await service.enforce_cache_limit(limit)


def _config_dict(config: Config) -> dict[str, object]:
    return {field: getattr(config, field) for field in sorted(_CONFIG_FIELDS)}
