"""TTL caching, stale-if-error fallback, negative caching, distinct failures."""

from __future__ import annotations

import time


def test_fresh_cache_hit_avoids_upstream(mirror, upstream, http):
    upstream.add_package("demo", "demo-1.0.tar.gz", b"X")
    first = http.get(f"{mirror.url}/simple/demo/")
    assert first.headers["X-Mirror-Source"] == "upstream"
    second = http.get(f"{mirror.url}/simple/demo/")  # within index_ttl
    assert second.headers["X-Mirror-Source"] == "cache"
    assert second.headers["X-Mirror-Freshness"] == "fresh"
    assert second.content == first.content
    assert upstream.hits("index", "demo") == 1


def test_stale_fallback_when_upstream_down(mirror, upstream, http):
    upstream.add_package("demo", "demo-1.0.tar.gz", b"X")
    ok = http.get(f"{mirror.url}/simple/demo/")
    assert ok.status_code == 200

    time.sleep(0.3)  # index_ttl=0.2 -> entry now stale
    upstream.online = False
    resp = http.get(f"{mirror.url}/simple/demo/")
    assert resp.status_code == 200
    assert resp.headers["X-Mirror-Source"] == "stale"
    assert resp.headers["X-Mirror-Freshness"] == "stale"
    assert "X-Mirror-Fetched-At" in resp.headers
    assert resp.content == ok.content


def test_no_cache_and_upstream_down_is_503(mirror, upstream, http):
    upstream.add_package("demo", "demo-1.0.tar.gz", b"X")
    upstream.online = False  # down before anything was cached
    resp = http.get(f"{mirror.url}/simple/demo/")
    assert resp.status_code == 503
    assert resp.headers["X-Mirror-Error"] == "upstream-unavailable"


def test_negative_cache_for_unknown_project(mirror, upstream, http):
    first = http.get(f"{mirror.url}/simple/ghost/")
    assert first.status_code == 404
    assert first.headers["X-Mirror-Source"] == "upstream"
    assert upstream.hits("index", "ghost") == 1

    second = http.get(f"{mirror.url}/simple/ghost/")  # within negative_ttl
    assert second.status_code == 404
    assert second.headers["X-Mirror-Source"] == "negative-cache"
    assert upstream.hits("index", "ghost") == 1  # no extra upstream hit

    time.sleep(0.3)  # negative_ttl=0.2 -> expired, upstream consulted again
    third = http.get(f"{mirror.url}/simple/ghost/")
    assert third.status_code == 404
    assert upstream.hits("index", "ghost") == 2


def test_negative_cache_does_not_mask_new_package(mirror, upstream, http):
    assert http.get(f"{mirror.url}/simple/newpkg/").status_code == 404
    time.sleep(0.3)  # let the negative entry expire
    upstream.add_package("newpkg", "newpkg-1.0.tar.gz", b"N")
    resp = http.get(f"{mirror.url}/simple/newpkg/")
    assert resp.status_code == 200
    assert b"newpkg-1.0.tar.gz" in resp.content
