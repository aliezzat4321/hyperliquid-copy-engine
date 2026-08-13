from hlcopy.profitability.selective_path_truth_cli import (
    MIN_FORWARD_AGE_NS,
    _promotion_groups,
)


def _row(scenario: str, *, actions: int = 30, edge_bps: str = "100") -> dict[str, object]:
    return {
        "lane": "WIDE",
        "wallet_address": "0xabc",
        "scenario": scenario,
        "notional_usd": "1000",
        "realized_actions": actions,
        "net_return_bps": edge_bps,
        "evidence_age_ns": MIN_FORWARD_AGE_NS,
        "path_truth_complete": True,
        "safe_leverage": {"max_safe_leverage": "5"},
    }


def _complete_rows() -> list[dict[str, object]]:
    return [
        _row("LIVE_100MS"),
        _row("LIVE_250MS"),
        _row("LIVE_500MS"),
        _row("LIVE_1000MS"),
    ]


def test_promotion_requires_all_forward_gates() -> None:
    result = _promotion_groups(_complete_rows())[0]
    assert result["validated_champion"] is True
    assert result["safe_leverage_floor"] == "5"
    assert result["worst_latency_return_pct"] == "1"


def test_promotion_fails_closed_for_short_evidence() -> None:
    rows = _complete_rows()
    rows[0]["evidence_age_ns"] = MIN_FORWARD_AGE_NS - 1
    result = _promotion_groups(rows)[0]
    assert result["validated_champion"] is False
    assert "INSUFFICIENT_FORWARD_TIME" in result["promotion_blockers"]


def test_promotion_fails_closed_for_negative_stress_scenario() -> None:
    rows = _complete_rows()
    rows[-1]["net_return_bps"] = "-1"
    result = _promotion_groups(rows)[0]
    assert result["validated_champion"] is False
    assert "NON_POSITIVE_WORST_LATENCY_RETURN" in result["promotion_blockers"]
