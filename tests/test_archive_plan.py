from decimal import Decimal

from hlcopy.copyability.archive_plan import required_l2_objects
from hlcopy.signals.invo import CopySignal

D = Decimal


def test_archive_plan_deduplicates_required_hours():
    signal = CopySignal(
        signal_id="x",
        source="test",
        trader="bones",
        coin="BTC",
        direction="LONG",
        source_leverage=D("40"),
        allocation_fraction=D("0.01"),
        entry_price=D("100"),
        exit_price=D("101"),
        opened_at_ms=1_786_112_000_000,
        closed_at_ms=1_786_115_600_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="user_closed",
        liquidated=False,
        raw={},
    )
    objects = required_l2_objects([signal], latencies_ms=[250, 500, 1000])
    assert len(objects) <= 4
    assert {obj.coin for obj in objects} == {"BTC"}
