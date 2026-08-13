from __future__ import annotations

import json
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from pathlib import Path

import polars as pl

from hlcopy.market.symbols import canonical_coin
from hlcopy.profitability.continuous_path_v2 import AssetContextMark, FundingRate
from hlcopy.profitability.margin_tables import (
    MarginMetadataSnapshot,
    parse_margin_metadata,
)

D = Decimal
ZERO = D("0")


def _d(value: object) -> Decimal | None:
    try:
        result = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result


def load_asset_context_marks(
    market_dir: Path,
    *,
    coins: Iterable[str] | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> tuple[AssetContextMark, ...]:
    """Load captured activeAssetCtx rows without substituting L2 mids."""
    wanted = {canonical_coin(coin) for coin in coins} if coins is not None else None
    rows: list[AssetContextMark] = []
    for path in sorted(
        market_dir.glob("date=*/coin=*/channel=activeAssetCtx/*.parquet")
    ):
        frame = pl.read_parquet(
            path,
            columns=["coin", "received_at_ns", "mark_px", "oracle_px"],
        )
        for row in frame.iter_rows(named=True):
            coin = canonical_coin(row.get("coin") or "")
            if wanted is not None and coin not in wanted:
                continue
            try:
                received_at_ns = int(row["received_at_ns"])
            except (KeyError, TypeError, ValueError):
                continue
            if start_ns is not None and received_at_ns < start_ns:
                continue
            if end_ns is not None and received_at_ns > end_ns:
                continue
            mark = _d(row.get("mark_px"))
            oracle = _d(row.get("oracle_px"))
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
    rows.sort(key=lambda item: (item.received_at_ns, item.coin))
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
