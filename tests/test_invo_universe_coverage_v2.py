from hlcopy.discovery.invo_universe_job import FILTER_LABELS, PORTFOLIO_FILTERS, _merge_portfolio


def test_live_invo_leaderboard_filter_mapping_is_complete() -> None:
    assert PORTFOLIO_FILTERS == ("trending", "day", "week", "month", "year", "all")
    assert FILTER_LABELS == {
        "trending": "CROWN",
        "day": "1D",
        "week": "1W",
        "month": "1M",
        "year": "1Y",
        "all": "AT",
    }


def test_same_owner_multiple_portfolios_are_not_collapsed() -> None:
    portfolios: dict[str, dict[str, object]] = {}
    _merge_portfolio(
        portfolios,
        {
            "portfolio_id": "main",
            "owner_id": "crypto-rocket-owner",
            "username": "crypto_rocket",
            "name": "Main",
            "closed_positions": 618,
        },
        surface="leaderboard:CROWN",
    )
    _merge_portfolio(
        portfolios,
        {
            "portfolio_id": "swings",
            "owner_id": "crypto-rocket-owner",
            "username": "crypto_rocket",
            "name": "Swings",
            "closed_positions": 21,
        },
        surface="profile:user",
    )
    _merge_portfolio(
        portfolios,
        {
            "portfolio_id": "casino",
            "owner_id": "crypto-rocket-owner",
            "username": "crypto_rocket",
            "name": "Casino",
            "closed_positions": 123,
        },
        surface="profile:user",
    )

    assert set(portfolios) == {"main", "swings", "casino"}
    assert portfolios["main"]["closed_positions"] == 618
    assert portfolios["swings"]["closed_positions"] == 21
    assert portfolios["casino"]["closed_positions"] == 123
