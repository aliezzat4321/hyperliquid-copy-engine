from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.copyability.backtest import BacktestConfig, run_backtest, write_backtest_outputs
from hlcopy.market.historical_archive import LocalArchiveBookProvider
from hlcopy.signals.invo import CopySignal

D = Decimal


def run_matrix(
    signals: list[CopySignal] | tuple[CopySignal, ...],
    *,
    output_dir: Path,
    capital: Decimal,
    latencies_ms: list[int],
    leverages: list[Decimal],
    taker_fee_bps: Decimal,
    max_slippage_bps: Decimal,
    max_margin_fraction_per_trade: Decimal,
    max_total_margin_fraction: Decimal,
    archive_dir: Path | None,
    max_book_age_ms: int,
) -> tuple[Path, Path]:
    provider = (
        LocalArchiveBookProvider(archive_dir, max_book_age_ms=max_book_age_ms)
        if archive_dir is not None
        else None
    )
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    mode = "l2" if provider is not None else "baseline"
    matrix: list[dict[str, object]] = []

    for latency_ms in latencies_ms:
        for leverage in leverages:
            config = BacktestConfig(
                starting_capital=capital,
                latency_ms=latency_ms,
                follower_leverage=leverage,
                taker_fee_rate=taker_fee_bps / D("10000"),
                max_slippage_bps=max_slippage_bps,
                max_margin_fraction_per_trade=max_margin_fraction_per_trade,
                max_total_margin_fraction=max_total_margin_fraction,
            )
            summary, rows = run_backtest(signals, config, book_provider=provider)
            stem = f"copy_{mode}_{stamp}_{latency_ms}ms_{leverage}x".replace(".", "p")
            write_backtest_outputs(output_dir, stem=stem, summary=summary, rows=rows)
            matrix.append(summary.to_dict())

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"copyability_matrix_{mode}_{stamp}.json"
    csv_path = output_dir / f"copyability_matrix_{mode}_{stamp}.csv"
    json_path.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = list(matrix[0]) if matrix else []
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(matrix)
    return json_path, csv_path
