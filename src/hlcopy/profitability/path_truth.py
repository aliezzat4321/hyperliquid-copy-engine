from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from hlcopy.profitability.champion_truth import ChampionTruth, evaluate_champion_truth
from hlcopy.profitability.continuous_path_v2 import (
    AssetContextMark,
    ContinuousPath,
    FundingRate,
    build_continuous_path,
)
from hlcopy.profitability.margin_tables import MarginMetadataSnapshot
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent
from hlcopy.profitability.safe_leverage import SafeLeverageSummary, evaluate_safe_leverage

D = Decimal


@dataclass(frozen=True, slots=True)
class CandidatePathTruth:
    path: ContinuousPath
    safe_leverage: SafeLeverageSummary | None
    champion_truth: ChampionTruth

    def to_dict(self) -> dict[str, object]:
        return {
            "coverage": {
                "complete": self.path.coverage.complete,
                "blockers": list(self.path.coverage.blockers),
                "checkpoint_count": self.path.coverage.checkpoint_count,
                "applied_funding_count": self.path.coverage.applied_funding_count,
            },
            "safe_leverage": self.safe_leverage.to_dict() if self.safe_leverage else None,
            **self.champion_truth.to_dict(),
        }


def evaluate_candidate_path_truth(
    *,
    state_events: Iterable[FollowerStateEvent],
    asset_contexts: Iterable[AssetContextMark],
    funding_rates: Iterable[FundingRate],
    margin_snapshots: Iterable[MarginMetadataSnapshot],
    leverages: Iterable[Decimal],
    round_trip_fee_accounting: bool,
    minimum_liquidation_buffer_usd: Decimal = D("0"),
    max_mark_age_ns: int = 15_000_000_000,
    max_margin_snapshot_age_ns: int = 7_200_000_000_000,
    max_funding_gap_ns: int = 3_900_000_000_000,
) -> CandidatePathTruth:
    """Produce one fail-closed truth result for a simulated follower candidate."""
    path = build_continuous_path(
        state_events,
        asset_contexts,
        funding_rates,
        margin_snapshots,
        max_mark_age_ns=max_mark_age_ns,
        max_margin_snapshot_age_ns=max_margin_snapshot_age_ns,
        max_funding_gap_ns=max_funding_gap_ns,
    )

    safe: SafeLeverageSummary | None = None
    if path.coverage.complete and path.checkpoints:
        # Exchange maximum leverage for a tier is exactly 1/(2*MMR). Filter the
        # research grid by the tightest maximum seen anywhere on the path.
        exchange_max = min(
            D("1") / (D("2") * position.maintenance_margin_rate)
            for checkpoint in path.checkpoints
            for position in checkpoint.positions
        )
        permitted = tuple(
            value
            for raw in leverages
            if (value := D(str(raw))) > 0 and value <= exchange_max
        )
        if permitted:
            safe = evaluate_safe_leverage(
                path.checkpoints,
                permitted,
                minimum_liquidation_buffer_usd=minimum_liquidation_buffer_usd,
            )

    coverage_complete = path.coverage.complete
    safe_found = safe is not None and safe.max_safe_leverage is not None
    truth = evaluate_champion_truth(
        {
            "round_trip_fee_accounting": round_trip_fee_accounting,
            "continuous_mtm": coverage_complete,
            "funding": coverage_complete,
            "maintenance_margin": coverage_complete,
            "liquidation_survival": coverage_complete and safe_found,
            "safe_leverage": coverage_complete and safe_found,
        }
    )
    return CandidatePathTruth(path=path, safe_leverage=safe, champion_truth=truth)
