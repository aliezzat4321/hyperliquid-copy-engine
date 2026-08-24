from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import polars as pl

from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.profitability.continuous_path_v2 import AssetContextMark, FundingRate
from hlcopy.profitability.margin_tables import (
    MarginMetadataSnapshot,
    parse_margin_metadata,
)

D = Decimal
ZERO = D("0")
_PARTITION_SAFE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _d(value: object) -> Decimal | None:
    try:
        result = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result


def _safe_partition(value: str) -> str:
    cleaned = _PARTITION_SAFE.sub("_", value.strip())
    return cleaned or "unknown"


def _date_from_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).date().isoformat()


def _active_asset_ctx_paths(
    market_dir: Path,
    *,
    coins: set[str] | None,
    start_ns: int | None,
    end_ns: int | None,
) -> list[Path]:
    """Resolve only date/coin partitions that can satisfy the request."""
    start_date = _date_from_ns(start_ns) if start_ns is not None else None
    end_date = _date_from_ns(end_ns) if end_ns is not None else None

    date_dirs: list[Path] = []
    for date_dir in sorted(market_dir.glob("date=*")):
        label = date_dir.name.removeprefix("date=")
        if start_date is not None and label < start_date:
            continue
        if end_date is not None and label > end_date:
            continue
        date_dirs.append(date_dir)

    paths: list[Path] = []
    if coins is None:
        for date_dir in date_dirs:
            paths.extend(date_dir.glob("coin=*/channel=activeAssetCtx/*.parquet"))
        return sorted(paths)

    partition_names: set[str] = set()
    for coin in coins:
        partition_names.add(_safe_partition(coin))
        partition_names.add(_safe_partition(wire_coin(coin)))

    for date_dir in date_dirs:
        for partition_name in partition_names:
            directory = date_dir / f"coin={partition_name}" / "channel=activeAssetCtx"
            paths.extend(directory.glob("*.parquet"))
    return sorted(paths)


def load_asset_context_marks(
    market_dir: Path,
    *,
    coins: Iterable[str] | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> tuple[AssetContextMark, ...]:
    """Load captured activeAssetCtx rows without substituting L2 mids.

    Partition pruning happens before parquet discovery: only the requested date and
    coin directories are considered. Polars then applies row-level timestamp filters.
    """
    wanted = {canonical_coin(coin) for coin in coins} if coins is not None else None
    paths = _active_asset_ctx_paths(
        market_dir,
        coins=wanted,
        start_ns=start_ns,
        end_ns=end_ns,
    )
    if not paths:
        return ()

    lazy = pl.scan_parquet(
        [str(path) for path in paths],
        hive_partitioning=False,
    ).select(
        "coin",
        "received_at_ns",
        "mark_px",
        "oracle_px",
    )
    if start_ns is not None:
        lazy = lazy.filter(pl.col("received_at_ns") >= start_ns)
    if end_ns is not None:
        lazy = lazy.filter(pl.col("received_at_ns") <= end_ns)

    frame = lazy.sort(["received_at_ns", "coin"]).collect(engine="streaming")
    rows: list[AssetContextMark] = []
    for coin_raw, received_raw, mark_raw, oracle_raw in frame.iter_rows():
        coin = canonical_coin(coin_raw or "")
        if wanted is not None and coin not in wanted:
            continue
        try:
            received_at_ns = int(received_raw)
        except (TypeError, ValueError):
            continue
        mark = _d(mark_raw)
        oracle = _d(oracle_raw)
        if mark is None or oracle is None or mark <= ZERO or oracle <= ZERO:
            continue
        rows.append(
            AssetContextMark(
                coin=coin,
                received_at_ns=received_at_ns,
                mark_price=mark,
                oracle_price=oracle,
            )
        )
    return tuple(rows)


def load_funding_history_jsonl(
    path: Path,
    *,
    coins: Iterable[str] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> tuple[FundingRate, ...]:
    """Load normalized or raw official fundingHistory rows from an append-only JSONL."""
    wanted = {canonical_coin(coin) for coin in coins} if coins is not None else None
    out: list[FundingRate] = []
    seen: set[tuple[str, int, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            outer = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidates: list[object]
        if isinstance(outer, dict) and isinstance(outer.get("rows"), list):
            candidates = outer["rows"]
        else:
            candidates = [outer]
        for raw in candidates:
            if not isinstance(raw, dict):
                continue
            coin = canonical_coin(raw.get("coin") or "")
            if wanted is not None and coin not in wanted:
                continue
            try:
                ts = int(raw.get("time"))
            except (TypeError, ValueError):
                continue
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            rate = _d(raw.get("fundingRate"))
            if rate is None:
                continue
            key = (coin, ts, str(rate))
            if key in seen:
                continue
            seen.add(key)
            out.append(FundingRate(coin=coin, payment_ts_ms=ts, funding_rate=rate))
    out.sort(key=lambda item: (item.payment_ts_ms, item.coin))
    return tuple(out)


def load_margin_snapshots_jsonl(path: Path) -> tuple[MarginMetadataSnapshot, ...]:
    """Load prospectively timestamped official margin metadata snapshots."""
    out: list[MarginMetadataSnapshot] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        try:
            fetched_at_ns = int(row["fetched_at_ns"])
        except (KeyError, TypeError, ValueError):
            continue
        payload = row.get("payload")
        try:
            out.append(
                parse_margin_metadata(
                    payload,
                    fetched_at_ns=fetched_at_ns,
                    dex=str(row.get("dex") or ""),
                )
            )
        except ValueError:
            continue
    out.sort(key=lambda item: item.fetched_at_ns)
    return tuple(out)
