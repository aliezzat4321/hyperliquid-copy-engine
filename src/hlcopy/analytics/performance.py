from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass

from hlcopy.models import Fill
from hlcopy.positions.state_machine import PositionEpisode


@dataclass(frozen=True, slots=True)
class WalletMetrics:
    trade_count: int
    net_pnl_before_funding: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    win_rate: float
    expectancy: float
    max_drawdown: float
    episode_sharpe: float
    episode_sortino: float
    median_hold_seconds: float
    mean_hold_seconds: float
    trades_per_day: float
    fills_per_trade: float
    maker_share: float
    taker_share: float
    asset_concentration: float
    fast_trade_fraction: float

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return max_dd


def calculate_wallet_metrics(episodes: list[PositionEpisode], fills: list[Fill]) -> WalletMetrics:
    complete = [
        ep
        for ep in episodes
        if ep.complete_start and ep.closed_at_ms is not None and ep.holding_seconds is not None
    ]
    pnls = [float(ep.net_pnl_before_funding) for ep in complete]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    profit_factor = gross_profit / gross_loss if gross_loss else (float("inf") if winners else 0.0)
    expectancy = statistics.fmean(pnls) if pnls else 0.0
    win_rate = len(winners) / len(pnls) if pnls else 0.0

    returns = [
        float(ep.net_pnl_before_funding / ep.entry_notional)
        for ep in complete
        if ep.entry_notional > 0
    ]
    if len(returns) >= 2 and statistics.pstdev(returns) > 0:
        episode_sharpe = (
            statistics.fmean(returns) / statistics.pstdev(returns) * math.sqrt(len(returns))
        )
    else:
        episode_sharpe = 0.0
    downside = [min(0.0, value) for value in returns]
    downside_dev = (
        math.sqrt(statistics.fmean([value * value for value in downside]))
        if downside
        else 0.0
    )
    episode_sortino = (
        statistics.fmean(returns) / downside_dev * math.sqrt(len(returns))
        if returns and downside_dev > 0
        else 0.0
    )

    holds = [float(ep.holding_seconds or 0.0) for ep in complete]
    median_hold = statistics.median(holds) if holds else 0.0
    mean_hold = statistics.fmean(holds) if holds else 0.0
    fast_fraction = sum(h < 60.0 for h in holds) / len(holds) if holds else 0.0

    if fills:
        times = [f.timestamp_ms for f in fills]
        span_days = max((max(times) - min(times)) / 86_400_000, 1 / 24)
        trades_per_day = len(complete) / span_days
        taker_notional = sum(float(f.notional) for f in fills if f.crossed is True)
        maker_notional = sum(float(f.notional) for f in fills if f.crossed is False)
        known = taker_notional + maker_notional
        taker_share = taker_notional / known if known else 0.0
        maker_share = maker_notional / known if known else 0.0
        notionals: dict[str, float] = defaultdict(float)
        for fill in fills:
            notionals[fill.coin] += float(fill.notional)
        total_notional = sum(notionals.values())
        concentration = max(notionals.values(), default=0.0) / total_notional if total_notional else 0.0
    else:
        trades_per_day = maker_share = taker_share = concentration = 0.0

    return WalletMetrics(
        trade_count=len(complete),
        net_pnl_before_funding=sum(pnls),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        win_rate=win_rate,
        expectancy=expectancy,
        max_drawdown=_max_drawdown(pnls),
        episode_sharpe=episode_sharpe,
        episode_sortino=episode_sortino,
        median_hold_seconds=median_hold,
        mean_hold_seconds=mean_hold,
        trades_per_day=trades_per_day,
        fills_per_trade=(
            sum(ep.fill_count for ep in complete) / len(complete) if complete else 0.0
        ),
        maker_share=maker_share,
        taker_share=taker_share,
        asset_concentration=concentration,
        fast_trade_fraction=fast_fraction,
    )
