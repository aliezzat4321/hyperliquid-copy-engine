from hlcopy.discovery.leaderboard import parse_leaderboard, shortlist


def row(address, account, month_roi, month_pnl, month_vlm, all_pnl=1000):
    return {
        "ethAddress": address,
        "accountValue": str(account),
        "displayName": None,
        "windowPerformances": [
            ["day", {"pnl": "1", "roi": "0.01", "vlm": "100"}],
            ["week", {"pnl": "10", "roi": "0.02", "vlm": "1000"}],
            ["month", {"pnl": str(month_pnl), "roi": str(month_roi), "vlm": str(month_vlm)}],
            ["allTime", {"pnl": str(all_pnl), "roi": "0.5", "vlm": "100000"}],
        ],
    }


def test_shortlist_filters_before_expensive_wallet_calls():
    a = "0x" + "1" * 40
    b = "0x" + "2" * 40
    c = "0x" + "3" * 40
    candidates = parse_leaderboard(
        {
            "leaderboardRows": [
                row(a, 50_000, 0.2, 10_000, 1_000_000),
                row(b, 2_000, 2.0, 100_000, 5_000_000),
                row(c, 50_000, -0.1, -1_000, 1_000_000),
            ]
        }
    )
    selected = shortlist(
        candidates,
        limit=10,
        min_account_value=10_000,
        min_month_roi=0,
        min_month_volume=50_000,
    )
    assert [c.address for c in selected] == [a]
