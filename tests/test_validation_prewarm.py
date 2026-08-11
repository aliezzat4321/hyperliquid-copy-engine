from pathlib import Path

import polars as pl

from hlcopy.research.cohort import CohortPolicy, apply_cohort
from hlcopy.research.cohort_cli import _filter_current_markets, _parse_active_perp_markets
from hlcopy.shadow.registry import WalletRegistry, WalletSpec


def _passing_artifact(path: Path, address: str) -> None:
    pl.DataFrame(
        [
            {
                "rank": 1,
                "address": address,
                "trade_count": 50,
                "composite_score": 80.0,
                "copyability_score": 85.0,
                "confidence_score": 60.0,
                "risk_score": 60.0,
                "profit_factor": 2.0,
                "expectancy": 1.0,
                "month_roi": 1.0,
                "all_time_roi": 1.0,
                "trades_per_day": 1.0,
                "asset_concentration": 0.5,
                "fast_trade_fraction": 0.0,
                "warning_flags": "",
            }
        ]
    ).write_parquet(path)


def test_apply_cohort_refreshes_existing_validation_market_prewarm(tmp_path: Path) -> None:
    address = "0x" + "1" * 40
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.init()
    registry.add(
        WalletSpec(
            id="wallet",
            label="wallet",
            source_type="hyperliquid_wallet",
            source_ref=address,
            stage="validation",
            coins=("BTC",),
        )
    )
    artifact = tmp_path / "ranked.parquet"
    _passing_artifact(artifact, address)

    result = apply_cohort(
        parquet_path=artifact,
        registry=registry,
        policy=CohortPolicy(),
        seed_coins_by_address={address: ("BTC", "xyz:SNDK", "ETH")},
    )

    stored = registry.load()[0]
    assert result.already_validation_ids == ("wallet",)
    assert stored.coins == ("BTC", "XYZ:SNDK", "ETH")


def test_active_perp_parser_keeps_live_native_and_hip3_and_drops_delisted() -> None:
    payload = [
        [
            {
                "universe": [
                    {"name": "BTC", "maxLeverage": 50},
                    {"name": "LOOM", "isDelisted": True},
                ]
            },
            [],
        ],
        [
            {
                "universe": [
                    {"name": "xyz:SNDK", "maxLeverage": 10},
                    {"name": "para:COHR", "maxLeverage": 10},
                ]
            },
            [],
        ],
    ]

    assert _parse_active_perp_markets(payload) == frozenset(
        {"BTC", "XYZ:SNDK", "PARA:COHR"}
    )


def test_current_market_filter_preserves_per_wallet_order() -> None:
    seeds = {
        "0x1": ("BTC", "XYZ:SNDK", "LOOM"),
        "0x2": ("PARA:COHR", "ETH"),
    }
    current = frozenset({"BTC", "XYZ:SNDK", "PARA:COHR"})

    assert _filter_current_markets(seeds, current) == {
        "0x1": ("BTC", "XYZ:SNDK"),
        "0x2": ("PARA:COHR",),
    }
