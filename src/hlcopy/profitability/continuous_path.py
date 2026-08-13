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
HOUR_NS = 3_600_000_000_000


@dataclass(frozen=True, slots=True)
class AssetContextMark:
    coin: str
    received_at_ns: int
    mark_price: Decimal
    oracle_price: Decimal

    def __post_init__(self) -> None:
        if self.received_at_ns <= 0:
            raise ValueError("received_at_ns must be positive")
        if self.mark_price <= ZERO or self.oracle_price <= ZERO:
            raise ValueError("mark and oracle prices must be positive")


@dataclass(frozen=True, slots=True)
class FundingRate:
    coin: str
    payment_ts_ms: int
    funding_rate: Decimal

    def __post_init__(self) -> None:
        if self.payment_ts_ms <= 0:
            raise ValueError("payment_ts_ms must be positive")


@dataclass(frozen=True, slots=True)
class PathCoverage:
    complete: bool
    blockers: tuple[str, ...]
    checkpoint_count: int
    first_checkpoint_ns: int | None
    last_checkpoint_ns: int | None
    applied_funding_count: int
    open_position_seconds: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "blockers": list(self.blockers),
            "checkpoint_count": self.checkpoint_count,
            "first_checkpoint_ns": self.first_checkpoint_ns,
            "last_checkpoint_ns": self.last_checkpoint_ns,
            "applied_funding_count": self.applied_funding_count,
            "open_position_seconds": str(self.open_position_seconds),
        }


@dataclass(frozen=True, slots=True)
class ContinuousPath:
    checkpoints: tuple[EquityCheckpoint, ...]
    checkpoint_received_at_ns: tuple[int, ...]
    coverage: PathCoverage


def _context_index(
    contexts: tuple[AssetContextMark, ...],
) -> tuple[dict[str, tuple[AssetContextMark, ...]], dict[str, tuple[int, ...]]]:
    grouped: dict[str, list[AssetContextMark]] = {}
    for row in contexts:
        grouped.setdefault(row.coin, []).append(row)
    rows_by_coin: dict[str, tuple[AssetContextMark, ...]] = {}
    times_by_coin: dict[str, tuple[int, ...]] = {}
    for coin, rows in grouped.items():
        ordered = tuple(sorted(rows, key=lambda item: item.received_at_ns))
        rows_by_coin[coin] = ordered
        times_by_coin[coin] = tuple(item.received_at_ns for item in ordered)
    return rows_by_coin, times_by_coin


def _context_at_or_before(
    coin: str,
    at_ns: int,
    rows_by_coin: dict[str, tuple[AssetContextMark, ...]],
    times_by_coin: dict[str, tuple[int, ...]],
) -> AssetContextMark | None:
    times = times_by_coin.get(coin, ())
    if not times:
        return None
    index = bisect_right(times, at_ns) - 1
    if index < 0:
        return None
    return rows_by_coin[coin][index]


def _state_at_or_before(
    coin: str,
    at_ns: int,
    state_rows: dict[str, tuple[FollowerStateEvent, ...]],
    state_times: dict[str, tuple[int, ...]],
) -> FollowerStateEvent | None:
    times = state_times.get(coin, ())
    if not times:
        return None
    index = bisect_right(times, at_ns) - 1
    if index < 0:
        return None
    return state_rows[coin][index]


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
    """Reconstruct the follower's prospective cross-margin path without interpolation.

    State changes use the local receipt time of the L2 book that actually executed the
    simulated follower order. Mark/oracle values use captured ``activeAssetCtx`` rows.
    Funding is taken from official hourly ``fundingHistory`` and valued using the last
    captured oracle at or before the payment timestamp. Margin requirements use the most
    recent prospectively captured metadata snapshot at or before each checkpoint.

    Entry fees still attached to open position quantity are subtracted from equity until
    they are allocated into a realized slice. This prevents open-position equity from
    receiving a temporary fee-accounting boost.

    Any missing/stale truth adds a blocker. A path with blockers must never be used to
    promote a validated champion or choose live leverage.
    """
    if max_mark_age_ns <= 0 or max_margin_snapshot_age_ns <= 0 or max_funding_gap_ns <= 0:
        raise ValueError("coverage age limits must be positive")

    states = tuple(
        sorted(
            state_events,
            key=lambda item: (item.execution_received_at_ns, item.source_tid, item.action),
        )
    )
    contexts = tuple(sorted(asset_contexts, key=lambda item: (item.received_at_ns, item.coin)))
    funding = tuple(sorted(funding_rates, key=lambda item: (item.payment_ts_ms, item.coin)))
    snapshots = tuple(sorted(margin_snapshots, key=lambda item: item.fetched_at_ns))

    blockers: set[str] = set()
    if not states:
        blockers.add("NO_FOLLOWER_STATE_EVENTS")
    if not contexts:
        blockers.add("NO_ACTIVE_ASSET_CONTEXT")
    if not snapshots:
        blockers.add("NO_MARGIN_METADATA_SNAPSHOTS")
    if blockers:
        return ContinuousPath(
            checkpoints=(),
            checkpoint_received_at_ns=(),
            coverage=PathCoverage(False, tuple(sorted(blockers)), 0, None, None, 0, ZERO),
        )

    context_rows, context_times = _context_index(contexts)
    state_grouped: dict[str, list[FollowerStateEvent]] = {}
    for row in states:
        state_grouped.setdefault(row.coin, []).append(row)
    state_rows = {
        coin: tuple(sorted(rows, key=lambda item: item.execution_received_at_ns))
        for coin, rows in state_grouped.items()
    }
    state_times = {
        coin: tuple(row.execution_received_at_ns for row in rows)
        for coin, rows in state_rows.items()
    }

    first_state_ns = states[0].execution_received_at_ns
    last_context_ns = contexts[-1].received_at_ns
    if last_context_ns < first_state_ns:
        blockers.add("MARK_STREAM_ENDS_BEFORE_FOLLOWER_STATE")

    # Funding PnL is cumulative and applied exactly once at official payment times.
    funding_by_apply_ns: dict[int, list[tuple[FundingRate, Decimal]]] = {}
    applied_funding_count = 0
    for payment in funding:
        payment_ns = payment.payment_ts_ms * 1_000_000
        if payment_ns < first_state_ns or payment_ns > last_context_ns:
            continue
        state = _state_at_or_before(payment.coin, payment_ns, state_rows, state_times)
        if state is None or state.qty_after == ZERO:
            continue
        oracle = _context_at_or_before(payment.coin, payment_ns, context_rows, context_times)
        if oracle is None:
            blockers.add(f"MISSING_FUNDING_ORACLE:{payment.coin}")
            continue
        if payment_ns - oracle.received_at_ns > max_mark_age_ns:
            blockers.add(f"STALE_FUNDING_ORACLE:{payment.coin}")
            continue
        # Positive funding is paid by longs and received by shorts.
        pnl = -state.qty_after * oracle.oracle_price * payment.funding_rate
        funding_by_apply_ns.setdefault(payment_ns, []).append((payment, pnl))
        applied_funding_count += 1

    # Build one ordered wall-clock event stream. Funding is applied before a mark at the
    # same timestamp; state is applied before the mark so the mark observes the new state.
    event_times = sorted(
        set(
            [row.execution_received_at_ns for row in states]
            + [row.received_at_ns for row in contexts if row.received_at_ns >= first_state_ns]
            + list(funding_by_apply_ns)
        )
    )

    current_qty: dict[str, Decimal] = {}
    current_entry: dict[str, Decimal | None] = {}
    pending_entry_fee: dict[str, Decimal] = {}
    current_realized = ZERO
    cumulative_funding = ZERO
    latest_context: dict[str, AssetContextMark] = {}
    latest_margin_snapshot_ns: int | None = None
    checkpoints: list[EquityCheckpoint] = []
    checkpoint_ns: list[int] = []
    last_event_ns: int | None = None
    open_position_ns = 0
    last_funding_seen: dict[str, int] = {}

    states_at: dict[int, list[FollowerStateEvent]] = {}
    for row in states:
        states_at.setdefault(row.execution_received_at_ns, []).append(row)
    contexts_at: dict[int, list[AssetContextMark]] = {}
    for row in contexts:
        if row.received_at_ns >= first_state_ns:
            contexts_at.setdefault(row.received_at_ns, []).append(row)
    snapshots_at: dict[int, list[MarginMetadataSnapshot]] = {}
    for snapshot in snapshots:
        if snapshot.fetched_at_ns >= first_state_ns:
            snapshots_at.setdefault(snapshot.fetched_at_ns, []).append(snapshot)
            if snapshot.fetched_at_ns not in event_times:
                event_times.append(snapshot.fetched_at_ns)
    event_times = sorted(set(event_times))

    for now_ns in event_times:
        if last_event_ns is not None and any(qty != ZERO for qty in current_qty.values()):
            open_position_ns += max(0, now_ns - last_event_ns)

        for snapshot in snapshots_at.get(now_ns, ()):
            latest_margin_snapshot_ns = snapshot.fetched_at_ns

        for payment, pnl in funding_by_apply_ns.get(now_ns, ()):
            cumulative_funding += pnl
            last_funding_seen[payment.coin] = now_ns

        for state in states_at.get(now_ns, ()):
            current_qty[state.coin] = state.qty_after
            current_entry[state.coin] = state.avg_entry_after
            pending_entry_fee[state.coin] = state.entry_fee_remaining_usd
            current_realized = state.realized_net_pnl_cumulative_usd
            if state.qty_after == ZERO:
                last_funding_seen.pop(state.coin, None)

        # Before accepting a fresh mark, detect whether an existing open position had a
        # mark gap that exceeded the permitted live-parity horizon.
        for coin, qty in current_qty.items():
            if qty == ZERO:
                continue
            previous = latest_context.get(coin)
            if previous is not None and now_ns - previous.received_at_ns > max_mark_age_ns:
                blockers.add(f"MARK_GAP:{coin}")

        for context in contexts_at.get(now_ns, ()):
            latest_context[context.coin] = context

        open_coins = sorted(coin for coin, qty in current_qty.items() if qty != ZERO)
        if not open_coins:
            last_event_ns = now_ns
            continue

        positions: list[OpenPositionMark] = []
        for coin in open_coins:
            qty = current_qty[coin]
            entry = current_entry.get(coin)
            if entry is None:
                blockers.add(f"MISSING_ENTRY:{coin}")
                continue
            mark = latest_context.get(coin)
            if mark is None:
                blockers.add(f"MISSING_MARK:{coin}")
                continue
            if now_ns - mark.received_at_ns > max_mark_age_ns:
                blockers.add(f"STALE_MARK:{coin}")
                continue
            table = snapshot_table_at(snapshots, coin, now_ns)
            if table is None:
                blockers.add(f"MISSING_MARGIN_TABLE:{coin}")
                continue
            snapshot_candidates = [
                snapshot.fetched_at_ns
                for snapshot in snapshots
                if snapshot.fetched_at_ns <= now_ns and coin in snapshot.by_coin()
            ]
            table_snapshot_ns = max(snapshot_candidates) if snapshot_candidates else None
            if table_snapshot_ns is None:
                blockers.add(f"MISSING_MARGIN_SNAPSHOT:{coin}")
                continue
            if now_ns - table_snapshot_ns > max_margin_snapshot_age_ns:
                blockers.add(f"STALE_MARGIN_SNAPSHOT:{coin}")
                continue
            notional = abs(qty * mark.mark_price)
            tier = table.tier_for_notional(notional)
            positions.append(
                OpenPositionMark(
                    coin=coin,
                    qty=qty,
                    avg_entry=entry,
                    mark_price=mark.mark_price,
                    maintenance_margin_rate=tier.maintenance_margin_rate,
                    maintenance_margin_deduction_usd=tier.maintenance_deduction_usd,
                )
            )

            # Once a position has remained open through more than one funding interval,
            # there must be an official payment record no more than 65 minutes behind.
            state_rows_coin = state_rows.get(coin, ())
            open_start = None
            for state_row in reversed(state_rows_coin):
                if state_row.execution_received_at_ns > now_ns:
                    continue
                if state_row.qty_after == ZERO:
                    break
                open_start = state_row.execution_received_at_ns
            reference = last_funding_seen.get(coin, open_start)
            if reference is not None and now_ns - reference > max_funding_gap_ns:
                blockers.add(f"FUNDING_GAP:{coin}")

        if len(positions) != len(open_coins):
            last_event_ns = now_ns
            continue

        # Entry fees are already paid even if the position has not realized yet.
        adjusted_realized = current_realized - sum(
            (pending_entry_fee.get(coin, ZERO) for coin in open_coins),
            ZERO,
        )
        checkpoints.append(
            EquityCheckpoint(
                exchange_ts_ms=now_ns // 1_000_000,
                realized_net_pnl_usd=adjusted_realized,
                funding_pnl_usd=cumulative_funding,
                positions=tuple(positions),
            )
        )
        checkpoint_ns.append(now_ns)
        last_event_ns = now_ns

    if states[-1].execution_received_at_ns > last_context_ns:
        blockers.add("FOLLOWER_STATE_AFTER_LAST_MARK")

    complete = bool(checkpoints) and not blockers
    return ContinuousPath(
        checkpoints=tuple(checkpoints),
        checkpoint_received_at_ns=tuple(checkpoint_ns),
        coverage=PathCoverage(
            complete=complete,
            blockers=tuple(sorted(blockers)),
            checkpoint_count=len(checkpoints),
            first_checkpoint_ns=checkpoint_ns[0] if checkpoint_ns else None,
            last_checkpoint_ns=checkpoint_ns[-1] if checkpoint_ns else None,
            applied_funding_count=applied_funding_count,
            open_position_seconds=D(open_position_ns) / D("1000000000"),
        ),
    )
