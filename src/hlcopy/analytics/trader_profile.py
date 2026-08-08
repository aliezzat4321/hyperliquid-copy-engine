from __future__ import annotations

import bisect
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from hlcopy.models import Fill
from hlcopy.positions.state_machine import PositionEpisode

PROFILE_MODEL_VERSION = "trader_forensics_v1"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return default


def _ratio(a: float, b: float) -> float:
    return a / b if b else 0.0


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    return ordered[lo] * (1 - weight) + ordered[hi] * weight


def _drawdown(pnls: list[float]) -> float:
    equity = peak = worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def _streak(pnls: list[float], positive: bool) -> int:
    best = current = 0
    for pnl in pnls:
        matches = pnl > 0 if positive else pnl < 0
        current = current + 1 if matches else 0
        best = max(best, current)
    return best


def _performance(
    episodes: list[PositionEpisode],
) -> tuple[dict[str, object], list[PositionEpisode]]:
    complete = [
        ep
        for ep in episodes
        if ep.complete_start and ep.closed_at_ms is not None
    ]
    complete.sort(key=lambda ep: (ep.closed_at_ms or 0, ep.coin))
    pnls = [float(ep.net_pnl_before_funding) for ep in complete]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    returns = [
        float(ep.net_pnl_before_funding / ep.entry_notional)
        for ep in complete
        if ep.entry_notional > 0
    ]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    mean_return = statistics.fmean(returns) if returns else 0.0
    std = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
    downside = (
        math.sqrt(statistics.fmean([min(0.0, value) ** 2 for value in returns]))
        if returns
        else 0.0
    )
    recent = pnls[-20:]
    avg_win = statistics.fmean(wins) if wins else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0
    return {
        "trade_count": len(complete),
        "winner_count": len(wins),
        "loser_count": len(losses),
        "breakeven_count": sum(pnl == 0 for pnl in pnls),
        "win_rate": _ratio(len(wins), len(complete)),
        "loss_rate": _ratio(len(losses), len(complete)),
        "net_pnl_before_funding": sum(pnls),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "expectancy_usd": statistics.fmean(pnls) if pnls else 0.0,
        "median_trade_pnl_usd": statistics.median(pnls) if pnls else 0.0,
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "payoff_ratio": _ratio(avg_win, abs(avg_loss)),
        "largest_win_usd": max(wins, default=0.0),
        "largest_loss_usd": min(losses, default=0.0),
        "max_drawdown_usd": _drawdown(pnls),
        "episode_sharpe": mean_return / std * math.sqrt(len(returns)) if std else 0.0,
        "episode_sortino": (
            mean_return / downside * math.sqrt(len(returns)) if downside else 0.0
        ),
        "max_consecutive_wins": _streak(pnls, True),
        "max_consecutive_losses": _streak(pnls, False),
        "median_episode_return": statistics.median(returns) if returns else 0.0,
        "p10_episode_return": _pct(returns, 0.10),
        "p90_episode_return": _pct(returns, 0.90),
        "worst_episode_return": min(returns, default=0.0),
        "best_episode_return": max(returns, default=0.0),
        "top_1_profit_share": _ratio(sum(sorted(wins, reverse=True)[:1]), gross_profit),
        "top_5_profit_share": _ratio(sum(sorted(wins, reverse=True)[:5]), gross_profit),
        "worst_3_loss_share": _ratio(abs(sum(sorted(losses)[:3])), gross_loss),
        "recent_20_win_rate": _ratio(sum(pnl > 0 for pnl in recent), len(recent)),
        "recent_20_expectancy_usd": statistics.fmean(recent) if recent else 0.0,
        "recent_20_net_pnl_usd": sum(recent),
        "return_basis": "NET_PNL_OVER_ENTRY_NOTIONAL",
    }, complete


def _style(episodes: list[PositionEpisode]) -> tuple[str, dict[str, object]]:
    holds = [float(ep.holding_seconds or 0.0) for ep in episodes]
    median_hold = statistics.median(holds) if holds else 0.0
    p10, p90 = _pct(holds, 0.10), _pct(holds, 0.90)
    if len(holds) >= 10 and p10 < 300 and p90 > 86_400:
        label = "HYBRID"
    elif median_hold < 300:
        label = "SCALPER"
    elif median_hold < 8 * 3600:
        label = "INTRADAY"
    elif median_hold < 3 * 86_400:
        label = "SWING"
    else:
        label = "POSITION"
    starts = [ep.opened_at_ms for ep in episodes if ep.opened_at_ms is not None]
    ends = [ep.closed_at_ms for ep in episodes if ep.closed_at_ms is not None]
    span_days = (
        max((max(ends) - min(starts)) / 86_400_000, 1 / 24)
        if starts and ends
        else 0.0
    )
    events: list[tuple[int, int]] = []
    for ep in episodes:
        if ep.opened_at_ms is not None:
            events.append((ep.opened_at_ms, 1))
        if ep.closed_at_ms is not None:
            events.append((ep.closed_at_ms, -1))
    current = concurrent_peak = 0
    for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
        current += delta
        concurrent_peak = max(concurrent_peak, current)
    longs = [ep for ep in episodes if ep.direction == "LONG"]
    shorts = [ep for ep in episodes if ep.direction == "SHORT"]
    return label, {
        "median_hold_seconds": median_hold,
        "mean_hold_seconds": statistics.fmean(holds) if holds else 0.0,
        "p10_hold_seconds": p10,
        "p90_hold_seconds": p90,
        "sub_1m_trade_fraction": _ratio(sum(h < 60 for h in holds), len(holds)),
        "sub_5m_trade_fraction": _ratio(sum(h < 300 for h in holds), len(holds)),
        "intraday_trade_fraction": _ratio(
            sum(h < 8 * 3600 for h in holds), len(holds)
        ),
        "long_trade_fraction": _ratio(len(longs), len(episodes)),
        "short_trade_fraction": _ratio(len(shorts), len(episodes)),
        "long_pnl_before_funding": sum(float(ep.net_pnl_before_funding) for ep in longs),
        "short_pnl_before_funding": sum(
            float(ep.net_pnl_before_funding) for ep in shorts
        ),
        "observed_span_days": span_days,
        "trades_per_day": _ratio(len(episodes), span_days),
        "fills_per_trade": _ratio(sum(ep.fill_count for ep in episodes), len(episodes)),
        "max_concurrent_positions": concurrent_peak,
    }


def _fill_action(fill: Fill) -> str:
    before, delta = fill.start_position, fill.signed_size
    after = before + delta
    if before == 0:
        return "OPEN"
    if before * delta > 0:
        return "SCALE_IN"
    if after == 0:
        return "CLOSE"
    return "REVERSAL" if before * after < 0 else "REDUCE"


def _behavior(fills: list[Fill], episodes: list[PositionEpisode]) -> dict[str, object]:
    counts = Counter(_fill_action(fill) for fill in fills)
    notionals: dict[str, float] = defaultdict(float)
    for fill in fills:
        notionals[_fill_action(fill)] += float(fill.notional)
    total = sum(notionals.values())
    states: dict[str, tuple[Decimal, Decimal | None]] = {}
    scale_ins = adverse = 0
    for fill in sorted(fills, key=lambda item: (item.timestamp_ms, item.tid)):
        qty, avg = states.get(fill.coin, (Decimal("0"), None))
        if qty != fill.start_position:
            qty, avg = fill.start_position, None
        delta, after = fill.signed_size, qty + fill.signed_size
        if qty == 0:
            avg = fill.price if after else None
        elif qty * delta > 0:
            scale_ins += 1
            if avg is not None:
                adverse += int(
                    (qty > 0 and fill.price < avg) or (qty < 0 and fill.price > avg)
                )
                avg = (avg * abs(qty) + fill.price * abs(delta)) / (
                    abs(qty) + abs(delta)
                )
        elif after == 0:
            avg = None
        elif qty * after < 0:
            avg = fill.price
        states[fill.coin] = (after, avg)
    ordered = sorted(episodes, key=lambda ep: (ep.opened_at_ms or 0, ep.coin))
    sizes = [float(ep.entry_notional) for ep in ordered if ep.entry_notional > 0]
    median_size = statistics.median(sizes) if sizes else 0.0
    post_loss = [
        float(nxt.entry_notional)
        for prior, nxt in zip(ordered, ordered[1:], strict=False)
        if prior.net_pnl_before_funding < 0 and nxt.entry_notional > 0
    ]
    return {
        "open_fill_count": counts["OPEN"],
        "scale_in_fill_count": counts["SCALE_IN"],
        "reduce_fill_count": counts["REDUCE"],
        "close_fill_count": counts["CLOSE"],
        "reversal_fill_count": counts["REVERSAL"],
        "scale_in_notional_share": _ratio(notionals["SCALE_IN"], total),
        "reduction_notional_share": _ratio(
            notionals["REDUCE"] + notionals["CLOSE"], total
        ),
        "reversal_notional_share": _ratio(notionals["REVERSAL"], total),
        "adverse_scale_in_fraction": _ratio(adverse, scale_ins),
        "post_loss_size_ratio_vs_median": (
            statistics.median(post_loss) / median_size
            if post_loss and median_size
            else 0.0
        ),
    }


def _execution(
    fills: list[Fill],
    orders: list[dict[str, Any]],
    twaps: list[dict[str, Any]],
) -> tuple[str, dict[str, object]]:
    maker = sum(float(fill.notional) for fill in fills if fill.crossed is False)
    taker = sum(float(fill.notional) for fill in fills if fill.crossed is True)
    known = maker + taker
    total = sum(float(fill.notional) for fill in fills)
    twap_tids = {
        int(row["fill"]["tid"])
        for row in twaps
        if isinstance(row, dict)
        and isinstance(row.get("fill"), dict)
        and row["fill"].get("tid") is not None
    }
    twap_ntl = sum(float(fill.notional) for fill in fills if fill.tid in twap_tids)
    statuses, order_types, tifs = Counter(), Counter(), Counter()
    trigger = reduce_only = tpsl = 0
    for row in orders:
        if not isinstance(row, dict):
            continue
        statuses[str(row.get("status", "unknown"))] += 1
        order = row.get("order") or {}
        if not isinstance(order, dict):
            continue
        order_types[str(order.get("orderType", "unknown"))] += 1
        tifs[str(order.get("tif", "unknown"))] += 1
        trigger += int(bool(order.get("isTrigger")))
        reduce_only += int(bool(order.get("reduceOnly")))
        tpsl += int(bool(order.get("isPositionTpsl")))
    n_orders = sum(statuses.values())
    maker_share, taker_share = _ratio(maker, known), _ratio(taker, known)
    twap_share = _ratio(twap_ntl, total)
    if twap_share >= 0.30:
        label = "TWAP_HEAVY"
    elif maker_share >= 0.70:
        label = "MAKER_HEAVY"
    elif taker_share >= 0.70:
        label = "TAKER_HEAVY"
    else:
        label = "MIXED"
    notionals = [float(fill.notional) for fill in fills]
    return label, {
        "maker_notional_share": maker_share,
        "taker_notional_share": taker_share,
        "unknown_liquidity_notional_share": _ratio(total - known, total),
        "twap_notional_share": twap_share,
        "twap_fill_count": len(twap_tids),
        "unique_twap_count": len(
            {row.get("twapId") for row in twaps if isinstance(row, dict)} - {None}
        ),
        "historical_order_count": n_orders,
        "order_filled_rate": _ratio(statuses["filled"], n_orders),
        "order_canceled_rate": _ratio(statuses["canceled"], n_orders),
        "order_rejected_rate": _ratio(statuses["rejected"], n_orders),
        "order_margin_canceled_rate": _ratio(statuses["marginCanceled"], n_orders),
        "market_order_share": _ratio(
            sum(v for k, v in order_types.items() if "market" in k.lower()), n_orders
        ),
        "post_only_order_share": _ratio(
            sum(v for k, v in tifs.items() if k.lower() == "alo"), n_orders
        ),
        "ioc_order_share": _ratio(
            sum(v for k, v in tifs.items() if "ioc" in k.lower()), n_orders
        ),
        "trigger_order_share": _ratio(trigger, n_orders),
        "reduce_only_order_share": _ratio(reduce_only, n_orders),
        "position_tpsl_order_share": _ratio(tpsl, n_orders),
        "median_fill_notional_usd": statistics.median(notionals) if notionals else 0.0,
        "p95_fill_notional_usd": _pct(notionals, 0.95),
        "leader_fill_fees_usd": sum(float(fill.fee + fill.builder_fee) for fill in fills),
    }


def _funding(rows: list[dict[str, Any]], trading_pnl: float) -> dict[str, object]:
    values: list[float] = []
    by_coin: dict[str, float] = defaultdict(float)
    for row in rows:
        delta = row.get("delta") if isinstance(row, dict) else None
        if not isinstance(delta, dict) or delta.get("type") != "funding":
            continue
        value = _f(delta.get("usdc"))
        values.append(value)
        by_coin[str(delta.get("coin", "UNKNOWN"))] += value
    net = sum(values)
    return {
        "event_count": len(values),
        "net_usd": net,
        "paid_usd": abs(sum(value for value in values if value < 0)),
        "received_usd": sum(value for value in values if value > 0),
        "absolute_funding_to_trading_pnl": _ratio(abs(net), abs(trading_pnl)),
        "net_pnl_after_observed_funding_usd": trading_pnl + net,
        "largest_funding_cost_usd": min(values, default=0.0),
        "largest_funding_credit_usd": max(values, default=0.0),
        "by_coin_json": json.dumps(dict(sorted(by_coin.items())), sort_keys=True),
    }


def _portfolio_samples(payload: Any) -> list[tuple[int, float]]:
    samples: dict[int, float] = {}
    if not isinstance(payload, list):
        return []
    for item in payload:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[1], dict):
            continue
        for point in item[1].get("accountValueHistory", []) or []:
            if isinstance(point, list) and len(point) == 2 and _f(point[1]) > 0:
                samples[int(point[0])] = _f(point[1])
    return sorted(samples.items())


def _historical_exposure(
    episodes: list[PositionEpisode], portfolio: Any
) -> dict[str, object]:
    samples = _portfolio_samples(portfolio)
    timestamps = [timestamp for timestamp, _ in samples]
    ratios, ages = [], []
    eligible = 0
    for ep in episodes:
        if ep.opened_at_ms is None or ep.avg_entry is None or ep.max_abs_size <= 0:
            continue
        eligible += 1
        idx = bisect.bisect_right(timestamps, ep.opened_at_ms) - 1
        if idx < 0:
            continue
        timestamp, equity = samples[idx]
        age = (ep.opened_at_ms - timestamp) / 3_600_000
        if 0 <= age <= 36 and equity > 0:
            ratios.append(float(ep.max_abs_size * ep.avg_entry) / equity)
            ages.append(age)
    return {
        "historical_effective_exposure_sample_count": len(ratios),
        "historical_effective_exposure_coverage": _ratio(len(ratios), eligible),
        "historical_effective_exposure_median": statistics.median(ratios) if ratios else 0.0,
        "historical_effective_exposure_p90": _pct(ratios, 0.90),
        "historical_effective_exposure_max": max(ratios, default=0.0),
        "portfolio_sample_count": len(samples),
        "portfolio_sample_age_median_hours": statistics.median(ages) if ages else 0.0,
        "historical_configured_leverage_status": "PENDING_REPLICA_CMDS_RECONSTRUCTION",
        "historical_effective_exposure_evidence": "SAMPLED_PORTFOLIO_ESTIMATE",
    }


def _current_leverage(payload: Any) -> tuple[dict[str, object], list[dict[str, object]]]:
    if not isinstance(payload, dict):
        return {"current_configured_leverage_evidence": "UNAVAILABLE"}, []
    summary = payload.get("marginSummary") or {}
    account_value = _f(summary.get("accountValue")) if isinstance(summary, dict) else 0.0
    positions: list[dict[str, object]] = []
    leverages, liq_distances = [], []
    gross = weighted = cross = isolated = margin = 0.0
    for item in payload.get("assetPositions", []) or []:
        position = item.get("position") if isinstance(item, dict) else None
        if not isinstance(position, dict):
            continue
        size = _f(position.get("szi"))
        notional = abs(_f(position.get("positionValue")))
        if not notional and not size:
            continue
        leverage = position.get("leverage") or {}
        leverage = leverage if isinstance(leverage, dict) else {}
        leverage_type = str(leverage.get("type", "unknown")).lower()
        configured = _f(leverage.get("value"))
        gross += notional
        margin += _f(position.get("marginUsed"))
        if configured:
            leverages.append(configured)
            weighted += notional * configured
        cross += notional if leverage_type == "cross" else 0.0
        isolated += notional if leverage_type == "isolated" else 0.0
        mark = notional / abs(size) if size else 0.0
        liquidation = _f(position.get("liquidationPx"))
        distance = abs(mark - liquidation) / mark if mark and liquidation else None
        if distance is not None:
            liq_distances.append(distance)
        cum_funding = position.get("cumFunding") or {}
        positions.append(
            {
                "coin": str(position.get("coin", "")),
                "side": "LONG" if size > 0 else "SHORT" if size < 0 else "FLAT",
                "size": size,
                "entry_px": _f(position.get("entryPx")),
                "mark_px_estimate": mark,
                "position_value": notional,
                "configured_leverage": configured,
                "leverage_type": leverage_type,
                "margin_used": _f(position.get("marginUsed")),
                "liquidation_px": liquidation or None,
                "liquidation_distance_pct": distance,
                "max_leverage": _f(position.get("maxLeverage")),
                "unrealized_pnl": _f(position.get("unrealizedPnl")),
                "cum_funding_since_open": (
                    _f(cum_funding.get("sinceOpen"))
                    if isinstance(cum_funding, dict)
                    else 0.0
                ),
            }
        )
    total_margin = _f(summary.get("totalMarginUsed")) if isinstance(summary, dict) else margin
    return {
        "current_account_value": account_value,
        "current_gross_notional": gross,
        "current_total_margin_used": total_margin,
        "current_effective_leverage": _ratio(gross, account_value),
        "current_weighted_configured_leverage": _ratio(weighted, gross),
        "current_max_configured_leverage": max(leverages, default=0.0),
        "current_cross_notional_share": _ratio(cross, gross),
        "current_isolated_notional_share": _ratio(isolated, gross),
        "current_margin_utilization": _ratio(total_margin, account_value),
        "current_min_liquidation_distance_pct": (
            min(liq_distances) if liq_distances else None
        ),
        "current_position_count": len(positions),
        "current_configured_leverage_evidence": "EXACT_CURRENT_CLEARINGHOUSE_STATE",
    }, positions


def _concentration(
    fills: list[Fill], episodes: list[PositionEpisode]
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    notionals: dict[str, float] = defaultdict(float)
    for fill in fills:
        notionals[fill.coin] += float(fill.notional)
    total = sum(notionals.values())
    shares = {coin: _ratio(value, total) for coin, value in notionals.items()}
    top = max(shares, key=shares.get) if shares else None
    grouped: dict[str, list[PositionEpisode]] = defaultdict(list)
    for ep in episodes:
        grouped[ep.coin].append(ep)
    assets: dict[str, dict[str, object]] = {}
    for coin, coin_eps in sorted(grouped.items()):
        pnls = [float(ep.net_pnl_before_funding) for ep in coin_eps]
        assets[coin] = {
            "trade_count": len(coin_eps),
            "net_pnl_before_funding": sum(pnls),
            "win_rate": _ratio(sum(pnl > 0 for pnl in pnls), len(pnls)),
            "notional_share": shares.get(coin, 0.0),
            "median_hold_seconds": statistics.median(
                [float(ep.holding_seconds or 0) for ep in coin_eps]
            ),
            "long_trade_fraction": _ratio(
                sum(ep.direction == "LONG" for ep in coin_eps), len(coin_eps)
            ),
        }
    return {
        "asset_count": len(notionals),
        "top_asset": top,
        "top_asset_notional_share": shares.get(top, 0.0) if top else 0.0,
        "asset_notional_hhi": sum(share * share for share in shares.values()),
    }, assets


def _consistency(episodes: list[PositionEpisode]) -> dict[str, object]:
    buckets: dict[str, dict[str, float]] = {
        "day": defaultdict(float),
        "week": defaultdict(float),
        "month": defaultdict(float),
    }
    for ep in episodes:
        dt = datetime.fromtimestamp((ep.closed_at_ms or 0) / 1000, tz=UTC)
        pnl = float(ep.net_pnl_before_funding)
        year, week, _ = dt.isocalendar()
        buckets["day"][dt.strftime("%Y-%m-%d")] += pnl
        buckets["week"][f"{year}-W{week:02d}"] += pnl
        buckets["month"][dt.strftime("%Y-%m")] += pnl
    result: dict[str, object] = {}
    for name, values in buckets.items():
        result[f"active_{name}s"] = len(values)
        result[f"profitable_{name}_rate"] = _ratio(
            sum(value > 0 for value in values.values()), len(values)
        )
    return result


def _account(role: Any, abstraction: Any, fees: Any) -> dict[str, object]:
    role_value = role.get("role") if isinstance(role, dict) else role
    if isinstance(abstraction, dict):
        abstraction = abstraction.get("abstraction", abstraction.get("type", abstraction))
    fee_data = fees if isinstance(fees, dict) else {}
    staking = fee_data.get("activeStakingDiscount") or {}
    staking = staking if isinstance(staking, dict) else {}
    return {
        "role": str(role_value) if role_value is not None else "unknown",
        "abstraction": (
            json.dumps(abstraction, sort_keys=True)
            if isinstance(abstraction, (dict, list))
            else str(abstraction) if abstraction is not None else "unknown"
        ),
        "current_leader_taker_fee_rate": _f(fee_data.get("userCrossRate")),
        "current_leader_maker_fee_rate": _f(fee_data.get("userAddRate")),
        "current_fee_evidence": "CURRENT_USER_FEES" if fee_data else "UNAVAILABLE",
        "active_referral_discount": _f(fee_data.get("activeReferralDiscount")),
        "active_staking_discount": _f(staking.get("discount")),
    }


@dataclass(frozen=True, slots=True)
class TraderProfile:
    wallet_address: str
    leaderboard_rank: int | None
    display_name: str | None
    as_of_ms: int
    lookback_start_ms: int
    style: str
    execution_style: str
    risk_style: str
    account: dict[str, object]
    leaderboard: dict[str, object]
    performance: dict[str, object]
    consistency: dict[str, object]
    style_metrics: dict[str, object]
    behavior: dict[str, object]
    execution: dict[str, object]
    leverage: dict[str, object]
    funding: dict[str, object]
    concentration: dict[str, object]
    data_quality: dict[str, object]
    assets: dict[str, dict[str, object]]
    current_positions: list[dict[str, object]]
    warnings: tuple[str, ...]
    model_version: str = PROFILE_MODEL_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_flat_dict(self) -> dict[str, object]:
        row: dict[str, object] = {
            "wallet_address": self.wallet_address,
            "leaderboard_rank": self.leaderboard_rank,
            "display_name": self.display_name,
            "as_of_ms": self.as_of_ms,
            "lookback_start_ms": self.lookback_start_ms,
            "style": self.style,
            "execution_style": self.execution_style,
            "risk_style": self.risk_style,
            "model_version": self.model_version,
            "warnings": ",".join(self.warnings),
        }
        for prefix, section in (
            ("account", self.account),
            ("leaderboard", self.leaderboard),
            ("performance", self.performance),
            ("consistency", self.consistency),
            ("style", self.style_metrics),
            ("behavior", self.behavior),
            ("execution", self.execution),
            ("leverage", self.leverage),
            ("funding", self.funding),
            ("concentration", self.concentration),
            ("quality", self.data_quality),
        ):
            for key, value in section.items():
                row[f"{prefix}_{key}"] = value
        row["assets_json"] = json.dumps(self.assets, sort_keys=True, allow_nan=False)
        row["current_positions_json"] = json.dumps(
            self.current_positions, sort_keys=True, allow_nan=False
        )
        return row


def build_trader_profile(
    *,
    wallet_address: str,
    leaderboard_rank: int | None,
    display_name: str | None,
    as_of_ms: int,
    lookback_start_ms: int,
    leaderboard_metrics: dict[str, object],
    fills: list[Fill],
    episodes: list[PositionEpisode],
    clearinghouse_state: Any,
    portfolio: Any,
    historical_orders: list[dict[str, Any]] | None = None,
    twap_slice_fills: list[dict[str, Any]] | None = None,
    funding_rows: list[dict[str, Any]] | None = None,
    user_role: Any = None,
    user_abstraction: Any = None,
    user_fees: Any = None,
    history_cap_hit: bool = False,
    historical_order_limit_hit: bool = False,
    twap_slice_limit_hit: bool = False,
    source_status: dict[str, bool] | None = None,
) -> TraderProfile:
    historical_orders = historical_orders or []
    twap_slice_fills = twap_slice_fills or []
    funding_rows = funding_rows or []
    source_status = source_status or {}
    performance, complete = _performance(episodes)
    style, style_metrics = _style(complete)
    behavior = _behavior(fills, complete)
    execution_style, execution = _execution(
        fills, historical_orders, twap_slice_fills
    )
    current_leverage, current_positions = _current_leverage(clearinghouse_state)
    leverage = current_leverage | _historical_exposure(complete, portfolio)
    leverage["observed_or_inferred_above_1x"] = bool(
        _f(leverage.get("current_max_configured_leverage")) > 1
        or _f(leverage.get("historical_effective_exposure_max")) > 1
    )
    funding = _funding(funding_rows, float(performance["net_pnl_before_funding"]))
    concentration, assets = _concentration(fills, complete)
    account = _account(user_role, user_abstraction, user_fees)
    historical_p90 = _f(leverage.get("historical_effective_exposure_p90"))
    exposure_signal = max(
        historical_p90, _f(leverage.get("current_effective_leverage"))
    )
    worst_return = _f(performance.get("worst_episode_return"))
    if exposure_signal >= 5 or worst_return <= -0.25:
        risk_style = "AGGRESSIVE_LIKE"
    elif exposure_signal >= 2 or worst_return <= -0.10:
        risk_style = "MODERATE_LIKE"
    else:
        risk_style = "CONSERVATIVE_LIKE"
    incomplete = sum(not ep.complete_start for ep in episodes)
    quality: dict[str, object] = {
        "raw_fill_count": len(fills),
        "complete_trade_count": len(complete),
        "incomplete_episode_count": incomplete,
        "history_cap_hit": history_cap_hit,
        "history_truncated": bool(history_cap_hit or incomplete),
        "historical_order_limit_hit": historical_order_limit_hit,
        "twap_slice_limit_hit": twap_slice_limit_hit,
        "first_fill_ms": min((fill.timestamp_ms for fill in fills), default=None),
        "last_fill_ms": max((fill.timestamp_ms for fill in fills), default=None),
        "lookback_days": max(0.0, (as_of_ms - lookback_start_ms) / 86_400_000),
    }
    for source, available in sorted(source_status.items()):
        quality[f"source_{source}_available"] = bool(available)
    checks = [
        (len(complete) < 30, "LOW_SAMPLE"),
        (bool(quality["history_truncated"]), "HISTORY_TRUNCATED"),
        (execution_style == "MAKER_HEAVY", "MAKER_HEAVY"),
        (execution_style == "TWAP_HEAVY", "TWAP_HEAVY"),
        (historical_order_limit_hit, "HISTORICAL_ORDER_API_TRUNCATED"),
        (twap_slice_limit_hit, "TWAP_API_TRUNCATED"),
        (_f(style_metrics.get("sub_5m_trade_fraction")) >= 0.60, "FAST_ALPHA"),
        (
            _f(leverage.get("current_max_configured_leverage")) >= 10,
            "CURRENT_HIGH_CONFIGURED_LEVERAGE",
        ),
        (historical_p90 >= 3, "HIGH_EFFECTIVE_EXPOSURE_ESTIMATE"),
        (
            _f(performance.get("top_5_profit_share")) >= 0.70,
            "PROFIT_CONCENTRATED_IN_FEW_TRADES",
        ),
        (worst_return <= -0.20, "FAT_TAIL_LOSSES"),
        (
            _f(behavior.get("post_loss_size_ratio_vs_median")) >= 1.50,
            "SIZE_UP_AFTER_LOSS",
        ),
        (
            int(behavior.get("scale_in_fill_count", 0)) >= 5
            and _f(behavior.get("adverse_scale_in_fraction")) >= 0.50,
            "AVERAGING_INTO_ADVERSE_MOVES",
        ),
        (
            _f(concentration.get("top_asset_notional_share")) >= 0.80,
            "ASSET_CONCENTRATED",
        ),
        (
            _f(funding.get("absolute_funding_to_trading_pnl")) >= 0.20,
            "FUNDING_MATERIAL",
        ),
        (
            _f(leverage.get("historical_effective_exposure_coverage")) < 0.50,
            "LOW_HISTORICAL_EXPOSURE_COVERAGE",
        ),
        (
            bool(source_status)
            and not source_status.get("clearinghouse_state", False),
            "CURRENT_STATE_UNAVAILABLE",
        ),
    ]
    warnings = [name for condition, name in checks if condition]
    role = str(account.get("role", "unknown"))
    if role in {"vault", "subAccount", "agent"}:
        warnings.append(f"ACCOUNT_ROLE_{role.upper()}")
    return TraderProfile(
        wallet_address=wallet_address.lower(),
        leaderboard_rank=leaderboard_rank,
        display_name=display_name,
        as_of_ms=as_of_ms,
        lookback_start_ms=lookback_start_ms,
        style=style,
        execution_style=execution_style,
        risk_style=risk_style,
        account=account,
        leaderboard=leaderboard_metrics,
        performance=performance,
        consistency=_consistency(complete),
        style_metrics=style_metrics,
        behavior=behavior,
        execution=execution,
        leverage=leverage,
        funding=funding,
        concentration=concentration,
        data_quality=quality,
        assets=assets,
        current_positions=current_positions,
        warnings=tuple(warnings),
    )
