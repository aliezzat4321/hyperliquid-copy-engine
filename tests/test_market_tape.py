from __future__ import annotations

from datetime import UTC, datetime

import polars as pl

from hlcopy.market.tape import MarketTapeWriter


def test_market_tape_writer_is_append_only_and_partitioned(tmp_path) -> None:
    received_at_ns = int(datetime(2026, 8, 8, tzinfo=UTC).timestamp() * 1_000_000_000)
    row = {
        "channel": "bbo",
        "coin": "BTC",
        "exchange_ts_ms": 1_000,
        "received_at_ns": received_at_ns,
        "received_monotonic_ns": 123,
        "observed_event_lag_ms": 1.0,
        "raw_json": "{}",
        "bid_px": 100.0,
        "bid_sz": 2.0,
        "bid_orders": 1,
        "ask_px": 100.1,
        "ask_sz": 1.0,
        "ask_orders": 1,
        "mid_px": 100.05,
        "spread_bps": 9.995002498750625,
        "bbo_imbalance": 1 / 3,
        "microprice": 100.06666666666666,
    }
    writer = MarketTapeWriter(tmp_path)
    writer.append(row)
    first = writer.flush()
    writer.append({**row, "exchange_ts_ms": 1_001})
    second = writer.flush()

    assert len(first) == 1
    assert len(second) == 1
    assert first[0] != second[0]
    assert first[0].exists()
    assert second[0].exists()
    assert "date=2026-08-08/coin=BTC/channel=bbo" in first[0].as_posix()

    frame = pl.read_parquet(first[0])
    assert frame.height == 1
    assert frame["coin"][0] == "BTC"
    assert frame["bid_px"][0] == 100.0
