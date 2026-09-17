from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from hlcopy.market.symbols import wire_coin
from hlcopy.profitability.position_copy import CopyFillEvent
from hlcopy.profitability.position_live_cli import SCENARIOS
from hlcopy.shadow.latency import ObservedSignalLatency

L2_MAX_AGE_MS = 6_000
ORACLE_MAX_AGE_MS = 10_000


def _date_for_ns(value_ns: int) -> str:
    return datetime.fromtimestamp(value_ns / 1_000_000_000, UTC).date().isoformat()


def _merge_windows(windows: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(windows)):
        if end < start:
            continue
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def l2_replay_windows(
    events: Iterable[CopyFillEvent],
    *,
    max_age_ms: int = L2_MAX_AGE_MS,
) -> tuple[tuple[int, int], ...]:
    """Return causal raw-L2 windows needed for every configured latency scenario."""
    age_ns = max(0, int(max_age_ms)) * 1_000_000
    windows: list[tuple[int, int]] = []
    for event in events:
        observed = ObservedSignalLatency(event.exchange_ts_ms, event.received_at_ns)
        for scenario in SCENARIOS:
            try:
                target_ms = observed.estimated_order_arrival_ms(scenario)
            except ValueError:
                continue
            target_ns = int(round(target_ms * 1_000_000))
            windows.append((max(0, target_ns - age_ns), target_ns))
    return _merge_windows(windows)


def oracle_replay_windows(
    funding_boundaries_ms: Iterable[int],
    *,
    max_age_ms: int = ORACLE_MAX_AGE_MS,
) -> tuple[tuple[int, int], ...]:
    """Return causal oracle windows used to price finalized funding settlements."""
    age_ns = max(0, int(max_age_ms)) * 1_000_000
    return _merge_windows(
        (
            max(0, int(boundary_ms) * 1_000_000 - age_ns),
            int(boundary_ms) * 1_000_000,
        )
        for boundary_ms in funding_boundaries_ms
    )


def _windows_by_date(
    windows: Iterable[tuple[int, int]],
) -> dict[str, tuple[tuple[int, int], ...]]:
    grouped: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for start, end in _merge_windows(windows):
        start_date = _date_for_ns(start)
        end_date = _date_for_ns(end)
        grouped[start_date].append((start, end))
        if end_date != start_date:
            grouped[end_date].append((start, end))
    return {day: _merge_windows(values) for day, values in grouped.items()}


def _window_expr(windows: Iterable[tuple[int, int]]) -> pl.Expr:
    expression: pl.Expr | None = None
    for start, end in windows:
        current = pl.col("received_at_ns").is_between(start, end, closed="both")
        expression = current if expression is None else expression | current
    return expression if expression is not None else pl.lit(False)


def extract_market_window_rows(
    market_dir: Path,
    destination: Path,
    *,
    coin: str,
    channel: str,
    windows: Iterable[tuple[int, int]],
) -> tuple[Path | None, int]:
    """Write only raw market rows inside the supplied causal replay windows."""
    frames: list[pl.DataFrame] = []
    wire = wire_coin(coin)
    for day, day_windows in sorted(_windows_by_date(windows).items()):
        directory = market_dir / f"date={day}" / f"coin={wire}" / f"channel={channel}"
        if not directory.exists() or next(directory.glob("*.parquet"), None) is None:
            continue
        source = str(directory / "*.parquet")
        try:
            frame = (
                pl.scan_parquet(source)
                .filter(_window_expr(day_windows))
                .collect(engine="streaming")
            )
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise RuntimeError(
                f"failed to extract {channel} evidence for {coin} date={day}: {exc}"
            ) from exc
        if frame.height:
            frames.append(frame)
    if not frames:
        return None, 0
    combined = pl.concat(frames, how="vertical_relaxed").sort("received_at_ns")
    destination.parent.mkdir(parents=True, exist_ok=True)
    combined.write_parquet(destination, compression="zstd", statistics=True)
    return destination, combined.height


def replay_window_summary(windows: Iterable[tuple[int, int]]) -> list[dict[str, Any]]:
    return [
        {"start_received_at_ns": start, "end_received_at_ns": end}
        for start, end in _merge_windows(windows)
    ]
