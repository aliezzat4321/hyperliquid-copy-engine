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


def test_invo_client_retries_request_after_successful_401_refresh_with_one_attempt() -> None:
    post_tokens: list[str] = []
    refresh_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        authorization = request.headers.get("Authorization", "")
        if request.url.path == "/v1_0/auth/refresh_token":
            refresh_calls += 1
            assert authorization == "Bearer refresh-token"
            return httpx.Response(200, json={"accessToken": "fresh-access"})
        post_tokens.append(authorization)
        if authorization == "Bearer stale-access":
            return httpx.Response(401, json={"error": "expired"})
        assert authorization == "Bearer fresh-access"
        return httpx.Response(200, json={"items": []})

    async def run() -> None:
        async with InvoReadOnlyClient(
            access_token="stale-access",
            refresh_token="refresh-token",
            retry_attempts=1,
            transport=httpx.MockTransport(handler),
        ) as client:
            result = await client.feed()
            assert result == {"items": []}

    asyncio.run(run())
    assert refresh_calls == 1
    assert post_tokens == ["Bearer stale-access", "Bearer fresh-access"]


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
