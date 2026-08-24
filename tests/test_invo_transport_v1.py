from __future__ import annotations

import asyncio
import base64
import json

import httpx

from hlcopy.discovery.invo_source import InvoReadOnlyClient


def test_invo_client_retries_transient_failure_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(529, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"items": []})

    async def run() -> None:
        async with InvoReadOnlyClient(
            access_token="access",
            transport=httpx.MockTransport(handler),
        ) as client:
            result = await client.feed()
            assert result == {"items": []}

    asyncio.run(run())
    assert calls == 2


def test_invo_client_accepts_base64_encoded_json_response() -> None:
    encoded = base64.b64encode(json.dumps({"items": [{"id": "p1"}]}).encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=encoded)

    async def run() -> None:
        async with InvoReadOnlyClient(
            access_token="access",
            transport=httpx.MockTransport(handler),
        ) as client:
            result = await client.feed()
            assert result["items"] == [{"id": "p1"}]

    asyncio.run(run())
