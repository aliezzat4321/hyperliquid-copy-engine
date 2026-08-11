from decimal import Decimal

from hlcopy.profitability.live_cli import _substrategy, _summary

D = Decimal


def test_summary_reports_fixed_notional_dollar_pnl_and_drawdown() -> None:
    rows = [
        {"coin": "BTC", "direction": "LONG", "feed_ms": 80.0, "net_bps": D("100")},
        {"coin": "BTC", "direction": "LONG", "feed_ms": 120.0, "net_bps": D("-50")},
        {"coin": "ETH", "direction": "SHORT", "feed_ms": 100.0, "net_bps": D("25")},
        {"coin": "SOL", "direction": "LONG", "feed_ms": 90.0, "net_bps": None},
    ]
    summary = _summary(
        lane="DIRECT",
        wallet_id="alpha",
        wallet_address="0x" + "1" * 40,
        scenario="LIVE_250MS",
        notional=D("1000"),
        rows=rows,
    )
    assert summary["executed"] == 3
    assert summary["execution_pct"] == 75.0
    assert D(str(summary["closed_net_pnl_usd"])) == D("7.5")
    assert D(str(summary["avg_net_bps"])) == D("25")
    assert D(str(summary["max_closed_drawdown_usd"])) == D("5")
    assert summary["evidence_tier"] == "EARLY"


def test_substrategy_separates_coin_and_direction() -> None:
    rows = [
        {"coin": "BTC", "direction": "LONG", "feed_ms": 1.0, "net_bps": D("10")},
        {"coin": "BTC", "direction": "LONG", "feed_ms": 1.0, "net_bps": D("20")},
        {"coin": "BTC", "direction": "SHORT", "feed_ms": 1.0, "net_bps": D("-5")},
    ]
    result = _substrategy(rows, D("1000"))
    assert result[0]["coin"] == "BTC"
    assert result[0]["direction"] == "LONG"
    assert result[0]["executed"] == 2
    assert D(str(result[0]["net_pnl_usd"])) == D("3")
