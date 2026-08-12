from decimal import Decimal

from hlcopy.profitability.max_profitability import build_tournament


def _row(
    scenario: str,
    roe: str,
    *,
    edge_bps: str = "100",
    wallet: str = "0xabc",
    leverage: str = "5",
    actions: int = 50,
    execution: str = "80",
) -> dict[str, object]:
    return {
        "wallet_address": wallet,
        "lane": "WIDE",
        "scenario": scenario,
        "notional_usd": "1000",
        "follower_leverage": leverage,
        "realized_actions": actions,
        "execution_pct": execution,
        "net_notional_return_bps": edge_bps,
        "net_equity_return_pct": roe,
        "net_pnl_usd": "100",
        "equity_required_usd": "500",
        "max_closed_drawdown_usd": "-25",
    }


def test_tournament_uses_worst_latency_not_best_case() -> None:
    rows = [
        _row("LIVE_100MS", "40", edge_bps="400"),
        _row("LIVE_250MS", "30", edge_bps="300"),
        _row("LIVE_500MS", "20", edge_bps="200"),
        _row("LIVE_1000MS", "10", edge_bps="100"),
    ]

    result = build_tournament(rows)

    assert result["candidate_count"] == 1
    winner = result["ranked"][0]
    assert Decimal(str(winner["worst_latency_roe_pct"])) == Decimal("10")
    assert Decimal(str(winner["worst_latency_notional_return_bps"])) == Decimal("100")
    assert Decimal(str(winner["best_latency_roe_pct"])) == Decimal("40")
    assert winner["live_eligible"] is False
    assert winner["research_only"] is True


def test_tournament_requires_all_latency_slices() -> None:
    rows = [
        _row("LIVE_100MS", "40"),
        _row("LIVE_250MS", "30"),
        _row("LIVE_500MS", "20"),
    ]

    result = build_tournament(rows)

    assert result["candidate_count"] == 0


def test_evidence_and_execution_break_profitability_ties() -> None:
    strong = [
        _row(scenario, "10", wallet="0xstrong", actions=50, execution="90")
        for scenario in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")
    ]
    weak = [
        _row(scenario, "10", wallet="0xweak", actions=10, execution="20")
        for scenario in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")
    ]

    result = build_tournament(strong + weak)

    assert result["candidate_count"] == 2
    assert result["ranked"][0]["wallet_address"] == "0xstrong"


def test_high_leverage_cannot_beat_stronger_underlying_copy_edge() -> None:
    strong_edge = [
        _row(
            scenario,
            "10",
            edge_bps="250",
            wallet="0xstrong",
            leverage="3",
        )
        for scenario in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")
    ]
    weak_edge_high_leverage = [
        _row(
            scenario,
            "150",
            edge_bps="100",
            wallet="0xlevered",
            leverage="40",
        )
        for scenario in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")
    ]

    result = build_tournament(strong_edge + weak_edge_high_leverage)

    assert result["ranked"][0]["wallet_address"] == "0xstrong"
    assert result["leveraged_exploration"][0]["wallet_address"] == "0xlevered"


def test_primary_ranking_deduplicates_leverage_denominator_variants() -> None:
    rows = []
    for leverage, roe in (("1", "5"), ("5", "25"), ("40", "200")):
        rows.extend(
            _row(scenario, roe, edge_bps="150", leverage=leverage)
            for scenario in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")
        )

    result = build_tournament(rows)

    assert result["candidate_count"] == 3
    assert result["primary_ranked_count"] == 1
    assert result["ranked"][0]["follower_leverage"] == "1"
    assert len(result["leveraged_exploration"]) == 3
    assert result["leveraged_exploration"][0]["follower_leverage"] == "40"


def test_tournament_does_not_cap_high_returns() -> None:
    rows = [
        _row(scenario, "125", wallet="0xmax", leverage="20")
        for scenario in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")
    ]

    result = build_tournament(rows)

    winner = result["leveraged_exploration"][0]
    assert Decimal(str(winner["worst_latency_roe_pct"])) == Decimal("125")
    assert "no arbitrary return ceiling" in str(result["objective"])
