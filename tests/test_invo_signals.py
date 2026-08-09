from decimal import Decimal
from pathlib import Path

from hlcopy.signals.invo import load_invo_closed_trades


def test_invo_import_reconstructs_card_return(tmp_path: Path):
    csv_path = tmp_path / "bones.csv"
    csv_path.write_text(
        "trade_id,trader_id,username,trader_name,portfolio_id,ticker,direction,"
        "leverage,entry_price,closing_price,entry_size,entry_sim,last_sim,"
        "is_liquidated,reason_closed,opened_at,closed_at\n"
        "t1,u1,bones,Bones,p1,BTC,SHORT,40,65183,65049,1,1513.70,1638.17,"
        "False,user_closed,2026-08-07T14:27:50.663Z,2026-08-07T14:50:01.743Z\n",
        encoding="utf-8",
    )

    result = load_invo_closed_trades(csv_path)

    assert len(result.signals) == 1
    assert not result.rejected_rows
    signal = result.signals[0]
    assert signal.coin == "BTC"
    assert signal.direction == "SHORT"
    assert signal.allocation_fraction == Decimal("0.01")
    assert signal.source_leverage == Decimal("40")
    assert abs(signal.source_leveraged_return - Decimal("0.08223")) < Decimal("0.00001")


def test_invo_import_filters_coin_direction_and_since(tmp_path: Path):
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        "trade_id,trader_id,username,trader_name,portfolio_id,ticker,direction,"
        "leverage,entry_price,closing_price,entry_size,entry_sim,last_sim,"
        "is_liquidated,reason_closed,opened_at,closed_at\n"
        "a,u,b,b,p,BTC,LONG,10,100,101,1,1,1.1,False,user_closed,"
        "2026-08-01T00:00:00Z,2026-08-01T01:00:00Z\n"
        "b,u,b,b,p,ETH,SHORT,10,100,99,1,1,1.1,False,user_closed,"
        "2026-08-02T00:00:00Z,2026-08-02T01:00:00Z\n",
        encoding="utf-8",
    )
    result = load_invo_closed_trades(
        csv_path,
        coins={"ETH"},
        directions={"SHORT"},
        since_ms=1785628800000,
    )
    assert [signal.signal_id for signal in result.signals] == ["b"]
