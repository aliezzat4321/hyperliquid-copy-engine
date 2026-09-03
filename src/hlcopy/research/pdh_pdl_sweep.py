from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
BPS = 10_000.0
BAR_MS = 5 * 60 * 1000


@dataclass(frozen=True, slots=True)
class Bar:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def utc_dt(self) -> datetime:
        return datetime.fromtimestamp(self.open_time_ms / 1000, tz=UTC)


@dataclass(frozen=True, slots=True)
class Setup:
    symbol: str
    trading_day: date
    direction: str
    pdh: float
    pdl: float
    sweep_time_ms: int
    confirm_time_ms: int
    entry_time_ms: int
    entry: float
    stop: float
    sweep_extreme: float


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    trading_day: date
    direction: str
    target_r: float
    entry_time_ms: int
    exit_time_ms: int
    entry: float
    stop: float
    target: float
    exit: float
    exit_reason: str
    risk_fraction_price: float
    exposure_multiple: float
    actual_risk_fraction_equity: float
    gross_r: float
    net_r: float
    gross_return_fraction: float
    cost_return_fraction: float
    net_return_fraction: float


@dataclass(frozen=True, slots=True)
class DayDiagnostics:
    symbol: str
    trading_day: date
    eligible_previous_day: bool
    breached_level: bool
    reclaimed_level: bool
    confirmed_setup: bool


def _valid_bar(bar: Bar) -> bool:
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        return False
    return bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high


def _is_complete_utc_day(day_bars: list[Bar]) -> bool:
    if len(day_bars) != 288:
        return False
    times = sorted(bar.open_time_ms for bar in day_bars)
    return all(b - a == BAR_MS for a, b in zip(times, times[1:], strict=False))


def build_setups(
    symbol: str,
    bars: list[Bar],
    *,
    stop_buffer_bps: float = 2.0,
) -> tuple[list[Setup], list[DayDiagnostics]]:
    clean = sorted(
        {bar.open_time_ms: bar for bar in bars if _valid_bar(bar)}.values(),
        key=lambda bar: bar.open_time_ms,
    )
    by_utc_day: dict[date, list[Bar]] = defaultdict(list)
    for bar in clean:
        by_utc_day[bar.utc_dt.date()].append(bar)

    setups: list[Setup] = []
    diagnostics: list[DayDiagnostics] = []
    clean_by_time = {bar.open_time_ms: bar for bar in clean}

    for day in sorted(by_utc_day):
        prev_day = day - timedelta(days=1)
        prev = by_utc_day.get(prev_day, [])
        eligible = _is_complete_utc_day(prev)
        if not eligible:
            diagnostics.append(DayDiagnostics(symbol, day, False, False, False, False))
            continue
        pdh = max(bar.high for bar in prev)
        pdl = min(bar.low for bar in prev)

        session = []
        for bar in by_utc_day[day]:
            london = bar.utc_dt.astimezone(LONDON)
            if london.date() == day and london.hour in (8, 9):
                session.append(bar)
        session.sort(key=lambda bar: bar.open_time_ms)
        breached = False
        reclaimed = False
        selected: Setup | None = None

        for sweep in session:
            london = sweep.utc_dt.astimezone(LONDON)
            # Need an immediate confirmation bar and at least one post-entry bar before 10:00.
            if (london.hour, london.minute) > (9, 45):
                break
            high_breach = sweep.high > pdh
            low_breach = sweep.low < pdl
            if high_breach or low_breach:
                breached = True
            if high_breach and low_breach:
                # OHLC cannot tell which side swept first, so fail closed on direction.
                continue

            direction = None
            if high_breach and sweep.close < pdh:
                direction = "SHORT"
            elif low_breach and sweep.close > pdl:
                direction = "LONG"
            if direction is None:
                continue
            reclaimed = True

            confirm = clean_by_time.get(sweep.open_time_ms + BAR_MS)
            if confirm is None:
                continue
            confirm_london = confirm.utc_dt.astimezone(LONDON)
            if confirm_london.date() != london.date() or confirm_london.hour not in (8, 9):
                continue
            confirmed = (direction == "SHORT" and confirm.close < sweep.low) or (
                direction == "LONG" and confirm.close > sweep.high
            )
            if not confirmed:
                continue

            entry = confirm.close
            buffer = stop_buffer_bps / BPS
            if direction == "SHORT":
                stop = sweep.high * (1.0 + buffer)
                extreme = sweep.high
                if stop <= entry:
                    continue
            else:
                stop = sweep.low * (1.0 - buffer)
                extreme = sweep.low
                if stop >= entry:
                    continue
            selected = Setup(
                symbol=symbol,
                trading_day=day,
                direction=direction,
                pdh=pdh,
                pdl=pdl,
                sweep_time_ms=sweep.open_time_ms,
                confirm_time_ms=confirm.open_time_ms,
                entry_time_ms=confirm.open_time_ms + BAR_MS,
                entry=entry,
                stop=stop,
                sweep_extreme=extreme,
            )
            break

        diagnostics.append(
            DayDiagnostics(symbol, day, True, breached, reclaimed, selected is not None)
        )
        if selected is not None:
            setups.append(selected)

    return setups, diagnostics


def simulate_trade(
    setup: Setup,
    bars_by_time: dict[int, Bar],
    *,
    target_r: float,
    risk_target_fraction: float = 0.005,
    max_exposure_multiple: float = 5.0,
    round_trip_cost_bps: float = 15.0,
) -> Trade | None:
    risk_price = abs(setup.stop - setup.entry)
    risk_fraction_price = risk_price / setup.entry
    if risk_fraction_price <= 0:
        return None
    exposure_multiple = min(risk_target_fraction / risk_fraction_price, max_exposure_multiple)
    actual_risk_fraction = exposure_multiple * risk_fraction_price
    if actual_risk_fraction <= 0:
        return None

    if setup.direction == "SHORT":
        target = setup.entry - target_r * risk_price
    else:
        target = setup.entry + target_r * risk_price
    if target <= 0:
        return None

    exit_price: float | None = None
    exit_time_ms: int | None = None
    reason = "TIME"
    cursor = setup.entry_time_ms
    last_bar: Bar | None = None
    while True:
        bar = bars_by_time.get(cursor)
        if bar is None:
            break
        london = bar.utc_dt.astimezone(LONDON)
        if london.date() != setup.trading_day or london.hour >= 10:
            break
        last_bar = bar
        stop_hit = bar.high >= setup.stop if setup.direction == "SHORT" else bar.low <= setup.stop
        target_hit = bar.low <= target if setup.direction == "SHORT" else bar.high >= target
        if stop_hit:
            # Conservative ordering when target and stop both occur inside one 5m candle.
            exit_price = setup.stop
            exit_time_ms = bar.open_time_ms + BAR_MS
            reason = "STOP"
            break
        if target_hit:
            exit_price = target
            exit_time_ms = bar.open_time_ms + BAR_MS
            reason = "TARGET"
            break
        cursor += BAR_MS

    if exit_price is None:
        if last_bar is None:
            return None
        exit_price = last_bar.close
        exit_time_ms = last_bar.open_time_ms + BAR_MS

    directional_return = (
        (setup.entry - exit_price) / setup.entry
        if setup.direction == "SHORT"
        else (exit_price - setup.entry) / setup.entry
    )
    gross_return = exposure_multiple * directional_return
    cost_return = exposure_multiple * (round_trip_cost_bps / BPS)
    net_return = gross_return - cost_return
    gross_r = gross_return / actual_risk_fraction
    net_r = net_return / actual_risk_fraction
    return Trade(
        symbol=setup.symbol,
        trading_day=setup.trading_day,
        direction=setup.direction,
        target_r=target_r,
        entry_time_ms=setup.entry_time_ms,
        exit_time_ms=exit_time_ms,
        entry=setup.entry,
        stop=setup.stop,
        target=target,
        exit=exit_price,
        exit_reason=reason,
        risk_fraction_price=risk_fraction_price,
        exposure_multiple=exposure_multiple,
        actual_risk_fraction_equity=actual_risk_fraction,
        gross_r=gross_r,
        net_r=net_r,
        gross_return_fraction=gross_return,
        cost_return_fraction=cost_return,
        net_return_fraction=net_return,
    )


def simulate_setups(setups: list[Setup], bars: list[Bar], target_r: float) -> list[Trade]:
    bars_by_time = {bar.open_time_ms: bar for bar in bars}
    return [
        trade
        for setup in setups
        if (trade := simulate_trade(setup, bars_by_time, target_r=target_r)) is not None
    ]


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in returns:
        equity *= 1.0 + ret
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd


def _bootstrap_ci(
    trades: list[Trade],
    *,
    samples: int = 10_000,
    confidence: float = 0.90,
    seed: int = 177,
) -> tuple[float, float]:
    if not trades:
        return (math.nan, math.nan)
    by_day: dict[date, list[float]] = defaultdict(list)
    for trade in trades:
        by_day[trade.trading_day].append(trade.net_r)
    days = sorted(by_day)
    rng = random.Random(seed)
    boot: list[float] = []
    for _ in range(samples):
        values: list[float] = []
        for _ in days:
            sampled_day = days[rng.randrange(len(days))]
            values.extend(by_day[sampled_day])
        boot.append(mean(values))
    boot.sort()
    alpha = (1.0 - confidence) / 2.0
    lo = boot[max(0, int(alpha * samples))]
    hi = boot[min(samples - 1, int((1.0 - alpha) * samples) - 1)]
    return lo, hi


def summarize(trades: list[Trade]) -> dict[str, object]:
    if not trades:
        return {"trades": 0}
    ordered = sorted(trades, key=lambda trade: (trade.entry_time_ms, trade.symbol))
    net_returns = [trade.net_return_fraction for trade in ordered]
    net_rs = [trade.net_r for trade in ordered]
    gross_rs = [trade.gross_r for trade in ordered]
    wins = [value for value in net_returns if value > 0]
    losses = [-value for value in net_returns if value < 0]
    profit_factor = sum(wins) / sum(losses) if losses else None
    positives = [value for value in net_returns if value > 0]
    concentration = max(positives) / sum(positives) if positives else None
    ci_lo, ci_hi = _bootstrap_ci(ordered)
    compounded = math.prod(1.0 + value for value in net_returns) - 1.0
    years: dict[int, list[float]] = defaultdict(list)
    for trade in ordered:
        years[trade.trading_day.year].append(trade.net_r)
    return {
        "trades": len(ordered),
        "distinct_days": len({trade.trading_day for trade in ordered}),
        "win_rate": sum(value > 0 for value in net_returns) / len(net_returns),
        "mean_gross_r": mean(gross_rs),
        "mean_net_r": mean(net_rs),
        "median_net_r": median(net_rs),
        "mean_net_return_pct": mean(net_returns) * 100.0,
        "compounded_return_pct": compounded * 100.0,
        "profit_factor": profit_factor,
        "max_drawdown_pct": _max_drawdown(net_returns) * 100.0,
        "max_winner_share_of_positive_returns": concentration,
        "bootstrap_90_mean_net_r": [ci_lo, ci_hi],
        "mean_exposure_multiple": mean(trade.exposure_multiple for trade in ordered),
        "mean_actual_risk_pct_equity": (
            mean(trade.actual_risk_fraction_equity for trade in ordered) * 100.0
        ),
        "year_mean_net_r": {str(year): mean(values) for year, values in sorted(years.items())},
        "exit_reasons": {
            reason: sum(trade.exit_reason == reason for trade in ordered)
            for reason in ("TARGET", "STOP", "TIME")
        },
    }


def _month_starts(start: date, end: date):
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        yield cursor
        cursor = date(
            cursor.year + (cursor.month == 12),
            1 if cursor.month == 12 else cursor.month + 1,
            1,
        )


def _read_zip_csv(payload: bytes) -> list[Bar]:
    bars: list[Bar] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected exactly one csv in archive, got {names}")
        with archive.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8")
            for row in csv.reader(text):
                if not row or not row[0].isdigit():
                    continue
                try:
                    bar = Bar(
                        open_time_ms=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                except (IndexError, ValueError):
                    continue
                if _valid_bar(bar):
                    bars.append(bar)
    return bars


def _download(url: str, *, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hlcopy-pdh-pdl-exp/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download_binance_um_5m(
    symbol: str,
    start: date,
    end: date,
    cache_dir: Path,
) -> tuple[list[Bar], list[str]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    bars: list[Bar] = []
    missing: list[str] = []
    base = "https://data.binance.vision/data/futures/um"
    for month in _month_starts(start, end):
        month_key = month.strftime("%Y-%m")
        monthly_name = f"{symbol}-5m-{month_key}.zip"
        cache_path = cache_dir / monthly_name
        payload: bytes | None = None
        if cache_path.exists():
            payload = cache_path.read_bytes()
        else:
            url = f"{base}/monthly/klines/{symbol}/5m/{monthly_name}"
            try:
                payload = _download(url)
                cache_path.write_bytes(payload)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise

        if payload is not None:
            bars.extend(_read_zip_csv(payload))
            continue

        # Monthly archives are delayed; fall back to daily files for a missing month.
        next_month = date(
            month.year + (month.month == 12),
            1 if month.month == 12 else month.month + 1,
            1,
        )
        month_end = next_month - timedelta(days=1)
        cursor = max(start, month)
        stop = min(end, month_end)
        while cursor <= stop:
            day_key = cursor.isoformat()
            daily_name = f"{symbol}-5m-{day_key}.zip"
            daily_path = cache_dir / daily_name
            try:
                if daily_path.exists():
                    daily_payload = daily_path.read_bytes()
                else:
                    daily_url = f"{base}/daily/klines/{symbol}/5m/{daily_name}"
                    daily_payload = _download(daily_url)
                    daily_path.write_bytes(daily_payload)
                bars.extend(_read_zip_csv(daily_payload))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    missing.append(day_key)
                else:
                    raise
            cursor += timedelta(days=1)
    deduped = sorted(
        {bar.open_time_ms: bar for bar in bars}.values(), key=lambda bar: bar.open_time_ms
    )
    return deduped, missing


def run_experiment(start: date, end: date, cache_dir: Path) -> dict[str, object]:
    symbols = ("BTCUSDT", "ETHUSDT")
    targets = (1.0, 1.5, 2.0, 3.0)
    split = date(2025, 1, 1)
    report: dict[str, object] = {
        "experiment": "EXP-177-PDH-PDL-LONDON-SWEEP",
        "source": (
            "Binance USD-M perpetual 5m public klines "
            "(proxy, not Hyperliquid execution evidence)"
        ),
        "window": [start.isoformat(), end.isoformat()],
        "discovery_end": "2024-12-31",
        "holdout_start": "2025-01-01",
        "round_trip_cost_bps_assumed": 15.0,
        "stop_buffer_bps": 2.0,
        "risk_target_pct_equity": 0.5,
        "max_exposure_multiple": 5.0,
        "targets_r": list(targets),
        "symbols": {},
    }
    pooled_by_target: dict[float, dict[str, list[Trade]]] = {
        target: {"discovery": [], "holdout": []} for target in targets
    }

    for symbol in symbols:
        bars, missing = download_binance_um_5m(symbol, start, end, cache_dir / symbol)
        setups, diagnostics = build_setups(symbol, bars)
        eligible_days = [item for item in diagnostics if item.eligible_previous_day]
        symbol_report: dict[str, object] = {
            "bars": len(bars),
            "first_bar": bars[0].utc_dt.isoformat() if bars else None,
            "last_bar": bars[-1].utc_dt.isoformat() if bars else None,
            "missing_daily_archives": missing,
            "eligible_days": len(eligible_days),
            "days_breaching_pdh_or_pdl_08_10_london": sum(
                item.breached_level for item in eligible_days
            ),
            "days_reclaiming_after_breach": sum(item.reclaimed_level for item in eligible_days),
            "days_with_confirmed_setup": sum(item.confirmed_setup for item in eligible_days),
            "breach_frequency": (
                sum(item.breached_level for item in eligible_days) / len(eligible_days)
                if eligible_days
                else None
            ),
            "confirmed_setup_frequency": (
                sum(item.confirmed_setup for item in eligible_days) / len(eligible_days)
                if eligible_days
                else None
            ),
            "targets": {},
        }
        for target in targets:
            trades = simulate_setups(setups, bars, target)
            discovery = [trade for trade in trades if trade.trading_day < split]
            holdout = [trade for trade in trades if trade.trading_day >= split]
            pooled_by_target[target]["discovery"].extend(discovery)
            pooled_by_target[target]["holdout"].extend(holdout)
            symbol_report["targets"][str(target)] = {
                "discovery": summarize(discovery),
                "holdout": summarize(holdout),
            }
        report["symbols"][symbol] = symbol_report

    report["pooled"] = {}
    for target in targets:
        report["pooled"][str(target)] = {
            phase: summarize(trades) for phase, trades in pooled_by_target[target].items()
        }

    verdicts: dict[str, object] = {}
    for target in targets:
        pooled = report["pooled"][str(target)]["holdout"]
        if pooled.get("trades", 0) == 0:
            verdicts[str(target)] = {"interesting_candidate": False, "reasons": ["NO_TRADES"]}
            continue
        reasons = []
        if pooled["trades"] < 30:
            reasons.append("MIN_TRADES")
        if pooled["distinct_days"] < 5:
            reasons.append("MIN_DAYS")
        if pooled["mean_net_r"] <= 0:
            reasons.append("NONPOSITIVE_NET_EXPECTANCY")
        if pooled["bootstrap_90_mean_net_r"][0] <= 0:
            reasons.append("BOOTSTRAP_LOWER_BOUND_NOT_ABOVE_ZERO")
        concentration = pooled["max_winner_share_of_positive_returns"]
        if concentration is not None and concentration > 0.50:
            reasons.append("PROFIT_CONCENTRATION")
        asset_means = []
        for symbol in symbols:
            asset_summary = report["symbols"][symbol]["targets"][str(target)]["holdout"]
            asset_means.append(asset_summary.get("mean_net_r", math.nan))
        if any(math.isnan(value) or value <= 0 for value in asset_means):
            reasons.append("ASSET_DEPENDENCE")
        year_means = pooled.get("year_mean_net_r", {})
        if any(value <= 0 for value in year_means.values()) or len(year_means) < 2:
            reasons.append("YEAR_DEPENDENCE")
        verdicts[str(target)] = {"interesting_candidate": not reasons, "reasons": reasons}
    report["frozen_holdout_verdicts"] = verdicts
    report["live_trading_authorized"] = False
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Frozen PDH/PDL London sweep experiment")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-08-31")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/pdh-pdl"))
    parser.add_argument("--output", type=Path, default=Path("pdh_pdl_sweep_report.json"))
    args = parser.parse_args()
    report = run_experiment(
        date.fromisoformat(args.start), date.fromisoformat(args.end), args.cache_dir
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
