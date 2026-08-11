from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from hlcopy.copyability.slippage import estimate_marketable_fill
from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.models import Fill
from hlcopy.positions.state_machine import POSITION_EPSILON, normalize_position
from hlcopy.shadow.evaluator import ParquetL2BookProvider, TapeBook
from hlcopy.shadow.latency import LatencyScenario, ObservedSignalLatency

D = Decimal
ZERO = D("0")
ONE = D("1")
BPS = D("10000")


@dataclass(frozen=True, slots=True)
class CopyFillEvent:
    lane: str
    wallet_id: str
    wallet_address: str
    coin: str
    exchange_ts_ms: int
    received_at_ns: int
    tid: int
    leader_start: Decimal
    leader_after: Decimal
    leader_delta: Decimal
    source_price: Decimal

    @property
    def direction_before(self) -> str | None:
        if self.leader_start > ZERO:
            return "LONG"
        if self.leader_start < ZERO:
            return "SHORT"
        return None

    @property
    def direction_after(self) -> str | None:
        if self.leader_after > ZERO:
            return "LONG"
        if self.leader_after < ZERO:
            return "SHORT"
        return None


@dataclass(slots=True)
class _FollowerState:
    qty: Decimal = ZERO
    avg_entry: Decimal | None = None
    scale: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RealizedSlice:
    lane: str
    wallet_id: str
    wallet_address: str
    coin: str
    direction: str
    exchange_ts_ms: int
    source_tid: int
    feed_ms: float
    action: str
    qty: Decimal
    execution_price: Decimal
    gross_pnl_usd: Decimal
    fee_usd: Decimal
    net_pnl_usd: Decimal
    entry_fee_usd_allocated: Decimal

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in row.items()}


@dataclass(frozen=True, slots=True)
class CopySimulation:
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


def _normalized(value: Decimal) -> Decimal:
    value = normalize_position(value)
    return ZERO if abs(value) <= POSITION_EPSILON else value


def _event_from_fill(
    *,
    lane: str,
    wallet_id: str,
    wallet_address: str,
    received_at_ns: int,
    raw_fill: dict[str, Any],
) -> CopyFillEvent | None:
    try:
        fill = Fill.from_raw(wallet_address, raw_fill)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    start = _normalized(fill.start_position)
    delta = _normalized(fill.signed_size)
    after = _normalized(start + delta)
    return CopyFillEvent(
        lane=lane,
        wallet_id=wallet_id,
        wallet_address=wallet_address.lower(),
        coin=canonical_coin(fill.coin),
        exchange_ts_ms=fill.timestamp_ms,
        received_at_ns=received_at_ns,
        tid=fill.tid,
        leader_start=start,
        leader_after=after,
        leader_delta=delta,
        source_price=fill.price,
    )


def _jsonl(folder: Path) -> Iterable[dict[str, Any]]:
    if not folder.exists():
        return ()
    rows: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def load_direct_events(shadow_dir: Path, wallet_id: str) -> tuple[CopyFillEvent, ...]:
    events: list[CopyFillEvent] = []
    seen: set[tuple[int, int]] = set()
    for row in _jsonl(shadow_dir / "fills"):
        if row.get("kind") != "wallet_fill" or row.get("wallet_id") != wallet_id:
            continue
        if row.get("is_snapshot"):
            continue
        raw = row.get("fill")
        if not isinstance(raw, dict):
            continue
        try:
            received = int(row["received_at_ns"])
        except (KeyError, TypeError, ValueError):
            continue
        event = _event_from_fill(
            lane="DIRECT",
            wallet_id=wallet_id,
            wallet_address=str(row.get("wallet_address") or ""),
            received_at_ns=received,
            raw_fill=raw,
        )
        if event is None or (event.exchange_ts_ms, event.tid) in seen:
            continue
        seen.add((event.exchange_ts_ms, event.tid))
        events.append(event)
    events.sort(key=lambda x: (x.exchange_ts_ms, x.tid))
    return tuple(events)


def load_wide_events(enriched_dir: Path, *, cutoff_ns: int) -> tuple[CopyFillEvent, ...]:
    events: list[CopyFillEvent] = []
    seen: set[tuple[str, int, int]] = set()
    for row in _jsonl(enriched_dir):
        if row.get("kind") != "wide_official_fill":
            continue
        try:
            received = int(row["public_received_at_ns"])
        except (KeyError, TypeError, ValueError):
            continue
        if received < cutoff_ns:
            continue
        raw = row.get("official_fill")
        if not isinstance(raw, dict):
            continue
        address = str(row.get("wallet_address") or "").lower()
        event = _event_from_fill(
            lane="WIDE",
            wallet_id=str(row.get("wallet_id") or address),
            wallet_address=address,
            received_at_ns=received,
            raw_fill=raw,
        )
        if event is None:
            continue
        key = (address, event.exchange_ts_ms, event.tid)
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    events.sort(key=lambda x: (x.exchange_ts_ms, x.wallet_address, x.coin, x.tid))
    return tuple(events)


def _book_for(
    provider: ParquetL2BookProvider,
    event: CopyFillEvent,
    scenario: LatencyScenario,
    max_book_forward_ms: int,
) -> tuple[TapeBook | None, float]:
    observed = ObservedSignalLatency(event.exchange_ts_ms, event.received_at_ns)
    try:
        target = observed.estimated_order_arrival_ms(scenario)
    except ValueError:
        return None, observed.feed_ms
    book = provider.first_at_or_after(wire_coin(event.coin), target)
    if book is None or book.exchange_ts_ms - target > max_book_forward_ms:
        return None, observed.feed_ms
    return book, observed.feed_ms


def _fill_price(
    book: TapeBook,
    *,
    signed_qty: Decimal,
    max_slippage_bps: Decimal,
) -> Decimal | None:
    if signed_qty == ZERO:
        return None
    side = "BUY" if signed_qty > ZERO else "SELL"
    levels = list(book.asks if side == "BUY" else book.bids)
    result = estimate_marketable_fill(
        side=side,
        quantity=abs(signed_qty),
        levels=levels,
        reference_mid=book.mid,
        max_slippage_bps=max_slippage_bps,
    )
    return result.vwap if result.complete else None


def _open_qty(
    *,
    event: CopyFillEvent,
    book: TapeBook,
    state: _FollowerState,
    notional_usd: Decimal,
) -> tuple[Decimal, Decimal]:
    after_abs = abs(event.leader_after)
    delta_abs = abs(event.leader_delta)
    if after_abs == ZERO or delta_abs == ZERO:
        return ZERO, ZERO
    if state.scale is None:
        # Copy only the prospectively observed portion of an already-open leader position.
        # A true flat->open event therefore copies 100% of configured notional.
        observed_fraction = min(ONE, delta_abs / after_abs)
        follower_notional = notional_usd * observed_fraction
        qty = follower_notional / book.mid
        scale = qty / delta_abs
    else:
        scale = state.scale
        qty = scale * delta_abs
    remaining_notional = max(ZERO, notional_usd - abs(state.qty) * book.mid)
    qty = min(qty, remaining_notional / book.mid if book.mid > ZERO else ZERO)
    signed = qty if event.leader_delta > ZERO else -qty
    return signed, scale


def simulate_copy(
    events: Iterable[CopyFillEvent],
    *,
    provider: ParquetL2BookProvider,
    scenario: LatencyScenario,
    notional_usd: Decimal,
    taker_fee_bps: Decimal,
    max_slippage_bps: Decimal,
    max_book_forward_ms: int,
) -> CopySimulation:
    ordered = sorted(events, key=lambda x: (x.exchange_ts_ms, x.wallet_address, x.coin, x.tid))
    if not ordered:
        return CopySimulation("UNKNOWN", "", "", scenario.name, notional_usd, 0, 0, 0, 0, (), ZERO, ZERO, ZERO, 0)
    lane = ordered[0].lane
    wallet_id = ordered[0].wallet_id
    wallet_address = ordered[0].wallet_address
    states: dict[str, _FollowerState] = {}
    realized: list[RealizedSlice] = []
    executable = 0
    missed = 0
    copied_increases = 0
    total_fees = ZERO
    fee_rate = taker_fee_bps / BPS

    for event in ordered:
        state = states.setdefault(event.coin, _FollowerState())
        book, feed_ms = _book_for(provider, event, scenario, max_book_forward_ms)
        if book is None:
            missed += 1
            continue

        start = event.leader_start
        after = event.leader_after
        delta = event.leader_delta
        same_direction_increase = (
            after != ZERO
            and (start == ZERO or start * after > ZERO)
            and abs(after) > abs(start)
        )
        reduction = start != ZERO and (after == ZERO or (start * after > ZERO and abs(after) < abs(start)))
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
            px = _fill_price(book, signed_qty=signed_qty, max_slippage_bps=max_slippage_bps)
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
                state.avg_entry = (state.avg_entry * old_abs + px * add_abs) / (old_abs + add_abs)
                state.qty += signed_qty
            state.scale = scale
            total_fees += add_abs * px * fee_rate
            continue

        if reduction and state.qty != ZERO and state.avg_entry is not None and state.scale is not None:
            close_abs = min(abs(state.qty), state.scale * abs(delta))
            signed_close = -close_abs if state.qty > ZERO else close_abs
            px = _fill_price(book, signed_qty=signed_close, max_slippage_bps=max_slippage_bps)
            if px is None:
                missed += 1
                continue
            executable += 1
            direction = "LONG" if state.qty > ZERO else "SHORT"
            sign = ONE if state.qty > ZERO else -ONE
            gross = sign * (px - state.avg_entry) * close_abs
            exit_fee = close_abs * px * fee_rate
            net = gross - exit_fee
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
                    entry_fee_usd_allocated=ZERO,
                )
            )
            state.qty += signed_close
            if abs(state.qty) <= D("1e-18") or after == ZERO:
                state.qty = ZERO
                state.avg_entry = None
                state.scale = None
            continue

        if flip:
            if state.qty != ZERO and state.avg_entry is not None:
                close_abs = abs(state.qty)
                signed_close = -state.qty
                px = _fill_price(book, signed_qty=signed_close, max_slippage_bps=max_slippage_bps)
                if px is not None:
                    executable += 1
                    direction = "LONG" if state.qty > ZERO else "SHORT"
                    sign = ONE if state.qty > ZERO else -ONE
                    gross = sign * (px - state.avg_entry) * close_abs
                    exit_fee = close_abs * px * fee_rate
                    net = gross - exit_fee
                    total_fees += exit_fee
                    realized.append(
                        RealizedSlice(lane, wallet_id, wallet_address, event.coin, direction, event.exchange_ts_ms, event.tid, feed_ms, "FLIP_CLOSE", close_abs, px, gross, exit_fee, net, ZERO)
                    )
                else:
                    missed += 1
            state.qty = ZERO
            state.avg_entry = None
            state.scale = None
            # Treat the post-flip remainder as a fresh prospectively observed open.
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
            signed_qty, scale = _open_qty(event=synthetic, book=book, state=state, notional_usd=notional_usd)
            px = _fill_price(book, signed_qty=signed_qty, max_slippage_bps=max_slippage_bps) if signed_qty else None
            if px is not None:
                executable += 1
                copied_increases += 1
                state.qty = signed_qty
                state.avg_entry = px
                state.scale = scale
                total_fees += abs(signed_qty) * px * fee_rate
            elif signed_qty:
                missed += 1

    realized_net = sum((x.net_pnl_usd for x in realized), ZERO)
    realized_gross = sum((x.gross_pnl_usd for x in realized), ZERO)
    return CopySimulation(
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
    )
