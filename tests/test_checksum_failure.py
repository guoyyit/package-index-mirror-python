from __future__ import annotations

from pathlib import Path


def _href_for_demo(client) -> str:
    page = client.get("/simple/demo/")
    text = page.text
    start = text.index('href="') + len('href="')
    end = text.index('"', start)
    return text[start:end]


def test_checksum_mismatch_quarantined_and_not_served(
    client_factory, upstream, artifact_content, tmp_path
):
    upstream.state["corrupt"]["demo-1.0-py3-none-any.whl"] = True
    client, service, _app = client_factory()
    href = _href_for_demo(client)

    response = client.get(href)
    assert response.status_code == 502
    assert response.headers["x-mirror-error"] == "checksum_mismatch"
    body = response.json()
    assert body["error"] == "checksum_mismatch"
    assert body["attempts"] == 3
    assert body["actual"] != body["expected"]

    # bad artifact never reached the servable cache
    assert service.artifact_cache.total_bytes() == 0
    quarantine = list((Path(tmp_path) / "cache" / "quarantine").iterdir())
    assert len(quarantine) == 3
    assert all(p.read_bytes() == artifact_content + b"CORRUPTED" for p in quarantine)

    # retries bounded: exactly max_attempts upstream downloads
    assert upstream.hits("/files/demo-1.0-py3-none-any.whl") == 3


def test_checksum_mismatch_distinct_from_unreachable(
    client_factory, upstream, artifact_content
):
    upstream.state["corrupt"]["demo-1.0-py3-none-any.whl"] = True
    client, _service, _app = client_factory()
    bad = client.get(_href_for_demo(client))
    assert bad.headers["x-mirror-error"] == "checksum_mismatch"

    # a dead upstream is a different, distinguishable error
    client2, _service2, _app2 = client_factory()
    upstream.state["reachable"] = False
    down = client2.get("/artifacts/?url=http%3A%2F%2F127.0.0.1%3A1%2Ff%23sha256%3Daa")
    assert down.status_code == 502
    assert down.headers["x-mirror-error"] == "upstream_unreachable"
