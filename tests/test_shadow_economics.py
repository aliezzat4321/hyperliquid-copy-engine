from __future__ import annotations

from decimal import Decimal

from hlcopy.shadow.economics import (
    AssetContextPoint,
    FundingRateEvent,
    follower_funding_cashflow,
    parse_asset_margin_spec,
    simulate_isolated_path,
)

D = Decimal


def _meta():
    return {
        "universe": [
            {
                "name": "BTC",
                "szDecimals": 5,
                "maxLeverage": 40,
                "marginTableId": 100,
            }
        ],
        "marginTables": [
            [
                100,
                {
                    "description": "tiered BTC",
                    "marginTiers": [
                        {"lowerBound": "0", "maxLeverage": 40},
                        {"lowerBound": "150000000", "maxLeverage": 20},
                    ],
                },
            ]
        ],
    }


def test_margin_tier_maintenance_rate_and_deduction_are_continuous():
    spec = parse_asset_margin_spec(_meta(), "BTC", 1000, "meta.json")
    assert spec is not None
    first, second = spec.tiers
    assert first.maintenance_rate == D("0.0125")
    assert second.maintenance_rate == D("0.025")
    assert second.maintenance_deduction == D("1875000.0000")
    boundary = D("150000000")
    assert spec.maintenance_margin(boundary) == D("1875000.0000")
    assert spec.maintenance_margin(boundary - D("1")) < D("1875000")


def test_positive_funding_charges_long_and_pays_short():
    long_cash = follower_funding_cashflow(
        direction="LONG",
        quantity=D("2"),
        oracle_px=D("100"),
        funding_rate=D("0.001"),
    )
    short_cash = follower_funding_cashflow(
        direction="SHORT",
        quantity=D("2"),
        oracle_px=D("100"),
        funding_rate=D("0.001"),
    )
    assert long_cash == D("-0.200")
    assert short_cash == D("0.200")


def test_isolated_path_detects_mark_price_liquidation():
    spec = parse_asset_margin_spec(_meta(), "BTC", 1000, "meta.json")
    assert spec is not None
    points = (
        AssetContextPoint("BTC", 1000, D("100"), D("100")),
        AssetContextPoint("BTC", 2000, D("98"), D("98")),
        AssetContextPoint("BTC", 3000, D("97"), D("97")),
    )
    result = simulate_isolated_path(
        direction="LONG",
        quantity=D("1"),
        entry_vwap=D("100"),
        leverage=D("40"),
        margin_spec=spec,
        context_points=points,
        funding_events=(),
    )
    assert result.liquidated
    assert result.liquidation_timestamp_ms == 3000
    assert result.liquidation_mark_px == D("97")


def test_funding_is_applied_before_margin_check_at_same_or_later_observation():
    spec = parse_asset_margin_spec(_meta(), "BTC", 1000, "meta.json")
    assert spec is not None
    points = (
        AssetContextPoint("BTC", 1000, D("100"), D("100")),
        AssetContextPoint("BTC", 2000, D("100"), D("100")),
    )
    funding = (FundingRateEvent("BTC", 1900, D("0.02")),)
    result = simulate_isolated_path(
        direction="LONG",
        quantity=D("1"),
        entry_vwap=D("100"),
        leverage=D("40"),
        margin_spec=spec,
        context_points=points,
        funding_events=funding,
        max_context_forward_ms=500,
    )
    assert result.liquidated
    assert result.cumulative_funding_usd == D("-2.00")


def test_leverage_above_point_in_time_asset_max_fails_closed():
    spec = parse_asset_margin_spec(_meta(), "BTC", 1000, "meta.json")
    assert spec is not None
    points = (AssetContextPoint("BTC", 1000, D("100"), D("100")),)
    try:
        simulate_isolated_path(
            direction="LONG",
            quantity=D("1"),
            entry_vwap=D("100"),
            leverage=D("50"),
            margin_spec=spec,
            context_points=points,
            funding_events=(),
        )
    except ValueError as exc:
        assert "exceeds" in str(exc)
    else:
        raise AssertionError("expected leverage gate to fail")
