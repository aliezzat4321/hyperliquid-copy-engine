from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from hlcopy.profitability.champion_truth import evaluate_champion_truth
from hlcopy.profitability.continuous_path_v2 import (
    AssetContextMark,
    ContinuousPath,
    FundingRate,
    PathCoverage,
)
from hlcopy.profitability.margin_tables import CoinMarginTable, MarginMetadataSnapshot
from hlcopy.profitability.path_truth import CandidatePathTruth
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent
from hlcopy.profitability.safe_leverage import SafeLeverageRow, SafeLeverageSummary

D = Decimal
ZERO = D("0")
ONE_HUNDRED = D("100")


@dataclass(slots=True)
class _Position:
    qty: Decimal = ZERO
    entry: Decimal | None = None
    fee_remaining: Decimal = ZERO
    opened_ns: int | None = None
    last_funding_ns: int | None = None


@dataclass(slots=True)
class _Contribution:
    unrealized: Decimal = ZERO
    gross: Decimal = ZERO
    maintenance: Decimal = ZERO
    valid: bool = False


@dataclass(frozen=True, slots=True)
class _PassResult:
    blockers: tuple[str, ...]
    checkpoint_count: int
    applied_funding_count: int
    peak_gross: Decimal
    min_free_component_by_leverage: dict[Decimal, Decimal]
    min_liquidation_component: Decimal | None
    max_drawdown_pct_by_leverage: dict[Decimal, Decimal]


def _mark_indexes(
    rows: tuple[AssetContextMark, ...],
) -> tuple[
    dict[str, tuple[AssetContextMark, ...]],
    dict[str, tuple[int, ...]],
]:
    grouped: dict[str, list[AssetContextMark]] = {}
    for row in rows:
        grouped.setdefault(row.coin, []).append(row)
    by_coin = {
        coin: tuple(sorted(items, key=lambda item: item.received_at_ns))
        for coin, items in grouped.items()
    }
    times = {
        coin: tuple(item.received_at_ns for item in items)
        for coin, items in by_coin.items()
    }
    return by_coin, times


def _mark_at(
    coin: str,
    at_ns: int,
    by_coin: dict[str, tuple[AssetContextMark, ...]],
    times: dict[str, tuple[int, ...]],
) -> AssetContextMark | None:
    seq = times.get(coin, ())
    if not seq:
        return None
    index = bisect_right(seq, at_ns) - 1
    return None if index < 0 else by_coin[coin][index]


def _margin_indexes(
    snapshots: tuple[MarginMetadataSnapshot, ...],
) -> tuple[
    dict[str, tuple[tuple[int, CoinMarginTable], ...]],
    dict[str, tuple[int, ...]],
]:
    grouped: dict[str, list[tuple[int, CoinMarginTable]]] = {}
    for snapshot in snapshots:
        for table in snapshot.tables:
            grouped.setdefault(table.coin, []).append((snapshot.fetched_at_ns, table))
    by_coin = {
        coin: tuple(sorted(items, key=lambda item: item[0]))
        for coin, items in grouped.items()
    }
    times = {coin: tuple(ts for ts, _ in items) for coin, items in by_coin.items()}
    return by_coin, times


def _margin_at(
    coin: str,
    at_ns: int,
    by_coin: dict[str, tuple[tuple[int, CoinMarginTable], ...]],
    times: dict[str, tuple[int, ...]],
) -> tuple[int, CoinMarginTable] | None:
    seq = times.get(coin, ())
    if not seq:
        return None
    index = bisect_right(seq, at_ns) - 1
    return None if index < 0 else by_coin[coin][index]


def _stream_pass(
    *,
    states: tuple[FollowerStateEvent, ...],
    marks: tuple[AssetContextMark, ...],
    funding: tuple[FundingRate, ...],
    snapshots: tuple[MarginMetadataSnapshot, ...],
    leverages: tuple[Decimal, ...],
    starting_equity_by_leverage: dict[Decimal, Decimal] | None,
    max_mark_age_ns: int,
    max_margin_snapshot_age_ns: int,
    max_funding_gap_ns: int,
) -> _PassResult:
    blockers: set[str] = set()
    if not states:
        blockers.add("NO_FOLLOWER_STATE_EVENTS")
    if not marks:
        blockers.add("NO_ACTIVE_ASSET_CONTEXT")
    if not snapshots:
        blockers.add("NO_MARGIN_METADATA_SNAPSHOTS")
    if blockers:
        return _PassResult(
            tuple(sorted(blockers)), 0, 0, ZERO, {}, None, {}
        )

    first_ns = states[0].execution_received_at_ns
    last_ns = marks[-1].received_at_ns
    marks_by_coin, mark_times = _mark_indexes(marks)
    margin_by_coin, margin_times = _margin_indexes(snapshots)

    latest_mark: dict[str, AssetContextMark] = {}
    for coin in marks_by_coin:
        row = _mark_at(coin, first_ns, marks_by_coin, mark_times)
        if row is not None:
            latest_mark[coin] = row

    positions: dict[str, _Position] = {}
    contributions: dict[str, _Contribution] = {}
    realized = ZERO
    funding_pnl = ZERO
    total_unrealized = ZERO
    total_gross = ZERO
    total_maintenance = ZERO
    checkpoint_count = 0
    applied_funding = 0
    peak_gross = ZERO
    min_free_component = {leverage: D("Infinity") for leverage in leverages}
    min_liquidation_component: Decimal | None = None
    peak_base = ZERO
    max_drawdown_pct = {leverage: ZERO for leverage in leverages}

    state_index = 0
    mark_index = bisect_right(
        tuple(row.received_at_ns for row in marks), first_ns - 1
    )
    funding_index = 0
    while (
        funding_index < len(funding)
        and funding[funding_index].payment_ts_ms * 1_000_000 < first_ns
    ):
        funding_index += 1

    def recompute_coin(coin: str, now_ns: int) -> None:
        nonlocal total_unrealized, total_gross, total_maintenance
        old = contributions.get(coin, _Contribution())
        if old.valid:
            total_unrealized -= old.unrealized
            total_gross -= old.gross
            total_maintenance -= old.maintenance

        position = positions.get(coin)
        if position is None or position.qty == ZERO:
            contributions[coin] = _Contribution()
            return
        mark = latest_mark.get(coin)
        if mark is None or position.entry is None:
            contributions[coin] = _Contribution()
            return
        margin = _margin_at(coin, now_ns, margin_by_coin, margin_times)
        if margin is None:
            contributions[coin] = _Contribution()
            return
        _, table = margin
        gross = abs(position.qty * mark.mark_price)
        tier = table.tier_for_notional(gross)
        contribution = _Contribution(
            unrealized=(mark.mark_price - position.entry) * position.qty,
            gross=gross,
            maintenance=max(
                ZERO,
                gross * tier.maintenance_margin_rate
                - tier.maintenance_deduction_usd,
            ),
            valid=True,
        )
        contributions[coin] = contribution
        total_unrealized += contribution.unrealized
        total_gross += contribution.gross
        total_maintenance += contribution.maintenance

    while True:
        next_state = (
            states[state_index].execution_received_at_ns
            if state_index < len(states)
            else None
        )
        next_mark = marks[mark_index].received_at_ns if mark_index < len(marks) else None
        next_funding = (
            funding[funding_index].payment_ts_ms * 1_000_000
            if funding_index < len(funding)
            else None
        )
        available = [value for value in (next_state, next_mark, next_funding) if value is not None]
        if not available:
            break
        now_ns = min(available)
        if now_ns > last_ns:
            break

        while (
            state_index < len(states)
            and states[state_index].execution_received_at_ns == now_ns
        ):
            state = states[state_index]
            position = positions.setdefault(state.coin, _Position())
            before = position.qty
            position.qty = state.qty_after
            position.entry = state.avg_entry_after
            position.fee_remaining = state.entry_fee_remaining_usd
            realized = state.realized_net_pnl_cumulative_usd
            if before == ZERO and state.qty_after != ZERO:
                position.opened_ns = now_ns
            if state.qty_after == ZERO:
                position.opened_ns = None
                position.last_funding_ns = None
            recompute_coin(state.coin, now_ns)
            state_index += 1

        mark_end = mark_index
        marks_now: dict[str, AssetContextMark] = {}
        while mark_end < len(marks) and marks[mark_end].received_at_ns == now_ns:
            marks_now[marks[mark_end].coin] = marks[mark_end]
            mark_end += 1

        while (
            funding_index < len(funding)
            and funding[funding_index].payment_ts_ms * 1_000_000 == now_ns
        ):
            payment = funding[funding_index]
            position = positions.get(payment.coin)
            if position is not None and position.qty != ZERO:
                oracle = marks_now.get(payment.coin) or latest_mark.get(payment.coin)
                if oracle is None or now_ns - oracle.received_at_ns > max_mark_age_ns:
                    blockers.add(f"FUNDING_ORACLE_COVERAGE:{payment.coin}")
                else:
                    funding_pnl += (
                        -position.qty * oracle.oracle_price * payment.funding_rate
                    )
                    position.last_funding_ns = now_ns
                    applied_funding += 1
            funding_index += 1

        had_mark_tick = mark_end > mark_index
        if had_mark_tick:
            for coin, mark in marks_now.items():
                latest_mark[coin] = mark
                recompute_coin(coin, now_ns)
            mark_index = mark_end
        if not had_mark_tick:
            continue

        open_coins = sorted(
            coin for coin, position in positions.items() if position.qty != ZERO
        )
        if not open_coins:
            continue

        valid = True
        for coin in open_coins:
            position = positions[coin]
            mark = latest_mark.get(coin)
            opened_ns = position.opened_ns if position.opened_ns is not None else now_ns
            if mark is None:
                if now_ns - opened_ns > max_mark_age_ns:
                    blockers.add(f"MISSING_MARK:{coin}")
                valid = False
                continue
            if now_ns - mark.received_at_ns > max_mark_age_ns:
                blockers.add(f"MARK_GAP:{coin}")
                valid = False
                continue
            margin = _margin_at(coin, now_ns, margin_by_coin, margin_times)
            if margin is None:
                blockers.add(f"MISSING_MARGIN_TABLE:{coin}")
                valid = False
                continue
            margin_ts, _ = margin
            if now_ns - margin_ts > max_margin_snapshot_age_ns:
                blockers.add(f"STALE_MARGIN_TABLE:{coin}")
                valid = False
                continue
            if position.entry is None:
                blockers.add(f"MISSING_ENTRY:{coin}")
                valid = False
                continue
            if not contributions.get(coin, _Contribution()).valid:
                valid = False
                continue
            funding_reference = (
                position.last_funding_ns
                if position.last_funding_ns is not None
                else opened_ns
            )
            if now_ns - funding_reference > max_funding_gap_ns:
                blockers.add(f"FUNDING_GAP:{coin}")

        if not valid:
            continue

        adjusted_realized = realized - sum(
            (positions[coin].fee_remaining for coin in open_coins), ZERO
        )
        base_equity = adjusted_realized + funding_pnl + total_unrealized
        checkpoint_count += 1
        peak_gross = max(peak_gross, total_gross)
        min_liquidation_component = (
            base_equity - total_maintenance
            if min_liquidation_component is None
            else min(min_liquidation_component, base_equity - total_maintenance)
        )
        for leverage in leverages:
            component = base_equity - total_gross / leverage
            min_free_component[leverage] = min(
                min_free_component[leverage], component
            )

        if starting_equity_by_leverage is not None:
            peak_base = max(peak_base, base_equity)
            drawdown = max(ZERO, peak_base - base_equity)
            for leverage in leverages:
                starting_equity = starting_equity_by_leverage[leverage]
                peak_equity = starting_equity + peak_base
                drawdown_pct = (
                    drawdown / peak_equity * ONE_HUNDRED
                    if peak_equity > ZERO
                    else ZERO
                )
                max_drawdown_pct[leverage] = max(
                    max_drawdown_pct[leverage], drawdown_pct
                )

    if states[-1].execution_received_at_ns > last_ns:
        blockers.add("FOLLOWER_STATE_AFTER_LAST_MARK")

    return _PassResult(
        blockers=tuple(sorted(blockers)),
        checkpoint_count=checkpoint_count,
        applied_funding_count=applied_funding,
        peak_gross=peak_gross,
        min_free_component_by_leverage=min_free_component,
        min_liquidation_component=min_liquidation_component,
        max_drawdown_pct_by_leverage=max_drawdown_pct,
    )


def evaluate_candidate_path_truth_streaming(
    *,
    state_events: Iterable[FollowerStateEvent],
    asset_contexts: Iterable[AssetContextMark],
    funding_rates: Iterable[FundingRate],
    margin_snapshots: Iterable[MarginMetadataSnapshot],
    leverages: Iterable[Decimal],
    round_trip_fee_accounting: bool,
    minimum_liquidation_buffer_usd: Decimal = ZERO,
    max_mark_age_ns: int = 15_000_000_000,
    max_margin_snapshot_age_ns: int = 7_200_000_000_000,
    max_funding_gap_ns: int = 3_900_000_000_000,
) -> CandidatePathTruth:
    """Lossless streaming equivalent of exact checkpoint path truth.

    Every supplied mark tick is evaluated. Unlike ``build_continuous_path`` this function
    never materializes one ``EquityCheckpoint`` per mark and never replays that million-row
    object graph once per leverage. It computes the same coverage and risk extrema in a
    bounded-memory first pass, then performs one additional bounded-memory pass solely for
    exact drawdown percentages after peak gross (and therefore starting equity) is known.
    """
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
    marks = tuple(sorted(asset_contexts, key=lambda item: (item.received_at_ns, item.coin)))
    funding = tuple(sorted(funding_rates, key=lambda item: (item.payment_ts_ms, item.coin)))
    snapshots = tuple(sorted(margin_snapshots, key=lambda item: item.fetched_at_ns))
    leverage_values = tuple(sorted({D(str(value)) for value in leverages if D(str(value)) > ZERO}))

    first = _stream_pass(
        states=states,
        marks=marks,
        funding=funding,
        snapshots=snapshots,
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
    if coverage_complete and first.peak_gross > ZERO and leverage_values:
        starting_equity = {
            leverage: first.peak_gross / leverage for leverage in leverage_values
        }
        second = _stream_pass(
            states=states,
            marks=marks,
            funding=funding,
            snapshots=snapshots,
            leverages=leverage_values,
            starting_equity_by_leverage=starting_equity,
            max_mark_age_ns=max_mark_age_ns,
            max_margin_snapshot_age_ns=max_margin_snapshot_age_ns,
            max_funding_gap_ns=max_funding_gap_ns,
        )
        rows: list[SafeLeverageRow] = []
        min_liq_component = (
            first.min_liquidation_component
            if first.min_liquidation_component is not None
            else ZERO
        )
        for leverage in leverage_values:
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
    return CandidatePathTruth(
        path=path,
        safe_leverage=safe_summary,
        champion_truth=truth,
    )
