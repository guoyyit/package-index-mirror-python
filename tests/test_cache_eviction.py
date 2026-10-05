from __future__ import annotations


def _href(client, package: str = "demo") -> str:
    page = client.get(f"/simple/{package}/")
    text = page.text
    start = text.index('href="') + len('href="')
    end = text.index('"', start)
    return text[start:end]


def test_lru_eviction_enforces_size_limit(client_factory, config_factory, upstream, artifact_content):
    demo2_size = len(b"PK\x03\x04 second wheel " + b"y" * 8192)
    client, service, _app = client_factory(
        config_factory(max_cache_bytes=demo2_size)
    )

    href1 = _href(client, "demo")
    href2 = _href(client, "demo2")

    first = client.get(href1)
    assert first.status_code == 200
    second = client.get(href2)
    assert second.status_code == 200

    # demo (older) evicted so total stays <= limit; demo2 remains
    total = service.artifact_cache.total_bytes()
    assert total <= demo2_size

    # evicted entry is re-fetchable from upstream and byte-identical
    upstream.state["reachable"] = True
    again = client.get(href1)
    assert again.status_code == 200
    assert again.content == artifact_content


def test_explicit_evict_endpoint(client_factory, config_factory, upstream, artifact_content):
    demo_size = len(artifact_content)
    client, service, _app = client_factory(config_factory())
    response = client.get(_href(client, "demo"))
    assert response.status_code == 200
    assert service.artifact_cache.total_bytes() == demo_size
    evicted = client.post("/admin/evict", params={"max_bytes": 1})
    assert evicted.status_code == 200
    assert service.artifact_cache.total_bytes() <= 1
    assert len(evicted.json()["removed"]) == 1
