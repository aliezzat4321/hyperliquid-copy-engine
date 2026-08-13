from __future__ import annotations

import json
from pathlib import Path

from hlcopy.research.selective_policy_publisher import publish_policy_from_attribution
from hlcopy.shadow.selective_policy import load_policy_store


def _cohort(wallet: str, coin: str, edge: str, actions: int, notional: str = "1000") -> dict:
    stats = {
        name: {
            "actions": actions,
            "win_pct": "75",
            "return_bps": edge,
            "last_exchange_ts_ms": 1_000,
        }
        for name in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")
    }
    return {
        "lane": "WIDE",
        "wallet_address": wallet,
        "coin": coin,
        "direction": "LONG",
        "action": "REDUCE",
        "notional_usd": notional,
        "latency_complete": True,
        "robust_return_bps": edge,
        "robust_actions_floor": actions,
        "robust_win_pct_floor": "75",
        "scenario_stats": stats,
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "mode": "DESCRIPTIVE_RESEARCH_ONLY_NO_AUTOMATIC_FILTER_PROMOTION",
                "real_trading": False,
                "generated_from": "2026-08-14T00:00:00+00:00",
                "ranked_complete_cohorts": rows,
            }
        ),
        encoding="utf-8",
    )


def test_publisher_creates_future_only_coin_lifecycle_rule(tmp_path: Path) -> None:
    attribution = tmp_path / "attribution.json"
    store = tmp_path / "policies.json"
    _write(attribution, [_cohort("0xabc", "BTC", "120", 10, "5000")])

    result = publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=2_000_000_000,
    )

    assert result.published is True
    loaded = load_policy_store(store)
    policy = loaded.policies[-1]
    assert policy.training_end_ns == 1_000_000_000
    assert policy.effective_from_ns == 2_000_000_000
    assert policy.research_only is True
    assert len(policy.rules) == 1
    rule = policy.rules[0]
    assert rule.wallet_address == "0xabc"
    assert rule.coin == "BTC"
    assert rule.direction is None
    assert rule.action is None
    assert rule.state == "SHADOW_ONLY"


def test_publisher_blocks_conflicted_coin(tmp_path: Path) -> None:
    attribution = tmp_path / "attribution.json"
    store = tmp_path / "policies.json"
    _write(
        attribution,
        [
            _cohort("0xabc", "BTC", "120", 10),
            _cohort("0xabc", "BTC", "-50", 10),
        ],
    )
    result = publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=2_000_000_000,
    )
    assert result.published is False
    assert result.reason == "NO_QUALIFYING_RULES"


def test_publisher_is_additive_and_idempotent(tmp_path: Path) -> None:
    attribution = tmp_path / "attribution.json"
    store = tmp_path / "policies.json"
    _write(attribution, [_cohort("0xabc", "BTC", "120", 10)])
    first = publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=2_000_000_000,
    )
    assert first.published is True

    same = publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=3_000_000_000,
    )
    assert same.published is False
    assert same.reason == "UNCHANGED_RULE_SET"

    _write(
        attribution,
        [
            _cohort("0xabc", "BTC", "120", 10),
            _cohort("0xdef", "ETH", "90", 12),
        ],
    )
    second = publish_policy_from_attribution(
        attribution_path=attribution,
        policy_store_path=store,
        now_ns=4_000_000_000,
    )
    assert second.published is True
    assert second.newly_added_rules == 1
    loaded = load_policy_store(store)
    assert len(loaded.policies[-1].rules) == 2
