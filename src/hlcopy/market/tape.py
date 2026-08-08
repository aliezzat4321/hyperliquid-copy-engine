from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

_PARTITION_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

_BASE_COLUMNS = {
    "channel": pl.String,
    "coin": pl.String,
    "exchange_ts_ms": pl.Int64,
    "received_at_ns": pl.Int64,
    "received_monotonic_ns": pl.Int64,
    "observed_event_lag_ms": pl.Float64,
    "raw_json": pl.String,
}

_CHANNEL_COLUMNS = {
    "bbo": {
        "bid_px": pl.Float64,
        "bid_sz": pl.Float64,
        "bid_orders": pl.Int64,
        "ask_px": pl.Float64,
        "ask_sz": pl.Float64,
        "ask_orders": pl.Int64,
        "mid_px": pl.Float64,
        "spread_bps": pl.Float64,
        "bbo_imbalance": pl.Float64,
        "microprice": pl.Float64,
    },
    "l2Book": {
        "bid_levels_json": pl.String,
        "ask_levels_json": pl.String,
        "best_bid_px": pl.Float64,
        "best_ask_px": pl.Float64,
        "mid_px": pl.Float64,
        "spread_bps": pl.Float64,
        "bbo_imbalance": pl.Float64,
        "microprice": pl.Float64,
        "bid_depth_usd_5bps": pl.Float64,
        "ask_depth_usd_5bps": pl.Float64,
        "depth_imbalance_5bps": pl.Float64,
        "bid_depth_usd_10bps": pl.Float64,
        "ask_depth_usd_10bps": pl.Float64,
        "depth_imbalance_10bps": pl.Float64,
    },
    "trades": {
        "tid": pl.Int64,
        "side": pl.String,
        "px": pl.Float64,
        "sz": pl.Float64,
        "notional_usd": pl.Float64,
        "signed_notional_usd": pl.Float64,
        "hash": pl.String,
        "buyer": pl.String,
        "seller": pl.String,
    },
    "activeAssetCtx": {
        "mark_px": pl.Float64,
        "mid_px": pl.Float64,
        "oracle_px": pl.Float64,
        "funding": pl.Float64,
        "open_interest": pl.Float64,
        "day_notional_volume": pl.Float64,
        "prev_day_px": pl.Float64,
        "premium": pl.Float64,
    },
    "system": {"event": pl.String, "detail": pl.String},
}


def _safe_partition(value: str) -> str:
    cleaned = _PARTITION_SAFE.sub("_", value.strip())
    return cleaned or "unknown"


def _typed_frame(channel: str, rows: list[dict[str, Any]]) -> pl.DataFrame:
    frame = pl.DataFrame(rows, infer_schema_length=None)
    schema = {**_BASE_COLUMNS, **_CHANNEL_COLUMNS.get(channel, {})}
    expressions = [
        pl.col(column).cast(dtype, strict=False).alias(column)
        for column, dtype in schema.items()
        if column in frame.columns
    ]
    return frame.with_columns(expressions) if expressions else frame


class MarketTapeWriter:
    """Append-only partitioned Parquet writer; existing tape files are never rewritten."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._buffers: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def append(self, row: dict[str, Any]) -> None:
        received_at_ns = int(row["received_at_ns"])
        date = datetime.fromtimestamp(received_at_ns / 1_000_000_000, tz=UTC).date().isoformat()
        channel = _safe_partition(str(row.get("channel", "unknown")))
        coin = _safe_partition(str(row.get("coin", "unknown")))
        self._buffers.setdefault((date, coin, channel), []).append(row)

    def buffered_rows(self) -> int:
        return sum(len(rows) for rows in self._buffers.values())

    def flush(self) -> list[Path]:
        written: list[Path] = []
        for key, rows in list(self._buffers.items()):
            date, coin, channel = key
            if not rows:
                continue
            directory = self.root / f"date={date}" / f"coin={coin}" / f"channel={channel}"
            directory.mkdir(parents=True, exist_ok=True)
            name = f"part-{time.time_ns()}-{uuid.uuid4().hex[:12]}.parquet"
            path = directory / name
            temp = directory / f".{name}.tmp"
            _typed_frame(channel, rows).write_parquet(
                temp,
                compression="zstd",
                statistics=True,
            )
            os.replace(temp, path)
            self._buffers.pop(key, None)
            written.append(path)
        return written


class AsyncMarketTapeSink:
    """Bounded async queue keeps disk I/O off the WebSocket receive loop."""

    _STOP = object()

    def __init__(
        self,
        writer: MarketTapeWriter,
        *,
        flush_rows: int,
        flush_seconds: float,
        queue_size: int,
    ) -> None:
        self.writer = writer
        self.flush_rows = max(1, flush_rows)
        self.flush_seconds = max(0.1, flush_seconds)
        self.queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="market-tape-writer")

    async def put(self, row: dict[str, Any]) -> None:
        if self._task is not None and self._task.done():
            await self._task
        await self.queue.put(row)

    async def close(self) -> None:
        if self._task is None:
            return
        await self.queue.put(self._STOP)
        await self._task
        self._task = None

    async def _flush(self) -> None:
        rows = self.writer.buffered_rows()
        if rows:
            paths = await asyncio.to_thread(self.writer.flush)
            logger.info("market tape flush rows=%d files=%d", rows, len(paths))

    async def _run(self) -> None:
        deadline = time.monotonic() + self.flush_seconds
        while True:
            timeout = max(0.0, deadline - time.monotonic())
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            except TimeoutError:
                await self._flush()
                deadline = time.monotonic() + self.flush_seconds
                continue
            if item is self._STOP:
                self.queue.task_done()
                await self._flush()
                return
            assert isinstance(item, dict)
            self.writer.append(item)
            self.queue.task_done()
            if self.writer.buffered_rows() >= self.flush_rows:
                await self._flush()
                deadline = time.monotonic() + self.flush_seconds
