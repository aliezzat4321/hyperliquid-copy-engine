from __future__ import annotations

import asyncio

import httpx

from hlcopy.discovery.invo_source import InvoReadOnlyClient


def test_refresh_token_bootstraps_access_token_without_manual_session() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("Authorization")))
        if request.url.path == "/v1_0/auth/refresh_token":
            assert request.method == "GET"
            assert request.headers["Authorization"] == "Bearer refresh-token"
            return httpx.Response(200, json={"accessToken": "short-lived-access"})
        if request.url.path == "/v1_0/trending/get_portfolios_pl":
            assert request.method == "POST"
            assert request.headers["Authorization"] == "Bearer short-lived-access"
            return httpx.Response(200, json={"items": []})
        return httpx.Response(500)

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with InvoReadOnlyClient(
            refresh_token="refresh-token",
            transport=transport,
        ) as client:
            await client.discover_portfolios(filter_name="trending")

    asyncio.run(run())

    assert [item[:2] for item in seen] == [
        ("GET", "/v1_0/auth/refresh_token"),
        ("POST", "/v1_0/trending/get_portfolios_pl"),
    ]
