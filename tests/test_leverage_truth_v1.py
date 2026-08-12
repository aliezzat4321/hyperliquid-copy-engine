from decimal import Decimal

from hlcopy.profitability.leverage_truth import leverage_matrix

D = Decimal


def test_leverage_changes_equity_return_not_underlying_pnl() -> None:
    summary = {
        "lane": "WIDE",
        "wallet_address": "0xabc",
        "scenario": "LIVE_250MS",
        "notional_usd": "10000",
        "peak_concurrent_gross_notional_usd": "10000",
        "closed_net_pnl_usd": "300",
        "realized_actions": 20,
    }
    rows = leverage_matrix(summary, [D("1"), D("5"), D("40")])
    assert [row["net_pnl_usd"] for row in rows] == ["300", "300", "300"]
    assert rows[0]["equity_required_usd"] == "10000"
    assert rows[1]["equity_required_usd"] == "2000"
    assert rows[2]["equity_required_usd"] == "250"
    assert D(str(rows[0]["net_equity_return_pct"])) == D("3")
    assert D(str(rows[1]["net_equity_return_pct"])) == D("15")
    assert D(str(rows[2]["net_equity_return_pct"])) == D("120")
    assert rows[0]["research_only"] is True
    assert rows[1]["research_only"] is True
    assert rows[2]["liquidation_path_mode"] == "NOT_MODELED_BLOCKS_LIVE_APPROVAL"


def test_leverage_fails_closed_without_portfolio_peak_exposure() -> None:
    summary = {
        "notional_usd": "10000",
        "closed_net_pnl_usd": "300",
        "realized_actions": 20,
    }
    assert leverage_matrix(summary, [D("5")]) == []


def test_overlapping_portfolio_exposure_reduces_roe() -> None:
    summary = {
        "notional_usd": "10000",
        "peak_concurrent_gross_notional_usd": "20000",
        "closed_net_pnl_usd": "300",
        "realized_actions": 20,
    }
    row = leverage_matrix(summary, [D("5")])[0]
    assert D(str(row["equity_required_usd"])) == D("4000")
    assert D(str(row["net_equity_return_pct"])) == D("7.5")
