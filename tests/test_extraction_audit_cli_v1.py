from decimal import Decimal

from hlcopy.profitability.extraction_audit_cli import build_audit


def _slice(ts: int, pnl: str, scenario: str) -> dict[str, object]:
    return {
        "lane": "WIDE",
        "wallet_address": "0xabc",
        "coin": "BTC",
        "direction": "LONG",
        "action": "REDUCE",
        "notional_usd": "1000",
        "scenario": scenario,
        "exchange_ts_ms": ts,
        "net_pnl_usd": pnl,
    }


def test_survivor_requires_positive_chronological_oos_all_latencies() -> None:
    rows = []
    for scenario in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS"):
        for i in range(30):
            rows.append(_slice(1_000 + i, "1", scenario))
    audit = build_audit({"realized_slices": rows}, train_fraction=Decimal("0.60"))
    assert audit["survivor_count"] == 1
    row = audit["top_survivors"][0]
    assert row["oos_actions_floor"] == 12
    assert Decimal(row["oos_worst_return_bps"]) > 0


def test_negative_oos_is_dead_even_when_training_positive() -> None:
    rows = []
    for scenario in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS"):
        for i in range(30):
            pnl = "1" if i < 18 else "-1"
            rows.append(_slice(1_000 + i, pnl, scenario))
    audit = build_audit({"realized_slices": rows}, train_fraction=Decimal("0.60"))
    assert audit["survivor_count"] == 0
    assert audit["dead_count"] == 1
    assert audit["all_cohorts"][0]["reason"] == "NON_POSITIVE_OOS_WORST_LATENCY"


def test_small_oos_sample_is_unresolved_not_survivor() -> None:
    rows = []
    for scenario in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS"):
        for i in range(20):
            rows.append(_slice(1_000 + i, "1", scenario))
    audit = build_audit({"realized_slices": rows}, train_fraction=Decimal("0.60"))
    assert audit["unresolved_count"] == 1
    assert audit["survivor_count"] == 0
