from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from hlcopy.profitability.position_copy import (
    CopyFillEvent,
    RealizedSlice,
    _book_for,
    _fill_price,
    _FollowerState,
    _open_qty,
)
from hlcopy.shadow.evaluator import ParquetL2BookProvider
from hlcopy.shadow.latency import LatencyScenario

D = Decimal
ZERO = D("0")
ONE = D("1")
BPS = D("10000")


@dataclass(frozen=True, slots=True)
class FollowerStateEvent:
    coin: str
    execution_ts_ms: int
    source_tid: int
    action: str
    qty_after: Decimal
    avg_entry_after: Decimal | None
    realized_net_pnl_cumulative_usd: Decimal
    entry_fee_remaining_usd: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioCopySimulation:
    lane: str
    wallet_id: str
    wallet_address: str
    scenario: str
    notional_usd: Decimal
    leader_events: int
    executable_events: int
    missed_events: int
    copied_increase_events: int
    realized_slices: tuple[RealizedSlice, ...]
    realized_net_pnl_usd: Decimal
    realized_gross_pnl_usd: Decimal
    total_fees_usd: Decimal
    open_positions: int
    peak_concurrent_gross_notional_usd: Decimal
    state_events: tuple[FollowerStateEvent, ...] = ()


def _allocate_entry_fee(
    remaining_fee: Decimal,
    *,
    close_qty: Decimal,
    position_qty_before: Decimal,
) -> tuple[Decimal, Decimal]:
    """Allocate paid entry fees to the realized fraction of a follower position."""
    if remaining_fee <= ZERO or close_qty <= ZERO or position_qty_before <= ZERO:
        return ZERO, max(ZERO, remaining_fee)
    if close_qty >= position_qty_before:
        return remaining_fee, ZERO
    allocated = remaining_fee * close_qty / position_qty_before
    return allocated, max(ZERO, remaining_fee - allocated)


def simulate_copy_with_portfolio_capital(
    events: Iterable[CopyFillEvent],
    *,
    provider: ParquetL2BookProvider,
    scenario: LatencyScenario,
    notional_usd: Decimal,
    taker_fee_bps: Decimal,
    max_slippage_bps: Decimal,
    max_book_forward_ms: int,
) -> PortfolioCopySimulation:
    """Run causal follower execution while tracking fees, exposure and state history.

    Entry fees are paid when an increase executes and carried as a per-coin fee pool.
    Reductions allocate that pool pro-rata to the quantity realized; a full close/flip
    allocates the entire remainder. Every realized slice therefore contains round-trip
    PnL after allocated entry and exit fees.

    ``state_events`` records the follower position immediately after each successful
    simulated execution at the execution book timestamp. It is the source of truth for
    continuous mark-to-market replay. A failed reduction or flip close never mutates
    follower state.
    """
    ordered = sorted(
        events,
        key=lambda x: (x.exchange_ts_ms, x.wallet_address, x.coin, x.tid),
    )
    if not ordered:
        return PortfolioCopySimulation(
            "UNKNOWN", "", "", scenario.name, notional_usd,
            0, 0, 0, 0, (), ZERO, ZERO, ZERO, 0, ZERO, (),
        )

    lane = ordered[0].lane
    wallet_id = ordered[0].wallet_id
    wallet_address = ordered[0].wallet_address
    states: dict[str, _FollowerState] = {}
    causal_marks: dict[str, Decimal] = {}
    entry_fee_remaining: dict[str, Decimal] = {}
    state_events: list[FollowerStateEvent] = []
    realized: list[RealizedSlice] = []
    executable = 0
    missed = 0
    copied_increases = 0
    total_fees = ZERO
    realized_net_cumulative = ZERO
    peak_gross = ZERO
    fee_rate = taker_fee_bps / BPS

    def update_peak() -> None:
        nonlocal peak_gross
        gross = sum(
            (
                abs(state.qty)
                * causal_marks.get(
                    coin,
                    state.avg_entry if state.avg_entry is not None else ZERO,
                )
                for coin, state in states.items()
                if state.qty != ZERO
            ),
            ZERO,
        )
        peak_gross = max(peak_gross, gross)

    def add_entry_fee(coin: str, qty: Decimal, px: Decimal) -> None:
        nonlocal total_fees
        fee = abs(qty) * px * fee_rate
        total_fees += fee
        entry_fee_remaining[coin] = entry_fee_remaining.get(coin, ZERO) + fee

    def reset_position(coin: str, state: _FollowerState) -> None:
        state.qty = ZERO
        state.avg_entry = None
        state.scale = None
        entry_fee_remaining[coin] = ZERO

    def record_state(
        *,
        event: CopyFillEvent,
        book_ts_ms: int,
        action: str,
        state: _FollowerState,
    ) -> None:
        state_events.append(
            FollowerStateEvent(
                coin=event.coin,
                execution_ts_ms=book_ts_ms,
                source_tid=event.tid,
                action=action,
                qty_after=state.qty,
                avg_entry_after=state.avg_entry,
                realized_net_pnl_cumulative_usd=realized_net_cumulative,
                entry_fee_remaining_usd=entry_fee_remaining.get(event.coin, ZERO),
            )
        )

    for event in ordered:
        state = states.setdefault(event.coin, _FollowerState())
        entry_fee_remaining.setdefault(event.coin, ZERO)
        book, feed_ms = _book_for(provider, event, scenario, max_book_forward_ms)
        if book is None:
            missed += 1
            continue

        causal_marks[event.coin] = book.mid
        update_peak()

        start = event.leader_start
        after = event.leader_after
        delta = event.leader_delta
        same_direction_increase = (
            after != ZERO
            and (start == ZERO or start * after > ZERO)
            and abs(after) > abs(start)
        )
        reduction = start != ZERO and (
            after == ZERO or (start * after > ZERO and abs(after) < abs(start))
        )
        flip = start != ZERO and after != ZERO and start * after < ZERO

        if same_direction_increase:
            signed_qty, scale = _open_qty(
                event=event,
                book=book,
                state=state,
                notional_usd=notional_usd,
            )
            if signed_qty == ZERO:
                continue
            px = _fill_price(
                book,
                signed_qty=signed_qty,
                max_slippage_bps=max_slippage_bps,
            )
            if px is None:
                missed += 1
                continue
            executable += 1
            copied_increases += 1
            old_abs = abs(state.qty)
            add_abs = abs(signed_qty)
            if old_abs == ZERO or state.avg_entry is None or state.qty * signed_qty <= ZERO:
                state.avg_entry = px
                state.qty = signed_qty
            else:
                state.avg_entry = (
                    state.avg_entry * old_abs + px * add_abs
                ) / (old_abs + add_abs)
                state.qty += signed_qty
            state.scale = scale
            causal_marks[event.coin] = px
            add_entry_fee(event.coin, signed_qty, px)
            record_state(event=event, book_ts_ms=book.exchange_ts_ms, action="INCREASE", state=state)
            update_peak()
            continue

        if (
            reduction
            and state.qty != ZERO
            and state.avg_entry is not None
            and state.scale is not None
        ):
            position_abs_before = abs(state.qty)
            close_abs = min(position_abs_before, state.scale * abs(delta))
            signed_close = -close_abs if state.qty > ZERO else close_abs
            px = _fill_price(
                book,
                signed_qty=signed_close,
                max_slippage_bps=max_slippage_bps,
            )
            if px is None:
                missed += 1
                continue
            executable += 1
            direction = "LONG" if state.qty > ZERO else "SHORT"
            sign = ONE if state.qty > ZERO else -ONE
            gross = sign * (px - state.avg_entry) * close_abs
            exit_fee = close_abs * px * fee_rate
            entry_alloc, entry_left = _allocate_entry_fee(
                entry_fee_remaining[event.coin],
                close_qty=close_abs,
                position_qty_before=position_abs_before,
            )
            entry_fee_remaining[event.coin] = entry_left
            net = gross - exit_fee - entry_alloc
            realized_net_cumulative += net
            total_fees += exit_fee
            realized.append(
                RealizedSlice(
                    lane=lane,
                    wallet_id=wallet_id,
                    wallet_address=wallet_address,
                    coin=event.coin,
                    direction=direction,
                    exchange_ts_ms=event.exchange_ts_ms,
                    source_tid=event.tid,
                    feed_ms=feed_ms,
                    action="CLOSE" if after == ZERO else "REDUCE",
                    qty=close_abs,
                    execution_price=px,
                    gross_pnl_usd=gross,
                    fee_usd=exit_fee,
                    net_pnl_usd=net,
                    entry_fee_usd_allocated=entry_alloc,
                )
            )
            state.qty += signed_close
            causal_marks[event.coin] = px
            action = "CLOSE" if after == ZERO else "REDUCE"
            if abs(state.qty) <= D("1e-18") or after == ZERO:
                reset_position(event.coin, state)
            record_state(event=event, book_ts_ms=book.exchange_ts_ms, action=action, state=state)
            update_peak()
            continue

        if flip:
            # A flip is modeled as close-then-open. If the close cannot execute, the
            # follower remains in its existing position and no opposite-side open is
            # attempted. This mirrors a safe live sequencer and avoids synthetic flips.
            if state.qty != ZERO and state.avg_entry is not None:
                close_abs = abs(state.qty)
                signed_close = -state.qty
                px = _fill_price(
                    book,
                    signed_qty=signed_close,
                    max_slippage_bps=max_slippage_bps,
                )
                if px is None:
                    missed += 1
                    continue
                executable += 1
                direction = "LONG" if state.qty > ZERO else "SHORT"
                sign = ONE if state.qty > ZERO else -ONE
                gross = sign * (px - state.avg_entry) * close_abs
                exit_fee = close_abs * px * fee_rate
                entry_alloc = entry_fee_remaining[event.coin]
                net = gross - exit_fee - entry_alloc
                realized_net_cumulative += net
                total_fees += exit_fee
                realized.append(
                    RealizedSlice(
                        lane=lane,
                        wallet_id=wallet_id,
                        wallet_address=wallet_address,
                        coin=event.coin,
                        direction=direction,
                        exchange_ts_ms=event.exchange_ts_ms,
                        source_tid=event.tid,
                        feed_ms=feed_ms,
                        action="FLIP_CLOSE",
                        qty=close_abs,
                        execution_price=px,
                        gross_pnl_usd=gross,
                        fee_usd=exit_fee,
                        net_pnl_usd=net,
                        entry_fee_usd_allocated=entry_alloc,
                    )
                )
                causal_marks[event.coin] = px
                reset_position(event.coin, state)
                record_state(
                    event=event,
                    book_ts_ms=book.exchange_ts_ms,
                    action="FLIP_CLOSE",
                    state=state,
                )
                update_peak()
            else:
                reset_position(event.coin, state)

            synthetic = CopyFillEvent(
                lane=event.lane,
                wallet_id=event.wallet_id,
                wallet_address=event.wallet_address,
                coin=event.coin,
                exchange_ts_ms=event.exchange_ts_ms,
                received_at_ns=event.received_at_ns,
                tid=event.tid,
                leader_start=ZERO,
                leader_after=after,
                leader_delta=after,
                source_price=event.source_price,
            )
            signed_qty, scale = _open_qty(
                event=synthetic,
                book=book,
                state=state,
                notional_usd=notional_usd,
            )
            px = (
                _fill_price(
                    book,
                    signed_qty=signed_qty,
                    max_slippage_bps=max_slippage_bps,
                )
                if signed_qty
                else None
            )
            if px is not None:
                executable += 1
                copied_increases += 1
                state.qty = signed_qty
                state.avg_entry = px
                state.scale = scale
                causal_marks[event.coin] = px
                add_entry_fee(event.coin, signed_qty, px)
                record_state(
                    event=event,
                    book_ts_ms=book.exchange_ts_ms,
                    action="FLIP_OPEN",
                    state=state,
                )
                update_peak()
            elif signed_qty:
                missed += 1

    realized_net = sum((item.net_pnl_usd for item in realized), ZERO)
    realized_gross = sum((item.gross_pnl_usd for item in realized), ZERO)
    return PortfolioCopySimulation(
        lane=lane,
        wallet_id=wallet_id,
        wallet_address=wallet_address,
        scenario=scenario.name,
        notional_usd=notional_usd,
        leader_events=len(ordered),
        executable_events=executable,
        missed_events=missed,
        copied_increase_events=copied_increases,
        realized_slices=tuple(realized),
        realized_net_pnl_usd=realized_net,
        realized_gross_pnl_usd=realized_gross,
        total_fees_usd=total_fees,
        open_positions=sum(state.qty != ZERO for state in states.values()),
        peak_concurrent_gross_notional_usd=peak_gross,
        state_events=tuple(state_events),
    )
