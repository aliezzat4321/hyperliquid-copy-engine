from datetime import UTC, datetime, timedelta

import polars as pl

from hlcopy.profitability.causal_book import CausalParquetL2BookProvider


def _ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def _levels(px: int) -> str:
    return f'[{ {"px": str(px), "sz": "10"} }]'.replace("'", '"')


def test_windowed_prime_does_not_materialize_full_day(tmp_path) -> None:
    start = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
    count = 10_000
    received = [_ns(start + timedelta(seconds=i)) for i in range(count)]
    folder = tmp_path / "date=2026-08-12" / "coin=ETH" / "channel=l2Book"
    folder.mkdir(parents=True)
    pl.DataFrame(
        {
            "exchange_ts_ms": [int(value / 1_000_000) - 50 for value in received],
            "received_at_ns": received,
            "bid_levels_json": [_levels(100)] * count,
            "ask_levels_json": [_levels(102)] * count,
        }
    ).write_parquet(folder / "part.parquet")

    provider = CausalParquetL2BookProvider(tmp_path, max_age_ms=2000)
    target = received[-1] + 500_000_000
    provider._resolve_targets("ETH", [target])

    chosen = provider.first_at_or_after("ETH", target / 1_000_000)
    assert chosen is not None
    assert chosen.received_at_ns == received[-1]
    assert provider.prime_candidate_rows <= 3
    assert provider.prime_book_rows == 1


def test_windows_merge_only_when_they_overlap(tmp_path) -> None:
    provider = CausalParquetL2BookProvider(tmp_path, max_age_ms=1000)
    assert provider._merge_windows([10_000, 10_500, 20_000], 1000) == [
        (9_000, 10_500),
        (19_000, 20_000),
    ]
