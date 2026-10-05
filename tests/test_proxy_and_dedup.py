"""Basic proxying, cache hits, and concurrent single-flight dedup."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .conftest import artifact_url, parse_links


def test_index_proxied_and_links_rewritten(mirror, upstream, http):
    digest = upstream.add_package("demo", "demo-1.0.tar.gz", b"SDIST")
    resp = http.get(f"{mirror.url}/simple/demo/")
    assert resp.status_code == 200
    assert resp.headers["X-Mirror-Source"] == "upstream"
    links = parse_links(resp.content)
    href = links["demo-1.0.tar.gz"]
    assert href.startswith("/artifacts/demo/demo-1.0.tar.gz?")
    assert f"#sha256={digest}" in href  # fragment kept for client-side checks
    # root index works too
    root = http.get(f"{mirror.url}/simple/")
    assert root.status_code == 200 and b"demo" in root.content


def test_artifact_fetch_then_cache_hit_byte_identical(mirror, upstream, http):
    content = b"WHEEL" * 1024
    upstream.add_package("demo", "demo-1.0-py3-none-any.whl", content)
    url = artifact_url(mirror.url, upstream, "demo", "demo-1.0-py3-none-any.whl")
    first = http.get(url)
    assert first.status_code == 200
    assert first.headers["X-Mirror-Source"] == "upstream"
    assert first.content == content
    second = http.get(url)
    assert second.headers["X-Mirror-Source"] == "cache"
    assert second.content == content  # byte-for-byte identical
    assert upstream.hits("artifact", "demo/demo-1.0-py3-none-any.whl") == 1


def test_concurrent_requests_trigger_single_upstream_download(mirror, upstream, http):
    content = b"CONCURRENT" * 4096
    upstream.add_package("demo", "demo-2.0.tar.gz", content)
    upstream.delay = 0.5  # widen the race window so requests overlap
    url = artifact_url(mirror.url, upstream, "demo", "demo-2.0.tar.gz")

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: http.get(url), range(8)))

    assert all(r.status_code == 200 for r in responses)
    assert all(r.content == content for r in responses)
    sources = {r.headers["X-Mirror-Source"] for r in responses}
    assert sources <= {"upstream", "cache"}
    assert upstream.hits("artifact", "demo/demo-2.0.tar.gz") == 1
