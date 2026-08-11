from __future__ import annotations

import json
from pathlib import Path

from hlcopy.market.tape import MarketTapeWriter
from hlcopy.shadow.evaluator import ParquetL2BookProvider


def test_l2_provider_reads_hip3_coin_from_sanitized_partition(tmp_path: Path) -> None:
    writer = MarketTapeWriter(tmp_path)
    writer.append(
        {
            "channel": "l2Book",
            "coin": "XYZ:SNDK",
            "exchange_ts_ms": 1_700_000_000_000,
            "received_at_ns": 1_700_000_000_100_000_000,
            "received_monotonic_ns": 123,
            "observed_event_lag_ms": 100.0,
            "raw_json": "{}",
            "bid_levels_json": json.dumps([{"px": "99", "sz": "10"}]),
            "ask_levels_json": json.dumps([{"px": "101", "sz": "10"}]),
            "best_bid_px": 99.0,
            "best_ask_px": 101.0,
            "mid_px": 100.0,
            "spread_bps": 200.0,
            "bbo_imbalance": 0.0,
            "microprice": 100.0,
            "bid_depth_usd_5bps": 0.0,
            "ask_depth_usd_5bps": 0.0,
            "depth_imbalance_5bps": 0.0,
            "bid_depth_usd_10bps": 0.0,
            "ask_depth_usd_10bps": 0.0,
            "depth_imbalance_10bps": 0.0,
        }
    )
    written = writer.flush()
    assert written
    assert "coin=XYZ_SNDK" in str(written[0])

    provider = ParquetL2BookProvider(tmp_path)
    book = provider.first_at_or_after("XYZ:SNDK", 1_700_000_000_000)

    assert book is not None
    assert book.coin == "XYZ:SNDK"
    assert book.mid == 100
