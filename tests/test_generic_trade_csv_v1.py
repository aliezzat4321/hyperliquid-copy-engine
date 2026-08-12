from __future__ import annotations

from pathlib import Path

import pytest

from hlcopy.signals.generic_csv import GenericTradeCsvError, load_generic_closed_trades


def test_generic_loader_autodetects_bones_style_columns(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    path.write_text(
        "trade_id,username,ticker,direction,leverage,entry_price,closing_price,entry_size,opened_at,closed_at,is_liquidated\n"
        "abc,bones,BTC,LONG,5,100000,101000,20,2026-08-01T10:00:00Z,2026-08-01T11:00:00Z,false\n"
        "def,bones,ETH,SHORT,3,3500,3400,0.15,2026-08-02T10:00:00Z,2026-08-02T11:00:00Z,false\n",
        encoding="utf-8",
    )

    result = load_generic_closed_trades(path)

    assert len(result.signals) == 2
    assert result.rejected_rows == ()
    assert result.column_map["coin"] == "ticker"
    assert result.column_map["exit_price"] == "closing_price"
    assert result.signals[0].source == "generic_closed_trades_csv"
    assert result.signals[0].direction == "LONG"
    assert str(result.signals[0].allocation_fraction) == "0.2"
    assert result.signals[1].direction == "SHORT"
    assert str(result.signals[1].allocation_fraction) == "0.15"


def test_generic_loader_accepts_explicit_position_side_header(tmp_path: Path) -> None:
    path = tmp_path / "other.csv"
    path.write_text(
        "id,symbol,position_side,avg_entry_price,avg_exit_price,start_time,end_time\n"
        "1,SOL,LONG,150,155,2026-08-01T10:00:00Z,2026-08-01T10:30:00Z\n",
        encoding="utf-8",
    )

    result = load_generic_closed_trades(path)

    assert len(result.signals) == 1
    signal = result.signals[0]
    assert signal.signal_id == "1"
    assert signal.coin == "SOL"
    assert signal.direction == "LONG"
    assert str(signal.source_leverage) == "1"
    assert str(signal.allocation_fraction) == "1"


def test_generic_loader_rejects_ambiguous_side_only_schema(tmp_path: Path) -> None:
    path = tmp_path / "ambiguous.csv"
    path.write_text(
        "id,symbol,side,avg_entry_price,avg_exit_price,start_time,end_time\n"
        "1,SOL,SELL,150,155,2026-08-01T10:00:00Z,2026-08-01T10:30:00Z\n",
        encoding="utf-8",
    )

    with pytest.raises(
        GenericTradeCsvError,
        match="intentionally not treated as position direction",
    ):
        load_generic_closed_trades(path)


def test_generic_loader_fails_when_required_schema_cannot_be_detected(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("foo,bar\n1,2\n", encoding="utf-8")

    with pytest.raises(GenericTradeCsvError, match="auto-detect required trade columns"):
        load_generic_closed_trades(path)
