from types import SimpleNamespace

from hlcopy.profitability.causal_selective_live_cli import _forward_vetoed
from hlcopy.profitability.forward_shadow_veto import evaluate_forward_vetoes


def _row(
    *,
    wallet: str = "0x1081",
    notional: str = "1000",
    actions: int = 85,
    return_bps: str = "-21.195",
    age_hours: str = "16.5",
    blockers: list[str] | None = None,
) -> dict[str, object]:
    return {
        "wallet_address": wallet,
        "notional_usd": notional,
        "realized_actions_floor": actions,
        "worst_latency_return_bps": return_bps,
        "forward_age_hours_floor": age_hours,
        "safe_leverage_floor": None if blockers else "10",
        "promotion_blockers": blockers or [],
    }


def _truth(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "real_trading": False,
        "policy_id": "shadow-test",
        "promotion_candidates": rows,
    }


def _empty() -> dict[str, object]:
    return {
        "mode": "PROSPECTIVE_FORWARD_SHADOW_VETO_V1",
        "real_trading": False,
        "wallet_states": {},
        "veto_intervals": [],
    }


def test_1081_like_path_failure_triggers_immediate_veto() -> None:
    rows = [
        _row(
            notional=n,
            return_bps=r,
            blockers=["INCOMPLETE_PATH_TRUTH", "NO_SAFE_LEVERAGE_ACROSS_SCENARIOS"],
        )
        for n, r in [
            ("1000", "-21.195"),
            ("5000", "-18.562"),
            ("10000", "-17.189"),
            ("25000", "-24.397"),
            ("50000", "-18.648"),
        ]
    ]
    result = evaluate_forward_vetoes(
        path_truth=_truth(rows),
        existing=_empty(),
        now_ns=10_000,
    )

    state = result["wallet_states"]["0x1081"]
    assert state["veto_active"] is True
    assert state["status"] == "FORWARD_EMERGENCY_PATH_SAFETY"
    assert result["active_veto_count"] == 1
    assert result["veto_intervals"] == [
        {
            "wallet_address": "0x1081",
            "coin": "*",
            "effective_from_ns": 10_000,
            "effective_until_ns": None,
            "reason": "FORWARD_EMERGENCY_PATH_SAFETY",
        }
    ]


def test_persistent_all_negative_requires_two_cycles_when_path_is_healthy() -> None:
    rows = [
        _row(notional=n, return_bps="-5", blockers=[])
        for n in ("1000", "5000", "10000", "25000", "50000")
    ]
    first = evaluate_forward_vetoes(
        path_truth=_truth(rows), existing=_empty(), now_ns=10_000
    )
    assert first["wallet_states"]["0x1081"]["status"] == "FORWARD_WATCH_NEGATIVE"
    assert first["wallet_states"]["0x1081"]["veto_active"] is False

    second = evaluate_forward_vetoes(
        path_truth=_truth(rows), existing=first, now_ns=20_000
    )
    assert second["wallet_states"]["0x1081"]["status"] == "FORWARD_EMERGENCY_PERSISTENT_NEGATIVE"
    assert second["wallet_states"]["0x1081"]["veto_active"] is True


def test_veto_is_point_in_time_and_does_not_rewrite_old_events() -> None:
    vetoes = (
        {
            "wallet_address": "0xabc",
            "coin": "*",
            "effective_from_ns": 100,
            "effective_until_ns": 200,
        },
    )
    old = SimpleNamespace(wallet_address="0xAbC", coin="BTC", received_at_ns=99)
    during = SimpleNamespace(wallet_address="0xAbC", coin="BTC", received_at_ns=150)
    after = SimpleNamespace(wallet_address="0xAbC", coin="BTC", received_at_ns=200)

    assert _forward_vetoed(vetoes, old) is False
    assert _forward_vetoed(vetoes, during) is True
    assert _forward_vetoed(vetoes, after) is False


def test_recovery_requires_24h_and_two_healthy_cycles() -> None:
    bad_rows = [
        _row(
            notional=n,
            blockers=["INCOMPLETE_PATH_TRUTH", "NO_SAFE_LEVERAGE_ACROSS_SCENARIOS"],
        )
        for n in ("1000", "5000", "10000")
    ]
    active = evaluate_forward_vetoes(
        path_truth=_truth(bad_rows), existing=_empty(), now_ns=10_000
    )

    healthy_rows = [
        _row(
            notional=n,
            return_bps="25",
            age_hours="25",
            blockers=[],
        )
        for n in ("1000", "5000", "10000")
    ]
    pending = evaluate_forward_vetoes(
        path_truth=_truth(healthy_rows), existing=active, now_ns=20_000
    )
    assert pending["wallet_states"]["0x1081"]["status"] == "FORWARD_RECOVERY_PENDING"
    assert pending["wallet_states"]["0x1081"]["veto_active"] is True

    released = evaluate_forward_vetoes(
        path_truth=_truth(healthy_rows), existing=pending, now_ns=30_000
    )
    assert released["wallet_states"]["0x1081"]["status"] == "FORWARD_VETO_RELEASED"
    assert released["wallet_states"]["0x1081"]["veto_active"] is False
    assert released["veto_intervals"][0]["effective_until_ns"] == 30_000
