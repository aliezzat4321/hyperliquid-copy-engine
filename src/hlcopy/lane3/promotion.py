from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .report import DiagnosticSlice, PromotableSlice

FORBIDDEN_SLICE_FIELDS = frozenset(
    {"held_ms", "add_count", "gross_pnl_usd", "net_pnl_usd", "outcome"}
)


@dataclass(frozen=True, slots=True)
class PromotionVerdict:
    verdict: str
    policy_version: str
    failed_gates: tuple[str, ...]


def validate_slice_spec(spec: dict[str, object]) -> None:
    forbidden = FORBIDDEN_SLICE_FIELDS.intersection(spec)
    if forbidden:
        raise TypeError(f"post-outcome fields cannot define promotable slices: {sorted(forbidden)}")


def evaluate_promotion(
    item: PromotableSlice, *, policy_path: Path, reconciliation_ok: bool,
    prospective: bool, true_orphan_share: float, signs_agree: bool,
) -> PromotionVerdict:
    if isinstance(item, DiagnosticSlice) or not isinstance(item, PromotableSlice):
        raise TypeError("promotion accepts PromotableSlice only")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    thresholds = policy["thresholds"]
    failed: list[str] = []
    checks = (
        ("G1_RECONCILIATION", reconciliation_ok),
        ("G2_COSTS", item.cost_completeness == "MEASURED" and item.funding_usd is not None),
        ("G3_SAMPLE", item.n_closed >= thresholds["min_closed_trades"]),
        ("G4_DAYS", item.distinct_utc_days >= thresholds["min_distinct_days"]),
        ("G5_CLUSTERS", item.day_clusters >= 10),
        ("G6_UNRESOLVED", _unresolved_ok(item, thresholds["max_unresolved_share"])),
        (
            "G7_CONCENTRATION",
            item.profit_concentration is not None
            and item.profit_concentration <= thresholds["max_profit_concentration"],
        ),
        # Costs are already inside NET. reference_round_trip_cost_bps is deliberately absent.
        ("G8_NET_LOWER_BOUND", item.ci.get("lower") is not None and item.ci["lower"] > 0),
        (
            "G9_MULTIPLE_TESTING",
            item.p_value_adjusted is not None and item.p_value_adjusted <= 0.10,
        ),
        ("G10_WEIGHTING_SIGN", signs_agree),
        ("G11_CAPACITY", (item.capacity.get("legs_complete_share") or 0) >= 0.95),
        ("G12_ORPHANS", true_orphan_share <= 0.05),
        ("G13_PROSPECTIVE", prospective),
        ("G14_POLICY_VERSION", item.policy_version == policy["policy_version"]),
    )
    failed.extend(name for name, passed in checks if not passed)
    return PromotionVerdict("PROMOTABLE" if not failed else "NOT_PROMOTABLE",
                            policy["policy_version"], tuple(failed))


def _unresolved_ok(item: PromotableSlice, maximum: float) -> bool:
    total_count = item.n_closed + item.n_open_unresolved
    count_share = item.n_open_unresolved / total_count if total_count else 1.0
    closed_notional = float(item.unresolved.get("closed_notional_usd") or 0)
    open_notional = float(item.unresolved.get("notional_usd") or 0)
    total_notional = closed_notional + open_notional
    notional_share = open_notional / total_notional if total_notional else 1.0
    return count_share <= maximum and notional_share <= maximum
