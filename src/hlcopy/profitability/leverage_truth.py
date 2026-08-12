from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

D = Decimal
ZERO = D("0")
BPS = D("10000")

DEFAULT_LEVERAGE_GRID = tuple(
    D(x) for x in ("1", "2", "3", "5", "7.5", "10", "20", "40")
)


@dataclass(frozen=True, slots=True)
class LeverageScenario:
    leverage: Decimal
    notional_usd: Decimal
    equity_required_usd: Decimal
    net_pnl_usd: Decimal
    net_notional_return_bps: Decimal
    net_equity_return_bps: Decimal
    research_only: bool
    liquidation_path_mode: str

    def to_dict(self) -> dict[str, object]:
        return {
            "follower_leverage": str(self.leverage),
            "notional_usd": str(self.notional_usd),
            "equity_required_usd": str(self.equity_required_usd),
            "net_pnl_usd": str(self.net_pnl_usd),
            "net_notional_return_bps": str(self.net_notional_return_bps),
            "net_equity_return_bps": str(self.net_equity_return_bps),
            "net_equity_return_pct": str(self.net_equity_return_bps / D("100")),
            "research_only": self.research_only,
            "liquidation_path_mode": self.liquidation_path_mode,
        }


def leverage_matrix(
    summary: dict[str, object],
    leverages: Iterable[Decimal] = DEFAULT_LEVERAGE_GRID,
) -> list[dict[str, object]]:
    """Translate execution PnL into return-on-equity without inventing new PnL.

    The base copy simulator already fixes follower notional and execution prices. Leverage
    changes the margin/equity required to support that same notional; it does not multiply
    the underlying dollar PnL. Every row remains research-only because the current base
    scorer still lacks funding, open-position MTM, and path-dependent liquidation truth.
    """

    notional = D(str(summary.get("notional_usd", "0")))
    net_pnl = D(str(summary.get("closed_net_pnl_usd", "0")))
    if notional <= ZERO:
        return []
    notional_bps = net_pnl / notional * BPS
    rows: list[dict[str, object]] = []
    for raw in leverages:
        leverage = D(str(raw))
        if leverage <= ZERO:
            continue
        equity = notional / leverage
        equity_bps = net_pnl / equity * BPS
        row = dict(summary)
        row.update(
            LeverageScenario(
                leverage=leverage,
                notional_usd=notional,
                equity_required_usd=equity,
                net_pnl_usd=net_pnl,
                net_notional_return_bps=notional_bps,
                net_equity_return_bps=equity_bps,
                research_only=True,
                liquidation_path_mode=(
                    "UNMODELED_BASE_TRUTH_BLOCKS_LIVE_APPROVAL"
                    if leverage == D("1")
                    else "NOT_MODELED_BLOCKS_LIVE_APPROVAL"
                ),
            ).to_dict()
        )
        rows.append(row)
    return rows
