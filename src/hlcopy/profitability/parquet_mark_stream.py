from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import polars as pl

from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.profitability.continuous_path_v2 import AssetContextMark

D = Decimal
ZERO = D("0")


def _d(value: object) -> Decimal | None:
    try:
        result = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result


def _date_from_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).date().isoformat()


def _partition_names(coin: str) -> tuple[str, ...]:
    canonical = canonical_coin(coin)
    return tuple(sorted({canonical, wire_coin(canonical)} - {""}))


def candidate_mark_files(
    market_dir: Path,
    *,
    coin: str,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> tuple[Path, ...]:
    """Resolve only the requested date/coin activeAssetCtx partitions."""
    start_date = _date_from_ns(start_ns) if start_ns is not None else None
    end_date = _date_from_ns(end_ns) if end_ns is not None else None
    out: list[Path] = []
    for date_dir in sorted(market_dir.glob("date=*")):
        date_value = date_dir.name.removeprefix("date=")
        if start_date is not None and date_value < start_date:
            continue
        if end_date is not None and date_value > end_date:
            continue
        for partition in _partition_names(coin):
            channel_dir = date_dir / f"coin={partition}" / "channel=activeAssetCtx"
            if channel_dir.is_dir():
                out.extend(sorted(channel_dir.glob("*.parquet")))
    return tuple(dict.fromkeys(out))


def _iter_coin_marks(
    market_dir: Path,
    *,
    coin: str,
    start_ns: int | None,
    end_ns: int | None,
) -> Iterator[AssetContextMark]:
    wanted = canonical_coin(coin)
    for path in candidate_mark_files(
        market_dir,
        coin=wanted,
        start_ns=start_ns,
        end_ns=end_ns,
    ):
        frame = pl.read_parquet(
            path,
            columns=["coin", "received_at_ns", "mark_px", "oracle_px"],
        ).sort("received_at_ns")
        for coin_raw, received_raw, mark_raw, oracle_raw in frame.iter_rows():
            row_coin = canonical_coin(coin_raw or "")
            if row_coin != wanted:
                continue
            try:
                received_at_ns = int(received_raw)
            except (TypeError, ValueError):
                continue
            if start_ns is not None and received_at_ns < start_ns:
                continue
            if end_ns is not None and received_at_ns > end_ns:
                continue
            mark = _d(mark_raw)
            oracle = _d(oracle_raw)
            if mark is None or oracle is None or mark <= ZERO or oracle <= ZERO:
                continue
            yield AssetContextMark(
                coin=row_coin,
                received_at_ns=received_at_ns,
                mark_price=mark,
                oracle_price=oracle,
            )


def iter_asset_context_marks(
    market_dir: Path,
    *,
    coins: Iterable[str],
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> Iterator[AssetContextMark]:
    """Globally ordered mark iterator with bounded memory.

    At most one parquet file per requested coin is materialized at a time. The
    individual coin streams are merged by receive timestamp, so callers never
    need a multi-million-row Python tuple just to obtain global ordering.
    """
    iterators = [
        _iter_coin_marks(
            market_dir,
            coin=coin,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        for coin in sorted({canonical_coin(raw) for raw in coins if canonical_coin(raw)})
    ]
    if not iterators:
        return iter(())
    return heapq.merge(*iterators, key=lambda row: (row.received_at_ns, row.coin))


def latest_asset_context_ns(
    market_dir: Path,
    *,
    coins: Iterable[str],
    start_ns: int | None = None,
) -> int | None:
    """Find the market tape high-water mark from only the newest file per coin."""
    latest: int | None = None
    for coin in sorted({canonical_coin(raw) for raw in coins if canonical_coin(raw)}):
        paths = candidate_mark_files(market_dir, coin=coin, start_ns=start_ns)
        for path in reversed(paths):
            frame = pl.read_parquet(path, columns=["received_at_ns"])
            if frame.height == 0:
                continue
            value = frame.get_column("received_at_ns").max()
            if value is not None:
                ts = int(value)
                latest = ts if latest is None else max(latest, ts)
            break
    return latest
