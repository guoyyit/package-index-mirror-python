"""Crash mid-download and restart recovery semantics."""

from __future__ import annotations

import time
from pathlib import Path

from .conftest import LiveMirror, artifact_url


def test_partial_download_never_served_and_cleaned_on_restart(mirror, upstream, http):
    content = b"FULL-ARTIFACT"
    upstream.add_package("demo", "demo-1.0.tar.gz", content)
    cache_dir = Path(mirror.settings.cache_dir)

    # simulate a crash mid-download: a leftover partial temp file
    part = cache_dir / "tmp" / "demo%2Fdemo-1.0.tar.gz.1234.deadbeef.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"PARTIAL")

    url = artifact_url(mirror.url, upstream, "demo", "demo-1.0.tar.gz")
    resp = http.get(url)  # must fetch from upstream, never serve the partial
    assert resp.status_code == 200
    assert resp.content == content
    assert resp.headers["X-Mirror-Source"] == "upstream"

    # restart the service against the same cache dir: tmp residue is cleaned
    with LiveMirror(mirror.settings) as mirror2:
        assert not part.exists()
        resp2 = http.get(artifact_url(mirror2.url, upstream, "demo", "demo-1.0.tar.gz"))
        assert resp2.headers["X-Mirror-Source"] == "cache"
        assert resp2.content == content
    assert upstream.hits("artifact", "demo/demo-1.0.tar.gz") == 1


def test_cache_survives_restart_and_serves_offline(mirror, upstream, http):
    content = b"DURABLE"
    upstream.add_package("demo", "demo-1.0.tar.gz", content)
    url = artifact_url(mirror.url, upstream, "demo", "demo-1.0.tar.gz")
    assert http.get(url).status_code == 200
    assert http.get(f"{mirror.url}/simple/demo/").status_code == 200

    upstream.stop()  # upstream gone entirely
    with LiveMirror(mirror.settings) as mirror2:
        # artifact: served from cache, byte-identical
        resp = http.get(artifact_url(mirror2.url, upstream, "demo", "demo-1.0.tar.gz"))
        assert resp.status_code == 200 and resp.content == content
        assert resp.headers["X-Mirror-Source"] == "cache"
        # index: TTL expired by now or upstream down -> stale fallback, marked stale
        time.sleep(0.3)  # index_ttl=0.2 in fixtures
        idx = http.get(f"{mirror2.url}/simple/demo/")
        assert idx.status_code == 200
        assert idx.headers["X-Mirror-Source"] == "stale"
        assert idx.headers["X-Mirror-Freshness"] == "stale"


def test_corrupt_metadata_dropped_on_recovery(mirror, upstream, http):
    upstream.add_package("demo", "demo-1.0.tar.gz", b"X")
    assert http.get(f"{mirror.url}/simple/demo/").status_code == 200

    meta_dir = Path(mirror.settings.cache_dir) / "meta"
    for jpath in meta_dir.glob("*.json"):
        jpath.write_bytes(b"{corrupted")  # simulate torn write / disk damage

    with LiveMirror(mirror.settings) as mirror2:
        # corrupt entries are gone; index is refetched from upstream
        assert all(not p.name.endswith(".json") or p.read_text().startswith("{")
                   for p in meta_dir.glob("*"))
        resp = http.get(f"{mirror2.url}/simple/demo/")
        assert resp.status_code == 200
        assert resp.headers["X-Mirror-Source"] == "upstream"
