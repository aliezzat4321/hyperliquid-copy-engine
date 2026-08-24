from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from decimal import Decimal

from hlcopy.profitability.champion_truth import evaluate_champion_truth
from hlcopy.profitability.continuous_path_v2 import (
    AssetContextMark,
    ContinuousPath,
    FundingRate,
    PathCoverage,
)
from hlcopy.profitability.margin_tables import MarginMetadataSnapshot
from hlcopy.profitability.path_truth import CandidatePathTruth
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent
from hlcopy.profitability.safe_leverage import SafeLeverageRow, SafeLeverageSummary
from hlcopy.profitability.streaming_path_pass import (
    build_margin_indexes,
    run_stream_pass,
)

D = Decimal
ZERO = D("0")


def evaluate_candidate_path_truth_from_factory(
    *,
    state_events: Iterable[FollowerStateEvent],
    mark_factory: Callable[[], Iterator[AssetContextMark]],
    funding_rates: Iterable[FundingRate],
    margin_snapshots: Iterable[MarginMetadataSnapshot],
    leverages: Iterable[Decimal],
    round_trip_fee_accounting: bool,
    minimum_liquidation_buffer_usd: Decimal = ZERO,
    max_mark_age_ns: int = 15_000_000_000,
    max_margin_snapshot_age_ns: int = 7_200_000_000_000,
    max_funding_gap_ns: int = 3_900_000_000_000,
) -> tuple[CandidatePathTruth, dict[str, dict[str, object]]]:
    """Evaluate exact path truth over a reopenable, bounded-memory mark stream."""
    states = tuple(
        sorted(
            state_events,
            key=lambda item: (
                item.execution_received_at_ns,
                item.source_tid,
                item.action,
            ),
        )
    )
    funding = tuple(sorted(funding_rates, key=lambda item: (item.payment_ts_ms, item.coin)))
    snapshots = tuple(sorted(margin_snapshots, key=lambda item: item.fetched_at_ns))
    leverage_values = tuple(
        sorted({value for raw in leverages if (value := D(str(raw))) > ZERO})
    )

    if not snapshots:
        coverage = PathCoverage(False, ("NO_MARGIN_METADATA_SNAPSHOTS",), 0, 0)
        path = ContinuousPath((), coverage)
        champion_truth = evaluate_champion_truth(
            {
                "round_trip_fee_accounting": round_trip_fee_accounting,
                "continuous_mtm": False,
                "funding": False,
                "maintenance_margin": False,
                "liquidation_survival": False,
                "safe_leverage": False,
            }
        )
        return (
            CandidatePathTruth(
                path=path,
                safe_leverage=None,
                champion_truth=champion_truth,
            ),
            {},
        )

    margin_indexes = build_margin_indexes(snapshots)
    first = run_stream_pass(
        states=states,
        mark_factory=mark_factory,
        funding=funding,
        margin_indexes=margin_indexes,
        leverages=leverage_values,
        starting_equity_by_leverage=None,
        max_mark_age_ns=max_mark_age_ns,
        max_margin_snapshot_age_ns=max_margin_snapshot_age_ns,
        max_funding_gap_ns=max_funding_gap_ns,
    )
    coverage_complete = first.checkpoint_count > 0 and not first.blockers
    coverage = PathCoverage(
        coverage_complete,
        first.blockers,
        first.checkpoint_count,
        first.applied_funding_count,
    )
    path = ContinuousPath((), coverage)

    safe_summary: SafeLeverageSummary | None = None
    permitted = tuple(
        leverage
        for leverage in leverage_values
        if first.exchange_max_leverage is not None and leverage <= first.exchange_max_leverage
    )
    if coverage_complete and first.peak_gross > ZERO and permitted:
        starting_equity = {
            leverage: first.peak_gross / leverage for leverage in permitted
        }
        second = run_stream_pass(
            states=states,
            mark_factory=mark_factory,
            funding=funding,
            margin_indexes=margin_indexes,
            leverages=permitted,
            starting_equity_by_leverage=starting_equity,
            max_mark_age_ns=max_mark_age_ns,
            max_margin_snapshot_age_ns=max_margin_snapshot_age_ns,
            max_funding_gap_ns=max_funding_gap_ns,
        )
        min_liq_component = (
            first.min_liquidation_component
            if first.min_liquidation_component is not None
            else ZERO
        )
        rows: list[SafeLeverageRow] = []
        for leverage in permitted:
            start = starting_equity[leverage]
            min_free = start + first.min_free_component_by_leverage[leverage]
            min_liq = start + min_liq_component
            liquidation_survived = min_liq > ZERO
            initial_margin_survived = min_free >= ZERO
            safe = (
                liquidation_survived
                and initial_margin_survived
                and min_liq > minimum_liquidation_buffer_usd
            )
            rows.append(
                SafeLeverageRow(
                    leverage=leverage,
                    starting_equity_usd=start,
                    peak_gross_notional_usd=first.peak_gross,
                    min_free_collateral_usd=min_free,
                    min_liquidation_buffer_usd=min_liq,
                    max_drawdown_pct=second.max_drawdown_pct_by_leverage[leverage],
                    liquidation_survived=liquidation_survived,
                    initial_margin_survived=initial_margin_survived,
                    safe=safe,
                )
            )
        safe_values = [row.leverage for row in rows if row.safe]
        safe_summary = SafeLeverageSummary(
            rows=tuple(rows),
            max_safe_leverage=max(safe_values) if safe_values else None,
        )

    safe_found = safe_summary is not None and safe_summary.max_safe_leverage is not None
    champion_truth = evaluate_champion_truth(
        {
            "round_trip_fee_accounting": round_trip_fee_accounting,
            "continuous_mtm": coverage_complete,
            "funding": coverage_complete,
            "maintenance_margin": coverage_complete,
            "liquidation_survival": coverage_complete and safe_found,
            "safe_leverage": coverage_complete and safe_found,
        }
    )
    return (
        CandidatePathTruth(
            path=path,
            safe_leverage=safe_summary,
            champion_truth=champion_truth,
        ),
        first.gaps,
    )
