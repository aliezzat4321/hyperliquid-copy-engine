from __future__ import annotations

from hlcopy.discovery.invo_evidence import closed_trade_evidence
from hlcopy.discovery.invo_source import normalize_trade_event


def test_closed_update_preserves_distinct_open_and_close_times() -> None:
    post = {
        "id": "close-post",
        "createdAt": "2026-08-20T11:00:00Z",
        "update": {
            "ticker": "HYPE",
            "directionLong": True,
            "leverage": 5,
            "entryPrice": 40.0,
            "closingPrice": 44.0,
            "isOpen": False,
            "verifiedTrade": True,
            "createdAt": "2026-08-20T10:00:00Z",
            "updatedAt": "2026-08-20T11:00:00Z",
            "portfolio": {"id": "portfolio-carmine"},
            "owner": {"id": "user-carmine", "username": "carmine"},
            "baseId": "trade-1",
        },
    }

    event = normalize_trade_event(post)
    assert event is not None
    row = closed_trade_evidence(event)

    assert row is not None
    assert row["trade_id"] == "trade-1"
    assert row["username"] == "carmine"
    assert row["ticker"] == "HYPE"
    assert row["opened_at"] == 1787220000000
    assert row["closed_at"] == 1787223600000


def test_close_without_provable_later_timestamp_is_not_identity_evidence() -> None:
    post = {
        "id": "ambiguous-close",
        "createdAt": "2026-08-20T10:00:00Z",
        "update": {
            "ticker": "SOL",
            "directionLong": False,
            "entryPrice": 150.0,
            "closingPrice": 145.0,
            "isOpen": False,
            "verifiedTrade": True,
            "createdAt": "2026-08-20T10:00:00Z",
            "portfolio": {"id": "portfolio-1"},
            "owner": {"id": "user-1", "username": "trader"},
            "baseId": "trade-2",
        },
    }

    event = normalize_trade_event(post)
    assert event is not None
    assert closed_trade_evidence(event) is None
