from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal

from hlcopy.profitability.continuous_path_v2 import AssetContextMark, FundingRate
from hlcopy.profitability.margin_tables import CoinMarginTable, MarginMetadataSnapshot
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent

D = Decimal
ZERO = D("0")
ONE_HUNDRED = D("100")
INFINITY = D("Infinity")


@dataclass(slots=True)
class Position:
    qty: Decimal = ZERO
    entry: Decimal | None = None
    fee_remaining: Decimal = ZERO
    opened_ns: int | None = None
    last_funding_ns: int | None = None


@dataclass(slots=True)
class Contribution:
    unrealized: Decimal = ZERO
    gross: Decimal = ZERO
    maintenance: Decimal = ZERO
    exchange_max_leverage: Decimal | None = None
    margin_ts_ns: int | None = None
    valid: bool = False


@dataclass(frozen=True, slots=True)
class MarginIndexes:
    by_coin: dict[str, tuple[tuple[int, CoinMarginTable], ...]]
    times: dict[str, tuple[int, ...]]


@dataclass(slots=True)
class Gap:
    count: int = 0
    first_seen_ns: int | None = None
    last_seen_ns: int | None = None
    max_gap_ns: int = 0

    def observe(self, *, at_ns: int, gap_ns: int) -> None:
        self.count += 1
        if self.first_seen_ns is None:
            self.first_seen_ns = at_ns
        self.last_seen_ns = at_ns
        self.max_gap_ns = max(self.max_gap_ns, max(0, gap_ns))

    def to_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "first_seen_ns": self.first_seen_ns,
            "last_seen_ns": self.last_seen_ns,
            "max_gap_ns": self.max_gap_ns,
            "max_gap_seconds": self.max_gap_ns / 1_000_000_000,
        }


@dataclass(frozen=True, slots=True)
class StreamPassResult:
    blockers: tuple[str, ...]
    checkpoint_count: int
    applied_funding_count: int
    peak_gross: Decimal
    exchange_max_leverage: Decimal | None
    min_free_component_by_leverage: dict[Decimal, Decimal]
    min_liquidation_component: Decimal | None
    max_drawdown_pct_by_leverage: dict[Decimal, Decimal]
    last_mark_ns: int | None
    gaps: dict[str, dict[str, object]]


def build_margin_indexes(
    snapshots: tuple[MarginMetadataSnapshot, ...],
) -> MarginIndexes:
    grouped: dict[str, list[tuple[int, CoinMarginTable]]] = {}
    for snapshot in snapshots:
        for table in snapshot.tables:
            grouped.setdefault(table.coin, []).append((snapshot.fetched_at_ns, table))
    by_coin = {
        coin: tuple(sorted(items, key=lambda item: item[0]))
        for coin, items in grouped.items()
    }
    return MarginIndexes(
        by_coin=by_coin,
        times={coin: tuple(ts for ts, _ in items) for coin, items in by_coin.items()},
    )


def _margin_at(
    coin: str,
    at_ns: int,
    indexes: MarginIndexes,
) -> tuple[int, CoinMarginTable] | None:
    seq = indexes.times.get(coin, ())
    if not seq:
        return None
    index = bisect_right(seq, at_ns) - 1
    if index < 0:
        return None
    return indexes.by_coin[coin][index]


def run_stream_pass(
    *,
    states: tuple[FollowerStateEvent, ...],
    mark_factory: Callable[[], Iterator[AssetContextMark]],
    funding: tuple[FundingRate, ...],
    margin_indexes: MarginIndexes,
    leverages: tuple[Decimal, ...],
    starting_equity_by_leverage: dict[Decimal, Decimal] | None,
    max_mark_age_ns: int,
    max_margin_snapshot_age_ns: int,
    max_funding_gap_ns: int,
) -> StreamPassResult:
    blockers: set[str] = set()
    gaps: dict[str, Gap] = {}
    if not states:
        blockers.add("NO_FOLLOWER_STATE_EVENTS")
        return StreamPassResult(
            tuple(sorted(blockers)), 0, 0, ZERO, None, {}, None, {}, None, {}
        )

    mark_iter = iter(mark_factory())
    current_mark = next(mark_iter, None)
    if current_mark is None:
        blockers.add("NO_ACTIVE_ASSET_CONTEXT")
        return StreamPassResult(
            tuple(sorted(blockers)), 0, 0, ZERO, None, {}, None, {}, None, {}
        )

    positions: dict[str, Position] = {}
    contributions: dict[str, Contribution] = {}
    latest_mark: dict[str, AssetContextMark] = {}
    realized = ZERO
    funding_pnl = ZERO
    total_unrealized = ZERO
    total_gross = ZERO
    total_maintenance = ZERO
    checkpoint_count = 0
    applied_funding = 0
    peak_gross = ZERO
    exchange_max: Decimal | None = None
    min_free_component = {leverage: INFINITY for leverage in leverages}
    min_liquidation_component: Decimal | None = None
    peak_base = ZERO
    max_drawdown_pct = {leverage: ZERO for leverage in leverages}
    last_mark_ns: int | None = None
    state_index = 0
    funding_index = 0

    def note_gap(kind: str, coin: str, *, at_ns: int, gap_ns: int) -> None:
        key = f"{kind}:{coin}"
        gaps.setdefault(key, Gap()).observe(at_ns=at_ns, gap_ns=gap_ns)

    def recompute_coin(coin: str, now_ns: int) -> None:
        nonlocal total_unrealized, total_gross, total_maintenance
        old = contributions.get(coin)
        if old is not None and old.valid:
            total_unrealized -= old.unrealized
            total_gross -= old.gross
            total_maintenance -= old.maintenance
        position = positions.get(coin)
        if position is None or position.qty == ZERO:
            contributions[coin] = Contribution()
            return
        mark = latest_mark.get(coin)
        if mark is None or position.entry is None:
            contributions[coin] = Contribution()
            return
        margin = _margin_at(coin, now_ns, margin_indexes)
        if margin is None:
            contributions[coin] = Contribution()
            return
        margin_ts, table = margin
        gross = abs(position.qty * mark.mark_price)
        tier = table.tier_for_notional(gross)
        contribution = Contribution(
            unrealized=(mark.mark_price - position.entry) * position.qty,
            gross=gross,
            maintenance=max(
                ZERO,
                gross * tier.maintenance_margin_rate - tier.maintenance_deduction_usd,
            ),
            exchange_max_leverage=D("1") / (D("2") * tier.maintenance_margin_rate),
            margin_ts_ns=margin_ts,
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
        next_mark = current_mark.received_at_ns if current_mark is not None else None
        next_funding = (
            funding[funding_index].payment_ts_ms * 1_000_000
            if funding_index < len(funding)
            else None
        )
        available = [value for value in (next_state, next_mark, next_funding) if value is not None]
        if not available:
            break
        now_ns = min(available)
        if current_mark is None and last_mark_ns is not None and now_ns > last_mark_ns:
            break

        while (
            state_index < len(states)
            and states[state_index].execution_received_at_ns == now_ns
        ):
            state = states[state_index]
            position = positions.setdefault(state.coin, Position())
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

        marks_now: dict[str, AssetContextMark] = {}
        while current_mark is not None and current_mark.received_at_ns == now_ns:
            marks_now[current_mark.coin] = current_mark
            last_mark_ns = current_mark.received_at_ns
            current_mark = next(mark_iter, None)

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
                    gap_ns = max_mark_age_ns if oracle is None else now_ns - oracle.received_at_ns
                    note_gap(
                        "FUNDING_ORACLE_COVERAGE",
                        payment.coin,
                        at_ns=now_ns,
                        gap_ns=gap_ns,
                    )
                else:
                    funding_pnl += -position.qty * oracle.oracle_price * payment.funding_rate
                    position.last_funding_ns = now_ns
                    applied_funding += 1
            funding_index += 1

        if marks_now:
            for coin, mark in marks_now.items():
                latest_mark[coin] = mark
                recompute_coin(coin, now_ns)
        else:
            continue

        open_coins = [coin for coin, position in positions.items() if position.qty != ZERO]
        if not open_coins:
            continue

        valid = True
        checkpoint_exchange_max: Decimal | None = None
        for coin in open_coins:
            position = positions[coin]
            mark = latest_mark.get(coin)
            opened_ns = position.opened_ns if position.opened_ns is not None else now_ns
            if mark is None:
                gap_ns = now_ns - opened_ns
                if gap_ns > max_mark_age_ns:
                    blockers.add(f"MISSING_MARK:{coin}")
                    note_gap("MISSING_MARK", coin, at_ns=now_ns, gap_ns=gap_ns)
                valid = False
                continue
            mark_gap = now_ns - mark.received_at_ns
            if mark_gap > max_mark_age_ns:
                blockers.add(f"MARK_GAP:{coin}")
                note_gap("MARK_GAP", coin, at_ns=now_ns, gap_ns=mark_gap)
                valid = False
                continue
            margin = _margin_at(coin, now_ns, margin_indexes)
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
            contribution = contributions.get(coin)
            if contribution is None or not contribution.valid:
                valid = False
                continue
            if contribution.margin_ts_ns != margin_ts:
                recompute_coin(coin, now_ns)
                contribution = contributions.get(coin)
                if contribution is None or not contribution.valid:
                    valid = False
                    continue
            coin_max = contribution.exchange_max_leverage
            if coin_max is None:
                valid = False
                continue
            checkpoint_exchange_max = (
                coin_max
                if checkpoint_exchange_max is None
                else min(checkpoint_exchange_max, coin_max)
            )
            funding_reference = (
                position.last_funding_ns if position.last_funding_ns is not None else opened_ns
            )
            funding_gap = now_ns - funding_reference
            if funding_gap > max_funding_gap_ns:
                blockers.add(f"FUNDING_GAP:{coin}")
                note_gap("FUNDING_GAP", coin, at_ns=now_ns, gap_ns=funding_gap)

        if not valid:
            continue

        adjusted_realized = realized - sum(
            (positions[coin].fee_remaining for coin in open_coins), ZERO
        )
        base_equity = adjusted_realized + funding_pnl + total_unrealized
        checkpoint_count += 1
        peak_gross = max(peak_gross, total_gross)
        if checkpoint_exchange_max is not None:
            exchange_max = (
                checkpoint_exchange_max
                if exchange_max is None
                else min(exchange_max, checkpoint_exchange_max)
            )
        liquidation_component = base_equity - total_maintenance
        min_liquidation_component = (
            liquidation_component
            if min_liquidation_component is None
            else min(min_liquidation_component, liquidation_component)
        )
        for leverage in leverages:
            free_component = base_equity - total_gross / leverage
            min_free_component[leverage] = min(min_free_component[leverage], free_component)

        if starting_equity_by_leverage is not None:
            peak_base = max(peak_base, base_equity)
            drawdown = max(ZERO, peak_base - base_equity)
            for leverage in leverages:
                start = starting_equity_by_leverage[leverage]
                peak_equity = start + peak_base
                drawdown_pct = (
                    drawdown / peak_equity * ONE_HUNDRED if peak_equity > ZERO else ZERO
                )
                max_drawdown_pct[leverage] = max(
                    max_drawdown_pct[leverage], drawdown_pct
                )

    if last_mark_ns is not None and states[-1].execution_received_at_ns > last_mark_ns:
        blockers.add("FOLLOWER_STATE_AFTER_LAST_MARK")

    return StreamPassResult(
        blockers=tuple(sorted(blockers)),
        checkpoint_count=checkpoint_count,
        applied_funding_count=applied_funding,
        peak_gross=peak_gross,
        exchange_max_leverage=exchange_max,
        min_free_component_by_leverage=min_free_component,
        min_liquidation_component=min_liquidation_component,
        max_drawdown_pct_by_leverage=max_drawdown_pct,
        last_mark_ns=last_mark_ns,
        gaps={key: value.to_dict() for key, value in sorted(gaps.items())},
    )
