from decimal import Decimal

from hlcopy.resolver.sqd_position_aware import SqdFill, match_episode
from hlcopy.signals.invo import CopySignal

D = Decimal
USER = "0x1111111111111111111111111111111111111111"


def _fill(
    *,
    px: str,
    sz: str,
    time_ms: int,
    direction: str,
    oid: str,
    start_position: str,
) -> SqdFill:
    return SqdFill(
        block_number=1,
        user=USER,
        coin="BTC",
        px=D(px),
        sz=D(sz),
        side="A",
        direction=direction,
        time_ms=time_ms,
        oid=oid,
        closed_pnl=D("0"),
        tid=f"tid-{time_ms}",
        start_position=D(start_position),
    )


def _signal() -> CopySignal:
    return CopySignal(
        signal_id="lifecycle",
        source="generic_closed_trades_csv",
        trader="alice",
        coin="BTC",
        direction="LONG",
        source_leverage=D("2"),
        allocation_fraction=D("0.25"),
        entry_price=D("100"),
        exit_price=D("108"),
        opened_at_ms=1_000_000,
        closed_at_ms=2_000_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="",
        liquidated=False,
        raw={"position_size": "1.0"},
    )


def test_exit_vwap_spans_separate_close_orders_until_final_flatten() -> None:
    fills = [
        _fill(
            px="100",
            sz="1",
            time_ms=1_000_000,
            direction="Open Long",
            oid="open",
            start_position="0",
        ),
        _fill(
            px="120",
            sz="0.4",
            time_ms=1_900_000,
            direction="Close Long",
            oid="close-a",
            start_position="1.0",
        ),
        _fill(
            px="100",
            sz="0.6",
            time_ms=2_000_000,
            direction="Close Long",
            oid="close-b",
            start_position="0.6",
        ),
    ]

    evidence = match_episode(
        _signal(),
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.05"),
        entry_time_tolerance_ms=5_000,
        entry_price_tolerance_bps=D("5"),
    )

    assert evidence.matched
    assert evidence.close_time_error_ms == 0
    assert evidence.close_price_bps == 0
    assert evidence.reconstructed_entry == D("100")
    assert evidence.reconstructed_size == D("1.0")


def test_partial_reduction_does_not_end_episode_before_later_add_and_flatten() -> None:
    fills = [
        _fill(
            px="100",
            sz="1",
            time_ms=1_000_000,
            direction="Open Long",
            oid="open",
            start_position="0",
        ),
        _fill(
            px="110",
            sz="0.5",
            time_ms=1_400_000,
            direction="Close Long",
            oid="reduce",
            start_position="1.0",
        ),
        _fill(
            px="100",
            sz="0.5",
            time_ms=1_500_000,
            direction="Long > Long",
            oid="add",
            start_position="0.5",
        ),
        _fill(
            px="106",
            sz="1.0",
            time_ms=2_000_000,
            direction="Close Long",
            oid="final",
            start_position="1.0",
        ),
    ]
    signal = _signal()
    signal = CopySignal(
        signal_id=signal.signal_id,
        source=signal.source,
        trader=signal.trader,
        coin=signal.coin,
        direction=signal.direction,
        source_leverage=signal.source_leverage,
        allocation_fraction=signal.allocation_fraction,
        entry_price=D("100"),
        exit_price=D("107.3333333333333333333333333"),
        opened_at_ms=signal.opened_at_ms,
        closed_at_ms=signal.closed_at_ms,
        entry_sim=signal.entry_sim,
        last_sim=signal.last_sim,
        reason_closed=signal.reason_closed,
        liquidated=signal.liquidated,
        raw={"position_size": "1.5"},
    )

    evidence = match_episode(
        signal,
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.05"),
        entry_time_tolerance_ms=5_000,
        entry_price_tolerance_bps=D("5"),
    )

    assert evidence.matched
    assert evidence.reconstructed_size == D("1.5")
