from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from hlcopy.analytics.performance import WalletMetrics
from hlcopy.copyability.proxy import copyability_proxy
from hlcopy.discovery.leaderboard import LeaderboardCandidate


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


@dataclass(frozen=True, slots=True)
class RankedWallet:
    address: str
    display_name: str | None
    account_value: float
    style: str
    performance_score: float
    risk_score: float
    persistence_score: float
    copyability_score: float
    confidence_score: float
    composite_score: float
    warning_flags: str
    month_roi: float
    all_time_roi: float
    metrics: WalletMetrics

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        row.pop("metrics")
        row.update(self.metrics.to_dict())
        return row


def classify_style(metrics: WalletMetrics) -> str:
    hold = metrics.median_hold_seconds
    if metrics.maker_share > 0.80 and metrics.trades_per_day > 40:
        return "MARKET_MAKER_LIKE"
    if hold < 180:
        return "SCALPER"
    if hold < 6 * 3600:
        return "INTRADAY"
    if hold < 3 * 86400:
        return "SWING"
    return "POSITION"


def _performance_score(metrics: WalletMetrics) -> float:
    pf = metrics.profit_factor
    pf_score = 100.0 if math.isinf(pf) else _clamp((pf - 0.8) / 1.7 * 100)
    expectancy_score = 100.0 if metrics.expectancy > 0 else 0.0
    sharpe_score = _clamp((metrics.episode_sharpe + 1.0) / 4.0 * 100)
    return _clamp(0.45 * pf_score + 0.25 * expectancy_score + 0.30 * sharpe_score)


def _risk_score(metrics: WalletMetrics) -> float:
    profit = max(metrics.gross_profit, 1.0)
    dd_ratio = abs(metrics.max_drawdown) / profit
    dd_score = 100 * math.exp(-2.5 * dd_ratio)
    concentration_penalty = max(0.0, metrics.asset_concentration - 0.70) / 0.30 * 30.0
    return _clamp(dd_score - concentration_penalty)


def _persistence_score(candidate: LeaderboardCandidate) -> float:
    windows = [candidate.window(w) for w in ("day", "week", "month", "allTime")]
    positive = sum(w.pnl > 0 and w.roi > 0 for w in windows)
    recent_vs_long = 1.0 if candidate.window("month").roi > 0 else 0.0
    return 100.0 * (0.75 * positive / 4 + 0.25 * recent_vs_long)


def _confidence_score(metrics: WalletMetrics) -> float:
    return _clamp(100.0 * (1.0 - math.exp(-metrics.trade_count / 60.0)))


def rank_wallet(candidate: LeaderboardCandidate, metrics: WalletMetrics) -> RankedWallet:
    perf = _performance_score(metrics)
    risk = _risk_score(metrics)
    persistence = _persistence_score(candidate)
    copy = copyability_proxy(metrics)
    confidence = _confidence_score(metrics)
    flags: list[str] = []
    if metrics.trade_count < 20:
        flags.append("LOW_SAMPLE")
    if metrics.fast_trade_fraction > 0.60:
        flags.append("FAST_ALPHA")
    if metrics.maker_share > 0.80:
        flags.append("MAKER_HEAVY")
    if metrics.asset_concentration > 0.90:
        flags.append("CONCENTRATED")
    if metrics.net_pnl_before_funding <= 0:
        flags.append("RECENT_RECONSTRUCTED_LOSS")

    penalty = 0.0
    penalty += 12.0 if "LOW_SAMPLE" in flags else 0.0
    penalty += 20.0 if "FAST_ALPHA" in flags else 0.0
    penalty += 20.0 if "MAKER_HEAVY" in flags else 0.0
    penalty += 10.0 if "RECENT_RECONSTRUCTED_LOSS" in flags else 0.0
    composite = (
        0.28 * perf
        + 0.18 * risk
        + 0.20 * persistence
        + 0.22 * copy
        + 0.12 * confidence
        - penalty
    )
    return RankedWallet(
        address=candidate.address,
        display_name=candidate.display_name,
        account_value=candidate.account_value,
        style=classify_style(metrics),
        performance_score=round(perf, 2),
        risk_score=round(risk, 2),
        persistence_score=round(persistence, 2),
        copyability_score=round(copy, 2),
        confidence_score=round(confidence, 2),
        composite_score=round(_clamp(composite), 2),
        warning_flags=",".join(flags),
        month_roi=candidate.window("month").roi,
        all_time_roi=candidate.window("allTime").roi,
        metrics=metrics,
    )
