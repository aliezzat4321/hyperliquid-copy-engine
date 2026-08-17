from pathlib import Path

import polars as pl

from hlcopy.profitability.path_inputs import load_asset_context_marks


def _write_mark(path: Path, *, coin: str, received_at_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "coin": [coin],
            "received_at_ns": [received_at_ns],
            "mark_px": [100.0],
            "oracle_px": [100.0],
        }
    ).write_parquet(path)


def test_market_loader_prunes_unrelated_coin_and_date_partitions(tmp_path: Path) -> None:
    root = tmp_path / "market"
    wanted_ts = 1_786_665_000_000_000_000
    wanted = (
        root
        / "date=2026-08-13"
        / "coin=xyz:HYUNDAI"
        / "channel=activeAssetCtx"
        / "wanted.parquet"
    )
    _write_mark(wanted, coin="xyz:HYUNDAI", received_at_ns=wanted_ts)

    unrelated_coin = (
        root
        / "date=2026-08-13"
        / "coin=BTC"
        / "channel=activeAssetCtx"
        / "corrupt.parquet"
    )
    unrelated_coin.parent.mkdir(parents=True, exist_ok=True)
    unrelated_coin.write_text("not parquet", encoding="utf-8")

    unrelated_date = (
        root
        / "date=2026-08-12"
        / "coin=xyz:HYUNDAI"
        / "channel=activeAssetCtx"
        / "corrupt.parquet"
    )
    unrelated_date.parent.mkdir(parents=True, exist_ok=True)
    unrelated_date.write_text("not parquet", encoding="utf-8")

    rows = load_asset_context_marks(
        root,
        coins=("XYZ:HYUNDAI",),
        start_ns=wanted_ts,
        end_ns=wanted_ts,
    )

    assert len(rows) == 1
    assert rows[0].coin == "XYZ:HYUNDAI"
    assert rows[0].received_at_ns == wanted_ts
