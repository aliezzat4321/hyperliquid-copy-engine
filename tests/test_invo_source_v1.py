from __future__ import annotations

import asyncio

import httpx

from hlcopy.discovery.invo_source import (
    InvoReadOnlyClient,
    normalize_portfolio_candidate,
    normalize_trade_event,
    portfolio_candidates,
    verified_trade_events,
)


def test_normalize_invo_portfolio_candidate() -> None:
    row = {
        "id": "portfolio-1",
        "ownerId": "user-1",
        "owner": {"id": "user-1", "username": "carmine"},
        "name": "10k-300k(OPEN)",
        "closedPositions": 395,
        "wonPositions": 383,
        "lostPositions": 12,
        "winRate": 97,
        "percentChange": 2900,
        "currentWinStreak": 21,
        "followerCount": 1430,
        "createdAt": "2026-01-01T00:00:00Z",
        "liquidated": False,
    }

    candidate = normalize_portfolio_candidate(row)

    assert candidate is not None
    assert candidate.portfolio_id == "portfolio-1"
    assert candidate.owner_id == "user-1"
    assert candidate.username == "carmine"
    assert candidate.closed_positions == 395
    assert candidate.win_rate == 97
    assert candidate.win_loss_ratio == 383 / 12


def test_normalize_verified_invo_trade_event() -> None:
    post = {
        "id": "post-1",
        "createdAt": "2026-08-20T12:00:00Z",
        "update": {
            "ticker": "hype",
            "directionLong": True,
            "leverage": 10,
            "entryPrice": 42.5,
            "closingPrice": 44.1,
            "isOpen": False,
            "verifiedTrade": True,
            "portfolio": {"id": "portfolio-1"},
            "owner": {"id": "user-1", "username": "carmine"},
            "baseId": "base-1",
            "baseShortId": "short-1",
        },
    }

    event = normalize_trade_event(post)

    assert event is not None
    assert event.coin == "HYPE"
    assert event.direction == "LONG"
    assert event.leverage == 10
    assert event.entry_price == 42.5
    assert event.closing_price == 44.1
    assert event.verified_trade is True
    assert event.portfolio_id == "portfolio-1"
    assert event.owner_id == "user-1"


def test_verified_trade_events_excludes_unverified_posts() -> None:
    payload = {
        "items": [
            {
                "id": "good",
                "update": {
                    "ticker": "SOL",
                    "directionLong": False,
                    "verifiedTrade": True,
                },
            },
            {
                "id": "bad",
                "update": {
                    "ticker": "BTC",
                    "directionLong": True,
                    "verifiedTrade": False,
                },
            },
        ]
    }

    events = verified_trade_events(payload)

    assert [event.post_id for event in events] == ["good"]
    assert events[0].direction == "SHORT"


def test_portfolio_candidates_skips_rows_without_stable_ids() -> None:
    payload = {
        "items": [
            {"id": "p1", "ownerId": "u1", "owner": {"username": "one"}},
            {"id": "p2", "owner": {"username": "missing-owner"}},
        ]
    }

    candidates = portfolio_candidates(payload)

    assert [candidate.portfolio_id for candidate in candidates] == ["p1"]


def test_read_only_client_uses_only_discovery_feed_endpoints() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/v1_0/trending/get_portfolios_pl":
            return httpx.Response(200, json={"items": []})
        if request.url.path == "/v1_0/trending/get_users":
            return httpx.Response(200, json={"items": []})
        if request.url.path == "/v1_0/posts/get_feed":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(500, json={"unexpected": request.url.path})

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with InvoReadOnlyClient(
            access_token="test",
            transport=transport,
        ) as client:
            await client.discover_portfolios(filter_name="trending")
            await client.trending_users()
            await client.feed()

    asyncio.run(run())

    assert seen == [
        ("POST", "/v1_0/trending/get_portfolios_pl"),
        ("POST", "/v1_0/trending/get_users"),
        ("POST", "/v1_0/posts/get_feed"),
    ]
