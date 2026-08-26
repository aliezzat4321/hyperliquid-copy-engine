from __future__ import annotations

import asyncio
from typing import Any

from hlcopy.discovery.invo_direct_history_job import (
    collect_direct_history,
    normalize_direct_investment,
    select_candidates,
)


def _investment(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "trade-1",
        "ticker": "hype",
        "directionLong": True,
        "leverage": 10,
        "entryPrice": 40.0,
        "closingPrice": 44.0,
        "entrySize": 12.5,
        "createdAt": "2026-08-25T10:00:00Z",
        "updatedAt": "2026-08-25T11:00:00Z",
        "verifiedTrade": True,
        "isOpen": False,
    }
    row.update(overrides)
    return row


def test_normalize_direct_investment_is_fail_closed() -> None:
    good = normalize_direct_investment(
        _investment(),
        portfolio_id="portfolio-1",
        username="carmine",
    )
    assert good is not None
    assert good["trade_id"] == "trade-1"
    assert good["ticker"] == "HYPE"
    assert good["direction"] == "LONG"
    assert good["entry_size"] == 12.5
    assert good["source_post_id"] == "direct-investment:portfolio-1:trade-1"

    assert normalize_direct_investment(
        _investment(verifiedTrade=False),
        portfolio_id="portfolio-1",
        username="carmine",
    ) is None
    assert normalize_direct_investment(
        _investment(isOpen=True),
        portfolio_id="portfolio-1",
        username="carmine",
    ) is None
    assert normalize_direct_investment(
        _investment(entrySize=None),
        portfolio_id="portfolio-1",
        username="carmine",
    ) is None
    assert normalize_direct_investment(
        _investment(updatedAt="2026-08-25T09:00:00Z"),
        portfolio_id="portfolio-1",
        username="carmine",
    ) is None


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, payload))
        page = int(payload["params"]["page"])
        if page == 1:
            return {
                "investmentsTicker": [
                    _investment(id="a"),
                    _investment(id="b", ticker="btc", directionLong=False),
                ]
            }
        raise AssertionError("pagination should stop after short first page")


def test_collect_direct_history_uses_read_only_endpoint_and_stops_on_short_page() -> None:
    client = _FakeClient()

    rows, pages = asyncio.run(
        collect_direct_history(
            client,  # type: ignore[arg-type]
            portfolio_id="portfolio-1",
            username="carmine",
            max_pages=4,
            page_size=50,
        )
    )

    assert pages == 1
    assert {row["trade_id"] for row in rows} == {"a", "b"}
    assert client.calls == [
        (
            "/v1_0/investments/get_investments",
            {
                "portfolioId": "portfolio-1",
                "isOpen": False,
                "params": {"page": 1, "size": 50},
            },
        )
    ]


def test_candidate_selection_prioritizes_named_traders_then_unscanned_score() -> None:
    universe = {
        "candidates": [
            {
                "portfolio_id": "ordinary-high",
                "username": "ordinary",
                "closed_positions": 200,
                "screen_score": 900,
            },
            {
                "portfolio_id": "carmine",
                "username": "carmine",
                "closed_positions": 200,
                "screen_score": 100,
            },
            {
                "portfolio_id": "ordinary-new",
                "username": "ordinary2",
                "closed_positions": 200,
                "screen_score": 800,
            },
            {
                "portfolio_id": "too-small",
                "username": "small",
                "closed_positions": 19,
                "screen_score": 9999,
            },
            {
                "portfolio_id": "verified",
                "username": "bones",
                "closed_positions": 500,
                "screen_score": 9999,
            },
        ]
    }
    state = {
        "items": {
            "ordinary-high": {"last_scan_s": 1},
        }
    }

    rows = select_candidates(
        universe,
        state=state,
        verified_portfolio_ids={"verified"},
        priority_names=("carmine", "bones"),
        max_portfolios=10,
        refresh_minutes=1,
        now_s=10_000,
    )

    assert [row["portfolio_id"] for row in rows] == [
        "carmine",
        "ordinary-new",
        "ordinary-high",
    ]


def test_candidate_selection_respects_refresh_window() -> None:
    universe = {
        "candidates": [
            {
                "portfolio_id": "p1",
                "username": "trader",
                "closed_positions": 100,
                "screen_score": 100,
            }
        ]
    }
    rows = select_candidates(
        universe,
        state={"items": {"p1": {"last_scan_s": 9_900}}},
        verified_portfolio_ids=set(),
        priority_names=(),
        max_portfolios=10,
        refresh_minutes=60,
        now_s=10_000,
    )
    assert rows == []
