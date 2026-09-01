from __future__ import annotations

from datetime import UTC, datetime

import polars as pl

from hlcopy.market.tape import MarketTapeWriter


def _bbo_row(*, coin: str = "BTC", exchange_ts_ms: int = 1_000) -> dict[str, object]:
    received_at_ns = int(datetime(2026, 8, 8, tzinfo=UTC).timestamp() * 1_000_000_000)
    return {
        "channel": "bbo",
        "coin": coin,
        "exchange_ts_ms": exchange_ts_ms,
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


def test_market_tape_writer_is_append_only_and_partitioned(tmp_path) -> None:
    row = _bbo_row()
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


def test_market_tape_can_flush_one_hot_partition_without_tiny_neighbor_file(tmp_path) -> None:
    writer = MarketTapeWriter(tmp_path)
    btc_key = writer.append(_bbo_row(coin="BTC", exchange_ts_ms=1_000))
    writer.append(_bbo_row(coin="BTC", exchange_ts_ms=1_001))
    eth_key = writer.append(_bbo_row(coin="ETH", exchange_ts_ms=1_002))

    paths = writer.flush((btc_key,))

    assert len(paths) == 1
    assert "coin=BTC" in paths[0].as_posix()
    assert writer.partition_rows(btc_key) == 0
    assert writer.partition_rows(eth_key) == 1
    assert writer.buffered_rows() == 1

    remaining = writer.flush()
    assert len(remaining) == 1
    assert "coin=ETH" in remaining[0].as_posix()


def test_market_tape_largest_partition_keys_follow_buffer_pressure(tmp_path) -> None:
    writer = MarketTapeWriter(tmp_path)
    btc_key = writer.append(_bbo_row(coin="BTC", exchange_ts_ms=1_000))
    writer.append(_bbo_row(coin="BTC", exchange_ts_ms=1_001))
    eth_key = writer.append(_bbo_row(coin="ETH", exchange_ts_ms=1_002))

    keys = writer.largest_partition_keys()

    assert keys[0] == btc_key
    assert keys[1] == eth_key


def test_market_tape_preserves_hip3_namespace_for_evaluator_lookup(tmp_path) -> None:
    received_at_ns = int(datetime(2026, 8, 11, tzinfo=UTC).timestamp() * 1_000_000_000)
    writer = MarketTapeWriter(tmp_path)
    writer.append(
        {
            "channel": "l2Book",
            "coin": "xyz:SKHX",
            "exchange_ts_ms": 1_000,
            "received_at_ns": received_at_ns,
            "received_monotonic_ns": 123,
            "observed_event_lag_ms": 1.0,
            "raw_json": "{}",
            "bid_levels_json": "[]",
            "ask_levels_json": "[]",
        }
    )
    paths = writer.flush()

    assert len(paths) == 1
    assert "coin=xyz:SKHX/channel=l2Book" in paths[0].as_posix()
