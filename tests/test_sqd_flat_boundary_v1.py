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
        side="B",
        direction=direction,
        time_ms=time_ms,
        oid=oid,
        closed_pnl=D("0"),
        tid=f"tid-{time_ms}",
        start_position=D(start_position),
    )


def _signal(*, opened_at_ms: int = 1_000_000) -> CopySignal:
    return CopySignal(
        signal_id="boundary",
        source="generic_closed_trades_csv",
        trader="alice",
        coin="BTC",
        direction="LONG",
        source_leverage=D("2"),
        allocation_fraction=D("0.25"),
        entry_price=D("100"),
        exit_price=D("110"),
        opened_at_ms=opened_at_ms,
        closed_at_ms=2_000_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="",
        liquidated=False,
        raw={"position_size": "1.0"},
    )


def _match(signal: CopySignal, fills: list[SqdFill]):
    return match_episode(
        signal,
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.10"),
        entry_time_tolerance_ms=300_000,
        entry_price_tolerance_bps=D("5"),
    )


def test_later_add_cannot_replace_observed_flat_to_open_boundary() -> None:
    fills = [
        _fill(
            px="99",
            sz="0.5",
            time_ms=900_000,
            direction="Open Long",
            oid="1",
            start_position="0",
        ),
        _fill(
            px="101",
            sz="0.5",
            time_ms=1_010_000,
            direction="Open Long",
            oid="2",
            start_position="0.5",
        ),
        _fill(
            px="110",
            sz="1",
            time_ms=2_000_000,
            direction="Close Long",
            oid="7",
            start_position="1",
        ),
    ]
    evidence = _match(_signal(), fills)
    assert evidence.matched
    assert evidence.entry_time_error_ms == 100_000
    assert evidence.reconstructed_entry == D("100")
    assert evidence.reconstructed_size == D("1.0")


def test_nonzero_start_position_open_cannot_establish_episode_boundary() -> None:
    fills = [
        _fill(
            px="100",
            sz="1",
            time_ms=1_010_000,
            direction="Open Long",
            oid="2",
            start_position="0.5",
        ),
        _fill(
            px="110",
            sz="1",
            time_ms=2_000_000,
            direction="Close Long",
            oid="7",
            start_position="1.5",
        ),
    ]
    evidence = _match(_signal(), fills)
    assert not evidence.matched
    assert evidence.entry_time_error_ms is None
