from __future__ import annotations

import asyncio

import httpx


def test_concurrent_artifact_requests_single_upstream_fetch(
    client_factory, upstream, artifact_content
):
    client, _service, app = client_factory()
    upstream.state["delay_seconds"] = 0.5
    page = client.get("/simple/demo/")
    proxy_href = _artifact_href(page.text)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://m") as ac:
            return await asyncio.gather(
                *[ac.get(proxy_href) for _ in range(20)]
            )

    responses = asyncio.run(run())

    assert len(responses) == 20
    assert all(response.status_code == 200 for response in responses)
    assert all(response.content == artifact_content for response in responses)
    assert upstream.hits("/files/demo-1.0-py3-none-any.whl") == 1

    sources = {response.headers["x-mirror-source"] for response in responses}
    assert sources == {"upstream"}



def _artifact_href(page_html: str) -> str:
    start = page_html.index('href="') + len('href="')
    end = page_html.index('"', start)
    return page_html[start:end]
