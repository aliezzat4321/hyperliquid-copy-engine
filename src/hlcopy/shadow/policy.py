from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationEvidence:
    source_id: str
    prospective: bool
    completed_trades: int
    observed_days: float
    execution_attempts: int
    executed_trades: int
    avg_net_return_pct: float
    median_net_return_pct: float
    mean_return_lower_bound_pct: float
    profit_factor: float
    max_drawdown_pct: float
    worst_trade_pct: float
    p95_signal_feed_lag_ms: float
    market_gap_fraction: float
    avg_net_return_1s_pct: float | None
    avg_net_return_5s_pct: float | None
    funding_modeled: bool
    liquidation_path_modeled: bool
    evidence_fingerprint: str

    @property
    def execution_fraction(self) -> float:
        if self.execution_attempts <= 0:
            return 0.0
        return self.executed_trades / self.execution_attempts


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """Conservative eligibility gate; it never mutates registry stages itself."""

    min_completed_trades: int = 30
    min_observed_days: float = 7.0
    min_execution_fraction: float = 0.90
    min_profit_factor: float = 1.20
    max_market_gap_fraction: float = 0.01
    max_p95_signal_feed_lag_ms: float = 2_000.0
    require_positive_median: bool = True
    require_positive_lower_bound: bool = True
    require_positive_1s_stress: bool = True
    require_funding: bool = True
    require_liquidation_path: bool = True


DEFAULT_VALIDATION_POLICY = ValidationPolicy()


@dataclass(frozen=True, slots=True)
class ValidationDecision:
    source_id: str
    status: str
    eligible_for_human_approval: bool
    failures: tuple[str, ...]
    evidence_fingerprint: str


def evaluate_validation(
    evidence: ValidationEvidence,
    policy: ValidationPolicy | None = None,
) -> ValidationDecision:
    policy = policy or DEFAULT_VALIDATION_POLICY
    failures: list[str] = []
    if not evidence.prospective:
        failures.append("NOT_PROSPECTIVE")
    if evidence.completed_trades < policy.min_completed_trades:
        failures.append("INSUFFICIENT_TRADES")
    if evidence.observed_days < policy.min_observed_days:
        failures.append("INSUFFICIENT_LIVE_DAYS")
    if evidence.execution_fraction < policy.min_execution_fraction:
        failures.append("LOW_EXECUTION_RATE")
    if evidence.avg_net_return_pct <= 0:
        failures.append("NONPOSITIVE_AVG_NET_RETURN")
    if policy.require_positive_median and evidence.median_net_return_pct <= 0:
        failures.append("NONPOSITIVE_MEDIAN_NET_RETURN")
    if policy.require_positive_lower_bound and evidence.mean_return_lower_bound_pct <= 0:
        failures.append("UNCERTAINTY_INCLUDES_NONPOSITIVE_MEAN")
    if evidence.profit_factor < policy.min_profit_factor:
        failures.append("LOW_PROFIT_FACTOR")
    if evidence.market_gap_fraction > policy.max_market_gap_fraction:
        failures.append("MARKET_DATA_GAPS")
    if evidence.p95_signal_feed_lag_ms > policy.max_p95_signal_feed_lag_ms:
        failures.append("SIGNAL_FEED_TOO_SLOW")
    if policy.require_positive_1s_stress and (
        evidence.avg_net_return_1s_pct is None or evidence.avg_net_return_1s_pct <= 0
    ):
        failures.append("FAILS_1S_LATENCY_STRESS")
    if policy.require_funding and not evidence.funding_modeled:
        failures.append("FUNDING_NOT_MODELED")
    if policy.require_liquidation_path and not evidence.liquidation_path_modeled:
        failures.append("LIQUIDATION_PATH_NOT_MODELED")

    eligible = not failures
    return ValidationDecision(
        source_id=evidence.source_id,
        status="ELIGIBLE_FOR_HUMAN_APPROVAL" if eligible else "CONTINUE_VALIDATION",
        eligible_for_human_approval=eligible,
        failures=tuple(failures),
        evidence_fingerprint=evidence.evidence_fingerprint,
    )
