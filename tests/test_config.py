from __future__ import annotations


def test_config_get_and_update(client_factory, config_factory):
    client, service, _app = client_factory()
    current = client.get("/admin/config").json()
    assert current["retry_attempts"] == 0
    assert current["request_timeout"] == 5.0

    updated = client.put(
        "/admin/config",
        json={"request_timeout": 1.5, "retry_attempts": 4},
    ).json()
    assert updated["applied"] == {"request_timeout": 1.5, "retry_attempts": 4}
    assert service.config.request_timeout == 1.5
    assert service.upstream._timeout == 1.5
    assert service.upstream._retry_attempts == 4


def test_config_update_rejects_unknown_fields(client_factory, config_factory):
    client, _service, _app = client_factory()
    response = client.put("/admin/config", json={"bogus": 1})
    assert response.status_code == 400
    assert response.json()["error"] == "unknown_fields"


def test_config_update_rejects_invalid_values(client_factory, config_factory):
    client, _service, _app = client_factory()
    response = client.put("/admin/config", json={"request_timeout": -1})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_config"


def test_inflight_request_uses_old_config(client_factory, config_factory, upstream, artifact_content):
    """A request started before the update keeps the old timeout snapshot."""
    import asyncio
    import httpx

    client, _service, app = client_factory(config_factory(request_timeout=5.0))
    upstream.state["delay_seconds"] = 1.0
    page = client.get("/simple/demo/")
    text = page.text
    start = text.index('href="') + len('href="')
    href = text[start : text.index('"', start)]

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://m") as ac:
            task = asyncio.create_task(ac.get(href))
            await asyncio.sleep(0.1)
            # shrink timeout while the download is in flight
            client.put("/admin/config", json={"request_timeout": 0.01})
            response = await task
            return response

    response = asyncio.run(scenario())
    assert response.status_code == 200
