"""Shared fixtures: a loopback mock upstream and a live mirror server."""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
import uvicorn

from mirror.app import create_app
from mirror.config import Settings


class MockUpstream:
    """Loopback PEP 503 index + artifact server with controllable failures."""

    def __init__(self) -> None:
        self.packages: dict[str, dict[str, bytes]] = {}
        self.declared_digests: dict[tuple[str, str], str] = {}
        self.online = True
        self.delay = 0.0
        self.artifact_hits: dict[str, int] = {}
        self.index_hits: dict[str, int] = {}
        self._lock = threading.Lock()
        self._server = HTTPServer(("127.0.0.1", 0), self._handler_class())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    # --- fixture API ---
    def add_package(self, project: str, filename: str, content: bytes) -> str:
        """Register a package; returns the declared sha256 hex digest."""
        self.packages.setdefault(project, {})[filename] = content
        digest = hashlib.sha256(content).hexdigest()
        self.declared_digests[(project, filename)] = digest
        return digest

    def declare_digest(self, project: str, filename: str, hexdigest: str) -> None:
        """Override the digest advertised in the index (simulate mismatch)."""
        self.declared_digests[(project, filename)] = hexdigest

    def hits(self, kind: str, name: str) -> int:
        with self._lock:
            return (self.artifact_hits if kind == "artifact" else self.index_hits).get(name, 0)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # --- HTTP plumbing ---
    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _count(self, bucket: dict[str, int], name: str) -> None:
                with outer._lock:
                    bucket[name] = bucket.get(name, 0) + 1

            def _send(self, status: int, body: bytes, content_type: str = "text/html") -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if not outer.online:
                    self.connection.close()  # simulate unreachable host
                    return
                path = self.path.split("?", 1)[0].split("#", 1)[0]
                if path == "/simple/":
                    self._count(outer.index_hits, "")
                    links = "".join(f'<a href="/simple/{p}/">{p}</a>' for p in sorted(outer.packages))
                    self._send(200, f"<html><body>{links}</body></html>".encode())
                    return
                if path.startswith("/simple/"):
                    project = path.strip("/").split("/")[-1]
                    self._count(outer.index_hits, project)
                    files = outer.packages.get(project)
                    if files is None:
                        self._send(404, b"no such project")
                        return
                    links = "".join(
                        f'<a href="/packages/{project}/{fn}'
                        f"#sha256={outer.declared_digests[(project, fn)]}\">{fn}</a>"
                        for fn in sorted(files)
                    )
                    self._send(200, f"<html><body>{links}</body></html>".encode())
                    return
                if path.startswith("/packages/"):
                    _, _, project, filename = path.split("/", 3)
                    self._count(outer.artifact_hits, f"{project}/{filename}")
                    content = outer.packages.get(project, {}).get(filename)
                    if content is None:
                        self._send(404, b"no such file")
                        return
                    if outer.delay:
                        time.sleep(outer.delay)
                    self._send(200, content, "application/octet-stream")
                    return
                self._send(404, b"nope")

        return Handler


@pytest.fixture()
def upstream() -> MockUpstream:
    server = MockUpstream()
    yield server
    server.stop()


class LiveMirror:
    """Runs the mirror app under uvicorn on a loopback port."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.app = create_app(settings)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "LiveMirror":
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("mirror server did not start")

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture()
def mirror_settings(tmp_path, upstream) -> Settings:
    return Settings(
        upstream_url=upstream.url,
        cache_dir=str(tmp_path / "cache"),
        request_timeout=2.0,
        retries=1,
        index_ttl=0.2,
        negative_ttl=0.2,
        max_cache_bytes=1 << 20,
    )


@pytest.fixture()
def mirror(mirror_settings) -> LiveMirror:
    with LiveMirror(mirror_settings) as server:
        yield server


@pytest.fixture()
def http() -> httpx.Client:
    with httpx.Client(timeout=10.0) as client:
        yield client


def artifact_url(mirror_url: str, upstream: MockUpstream, project: str, filename: str) -> str:
    """Build the proxied artifact URL the rewritten index would point to."""
    from urllib.parse import quote

    digest = upstream.declared_digests[(project, filename)]
    return (
        f"{mirror_url}/artifacts/{quote(project)}/{quote(filename)}"
        f"?u={quote(f'/packages/{project}/{filename}', safe='')}&d=sha256%3D{digest}"
    )


def parse_links(body: bytes) -> dict[str, str]:
    """Extract {filename: href} from a PEP 503 project page."""
    import re

    return {
        m.group(2).decode(): m.group(1).decode()
        for m in re.finditer(rb'href="([^"]+)"[^>]*>([^<]+)<', body)
    }
