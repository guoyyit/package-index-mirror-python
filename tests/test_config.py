"""Runtime config updates: new requests see new settings, in-flight keep old."""

from __future__ import annotations

from mirror.config import ConfigProvider, Settings

from .conftest import MockUpstream


def test_snapshot_isolation_for_in_flight_requests():
    provider = ConfigProvider(Settings(request_timeout=5.0, retries=3))
    in_flight = provider.get()  # snapshot taken when the request started
    provider.update(request_timeout=0.1, retries=0)
    assert in_flight.request_timeout == 5.0 and in_flight.retries == 3
    fresh = provider.get()
    assert fresh.request_timeout == 0.1 and fresh.retries == 0


def test_admin_config_endpoint_applies_to_new_requests(mirror, upstream, http):
    upstream.add_package("demo", "demo-1.0.tar.gz", b"X")
    resp = http.post(f"{mirror.url}/admin/config", json={"index_ttl": 0.0})
    assert resp.status_code == 200
    assert resp.json()["settings"]["index_ttl"] == 0.0

    http.get(f"{mirror.url}/simple/demo/")
    again = http.get(f"{mirror.url}/simple/demo/")
    # ttl=0 means no fresh cache window: second request hits upstream again
    assert again.headers["X-Mirror-Source"] == "upstream"
    assert upstream.hits("index", "demo") == 2


def test_upstream_url_switch_takes_effect(mirror, upstream, http):
    other = MockUpstream()
    try:
        upstream.add_package("demo", "demo-1.0.tar.gz", b"FROM-A")
        other.add_package("demo", "demo-1.0.tar.gz", b"FROM-B")
        resp = http.get(f"{mirror.url}/simple/demo/")
        assert resp.status_code == 200

        http.post(f"{mirror.url}/admin/config", json={"upstream_url": other.url, "index_ttl": 0.0})
        # artifact cache key is upstream-agnostic; use a fresh project to observe B
        other.add_package("only-b", "only_b-1.0.tar.gz", b"B-ONLY")
        resp = http.get(f"{mirror.url}/simple/only-b/")
        assert resp.status_code == 200
        assert b"only_b-1.0.tar.gz" in resp.content
    finally:
        other.stop()
