from decimal import Decimal

import polars as pl

from hlcopy.profitability.causal_book import CausalParquetL2BookProvider


def _levels(px: str) -> str:
    return f'[{ {"px": px, "sz": "10"} }]'.replace("'", '"')


def test_targeted_resolver_materializes_only_requested_books(tmp_path) -> None:
    folder = tmp_path / "date=2026-08-12" / "coin=ETH" / "channel=l2Book"
    folder.mkdir(parents=True)
    pl.DataFrame(
        {
            "exchange_ts_ms": [1000, 5000, 9000],
            "received_at_ns": [1_100_000_000, 5_100_000_000, 9_100_000_000],
            "bid_levels_json": [_levels("99"), _levels("100"), _levels("101")],
            "ask_levels_json": [_levels("101"), _levels("102"), _levels("103")],
        }
    ).write_parquet(folder / "part.parquet")

    provider = CausalParquetL2BookProvider(tmp_path, max_age_ms=6000)
    provider._resolve_targets("ETH", [7_000_000_000])

    chosen = provider.first_at_or_after("ETH", 7000)
    assert chosen is not None
    assert chosen.exchange_ts_ms == 5000
    assert chosen.received_at_ns == 5_100_000_000
    assert provider._cache == {}
    assert chosen.bids[0].price == Decimal("100")
