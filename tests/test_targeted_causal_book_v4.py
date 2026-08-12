from datetime import UTC, datetime
from decimal import Decimal

import polars as pl

from hlcopy.profitability.causal_book import CausalParquetL2BookProvider


def _levels(px: str) -> str:
    return f'[{ {"px": px, "sz": "10"} }]'.replace("'", '"')


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp() * 1_000_000_000)


def _write_book_day(tmp_path, date: str, coin: str, rows: list[tuple[int, str]]) -> None:
    folder = tmp_path / f"date={date}" / f"coin={coin}" / "channel=l2Book"
    folder.mkdir(parents=True)
    received = [item[0] for item in rows]
    prices = [item[1] for item in rows]
    pl.DataFrame(
        {
            "exchange_ts_ms": [int(value / 1_000_000) - 100 for value in received],
            "received_at_ns": received,
            "bid_levels_json": [_levels(price) for price in prices],
            "ask_levels_json": [_levels(str(Decimal(price) + Decimal("2"))) for price in prices],
        }
    ).write_parquet(folder / "part.parquet")


def test_targeted_resolver_materializes_only_requested_books(tmp_path) -> None:
    first = _ns("2026-08-12T09:00:01")
    second = _ns("2026-08-12T09:00:05")
    third = _ns("2026-08-12T09:00:09")
    _write_book_day(
        tmp_path,
        "2026-08-12",
        "ETH",
        [(first, "99"), (second, "100"), (third, "101")],
    )

    provider = CausalParquetL2BookProvider(tmp_path, max_age_ms=6000)
    target = _ns("2026-08-12T09:00:07")
    provider._resolve_targets("ETH", [target])

    chosen = provider.first_at_or_after("ETH", target / 1_000_000)
    assert chosen is not None
    assert chosen.received_at_ns == second
    assert provider._cache == {}
    assert chosen.bids[0].price == Decimal("100")
    assert len(provider._targeted) == 1


def test_targeted_resolver_rejects_future_and_stale_books(tmp_path) -> None:
    book_ns = _ns("2026-08-12T09:00:05")
    _write_book_day(tmp_path, "2026-08-12", "ETH", [(book_ns, "100")])
    provider = CausalParquetL2BookProvider(tmp_path, max_age_ms=1000)

    before = _ns("2026-08-12T09:00:04")
    stale = _ns("2026-08-12T09:00:07")
    provider._resolve_targets("ETH", [before, stale])

    assert provider.first_at_or_after("ETH", before / 1_000_000) is None
    assert provider.first_at_or_after("ETH", stale / 1_000_000) is None


def test_sparse_targets_skip_irrelevant_date_partitions(monkeypatch, tmp_path) -> None:
    day_one = _ns("2026-08-10T09:00:05")
    middle = _ns("2026-08-11T09:00:05")
    day_three = _ns("2026-08-12T09:00:05")
    _write_book_day(tmp_path, "2026-08-10", "ETH", [(day_one, "100")])
    _write_book_day(tmp_path, "2026-08-11", "ETH", [(middle, "999")])
    _write_book_day(tmp_path, "2026-08-12", "ETH", [(day_three, "102")])

    real_scan = pl.scan_parquet
    scanned: list[str] = []

    def recording_scan(path, *args, **kwargs):
        scanned.append(str(path))
        return real_scan(path, *args, **kwargs)

    monkeypatch.setattr(pl, "scan_parquet", recording_scan)
    provider = CausalParquetL2BookProvider(tmp_path, max_age_ms=6000)
    targets = [
        _ns("2026-08-10T09:00:06"),
        _ns("2026-08-12T09:00:06"),
    ]
    provider._resolve_targets("ETH", targets)

    assert any("date=2026-08-10" in path for path in scanned)
    assert any("date=2026-08-12" in path for path in scanned)
    assert all("date=2026-08-11" not in path for path in scanned)
    assert provider.first_at_or_after("ETH", targets[0] / 1_000_000) is not None
    assert provider.first_at_or_after("ETH", targets[1] / 1_000_000) is not None
