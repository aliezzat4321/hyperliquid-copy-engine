from __future__ import annotations

import math
from decimal import Decimal

from hlcopy.analytics.trader_profile import build_trader_profile
from hlcopy.models import Fill
from hlcopy.positions.reconstruction import reconstruct_positions

D = Decimal
WALLET = "0x1111111111111111111111111111111111111111"


def _fill(
    tid: int,
    time_ms: int,
    coin: str,
    side: str,
    direction: str,
    price: str,
    size: str,
    start: str,
    pnl: str = "0",
    *,
    crossed: bool = True,
) -> Fill:
    raw = {
        "tid": tid,
        "oid": tid + 1000,
        "hash": f"0x{tid:064x}",
        "time": time_ms,
        "coin": coin,
        "side": side,
        "dir": direction,
        "px": price,
        "sz": size,
        "startPosition": start,
        "closedPnl": pnl,
        "fee": "0",
        "feeToken": "USDC",
        "crossed": crossed,
        "builderFee": "0",
    }
    return Fill.from_raw(WALLET, raw)


def _sample_fills() -> list[Fill]:
    return [
        _fill(1, 1_000, "BTC", "B", "Open Long", "100", "1", "0", crossed=False),
        _fill(2, 2_000, "BTC", "B", "Add Long", "95", "1", "1"),
        _fill(3, 61_000, "BTC", "A", "Close Long", "110", "2", "2", "30"),
        _fill(4, 121_000, "ETH", "A", "Open Short", "50", "2", "0"),
        _fill(5, 181_000, "ETH", "B", "Close Short", "55", "2", "-2", "-10"),
        _fill(6, 241_000, "BTC", "B", "Open Long", "120", "1", "0"),
        _fill(7, 301_000, "BTC", "A", "Close Long", "130", "1", "1", "10"),
    ]


def test_profile_covers_success_style_execution_leverage_and_funding():
    fills = _sample_fills()
    episodes, _ = reconstruct_positions(fills)
    clearinghouse = {
        "marginSummary": {
            "accountValue": "1000",
            "totalMarginUsed": "50",
        },
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "2",
                    "entryPx": "100",
                    "positionValue": "200",
                    "leverage": {"type": "cross", "value": 10},
                    "liquidationPx": "50",
                    "marginUsed": "20",
                    "maxLeverage": 40,
                    "unrealizedPnl": "5",
                    "cumFunding": {"sinceOpen": "-1"},
                }
            },
            {
                "position": {
                    "coin": "ETH",
                    "szi": "-1",
                    "entryPx": "50",
                    "positionValue": "50",
                    "leverage": {"type": "isolated", "value": 5},
                    "liquidationPx": "90",
                    "marginUsed": "10",
                    "maxLeverage": 25,
                    "unrealizedPnl": "2",
                    "cumFunding": {"sinceOpen": "0"},
                }
            },
        ],
    }
    portfolio = [
        [
            "day",
            {
                "accountValueHistory": [
                    [0, "100"],
                    [120_000, "200"],
                    [240_000, "300"],
                ]
            },
        ]
    ]
    orders = [
        {
            "status": "filled",
            "statusTimestamp": 1_000,
            "order": {
                "coin": "BTC",
                "orderType": "Limit",
                "tif": "Alo",
                "reduceOnly": False,
                "isTrigger": False,
                "isPositionTpsl": False,
            },
        },
        {
            "status": "canceled",
            "statusTimestamp": 2_000,
            "order": {
                "coin": "BTC",
                "orderType": "Limit",
                "tif": "Ioc",
                "reduceOnly": True,
                "isTrigger": True,
                "isPositionTpsl": True,
            },
        },
    ]
    twaps = [{"twapId": 7, "fill": fills[2].raw}]
    funding = [
        {
            "time": 180_000,
            "delta": {"type": "funding", "coin": "ETH", "usdc": "-1"},
        }
    ]
    profile = build_trader_profile(
        wallet_address=WALLET,
        leaderboard_rank=1,
        display_name="sample",
        as_of_ms=400_000,
        lookback_start_ms=0,
        leaderboard_metrics={"month_roi": 0.4, "account_value": 1000.0},
        fills=fills,
        episodes=episodes,
        clearinghouse_state=clearinghouse,
        portfolio=portfolio,
        historical_orders=orders,
        twap_slice_fills=twaps,
        funding_rows=funding,
        user_role={"role": "trader"},
        user_abstraction="unifiedAccount",
        user_fees={"userCrossRate": "0.00045", "userAddRate": "0.00015"},
        source_status={"clearinghouse_state": True},
    )

    assert profile.performance["trade_count"] == 3
    assert math.isclose(float(profile.performance["win_rate"]), 2 / 3)
    assert math.isclose(float(profile.performance["profit_factor"]), 4.0)
    assert profile.style == "SCALPER"
    assert profile.behavior["scale_in_fill_count"] == 1
    assert profile.behavior["adverse_scale_in_fraction"] == 1.0
    assert float(profile.execution["maker_notional_share"]) > 0
    assert profile.execution["twap_fill_count"] == 1
    assert profile.leverage["current_effective_leverage"] == 0.25
    assert profile.leverage["current_max_configured_leverage"] == 10.0
    assert profile.leverage["current_cross_notional_share"] == 0.8
    assert profile.leverage["historical_effective_exposure_sample_count"] == 3
    assert profile.funding["net_usd"] == -1.0
    assert profile.concentration["asset_count"] == 2
    assert len(profile.current_positions) == 2
    assert profile.account["role"] == "trader"
    assert profile.account["abstraction"] == "unifiedAccount"
    assert profile.account["current_leader_taker_fee_rate"] == 0.00045


def test_behavior_metrics_use_position_chain_not_tid_for_same_timestamp_fills():
    fills = [
        _fill(300, 1_000, "BTC", "B", "Open Long", "100", "1", "0"),
        _fill(100, 1_000, "BTC", "B", "Add Long", "90", "1", "1"),
        _fill(200, 1_000, "BTC", "A", "Close Long", "110", "2", "2", "30"),
    ]
    episodes, _ = reconstruct_positions(fills)

    profile = build_trader_profile(
        wallet_address=WALLET,
        leaderboard_rank=1,
        display_name=None,
        as_of_ms=2_000,
        lookback_start_ms=0,
        leaderboard_metrics={},
        fills=fills,
        episodes=episodes,
        clearinghouse_state=None,
        portfolio=None,
    )

    assert profile.behavior["scale_in_fill_count"] == 1
    assert profile.behavior["adverse_scale_in_fraction"] == 1.0
    assert profile.behavior["close_fill_count"] == 1


def test_profile_flags_truncated_history_and_flattens_safely():
    fills = [
        _fill(
            10,
            1_000,
            "BTC",
            "A",
            "Close Long",
            "101",
            "1",
            "1",
            "1",
        )
    ]
    episodes, _ = reconstruct_positions(fills)
    profile = build_trader_profile(
        wallet_address=WALLET,
        leaderboard_rank=2,
        display_name=None,
        as_of_ms=2_000,
        lookback_start_ms=0,
        leaderboard_metrics={},
        fills=fills,
        episodes=episodes,
        clearinghouse_state=None,
        portfolio=None,
        history_cap_hit=True,
        source_status={"clearinghouse_state": False},
    )

    assert "HISTORY_TRUNCATED" in profile.warnings
    assert "CURRENT_STATE_UNAVAILABLE" in profile.warnings
    assert profile.data_quality["complete_trade_count"] == 0
    assert profile.data_quality["incomplete_episode_count"] == 1
    flat = profile.to_flat_dict()
    assert flat["wallet_address"] == WALLET
    assert isinstance(flat["assets_json"], str)
    assert isinstance(flat["current_positions_json"], str)
