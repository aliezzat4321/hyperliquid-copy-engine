from hlcopy.profitability.attribution import build_trade_attribution


def _slice(
    scenario: str,
    *,
    wallet: str = "0xaaa",
    coin: str = "BTC",
    pnl: str = "10",
    action: str = "CLOSE",
    notional: str = "1000",
) -> dict[str, object]:
    return {
        "lane": "WIDE",
        "wallet_address": wallet,
        "coin": coin,
        "direction": "LONG",
        "action": action,
        "scenario": scenario,
        "notional_usd": notional,
        "exchange_ts_ms": 1000,
        "feed_ms": 25,
        "gross_pnl_usd": pnl,
        "fee_usd": "0",
        "net_pnl_usd": pnl,
    }


def test_requires_all_latency_scenarios_for_robust_attribution() -> None:
    result = build_trade_attribution(
        {
            "generated_at": "now",
            "pnl_model": "TEST",
            "realized_slices": [
                _slice("LIVE_100MS"),
                _slice("LIVE_250MS"),
                _slice("LIVE_500MS"),
            ],
        }
    )

    assert result["latency_complete_cohort_count"] == 0
    assert result["ranked_complete_cohorts"] == []
    assert len(result["incomplete_cohorts"]) == 1


def test_robust_return_uses_worst_latency_slice() -> None:
    rows = [
        _slice("LIVE_100MS", pnl="20"),
        _slice("LIVE_250MS", pnl="18"),
        _slice("LIVE_500MS", pnl="15"),
        _slice("LIVE_1000MS", pnl="5"),
    ]
    result = build_trade_attribution(
        {"generated_at": "now", "pnl_model": "TEST", "realized_slices": rows}
    )

    cohort = result["ranked_complete_cohorts"][0]
    assert cohort["robust_return_bps"] == "50.000"
    assert cohort["robust_actions_floor"] == 1


def test_wallets_and_coins_never_mix_into_one_cohort() -> None:
    scenarios = ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")
    rows = []
    for scenario in scenarios:
        rows.append(_slice(scenario, wallet="0xaaa", coin="BTC", pnl="10"))
        rows.append(_slice(scenario, wallet="0xbbb", coin="ETH", pnl="-5"))

    result = build_trade_attribution(
        {"generated_at": "now", "pnl_model": "TEST", "realized_slices": rows}
    )

    assert result["latency_complete_cohort_count"] == 2
    ranked = result["ranked_complete_cohorts"]
    assert ranked[0]["wallet_address"] == "0xaaa"
    assert ranked[0]["coin"] == "BTC"
    assert ranked[1]["wallet_address"] == "0xbbb"
    assert ranked[1]["coin"] == "ETH"
    assert ranked[1]["robust_return_bps"].startswith("-50")


def test_attribution_is_explicitly_research_only() -> None:
    scenarios = ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")
    result = build_trade_attribution(
        {
            "generated_at": "now",
            "pnl_model": "TEST",
            "realized_slices": [_slice(scenario) for scenario in scenarios],
        }
    )

    assert result["real_trading"] is False
    assert result["mode"] == "DESCRIPTIVE_RESEARCH_ONLY_NO_AUTOMATIC_FILTER_PROMOTION"
    assert "chronologically" in result["causal_note"]
