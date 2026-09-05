from decimal import Decimal as D
from pathlib import Path

from hlcopy.lane3.costs import CostCompleteness, LegCost, position_economics
from hlcopy.lane3.reconstruction import ExecutionLeg, ReconstructedPosition


def test_waterfall_does_not_subtract_copy_decay_or_reference_scenario():
    entry = ExecutionLeg(1, D("100"), D("1"), D("100"), "ENTRY")
    exit_leg = ExecutionLeg(2, D("110"), D("1"), D("110"), "EXIT")
    position = ReconstructedPosition("b", "alice", "ETH", "long", [entry], exit_leg)
    costs = [
        LegCost(entry, D("1"), D("2"), D("1"), D("1"), D("200"),
                CostCompleteness.MEASURED),
        LegCost(exit_leg, D("1"), D("2"), D("1"), D("1"), D("182"),
                CostCompleteness.MEASURED),
    ]
    economics = position_economics(
        position, costs, funding_usd=D(".5"), funding_measured=True
    )
    assert economics.net_pnl_usd == D("4.5")
    source = Path("src/hlcopy/lane3/promotion.py").read_text(encoding="utf-8")
    assert 'thresholds["reference_round_trip_cost_bps"]' not in source
