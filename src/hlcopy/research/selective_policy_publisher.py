from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import time_ns
from typing import Any

D = Decimal
ZERO = D("0")


@dataclass(frozen=True, slots=True)
class PolicyPublishConfig:
    min_actions: int = 5
    min_robust_return_bps: Decimal = D("25")
    min_win_pct: Decimal = D("40")
    negative_block_bps: Decimal = D("-25")
    max_rules: int = 500


@dataclass(frozen=True, slots=True)
class PolicyPublishResult:
    published: bool
    policy_id: str | None
    rules: int
    newly_added_rules: int
    training_end_ns: int | None
    effective_from_ns: int | None
    reason: str


def _decimal(value: object, default: Decimal = ZERO) -> Decimal:
    try:
        return D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _training_end_ns(rows: list[dict[str, Any]]) -> int | None:
    latest_ms: int | None = None
    for row in rows:
        stats = row.get("scenario_stats")
        if not isinstance(stats, dict):
            continue
        for item in stats.values():
            if not isinstance(item, dict):
                continue
            raw = item.get("last_exchange_ts_ms")
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            latest_ms = value if latest_ms is None else max(latest_ms, value)
    return latest_ms * 1_000_000 if latest_ms is not None else None


def _rule_key(rule: dict[str, object]) -> tuple[str, str]:
    return str(rule["wallet_address"]).lower(), str(rule["coin"])


def _canonical_rules(rules: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(rules, key=lambda row: (_rule_key(row), str(row.get("max_notional_usd") or "")))


def _fingerprint(rules: list[dict[str, object]]) -> str:
    raw = json.dumps(_canonical_rules(rules), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mode": "PROSPECTIVE_SHADOW_ONLY", "policies": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("policies"), list):
        raise ValueError("policy store must contain a policies list")
    return payload


def _existing_rules(store: dict[str, Any]) -> dict[tuple[str, str], dict[str, object]]:
    policies = store.get("policies") or []
    if not policies:
        return {}
    latest = policies[-1]
    rows = latest.get("rules") if isinstance(latest, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[tuple[str, str], dict[str, object]] = {}
    for raw in rows:
        if not isinstance(raw, dict) or raw.get("wallet_address") is None or raw.get("coin") is None:
            continue
        rule = dict(raw)
        out[_rule_key(rule)] = rule
    return out


def _candidate_coin_rules(
    attribution: dict[str, Any],
    *,
    config: PolicyPublishConfig,
) -> list[dict[str, object]]:
    raw_rows = attribution.get("ranked_complete_cohorts")
    rows = [row for row in raw_rows or [] if isinstance(row, dict)]
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        wallet = str(row.get("wallet_address") or "").lower()
        coin = str(row.get("coin") or "")
        if not wallet or not coin:
            continue
        by_key.setdefault((wallet, coin), []).append(row)

    candidates: list[tuple[Decimal, int, dict[str, object]]] = []
    for (wallet, coin), cohort_rows in by_key.items():
        evidenced = [
            row
            for row in cohort_rows
            if int(row.get("robust_actions_floor") or 0) >= config.min_actions
        ]
        if not evidenced:
            continue
        # Do not promote a coin hypothesis when the same wallet/coin already has
        # materially negative evidence in another direction/action at adequate size.
        if any(_decimal(row.get("robust_return_bps")) <= config.negative_block_bps for row in evidenced):
            continue
        positive = [
            row
            for row in evidenced
            if _decimal(row.get("robust_return_bps")) >= config.min_robust_return_bps
            and _decimal(row.get("robust_win_pct_floor")) >= config.min_win_pct
        ]
        if not positive:
            continue
        best = max(
            positive,
            key=lambda row: (
                _decimal(row.get("robust_return_bps")),
                int(row.get("robust_actions_floor") or 0),
            ),
        )
        eligible_notionals = [
            _decimal(row.get("notional_usd"))
            for row in positive
            if _decimal(row.get("notional_usd")) > ZERO
        ]
        if not eligible_notionals:
            continue
        max_notional = max(eligible_notionals)
        edge = _decimal(best.get("robust_return_bps"))
        actions = int(best.get("robust_actions_floor") or 0)
        candidates.append(
            (
                edge,
                actions,
                {
                    "wallet_address": wallet,
                    "state": "SHADOW_ONLY",
                    "coin": coin,
                    "direction": None,
                    "action": None,
                    "max_notional_usd": str(max_notional),
                    "reason_codes": [
                        "DESCRIPTIVE_COHORT_HYPOTHESIS",
                        "FULL_COIN_LIFECYCLE_REQUIRED",
                        f"ROBUST_EDGE_BPS={edge}",
                        f"ROBUST_ACTIONS={actions}",
                    ],
                },
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in candidates[: max(0, config.max_rules)]]


def publish_policy_from_attribution(
    *,
    attribution_path: Path,
    policy_store_path: Path,
    config: PolicyPublishConfig = PolicyPublishConfig(),
    now_ns: int | None = None,
) -> PolicyPublishResult:
    attribution = json.loads(attribution_path.read_text(encoding="utf-8"))
    if attribution.get("real_trading") is not False:
        raise ValueError("attribution must be research-only")
    mode = str(attribution.get("mode") or "")
    if "DESCRIPTIVE_RESEARCH_ONLY" not in mode:
        raise ValueError("unexpected attribution mode")

    rows = [
        row
        for row in attribution.get("ranked_complete_cohorts") or []
        if isinstance(row, dict)
    ]
    training_end_ns = _training_end_ns(rows)
    if training_end_ns is None:
        return PolicyPublishResult(False, None, 0, 0, None, None, "NO_TRAINING_TIMESTAMP")

    effective_from_ns = int(now_ns if now_ns is not None else time_ns())
    if training_end_ns >= effective_from_ns:
        raise ValueError("training data must end strictly before policy activation")

    store = _load_store(policy_store_path)
    cumulative = _existing_rules(store)
    before = len(cumulative)
    for rule in _candidate_coin_rules(attribution, config=config):
        cumulative.setdefault(_rule_key(rule), rule)
    rules = _canonical_rules(list(cumulative.values()))
    added = len(cumulative) - before
    if not rules:
        return PolicyPublishResult(False, None, 0, 0, training_end_ns, None, "NO_QUALIFYING_RULES")

    latest = (store.get("policies") or [])[-1] if store.get("policies") else None
    fingerprint = _fingerprint(rules)
    if isinstance(latest, dict) and latest.get("rule_fingerprint") == fingerprint:
        return PolicyPublishResult(
            False,
            str(latest.get("policy_id") or "") or None,
            len(rules),
            0,
            training_end_ns,
            None,
            "UNCHANGED_RULE_SET",
        )

    policy_id = f"shadow-{effective_from_ns}-{fingerprint[:12]}"
    policy_rules = [
        {
            **rule,
            "policy_id": policy_id,
            "effective_from_ns": effective_from_ns,
            "training_end_ns": training_end_ns,
        }
        for rule in rules
    ]
    policy = {
        "policy_id": policy_id,
        "generated_at_ns": effective_from_ns,
        "effective_from_ns": effective_from_ns,
        "training_end_ns": training_end_ns,
        "research_only": True,
        "rule_fingerprint": fingerprint,
        "source_attribution": str(attribution_path),
        "source_generated_from": attribution.get("generated_from"),
        "rules": policy_rules,
    }
    store.setdefault("policies", []).append(policy)
    store["mode"] = "PROSPECTIVE_SHADOW_ONLY"
    store["real_trading"] = False
    store["latest_policy_id"] = policy_id

    policy_store_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = policy_store_path.with_suffix(policy_store_path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    tmp.replace(policy_store_path)
    return PolicyPublishResult(
        True,
        policy_id,
        len(policy_rules),
        added,
        training_end_ns,
        effective_from_ns,
        "PUBLISHED",
    )
