from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from hlcopy.profitability.path_risk import EquityCheckpoint, evaluate_cross_margin_path

D = Decimal
ZERO = D("0")


@dataclass(frozen=True, slots=True)
class SafeLeverageRow:
    leverage: Decimal
    starting_equity_usd: Decimal
    peak_gross_notional_usd: Decimal
    min_free_collateral_usd: Decimal
    min_liquidation_buffer_usd: Decimal
    max_drawdown_pct: Decimal
    liquidation_survived: bool
    initial_margin_survived: bool
    safe: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "leverage": str(self.leverage),
            "starting_equity_usd": str(self.starting_equity_usd),
            "peak_gross_notional_usd": str(self.peak_gross_notional_usd),
            "min_free_collateral_usd": str(self.min_free_collateral_usd),
            "min_liquidation_buffer_usd": str(self.min_liquidation_buffer_usd),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "liquidation_survived": self.liquidation_survived,
            "initial_margin_survived": self.initial_margin_survived,
            "safe": self.safe,
        }


@dataclass(frozen=True, slots=True)
class SafeLeverageSummary:
    rows: tuple[SafeLeverageRow, ...]
    max_safe_leverage: Decimal | None

    def to_dict(self) -> dict[str, object]:
        return {
            "max_safe_leverage": (
                str(self.max_safe_leverage) if self.max_safe_leverage is not None else None
            ),
            "rows": [row.to_dict() for row in self.rows],
        }


def evaluate_safe_leverage(
    checkpoints: Iterable[EquityCheckpoint],
    leverages: Iterable[Decimal],
    *,
    minimum_liquidation_buffer_usd: Decimal = ZERO,
) -> SafeLeverageSummary:
    """Evaluate capital implied by each leverage against the exact same path.

    For a fixed copied-notional path, configured leverage determines how much starting
    equity is allocated: ``peak path gross / leverage``. A leverage is called safe only
    if the full supplied path (including MTM, funding and maintenance deductions)
    avoids liquidation, retains non-negative free collateral, and keeps liquidation
    buffer strictly above the configured minimum.

    Completeness is intentionally the caller's responsibility. This function must only
    be used after continuous mark, funding and margin-table truth have been established.
    """
    ordered = tuple(sorted(checkpoints, key=lambda item: item.exchange_ts_ms))
    if not ordered:
        raise ValueError("at least one equity checkpoint is required")
    if minimum_liquidation_buffer_usd < ZERO:
        raise ValueError("minimum_liquidation_buffer_usd cannot be negative")

    peak_gross = max(
        sum((position.gross_notional_usd for position in point.positions), ZERO)
        for point in ordered
    )
    if peak_gross <= ZERO:
        raise ValueError("path must contain positive gross exposure")

    rows: list[SafeLeverageRow] = []
    for raw in sorted({D(str(value)) for value in leverages}):
        if raw <= ZERO:
            continue
        starting_equity = peak_gross / raw
        path = evaluate_cross_margin_path(
            ordered,
            starting_equity_usd=starting_equity,
            leverage=raw,
        )
        liquidation_survived = not path.liquidated
        initial_margin_survived = path.min_free_collateral_usd >= ZERO
        safe = (
            liquidation_survived
            and initial_margin_survived
            and path.min_liquidation_buffer_usd > minimum_liquidation_buffer_usd
        )
        rows.append(
            SafeLeverageRow(
                leverage=raw,
                starting_equity_usd=starting_equity,
                peak_gross_notional_usd=peak_gross,
                min_free_collateral_usd=path.min_free_collateral_usd,
                min_liquidation_buffer_usd=path.min_liquidation_buffer_usd,
                max_drawdown_pct=path.max_drawdown_pct,
                liquidation_survived=liquidation_survived,
                initial_margin_survived=initial_margin_survived,
                safe=safe,
            )
        )

    safe_values = [row.leverage for row in rows if row.safe]
    return SafeLeverageSummary(
        rows=tuple(rows),
        max_safe_leverage=max(safe_values) if safe_values else None,
    )
