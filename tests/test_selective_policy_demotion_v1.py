import json
from decimal import Decimal
from pathlib import Path

from hlcopy.research.selective_policy_publisher import publish_policy_from_attribution
from hlcopy.shadow.selective_policy import load_policy_store


def _row(
    *,
    edge_bps: str,
    actions: int = 10,
    win_pct: str = "60",
    wallet: str = "0xabc",
    coin: str = "BTC",
    notional: str = "1000",
    last_ms: int = 1000,
) -> dict[str, object]:
    return {
        "wallet_address": wallet,
        "coin": coin,
        "notional_usd": notional,
        "robust_return_bps": edge_bps,
        "robust_actions_floor": actions,
        "robust_win_pct_floor": win_pct,
        "scenario_stats": {"LIVE_1000MS": {"last_exchange_ts_ms": last_ms}},
    }


def _write_attribution(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "real_trading": False,
                "mode": "DESCRIPTIVE_RESEARCH_ONLY_TEST",
                "ranked_complete_cohorts": rows,
            }
        ),
        encoding="utf-8",
    )


def _latest_rule(store_path: Path) -> dict[str, object]:
    store = json.loads(store_path.read_text(encoding="utf-8"))
    return store["policies"][-1]["rules"][0]


def test_new_qualifier_enters_shadow(tmp_path: Path) -> None:
    attribution = tmp_path / "attribution.json"
    store = tmp_path / "policies.json"
    _write_attribution(attribution, [_row(edge_bps="50")])

    result = publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=2_000_000_000,
    )

    assert result.published is True
    assert result.newly_added_rules == 1
    rule = _latest_rule(store)
    assert rule["state"] == "SHADOW_ONLY"
    assert "LIFECYCLE_ACTIVE" in rule["reason_codes"]


def test_mild_decay_watches_then_demotes_without_rewriting_history(tmp_path: Path) -> None:
    attribution = tmp_path / "attribution.json"
    store = tmp_path / "policies.json"

    _write_attribution(attribution, [_row(edge_bps="50", last_ms=1000)])
    publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=2_000_000_000,
    )

    _write_attribution(attribution, [_row(edge_bps="-5", last_ms=1100)])
    watch = publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=3_000_000_000,
    )
    watch_rule = _latest_rule(store)
    assert watch.watch_rules == 1
    assert watch.demoted_rules == 0
    assert watch_rule["state"] == "SHADOW_ONLY"
    assert "LIFECYCLE_WATCH" in watch_rule["reason_codes"]
    assert "DEGRADATION_CYCLES=1" in watch_rule["reason_codes"]

    _write_attribution(attribution, [_row(edge_bps="-5", last_ms=1200)])
    demoted = publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=4_000_000_000,
    )
    demoted_rule = _latest_rule(store)
    assert demoted.demoted_rules == 1
    assert demoted_rule["state"] == "SKIP"
    assert "LIFECYCLE_DEMOTED_PERSISTENT_DECAY" in demoted_rule["reason_codes"]
    assert "DEGRADATION_CYCLES=2" in demoted_rule["reason_codes"]

    policies = load_policy_store(store)
    historical = policies.decide(
        decision_time_ns=2_500_000_000,
        wallet_address="0xabc",
        coin="BTC",
        direction="LONG",
        action="INCREASE",
        notional_usd=Decimal("0"),
    )
    future = policies.decide(
        decision_time_ns=4_500_000_000,
        wallet_address="0xabc",
        coin="BTC",
        direction="LONG",
        action="INCREASE",
        notional_usd=Decimal("0"),
    )
    assert historical.state == "SHADOW_ONLY"
    assert future.state == "SKIP"


def test_hard_negative_demotes_immediately(tmp_path: Path) -> None:
    attribution = tmp_path / "attribution.json"
    store = tmp_path / "policies.json"

    _write_attribution(attribution, [_row(edge_bps="50", last_ms=1000)])
    publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=2_000_000_000,
    )

    _write_attribution(attribution, [_row(edge_bps="-30", last_ms=1100)])
    result = publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=3_000_000_000,
    )

    rule = _latest_rule(store)
    assert result.demoted_rules == 1
    assert rule["state"] == "SKIP"
    assert "LIFECYCLE_DEMOTED_HARD_NEGATIVE" in rule["reason_codes"]


def test_demoted_rule_requires_entry_threshold_to_requalify(tmp_path: Path) -> None:
    attribution = tmp_path / "attribution.json"
    store = tmp_path / "policies.json"

    _write_attribution(attribution, [_row(edge_bps="50", last_ms=1000)])
    publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=2_000_000_000,
    )
    _write_attribution(attribution, [_row(edge_bps="-30", last_ms=1100)])
    publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=3_000_000_000,
    )

    # Positive but below the +25 bps entry threshold must stay demoted.
    _write_attribution(attribution, [_row(edge_bps="10", last_ms=1200)])
    publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=4_000_000_000,
    )
    assert _latest_rule(store)["state"] == "SKIP"

    _write_attribution(attribution, [_row(edge_bps="50", last_ms=1300)])
    result = publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=5_000_000_000,
    )
    rule = _latest_rule(store)
    assert result.published is True
    assert rule["state"] == "SHADOW_ONLY"
    assert "LIFECYCLE_REQUALIFIED" in rule["reason_codes"]
