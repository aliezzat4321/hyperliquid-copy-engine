from decimal import Decimal as D

import pytest

from hlcopy.lane3.costs import CostCompleteness, LegCost, position_economics
from hlcopy.lane3.reconstruction import ExecutionLeg, ReconstructedPosition
from hlcopy.lane3.report import AssumedCostAsNetError, PromotableSlice


def test_multileg_fees_and_no_mid_fill():
    entries = [ExecutionLeg(1, D("100"), D("1"), D("100"), "ENTRY"),
               ExecutionLeg(2, D("110"), D("1"), D("110"), "ENTRY")]
    exit_leg = ExecutionLeg(3, D("120"), D("2"), D("240"), "EXIT")
    position = ReconstructedPosition("b", "a", "ETH", "long", entries, exit_leg)
    costs = [LegCost(leg, leg.notional * D(".00045"), None, None, None, None,
                     CostCompleteness.UNMEASURED_NO_BOOK) for leg in entries + [exit_leg]]
    result = position_economics(position, costs, funding_usd=D("0"), funding_measured=True)
    assert result.entry_fees_usd == D(".0945")
    assert result.exit_fee_usd == D(".108")
    assert result.net_pnl_usd is None


def test_assumed_cost_cannot_be_net():
    values = dict(slice_id="x", trader=None, coin=None, n_closed=1, n_open_unresolved=0,
                  n_quarantined=0, distinct_utc_days=1, day_clusters=1, first_open_ts=None,
                  last_open_ts=None, evidence_level="RETROSPECTIVE", gross_mid_to_mid_pnl_usd=1,
                  fees_usd=1, crossing_usd={}, funding_usd=0, net_pnl_usd=1,
                  net_return_bps_trade_weighted=None, net_return_bps_notional_weighted=None,
                  win_rate_net=None, profit_factor_net=None, max_drawdown_usd=None,
                  profit_concentration=None, breakeven_cost_bps_trade_weighted=None,
                  breakeven_cost_bps_notional_weighted=None, unresolved={}, latency={},
                  chase_bps={},
                  capacity={}, cost_completeness="SCENARIO_ONLY", ci={}, p_value_raw=None,
                  p_value_adjusted=None, policy_version="v2", verdict="NOT_PROMOTABLE")
    with pytest.raises(AssumedCostAsNetError):
        PromotableSlice(**values)
