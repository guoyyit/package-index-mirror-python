from __future__ import annotations

import time


def test_negative_entry_expires_and_revalidates(client_factory, config_factory, upstream):
    client, _service, _app = client_factory(
        config_factory(negative_ttl_seconds=0.05)
    )
    assert client.get("/simple/missing/").status_code == 404
    assert upstream.hits("/simple/missing/") == 1

    # still negative within TTL: no upstream contact even when "down"
    upstream.state["reachable"] = False
    assert client.get("/simple/missing/").status_code == 404
    assert upstream.hits("/simple/missing/") == 1

    time.sleep(0.1)
    # expired negative -> revalidation attempted; upstream down -> stale
    # negative fallback, annotated as stale rather than fresh negative.
    response = client.get("/simple/missing/")
    assert response.status_code == 404
    assert response.headers["x-mirror-freshness"] == "stale"
    assert response.headers["x-mirror-negative"] == "1"

    # once back up, revalidation yields a fresh 404 negative again
    upstream.state["reachable"] = True
    response = client.get("/simple/missing/")
    assert response.status_code == 404
    assert response.headers["x-mirror-freshness"] == "negative"
    assert upstream.hits("/simple/missing/") >= 2
