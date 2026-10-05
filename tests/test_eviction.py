"""Cache size cap: LRU eviction frees space and never breaks in-flight reads."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mirror.config import Settings

from .conftest import LiveMirror, artifact_url


def _cache_bytes(cache_dir: str) -> int:
    return sum(p.stat().st_size for p in Path(cache_dir).rglob("*") if p.is_file())


def test_total_cache_size_capped_by_lru_eviction(tmp_path, upstream, http):
    settings = Settings(
        upstream_url=upstream.url,
        cache_dir=str(tmp_path / "cache"),
        request_timeout=2.0,
        retries=1,
        max_cache_bytes=1500,
    )
    with LiveMirror(settings) as mirror:
        for i in range(5):
            name = f"pkg{i}-1.0.tar.gz"
            upstream.add_package("pkg", name, bytes([i]) * 600)
            resp = http.get(artifact_url(mirror.url, upstream, "pkg", name))
            assert resp.status_code == 200
        # cache holds at most ~2 artifacts of 600B plus small metadata
        assert _cache_bytes(settings.cache_dir) <= 1500 + 600
        cached = list((Path(settings.cache_dir) / "artifacts").rglob("*.tar.gz"))
        assert 1 <= len(cached) <= 2
        # evicted artifact is transparently refetched on next access
        resp = http.get(artifact_url(mirror.url, upstream, "pkg", "pkg0-1.0.tar.gz"))
        assert resp.status_code == 200
        assert resp.content == bytes([0]) * 600


def test_eviction_never_removes_in_flight_artifact(tmp_path, upstream, http):
    settings = Settings(
        upstream_url=upstream.url,
        cache_dir=str(tmp_path / "cache"),
        request_timeout=2.0,
        retries=1,
        max_cache_bytes=10,  # tiny: every publish triggers aggressive eviction
    )
    upstream.add_package("demo", "demo-1.0.tar.gz", b"A" * 500)
    upstream.delay = 0.4
    with LiveMirror(settings) as mirror:
        url = artifact_url(mirror.url, upstream, "demo", "demo-1.0.tar.gz")
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: http.get(url), range(4)))
        # in-flight key was protected; every waiter got complete bytes
        assert all(r.status_code == 200 for r in responses)
        assert all(r.content == b"A" * 500 for r in responses)
        assert upstream.hits("artifact", "demo/demo-1.0.tar.gz") == 1
