from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from hlcopy.shadow.registry import MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP, WalletRegistry

POLICY_VERSION = "shadow-cohort-v1"


@dataclass(frozen=True, slots=True)
class CohortPolicy:
    max_validation_wallets: int = 6
    min_trade_count: int = 25
    min_composite_score: float = 45.0
    min_copyability_score: float = 70.0
    min_confidence_score: float = 30.0
    min_risk_score: float = 30.0
    min_profit_factor: float = 1.25
    min_expectancy: float = 0.0
    min_trades_per_day: float = 0.04
    max_asset_concentration: float = 0.80
    max_fast_trade_fraction: float = 0.25
    disallowed_flags: tuple[str, ...] = (
        "LOW_SAMPLE",
        "RECENT_RECONSTRUCTED_LOSS",
        "FAST_ALPHA",
        "MAKER_HEAVY",
    )

    def __post_init__(self) -> None:
        if not 1 <= self.max_validation_wallets <= MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP:
            raise ValueError("max_validation_wallets exceeds safe per-IP cohort size")


@dataclass(frozen=True, slots=True)
class CohortCandidate:
    address: str
    rank: int
    composite_score: float
    copyability_score: float
    confidence_score: float
    trade_count: int
    warning_flags: str
    selected: bool
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CohortApplyResult:
    selected: tuple[CohortCandidate, ...]
    promoted_ids: tuple[str, ...]
    already_validation_ids: tuple[str, ...]
    remaining_capacity: int


def _f(row: dict[str, object], key: str) -> float:
    value = row.get(key)
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _i(row: dict[str, object], key: str) -> int:
    value = row.get(key)
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def evaluate_row(row: dict[str, object], policy: CohortPolicy) -> CohortCandidate:
    flags = str(row.get("warning_flags") or "")
    flag_set = {flag.strip() for flag in flags.split(",") if flag.strip()}
    reasons: list[str] = []
    checks = (
        (_i(row, "trade_count") >= policy.min_trade_count, "LOW_TRADE_COUNT"),
        (_f(row, "composite_score") >= policy.min_composite_score, "LOW_COMPOSITE"),
        (_f(row, "copyability_score") >= policy.min_copyability_score, "LOW_COPYABILITY"),
        (_f(row, "confidence_score") >= policy.min_confidence_score, "LOW_CONFIDENCE"),
        (_f(row, "risk_score") >= policy.min_risk_score, "LOW_RISK_SCORE"),
        (_f(row, "profit_factor") >= policy.min_profit_factor, "LOW_PROFIT_FACTOR"),
        (_f(row, "expectancy") > policy.min_expectancy, "NON_POSITIVE_EXPECTANCY"),
        (_f(row, "month_roi") > 0, "NON_POSITIVE_MONTH"),
        (_f(row, "all_time_roi") > 0, "NON_POSITIVE_ALL_TIME"),
        (_f(row, "trades_per_day") >= policy.min_trades_per_day, "TOO_INACTIVE"),
        (
            _f(row, "asset_concentration") <= policy.max_asset_concentration,
            "TOO_CONCENTRATED",
        ),
        (
            _f(row, "fast_trade_fraction") <= policy.max_fast_trade_fraction,
            "TOO_FAST_FOR_COPYING",
        ),
    )
    for passed, reason in checks:
        if not passed:
            reasons.append(reason)
    disallowed = sorted(flag_set & set(policy.disallowed_flags))
    reasons.extend(f"FLAG_{flag}" for flag in disallowed)
    return CohortCandidate(
        address=str(row.get("address") or "").lower(),
        rank=_i(row, "rank"),
        composite_score=_f(row, "composite_score"),
        copyability_score=_f(row, "copyability_score"),
        confidence_score=_f(row, "confidence_score"),
        trade_count=_i(row, "trade_count"),
        warning_flags=flags,
        selected=not reasons,
        rejection_reasons=tuple(reasons),
    )


def plan_cohort(parquet_path: Path, policy: CohortPolicy) -> tuple[CohortCandidate, ...]:
    frame = pl.read_parquet(parquet_path)
    if frame.is_empty():
        return ()
    rows = frame.sort("rank").to_dicts()
    return tuple(evaluate_row(row, policy) for row in rows)


def apply_cohort(
    *,
    parquet_path: Path,
    registry: WalletRegistry,
    policy: CohortPolicy,
) -> CohortApplyResult:
    registry.init()
    plan = plan_cohort(parquet_path, policy)
    wallets = registry.load()
    by_address = {
        wallet.source_ref.lower(): wallet
        for wallet in wallets
        if wallet.source_type == "hyperliquid_wallet"
    }
    active = [
        wallet
        for wallet in wallets
        if wallet.enabled
        and wallet.source_type == "hyperliquid_wallet"
        and wallet.stage in {"validation", "approved"}
    ]
    capacity = max(0, policy.max_validation_wallets - len(active))
    promoted: list[str] = []
    already: list[str] = []
    chosen = [candidate for candidate in plan if candidate.selected]

    for candidate in chosen:
        wallet = by_address.get(candidate.address)
        if wallet is None:
            continue
        if wallet.stage in {"validation", "approved"}:
            already.append(wallet.id)
            continue
        if wallet.stage != "research" or not wallet.enabled or capacity <= 0:
            continue
        note = (
            f"{wallet.notes}; {POLICY_VERSION} shadow-only promotion from "
            f"{parquet_path.name}; rank={candidate.rank} "
            f"composite={candidate.composite_score:.2f} "
            f"copyability={candidate.copyability_score:.2f} "
            f"confidence={candidate.confidence_score:.2f}"
        ).strip("; ")
        registry.update(wallet.id, stage="validation", notes=note)
        promoted.append(wallet.id)
        capacity -= 1

    return CohortApplyResult(
        selected=tuple(chosen),
        promoted_ids=tuple(promoted),
        already_validation_ids=tuple(already),
        remaining_capacity=capacity,
    )
