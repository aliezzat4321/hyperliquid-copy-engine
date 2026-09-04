from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import REPORT_SCHEMA


class AssumedCostAsNetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DiagnosticSlice:
    name: str
    dimensions: dict[str, Any]
    metrics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PromotableSlice:
    slice_id: str
    trader: str | None
    coin: str | None
    n_closed: int
    n_open_unresolved: int
    n_quarantined: int
    distinct_utc_days: int
    day_clusters: int
    first_open_ts: str | None
    last_open_ts: str | None
    evidence_level: str
    gross_mid_to_mid_pnl_usd: float
    fees_usd: float
    crossing_usd: dict[str, float | None]
    funding_usd: float | None
    net_pnl_usd: float | None
    net_return_bps_trade_weighted: float | None
    net_return_bps_notional_weighted: float | None
    win_rate_net: float | None
    profit_factor_net: float | None
    max_drawdown_usd: float | None
    profit_concentration: float | None
    breakeven_cost_bps_trade_weighted: float | None
    breakeven_cost_bps_notional_weighted: float | None
    unresolved: dict[str, Any]
    latency: dict[str, float | None]
    chase_bps: dict[str, float | None]
    capacity: dict[str, float | None]
    cost_completeness: str
    ci: dict[str, Any]
    p_value_raw: float | None
    p_value_adjusted: float | None
    policy_version: str
    verdict: str
    scenarios: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.cost_completeness != "MEASURED" and (
            self.net_pnl_usd is not None
            or self.net_return_bps_trade_weighted is not None
            or self.net_return_bps_notional_weighted is not None
        ):
            raise AssumedCostAsNetError("unmeasured costs cannot be typed as net")


@dataclass(frozen=True, slots=True)
class Lane3Report:
    generated_at: str
    contract_id: str
    policy_version: str
    reconciliation: dict[str, Any]
    coverage: dict[str, Any]
    slices: list[PromotableSlice]
    diagnostics_non_promotable: list[DiagnosticSlice]
    gross_mid_to_mid_pnl_usd: dict[str, Any]
    schema_version: str = REPORT_SCHEMA

    def __post_init__(self) -> None:
        required = {"value", "is_headline", "warning"}
        if set(self.gross_mid_to_mid_pnl_usd) != required:
            raise ValueError("gross metric requires value/is_headline/warning")
        if self.gross_mid_to_mid_pnl_usd["is_headline"] is not False:
            raise ValueError("gross metric cannot be the headline")

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if "gross_pnl_usd" in result:
            raise AssertionError("bare gross_pnl_usd is forbidden")
        return result
