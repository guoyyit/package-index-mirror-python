from __future__ import annotations

from urllib.parse import parse_qs, urlparse


def test_root_and_package_index_proxied(client_factory, upstream, artifact_content):
    client, _service, _app = client_factory()

    root = client.get("/simple/")
    assert root.status_code == 200
    assert root.headers["x-mirror-freshness"] == "fresh"
    assert b"/simple/demo/" in root.content

    page = client.get("/simple/demo/")
    assert page.status_code == 200
    href = page.content.decode()
    assert "/artifacts/?url=" in href
    # hash fragment must survive rewriting so verification can use it
    assert "sha256%3D" in href


def test_artifact_download_and_cache_byte_identical(
    client_factory, upstream, artifact_content
):
    client, _service, _app = client_factory()
    page = client.get("/simple/demo/")
    proxy_href = _artifact_url(page.text)

    first = client.get(proxy_href)
    assert first.status_code == 200
    assert first.headers["x-mirror-source"] == "upstream"
    assert first.content == artifact_content

    second = client.get(proxy_href)
    assert second.status_code == 200
    assert second.headers["x-mirror-source"] == "cache"
    assert second.content == artifact_content

    # second hit must not contact the upstream for the file
    assert upstream.hits("/files/demo-1.0-py3-none-any.whl") == 1


def _artifact_url(page_html: str) -> str:
    start = page_html.index('href="') + len('href="')
    end = page_html.index('"', start)
    href = page_html[start:end]
    parsed = urlparse(href)
    return f"{parsed.path}?{parsed.query}"
