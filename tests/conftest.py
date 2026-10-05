"""Shared fixtures: loopback mock upstream, temp cache, configured app."""

from __future__ import annotations

import hashlib
import http.server
import socketserver
import threading
from collections import defaultdict
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from pkgmirror.app import build_app
from pkgmirror.config import Config
from pkgmirror.service import MirrorService


ARTIFACTS: dict[str, bytes] = {}


class MockUpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Serves ``/simple/``, ``/simple/<pkg>/`` and ``/files/...``.

    Behavior can be toggled via server-side state for negative replies,
    injected corruption and failure counters.
    """

    server: "MockUpstreamServer"

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        state = self.server.state
        state["hits"][self.path] += 1
        if not state["reachable"]:
            self.send_error(503)
            return
        if self.path == "/simple/":
            self._html(
                b'<html><body><a href="/simple/demo/">demo</a>'
                b'<a href="/simple/missing/">missing</a></body></html>'
            )
            return
        if self.path == "/simple/demo/":
            body = ARTIFACTS["demo-1.0-py3-none-any.whl"]
            digest = hashlib.sha256(body).hexdigest()
            self._html(
                b'<html><body><a href="/files/demo-1.0-py3-none-any.whl'
                + b"#sha256="
                + digest.encode()
                + b'">demo-1.0</a></body></html>'
            )
            return
        if self.path == "/simple/demo2/":
            body = ARTIFACTS["demo2-2.0-py3-none-any.whl"]
            digest = hashlib.sha256(body).hexdigest()
            self._html(
                b'<html><body><a href="/files/demo2-2.0-py3-none-any.whl'
                + b"#sha256="
                + digest.encode()
                + b'">demo2-2.0</a></body></html>'
            )
            return
        if self.path == "/simple/missing/":
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            return
        if self.path.startswith("/files/"):
            name = self.path.removeprefix("/files/")
            artifact = ARTIFACTS.get(name)
            if artifact is None:
                self.send_error(404)
                return
            if state["corrupt"].get(name, False):
                artifact = artifact + b"CORRUPTED"
            delay = state["delay_seconds"]
            if delay:
                import time

                time.sleep(delay)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(artifact)))
            self.end_headers()
            self.wfile.write(artifact)
            return
        self.send_error(404)

    def _html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MockUpstreamServer(socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), MockUpstreamHandler)
        self.state: dict[str, object] = {
            "reachable": True,
            "hits": defaultdict(int),
            "corrupt": {},
            "delay_seconds": 0.0,
        }
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def hits(self, path: str) -> int:
        return self.state["hits"][path]  # type: ignore[index]

    def shutdown(self) -> None:  # type: ignore[override]
        super().shutdown()
        super().server_close()


@pytest.fixture
def artifact_content() -> bytes:
    body = b"PK\x03\x04 fake wheel content " + b"x" * 4096
    ARTIFACTS["demo-1.0-py3-none-any.whl"] = body
    ARTIFACTS["demo2-2.0-py3-none-any.whl"] = b"PK\x03\x04 second wheel " + b"y" * 8192
    yield body
    ARTIFACTS.clear()


@pytest.fixture
def upstream() -> MockUpstreamServer:
    server = MockUpstreamServer()
    yield server
    server.shutdown()


@pytest.fixture
def config_factory(upstream: MockUpstreamServer):
    def _make(**overrides: object) -> Config:
        values = dict(
            upstream_url=upstream.url,
            request_timeout=5.0,
            retry_attempts=0,
            index_ttl_seconds=300.0,
            negative_ttl_seconds=60.0,
            max_cache_bytes=0,
            artifact_verify_max_attempts=3,
            serve_stale_on_failure=True,
        )
        values.update(overrides)
        return Config(**values)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def client_factory(tmp_path: Path, config_factory):
    clients: list[TestClient] = []

    def _make(config: Config | None = None):
        service = MirrorService.build(
            config or config_factory(), str(tmp_path / "cache")
        )
        app = build_app(service)
        client = TestClient(app)
        clients.append(client)
        return client, service, app

    yield _make

    for client in clients:
        client.close()
