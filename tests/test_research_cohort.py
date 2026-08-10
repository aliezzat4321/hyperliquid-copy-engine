from __future__ import annotations

from pathlib import Path

import polars as pl

from hlcopy.research.cohort import CohortPolicy, apply_cohort, plan_cohort
from hlcopy.shadow.registry import WalletRegistry, WalletSpec

ADDRESS_1 = "0x1111111111111111111111111111111111111111"
ADDRESS_2 = "0x2222222222222222222222222222222222222222"


def _row(address: str, *, rank: int = 1, warning_flags: str = "") -> dict[str, object]:
    return {
        "address": address,
        "rank": rank,
        "composite_score": 80.0,
        "copyability_score": 85.0,
        "confidence_score": 55.0,
        "risk_score": 70.0,
        "trade_count": 50,
        "profit_factor": 3.0,
        "expectancy": 100.0,
        "month_roi": 3.0,
        "all_time_roi": 10.0,
        "trades_per_day": 1.0,
        "asset_concentration": 0.40,
        "fast_trade_fraction": 0.05,
        "warning_flags": warning_flags,
    }


def test_policy_selects_copyable_candidate_and_rejects_low_sample(tmp_path: Path):
    artifact = tmp_path / "ranked.parquet"
    pl.DataFrame(
        [
            _row(ADDRESS_1, rank=1),
            _row(ADDRESS_2, rank=2, warning_flags="LOW_SAMPLE"),
        ]
    ).write_parquet(artifact)
    plan = plan_cohort(artifact, CohortPolicy())
    assert plan[0].selected is True
    assert plan[1].selected is False
    assert "FLAG_LOW_SAMPLE" in plan[1].rejection_reasons


def test_apply_only_promotes_to_validation_and_seeds_coins(tmp_path: Path):
    artifact = tmp_path / "ranked.parquet"
    pl.DataFrame([_row(ADDRESS_1)]).write_parquet(artifact)
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.add(
        WalletSpec(
            id="alpha",
            label="Alpha",
            source_type="hyperliquid_wallet",
            source_ref=ADDRESS_1,
            stage="research",
        )
    )
    result = apply_cohort(
        parquet_path=artifact,
        registry=registry,
        policy=CohortPolicy(max_validation_wallets=6),
        seed_coins_by_address={ADDRESS_1: ("BTC", "ETH")},
    )
    assert result.promoted_ids == ("alpha",)
    stored = registry.load()[0]
    assert stored.stage == "validation"
    assert stored.coins == ("BTC", "ETH")
    assert stored.stage != "approved"
