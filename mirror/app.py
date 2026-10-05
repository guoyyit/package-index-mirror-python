"""FastAPI application exposing PEP 503 simple index and artifact routes."""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin, urlsplit

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response

from .cache import CacheStore
from .config import ConfigProvider, Settings
from .service import (
    IndexNotFoundError,
    IntegrityFailedError,
    MirrorService,
    UpstreamUnavailableError,
)
from .upstream import UpstreamNotFound

_HREF_RE = re.compile(r'href="([^"]+)"')


def _rewrite_index_links(body: bytes, page_url: str, project: str) -> bytes:
    """Point artifact links at this mirror, carrying upstream path + digest."""

    def repl(match: re.Match[str]) -> str:
        url = match.group(1)
        parts = urlsplit(url)
        filename = parts.path.rsplit("/", 1)[-1]
        if not filename or "." not in filename:
            return match.group(0)  # not an artifact link (e.g. index navigation)
        resolved = urljoin(page_url, url)
        upstream_path = urlsplit(resolved).path
        proxied = (
            f"/artifacts/{quote(project)}/{quote(filename)}"
            f"?u={quote(upstream_path, safe='')}"
            f"&d={quote(parts.fragment, safe='=')}"
        )
        if parts.fragment:
            proxied += f"#{parts.fragment}"
        return f'href="{proxied}"'

    return _HREF_RE.sub(repl, body.decode("utf-8", errors="replace")).encode()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app with a service wired to ``settings``."""
    settings = settings or Settings()
    cache = CacheStore(settings.cache_dir, settings.max_cache_bytes)
    cache.recover()  # startup: drop partial downloads and corrupt entries
    config = ConfigProvider(settings)
    service = MirrorService(cache, config)

    app = FastAPI(title="package-index-mirror")
    app.state.config = config
    app.state.service = service

    def index_response(result) -> HTMLResponse:
        headers = {
            "X-Mirror-Source": result.source,
            "X-Mirror-Freshness": "stale" if result.source == "stale" else "fresh",
        }
        if result.fetched_at is not None:
            headers["X-Mirror-Fetched-At"] = str(result.fetched_at)
        return HTMLResponse(content=result.body, headers=headers)

    @app.get("/simple/", response_class=HTMLResponse)
    def root_index() -> Response:
        try:
            result = service.get_index(None)
        except UpstreamUnavailableError as exc:
            return Response(str(exc), status_code=503, headers={"X-Mirror-Error": "upstream-unavailable"})
        return index_response(result)

    @app.get("/simple/{project}/", response_class=HTMLResponse)
    def project_index(project: str) -> Response:
        try:
            result = service.get_index(project)
        except IndexNotFoundError as exc:
            return Response(str(exc), status_code=404, headers={"X-Mirror-Source": exc.source})
        except UpstreamUnavailableError as exc:
            return Response(str(exc), status_code=503, headers={"X-Mirror-Error": "upstream-unavailable"})
        body = result.body
        if result.source in ("upstream", "cache", "stale"):
            base = f"{config.get().upstream_url.rstrip('/')}/simple/{project}/"
            body = _rewrite_index_links(body, base, project)
        headers = {
            "X-Mirror-Source": result.source,
            "X-Mirror-Freshness": "stale" if result.source == "stale" else "fresh",
        }
        if result.fetched_at is not None:
            headers["X-Mirror-Fetched-At"] = str(result.fetched_at)
        return HTMLResponse(content=body, headers=headers)

    @app.get("/artifacts/{project}/{filename}")
    def artifact(project: str, filename: str, u: str = Query(...), d: str = Query("")) -> Response:
        key = f"{project}/{filename}"
        try:
            result = service.get_artifact(key, u, d)
        except IntegrityFailedError as exc:
            return Response(
                str(exc),
                status_code=502,
                headers={"X-Mirror-Error": "integrity-failed", "X-Mirror-Reason": exc.reason},
            )
        except UpstreamNotFound:
            return Response("artifact not found upstream", status_code=404,
                            headers={"X-Mirror-Error": "not-found"})
        except UpstreamUnavailableError as exc:
            return Response(str(exc), status_code=503, headers={"X-Mirror-Error": "upstream-unavailable"})
        except Exception as exc:  # UpstreamError and friends
            return Response(str(exc), status_code=503, headers={"X-Mirror-Error": "upstream-unavailable"})
        return Response(
            content=result.data,
            media_type="application/octet-stream",
            headers={"X-Mirror-Source": result.source},
        )

    @app.post("/admin/config")
    def update_config(changes: dict) -> dict:
        """Apply a partial config update; in-flight requests keep their snapshot."""
        new = config.update(**changes)
        return {"applied": sorted(changes), "settings": new.__dict__}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return app
