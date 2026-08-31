"""Net-edge reconstruction and attribution for the Invo notification shadow ledger.

The notification executor writes an append-only JSONL audit stream. Until now that
stream was only summarised by an ad-hoc probe that reported **gross** mid-to-mid
copied PnL over completed trades. That number cannot support a capital decision:

* it excludes execution cost entirely (taker fee, spread, impact, funding);
* it is survivorship-biased, because a source position that is still open never
  emits ``shadow_closed`` and therefore never enters the sample;
* it is unattributed, so a positive aggregate can be produced by one trader, one
  coin or one outlier trade while the rest of the followed universe loses money;
* it discards the arrival-latency distribution of the signals we refused.

This module reconstructs the ledger into scored trades, verifies the reported
economics against an independently recomputed value, and attributes realistic net
edge across arbitrary slices (trader, coin, direction, leverage band, signal age,
hold time).

Cost handling is deliberately assumption-light. Rather than hard-coding a spread
guess, every slice reports ``breakeven_cost_bps`` -- the round-trip execution cost
at which the slice's mean return reaches zero -- alongside net results at explicit
cost scenarios. A slice is only ever promotable when its uncertainty *lower bound*
clears the cost it must actually pay.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

D = Decimal
ZERO = D("0")
BPS = D("10000")

OPEN_TYPES = frozenset({"shadow_opened", "shadow_opened_from_increase"})
REUP_TYPE = "shadow_reupped"
CLOSE_TYPE = "shadow_closed"
SKIP_TYPE = "skip"
STALE_SKIP_REASON = "stale_signal_over_25s_window"

#: Hyperliquid base-tier perp taker fee, one side, in basis points.
HL_TAKER_FEE_BPS = D("4.5")

#: Round-trip cost scenarios reported for every slice, in basis points of notional.
#: ``9`` is two Hyperliquid taker fees and nothing else -- an unreachable floor that
#: assumes we cross a zero-width spread with no impact. The wider scenarios add
#: progressively more spread/impact and are the realistic range for alt-coin books.
DEFAULT_COST_SCENARIOS_BPS: tuple[Decimal, ...] = (D("9"), D("15"), D("25"), D("40"))

#: Cost used for promotion verdicts unless the caller overrides it.
DEFAULT_REFERENCE_COST_BPS = D("15")

#: Default bootstrap confidence level, hoisted so it is not constructed per call.
DEFAULT_CONFIDENCE = D("0.90")


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _signal(row: Mapping[str, Any]) -> Mapping[str, Any]:
    signal = row.get("signal")
    return signal if isinstance(signal, Mapping) else {}


def _field(row: Mapping[str, Any], name: str) -> Any:
    """Read a field from the audit row, falling back to its embedded signal."""
    value = row.get(name)
    return _signal(row).get(name) if value is None else value


def _source_id(row: Mapping[str, Any]) -> str:
    return _text(_field(row, "sourceBaseId"))


def _row_epoch_ms(row: Mapping[str, Any]) -> int | None:
    raw = row.get("ts")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def load_audit_rows(path: Path) -> tuple[dict[str, Any], ...]:
    """Read the append-only audit stream, skipping unparsable lines."""
    if not path.exists():
        return ()
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class ShadowTrade:
    """One reconstructed shadow copy trade, closed or still open."""

    source_base_id: str
    username: str
    coin: str
    side: str
    leverage: int
    opened_at_ms: int
    entry_mid: Decimal
    size: Decimal
    notional_usd: Decimal
    margin_usd: Decimal
    add_count: int
    closed_at_ms: int | None = None
    exit_mid: Decimal | None = None
    source_entry_price: Decimal | None = None
    source_closing_price: Decimal | None = None
    signal_age_ms: int | None = None
    entry_chase_bps: Decimal | None = None
    reported_gross_pnl_usd: Decimal | None = None

    @property
    def closed(self) -> bool:
        return self.exit_mid is not None and self.closed_at_ms is not None

    @property
    def direction(self) -> Decimal:
        return D("1") if self.side == "long" else D("-1")

    @property
    def exit_notional_usd(self) -> Decimal | None:
        return None if self.exit_mid is None else self.size * self.exit_mid

    @property
    def hold_ms(self) -> int | None:
        if self.closed_at_ms is None:
            return None
        return self.closed_at_ms - self.opened_at_ms

    @property
    def gross_pnl_usd(self) -> Decimal | None:
        """Mid-to-mid PnL recomputed from entry/exit rather than trusted from the row."""
        if self.exit_mid is None:
            return None
        return (self.exit_mid - self.entry_mid) * self.size * self.direction

    @property
    def gross_return_bps(self) -> Decimal | None:
        if self.exit_mid is None or self.entry_mid <= ZERO:
            return None
        return (self.exit_mid - self.entry_mid) / self.entry_mid * self.direction * BPS

    @property
    def exit_vs_source_bps(self) -> Decimal | None:
        """How much better (+) or worse (-) our exit mid was than the source's close.

        The executor records ``sourceClosingPrice`` on every close but has never
        compared it to the price we actually saw. This is the exit-side analogue of
        entry chase and is the only available measure of how late our exits are.
        """
        source = self.source_closing_price
        if source is None or source <= ZERO or self.exit_mid is None:
            return None
        return (self.exit_mid - source) / source * self.direction * BPS

    def cost_usd(self, round_trip_bps: Decimal) -> Decimal | None:
        """Execution cost charged half on the entry leg and half on the exit leg."""
        exit_notional = self.exit_notional_usd
        if exit_notional is None:
            return None
        per_side = round_trip_bps / D("2") / BPS
        return self.notional_usd * per_side + exit_notional * per_side

    def net_pnl_usd(self, round_trip_bps: Decimal) -> Decimal | None:
        gross = self.gross_pnl_usd
        cost = self.cost_usd(round_trip_bps)
        if gross is None or cost is None:
            return None
        return gross - cost

    def net_return_bps(self, round_trip_bps: Decimal) -> Decimal | None:
        net = self.net_pnl_usd(round_trip_bps)
        if net is None or self.notional_usd <= ZERO:
            return None
        return net / self.notional_usd * BPS


@dataclass(frozen=True, slots=True)
class LedgerIntegrity:
    """Data-quality counters raised during reconstruction.

    Mirrors the repository's existing ``startPosition`` reconstruction invariant:
    disagreement is surfaced as a data-quality failure rather than silently
    producing PnL that nothing verified.
    """

    audit_rows: int
    open_rows: int
    reup_rows: int
    close_rows: int
    duplicate_open_rows: int
    duplicate_close_rows: int
    orphan_close_rows: int
    unpriced_rows: int
    pnl_mismatch_rows: int

    def to_dict(self) -> dict[str, int]:
        return {
            "audit_rows": self.audit_rows,
            "open_rows": self.open_rows,
            "reup_rows": self.reup_rows,
            "close_rows": self.close_rows,
            "duplicate_open_rows": self.duplicate_open_rows,
            "duplicate_close_rows": self.duplicate_close_rows,
            "orphan_close_rows": self.orphan_close_rows,
            "unpriced_rows": self.unpriced_rows,
            "pnl_mismatch_rows": self.pnl_mismatch_rows,
        }


@dataclass(frozen=True, slots=True)
class StaleSkip:
    """A signal the freshness gate refused."""

    username: str
    coin: str
    side: str
    age_ms: int


@dataclass(frozen=True, slots=True)
class Ledger:
    trades: tuple[ShadowTrade, ...]
    stale_skips: tuple[StaleSkip, ...]
    integrity: LedgerIntegrity

    @property
    def closed(self) -> tuple[ShadowTrade, ...]:
        return tuple(trade for trade in self.trades if trade.closed)

    @property
    def open(self) -> tuple[ShadowTrade, ...]:
        return tuple(trade for trade in self.trades if not trade.closed)


@dataclass
class _OpenTrade:
    source_base_id: str
    username: str
    coin: str
    side: str
    leverage: int
    opened_at_ms: int
    entry_mid: Decimal
    size: Decimal
    notional_usd: Decimal
    margin_usd: Decimal
    add_count: int = 0
    source_entry_price: Decimal | None = None
    signal_age_ms: int | None = None
    entry_chase_bps: Decimal | None = None


def _pnl_disagrees(trade: ShadowTrade) -> bool:
    reported = trade.reported_gross_pnl_usd
    recomputed = trade.gross_pnl_usd
    if reported is None or recomputed is None:
        return False
    scale = max(abs(reported), abs(recomputed), D("1"))
    return abs(reported - recomputed) / scale > D("0.001")


def reconstruct_ledger(rows: Iterable[Mapping[str, Any]]) -> Ledger:
    """Pair open/re-up/close audit rows into scored trades.

    A close without a matching open is *not* scored. The audit stream can begin
    mid-position, and inventing an entry price for those rows would manufacture PnL.
    """
    live: dict[str, _OpenTrade] = {}
    trades: list[ShadowTrade] = []
    stale: list[StaleSkip] = []
    audit_rows = 0
    open_rows = reup_rows = close_rows = 0
    duplicate_open = duplicate_close = orphan_close = unpriced = 0
    closed_ids: set[str] = set()

    for row in rows:
        audit_rows += 1
        row_type = _text(row.get("type"))

        if row_type == SKIP_TYPE:
            if _text(row.get("reason")) != STALE_SKIP_REASON:
                continue
            age = _int(row.get("ageMs"))
            if age is None:
                continue
            signal = _signal(row)
            stale.append(
                StaleSkip(
                    username=_text(signal.get("username")).lower(),
                    coin=_text(signal.get("coin")).upper(),
                    side=_text(signal.get("side")).lower(),
                    age_ms=age,
                )
            )
            continue

        if row_type in OPEN_TYPES:
            open_rows += 1
            source_id = _source_id(row)
            entry_mid = _decimal(row.get("entryMid"))
            size = _decimal(row.get("size"))
            opened_at = _row_epoch_ms(row)
            if not source_id or entry_mid is None or size is None or opened_at is None:
                unpriced += 1
                continue
            if entry_mid <= ZERO or size <= ZERO:
                unpriced += 1
                continue
            if source_id in live:
                duplicate_open += 1
                continue
            notional = _decimal(row.get("notionalUsd")) or entry_mid * size
            live[source_id] = _OpenTrade(
                source_base_id=source_id,
                username=_text(_field(row, "username")).lower(),
                coin=_text(_field(row, "coin")).upper(),
                side=_text(_field(row, "side")).lower(),
                leverage=_int(_field(row, "leverage")) or 1,
                opened_at_ms=opened_at,
                entry_mid=entry_mid,
                size=size,
                notional_usd=notional,
                margin_usd=_decimal(row.get("marginUsd")) or ZERO,
                source_entry_price=_decimal(_signal(row).get("entryPrice")),
                signal_age_ms=_int(row.get("detectionLatencyMs")),
                entry_chase_bps=_decimal(row.get("chaseBps")),
            )
            continue

        if row_type == REUP_TYPE:
            reup_rows += 1
            current = live.get(_source_id(row))
            if current is None:
                continue
            new_size = _decimal(row.get("newSize"))
            new_entry = _decimal(row.get("newEntryMid"))
            if new_size is not None and new_size > ZERO:
                current.size = new_size
            if new_entry is not None and new_entry > ZERO:
                current.entry_mid = new_entry
            add_notional = _decimal(row.get("addNotionalUsd"))
            if add_notional is not None:
                current.notional_usd += add_notional
            add_margin = _decimal(row.get("addMarginUsd"))
            if add_margin is not None:
                current.margin_usd += add_margin
            current.add_count += 1
            continue

        if row_type != CLOSE_TYPE:
            continue

        close_rows += 1
        source_id = _source_id(row)
        current = live.pop(source_id, None)
        if current is None:
            # Invo can emit more than one close post per source position. A repeat
            # after the position was already scored is a duplicate; a close we never
            # owned an entry for is an orphan. Neither may be scored.
            if source_id in closed_ids:
                duplicate_close += 1
            else:
                orphan_close += 1
            continue
        closed_ids.add(source_id)
        exit_mid = _decimal(row.get("exitMid"))
        closed_at = _row_epoch_ms(row)
        if exit_mid is None or exit_mid <= ZERO or closed_at is None:
            unpriced += 1
            continue
        trades.append(
            ShadowTrade(
                source_base_id=current.source_base_id,
                username=current.username or _text(_field(row, "username")).lower(),
                coin=current.coin,
                side=current.side,
                leverage=current.leverage,
                opened_at_ms=current.opened_at_ms,
                entry_mid=current.entry_mid,
                size=current.size,
                notional_usd=current.notional_usd,
                margin_usd=current.margin_usd,
                add_count=current.add_count,
                closed_at_ms=closed_at,
                exit_mid=exit_mid,
                source_entry_price=current.source_entry_price,
                source_closing_price=_decimal(row.get("sourceClosingPrice")),
                signal_age_ms=current.signal_age_ms,
                entry_chase_bps=current.entry_chase_bps,
                reported_gross_pnl_usd=_decimal(row.get("grossPnlUsd")),
            )
        )

    for current in live.values():
        trades.append(
            ShadowTrade(
                source_base_id=current.source_base_id,
                username=current.username,
                coin=current.coin,
                side=current.side,
                leverage=current.leverage,
                opened_at_ms=current.opened_at_ms,
                entry_mid=current.entry_mid,
                size=current.size,
                notional_usd=current.notional_usd,
                margin_usd=current.margin_usd,
                add_count=current.add_count,
                source_entry_price=current.source_entry_price,
                signal_age_ms=current.signal_age_ms,
                entry_chase_bps=current.entry_chase_bps,
            )
        )

    trades.sort(key=lambda trade: (trade.closed_at_ms or trade.opened_at_ms))
    integrity = LedgerIntegrity(
        audit_rows=audit_rows,
        open_rows=open_rows,
        reup_rows=reup_rows,
        close_rows=close_rows,
        duplicate_open_rows=duplicate_open,
        duplicate_close_rows=duplicate_close,
        orphan_close_rows=orphan_close,
        unpriced_rows=unpriced,
        pnl_mismatch_rows=sum(1 for trade in trades if _pnl_disagrees(trade)),
    )
    return Ledger(trades=tuple(trades), stale_skips=tuple(stale), integrity=integrity)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

#: Signal-age buckets in milliseconds. The executor accepts signals up to 25s old,
#: so edge decay across these buckets is the direct test of whether Invo's
#: publication latency is destroying the copied edge.
SIGNAL_AGE_BUCKETS_MS: tuple[tuple[str, int, int], ...] = (
    ("age_0_2s", 0, 2_000),
    ("age_2_5s", 2_000, 5_000),
    ("age_5_10s", 5_000, 10_000),
    ("age_10_15s", 10_000, 15_000),
    ("age_15s_plus", 15_000, 2**62),
)

HOLD_BUCKETS_MS: tuple[tuple[str, int, int], ...] = (
    ("hold_under_15m", 0, 900_000),
    ("hold_15m_1h", 900_000, 3_600_000),
    ("hold_1h_6h", 3_600_000, 21_600_000),
    ("hold_6h_24h", 21_600_000, 86_400_000),
    ("hold_over_24h", 86_400_000, 2**62),
)

LEVERAGE_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("lev_1_3x", 1, 4),
    ("lev_4_10x", 4, 11),
    ("lev_11_25x", 11, 26),
    ("lev_over_25x", 26, 2**31),
)


def _bucket(value: int | None, buckets: Sequence[tuple[str, int, int]]) -> str:
    if value is None:
        return "unknown"
    for name, low, high in buckets:
        if low <= value < high:
            return name
    return "unknown"


def signal_age_bucket(trade: ShadowTrade) -> str:
    return _bucket(trade.signal_age_ms, SIGNAL_AGE_BUCKETS_MS)


def hold_bucket(trade: ShadowTrade) -> str:
    return _bucket(trade.hold_ms, HOLD_BUCKETS_MS)


def leverage_bucket(trade: ShadowTrade) -> str:
    return _bucket(trade.leverage, LEVERAGE_BUCKETS)


#: Slice dimensions evaluated by default. A trader is rarely uniformly good: these
#: keys let one profitable trader-coin-direction combination be promoted without
#: adopting the rest of that trader's book.
DEFAULT_DIMENSIONS: tuple[tuple[str, Callable[[ShadowTrade], str]], ...] = (
    ("all", lambda t: "ALL"),
    ("trader", lambda t: t.username),
    ("coin", lambda t: t.coin),
    ("side", lambda t: t.side),
    ("trader_coin", lambda t: f"{t.username}|{t.coin}"),
    ("trader_side", lambda t: f"{t.username}|{t.side}"),
    ("trader_coin_side", lambda t: f"{t.username}|{t.coin}|{t.side}"),
    ("signal_age", signal_age_bucket),
    ("hold_time", hold_bucket),
    ("leverage", leverage_bucket),
)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / D("2")


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, ZERO) / D(len(values))


def bootstrap_mean_ci(
    values: Sequence[Decimal],
    *,
    confidence: Decimal = DEFAULT_CONFIDENCE,
    resamples: int = 2000,
    seed: int = 20260831,
) -> tuple[Decimal, Decimal] | None:
    """Seeded percentile bootstrap on the mean.

    Seeded so a promotion decision is reproducible from the same ledger: a gate that
    flickers between runs is not a gate.
    """
    count = len(values)
    if count < 2:
        return None
    rng = random.Random(seed)
    floats = [float(value) for value in values]
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(count):
            total += floats[rng.randrange(count)]
        means.append(total / count)
    means.sort()
    tail = (D("1") - confidence) / D("2")
    low_index = int(float(tail) * resamples)
    high_index = min(resamples - 1, int((1.0 - float(tail)) * resamples))
    return D(str(means[low_index])), D(str(means[high_index]))


def _max_drawdown_usd(pnls: Sequence[Decimal]) -> Decimal:
    """Peak-to-trough drawdown of the cumulative closed-PnL path."""
    peak = ZERO
    equity = ZERO
    worst = ZERO
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return -worst


def _distinct_days(trades: Sequence[ShadowTrade]) -> int:
    days = set()
    for trade in trades:
        stamp = trade.closed_at_ms
        if stamp is not None:
            days.add(datetime.fromtimestamp(stamp / 1000, tz=UTC).date())
    return len(days)


def _concentration(pnls: Sequence[Decimal]) -> Decimal | None:
    """Share of total gross profit contributed by the single best trade."""
    gains = [pnl for pnl in pnls if pnl > ZERO]
    if not gains:
        return None
    total = sum(gains, ZERO)
    return max(gains) / total if total > ZERO else None


@dataclass(frozen=True, slots=True)
class EdgePolicy:
    """Evidence required before a slice may be proposed for micro-live capital."""

    min_closed_trades: int = 30
    min_distinct_days: int = 5
    reference_cost_bps: Decimal = DEFAULT_REFERENCE_COST_BPS
    max_profit_concentration: Decimal = D("0.50")
    max_open_trade_share: Decimal = D("0.35")
    confidence: Decimal = DEFAULT_CONFIDENCE
    bootstrap_resamples: int = 2000


#: Shared default policy instance so callers do not construct one per invocation.
DEFAULT_POLICY = EdgePolicy()


@dataclass(frozen=True, slots=True)
class SliceEdge:
    dimension: str
    key: str
    closed_trades: int
    open_trades: int
    distinct_days: int
    gross_pnl_usd: Decimal
    gross_notional_usd: Decimal
    mean_gross_return_bps: Decimal | None
    median_gross_return_bps: Decimal | None
    gross_return_ci_low_bps: Decimal | None
    gross_return_ci_high_bps: Decimal | None
    breakeven_cost_bps: Decimal | None
    win_rate: Decimal | None
    worst_trade_bps: Decimal | None
    profit_concentration: Decimal | None
    max_drawdown_usd: Decimal
    median_signal_age_ms: Decimal | None
    median_entry_chase_bps: Decimal | None
    median_exit_vs_source_bps: Decimal | None
    net_by_cost_bps: dict[str, dict[str, Decimal | None]]
    verdict: str
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        def render(value: object) -> object:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {key: render(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [render(item) for item in value]
            return value

        return {
            "dimension": self.dimension,
            "key": self.key,
            "closed_trades": self.closed_trades,
            "open_trades": self.open_trades,
            "distinct_days": self.distinct_days,
            "gross_pnl_usd": render(self.gross_pnl_usd),
            "gross_notional_usd": render(self.gross_notional_usd),
            "mean_gross_return_bps": render(self.mean_gross_return_bps),
            "median_gross_return_bps": render(self.median_gross_return_bps),
            "gross_return_ci_low_bps": render(self.gross_return_ci_low_bps),
            "gross_return_ci_high_bps": render(self.gross_return_ci_high_bps),
            "breakeven_cost_bps": render(self.breakeven_cost_bps),
            "win_rate": render(self.win_rate),
            "worst_trade_bps": render(self.worst_trade_bps),
            "profit_concentration": render(self.profit_concentration),
            "max_drawdown_usd": render(self.max_drawdown_usd),
            "median_signal_age_ms": render(self.median_signal_age_ms),
            "median_entry_chase_bps": render(self.median_entry_chase_bps),
            "median_exit_vs_source_bps": render(self.median_exit_vs_source_bps),
            "net_by_cost_bps": render(self.net_by_cost_bps),
            "verdict": self.verdict,
            "blockers": list(self.blockers),
        }


def score_slice(
    dimension: str,
    key: str,
    trades: Sequence[ShadowTrade],
    *,
    policy: EdgePolicy = DEFAULT_POLICY,
    cost_scenarios_bps: Sequence[Decimal] = DEFAULT_COST_SCENARIOS_BPS,
) -> SliceEdge:
    closed = [trade for trade in trades if trade.closed]
    open_trades = [trade for trade in trades if not trade.closed]
    returns = [
        value for value in (trade.gross_return_bps for trade in closed) if value is not None
    ]
    pnls = [value for value in (trade.gross_pnl_usd for trade in closed) if value is not None]
    notional = sum((trade.notional_usd for trade in closed), ZERO)

    mean_return = _mean(returns)
    ci = (
        bootstrap_mean_ci(
            returns,
            confidence=policy.confidence,
            resamples=policy.bootstrap_resamples,
        )
        if len(returns) >= 2
        else None
    )
    distinct_days = _distinct_days(closed)
    concentration = _concentration(pnls)

    net_by_cost: dict[str, dict[str, Decimal | None]] = {}
    for cost in cost_scenarios_bps:
        net_pnls = [
            value for value in (trade.net_pnl_usd(cost) for trade in closed) if value is not None
        ]
        net_returns = [
            value for value in (trade.net_return_bps(cost) for trade in closed) if value is not None
        ]
        net_by_cost[str(cost)] = {
            "net_pnl_usd": sum(net_pnls, ZERO),
            "mean_net_return_bps": _mean(net_returns),
            "median_net_return_bps": _median(net_returns),
            "net_win_rate": (
                D(sum(1 for value in net_pnls if value > ZERO)) / D(len(net_pnls))
                if net_pnls
                else None
            ),
            "max_drawdown_usd": _max_drawdown_usd(net_pnls),
        }

    blockers: list[str] = []
    if len(closed) < policy.min_closed_trades:
        blockers.append(f"SAMPLE_{len(closed)}_OF_{policy.min_closed_trades}")
    if distinct_days < policy.min_distinct_days:
        blockers.append(f"DAYS_{distinct_days}_OF_{policy.min_distinct_days}")
    if ci is None:
        blockers.append("NO_UNCERTAINTY_ESTIMATE")
    elif ci[0] <= policy.reference_cost_bps:
        blockers.append(f"CI_LOW_{ci[0]:.1f}BPS_UNDER_COST_{policy.reference_cost_bps}BPS")
    if concentration is not None and concentration > policy.max_profit_concentration:
        blockers.append(f"CONCENTRATED_{concentration:.2f}")
    total = len(closed) + len(open_trades)
    if total and D(len(open_trades)) / D(total) > policy.max_open_trade_share:
        # Unclosed positions never emit a close row, so a slice whose sample is mostly
        # still open is survivorship-biased upward and cannot be scored honestly.
        blockers.append(f"UNRESOLVED_{len(open_trades)}_OF_{total}")

    return SliceEdge(
        dimension=dimension,
        key=key,
        closed_trades=len(closed),
        open_trades=len(open_trades),
        distinct_days=distinct_days,
        gross_pnl_usd=sum(pnls, ZERO),
        gross_notional_usd=notional,
        mean_gross_return_bps=mean_return,
        median_gross_return_bps=_median(returns),
        gross_return_ci_low_bps=None if ci is None else ci[0],
        gross_return_ci_high_bps=None if ci is None else ci[1],
        breakeven_cost_bps=mean_return,
        win_rate=(
            D(sum(1 for value in pnls if value > ZERO)) / D(len(pnls)) if pnls else None
        ),
        worst_trade_bps=min(returns) if returns else None,
        profit_concentration=concentration,
        max_drawdown_usd=_max_drawdown_usd(pnls),
        median_signal_age_ms=_median(
            [D(trade.signal_age_ms) for trade in closed if trade.signal_age_ms is not None]
        ),
        median_entry_chase_bps=_median(
            [trade.entry_chase_bps for trade in closed if trade.entry_chase_bps is not None]
        ),
        median_exit_vs_source_bps=_median(
            [
                value
                for value in (trade.exit_vs_source_bps for trade in closed)
                if value is not None
            ]
        ),
        net_by_cost_bps=net_by_cost,
        verdict="ELIGIBLE_FOR_MICRO_LIVE" if not blockers else "NOT_READY",
        blockers=tuple(blockers),
    )


def score_dimensions(
    ledger: Ledger,
    *,
    policy: EdgePolicy = DEFAULT_POLICY,
    cost_scenarios_bps: Sequence[Decimal] = DEFAULT_COST_SCENARIOS_BPS,
    dimensions: Sequence[tuple[str, Callable[[ShadowTrade], str]]] = DEFAULT_DIMENSIONS,
) -> tuple[SliceEdge, ...]:
    scored: list[SliceEdge] = []
    for dimension, keyfn in dimensions:
        grouped: dict[str, list[ShadowTrade]] = {}
        for trade in ledger.trades:
            grouped.setdefault(keyfn(trade) or "unknown", []).append(trade)
        for key in sorted(grouped):
            scored.append(
                score_slice(
                    dimension,
                    key,
                    grouped[key],
                    policy=policy,
                    cost_scenarios_bps=cost_scenarios_bps,
                )
            )
    return tuple(scored)


@dataclass(frozen=True, slots=True)
class StaleSignalReport:
    """What the freshness gate refused, and what admitting it would be worth."""

    skipped: int
    admitted: int
    median_age_ms: Decimal | None
    p90_age_ms: Decimal | None
    max_age_ms: int | None
    by_trader: dict[str, int] = field(default_factory=dict)

    @property
    def rejection_rate(self) -> Decimal | None:
        total = self.skipped + self.admitted
        return D(self.skipped) / D(total) if total else None

    def to_dict(self) -> dict[str, object]:
        return {
            "skipped": self.skipped,
            "admitted": self.admitted,
            "rejection_rate": (
                None if self.rejection_rate is None else str(self.rejection_rate)
            ),
            "median_age_ms": None if self.median_age_ms is None else str(self.median_age_ms),
            "p90_age_ms": None if self.p90_age_ms is None else str(self.p90_age_ms),
            "max_age_ms": self.max_age_ms,
            "by_trader": dict(sorted(self.by_trader.items())),
        }


def stale_signal_report(ledger: Ledger) -> StaleSignalReport:
    ages = sorted(skip.age_ms for skip in ledger.stale_skips)
    by_trader: dict[str, int] = {}
    for skip in ledger.stale_skips:
        by_trader[skip.username or "unknown"] = by_trader.get(skip.username or "unknown", 0) + 1
    p90 = None
    if ages:
        p90 = D(ages[min(len(ages) - 1, int(0.9 * len(ages)))])
    return StaleSignalReport(
        skipped=len(ages),
        admitted=len(ledger.trades),
        median_age_ms=_median([D(age) for age in ages]),
        p90_age_ms=p90,
        max_age_ms=ages[-1] if ages else None,
        by_trader=by_trader,
    )


def build_report(
    ledger: Ledger,
    *,
    policy: EdgePolicy = DEFAULT_POLICY,
    cost_scenarios_bps: Sequence[Decimal] = DEFAULT_COST_SCENARIOS_BPS,
) -> dict[str, object]:
    slices = score_dimensions(
        ledger, policy=policy, cost_scenarios_bps=cost_scenarios_bps
    )
    eligible = [item for item in slices if item.verdict == "ELIGIBLE_FOR_MICRO_LIVE"]
    return {
        "model_version": "invo-notification-edge-v1",
        "real_trading": False,
        "policy": {
            "min_closed_trades": policy.min_closed_trades,
            "min_distinct_days": policy.min_distinct_days,
            "reference_cost_bps": str(policy.reference_cost_bps),
            "max_profit_concentration": str(policy.max_profit_concentration),
            "max_open_trade_share": str(policy.max_open_trade_share),
            "confidence": str(policy.confidence),
        },
        "cost_scenarios_bps": [str(cost) for cost in cost_scenarios_bps],
        "integrity": ledger.integrity.to_dict(),
        "stale_signals": stale_signal_report(ledger).to_dict(),
        "eligible_slice_count": len(eligible),
        "slices": [item.to_dict() for item in slices],
    }
