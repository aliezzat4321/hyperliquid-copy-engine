from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Iterable

D = Decimal
ZERO = D("0")
ONE_HUNDRED = D("100")


@dataclass(frozen=True, slots=True)
class OpenPositionMark:
    coin: str
    qty: Decimal
    avg_entry: Decimal
    mark_price: Decimal
    maintenance_margin_rate: Decimal

    @property
    def unrealized_pnl_usd(self) -> Decimal:
        return (self.mark_price - self.avg_entry) * self.qty

    @property
    def gross_notional_usd(self) -> Decimal:
        return abs(self.qty * self.mark_price)

    @property
    def maintenance_margin_usd(self) -> Decimal:
        return self.gross_notional_usd * self.maintenance_margin_rate


@dataclass(frozen=True, slots=True)
class EquityCheckpoint:
    exchange_ts_ms: int
    realized_net_pnl_usd: Decimal
    funding_pnl_usd: Decimal
    positions: tuple[OpenPositionMark, ...]


@dataclass(frozen=True, slots=True)
class PathPoint:
    exchange_ts_ms: int
    equity_usd: Decimal
    unrealized_pnl_usd: Decimal
    realized_net_pnl_usd: Decimal
    funding_pnl_usd: Decimal
    gross_notional_usd: Decimal
    initial_margin_usd: Decimal
    maintenance_margin_usd: Decimal
    free_collateral_usd: Decimal
    liquidation_buffer_usd: Decimal
    drawdown_usd: Decimal
    drawdown_pct: Decimal
    liquidated: bool

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in row.items()}


@dataclass(frozen=True, slots=True)
class PathRiskSummary:
    starting_equity_usd: Decimal
    leverage: Decimal
    checkpoints: tuple[PathPoint, ...]
    min_equity_usd: Decimal
    max_equity_usd: Decimal
    max_drawdown_usd: Decimal
    max_drawdown_pct: Decimal
    min_liquidation_buffer_usd: Decimal
    liquidated: bool
    first_liquidation_ts_ms: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "starting_equity_usd": str(self.starting_equity_usd),
            "leverage": str(self.leverage),
            "min_equity_usd": str(self.min_equity_usd),
            "max_equity_usd": str(self.max_equity_usd),
            "max_drawdown_usd": str(self.max_drawdown_usd),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "min_liquidation_buffer_usd": str(self.min_liquidation_buffer_usd),
            "liquidated": self.liquidated,
            "first_liquidation_ts_ms": self.first_liquidation_ts_ms,
            "checkpoints": [point.to_dict() for point in self.checkpoints],
        }


def evaluate_cross_margin_path(
    checkpoints: Iterable[EquityCheckpoint],
    *,
    starting_equity_usd: Decimal,
    leverage: Decimal,
) -> PathRiskSummary:
    """Evaluate a causal cross-margin MTM path at supplied historical checkpoints.

    This primitive deliberately does not invent marks, funding, or maintenance rates. The
    caller must supply values available for each historical checkpoint. It therefore fails
    closed: path-risk truth is only as complete as the supplied checkpoint series.
    """

    if starting_equity_usd <= ZERO:
        raise ValueError("starting_equity_usd must be positive")
    if leverage <= ZERO:
        raise ValueError("leverage must be positive")

    ordered = sorted(checkpoints, key=lambda item: item.exchange_ts_ms)
    if not ordered:
        raise ValueError("at least one equity checkpoint is required")

    points: list[PathPoint] = []
    peak_equity = starting_equity_usd
    min_equity = starting_equity_usd
    max_equity = starting_equity_usd
    max_drawdown = ZERO
    max_drawdown_pct = ZERO
    min_liquidation_buffer: Decimal | None = None
    first_liquidation: int | None = None

    for checkpoint in ordered:
        unrealized = sum(
            (position.unrealized_pnl_usd for position in checkpoint.positions),
            ZERO,
        )
        gross = sum(
            (position.gross_notional_usd for position in checkpoint.positions),
            ZERO,
        )
        maintenance = sum(
            (position.maintenance_margin_usd for position in checkpoint.positions),
            ZERO,
        )
        initial_margin = gross / leverage
        equity = (
            starting_equity_usd
            + checkpoint.realized_net_pnl_usd
            + checkpoint.funding_pnl_usd
            + unrealized
        )
        peak_equity = max(peak_equity, equity)
        min_equity = min(min_equity, equity)
        max_equity = max(max_equity, equity)
        drawdown = max(ZERO, peak_equity - equity)
        drawdown_pct = drawdown / peak_equity * ONE_HUNDRED if peak_equity > ZERO else ZERO
        max_drawdown = max(max_drawdown, drawdown)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
        free_collateral = equity - initial_margin
        liquidation_buffer = equity - maintenance
        min_liquidation_buffer = (
            liquidation_buffer
            if min_liquidation_buffer is None
            else min(min_liquidation_buffer, liquidation_buffer)
        )
        liquidated = liquidation_buffer <= ZERO
        if liquidated and first_liquidation is None:
            first_liquidation = checkpoint.exchange_ts_ms

        points.append(
            PathPoint(
                exchange_ts_ms=checkpoint.exchange_ts_ms,
                equity_usd=equity,
                unrealized_pnl_usd=unrealized,
                realized_net_pnl_usd=checkpoint.realized_net_pnl_usd,
                funding_pnl_usd=checkpoint.funding_pnl_usd,
                gross_notional_usd=gross,
                initial_margin_usd=initial_margin,
                maintenance_margin_usd=maintenance,
                free_collateral_usd=free_collateral,
                liquidation_buffer_usd=liquidation_buffer,
                drawdown_usd=drawdown,
                drawdown_pct=drawdown_pct,
                liquidated=liquidated,
            )
        )

    return PathRiskSummary(
        starting_equity_usd=starting_equity_usd,
        leverage=leverage,
        checkpoints=tuple(points),
        min_equity_usd=min_equity,
        max_equity_usd=max_equity,
        max_drawdown_usd=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        min_liquidation_buffer_usd=min_liquidation_buffer or ZERO,
        liquidated=first_liquidation is not None,
        first_liquidation_ts_ms=first_liquidation,
    )
