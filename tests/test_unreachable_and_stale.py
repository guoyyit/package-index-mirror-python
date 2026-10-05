from __future__ import annotations


def test_stale_index_served_with_freshness_header(
    client_factory, config_factory, upstream, artifact_content
):
    client, _service, _app = client_factory(
        config_factory(index_ttl_seconds=0.05)
    )
    first = client.get("/simple/demo/")
    assert first.status_code == 200
    assert first.headers["x-mirror-freshness"] == "fresh"

    import time

    time.sleep(0.1)
    upstream.state["reachable"] = False
    second = client.get("/simple/demo/")
    assert second.status_code == 200
    assert second.headers["x-mirror-freshness"] == "stale"
    assert float(second.headers["x-mirror-age"]) >= 0.1
    # body is the previously cached, rewritten page
    assert b"/artifacts/?url=" in second.content


def test_unreachable_error_code_is_distinct(client_factory, upstream):
    client, _service, _app = client_factory()
    upstream.state["reachable"] = False
    response = client.get("/simple/never-fetched/")
    assert response.status_code == 502
    assert response.headers["x-mirror-error"] == "upstream_unreachable"
    assert response.json()["error"] == "upstream_unreachable"

    root = client.get("/simple/")
    assert root.status_code == 502
    assert root.headers["x-mirror-error"] == "upstream_unreachable"


def test_stale_disabled_raises_instead(client_factory, config_factory, upstream):
    client, _service, _app = client_factory(
        config_factory(serve_stale_on_failure=False)
    )
    client.get("/simple/demo/")
    upstream.state["reachable"] = False
    response = client.get("/simple/demo/")
    assert response.status_code == 502
    assert response.headers["x-mirror-error"] == "upstream_unreachable"


def test_negative_cache_served_then_revalidated(client_factory, upstream):
    client, _service, _app = client_factory()
    first = client.get("/simple/missing/")
    assert first.status_code == 404
    assert first.headers["x-mirror-negative"] == "1"
    assert first.headers["x-mirror-freshness"] == "negative"

    # second call within TTL must not contact upstream at all
    upstream.state["reachable"] = False
    second = client.get("/simple/missing/")
    assert second.status_code == 404
    assert second.headers["x-mirror-negative"] == "1"
