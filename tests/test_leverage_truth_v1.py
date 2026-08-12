from decimal import Decimal

from hlcopy.profitability.leverage_truth import leverage_matrix

D = Decimal


def test_leverage_changes_equity_return_not_underlying_pnl() -> None:
    summary = {
        "lane": "WIDE",
        "wallet_address": "0xabc",
        "scenario": "LIVE_250MS",
        "notional_usd": "10000",
        "closed_net_pnl_usd": "300",
        "realized_actions": 20,
    }
    rows = leverage_matrix(summary, [D("1"), D("5"), D("40")])
    assert [row["net_pnl_usd"] for row in rows] == ["300", "300", "300"]
    assert rows[0]["equity_required_usd"] == "10000"
    assert rows[1]["equity_required_usd"] == "2000"
    assert rows[2]["equity_required_usd"] == "250"
    assert rows[0]["net_equity_return_pct"] == "3"
    assert rows[1]["net_equity_return_pct"] == "15"
    assert rows[2]["net_equity_return_pct"] == "120"
    assert rows[1]["research_only"] is True
    assert rows[2]["liquidation_path_mode"] == "NOT_MODELED_BLOCKS_LIVE_APPROVAL"
