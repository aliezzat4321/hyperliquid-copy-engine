from __future__ import annotations

import math

from hlcopy.market.normalize import TradeDeduper, build_subscriptions, normalize_market_message


def test_build_subscriptions_dedupes_coins_and_covers_required_streams() -> None:
    subscriptions = build_subscriptions(["btc", "ETH", "BTC"])
    assert subscriptions == [
        {"type": "bbo", "coin": "BTC"},
        {"type": "l2Book", "coin": "BTC"},
        {"type": "trades", "coin": "BTC"},
        {"type": "activeAssetCtx", "coin": "BTC"},
        {"type": "bbo", "coin": "ETH"},
        {"type": "l2Book", "coin": "ETH"},
        {"type": "trades", "coin": "ETH"},
        {"type": "activeAssetCtx", "coin": "ETH"},
    ]


def test_bbo_normalization_calculates_microstructure() -> None:
    rows = normalize_market_message(
        {
            "channel": "bbo",
            "data": {
                "coin": "BTC",
                "time": 1_000,
                "bbo": [
                    {"px": "100", "sz": "3", "n": 2},
                    {"px": "102", "sz": "1", "n": 1},
                ],
            },
        },
        received_at_ns=1_010_000_000,
        received_monotonic_ns=123,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["coin"] == "BTC"
    assert row["mid_px"] == 101.0
    assert row["bbo_imbalance"] == 0.5
    assert row["microprice"] == 101.5
    assert math.isclose(row["spread_bps"], 198.01980198019803)
    assert row["observed_event_lag_ms"] == 10.0


def test_l2_normalization_captures_depth_buckets() -> None:
    rows = normalize_market_message(
        {
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "time": 5_000,
                "levels": [
                    [
                        {"px": "100.00", "sz": "2", "n": 1},
                        {"px": "99.95", "sz": "3", "n": 2},
                    ],
                    [
                        {"px": "100.10", "sz": "1", "n": 1},
                        {"px": "100.15", "sz": "4", "n": 2},
                    ],
                ],
            },
        },
        received_at_ns=5_001_000_000,
        received_monotonic_ns=456,
    )
    row = rows[0]
    assert row["best_bid_px"] == 100.0
    assert row["best_ask_px"] == 100.1
    assert row["bid_depth_usd_5bps"] == 200.0
    assert row["ask_depth_usd_5bps"] == 100.1
    assert row["bid_depth_usd_10bps"] == 499.85
    assert math.isclose(row["ask_depth_usd_10bps"], 500.7)


def test_trade_normalization_uses_aggressing_side_sign() -> None:
    rows = normalize_market_message(
        {
            "channel": "trades",
            "data": [
                {
                    "coin": "ETH",
                    "side": "B",
                    "px": "2500",
                    "sz": "2",
                    "hash": "0xabc",
                    "time": 7_000,
                    "tid": 11,
                    "users": ["buyer", "seller"],
                },
                {
                    "coin": "ETH",
                    "side": "A",
                    "px": "2499",
                    "sz": "1",
                    "hash": "0xdef",
                    "time": 7_001,
                    "tid": 12,
                    "users": ["buyer2", "seller2"],
                },
            ],
        },
        received_at_ns=7_002_000_000,
        received_monotonic_ns=789,
    )
    assert rows[0]["signed_notional_usd"] == 5_000.0
    assert rows[1]["signed_notional_usd"] == -2_499.0
    assert rows[0]["buyer"] == "buyer"
    assert rows[0]["seller"] == "seller"


def test_trade_deduper_uses_block_time_coin_tid() -> None:
    deduper = TradeDeduper(max_keys=2)
    row = {"channel": "trades", "coin": "BTC", "exchange_ts_ms": 100, "tid": 9}
    assert deduper.seen(row) is False
    assert deduper.seen(row) is True
    assert deduper.seen({**row, "coin": "ETH"}) is False
    assert deduper.seen({**row, "exchange_ts_ms": 101}) is False
    assert deduper.seen(row) is False
