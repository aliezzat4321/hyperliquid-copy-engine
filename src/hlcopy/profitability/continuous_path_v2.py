from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from hlcopy.profitability.margin_tables import MarginMetadataSnapshot, snapshot_table_at
from hlcopy.profitability.path_risk import EquityCheckpoint, OpenPositionMark
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent

D = Decimal
ZERO = D("0")


@dataclass(frozen=True, slots=True)
class AssetContextMark:
    coin: str
    received_at_ns: int
    mark_price: Decimal
    oracle_price: Decimal


@dataclass(frozen=True, slots=True)
class FundingRate:
    coin: str
    payment_ts_ms: int
    funding_rate: Decimal


@dataclass(frozen=True, slots=True)
class PathCoverage:
    complete: bool
    blockers: tuple[str, ...]
    checkpoint_count: int
    applied_funding_count: int


@dataclass(frozen=True, slots=True)
class ContinuousPath:
    checkpoints: tuple[EquityCheckpoint, ...]
    coverage: PathCoverage


def _indexes(rows: tuple[AssetContextMark, ...]):
    grouped: dict[str, list[AssetContextMark]] = {}
    for row in rows:
        grouped.setdefault(row.coin, []).append(row)
    by_coin = {coin: tuple(sorted(items, key=lambda x: x.received_at_ns)) for coin, items in grouped.items()}
    times = {coin: tuple(x.received_at_ns for x in items) for coin, items in by_coin.items()}
    return by_coin, times


def _ctx(coin: str, at_ns: int, by_coin, times) -> AssetContextMark | None:
    seq = times.get(coin, ())
    if not seq:
        return None
    i = bisect_right(seq, at_ns) - 1
    return None if i < 0 else by_coin[coin][i]


def build_continuous_path(
    state_events: Iterable[FollowerStateEvent],
    asset_contexts: Iterable[AssetContextMark],
    funding_rates: Iterable[FundingRate],
    margin_snapshots: Iterable[MarginMetadataSnapshot],
    *,
    max_mark_age_ns: int = 15_000_000_000,
    max_margin_snapshot_age_ns: int = 7_200_000_000_000,
    max_funding_gap_ns: int = 3_900_000_000_000,
) -> ContinuousPath:
    """Strict prospective follower MTM replay; no interpolated marks or margin truth."""
    states = tuple(sorted(state_events, key=lambda x: (x.execution_received_at_ns, x.source_tid, x.action)))
    marks = tuple(sorted(asset_contexts, key=lambda x: (x.received_at_ns, x.coin)))
    funding = tuple(sorted(funding_rates, key=lambda x: (x.payment_ts_ms, x.coin)))
    snapshots = tuple(sorted(margin_snapshots, key=lambda x: x.fetched_at_ns))
    blockers: set[str] = set()
    if not states:
        blockers.add("NO_FOLLOWER_STATE_EVENTS")
    if not marks:
        blockers.add("NO_ACTIVE_ASSET_CONTEXT")
    if not snapshots:
        blockers.add("NO_MARGIN_METADATA_SNAPSHOTS")
    if blockers:
        return ContinuousPath((), PathCoverage(False, tuple(sorted(blockers)), 0, 0))

    first_ns = states[0].execution_received_at_ns
    last_ns = marks[-1].received_at_ns
    by_coin, mark_times = _indexes(marks)
    states_at: dict[int, list[FollowerStateEvent]] = {}
    marks_at: dict[int, list[AssetContextMark]] = {}
    funding_at: dict[int, list[FundingRate]] = {}
    for row in states:
        states_at.setdefault(row.execution_received_at_ns, []).append(row)
    for row in marks:
        if row.received_at_ns >= first_ns:
            marks_at.setdefault(row.received_at_ns, []).append(row)
    for row in funding:
        ns = row.payment_ts_ms * 1_000_000
        if first_ns <= ns <= last_ns:
            funding_at.setdefault(ns, []).append(row)

    times = sorted(set(states_at) | set(marks_at) | set(funding_at))
    qty: dict[str, Decimal] = {}
    entry: dict[str, Decimal | None] = {}
    fee_remaining: dict[str, Decimal] = {}
    open_since: dict[str, int] = {}
    last_funding: dict[str, int] = {}
    latest_mark = {
        coin: row for coin in by_coin
        if (row := _ctx(coin, first_ns, by_coin, mark_times)) is not None
    }
    realized = ZERO
    funding_pnl = ZERO
    applied = 0
    checkpoints: list[EquityCheckpoint] = []

    for now_ns in times:
        for state in states_at.get(now_ns, ()):
            before = qty.get(state.coin, ZERO)
            qty[state.coin] = state.qty_after
            entry[state.coin] = state.avg_entry_after
            fee_remaining[state.coin] = state.entry_fee_remaining_usd
            realized = state.realized_net_pnl_cumulative_usd
            if before == ZERO and state.qty_after != ZERO:
                open_since[state.coin] = now_ns
            if state.qty_after == ZERO:
                open_since.pop(state.coin, None)
                last_funding.pop(state.coin, None)

        for payment in funding_at.get(now_ns, ()):
            position = qty.get(payment.coin, ZERO)
            if position == ZERO:
                continue
            oracle = _ctx(payment.coin, now_ns, by_coin, mark_times)
            if oracle is None or now_ns - oracle.received_at_ns > max_mark_age_ns:
                blockers.add(f"FUNDING_ORACLE_COVERAGE:{payment.coin}")
                continue
            funding_pnl += -position * oracle.oracle_price * payment.funding_rate
            last_funding[payment.coin] = now_ns
            applied += 1

        tick = marks_at.get(now_ns, ())
        for mark in tick:
            latest_mark[mark.coin] = mark
        if not tick:
            continue

        open_coins = sorted(coin for coin, position in qty.items() if position != ZERO)
        if not open_coins:
            continue
        positions: list[OpenPositionMark] = []
        for coin in open_coins:
            mark = latest_mark.get(coin)
            opened = open_since.get(coin, now_ns)
            if mark is None:
                if now_ns - opened > max_mark_age_ns:
                    blockers.add(f"MISSING_MARK:{coin}")
                continue
            if now_ns - mark.received_at_ns > max_mark_age_ns:
                blockers.add(f"MARK_GAP:{coin}")
                continue
            table = snapshot_table_at(snapshots, coin, now_ns)
            snap_times = [s.fetched_at_ns for s in snapshots if s.fetched_at_ns <= now_ns and coin in s.by_coin()]
            if table is None or not snap_times:
                blockers.add(f"MISSING_MARGIN_TABLE:{coin}")
                continue
            if now_ns - max(snap_times) > max_margin_snapshot_age_ns:
                blockers.add(f"STALE_MARGIN_TABLE:{coin}")
                continue
            avg = entry.get(coin)
            if avg is None:
                blockers.add(f"MISSING_ENTRY:{coin}")
                continue
            tier = table.tier_for_notional(abs(qty[coin] * mark.mark_price))
            positions.append(OpenPositionMark(
                coin=coin,
                qty=qty[coin],
                avg_entry=avg,
                mark_price=mark.mark_price,
                maintenance_margin_rate=tier.maintenance_margin_rate,
                maintenance_margin_deduction_usd=tier.maintenance_margin_deduction_usd,
            ))
            if now_ns - last_funding.get(coin, opened) > max_funding_gap_ns:
                blockers.add(f"FUNDING_GAP:{coin}")

        if len(positions) != len(open_coins):
            continue
        adjusted_realized = realized - sum((fee_remaining.get(coin, ZERO) for coin in open_coins), ZERO)
        checkpoints.append(EquityCheckpoint(
            exchange_ts_ms=now_ns // 1_000_000,
            realized_net_pnl_usd=adjusted_realized,
            funding_pnl_usd=funding_pnl,
            positions=tuple(positions),
        ))

    if states[-1].execution_received_at_ns > last_ns:
        blockers.add("FOLLOWER_STATE_AFTER_LAST_MARK")
    return ContinuousPath(
        tuple(checkpoints),
        PathCoverage(bool(checkpoints) and not blockers, tuple(sorted(blockers)), len(checkpoints), applied),
    )
