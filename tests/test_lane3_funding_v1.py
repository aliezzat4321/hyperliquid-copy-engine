from decimal import Decimal as D

from hlcopy.lane3.funding import HOUR_MS, attribute_funding
from hlcopy.lane3.reconstruction import ExecutionLeg, ReconstructedPosition
from hlcopy.profitability.continuous_path_v2 import FundingRate


def test_missing_funding_fails_and_reup_changes_notional():
    legs = [ExecutionLeg(0, D("100"), D("1"), D("100"), "ENTRY"),
            ExecutionLeg(HOUR_MS + 1, D("100"), D("1"), D("100"), "ENTRY")]
    position = ReconstructedPosition("b", "a", "ETH", "long", legs)
    missing = attribute_funding(position, (), end_ms=2 * HOUR_MS)
    assert not missing.measured and missing.funding_usd is None
    rates = (FundingRate("ETH", HOUR_MS, D(".001")), FundingRate("ETH", 2 * HOUR_MS, D(".001")))
    measured = attribute_funding(position, rates, end_ms=2 * HOUR_MS)
    assert measured.funding_usd == D("-.3")
